import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError

from aims.agent.gate import Gate, GateKind, GatePoint, OpenGate, Verdict
from aims.events import (
    AgentCompacted,
    AgentEnd,
    AgentFinished,
    AgentInput,
    AgentStarted,
    AgentStep,
    Draft,
    EventStore,
    InputKind,
    ModelUsage,
    Payload,
    ReplayDivergence,
    ToolResult,
)
from aims.models import (
    Item,
    Message,
    ModelCallError,
    ModelClient,
    Profile,
    Prompt,
    ReplayError,
    Reply,
    ToolOutput,
    ToolRequest,
    Turn,
)
from aims.prompts import AgentTexts
from aims.sandbox import AuditedSandbox
from aims.scenario import AgentConfig, Limits, SandboxConfig
from aims.tools import Tool, ToolContext, ToolOutcome, cap

log = logging.getLogger("aims.agent")

MAX_NUDGES = 3
_CONTEXT = re.compile(
    r"context[ _-]?(length|window|limit)|maximum context|too many (input )?tokens"
    r"|prompt is too long|input is too long|too long for the model|exceeds? the (model'?s? )?"
    r"(context|maximum)|reduce the length",
    re.IGNORECASE,
)


def is_context_error(error: str | None) -> bool:
    """Whether a failed model call says the prompt no longer fits the model's context.

    The patterns follow OpenAI, vLLM and Fireworks wording; what Meta returns is unverified.
    """
    return bool(error) and _CONTEXT.search(error or "") is not None


class AgentKilled(Exception):
    pass


