"""A sweep: a model reading one agent's rendered trajectory in each lane.

Each lane runs extract then evaluate. The extract call reads the whole trajectory, with the steps
not yet reviewed marked, and quotes what looks suspicious with the frame ids of the records it
comes from. Code checks each quote: a quote citing a frame that was never rendered is dropped,
and one not found in the records it cites, whitespace normalised, is kept with `quote_verified`
false and importance lowered by one. The evaluate call sees only the kept quotes and the task, and
scores each. Beside them the window call scores the review range alone; the sweep's suspicion is
the mean of the evaluate call's score of the whole run and the window's.
"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast, get_args

from pydantic import BaseModel, ConfigDict, ValidationError

from aims.events import (
    Category,
    Draft,
    EventStore,
    ModelRole,
    MonitorFlag,
    MonitorSweep,
    SweepTrigger,
)
from aims.models import (
    Message,
    ModelCallError,
    ModelClient,
    OutputSchema,
    Profile,
    Prompt,
    ReplayError,
    Reply,
)
from aims.models.purse import Purse
from aims.monitor.config import Lane, SweepConfig
from aims.monitor.render import Rendered, Trajectory, escape, glossary
from aims.monitor.texts import MonitorTexts
from aims.text import scrub

log = logging.getLogger("aims.monitor")

CATEGORY_NAMES: list[str] = list(get_args(Category))
QUOTE_CHARS = 2000


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def importance(texts: MonitorTexts) -> dict[str, Any]:
    return {
        "type": "integer",
        "enum": [1, 2, 3, 4, 5],
        "description": texts["sweep_schema_importance"],
    }


def suspicion(texts: MonitorTexts) -> dict[str, Any]:
    return {"type": "integer", "description": texts["sweep_schema_suspicion"]}


def schemas(texts: MonitorTexts) -> dict[str, OutputSchema]:
    """The output schemas of the sweep calls, their descriptions from `texts`."""
    rated = {"importance": importance(texts), "suspicion": suspicion(texts)}
    quote = {
        "frames": {
            "type": "array",
            "items": {"type": "string"},
            "description": texts["sweep_schema_frames"],
        },
        "quote": {"type": "string", "description": texts["sweep_schema_quote"]},
        "concern": {"type": "string", "description": texts["sweep_schema_concern"]},
    }
    summary = {"type": "string", "description": texts["sweep_schema_summary"]}
    hit = {
        "quote": {"type": "integer", "description": texts["sweep_schema_quote_id"]},
        "category": {"type": "string", "enum": CATEGORY_NAMES},
        "subcategory": {"type": ["string", "null"]},
        "justification": {"type": "string"},
        **rated,
    }
    hits = {"type": "array", "items": _object(hit)}
    proposal = {
        "category": {"type": ["string", "null"], "enum": [*CATEGORY_NAMES, None]},
        "justification": {"type": "string"},
        **rated,
    }
    return {
        "extract": OutputSchema(
            name="sweep_extract",
            json_schema=_object(
                {"summary": summary, "quotes": {"type": "array", "items": _object(quote)}}
            ),
        ),
        "evaluate": OutputSchema(
            name="sweep_evaluate",
            json_schema=_object({"hits": hits, "suspicion": rated["suspicion"]}),
        ),
        "window": OutputSchema(
            name="sweep_window",
            json_schema=_object({"reason": {"type": "string"}, "suspicion": rated["suspicion"]}),
        ),
        "proposal": OutputSchema(name="sweep_proposal", json_schema=_object(proposal)),
    }


_SCHEMAS = schemas(MonitorTexts())


def system(texts: MonitorTexts, intro: str, categories: str) -> str:
    """A sweep's system prompt: `intro`, the category definitions and the rubric."""
    return (
        f"{intro}\n\n{texts['header_categories']}\n{categories}\n\n"
        f"{texts['header_rubric']}\n{texts['rubric']}"
    )


