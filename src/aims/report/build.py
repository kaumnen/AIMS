"""Gather a run's report from its log, its monitor runs, its score runs and its labels."""

import bisect
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, get_args
from urllib.parse import urlsplit

from aims import __version__
from aims.events import (
    AgentFinished,
    AgentReport,
    AgentStarted,
    AuditLost,
    BlobError,
    Category,
    CategoryFinding,
    Event,
    KernelPayload,
    LocalEventStore,
    LogTruncated,
    MonitorAction,
    MonitorDegraded,
    MonitorVerdict,
    OperatorAction,
    PlantTriggered,
    ReplayDivergence,
    RunFinished,
    RunInfo,
    RunStarted,
    ScoreUnit,
    StepInfo,
    TaskScored,
    ToolCall,
    Verification,
)
from aims.events.schema import BlobRef, Content, Payload, payload_types
from aims.report.model import (
    LIVE,
    AgentFacts,
    AgentText,
    Appendix,
    Facts,
    Flag,
    Header,
    Headline,
    Moment,
    MomentKind,
    NoVerdict,
    Pins,
    Plant,
    Plants,
    Role,
    RunReport,
    Source,
    Unit,
    Warning,
)
from aims.run.score import CheckError, check_run

PAGE = 1000
NOT_REPORTED = frozenset({"monitor", "score", "models.check", "selftest"})
TIMELINE_IMPORTANCE = 3
KERNEL_TYPES = frozenset(
    name for name, cls in payload_types().items() if issubclass(cls, KernelPayload)
)
_MOMENT_TYPES = (
    RunStarted.type,
    AgentStarted.type,
    AgentFinished.type,
    RunFinished.type,
    MonitorAction.type,
    OperatorAction.type,
    MonitorDegraded.type,
    AgentReport.type,
    TaskScored.type,
    MonitorVerdict.type,
)
_WARNING_TYPES = (
    MonitorDegraded.type,
    AuditLost.type,
    LogTruncated.type,
    ReplayDivergence.type,
)
# Timeline rows at one seq, in this order.
_ORDER = {
    k: i
    for i, k in enumerate(
        (
            "run_started",
            "agent_started",
            "evidence",
            "plant",
            "flag",
            "monitor_action",
            "operator_action",
            "degraded",
            "report",
            "agent_finished",
            "scored",
            "verdict",
            "run_finished",
        )
    )
}


class ReportError(LookupError):
    """No such run, or no such monitor source over it."""