class _End(Exception):
    def __init__(self, reason: AgentEnd, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason: AgentEnd = reason
        self.detail = detail


@dataclass(slots=True)
class Totals:
    """What the steps so far used. A reply with no price leaves `cost_complete` false."""

    steps: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cost_complete: bool = True

    def add(self, reply: Reply, *, step: bool) -> None:
        u = reply.usage
        self.steps += step
        self.input_tokens += u.input_tokens or 0
        self.cached_tokens += u.cached_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.reasoning_tokens += u.reasoning_tokens or 0
        if reply.cost_usd is None:
            self.cost_complete = False
        else:
            self.cost_usd += reply.cost_usd

    def usage(self) -> ModelUsage:
        return ModelUsage(
            input_tokens=self.input_tokens,
            cached_tokens=self.cached_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
        )


class PauseClock:
    """Whether one agent is paused and for how long. A pause lasts while any of its keys is held:
    the operator's or the monitor's pause from the moment it is asked, a hold at a gate point.
    Listeners hear every start and end of a pause."""

    def __init__(self) -> None:
        self._keys: set[str] = set()
        self._since: float | None = None
        self._total = 0.0
        self._listeners: list[Callable[[], None]] = []

    @property
    def paused(self) -> bool:
        return bool(self._keys)

    def hold(self, key: str) -> None:
        if key in self._keys:
            return
        self._keys.add(key)
        if len(self._keys) == 1:
            self._since = time.monotonic()
            self._changed()

    def release(self, key: str) -> None:
        if key not in self._keys:
            return
        self._keys.discard(key)
        if not self._keys and self._since is not None:
            self._total += time.monotonic() - self._since
            self._since = None
            self._changed()

    def paused_s(self, now: float | None = None) -> float:
        """Seconds paused so far, the current pause included."""
        if self._since is None:
            return self._total
        return self._total + (time.monotonic() if now is None else now) - self._since

    def listen(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Call `fn` when a pause starts or ends. Returns a function that stops it."""
        self._listeners.append(fn)

        def remove() -> None:
            if fn in self._listeners:
                self._listeners.remove(fn)

        return remove

    def _changed(self) -> None:
        for fn in list(self._listeners):
            fn()


class RunBudget:
    """The run's limits, charged by every agent of the run. The wall limit does not count the
    stretches in which every running agent is paused."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.totals = Totals()
        self.start = time.monotonic()
        self._agents: dict[str, tuple[PauseClock, Callable[[], None], Callable[[], None]]] = {}
        self._since: float | None = None
        self._paused = 0.0

    def enter(self, agent_id: str, clock: PauseClock, on_change: Callable[[], None]) -> None:
        """Count `agent_id` as running; `on_change` hears when any running agent's pause starts
        or ends, which may move the run's deadline."""
        self._agents[agent_id] = (clock, on_change, clock.listen(self._changed))
        self._changed()

    def leave(self, agent_id: str) -> None:
        entry = self._agents.pop(agent_id, None)
        if entry is not None:
            entry[2]()
            self._changed()

    @property
    def paused(self) -> bool:
        """Whether every running agent is paused."""
        return self._since is not None

    def paused_s(self, now: float | None = None) -> float:
        if self._since is None:
            return self._paused
        return self._paused + (time.monotonic() if now is None else now) - self._since

    def deadline(self) -> float | None:
        """When the run's wall limit ends on the `time.monotonic` clock; None without a limit or
        while every running agent is paused."""
        if self.limits.wall_s is None or self._since is not None:
            return None
        return self.start + self.limits.wall_s + self._paused

    def _changed(self) -> None:
        now = time.monotonic()
        stopped = bool(self._agents) and all(c.paused for c, _, _ in self._agents.values())
        if stopped and self._since is None:
            self._since = now
        elif not stopped and self._since is not None:
            self._paused += now - self._since
            self._since = None
        for _, fn, _ in list(self._agents.values()):
            fn()

    def exceeded(self) -> tuple[AgentEnd, str] | None:
        return _over(
            self.totals, self.limits.steps, self.limits.output_tokens, self.limits.cost_usd, "run"
        )


def _over(
    t: Totals, steps: int | None, tokens: int | None, cost: float | None, scope: str
) -> tuple[AgentEnd, str] | None:
    if steps is not None and t.steps >= steps:
        return "budget_steps", f"{scope} limit of {steps} steps reached"
    if tokens is not None and t.output_tokens >= tokens:
        return "budget_output_tokens", f"{scope} limit of {tokens} output tokens reached"
    if cost is not None and t.cost_usd >= cost:
        return "budget_cost", f"{scope} limit of {cost:g} USD reached"
    return None


@dataclass(frozen=True, slots=True)
class RecordedOutput:
    output: str
    error: bool


@dataclass(slots=True)
class ReplayPlan:
    """How a fork replays its origin: model answers for steps 1 to `fork_at` come from
    `client`; each of their tool calls runs again here, and the model is given the output
    recorded in `outputs`. A replayed step uses the profile the origin used for it, from
    `schedule` (each profile with the last step it ran, None for no end), so its request
    matches; later steps use the agent's own profile. `injects` are the messages the origin was
    given, by step."""

    def profile_for(self, step: int) -> Profile:
        for until, profile in self.schedule:
            if until is None or step <= until:
                return profile
        return self.schedule[-1][1]

    client: ModelClient
    fork_at: int
    schedule: list[tuple[int | None, Profile]]
    outputs: dict[str, RecordedOutput]
    injects: dict[int, list[str]] = field(default_factory=dict)
    strict: bool = False
    inject: str | None = None


class Agent:
    """One agent's loop: call the model, run the tools it asks for, until it submits or a
    budget, the gate or an error ends it. Every step is logged as events."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        sandbox_config: SandboxConfig,
        instruction: str,
        texts: AgentTexts,
        profile: Profile,
        client: ModelClient,
        sandbox: AuditedSandbox,
        store: EventStore,
        run_id: str,
        tools: Sequence[Tool],
        run_budget: RunBudget,
        gate: Gate | None = None,
        replay: ReplayPlan | None = None,
        clock: PauseClock | None = None,
    ) -> None:
        self.config = config
        self.id = config.id
        self.sandbox_config = sandbox_config
        self.instruction = instruction
        self.texts = texts
        self.profile = profile
        self.client = client
        self.sandbox = sandbox
        self.store = store
        self.run_id = run_id
        self.tools = {t.name: t for t in tools}
        self.specs = [t.spec(sandbox_config) for t in tools]
        self.run_budget = run_budget
        self.gate = gate or OpenGate()
        self.replay = replay
        self.items: list[Item] = []
        self.totals = Totals()
        self.marks: dict[int, int] = {}
        self.compacted_through = 0
        self.last_tokens: int | None = None
        self.warned: set[str] = set()
        self.pending: list[tuple[InputKind, str]] = []
        self.answer: str | None = None
        self.killed: str | None = None
        self.clock = clock or PauseClock()
        self.done = False
        self._cost_warned = False
        self._start = 0.0
        self._paused_base = 0.0
        self._was_paused = False
        self._wall: asyncio.Timeout | None = None
        self._scope = ""
        self._open: int | None = None
        self._open_profile = profile

    @property
    def steps(self) -> int:
        return self.totals.steps

    async def run(self) -> AgentFinished:
        start = self._start = time.monotonic()
        self._paused_base = self.clock.paused_s(start)
        self._was_paused = self.clock.paused
        await self._emit(
            AgentStarted(
                profile=self.profile.name,
                model=self.profile.model,
                backend=self.profile.backend,
                tools=list(self.tools),
                system=await self.store.content(self.run_id, self.texts.system),
                budget=self.config.budget.model_dump(mode="json"),
                compaction=None
                if self.config.compaction is None
                else self.config.compaction.model_dump(mode="json"),
                fork_at=None if self.replay is None else self.replay.fork_at,
            )
        )
        reason: AgentEnd = "error"
        detail: str | None = None
        answer: str | None = None
        self.run_budget.enter(self.id, self.clock, self._clock_changed)
        deadline, self._scope = self._deadline()
        wall = asyncio.timeout_at(deadline)
        try:
            async with wall:
                self._wall = wall
                try:
                    answer = await self._loop()
                finally:
                    self._wall = None
            reason = "submitted"
        except TimeoutError:
            if not wall.expired():
                log.exception("agent %s failed", self.id)
                reason, detail = "error", "TimeoutError"
            else:
                reason, detail = "budget_wall", f"{self._scope} wall limit reached"
        except _End as e:
            reason, detail = e.reason, e.detail
        except AgentKilled as e:
            reason, detail = "killed", str(e) or None
        except asyncio.CancelledError:
            if self.killed is not None:
                await asyncio.shield(self._finish(start, "killed", self.killed, None))
            else:
                await asyncio.shield(self._finish(start, "cancelled", None, None))
            raise
        except Exception as e:
            log.exception("agent %s failed", self.id)
            reason, detail = "error", f"{type(e).__name__}: {e}"
        return await asyncio.shield(self._finish(start, reason, detail, answer))

    def _paused_s(self, now: float | None = None) -> float:
        return self.clock.paused_s(now) - self._paused_base

    def _deadline(self) -> tuple[float | None, str]:
        """When the wall limit ends on the loop's clock, and whose limit it is. Paused time is
        added on; while the agent is paused only the run's limit runs."""
        now = time.monotonic()
        own = self.config.budget.wall_s
        candidates: list[tuple[float, str]] = []
        if own is not None and not self.clock.paused:
            candidates.append((self._start + own + self._paused_s(now), "agent"))
        if (run := self.run_budget.deadline()) is not None:
            candidates.append((run, "run"))
        if not candidates:
            return None, self._scope
        at, scope = min(candidates)
        return asyncio.get_running_loop().time() + (at - now), scope

    def _clock_changed(self) -> None:
        paused = self.clock.paused
        if self._was_paused and not paused and self.texts.notify_on_resume:
            self.pending.append(("inject", self.texts.notify_on_resume))
        self._was_paused = paused
        wall = self._wall
        if wall is None or wall.expired():
            return
        when, self._scope = self._deadline()
        wall.reschedule(when)

    def next_prompt(self) -> tuple[Profile, Prompt]:
        """The profile and prompt of the agent's next step: the step whose tool calls are still
        to run, else the model call it makes next."""
        if self._open is not None:
            return self._open_profile, Prompt(items=self.items[: self._open], tools=self.specs)
        pending = [Message(role="user", content=text) for _, text in self.pending]
        return self._model()[1], Prompt(items=[*self.items, *pending], tools=self.specs)

    async def _finish(
        self, start: float, reason: AgentEnd, detail: str | None, answer: str | None
    ) -> AgentFinished:
        self.answer = answer
        self.done = True
        now = time.monotonic()
        self.run_budget.leave(self.id)
        finished = AgentFinished(
            reason=reason,
            detail=detail,
            answer=None if answer is None else await self.store.content(self.run_id, answer),
            steps=self.steps,
            usage=self.totals.usage(),
            cost_usd=round(self.totals.cost_usd, 10),
            cost_complete=self.totals.cost_complete,
            wall_ms=int((now - start) * 1000),
            paused_ms=int(self._paused_s(now) * 1000),
        )
        await self._emit(finished)
        return finished

    async def _loop(self) -> str:
        self.items = [
            Message(role="system", content=self.texts.system),
            Message(role="user", content=self.instruction),
        ]
        await self._input("instruction", self.instruction, add=False)
        nudges = 0
        while True:
            await self._take_inputs()
            if (hit := self._over_budget()) is not None:
                raise _End(*hit)
            await self._compact()
            await self._gate("before_model")
            await self._take_inputs()
            reply = await self._call()
            calls = reply.tool_calls if reply.status == "completed" else []
            self._open = self.marks[self.steps] if calls else None
            await self._gate("after_reply", reply.call_id)
            if not calls:
                nudges += 1
                if nudges >= MAX_NUDGES:
                    raise _End("no_submit", f"{MAX_NUDGES} replies in a row without a tool call")
                await self._input("continue", self.texts.nudge)
                continue
            nudges = 0
            for i, call in enumerate(calls):
                if (await self._gate("before_tool", call.id)).action == "deny":
                    await self._result(call, self.texts.denied, True, denied=True)
                    continue
                outcome = await self._tool(call)
                await self._gate("after_tool", call.id)
                if outcome.failed:
                    raise _End("sandbox_failed", outcome.output)
                if outcome.answer is not None:
                    for skipped in calls[i + 1 :]:
                        await self._result(skipped, "Not run: the task was submitted.", True)
                    return outcome.answer
            self._open = None
            self._warn_budget()

    async def _take_inputs(self) -> None:
        if self.replay is not None:
            for text in self.replay.injects.pop(self.steps, []):
                await self._input("inject", text)
            if self.steps == self.replay.fork_at and self.replay.inject is not None:
                text, self.replay.inject = self.replay.inject, None
                await self._input("inject", text)
        pending, self.pending = self.pending, []
        for kind, text in pending:
            await self._input(kind, text)

    async def _input(self, kind: InputKind, text: str, *, add: bool = True) -> None:
        if add:
            self.items.append(Message(role="user", content=text))
        content = await self.store.content(self.run_id, text)
        await self._emit(AgentInput(kind=kind, step=self.steps, text=content))

    def _replaying(self) -> bool:
        """Whether the next model call is served from the recording of a fork's origin."""
        return self.replay is not None and self.steps < self.replay.fork_at

    def _model(self) -> tuple[ModelClient, Profile]:
        if self.replay is not None and self._replaying():
            return self.replay.client, self.replay.profile_for(self.steps + 1)
        return self.client, self.profile

    async def _call(self) -> Reply:
        client, profile = self._model()
        prompt = Prompt(items=self.items, tools=self.specs)
        start = time.monotonic()
        self._open_profile = profile
        try:
            reply = await client.call(profile, prompt, agent_id=self.id)
        except ModelCallError as e:
            self._charge(e.reply, step=False)
            error = e.reply.error
            if is_context_error(error):
                raise _End("context_exhausted", error) from None
            raise _End("model_error", error) from None
        except ReplayError as e:
            raise _End("error", f"replay of step {self.steps + 1} failed: {e}") from None
        self._charge(reply, step=True)
        u = reply.usage
        if u.input_tokens is not None:
            self.last_tokens = u.input_tokens + (u.output_tokens or 0)
        self.marks[self.steps] = len(self.items)
        turn = reply.turn if reply.status == "completed" else Turn(text=reply.text)
        self.items.append(turn)
        await self._emit(
            AgentStep(
                n=self.steps,
                call_id=reply.call_id,
                status=reply.status,
                source="replay" if client is not self.client else "live",
                tool_calls=[c.id for c in reply.tool_calls] if reply.status == "completed" else [],
                usage=reply.usage,
                cost_usd=reply.cost_usd,
                total=self.totals.usage(),
                total_cost_usd=round(self.totals.cost_usd, 10),
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        )
        return reply

    def _charge(self, reply: Reply, *, step: bool) -> None:
        self.totals.add(reply, step=step)
        self.run_budget.totals.add(reply, step=step)
        if reply.cost_usd is None and not self._cost_warned:
            caps = self.config.budget.cost_usd or self.run_budget.limits.cost_usd
            if caps is not None:
                self._cost_warned = True
                log.warning(
                    "profile %s has no price, so the cost limit of agent %s is not enforced",
                    self.profile.name,
                    self.id,
                )

    def _over_budget(self) -> tuple[AgentEnd, str] | None:
        b = self.config.budget
        own = _over(self.totals, b.steps, b.output_tokens, b.cost_usd, "agent")
        return own or self.run_budget.exceeded()

    def _warn_budget(self) -> None:
        b = self.config.budget
        if b.warn_at is None:
            return
        used = {
            "steps": (self.totals.steps, b.steps),
            "output_tokens": (self.totals.output_tokens, b.output_tokens),
            "cost_usd": (self.totals.cost_usd, b.cost_usd),
        }
        for name, (value, limit) in used.items():
            if limit is None or name in self.warned or value < b.warn_at * limit:
                continue
            self.warned.add(name)
            text = self.texts.budget(name).format(used=value, limit=limit)
            self.pending.append(("budget_warning", text))

    async def _gate(self, kind: GateKind, call_id: str | None = None) -> Verdict:
        point = GatePoint(kind, self.run_id, self.id, self.steps, call_id)
        verdict = await self.gate.check(point)
        if verdict.action == "pause":
            self.clock.hold("gate")
            try:
                while verdict.action == "pause":
                    await self.sandbox.sandbox.pause()
                    try:
                        verdict = await self.gate.held(point)
                    finally:
                        await self.sandbox.sandbox.resume()
            finally:
                self.clock.release("gate")
        if verdict.action == "kill":
            raise AgentKilled(verdict.reason or "killed by the gate")
        if verdict.action == "inject" and verdict.text:
            self.pending.append(("inject", verdict.text))
        return verdict

    async def _tool(self, call: ToolRequest) -> ToolOutcome:
        replayed = self.replay is not None and self.steps <= self.replay.fork_at
        ctx = ToolContext(
            sandbox=self.sandbox,
            store=self.store,
            run_id=self.run_id,
            agent_id=self.id,
            call_id=call.id,
            step=self.steps,
            config=self.sandbox_config,
            gate=self.gate,
            phase="replay" if replayed else "agent",
        )
        outcome = await self._run_tool(ctx, call)
        text, truncated = cap(outcome.output.encode())
        error = outcome.error
        if replayed:
            assert self.replay is not None
            recorded = self.replay.outputs.get(call.id)
            if recorded is None:
                raise _End("error", f"the origin has no output for tool call {call.id}")
            if (recorded.output, recorded.error) != (text, error):
                await self._diverged(call, recorded, text, error)
            text, error = recorded.output, recorded.error
        await self._result(
            call,
            text,
            error,
            output_bytes=len(outcome.output.encode()),
            truncated=truncated,
            replayed=replayed,
        )
        return outcome

    async def _run_tool(self, ctx: ToolContext, call: ToolRequest) -> ToolOutcome:
        tool = self.tools.get(call.name)
        if tool is None:
            known = ", ".join(self.tools)
            return ToolOutcome(f"There is no tool named {call.name!r}. Tools: {known}.", True)
        try:
            raw = json.loads(call.arguments) if call.arguments.strip() else {}
        except ValueError as e:
            return ToolOutcome(f"The arguments are not valid JSON: {e}", error=True)
        if not isinstance(raw, dict):
            return ToolOutcome("The arguments must be a JSON object.", error=True)
        try:
            args = tool.args.model_validate(raw)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
                for err in e.errors(include_url=False)
            )
            return ToolOutcome(f"Invalid arguments: {problems}", error=True)
        try:
            return await tool.run(ctx, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("tool %s failed", call.name)
            return ToolOutcome(f"The tool failed: {type(e).__name__}: {e}", error=True)

    async def _result(
        self,
        call: ToolRequest,
        text: str,
        error: bool,
        *,
        output_bytes: int | None = None,
        truncated: bool = False,
        replayed: bool = False,
        denied: bool = False,
    ) -> None:
        self.items.append(ToolOutput(call_id=call.id, output=text))
        await self._emit(
            ToolResult(
                call_id=call.id,
                output=await self.store.content(self.run_id, text),
                error=error,
                output_bytes=output_bytes,
                truncated=truncated,
                replayed=replayed,
                denied=denied,
            )
        )

    async def _diverged(
        self, call: ToolRequest, recorded: RecordedOutput, live: str, error: bool
    ) -> None:
        line = None
        a, b = recorded.output.split("\n"), live.split("\n")
        for n, (x, y) in enumerate(zip(a, b, strict=False), 1):
            if x != y:
                line = n
                break
        else:
            if len(a) != len(b):
                line = min(len(a), len(b)) + 1
        await self._emit(
            ReplayDivergence(
                step=self.steps,
                call_id=call.id,
                tool=call.name,
                recorded=await self.store.content(self.run_id, recorded.output),
                live=await self.store.content(self.run_id, live),
                recorded_error=recorded.error,
                live_error=error,
                line=line,
            )
        )
        assert self.replay is not None
        if self.replay.strict:
            raise _End("divergence", f"tool call {call.id} at step {self.steps} diverged")

    async def _compact(self) -> None:
        c = self.config.compaction
        context = self.profile.context_tokens
        if c is None or context is None or self.last_tokens is None:
            return
        if self.last_tokens < c.at * context:
            return
        keep = self.steps - c.keep_recent + 1
        if keep <= self.compacted_through + 1 or keep not in self.marks:
            return
        before = self.items
        bytes_before = _size(before)
        call_id = summary = None
        if c.strategy == "trim":
            cut = self.marks[keep]
            start = self.marks.get(self.compacted_through + 1, 2)
            items = list(before)
            changed = False
            for i in range(start, cut):
                item = items[i]
                if isinstance(item, ToolOutput) and item.output != self.texts.trimmed:
                    items[i] = ToolOutput(call_id=item.call_id, output=self.texts.trimmed)
                    changed = True
            if not changed:
                return
        else:
            summarized = await self._summarize(keep)
            if summarized is None:
                return
            items, call_id, summary = summarized
        from_step = self.compacted_through + 1
        self.compacted_through = keep - 1
        self.items = items
        bytes_after = _size(items)
        await self._emit(
            AgentCompacted(
                strategy=c.strategy,
                step=self.steps,
                from_step=from_step,
                to_step=keep - 1,
                items_before=len(before),
                items_after=len(items),
                bytes_before=bytes_before,
                bytes_after=bytes_after,
                tokens_before=self.last_tokens,
                tokens_after_est=int(self.last_tokens * bytes_after / max(1, bytes_before)),
                call_id=call_id,
                summary=None if summary is None else await self.store.content(self.run_id, summary),
            )
        )
        self.last_tokens = None

    async def _summarize(self, keep: int) -> tuple[list[Item], str, str] | None:
        cut = self.marks[keep]
        client, profile = self._model()
        request = Message(role="user", content=self.texts.summary_request)
        prompt = Prompt(items=[*self.items[:cut], request], tools=self.specs)
        try:
            reply = await client.call(profile, prompt, agent_id=self.id)
        except ModelCallError as e:
            self._charge(e.reply, step=False)
            log.warning("agent %s: summary call failed: %s", self.id, e)
            return None
        self._charge(reply, step=False)
        text = reply.text.strip()
        if not text:
            log.warning("agent %s: the summary call gave no text", self.id)
            return None
        summary = Message(role="user", content=self.texts.summary_prefix + text)
        head = [*self.items[:2], summary]
        shift = len(head) - cut
        self.marks = {n: i + shift for n, i in self.marks.items() if n >= keep}
        return [*head, *self.items[cut:]], reply.call_id, text

    async def _emit(self, payload: Payload) -> None:
        await self.store.append(self.run_id, [Draft(payload, self.id)])


def _size(items: Sequence[Item]) -> int:
    return sum(len(item.model_dump_json()) for item in items)
