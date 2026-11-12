"""What the UI's fork dialog needs from the control plane: the estimate of a fork, the cost guard
on forks, and an oversight and a budget that replace the scenario's on runs and forks."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import click
import pytest
from cp import CONFIG, CP, control_plane
from cp import SCENARIO as CONTROL
from test_cli_monitor import backend
from test_fork_whole import step
from test_server import FakeRunner, push_forged, ship
from typer.testing import CliRunner

from aims.cli import app
from aims.config import load as load_settings
from aims.events import (
    AgentStep,
    Draft,
    LocalEventStore,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.monitor.setup import ResolvedMonitor
from aims.prompts import Prompts
from aims.run import truth
from aims.run.estimate import plan_fork
from aims.run.fork import CutAt, cut_at, fork_agents, fork_of, load_origin
from aims.run.runner import Result, Runner, resolve_texts
from aims.sandbox import DockerBackend
from aims.scenario import load, parse
from aims.scenario.budget import BudgetOverride
from aims.scenario.load import Bundle

pytestmark = pytest.mark.anyio

SCENARIO = {"source": "project", "name": "control"}


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        idle = FakeRunner(cp, "idle")
        await idle.client.hello(idle.hello_body())
        await idle.client.aclose()
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


async def test_forks_are_estimated_and_above_the_confirm_limit_need_confirm(
    cp: CP, local: LocalEventStore
) -> None:
    origin = await push_forged(cp, local, "http://model.invalid/v1")
    estimate = f"/api/runs/{origin}/forks/estimate"
    forks = f"/api/runs/{origin}/forks"
    async with cp.client(cp.viewer) as c:
        assert (await c.get(estimate)).status_code == 403
    async with cp.client(cp.operator) as c:
        r = await c.get(estimate, params={"samples": 5, "monitor": "signals"})
        assert r.status_code == 200, r.text
        e = r.json()
        # five forks whose agent may spend $5 each: the caps allow $25, above the $20 default
        assert (e["confirm"], e["samples"], e["parts"][0]["replayed"]) == (True, 5, {"a": 0})
        assert e["lines"][1] == (
            "A fork after step 0: the agents pay only for the steps after it; the monitor"
            " reviews every step, replayed too."
        )
        r = await c.post(forks, json={"samples": 5, "monitor": "signals"})
        assert (r.status_code, r.json()["code"]) == (409, "confirm")
        assert "set confirm to start anyway" in r.json()["detail"]
        assert await cp.state.db.jobs() == []
        r = await c.post(forks, json={"samples": 5, "monitor": "signals", "confirm": True})
        assert r.status_code == 202, r.text
        assert len(r.json()["run_ids"]) == 5
        r = await c.post(forks, json={"monitor": "signals"})
        assert r.status_code == 202, r.text
        r = await c.get(estimate, params={"step": 0, "at": "a:0"})
        assert (r.status_code, r.json()["detail"]) == (422, "give step or at, not both")


# Step 1 returned; the run ended in step 2 before its tool call returned.
MID_STEP = [
    Draft(ToolCall(call_id="t1", name="bash", arguments="{}", model_call="m1"), "a"),
    Draft(AgentStep(n=1, call_id="m1", status="completed", tool_calls=["t1"]), "a"),
    Draft(ToolResult(call_id="t1", output="ok"), "a"),
    Draft(ToolCall(call_id="t2", name="bash", arguments="{}", model_call="m2"), "a"),
    Draft(AgentStep(n=2, call_id="m2", status="completed", tool_calls=["t2"]), "a"),
]
HALF = (
    "step 2 of agent a cannot be replayed: the run ended before its tool calls returned, so"
    " they have no recorded output; fork at step 1 or before"
)


async def test_a_fork_of_a_run_that_ended_mid_step_cuts_before_that_step(
    cp: CP, local: LocalEventStore
) -> None:
    origin = await push_forged(cp, local, "http://model.invalid/v1", drafts=MID_STEP)
    estimate = f"/api/runs/{origin}/forks/estimate"
    forks = f"/api/runs/{origin}/forks"
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{origin}")
        assert r.json()["replayable"] == {"a": 1}
    async with cp.client(cp.operator) as c:
        r = await c.get(estimate, params={"monitor": "signals"})
        assert r.status_code == 200, r.text
        assert r.json()["parts"][0]["replayed"] == {"a": 1}
        r = await c.get(estimate, params={"step": 2, "monitor": "signals"})
        assert (r.status_code, r.json()["code"], r.json()["detail"]) == (422, "fork", HALF)
        r = await c.post(forks, json={"step": 2, "monitor": "signals"})
        assert (r.status_code, r.json()["code"], r.json()["detail"]) == (422, "fork", HALF)
        r = await c.post(forks, json={"monitor": "signals"})
        assert r.status_code == 202, r.text
        (fork,) = r.json()["run_ids"]
    job = await cp.state.db.job(fork)
    assert job is not None
    assert job.payload["fork"]["step"] == 1


async def test_runs_and_forks_take_an_oversight_over_the_scenarios(
    cp: CP, local: LocalEventStore
) -> None:
    origin = await push_forged(cp, local, "http://model.invalid/v1")
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs", json={"scenario": SCENARIO, "monitor": "signals", "oversight": "human"}
        )
        assert r.status_code == 202, r.text
        (run,) = r.json()["run_ids"]
        r = await c.post("/api/runs", json={"scenario": SCENARIO, "oversight": "loud"})
        assert r.status_code == 422
        r = await c.post(
            f"/api/runs/{origin}/forks", json={"monitor": "signals", "oversight": "autonomous"}
        )
        assert r.status_code == 202, r.text
        (fork,) = r.json()["run_ids"]
        r = await c.post(f"/api/runs/{origin}/forks", json={"monitor": "signals"})
        (plain,) = r.json()["run_ids"]
    oversight = {}
    for run_id in (run, fork, plain):
        job = await cp.state.db.job(run_id)
        assert job is not None
        oversight[run_id] = job.payload["options"]["oversight"]
    assert oversight == {run: "human", fork: "autonomous", plain: None}


async def test_runs_and_forks_take_a_budget_over_the_scenarios(
    cp: CP, local: LocalEventStore
) -> None:
    origin = await push_forged(
        cp,
        local,
        "http://model.invalid/v1",
        budget={"every": {"steps": 200}, "run": {"cost_usd": 10}},
    )

    async def caps(run_id: str) -> BudgetOverride:
        job = await cp.state.db.job(run_id)
        assert job is not None
        return BudgetOverride.model_validate(job.payload["options"]["budget"])

    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{origin}")
        assert r.json()["budget"] == [
            {"scope": "agent", "agent": "a", "key": "steps", "value": 200, "scenario": 100},
            {"scope": "run", "agent": None, "key": "cost_usd", "value": 10.0, "scenario": None},
        ]
    async with cp.client(cp.operator) as c:
        body = {"scenario": SCENARIO, "monitor": "signals"}
        r = await c.post("/api/runs", json=body | {"budget": {"every": {"steps": 7}}})
        assert r.status_code == 202, r.text
        (run,) = r.json()["run_ids"]
        lifted = {"every": {"steps": None, "cost_usd": None}}
        r = await c.post("/api/runs", json=body | {"budget": lifted})
        assert (r.status_code, r.json()["code"]) == (409, "confirm")
        r = await c.post("/api/runs", json=body | {"budget": lifted, "confirm": True})
        (free,) = r.json()["run_ids"]
        r = await c.post("/api/runs", json=body | {"budget": {"agents": {"zed": {"steps": 1}}}})
        assert (r.status_code, r.json()["code"]) == (422, "budget")
        assert r.json()["detail"] == "no agent zed in the scenario; its agents are a"
        r = await c.post("/api/runs", json=body | {"budget": {"every": {"steps": 0}}})
        assert r.status_code == 422

        estimate = "/api/scenarios/project/control/estimate"
        r = await c.get(estimate, params={"budget": json.dumps({"every": {"steps": 7}})})
        assert r.status_code == 200, r.text
        assert r.json()["parts"][0]["caps"]["agents"]["a"]["steps"] == 7
        r = await c.get(estimate, params={"budget": '{"every": {"steps": 0}}'})
        assert (r.status_code, r.json()["code"]) == (422, "budget")

        forks = f"/api/runs/{origin}/forks"
        r = await c.post(forks, json={"monitor": "signals"})
        (kept,) = r.json()["run_ids"]
        r = await c.post(forks, json={"monitor": "signals", "budget": {"every": {"steps": 300}}})
        (raised,) = r.json()["run_ids"]
        r = await c.post(forks, json={"monitor": "signals", "budget": {"agents": {"b": {}}}})
        assert (r.status_code, r.json()["code"]) == (422, "budget")
        params = {"budget": json.dumps({"every": {"wall_s": 60}})}
        r = await c.get(f"{forks}/estimate", params=params)
        part = r.json()["parts"][0]["caps"]
        assert (part["agents"]["a"]["steps"], part["agents"]["a"]["wall_s"]) == (200, 60)
        assert part["run"]["cost_usd"] == 10
    assert (await caps(run)).agent_caps("a") == {"steps": 7}
    assert (await caps(free)).agent_caps("a") == {"steps": None, "cost_usd": None}
    assert ((await caps(kept)).agent_caps("a"), (await caps(kept)).run_caps()) == (
        {"steps": 200},
        {"cost_usd": 10},
    )
    assert ((await caps(raised)).agent_caps("a"), (await caps(raised)).run_caps()) == (
        {"steps": 300},
        {"cost_usd": 10},
    )


async def test_runs_and_forks_are_watched_by_the_monitor_their_estimate_prices(
    cp: CP, local: LocalEventStore
) -> None:
    origin = await push_forged(
        cp, local, "http://model.invalid/v1", budget={"every": {"steps": 2000}}
    )
    # the standard monitor calls the built-in sweep profile, which reads MODEL_API_KEY
    keyed = FakeRunner(cp, "keyed", envs=["MODEL_API_KEY"])
    await keyed.client.hello(keyed.hello_body())
    await keyed.client.aclose()

    async def cap(run_id: str) -> float:
        job = await cp.state.db.job(run_id)
        assert job is not None
        return job.payload["monitor"]["budget_usd"]

    async with cp.client(cp.operator) as c:

        async def started(path: str, budget: dict[str, Any] | None) -> str:
            body: dict[str, Any] = {"monitor": "standard", "budget": budget, "confirm": True}
            if path == "/api/runs":
                body["scenario"] = SCENARIO
            r = await c.post(path, json=body)
            assert r.status_code == 202, r.text
            return r.json()["run_ids"][0]

        async def ceiling(path: str, budget: dict[str, Any] | None) -> float:
            params = {"monitor": "standard"}
            if budget is not None:
                params["budget"] = json.dumps(budget)
            r = await c.get(path, params=params)
            assert r.status_code == 200, r.text
            return r.json()["parts"][0]["monitor"]["ceiling_usd"]

        runs = ("/api/runs", "/api/scenarios/project/control/estimate")
        forks = (f"/api/runs/{origin}/forks", f"/api/runs/{origin}/forks/estimate")
        plain = await cap(await started(runs[0], None))
        for (start, estimate), budget in [
            (runs, {"every": {"steps": 5}}),
            (runs, {"every": {"steps": 2000}}),
            (forks, None),
            (forks, {"every": {"steps": 5}}),
        ]:
            watched = await cap(await started(start, budget))
            assert watched == await ceiling(estimate, budget)
            assert watched != plain


CREW = """\
version: 1
name: crew
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
agents:
  - id: a
  - id: b
    role: confederate
    script:
      - {run: "true"}
      - {run: "true"}
