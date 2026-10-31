import asyncio
import json
import math
import statistics
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from typer.testing import CliRunner

from aims.cli import app
from aims.config import ConfigError, Settings, load
from aims.events import (
    AgentEnd,
    AgentFinished,
    AgentStarted,
    AgentStep,
    Draft,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
    MonitorDegraded,
    RunFinished,
    RunStarted,
    RunStatus,
    split_lines,
)
from aims.events.store import LOG
from aims.models import DeltaBus
from aims.run.estimate import (
    CACHED_SHARE,
    Estimate,
    History,
    Progress,
    estimate_runs,
    guard,
    history,
    load_history,
    plan,
    project,
    project_run,
    provider,
    spend,
    spend_lines,
)
from aims.run.view import LiveView, _Run
from aims.scenario import Bundle
from aims.scenario import load as load_scenario

CONFIG = """\
[profiles.agent]
backend = "chat"
base_url = "https://api.meta.ai/v1"
model = "agent-model"
weights = "agent-weights"
reasoning = "full"
effort = "medium"
context_tokens = 100000
max_output_tokens = 1000
price = { input = 1.0, cached = 0.1, output = 2.0 }

[profiles.other]
extends = "agent"
model = "other-model"
weights = "other-weights"

[profiles.free]
extends = "agent"
model = "free-model"
weights = "free-weights"
price = { input = 0.0, cached = 0.0, output = 0.0 }

[profiles.glimmer]
backend = "chat"
base_url = "https://someone--glimmer.modal.run/v1"
model = "glimmer-model"
weights = "glimmer-weights"
reasoning = "full"

[profiles.watcher]
backend = "chat"
base_url = "https://api.fireworks.ai/inference/v1"
model = "watch-model"
weights = "watch-weights"
reasoning = "full"
effort = "medium"
price = { input = 2.0, cached = 0.2, output = 4.0 }

[roles]
agent = "agent"
sweep = "watcher"
judge = "watcher"

[credits]
meta = 100
"""

SCENARIO = """\
version: 1
name: {name}
oversight: observe
task:
  instruction: Do it.
  environment: {{image: python:3.13-slim}}
monitor: {{preset: {preset}}}
agents:
  - id: a
    budget: {budget}
{extra}"""

# One call of the agent profile costs at most 100k input at $1 and 1k output at $2 per million.
STEP = 0.102


def write(
    tmp: Path,
    *,
    name: str = "s",
    budget: str = "{steps: 10}",
    preset: str = "signals",
    extra: str = "",
    config: str = CONFIG,
) -> tuple[Settings, Path]:
    (tmp / "aims.toml").write_text(config)
    path = tmp / f"{name}.yaml"
    path.write_text(SCENARIO.format(name=name, budget=budget, preset=preset, extra=extra))
    return load(tmp / "aims.toml"), path


def output_for(cost: float) -> int:
    """Output tokens that cost `cost` at the agent profile's price."""
    return round(cost * 1_000_000 / 2.0)


