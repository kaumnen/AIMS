import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

import aiodocker
from aiodocker.containers import DockerContainer

from aims.sandbox.base import Denied, Exec, Exit, File, Fork, Lost, Net, Oom, Record
from aims.sandbox.images import ensure_image
from aims.sandbox.trace import (
    Bprm,
    Delete,
    Dropped,
    Garbled,
    Mounts,
    Note,
    RawDenied,
    RawExec,
    Ready,
    Rename,
    parse,
)

log = logging.getLogger("aims.sandbox")

QUEUE = 100_000
MEMORY = 4096
READY_S = 120.0
CONFIRM_S = 5.0


class TracerError(Exception):
    pass


@dataclass(slots=True)
class Route:
    """Kernel records of one sandbox, keyed by its pid namespace."""

    pidns: int
    mounts: Mounts
    syscalls: dict[int, str]
    queue: asyncio.Queue[Record] = field(default_factory=lambda: asyncio.Queue(QUEUE))
    overflow: int = 0

    def put(self, record: Record) -> None:
        if self.overflow:
            try:
                self.queue.put_nowait(Lost(self.overflow, "audit queue full"))
            except asyncio.QueueFull:
                self.overflow += 1
                return
            self.overflow = 0
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.overflow += 1


class Tracer:
    """A privileged bpftrace container that sees every process, file and socket call in sandboxes.

    One per backend. Records reach a sandbox once it registers its pid namespace.
    """

    def __init__(self, docker: aiodocker.Docker, labels: dict[str, str], name: str) -> None:
        self._docker = docker
        self._labels = labels
        self._name = name
        self._container: DockerContainer | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._routes: dict[int, Route] = {}
        self._pids: dict[int, Route] = {}
        self._bprm: OrderedDict[int, str] = OrderedDict()
        self._roots: OrderedDict[int, int] = OrderedDict()
        self._waiters: dict[int, asyncio.Future[int]] = {}
        self.alive = False
        self.garbled = 0

    async def start(self) -> None:
        image = await ensure_image(self._docker, "tracer")
        log.warning(
            "starting %s, a privileged container with host pid access that traces sandbox "
            "processes; use audit 'snapshot' to run without it",
            self._name,
        )
        self._container = await self._docker.containers.create(
            config={
                "Image": image,
                "Labels": {**self._labels, "aims.role": "tracer"},
                "HostConfig": {
                    "Privileged": True,
                    "PidMode": "host",
                    "NetworkMode": "none",
                    "LogConfig": {"Type": "local", "Config": {"max-size": "20m", "max-file": "3"}},
                },
            },
            name=self._name,
        )
        await self._container.start()
        self._reader = asyncio.create_task(self._read(self._container))
        try:
            async with asyncio.timeout(READY_S):
                base = await asyncio.shield(self._ready)
        except BaseException:
            await self.stop()
            raise
        if base != 0:
            await self.stop()
            raise TracerError("the tracer is not in the host pid namespace")
        self.alive = True

    async def stop(self) -> None:
        self.alive = False
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._container is not None:
            with contextlib.suppress(aiodocker.DockerError):
                await self._container.delete(force=True)
            self._container = None

    def register(self, pidns: int, mounts: Mounts, syscalls: dict[int, str]) -> Route:
        route = Route(pidns, mounts, syscalls)
        self._routes[pidns] = route
        return route

    def unregister(self, route: Route) -> None:
        self._routes.pop(route.pidns, None)
        for pid in [p for p, r in self._pids.items() if r is route]:
            del self._pids[pid]

    async def confirm(self, pid: int, pidns: int) -> bool:
        """Whether the tracer saw the runtime start process `pid` in namespace `pidns`."""
        if pid in self._roots:
            return self._roots[pid] == pidns
        future = self._waiters.setdefault(pid, asyncio.get_running_loop().create_future())
        try:
            async with asyncio.timeout(CONFIRM_S):
                return await future == pidns
        except TimeoutError:
            return False
        finally:
            self._waiters.pop(pid, None)

    async def _read(self, container: DockerContainer) -> None:
        pending = ""
        try:
            async for chunk in container.log(stdout=True, stderr=True, follow=True):
                pending += chunk
                *lines, pending = pending.split("\n")
                for line in lines:
                    self._handle(line)
        except Exception as e:
            log.error("tracer stream failed: %s", e)
        finally:
            if not self._ready.done():
                self._ready.set_exception(TracerError("the tracer stopped before it was ready"))
            if self.alive:
                log.error("tracer stopped; sandboxes fall back to snapshot audit")
            self.alive = False
            for route in self._routes.values():
                route.put(Lost(None, "tracer stopped"))

    def _handle(self, line: str) -> None:
        item = parse(line)
        match item:
            case None:
                return
            case Ready(base):
                if not self._ready.done():
                    self._ready.set_result(base)
            case Note(text):
                log.warning("tracer: %s", text)
                if not self._ready.done() and "ERROR" in text:
                    self._ready.set_exception(TracerError(text))
            case Dropped(count):
                for route in self._routes.values():
                    route.put(Lost(count, "kernel buffer overflow"))
            case Garbled(pid, text):
                self.garbled += 1
                route = self._pids.get(pid) if pid is not None else None
                if route is not None:
                    route.put(Lost(1, f"unparseable tracer line: {text[:500]}"))
            case Fork(parent, child):
                route = self._pids.get(parent)
                if route is not None:
                    self._pids[child] = route
                    route.put(item)
            case Bprm(pid, path):
                self._bprm[pid] = path
                if len(self._bprm) > MEMORY:
                    self._bprm.popitem(last=False)
            case RawExec():
                self._exec(item)
            case Exit(pid):
                self._bprm.pop(pid, None)
                route = self._pids.pop(pid, None)
                if route is not None:
                    route.put(item)
            case Oom(pid) | File(pid) | Net(pid):
                route = self._pids.get(pid)
                if route is not None:
                    route.put(item)
            case Delete(pid, walk):
                route = self._pids.get(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(File(pid, "delete", path, partial=partial))
            case Rename(pid, old, new):
                route = self._pids.get(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(old)
                    target, partial_target = route.mounts.resolve(new)
                    route.put(File(pid, "rename", path, target, partial=partial or partial_target))
            case RawDenied(pid, nr, errno):
                route = self._pids.get(pid)
                if route is not None and nr in route.syscalls:
                    route.put(Denied(pid, route.syscalls[nr], errno))

    def _exec(self, raw: RawExec) -> None:
        if raw.root:
            self._roots[raw.pid] = raw.pidns
            if len(self._roots) > MEMORY:
                self._roots.popitem(last=False)
            waiter = self._waiters.get(raw.pid)
            if waiter is not None and not waiter.done():
                waiter.set_result(raw.pidns)
        route = self._routes.get(raw.pidns)
        if route is None:
            return
        self._pids[raw.pid] = route
        path = self._bprm.pop(raw.pid, None) or (raw.argv[0] if raw.argv else "")
        route.put(
            Exec(
                raw.pid,
                raw.ppid,
                raw.ns_pid,
                raw.uid,
                path,
                raw.argv,
                raw.argv_bytes,
                raw.root,
            )
        )