"""


async def test_a_fork_is_watched_over_the_full_scenario_its_estimate_prices(
    cp: CP, local: LocalEventStore
) -> None:
    path = cp.project.parent / "crew.yaml"
    path.write_text(CREW)
    prepared = truth.prepare(load(path), None)
    # the public copy has no script, so it plans b's default 100 steps for the monitor
    profile = cp.state.config.settings().models.get("scripted").snapshot()
    agents = {
        a.id: {
            "profile": profile,
            "schedule": [[None, profile]],
            "texts": resolve_texts(Prompts(), a).snapshot(),
        }
        for a in prepared.scenario.agents
    }
    run_id = str(uuid.uuid7())
    local.prepare_run(run_id)
    first = RunStarted(
        scenario="crew",
        oversight="observe",
        agents=["a", "b"],
        config={"scenario": prepared.bundle.snapshot(), "agents": agents},
        truth=truth.write(local.root / run_id, prepared.truth),
    )
    await local.create_run(first, blobs=prepared.bundle.blobs(), run_id=run_id)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    keyed = FakeRunner(cp, "keyed", envs=["MODEL_API_KEY"])
    await keyed.client.hello(keyed.hello_body())
    await ship(local, keyed.client, run_id)
    await keyed.client.aclose()
    body = {"at": "a:0", "monitor": "standard"}
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{run_id}/forks/estimate", params=body)
        assert r.status_code == 200, r.text
        ceiling = r.json()["parts"][0]["monitor"]["ceiling_usd"]
        r = await c.post(f"/api/runs/{run_id}/forks", json=body | {"confirm": True})
        assert r.status_code == 202, r.text
        (fork,) = r.json()["run_ids"]
    job = await cp.state.db.job(fork)
    assert job is not None
    assert job.payload["monitor"]["budget_usd"] == ceiling


async def recorded(
    runs: Path,
    scenario: Path,
    config: Path,
    budget: dict[str, Any] | None = None,
    drafts: list[Draft] | None = None,
) -> str:
    """A sealed run of `scenario` as `aims run` records it, run under `budget`, whose events
    are `drafts`: no steps by default."""
    bundle = load(scenario)
    profile = load_settings(config).models.get("scripted")
    entry = {
        "profile": profile.snapshot(),
        "schedule": [[None, profile.snapshot()]],
        "texts": resolve_texts(Prompts(), bundle.scenario.agents[0]).snapshot(),
    }
    started = RunStarted(
        scenario="control",
        oversight="observe",
        agents=["a"],
        config={"scenario": bundle.snapshot(), "agents": {"a": entry}, "budget": budget},
    )
    async with await LocalEventStore.open(runs) as store:
        run_id = await store.create_run(started, blobs=bundle.blobs())
        await store.append(run_id, [*(drafts or []), Draft(RunFinished(status="completed"))])
    return run_id


def test_aims_run_forks_a_run_that_ended_mid_step_before_that_step(tmp_path: Path) -> None:
    config = tmp_path / "aims.toml"
    config.write_text(CONFIG + "[estimate]\nconfirm_above_usd = 1\n")
    scenario = tmp_path / "control.yaml"
    scenario.write_text(CONTROL)
    runs = tmp_path / "runs"
    origin = asyncio.run(recorded(runs, scenario, config, drafts=MID_STEP))
    args = ["run", "--replay", origin, "--config", str(config), "--runs-dir", str(runs)]
    args += ["--no-server", "--monitor", "off", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    first = json.loads(result.stdout.splitlines()[0])
    assert (first["type"], first["parts"][0]["replayed"]) == ("estimate", {"a": 1})
    result = CliRunner().invoke(app, [*args, "--fork-at", "2"])
    assert result.exit_code == 1, result.output
    assert f"error: {HALF}" in " ".join(result.stderr.split())
    assert not result.stdout


def test_aims_run_prices_a_replay_as_a_fork_and_checks_the_oversight(tmp_path: Path) -> None:
    config = tmp_path / "aims.toml"
    config.write_text(CONFIG + "[estimate]\nconfirm_above_usd = 1\n")
    scenario = tmp_path / "control.yaml"
    scenario.write_text(CONTROL)
    runs = tmp_path / "runs"
    origin = asyncio.run(recorded(runs, scenario, config))
    args = ["run", "--replay", origin, "--config", str(config), "--runs-dir", str(runs)]
    args += ["--no-server", "--monitor", "off", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "pass --yes to start anyway" in " ".join(result.stderr.split())
    first = json.loads(result.stdout.splitlines()[0])
    assert (first["type"], first["confirm"], first["parts"][0]["replayed"]) == (
        "estimate",
        True,
        {"a": 0},
    )
    assert first["lines"][1] == "A fork after step 0: the agents pay only for the steps after it."
    result = CliRunner().invoke(app, [*args, "--oversight", "loud"])
    assert result.exit_code == 2
    assert "use observe, autonomous, human" in " ".join(result.output.split())


def test_aims_run_and_estimate_take_a_budget_and_say_it(tmp_path: Path) -> None:
    config = tmp_path / "aims.toml"
    config.write_text(CONFIG + "[estimate]\nconfirm_above_usd = 1\n")
    scenario = tmp_path / "control.yaml"
    scenario.write_text(CONTROL)
    runs = tmp_path / "runs"
    origin = asyncio.run(recorded(runs, scenario, config))
    args = ["run", "--replay", origin, "--config", str(config), "--runs-dir", str(runs)]
    args += ["--no-server", "--monitor", "off", "--json"]
    result = CliRunner().invoke(app, [*args, "--budget", "steps=7,run.cost_usd=2"])
    assert result.exit_code == 1, result.output
    assert "Budget: steps 7 (scenario 100), run cost $2 (scenario no cap)" in result.stderr
    first = json.loads(result.stdout.splitlines()[0])
    assert first["parts"][0]["caps"]["agents"]["a"]["steps"] == 7
    result = CliRunner().invoke(app, [*args, "--budget", "zed.steps=1"])
    assert result.exit_code == 1
    assert "--budget: no agent zed in the scenario" in result.stderr
    result = CliRunner().invoke(app, [*args, "--budget", "steps=0"])
    assert result.exit_code == 2
    said = " ".join(click.unstyle(result.output).replace("│", "").split())
    assert "greater than or equal to 1" in said

    estimate = ["estimate", str(scenario), "--config", str(config), "--runs-dir", str(runs)]
    result = CliRunner().invoke(app, [*estimate, "--json", "--budget", "cost_usd=none"])
    assert result.exit_code == 0, result.output
    caps = json.loads(result.stdout)["parts"][0]["caps"]["agents"]["a"]
    assert (caps["steps"], caps["cost_usd"]) == (100, None)


def test_aims_run_is_watched_by_the_monitor_its_estimate_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "aims.toml"
    config.write_text(CONFIG)
    scenario = tmp_path / "control.yaml"
    scenario.write_text(CONTROL)
    runs = tmp_path / "runs"
    origin = asyncio.run(recorded(runs, scenario, config, {"every": {"steps": 2000}}))
    watched: list[float] = []

    async def run(self: Runner) -> Result:
        assert self.watch is not None
        watched.append(self.watch.budget_usd)
        return Result(None, "cancelled", "not run")

    monkeypatch.setattr(DockerBackend, "open", backend)
    monkeypatch.setattr(Runner, "run", run)
    monkeypatch.setenv("MODEL_API_KEY", "key")

    def ceiling(*args: str) -> float:
        common = ["--config", str(config), "--runs-dir", str(runs), "--no-server"]
        common += ["--monitor", "standard", "--json", "--yes"]
        result = CliRunner().invoke(app, ["run", *args, *common])
        first = json.loads(result.stdout.splitlines()[0])
        assert first["type"] == "estimate", result.output
        return first["parts"][0]["monitor"]["ceiling_usd"]

    plain = ceiling(str(scenario))
    assert watched == [plain]
    for args in (
        [str(scenario), "--budget", "steps=5"],
        [str(scenario), "--budget", "steps=2000"],
        ["--replay", origin],
        ["--replay", origin, "--budget", "steps=5"],
    ):
        assert ceiling(*args) == watched[-1] != plain


PAIR = """\
version: 1
name: pair
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
agents:
  - id: a
  - id: b
