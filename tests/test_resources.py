import asyncio
import itertools
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from aims.events import Event, LocalEventStore, ResourceSample, RunStarted
from aims.sandbox import AuditedSandbox, DockerBackend, SandboxSpec
from aims.sandbox import execs as execs_module
from aims.sandbox import resources as resources_module
from aims.sandbox.base import Lost, Sample
from aims.sandbox.resources import (
    GONE,
    Sampler,
    SamplerError,
    Watch,
    Windows,
    parse_sample,
)
from aims.sandbox.tracer import parse_pmu

COLUMNS = len(ResourceSample.COLUMNS)
IMAGE = "python:3.13-slim"
# The sampler's timings hold only on a quiet host.
pytestmark = pytest.mark.serial


def reading(ts: int, cpu: int, memory: int = 100, *, unread: tuple[int, ...] = ()) -> Sample:
    values = [cpu, *range(1, COLUMNS - 2), memory, 3]
    for i in unread:
        values[i] = -1
    return Sample(ts, tuple(values))


def test_parse_sample() -> None:
    line = b"1362547808 " + b" ".join(str(i).encode() for i in range(COLUMNS))
    assert parse_sample(line) == Sample(1362547808, tuple(range(COLUMNS)))
    assert parse_sample(b"1 2 3") is None
    assert parse_sample(line + b" 9") is None
    assert parse_sample(b"x " + b"0 " * COLUMNS) is None
    assert parse_sample(b"") is None


def test_windows_split_on_boot_clock_seconds() -> None:
    windows = Windows("sb", 50)
    assert windows.add(reading(1_999_000, 10)) is None
    assert windows.add(reading(1_999_500, 25, memory=300)) is None
    first = windows.add(reading(2_000_100, 40, memory=200))
    assert first is not None
    assert (first.sandbox_id, first.hz, first.ts) == ("sb", 50, [1_999_000, 500])
    assert first.cpu_usec == [10, 15]
    assert first.values("cpu_usec") == [10, 25]
    assert first.values("ts") == [1_999_000, 1_999_500]
    assert first.memory_bytes == [100, 200]
    assert windows.add(reading(2_000_050, 50)) is None
    last = windows.flush()
    assert last is not None
    assert last.values("ts") == [2_000_100]
    assert windows.flush() is None


def test_unreadable_columns() -> None:
    windows = Windows("sb", 10)
    for ts in (0, 100_000):
        windows.add(reading(ts, 5, unread=(5,)))
    windows.add(reading(200_000, 7, unread=(5, 6)))
    window = windows.flush()
    assert window is not None
    assert window.io_rbytes is None
    assert window.values("io_wbytes") == [6, 6, -1]
    assert window.summary()["io_wbytes"] == 6
    assert window.summary()["io_rbytes"] is None


def test_summary_takes_last_counters_and_peak_gauges() -> None:
    windows = Windows("sb", 10)
    windows.add(reading(0, 5, memory=900))
    windows.add(reading(100_000, 8, memory=400))
    window = windows.flush()
    assert window is not None
    summary = window.summary()
    assert (summary["cpu_usec"], summary["memory_bytes"], summary["pids"]) == (8, 900, 3)


def test_window_lengths_must_agree() -> None:
    columns: dict[str, Any] = {name: [0] for name in ResourceSample.COLUMNS}
    ResourceSample(sandbox_id="sb", hz=10, ts=[1, 2], **columns)
    with pytest.raises(ValidationError):
        ResourceSample(sandbox_id="sb", hz=10, ts=[1], **{**columns, "pids": [1, 0]})
    with pytest.raises(ValidationError):
        ResourceSample(sandbox_id="sb", hz=10, ts=[1], **{**columns, "pids": []})
    with pytest.raises(ValidationError):
        ResourceSample(sandbox_id="sb", hz=10, ts=[], **columns)


