from dataclasses import dataclass
from typing import Literal, Protocol

from aims.events.payloads import GateKind

Action = Literal["continue", "pause", "kill", "inject", "deny"]


@dataclass(frozen=True, slots=True)
class GatePoint:
    """Where an agent is: before or after a model call, or before or after one tool call.
    `call_id` names that call. `step` counts the model calls made so far. After a reply,
    `calls` are the tool calls it asks for, none of them run yet."""

    kind: GateKind
    run_id: str
    agent_id: str
    step: int
    call_id: str | None = None
    calls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Verdict:
    """`pause` freezes the agent and its sandbox until `Gate.held` returns another verdict;
    `kill` ends the run; `inject` gives the agent `text` before its next model call; `deny` at
    before_tool skips the call and gives the agent the fixed denied reply instead; after a reply
    it does so for the reply's first tool call."""

    action: Action = "continue"
    text: str | None = None
    reason: str | None = None


CONTINUE = Verdict()
DENY = Verdict("deny")


class Gate(Protocol):
    async def check(self, point: GatePoint) -> Verdict: ...

    async def held(self, point: GatePoint) -> Verdict:
        """Wait while the agent is paused at `point`; return what to do next."""
        ...


class OpenGate:
    """Lets every point through."""

    async def check(self, point: GatePoint) -> Verdict:
        return CONTINUE

    async def held(self, point: GatePoint) -> Verdict:
        return CONTINUE