"""

# The run used a profile old, which this config no longer has.
SWITCHED = """\
[profiles.new]
backend = "chat"
base_url = "http://model.invalid/v1"
model = "new"
weights = "w-new"
reasoning = "full"
stream = false

[profiles.other]
extends = "new"
model = "other"
weights = "w-other"

[profiles.extract]
extends = "new"
model = "extract"
weights = "w-extract"

[roles]
agent = "new"

[monitor]
extract = ["extract"]
"""


async def switched(store: LocalEventStore, config: Path) -> str:
    """A sealed run of PAIR: agent a took its steps 1 and 2 with profile old and its step 3
    with new; b took its step 1 with new, after a's first."""
    bundle = Bundle(scenario=parse(PAIR), text=PAIR.encode())
    new = load_settings(config).models.get("new")
    old = new.model_copy(update={"name": "old", "model": "old", "weights": "w-old"})
    schedules = {"a": [[2, old.snapshot()], [None, new.snapshot()]], "b": [[None, new.snapshot()]]}
    agents = {
        a.id: {
            "profile": new.snapshot(),
            "schedule": schedules[a.id],
            "texts": resolve_texts(Prompts(), a).snapshot(),
        }
        for a in bundle.scenario.agents
    }
    started = RunStarted(
        scenario="pair",
        oversight="observe",
        agents=["a", "b"],
        config={"scenario": bundle.snapshot(), "agents": agents},
    )
    run_id = await store.create_run(started, blobs=bundle.blobs())
    drafts = [*step("a", 1), *step("b", 1), *step("a", 2), *step("a", 3)]
    await store.append(run_id, [*drafts, Draft(RunFinished(status="completed"))])
    return run_id


