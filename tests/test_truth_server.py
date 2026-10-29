"""The control plane's side of a run's hidden half: truth files shipped by their own path once a
run seals, read by operators only, the variant and series they put in the run list, the cost
rows that keep the history from reading every log again, and the live projection."""

import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_estimate import load_scenario, record, write
from test_server import FakeRunner, ship

from aims.events import (
    AgentStarted,
    AgentStep,
    Draft,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    RunFinished,
    RunStarted,
    Series,
)
from aims.events.chain import Head as LogHead
from aims.events.store import TruthRefused
from aims.node.client import ControlClient
from aims.run import truth
from aims.run.estimate import history, projection

pytestmark = pytest.mark.anyio

RECORD: dict[str, Any] = {"variant": "unobserved", "plants": {}, "canaries": {}}


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


async def sealed(
    store: LocalEventStore, record: dict[str, Any] | None = RECORD, index: int = 0
) -> str:
    """A run of series `nightly` that committed to `record` as its truth, written beside its
    log as the runner writes it, then sealed."""
    started = RunStarted(
        scenario="pushed",
        oversight="observe",
        agents=["a1"],
        truth=None if record is None else truth.digest(record),
        series=Series(name="nightly", index=index),
    )
    run_id = await store.create_run(started)
    if record is not None:
        truth.write(store.root / run_id, record)
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    return run_id


async def copy(cp: CP, local: LocalEventStore, run_id: str) -> None:
    """Put run `run_id` of `local` on the control plane as a runner's shipper would."""
    store = cp.state.store
    store.prepare_run(run_id)
    for path in (local.root / run_id / "blobs").glob("*"):

        async def chunks(data: bytes = path.read_bytes()) -> AsyncIterator[bytes]:
            yield data

        await store.put_blob_stream(run_id, path.name, chunks(), limit=1 << 30)
    head = await local.head(run_id)
    assert head is not None
    lines, _ = await local.lines(run_id, LogHead(), upto=head.seq, max_events=100_000)
    await store.ingest(run_id, lines)


async def test_a_sealed_run_names_its_series_and_its_variant(local: LocalEventStore) -> None:
    first = await sealed(local)
    second = await sealed(local, {"variant": "observed"}, index=1)
    plain = await sealed(local, None, index=2)
    info = await local.run(first)
    assert info is not None
    assert (info.series, info.series_index, info.variant) == ("nightly", 0, "unobserved")
    assert [i.run_id for i in await local.runs(variant="observed")] == [second]
    assert {i.run_id for i in await local.runs(series="nightly")} == {first, second, plain}
    assert await local.runs(series="other") == []

    await local.rebuild()
    again = await local.run(first)
    assert again is not None
    assert (again.series, again.variant) == ("nightly", "unobserved")


async def test_a_sealed_run_ships_its_truth_by_its_own_path(cp: CP, local: LocalEventStore) -> None:
    run_id = await sealed(local)
    await FakeRunner(cp, "pusher").client.hello(FakeRunner(cp, "pusher").hello_body())
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        shipper = await ship(local, client, run_id)
        assert (shipper.done, shipper.truthful, shipper.truth_refused) == (True, True, None)
        heads = await client.heads([run_id])
        assert heads[run_id].truth
        again = await ship(local, client, run_id)
        assert again.truthful
    finally:
        await client.aclose()

    kept = cp.state.store.truth_path(run_id)
    assert kept.read_bytes() == truth.path(local.root / run_id).read_bytes()
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert "unobserved" not in (kept.parent / "events.jsonl").read_text()
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{run_id}/truth")
        assert r.status_code == 403
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["series"], run["series_index"], run["variant"]) == ("nightly", 0, "unobserved")
        page = (await c.get("/api/runs", params={"variant": "unobserved"})).json()
        assert [r["run_id"] for r in page["items"]] == [run_id]
        page = (await c.get("/api/runs", params={"series": "nightly"})).json()
        assert [r["run_id"] for r in page["items"]] == [run_id]
        page = (await c.get("/api/runs", params={"variant": "observed"})).json()
        assert page["items"] == []
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{run_id}/truth")
        assert r.status_code == 200
        assert r.json() == RECORD
        r = await c.get("/api/runs/01a18000-0000-7000-8000-000000000000/truth")
        assert r.status_code == 404


