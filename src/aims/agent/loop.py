import asyncio
import contextlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from aims.agent.gate import Gate, GateKind, GatePoint, OpenGate, Verdict
from aims.agent.turnstile import Turnstile
from aims.aio import settle
from aims.events import (
    MAX_INT,
    AgentCompacted,
    AgentEnd,
    AgentFinished,
    AgentHeld,
    AgentInput,
    AgentStarted,
    AgentStep,
    Draft,
    EventStore,
    InputKind,
    ModelUsage,
    Payload,
    ReplayDivergence,
    RunCorrupt,
    ToolResult,
    logged_events,
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
    SpendCap,
    ToolOutput,
    ToolRequest,
    Turn,
)
from aims.models.types import CHARS_PER_TOKEN
from aims.prompts import AgentTexts
from aims.sandbox import AuditedSandbox
from aims.scenario import AgentConfig, Budget, Limits, SandboxConfig
from aims.text import scrub
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

    The patterns follow the wording of OpenAI, vLLM and other OpenAI-compatible servers; a
    server that words it otherwise is not caught.
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
    """What the steps so far used. A reply with no price leaves `cost_complete` false.
    `unseen_usd` is what failed attempts that may have been billed with no cost reported count
    at, and `sized_usd` what answers that came with no token counts count at by their size,
    both against the cost caps only."""

    steps: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    unseen_usd: float = 0.0
    sized_usd: float = 0.0
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
        self.unseen_usd += reply.unseen_usd
        self.sized_usd += reply.sized_usd

    @property
    def counted_usd(self) -> float:
        """What the cost caps count: the cost, what failed attempts with no cost reported and
        answers with no token counts count at."""
        return self.cost_usd + self.unseen_usd + self.sized_usd

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

    @property
    def since(self) -> float | None:
        """When the current pause began on the `time.monotonic` clock; None while not paused."""
        return self._since

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
    stretches in which a running agent is paused and every other is paused or waits on another
    agent's event. A stretch that begins once the limit has ended does not stop it, so an agent
    that joins or pauses late ends at once. A waiting agent's own wall limit stands still while
    the run's does (`still_s`). `owed` holds what the failed attempts of each agent's call still
    being tried may have cost, which the agent charges once the call ends."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.totals = Totals()
        self.owed: dict[str, float] = {}
        self.start = time.monotonic()
        self._agents: dict[str, tuple[PauseClock, Callable[[], None], Callable[[], None]]] = {}
        self._waiting: set[str] = set()
        self._still: dict[str, float] = {}
        self._still_since: dict[str, float] = {}
        self._listeners: list[Callable[[], None]] = []
        self._since: float | None = None
        self._paused = 0.0

    def enter(self, agent_id: str, clock: PauseClock, on_change: Callable[[], None]) -> None:
        """Count `agent_id` as running; `on_change` hears when any running agent's pause starts
        or ends, which may move the run's deadline."""
        self._agents[agent_id] = (clock, on_change, clock.listen(self._changed))
        self._still[agent_id] = 0.0
        self._changed()

    def owed_by_others(self, agent_id: str) -> float:
        """What the calls still being tried of the agents other than `agent_id` may have cost."""
        return sum(v for k, v in self.owed.items() if k != agent_id)

    def leave(self, agent_id: str) -> None:
        entry = self._agents.pop(agent_id, None)
        self._waiting.discard(agent_id)
        self._still_since.pop(agent_id, None)
        if entry is not None:
            entry[2]()
            self._changed()

    @contextlib.contextmanager
    def waiting(self, agent_id: str) -> Generator[None]:
        """Count running agent `agent_id` as waiting on another agent's event in the block."""
        self._waiting.add(agent_id)
        self._changed()
        try:
            yield
        finally:
            self._waiting.discard(agent_id)
            self._changed()

    def still_since(self, agent_id: str) -> float | None:
        """When the current stretch of `still_s` of `agent_id` began; None outside one."""
        return self._still_since.get(agent_id)

    def still_s(self, agent_id: str, now: float | None = None) -> float:
        """Seconds the run's wall limit stood still while running agent `agent_id` waited on
        another agent's event, not paused itself, the current stretch included."""
        total = self._still.get(agent_id, 0.0)
        since = self._still_since.get(agent_id)
        if since is None:
            return total
        return total + (time.monotonic() if now is None else now) - since

    def listen(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Call `fn` whenever the run's deadline may move. Returns a function that stops it."""
        self._listeners.append(fn)

        def remove() -> None:
            if fn in self._listeners:
                self._listeners.remove(fn)

        return remove

    async def before_wall(self, aw: Awaitable[object]) -> None:
        """Wait for `aw`, but stop waiting once the run's wall limit ends."""

        def when() -> float | None:
            end = self.deadline()
            return None if end is None else on_loop(end)

        timeout = asyncio.timeout_at(when())

        def changed() -> None:
            if not timeout.expired():
                timeout.reschedule(when())

        try:
            async with timeout:
                remove = self.listen(changed)
                try:
                    await aw
                finally:
                    remove()
        except TimeoutError:
            if not timeout.expired():
                raise

    @property
    def paused(self) -> bool:
        """Whether the wall limit stands still: a running agent is paused and every other is
        paused or waits on another agent's event."""
        return self._since is not None

    def paused_s(self, now: float | None = None) -> float:
        if self._since is None:
            return self._paused
        return self._paused + (time.monotonic() if now is None else now) - self._since

    def deadline(self) -> float | None:
        """When the run's wall limit ends on the `time.monotonic` clock; None without a limit or
        while every running agent is paused, unless the limit ended before that pause began."""
        if self.limits.wall_s is None:
            return None
        end = self.start + self.limits.wall_s + self._paused
        if self._since is not None and end > self._since:
            return None
        return end

    def _changed(self) -> None:
        now = time.monotonic()
        clocks = [(k, c) for k, (c, _, _) in self._agents.items()]
        stopped = any(c.paused for _, c in clocks) and all(
            c.paused or k in self._waiting for k, c in clocks
        )
        if stopped and self._since is None:
            self._since = now
        elif not stopped and self._since is not None:
            self._paused += now - self._since
            self._since = None
        for k, c in clocks:
            still = stopped and k in self._waiting and not c.paused
            since = self._still_since.get(k)
            if still and since is None:
                self._still_since[k] = now
            elif not still and since is not None:
                self._still[k] = self._still.get(k, 0.0) + now - since
                del self._still_since[k]
        for _, fn, _ in list(self._agents.values()):
            fn()
        for fn in list(self._listeners):
            fn()


def wall_deadline(
    run_budget: RunBudget,
    agent_id: str,
    clock: PauseClock,
    start: float,
    paused_base: float,
    own_s: float | None,
) -> tuple[float, str] | None:
    """When the wall limit of running agent `agent_id`, which began at `start`, ends on the
    running loop's clock, and whose limit it is: "agent" for its own `own_s`, "run" for the
    run's. Its own limit adds the time paused on `clock` since `start`, when `clock` had
    `paused_base`, and the run's `still_s` for it. While it is paused or still, only the run's
    limit runs, unless its own ended before that stretch began. None while neither runs."""
    candidates: list[tuple[float, str]] = []
    if own_s is not None:
        since = clock.since
        if since is None:
            since = run_budget.still_since(agent_id)
        held = clock.paused_s(since) - paused_base + run_budget.still_s(agent_id, since)
        end = start + own_s + held
        if since is None or end <= since:
            candidates.append((end, "agent"))
    if (run := run_budget.deadline()) is not None:
        candidates.append((run, "run"))
    if not candidates:
        return None
    at, scope = min(candidates)
    return on_loop(at), scope


def on_loop(at: float) -> float:
    """`at` on the `time.monotonic` clock as a time of the running event loop."""
    return asyncio.get_running_loop().time() + (at - time.monotonic())


def _over(
    t: Totals,
    steps: int | None,
    tokens: int | None,
    cost: float | None,
    scope: str,
    owed: float = 0.0,
) -> tuple[AgentEnd, str] | None:
    """Which of the caps `t` reached, with `owed`, what other agents' calls still being tried
    may have cost, counted against the cost cap."""
    if steps is not None and t.steps >= steps:
        return "budget_steps", f"{scope} limit of {steps} steps reached"
    if tokens is not None and t.output_tokens >= tokens:
        return "budget_output_tokens", f"{scope} limit of {tokens} output tokens reached"
    if cost is not None and t.counted_usd + owed >= cost:
        return "budget_cost", f"{scope} limit of {cost:g} USD reached" + _counted(t, owed)
    return None


def _counted(t: Totals, owed: float = 0.0) -> str:
    """What the cost caps count beside the cost, for the text of a stop."""
    detail = ""
    if t.unseen_usd:
        detail += f"; failed calls with no cost reported may add ${t.unseen_usd:.4f}"
    if t.sized_usd:
        detail += f"; answers with no token counts count at ${t.sized_usd:.4f}"
    if owed:
        detail += f"; calls of other agents still being tried may have cost ${owed:.4f}"
    return detail


@dataclass(frozen=True, slots=True)
class RecordedOutput:
    output: str
    error: bool


@dataclass(frozen=True, slots=True)
class ChannelVersions:
    """What a tool call met of the run's channels in the origin, at the origin's event `seq`:
    the last message id each board it read showed (`reads`), and the sha256 each shared file
    its exec changed was left at, None for removed (`writes`, with each path's channel in
    `channels`)."""

    seq: int
    reads: dict[str, int] = field(default_factory=dict)
    writes: dict[str, str | None] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ReplayPlan:
    """How a fork replays its origin: model answers for steps 1 to `fork_at` come from
    `client`; each of their tool calls runs again here, and the model is given the output
    recorded in `outputs`. `outputs`, `order` and `versions` hold each tool call by its step
    and call id. A replayed step uses the profile the origin used for it, from
    `schedule` (each profile with the last step it ran, None for no end), so its request
    matches; later steps use the agent's own profile. `injects` are the messages the origin was
    given, by step.

    A fork of several agents runs the replayed tool calls of every agent in the origin's
    order: each waits at `turnstile` for its seq in `order`, and what it met of the channels is
    checked against `versions`. With `keep`, the agent goes on with recorded answers after
    `fork_at` until a request of its own is not in the recording, then goes live. `actions`
    are the origin seqs of a scripted agent's steps. `budget` and `limits` are the caps the
    origin ran under, which hold while its steps replay so the replayed requests match; the
    fork's own caps hold from the cut."""

    def profile_for(self, step: int) -> Profile:
        for until, profile in self.schedule:
            if until is None or step <= until:
                return profile
        return self.schedule[-1][1]

    client: ModelClient
    fork_at: int
    schedule: list[tuple[int | None, Profile]]
    outputs: dict[tuple[int, str], RecordedOutput]
    injects: dict[int, list[str]] = field(default_factory=dict)
    strict: bool = False
    inject: str | None = None
    turnstile: Turnstile | None = None
    order: dict[tuple[int, str], int] = field(default_factory=dict)
    versions: dict[tuple[int, str], ChannelVersions] = field(default_factory=dict)
    keep: bool = False
    actions: list[int] = field(default_factory=list)
    budget: Budget | None = None
    limits: Limits | None = None


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
        self.warned: dict[str, float] = {}
        """The limit each cap was warned at: a fork whose caps differ from its origin's warns
        again at the new limit."""
        self.pending: list[tuple[InputKind, str]] = []
        self.answer: str | None = None
        self._left_recording = False
        self._replayed = False
        """Whether the last reply came from the recording of a fork's origin."""
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
        try:
            # Logged whole even when a cancel lands meanwhile, so the agent's end follows it.
            await settle(self._log_start())
        except asyncio.CancelledError:
            await self._end_cancelled(start)
            raise
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
            await self._end_cancelled(start)
            raise
        except Exception as e:
            log.exception("agent %s failed", self.id)
            reason, detail = "error", f"{type(e).__name__}: {e}"
        return await self._end(start, reason, detail, answer)

    async def _log_start(self) -> None:
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

    async def _end_cancelled(self, start: float) -> None:
        """Log the end of an agent cancelled while it ran, killed when a kill cancelled it."""
        if self.killed is not None:
            await self._end(start, "killed", self.killed, None)
        else:
            await self._end(start, "cancelled", None, None)

    async def _end(
        self, start: float, reason: AgentEnd, detail: str | None, answer: str | None
    ) -> AgentFinished:
        """`_finish` to its end however often the caller is cancelled meanwhile. When it
        raises, the agent ends as an error with no answer, so its end is still logged."""
        try:
            return await settle(self._finish(start, reason, detail, answer))
        except Exception as e:
            log.exception("agent %s could not log its end", self.id)
            return await settle(self._finish(start, "error", f"{type(e).__name__}: {e}", None))

    def _paused_s(self, now: float | None = None) -> float:
        return self.clock.paused_s(now) - self._paused_base

    def _deadline(self) -> tuple[float | None, str]:
        """When the wall limit ends on the loop's clock, and whose limit it is."""
        own = self.config.budget.wall_s
        found = wall_deadline(
            self.run_budget, self.id, self.clock, self._start, self._paused_base, own
        )
        return (None, self._scope) if found is None else found

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
        if self.replay is not None and self.replay.turnstile is not None:
            await self.replay.turnstile.drop(self.replay.order.values())
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
        live = False
        while True:
            await self._take_inputs()
            if (hit := self._over_budget()) is not None:
                raise _End(*hit)
            await self._compact()
            await self._gate("before_model")
            await self._take_inputs()
            reply = await self._call()
            if not live and not self._replayed:
                # A fork's agent counts its replies without a tool call from its first live one.
                live, nudges = True, 0
            calls = reply.tool_calls if reply.status == "completed" else []
            self._open = self.marks[self.steps] if calls else None
            await self._gate("after_reply", reply.call_id, tuple(c.id for c in calls))
            if not calls:
                nudges += 1
                # A replayed reply ends the agent only while it keeps the origin's answers.
                if nudges >= MAX_NUDGES and (live or self._kept()):
                    raise _End("no_submit", f"{MAX_NUDGES} replies in a row without a tool call")
                await self._input("continue", self.texts.nudge)
                continue
            nudges = 0
            for i, call in enumerate(calls):
                if (await self._gate("before_tool", call.id)).action == "deny":
                    await self._pass_turn(call)
                    await self._result(call, self.texts.denied, True, denied=True)
                    continue
                outcome = await self._tool(call)
                await self._gate("after_tool", call.id)
                if outcome.failed:
                    raise _End("sandbox_failed", outcome.output)
                if outcome.answer is not None:
                    for skipped in calls[i + 1 :]:
                        await self._pass_turn(skipped)
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

    def _kept(self) -> bool:
        """Whether a kept agent still answers from the recording past its fork step."""
        return self.replay is not None and self.replay.keep and not self._left_recording

    def _model(self) -> tuple[ModelClient, Profile]:
        if self.replay is not None and (self._replaying() or self._kept()):
            return self.replay.client, self.replay.profile_for(self.steps + 1)
        return self.client, self.profile

    async def _call(self) -> Reply:
        client, profile = self._model()
        prompt = Prompt(items=self.items, tools=self.specs)
        start = time.monotonic()
        self._open_profile = profile
        try:
            try:
                reply = await client.call(
                    profile, prompt, agent_id=self.id, cap=self._cap(profile, prompt)
                )
            except ReplayError:
                if self._replaying() or not self._kept():
                    raise
                self._left_recording = True
                client, profile = self._model()
                self._open_profile = profile
                reply = await client.call(
                    profile, prompt, agent_id=self.id, cap=self._cap(profile, prompt)
                )
            finally:
                self.run_budget.owed.pop(self.id, None)
        except ModelCallError as e:
            self._charge(e.reply, step=False)
            error = e.reply.error
            if e.no_room:
                raise _End("budget_cost", self._no_room(error)) from None
            if is_context_error(error):
                raise _End("context_exhausted", error) from None
            raise _End("model_error", error) from None
        except ReplayError as e:
            raise _End("error", f"replay of step {self.steps + 1} failed: {e}") from None
        self._charge(reply, step=True)
        self._replayed = client is not self.client
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
                source="replay" if self._replayed else "live",
                tool_calls=[c.id for c in reply.tool_calls] if reply.status == "completed" else [],
                usage=reply.usage,
                cost_usd=reply.cost_usd,
                total=self.totals.usage(),
                total_cost_usd=round(self.totals.cost_usd, 10),
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        )
        return reply

    def _budget(self) -> Budget:
        """The agent's caps now: the origin's while its steps replay, else its own."""
        r = self.replay
        if r is not None and r.budget is not None and (self._replaying() or self._kept()):
            return r.budget
        return self.config.budget

    def _limits(self) -> Limits:
        r = self.replay
        if r is not None and r.limits is not None and (self._replaying() or self._kept()):
            return r.limits
        return self.run_budget.limits

    def _charge(self, reply: Reply, *, step: bool) -> None:
        self.totals.add(reply, step=step)
        self.run_budget.totals.add(reply, step=step)
        # Only an answer shows a missing price: a failed call may have no cost on a priced
        # profile, and a priced answer with no token counts is counted by its size.
        answered = reply.status in ("completed", "incomplete")
        if answered and reply.cost_usd is None and not reply.sized_usd and not self._cost_warned:
            caps = self._budget().cost_usd or self._limits().cost_usd
            if caps is not None:
                self._cost_warned = True
                log.warning(
                    "profile %s has no price, so the cost limit of agent %s is not enforced",
                    self.profile.name,
                    self.id,
                )

    def _over_budget(self) -> tuple[AgentEnd, str] | None:
        b = self._budget()
        own = _over(self.totals, b.steps, b.output_tokens, b.cost_usd, "agent")
        limits = self._limits()
        run = _over(
            self.run_budget.totals,
            limits.steps,
            limits.output_tokens,
            limits.cost_usd,
            "run",
            self.run_budget.owed_by_others(self.id),
        )
        return own or run

    def _left(self) -> list[tuple[float, str, float]]:
        """What each cost cap leaves, with its scope and its limit: the agent's of what the
        agent counted, the run's of what the run counted and what other agents' calls still
        being tried may have cost."""
        left: list[tuple[float, str, float]] = []
        own, run = self._budget().cost_usd, self._limits().cost_usd
        if own is not None:
            left.append((own - self.totals.counted_usd, "agent", own))
        if run is not None:
            counted = self.run_budget.totals.counted_usd
            left.append((run - counted - self.run_budget.owed_by_others(self.id), "run", run))
        return left

    def _cap(self, profile: Profile, prompt: Prompt) -> SpendCap | None:
        """How a call of `profile` with `prompt` is tried again under the cost caps: only while
        another attempt fits what the caps leave beside what its attempts so far may have cost.
        An attempt is planned at the output cap plus the prompt's size, at most a full context,
        uncached. The caps let the call start, so a retry after attempts that cost nothing
        always fits. None with no price or no cost cap."""
        price = profile.price
        if price is None or not self._left():
            return None
        sent: list[Any] = [*prompt.items, *prompt.tools, *filter(None, [prompt.output_schema])]
        given = sum(len(x.model_dump_json()) for x in sent) // CHARS_PER_TOKEN
        if profile.context_tokens is not None:
            given = min(given, profile.context_tokens)
        made = profile.max_output_tokens or 0
        attempt = round((given * price.input + made * price.output) / 1_000_000, 10)

        def fits(owed: float) -> bool:
            self.run_budget.owed[self.id] = owed
            left = min((value for value, _, _ in self._left()), default=math.inf)
            return owed + attempt <= max(left, attempt)

        return SpendCap(attempt_usd=attempt, fits=fits)

    def _no_room(self, error: str | None) -> str:
        """The text of a stop when a failed call had no room under a cost cap to be tried
        again: the cap with the least left, and the error."""
        left = self._left()
        if not left:
            return error or "no room to try a failed call again"
        _, scope, limit = min(left)
        return f"{scope} limit of {limit:g} USD: {error}"

    def _warn_budget(self) -> None:
        b = self._budget()
        if b.warn_at is None:
            return
        used = {
            "steps": (self.totals.steps, b.steps),
            "output_tokens": (self.totals.output_tokens, b.output_tokens),
            "cost_usd": (self.totals.counted_usd, b.cost_usd),
        }
        for name, (value, limit) in used.items():
            if limit is None or self.warned.get(name) == limit or value < b.warn_at * limit:
                continue
            self.warned[name] = limit
            text = self.texts.budget(name).format(used=value, limit=limit)
            self.pending.append(("budget_warning", text))

    async def _gate(
        self, kind: GateKind, call_id: str | None = None, calls: tuple[str, ...] = ()
    ) -> Verdict:
        point = GatePoint(kind, self.run_id, self.id, self.steps, call_id, calls)
        verdict = await pass_gate(self.gate, point, self.sandbox, self.clock, self._emit)
        if verdict.action == "kill":
            raise AgentKilled(verdict.reason or "killed by the gate")
        if verdict.action == "inject" and verdict.text:
            self.pending.append(("inject", verdict.text))
        return verdict

    async def _tool(self, call: ToolRequest) -> ToolOutcome:
        replayed = self.replay is not None and self.steps <= self.replay.fork_at
        turn = None
        if replayed and self.replay is not None and self.replay.turnstile is not None:
            turn = self.replay.order.get((self.steps, call.id))
        if turn is None:
            return await self._run_logged(call, replayed)
        assert self.replay is not None
        turnstile = self.replay.turnstile
        assert turnstile is not None
        await turnstile.wait(turn)
        try:
            mark = await self._mark()
            outcome = await self._run_logged(call, replayed)
        finally:
            await turnstile.done(turn)
        versions = self.replay.versions.get((self.steps, call.id))
        if versions is not None:
            await self._check_channels(call, mark, versions)
        return outcome

    async def _mark(self) -> int:
        """The seq of the run's last event: the log's head, as the index may be behind it. The
        index's own for a run it marks corrupt, whose log head is not read."""
        try:
            head = await self.store.log_head(self.run_id)
        except RunCorrupt:
            last = await self.store.events(self.run_id, before=MAX_INT, limit=1)
            return last[-1].seq if last else -1
        return -1 if head is None else head.seq

    async def _pass_turn(self, call: ToolRequest) -> None:
        """Give up the turn of a replayed call that will not run, so the others go on."""
        if self.replay is not None and self.replay.turnstile is not None:
            turn = self.replay.order.get((self.steps, call.id))
            if turn is not None:
                await self.replay.turnstile.done(turn)

    async def _run_logged(self, call: ToolRequest, replayed: bool) -> ToolOutcome:
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
            recorded = self.replay.outputs.get((self.steps, call.id))
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
        return await run_tool(self.tools, ctx, call.name, call.arguments)

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

    async def _check_channels(
        self, call: ToolRequest, mark: int, versions: ChannelVersions
    ) -> None:
        """Compare what the replayed call met of the channels with the origin's: each board's
        last message id it read, and the sha256 each shared file its exec left. The events
        are read from the log where the index is behind it."""
        types = ["exec.started", "channel.read", "channel.snapshot"]
        events = await logged_events(self.store, self.run_id, after=mark, types=types, limit=1000)
        execs = {
            str(e.data.get("exec_id"))
            for e in events
            if e.type == "exec.started"
            and e.agent_id == self.id
            and e.data.get("call_id") == call.id
        }
        reads: dict[str, int] = {}
        writes: dict[str, str | None] = {}
        for e in events:
            d = e.data
            if e.type == "channel.read" and e.agent_id == self.id and d.get("call_id") == call.id:
                through = d.get("through")
                if isinstance(through, int):
                    reads[str(d.get("channel"))] = through
            elif e.type == "channel.snapshot" and str(d.get("exec_id")) in execs:
                changes = d.get("changes")
                for c in changes if isinstance(changes, list) else []:
                    if isinstance(c, dict) and c.get("agent") == self.id:
                        sha = c.get("sha256")
                        writes[str(c.get("path"))] = sha if isinstance(sha, str) else None
        found: list[tuple[str | None, str | None, str | None, str | None]] = []
        for channel in sorted(set(versions.reads) | set(reads)):
            want, got = versions.reads.get(channel), reads.get(channel)
            if want != got:
                found.append((channel, None, _version(want), _version(got)))
        for path in sorted(set(versions.writes) | set(writes)):
            want_sha, got_sha = versions.writes.get(path), writes.get(path)
            if want_sha != got_sha or (path in versions.writes) != (path in writes):
                found.append((versions.channels.get(path), path, want_sha, got_sha))
        for channel, path, want_v, got_v in found:
            await self._emit(
                ReplayDivergence(
                    step=self.steps,
                    call_id=call.id,
                    tool=call.name,
                    kind="channel",
                    channel=channel,
                    path=path,
                    recorded_version=want_v,
                    live_version=got_v,
                    origin_seq=versions.seq,
                )
            )
        assert self.replay is not None
        if found and self.replay.strict:
            raise _End("divergence", f"tool call {call.id} at step {self.steps} met other channels")

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
            reply = await client.call(
                profile, prompt, agent_id=self.id, cap=self._cap(profile, prompt)
            )
        except ModelCallError as e:
            self._charge(e.reply, step=False)
            if e.no_room:
                raise _End("budget_cost", self._no_room(e.reply.error)) from None
            log.warning("agent %s: summary call failed: %s", self.id, e)
            return None
        finally:
            self.run_budget.owed.pop(self.id, None)
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


