import asyncio
import time
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
    Turnstile,
    Verdict,
    is_context_error,
)
from aims.events import (
    AgentCompacted,
    AgentFinished,
    AgentHeld,
    AgentInput,
    AgentReport,
    AgentStarted,
    AgentStep,
    ExecFinished,
    LocalEventStore,
    ModelResponse,
    ReplayDivergence,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.models import Message, ModelClient, Profile, Prompt, Retries, ToolOutput
from aims.models.types import NO_ROOM
from aims.prompts import AgentTexts, Prompts
from aims.run.control import ControlGate
from aims.run.estimate import _agent_ceiling, step_usd
from aims.run.script import ScriptedAgent, Watch
from aims.sandbox import AuditedSandbox
from aims.sandbox.audit import ExecResult
from aims.scenario import AgentConfig, Budget, Compaction, Limits, SandboxConfig, Scenario
from aims.scenario.model import ScriptStep
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

    async def pause(self) -> str | None:
        await self.sandbox.pause()
        return None

    async def resume(self) -> str | None:
        await self.sandbox.resume()
        return None

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
    marker = prompts.OMITTED
    assert cap(b"short", marker=marker) == ("short", False)
    text, cut = cap(b"a" * 10 + b"b" * 10, limit=8, marker=marker)
    assert cut
    assert text == "aaaa\n[... 12 bytes omitted ...]\nbbbb"
    text, cut = cap_stream(b"x" * 100, b"y" * 50, 1000, limit=20, marker=marker)
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


async def test_a_fork_counts_replies_without_a_tool_call_from_its_first_live_one(
    store: LocalEventStore,
) -> None:
    """The origin ended as no_submit on its third reply without a tool call. A fork after its
    second or third reply has three live replies without a tool call, as a new run has; a fork
    that keeps the recorded answers ends where the origin did."""
    from aims.models import Recording

    agent, origin = await make_agent(store, Script(say("a"), say("b"), say("c")))
    assert (await agent.run()).reason == "no_submit"

    async def fork(step: int, live: Script, keep: bool = False) -> tuple[AgentFinished, list[str]]:
        agent, run_id = await make_agent(store, live)
        recording = await Recording.load(store, origin)
        agent.replay = ReplayPlan(
            client=ModelClient(store, run_id, models(agent.profile), replay=recording, env={}),
            fork_at=step,
            schedule=[(None, agent.profile)],
            outputs={},
            keep=keep,
        )
        finished = await agent.run()
        return finished, [i.kind for i in await payloads(store, run_id, AgentInput)]

    for step in (2, 3):
        live = Script(say("x"), say("y"), call("submit", answer="forked"))
        finished, inputs = await fork(step, live)
        assert (finished.reason, finished.answer, len(live.requests)) == ("submitted", "forked", 3)
        assert inputs == ["instruction", *["continue"] * (step + 2)]
    live = Script(call("submit", answer="live"))
    finished, _ = await fork(1, live, keep=True)
    assert (finished.reason, finished.steps, live.requests) == ("no_submit", 3, [])


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


async def test_a_command_timeout_the_model_asks_for_stays_within_the_scenario_s_most(
    store: LocalEventStore,
) -> None:
    timeouts: list[float] = []

    class Timed(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            timeouts.append(kw["timeout_s"])
            return await super().exec(command, **kw)

    asked = ["Infinity", "1e308", "1e400", "30", "NaN", "-Infinity", "0"]
    script = Script(
        *(raw_call("bash", f'{{"command": "true", "timeout_s": {t}}}') for t in asked),
        call("submit", answer="x"),
    )
    agent, run_id = await make_agent(store, script, sandbox=Timed(echo))
    finished = await agent.run()
    assert finished.reason == "submitted"
    most = SandboxConfig().max_command_timeout_s
    assert timeouts == [most, most, most, 30]
    results = await payloads(store, run_id, ToolResult)
    for result in results[4:7]:
        assert result.error
        assert "timeout_s: Input should be greater than 0" in str(result.output)


async def test_a_lone_surrogate_in_tool_arguments_is_kept_as_text(store: LocalEventStore) -> None:
    script = Script(
        raw_call("bash", '{"command": "echo \\ud800"}'),
        raw_call("submit", '{"answer": "\\ud83d done"}'),
    )
    agent, run_id = await make_agent(store, script)
    finished = await agent.run()
    assert (finished.reason, finished.answer) == ("submitted", "\\ud83d done")
    assert agent.answer == "\\ud83d done"
    results = await payloads(store, run_id, ToolResult)
    assert results[0].output == "stdout:\nran echo \\ud800\nexit code: 0"
    (logged,) = await payloads(store, run_id, AgentFinished)
    assert logged.answer == "\\ud83d done"


async def test_an_end_that_cannot_be_logged_ends_the_agent_as_an_error(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script(call("submit", answer="done"))
    agent, run_id = await make_agent(store, script)
    finish = agent._finish
    ends: list[Any] = []

    async def failing(*args: Any) -> AgentFinished:
        ends.append(args[1])
        if len(ends) == 1:
            raise OSError("disk full")
        return await finish(*args)

    monkeypatch.setattr(agent, "_finish", failing)
    finished = await agent.run()
    assert ends == ["submitted", "error"]
    assert (finished.reason, finished.detail, finished.answer) == (
        "error",
        "OSError: disk full",
        None,
    )
    assert agent.answer is None
    (logged,) = await payloads(store, run_id, AgentFinished)
    assert (logged.reason, logged.answer) == ("error", None)


@pytest.mark.timing
async def test_a_cancelled_agent_whose_end_cannot_be_logged_still_raises_the_cancel(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = asyncio.Event()

    class Hang(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            running.set()
            await asyncio.sleep(30)
            raise AssertionError("not reached")

    script = Script(call("bash", command="sleep 30"))
    agent, run_id = await make_agent(store, script, sandbox=Hang(echo))
    finish = agent._finish
    ends: list[Any] = []

    async def failing(*args: Any) -> AgentFinished:
        ends.append(args[1])
        if len(ends) == 1:
            raise OSError("disk full")
        return await finish(*args)

    monkeypatch.setattr(agent, "_finish", failing)
    task = asyncio.create_task(agent.run())
    async with asyncio.timeout(5):
        await running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ends == ["cancelled", "error"]
    (logged,) = await payloads(store, run_id, AgentFinished)
    assert (logged.reason, logged.detail) == ("error", "OSError: disk full")


async def test_an_agent_cancelled_while_its_start_is_logged_still_logs_its_end(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, run_id = await make_agent(store, Script(call("bash", command="true")))
    writing, release = asyncio.Event(), asyncio.Event()
    append = store._append

    async def held(rid: str, drafts: Any) -> Any:
        if any(isinstance(d.payload, AgentStarted) for d in drafts):
            writing.set()
            await release.wait()
        return await append(rid, drafts)

    monkeypatch.setattr(store, "_append", held)
    task = asyncio.create_task(agent.run())
    async with asyncio.timeout(5):
        await writing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await payloads(store, run_id, AgentStarted)) == 1
    (finished,) = await payloads(store, run_id, AgentFinished)
    assert (finished.reason, finished.steps) == ("cancelled", 0)


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


async def test_answers_with_no_token_counts_count_against_the_cost_cap_by_size(
    store: LocalEventStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A priced profile whose answers come with no usage: each step counts at its size, so the
    cost cap ends the agent though its recorded cost stays $0, and its stop says so. Only a
    profile with no price is warned about, not a failed call with no cost."""
    free = call("bash", command="true") | {"usage": None}
    agent, _ = await make_agent(store, Script(*[free] * 6), budget=Budget(cost_usd=0.0001))
    finished = await agent.run()
    assert (finished.reason, finished.steps, finished.cost_usd) == ("budget_cost", 1, 0)
    sized = agent.totals.sized_usd
    assert sized > 0.0001
    assert finished.detail == (
        f"agent limit of 0.0001 USD reached; answers with no token counts count at ${sized:.4f}"
    )
    busy = Script({"status": 503, "error": "busy"})
    agent, _ = await make_agent(store, busy, budget=Budget(cost_usd=0.0001))
    assert (await agent.run()).reason == "model_error"
    assert "not enforced" not in caplog.text
    unpriced = profile(price=None)
    agent, _ = await make_agent(
        store, Script(*[free] * 2), p=unpriced, budget=Budget(steps=2, cost_usd=0.0001)
    )
    finished = await agent.run()
    assert (finished.reason, finished.steps) == ("budget_steps", 2)
    assert "has no price, so the cost limit of agent" in caplog.text


def retrying(**kw: Any) -> Profile:
    """A profile whose calls cost at most $0.002 an attempt and are tried 4 times."""
    base: dict[str, Any] = {"context_tokens": 1000, "max_output_tokens": 500}
    return profile(retries=Retries(attempts=4, backoff=0, max=0), **(base | kw))


# One full call of `retrying()`: 1000 input at $1 and 500 output at $2 per million.
FULL = {"prompt_tokens": 1000, "completion_tokens": 500}
BILLED = {"finish": "error", "usage": FULL}


async def test_billed_retries_stay_within_the_estimate_s_ceiling(store: LocalEventStore) -> None:
    """A step cap bounds a call with every attempt its profile gives it, as a provider may bill
    each failed one."""
    p = retrying()
    ok = call("bash", command="true") | {"usage": FULL}
    budget = Budget(steps=1)
    agent, _ = await make_agent(store, Script(BILLED, BILLED, BILLED, ok), p=p, budget=budget)
    finished = await agent.run()
    ceiling, _ = _agent_ceiling(AgentConfig(budget=budget), p)
    assert (finished.reason, finished.steps) == ("budget_steps", 1)
    assert ceiling == pytest.approx(4 * 0.002)
    assert finished.cost_usd == pytest.approx(ceiling)


async def test_the_tokens_of_a_billed_failed_attempt_count_as_its_cost_does(
    store: LocalEventStore,
) -> None:
    """The output cap and the agent's usage count the tokens of every attempt, as the provider
    may bill a failed one for them."""
    p = retrying()
    ok = call("bash", command="true") | {"usage": FULL}
    budget = Budget(output_tokens=900, steps=3)
    agent, run_id = await make_agent(store, Script(BILLED, ok, ok), p=p, budget=budget)
    finished = await agent.run()
    # step 1 wrote 500 output tokens in its failed attempt and 500 in its answer
    assert (finished.reason, finished.steps) == ("budget_output_tokens", 1)
    responses = await payloads(store, run_id, ModelResponse)
    assert [r.usage.output_tokens for r in responses] == [500, 500]
    assert finished.usage.output_tokens == 1000
    assert finished.usage.input_tokens == 2000
    assert finished.cost_usd == pytest.approx(2 * 0.002)
    # the step keeps the answer's own usage; its total counts both attempts
    (step,) = await payloads(store, run_id, AgentStep)
    assert (step.usage.output_tokens, step.total.output_tokens) == (500, 1000)


async def test_the_cost_cap_holds_a_call_s_billed_retries(store: LocalEventStore) -> None:
    """A billed failure is tried again only when another full attempt fits what the cost cap
    leaves, so the agent stays under its cap and one last step. A failure that cost nothing,
    such as a 503, is tried again near the cap."""
    p = retrying()
    ok = call("bash", command="true") | {"usage": FULL}
    script = Script(BILLED, BILLED, BILLED, ok, ok, ok)
    budget = Budget(cost_usd=0.001)
    agent, _ = await make_agent(store, script, p=p, budget=budget)
    finished = await agent.run()
    ceiling, _ = _agent_ceiling(AgentConfig(budget=budget), p)
    assert ceiling == pytest.approx(0.001 + 0.002)
    assert (finished.reason, finished.steps, len(script.requests)) == ("budget_cost", 0, 1)
    assert finished.cost_usd == pytest.approx(0.002)
    assert finished.detail is not None
    assert finished.detail.startswith("agent limit of 0.001 USD: ")
    assert finished.detail.endswith(NO_ROOM)

    busy = Script({"status": 503, "error": "busy"}, ok, ok)
    agent, _ = await make_agent(store, busy, p=p, budget=budget)
    finished = await agent.run()
    assert (finished.reason, finished.steps, len(busy.requests)) == ("budget_cost", 1, 2)
    assert finished.detail == "agent limit of 0.001 USD reached"


async def test_failed_attempts_with_no_cost_count_against_the_cost_cap(
    store: LocalEventStore,
) -> None:
    """A failed attempt that may have been billed with no cost reported counts at what it was
    planned to cost: the cap stops the agent a step sooner and its stop says so."""
    p = retrying()
    unseen = {"finish": "error", "usage": None}
    ok = call("bash", command="true") | {"usage": FULL}
    script = Script(unseen, ok, ok, ok, ok)
    agent, _ = await make_agent(store, script, p=p, budget=Budget(cost_usd=0.0045))
    finished = await agent.run()
    owed = agent.totals.unseen_usd
    assert 0.001 < owed <= 0.002
    assert (finished.reason, finished.steps) == ("budget_cost", 2)
    assert finished.cost_usd == pytest.approx(0.004)
    assert finished.detail == (
        f"agent limit of 0.0045 USD reached; failed calls with no cost reported may add ${owed:.4f}"
    )
    assert agent.run_budget.owed == {}


async def test_a_summary_call_with_no_room_to_retry_ends_the_agent(store: LocalEventStore) -> None:
    p = retrying(max_output_tokens=100)
    full = {"prompt_tokens": 900, "completion_tokens": 10}

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if body["messages"][-1] == {"role": "user", "content": prompts.SUMMARY_REQUEST}:
            return {"finish": "error", "usage": full}
        return {**call("bash", command="true"), "usage": full}

    script = Script(*[reply] * 10)
    compaction = Compaction(at=0.5, strategy="summary", keep_recent=1)
    budget = Budget(cost_usd=0.002)
    agent, _ = await make_agent(store, script, p=p, budget=budget, compaction=compaction)
    finished = await agent.run()
    assert (finished.reason, finished.steps, len(script.requests)) == ("budget_cost", 2, 3)
    assert finished.detail is not None
    assert finished.detail.endswith(NO_ROOM)


async def test_the_run_cost_cap_counts_what_another_agent_s_retries_owe(
    store: LocalEventStore,
) -> None:
    """While one agent's call is tried again, what its failed attempts may have cost counts
    against the run's cost cap for the others."""
    p = retrying()
    run_budget = RunBudget(Limits(cost_usd=0.01))
    a, _ = await make_agent(store, Script(), p=p, run_budget=run_budget, id="a")
    b, _ = await make_agent(store, Script(), p=p, run_budget=run_budget, id="b")
    prompt = Prompt(items=[Message(role="user", content="hi")])
    a_cap, b_cap = a._cap(p, prompt), b._cap(p, prompt)
    assert a_cap is not None
    assert b_cap is not None
    assert a_cap.fits(0.006)
    assert run_budget.owed == {"a": 0.006}
    # 0.01 less a's 0.006 leaves no room for b's 0.0035 and another attempt
    assert not b_cap.fits(0.0035)
    del run_budget.owed["a"]
    assert b_cap.fits(0.0035)
    run_budget.owed = {"a": 0.006}
    assert b._over_budget() is None
    run_budget.totals.cost_usd = 0.004
    assert b._over_budget() == (
        "budget_cost",
        "run limit of 0.01 USD reached; calls of other agents still being tried may have cost"
        " $0.0060",
    )
    assert a._over_budget() is None


async def test_two_agents_stay_under_the_run_cap_and_a_step_each(store: LocalEventStore) -> None:
    p = retrying()
    ok = call("bash", command="true") | {"usage": FULL}
    run_budget = RunBudget(Limits(cost_usd=0.003))
    a_script = Script(BILLED, BILLED, BILLED, BILLED, ok)
    b_script = Script(*[ok] * 5)
    a, _ = await make_agent(store, a_script, p=p, run_budget=run_budget, id="a")
    b, _ = await make_agent(store, b_script, p=p, run_budget=run_budget, id="b")
    a_done, b_done = await asyncio.gather(a.run(), b.run())
    step = step_usd(p)
    assert step is not None
    assert (a_done.reason, b_done.reason) == ("budget_cost", "budget_cost")
    assert run_budget.totals.cost_usd <= 0.003 + 2 * step
    # a's billed failure has no room to be tried again, unless b spent the cap before it started
    assert len(a_script.requests) <= 1
    assert a_done.detail is not None
    assert a_done.detail.startswith("run limit of 0.003 USD")
    if a_script.requests:
        assert a_done.detail.startswith("run limit of 0.003 USD: ")
        assert a_done.detail.endswith(NO_ROOM)
    assert run_budget.owed == {}


def test_a_step_cap_bounds_every_attempt_of_each_call() -> None:
    """32,768 tokens of context at $0.10 and 4,096 of output at $0.40 per million, with the
    default caps of 100 steps and $5: the steps with every attempt bound it below the cost cap."""
    p = profile(
        context_tokens=32_768,
        max_output_tokens=4_096,
        price={"input": 0.1, "cached": 0.01, "output": 0.4},
        retries=Retries(),
    )
    ceiling, stepped = _agent_ceiling(AgentConfig(), p)
    assert (round(ceiling or 0, 2), stepped) == (1.97, False)


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


async def summary_fork(
    store: LocalEventStore,
    origin: str,
    live: Script,
    *,
    fork_at: int,
    keep: bool = False,
    ran_on: Profile,
    outputs: dict[tuple[int, int], RecordedOutput] | None = None,
) -> tuple[AgentFinished, list[AgentCompacted]]:
    """A fork of `origin` after `fork_at` on a profile with 150 tokens of context, which
    compacts by summary; its schedule says the origin ran on `ran_on`."""
    from aims.models import Recording

    summary = Compaction(at=0.5, strategy="summary", keep_recent=1)
    agent, run_id = await make_agent(store, live, p=profile(context_tokens=150), compaction=summary)
    agent.replay = ReplayPlan(
        client=ModelClient(
            store, run_id, models(agent.profile), replay=await Recording.load(store, origin), env={}
        ),
        fork_at=fork_at,
        schedule=[(None, ran_on)],
        outputs=outputs or {},
        keep=keep,
    )
    finished = await agent.run()
    return finished, await payloads(store, run_id, AgentCompacted)


async def test_a_fork_compacts_where_its_origin_did_until_it_goes_live(
    store: LocalEventStore,
) -> None:
    """The origin ran on a context its calls never filled half of; the fork's profile has a
    smaller one. While its steps replay or keep the recorded answers, no summary falls due that
    the origin did not make. Once live, its own context counts."""
    summary = Compaction(at=0.5, strategy="summary", keep_recent=1)
    big = profile(context_tokens=1_000_000)
    play = Script(say("a"), say("b"), call("bash", command="true"), call("submit", answer="done"))
    origin_agent, origin = await make_agent(store, play, p=big, compaction=summary)
    assert (await origin_agent.run()).reason == "submitted"

    live = Script()
    finished, compacted = await summary_fork(store, origin, live, fork_at=1, keep=True, ran_on=big)
    assert (finished.reason, finished.answer, finished.steps) == ("submitted", "done", 4)
    assert (live.requests, compacted) == ([], [])

    live = Script(say("Said a and b, ran true."), call("submit", answer="live"))
    recorded = {(3, 0): RecordedOutput("stdout:\nran true\nexit code: 0", False)}
    finished, compacted = await summary_fork(
        store, origin, live, fork_at=3, ran_on=big, outputs=recorded
    )
    assert (finished.reason, finished.answer, finished.steps) == ("submitted", "live", 4)
    assert [(c.step, c.to_step) for c in compacted] == [(3, 2)]
    asked = live.requests[0]["messages"][-1]
    assert asked == {"role": "user", "content": prompts.SUMMARY_REQUEST}


async def test_a_summary_the_recording_lacks_sends_a_kept_agent_live(
    store: LocalEventStore,
) -> None:
    """The schedule says the origin ran on a small context, so a summary falls due that the
    recording does not hold. A kept agent then makes it live and goes on live, as it does when
    a step's request is not recorded; while the steps replay, the replay fails as a step's
    does."""
    summary = Compaction(at=0.5, strategy="summary", keep_recent=1)
    big = profile(context_tokens=1_000_000)
    play = Script(say("a"), say("b"), call("bash", command="true"), call("submit", answer="done"))
    origin_agent, origin = await make_agent(store, play, p=big, compaction=summary)
    assert (await origin_agent.run()).reason == "submitted"
    small = big.model_copy(update={"context_tokens": 150})

    live = Script(say("Said a and b."), call("submit", answer="live"))
    finished, compacted = await summary_fork(
        store, origin, live, fork_at=1, keep=True, ran_on=small
    )
    assert (finished.reason, finished.answer, finished.steps) == ("submitted", "live", 3)
    assert [c.summary for c in compacted] == ["Said a and b."]
    assert len(live.requests) == 2

    live = Script()
    finished, _ = await summary_fork(store, origin, live, fork_at=3, ran_on=small)
    assert finished.reason == "error"
    assert (finished.detail or "").startswith("replay of step 3 failed: "), finished.detail
    assert live.requests == []


def summarizing(body: dict[str, Any]) -> dict[str, Any]:
    """The most calls summary compaction makes: each call fills the context, each step runs a
    tool and each summary request gets a summary."""
    full = {"prompt_tokens": 900, "completion_tokens": 10}
    if body["messages"][-1] == {"role": "user", "content": prompts.SUMMARY_REQUEST}:
        return {**say("Steps so far."), "usage": full}
    return {**call("bash", command="true"), "usage": full}


async def test_the_estimate_bounds_the_calls_of_summary_compaction(store: LocalEventStore) -> None:
    p = profile(context_tokens=1000, max_output_tokens=100)
    step = step_usd(p)
    assert step is not None
    for keep in (1, 2, 5):
        compaction = Compaction(at=0.5, strategy="summary", keep_recent=keep)
        for steps in (1, 2, 3, 4, 7, 10):
            budget = Budget(steps=steps, cost_usd=None)
            script = Script(*[summarizing] * 30)
            agent, _ = await make_agent(store, script, p=p, budget=budget, compaction=compaction)
            assert (await agent.run()).reason == "budget_steps"
            ceiling, _ = _agent_ceiling(AgentConfig(budget=budget, compaction=compaction), p)
            assert ceiling == pytest.approx(len(script.requests) * step), (keep, steps)


async def test_the_estimate_bounds_the_calls_of_a_fork_that_compacts(
    store: LocalEventStore,
) -> None:
    from aims.models import Recording

    p = profile(context_tokens=1000, max_output_tokens=100)
    step = step_usd(p)
    assert step is not None
    for keep in (1, 3):
        compaction = Compaction(at=0.5, strategy="summary", keep_recent=keep)
        before = Budget(steps=6, cost_usd=None)
        script = Script(*[summarizing] * 30)
        agent, origin = await make_agent(store, script, p=p, budget=before, compaction=compaction)
        await agent.run()
        # one tool call a step
        recorded = {
            (n, 0): RecordedOutput(str(r.output), r.error)
            for n, r in enumerate(await payloads(store, origin, ToolResult), 1)
        }
        for fork_at in (1, 3, 5):
            for steps in (fork_at, fork_at + 1, 8):
                live = Script(*[summarizing] * 30)
                run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
                budget = Budget(steps=steps, cost_usd=None)
                config = AgentConfig(budget=budget, compaction=compaction)
                recording = await Recording.load(store, origin)
                transport = live.transport()
                fork = Agent(
                    config=config,
                    sandbox_config=SandboxConfig(),
                    instruction="do the task",
                    texts=AgentTexts(),
                    profile=p,
                    client=ModelClient(store, run_id, models(p), transport=transport, env={}),
                    sandbox=cast(AuditedSandbox, FakeSandbox(echo)),
                    store=store,
                    run_id=run_id,
                    tools=load_tools(config.tool_names),
                    run_budget=RunBudget(Limits()),
                    replay=ReplayPlan(
                        client=ModelClient(store, run_id, models(p), replay=recording, env={}),
                        fork_at=fork_at,
                        schedule=[(None, p)],
                        outputs=recorded,
                        budget=before,
                    ),
                )
                assert (await fork.run()).reason == "budget_steps"
                ceiling, _ = _agent_ceiling(config, p, fork_at)
                assert ceiling is not None
                calls = len(live.requests) * step
                where = (keep, fork_at, steps)
                if steps > fork_at:
                    assert ceiling == pytest.approx(calls), where
                else:
                    assert ceiling >= calls, where


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


async def test_a_held_agent_logs_the_point_it_waits_at(store: LocalEventStore) -> None:
    gate = Gate({("after_reply", 1): Verdict("pause"), ("before_tool", 1): Verdict("pause")})
    script = Script(call("bash", command="ls"), call("submit", answer="done"))
    agent, run_id = await make_agent(store, script, sandbox=FakeSandbox(echo), gate=gate)
    await agent.run()

    (step, _) = await payloads(store, run_id, AgentStep)
    (tool, _) = await payloads(store, run_id, ToolCall)
    held = await payloads(store, run_id, AgentHeld)
    assert [(h.kind, h.call_id, h.step) for h in held] == [
        ("after_reply", step.call_id, 1),
        ("before_tool", tool.call_id, 1),
    ]


async def test_a_fork_feeds_recorded_outputs_and_logs_divergence(store: LocalEventStore) -> None:
    script = Script(
        call("bash", command="date"), call("bash", command="ls"), call("submit", answer="x")
    )
    agent, origin = await make_agent(store, script)
    assert (await agent.run()).reason == "submitted"
    from aims.models import Recording

    # one tool call a step
    recorded = {
        (n, 0): RecordedOutput(str(r.output), r.error)
        for n, r in enumerate(await payloads(store, origin, ToolResult), 1)
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


async def test_a_second_cancel_while_the_agent_ends_still_logs_its_end(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Hang(FakeSandbox):
        async def exec(self, command: str, **kw: Any) -> ExecResult:
            await asyncio.sleep(30)
            raise AssertionError("not reached")

    append = store.append

    async def slow(run_id: str, drafts: Any) -> Any:
        if any(isinstance(d.payload, AgentFinished) for d in drafts):
            await asyncio.sleep(0.2)
        return await append(run_id, drafts)

    script = Script(call("bash", command="sleep 30"))
    agent, run_id = await make_agent(store, script, sandbox=Hang(echo))
    agent.killed = "killed by the monitor"
    monkeypatch.setattr(store, "append", slow)
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (finished,) = await payloads(store, run_id, AgentFinished)
    assert (finished.reason, finished.detail) == ("killed", "killed by the monitor")


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


async def test_a_denied_replayed_call_gives_up_its_turn(store: LocalEventStore) -> None:
    gate = Gate({("before_tool", 1): Verdict("deny")})
    script = Script(call("bash", command="rm -rf /work"), call("submit", answer="done"))
    agent, _ = await make_agent(store, script, gate=gate)
    turnstile = Turnstile([10, 20, 30])
    agent.replay = ReplayPlan(
        client=agent.client,
        fork_at=2,
        schedule=[(None, agent.profile)],
        outputs={
            (1, 0): RecordedOutput("x", False),
            (2, 0): RecordedOutput("y", False),
        },
        turnstile=turnstile,
        order={(1, 0): 10, (2, 0): 30},
    )

    async def other() -> None:
        await turnstile.wait(20)
        await turnstile.done(20)

    waiting = asyncio.create_task(other())
    finished = await asyncio.wait_for(agent.run(), 5)
    assert finished.reason == "submitted"
    await asyncio.wait_for(waiting, 1)


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
    # The pause outlasts the wall, so a counted pause would end both agents. The wall leaves
    # room for the store writes it does count, which are slow on a busy CI runner.
    budget = RunBudget(Limits(wall_s=3.0))
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
    await asyncio.sleep(3.3)
    assert budget.paused
    gate.resume(["a", "b"])
    done = await asyncio.gather(*tasks)
    assert [f.reason for f in done] == ["submitted", "submitted"]
    assert all(f.paused_ms >= 3200 for f in done)

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


@pytest.mark.timing
async def test_a_pause_that_begins_after_the_run_wall_does_not_stop_it(
    store: LocalEventStore,
) -> None:
    gate = ControlGate()
    # The wall leaves room for the store writes before the call starts, slow on a busy runner.
    budget = RunBudget(Limits(wall_s=2.0))
    sandbox = Sleepy(5)
    busy, _ = await make_agent(
        store,
        Script(call("bash", command="x")),
        sandbox=sandbox,
        run_budget=budget,
        clock=gate.clock("busy"),
        gate=gate,
        id="busy",
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
    tasks = [asyncio.create_task(agent.run()) for agent in (busy, held)]
    async with asyncio.timeout(5):
        await sandbox.started.wait()
    deadline = budget.deadline()
    assert deadline is not None
    # A busy loop passes the deadline before the wall's timer fires; a pause lands in between.
    time.sleep(max(0.0, deadline - time.monotonic()) + 0.05)  # noqa: ASYNC251
    gate.pause(["busy"])
    ends = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert [(f.reason, f.detail) for f in ends] == [("budget_wall", "run wall limit reached")] * 2


async def test_an_agent_that_starts_paused_after_the_run_wall_ends_at_once(
    store: LocalEventStore,
) -> None:
    gate = ControlGate()
    budget = RunBudget(Limits(wall_s=0.5))
    busy, _ = await make_agent(
        store,
        Script(call("bash", command="x")),
        sandbox=Sleepy(5),
        run_budget=budget,
        clock=gate.clock("busy"),
        gate=gate,
        id="busy",
    )
    late, _ = await make_agent(
        store,
        Script(call("submit", answer="never")),
        run_budget=budget,
        clock=gate.clock("late"),
        gate=gate,
        id="late",
    )
    first = await busy.run()
    assert (first.reason, first.detail) == ("budget_wall", "run wall limit reached")
    gate.pause(["late"])
    finished = await asyncio.wait_for(late.run(), 5)
    assert (finished.reason, finished.detail) == ("budget_wall", "run wall limit reached")
    assert finished.steps == 0


@pytest.mark.timing
async def test_a_pause_that_begins_after_the_agent_wall_does_not_stop_it(
    store: LocalEventStore,
) -> None:
    gate = ControlGate()
    sandbox = Sleepy(5)
    script = Script(call("bash", command="x"), call("submit", answer="ok"))
    agent, _ = await make_agent(
        store,
        script,
        sandbox=sandbox,
        gate=gate,
        clock=gate.clock("agent"),
        budget=Budget(wall_s=2.0),
    )
    task = asyncio.create_task(agent.run())
    async with asyncio.timeout(5):
        await sandbox.started.wait()
    # The wall began before the call did, so this passes its deadline before its timer fires.
    time.sleep(2.05)  # noqa: ASYNC251
    gate.pause(["agent"])
    finished = await asyncio.wait_for(task, 5)
    assert (finished.reason, finished.detail) == ("budget_wall", "agent wall limit reached")


async def test_the_run_wall_stands_still_only_while_an_agent_is_paused_and_the_rest_wait() -> None:
    budget = RunBudget(Limits(wall_s=60))
    a, b = PauseClock(), PauseClock()
    budget.enter("a", a, lambda: None)
    budget.enter("b", b, lambda: None)
    with budget.waiting("b"):
        assert not budget.paused
        a.hold("operator")
        assert budget.paused
        assert budget.deadline() is None
    assert not budget.paused
    with budget.waiting("b"):
        budget.leave("b")
    assert budget.paused
    budget.enter("b", b, lambda: None)
    assert not budget.paused


async def test_a_waiting_agent_counts_the_time_the_run_wall_stood_still_for_it() -> None:
    budget = RunBudget(Limits(wall_s=60))
    a, b = PauseClock(), PauseClock()
    budget.enter("a", a, lambda: None)
    budget.enter("b", b, lambda: None)
    with budget.waiting("b"):
        a.hold("operator")
        since = budget.still_since("b")
        assert since is not None
        assert budget.still_s("b", since + 2) == pytest.approx(2)
        b.hold("operator")
        assert budget.still_since("b") is None
        held = budget.still_s("b")
        b.release("operator")
        assert budget.still_since("b") is not None
        a.release("operator")
    assert budget.still_since("b") is None
    assert held <= budget.still_s("b") < held + 1
    assert budget.still_s("a") == 0


async def test_a_wait_before_the_run_wall_ends_with_it_and_stands_still_with_it() -> None:
    budget = RunBudget(Limits(wall_s=0.2))
    clock = PauseClock()
    clock.hold("operator")
    budget.enter("a", clock, lambda: None)
    task = asyncio.create_task(budget.before_wall(asyncio.Event().wait()))
    await asyncio.sleep(0.4)
    assert not task.done()
    clock.release("operator")
    await asyncio.wait_for(task, 5)

    async def times_out() -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await RunBudget(Limits()).before_wall(times_out())


async def at_the_turnstile(
    store: LocalEventStore, kind: str, budget: RunBudget, gate: ControlGate, own_s: float | None
) -> tuple[Agent | ScriptedAgent, Turnstile]:
    """Agent a of a fork, model-driven or scripted, whose replayed step waits at the turnstile
    for turn 10, another agent's."""
    turnstile = Turnstile([10, 20])
    own = Budget() if own_s is None else Budget(wall_s=own_s)
    if kind == "model":
        script = Script(call("bash", command="x"), call("submit", answer="done"))
        agent, _ = await make_agent(
            store, script, run_budget=budget, clock=gate.clock("a"), gate=gate, id="a", budget=own
        )
        agent.replay = ReplayPlan(
            client=agent.client,
            fork_at=1,
            schedule=[(None, agent.profile)],
            outputs={(1, 0): RecordedOutput("x", False)},
            turnstile=turnstile,
            order={(1, 0): 20},
        )
        return agent, turnstile
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    p = profile()
    scripted = ScriptedAgent(
        config=AgentConfig(id="a", budget=own),
        steps=[ScriptStep(run="echo a"), ScriptStep(run="echo done")],
        sandbox_config=SandboxConfig(),
        instruction="do the task",
        profile=p,
        sandbox=cast(AuditedSandbox, FakeSandbox(echo)),
        store=store,
        run_id=run_id,
        watch=Watch(store, run_id, cast(Scenario, None)),
        run_budget=budget,
        gate=gate,
        clock=gate.clock("a"),
        replay=ReplayPlan(
            client=cast(ModelClient, None),
            fork_at=1,
            schedule=[(None, p)],
            outputs={},
            turnstile=turnstile,
            actions=[20],
        ),
    )
    return scripted, turnstile


@pytest.mark.timing
@pytest.mark.parametrize("scope", ["run", "agent"])
@pytest.mark.parametrize("kind", ["model", "scripted"])
async def test_a_wait_at_the_turnstile_on_a_paused_agent_holds_the_wall_limits(
    store: LocalEventStore, kind: str, scope: str
) -> None:
    gate = ControlGate()
    # The wall leaves room for the store writes before the wait, slow on a busy runner.
    budget = RunBudget(Limits(wall_s=2.0) if scope == "run" else Limits())
    agent, turnstile = await at_the_turnstile(
        store, kind, budget, gate, 2.0 if scope == "agent" else None
    )
    budget.enter("b", gate.clock("b"), lambda: None)
    gate.pause(["b"])
    task = asyncio.create_task(agent.run())
    async with asyncio.timeout(5):
        while budget.still_since("a") is None and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await asyncio.sleep(2.5)
    assert not task.done()
    gate.resume(["b"])
    await turnstile.done(10)
    budget.leave("b")
    finished = await asyncio.wait_for(task, 5)

    end = ("submitted", "done") if kind == "model" else ("no_submit", "the steps finished")
    assert (finished.reason, finished.detail or finished.answer) == end


@pytest.mark.parametrize("kind", ["model", "scripted"])
async def test_a_wait_at_the_turnstile_on_a_running_agent_ends_at_the_run_wall(
    store: LocalEventStore, kind: str
) -> None:
    gate = ControlGate()
    budget = RunBudget(Limits(wall_s=0.5))
    agent, _ = await at_the_turnstile(store, kind, budget, gate, None)
    budget.enter("b", gate.clock("b"), lambda: None)
    finished = await asyncio.wait_for(agent.run(), 5)

    assert (finished.reason, finished.detail) == ("budget_wall", "run wall limit reached")


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
