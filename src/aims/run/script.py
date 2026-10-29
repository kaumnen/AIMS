"""Scripted agents and the waits that order the agents of one run.

A scripted agent follows its scenario script step by step instead of calling a model: it runs
commands, posts to boards and calls its tools. A step may wait for an event first: another
agent starting or finishing, reaching a step, running a command, reading or writing a channel,
or posting to a board.
"""

import asyncio
import contextlib
import json
import logging
import posixpath
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from aims.agent import Gate, OpenGate, ReplayPlan, run_tool, settle
from aims.events import (
    AgentEnd,
    AgentFinished,
    AgentStarted,
    Content,
    Draft,
    Event,
    LocalEventStore,
    Payload,
    ToolCall,
    ToolResult,
)
from aims.models import Profile
from aims.plants.canary import Canary, render
from aims.sandbox import AuditedSandbox
from aims.scenario import AgentConfig, Scenario
from aims.scenario.model import Call, FolderChannel, ScriptStep, Start, Trigger
from aims.tools import Tool, ToolContext, cap

log = logging.getLogger("aims.run.script")

Post = Callable[[str, str, str], Awaitable[Any]]
"""Post `text` to board `channel` for scripted agent `agent`."""
End = tuple[AgentEnd, str | None, str | None]
"""How a step ended its agent: the reason, the detail and the answer."""

_READ_OPS = frozenset({"read"})
_WRITE_OPS = frozenset({"write", "delete", "rename", "mkdir", "symlink", "link"})


class Watch:
    """The events of one run, kept for the agents that wait on them.

    The store tap records each event as it is written. A waiter takes the change signal
    before it looks, so an event written between its look and its wait is not missed.
    """

    def __init__(self, store: LocalEventStore, run_id: str, scenario: Scenario) -> None:
        self.store = store
        self.run_id = run_id
        self.scenario = scenario
        self.events: list[tuple[Event, float]] = []
        self._changed = asyncio.Event()
        self._steps: dict[str, dict[int, float]] = {}
        self._finished: dict[str, float] = {}
        self._untap = store.tap(run_id, self._feed)

    def close(self) -> None:
        self._untap()

    def _feed(self, batch: Sequence[Event]) -> None:
        now = asyncio.get_running_loop().time()
        for event in batch:
            self.events.append((event, now))
            if event.agent_id is None:
                continue
            if event.type == "agent.step":
                n = _int(event.data.get("n"))
                if n:
                    self._reached(event.agent_id, n, now)
            elif event.type == "agent.finished":
                self._finished.setdefault(event.agent_id, now)
        self._wake()

    def record_step(self, agent_id: str, n: int) -> None:
        """Record a step a scripted agent took, which has no `agent.step` event."""
        self._reached(agent_id, n, asyncio.get_running_loop().time())
        self._wake()

    def _reached(self, agent_id: str, n: int, at: float) -> None:
        steps = self._steps.setdefault(agent_id, {})
        for i in range(1, n + 1):
            steps.setdefault(i, at)

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait_start(self, start: Start | None, begin: float) -> None:
        """Wait until `start` allows its agent to begin."""
        if start is None:
            return
        if start.agent is None:
            await _sleep_until(begin + start.delay_s)
            return
        while True:
            changed = self._changed
            at = self._started_at(start)
            if at is not None:
                break
            await changed.wait()
        await _sleep_until(at + start.delay_s)

    def _started_at(self, start: Start) -> float | None:
        assert start.agent is not None
        if start.on == "step":
            assert start.step is not None
            reached = self._steps.get(start.agent, {})
            at = reached.get(start.step)
            if at is None and start.agent in self._finished:
                log.warning(
                    "agent %s finished after %d steps, short of step %d; starting what waits on it",
                    start.agent,
                    len(reached),
                    start.step,
                )
                return self._finished[start.agent]
            return at
        kind = "agent.started" if start.on == "started" else "agent.finished"
        for event, at in self.events:
            if event.type == kind and event.agent_id == start.agent:
                return at
        return None

    async def wait_trigger(self, trigger: Trigger) -> None:
        """Wait until an event matches `trigger`."""
        while True:
            changed = self._changed
            if await self._triggered(trigger):
                return
            await changed.wait()

    async def _triggered(self, trigger: Trigger) -> bool:
        targets = [trigger.agent] if trigger.agent is not None else self.scenario.subjects
        if trigger.event == "step":
            assert trigger.step is not None
            return any(trigger.step in self._steps.get(a, {}) for a in targets)
        for event, _ in self.events:
            if await self._matches(event, trigger, targets):
                return True
        return False

    async def _matches(self, event: Event, trigger: Trigger, targets: Sequence[str]) -> bool:
        if event.agent_id not in targets:
            changes = event.data.get("changes")
            changed = (
                event.type == "channel.snapshot"
                and isinstance(changes, list)
                and any(
                    isinstance(change, dict) and change.get("agent") in targets
                    for change in changes
                )
            )
            if not changed:
                return False
        kind = trigger.event
        if kind == "started":
            return event.type == "agent.started"
        if kind == "finished":
            return event.type == "agent.finished"
        if kind == "exec":
            if event.type != "exec.started" or event.data.get("phase") != "agent":
                return False
            if trigger.contains is None:
                return True
            command = await _text(self.store, self.run_id, event.data.get("command"))
            return trigger.contains in command
        if kind == "post":
            if event.type != "channel.message":
                return False
            if trigger.channel is not None and event.data.get("channel") != trigger.channel:
                return False
            if trigger.contains is None:
                return True
            text = await _text(self.store, self.run_id, event.data.get("text"))
            return trigger.contains in text
        if kind in ("read", "write"):
            return await self._channel_event(event, trigger, kind)
        return False

    async def _channel_event(self, event: Event, trigger: Trigger, kind: str) -> bool:
        channel = trigger.channel
        if event.type == "channel.read":
            if kind != "read":
                return False
            return channel is None or event.data.get("channel") == channel
        if event.type == "channel.message":
            if kind != "write":
                return False
            return channel is None or event.data.get("channel") == channel
        if event.type == "channel.snapshot":
            if kind != "write":
                return False
            changes = event.data.get("changes")
            if not isinstance(changes, list):
                return False
            paths = [str(change.get("path", "")) for change in changes if isinstance(change, dict)]
            if channel is None:
                return any(_in_channel(path, c) for c in self._folders(None) for path in paths)
            return any(_in_channel(path, c) for c in self._folders(str(channel)) for path in paths)
        path = event.data.get("path")
        if not isinstance(path, str):
            return False
        ops = _READ_OPS if kind == "read" else _WRITE_OPS
        if event.type == "file.access":
            if event.data.get("op") not in ops:
                return False
        elif event.type != "file.write" or kind != "write":
            return False
        return any(_in_channel(path, c) for c in self._folders(channel))

    def _folders(self, name: str | None) -> list[FolderChannel]:
        return [
            c
            for c in self.scenario.channels
            if isinstance(c, FolderChannel) and (name is None or c.name == name)
        ]

    async def subjects_done(self, subjects: Sequence[str]) -> None:
        """Wait until every subject has written `agent.finished`."""
        while True:
            changed = self._changed
            if all(s in self._finished for s in subjects):
                return
            await changed.wait()


