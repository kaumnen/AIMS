from datetime import UTC, datetime
from typing import Any

from aims.events import Event, ModelRequest, MonitorFlag, ToolResult
from aims.events.schema import Payload
from aims.monitor.config import CategoryPolicy, PolicyConfig
from aims.monitor.policy import Cutoffs, Policy

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


def test_observe_mode_mirrors_autonomous() -> None:
    p = policy("observe")
    [d] = p.decide([flag(1, 5)])
    assert d.action == "kill"
    for seq in (2, 3):
        p.observe(denied(seq))
    d2 = p.observe(denied(4))
    assert d2 is not None
    assert d2.action == "kill"
