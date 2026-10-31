"""A series queued through the API: every run at once, each after the first held until the one
before it completed and its truth file arrived, the rest cancelled with a reason when one does
not complete."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_server import FakeRunner, job_state, ship, start_run, until

from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, RunStatus
from aims.events.blobs import decode
from aims.node.client import ApiError
from aims.run import truth
from aims.run.job import Job
from aims.server import api
from aims.wire import OfferMessage

pytestmark = pytest.mark.anyio

RECORD: dict[str, Any] = {"variant": None, "plants": {}, "canaries": {}}


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


@pytest.fixture
async def fake(cp: CP) -> AsyncIterator[FakeRunner]:
    runner = FakeRunner(cp)
    await runner.connect()
    yield runner
    await runner.close()


async def series(cp: CP, name: str = "nightly", runs: int = 3, **body: Any) -> list[str]:
    return await start_run(cp, series=name, runs=runs, **body)


async def play(
    fake: FakeRunner,
    local: LocalEventStore,
    run_id: str,
    status: RunStatus = "completed",
    record: dict[str, Any] | None = RECORD,
) -> Job:
    """Claim the series job `run_id`, record its run with the truth file it committed to, seal
    it with `status` and ship it all as a runner would."""
    job = await fake.client.claim(run_id)
    assert isinstance(job, Job)
    assert job.options.series is not None
    started = RunStarted(
        scenario="control",
        oversight="observe",
        config={"scenario": job.scenario},
        truth=None if record is None else truth.digest(record),
        series=job.options.series,
    )
    blobs = [await fake.client.job_blob(run_id, sha) for sha in job.blobs()]
    await local.create_run(started, blobs=[decode(b) for b in blobs], run_id=run_id)
    if record is not None:
        truth.write(local.root / run_id, record)
    await local.append(run_id, [Draft(RunFinished(status=status))])
    await ship(local, fake.client, run_id)
    return job


async def offered(fake: FakeRunner, within: float = 10) -> str:
    return (await fake.next(OfferMessage, within)).run_id


async def reason(cp: CP, run_id: str) -> tuple[str, str | None]:
    async with cp.client(cp.viewer) as c:
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
    return run["state"], run["reason"]


async def test_a_series_needs_a_name_and_one_sample_each(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        scenario = {"source": "project", "name": "control"}
        for body, field in [
            ({"runs": 2}, "runs"),
            ({"restart": True}, "restart"),
            ({"series": "  "}, "series"),
            ({"series": "s", "samples": 2}, "samples"),
            ({"series": "s", "runs": 0}, "runs"),
            ({"series": "s", "runs": 101}, "runs"),
            ({"series": "s" * 101}, "series"),
        ]:
            r = await c.post("/api/runs", json={"scenario": scenario, **body})
            assert r.status_code == 422, (body, r.text)
            assert field in r.text, (body, r.text)


async def test_a_series_queues_every_run_and_holds_all_but_the_first(
    cp: CP, fake: FakeRunner
) -> None:
    ids = await series(cp)
    assert len(ids) == 3
    jobs = [await cp.state.db.job(i) for i in ids]
    assert [j.state if j else None for j in jobs] in (
        ["offered", "waiting", "waiting"],
        ["queued", "waiting", "waiting"],
    )
    places = [Job.model_validate(j.payload).options.series for j in jobs if j]
    assert [(p.name, p.index, p.previous) for p in places if p] == [
        ("nightly", 0, None),
        ("nightly", 1, ids[0]),
        ("nightly", 2, ids[1]),
    ]
    assert all(j is not None and j.group is None for j in jobs)
    assert await offered(fake) == ids[0]
    with pytest.raises(TimeoutError):
        await offered(fake, within=1.5)
    async with cp.client(cp.viewer) as c:
        page = (await c.get("/api/runs", params={"series": "nightly"})).json()["items"]
        assert sorted((r["series_index"], r["run_id"], r["state"]) for r in page) == [
            (0, ids[0], "offered"),
            (1, ids[1], "waiting"),
            (2, ids[2], "waiting"),
        ]
        assert {r["series"] for r in page} == {"nightly"}
        assert (await c.get("/api/runs", params={"series": "other"})).json()["items"] == []
    assert await reason(cp, ids[1]) == ("waiting", f"waits for #0 (run {ids[0]})")
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={"scenario": {"source": "project", "name": "control"}, "series": "nightly"},
        )
        assert (r.status_code, r.json()["code"]) == (409, "series_busy")
        assert ids[0] in r.json()["detail"]
        found = (await c.get("/api/series/nightly")).json()
        assert found["busy"] == ids[0]
        assert [(r["run_id"], r["series_index"]) for r in found["runs"]] == [
            (ids[0], 0),
            (ids[1], 1),
            (ids[2], 2),
        ]


async def test_each_run_starts_once_the_one_before_completed_and_its_truth_arrived(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    ids = await series(cp, runs=2)
    assert await offered(fake) == ids[0]
    await play(fake, local, ids[0])
    assert await offered(fake) == ids[1]
    job = await play(fake, local, ids[1])
    assert job.options.series is not None
    assert (job.options.series.index, job.options.series.previous) == (1, ids[0])
    async with cp.client(cp.viewer) as c:
        found = (await c.get("/api/series/nightly")).json()
        assert (found["next_index"], found["previous"], found["busy"]) == (2, ids[1], None)
        detail = (await c.get(f"/api/runs/{ids[1]}")).json()
        assert detail["series_previous"] == ids[0]
        detail = (await c.get(f"/api/runs/{ids[0]}")).json()
        assert detail["series_previous"] is None


async def test_a_run_waits_for_the_truth_file_and_the_tick_releases_it(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await series(cp, runs=2)
    assert await offered(fake) == ids[0]
    job = await fake.client.claim(ids[0])
    assert isinstance(job, Job)
    started = RunStarted(
        scenario="control",
        oversight="observe",
        truth=truth.digest(RECORD),
        series=job.options.series,
    )
    await local.create_run(started, run_id=ids[0])
    truth.write(local.root / ids[0], RECORD)
    await local.append(ids[0], [Draft(RunFinished(status="completed"))])
    held = truth.path(local.root / ids[0]).rename(local.root / "held.json")
    await ship(local, fake.client, ids[0])
    waits = ("waiting", f"waits for the truth file of #0 (run {ids[0]})")
    await until(lambda: reason(cp, ids[1]), waits)
    monkeypatch.setattr(cp.state, "on_truth", None)
    held.rename(truth.path(local.root / ids[0]))
    await ship(local, fake.client, ids[0])
    assert await offered(fake) == ids[1]


async def test_a_series_stops_when_a_run_does_not_complete(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    ids = await series(cp)
    assert await offered(fake) == ids[0]
    await play(fake, local, ids[0], status="failed")
    await until(lambda: job_state(cp, ids[2]), "cancelled")
    assert await reason(cp, ids[1]) == ("cancelled", f"series stopped: #0 failed (run {ids[0]})")
    assert await reason(cp, ids[2]) == (
        "cancelled",
        f"series stopped: #1 cancelled (run {ids[1]})",
    )
    async with cp.client(cp.viewer) as c:
        found = (await c.get("/api/series/nightly")).json()
        assert (found["next_index"], found["previous"], found["busy"]) == (3, None, None)


async def test_cancelling_a_waiting_run_cancels_the_ones_after_it(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    ids = await series(cp)
    assert await offered(fake) == ids[0]
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{ids[1]}/cancel", json={"reason": "enough"})
        assert r.json()["state"] == "cancelled"
    assert await reason(cp, ids[1]) == ("cancelled", "enough")
    assert await reason(cp, ids[2]) == (
        "cancelled",
        f"series stopped: #1 cancelled (run {ids[1]})",
    )
    await play(fake, local, ids[0])
    with pytest.raises(TimeoutError):
        await offered(fake, within=1.5)
    assert await job_state(cp, ids[0]) == "done"


async def test_a_series_goes_on_after_its_last_completed_run_unless_restarted(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    async with cp.client(cp.viewer) as c:
        found = (await c.get("/api/series/nightly")).json()
        assert found == {
            "name": "nightly",
            "next_index": 0,
            "previous": None,
            "busy": None,
            "runs": [],
        }
    (first,) = await series(cp, runs=1)
    assert await offered(fake) == first
    await play(fake, local, first)
    await until(lambda: job_state(cp, first), "done")
    more = await series(cp, runs=2)
    jobs = [await cp.state.db.job(i) for i in more]
    places = [Job.model_validate(j.payload).options.series for j in jobs if j]
    assert [(p.index, p.previous) for p in places if p] == [(1, first), (2, more[0])]
    async with cp.client(cp.operator) as c:
        for run_id in more:
            await c.post(f"/api/runs/{run_id}/cancel", json={})
    again = await series(cp, runs=1, restart=True)
    job = await cp.state.db.job(again[0])
    assert job is not None
    place = Job.model_validate(job.payload).options.series
    assert place is not None
    assert (place.index, place.previous) == (0, None)


async def test_a_series_job_reads_the_run_before_it_and_no_one_else_does(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    ids = await series(cp, runs=2)
    assert await offered(fake) == ids[0]
    await play(fake, local, ids[0])
    await fake.disconnect()
    other = FakeRunner(cp, "other")
    third = FakeRunner(cp, "third")
    try:
        await other.connect()
        assert await offered(other) == ids[1]
        assert await other.client.claim(ids[1]) is not None
        assert await other.client.run_log(ids[0])
        assert truth.encode(RECORD) == await other.client.run_truth(ids[0])
        await third.client.hello(third.hello_body())
        with pytest.raises(ApiError) as refused:
            await third.client.run_truth(ids[0])
        assert refused.value.status == 403
    finally:
        await other.close()
        await third.close()


async def test_the_estimate_of_a_series_counts_its_runs(
    cp: CP, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real = api.estimate_runs

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["samples"])
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "estimate_runs", spy)
    await series(cp, runs=4)
    assert seen == [4]


async def test_a_busy_series_says_so_before_the_cost_asks_for_a_yes(
    cp: CP, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    await series(cp, runs=1)
    monkeypatch.setattr(api, "needs_confirm", lambda cost: "the caps allow more than the limit")
    async with cp.client(cp.operator) as c:
        body = {"scenario": {"source": "project", "name": "control"}, "monitor": "signals"}
        r = await c.post("/api/runs", json={**body, "series": "nightly"})
        assert (r.status_code, r.json()["code"]) == (409, "series_busy")
        r = await c.post("/api/runs", json={**body, "series": "other"})
        assert (r.status_code, r.json()["code"]) == (409, "confirm")