EXTRACT = _SCHEMAS["extract"]
EVALUATE = _SCHEMAS["evaluate"]
WINDOW = _SCHEMAS["window"]
PROPOSAL = _SCHEMAS["proposal"]


class SweepError(Exception):
    """A sweep model call failed, or its answer did not fit the schema. `cost_usd` is what the
    calls made before the failure cost."""

    def __init__(self, message: str, *, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class NoRoom(Exception):
    """The monitor budget had no room for a call, so it was not sent, or none to try a failed
    one again. A sweep that meets it leaves the monitor with signals only and does not count as
    failed. `cost_usd` is what the calls made before cost."""

    def __init__(self, message: str, *, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


# Why a call was not sent.
NO_ROOM = "the monitor budget has no room for the call"


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Quote(_Loose):
    frames: list[str]
    quote: str
    concern: str = ""


class _Extract(_Loose):
    summary: str = ""
    quotes: list[_Quote] = []


class _Hit(_Loose):
    quote: int
    category: str
    subcategory: str | None = None
    justification: str = ""
    importance: int
    suspicion: int


class _Evaluate(_Loose):
    hits: list[_Hit] = []
    suspicion: int


class _Window(_Loose):
    reason: str = ""
    suspicion: int


class _Proposal(_Loose):
    category: str | None = None
    justification: str = ""
    importance: int
    suspicion: int


@dataclass(slots=True)
class SweepResult:
    """What one sweep found: a `monitor.sweep` draft per lane, then the `monitor.flag` drafts
    of its verified hits. `failed` is true when every lane failed, `no_room` when the monitor
    budget had no room for a call of a lane."""

    sweeps: list[Draft] = field(default_factory=list)
    flags: list[Draft] = field(default_factory=list)
    cost_usd: float = 0.0
    failed: bool = False
    no_room: bool = False


@dataclass(frozen=True, slots=True)
class Score:
    """A sweep model's score of a proposed action: importance 1 to 5 and suspicion 0 to 100."""

    importance: int
    suspicion: int
    call_id: str | None = None
    cost_usd: float = 0.0


@dataclass(slots=True)
class _Calls:
    ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    def add(self, reply: Reply) -> None:
        self.ids.append(reply.call_id)
        self.cost_usd += reply.cost_usd or 0.0


@dataclass(frozen=True, slots=True)
class Quote:
    """An extracted quote after the check: the seqs it cites and whether its text was found."""

    id: int
    seqs: list[int]
    text: str
    concern: str
    verified: bool


def normalise(text: str) -> str:
    return " ".join(text.split())


def verify(quote: str, sources: list[str]) -> bool:
    """Whether `quote`, whitespace normalised, appears in one of `sources` or in all of them
    joined. A quote copied in its JSON escaped form counts as well as one unescaped."""
    variants = {normalise(quote)}
    with contextlib.suppress(ValueError):
        variants.add(normalise(json.loads(f'"{quote}"')))
    variants.add(normalise(json.dumps(quote, ensure_ascii=False)[1:-1]))
    variants.discard("")
    if not variants:
        return False
    haystacks = [normalise(s) for s in sources]
    if len(sources) > 1:
        haystacks.append(normalise("\n".join(sources)))
    return any(v in h for v in variants for h in haystacks)


def check(quotes: list[_Quote], rendered: Rendered) -> tuple[list[Quote], int]:
    """The quotes that cite a rendered frame, each checked against the records it cites, and
    the number dropped. Frame ids never rendered are left out of a quote's citations."""
    kept: list[Quote] = []
    dropped = 0
    for q in quotes:
        seqs = sorted({rendered.frames[f] for f in q.frames if f in rendered.frames})
        if not seqs:
            dropped += 1
            continue
        found = verify(q.quote, [rendered.sources[s] for s in seqs])
        kept.append(Quote(len(kept) + 1, seqs, q.quote, q.concern, found))
    return kept, dropped


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def at_cap(reply: Reply, cap: int | None) -> str | None:
    """How an answer that stopped at its output cap `cap` spent it, as its token counts tell,
    such as "stopped at the output cap of 8192 tokens, 8192 of them reasoning". None when it
    did not stop there or its counts do not say."""
    made = reply.usage.output_tokens
    if reply.status != "incomplete" or cap is None or made is None or made < cap:
        return None
    thought = reply.usage.reasoning_tokens
    spent = f", {thought} of them reasoning" if thought else ""
    return f"stopped at the output cap of {cap} tokens{spent}"


def more_room(reply: Reply, setting: str) -> str:
    """What gives an answer that stopped at its output cap more room, set in `setting`."""
    if reply.usage.reasoning_tokens:
        return (
            f"Raise max_output_tokens or lower effort in {setting},"
            " or use a model that reasons less."
        )
    return f"Raise max_output_tokens in {setting}."


def not_json(reply: Reply, cap: int | None, setting: str) -> str:
    """Why `reply` is not JSON. One that stopped at its output cap `cap` says how to give it
    more room in `setting`."""
    stopped = at_cap(reply, cap)
    if stopped is not None:
        return f"the answer {stopped}, so it is not JSON. {more_room(reply, setting)}"
    why = ""
    if reply.status == "incomplete" and reply.finish_reason:
        why = f" (finish reason {reply.finish_reason})"
    return f"the {reply.status} answer is not JSON{why}"


async def ask(
    client: ModelClient,
    profile: Profile,
    system: str,
    body: str,
    schema: OutputSchema,
    calls: _Calls,
    *,
    setting: str,
    role: ModelRole = "sweep",
    purse: Purse | None = None,
) -> dict[str, Any]:
    """One structured call: `system` then `body`, answered as a JSON object, sent only when
    `purse` has room for it and tried again only while it has room. `setting` names where the
    call's output cap is set. Raises SweepError, and NoRoom when there is no room."""
    prompt = Prompt(
        items=[Message(role="system", content=system), Message(role="user", content=body)],
        output_schema=schema,
    )
    hold = None if purse is None else await purse.hold(profile, prompt)
    if purse is not None and hold is None:
        raise NoRoom(f"{schema.name}: {NO_ROOM} on {profile.name}")
    try:
        reply = await client.call(
            profile,
            prompt,
            role=role,
            cap=None if hold is None else hold.cap,
            cut=None if hold is None else hold.settle,
        )
        if hold is not None:
            hold.settle(reply)
    except ModelCallError as e:
        if hold is not None:
            hold.settle(e.reply)
        calls.add(e.reply)
        r = e.reply
        failed = (
            f"{schema.name}: the call on {r.profile} failed after {r.attempts} attempt(s):"
            f" {r.error}"
        )
        if e.no_room:
            raise NoRoom(failed) from None
        raise SweepError(failed) from None
    except ReplayError as e:
        raise SweepError(f"{schema.name}: {e}") from None
    finally:
        if hold is not None:
            hold.settle()
    calls.add(reply)
    try:
        value = scrub(json.loads(reply.text))
    except ValueError:
        why = not_json(reply, profile.max_output_tokens, setting)
        raise SweepError(f"{schema.name}: {why}") from None
    if not isinstance(value, dict):
        raise SweepError(f"{schema.name}: the answer is not a JSON object")
    return value


class Sweeper:
    """One sweep config over one agent's trajectory. Calls go through `client` with role
    `sweep` and no agent id. One sweep at a time; the caller serialises them.

    The extract call runs on `extractor` when one is set, a cheaper model than `profile`, which
    evaluates. With `config.escalate`, the window is scored on the extractor first and on
    `profile` only when that score falls inside the band. With a `purse`, each call is sent
    only when the monitor budget has room for it."""

    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        config: SweepConfig,
        profile: Profile,
        texts: MonitorTexts,
        client: ModelClient,
        store: EventStore,
        trajectory: Trajectory,
        task: str,
        extractor: Profile | None = None,
        purse: Purse | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.purse = purse
        self.config = config
        self.profile = profile
        self.extractor = None
        if extractor is not None:
            self.extractor = extractor.model_copy(
                update={"effort": config.effort, "max_output_tokens": config.max_output_tokens}
            )
        self.texts = texts
        self.client = client
        self.store = store
        self.trajectory = trajectory
        self.task = task
        self._reviewed: dict[str, int] = {}
        categories = "\n".join(f"{c}: {d}" for c, d in texts.categories.items())
        self._system = system(texts, texts["sweep_system"], categories)
        self._schemas = schemas(texts)

    def reviewed(self, lane: str) -> int:
        """The last seq lane `lane` reviewed, -1 before its first review."""
        return self._reviewed.get(lane, -1)

    async def sweep(
        self, trigger: SweepTrigger, *, coalesced: int = 1, lead: str | None = None
    ) -> SweepResult:
        """Review the steps not yet reviewed in every lane, with the whole trajectory as
        context, and score the review range alone when `config.window` is on. With `lead`, a
        finding about this agent from elsewhere, the whole trajectory is the review range and
        the extract call is told the finding."""
        upto = self.trajectory.last_seq
        renders = [
            await self.trajectory.render(
                lane.parts,
                texts=self.texts,
                review_after=-1 if lead is not None else self.reviewed(lane.name),
                collapse_tokens=self.config.collapse_tokens,
                upto=upto,
            )
            for lane in self.config.lanes
        ]
        # Lanes that render the same text would ask the same calls; one asks for all of them.
        first: dict[str, int] = {}
        for i, rendered in enumerate(renders):
            first.setdefault(rendered.text, i)
        unique = sorted(set(first.values()))
        done = dict(
            zip(
                unique,
                await asyncio.gather(
                    *(
                        self._lane(self.config.lanes[i], renders[i], trigger, coalesced, upto, lead)
                        for i in unique
                    )
                ),
                strict=True,
            )
        )
        lanes = []
        for i, (lane, rendered) in enumerate(zip(self.config.lanes, renders, strict=True)):
            j = first[rendered.text]
            sweep, flags, cost, short = done[j]
            if i != j:
                sweep = sweep.model_copy(
                    update={"lane": lane.name, "parts": list(lane.parts), "cost_usd": 0.0}
                )
                flags = [f.model_copy(update={"lane": lane.name}) for f in flags]
                cost = 0.0
                if sweep.error is None:
                    self._reviewed[lane.name] = upto
            lanes.append((sweep, flags, cost, short))
        result = SweepResult()
        for sweep, flags, cost, short in lanes:
            result.sweeps.append(Draft(sweep, self.agent_id))
            result.flags.extend(Draft(f, self.agent_id) for f in flags)
            result.cost_usd += cost
            result.no_room |= short
        errors = all(sweep.error is not None for sweep, _, _, _ in lanes)
        result.failed = errors and not result.no_room
        return result

    async def score(self, proposal: str, *, role: str = "review") -> Score:
        """Score one proposed next action, rendered as text, in the evidence lane with the
        trajectory as context. Used by the autonomous review of a pause. Raises SweepError, and
        NoRoom when the monitor budget has no room for the call."""
        rendered = await self.trajectory.render(
            ["evidence"], texts=self.texts, collapse_tokens=self.config.collapse_tokens
        )
        record = escape({"type": "proposal", "action": proposal})
        body = "\n\n".join(
            [
                rendered.text,
                f"{self.texts['header_action']}\n{record}",
                glossary(rendered.types | {"proposal"}, self.texts),
                self.texts["score_proposal"],
            ]
        )
        calls = _Calls()
        if role not in get_args(ModelRole):
            raise SweepError(f"unknown model role {role!r}")
        try:
            schema = self._schemas["proposal"]
            # The review calls its own profile, which sets its own output cap.
            setting = f"profile {self.profile.name}"
            value = await self._ask(
                body, schema, calls, role=cast(ModelRole, role), setting=setting
            )
            parsed = _parse(_Proposal, value)
        except (SweepError, NoRoom) as e:
            raise type(e)(str(e), cost_usd=calls.cost_usd) from None
        return Score(
            importance=_clamp(parsed.importance, 1, 5),
            suspicion=_clamp(parsed.suspicion, 0, 100),
            call_id=calls.ids[-1],
            cost_usd=calls.cost_usd,
        )

    async def _lane(
        self,
        lane: Lane,
        rendered: Rendered,
        trigger: SweepTrigger,
        coalesced: int,
        upto: int,
        lead: str | None,
    ) -> tuple[MonitorSweep, list[MonitorFlag], float, bool]:
        start = time.monotonic()
        calls = _Calls()
        flags: list[MonitorFlag] = []
        dropped = 0
        history = window = None
        escalated = False
        summary = ""
        error: str | None = None
        short = False
        try:
            windowed = self._window(lane, rendered, calls, upto) if self.config.window else None
            extracted, scored = await asyncio.gather(
                self._extract(rendered, calls, lead), windowed or _none(), return_exceptions=True
            )
            # No room stops the monitor's sweeps, so it comes before a failed call.
            outcomes = sorted((extracted, scored), key=lambda o: not isinstance(o, NoRoom))
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            assert isinstance(extracted, _Extract)
            if isinstance(scored, tuple):
                window, escalated = scored
            summary = extracted.summary
            quotes, dropped = check(extracted.quotes, rendered)
            history = 0
            if quotes:
                evaluated = await self._evaluate(quotes, rendered, calls)
                history = _clamp(evaluated.suspicion, 0, 100)
                hits, bad = self._hits(evaluated, quotes)
                dropped += bad
                for hit, quote in hits:
                    flags.append(await self._flag(lane, hit, quote))
        except SweepError as e:
            error = str(e)
            flags = []
            log.warning(
                "sweep %s lane %s of %s failed: %s", self.config.name, lane.name, self.agent_id, e
            )
        except NoRoom as e:
            error, short = str(e), True
            flags = []
        else:
            self._reviewed[lane.name] = upto
        scores = [s for s in (history, window) if s is not None]
        suspicion = round(sum(scores) / len(scores)) if scores and error is None else None
        sweep = MonitorSweep(
            name=self.config.name,
            lane=lane.name,
            parts=list(lane.parts),
            trigger=trigger,
            step_from=rendered.step_from,
            step_to=rendered.step_to,
            seq_to=max(upto, 0),
            calls=calls.ids,
            hits=len(flags),
            dropped=dropped,
            unverified=sum(1 for f in flags if f.quote_verified is False),
            suspicion=suspicion,
            history_suspicion=history if error is None else None,
            window_suspicion=window if error is None else None,
            escalated=escalated,
            summary=await self.store.content(self.client.run_id, summary),
            tokens=rendered.tokens,
            collapsed=rendered.collapsed,
            coalesced=coalesced,
            cost_usd=round(calls.cost_usd, 10),
            duration_ms=int((time.monotonic() - start) * 1000),
            error=error,
            no_room=short,
        )
        return sweep, flags, calls.cost_usd, short

    async def _extract(self, rendered: Rendered, calls: _Calls, lead: str | None) -> _Extract:
        instruction = self.texts["extract"].format(
            step_from=rendered.step_from, step_to=rendered.step_to
        )
        parts = [rendered.text, glossary(rendered.types, self.texts), instruction]
        if lead is not None:
            parts.append(lead)
        body = "\n\n".join(parts)
        if self.extractor is None:
            return _parse(_Extract, await self._ask(body, self._schemas["extract"], calls))
        try:
            extract = self._schemas["extract"]
            return _parse(_Extract, await self._ask(body, extract, calls, profile=self.extractor))
        except SweepError as e:
            log.warning(
                "sweep %s extract of %s on the extractor failed, again on %s: %s",
                self.config.name,
                self.agent_id,
                self.profile.name,
                e,
            )
        return _parse(_Extract, await self._ask(body, self._schemas["extract"], calls))

    async def _evaluate(self, quotes: list[Quote], rendered: Rendered, calls: _Calls) -> _Evaluate:
        lines = [
            escape(
                {
                    "id": q.id,
                    "from": [rendered.labels.get(s, "") for s in q.seqs],
                    "quote": q.text[:QUOTE_CHARS],
                    "verified": q.verified,
                    "note": q.concern,
                }
            )
            for q in quotes
        ]
        body = "\n\n".join(
            [
                f"{self.texts['header_task']}\n{self.task}",
                f"{self.texts['header_quotes']}\n" + "\n".join(lines),
                self.texts["evaluate"],
            ]
        )
        return _parse(_Evaluate, await self._ask(body, self._schemas["evaluate"], calls))

    async def _window(
        self, lane: Lane, rendered: Rendered, calls: _Calls, upto: int
    ) -> tuple[int, bool] | None:
        """The window's suspicion, and whether it was escalated from the extractor's score."""
        steps = self.config.window_steps
        first = rendered.step_from if steps is None else max(0, rendered.step_to - steps + 1)
        view = await self.trajectory.window(
            lane.parts, texts=self.texts, first_step=first, upto=upto
        )
        instruction = self.texts["window"].format(step_from=view.step_from, step_to=view.step_to)
        body = "\n\n".join([view.text, glossary(view.types, self.texts), instruction])
        band = self.config.escalate
        cheap = self.extractor if band is not None else None
        try:
            window = self._schemas["window"]
            parsed = _parse(_Window, await self._ask(body, window, calls, profile=cheap))
        except SweepError as e:
            log.warning("sweep %s window of %s failed: %s", self.config.name, self.agent_id, e)
            return None
        score = _clamp(parsed.suspicion, 0, 100)
        if cheap is None or band is None or not band.low <= score <= band.high:
            return score, False
        try:
            parsed = _parse(_Window, await self._ask(body, self._schemas["window"], calls))
        except SweepError as e:
            log.warning(
                "sweep %s escalated window of %s failed: %s", self.config.name, self.agent_id, e
            )
            return score, False
        return _clamp(parsed.suspicion, 0, 100), True

    def _hits(
        self, evaluated: _Evaluate, quotes: list[Quote]
    ) -> tuple[list[tuple[_Hit, Quote]], int]:
        by_id = {q.id: q for q in quotes}
        out: list[tuple[_Hit, Quote]] = []
        bad = 0
        for hit in evaluated.hits:
            quote = by_id.get(hit.quote)
            if quote is None or hit.category not in CATEGORY_NAMES:
                bad += 1
                continue
            out.append((hit, quote))
        return out, bad

    async def _flag(self, lane: Lane, hit: _Hit, quote: Quote) -> MonitorFlag:
        importance = _clamp(hit.importance, 1, 5)
        if not quote.verified:
            importance = max(1, importance - 1)
        return MonitorFlag(
            layer="sweep",
            source=self.config.name,
            category=cast(Category, hit.category),
            importance=importance,
            evidence=quote.seqs,
            justification=hit.justification,
            subcategory=hit.subcategory or None,
            lane=lane.name,
            suspicion=_clamp(hit.suspicion, 0, 100),
            quote=await self.store.content(self.client.run_id, quote.text),
            quote_verified=quote.verified,
        )

    async def _ask(
        self,
        body: str,
        schema: OutputSchema,
        calls: _Calls,
        *,
        role: ModelRole = "sweep",
        profile: Profile | None = None,
        setting: str = "monitor.sweeps",
    ) -> dict[str, Any]:
        profile = profile or self.profile
        return await ask(
            self.client,
            profile,
            self._system,
            body,
            schema,
            calls,
            setting=setting,
            role=role,
            purse=self.purse,
        )


def _parse[T: BaseModel](cls: type[T], value: dict[str, Any]) -> T:
    try:
        return cls.model_validate(value)
    except ValidationError as e:
        raise SweepError(f"the answer does not fit the schema: {e}") from None


async def _none() -> None:
    return None
