import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, call, models, profile, raw_call, say

from aims import prompts
from aims.agent import (
    Agent,
    GatePoint,
    PauseClock,
    RecordedOutput,
    ReplayPlan,
    RunBudget,
    Verdict,
    is_context_error,
)
from aims.events import (
    AgentCompacted,
    AgentFinished,
    AgentInput,
    AgentReport,
    AgentStep,
    ExecFinished,
    LocalEventStore,
    ReplayDivergence,
    RunStarted,
    ToolResult,
)
from aims.models import Message, ModelClient, Profile, ToolOutput
from aims.prompts import AgentTexts, Prompts
from aims.run.control import ControlGate
from aims.sandbox import AuditedSandbox
from aims.sandbox.audit import ExecResult
from aims.scenario import AgentConfig, Budget, Compaction, Limits, SandboxConfig
from aims.tools import cap, cap_stream, load_tools

pytestmark = pytest.mark.anyio


class Paused:
    def __init__(self) -> None:
        self.log: list[str] = []

    async def pause(self) -> None:
        self.log.append("pause")

    async def resume(self) -> None:
        self.log.append("resume")


class FakeSandbox:
    """Answers execs from `handler(command, stdin) -> (stdout, exit code)`."""

    def __init__(self, handler: Callable[[str, bytes | None], tuple[bytes, int]]) -> None:
        self.handler = handler
        self.sandbox = Paused()
        self.stopped = False
        self.commands: list[str] = []

    async def exec(self, command: str, **kw: Any) -> ExecResult:
        self.commands.append(command)
        out, code = self.handler(command, kw.get("stdin"))
        finished = ExecFinished(
            exec_id=str(uuid.uuid7()),
            call_id=kw.get("call_id"),
            end="exited",
            exit_code=code,
            pid=1,
            duration_ms=1,
            stdout="",
            stderr="",
            stdout_bytes=len(out),
            stderr_bytes=0,
            truncated=False,
        )
        return ExecResult(finished, out, b"", out[-8192:], b"")


def echo(command: str, stdin: bytes | None) -> tuple[bytes, int]:
    return f"ran {command}\n".encode(), 0


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def make_agent(
    store: LocalEventStore,
    script: Script,
    *,
    p: Profile | None = None,
    sandbox: FakeSandbox | None = None,
    limits: Limits | None = None,
    replay: ReplayPlan | None = None,
    gate: Any = None,
    texts: AgentTexts | None = None,
    tool_texts: dict[str, dict[str, str]] | None = None,
    run_budget: RunBudget | None = None,
    clock: PauseClock | None = None,
    **config: Any,
) -> tuple[Agent, str]:
    p = p or profile()
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    client = ModelClient(store, run_id, models(p), transport=script.transport(), env={})
    agent_config = AgentConfig(**config)
    agent = Agent(
        config=agent_config,
        sandbox_config=SandboxConfig(),
        instruction="do the task",
        texts=texts or AgentTexts(),
        profile=p,
        client=client,
        sandbox=cast(AuditedSandbox, sandbox or FakeSandbox(echo)),
        store=store,
        run_id=run_id,
        tools=load_tools(agent_config.tool_names, tool_texts),
        run_budget=run_budget or RunBudget(limits or Limits()),
        gate=gate,
        replay=replay,
        clock=clock,
    )
    return agent, run_id


async def payloads[T](store: LocalEventStore, run_id: str, kind: type[T]) -> list[T]:
    events = await store.events(run_id, limit=1000)
    return [p for p in (e.payload() for e in events) if isinstance(p, kind)]


def test_cap_keeps_head_and_tail() -> None:
    assert cap(b"short") == ("short", False)
    text, cut = cap(b"a" * 10 + b"b" * 10, limit=8)
    assert cut
    assert text == "aaaa\n[... 12 bytes omitted ...]\nbbbb"
    text, cut = cap_stream(b"x" * 100, b"y" * 50, 1000, limit=20)
    assert text == "xxxxxxxxxx\n[... 980 bytes omitted ...]\nyyyyyyyyyy"
    assert cut


def test_context_errors_are_recognised() -> None:
    assert is_context_error(
        "HTTP 400: This model's maximum context length is 8192 tokens. However, you requested"
    )
    assert is_context_error("HTTP 400: The prompt is too long: 2000000, model maximum context")
    assert is_context_error("HTTP 400: context_length_exceeded")
    assert not is_context_error("HTTP 400: unknown parameter foo")
    assert not is_context_error(None)


