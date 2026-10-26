"""Per-sandbox resource timelines read from the host cgroup tree.

One unprivileged sampler container per backend sees every container's cgroup through the host
cgroup namespace. Each watched sandbox gets its own `aims-sample` process in it, streaming one
line per reading over an attached exec.
"""

import asyncio
import contextlib
import itertools
import logging

import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec as DockerExec

from aims.events import ResourceSample
from aims.sandbox.base import RUNNER_UID, Lost, Sample

log = logging.getLogger("aims.sandbox")

QUEUE = 10_000
WINDOW_US = 1_000_000
CGROUPS = "/sys/fs/cgroup"


class SamplerError(Exception):
    pass


def parse_sample(line: bytes) -> Sample | None:
    """A reading as `aims-sample` prints it, or None if the line does not fit."""
    try:
        ts, *values = (int(x) for x in line.split())
    except ValueError:
        return None
    if len(values) != len(ResourceSample.COLUMNS) or ts < 0:
        return None
    return Sample(ts, tuple(values))


class Windows:
    """Groups readings into one-second windows of the boot clock, one `ResourceSample` each."""

    def __init__(self, sandbox_id: str, hz: int) -> None:
        self.sandbox_id = sandbox_id
        self.hz = hz
        self._readings: list[Sample] = []

    def add(self, sample: Sample) -> ResourceSample | None:
        """Take `sample`; return the window it closed, if any."""
        done = None
        if self._readings and sample.ts // WINDOW_US != self._readings[0].ts // WINDOW_US:
            done = self.flush()
        if self._readings and sample.ts <= self._readings[-1].ts:
            return done
        self._readings.append(sample)
        return done

    def flush(self) -> ResourceSample | None:
        readings, self._readings = self._readings, []
        if not readings:
            return None
        columns: dict[str, list[int] | None] = {}
        for i, name in enumerate(ResourceSample.COLUMNS):
            raw = [r.values[i] for r in readings]
            columns[name] = None if all(v < 0 for v in raw) else _deltas(raw)
        return ResourceSample(
            sandbox_id=self.sandbox_id,
            hz=self.hz,
            ts=_deltas([r.ts for r in readings]),
            **columns,
        )


def _deltas(values: list[int]) -> list[int]:
    return [values[0], *(b - a for a, b in itertools.pairwise(values))]


class Watch:
    """The readings of one sandbox's cgroup, as they arrive."""

    def __init__(self, cgroup: str, hz: int, trace: DockerExec) -> None:
        self.cgroup = cgroup
        self.hz = hz
        self.queue: asyncio.Queue[Sample | Lost] = asyncio.Queue(QUEUE)
        self.overflow = 0
        self.garbled = 0
        self._trace = trace
        self._reader = asyncio.create_task(self._read())
        self._closing = False

    async def close(self) -> None:
        self._closing = True
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader

    def _put(self, item: Sample | Lost) -> None:
        if self.overflow:
            try:
                self.queue.put_nowait(Lost(self.overflow, "resource queue full"))
            except asyncio.QueueFull:
                self.overflow += 1
                return
            self.overflow = 0
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow += 1

    async def _read(self) -> None:
        pending = b""
        failure: str | None = None
        try:
            stream = await self._trace.start(detach=False).__aenter__()
            try:
                while (message := await stream.read_out()) is not None:
                    if message.stream == 2:
                        log.warning(
                            "sampler %s: %s", self.cgroup, message.data.decode(errors="replace")
                        )
                        continue
                    *lines, pending = (pending + message.data).split(b"\n")
                    for line in lines:
                        sample = parse_sample(line)
                        if sample is None:
                            self.garbled += 1
                        else:
                            self._put(sample)
            finally:
                await stream.close()
            state = await self._trace.inspect()
            if state.get("ExitCode") != 0:
                failure = f"exit {state.get('ExitCode')}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failure = f"{type(e).__name__}: {e}"
        if failure is not None and not self._closing:
            self._put(Lost(None, f"resource sampler stopped: {failure}"))


class Sampler:
    """An unprivileged container that reads sandbox cgroup files. One per backend."""

    def __init__(
        self, docker: aiodocker.Docker, labels: dict[str, str], name: str, image: str
    ) -> None:
        self._docker = docker
        self._labels = labels
        self._name = name
        self._image = image
        self._container: DockerContainer | None = None

    @property
    def alive(self) -> bool:
        return self._container is not None

    async def start(self) -> None:
        self._container = await self._docker.containers.create(
            config={
                "Image": self._image,
                "Cmd": ["sleep", "infinity"],
                "User": f"{RUNNER_UID}:{RUNNER_UID}",
                "Labels": {**self._labels, "aims.role": "sampler"},
                "NetworkDisabled": True,
                "HostConfig": {
                    "NetworkMode": "none",
                    "CgroupnsMode": "host",
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "ReadonlyRootfs": True,
                    "PidsLimit": 1024,
                    "LogConfig": {"Type": "none"},
                },
            },
            name=self._name,
        )
        try:
            await self._container.start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._container is not None:
            with contextlib.suppress(aiodocker.DockerError):
                await self._container.delete(force=True)
            self._container = None

    async def watch(self, container_id: str, hz: int) -> Watch:
        """Start reading the cgroup of container `container_id` `hz` times a second."""
        if self._container is None:
            raise SamplerError("the sampler is not running")
        cgroup = await self._find(container_id)
        trace = await self._container.exec(
            ["/aims/aims-sample", str(hz), cgroup], stdout=True, stderr=True
        )
        return Watch(cgroup, hz, trace)

    async def _find(self, container_id: str) -> str:
        """The cgroup directory of a container, wherever the cgroup driver put it."""
        assert self._container is not None
        if not container_id.isalnum():
            raise SamplerError(f"not a container id: {container_id!r}")
        find = await self._container.exec(
            ["find", CGROUPS, "-maxdepth", "4", "-type", "d", "-name", f"*{container_id}*"],
            stdout=True,
            stderr=True,
        )
        out = b""
        stream = await find.start(detach=False).__aenter__()
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 1:
                    out += message.data
        finally:
            await stream.close()
        found = [p for p in out.decode(errors="replace").split("\n") if p]
        if len(found) != 1:
            raise SamplerError(f"found {len(found)} cgroups for {container_id[:12]}")
        return found[0]