async def test_the_control_plane_checks_a_truth_file_before_it_keeps_it(
    cp: CP, local: LocalEventStore, tmp_path: Path
) -> None:
    store = cp.state.store
    data = truth.encode(RECORD)
    with pytest.raises(TruthRefused) as caught:
        await store.put_truth("01a18000-0000-7000-8000-000000000000", data)
    assert caught.value.status == 404

    started = RunStarted(scenario="x", oversight="observe", truth=truth.digest(RECORD))
    running = await local.create_run(started)
    await copy(cp, local, running)
    with pytest.raises(TruthRefused) as caught:
        await store.put_truth(running, data)
    assert caught.value.status == 409

    await local.append(running, [Draft(RunFinished(status="completed"))])
    await copy(cp, local, running)
    with pytest.raises(TruthRefused) as caught:
        await store.put_truth(running, truth.encode({"variant": "other"}))
    assert caught.value.status == 422
    assert "committed to" in str(caught.value)
    assert not store.truth_path(running).exists()
    assert await store.put_truth(running, data)
    assert not await store.put_truth(running, data)

    blank = await local.create_run(RunStarted(scenario="x", oversight="observe"))
    await local.append(blank, [Draft(RunFinished(status="completed"))])
    await copy(cp, local, blank)
    with pytest.raises(TruthRefused) as caught:
        await store.put_truth(blank, data)
    assert caught.value.status == 409

    async with cp.client(cp.runner) as c:
        await FakeRunner(cp, "pusher").client.hello(FakeRunner(cp, "pusher").hello_body())
        await cp.state.db.attribute(running, "pusher", "push")
        r = await c.put(
            f"/runner/v1/runs/{running}/truth",
            content=truth.encode({"variant": "forged"}),
            headers={"X-Aims-Runner": "pusher", "Content-Type": "application/json"},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "truth"


async def test_a_refused_truth_file_does_not_hold_the_shipper(
    cp: CP, local: LocalEventStore
) -> None:
    run_id = await sealed(local)
    truth.write(local.root / run_id, {"variant": "changed after the run"})
    await FakeRunner(cp, "pusher").client.hello(FakeRunner(cp, "pusher").hello_body())
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert shipper.done
    assert shipper.truthful
    assert shipper.truth_refused is not None
    assert "422" in shipper.truth_refused
    assert not cp.state.store.truth_path(run_id).exists()


# The history's cost rows.


async def test_the_history_reads_each_sealed_run_once(
    tmp_path: Path, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, path = write(tmp_path)
    bundle = load_scenario(path)
    for cost in (0.2, 0.4):
        await record(local, settings, bundle, cost)
    reads: list[str] = []
    events = local.events

    async def counted(run_id: str, **kw: Any) -> Any:
        reads.append(run_id)
        return await events(run_id, **kw)

    monkeypatch.setattr(local, "events", counted)
    first = await history(local)
    assert len(set(reads)) == 2
    reads.clear()
    second = await history(local)
    assert reads == []
    assert [p.agents for p in second.runs] == [p.agents for p in first.runs]
    assert second.spend == first.spend
    assert second.shares == first.shares


async def test_a_truth_file_that_arrives_later_is_read_into_the_history(
    cp: CP, local: LocalEventStore
) -> None:
    store = cp.state.store
    started = RunStarted(scenario="x", oversight="observe", truth=truth.digest(RECORD))
    run_id = await local.create_run(started)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await copy(cp, local, run_id)
    await history(store)
    assert run_id in await store.costs()
    assert await store.put_truth(run_id, truth.encode(RECORD))
    assert run_id not in await store.costs()
    await history(store)
    assert run_id in await store.costs()


# The live projection.


async def test_a_running_run_projects_its_cost(cp: CP, local: LocalEventStore) -> None:
    store = cp.state.store
    run_id = await local.create_run(RunStarted(scenario="pushed", oversight="observe"))
    body = await local.put_blob(run_id, b"{}")
    started = AgentStarted(
        profile="p", model="m", backend="chat", tools=[], system="s", budget={"steps": 10}
    )
    step = AgentStep.model_validate(
        {"n": 3, "call_id": "m3", "status": "completed", "tool_calls": [], "total_cost_usd": 0.3}
    )
    request = ModelRequest(
        call_id="w1",
        attempt=1,
        profile="sweep",
        backend="chat",
        model="wonder",
        url="u",
        params=ModelParams(stream=False),
        body=body,
        key=body.sha256,
        role="sweep",
    )
    response = ModelResponse(
        call_id="w1", attempt=1, status="completed", latency_ms=1, cost_usd=0.06
    )
    await local.append(
        run_id,
        [Draft(started, "a1"), Draft(step, "a1"), Draft(request), Draft(response)],
    )
    await copy(cp, local, run_id)
    found = await projection(store, run_id, await history(store))
    assert found is not None
    assert found.agents_spent_usd == pytest.approx(0.3)
    assert found.monitor_spent_usd == pytest.approx(0.06)
    assert found.agents_usd == pytest.approx(1.0)
    assert found.monitor_usd == pytest.approx(0.2)
    assert found.on_track_usd == pytest.approx(1.2)
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{run_id}/projection")
        assert r.status_code == 200
        assert r.json()["on_track_usd"] == pytest.approx(1.2)
        r = await c.get("/api/runs/01a18000-0000-7000-8000-000000000000/projection")
        assert r.status_code == 404
