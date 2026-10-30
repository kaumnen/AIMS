"""Scoring through the control plane: queuing a score job, the runner reading the truth file it
needs, scoring a run held by another store, labels from the API and from runners, and the
report endpoint."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_monitor_api import call, claim, flag, started
from test_server import FakeRunner, ship

from aims.events import Draft, LocalEventStore, RunFinished
from aims.events.chain import Head as LogHead
from aims.models import DeltaBus
from aims.monitor.texts import resolve
from aims.node.link import Link
from aims.prompts import Prompts
from aims.run import scoring, truth
from aims.run.score import Taken
from aims.run.scoring import score_with
from aims.wire import OfferMessage, RunnerHello, ScoreJob

pytestmark = pytest.mark.anyio

RECORD: dict[str, Any] = {"roles": {"a": "subject"}, "plants": {"p1": {}}, "variant": "v1"}


class ScoreRunner(FakeRunner):
    """A fake runner that says it runs score jobs."""

    def hello_body(self, jobs: list[str] | None = None) -> RunnerHello:
        body = super().hello_body(jobs)
        return body.model_copy(update={"kinds": ["scenario", "models.check", "score"]})


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


async def planted(
    cp: CP, local: LocalEventStore, runner: FakeRunner, *, record: dict[str, Any] | None = RECORD
) -> str:
    """A sealed run as `aims run` writes it, committed to `record` as its truth, shipped."""
    first, blobs = started(cp)
    if record is not None:
        first = first.model_copy(update={"truth": truth.digest(record)})
    run_id = await local.create_run(first, blobs=blobs)
    if record is not None:
        truth.write(local.root / run_id, record)
    drafts = [call(1), flag(evidence=[1]), Draft(RunFinished(status="completed"))]
    await local.append(run_id, drafts)
    await ship(local, runner.client, run_id)
    return run_id


async def test_a_score_job_queues_for_runners_that_take_it(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    origin = await planted(cp, local, fake)
    blank = await planted(cp, local, fake, record=None)
    url = f"/api/runs/{origin}/score"
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        assert (await viewer.post(url, json={})).status_code == 403
        r = await c.post(f"/api/runs/{blank}/score", json={})
        assert (r.status_code, r.json()["code"]) == (409, "truth")
        r = await c.post(url, json={"monitor": blank})
        assert r.status_code == 422
        r = await c.post(url, json={"awareness": False})
        assert (r.status_code, r.json()["code"]) == (422, "kind")

        scorer = ScoreRunner(cp, "scorer", envs=["MODEL_API_KEY"])
        await scorer.connect()
        try:
            r = await c.post(url, json={"awareness": False})
            assert r.status_code == 202, r.text
            (score_id,) = r.json()["run_ids"]
            offer = await scorer.next(OfferMessage)
            assert offer.run_id == score_id
            assert offer.needs == {"envs": [], "kernel": False, "kind": "score"}
            job = await cp.state.db.job(score_id)
            assert job is not None
            spec = ScoreJob.model_validate(job.payload)
            assert (spec.origin, spec.monitor, spec.classifier) == (origin, None, None)
            texts: Any = spec.texts["texts"]
            assert "awareness" in texts

            path = f"/runner/v1/runs/{origin}/truth"
            async with cp.client(cp.runner) as rc:
                r = await rc.get(path, headers={"X-Aims-Runner": "scorer"})
                assert r.status_code == 403
                r = await claim(cp, "scorer", score_id)
                assert r.status_code == 200, r.text
                assert r.json()["job"]["kind"] == "score"
                r = await rc.get(path, headers={"X-Aims-Runner": "scorer"})
                assert r.status_code == 200
                assert r.content == truth.path(local.root / origin).read_bytes()

            r = await c.post(url, json={})
            assert r.status_code == 202, r.text
            job = await cp.state.db.job(r.json()["run_ids"][0])
            assert job is not None
            aware = ScoreJob.model_validate(job.payload)
            profile = aware.profile()
            assert profile is not None
            assert profile.name == "meta-spark-1.2-contributor"
            assert aware.envs() == ["MODEL_API_KEY"]
        finally:
            await scorer.close()


async def test_a_runner_scores_a_run_another_store_holds(
    cp: CP,
    local: LocalEventStore,
    fake: FakeRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = await planted(cp, local, fake)
    rows = [Taken("a", "p1", "file", "scorer_tampering", "used", True, 1, (1,))]

    async def check(store: Any, run_id: str, added: Any = None) -> list[Taken]:
        assert store.root == tmp_path / "origins"
        return rows

    monkeypatch.setattr(scoring, "check_run", check)
    async with (
        await LocalEventStore.open(tmp_path / "origins", ingest_only=True) as origins,
        await LocalEventStore.open(tmp_path / "mine") as mine,
    ):
        origins.prepare_run(origin)
        for path in (local.root / origin / "blobs").glob("*"):

            async def chunks(data: bytes = path.read_bytes()) -> AsyncIterator[bytes]:
                yield data

            await origins.put_blob_stream(origin, path.name, chunks(), limit=1 << 30)
        head = await local.head(origin)
        assert head is not None
        lines, _ = await local.lines(origin, LogHead(), upto=head.seq, max_events=1000)
        await origins.ingest(origin, lines)
        await origins.put_truth(origin, truth.path(local.root / origin).read_bytes())
        result = await score_with(
            mine,
            origin,
            models=cp.state.config.settings().models,
            texts=resolve(Prompts()),
            candidates=[],
            source=origins,
        )
        assert result.units["a"].positive is True
        info = await mine.run(result.run_id)
        assert info is not None
        assert (info.kind, info.origin) == ("score", origin)
        assert await mine.run(origin) is None


async def test_labels_from_the_api_and_from_runners_and_the_report(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = await planted(cp, local, fake)
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        url = f"/api/runs/{origin}/labels"
        assert (await viewer.post(url, json={"flag": 2, "label": "real"})).status_code == 403
        r = await c.post(url, json={"flag": 1, "label": "real"})
        assert r.status_code == 404
        r = await c.post(url, json={"flag": 2, "label": "real", "note": "seen it"})
        assert r.status_code == 201, r.text
        assert r.json()["by"] == "op"
        got = (await viewer.get(url)).json()
        assert [(x["flag"], x["label"]) for x in got] == [(2, "real")]

    await local.add_label(origin, 2, "false_alarm", by="me")
    link = Link(fake.client, local, DeltaBus())
    await link.ship_all()
    held = await cp.state.store.labels(origin)
    assert held[2].label == "false_alarm"
    assert len(await cp.state.store.label_history(origin)) == 2
    await link.ship_all()
    assert len(await cp.state.store.label_history(origin)) == 2

    monkeypatch.setattr(scoring, "check_run", lambda store, run_id, added=None: _rows())
    await scoring.score_run(local, origin)
    score = next(i for i in await local.runs(kind="score"))
    await ship(local, fake.client, score.run_id)
    async with cp.client(cp.viewer) as viewer:
        assert (await viewer.get("/api/report")).status_code == 403
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/report", params={"runs": [origin]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["runs"] == [origin]
        group = data["groups"][0]
        assert (group["variant"], group["positives"]) == ("v1", 1)
        r = await c.get("/api/report", params={"runs": [score.run_id]})
        assert r.json()["runs"] == [origin]


async def _rows() -> list[Taken]:
    return [Taken("a", "p1", "file", "scorer_tampering", "used", True, 1, (1,), "canary-zq9")]


async def scored(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """A shipped run and a shipped score of it."""
    origin = await planted(cp, local, fake)
    monkeypatch.setattr(scoring, "check_run", lambda store, run_id, added=None: _rows())
    result = await scoring.score_run(local, origin)
    await ship(local, fake.client, result.run_id)
    return origin, result.run_id


async def test_viewers_see_score_runs_listed_but_never_opened(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, score = await scored(cp, local, fake, monkeypatch)
    base = f"/api/runs/{score}"
    sha = "0" * 64
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        listed = (await viewer.get("/api/runs", params={"kind": "score"})).json()["items"]
        assert [i["run_id"] for i in listed] == [score]
        for path in (base, f"{base}/events", f"{base}/events/0", f"{base}/blobs/{sha}"):
            r = await viewer.get(path)
            assert (r.status_code, r.json()["code"]) == (403, "truth"), path
        r = await viewer.get("/api/stream", params={"run": [score]})
        assert r.status_code == 403
        r = await viewer.get("/api/stream", params={"run": [origin], "after": [1, 2]})
        assert r.status_code == 422
        assert (await viewer.get(f"/api/runs/{origin}/events")).status_code == 200
        assert (await viewer.get("/api/events", params={"q": "canary-zq9"})).json()["items"] == []
        found = (await c.get("/api/events", params={"q": "canary-zq9"})).json()["items"]
        assert [(e["run_id"], e["type"]) for e in found] == [(score, "plant.triggered")]
        assert (await c.get(base)).status_code == 200
        assert (await c.get(f"{base}/events")).status_code == 200
        assert (await c.get(f"{base}/blobs/{sha}")).status_code == 404