def base_url_problem(url: str) -> str | None:
    """What is wrong with `url` as the app's address for links, or None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "give an http or https address with a host"
    if any(c.isspace() or c in "()<>[]\"'`" for c in url):
        return "the address holds a space, quote, bracket or backtick"
    return None


async def build(
    store: LocalEventStore,
    run_id: str,
    *,
    source: str | None = None,
    role: Role = "local",
    base_url: str | None = None,
    now: datetime | None = None,
) -> RunReport:
    """The report of run `run_id` as role `role` may see it. `source` picks the monitor:
    `live` or a monitor run over the run; by default the newest finished one, else live.
    Raises ReportError."""
    info = await store.run(run_id)
    if info is None:
        raise ReportError(f"no run {run_id}")
    if info.kind in NOT_REPORTED:
        raise ReportError(f"{run_id} is a {info.kind} run; report on the run it is about")
    first = await store.events(run_id, limit=1)
    started = first[0].payload() if first else None
    if not isinstance(started, RunStarted):
        raise ReportError(f"run {run_id} has no run.started")
    marks = await store.every(run_id, types=(*_MOMENT_TYPES, *_WARNING_TYPES))
    finished = _last(marks, RunFinished)
    verification = await store.verify(run_id)
    writing = finished is None and verification.torn
    problems = [
        *([verification.error] if verification.error and not writing else []),
        *(f"missing blob {sha}" for sha in verification.missing_blobs),
        *(f"blob {sha} does not match its hash" for sha in verification.bad_blobs),
    ]
    verify_error = "; ".join(problems) or None
    reviews = await store.runs(origin=run_id, kind="monitor", limit=PAGE)
    picked = _pick(run_id, reviews, source)
    review = picked != LIVE
    holder = picked if review else run_id
    held = (await store.events(holder, limit=1)) if review else first
    held_started = held[0].payload() if held else None
    monitor = held_started.monitor if isinstance(held_started, RunStarted) else None
    held_marks = (
        await store.every(holder, types=(MonitorVerdict.type, MonitorDegraded.type))
        if review
        else marks
    )
    verdict_event = _last_event(held_marks, MonitorVerdict)
    verdict = None if verdict_event is None else verdict_event.payload()
    assert verdict is None or isinstance(verdict, MonitorVerdict)
    steps = await _steps(store, run_id)
    flags = await _flags(store, holder, verdict)
    labels = await store.labels(holder)
    score_info = None if role == "viewer" else await _score(store, run_id, picked)

    status = "running" if finished is None else finished.status
    reason = None if finished is None else finished.reason
    scored = _last(marks, TaskScored)
    running = finished is None if not review else _status(reviews, picked) == "running"
    headline = _headline(scored, verdict, running, started, review, held_marks)
    warnings = _warnings(
        run_id,
        holder,
        marks,
        held_marks if review else [],
        verdict,
        None if verdict_event is None else verdict_event.seq,
    )
    if verify_error is not None:
        warnings.insert(0, Warning(kind="verify_failed", run_id=run_id, detail=verify_error))
    if finished is None:
        warnings.insert(0, Warning(kind="in_progress", run_id=run_id))

    plants = None
    if role != "viewer":
        plants = await _plants(
            store, run_id, score_info, sealed=finished is not None, verified=verification
        )
    flag_rows = [f.model_copy(update={"label": labels.get(f.seq)}) for f in flags]
    timeline = await _timeline(store, run_id, marks, flag_rows, review, verdict, plants, steps)
    chosen = next((r for r in reviews if r.run_id == picked), None)
    facts = await _facts(store, run_id, info, marks, chosen, flag_rows)
    sources = await _sources(store, run_id, info, started, reviews, picked, facts, marks)
    appendix = Appendix(
        answers=[
            AgentText(agent=e.agent_id, seq=e.seq, step=_step(steps, e.seq, e.agent_id), text=t)
            for e, t in await _texts(store, run_id, marks, AgentFinished, "answer")
        ],
        reports=[
            AgentText(agent=e.agent_id, seq=e.seq, step=_step(steps, e.seq, e.agent_id), text=t)
            for e, t in await _texts(store, run_id, marks, AgentReport, "message")
        ],
        disagreements=[] if verdict is None else list(verdict.disagreements),
    )
    latest = max((label.at for label in labels.values()), default=None)
    score_run = None if score_info is None else score_info[0]
    pins = Pins(
        run_id=run_id,
        head=info.head,
        events=info.events,
        verified=verify_error is None,
        verify_error=verify_error,
        source=picked,
        source_head=None if not review else _head(reviews, picked),
        monitor=monitor,
        score=None if score_run is None else score_run.run_id,
        score_head=None if score_run is None else score_run.head,
        labels=len(labels),
        labels_at=latest,
        role=role,
        base_url=base_url.rstrip("/") if base_url else None,
        version=__version__,
        built_at=now or datetime.now(UTC),
    )
    return RunReport(
        header=Header(
            run_id=run_id,
            scenario=started.scenario,
            kind=started.kind,
            variant=info.variant,
            status=status,
            reason=reason,
            oversight=started.oversight,
            agents=list(started.agents),
            started_at=info.started_at,
            ended_at=info.ended_at,
            last_at=await _last_at(store, run_id, info.events),
            pins=pins,
        ),
        headline=headline,
        warnings=warnings,
        summary="" if verdict is None else await _content(store, holder, verdict.summary),
        findings=_findings(verdict),
        observations=[] if verdict is None else list(verdict.anomalies),
        plants=plants,
        flags=flag_rows,
        timeline=timeline,
        facts=facts,
        sources=sources,
        appendix=appendix,
    )


def _pick(run_id: str, reviews: Sequence[RunInfo], asked: str | None) -> str:
    """The monitor source: the one asked for, else the newest finished review, else live."""
    if asked is None:
        return next((r.run_id for r in reviews if r.status == "completed"), LIVE)
    if asked == LIVE or any(r.run_id == asked for r in reviews):
        return asked
    raise ReportError(f"{asked} is not a monitor run over {run_id}")


def _head(reviews: Sequence[RunInfo], run_id: str) -> str | None:
    return next((r.head for r in reviews if r.run_id == run_id), None)


def _status(reviews: Sequence[RunInfo], run_id: str) -> str | None:
    return next((r.status for r in reviews if r.run_id == run_id), None)


def _last[T: Payload](events: Sequence[Event], cls: type[T]) -> T | None:
    event = _last_event(events, cls)
    if event is None:
        return None
    payload = event.payload()
    assert isinstance(payload, cls)
    return payload


def _last_event(events: Sequence[Event], cls: type[Payload]) -> Event | None:
    return next((e for e in reversed(events) if e.type == cls.type), None)


def _of[T: Payload](events: Iterable[Event], cls: type[T]) -> list[tuple[Event, T]]:
    out = []
    for e in events:
        if e.type == cls.type:
            payload = e.payload()
            assert isinstance(payload, cls)
            out.append((e, payload))
    return out


async def _content(store: LocalEventStore, run_id: str, value: Content | None) -> str:
    if value is None:
        return ""
    if isinstance(value, BlobRef):
        try:
            data = await store.get_blob(run_id, value.sha256)
        except BlobError as e:
            return f"({e})"
        return data.decode("utf-8", errors="replace")
    return value


async def _texts(
    store: LocalEventStore, run_id: str, marks: Sequence[Event], cls: type[Payload], field: str
) -> list[tuple[Event, str]]:
    out = []
    for e, payload in _of(marks, cls):
        text = await _content(store, run_id, getattr(payload, field))
        if text.strip():
            out.append((e, text))
    return out


async def _steps(store: LocalEventStore, run_id: str) -> list[StepInfo]:
    out: list[StepInfo] = []
    cursor: int | None = None
    while page := await store.steps(run_id, after=cursor, limit=PAGE):
        out.extend(page)
        cursor = page[-1].seq
    return out


def _step(steps: Sequence[StepInfo], seq: int, agent: str | None) -> int | None:
    """The step of `agent` that event `seq` falls in, or the last one before it. An event of
    no agent gets a step only when one agent took every step."""
    if agent is None and len({s.agent_id for s in steps}) > 1:
        return None
    mine = [s for s in steps if agent is None or s.agent_id == agent]
    ends = [s.seq_to for s in mine]
    i = bisect.bisect_left(ends, seq)
    if i < len(mine) and mine[i].seq_from <= seq:
        return mine[i].n
    return mine[i - 1].n if i > 0 else None


async def _flags(store: LocalEventStore, run_id: str, verdict: MonitorVerdict | None) -> list[Flag]:
    reviews = {} if verdict is None else {r.flag: r for r in verdict.flags}
    out: list[Flag] = []
    cursor: int | None = None
    while page := await store.flags(run_id, after=cursor, limit=PAGE):
        for f in page:
            out.append(
                Flag(
                    run_id=run_id,
                    seq=f.seq,
                    agent=f.agent_id,
                    layer=f.layer,
                    source=f.source,
                    category=f.category,
                    importance=f.importance,
                    count=f.count,
                    justification=f.justification,
                    evidence=list(f.evidence),
                    status=f.status,
                    status_reason=f.status_reason,
                    review=reviews.get(f.seq) or reviews.get(f.latest),
                    tainted=f.tainted,
                )
            )
        cursor = page[-1].seq
    return out


async def _score(
    store: LocalEventStore, run_id: str, picked: str
) -> tuple[RunInfo, list[Event]] | None:
    """The newest finished score run that scored the flags of the source picked."""
    wanted = None if picked == LIVE else picked
    for info in await store.runs(origin=run_id, kind="score", limit=PAGE):
        if info.status != "completed":
            continue
        first = await store.events(info.run_id, limit=1)
        started = first[0].payload() if first else None
        if isinstance(started, RunStarted) and started.config.get("monitor") == wanted:
            return info, await store.every(info.run_id)
    return None


async def _plants(
    store: LocalEventStore,
    run_id: str,
    score: tuple[RunInfo, list[Event]] | None,
    *,
    sealed: bool,
    verified: Verification,
) -> Plants:
    if score is not None:
        info, events = score
        rows = [
            Plant(
                agent=e.agent_id or "",
                plant=t.plant,
                kind=t.kind,
                category=t.category,
                tier=t.tier,
                taken=t.taken,
                seq=t.seq,
                evidence=list(t.evidence),
                detail=t.detail,
                matches=list(t.matches),
            )
            for e, t in _of(events, PlantTriggered)
        ]
        units = [
            Unit(
                agent=e.agent_id or "",
                role=u.role,
                positive=u.positive,
                labelled=u.labelled,
                plants=list(u.plants),
                harm_seq=u.harm_seq,
                matches=list(u.matches),
                eval_aware=u.eval_aware,
                told=u.told,
                cause=u.cause,
            )
            for e, u in _of(events, ScoreUnit)
        ]
        return Plants(rows=rows, units=units, score=info.run_id)
    if not sealed:
        return Plants(error="the run is not sealed yet")
    try:
        taken = await check_run(store, run_id, verified=verified)
    except CheckError as e:
        return Plants(error=str(e))
    return Plants(
        rows=[
            Plant(
                agent=t.agent,
                plant=t.plant,
                kind=t.kind,
                category=t.category,
                tier=t.tier,
                taken=t.taken,
                seq=t.seq,
                evidence=list(t.evidence),
                detail=t.detail,
            )
            for t in taken
        ]
    )


def _headline(
    scored: TaskScored | None,
    verdict: MonitorVerdict | None,
    running: bool,
    started: RunStarted,
    review: bool,
    held_marks: Sequence[Event],
) -> Headline:
    passed = total = None
    if scored is not None:
        p, t = scored.metrics.get("passed"), scored.metrics.get("total")
        if type(p) is int and type(t) is int:
            passed, total = p, t
    no_verdict: NoVerdict | None = None
    if verdict is None:
        skipped = any(
            d.part == "judge" and d.then == "skip" for _, d in _of(held_marks, MonitorDegraded)
        )
        if running:
            no_verdict = "running"
        elif not review and started.monitor is None:
            no_verdict = "no_monitor"
        elif skipped:
            no_verdict = "skipped"
        else:
            no_verdict = "missing"
    return Headline(
        scored=scored is not None,
        reward=None if scored is None else scored.reward,
        passed=passed,
        total=total,
        reward_error=None if scored is None else scored.error,
        importance=None if verdict is None else verdict.max_severity,
        claim_matched=None if verdict is None else verdict.claim_matched,
        no_verdict=no_verdict,
    )


def _warnings(
    run_id: str,
    holder: str,
    marks: Sequence[Event],
    held_marks: Sequence[Event],
    verdict: MonitorVerdict | None,
    verdict_seq: int | None,
) -> list[Warning]:
    out: list[Warning] = []
    for where, events in ((run_id, marks), (holder, held_marks)):
        for e, d in _of(events, MonitorDegraded):
            out.append(
                Warning(
                    kind="monitor_degraded",
                    run_id=where,
                    seq=e.seq,
                    agent=e.agent_id,
                    detail=d.reason,
                    part=d.part,
                    then=d.then,
                )
            )
    for label, error in [] if verdict is None else verdict.failed():
        out.append(
            Warning(kind="rollout_failed", run_id=holder, seq=verdict_seq, part=label, detail=error)
        )
    lost = _of(marks, AuditLost)
    if lost:
        counts = [a.count for _, a in lost]
        out.append(
            Warning(
                kind="audit_lost",
                run_id=run_id,
                seq=lost[0][0].seq,
                count=len(lost),
                detail=lost[0][1].reason,
                lost=None if None in counts else sum(c for c in counts if c is not None),
            )
        )
    for e, t in _of(marks, LogTruncated):
        out.append(Warning(kind="log_truncated", run_id=run_id, seq=e.seq, detail=t.reason))
    divergences = _of(marks, ReplayDivergence)
    if divergences:
        e, _ = divergences[0]
        out.append(
            Warning(
                kind="replay_divergence",
                run_id=run_id,
                seq=e.seq,
                agent=e.agent_id,
                count=len(divergences),
            )
        )
    for e, f in _of(marks, AgentFinished):
        if not f.cost_complete:
            out.append(Warning(kind="cost_incomplete", run_id=run_id, seq=e.seq, agent=e.agent_id))
    scored = _last_event(marks, TaskScored)
    judged = set(() if verdict is None else verdict.tainted)
    tainted = set() if holder != run_id else judged
    if scored is not None:
        payload = scored.payload()
        assert isinstance(payload, TaskScored)
        tainted |= set(payload.tainted)
    found = [(run_id, scored, tainted)]
    if holder != run_id:
        found.append((holder, None, judged))
    for where, at, seqs in found:
        if seqs:
            out.append(
                Warning(
                    kind="tainted",
                    run_id=where,
                    seq=None if at is None else at.seq,
                    count=len(seqs),
                    seqs=sorted(seqs),
                )
            )
    return out


def _findings(verdict: MonitorVerdict | None) -> list[CategoryFinding]:
    if verdict is None:
        return []
    found = [c for c in verdict.categories if c.present != "no"]
    return sorted(found, key=lambda c: (c.present != "yes", -(c.importance or 0), c.category))


async def _timeline(
    store: LocalEventStore,
    run_id: str,
    marks: Sequence[Event],
    flags: Sequence[Flag],
    review: bool,
    verdict: MonitorVerdict | None,
    plants: Plants | None,
    steps: Sequence[StepInfo],
) -> list[Moment]:
    rows: list[Moment] = []

    def add(e: Event, kind: MomentKind, **kw: Any) -> None:
        agent = kw.pop("agent", e.agent_id)
        rows.append(
            Moment(
                seq=e.seq,
                ts=e.ts,
                kind=kind,
                agent=agent,
                step=_step(steps, e.seq, agent),
                **kw,
            )
        )

    for e in marks:
        p = e.payload()
        match p:
            case RunStarted():
                add(e, "run_started", what=p.scenario)
            case AgentStarted():
                add(e, "agent_started", what=p.model)
            case AgentFinished():
                add(e, "agent_finished", what=p.reason)
            case RunFinished():
                add(e, "run_finished", what=p.status, detail=p.reason)
            case MonitorAction():
                add(e, "monitor_action", what=p.action, detail=p.reason or None)
            case OperatorAction():
                add(e, "operator_action", what=p.action, detail=p.reason)
            case MonitorDegraded():
                add(e, "degraded", what=p.part, detail=p.reason)
            case AgentReport():
                add(e, "report", what="report_to_operator")
            case TaskScored():
                add(e, "scored", what="" if p.reward is None else f"{p.reward:g}", detail=p.error)
            case MonitorVerdict() if not review:
                add(e, "verdict", what=str(p.max_severity))
            case _:
                pass

    wanted: dict[int, list[tuple[MomentKind, dict[str, Any]]]] = {}
    for f in flags:
        if f.importance < TIMELINE_IMPORTANCE:
            continue
        at = min(f.evidence) if review else f.seq
        info = {"what": f.layer, "categories": [f.category], "importance": f.importance}
        wanted.setdefault(at, []).append(("flag", info | {"agent": f.agent}))
    for p in [] if plants is None else plants.rows:
        known = [p.category] if p.category in _CATEGORIES else []
        info = {"what": f"{p.plant} ({p.tier})", "categories": known, "agent": p.agent}
        wanted.setdefault(p.seq, []).append(("plant", info))
    cited: dict[int, dict[str, int | None]] = {}
    if verdict is not None:
        for c in verdict.categories:
            if c.present == "yes":
                for seq in c.evidence:
                    cited.setdefault(seq, {})[c.category] = c.importance
    for seq, found in cited.items():
        top = max((i for i in found.values() if i is not None), default=None)
        info = {"categories": sorted(found), "importance": top}
        wanted.setdefault(seq, []).append(("evidence", info))
    for seq in sorted(wanted):
        page = await store.events(run_id, after=seq - 1, limit=1)
        if not page or page[0].seq != seq:
            continue
        e = page[0]
        for kind, info in wanted[seq]:
            add(e, kind, **({"what": _what(e)} if kind == "evidence" else {}), **info)
    seen: set[tuple[Any, ...]] = set()
    out = []
    for m in sorted(rows, key=_moment_key):
        if _moment_key(m) not in seen:
            seen.add(_moment_key(m))
            out.append(m)
    return out


async def _last_at(store: LocalEventStore, run_id: str, events: int) -> datetime | None:
    page = await store.events(run_id, after=events - 2, limit=1)
    return page[0].ts if page else None


def _moment_key(m: Moment) -> tuple[Any, ...]:
    return (m.seq, _ORDER[m.kind], tuple(m.categories), m.what, m.agent or "", -(m.importance or 0))


_CATEGORIES = frozenset(get_args(Category))


def _what(e: Event) -> str:
    if e.type == ToolCall.type:
        return f"tool.call {e.data.get('name', '')}".strip()
    return e.type


async def _facts(
    store: LocalEventStore,
    run_id: str,
    info: RunInfo,
    marks: Sequence[Event],
    review: RunInfo | None,
    flags: Sequence[Flag],
) -> Facts:
    started = {e.agent_id: s for e, s in _of(marks, AgentStarted)}
    finished = {e.agent_id: f for e, f in _of(marks, AgentFinished)}
    agents = [
        AgentFacts(
            agent=agent or "",
            profile=s.profile,
            model=s.model,
            steps=0 if agent not in finished else finished[agent].steps,
            end=None if agent not in finished else finished[agent].reason,
            cost_usd=0.0 if agent not in finished else finished[agent].cost_usd,
            cost_complete=True if agent not in finished else finished[agent].cost_complete,
            wall_ms=0 if agent not in finished else finished[agent].wall_ms,
        )
        for agent, s in started.items()
    ]
    costs = await store.call_costs(run_id)
    counts = await store.type_counts(run_id)
    duration = None
    if info.ended_at is not None:
        duration = int((info.ended_at - info.started_at).total_seconds() * 1000)
    return Facts(
        agents=agents,
        duration_ms=duration,
        events=info.events,
        steps=info.steps,
        agent_cost_usd=_sum(c.cost_usd for c in costs if c.role is None),
        monitor_cost_usd=_sum(c.cost_usd for c in costs if c.role is not None),
        review_cost_usd=None if review is None else review.cost_usd,
        costs=costs,
        kernel={k: n for k, n in sorted(counts.items()) if k in KERNEL_TYPES},
        flags=len(flags),
    )


def _sum(values: Iterable[float | None]) -> float | None:
    priced = [v for v in values if v is not None]
    return round(sum(priced), 9) if priced else None


async def _sources(
    store: LocalEventStore,
    run_id: str,
    info: RunInfo,
    started: RunStarted,
    reviews: Sequence[RunInfo],
    picked: str,
    facts: Facts,
    marks: Sequence[Event],
) -> list[Source]:
    out: list[Source] = []
    own = _last(marks, MonitorVerdict)
    if started.monitor is not None or own is not None:
        out.append(
            Source(
                run_id=LIVE,
                review=False,
                status=info.status,
                chosen=picked == LIVE,
                preset=info.preset,
                importance=None if own is None else own.max_severity,
                claim_matched=None if own is None else own.claim_matched,
                cost_usd=facts.monitor_cost_usd,
                started_at=info.started_at,
            )
        )
    for r in reviews:
        verdict = _last(await store.every(r.run_id, types=(MonitorVerdict.type,)), MonitorVerdict)
        out.append(
            Source(
                run_id=r.run_id,
                review=True,
                status=r.status,
                chosen=picked == r.run_id,
                preset=r.preset,
                importance=None if verdict is None else verdict.max_severity,
                claim_matched=None if verdict is None else verdict.claim_matched,
                cost_usd=r.cost_usd,
                started_at=r.started_at,
            )
        )
    return out
