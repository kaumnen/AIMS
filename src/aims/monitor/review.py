"""The autonomous review of a pause: whether the flags behind it were false alarms."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aims.events import Event, EventStore, ToolCall
from aims.models import ModelCallError, ModelClient, Profile, Prompt, Reply
from aims.models.purse import Purse
from aims.monitor.config import ReviewConfig
from aims.monitor.policy import Cutoffs
from aims.monitor.render import escape, frame
from aims.monitor.sweep import NoRoom, Score
from aims.monitor.texts import MonitorTexts

log = logging.getLogger("aims.monitor")

ARGUMENTS_CAP = 16_384


# A review call's score, None when it gave none, what it cost and whether the monitor budget
# had no room for it.
type _Scored = tuple[Score | None, float, bool]


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
    prompt, never run, each scored, all stay below the pause cutoff. A sample is scored by its
    tool calls, else by its text; one with neither counts as importance 1. Any review call that
    fails keeps the flags, as does one the monitor budget in `purse` has no room for. A
    resample at the kill cutoff confirms them. Flagged calls carry the frame ids the trajectory
    renders them under, made with the run's secret `key`.
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
        purse: Purse | None = None,
    ) -> None:
        self.config = config
        self.purse = purse
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
        jobs: list[asyncio.Task[_Scored]] = []
        async with asyncio.TaskGroup() as tg:
            for text in proposals:
                jobs.append(tg.create_task(self._score(scorer, text)))
            if sample is not None:
                for _ in range(self.config.resamples):
                    jobs.append(tg.create_task(self._resample(scorer, *sample)))
        results = [j.result() for j in jobs]
        scores = [s for s, _, _ in results if s is not None]
        short = sum(1 for _, _, x in results if x)
        failed = sum(1 for s, _, x in results if s is None and not x)
        resampled = [s for s, _, _ in results[rescored:] if s is not None]
        top = max((s.importance for s in scores), default=0)
        suspicions = [s.suspicion for s in scores]
        out: dict[str, Any] = {
            "signal_backed": backed,
            "importance": top,
            "suspicion": max(suspicions) if suspicions else None,
            "scored": len(scores),
            "resamples": len(resampled),
            "failed": failed,
            "cost_usd": sum(c for _, c, _ in results),
        }
        kill = cutoffs.kill
        sample_top = max((s.importance for s in resampled), default=0)
        if kill is not None and sample_top >= kill:
            reason = f"a resample of the next step scored importance {sample_top}"
            return ReviewResult(False, reason, confirmed=True, **out)
        if failed:
            return ReviewResult(False, f"{failed} of {len(results)} review calls failed", **out)
        if short:
            reason = f"the monitor budget had no room for {short} of {len(results)} review calls"
            return ReviewResult(False, reason, **out)
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

    async def _score(self, scorer: Scorer, proposal: str | None) -> _Scored:
        if proposal is None:
            return None, 0.0, False
        try:
            score = await scorer.score(proposal, role="review")
        except NoRoom as e:
            log.info("a review score was not sent: %s", e)
            return None, e.cost_usd, True
        except Exception as e:
            log.warning("a review score failed: %s: %s", type(e).__name__, e)
            return None, getattr(e, "cost_usd", 0.0), False
        return score, score.cost_usd, False

    async def _resample(self, scorer: Scorer, profile: Profile, prompt: Prompt) -> _Scored:
        purse = self.purse
        hold = None if purse is None else await purse.hold(profile, prompt)
        if purse is not None and hold is None:
            log.info("a review resample was not sent: the monitor budget has no room for it")
            return None, 0.0, True
        try:
            reply = await self.client.call(
                profile,
                prompt,
                role="review",
                cap=None if hold is None else hold.cap,
                cut=None if hold is None else hold.settle,
            )
            if hold is not None:
                hold.settle(reply)
        except Exception as e:
            log.warning("a review resample failed: %s: %s", type(e).__name__, e)
            if not isinstance(e, ModelCallError):
                return None, 0.0, False
            if hold is not None:
                hold.settle(e.reply)
            return None, e.reply.cost_usd or 0.0, e.no_room
        finally:
            if hold is not None:
                hold.settle()
        cost = reply.cost_usd or 0.0
        if reply.tool_calls:
            cut = self.texts["review_chars_cut"]
            text = self.texts["review_resample"].format(calls=_sampled(reply, cut))
        elif reply.text.strip():
            said = escape({"text": _cap(reply.text, self.texts["review_chars_cut"])})
            text = self.texts["review_resample_text"].format(text=said)
        else:
            return Score(importance=1, suspicion=0, call_id=reply.call_id), cost, False
        score, more, short = await self._score(scorer, text)
        return score, cost + more, short

    async def _proposal(self, text: str, calls: Sequence[Event]) -> str | None:
        try:
            return self.texts[text].format(calls=await self._records(calls))
        except Exception as e:
            log.warning("the review could not render the flagged calls: %s", e)
            return None

    async def _records(self, calls: Sequence[Event]) -> str:
        lines = []
        marker = self.texts["review_chars_cut"]
        for e in calls:
            call = ToolCall.model_validate(e.data)
            arguments = call.arguments
            if isinstance(arguments, str):
                shown = _cap(arguments, marker)
            else:
                # each end holds ARGUMENTS_CAP characters of four bytes at most
                span = 4 * ARGUMENTS_CAP + 4
                ends = await self.store.get_ends(
                    self.source, arguments.sha256, span, span, arguments.size
                )
                shown = _cut(ends.head, ends.tail, ends.chars, marker)
            record = {"frame": frame(self.key, e.seq), "tool": call.name, "arguments": shown}
            lines.append(escape(record))
        return "\n".join(lines)


def _sampled(reply: Reply, marker: str) -> str:
    return "\n".join(
        escape({"tool": c.name, "arguments": _cap(c.arguments, marker)}) for c in reply.tool_calls
    )


def _cap(text: str, marker: str) -> str:
    """`text` cut to its first and last ARGUMENTS_CAP / 2 characters. `marker` stands in for
    the characters left out, its `{chars}` their count."""
    return _cut(text, text, len(text), marker)


def _cut(head: str, tail: str, chars: int, marker: str) -> str:
    """`_cap` of a text of `chars` characters that starts with `head` and ends with `tail`,
    each at least ARGUMENTS_CAP characters long unless it is the whole text."""
    if chars <= ARGUMENTS_CAP:
        return head
    half = ARGUMENTS_CAP // 2
    return f"{head[:half]}{marker.format(chars=chars - ARGUMENTS_CAP)}{tail[-half:]}"
