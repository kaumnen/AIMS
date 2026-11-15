"""The classifier of a score's awareness and answer checks: the first profile of `[monitor]
extract`, then the sweep role, whose weights no agent of the run runs. When none fits, the
score runs without those checks and its estimate says why."""

import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from cp import CP, control_plane
from scripted import profile
from test_score_api import planted
from test_scoring import oracle, words
from test_server import FakeRunner
from typer.testing import CliRunner

from aims import config
from aims.cli import app
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted
from aims.models import ProfileError
from aims.models import profiles as profiles_module
from aims.monitor.setup import resolve_monitor
from aims.prompts import Prompts
from aims.run import scoring, truth
from aims.run.runner import resolve_texts
from aims.scenario import load
from aims.wire import ScoreJob

pytestmark = pytest.mark.anyio


def table(name: str, weights: str, *extra: str) -> str:
    lines = [
        f"[profiles.{name}]",
        'backend = "chat"',
        f'base_url = "http://{name}.invalid/v1"',
        f'model = "{name}"',
        f'weights = "{weights}"',
        'reasoning = "full"',
        *extra,
    ]
    return "\n".join(lines) + "\n\n"


PROFILES = (
    table("agent", "w-agent")
    + table("cheap", "w-cheap")
    + table("other", "w-other")
    + table("watch", "w-watch")
    + table("plain", "w-plain", "structured_output = false")
)

SCENARIO = """\
version: 1
name: s
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
  verifier:
    command: echo 1 > "$AIMS_LOGS_DIR/reward.txt"
agents:
  - id: a
"""


@pytest.fixture(autouse=True)
def no_builtin_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = profiles_module.builtin_tables()
    del tables["roles"]
    monkeypatch.setattr(profiles_module, "builtin_tables", lambda: copy.deepcopy(tables))


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as s:
        yield s


def settings(tmp_path: Path, text: str) -> config.Settings:
    path = tmp_path / "aims.toml"
    path.write_text(PROFILES + text)
    return config.load(path)


async def run_on(store: LocalEventStore, weights: str) -> str:
    """A sealed run whose agent runs on `weights`, with words for the awareness check."""
    path = store.root.parent / "scenario" / "scenario.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(SCENARIO)
    bundle = load(path)
    agent = profile(name="agent", weights=weights).snapshot()
    texts = resolve_texts(Prompts(), bundle.scenario.agents[0]).snapshot()
    entry = {"profile": agent, "schedule": [[None, agent]], "texts": texts}
    record = {"roles": {"a": "subject"}, "plants": {"p1": {}}, "variant": None}
    started = RunStarted(
        scenario="s",
        oversight="observe",
        agents=["a"],
        truth=truth.digest(record),
        config={"scenario": bundle.snapshot(), "agents": {"a": entry}},
    )
    run_id = await store.create_run(started, blobs=bundle.blobs())
    truth.write(store.root / run_id, record)
    drafts = [*await words(store, 2), Draft(RunFinished(status="completed"))]
    await store.append(run_id, drafts)
    return run_id


def flat(text: str) -> str:
    return " ".join(text.split())


async def test_the_first_extract_profile_on_other_weights_classifies(
    tmp_path: Path, store: LocalEventStore
) -> None:
    s = settings(tmp_path, '[roles]\nsweep = "watch"\n\n[monitor]\nextract = ["cheap", "other"]\n')
    assert scoring.classifier_candidates(s) == ["cheap", "other", "watch"]
    for weights, picked in (("w-agent", "cheap"), ("w-cheap", "other"), ("w-watch", "cheap")):
        e = await scoring.estimate_sealed(store, s, await run_on(store, weights))
        assert (e.classifier, e.checks_off) == (picked, None)
        assert e.lines[0].startswith(f"Score: checks on {picked}")


async def test_with_no_extract_the_sweep_role_classifies(
    tmp_path: Path, store: LocalEventStore
) -> None:
    s = settings(tmp_path, '[roles]\nsweep = "watch"\n')
    assert scoring.classifier_candidates(s) == ["watch"]
    e = await scoring.estimate_sealed(store, s, await run_on(store, "w-agent"))
    assert e.classifier == "watch"
    # a sweep role listed in extract too is a candidate once
    s = settings(tmp_path, '[roles]\nsweep = "watch"\n\n[monitor]\nextract = ["watch", "cheap"]\n')
    assert scoring.classifier_candidates(s) == ["watch", "cheap"]


async def test_with_no_profile_named_the_checks_are_off_and_the_estimate_says_why(
    tmp_path: Path, store: LocalEventStore
) -> None:
    s = settings(tmp_path, "")
    run = await run_on(store, "w-agent")
    e = await scoring.estimate_sealed(store, s, run)
    assert (e.classifier, e.calls) == (None, 0)
    assert e.checks_off is not None
    assert e.checks_off.startswith("No model fits the awareness and answer checks")
    assert "set [monitor] extract or the sweep role in aims.toml" in e.checks_off
    assert e.lines == [f"Score: {e.checks_off} It calls no model, so it costs nothing."]
    one = await scoring.estimate_sealed(store, s, run, answer=False)
    assert one.checks_off is not None
    assert one.checks_off.startswith("No model fits the awareness check, so it is off")
    # no check asked for is no check off
    none = await scoring.estimate_sealed(store, s, run, awareness=False, answer=False)
    assert none.checks_off is None
    assert none.lines == ["Score: no check calls a model, so it costs nothing."]


