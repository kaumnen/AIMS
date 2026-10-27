import asyncio
import contextlib
import io
import json
import logging
import os
import re
import socket
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Self

import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.volumes import DockerVolume
from pydantic import JsonValue

from aims.events import AuditLevel, Proc
from aims.sandbox.base import (
    AGENT_UID,
    RUNNER_UID,
    TOOLBOX,
    AuditMode,
    Capture,
    Lost,
    Outcome,
    Record,
    Sample,
    SandboxInfo,
    SandboxSpec,
    Tree,
    check_name,
)
from aims.sandbox.images import ensure_image, ensure_pulled
from aims.sandbox.reader import Reader
from aims.sandbox.resources import Sampler, SamplerError, Watch
from aims.sandbox.trace import ARCHES, SYSCALLS, Mounts
from aims.sandbox.tracer import Route, Tracer, TracerError

log = logging.getLogger("aims.sandbox")

GRACE_MS = 1000
DRAIN_S = 5.0
MIB = 1024 * 1024
HOME = "/home/agent"

_PIDNS = re.compile(r"^pid:\[(\d+)\]$")


class SandboxError(Exception):
    pass


class DockerBackend:
    """Sandboxes as hardened Docker containers, optionally watched by the kernel tracer."""

    name = "docker"

    def __init__(
        self,
        docker: aiodocker.Docker,
        *,
        arch: str,
        labels: dict[str, str],
        toolbox: tuple[str, str],
        tracer: Tracer | None,
        sampler: Sampler | None = None,
    ) -> None:
        self.docker = docker
        self.arch = arch
        self.tracer = tracer
        self.sampler = sampler
        self._labels = labels
        self._toolbox_image, self._toolbox_volume = toolbox

    @classmethod
    async def open(
        cls, *, audit: AuditMode = "auto", url: str | None = None, ring_pages: int | None = None
    ) -> DockerBackend:
        """Connect to Docker and start the kernel tracer unless `audit` is snapshot.

        `ring_pages` sizes the tracer's kernel ring in 4 KiB pages, a power of two.
        """
        docker = aiodocker.Docker(url=url)
        sampler: Sampler | None = None
        tracer: Tracer | None = None
        try:
            info = await docker.system.info()
            arch = ARCHES.get(info["Architecture"], info["Architecture"])
            labels = {
                "aims.managed": "1",
                "aims.host": socket.gethostname(),
                "aims.pid": str(os.getpid()),
            }
            await _sweep(docker, labels["aims.host"])
            toolbox = await _toolbox(docker)
            sampler = await _sampler(docker, labels, toolbox[0])
            if audit != "snapshot":
                name = f"aims-tracer-{uuid.uuid4().hex[:12]}"
                tracer = Tracer(docker, labels, name, ring_pages=ring_pages)
                try:
                    await tracer.start()
                except (TracerError, aiodocker.DockerError, TimeoutError) as e:
                    if audit == "kernel":
                        raise SandboxError(f"kernel audit unavailable: {e}") from e
                    log.warning("kernel audit unavailable, using snapshot audit: %s", e)
                    tracer = None
            return cls(
                docker, arch=arch, labels=labels, toolbox=toolbox, tracer=tracer, sampler=sampler
            )
        except BaseException:
            if tracer is not None:
                await tracer.stop()
            if sampler is not None:
                await sampler.stop()
            await docker.close()
            raise

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self.tracer is not None:
            await self.tracer.stop()
        if self.sampler is not None:
            await self.sampler.stop()
        await self.docker.close()

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> DockerSandbox:
        check_name(agent_id)
        image = await ensure_pulled(self.docker, spec.image)
        work = f"aims-{run_id}-{agent_id}-work"
        volumes = [(work, spec.workdir, False)]
        volumes += [(f"aims-{run_id}-{m.volume}", m.target, m.read_only) for m in spec.mounts]
        created = await self._volumes(run_id, [name for name, _, _ in volumes])
        if created:
            targets = {name: target for name, target, _ in volumes}
            await self._prepare(image["Id"], [(name, targets[name]) for name in created])

        owner = f"uid={AGENT_UID},gid={AGENT_UID},mode=0755"
        scratch = [
            (f"aims-{run_id}-{agent_id}-tmp", "/tmp", f"size={spec.tmp_mb}m,mode=1777"),
            (f"aims-{run_id}-{agent_id}-home", HOME, f"size=64m,{owner}"),
        ]
        for volume, _, options in scratch:
            await self._scratch(run_id, volume, f"nosuid,nodev,{options}")
        volumes += [(volume, target, False) for volume, target, _ in scratch]

        labels = {**self._labels, "aims.run": run_id, "aims.agent": agent_id}
        mounts = [
            {"Type": "volume", "Source": self._toolbox_volume, "Target": TOOLBOX, "ReadOnly": True},
            *(
                {
                    "Type": "volume",
                    "Source": name,
                    "Target": target,
                    "ReadOnly": ro,
                    "VolumeOptions": {"NoCopy": True},
                }
                for name, target, ro in volumes
            ),
        ]
        env = {"HOME": HOME, **spec.env}
        config: dict[str, Any] = {
            "Image": image["Id"],
            "Entrypoint": [f"{TOOLBOX}/busybox"],
            "Cmd": ["sleep", "infinity"],
            "User": f"{RUNNER_UID}:{RUNNER_UID}",
            "WorkingDir": spec.workdir,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": labels,
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "Init": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "PidsLimit": spec.pids,
                "Memory": spec.memory_mb * MIB,
                "MemorySwap": spec.memory_mb * MIB,
                "NanoCpus": int(spec.cpus * 1e9),
                "ReadonlyRootfs": not spec.writable_root,
                "Mounts": mounts,
                "LogConfig": {"Type": "none"},
            },
        }
        name = f"aims-{run_id}-{agent_id}"
        container = await self.docker.containers.create(config=config, name=name)
        reader: Reader | str | None = None
        try:
            await container.start()
            reader = await self._reader(name, labels, volumes)
            sandbox = await DockerSandbox.attach(
                self, container, spec, image, work, reader, tuple(v for v, _, _ in scratch)
            )
        except BaseException:
            if isinstance(reader, Reader):
                await reader.close()
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)
            raise
        return sandbox

    async def _reader(
        self, name: str, labels: dict[str, str], volumes: list[tuple[str, str, bool]]
    ) -> Reader | str:
        """The sandbox's file reader, or why it could not start."""
        try:
            return await Reader.start(
                self.docker,
                self._toolbox_image,
                f"{name}-reader",
                [(volume, target) for volume, target, _ in volumes],
                labels,
            )
        except Exception as e:
            log.error("file reader for %s did not start: %s", name, e)
            return f"the file reader did not start: {e}"

    async def remove_run(self, run_id: str) -> None:
        """Delete the containers and volumes of one run."""
        query = {"label": [f"aims.run={run_id}"]}
        for container in await self.docker.containers.list(all=True, filters=json.dumps(query)):
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)
        listing = await self.docker.volumes.list(filters=query)
        for volume in listing.get("Volumes") or []:
            with contextlib.suppress(aiodocker.DockerError):
                await DockerVolume(self.docker, volume["Name"]).delete()

    async def _volumes(self, run_id: str, names: list[str]) -> list[str]:
        created: list[str] = []
        for name in names:
            try:
                await DockerVolume(self.docker, name).show()
            except aiodocker.DockerError as e:
                if e.status != 404:
                    raise
                labels = {**self._labels, "aims.run": run_id}
                await self.docker.volumes.create({"Name": name, "Labels": labels})
                created.append(name)
        return created

    async def _scratch(self, run_id: str, name: str, options: str) -> None:
        """A fresh tmpfs volume, which the reader can see into, unlike a container's own tmpfs.

        Its content lives while a container mounts it.
        """
        with contextlib.suppress(aiodocker.DockerError):
            await DockerVolume(self.docker, name).delete()
        await self.docker.volumes.create(
            {
                "Name": name,
                "Labels": {**self._labels, "aims.run": run_id},
                "Driver": "local",
                "DriverOpts": {"type": "tmpfs", "device": "tmpfs", "o": options},
            }
        )

    async def _prepare(self, image: str, volumes: list[tuple[str, str]]) -> None:
        """Fill new volumes from the image at their mount points and hand them to the agent."""
        mounts = [
            {"Type": "volume", "Source": self._toolbox_volume, "Target": TOOLBOX, "ReadOnly": True},
            *({"Type": "volume", "Source": name, "Target": target} for name, target in volumes),
        ]
        await _helper(
            self.docker,
            image,
            ["chown", "-R", f"{AGENT_UID}:{AGENT_UID}", *(target for _, target in volumes)],
            mounts,
            caps=["CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER"],
            entrypoint=[f"{TOOLBOX}/busybox"],
        )


