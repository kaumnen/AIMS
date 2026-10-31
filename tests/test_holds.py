from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from cp import CP, control_plane

from aims.events import (
    AgentFinished,
    AgentHeld,
    Draft,
    HoldInfo,
    LocalEventStore,
    OperatorAction,
    RunFinished,
    RunStarted,
    split_lines,
)
from aims.events.payloads import MonitorAction
from aims.events.store import LOG

pytestmark = pytest.mark.anyio

POLICY = {"human_timeout_s": 60, "pause_timeout_s": 30}
STARTED = RunStarted(
    scenario="demo",
    oversight="human",
    agents=["a1", "a2", "a3"],
    config={"monitor": {"config": {"policy": POLICY}}},
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path / "cp") as cp:
        yield cp


def monitor(action: str, *agents: str, simulated: bool = False, mode: str = "human") -> Draft:
    payload = MonitorAction.model_validate(
        {
            "action": action,
            "agents": list(agents),
            "flags": [1],
            "mode": mode,
            "simulated": simulated,
            "reason": "flag 1",
        }
    )
    return Draft(payload, agents[0] if len(agents) == 1 else None)


def operator(action: str, agent: str | None = None, **fields: str) -> Draft:
    return Draft(OperatorAction.model_validate({"action": action, **fields}), agent)


def held(agent: str, kind: str = "before_tool", call: str = "c1") -> Draft:
    return Draft(AgentHeld.model_validate({"kind": kind, "call_id": call, "step": 1}), agent)


async def holds(store: LocalEventStore, run_id: str) -> list[tuple[str, str]]:
    info = await store.run(run_id)
    assert info is not None
    return [(h.agent, h.by) for h in info.held]


async def test_a_monitor_pause_holds_until_its_resume(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1"), held("a1")])
    info = await store.run(run_id)
    assert info is not None
    (hold,) = info.held
    assert (hold.agent, hold.by, hold.reason, hold.flags, hold.mode) == (
        "a1",
        "monitor",
        "flag 1",
        [1],
        "human",
    )
    assert (hold.at, hold.call, hold.seq) == ("before_tool", "c1", 1)
    assert hold.until is not None
    assert hold.until - hold.since == timedelta(seconds=60)

    await store.append(run_id, [monitor("resume", "a1")])
    assert await holds(store, run_id) == []


async def test_an_autonomous_pause_runs_to_the_pause_timeout(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1", mode="autonomous")])
    info = await store.run(run_id)
    assert info is not None
    (hold,) = info.held
    assert hold.until is not None
    assert hold.until - hold.since == timedelta(seconds=30)
    assert hold.at is None


async def test_a_simulated_pause_holds_nothing(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1", simulated=True, mode="observe")])
    assert await holds(store, run_id) == []


async def test_a_monitor_resume_leaves_an_operator_pause(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(
        run_id,
        [monitor("pause", "a1", "a2"), operator("pause", "a1", reason="mine"), held("a1")],
    )
    assert await holds(store, run_id) == [("a1", "monitor"), ("a2", "monitor"), ("a1", "operator")]

    await store.append(run_id, [monitor("resume", "a1", "a2")])
    info = await store.run(run_id)
    assert info is not None
    (hold,) = info.held
    assert (hold.agent, hold.by, hold.reason, hold.at) == ("a1", "operator", "mine", "before_tool")

    await store.append(run_id, [operator("resume", "a1", then="continue")])
    assert await holds(store, run_id) == []


async def test_an_operator_resume_ends_the_monitors_pause_too(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1"), operator("resume", "a1", then="deny")])
    assert await holds(store, run_id) == []


async def test_a_pause_of_every_agent_skips_the_finished_ones(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [Draft(AgentFinished(reason="submitted"), "a3"), operator("pause")])
    assert await holds(store, run_id) == [("a1", "operator"), ("a2", "operator")]

    await store.append(run_id, [Draft(AgentFinished(reason="submitted"), "a2")])
    assert await holds(store, run_id) == [("a1", "operator")]

    await store.append(run_id, [operator("pause", "a3")])
    assert await holds(store, run_id) == [("a1", "operator")]

    await store.append(run_id, [operator("resume", then="continue")])
    assert await holds(store, run_id) == []


@pytest.mark.parametrize(
    "end",
    [
        monitor("kill", "a1"),
        operator("kill"),
        operator("cancel"),
        operator("resume", "a1", then="kill"),
        Draft(RunFinished(status="killed")),
    ],
)
async def test_a_kill_or_the_runs_end_lets_every_hold_go(
    store: LocalEventStore, end: Draft
) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1"), operator("pause", "a2")])
    await store.append(run_id, [end])
    assert await holds(store, run_id) == []


async def test_a_hold_reads_the_same_in_any_batching_and_after_a_rebuild(
    tmp_path: Path,
) -> None:
    story = [
        monitor("pause", "a1"),
        held("a1", "after_reply", "m1"),
        operator("pause", "a2"),
        monitor("resume", "a1"),
        operator("pause"),
        held("a2"),
    ]
    seen: list[list[HoldInfo]] = []
    for batch in (1, len(story)):
        async with await LocalEventStore.open(tmp_path / f"b{batch}") as store:
            run_id = await store.create_run(STARTED)
            for i in range(0, len(story), batch):
                await store.append(run_id, story[i : i + batch])
            info = await store.run(run_id)
            assert info is not None
            seen.append(info.held)
            await store.rebuild()
            again = await store.run(run_id)
            assert again is not None
            assert again.held == info.held
    one, all_at_once = seen
    assert [(h.agent, h.by, h.at) for h in one] == [
        ("a2", "operator", "before_tool"),
        ("a1", "operator", None),
        ("a3", "operator", None),
    ]
    assert [(h.agent, h.by, h.seq, h.at, h.call) for h in one] == [
        (h.agent, h.by, h.seq, h.at, h.call) for h in all_at_once
    ]


async def test_the_run_summary_lists_its_holds(cp: CP, store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [monitor("pause", "a1"), held("a1")])
    await cp.state.store.ingest(run_id, split_lines((store.root / run_id / LOG).read_bytes()))
    async with cp.client(cp.viewer) as c:
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        (listed,) = (await c.get("/api/runs")).json()["items"]
    (hold,) = run["held"]
    assert listed["held"] == run["held"]
    assert {k: hold[k] for k in ("agent", "by", "seq", "flags", "mode", "at", "call")} == {
        "agent": "a1",
        "by": "monitor",
        "seq": 1,
        "flags": [1],
        "mode": "human",
        "at": "before_tool",
        "call": "c1",
    }
    assert hold["since"].endswith("Z")
    assert hold["until"] > hold["since"]
