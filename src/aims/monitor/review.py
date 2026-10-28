"""The autonomous review of a pause: whether the flags behind it were false alarms."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aims.events import Event, EventStore, ToolCall
from aims.models import ModelCallError, ModelClient, Profile, Prompt, Reply
from aims.monitor.config import ReviewConfig
from aims.monitor.policy import Cutoffs
from aims.monitor.render import escape, frame
from aims.monitor.sweep import Score
from aims.monitor.texts import MonitorTexts

log = logging.getLogger("aims.monitor")

ARGUMENTS_CAP = 16_384


class Scorer(Protocol):
    async def score(self, proposal: str, *, role: str = "review") -> Score: ...


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """What a review found. `clear` when the flags may be cleared; `confirmed` when a resample
    reached the kill cutoff. `importance` and `suspicion` are the highest scores taken, over
    `scored` actions: the flagged and held calls rescored and `resamples` samples."""

    clear: bool
    reason: str
    confirmed: bool = False
    signal_backed: bool = False
    importance: int = 0
    suspicion: int | None = None
    scored: int = 0
    resamples: int = 0
    failed: int = 0
    cost_usd: float = 0.0

    def advice(self) -> str:
        """The result for a person deciding the pause."""
        parts = []
        if self.signal_backed:
            parts.append("A signal flag is behind this pause; no model review clears it.")
        if self.scored:
            suspicion = "" if self.suspicion is None else f", suspicion {self.suspicion}"
            parts.append(
                f"Review: {self.scored} actions scored, {self.resamples} of them hidden "
                f"resamples of the next step; highest importance {self.importance}{suspicion}."
            )
        verdict = "would clear" if self.clear else "would not clear"
        parts.append(f"The review {verdict} the flags: {self.reason}.")
        return " ".join(parts)


class Reviewer:
    """Reviews the flags behind a pause of one agent.

    A flag from a signal is never cleared by a model. Sweep-only flags clear when the flagged
    tool calls and the call the agent holds at, scored again in the evidence lane, and
    `config.resamples` hidden samples of the agent's next step, drawn from its own profile and
    prompt, never run, each scored, all stay below the pause cutoff. Any review call that fails
    keeps the flags. A resample at the kill cutoff confirms them. Flagged calls carry the frame
    ids the trajectory renders them under, made with the run's secret `key`.
    """

    def __init__(
        self,
        *,
        config: ReviewConfig,
        texts: MonitorTexts,
        client: ModelClient,
        store: EventStore,
        source: str,
        key: bytes,
    ) -> None:
        self.config = config
        self.texts = texts
        self.client = client
        self.store = store
        self.source = source
        self.key = key

    async def review(
        self,
        flags: Sequence[Event],
        *,
        scorer: Scorer | None,
        cutoffs: Cutoffs,
        actions: Sequence[Event] = (),
        held: Event | None = None,
        sample: tuple[Profile, Prompt] | None = None,
        allowed: Callable[[], bool] = lambda: True,
    ) -> ReviewResult:
        """Review `flags` of one agent. `actions` are the `tool.call` events they cite, `held`
        the one the agent holds at, `sample` its next model call to resample."""
        backed = any(e.data.get("layer") == "signal" for e in flags)
        if scorer is None:
            return ReviewResult(False, "no review model is set", signal_backed=backed)
        if not allowed():
            return ReviewResult(False, "the monitor budget is spent", signal_backed=backed)
        proposals: list[str | None] = []
        calls = [e for e in actions if held is None or e.seq != held.seq]
        if calls:
            proposals.append(await self._proposal("review_action", calls))
        if held is not None:
            proposals.append(await self._proposal("review_held", [held]))
        rescored = len(proposals)
        jobs: list[asyncio.Task[tuple[Score | None, float]]] = []
        async with asyncio.TaskGroup() as tg:
            for text in proposals:
                jobs.append(tg.create_task(self._score(scorer, text)))
            if sample is not None:
                for _ in range(self.config.resamples):
                    jobs.append(tg.create_task(self._resample(scorer, *sample)))
        results = [j.result() for j in jobs]
        scores = [s for s, _ in results if s is not None]
        failed = sum(1 for s, _ in results if s is None)
        resampled = [s for s, _ in results[rescored:] if s is not None]
        top = max((s.importance for s in scores), default=0)
        suspicions = [s.suspicion for s in scores]
        out: dict[str, Any] = {
            "signal_backed": backed,
            "importance": top,
            "suspicion": max(suspicions) if suspicions else None,
            "scored": len(scores),
            "resamples": len(resampled),
            "failed": failed,
            "cost_usd": sum(c for _, c in results),
        }
        kill = cutoffs.kill
        sample_top = max((s.importance for s in resampled), default=0)
        if kill is not None and sample_top >= kill:
            reason = f"a resample of the next step scored importance {sample_top}"
            return ReviewResult(False, reason, confirmed=True, **out)
        if failed:
            return ReviewResult(False, f"{failed} of {len(results)} review calls failed", **out)
        if sample is None and self.config.resamples:
            return ReviewResult(False, "the agent has no next step to resample", **out)
        pause = cutoffs.pause
        if pause is not None and top >= pause:
            reason = f"a review score reached importance {top}, at or above the cutoff {pause}"
            return ReviewResult(False, reason, **out)
        if backed:
            return ReviewResult(False, "a signal flag is behind the pause", **out)
        if not scores:
            return ReviewResult(False, "the review had nothing to score", **out)
        reason = f"{len(scores)} review scores stayed below importance {pause}"
        return ReviewResult(True, reason, **out)

    async def _score(self, scorer: Scorer, proposal: str | None) -> tuple[Score | None, float]:
        if proposal is None:
            return None, 0.0
        try:
            score = await scorer.score(proposal, role="review")
        except Exception as e:
            log.warning("a review score failed: %s: %s", type(e).__name__, e)
            return None, getattr(e, "cost_usd", 0.0)
        return score, score.cost_usd

    async def _resample(
        self, scorer: Scorer, profile: Profile, prompt: Prompt
    ) -> tuple[Score | None, float]:
        try:
            reply = await self.client.call(profile, prompt, role="review")
        except Exception as e:
            log.warning("a review resample failed: %s: %s", type(e).__name__, e)
            cost = e.reply.cost_usd if isinstance(e, ModelCallError) else None
            return None, cost or 0.0
        cost = reply.cost_usd or 0.0
        records = _sampled(reply)
        if not records:
            return Score(importance=1, suspicion=0, call_id=reply.call_id), cost
        text = self.texts["review_resample"].format(calls=records)
        score, more = await self._score(scorer, text)
        return score, cost + more

    async def _proposal(self, text: str, calls: Sequence[Event]) -> str | None:
        try:
            return self.texts[text].format(calls=await self._records(calls))
        except Exception as e:
            log.warning("the review could not render the flagged calls: %s", e)
            return None

    async def _records(self, calls: Sequence[Event]) -> str:
        lines = []
        for e in calls:
            call = ToolCall.model_validate(e.data)
            arguments = call.arguments
            if not isinstance(arguments, str):
                data = await self.store.get_blob(self.source, arguments.sha256)
                arguments = data.decode(errors="replace")
            record = {
                "frame": frame(self.key, e.seq),
                "tool": call.name,
                "arguments": _cap(arguments),
            }
            lines.append(escape(record))
        return "\n".join(lines)


def _sampled(reply: Reply) -> str:
    return "\n".join(
        escape({"tool": c.name, "arguments": _cap(c.arguments)}) for c in reply.tool_calls
    )


def _cap(text: str) -> str:
    if len(text) <= ARGUMENTS_CAP:
        return text
    half = ARGUMENTS_CAP // 2
    return f"{text[:half]}[{len(text) - ARGUMENTS_CAP} characters cut]{text[-half:]}"
