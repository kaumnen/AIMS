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
from aims.sandbox.execs import exec_output

log = logging.getLogger("aims.sandbox")

QUEUE = 10_000
WINDOW_US = 1_000_000
CGROUPS = "/sys/fs/cgroup"
FIND_S = 10
RETRY_S = 1.0
RETRY_MAX_S = 30.0
GONE = 1


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
            ts=_deltas([r.ts for r in readings], trim=False),
            **columns,
        )


def _deltas(values: list[int], *, trim: bool = True) -> list[int]:
    out = [values[0], *(b - a for a, b in itertools.pairwise(values))]
    while trim and len(out) > 1 and out[-1] == 0:
        out.pop()
    return out


class Watch:
    """The readings of one sandbox's cgroup, as they arrive.

    A stream that fails is recorded as a gap and replaced, after a backoff, until the cgroup is
    gone. The sampler container is started again if it died.
    """

    def __init__(self, sampler: Sampler, cgroup: str, hz: int, trace: DockerExec) -> None:
        self.cgroup = cgroup
        self.hz = hz
        self.queue: asyncio.Queue[Sample | Lost] = asyncio.Queue(QUEUE)
        self.overflow = 0
        self.garbled = 0
        self.restarts = 0
        self._sampler = sampler
        self._reader = asyncio.create_task(self._run(trace))
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

    async def _run(self, trace: DockerExec) -> None:
        tries = 0
        while True:
            failure, delivered = await self._read(trace)
            if failure is None or self._closing:
                return
            if delivered:
                tries = 0
            if tries == 0:
                self._put(Lost(None, f"resource sampler stopped: {failure}"))
            while True:
                await asyncio.sleep(min(RETRY_MAX_S, RETRY_S * (2**tries - 1)))
                tries += 1
                try:
                    trace = await self._sampler.stream(self.cgroup, self.hz)
                    break
                except Exception as e:
                    log.warning("resource sampler for %s: %s", self.cgroup, e)
            self.restarts += 1

    async def _read(self, trace: DockerExec) -> tuple[str | None, bool]:
        """Read one stream to its end; return why it failed, if it did, and if it read a sample.

        `aims-sample` exits 0 or GONE when the cgroup is removed or missing.
        """
        pending = b""
        delivered = False
        try:
            stream = await trace.start(detach=False).__aenter__()
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
                            delivered = True
                            self._put(sample)
            finally:
                await stream.close()
            code = (await trace.inspect()).get("ExitCode")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"{type(e).__name__}: {e}", delivered
        return (None if code in (0, GONE) else f"exit {code}"), delivered


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
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._container is not None

    async def start(self) -> None:
        self._container = await self._create()

    async def _create(self) -> DockerContainer:
        container = await self._docker.containers.create(
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
            await container.start()
        except BaseException:
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)
            raise
        return container

    async def stop(self) -> None:
        if self._container is not None:
            with contextlib.suppress(aiodocker.DockerError):
                await self._container.delete(force=True)
            self._container = None

    async def watch(self, container_id: str, hz: int) -> Watch:
        """Start reading the cgroup of container `container_id` `hz` times a second."""
        container = await self._ensure()
        cgroup = await self._find(container, container_id)
        return Watch(self, cgroup, hz, await self._sample(container, cgroup, hz))

    async def stream(self, cgroup: str, hz: int) -> DockerExec:
        """A new reading stream of `cgroup`."""
        return await self._sample(await self._ensure(), cgroup, hz)

    async def _sample(self, container: DockerContainer, cgroup: str, hz: int) -> DockerExec:
        return await container.exec(
            ["/aims/aims-sample", str(hz), cgroup], stdout=True, stderr=True
        )

    async def _ensure(self) -> DockerContainer:
        """The running sampler container, started again if it died.

        Watches that find it dead at the same time share one restart.
        """
        container = self._container
        if container is None:
            raise SamplerError("the sampler is not running")
        try:
            running = (await container.show())["State"]["Running"]
        except aiodocker.DockerError as e:
            if e.status != 404:
                raise
            running = False
        if running:
            return container
        async with self._lock:
            if self._container is container:
                log.warning("resource sampler %s stopped; starting it again", self._name)
                with contextlib.suppress(aiodocker.DockerError):
                    await container.delete(force=True)
                self._container = await self._create()
        if self._container is None:
            raise SamplerError("the sampler is not running")
        return self._container

    async def _find(self, container: DockerContainer, container_id: str) -> str:
        """The cgroup directory of a container, wherever the cgroup driver put it."""
        if not container_id.isalnum():
            raise SamplerError(f"not a container id: {container_id!r}")
        try:
            out = await exec_output(
                container,
                ["find", CGROUPS, "-maxdepth", "4", "-type", "d", "-name", f"*{container_id}*"],
                timeout_s=FIND_S,
            )
        except TimeoutError as e:
            raise SamplerError(f"cgroup lookup for {container_id[:12]} timed out") from e
        found = [p for p in out.decode(errors="replace").split("\n") if p]
        if len(found) != 1:
            raise SamplerError(f"found {len(found)} cgroups for {container_id[:12]}")
        return found[0]
