from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from aims.events import Event, ModelRequest, MonitorFlag, ToolResult
from aims.events.schema import Payload
from aims.monitor.config import MAX_HOLD_S, CategoryPolicy, PolicyConfig, merge
from aims.monitor.policy import Cutoffs, Policy
from aims.monitor.setup import ResolvedMonitor
from aims.monitor.texts import resolve
from aims.prompts import Prompts

HASH = "0" * 64


def event(seq: int, payload: Payload, agent: str | None = "a") -> Event:
    return Event(
        v=1,
        run_id="r",
        seq=seq,
        ts=datetime.now(UTC),
        agent_id=agent,
        type=payload.type,
        data=payload.model_dump(mode="json"),
        prev_hash=HASH,
        hash=HASH,
    )


def flag(seq: int, importance: int, agent: str | None = "a", **kw: Any) -> Event:
    fields: dict[str, Any] = {
        "layer": "signal",
        "source": "sig",
        "category": "scorer_tampering",
        "evidence": [max(seq - 1, 0)],
        "justification": "j",
    }
    return event(seq, MonitorFlag(importance=importance, **(fields | kw)), agent)


def request(seq: int, agent: str = "a", attempt: int = 1, role: Any = None) -> Event:
    body = {"sha256": HASH, "size": 1}
    payload = ModelRequest.model_validate(
        {
            "call_id": f"c{seq}",
            "attempt": attempt,
            "profile": "p",
            "backend": "chat",
            "model": "m",
            "url": "http://x",
            "params": {"stream": False},
            "body": body,
            "key": HASH,
            "role": role,
        }
    )
    return event(seq, payload, agent)


def denied(seq: int, yes: bool = True, agent: str = "a") -> Event:
    return event(seq, ToolResult(call_id=f"t{seq}", output="no", denied=yes, error=yes), agent)


def policy(mode: Any = "autonomous", agents: tuple[str, ...] = ("a", "b"), **kw: Any) -> Policy:
    return Policy(PolicyConfig.model_validate(kw), mode, agents)


def test_default_cutoffs() -> None:
    p = policy()
    assert p.decide([flag(1, 3)]) == []
    [d] = p.decide([flag(2, 4)])
    assert (d.action, d.agents, d.flags) == ("pause", ("a",), (2,))
    assert "scorer_tampering at importance 4" in d.reason


def test_signal_at_kill_cutoff_kills() -> None:
    [d] = policy().decide([flag(1, 4), flag(2, 5)])
    assert (d.action, d.flags) == ("kill", (2,))
    assert "signal sig" in d.reason


def test_sweep_at_kill_cutoff_alone_pauses() -> None:
    [d] = policy().decide([flag(5, 5, layer="sweep", source="main", lane="full")])
    assert d.action == "pause"


def test_two_lanes_on_one_step_kill() -> None:
    p = policy()
    for seq in (1, 10, 20):
        p.observe(request(seq))
    p.observe(request(12, attempt=2))
    p.observe(request(15, role="sweep"))
    assert p.step("a", 9) == 1
    assert p.step("a", 12) == 2
    assert p.step("a", 25) == 3
    assert p.step_start("a", 15) == 10
    assert p.step_start("a", 0) is None
    full = flag(30, 5, layer="sweep", source="main", lane="full", evidence=[11])
    assert p.decide([full])[0].action == "pause"
    other_step = flag(31, 5, layer="sweep", source="main", lane="evidence", evidence=[21])
    assert p.decide([other_step])[0].action == "pause"
    same_lane = flag(32, 5, layer="sweep", source="main", lane="full", evidence=[12])
    assert p.decide([same_lane])[0].action == "pause"
    [d] = p.decide([flag(33, 5, layer="sweep", source="main", lane="evidence", evidence=[14])])
    assert (d.action, d.flags) == ("kill", (33,))
    assert "main/evidence, main/full" in d.reason
    assert "step 2" in d.reason


def test_two_lanes_in_one_batch_kill() -> None:
    p = policy()
    p.observe(request(1))
    a = flag(5, 5, layer="sweep", source="main", lane="full", evidence=[2])
    b = flag(6, 5, layer="sweep", source="main", lane="evidence", evidence=[3])
    [d] = p.decide([a, b])
    assert (d.action, d.flags) == ("kill", (6,))