async def test_a_forks_monitor_is_resolved_with_the_profiles_its_agents_run(
    local: LocalEventStore, tmp_path: Path
) -> None:
    """The steps an agent replays as recorded, then its live profile: a profile the origin used
    only after the cut is left out, unless the agent keeps its recorded answers."""
    config = tmp_path / "aims.toml"
    config.write_text(SWITCHED)
    models = load_settings(config).models
    origin = await load_origin(local, await switched(local, config))

    def names(at: str, **options: Any) -> list[str]:
        cut = cut_at(origin, at)
        assert isinstance(cut, CutAt)
        fork = fork_of(origin, min(cut.steps.values()), cut, **options)
        return sorted({p.name for p in fork_agents(models, origin, fork)})

    assert names("a:3") == ["new", "old"]
    assert names("a:1", profile="other") == ["old", "other"]
    assert names("a:1", profile="other", keep_others=True) == ["new", "old", "other"]
    assert names("a:0", profile="other") == ["other"]


def test_aims_run_watches_a_cut_fork_with_the_monitor_its_estimate_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork at a:3 replays a's steps with old and b's first with new, then each goes live with
    new: it needs no old in the config, and its monitor is the one its estimate prices."""
    config = tmp_path / "aims.toml"
    config.write_text(SWITCHED)
    runs = tmp_path / "runs"

    async def priced() -> tuple[str, ResolvedMonitor | None]:
        async with await LocalEventStore.open(runs) as store:
            run_id = await switched(store, config)
            origin = await load_origin(store, run_id)
        cut = cut_at(origin, "a:3")
        assert isinstance(cut, CutAt)
        assert cut.steps == {"a": 3, "b": 1}
        fork = fork_of(origin, 1, cut)
        return run_id, plan_fork(load_settings(config), origin, fork, preset="standard").monitor

    run_id, estimated = asyncio.run(priced())
    assert estimated is not None
    assert estimated.extract is not None
    watched: list[Runner] = []

    async def run(self: Runner) -> Result:
        watched.append(self)
        return Result(None, "cancelled", "not run")

    monkeypatch.setattr(DockerBackend, "open", backend)
    monkeypatch.setattr(Runner, "run", run)
    monkeypatch.setenv("MODEL_API_KEY", "key")
    args = ["run", "--replay", run_id, "--fork-at", "a:3", "--config", str(config)]
    args += ["--runs-dir", str(runs), "--no-server", "--monitor", "standard", "--json", "--yes"]
    result = CliRunner().invoke(app, args)
    assert len(watched) == 1, result.output
    (runner,) = watched
    assert runner.watch is not None
    assert (runner.watch.identity, runner.watch.extract) == (estimated.identity, estimated.extract)
    assert runner.given_profiles is not None
    assert {a: p.name for a, p in runner.given_profiles.items()} == {"a": "new", "b": "new"}


async def test_the_control_plane_queues_a_cut_fork_with_the_monitor_its_estimate_prices(
    cp: CP, local: LocalEventStore
) -> None:
    """The fork job carries the monitor the estimate prices and the profile its runner needs
    for each agent's live steps."""
    cp.project.write_text(SWITCHED)
    run_id = await switched(local, cp.project)
    keyed = FakeRunner(cp, "keyed", envs=["MODEL_API_KEY"])
    await keyed.client.hello(keyed.hello_body())
    await ship(local, keyed.client, run_id)
    await keyed.client.aclose()
    origin = await load_origin(local, run_id)
    cut = cut_at(origin, "a:3")
    assert isinstance(cut, CutAt)
    fork = fork_of(origin, 1, cut)
    estimated = plan_fork(cp.state.config.settings(), origin, fork, preset="standard").monitor
    assert estimated is not None
    body = {"at": "a:3", "monitor": "standard", "confirm": True}
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{run_id}/forks", json=body)
        assert r.status_code == 202, r.text
        (fork_id,) = r.json()["run_ids"]
    job = await cp.state.db.job(fork_id)
    assert job is not None
    assert job.payload["monitor"]["identity"] == estimated.identity
    assert job.payload["monitor"]["extract"]["name"] == "extract"
    assert "new" in job.payload["models"]
    assert "old" not in job.payload["models"]