async def test_submit_ends_the_agent_with_its_answer(store: LocalEventStore) -> None:
    script = Script(call("bash", command="ls"), call("submit", answer="done"))
    agent, run_id = await make_agent(store, script)
    finished = await agent.run()
    assert (finished.reason, finished.answer, finished.steps) == ("submitted", "done", 2)
    assert finished.cost_usd == pytest.approx(2 * (100 * 1.0 + 10 * 2.0) / 1e6)
    results = await payloads(store, run_id, ToolResult)
    assert results[0].output == "stdout:\nran ls\nexit code: 0"
    assert results[1].output == "Submitted."
    steps = await payloads(store, run_id, AgentStep)
    assert [s.n for s in steps] == [1, 2]
    assert steps[1].total.output_tokens == 20
    second = script.requests[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[0]["content"] == prompts.SYSTEM
    names = [t["function"]["name"] for t in script.requests[0]["tools"]]
    assert names == ["bash", "edit", "submit", "report_to_operator"]


async def test_three_replies_without_a_tool_end_as_no_submit(store: LocalEventStore) -> None:
    script = Script(
        say("thinking"), say("still"), call("bash", command="true"), say("a"), say("b"), say("c")
    )
    agent, run_id = await make_agent(store, script)
    finished = await agent.run()
    assert (finished.reason, finished.steps) == ("no_submit", 6)
    inputs = await payloads(store, run_id, AgentInput)
    assert [i.kind for i in inputs] == [
        "instruction",
        "continue",
        "continue",
        "continue",
        "continue",
    ]
    assert inputs[1].text == prompts.NUDGE


async def test_bad_arguments_and_unknown_tools_are_tool_errors(store: LocalEventStore) -> None:
    script = Script(
        raw_call("bash", "{not json"),
        call("nope"),
        call("bash", timeout_s=3),
        call("submit", answer="x"),
    )
    agent, run_id = await make_agent(store, script)
    finished = await agent.run()
    assert (finished.reason, finished.steps) == ("submitted", 4)
    results = await payloads(store, run_id, ToolResult)
    assert [r.error for r in results] == [True, True, True, False]
    assert str(results[0].output).startswith("The arguments are not valid JSON")
    assert "There is no tool named 'nope'" in str(results[1].output)
    assert "command: Field required" in str(results[2].output)


async def test_step_and_cost_budgets_end_the_agent(store: LocalEventStore) -> None:
    script = Script(*[call("bash", command="true")] * 5)
    agent, _ = await make_agent(store, script, budget=Budget(steps=2))
    finished = await agent.run()
    assert (finished.reason, finished.steps) == ("budget_steps", 2)
    assert finished.detail == "agent limit of 2 steps reached"

    script = Script(*[call("bash", command="true")] * 5)
    agent, _ = await make_agent(store, script, limits=Limits(cost_usd=0.0002))
    finished = await agent.run()
    assert (finished.reason, finished.steps) == ("budget_cost", 2)
    assert finished.detail == "run limit of 0.0002 USD reached"


async def test_the_wall_budget_cancels_a_slow_step(store: LocalEventStore) -> None:
    def slow(command: str, stdin: bytes | None) -> tuple[bytes, int]:
        raise AssertionError("not reached")

    class Slow(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            await asyncio.sleep(5)
            raise AssertionError("not reached")

    script = Script(call("bash", command="sleep 5"))
    agent, _ = await make_agent(store, script, sandbox=Slow(slow), budget=Budget(wall_s=0.3))
    finished = await agent.run()
    assert (finished.reason, finished.detail) == ("budget_wall", "agent wall limit reached")


async def test_a_context_error_ends_as_context_exhausted(store: LocalEventStore) -> None:
    script = Script({"status": 400, "error": "maximum context length is 10 tokens"})
    agent, _ = await make_agent(store, script)
    finished = await agent.run()
    assert finished.reason == "context_exhausted"
    script = Script({"status": 400, "error": "bad request"})
    agent, _ = await make_agent(store, script)
    assert (await agent.run()).reason == "model_error"


async def test_report_to_operator_logs_and_continues(store: LocalEventStore) -> None:
    script = Script(
        call("report_to_operator", message="tests look odd"), call("submit", answer="ok")
    )
    agent, run_id = await make_agent(store, script)
    assert (await agent.run()).reason == "submitted"
    (report,) = await payloads(store, run_id, AgentReport)
    assert (report.message, report.step) == ("tests look odd", 1)


async def test_trim_compaction_replaces_old_outputs(store: LocalEventStore) -> None:
    big = {"prompt_tokens": 900, "completion_tokens": 10}
    turns = [{**call("bash", command=f"echo {i}"), "usage": big} for i in range(4)]
    script = Script(*turns, call("submit", answer="x"))
    p = profile(context_tokens=1000)
    agent, run_id = await make_agent(
        store, script, p=p, compaction=Compaction(at=0.5, strategy="trim", keep_recent=1)
    )
    assert (await agent.run()).reason == "submitted"
    compacted = await payloads(store, run_id, AgentCompacted)
    assert [(c.from_step, c.to_step) for c in compacted] == [(1, 1), (2, 2), (3, 3)]
    assert compacted[0].bytes_after < compacted[0].bytes_before
    last = script.requests[-1]["messages"]
    tools = [m["content"] for m in last if m["role"] == "tool"]
    assert tools[:3] == [prompts.TRIMMED] * 3
    assert tools[3].startswith("stdout:\nran echo 3")


async def test_summary_compaction_rewrites_the_history(store: LocalEventStore) -> None:
    big = {"prompt_tokens": 900, "completion_tokens": 10}
    script = Script(
        {**call("bash", command="echo 1"), "usage": big},
        {**call("bash", command="echo 2"), "usage": big},
        say("I listed files twice."),
        call("submit", answer="x"),
    )
    p = profile(context_tokens=1000)
    agent, run_id = await make_agent(
        store, script, p=p, compaction=Compaction(at=0.5, strategy="summary", keep_recent=1)
    )
    assert (await agent.run()).reason == "submitted"
    summary_request = script.requests[2]["messages"]
    assert summary_request[-1] == {"role": "user", "content": prompts.SUMMARY_REQUEST}
    after = script.requests[3]["messages"]
    assert after[2] == {
        "role": "user",
        "content": prompts.SUMMARY_PREFIX + "I listed files twice.",
    }
    assert [m["role"] for m in after] == ["system", "user", "user", "assistant", "tool"]
    (compacted,) = await payloads(store, run_id, AgentCompacted)
    assert (compacted.strategy, compacted.from_step, compacted.to_step) == ("summary", 1, 1)
    assert compacted.summary == "I listed files twice."
    steps = await payloads(store, run_id, AgentStep)
    assert [s.n for s in steps] == [1, 2, 3]


class Gate:
    def __init__(self, verdicts: dict[tuple[str, int], Verdict]) -> None:
        self.verdicts = verdicts
        self.points: list[GatePoint] = []

    async def check(self, point: GatePoint) -> Verdict:
        self.points.append(point)
        return self.verdicts.pop((point.kind, point.step), Verdict())

    async def held(self, point: GatePoint) -> Verdict:
        return Verdict()


async def test_the_gate_pauses_injects_and_kills(store: LocalEventStore) -> None:
    gate = Gate(
        {
            ("after_reply", 1): Verdict("pause"),
            ("after_tool", 1): Verdict("inject", text="note this"),
            ("before_tool", 2): Verdict("kill", reason="policy"),
        }
    )
    sandbox = FakeSandbox(echo)
    script = Script(call("bash", command="ls"), call("bash", command="rm -rf /"))
    agent, run_id = await make_agent(store, script, sandbox=sandbox, gate=gate)
    finished = await agent.run()
    assert (finished.reason, finished.detail) == ("killed", "policy")
    assert sandbox.sandbox.log == ["pause", "resume"]
    assert sandbox.commands == ["ls"]
    kinds = [(p.kind, p.step) for p in gate.points]
    assert kinds[:4] == [
        ("before_model", 0),
        ("after_reply", 1),
        ("before_tool", 1),
        ("after_tool", 1),
    ]
    inputs = await payloads(store, run_id, AgentInput)
    assert [(i.kind, i.text) for i in inputs][1] == ("inject", "note this")
    assert script.requests[1]["messages"][-1] == {"role": "user", "content": "note this"}


async def test_a_fork_feeds_recorded_outputs_and_logs_divergence(store: LocalEventStore) -> None:
    script = Script(
        call("bash", command="date"), call("bash", command="ls"), call("submit", answer="x")
    )
    agent, origin = await make_agent(store, script)
    assert (await agent.run()).reason == "submitted"
    from aims.models import Recording

    recorded = {
        r.call_id: RecordedOutput(str(r.output), r.error)
        for r in await payloads(store, origin, ToolResult)
    }

    def changed(command: str, stdin: bytes | None) -> tuple[bytes, int]:
        return (b"another day\n", 0) if command == "date" else echo(command, stdin)

    for strict, reason in ((False, "submitted"), (True, "divergence")):
        recording = await Recording.load(store, origin)
        live = Script(call("submit", answer="forked"))
        p = profile()
        run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
        plan = ReplayPlan(
            client=ModelClient(store, run_id, models(p), replay=recording, env={}),
            fork_at=2,
            schedule=[(None, p)],
            outputs=recorded,
            strict=strict,
            inject="you are being forked" if not strict else None,
        )
        fork = Agent(
            config=AgentConfig(),
            sandbox_config=SandboxConfig(),
            instruction="do the task",
            texts=AgentTexts(),
            profile=p,
            client=ModelClient(store, run_id, models(p), transport=live.transport(), env={}),
            sandbox=cast(AuditedSandbox, FakeSandbox(changed)),
            store=store,
            run_id=run_id,
            tools=load_tools(AgentConfig().tool_names),
            run_budget=RunBudget(Limits()),
            replay=plan,
        )
        finished = await fork.run()
        assert finished.reason == reason
        (divergence,) = await payloads(store, run_id, ReplayDivergence)
        assert (divergence.step, divergence.tool, divergence.line) == (1, "bash", 2)
        assert divergence.recorded == "stdout:\nran date\nexit code: 0"
        assert divergence.live == "stdout:\nanother day\nexit code: 0"
        if not strict:
            assert finished.answer == "forked"
            (request,) = live.requests
            tools = [m["content"] for m in request["messages"] if m["role"] == "tool"]
            assert tools[0] == "stdout:\nran date\nexit code: 0"
            assert request["messages"][-1] == {"role": "user", "content": "you are being forked"}
            results = await payloads(store, run_id, ToolResult)
            assert [r.replayed for r in results] == [True, True, False]


async def test_a_replay_miss_ends_the_fork_with_an_error(store: LocalEventStore) -> None:
    script = Script(call("bash", command="ls"), call("submit", answer="x"))
    agent, origin = await make_agent(store, script)
    await agent.run()
    from aims.models import Recording

    p = profile()
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    plan = ReplayPlan(
        client=ModelClient(store, run_id, models(p), replay=await Recording.load(store, origin)),
        fork_at=2,
        schedule=[(None, p)],
        outputs={},
    )
    fork = Agent(
        config=AgentConfig(),
        sandbox_config=SandboxConfig(),
        instruction="another task",
        texts=AgentTexts(),
        profile=p,
        client=ModelClient(store, run_id, models(p), env={}),
        sandbox=cast(AuditedSandbox, FakeSandbox(echo)),
        store=store,
        run_id=run_id,
        tools=load_tools(AgentConfig().tool_names),
        run_budget=RunBudget(Limits()),
        replay=plan,
    )
    finished = await fork.run()
    assert finished.reason == "error"
    assert finished.detail is not None
    assert "replay of step 1 failed" in finished.detail
    assert "first difference at $.messages[1].content" in finished.detail


async def test_cancelling_an_agent_logs_its_end(store: LocalEventStore) -> None:
    class Hang(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            await asyncio.sleep(30)
            raise AssertionError("not reached")

    script = Script(call("bash", command="sleep 30"))
    agent, run_id = await make_agent(store, script, sandbox=Hang(echo))
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (finished,) = await payloads(store, run_id, AgentFinished)
    assert (finished.reason, finished.steps) == ("cancelled", 1)


def test_budget_warning_text() -> None:
    text = AgentTexts().budget("cost_usd").format(used=0.4, limit=1.0)
    assert text == "You have spent 0.40 of your 1.00 USD."


async def test_overridden_texts_reach_the_model(store: LocalEventStore) -> None:
    layered = Prompts(system="sys", nudge="go on", tools={"submit": {"reply": "thanks"}}).under(
        Prompts(nudge="keep going", tools={"bash": {"description": "Run {workdir} things."}})
    )
    texts = layered.agent_texts()
    assert (texts.system, texts.nudge, texts.trimmed) == ("sys", "keep going", prompts.TRIMMED)
    script = Script(say("hm"), call("submit", answer="x"))
    agent, run_id = await make_agent(store, script, texts=texts, tool_texts=layered.tools)
    assert (await agent.run()).reason == "submitted"
    first = script.requests[0]
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    bash = next(t for t in first["tools"] if t["function"]["name"] == "bash")
    assert bash["function"]["description"] == "Run /work things."
    assert script.requests[1]["messages"][-1] == {"role": "user", "content": "keep going"}
    results = await payloads(store, run_id, ToolResult)
    assert results[-1].output == "thanks"


def test_bad_texts_are_refused() -> None:
    from aims.prompts import TextError
    from aims.tools import UnknownTool

    with pytest.raises(TextError, match="unknown placeholder count"):
        Prompts(budget_steps="{count} steps").agent_texts()
    with pytest.raises(TextError, match=r"tools\.bash\.description: unknown placeholder cwd"):
        load_tools(["bash"], {"bash": {"description": "in {cwd}"}})
    with pytest.raises(TextError, match="tool submit has no text color"):
        load_tools(["submit"], {"submit": {"color": "red"}})
    with pytest.raises(UnknownTool, match="nope"):
        load_tools(["submit"], {"nope": {"description": "x"}})
    assert load_tools(["submit"], {"bash": {"description": "unused"}})[0].name == "submit"


async def test_a_denied_call_is_not_run_and_the_agent_moves_on(store: LocalEventStore) -> None:
    gate = Gate({("before_tool", 1): Verdict("deny")})
    sandbox = FakeSandbox(echo)
    script = Script(call("bash", command="rm -rf /work"), call("submit", answer="done"))
    agent, run_id = await make_agent(store, script, sandbox=sandbox, gate=gate)
    finished = await agent.run()

    assert finished.reason == "submitted"
    assert sandbox.commands == []
    results = await payloads(store, run_id, ToolResult)
    assert (results[0].denied, results[0].error) == (True, True)
    assert results[0].output == AgentTexts().denied
    assert results[0].output_bytes is None
    assert not results[1].denied
    sent = script.requests[1]["messages"][-1]
    assert (sent["role"], sent["content"]) == ("tool", AgentTexts().denied)
    kinds = [(p.kind, p.step) for p in gate.points]
    assert kinds[:5] == [
        ("before_model", 0),
        ("after_reply", 1),
        ("before_tool", 1),
        ("before_model", 1),
        ("after_reply", 2),
    ]


class Sleepy(FakeSandbox):
    def __init__(self, seconds: float) -> None:
        super().__init__(echo)
        self.seconds = seconds
        self.started = asyncio.Event()

    async def exec(self, command: str, **kw: Any) -> ExecResult:
        self.started.set()
        await asyncio.sleep(self.seconds)
        return await super().exec(command, **kw)


async def until_held(gate: ControlGate, agent: str) -> None:
    async with asyncio.timeout(10):
        while not gate.is_held(agent):  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_paused_time_mid_call_does_not_count_against_the_wall(
    store: LocalEventStore,
) -> None:
    gate = ControlGate()
    sandbox = Sleepy(0.3)
    script = Script(call("bash", command="sleep"), call("submit", answer="ok"))
    agent, run_id = await make_agent(
        store,
        script,
        sandbox=sandbox,
        gate=gate,
        clock=gate.clock("agent"),
        budget=Budget(wall_s=1.0),
    )
    task = asyncio.create_task(agent.run())
    await sandbox.started.wait()
    gate.pause(["agent"])
    await asyncio.sleep(1.5)
    assert gate.is_held("agent")
    gate.resume(["agent"])
    finished = await task

    assert finished.reason == "submitted", finished.detail
    assert finished.paused_ms >= 1400
    assert finished.wall_ms >= max(finished.paused_ms, 1000)
    (recorded,) = await payloads(store, run_id, AgentFinished)
    assert recorded.paused_ms == finished.paused_ms


async def test_without_a_pause_the_same_wait_ends_the_agent(store: LocalEventStore) -> None:
    script = Script(call("bash", command="sleep"), call("submit", answer="ok"))
    agent, _ = await make_agent(store, script, sandbox=Sleepy(1.5), budget=Budget(wall_s=1.0))
    finished = await agent.run()
    assert (finished.reason, finished.detail) == ("budget_wall", "agent wall limit reached")
    assert finished.paused_ms == 0


async def test_the_run_wall_stops_only_while_every_agent_is_paused(
    store: LocalEventStore,
) -> None:
    gate = ControlGate()
    budget = RunBudget(Limits(wall_s=1.0))
    made = []
    for name in ("a", "b"):
        script = Script(call("bash", command="x"), call("submit", answer=name))
        made.append(
            await make_agent(
                store,
                script,
                gate=gate,
                run_budget=budget,
                clock=gate.clock(name),
                id=name,
                budget=Budget(wall_s=None),
            )
        )
    gate.pause(["a", "b"])
    tasks = [asyncio.create_task(agent.run()) for agent, _ in made]
    await asyncio.sleep(1.3)
    assert budget.paused
    gate.resume(["a", "b"])
    done = await asyncio.gather(*tasks)
    assert [f.reason for f in done] == ["submitted", "submitted"]
    assert all(f.paused_ms >= 1200 for f in done)

    gate = ControlGate()
    budget = RunBudget(Limits(wall_s=0.5))
    slow, _ = await make_agent(
        store,
        Script(call("bash", command="x")),
        sandbox=Sleepy(5),
        run_budget=budget,
        clock=gate.clock("slow"),
        gate=gate,
        id="slow",
    )
    held, _ = await make_agent(
        store,
        Script(call("submit", answer="never")),
        run_budget=budget,
        clock=gate.clock("held"),
        gate=gate,
        id="held",
    )
    gate.pause(["held"])
    ends = await asyncio.wait_for(asyncio.gather(slow.run(), held.run()), 5)
    assert [(f.reason, f.detail) for f in ends] == [("budget_wall", "run wall limit reached")] * 2
    assert not budget.paused


async def test_notify_on_resume_reaches_the_agent(store: LocalEventStore) -> None:
    gate = ControlGate()
    texts = AgentTexts(notify_on_resume="You were paused for a while.")
    script = Script(call("submit", answer="ok"))
    agent, run_id = await make_agent(
        store, script, gate=gate, clock=gate.clock("agent"), texts=texts
    )
    gate.pause(["agent"])
    task = asyncio.create_task(agent.run())
    await until_held(gate, "agent")
    gate.resume(["agent"])
    assert (await task).reason == "submitted"
    inputs = await payloads(store, run_id, AgentInput)
    assert [(i.kind, i.text) for i in inputs][-1] == ("inject", "You were paused for a while.")
    sent = script.requests[0]["messages"][-1]
    assert sent == {"role": "user", "content": "You were paused for a while."}

    quiet = Script(call("submit", answer="ok"))
    gate = ControlGate()
    agent, run_id = await make_agent(store, quiet, gate=gate, clock=gate.clock("agent"))
    gate.pause(["agent"])
    task = asyncio.create_task(agent.run())
    await until_held(gate, "agent")
    gate.resume(["agent"])
    await task
    assert [i.kind for i in await payloads(store, run_id, AgentInput)] == ["instruction"]


async def test_next_prompt_is_the_step_whose_calls_wait(store: LocalEventStore) -> None:
    seen: dict[str, tuple[Profile, Any]] = {}

    class Peek:
        async def check(self, point: GatePoint) -> Verdict:
            if point.kind in ("before_tool", "before_model") and point.step == 1:
                seen[point.kind] = agent.next_prompt()
            return Verdict()

        async def held(self, point: GatePoint) -> Verdict:
            return Verdict()

    script = Script(call("bash", command="ls"), call("submit", answer="ok"))
    agent, _ = await make_agent(store, script, gate=Peek())
    await agent.run()
    profile_at_tool, at_tool = seen["before_tool"]
    assert profile_at_tool.name == "scripted"
    assert [type(i) for i in at_tool.items] == [Message, Message]
    assert [t.name for t in at_tool.tools] == [t.name for t in agent.specs]
    _, next_call = seen["before_model"]
    assert isinstance(next_call.items[-1], ToolOutput)
    assert len(next_call.items) == 4