def test_columns_that_hold_still_are_one_value() -> None:
    windows = Windows("sb", 10)
    for ts, cpu in ((0, 5), (100_000, 9), (200_000, 9), (300_000, 9)):
        windows.add(reading(ts, cpu))
    window = windows.flush()
    assert window is not None
    assert (window.cpu_usec, window.pids, window.ts) == (
        [5, 4],
        [3],
        [0, 100_000, 100_000, 100_000],
    )
    assert window.values("cpu_usec") == [5, 9, 9, 9]
    assert window.values("pids") == [3, 3, 3, 3]
    assert window.summary()["cpu_usec"] == 9


def test_windows_drop_readings_that_go_back_in_time() -> None:
    windows = Windows("sb", 10)
    windows.add(reading(500, 1))
    windows.add(reading(400, 2))
    window = windows.flush()
    assert window is not None
    assert window.values("ts") == [500]


def test_parse_pmu() -> None:
    text = (
        "devices breakpoint kprobe software tracepoint uprobe\n"
        "cpu-cycles perf_event_open failed: No such file or directory\n"
        "instructions ok\n"
        "cache-misses \n"
    )
    assert parse_pmu(text) == {
        "available": True,
        "devices": ["breakpoint", "kprobe", "software", "tracepoint", "uprobe"],
        "events": {
            "cpu-cycles": "perf_event_open failed: No such file or directory",
            "instructions": "ok",
            "cache-misses": "unknown",
        },
    }
    assert parse_pmu("")["available"] is False


class StuckContainer:
    async def show(self) -> dict[str, Any]:
        return {"State": {"Running": True}}

    async def exec(self, *args: object, **kwargs: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError


@pytest.mark.anyio
async def test_a_slow_cgroup_lookup_is_a_sampler_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources_module, "FIND_S", 0)
    monkeypatch.setattr(execs_module, "GRACE_S", 0.05)
    sampler = Sampler(cast(Any, None), {}, "s", "img")
    sampler._container = cast(Any, StuckContainer())
    with pytest.raises(SamplerError, match="timed out"):
        await sampler.watch("abc123", 50)


@dataclass
class Message:
    stream: int
    data: bytes


class FakeStream:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages

    async def read_out(self) -> Message | None:
        return self._messages.pop(0) if self._messages else None

    async def close(self) -> None:
        pass


class FakeExec:
    """An `aims-sample` exec that writes `messages` and exits `code`."""

    def __init__(self, code: int, *messages: Message) -> None:
        self._stream = FakeStream(list(messages))
        self._code = code

    def start(self, *, detach: bool) -> Any:
        stream = self._stream

        class Start:
            async def __aenter__(self) -> FakeStream:
                return stream

        return Start()

    async def inspect(self) -> dict[str, Any]:
        return {"ExitCode": self._code}


class FakeSampler:
    def __init__(self, *later: FakeExec) -> None:
        self.later = list(later)

    async def stream(self, cgroup: str, hz: int) -> FakeExec:
        return self.later.pop(0)


def drain(watch: Watch) -> list[Sample | Lost]:
    out: list[Sample | Lost] = []
    while not watch.queue.empty():
        out.append(watch.queue.get_nowait())
    return out


@pytest.mark.anyio
async def test_a_sampler_that_cannot_read_the_cgroup_is_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources_module, "RETRY_S", 0.01)
    line = Message(1, f"5 {' '.join(['7'] * COLUMNS)}\n".encode())
    denied = Message(2, b"/sys/fs/cgroup/x/cpu.stat: Permission denied\n")
    sampler = FakeSampler(FakeExec(3, denied), FakeExec(GONE))
    watch = Watch(cast(Any, sampler), "/sys/fs/cgroup/x", 10, cast(Any, FakeExec(3, line, denied)))
    await watch.ended(2)
    items = drain(watch)
    assert items[0] == Sample(5, (7,) * COLUMNS)
    assert items[1:] == [
        Lost(
            None,
            "resource sampler stopped: exit 3: /sys/fs/cgroup/x/cpu.stat: Permission denied",
        )
    ]
    assert watch.restarts == 2
    assert not sampler.later


