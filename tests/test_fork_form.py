"""What the UI's fork dialog needs from the control plane: the estimate of a fork, the cost guard
on forks, and an oversight and a budget that replace the scenario's on runs and forks."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import click
import pytest
from cp import CONFIG, CP, control_plane
from cp import SCENARIO as CONTROL
from test_server import FakeRunner, push_forged
from typer.testing import CliRunner

from aims.cli import app
from aims.config import load as load_settings
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted
from aims.prompts import Prompts
from aims.run.runner import resolve_texts
from aims.scenario import load
from aims.scenario.budget import BudgetOverride

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


async def recorded(runs: Path, scenario: Path, config: Path) -> str:
    """A sealed run of `scenario` as `aims run` records it, with no steps."""
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
        config={"scenario": bundle.snapshot(), "agents": {"a": entry}},
    )
    async with await LocalEventStore.open(runs) as store:
        run_id = await store.create_run(started, blobs=bundle.blobs())
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
    return run_id


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
