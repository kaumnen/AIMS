"""The classifier of a score's awareness and answer checks: the first profile of `[monitor]
extract`, then the sweep role, whose weights no agent of the run runs. When none fits, the
score runs without those checks and its estimate says why."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from cp import CP, control_plane
from scripted import Script, profile, say
from test_score_api import planted
from test_scoring import oracle, words
from test_server import FakeRunner
from typer.testing import CliRunner

from aims import config
from aims.cli import app
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted
from aims.models import ProfileError
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
    run = await run_on(store, "w-agent")
    result = await scoring.score_sealed(store, s, run)
    (began,) = await store.events(result.run_id, types=["run.started"], limit=1)
    config = cast(dict[str, Any], began.data["config"])
    assert config["classifier"] is None
    assert await store.events(result.run_id, types=["model.request"], limit=1) == []
    # the score records the checks asked for and why they did not run
    off = "No model fits the awareness and answer checks, so they are off"
    assert result.checks_off is not None
    assert result.checks_off.startswith(off)
    assert config["checks"] == {"awareness": True, "answer": True, "off": result.checks_off}
    # checks turned off are not a reason to give
    quiet = await scoring.score_sealed(store, s, run, awareness=False, answer=False)
    (began,) = await store.events(quiet.run_id, types=["run.started"], limit=1)
    checks = cast(dict[str, Any], began.data["config"])["checks"]
    assert (quiet.checks_off, checks) == (None, {"awareness": False, "answer": False, "off": None})


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
    shown = CliRunner().invoke(
        app, ["score", run, "--config", str(toml), "--runs-dir", str(tmp_path / "runs"), "--json"]
    )
    assert shown.exit_code == 0, shown.output
    off = json.loads(shown.stdout)["checks_off"]
    assert off.startswith("No model fits the awareness and answer checks, so they are off")


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
    queued = ScoreJob.model_validate(job.payload)
    assert queued.classifier is None
    assert queued.checks_off == e["checks_off"]
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
    queued = ScoreJob.model_validate(job.payload)
    profile = queued.profile()
    assert profile is not None
    assert (profile.name, queued.checks_off) == ("classify", None)


# What a score costs on the command line. The classifier answers from a script. Nothing opens
# a Docker backend.

# A classifier whose output cap alone could cost $10 a call, a priced agent and a threshold of
# $1: the score's spend cap bounds it.
COSTLY = (
    PROFILES.replace(
        "[profiles.agent]\n",
        "[profiles.agent]\nprice = { input = 1.0, cached = 0.1, output = 2.0 }\n",
    )
    + table(
        "classify",
        "w-classify",
        "max_output_tokens = 1000000",
        "price = { input = 10.0, cached = 1.0, output = 10.0 }",
        "stream = false",
        "retries = { attempts = 1, backoff = 0, max = 0 }",
    )
    + '[roles]\nagent = "agent"\n\n[monitor]\nextract = ["classify"]\n\n'
    + "[estimate]\nconfirm_above_usd = 1\n"
)


@pytest.fixture
def costly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, Script]:
    import asyncio

    from aims.sandbox import DockerBackend

    async def make() -> str:
        async with await LocalEventStore.open(tmp_path / "runs") as s:
            return await run_on(s, "w-agent")

    run = asyncio.run(make())
    toml = tmp_path / "aims.toml"
    toml.write_text(COSTLY)
    oracle(monkeypatch, [])
    no = json.dumps({"finding": "no", "quote": "", "frames": []})
    script = Script(*[say(no)] * 10)
    real = scoring.score_sealed

    async def scripted(*args: Any, **kwargs: Any) -> Any:
        return await real(*args, **kwargs, transport=script.transport())

    async def no_docker(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no Docker backend here")

    monkeypatch.setattr(scoring, "score_sealed", scripted)
    monkeypatch.setattr(DockerBackend, "open", no_docker)
    return toml, run, script


def test_aims_score_asks_past_the_threshold_and_refuses_without_a_terminal(
    tmp_path: Path, costly: tuple[Path, str, Script]
) -> None:
    toml, run, script = costly
    base = ["score", run, "--config", str(toml), "--runs-dir", str(tmp_path / "runs")]
    # a cap of $5 is past the threshold of $1: with no terminal it refuses and calls nothing
    out = CliRunner().invoke(app, [*base, "--budget-usd", "5"], input="n\n")
    assert out.exit_code == 1, out.output
    said = flat(out.stderr)
    assert "No past scores to go by. Stops at $5.00, its spend cap." in said
    assert "the caps allow $5.00, above confirm_above_usd $1.00; pass --yes to start anyway" in said
    assert script.requests == []
    out = CliRunner().invoke(app, [*base, "--budget-usd", "5", "--yes", "--json"])
    assert out.exit_code == 0, out.output
    shown = json.loads(out.stdout)
    assert (shown["estimate"]["budget_usd"], shown["estimate"]["confirm"]) == (5, True)
    assert shown["stopped"] is None
    assert len(script.requests) == 1
    # the call gets the output its cap leaves room for, not the profile's million
    assert 0 < script.requests[0]["max_tokens"] < 1_000_000
    # the config's cap of $1 is not past the threshold, so nothing asks
    out = CliRunner().invoke(app, [*base, "--json"], input="n\n")
    assert out.exit_code == 0, out.output
    e = json.loads(out.stdout)["estimate"]
    assert (e["budget_usd"], e["ceiling_usd"], e["confirm"]) == (1.0, 1.0, False)
    assert len(script.requests) == 2
    assert script.requests[1]["max_tokens"] < script.requests[0]["max_tokens"]
    # a cap of 0 starts no call; the score says the checks stopped
    out = CliRunner().invoke(app, [*base, "--budget-usd", "0"])
    assert out.exit_code == 0, out.output
    assert "warning: the checks stopped: the score spent $0.0000 of its $0" in flat(out.stderr)
    assert len(script.requests) == 2


def test_aims_run_counts_the_score_it_makes(
    tmp_path: Path, costly: tuple[Path, str, Script]
) -> None:
    toml, _, script = costly
    scenario = tmp_path / "scenario" / "scenario.yaml"
    args = ["run", str(scenario), "--config", str(toml), "--no-server", "--monitor", "signals"]
    args += ["--runs-dir", str(tmp_path / "runs"), "--json"]
    out = CliRunner().invoke(app, args)
    assert out.exit_code == 1, out.output
    assert "pass --yes to start anyway" in flat(out.stderr)
    first = json.loads(out.stdout.splitlines()[0])
    assert (first["type"], first["confirm"]) == ("estimate", True)
    line = "Score: checks on classify. No past scores to go by. Stops at $1.00, its spend cap."
    assert line in first["lines"]
    assert first["parts"][0]["score"]["ceiling_usd"] == 1.0
    out = CliRunner().invoke(app, [*args, "--no-score"])
    first = json.loads(out.stdout.splitlines()[0])
    assert first["parts"][0]["score"] is None
    assert not any(x.startswith("Score:") for x in first["lines"])
    assert script.requests == []


def test_the_score_and_estimate_commands_take_the_cost_flags() -> None:
    import typer.core
    import typer.main

    command = typer.main.get_command(app)
    assert isinstance(command, typer.core.TyperGroup)
    params = {c: {p.name: p for p in command.commands[c].params} for c in ("score", "estimate")}
    assert params["score"]["yes"].default is False
    assert params["score"]["budget_usd"].default is None
    assert params["estimate"]["score"].default is True