def test_two_lanes_in_different_categories_kill() -> None:
    p = policy(categories={"drift": CategoryPolicy(kill=4)})
    p.observe(request(1))
    a = flag(5, 4, layer="sweep", source="main", lane="full", category="drift", evidence=[2])
    assert p.decide([a])[0].action == "pause"
    b = flag(6, 5, layer="sweep", source="main", lane="evidence", evidence=[3])
    [d] = p.decide([b])
    assert (d.action, d.flags) == ("kill", (6,))
    assert "main/evidence, main/full" in d.reason


def test_alerting_operator_never_acts() -> None:
    assert policy().decide([flag(1, 5, category="alerting_operator")]) == []


def test_category_overrides_and_scope() -> None:
    p = policy(
        categories={
            "egress_attempt": CategoryPolicy(pause=2, kill=None, scope="run"),
            "drift": CategoryPolicy(pause=None, kill=None),
            "evading_checks": CategoryPolicy(pause=None, kill=4),
        }
    )
    [d] = p.decide([flag(1, 2, category="egress_attempt")])
    assert (d.action, d.agents) == ("pause", ("a", "b"))
    assert p.decide([flag(2, 5, category="egress_attempt")])[0].action == "pause"
    assert p.decide([flag(3, 5, category="drift")]) == []
    assert p.decide([flag(4, 4, category="evading_checks")])[0].action == "kill"
    swept = flag(5, 4, category="evading_checks", layer="sweep", source="s", lane="x")
    assert p.decide([swept])[0].action == "pause"
    assert p.cutoffs("evading_checks") == Cutoffs(4, 4)
    assert p.cutoffs_of([flag(6, 1, category="drift"), flag(7, 1)]) == Cutoffs(4, 5)


def test_run_level_flag_acts_on_every_agent() -> None:
    [d] = policy().decide([flag(1, 4, agent=None)])
    assert d.agents == ("a", "b")


def test_judge_setup_and_final_flags_never_act() -> None:
    p = policy()
    assert p.decide([flag(1, 5, layer="judge", source="judge")]) == []
    assert p.decide([flag(2, 5, phase="setup")]) == []
    assert p.decide([flag(3, 5, final=True, count=12)]) == []


def test_verifier_flags_taint_and_never_act() -> None:
    p = policy()
    assert p.decide([flag(4, 5, phase="verifier"), flag(5, 5, phase="verifier", agent="b")]) == []
    assert p.decide([flag(6, 2, phase="verifier", agent=None)]) == []
    assert p.tainted("a") == [4, 6]
    assert p.tainted("b") == [5, 6]


def test_breaker_in_a_row() -> None:
    p = policy()
    assert p.observe(denied(1)) is None
    assert p.observe(denied(2)) is None
    assert p.observe(denied(3, yes=False)) is None
    assert p.observe(denied(4)) is None
    assert p.observe(denied(5)) is None
    d = p.observe(denied(6))
    assert d is not None
    assert (d.action, d.agents) == ("kill", ("a",))
    assert d.reason == "3 denied tool calls in a row"
    assert p.observe(denied(7)) is None


def test_breaker_total_and_human_pauses() -> None:
    p = policy("human", deny_consecutive=5, deny_total=4)
    decisions = []
    for seq in range(1, 9):
        decisions.append(p.observe(denied(seq, yes=seq % 2 == 1)))
    tripped = [d for d in decisions if d is not None]
    assert len(tripped) == 1
    assert (tripped[0].action, tripped[0].reason) == ("pause", "4 denied tool calls in all")
    assert decisions.index(tripped[0]) == 6


def test_breaker_total_coinciding_with_in_a_row() -> None:
    p = policy(deny_consecutive=3, deny_total=6)
    decisions = [p.observe(denied(seq, yes=seq not in (10, 12))) for seq in range(1, 14)]
    tripped = {i + 1: d.reason for i, d in enumerate(decisions) if d is not None}
    assert tripped == {
        3: "3 denied tool calls in a row",
        6: "3 denied tool calls in a row",
        9: "3 denied tool calls in a row",
    }


