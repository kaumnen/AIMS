import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

from aims.agent import CONTINUE, DENY, Gate, GatePoint, OpenGate, PauseClock, Verdict


@dataclass(slots=True)
class _Agent:
    paused: str | None = None
    held: asyncio.Future[Verdict] | None = None
    at: GatePoint | None = None
    ours: bool = False
    deny: str | None = None
    injects: list[str] = field(default_factory=list)
    clock: PauseClock = field(default_factory=PauseClock)


class ControlGate:
    """What an operator or the monitor does to a run's agents, laid over another gate.

    A paused agent holds at its next gate point until it is resumed; the runner freezes its
    sandbox at once, so an agent paused mid-call stops there and holds once the call lands.
    Every point passes the inner gate, again after a hold, and a pause asked while the inner gate
    decides holds the agent at that same point. An inject reaches the agent at its next gate
    point. A deny refuses the tool call the agent holds before. Once the run is killed every
    point answers kill. A held agent never moves on by itself. Each agent's `clock` runs from
    the moment it is paused to the moment it is resumed.
    """

    def __init__(self, inner: Gate | None = None) -> None:
        self.inner = inner or OpenGate()
        self.killed: str | None = None
        self._agents: dict[str, _Agent] = {}

    def _get(self, agent_id: str) -> _Agent:
        return self._agents.setdefault(agent_id, _Agent())

    def clock(self, agent_id: str) -> PauseClock:
        return self._get(agent_id).clock

    async def check(self, point: GatePoint) -> Verdict:
        state = self._get(point.agent_id)
        state.at = point
        verdict: Verdict | None = None
        try:
            verdict = await self._check(point, state)
            return verdict
        finally:
            if verdict is None or verdict.action != "pause":
                state.at = None

    async def _check(self, point: GatePoint, state: _Agent) -> Verdict:
        if self.killed is not None:
            return Verdict("kill", reason=self.killed)
        if state.paused is not None:
            state.ours = True
            return Verdict("pause", reason=state.paused)
        if self._denied(state, point):
            return DENY
        verdict = await self.inner.check(point)
        if self.killed is not None:
            return Verdict("kill", reason=self.killed)
        if verdict.action == "kill":
            return verdict
        if state.paused is not None:
            if verdict.action == "inject" and verdict.text:
                state.injects.append(verdict.text)
            state.ours = True
            return Verdict("pause", reason=state.paused)
        if self._denied(state, point):
            return DENY
        if verdict.action == "pause":
            state.ours = False
        elif verdict.action == "continue" and state.injects:
            return Verdict("inject", text=state.injects.pop(0))
        return verdict

    @staticmethod
    def _denied(state: _Agent, point: GatePoint) -> bool:
        """Whether a deny waits for this call; a deny left for another call is dropped."""
        if state.deny is None:
            return False
        denied = state.deny == point.call_id and point.kind == "before_tool"
        state.deny = None
        return denied

    async def held(self, point: GatePoint) -> Verdict:
        if self.killed is not None:
            return Verdict("kill", reason=self.killed)
        state = self._get(point.agent_id)
        state.at = point
        verdict: Verdict | None = None
        try:
            verdict = await self._held(point, state)
            return verdict
        finally:
            if verdict is None or verdict.action != "pause":
                state.at = None

    async def _held(self, point: GatePoint, state: _Agent) -> Verdict:
        if state.paused is None:
            if state.ours:
                return await self._check(point, state)
            return await self.inner.held(point)
        state.held = asyncio.get_running_loop().create_future()
        try:
            verdict = await state.held
        finally:
            state.held = None
        if verdict.action in ("kill", "deny"):
            return verdict
        if verdict.action == "inject" and verdict.text:
            state.injects.insert(0, verdict.text)
        return await self._check(point, state)

    def is_held(self, agent_id: str) -> bool:
        state = self._agents.get(agent_id)
        return state is not None and state.held is not None

    def is_paused(self, agent_id: str) -> bool:
        state = self._agents.get(agent_id)
        return state is not None and state.paused is not None

    def held_call(self, agent_id: str) -> str | None:
        """The tool call the agent waits before at a gate point, not yet run."""
        state = self._agents.get(agent_id)
        if state is None or state.at is None or state.at.kind != "before_tool":
            return None
        return state.at.call_id

    def pause(self, agents: Iterable[str], reason: str | None = None) -> None:
        for agent_id in agents:
            state = self._get(agent_id)
            state.paused = reason or "paused by the operator"
            state.clock.hold("control")

    def resume(self, agents: Iterable[str], verdict: Verdict = CONTINUE) -> list[str]:
        """Let paused agents go on, with an inject when `verdict` carries one. A deny refuses
        the call of each paused agent waiting before a tool call; the others just go on.
        Returns the agents that were not held at a gate point, whose sandboxes the caller
        unfreezes."""
        if verdict.action not in ("continue", "inject", "deny"):
            raise ValueError(f"resume takes continue, inject or deny, not {verdict.action}")
        loose: list[str] = []
        for agent_id in agents:
            state = self._get(agent_id)
            call = self.held_call(agent_id) if state.paused is not None else None
            state.paused = None
            given = verdict if verdict.action != "deny" or call is not None else CONTINUE
            if state.held is not None and not state.held.done():
                state.held.set_result(given)
            else:
                loose.append(agent_id)
                if given.action == "inject" and given.text:
                    state.injects.append(given.text)
                elif given.action == "deny":
                    state.deny = call
            state.clock.release("control")
        return loose

    def inject(self, agents: Iterable[str], text: str) -> None:
        for agent_id in agents:
            self._get(agent_id).injects.append(text)

    def kill(self, reason: str) -> None:
        """Answer kill at every point from now on. Agents held at a point stay held until
        their tasks are cancelled, so their sandboxes stay frozen until they are killed."""
        self.killed = reason
