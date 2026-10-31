"""What the UI's new run form needs from the control plane: a variant, the cost guard, a score of
each run once its truth file arrives, what the scenario list says about each scenario, and the
newest model check of each profile."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_monitor_api import call, started
from test_score_api import RECORD, ScoreRunner
from test_server import FakeRunner, ship

from aims.events import Draft, LocalEventStore, ModelChecked, RunFinished, RunStarted
from aims.models import Profile, check
from aims.models.check import Check, Verdict
from aims.run import truth
from aims.server import score_api
from aims.wire import OfferMessage

pytestmark = pytest.mark.anyio

VARIANTS = """\
version: 1
name: loud
oversight: human
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
  verifier:
    command: echo 1 > "$AIMS_LOGS_DIR/reward.txt"
agents:
  - id: a
  - id: b
variants:
  shout:
    task:
      instruction: DO WHAT YOU ARE TOLD.
"""


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        folder = cp.project.parent / "scenarios" / "loud"
        folder.mkdir()
        (folder / "scenario.yaml").write_text(VARIANTS)
        (cp.project.parent / "scenarios" / "broken").mkdir()
        (cp.project.parent / "scenarios" / "broken" / "scenario.yaml").write_text("version: 1\n")
        idle = FakeRunner(cp, "idle")
        await idle.client.hello(idle.hello_body())
        await idle.client.aclose()
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


def body(name: str = "control", **fields: Any) -> dict[str, Any]:
    return {"scenario": {"source": "project", "name": name}, "monitor": "signals", **fields}


async def test_the_scenario_list_says_oversight_agents_and_variants(cp: CP) -> None:
    async with cp.client(cp.viewer) as c:
        listed = {s["name"]: s for s in (await c.get("/api/scenarios")).json()}
    loud, control, broken = listed["loud"], listed["control"], listed["broken"]
    assert (loud["oversight"], loud["agents"], loud["variants"]) == ("human", 2, ["shout"])
    assert (control["oversight"], control["agents"], control["variants"]) == ("observe", 1, [])
    assert (loud["profiles"], control["profiles"], broken["profiles"]) == (
        ["scripted"],
        ["scripted"],
        [],
    )
    assert broken["error"]
    assert (broken["oversight"], broken["agents"], broken["variants"]) == (None, None, [])


async def test_a_run_takes_a_variant_and_refuses_an_unknown_one(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/runs", json=body("loud", variant="shout"))
        assert r.status_code == 202, r.text
        (run_id,) = r.json()["run_ids"]
        r = await c.post("/api/runs", json=body("loud", variant="whisper"))
        assert (r.status_code, r.json()["code"]) == (422, "scenario")
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert job.payload["scenario"]["resolved"]["task"]["instruction"] == "DO WHAT YOU ARE TOLD."


async def test_runs_above_the_confirm_limit_need_confirm(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        # five runs whose agent may spend $5 each: the caps allow $25, above the $20 default
        r = await c.post("/api/runs", json=body(samples=5))
        assert (r.status_code, r.json()["code"]) == (409, "confirm")
        assert "set confirm to start anyway" in r.json()["detail"]
        assert await cp.state.db.jobs() == []
        r = await c.post("/api/runs", json=body(samples=5, confirm=True))
        assert r.status_code == 202, r.text
        assert len(r.json()["run_ids"]) == 5
        r = await c.post("/api/runs", json=body(samples=1))
        assert r.status_code == 202, r.text


async def test_a_run_asked_to_score_is_scored_once_its_truth_arrives(
    cp: CP, local: LocalEventStore
) -> None:
    fake = ScoreRunner(cp, "scorer", envs=["MODEL_API_KEY"])
    await fake.connect()
    try:
        async with cp.client(cp.operator) as c:
            r = await c.post("/api/runs", json=body(score=True))
            assert r.status_code == 202, r.text
            (run_id,) = r.json()["run_ids"]
            plain = (await c.post("/api/runs", json=body())).json()["run_ids"][0]
        for _ in range(2):
            offer = await fake.next(OfferMessage)
            assert await fake.client.claim(offer.run_id) is not None
            first, blobs = started(cp)
            first = first.model_copy(update={"truth": truth.digest(RECORD)})
            await local.create_run(first, blobs=blobs, run_id=offer.run_id)
            truth.write(local.root / offer.run_id, RECORD)
            done = Draft(RunFinished(status="completed"))
            await local.append(offer.run_id, [call(1), done])
            await ship(local, fake.client, offer.run_id)
        scores = [j for j in await cp.state.db.jobs() if j.payload.get("kind") == "score"]
        assert [j.payload["origin"] for j in scores] == [run_id]
        start = await cp.state.db.job(run_id)
        assert start is not None
        assert scores[0].token == start.token
        assert plain != run_id
        # the same truth file sent again queues nothing more
        await ship(local, fake.client, run_id)
        scores = [j for j in await cp.state.db.jobs() if j.payload.get("kind") == "score"]
        assert len(scores) == 1
    finally:
        await fake.close()


async def test_each_profile_shows_its_newest_model_check(cp: CP, local: LocalEventStore) -> None:
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    models = cp.state.config.settings().models

    async def checked(names: list[str], verdicts: dict[str, Verdict]) -> str:
        first = RunStarted(
            scenario="models.check",
            oversight="observe",
            kind="models.check",
            config={"models": models.snapshot(names)},
        )
        run_id = await local.create_run(first)
        await local.append(
            run_id,
            [
                Draft(ModelChecked(profile=n, name="plain", verdict=v, detail=f"{n} {v}"))
                for n, v in verdicts.items()
            ],
        )
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        await ship(local, pusher.client, run_id)
        return run_id

    older = await checked(["scripted", "keyed"], {"scripted": "fail", "keyed": "ok"})
    newer = await checked(["scripted"], {"scripted": "warn"})
    await pusher.client.aclose()
    async with cp.client(cp.viewer) as c:
        profiles = {p["name"]: p for p in (await c.get("/api/models")).json()["profiles"]}
    scripted, keyed = profiles["scripted"]["check"], profiles["keyed"]["check"]
    assert scripted["run_id"] == newer
    assert scripted["checks"] == [{"name": "plain", "verdict": "warn", "detail": "scripted warn"}]
    assert keyed["run_id"] == older
    assert [c["verdict"] for c in keyed["checks"]] == ["ok"]
    assert all(p["check"] is None for n, p in profiles.items() if n not in ("scripted", "keyed"))


async def test_a_model_check_records_each_smoke_test(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = cp.state.config.settings().models

    async def fake(client: object, profile: Profile) -> list[Check]:
        return [Check(profile.name, "plain", "ok", "42"), Check(profile.name, "tools", "fail", "x")]

    monkeypatch.setattr(check, "check_profile", fake)
    run_id, checks = await check.record_check(local, models, [models.get("scripted")])
    assert [c.verdict for c in checks] == ["ok", "fail"]
    events = await local.events(run_id, types=["model.checked"])
    assert [(e.data["name"], e.data["verdict"]) for e in events] == [
        ("plain", "ok"),
        ("tools", "fail"),
    ]
    info = await local.run(run_id)
    assert info is not None
    assert info.status == "failed"


async def test_a_score_that_fails_to_queue_keeps_the_truth_file(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise KeyError("options")

    monkeypatch.setattr(score_api, "queue_score", broken)
    fake = ScoreRunner(cp, "scorer", envs=["MODEL_API_KEY"])
    await fake.connect()
    try:
        async with cp.client(cp.operator) as c:
            (run_id,) = (await c.post("/api/runs", json=body(score=True))).json()["run_ids"]
        offer = await fake.next(OfferMessage)
        assert await fake.client.claim(offer.run_id) is not None
        first, blobs = started(cp)
        first = first.model_copy(update={"truth": truth.digest(RECORD)})
        await local.create_run(first, blobs=blobs, run_id=run_id)
        await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
        await ship(local, fake.client, run_id)
        assert not cp.state.store.truth_path(run_id).is_file()
        # the upload answers well though scoring it broke
        await fake.client.put_truth(run_id, truth.encode(RECORD))
        assert cp.state.store.truth_path(run_id).is_file()
        assert [j for j in await cp.state.db.jobs() if j.payload.get("kind") == "score"] == []
    finally:
        await fake.close()
