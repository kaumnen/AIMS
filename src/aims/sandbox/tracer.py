import asyncio
import contextlib
import dataclasses
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec as DockerExec
from pydantic import JsonValue

from aims.sandbox.base import Denied, Exec, Exit, File, Fork, Kernel, Lost, Record
from aims.sandbox.images import ensure_image
from aims.sandbox.trace import (
    Delete,
    Dropped,
    ExecPart,
    Gap,
    Garbled,
    Mounts,
    Note,
    RawDenied,
    RawExec,
    Ready,
    Rename,
    Sequence,
    parse,
)

log = logging.getLogger("aims.sandbox")

QUEUE = 100_000
MEMORY = 4096
READY_S = 120.0
CONFIRM_S = 5.0
SETTLE_S = 2.0
TRACE = (
    "mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing; "
    "exec bpftrace -B line -f json /aims/trace.bt"
)
PMU_EVENTS = ("cpu-cycles", "instructions", "cache-misses")
PMU = (
    "echo devices $(ls /sys/bus/event_source/devices); for e in "
    + " ".join(PMU_EVENTS)
    + '; do if out=$(bpftrace -e "hardware:$e:1000000 { exit(); } interval:ms:50 { exit(); }"'
    ' 2>&1 >/dev/null); then echo "$e ok"; else echo "$e $(echo "$out" | grep -v Attaching'
    ' | head -n 1)"; fi; done'
)


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
    window: tuple[int, int] | None = None

    def put(self, record: Record) -> None:
        if self.overflow:
            low, high = self.window or (None, None)
            lost = Lost(self.overflow, "audit queue full", (), low, high)
            try:
                self.queue.put_nowait(lost)
            except asyncio.QueueFull:
                self._drop(record)
                return
            self.overflow, self.window = 0, None
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self._drop(record)

    def _drop(self, record: Record) -> None:
        self.overflow += 1
        if isinstance(record, Kernel):
            ts = record.at.ts
            low, high = self.window or (ts, ts)
            self.window = (min(low, ts), max(high, ts))