class DockerSandbox:
    def __init__(
        self,
        backend: DockerBackend,
        container: DockerContainer,
        spec: SandboxSpec,
        info: SandboxInfo,
        work: str,
        shell: list[str],
        route: Route | None,
        reader: Reader | str | None = None,
        scratch: tuple[str, ...] = (),
    ) -> None:
        self._backend = backend
        self._container = container
        self._spec = spec
        self._info = info
        self._work = work
        self._scratch = scratch
        self._shell = shell
        self._route = route
        self._watch: Watch | None = None
        self._reader = reader

    @classmethod
    async def attach(
        cls,
        backend: DockerBackend,
        container: DockerContainer,
        spec: SandboxSpec,
        image: dict[str, Any],
        work: str,
        reader: Reader | str | None = None,
        scratch: tuple[str, ...] = (),
    ) -> DockerSandbox:
        """Learn the sandbox's pid namespace and mounts, and hand them to the tracer."""
        probe = (
            "readlink /proc/self/ns/pid; [ -x /bin/sh ] && echo /bin/sh || echo none; "
            "cat /proc/self/mountinfo"
        )
        capture = Capture(limit=MIB)
        info = _info(container, spec, image)
        holder = cls(backend, container, spec, info, work, [], None, reader, scratch)
        outcome = await holder.run(
            [f"{TOOLBOX}/busybox", "sh", "-c", probe],
            uid=RUNNER_UID,
            cwd="/",
            timeout_ms=10_000,
            capture=capture,
        )
        lines = capture.stdout.decode(errors="replace").split("\n", 2)
        match = _PIDNS.match(lines[0]) if lines else None
        if outcome.exit_code != 0 or match is None or len(lines) < 3:
            raise SandboxError(f"sandbox probe failed: {outcome} {bytes(capture.stderr)!r}")
        pidns = int(match.group(1))
        shell = ["/bin/sh", "-c"] if lines[1] == "/bin/sh" else [f"{TOOLBOX}/busybox", "sh", "-c"]
        route = None
        tracer = backend.tracer
        if tracer is not None and tracer.alive and outcome.pid is not None:
            route = tracer.register(pidns, Mounts.parse(lines[2]), SYSCALLS.get(backend.arch, {}))
            if not await tracer.confirm(outcome.pid, pidns):
                log.warning("tracer did not see sandbox %s start; using snapshot audit", pidns)
                tracer.unregister(route)
                route = None
            elif tracer.clock is not None:
                boot_us, tai_us = tracer.clock
                holder._info.config["kernel_clock"] = {"boot_us": boot_us, "tai_us": tai_us}
        if tracer is not None and tracer.pmu is not None:
            holder._info.config["pmu"] = tracer.pmu
        await holder._sample(backend.sampler)
        holder._shell = shell
        holder._route = route
        return holder

    async def _sample(self, sampler: Sampler | None) -> None:
        """Start the resource timeline of this sandbox, recording where it comes from."""
        resources: JsonValue = None
        if sampler is not None and sampler.alive:
            try:
                self._watch = await sampler.watch(self._container.id, self._spec.sample_hz)
                resources = {"hz": self._watch.hz, "cgroup": self._watch.cgroup}
            except (SamplerError, aiodocker.DockerError) as e:
                log.warning("no resource timeline for %s: %s", self._container.id[:12], e)
        self._info.config["resources"] = resources

    @property
    def info(self) -> SandboxInfo:
        return self._info

    @property
    def audit(self) -> AuditLevel:
        tracer = self._backend.tracer
        traced = self._route is not None and tracer is not None and tracer.alive
        return "kernel" if traced else "snapshot"

    def wrapper(self, timeout_ms: int) -> list[str]:
        return [f"{TOOLBOX}/aims-exec", str(timeout_ms), str(GRACE_MS), *self._shell]

    async def run(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: str,
        timeout_ms: int,
        capture: Capture,
        on_pid: Callable[[int], None] | None = None,
    ) -> Outcome:
        start = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - start) * 1000)

        try:
            exe = await self._container.exec(
                list(argv), user=f"{uid}:{uid}", workdir=cwd, stdout=True, stderr=True
            )
        except aiodocker.DockerError as e:
            return Outcome("failed", None, None, elapsed(), error=e.message)
        end = "exited"
        pid: int | None = None
        try:
            async with asyncio.timeout(timeout_ms / 1000 + GRACE_MS / 1000 + DRAIN_S):
                stream = await exe.start(detach=False).__aenter__()
                try:
                    with contextlib.suppress(aiodocker.DockerError):
                        pid = (await exe.inspect()).get("Pid") or None
                    if pid is not None and on_pid is not None:
                        on_pid(pid)
                    while (message := await stream.read_out()) is not None:
                        capture.add(message.stream, message.data)
                finally:
                    await stream.close()
        except TimeoutError:
            end = "timeout"
        except aiodocker.DockerError as e:
            return Outcome("failed", None, pid, elapsed(), error=e.message)
        duration = elapsed()
        try:
            state = await exe.inspect()
        except aiodocker.DockerError as e:
            return Outcome("failed", None, pid, duration, error=e.message)
        if pid is None and state.get("Pid"):
            pid = state["Pid"]
            if on_pid is not None:
                on_pid(pid)
        exit_code = None if state.get("Running") else state.get("ExitCode")
        if duration >= timeout_ms:
            end = "timeout"
        return Outcome(end, exit_code, pid, duration)

    async def processes(self) -> list[Proc]:
        top = await self._container.top(ps_args="-o pid,ppid,uid,args")
        titles = top.get("Titles") or []
        rows = top.get("Processes") or []
        if titles[:3] != ["PID", "PPID", "UID"]:
            raise SandboxError(f"unexpected docker top columns: {titles}")
        return [
            Proc(pid=int(r[0]), ppid=int(r[1]), uid=int(r[2]), command=" ".join(r[3:])[:2000])
            for r in rows
        ]

    def records(self) -> asyncio.Queue[Record] | None:
        return None if self._route is None else self._route.queue

    def samples(self) -> asyncio.Queue[Sample | Lost] | None:
        return None if self._watch is None else self._watch.queue

    async def tree(self) -> Tree | None:
        if self._reader is None:
            return None
        if isinstance(self._reader, str):
            raise SandboxError(self._reader)
        return await self._reader.tree()

    async def pause(self) -> None:
        await self._container.pause()

    async def resume(self) -> None:
        await self._container.unpause()

    async def put_archive(self, path: str, data: bytes) -> None:
        await self._container.put_archive(path, data)

    async def get_archive(self, path: str) -> bytes:
        archive = await self._container.get_archive(path)
        if not isinstance(archive.fileobj, io.BytesIO):
            raise SandboxError("archive came back without its bytes")
        return archive.fileobj.getvalue()

    async def kill(self) -> None:
        with contextlib.suppress(aiodocker.DockerError):
            await self._container.kill(signal="SIGKILL")

    async def close(self, *, keep_volumes: bool = False) -> None:
        if self._watch is not None:
            await self._watch.close()
            self._watch = None
        tracer = self._backend.tracer
        if self._route is not None and tracer is not None:
            tracer.unregister(self._route)
            self._route = None
        if isinstance(self._reader, Reader):
            await self._reader.close()
        with contextlib.suppress(aiodocker.DockerError):
            await self._container.delete(force=True)
        for name in (*self._scratch, *(() if keep_volumes else (self._work,))):
            with contextlib.suppress(aiodocker.DockerError):
                await DockerVolume(self._backend.docker, name).delete()


