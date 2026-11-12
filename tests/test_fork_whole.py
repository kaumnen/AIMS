"""A fork replays only whole steps: steps whose every tool call has a result in the log. A run
that ends at a gate, mid-call or in a crash leaves its last step without them, so a fork refuses
that step, names the last one it can replay and cuts there by default."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, call, calls, models, profile
from test_agent import FakeSandbox, Gate, echo, payloads

from aims.agent import Agent, ReplayPlan, RunBudget, Verdict
from aims.events import (
    AgentFinished,
    AgentStep,
    Draft,
    LocalEventStore,
    ModelUsage,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.models import ModelClient
from aims.prompts import AgentTexts, Prompts
from aims.run.fork import (
    CutAt,
    Origin,
    check_fork,
    cut_at,
    fork_step,
    load_origin,
    planner,
    preview,
    replayable_steps,
)
from aims.run.runner import Bench, Fork, RunError, resolve_texts
from aims.sandbox import AuditedSandbox
from aims.sandbox.audit import ExecResult
from aims.scenario import AgentConfig, Limits, SandboxConfig, parse
from aims.scenario.load import Bundle
from aims.tools import load_tools

pytestmark = pytest.mark.anyio

ONE = """\
version: 1
name: t
oversight: observe
task:
  instruction: do it
  environment: {image: python:3.13-slim}
agents:
  - id: agent