async def record(
    store: LocalEventStore,
    settings: Settings,
    bundle: Bundle,
    cost: float,
    *,
    variant: str | None = None,
    reason: AgentEnd = "submitted",
    status: RunStatus = "completed",
    taken: int = 5,
    usage: ModelUsage | None = None,
    monitor_cost: float = 0.0,
    monitor_capped: bool = False,
    fork: bool = False,
    profile: str | None = None,
) -> str:
    """A sealed run of `bundle` as the runner writes it: run.started with the agents' profiles,
    the resolved scenario and monitor, one agent call costing `cost`, the end of each agent."""
    p = plan(settings, bundle, profile=profile)
    agent = p.profiles["a"]
    config: dict[str, Any] = {
        "agents": {a: {"profile": q.snapshot()} for a, q in p.profiles.items()},
        "scenario": {"resolved": bundle.scenario.model_dump(mode="json")},
        "monitor": None if p.monitor is None else p.monitor.snapshot(),
        "fork": {"origin": "x", "step": 1} if fork else None,
    }
    started = RunStarted(scenario=bundle.scenario.name, oversight="observe", agents=["a"])
    run_id = await store.create_run(started.model_copy(update={"config": config}))
    body = await store.put_blob(run_id, b"{}")
    usage = usage or ModelUsage(input_tokens=0, cached_tokens=0, output_tokens=output_for(cost))
    drafts = [
        Draft(_request("c1", agent.name, agent.model, str(agent.base_url), None, body), "a"),
        Draft(_response("c1", usage, cost), "a"),
    ]
    if p.monitor is not None and p.monitor.sweeps:
        sweep = next(iter(p.monitor.sweeps.values()))
        tokens = output_for(monitor_cost / 2)
        watch = ModelUsage(input_tokens=0, cached_tokens=0, output_tokens=tokens)
        drafts += [
            Draft(_request("w1", sweep.name, sweep.model, str(sweep.base_url), "sweep", body), "a"),
            Draft(_response("w1", watch, monitor_cost), "a"),
        ]
    if monitor_capped:
        drafts.append(Draft(MonitorDegraded(part="budget", reason="spent", then="signals_only")))
    finished = AgentFinished(reason=reason, steps=taken, cost_usd=cost)
    drafts += [Draft(finished, "a"), Draft(RunFinished(status=status))]
    await store.append(run_id, drafts)
    if variant is not None:
        (store.root / run_id / "truth.json").write_text(json.dumps({"variant": variant}))
    return run_id


def _request(
    call: str, profile: str, model: str, url: str, role: ModelRole | None, body: Any
) -> ModelRequest:
    return ModelRequest(
        call_id=call,
        attempt=1,
        profile=profile,
        backend="chat",
        model=model,
        url=url,
        params=ModelParams(stream=False),
        body=body,
        key=body.sha256,
        role=role,
    )