def _info(container: DockerContainer, spec: SandboxSpec, image: dict[str, Any]) -> SandboxInfo:
    config: dict[str, JsonValue] = {
        "uid": AGENT_UID,
        "runner_uid": RUNNER_UID,
        "workdir": spec.workdir,
        "env": dict(spec.env),
        "cpus": spec.cpus,
        "memory_mb": spec.memory_mb,
        "pids": spec.pids,
        "tmp_mb": spec.tmp_mb,
        "writable_root": spec.writable_root,
        "network": "none",
        "mounts": [m.model_dump() for m in spec.mounts],
        "repo_digests": list(image.get("RepoDigests") or []),
    }
    return SandboxInfo(container.id, "docker", spec.image, image["Id"], config)


async def _toolbox(docker: aiodocker.Docker) -> tuple[str, str]:
    image = await ensure_image(docker, "toolbox")
    volume = image.replace(":", "-")
    await docker.volumes.create({"Name": volume, "Labels": {"aims.managed": "1"}})
    await _helper(
        docker,
        image,
        ["sh", "-c", "test -f /out/.ready || { cp -a /aims/. /out/ && touch /out/.ready; }"],
        [{"Type": "volume", "Source": volume, "Target": "/out"}],
        caps=["CHOWN", "FOWNER", "DAC_OVERRIDE"],
    )
    return image, volume


