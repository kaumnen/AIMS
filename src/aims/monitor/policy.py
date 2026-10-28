"""What the monitor does about its flags: per-category pause and kill cutoffs, kills only on
corroborated findings, the deny breaker, and the taint of verifier-phase flags."""

import bisect
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from aims.events import Category, Event, MonitorActionKind, MonitorFlag, Oversight
from aims.monitor.config import PolicyConfig


@dataclass(frozen=True, slots=True)
class Decision:
    """An action the policy chose for `agents`; `flags` are the seqs of the flags behind it."""

    action: MonitorActionKind
    agents: tuple[str, ...]
    flags: tuple[int, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Cutoffs:
    """The importance at which flags pause and kill, None for never. A flag at the kill cutoff
    pauses when it is not corroborated."""

    pause: int | None
    kill: int | None

    def merge(self, other: Cutoffs) -> Cutoffs:
        """The stricter of both."""
        return Cutoffs(_least(self.pause, other.pause), _least(self.kill, other.kill))


def _least(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


class Policy:
    """The policy of one run. `observe` takes every run event in seq order, the monitor's own
    skipped; `decide` takes flags as they were written.

    A flag pauses at its category's pause cutoff, on any lane: the policy acts on the highest
    importance any lane gave. It kills at the kill cutoff only when corroborated: a signal flag,
    or two sweep lanes at the cutoff over the same agent step; a resample confirmed by the review
    is the third way, decided there. Judge flags never act, nor do flags of the setup or verifier
    phase; verifier-phase flags are kept as the taint of the agent's score.
    """

    def __init__(self, config: PolicyConfig, mode: Oversight, agents: Iterable[str]) -> None:
        self.config = config
        self.mode = mode
        self.agents: list[str] = list(dict.fromkeys(agents))
        self._starts: dict[str, list[int]] = defaultdict(list)
        self._lanes: dict[tuple[str, int], set[tuple[str, str | None]]] = defaultdict(set)
        self._tainted: dict[str | None, list[int]] = defaultdict(list)
        self._in_a_row: dict[str, int] = defaultdict(int)
        self._denied: dict[str, int] = defaultdict(int)

    def cutoffs(self, category: Category) -> Cutoffs:
        c = self.config.category(category)
        return Cutoffs(_least(c.pause, c.kill), c.kill)

    def cutoffs_of(self, flags: Iterable[Event]) -> Cutoffs:
        """The strictest cutoffs over the categories of `flags`."""
        out = Cutoffs(None, None)
        for e in flags:
            out = out.merge(self.cutoffs(MonitorFlag.model_validate(e.data).category))
        return out

    def step(self, agent_id: str, seq: int) -> int:
        """The agent step `seq` falls in: steps start at the agent's model calls."""
        return bisect.bisect_right(self._starts.get(agent_id, []), seq)

    def step_start(self, agent_id: str, seq: int) -> int | None:
        """The seq of the model call that began the step `seq` falls in."""
        step = self.step(agent_id, seq)
        return None if step == 0 else self._starts[agent_id][step - 1]

    def tainted(self, agent_id: str | None) -> list[int]:
        """Seqs of verifier-phase flags of `agent_id`, run-level ones included."""
        own = self._tainted.get(agent_id, []) if agent_id is not None else []
        return sorted({*own, *self._tainted.get(None, [])})

    def observe(self, event: Event) -> Decision | None:
        """Take in a run event; returns the deny breaker's decision when it trips."""
        agent = event.agent_id
        if agent is None:
            return None
        if agent not in self.agents:
            self.agents.append(agent)
        data = event.data
        if event.type == "model.request":
            if data.get("role") is None and data.get("attempt") == 1:
                self._starts[agent].append(event.seq)
        elif event.type == "tool.result":
            if not data.get("denied"):
                self._in_a_row[agent] = 0
                return None
            self._in_a_row[agent] += 1
            self._denied[agent] += 1
            reason = None
            if self._in_a_row[agent] >= self.config.deny_consecutive:
                reason = f"{self._in_a_row[agent]} denied tool calls in a row"
                self._in_a_row[agent] = 0
            if self._denied[agent] == self.config.deny_total:
                reason = reason or f"{self._denied[agent]} denied tool calls in all"
            if reason is not None:
                return Decision("pause" if self.mode == "human" else "kill", (agent,), (), reason)
        return None

    def decide(self, flags: Sequence[Event]) -> list[Decision]:
        """What the newly written `flags` call for: one kill, or one pause, or nothing."""
        kills: list[tuple[Event, tuple[str, ...], str]] = []
        pauses: list[tuple[Event, tuple[str, ...], str]] = []
        for e in flags:
            if e.type != "monitor.flag":
                continue
            f = MonitorFlag.model_validate(e.data)
            if f.layer == "judge" or f.final or f.phase == "setup":
                continue
            if f.phase == "verifier":
                self._tainted[e.agent_id].append(e.seq)
                continue
            cut = self.cutoffs(f.category)
            scope = self.config.category(f.category).scope
            targets = self._targets(e.agent_id, scope)
            if cut.kill is not None and f.importance >= cut.kill:
                why = self._corroborated(e, f)
                if why is not None:
                    kills.append((e, targets, why))
                    continue
            if cut.pause is not None and f.importance >= cut.pause:
                pauses.append((e, targets, _what(f)))
        chosen: tuple[MonitorActionKind, list[tuple[Event, tuple[str, ...], str]]]
        if kills:
            chosen = ("kill", kills)
        elif pauses:
            chosen = ("pause", pauses)
        else:
            return []
        action, causes = chosen
        agents = [a for a in self.agents if any(a in t for _, t, _ in causes)]
        reasons = list(dict.fromkeys(r for _, _, r in causes))
        return [
            Decision(action, tuple(agents), tuple(e.seq for e, _, _ in causes), "; ".join(reasons))
        ]

    def _targets(self, agent_id: str | None, scope: str) -> tuple[str, ...]:
        if agent_id is None or scope == "run":
            return tuple(self.agents)
        return (agent_id,)

    def _corroborated(self, e: Event, f: MonitorFlag) -> str | None:
        if f.layer == "signal":
            return f"signal {f.source} flagged {f.category} at importance {f.importance}"
        agent = e.agent_id or ""
        lane = (f.source, f.lane)
        steps = sorted({self.step(agent, s) for s in f.evidence})
        for step in steps:
            self._lanes[(agent, step)].add(lane)
        for step in steps:
            seen = self._lanes[(agent, step)]
            if len(seen) >= 2:
                names = ", ".join(sorted(f"{s}/{n}" for s, n in seen))
                return f"lanes {names} rated step {step} of {agent} at the kill cutoff"
        return None


def _what(f: MonitorFlag) -> str:
    where = f.source if f.lane is None else f"{f.source}/{f.lane}"
    return f"{f.layer} {where} flagged {f.category} at importance {f.importance}"