async def pass_gate(
    gate: Gate,
    point: GatePoint,
    sandbox: AuditedSandbox,
    clock: PauseClock,
    emit: Callable[[Payload], Awaitable[None]],
) -> Verdict:
    """Check `point` at `gate`. While the gate answers pause, log the hold, freeze the agent's
    sandbox and wait at the gate, then unfreeze the sandbox. The hold counts as paused on
    `clock`. A sandbox that does not freeze is logged and the agent holds all the same, since
    it takes no step while it holds. One that does not unfreeze is logged too and the agent
    goes on with its verdict: its next exec waits until a resume unfreezes the sandbox.
    Returns the verdict the agent goes on with."""
    verdict = await gate.check(point)
    if verdict.action != "pause":
        return verdict
    clock.hold("gate")
    try:
        while verdict.action == "pause":
            held = AgentHeld(
                kind=point.kind,
                call_id=point.call_id,
                step=point.step,
                next_call=point.calls[0] if point.calls else None,
            )
            await emit(held)
            error = await sandbox.pause()
            if error is not None:
                log.warning(
                    "agent %s holds at its gate, but its sandbox did not freeze: %s",
                    point.agent_id,
                    error,
                )
            try:
                verdict = await gate.held(point)
            finally:
                error = await sandbox.resume()
                if error is not None:
                    log.warning(
                        "agent %s left its gate, but its sandbox did not unfreeze: %s",
                        point.agent_id,
                        error,
                    )
    finally:
        clock.release("gate")
    return verdict


async def run_tool(
    tools: Mapping[str, Tool], ctx: ToolContext, name: str, arguments: str
) -> ToolOutcome:
    """Run tool `name` of `tools` with `arguments`, a JSON object as text. An unknown tool, bad
    arguments or a tool that raises give an error outcome, never an exception."""
    tool = tools.get(name)
    if tool is None:
        known = ", ".join(tools)
        return ToolOutcome(f"There is no tool named {name!r}. Tools: {known}.", True)
    try:
        raw = scrub(json.loads(arguments)) if arguments.strip() else {}
    except (ValueError, RecursionError) as e:
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
        log.exception("tool %s failed", name)
        return ToolOutcome(f"The tool failed: {type(e).__name__}: {e}", error=True)


def _size(items: Sequence[Item]) -> int:
    return sum(len(item.model_dump_json()) for item in items)


def _version(value: int | None) -> str | None:
    return None if value is None else str(value)