class Tracer:
    """A privileged bpftrace container that sees every process, file and socket call in sandboxes.

    One per backend. Records reach a sandbox once it registers its pid namespace.
    """

    def __init__(
        self,
        docker: aiodocker.Docker,
        labels: dict[str, str],
        name: str,
        *,
        ring_pages: int | None = None,
    ) -> None:
        self._docker = docker
        self._labels = labels
        self._name = name
        self._ring_pages = ring_pages
        self._container: DockerContainer | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._routes: dict[int, Route] = {}
        self._pids: dict[int, Route] = {}
        self._parts: OrderedDict[int, dict[str, object]] = OrderedDict()
        self._roots: OrderedDict[int, int] = OrderedDict()
        self._waiters: dict[int, asyncio.Future[int]] = {}
        self._sequence = Sequence()
        self._settle: asyncio.TimerHandle | None = None
        self.alive = False
        self.garbled = 0
        self.clock: tuple[int, int] | None = None
        self.kernel_lost = 0
        self.located = 0
        self.unlocated = 0
        self.pmu: dict[str, JsonValue] | None = None

    async def start(self) -> None:
        image = await ensure_image(self._docker, "tracer")
        log.warning(
            "starting %s, a privileged container with host pid access that traces sandbox "
            "processes; use audit 'snapshot' to run without it",
            self._name,
        )
        env = [] if self._ring_pages is None else [f"BPFTRACE_PERF_RB_PAGES={self._ring_pages}"]
        self._container = await self._docker.containers.create(
            config={
                "Image": image,
                "Env": env,
                "Labels": {**self._labels, "aims.role": "tracer"},
                "HostConfig": {
                    "Privileged": True,
                    "PidMode": "host",
                    "NetworkMode": "none",
                    "LogConfig": {"Type": "none"},
                },
            },
            name=self._name,
        )
        await self._container.start()
        trace = await self._container.exec(["sh", "-c", TRACE], stdout=True, stderr=True)
        self._reader = asyncio.create_task(self._read(trace))
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
        try:
            self.pmu = await self._probe_pmu()
        except aiodocker.DockerError as e:
            log.warning("hardware counter probe failed: %s", e)

    async def _probe_pmu(self) -> dict[str, JsonValue]:
        """Which hardware counters the host offers: event source devices and an attach attempt."""
        assert self._container is not None
        probe = await self._container.exec(["sh", "-c", PMU], stdout=True, stderr=True)
        out = b""
        stream = await probe.start(detach=False).__aenter__()
        try:
            while (message := await stream.read_out()) is not None:
                out += message.data
        finally:
            await stream.close()
        return parse_pmu(out.decode(errors="replace"))

    async def stop(self) -> None:
        self.alive = False
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None
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

    async def _read(self, trace: DockerExec) -> None:
        """Read bpftrace's output as bytes, so file names that are not UTF-8 keep their bytes."""
        pending = {1: b"", 2: b""}
        try:
            stream = await trace.start(detach=False).__aenter__()
            try:
                while (message := await stream.read_out()) is not None:
                    data = pending.get(message.stream, b"") + message.data
                    *lines, pending[message.stream] = data.split(b"\n")
                    for line in lines:
                        self._handle(line.decode("utf-8", "surrogateescape"))
            finally:
                await stream.close()
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

    def settle(self) -> None:
        """Report holes older than a second, then any loss the kernel counted that no hole explains.

        Unexplained loss is reported with an unknown count, so a hole found later is not counted
        twice.
        """
        self._settle = None
        self._gaps(self._sequence.expire(self._sequence.latest - 1_000_000))
        excess = self.kernel_lost - self.located
        if excess > self.unlocated:
            reason = (
                f"the kernel dropped {excess - self.unlocated} records no sequence hole located"
            )
            for route in self._routes.values():
                route.put(Lost(None, reason))
            self.unlocated = excess

    def _handle(self, line: str) -> None:
        item = parse(line)
        at = getattr(item, "at", None)
        if at is not None:
            self._gaps(self._sequence.observe(at))
        match item:
            case None:
                return
            case Ready(base, boot_us, tai_us):
                self.clock = (boot_us, tai_us)
                if not self._ready.done():
                    self._ready.set_result(base)
            case Note(text):
                log.warning("tracer: %s", text)
                if not self._ready.done() and "ERROR" in text:
                    self._ready.set_exception(TracerError(text))
            case Dropped(count):
                self.kernel_lost += count
                if self._settle is None:
                    self._settle = asyncio.get_running_loop().call_later(SETTLE_S, self.settle)
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
            case ExecPart(pid):
                self._parts.setdefault(pid, {}).update(item.fields())
                self._parts.move_to_end(pid)
                if len(self._parts) > MEMORY:
                    self._parts.popitem(last=False)
            case RawExec():
                self._exec(item)
            case Exit(pid):
                self._parts.pop(pid, None)
                route = self._pids.pop(pid, None)
                if route is not None:
                    route.put(item)
            case Delete(pid, walk):
                route = self._pids.get(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(File(pid, "delete", path, partial=partial, at=item.at))
            case Rename(pid, old, new):
                route = self._pids.get(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(old)
                    target, partial_target = route.mounts.resolve(new)
                    partial = partial or partial_target
                    route.put(File(pid, "rename", path, target, partial=partial, at=item.at))
            case RawDenied(pid, nr, errno):
                route = self._pids.get(pid)
                if route is not None and nr in route.syscalls:
                    route.put(Denied(pid, route.syscalls[nr], errno, at=item.at))
            case Kernel(pid):
                route = self._pids.get(pid)
                if route is not None:
                    route.put(item)

    def _gaps(self, gaps: list[Gap]) -> None:
        for gap in gaps:
            self.located += gap.count
            lost = Lost(gap.count, "kernel record gap", (gap.cpu,), gap.ts_from, gap.ts_to)
            for route in self._routes.values():
                route.put(lost)

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
        parts = self._parts.pop(raw.pid, {})
        parts.setdefault("path", raw.argv[0] if raw.argv else "")
        record = Exec(
            raw.pid,
            raw.ppid,
            raw.ns_pid,
            raw.uid,
            "",
            raw.argv,
            raw.argv_bytes,
            raw.root,
            at=raw.at,
        )
        route.put(dataclasses.replace(record, **parts))


def parse_pmu(text: str) -> dict[str, JsonValue]:
    devices: list[JsonValue] = []
    events: dict[str, JsonValue] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(" ")
        if name == "devices":
            devices = list(rest.split())
        elif name in PMU_EVENTS:
            events[name] = rest.strip() or "unknown"
    available = any(v == "ok" for v in events.values())
    return {"available": available, "devices": devices, "events": events}