async def _sampler(docker: aiodocker.Docker, labels: dict[str, str], image: str) -> Sampler | None:
    sampler = Sampler(docker, labels, f"aims-sampler-{uuid.uuid4().hex[:12]}", image)
    try:
        await sampler.start()
    except aiodocker.DockerError as e:
        log.warning("resource sampling unavailable: %s", e)
        return None
    return sampler


async def _helper(
    docker: aiodocker.Docker,
    image: str,
    cmd: list[str],
    mounts: list[dict[str, Any]],
    *,
    caps: list[str],
    entrypoint: list[str] | None = None,
) -> None:
    container = await docker.containers.create(
        config={
            "Image": image,
            "Entrypoint": entrypoint,
            "Cmd": cmd,
            "User": "0:0",
            "Labels": {"aims.managed": "1", "aims.role": "helper"},
            "HostConfig": {
                "NetworkMode": "none",
                "CapDrop": ["ALL"],
                "CapAdd": caps,
                "SecurityOpt": ["no-new-privileges"],
                "Mounts": mounts,
            },
        }
    )
    try:
        await container.start()
        result = await container.wait()
        if result.get("StatusCode") != 0:
            logs = await container.log(stdout=True, stderr=True)
            raise SandboxError(f"helper {cmd[0]} failed: {''.join(logs)[-2000:]}")
    finally:
        with contextlib.suppress(aiodocker.DockerError):
            await container.delete(force=True)


async def _sweep(docker: aiodocker.Docker, host: str) -> None:
    """Remove containers left by AIMS processes on this host that no longer run."""
    query = {"label": ["aims.managed=1", f"aims.host={host}"]}
    for container in await docker.containers.list(all=True, filters=json.dumps(query)):
        labels = container["Labels"] or {}
        try:
            pid = int(labels.get("aims.pid", ""))
        except ValueError:
            continue
        if pid == os.getpid() or _alive(pid):
            continue
        log.info("removing %s left by process %d", container.id[:12], pid)
        with contextlib.suppress(aiodocker.DockerError):
            await container.delete(force=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
