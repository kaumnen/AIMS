"""A fork replays only whole steps: steps whose every tool call has a result in the log. A run
that ends at a gate, mid-call or in a crash leaves its last step without them, so a fork refuses
that step, names the last one it can replay and cuts there by default."""

import asyncio
import itertools
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, call, calls, models, profile
from test_agent import BILLED, FULL, FakeSandbox, Gate, echo, payloads, retrying

from aims.agent import Agent, ReplayPlan, RunBudget, Totals, Verdict
from aims.events import (
    AgentFinished,
    AgentInput,
    AgentStep,
    Draft,
    LocalEventStore,
    ModelUsage,
    ReplayDivergence,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.models import ModelClient, Profile, Reply
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
from aims.scenario import AgentConfig, Limits, SandboxConfig, ScenarioError, parse
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


async def started(store: LocalEventStore, text: str, on: Profile | None = None) -> str:
    """A run of the scenario `text` as `aims run` starts it on `on`, before its agents act."""
    bundle = Bundle(scenario=parse(text), text=text.encode())
    p = (on or profile()).snapshot()
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
    config: AgentConfig | None = None,
    p: Profile | None = None,
) -> Agent:
    p = p or profile()
    config = config or AgentConfig()
    return Agent(
        config=config,
        sandbox_config=SandboxConfig(),
        instruction="do the task",
        texts=AgentTexts(),
        profile=p,
        client=ModelClient(store, run_id, models(p), transport=script.transport(), env={}),
        sandbox=cast(AuditedSandbox, sandbox or FakeSandbox(echo)),
        store=store,
        run_id=run_id,
        tools=load_tools(config.tool_names),
        run_budget=RunBudget(Limits()),
        gate=gate,
        replay=replay,
    )


async def test_a_run_recorded_before_its_texts_were_kept_loads_as_an_origin(
    store: LocalEventStore,
) -> None:
    """The first runs kept a `description` the scenario has since dropped, and each agent's
    system prompt in place of its texts."""
    old = ONE.replace("name: t\n", "name: t\ndescription: An early run.\n")
    with pytest.raises(ScenarioError, match="description: Extra inputs are not permitted"):
        parse(old)
    bundle = Bundle(scenario=parse(ONE), text=old.encode())
    entry = {"profile": profile().snapshot(), "system": "Be brief."}
    origin = await load_origin(store, await recorded(store, bundle, entry))
    assert origin.bundle.digest == bundle.digest
    scenario = origin.bundle.scenario
    made = resolve_texts(Prompts(), scenario.agent("agent"), scenario)
    texts = origin.texts["agent"]
    assert texts.agent == made.agent.model_copy(update={"system": "Be brief."})
    assert (texts.tools, texts.channels) == (made.tools, made.channels)


async def test_texts_recorded_with_a_text_since_removed_load(store: LocalEventStore) -> None:
    texts: dict[str, Any] = resolve_texts(Prompts(), AgentConfig()).snapshot()
    texts["agent"] = {**texts["agent"], "gone": "A text no longer read."}
    entry = {"profile": profile().snapshot(), "texts": texts}
    bundle = Bundle(parse(ONE), ONE.encode())
    origin = await load_origin(store, await recorded(store, bundle, entry))
    assert origin.texts["agent"].agent == AgentTexts()