def test_kill_keeps_a_pause_for_another_agent() -> None:
    decisions = policy().decide([flag(1, 5, agent="a"), flag(2, 4, agent="b")])
    assert [(d.action, d.agents, d.flags) for d in decisions] == [
        ("kill", ("a",), (1,)),
        ("pause", ("b",), (2,)),
    ]
    assert decisions[1].reason == policy().decide([flag(2, 4, agent="b")])[0].reason


def test_kill_and_pause_on_one_agent_kill_once() -> None:
    [d] = policy().decide([flag(1, 4, agent="a"), flag(2, 5, agent="a")])
    assert (d.action, d.agents, d.flags) == ("kill", ("a",), (2,))


def test_kill_drops_only_the_pause_of_the_killed_agent() -> None:
    flags = [flag(1, 4, agent="a"), flag(2, 5, agent="a"), flag(3, 4, agent="b")]
    decisions = policy(agents=("a", "b", "c")).decide(flags)
    assert [(d.action, d.agents, d.flags) for d in decisions] == [
        ("kill", ("a",), (2,)),
        ("pause", ("b",), (3,)),
    ]


def test_kill_leaves_the_rest_of_a_run_scoped_pause() -> None:
    p = policy(
        agents=("a", "b", "c"),
        categories={"egress_attempt": CategoryPolicy(pause=2, kill=None, scope="run")},
    )
    decisions = p.decide([flag(1, 5, agent="b"), flag(2, 3, category="egress_attempt")])
    assert [(d.action, d.agents, d.flags) for d in decisions] == [
        ("kill", ("b",), (1,)),
        ("pause", ("a", "c"), (2,)),
    ]


def test_run_level_kill_leaves_no_pause() -> None:
    [d] = policy().decide([flag(1, 5, agent=None), flag(2, 4, agent="b")])
    assert (d.action, d.agents, d.flags) == ("kill", ("a", "b"), (1,))


def test_uncorroborated_kill_level_flag_of_another_agent_pauses() -> None:
    p = policy()
    p.observe(request(1, agent="b"))
    swept = flag(5, 5, agent="b", layer="sweep", source="main", lane="full", evidence=[2])
    decisions = p.decide([flag(4, 5, agent="a"), swept])
    assert [(d.action, d.agents, d.flags) for d in decisions] == [
        ("kill", ("a",), (4,)),
        ("pause", ("b",), (5,)),
    ]


def test_observe_mode_mirrors_autonomous() -> None:
    p = policy("observe")
    [d] = p.decide([flag(1, 5)])
    assert d.action == "kill"
    for seq in (2, 3):
        p.observe(denied(seq))
    d2 = p.observe(denied(4))
    assert d2 is not None
    assert d2.action == "kill"


@pytest.mark.parametrize("name", ["human_timeout_s", "pause_timeout_s"])
def test_a_pause_holds_at_most_thirty_days(name: str) -> None:
    assert MAX_HOLD_S == 30 * 86_400
    assert getattr(PolicyConfig.model_validate({name: MAX_HOLD_S}), name) == MAX_HOLD_S
    for value in (MAX_HOLD_S + 1, 1e12, float("inf")):
        with pytest.raises(ValidationError):
            PolicyConfig.model_validate({name: value})


@pytest.mark.parametrize("value", [60 * 86_400, 1e12, float("inf")])
def test_a_snapshot_stored_with_a_longer_hold_restores_at_thirty_days(value: float) -> None:
    resolved = ResolvedMonitor(
        config=merge({"preset": "signals"}),
        texts=resolve(Prompts()),
        oversight="human",
        budget_usd=0.0,
        identity="id",
    )
    snapshot = resolved.snapshot()
    config = snapshot["config"]
    assert isinstance(config, dict)
    policy = config["policy"]
    assert isinstance(policy, dict)
    policy["pause_timeout_s"] = policy["human_timeout_s"] = value
    again = ResolvedMonitor.restore(snapshot)
    assert again.config.policy.pause_timeout_s == again.config.policy.human_timeout_s == MAX_HOLD_S
    assert again.identity == "id"
    assert again.config.model_copy(update={"policy": resolved.config.policy}) == resolved.config
