"""What the UI's fork dialog needs from the control plane: the estimate of a fork and the cost
guard on forks."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

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

pytestmark = pytest.mark.anyio


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


def test_aims_run_prices_a_replay_as_a_fork(tmp_path: Path) -> None:
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
