"""A run's budget override: how `--budget` reads, how it survives a job's JSON, how a fork's
goes over its origin's, and what it changes in a scenario."""

import pytest

from aims.run.fork import Origin
from aims.run.job import JobOptions
from aims.scenario import Bundle
from aims.scenario import parse as parse_scenario
from aims.scenario.budget import (
    BudgetError,
    BudgetOverride,
    apply,
    apply_resolved,
    changes,
    describe,
    load,
    over,
    parse,
    read_json,
)

SCENARIO = """\
version: 1
name: pair
oversight: observe
task:
  instruction: Do it.
  environment: {image: python:3.13-slim}
limits: {cost_usd: 8}
agents:
  - id: alice
    budget: {steps: 80, cost_usd: 4, wall_s: 1800}
  - id: bob
    budget: {steps: 30}
"""


def test_bare_keys_set_every_agent_a_prefix_one_agent_or_the_run() -> None:
    o = parse("steps=200, cost_usd=none, alice.steps=20, run.cost_usd=10, run.wall_s=600")
    assert o.agent_caps("alice") == {"steps": 20, "cost_usd": None}
    assert o.agent_caps("bob") == {"steps": 200, "cost_usd": None}
    assert o.run_caps() == {"cost_usd": 10.0, "wall_s": 600.0}
    assert parse(" , ").empty


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("steps=0", "steps: Input should be greater than or equal to 1"),
        ("run.cost_usd=-1", "run.cost_usd: Input should be greater than 0"),
        ("bob.steps=1.5", "bob.steps: Input should be a valid integer"),
        ("warn_at=0.5", "warn_at: no cap 'warn_at'; use steps, output_tokens, cost_usd, wall_s"),
        ("run.monitor_cost_usd=1", "no cap 'monitor_cost_usd'"),
        ("steps", "write key=value"),
        ("steps=1,steps=2", "steps is given twice"),
        ("Bob.steps=3", "'Bob' is not a valid name"),
    ],
)
def test_a_bad_budget_says_which_cap(text: str, problem: str) -> None:
    with pytest.raises(BudgetError) as e:
        parse(text)
    assert problem in str(e.value)


def test_a_lifted_cap_survives_the_job_json_and_a_kept_one_stays_out() -> None:
    o = parse("cost_usd=none, bob.steps=50")
    job = JobOptions(budget=o)
    back = JobOptions.model_validate_json(job.model_dump_json())
    assert back.budget is not None
    assert back.budget.agent_caps("alice") == {"cost_usd": None}
    assert back.budget.agent_caps("bob") == {"cost_usd": None, "steps": 50}
    assert job.model_dump(mode="json")["budget"] == {
        "every": {"cost_usd": None},
        "agents": {"bob": {"steps": 50}},
        "run": None,
    }
    assert o.dump() == {"every": {"cost_usd": None}, "agents": {"bob": {"steps": 50}}}
    assert load(o.dump()) == o
    assert load({"every": {"steps": 0}}) is None
    assert load(None) is None


def test_the_json_the_estimate_endpoints_take_reads_like_the_flag() -> None:
    o = read_json('{"every": {"steps": 9}, "run": {"cost_usd": null}}')
    assert o is not None
    assert (o.agent_caps("x"), o.run_caps()) == ({"steps": 9}, {"cost_usd": None})
    assert read_json("{}") is None
    with pytest.raises(BudgetError, match=r"^steps: Input should be greater"):
        read_json('{"every": {"steps": 0}}')


def test_a_fork_s_caps_go_over_its_origin_s_per_agent() -> None:
    origin = parse("alice.steps=200, run.cost_usd=10")
    fork = parse("steps=50, cost_usd=2")
    merged = over(origin, fork, ["alice", "bob"])
    assert merged is not None
    assert merged.agent_caps("alice") == {"steps": 50, "cost_usd": 2.0}
    assert merged.agent_caps("bob") == {"steps": 50, "cost_usd": 2.0}
    assert merged.run_caps() == {"cost_usd": 10.0}
    assert over(origin, None, ["alice", "bob"]) is origin
    assert over(None, fork, ["alice"]) is fork
    assert over(None, BudgetOverride(), ["alice"]) is None


def test_the_override_replaces_the_caps_it_names_and_keeps_the_rest() -> None:
    scenario = parse_scenario(SCENARIO)
    changed = apply(scenario, parse("cost_usd=none, alice.steps=5, run.steps=60"))
    alice, bob = changed.agents
    assert (alice.budget.steps, alice.budget.cost_usd, alice.budget.wall_s) == (5, None, 1800)
    assert (bob.budget.steps, bob.budget.cost_usd) == (30, None)
    assert (changed.limits.steps, changed.limits.cost_usd) == (60, 8)
    assert scenario.agents[0].budget.steps == 80
    assert apply(scenario, None) is scenario
    with pytest.raises(BudgetError, match="no agent carol in the scenario; its agents are alice"):
        apply(scenario, parse("carol.steps=3"))


def test_the_changes_are_said_against_the_recorded_scenario() -> None:
    resolved = parse_scenario(SCENARIO).model_dump(mode="json")
    o = parse("alice.steps=200, bob.steps=30, alice.wall_s=3600, run.cost_usd=none")
    found = changes(resolved, o)
    assert [(c.scope, c.agent, c.key, c.value, c.scenario) for c in found] == [
        ("agent", "alice", "steps", 200, 80),
        ("agent", "alice", "wall_s", 3600, 1800),
        ("run", None, "cost_usd", None, 8),
    ]
    assert describe(found, agents=2) == (
        "Budget: alice steps 200 (scenario 80), alice wall 1h (scenario 30m),"
        " run cost no cap (scenario $8)"
    )
    assert describe([], agents=2) is None
    one = changes(resolved | {"agents": resolved["agents"][:1]}, parse("cost_usd=2.5"))
    assert describe(one) == "Budget: cost $2.5 (scenario $4)"
    assert changes(resolved, None) == []


def test_a_recorded_scenario_takes_the_override_for_the_estimate_history() -> None:
    resolved = parse_scenario(SCENARIO).model_dump(mode="json")
    out = apply_resolved(resolved, parse("steps=7, run.steps=9"))
    assert [a["budget"]["steps"] for a in out["agents"]] == [7, 7]
    assert out["agents"][0]["budget"]["cost_usd"] == 4
    assert (out["limits"]["steps"], out["limits"]["cost_usd"]) == (9, 8)
    assert resolved["agents"][0]["budget"]["steps"] == 80


def test_a_fork_s_step_cap_must_leave_it_a_live_step() -> None:
    scenario = parse_scenario(SCENARIO)
    origin = Origin("o", Bundle(scenario, SCENARIO.encode()), {}, {}, budget=parse("bob.steps=40"))
    replayed = {"alice": 30, "bob": 35}
    origin.check_budget(None, replayed)
    origin.check_budget(parse("alice.steps=31, cost_usd=1"), replayed)
    with pytest.raises(BudgetError, match="agent alice replays 30 steps, so a cap of 30 steps"):
        origin.check_budget(parse("alice.steps=30"), replayed)
    with pytest.raises(BudgetError, match="agent bob replays 35 steps, so a cap of 20"):
        origin.check_budget(parse("bob.steps=20"), replayed)
    with pytest.raises(BudgetError, match="the agents replay 65 steps, so a run cap of 65"):
        origin.check_budget(parse("run.steps=65"), replayed)
    origin.check_budget(parse("run.steps=66"), replayed)