async def test_profiles_on_an_agent_s_weights_or_unknown_are_named(
    tmp_path: Path, store: LocalEventStore
) -> None:
    s = settings(tmp_path, '[roles]\nsweep = "watch"\n\n[monitor]\nextract = ["cheap", "gone"]\n')
    e = await scoring.estimate_sealed(store, s, await run_on(store, "w-cheap"))
    assert e.classifier == "watch"
    s = settings(tmp_path, '[monitor]\nextract = ["cheap", "gone"]\n')
    e = await scoring.estimate_sealed(store, s, await run_on(store, "w-cheap"))
    assert e.classifier is None
    assert e.checks_off is not None
    assert "cheap runs on an agent's weights; gone is unknown" in e.checks_off
    assert "add one on other weights to [monitor] extract in aims.toml" in e.checks_off


async def test_a_score_with_no_classifier_runs_without_its_checks(
    tmp_path: Path, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle(monkeypatch, [])
    s = settings(tmp_path, "")
    result = await scoring.score_sealed(store, s, await run_on(store, "w-agent"))
    (began,) = await store.events(result.run_id, types=["run.started"], limit=1)
    assert cast(dict[str, Any], began.data["config"])["classifier"] is None
    assert await store.events(result.run_id, types=["model.request"], limit=1) == []


def test_aims_score_prints_why_its_checks_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def make() -> str:
        async with await LocalEventStore.open(tmp_path / "runs") as s:
            return await run_on(s, "w-agent")

    run = asyncio.run(make())
    oracle(monkeypatch, [])
    toml = tmp_path / "aims.toml"
    toml.write_text(PROFILES)
    out = CliRunner().invoke(
        app, ["score", run, "--config", str(toml), "--runs-dir", str(tmp_path / "runs")]
    )
    assert out.exit_code == 0, out.output
    assert "No model fits the awareness and answer checks, so they are off" in flat(out.stderr)


def test_an_extract_profile_without_structured_output_is_refused_at_load(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProfileError, match="extract needs structured output, which plain lacks"):
        settings(tmp_path, '[monitor]\nextract = ["cheap", "plain"]\n')
    # an unknown name loads, as the monitor passes over it
    assert settings(tmp_path, '[monitor]\nextract = ["gone"]\n').models.roles.sweep is None


def test_a_scenario_s_extract_profile_without_structured_output_is_refused(
    tmp_path: Path,
) -> None:
    s = settings(tmp_path, '[roles]\nsweep = "watch"\njudge = "watch"\n')
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO + "monitor:\n  extract: [plain]\n")
    scenario = load(path).scenario
    agent = s.models.get("agent")
    with pytest.raises(ProfileError, match="extract needs structured output, which plain lacks"):
        resolve_monitor(s.models, Prompts(), scenario, [agent], layers=s.monitor)


async def test_the_api_says_why_a_score_s_checks_are_off(tmp_path: Path) -> None:
    from test_score_api import ScoreRunner

    async with (
        control_plane(tmp_path) as cp,
        await LocalEventStore.open(tmp_path / "local") as local,
    ):
        fake = FakeRunner(cp)
        await fake.connect()
        scorer = ScoreRunner(cp, "scorer")
        await scorer.connect()
        try:
            origin = await planted(cp, local, fake)
            await _api(cp, origin)
        finally:
            await scorer.close()
            await fake.close()


async def _api(cp: CP, origin: str) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{origin}/score/estimate")
        assert r.status_code == 200, r.text
        e = r.json()
        assert e["classifier"] is None
        assert e["checks_off"].startswith("No model fits the awareness and answer checks")
        assert e["lines"][0].startswith("Score: No model fits")
        # the score still starts, without the checks
        r = await c.post(f"/api/runs/{origin}/score", json={})
        assert r.status_code == 202, r.text
        (score_id,) = r.json()["run_ids"]
    job = await cp.state.db.job(score_id)
    assert job is not None
    assert ScoreJob.model_validate(job.payload).classifier is None
    # an extract profile on other weights than the agent's is the classifier
    cp.project.write_text(
        cp.project.read_text()
        + '\n[profiles.classify]\nextends = "scripted"\nweights = "w-classify"\n'
        + '\n[monitor]\nextract = ["classify"]\n'
    )
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{origin}/score/estimate")
        assert (r.json()["classifier"], r.json()["checks_off"]) == ("classify", None)
        r = await c.post(f"/api/runs/{origin}/score", json={})
        assert r.status_code == 202, r.text
        (score_id,) = r.json()["run_ids"]
    job = await cp.state.db.job(score_id)
    assert job is not None
    profile = ScoreJob.model_validate(job.payload).profile()
    assert profile is not None
    assert profile.name == "classify"
