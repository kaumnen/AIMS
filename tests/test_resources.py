import asyncio
import itertools
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aims.events import Event, LocalEventStore, ResourceSample, RunStarted
from aims.sandbox import AuditedSandbox, DockerBackend, SandboxSpec
from aims.sandbox.base import Sample
from aims.sandbox.resources import Windows, parse_sample
from aims.sandbox.tracer import parse_pmu

COLUMNS = len(ResourceSample.COLUMNS)
IMAGE = "python:3.13-slim"


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
    ResourceSample(sandbox_id="sb", hz=10, ts=[1], **columns)
    with pytest.raises(ValidationError):
        ResourceSample(sandbox_id="sb", hz=10, ts=[1, 2], **columns)
    with pytest.raises(ValidationError):
        ResourceSample(sandbox_id="sb", hz=10, ts=[], **{n: [] for n in ResourceSample.COLUMNS})


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
        backend = await DockerBackend.open(audit="auto")
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
