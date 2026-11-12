"""Deleting runs and scenarios on a control plane, keeping runs a number of days and runners
that never ship a deleted run back."""

import asyncio
import contextlib
import dataclasses
import io
import json
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cp import CP, control_plane
from test_cli_monitor import node, queue_monitor
from test_monitor_api import call, pushed
from test_server import (
    FakeRunner,
    log_of,
    post_tar,
    push_forged,
    raw_events,
    run_tar,
    ship,
    sse,
    start_run,
    until,
)
from typer.testing import CliRunner

from aims.cli import app
from aims.config import MAX_KEEP_DAYS
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall, split_lines
from aims.events import store as event_store
from aims.events.index import Index
from aims.events.schema import format_ts
from aims.models import DeltaBus
from aims.monitor import offline
from aims.node.link import Link
from aims.node.service import Node, NodeOptions
from aims.run.job import Job as RunJob
from aims.server import dispatch, retention
from aims.server.db import Job, JobState
from aims.server.serve import UiOptions, _spawn_runner
from aims.wire import MonitorJob, ScoreJob

pytestmark = pytest.mark.anyio


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


@pytest.fixture
async def runner(cp: CP) -> AsyncIterator[FakeRunner]:
    made = FakeRunner(cp, "r1")
    await made.client.hello(made.hello_body())
    yield made
    await made.client.aclose()


async def made(
    cp: CP,
    local: LocalEventStore,
    runner: FakeRunner,
    *,
    kind: Any = "scenario",
    origin: str | None = None,
    monitor: str | None = None,
    sealed: bool = True,
) -> str:
    """A run `runner` shipped: of `kind` over `origin`; a score of the monitor run `monitor`."""
    started = RunStarted(
        scenario="control",
        oversight="observe",
        agents=["a"],
        kind=kind,
        origin=origin,
        config={} if monitor is None else {"monitor": monitor},
    )
    run_id = await local.create_run(started)
    drafts = [Draft(ToolCall(call_id="c", name="shell", arguments="{}"), "a")]
    if sealed:
        drafts.append(Draft(RunFinished(status="completed")))
    await local.append(run_id, drafts)
    if sealed:
        await ship(local, runner.client, run_id)
    else:
        lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
        assert (await raw_events(cp, runner.name, run_id, lines)).status_code == 200
    return run_id


def job(state: JobState, payload: dict[str, Any], *, at: float | None = None) -> Job:
    at = time.time() if at is None else at
    return Job(
        run_id=str(uuid.uuid7()),
        group=None,
        parallel=None,
        state=state,
        payload=payload,
        needs={"kind": "nothing runs this"},
        target=None,
        runner=None,
        offered_at=None,
        tried=[],
        token="op",
        created=at,
        updated=at,
        reason=None,
    )


async def gone(cp: CP, run_id: str) -> bool:
    return (
        await cp.state.store.run(run_id) is None
        and not (cp.state.store.root / run_id).exists()
        and await cp.state.db.job(run_id) is None
        and await cp.state.db.run_meta(run_id) is None
        and await cp.state.db.commands_of(run_id) == []
    )


# Deleting a run