"""

PAIR = ONE.replace("  - id: agent\n", "  - id: a\n  - id: b\n")

ENDED = "cannot be replayed: the run ended before its tool calls returned"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def started(store: LocalEventStore, text: str) -> str:
    """A run of the scenario `text` as `aims run` starts it, before its agents act."""
    bundle = Bundle(scenario=parse(text), text=text.encode())
    p = profile().snapshot()
    agents: dict[str, Any] = {
        a.id: {
            "profile": p,
            "schedule": [[None, p]],
            "texts": resolve_texts(Prompts(), a).snapshot(),
        }
        for a in bundle.scenario.agents
    }
    return await store.create_run(
        RunStarted(
            scenario="t",
            oversight="observe",
            agents=list(agents),
            config={"scenario": bundle.snapshot(), "agents": agents},
        ),
        blobs=bundle.blobs(),
    )


def agent(
    store: LocalEventStore,
    run_id: str,
    script: Script,
    *,
    gate: Any = None,
    replay: ReplayPlan | None = None,
    sandbox: FakeSandbox | None = None,
) -> Agent:
    p = profile()
    return Agent(
        config=AgentConfig(),
        sandbox_config=SandboxConfig(),
        instruction="do the task",
        texts=AgentTexts(),
        profile=p,
        client=ModelClient(store, run_id, models(p), transport=script.transport(), env={}),
        sandbox=cast(AuditedSandbox, sandbox or FakeSandbox(echo)),
        store=store,
        run_id=run_id,
        tools=load_tools(AgentConfig().tool_names),
        run_budget=RunBudget(Limits()),
        gate=gate,
        replay=replay,
    )


async def origin_run(store: LocalEventStore, script: Script, gate: Gate | None = None) -> str:
    """A run of one agent that follows `script` until it submits or `gate` kills it."""
    run_id = await started(store, ONE)
    finished = await agent(store, run_id, script, gate=gate).run()
    status = "killed" if finished.reason == "killed" else "completed"
    await store.append(run_id, [Draft(RunFinished(status=status))])
    return run_id


async def fork_live(store: LocalEventStore, origin: Origin, step: int, live: Script) -> str:
    """Fork `origin` after `step` with the plan the bench builds; `live` answers after it."""
    fork = Fork(origin=origin.run_id, step=step)
    bench = Bench(store, cast(Any, None), models(profile()))
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = (await planner(bench, origin, fork)(run_id))["agent"]
    finished = await agent(store, run_id, live, replay=plan).run()
    assert (finished.reason, finished.answer) == ("submitted", "forked"), finished.detail
    return run_id


@pytest.mark.parametrize(
    ("kill", "second"),
    [
        (("after_reply", 2), [call("bash", command="rm -rf /")]),
        (("before_tool", 2), [call("bash", command="rm -rf /")]),
        # the step's first call ran, its second never did
        (("after_tool", 2), [call("bash", command="pwd"), call("bash", command="rm -rf /")]),
    ],
)
async def test_a_fork_of_a_killed_run_cuts_before_the_step_it_was_killed_in(
    store: LocalEventStore, kill: tuple[str, int], second: list[dict[str, Any]]
) -> None:
    gate = Gate({kill: Verdict("kill", reason="policy")})
    run_id = await origin_run(store, Script(call("bash", command="ls"), calls(*second)), gate)
    origin = await load_origin(store, run_id)
    assert (origin.steps, origin.replayable) == ({"agent": 2}, {"agent": 1})

    with pytest.raises(RunError) as refused:
        check_fork(origin, 2)
    assert str(refused.value) == (
        f"step 2 of agent agent {ENDED}, so they have no recorded output; fork at step 1 or before"
    )
    assert fork_step(origin) == 1
    check_fork(origin, 1)

    # the killed step is made again live: its model call, then its tool calls
    live = Script(call("bash", command="echo again"), call("submit", answer="forked"))
    fork_id = await fork_live(store, origin, fork_step(origin), live)
    assert len(live.requests) == 2
    results = await payloads(store, fork_id, ToolResult)
    assert [r.replayed for r in results] == [True, False, False]
    steps = await payloads(store, fork_id, AgentStep)
    assert [s.source for s in steps] == ["replay", "live", "live"]


async def test_a_step_whose_calls_all_returned_replays_though_the_run_was_killed(
    store: LocalEventStore,
) -> None:
    script = Script(call("bash", command="ls"), call("bash", command="pwd"))
    gate = Gate({("after_tool", 2): Verdict("kill", reason="policy")})
    origin = await load_origin(store, await origin_run(store, script, gate))
    assert (origin.steps, origin.replayable) == ({"agent": 2}, {"agent": 2})
    assert fork_step(origin) == 2
    check_fork(origin, 2)
    live = Script(call("submit", answer="forked"))
    fork_id = await fork_live(store, origin, 2, live)
    results = await payloads(store, fork_id, ToolResult)
    assert [r.replayed for r in results] == [True, True, False]


async def test_a_run_killed_in_its_first_step_forks_at_step_0(store: LocalEventStore) -> None:
    gate = Gate({("before_tool", 1): Verdict("kill", reason="policy")})
    origin = await load_origin(store, await origin_run(store, Script(call("bash")), gate))
    assert (origin.steps, origin.replayable) == ({"agent": 1}, {"agent": 0})
    with pytest.raises(RunError) as refused:
        check_fork(origin, 1)
    assert str(refused.value).endswith("; fork at step 0 to make every step again live")
    assert fork_step(origin) == 0
    check_fork(origin, 0)
    live = Script(call("submit", answer="forked"))
    fork_id = await fork_live(store, origin, 0, live)
    assert len(live.requests) == 1
    assert [r.replayed for r in await payloads(store, fork_id, ToolResult)] == [False]


async def test_a_run_cancelled_mid_call_forks_before_that_step(store: LocalEventStore) -> None:
    hung = asyncio.Event()

    class Hang(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            if command == "sleep 30":
                hung.set()
                await asyncio.sleep(30)
            return await super().exec(command, **kw)

    run_id = await started(store, ONE)
    script = Script(call("bash", command="ls"), call("bash", command="sleep 30"))
    task = asyncio.create_task(agent(store, run_id, script, sandbox=Hang(echo)).run())
    await asyncio.wait_for(hung.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.append(run_id, [Draft(RunFinished(status="cancelled"))])
    origin = await load_origin(store, run_id)
    assert (origin.steps, origin.replayable, fork_step(origin)) == ({"agent": 2}, {"agent": 1}, 1)


async def test_a_run_still_in_the_step_says_its_calls_have_not_returned(
    store: LocalEventStore,
) -> None:
    run_id = await started(store, ONE)
    await store.append(
        run_id,
        [
            Draft(ToolCall(call_id="t1", name="bash", arguments="{}", model_call="m1"), "agent"),
            Draft(AgentStep(n=1, call_id="m1", status="completed", tool_calls=["t1"]), "agent"),
        ],
    )
    origin = await load_origin(store, run_id)
    assert origin.running
    with pytest.raises(RunError) as refused:
        check_fork(origin, 1)
    assert str(refused.value) == (
        "step 1 of agent agent cannot be replayed yet: its tool calls have not returned, so they"
        " have no recorded output; fork at step 0 to make every step again live"
    )


def step(agent: str, n: int, *, done: bool = True) -> list[Draft]:
    """Step `n` of `agent` with one tool call, which returned when `done`."""
    call_id, model = f"{agent}{n}", f"m{agent}{n}"
    made = [
        Draft(ToolCall(call_id=call_id, name="bash", arguments="{}", model_call=model), agent),
        Draft(AgentStep(n=n, call_id=model, status="completed", tool_calls=[call_id]), agent),
    ]
    return [*made, Draft(ToolResult(call_id=call_id, output="ok"), agent)] if done else made


async def test_a_cut_names_a_whole_step_and_others_mid_step_go_live_from_it(
    store: LocalEventStore,
) -> None:
    """Agent b was in its step 2 when agent a took its step 2, and the run was killed in a's step
    3 before either returned: a cut at a:2 replays b's one whole step and b makes step 2 again."""
    run_id = await started(store, PAIR)
    await store.append(
        run_id,
        [
            *step("a", 1),
            *step("b", 1),
            *step("b", 2, done=False),
            *step("a", 2),
            *step("a", 3, done=False),
            Draft(RunFinished(status="killed")),
        ],
    )
    origin = await load_origin(store, run_id)
    assert (origin.steps, origin.replayable) == ({"a": 3, "b": 2}, {"a": 2, "b": 1})

    with pytest.raises(RunError) as refused:
        cut_at(origin, "a:3")
    assert str(refused.value) == (
        f"step 3 of agent a {ENDED}, so they have no recorded output; fork at a:2 or before"
    )
    with pytest.raises(RunError) as refused:
        cut_at(origin, "b:2")
    assert str(refused.value) == (
        f"step 2 of agent b {ENDED}, so they have no recorded output; fork at b:1 or before"
    )

    at = cut_at(origin, "a:2")
    assert isinstance(at, CutAt)
    assert at.steps == {"a": 2, "b": 1}
    assert at.seq == origin.step_seqs["a"][1]
    assert origin.step_seqs["b"][1] < at.seq
    end = cut_at(origin, f"@{origin.last_seq}")
    assert isinstance(end, CutAt)
    assert end.steps == {"a": 2, "b": 1}
    first = cut_at(origin, "b:1")
    assert isinstance(first, CutAt)
    assert first.steps == {"a": 1, "b": 1}
    lines = preview(origin, Fork(origin=run_id, step=1, steps=at.steps, cut=at.seq, agent="a"))
    assert "  b: replays 1 of 2 steps, goes live after" in lines