@pytest.mark.anyio
@pytest.mark.parametrize("code", [0, GONE])
async def test_a_sampler_whose_cgroup_is_gone_just_ends(code: int) -> None:
    gone = Message(2, b"/sys/fs/cgroup/x/cpu.stat: No such file or directory\n")
    watch = Watch(cast(Any, FakeSampler()), "/sys/fs/cgroup/x", 10, cast(Any, FakeExec(code, gone)))
    await watch.ended(2)
    assert watch._reader.done()
    assert drain(watch) == []
    assert watch.restarts == 0


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


BURN = (
    'python3 -c "import time\nend = time.time() + 3\nwhile time.time() < end:\n'
    ' t = time.time()\n while time.time() - t < 0.2: pass\n time.sleep(0.2)"'
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        audit = "kernel" if os.environ.get("AIMS_REQUIRE_TRACER") else "auto"
        backend = await DockerBackend.open(audit=audit)
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        yield backend


def busy(window: ResourceSample) -> list[bool]:
    points = list(zip(window.values("ts") or [], window.values("cpu_usec") or [], strict=True))
    return [c2 - c1 > 0.5 * (t2 - t1) for (t1, c1), (t2, c2) in itertools.pairwise(points)]


@pytest.mark.docker
@pytest.mark.anyio
async def test_timeline_shows_cpu_phases(backend: DockerBackend, store: LocalEventStore) -> None:
    assert backend.sampler is not None
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    spec = SandboxSpec(image=IMAGE, sample_hz=200)
    a = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a1")
    b = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a2")
    try:
        await a.exec(BURN, timeout_s=30)
    finally:
        await a.close()
        await b.close()
        await backend.remove_run(run_id)

    for e in await store.events(run_id, types=["sandbox.started"]):
        config = e.data["config"]
        assert isinstance(config, dict)
        resources = config["resources"]
        assert isinstance(resources, dict)
        assert resources["hz"] == 200

    def timeline(agent: str) -> list[bool]:
        return [x for w in by_agent[agent] for x in busy(w)]

    by_agent: dict[str, list[ResourceSample]] = {"a1": [], "a2": []}
    for e in await store.events(run_id, limit=1000, types=["resource.sample"]):
        assert e.agent_id is not None
        by_agent[e.agent_id].append(ResourceSample.model_validate(e.data))
    hot, cold = timeline("a1"), timeline("a2")
    assert len(hot) > 400
    assert sum(cold) <= 2
    runs = [len(r) for r in "".join("#" if x else "." for x in hot).split(".") if r]
    assert len([n for n in runs if 25 <= n <= 55]) >= 5, runs
    for w in by_agent["a1"]:
        ts = w.values("ts") or []
        assert len({t // 1_000_000 for t in ts}) == 1
    rows = await store.samples(run_id, agent_id="a1")
    assert len(rows) == len(by_agent["a1"])


@pytest.mark.docker
@pytest.mark.anyio
async def test_killed_sandbox_keeps_its_last_window(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    spec = SandboxSpec(image=IMAGE, sample_hz=100)
    sb = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a1")
    try:
        await asyncio.sleep(1.5)
        await sb.stop()
        await asyncio.sleep(0.3)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    evs: list[Event] = await store.events(run_id, limit=1000)
    kinds = [e.type for e in evs]
    assert kinds.count("resource.sample") >= 1
    assert kinds.index("sandbox.stopped") > max(
        i for i, k in enumerate(kinds) if k == "resource.sample"
    )
    assert "audit.lost" not in kinds


@pytest.mark.docker
@pytest.mark.tracer
@pytest.mark.anyio
async def test_sandbox_records_hardware_counters(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    if backend.tracer is None:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            pytest.fail("the kernel tracer did not start")
        pytest.skip("the kernel tracer is not available here")
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    sb = await AuditedSandbox.start(
        backend, SandboxSpec(image=IMAGE), store, run_id=run_id, agent_id="a1"
    )
    await sb.close()

    config = (await store.events(run_id, types=["sandbox.started"]))[0].data["config"]
    assert isinstance(config, dict)
    pmu = config["pmu"]
    assert isinstance(pmu, dict)
    devices, events = pmu["devices"], pmu["events"]
    assert isinstance(pmu["available"], bool)
    assert isinstance(devices, list)
    assert "software" in devices
    assert isinstance(events, dict)
    assert set(events) == {"cpu-cycles", "instructions", "cache-misses"}


@pytest.mark.docker
@pytest.mark.tracer
@pytest.mark.anyio
async def test_tracer_counts_the_runs_every_probe_skips(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    tracer = backend.tracer
    if tracer is None:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            pytest.fail("the kernel tracer did not start")
        pytest.skip("the kernel tracer is not available here")
    preemption = tracer.preemption
    assert preemption is not None
    assert isinstance(preemption["preemptible"], bool)
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    sb = await AuditedSandbox.start(
        backend, SandboxSpec(image=IMAGE), store, run_id=run_id, agent_id="a1"
    )
    try:
        await sb.exec("cat /etc/hostname > /tmp/x", timeout_s=20)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    config = (await store.events(run_id, types=["sandbox.started"]))[0].data["config"]
    assert isinstance(config, dict)
    assert config["preemption"] == preemption
    # The close read every program's count, and each has its probe's name.
    names = {tracer._probes.get(prog, f"program {prog}") for prog in tracer._skips}
    assert {
        "fentry_vmlinux_security_file_open",
        "tracepoint_sched_sched_process_exec",
        "profile_s_1",
    } <= names
    if not preemption["preemptible"]:
        lost = await store.events(run_id, types=["audit.lost"])
        assert not [e for e in lost if "skipped" in str(e.data.get("reason"))]
    # The counts are also read once a second, between closes.
    read = tracer._skips_ts
    assert read is not None
    await asyncio.sleep(2.2)
    assert tracer._skips_ts is not None
    assert tracer._skips_ts > read


@pytest.mark.docker
@pytest.mark.anyio
async def test_sampler_exit_codes(backend: DockerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources_module, "RETRY_S", 0.05)
    sampler = backend.sampler
    assert sampler is not None
    container = await sampler._ensure()

    denied = Watch(sampler, "/root", 10, await sampler.stream("/root", 10))
    try:
        async with asyncio.timeout(10):
            item = await denied.queue.get()
        # The retries fail the same way and add no gap.
        await asyncio.sleep(1)
    finally:
        await denied.close()
    assert denied.restarts >= 2
    assert item == Lost(None, "resource sampler stopped: exit 3: /root/cpu.stat: Permission denied")
    assert denied.queue.empty()

    missing = Watch(sampler, "/nonexistent", 10, await sampler.stream("/nonexistent", 10))
    await missing.ended(10)
    assert missing._reader.done()
    assert missing.queue.empty()
    assert missing.restarts == 0

    # A reader that leaves is not a removed cgroup.
    cgroup = await sampler._find(container, container.id)
    script = '{ { /aims/aims-sample 50 "$0"; echo $? >&3; } | head -c 1 >/dev/null; } 3>&1'
    out = await execs_module.exec_output(container, ["sh", "-c", script, cgroup], timeout_s=10)
    assert out == b"4\n"


@pytest.mark.docker
@pytest.mark.anyio
async def test_a_closed_watch_ends_its_sampler(backend: DockerBackend) -> None:
    sampler = backend.sampler
    assert sampler is not None
    container = await sampler._ensure()
    # The sampler's own cgroup lives on, so only the closed attach can end the exec.
    cgroup = await sampler._find(container, container.id)
    trace = await sampler.stream(cgroup, 10)
    watch = Watch(sampler, cgroup, 10, trace)
    try:
        async with asyncio.timeout(10):
            await watch.queue.get()
        assert (await trace.inspect())["Running"]
    finally:
        await watch.close()
    async with asyncio.timeout(2):
        while (info := await trace.inspect())["Running"]:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    assert info["ExitCode"] == 4

    # A stdin that is not a pipe or socket, such as /dev/null, is not watched.
    script = (
        'true | /aims/aims-sample 10 "$0" >/dev/null; echo $?;'
        ' timeout -s KILL 1 /aims/aims-sample 10 "$0" </dev/null >/dev/null; echo $?'
    )
    out = await execs_module.exec_output(container, ["sh", "-c", script, cgroup], timeout_s=10)
    assert out == b"4\n137\n"


@pytest.mark.docker
@pytest.mark.anyio
@pytest.mark.parametrize("death", ["kill", "delete"])
async def test_sampler_death_is_a_gap(store: LocalEventStore, death: str) -> None:
    async with await DockerBackend.open(audit="snapshot") as snapshot:
        sampler = snapshot.sampler
        assert sampler is not None
        dead = sampler._container
        assert dead is not None
        run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
        sb = await AuditedSandbox.start(
            snapshot, SandboxSpec(image=IMAGE), store, run_id=run_id, agent_id="a1"
        )
        try:
            assert sb.sandbox.audit == "snapshot"
            await asyncio.sleep(1.5)
            await (dead.kill() if death == "kill" else dead.delete(force=True))
            await asyncio.sleep(2.5)
            later = await AuditedSandbox.start(
                snapshot, SandboxSpec(image=IMAGE), store, run_id=run_id, agent_id="a2"
            )
            await later.close()
        finally:
            await sb.close()
            await snapshot.remove_run(run_id)
        assert sampler._container is not dead

    evs = await store.events(run_id, limit=1000)
    mine = [e for e in evs if e.agent_id == "a1"]
    lost = [i for i, e in enumerate(mine) if e.type == "audit.lost"]
    assert len(lost) == 1
    assert str(mine[lost[0]].data["reason"]).startswith("resource sampler stopped")
    samples = [i for i, e in enumerate(mine) if e.type == "resource.sample"]
    assert samples[0] < lost[0] < samples[-1]
    started = [e for e in evs if e.type == "sandbox.started" and e.agent_id == "a2"]
    config = started[0].data["config"]
    assert isinstance(config, dict)
    assert config["resources"] is not None


@pytest.mark.docker
@pytest.mark.tracer
@pytest.mark.anyio
async def test_samples_share_the_kernel_clock(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    if backend.tracer is None:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            pytest.fail("the kernel tracer did not start")
        pytest.skip("the kernel tracer is not available here")
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    spec = SandboxSpec(image=IMAGE, sample_hz=200)
    sb = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a1")
    try:
        await asyncio.sleep(1)
        await sb.exec("sleep 0.5; timeout 1 sh -c 'while :; do :; done'", timeout_s=20)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    procs = await store.events(run_id, types=["proc.exec"])
    loop = next(e.data for e in procs if e.data["argv"] == ["sh", "-c", "while :; do :; done"])
    started = int(str(loop["ts_kernel"]))
    points: list[tuple[int, int]] = []
    for e in await store.events(run_id, limit=1000, types=["resource.sample"]):
        w = ResourceSample.model_validate(e.data)
        points += zip(w.values("ts") or [], w.values("cpu_usec") or [], strict=True)
    flags = [(t1, c2 - c1 > 0.5 * (t2 - t1)) for (t1, c1), (t2, c2) in itertools.pairwise(points)]
    hot = next(t for i, (t, _) in enumerate(flags) if all(f for _, f in flags[i : i + 10]))
    assert started - 20_000 <= hot <= started + 50_000, hot - started