async def recorded(store: LocalEventStore, bundle: Bundle, entry: dict[str, Any]) -> str:
    """A run of one agent whose `run.started` holds `entry` for it."""
    return await store.create_run(
        RunStarted(
            scenario="t",
            oversight="observe",
            agents=["agent"],
            config={"scenario": bundle.snapshot(), "agents": {"agent": entry}},
        ),
        blobs=bundle.blobs(),
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


async def test_a_call_id_used_again_in_a_later_step_replays_its_own_output(
    store: LocalEventStore,
) -> None:
    """A provider that gives every tool call the same id: each replayed call gets the output of
    its own step and waits for its own turn, so the replayed requests match the recorded ones."""
    script = Script(
        call("bash", command="ls"), call("bash", command="pwd"), call("submit", answer="x")
    )
    script.calls = itertools.count(1, 0)
    run_id = await origin_run(store, script)
    origin = await load_origin(store, run_id)
    made = [e.seq for e in await store.events(run_id, types=["tool.call"], limit=10)]
    assert origin.step_calls["agent"] == [[seq] for seq in made]
    outputs = ["stdout:\nran ls\nexit code: 0", "stdout:\nran pwd\nexit code: 0"]

    fork_id = await fork_live(store, origin, 2, Script(call("submit", answer="forked")))
    assert [r.output for r in await payloads(store, fork_id, ToolResult)][:2] == outputs

    at = cut_at(origin, f"@{origin.step_seqs['agent'][1]}")
    assert isinstance(at, CutAt)
    fork = Fork(origin=run_id, step=2, steps=at.steps, cut=at.seq)
    bench = Bench(store, cast(Any, None), models(profile()))
    fork_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = (await planner(bench, origin, fork)(fork_id))["agent"]
    assert plan.order == {(1, 0): made[0], (2, 0): made[1]}
    finished = await agent(store, fork_id, Script(call("submit", answer="cut")), replay=plan).run()
    assert (finished.reason, finished.answer) == ("submitted", "cut"), finished.detail
    assert [r.output for r in await payloads(store, fork_id, ToolResult)][:2] == outputs


TWO = calls(call("bash", command="echo first"), call("bash", command="echo second"))


async def test_a_step_whose_calls_share_an_id_is_whole_only_with_a_result_for_each(
    store: LocalEventStore,
) -> None:
    """A provider that gives two calls of one reply the same id. The run was killed after the
    first of them ran, so its step has one result of two and a fork cannot replay it."""
    script = Script(call("bash", command="ls"), TWO)
    script.calls = itertools.count(7, 0)
    gate = Gate({("after_tool", 2): Verdict("kill", reason="policy")})
    origin = await load_origin(store, await origin_run(store, script, gate))
    assert (origin.steps, origin.replayable) == ({"agent": 2}, {"agent": 1})
    with pytest.raises(RunError, match=ENDED):
        check_fork(origin, 2)


async def test_calls_of_one_step_that_share_an_id_replay_their_own_outputs(
    store: LocalEventStore,
) -> None:
    """Each call keeps the output and the turn of its place in the step."""
    script = Script(TWO, call("submit", answer="x"))
    script.calls = itertools.count(7, 0)
    run_id = await origin_run(store, script)
    origin = await load_origin(store, run_id)
    outputs = ["stdout:\nran echo first\nexit code: 0", "stdout:\nran echo second\nexit code: 0"]
    assert [origin.outputs["agent"][1, place].output for place in (0, 1)] == outputs

    fork_id = await fork_live(store, origin, 1, Script(call("submit", answer="forked")))
    replayed = [r for r in await payloads(store, fork_id, ToolResult) if r.replayed]
    assert [(r.call_id, r.output) for r in replayed] == [("call_7", out) for out in outputs]
    assert await payloads(store, fork_id, ReplayDivergence) == []

    made = [e.seq for e in await store.events(run_id, types=["tool.call"], limit=10)]
    at = cut_at(origin, f"@{origin.step_seqs['agent'][0]}")
    assert isinstance(at, CutAt)
    fork = Fork(origin=run_id, step=1, steps=at.steps, cut=at.seq)
    bench = Bench(store, cast(Any, None), models(profile()))
    fork_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = (await planner(bench, origin, fork)(fork_id))["agent"]
    assert plan.order == {(1, 0): made[0], (1, 1): made[1]}
    finished = await agent(store, fork_id, Script(call("submit", answer="cut")), replay=plan).run()
    assert (finished.reason, finished.answer) == ("submitted", "cut"), finished.detail
    assert [r.output for r in await payloads(store, fork_id, ToolResult)][:2] == outputs
    assert await payloads(store, fork_id, ReplayDivergence) == []


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


@pytest.mark.parametrize("keep", [False, True], ids=["cut", "kept"])
@pytest.mark.parametrize("warn_at", [None, 0.6], ids=["no-warning", "warning"])
async def test_a_replay_stops_and_warns_where_its_origin_did(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch, warn_at: float | None, keep: bool
) -> None:
    """The origin counted only each answer's tokens, as runs did before the tokens of a failed
    attempt counted. Its first call failed once with 500 output tokens billed. A fork counts
    them, so by its own count the cap of 2000 falls a step early, and so does the warning at
    1200. A fork after step 4 replays the 4 steps the origin took, with the origin's warning,
    then goes live. A fork that keeps the recorded answers from step 1 ends where the origin
    did."""
    caps = "output_tokens: 2000" + ("" if warn_at is None else f", warn_at: {warn_at}")
    text = ONE.replace("  - id: agent\n", f"  - id: agent\n    budget: {{{caps}}}\n")
    config = parse(text).agent("agent")
    p = retrying()
    add = Totals.add

    def answer_only(self: Totals, reply: Reply, *, step: bool) -> None:
        add(self, reply.model_copy(update={"billed": reply.usage}), step=step)

    origin_id = await started(store, text, p)
    ok = call("bash", command="true") | {"usage": FULL}
    with monkeypatch.context() as m:
        m.setattr(Totals, "add", answer_only)
        play = Script(BILLED, *[ok] * 5)
        ended = await agent(store, origin_id, play, config=config, p=p).run()
    assert (ended.reason, ended.steps) == ("budget_output_tokens", 4)
    await store.append(origin_id, [Draft(RunFinished(status="completed"))])

    origin = await load_origin(store, origin_id)
    fork = Fork(origin=origin_id, step=1, keep_others=True) if keep else Fork(origin_id, 4)
    bench = Bench(store, cast(Any, None), models(p))
    fork_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = (await planner(bench, origin, fork)(fork_id))["agent"]
    own = config.model_copy(
        update={"budget": config.budget.model_copy(update={"output_tokens": None})}
    )
    live = Script(call("submit", answer="forked"))
    finished = await agent(store, fork_id, live, replay=plan, config=own, p=p).run()
    steps = await payloads(store, fork_id, AgentStep)
    if keep:
        assert (finished.reason, finished.steps) == ("budget_output_tokens", 4), finished.detail
        assert ([s.source for s in steps], live.requests) == (["replay"] * 4, [])
    else:
        assert (finished.reason, finished.steps, finished.answer) == ("submitted", 5, "forked"), (
            finished.detail
        )
        assert [s.source for s in steps] == ["replay"] * 4 + ["live"]
    # the fork's own count, which the caps did not go by while the steps replayed
    assert [s.total.output_tokens for s in steps][:4] == [1000, 1500, 2000, 2500]

    async def warned(run_id: str) -> list[tuple[int, str]]:
        inputs = await payloads(store, run_id, AgentInput)
        return [(i.step, str(i.text)) for i in inputs if i.kind == "budget_warning"]

    told = [] if warn_at is None else [(3, "You have used 1500 of your 2000 output tokens.")]
    assert await warned(origin_id) == told
    assert await warned(fork_id) == told


@pytest.mark.parametrize("keep", [False, True], ids=["cut", "kept"])
async def test_a_replay_warns_of_its_cost_and_stops_where_its_origin_did(
    store: LocalEventStore, keep: bool
) -> None:
    """Each answer costs $2. The origin is told of its cost after step 3 and stops at its cap
    of $10 after step 5. A replayed answer costs the fork nothing, so by its own count the
    warning never falls. A fork after step 4 replays the warning, then goes live. A fork that
    keeps the recorded answers from step 1 ends where the origin did, having spent nothing."""
    text = ONE.replace(
        "  - id: agent\n", "  - id: agent\n    budget: {cost_usd: 10, warn_at: 0.5}\n"
    )
    config = parse(text).agent("agent")
    p = retrying(price={"input": 1000.0, "cached": 100.0, "output": 2000.0})
    origin_id = await started(store, text, p)
    ok = call("bash", command="true") | {"usage": FULL}
    ended = await agent(store, origin_id, Script(*[ok] * 6), config=config, p=p).run()
    assert (ended.reason, ended.steps, ended.cost_usd) == ("budget_cost", 5, 10.0)
    await store.append(origin_id, [Draft(RunFinished(status="completed"))])

    origin = await load_origin(store, origin_id)
    fork = Fork(origin=origin_id, step=1, keep_others=True) if keep else Fork(origin_id, 4)
    bench = Bench(store, cast(Any, None), models(p))
    fork_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = (await planner(bench, origin, fork)(fork_id))["agent"]
    live = Script(call("submit", answer="forked"))
    finished = await agent(store, fork_id, live, replay=plan, config=config, p=p).run()
    if keep:
        assert (finished.reason, finished.steps) == ("budget_cost", 5), finished.detail
        assert (finished.cost_usd, live.requests) == (0.0, [])
    else:
        assert (finished.reason, finished.steps, finished.answer) == ("submitted", 5, "forked"), (
            finished.detail
        )
    warnings = [i for i in await payloads(store, fork_id, AgentInput) if i.kind == "budget_warning"]
    assert [(i.step, str(i.text)) for i in warnings] == [
        (3, "You have spent 6.00 of your 10.00 USD.")
    ]