async def test_an_agent_that_ended_mid_step_in_a_running_run_says_it_ended(
    store: LocalEventStore,
) -> None:
    """Agent a was killed in its step 2 before its tool call returned, and b goes on: the step
    will never be whole, so the refusal says a ended rather than that it is not yet."""
    run_id = await started(store, PAIR)
    end = AgentFinished(
        reason="killed", steps=2, usage=ModelUsage(), cost_usd=0, wall_ms=1, paused_ms=0
    )
    await store.append(
        run_id, [*step("a", 1), *step("a", 2, done=False), Draft(end, "a"), *step("b", 1)]
    )
    origin = await load_origin(store, run_id)
    assert (origin.running, origin.ended, origin.replayable) == (True, {"a"}, {"a": 1, "b": 1})
    with pytest.raises(RunError) as refused:
        cut_at(origin, "a:2")
    assert str(refused.value) == (
        "step 2 of agent a cannot be replayed: the agent ended before its tool calls returned,"
        " so they have no recorded output; fork at a:1 or before"
    )

    await store.append(run_id, [*step("b", 2, done=False)])
    with pytest.raises(RunError) as refused:
        cut_at(await load_origin(store, run_id), "b:2")
    assert "step 2 of agent b cannot be replayed yet: its tool calls have not" in str(refused.value)


async def test_a_step_without_tool_calls_is_whole(store: LocalEventStore) -> None:
    run_id = await started(store, ONE)
    await store.append(
        run_id,
        [
            *step("agent", 1),
            Draft(AgentStep(n=2, call_id="m2", status="incomplete"), "agent"),
        ],
    )
    assert await replayable_steps(store, run_id, ["agent", "nobody"]) == {"agent": 2, "nobody": 0}