def _response(call: str, usage: ModelUsage, cost: float | None) -> ModelResponse:
    return ModelResponse(
        call_id=call, attempt=1, status="completed", latency_ms=1, usage=usage, cost_usd=cost
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as s:
        yield s


def one(settings: Settings, found: History, bundle: Bundle, **kw: Any) -> Estimate:
    return estimate_runs(found, settings, [bundle], **kw)


# Config


def test_credits_and_the_confirm_threshold_come_from_the_config_layers(
    tmp_path: Path, user_config: Path
) -> None:
    (user_config / "config.toml").write_text("[credits]\nmeta = 150\nFireworks = 100\n")
    project = tmp_path / "aims.toml"
    project.write_text("[credits]\nmeta = 120\n[estimate]\nconfirm_above_usd = 3\n")
    settings = load(project)
    assert settings.credits == {"meta": 120.0, "fireworks": 100.0}
    assert settings.confirm_above_usd == 3.0
    assert load(None).confirm_above_usd == 20.0
    for bad in ("[credits]\nmeta = -1\n", '[credits]\nmeta = "x"\n', "[estimate]\nother = 1\n"):
        project.write_text(bad)
        with pytest.raises(ConfigError):
            load(project)


# Caps


def test_the_default_agent_cap_bounds_a_run_with_one_last_step(tmp_path: Path) -> None:
    settings, path = write(tmp_path, budget="{}")
    e = one(settings, History(), load_scenario(path))
    agents = e.parts[0].agents
    assert agents.ceiling_usd == pytest.approx(5.0 + STEP)
    assert (agents.stepped, agents.tier, agents.typical_usd) == (True, "cap", None)
    assert (
        "Agents: no past runs to go by. Cannot exceed $5.10 (the caps plus a last step per agent)."
        in (e.lines)
    )
    assert "Monitor: signals only, no model calls." in e.lines


def test_steps_and_context_bound_an_agent_below_its_cost_cap(tmp_path: Path) -> None:
    settings, path = write(tmp_path, budget="{steps: 10}", extra="limits: {cost_usd: 0.5}\n")
    e = one(settings, History(), load_scenario(path))
    assert e.parts[0].agents.ceiling_usd == pytest.approx(min(10 * STEP, 0.5 + STEP))


def test_no_cap_set_is_said_with_a_warning(tmp_path: Path) -> None:
    settings, path = write(tmp_path, budget="{steps: null, cost_usd: null}")
    e = one(settings, History(), load_scenario(path), samples=2)
    assert e.parts[0].agents.ceiling_usd is None
    assert e.total.ceiling_usd is None
    assert any("Cannot exceed: no cap set." in line for line in e.lines)
    assert e.warnings == [
        "no cap bounds the cost of agent a; set its budget.cost_usd or the scenario's"
        " limits.cost_usd"
    ]
    assert e.confirm


def test_the_monitor_budget_is_its_ceiling(tmp_path: Path) -> None:
    settings, path = write(tmp_path, preset="standard", extra="limits: {monitor_cost_usd: 2}\n")
    e = one(settings, History(), load_scenario(path))
    monitor = e.parts[0].monitor
    assert monitor is not None
    assert monitor.ceiling_usd == 2.0
    assert e.total.ceiling_usd == pytest.approx(10 * STEP + 2.0)


@pytest.mark.anyio
async def test_a_monitor_whose_model_runs_with_and_without_an_effort_is_matched(
    tmp_path: Path, store: LocalEventStore
) -> None:
    plain = """
[profiles.plain]
backend = "chat"
base_url = "https://api.fireworks.ai/inference/v1"
model = "watch-model"
weights = "watch-weights"
reasoning = "full"
price = { input = 2.0, cached = 0.2, output = 4.0 }
"""
    preset = "standard, review: {profile: plain}"
    settings, path = write(tmp_path, preset=preset, config=CONFIG + plain)
    bundle = load_scenario(path)
    m = plan(settings, bundle).monitor
    assert m is not None
    assert m.review is not None
    assert m.judge is not None
    assert (m.review.model, m.review.effort) == (m.judge.model, None)
    assert m.judge.effort is not None
    await record(store, settings, bundle, 0.1, monitor_cost=0.2)
    monitor = one(settings, await history(store), bundle).parts[0].monitor
    assert monitor is not None
    assert (monitor.tier, monitor.runs) == ("scenario", 1)


def test_a_profile_without_a_price_is_named_and_not_counted(tmp_path: Path) -> None:
    settings, path = write(tmp_path)
    e = one(settings, History(), load_scenario(path), profile="glimmer")
    assert e.unpriced == ["glimmer"]
    assert not e.parts[0].agents.priced
    assert "Agents: no list price, so not counted." in e.lines
    assert not e.confirm


# History


@pytest.mark.anyio
async def test_past_runs_match_on_scenario_variant_models_and_effort(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(
        tmp_path,
        budget="{steps: null, cost_usd: 50}",
        extra="variants: {hinted: {task: {instruction: Hint.}}}\n",
    )
    base, hinted = load_scenario(path), load_scenario(path, "hinted")
    for cost in (1.0, 2.0, 4.0):
        await record(store, settings, hinted, cost, variant="hinted")
    await record(store, settings, base, 0.01)
    await record(store, settings, hinted, 9.0, variant="hinted", profile="other")
    found = await history(store)
    e = one(settings, found, hinted)
    agents = e.parts[0].agents
    assert (agents.tier, agents.runs, agents.doubled) == ("scenario", 3, True)
    assert agents.typical_usd == pytest.approx(2.0)
    assert e.parts[0].variant == "hinted"
    base_estimate = one(settings, found, base)
    assert base_estimate.parts[0].agents.runs == 1
    assert base_estimate.parts[0].agents.typical_usd == pytest.approx(0.01)


@pytest.mark.anyio
async def test_other_scenarios_with_the_same_models_scale_by_step_budget(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path, name="old", budget="{steps: 40, cost_usd: 50}")
    old = load_scenario(path)
    for cost in (0.4, 0.4, 0.8):
        await record(store, settings, old, cost)
    settings, path = write(tmp_path, name="new", budget="{steps: 10, cost_usd: 50}")
    found = await history(store)
    e = one(settings, found, load_scenario(path))
    agents = e.parts[0].agents
    assert (agents.tier, agents.runs) == ("models", 3)
    assert agents.typical_usd == pytest.approx(0.1)
    assert any("of the same models, scaled to this step budget" in line for line in e.lines)
    other = one(settings, found, load_scenario(path), profile="other")
    assert other.parts[0].agents.tier == "cap"


@pytest.mark.anyio
async def test_percentiles_are_taken_on_log_cost(tmp_path: Path, store: LocalEventStore) -> None:
    settings, path = write(tmp_path, budget="{steps: null, cost_usd: 50}")
    bundle = load_scenario(path)
    costs = [float(c) for c in range(1, 13)]
    for cost in costs:
        await record(store, settings, bundle, cost)
    e = one(settings, await history(store), bundle)
    logs = [math.log(c) for c in costs]
    agents = e.parts[0].agents
    assert agents.typical_usd == pytest.approx(math.exp(statistics.median(logs)))
    p90 = math.exp(statistics.quantiles(logs, n=10, method="inclusive")[8])
    assert agents.high_usd == pytest.approx(p90)
    assert not agents.doubled
    typical, high = agents.typical_usd, agents.high_usd
    assert typical is not None
    assert high is not None
    line = (
        f"Agents: typically ${typical:.2f} (median of 12 runs). 1 in 10 runs exceeds ${high:.2f}."
    )
    assert any(text.startswith(line) for text in e.lines)


@pytest.mark.anyio
async def test_under_ten_runs_the_high_figure_is_twice_the_median(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path, budget="{steps: null, cost_usd: 50}")
    bundle = load_scenario(path)
    for cost in (1.0, 1.0, 4.0):
        await record(store, settings, bundle, cost)
    agents = one(settings, await history(store), bundle).parts[0].agents
    assert agents.typical_usd == pytest.approx(math.exp(statistics.median([0.0, 0.0, math.log(4)])))
    assert agents.high_usd == pytest.approx(2.0)
    assert agents.doubled


@pytest.mark.anyio
async def test_runs_that_hit_a_cap_are_counted(tmp_path: Path, store: LocalEventStore) -> None:
    settings, path = write(tmp_path)
    bundle = load_scenario(path)
    await record(store, settings, bundle, 0.5, reason="budget_cost")
    await record(store, settings, bundle, 0.5, reason="budget_steps")
    await record(store, settings, bundle, 0.2)
    e = one(settings, await history(store), bundle)
    assert e.parts[0].agents.capped == 2
    assert any("2 of them hit a cap." in line for line in e.lines)


@pytest.mark.anyio
async def test_forks_unsealed_and_failed_runs_are_left_out_but_their_spend_counts(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path)
    bundle = load_scenario(path)
    await record(store, settings, bundle, 1.0, fork=True)
    await record(store, settings, bundle, 1.0, status="failed")
    await record(store, settings, bundle, 1.0, status="cancelled")
    found = await history(store)
    assert found.runs == []
    assert found.spend == {"meta": pytest.approx(3.0)}


@pytest.mark.anyio
async def test_each_model_prices_unreported_cache_at_its_own_share(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path, budget="{steps: null, cost_usd: 50}")
    bundle = load_scenario(path)
    reported = ModelUsage(input_tokens=1000, cached_tokens=800, output_tokens=0)
    unreported = ModelUsage(input_tokens=1000, output_tokens=0)
    await record(store, settings, bundle, 0.00028, usage=reported)
    await record(store, settings, bundle, 0.001, usage=unreported)
    found = await history(store)
    assert found.shares == {"agent-model": pytest.approx(0.8)}
    costs = sorted(t.priced(settings.models.get("agent").price, 0.8) for t in _agent_tokens(found))
    assert costs == pytest.approx([0.00028, 0.00028])
    e = one(settings, found, bundle, preset="standard")
    rates = {r.model: r for r in e.rates}
    assert rates["agent-model"].cached_share == pytest.approx(0.8)
    assert rates["agent-model"].input_usd == pytest.approx(0.8 * 0.1 + 0.2 * 1.0)
    assert not rates["agent-model"].assumed
    assert rates["watch-model"].cached_share == CACHED_SHARE
    assert rates["watch-model"].assumed
    assert any(line.startswith("Blended input per million tokens:") for line in e.lines)


def _agent_tokens(found: History) -> list[Any]:
    return [t for past in found.runs for t in past.agents.values()]


@pytest.mark.anyio
async def test_the_monitor_estimate_matches_the_same_monitor(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path, preset="standard")
    bundle = load_scenario(path)
    for cost in (0.2, 0.4):
        await record(store, settings, bundle, 0.1, monitor_cost=cost, monitor_capped=cost > 0.3)
    found = await history(store)
    e = one(settings, found, bundle)
    monitor = e.parts[0].monitor
    assert monitor is not None
    assert (monitor.tier, monitor.runs, monitor.capped) == ("scenario", 2, 1)
    assert monitor.typical_usd == pytest.approx(math.sqrt(0.2 * 0.4))
    assert e.total.typical_usd == pytest.approx(0.1 + math.sqrt(0.08))
    signals = one(settings, found, bundle, preset="signals")
    assert signals.parts[0].monitor_preset == "signals"


# Spend and credits


@pytest.mark.anyio
async def test_spend_is_counted_per_provider_against_the_credits(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path, preset="standard")
    bundle = load_scenario(path)
    await record(store, settings, bundle, 1.5, monitor_cost=0.25)
    await record(store, settings, bundle, 2.5, monitor_cost=0.25)
    found = await history(store)
    assert found.spend == {"meta": pytest.approx(4.0), "fireworks": pytest.approx(0.5)}
    credits = spend(found, settings.credits)
    assert [(c.provider, c.left_usd) for c in credits] == [
        ("fireworks", None),
        ("meta", pytest.approx(96.0)),
    ]
    assert spend_lines(credits) == [
        "Fireworks: $0.50 spent.",
        "Meta: $4.00 of $100.00 spent, $96.00 left.",
    ]
    assert spend_lines([]) == ["No model calls on record."]


@pytest.mark.anyio
async def test_the_estimate_shows_the_credit_left_after_typical_and_high(
    tmp_path: Path, store: LocalEventStore
) -> None:
    settings, path = write(tmp_path)
    bundle = load_scenario(path)
    for cost in (0.5, 0.5):
        await record(store, settings, bundle, cost)
    e = one(settings, await history(store), bundle, samples=2)
    meta = next(c for c in e.credits if c.provider == "meta")
    assert meta.spent_usd == pytest.approx(1.0)
    assert meta.left_after_typical_usd == pytest.approx(100 - 1.0 - 2 * 0.5)
    assert meta.left_after_high_usd == pytest.approx(100 - 1.0 - 2 * 1.0)
    assert (
        "Meta: $1.00 of $100.00 spent, $98.00 left after the typical cost, $97.00 after the"
        " high one." in e.lines
    )
    assert (
        "Total for 2 runs: typically $1.00, $2.00 if every run is high. Cannot exceed $2.04."
        in (e.lines)
    )


def test_providers_are_named_by_their_domain() -> None:
    assert provider("https://api.meta.ai/v1") == "meta"
    assert provider("https://api.fireworks.ai/inference/v1") == "fireworks"
    assert provider("https://inference.baseten.co/v1") == "baseten"
    assert provider("https://someone--glimmer.modal.run/v1") == "modal"
    assert provider("http://127.0.0.1:8000/v1") == "127.0.0.1"
    assert provider(None, "modal-glimmer") == "modal"


# The guard


def test_the_guard_asks_on_a_terminal_and_refuses_without_one(tmp_path: Path) -> None:
    settings, path = write(tmp_path, budget="{}")
    e = estimate_runs(History(), settings, [load_scenario(path)])
    assert not e.confirm
    asked: list[str] = []
    answer = False

    def ask(question: str) -> bool:
        asked.append(question)
        return answer

    assert guard(e, yes=False, tty=False, ask=ask) is None
    high = e.model_copy(update={"confirm": True, "confirm_above_usd": 1.0})
    refused = guard(high, yes=False, tty=False, ask=ask)
    assert (
        refused == "the caps allow $5.10, above confirm_above_usd $1.00; pass --yes to start anyway"
    )
    assert guard(high, yes=True, tty=False, ask=ask) is None
    answer = True
    assert guard(high, yes=False, tty=True, ask=ask) is None
    answer = False
    assert guard(high, yes=False, tty=True, ask=ask) == "not started"
    assert asked == ["The caps allow $5.10, above confirm_above_usd $1.00. Start anyway?"] * 2
    unbounded = high.model_copy(
        update={"total": high.total.model_copy(update={"ceiling_usd": None})}
    )
    assert "no cap bounds the cost" in (guard(unbounded, yes=False, tty=False, ask=ask) or "")


def test_aims_run_refuses_without_a_terminal_unless_yes(tmp_path: Path) -> None:
    _, path = write(tmp_path, budget="{}", config=CONFIG + "[estimate]\nconfirm_above_usd = 1\n")
    args = ["run", str(path), "--config", str(tmp_path / "aims.toml"), "--no-server"]
    args += ["--runs-dir", str(tmp_path / "runs"), "--monitor", "off", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "pass --yes to start anyway" in " ".join(result.stderr.split())
    first = json.loads(result.stdout.splitlines()[0])
    assert first["type"] == "estimate"
    assert first["confirm"] is True
    assert first["parts"][0]["monitor"] is None


# CLI and API


def test_aims_estimate_and_spend_print_the_same_figures(tmp_path: Path) -> None:
    settings, path = write(
        tmp_path, extra="variants: {hinted: {task: {instruction: Hint.}}, bare: {}}\n"
    )

    async def seed() -> None:
        async with await LocalEventStore.open(tmp_path / "runs") as s:
            await record(s, settings, load_scenario(path), 0.3)

    asyncio.run(seed())
    config = ["--config", str(tmp_path / "aims.toml"), "--runs-dir", str(tmp_path / "runs")]
    runner = CliRunner()
    out = runner.invoke(app, ["estimate", str(path), *config])
    assert out.exit_code == 0, out.output
    found = asyncio.run(load_history(tmp_path / "runs"))
    expected = estimate_runs(found, settings, [load_scenario(path)])
    assert out.stdout.splitlines() == expected.lines
    assert expected.lines[1].startswith("Agents: typically $0.30 (median of 1 run).")
    many = runner.invoke(app, ["estimate", str(path), *config, "--variants", "all", "--json"])
    assert many.exit_code == 0, many.output
    data = json.loads(many.stdout)
    assert [p["variant"] for p in data["parts"]] == ["hinted", "bare"]
    both = runner.invoke(
        app, ["estimate", str(path), *config, "--variant", "x", "--variants", "all"]
    )
    assert both.exit_code == 2
    spent = runner.invoke(app, ["spend", *config])
    assert spent.stdout.splitlines() == ["Meta: $0.30 of $100.00 spent, $99.70 left."]
    check = runner.invoke(app, ["scenario", "check", str(path), *config])
    assert check.exit_code == 0, check.output
    assert "Cost estimate for s, one run, at list prices." in check.stdout


def test_scenario_check_reads_no_runs_it_does_not_have(tmp_path: Path) -> None:
    _, path = write(tmp_path)
    missing = tmp_path / "nothing"
    args = ["scenario", "check", str(path), "--config", str(tmp_path / "aims.toml")]
    result = CliRunner().invoke(app, [*args, "--runs-dir", str(missing)])
    assert result.exit_code == 0, result.output
    assert "Agents: no past runs to go by." in result.stdout
    assert not missing.exists()


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.mark.anyio
async def test_the_api_estimates_a_scenario_and_shows_spend(cp: CP) -> None:
    settings = cp.state.config.settings()
    bundle = load_scenario(cp.project.parent / "scenarios" / "control" / "scenario.yaml")
    p = plan(settings, bundle)
    started = RunStarted(scenario="control", oversight="observe", agents=["a"])
    config = {
        "agents": {"a": {"profile": p.profiles["a"].snapshot()}},
        "scenario": {"resolved": bundle.scenario.model_dump(mode="json")},
        "monitor": None if p.monitor is None else p.monitor.snapshot(),
    }
    local = await LocalEventStore.open(cp.project.parent / "local")
    run_id = await local.create_run(started.model_copy(update={"config": config}))
    body = await local.put_blob(run_id, b"{}")
    usage = ModelUsage(input_tokens=0, cached_tokens=0, output_tokens=100_000)
    await local.append(
        run_id,
        [
            Draft(_request("c1", "scripted", "m", "http://model.invalid/v1", None, body), "a"),
            Draft(_response("c1", usage, 0.2), "a"),
            Draft(AgentFinished(reason="submitted", steps=3, cost_usd=0.2), "a"),
            Draft(RunFinished(status="completed")),
        ],
    )
    await local.close()
    cp.state.store.prepare_run(run_id)
    await cp.state.store.put_blob(run_id, b"{}")
    await cp.state.store.ingest(run_id, split_lines((local.root / run_id / LOG).read_bytes()))
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/scenarios/project/control/estimate", params={"samples": 2})
        assert r.status_code == 200, r.text
        data = r.json()
        expected = estimate_runs(await history(cp.state.store), settings, [bundle], samples=2)
        assert data == expected.model_dump(mode="json")
        assert data["parts"][0]["agents"]["typical_usd"] == pytest.approx(0.2)
        assert data["parts"][0]["steps"] == {"a": 3.0}
        both = await c.get(
            "/api/scenarios/project/control/estimate", params={"variant": "x", "variants": "all"}
        )
        assert both.status_code == 422
        none = await c.get("/api/scenarios/project/control/estimate", params={"variants": "all"})
        assert none.status_code == 422
        missing = await c.get("/api/scenarios/project/nope/estimate")
        assert missing.status_code == 404
        spent = await c.get("/api/spend")
        assert spent.status_code == 200, spent.text
        assert spent.json()["providers"][0]["provider"] == "model"
        assert spent.json()["lines"] == ["Model: $0.20 spent."]


# While a run runs


def test_an_agent_is_projected_from_its_steps_and_bounded_by_its_caps() -> None:
    assert project(Progress(cost=1.0, steps=4, steps_cap=20)) == (5.0, 20.0)
    assert project(Progress(cost=1.0, steps=4, steps_cap=20, typical_steps=8)) == (2.0, 8.0)
    assert project(Progress(cost=1.0, steps=10, steps_cap=20, typical_steps=8)) == (1.0, 10.0)
    assert project(Progress(cost=1.0, steps=4, steps_cap=20, cost_cap=3.0)) == (3.0, 20.0)
    assert project(Progress(cost=1.0, steps=4)) == (1.0, 4.0)
    assert project(Progress(cost=2.0, steps=4, steps_cap=20, done=True)) == (2.0, 4.0)
    running = Progress(cost=1.0, steps=4, steps_cap=8)
    assert project_run([Progress(cost=1.0, steps=2, steps_cap=8)]) is None
    agents, monitor = project_run([running, running], monitor_cost=0.5) or (0, 0)
    assert (agents, monitor) == (4.0, 1.0)
    capped = project_run([running, running], cost_cap=3.0, monitor_cost=0.5, monitor_cap=0.6)
    assert capped == (3.0, 0.6)


@pytest.mark.anyio
async def test_the_live_view_shows_what_the_run_is_on_track_for(store: LocalEventStore) -> None:
    config = {
        "scenario": {"resolved": {"limits": {"cost_usd": 10.0}}},
        "monitor": {"budget_usd": 5.0},
    }
    started = RunStarted(scenario="s", oversight="observe", agents=["a"])
    run_id = await store.create_run(started.model_copy(update={"config": config}))
    body = await store.put_blob(run_id, b"{}")
    drafts = [
        Draft(
            AgentStarted(
                profile="p",
                model="m",
                backend="chat",
                tools=["bash"],
                system="x",
                budget={"steps": 20, "cost_usd": 5.0},
            ),
            "a",
        ),
        Draft(_request("w1", "w", "w", "https://api.meta.ai/v1", "sweep", body), "a"),
        Draft(_response("w1", ModelUsage(), 0.1), "a"),
    ]
    drafts += [
        Draft(AgentStep(n=n, call_id=f"c{n}", status="completed", total_cost_usd=0.1 * n), "a")
        for n in range(1, 5)
    ]
    await store.append(run_id, drafts)
    view = LiveView(store, DeltaBus(), {"a": 10.0})
    run = _Run(run_id)
    for e in await store.events(run_id, limit=100):
        view._on_event(run, e)
    row = view._cost_row(run)
    assert row is not None
    assert row.plain == "cost $0.50 so far, on track for about $1.25 (agents $1.00, monitor $0.25)"