async def test_a_run_goes_with_every_run_made_from_it_and_every_row_of_them(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run = await made(cp, local, runner)
    review = await made(cp, local, runner, kind="monitor", origin=run)
    score = await made(cp, local, runner, kind="score", origin=run, monitor=review)
    second = await made(cp, local, runner, kind="monitor", origin=review)
    failed = job("done", {"kind": "score", "origin": run, "scenario": "control"})
    await cp.state.db.add_jobs([failed])
    sent = await cp.state.db.add_command("r1", run, {"action": "pause"})
    assert await cp.state.db.ack(sent.id, "r1", "done")
    other = await made(cp, local, runner)
    other_review = await made(cp, local, runner, kind="monitor", origin=other)
    family = {review: "monitor", score: "score", second: "monitor", failed.run_id: "score"}
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{run}/dependents")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["blocked"] is None
        assert {d["run_id"]: d["kind"] for d in body["runs"]} == family
        r = await c.get(f"/api/runs/{review}/dependents")
        assert {d["run_id"] for d in r.json()["runs"]} == {score, second}
        r = await c.delete(f"/api/runs/{run}")
        assert r.status_code == 200, r.text
        assert sorted(r.json()["run_ids"]) == sorted([run, *family])
        assert (await c.get(f"/api/runs/{run}")).status_code == 404
        assert (await c.delete(f"/api/runs/{run}")).status_code == 404
        listed = {x["run_id"] for x in (await c.get("/api/runs")).json()["items"]}
        assert listed == {other, other_review}
    for run_id in (run, *family):
        assert await gone(cp, run_id), run_id
    assert await cp.state.db.deleted([run, *family, other]) == {run, *family}
    assert await cp.state.store.run(other) is not None
    assert await cp.state.store.run(other_review) is not None


async def test_a_delete_is_refused_while_the_run_or_its_family_is_in_use(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    live = FakeRunner(cp, "live")
    await live.connect()
    running = await made(cp, local, live, sealed=False)
    watched = await made(cp, local, runner)
    await made(cp, local, live, kind="monitor", origin=watched, sealed=False)
    queued = await made(cp, local, runner)
    forked = await made(cp, local, runner)
    jobs = [
        job("queued", {"kind": "monitor", "origin": queued, "scenario": "control"}),
        job("waiting", {"kind": "scenario", "fork": {"origin": forked}}),
    ]
    await cp.state.db.add_jobs(jobs)
    async with cp.client(cp.viewer) as c:
        assert (await c.delete(f"/api/runs/{watched}")).status_code == 403
        assert (await c.get(f"/api/runs/{watched}/dependents")).status_code == 403
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{uuid.uuid7()}")).status_code == 404
        assert (await c.delete("/api/runs/not-a-run")).status_code == 404
        assert (await c.get(f"/api/runs/{uuid.uuid7()}/dependents")).status_code == 404
        for run_id, why in (
            (running, "still running on runner live"),
            (watched, "still running on runner live"),
            (queued, "queued"),
            (forked, "waiting"),
        ):
            r = await c.delete(f"/api/runs/{run_id}", params={"force": "true"})
            assert r.status_code == 409, r.text
            assert r.json()["code"] == "in_use"
            assert why in r.json()["detail"]
            found = (await c.get(f"/api/runs/{run_id}/dependents")).json()
            assert (found["blocked"], found["offline"]) == (r.json()["detail"], None)
    await live.close()
    for run_id in (running, watched, queued, forked):
        assert await cp.state.store.run(run_id) is not None
    assert await cp.state.db.deleted([running, watched, queued, forked]) == set()


async def test_a_run_whose_runner_is_offline_goes_only_with_force(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    stuck = await made(cp, local, runner, sealed=False)
    sealed = await made(cp, local, runner)
    held = job("running", {"kind": "monitor", "origin": sealed, "scenario": "control"})
    await cp.state.db.add_jobs([dataclasses.replace(held, runner="gone")])
    async with cp.client(cp.operator) as c:
        for run_id, why in (
            (stuck, f"run {stuck} is marked running but its runner r1 is offline"),
            (sealed, f"run {held.run_id} is running on runner gone, which is offline"),
        ):
            found = (await c.get(f"/api/runs/{run_id}/dependents")).json()
            assert (found["blocked"], found["offline"]) == (None, why)
            r = await c.delete(f"/api/runs/{run_id}")
            assert (r.status_code, r.json()["code"]) == (409, "offline")
            assert r.json()["detail"] == f"{why}. Force deletes it."
            assert await cp.state.store.run(run_id) is not None
        r = await c.delete(f"/api/runs/{stuck}", params={"force": "true"})
        assert (r.status_code, r.json()["run_ids"]) == (200, [stuck])
        r = await c.delete(f"/api/runs/{sealed}", params={"force": "true"})
        assert sorted(r.json()["run_ids"]) == sorted([sealed, held.run_id])
    for run_id in (stuck, sealed, held.run_id):
        assert await gone(cp, run_id), run_id
    queued = await made(cp, local, runner, sealed=False)
    await cp.state.db.add_jobs(
        [job("queued", {"kind": "monitor", "origin": queued, "scenario": "control"})]
    )
    async with cp.client(cp.operator) as c:
        r = await c.delete(f"/api/runs/{queued}", params={"force": "true"})
        assert (r.status_code, r.json()["code"]) == (409, "in_use")


async def test_a_run_whose_folder_went_before_its_rows_reads_as_unknown(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await push_forged(cp, local, "http://model.invalid/v1")
    entered, opened = asyncio.Event(), asyncio.Event()
    real = Index.remove

    async def remove(index: Index, run: str) -> None:
        entered.set()
        await opened.wait()
        await real(index, run)

    monkeypatch.setattr(Index, "remove", remove)
    async with cp.client(cp.operator) as c:
        deleting = asyncio.create_task(c.delete(f"/api/runs/{run_id}"))
        try:
            await entered.wait()
            assert not (cp.state.store.root / run_id).exists()
            assert await cp.state.store.run(run_id) is not None
            asks: list[tuple[str, str, dict[str, str] | None, dict[str, str] | None]] = [
                ("GET", "score/estimate", None, None),
                ("GET", "monitor/estimate", {"preset": "signals"}, None),
                ("POST", "monitor", None, {"preset": "signals"}),
                ("GET", "forks/estimate", None, None),
                ("POST", "forks", None, {"monitor": "signals"}),
            ]
            for method, path, params, body in asks:
                url = f"/api/runs/{run_id}/{path}"
                r = await c.request(method, url, params=params, json=body)
                assert (r.status_code, r.json()["detail"]) == (422, f"unknown run {run_id}"), path
        finally:
            opened.set()
        assert (await deleting).status_code == 200
    assert await gone(cp, run_id)


# Keeping runs for a number of days


def back(days: float) -> str:
    return format_ts(datetime.now(UTC) - timedelta(days=days))


async def test_keep_days_deletes_old_runs_with_their_family_and_skips_those_in_use(
    cp: CP, local: LocalEventStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(event_store, "now", lambda: back(10))
        old = await made(cp, local, runner)
        busy = await made(cp, local, runner)
        held = await made(cp, local, runner)
        still = await made(cp, local, runner, sealed=False)
    review = await made(cp, local, runner, kind="monitor", origin=old)
    await made(cp, local, runner, kind="monitor", origin=busy, sealed=False)
    young = await made(cp, local, runner)
    long_ago = time.time() - 10 * 86400
    dropped = job("cancelled", {"kind": "scenario"}, at=long_ago)
    recent = job("cancelled", {"kind": "scenario"})
    reads = job("queued", {"kind": "monitor", "origin": held, "scenario": "control"})
    await cp.state.db.add_jobs([dropped, recent, reads])
    cp.state.store.prepare_run(dropped.run_id)
    deleted = await retention.prune(cp.state, 5)
    assert sorted(deleted) == sorted([old, review, dropped.run_id])
    for run_id in deleted:
        assert await gone(cp, run_id), run_id
    for run_id in (busy, held, still, young):
        assert await cp.state.store.run(run_id) is not None, run_id
    assert await cp.state.db.job(recent.run_id) is not None
    assert await cp.state.db.job(reads.run_id) is not None
    assert await retention.prune(cp.state, 5) == []


async def test_keep_days_at_its_most_reaches_back_to_a_date_a_timestamp_holds(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    kept = await made(cp, local, runner)
    assert await retention.prune(cp.state, MAX_KEEP_DAYS) == []
    assert await cp.state.store.run(kept) is not None


async def test_housekeeping_keeps_runs_the_days_the_server_was_given(
    cp: CP, local: LocalEventStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(event_store, "now", lambda: back(3))
        run_id = await made(cp, local, runner)
    await dispatch.housekeeping(cp.state)
    assert await cp.state.store.run(run_id) is not None
    cp.state.keep_days = 2
    await dispatch.housekeeping(cp.state)
    assert await gone(cp, run_id)


async def test_the_dispatcher_keeps_house_as_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aims.server.state import close_state, open_state

    state = await open_state(tmp_path / "server")
    kept = asyncio.Event()

    async def housekeeping(_: Any) -> None:
        kept.set()

    monkeypatch.setattr(dispatch, "housekeeping", housekeeping)
    task = asyncio.create_task(dispatch.run(state))
    try:
        async with asyncio.timeout(5):
            await kept.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await close_state(state)


async def test_aims_ui_hands_its_keep_days_to_its_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    argv: list[str] = []

    async def spawn(*args: str, **kwargs: Any) -> None:
        argv.extend(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(), 0)
    assert "--keep-days" not in argv
    argv.clear()
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(keep_days=30), 0)
    assert argv[argv.index("--keep-days") + 1] == "30"


def test_aims_ui_takes_keep_days(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[UiOptions] = []

    async def serve(o: UiOptions) -> int:
        seen.append(o)
        return 0

    monkeypatch.setattr("aims.server.serve.serve", serve)
    base = ["ui", "--no-open", "--no-runner", "--data-dir", str(tmp_path)]
    for args, days in ((base, None), ([*base, "--keep-days", "14"], 14.0)):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        assert seen.pop().keep_days == days


# Deleting a scenario


async def test_a_scenario_is_deleted_once_no_job_uses_it_and_its_runs_stay(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    folder = cp.project.parent / "scenarios" / "control"
    sealed = await made(cp, local, runner)
    (queued,) = await start_run(cp)
    url = "/api/scenarios/project/control"
    async with cp.client(cp.viewer) as c:
        assert (await c.delete(url)).status_code == 403
    async with cp.client(cp.operator) as c:
        assert (await c.delete("/api/scenarios/builtin/word-count")).status_code == 403
        assert (await c.delete("/api/scenarios/project/missing")).status_code == 404
        r = await c.delete(url)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "in_use"
        assert queued in r.json()["detail"]
        assert folder.is_dir()
        assert (await c.post(f"/api/runs/{queued}/cancel", json={})).status_code == 202
        assert (await c.delete(url)).status_code == 204
        assert not folder.exists()
        assert (await c.get(url)).status_code == 404
        listed = [s["name"] for s in (await c.get("/api/scenarios")).json()]
        assert "control" not in listed
        for path in ("", "/events", "/steps", "/report", "/verification"):
            r = await c.get(f"/api/runs/{sealed}{path}")
            assert r.status_code == 200, (path, r.text)


async def test_deletes_and_pruning_leave_audit_rows(
    cp: CP, local: LocalEventStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await made(cp, local, runner)
    review = await made(cp, local, runner, kind="monitor", origin=run)
    stuck = await made(cp, local, runner, sealed=False)
    with monkeypatch.context() as m:
        m.setattr(event_store, "now", lambda: back(3))
        old = await made(cp, local, runner)
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{run}")).status_code == 200
        assert (await c.delete(f"/api/runs/{stuck}")).status_code == 409
        r = await c.delete(f"/api/runs/{stuck}", params={"force": "true"})
        assert r.status_code == 200
        assert (await c.delete("/api/scenarios/project/control")).status_code == 204
    cp.state.keep_days = 2
    await dispatch.housekeeping(cp.state)
    rows = [
        (r.action, r.outcome, r.status, r.target, r.detail)
        for r in reversed(await cp.state.db.audit_rows(limit=100))
        if r.action in ("run.delete", "scenario.delete", "run.prune")
    ]
    a, b = sorted([run, review])
    assert rows == [
        ("run.delete", "ok", 200, run, f"2 runs, {a} to {b}"),
        ("run.delete", "refused", 409, stuck, "offline"),
        ("run.delete", "ok", 200, stuck, f"forced, {stuck}"),
        ("scenario.delete", "ok", 204, "project/control", None),
        ("run.prune", "ok", None, None, f"ended over 2 days ago: {old}"),
    ]


# A runner never ships a deleted run back


async def test_a_deleted_run_is_refused_on_every_ingest_path(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run_id = await made(cp, local, runner)
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{run_id}")).status_code == 200
    assert (await runner.client.heads([run_id]))[run_id].deleted
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    r = await raw_events(cp, runner.name, run_id, lines)
    assert (r.status_code, r.json()["code"]) == (410, "deleted")
    shipper = await ship(local, runner.client, run_id)
    assert shipper.deleted
    assert shipper.refused is not None
    assert await cp.state.store.run(run_id) is None
    assert not (cp.state.store.root / run_id).exists()
    dropped: list[str] = []

    async def drop(run: str) -> None:
        dropped.append(run)

    link = Link(runner.client, local, DeltaBus(), on_deleted=drop)
    await link.ship_all()
    assert dropped == [run_id]
    assert run_id not in link.shippers
    link.ship(run_id)
    assert await link.drain(20)
    await until(lambda: asyncio.sleep(0, len(dropped)), 2)
    assert run_id not in link.shippers
    assert await cp.state.store.run(run_id) is None


async def deleted_seen(c: Any, mark: str, last: str, *, linger: float = 0.0) -> list[str]:
    """The deleted runs a stream resumed from `mark` tells of, up to run `last` and for
    `linger` seconds after it."""
    seen: list[str] = []
    params = {"runs": "true", "last_event_id": mark}
    async with c.sse("/api/stream", params=params) as source:
        events = aiter(source)

        async def read() -> None:
            e = await anext(events)
            if e.event == "deleted":
                seen.append(json.loads(e.data)["run_id"])

        async with asyncio.timeout(10):
            while last not in seen:
                await read()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(linger):
                while True:
                    await read()
    return seen


async def test_live_streams_tell_of_deleted_runs_a_viewer_may_see(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run = await made(cp, local, runner)
    score = await made(cp, local, runner, kind="score", origin=run)
    async with cp.client(cp.operator) as op, cp.client(cp.viewer) as viewer:
        [(_, _, mark)] = await sse(op, {"runs": "true"}, want=1)
        assert (await op.delete(f"/api/runs/{run}")).status_code == 200
        assert set(await deleted_seen(op, mark, score)) == {run, score}
        assert await deleted_seen(viewer, mark, run, linger=1) == [run]


async def test_a_runner_drops_its_copy_of_a_deleted_run_and_never_ships_it_back(
    cp: CP, tmp_path: Path
) -> None:
    data = tmp_path / "node"
    async with await LocalEventStore.open(data / "runs") as store:
        run_id = await store.create_run(
            RunStarted(scenario="control", oversight="observe", agents=["a"])
        )
        await store.append(run_id, [Draft(RunFinished(status="completed"))])

    async def shipped() -> bool:
        head = await cp.state.store.head(run_id)
        return head is not None and head.finished

    async with node(cp, data):
        await until(shipped, True, 20)
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{run_id}")).status_code == 200
    async with node(cp, data):
        await until(lambda: asyncio.sleep(0, (data / "runs" / run_id).exists()), False, 20)
    assert await cp.state.store.run(run_id) is None
    assert not (cp.state.store.root / run_id).exists()


async def test_a_runner_back_from_offline_drops_a_run_deleted_with_force(
    cp: CP, tmp_path: Path
) -> None:
    data = tmp_path / "node"
    async with await LocalEventStore.open(data / "runs") as store:
        run_id = await store.create_run(
            RunStarted(scenario="control", oversight="observe", agents=["a"])
        )
        await store.append(run_id, [Draft(ToolCall(call_id="c", name="x", arguments="{}"), "a")])
    gone_runner = FakeRunner(cp, "n1")
    await gone_runner.client.hello(gone_runner.hello_body())
    lines = split_lines((data / "runs" / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, "n1", run_id, lines)).status_code == 200
    await gone_runner.client.aclose()
    async with cp.client(cp.operator) as c:
        r = await c.delete(f"/api/runs/{run_id}")
        assert (r.status_code, r.json()["code"]) == (409, "offline")
        r = await c.delete(f"/api/runs/{run_id}", params={"force": "true"})
        assert r.status_code == 200, r.text
    async with node(cp, data):
        await until(lambda: asyncio.sleep(0, (data / "runs" / run_id).exists()), False, 20)
    assert await cp.state.store.run(run_id) is None
    assert not (cp.state.store.root / run_id).exists()


async def test_a_runner_cancels_a_deleted_run_it_still_runs_then_drops_it(
    tmp_path: Path,
) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(
            RunStarted(scenario="control", oversight="observe", agents=["a"])
        )
        stop = asyncio.Event()

        class Running:
            finished = False

            def cancel(self) -> None:
                stop.set()

        async def runs() -> None:
            await stop.wait()
            await store.append(run_id, [Draft(RunFinished(status="cancelled"))])

        made_node = Node(NodeOptions(url="http://cp", token="t", name="n", data=tmp_path))
        link = SimpleNamespace(runners={run_id: Running()})
        made_node._open = cast(Any, SimpleNamespace(store=store, link=link))
        made_node._jobs[run_id] = asyncio.create_task(runs())
        await made_node._drop(run_id)
        assert stop.is_set()
        assert await store.run(run_id) is None
        assert not (store.root / run_id).exists()


@pytest.mark.parametrize("kind", ["monitor", "score", "models.check"])
async def test_a_runner_cancels_a_job_with_no_runner_whose_run_was_deleted_then_drops_it(
    tmp_path: Path, kind: str
) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(
            RunStarted(scenario="control", oversight="observe", agents=["a"])
        )
        made_node = Node(NodeOptions(url="http://cp", token="t", name="n", data=tmp_path))
        claimed: Any
        if kind == "monitor":
            claimed = MonitorJob.model_construct(run_id=run_id)
        elif kind == "score":
            claimed = ScoreJob.model_construct(run_id=run_id)
        else:
            claimed = RunJob(kind="models.check", run_id=run_id)
        cut = asyncio.Event()

        async def works() -> None:
            # stands for a model call, which only a cancel ends
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                cut.set()
                await store.append(run_id, [Draft(RunFinished(status="cancelled"))])
                raise

        made_node._open = cast(Any, SimpleNamespace(store=store, link=SimpleNamespace(runners={})))
        made_node.claimed[run_id] = claimed
        made_node._jobs[run_id] = asyncio.create_task(works())
        await asyncio.sleep(0)
        async with asyncio.timeout(10):
            await made_node._drop(run_id)
        assert cut.is_set()
        assert await store.run(run_id) is None
        assert not (store.root / run_id).exists()


async def test_a_monitor_job_whose_run_a_forced_delete_took_is_cancelled_and_dropped(
    cp: CP, local: LocalEventStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = await pushed(cp, local, runner, [call(1)], monitor=None)
    feeding, poke, cut = asyncio.Event(), asyncio.Event(), asyncio.Event()
    data = local.root.parent / "node"

    async def feed(monitor: Any, *args: Any) -> None:
        # stands for the monitor's model calls, which only a cancel ends; one more line first
        # makes the shipper call the control plane, where it learns of the delete
        feeding.set()
        try:
            await poke.wait()
            await n.ctx.store.append(monitor.run_id, [call(2)])
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            cut.set()
            raise

    monkeypatch.setattr(offline, "_feed", feed)
    async with node(cp, data) as n:
        run_id = await queue_monitor(cp, origin)
        async with asyncio.timeout(20):
            await feeding.wait()
        await until(lambda: cp.state.store.run(run_id), lambda info: info is not None)
        # what a forced delete does once the runner looks offline
        async with cp.state.delete_lock:
            found = await retention.family(cp.state, origin)
            assert found is not None
            assert set(found.runs) == {origin, run_id}
            await retention.delete(cp.state, found.runs)
        poke.set()
        async with asyncio.timeout(20):
            await cut.wait()
        await until(lambda: asyncio.sleep(0, (data / "runs" / run_id).exists()), False, 20)
        assert await n.ctx.store.run(run_id) is None
        await until(lambda: asyncio.sleep(0, bool(n.claimed)), False, 20)
    assert await cp.state.store.run(run_id) is None
    assert not (cp.state.store.root / run_id).exists()


async def test_import_takes_back_a_deleted_run(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run_id = await made(cp, local, runner)
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        t.add(local.root / run_id / "events.jsonl", arcname=f"{run_id}/events.jsonl")
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{run_id}")).status_code == 200
        r = await c.post(
            "/api/runs/import",
            content=tar.getvalue(),
            headers={"Content-Type": "application/x-tar"},
        )
        assert r.status_code == 201, r.text
    assert await cp.state.store.run(run_id) is not None
    assert await cp.state.db.deleted([run_id]) == set()


async def test_a_refused_import_keeps_a_deleted_run_deleted(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run_id = await made(cp, local, runner)
    async with cp.client(cp.operator) as c:
        assert (await c.delete(f"/api/runs/{run_id}")).status_code == 200
    r = await post_tar(cp, run_tar(local.root / run_id, lines=log_of(local, run_id)[1:]))
    assert (r.status_code, r.json()["code"]) == (409, "ingest")
    assert await cp.state.db.deleted([run_id]) == {run_id}
    assert await cp.state.db.run_meta(run_id) is None
    assert not (cp.state.store.root / run_id).exists()


# The CLI


async def cli(cp: CP, *args: str, token: str | None = None, input: str | None = None) -> Any:
    return await asyncio.to_thread(
        CliRunner().invoke,
        app,
        [*args, "--server", cp.url],
        env={"AIMS_TOKEN": token or cp.operator},
        input=input,
    )


async def test_aims_delete_shows_what_goes_and_asks_first(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run_id = await made(cp, local, runner)
    review = await made(cp, local, runner, kind="monitor", origin=run_id)
    score = await made(cp, local, runner, kind="score", origin=run_id)
    result = await cli(cp, "delete", run_id, input="n\n")
    assert result.exit_code == 1, result.output
    assert "1 monitor run and 1 score run" in result.output
    assert await cp.state.store.run(run_id) is not None
    result = await cli(cp, "delete", run_id, token=cp.viewer, input="y\n")
    assert result.exit_code == 1
    assert "403" in result.output
    result = await cli(cp, "delete", run_id, input="y\n")
    assert result.exit_code == 0, result.output
    for deleted in (run_id, review, score):
        assert f"deleted {deleted}" in result.output
        assert await gone(cp, deleted)
    result = await cli(cp, "delete", run_id, "--yes")
    assert result.exit_code == 1
    assert "404" in result.output


async def test_aims_delete_says_why_a_run_stays_and_force_takes_an_abandoned_one(
    cp: CP, local: LocalEventStore, runner: FakeRunner
) -> None:
    run_id = await made(cp, local, runner, sealed=False)
    result = await cli(cp, "delete", run_id, "--yes")
    assert result.exit_code == 1
    assert "409" in result.output
    assert "its runner r1 is offline" in result.output
    assert "--force" in result.output
    assert await cp.state.store.run(run_id) is not None
    result = await cli(cp, "delete", run_id, "--force", input="y\n")
    assert result.exit_code == 0, result.output
    assert "looks abandoned" in result.output
    assert f"deleted {run_id}" in result.output
    assert await gone(cp, run_id)


async def test_aims_scenario_delete(cp: CP) -> None:
    folder = cp.project.parent / "scenarios" / "control"
    result = await cli(cp, "scenario", "delete", "project", "control", input="n\n")
    assert result.exit_code == 1, result.output
    assert folder.is_dir()
    result = await cli(cp, "scenario", "delete", "builtin", "word-count", "--yes")
    assert result.exit_code == 1
    assert "403" in result.output
    result = await cli(cp, "scenario", "delete", "project", "control", "--yes")
    assert result.exit_code == 0, result.output
    assert "deleted scenario control" in result.output
    assert not folder.exists()