class ScriptedAgent:
    """One scripted agent: log its start, take its steps, log its finish. Its steps are its
    own script, a confederate's, unless `steps` gives others; a call of `submit` ends it. In a
    fork it takes its first `replay.fork_at` steps again without their triggers, each when the
    origin's order reaches it at the turnstile, then goes on with the rest as written."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        sandbox_config: Any,
        instruction: str,
        profile: Profile,
        sandbox: AuditedSandbox,
        store: LocalEventStore,
        run_id: str,
        watch: Watch,
        post: Post | None = None,
        canaries: Mapping[str, Canary] | None = None,
        replay: ReplayPlan | None = None,
        steps: Sequence[ScriptStep] | None = None,
        tools: Sequence[Tool] = (),
        gate: Gate | None = None,
    ) -> None:
        self.config = config
        self.id = config.id
        self.sandbox_config = sandbox_config
        self.instruction = instruction
        self.profile = profile
        self.sandbox = sandbox
        self.store = store
        self.run_id = run_id
        self.watch = watch
        self.post = post
        self.canaries = canaries or {}
        self.replay = replay
        self.script: list[ScriptStep] = list(config.script or [] if steps is None else steps)
        self.tools = {t.name: t for t in tools}
        self.gate = gate or OpenGate()
        self.steps = 0
        self.answer: str | None = None
        self.killed: str | None = None
        self.done = False

    async def run(self) -> AgentFinished:
        start = time.monotonic()
        reason: AgentEnd = "error"
        detail: str | None = None
        answer: str | None = None
        await self._emit(
            AgentStarted(
                profile=self.profile.name,
                model=self.profile.model,
                backend=None,
                tools=list(self.tools),
                system=await self.store.content(self.run_id, ""),
                budget=self.config.budget.model_dump(mode="json"),
                compaction=None
                if self.config.compaction is None
                else self.config.compaction.model_dump(mode="json"),
            )
        )
        try:
            reason, detail = "no_submit", "the steps finished"
            for n, step in enumerate(self.script, 1):
                if self.replay is not None and n <= self.replay.fork_at:
                    end = await self._again(n, step)
                else:
                    if step.on is not None:
                        await self.watch.wait_trigger(step.on)
                    if step.after_s:
                        await asyncio.sleep(step.after_s)
                    end = await self._take(n, step)
                self.steps = n
                self.watch.record_step(self.id, n)
                if end is not None:
                    reason, detail, answer = end
                    break
        except asyncio.CancelledError:
            if self.killed is not None:
                await settle(self._finish(start, "killed", self.killed))
            else:
                await settle(self._finish(start, "cancelled", None))
            raise
        except Exception as e:
            log.exception("scripted agent %s failed", self.id)
            reason, detail = "error", f"{type(e).__name__}: {e}"
        return await settle(self._finish(start, reason, detail, answer))

    async def _again(self, n: int, step: ScriptStep) -> End | None:
        """Take step `n` again where the origin's order has it."""
        assert self.replay is not None
        turnstile = self.replay.turnstile
        actions = self.replay.actions
        seq = actions[n - 1] if n - 1 < len(actions) else None
        if turnstile is None or seq is None:
            return await self._take(n, step)
        await turnstile.wait(seq)
        try:
            return await self._take(n, step)
        finally:
            await turnstile.done(seq)

    async def _take(self, n: int, step: ScriptStep) -> End | None:
        if step.run is not None:
            timeout = step.timeout_s or self.sandbox_config.command_timeout_s
            command = self._render(step.run)
            result = await self.sandbox.exec(command, timeout_s=timeout, phase="agent")
            finished = result.finished
            if finished.end != "exited" or finished.exit_code != 0:
                if finished.end == "exited":
                    end = f"exit code {finished.exit_code}"
                else:
                    end = finished.end
                log.warning("scripted agent %s step %d ended with %s", self.id, n, end)
            return None
        if step.call is not None:
            return await self._call(n, step.call)
        assert step.post is not None
        if self.post is None:
            raise RuntimeError(f"agent {self.id}: a post of step {n} needs a board post function")
        await self.post(self.id, step.post.channel, self._render(step.post.text))
        return None

    async def _call(self, n: int, call: Call) -> End | None:
        """Call one of the agent's tools as its model would, with no model call behind it."""
        call_id = str(uuid.uuid7())
        arguments = json.dumps(self._render_value(call.arguments), ensure_ascii=False)
        await self._emit(
            ToolCall(
                call_id=call_id,
                name=call.tool,
                arguments=await self.store.content(self.run_id, arguments),
            )
        )
        ctx = ToolContext(
            sandbox=self.sandbox,
            store=self.store,
            run_id=self.run_id,
            agent_id=self.id,
            call_id=call_id,
            step=n,
            config=self.sandbox_config,
            gate=self.gate,
        )
        outcome = await run_tool(self.tools, ctx, call.tool, arguments)
        data = outcome.output.encode()
        text, truncated = cap(data)
        await self._emit(
            ToolResult(
                call_id=call_id,
                output=await self.store.content(self.run_id, text),
                error=outcome.error,
                output_bytes=len(data),
                truncated=truncated,
            )
        )
        if outcome.failed:
            return "sandbox_failed", outcome.output, None
        if outcome.answer is not None:
            return "submitted", None, outcome.answer
        if outcome.error:
            log.warning("scripted agent %s step %d: %s failed: %s", self.id, n, call.tool, text)
        return None

    def _render(self, text: str) -> str:
        if not self.canaries:
            return text
        return render(text, self.canaries)

    def _render_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._render(value)
        if isinstance(value, dict):
            return {k: self._render_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render_value(v) for v in value]
        return value

    async def _finish(
        self, start: float, reason: AgentEnd, detail: str | None, answer: str | None = None
    ) -> AgentFinished:
        self.done = True
        self.answer = answer
        if self.replay is not None and self.replay.turnstile is not None:
            await self.replay.turnstile.drop(self.replay.actions)
        finished = AgentFinished(
            reason=reason,
            detail=detail,
            answer=None if answer is None else await self.store.content(self.run_id, answer),
            steps=self.steps,
            wall_ms=int((time.monotonic() - start) * 1000),
        )
        await self._emit(finished)
        return finished

    async def _emit(self, payload: Payload) -> None:
        await self.store.append(self.run_id, [Draft(payload, self.id)])


async def _text(store: LocalEventStore, run_id: str, value: Content | Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        sha = value.get("sha256")
        if isinstance(sha, str):
            with contextlib.suppress(Exception):
                return (await store.get_blob(run_id, sha)).decode(errors="replace")
    return "" if value is None else str(value)


async def _sleep_until(when: float) -> None:
    delay = when - asyncio.get_running_loop().time()
    if delay > 0:
        await asyncio.sleep(delay)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _in_channel(path: str, channel: FolderChannel) -> bool:
    found = posixpath.normpath(path)
    root = posixpath.normpath(channel.path)
    return found == root or found.startswith(f"{root.rstrip('/')}/")
