"""Write a run report as GitHub Markdown. Text from the log goes through `escape` only."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from urllib.parse import quote as urlquote

from aims.events import ScoreMatch
from aims.report import texts
from aims.report.escape import cell, code, quote, text
from aims.report.model import (
    CATCH_IMPORTANCE,
    LIVE,
    Flag,
    Moment,
    RunReport,
    Warning,
)

TABLE_ROWS = 30
MAX_LINKS = 12
CELL_CHARS = 300


def render(report: RunReport) -> str:
    """The report as Markdown. Two renders of one report give the same bytes."""
    parts: list[str] = []
    for section in report.sections:
        parts.append(texts.SECTION_MARK.format(id=section))
        body = _SECTIONS[section](report)
        if body:
            parts.append(body)
    return "\n\n".join(parts) + "\n"


def _header(r: RunReport) -> str:
    h, p = r.header, r.header.pins
    status = h.status if h.reason is None else f"{h.status} ({text(h.reason)})"
    scenario = code(h.scenario) + ("" if h.variant is None else f", variant {code(h.variant)}")
    chain = (
        f"verified, {p.events:,} events, head {code(p.head)}"
        if p.verified
        else f"not verified ({text(p.verify_error or '')}), head {code(p.head)}"
    )
    if p.source == LIVE:
        monitor = "none" if p.monitor is None else "live monitor"
    else:
        monitor = f"review {_run_link(r, p.source)}, head {code(p.source_head or '')}"
    if p.monitor is not None:
        monitor += f", identity {code(p.monitor)}"
    score = (
        "none" if p.score is None else f"{_run_link(r, p.score)}, head {code(p.score_head or '')}"
    )
    if p.role == "viewer":
        score = "left out"
    labels = "none" if p.labels == 0 else f"{p.labels}, latest {_time(p.labels_at)}"
    rows = [
        ("run", _run_link(r, h.run_id)),
        ("scenario", scenario),
        ("status", status),
        ("started", _time(h.started_at)),
        ("duration", _duration(r.facts.duration_ms)),
        ("chain", chain),
        ("monitor", monitor),
        ("score", score),
        ("labels", f"{labels}. {texts.LABELS_NOTE}"),
        ("access", texts.ACCESS[p.role]),
        ("links", texts.NO_LINKS if p.base_url is None else code(p.base_url)),
        ("built", f"AIMS {code(p.version)} at {_time(p.built_at, seconds=True)}"),
    ]
    lines = [f"# {texts.TITLE.format(scenario=text(h.scenario))}", ""]
    lines += [f"- **{texts.PIN_LABELS[k]}:** {v}" for k, v in rows]
    return "\n".join(lines)


def _headline(r: RunReport) -> str:
    h = r.headline
    if not h.scored:
        reward = "Reward: none"
    elif h.reward is None:
        reward = "Reward: none (verifier error)" if h.reward_error else "Reward: none"
    else:
        reward = f"Reward {h.reward:g}"
        if h.passed is not None and h.total is not None:
            reward += f" ({h.passed} of {h.total} tests)"
    if h.no_verdict is not None:
        judge = f"Judge: {texts.NO_VERDICT[h.no_verdict]}"
    elif h.importance:
        judge = f"Judge: importance {h.importance} of 5"
    else:
        judge = "Judge: nothing found"
    parts = [reward, judge]
    if h.claim_matched is not None:
        parts.append(f"Claim matched: {h.claim_matched}")
    return "**" + " · ".join(parts) + "**"


def _warnings(r: RunReport) -> str:
    return "\n\n".join(_warning(r, w) for w in r.warnings)


def _warning(r: RunReport, w: Warning) -> str:
    level, template = texts.WARNINGS[w.kind]
    at = "" if w.seq is None else _link(r, w.run_id, w.seq)
    if w.run_id != r.header.run_id:
        who = texts.WHO_REVIEW.format(run=_run_link(r, w.run_id))
        if w.kind == "tainted":
            template = texts.TAINTED_REVIEW
    elif r.header.pins.source != LIVE:
        who = texts.WHO_LIVE
    else:
        who = texts.WHO
    if w.kind == "monitor_degraded" and w.part == "budget":
        template = texts.BUDGET
    lost = texts.LOST_UNKNOWN if w.lost is None else texts.LOST_EVENTS.format(n=f"{w.lost:,}")
    line = template.format(
        who=who,
        at=at,
        part=texts.PART.get(w.part or "", text(w.part or "")),
        then=texts.THEN.get(w.then or "", ""),
        lost=lost,
        count=w.count,
        agent=code(w.agent or ""),
        seqs=_seqs(r, w.run_id, w.seqs),
    ).strip()
    lines = [f"> [!{level}]", f"> {line}"]
    if w.detail:
        lines += [">", f"> {texts.LOG_SAYS} {text(w.detail)}"]
    return "\n".join(lines)


def _summary(r: RunReport) -> str:
    head = f"## {texts.HEADINGS['summary']}"
    if not r.summary.strip():
        return f"{head}\n\n{texts.NO_SUMMARY}"
    return f"{head}\n\n{texts.JUDGE_VIEW}\n\n{quote(r.summary)}"


def _findings(r: RunReport) -> str:
    out = [f"## {texts.HEADINGS['findings']}"]
    if r.headline.no_verdict is not None:
        return "\n\n".join([*out, f"Judge: {texts.NO_VERDICT[r.headline.no_verdict]}."])
    if not r.findings:
        return "\n\n".join([*out, texts.NO_FINDINGS])
    out.append(texts.FINDINGS_INTRO)
    for f in r.findings:
        level = "unclear" if f.present == "unclear" else _importance(f.importance)
        out.append(f"### {_category(f.category)}: {level}")
        if f.evidence:
            out.append(f"Evidence: {_seqs(r, r.header.run_id, f.evidence)}.")
        if f.justification.strip():
            out.append(quote(f.justification, more=_first(r, f.evidence)))
    return "\n\n".join(out)


def _observations(r: RunReport) -> str:
    out = [f"## {texts.HEADINGS['observations']}"]
    if not r.observations:
        return "\n\n".join([*out, texts.NO_OBSERVATIONS])
    out.append(texts.OBSERVATIONS_INTRO)
    for a in r.observations:
        title = texts.ANOMALY_KIND[a.kind]
        if a.agent is not None:
            title += f" of {code(a.agent)}"
        out.append(f"### {title}")
        if a.evidence:
            out.append(f"Evidence: {_seqs(r, r.header.run_id, a.evidence)}.")
        out.append(quote(a.note, more=_first(r, a.evidence)))
    return "\n\n".join(out)


def _plants(r: RunReport) -> str:
    p = r.plants
    if p is None:
        return ""
    out = [f"## {texts.HEADINGS['plants']}", texts.PLANTS_INTRO]
    if p.error is not None:
        out.append(texts.PLANTS_ERROR.format(error=text(p.error)))
        return "\n\n".join(out)
    run = r.header.run_id
    if p.score is None:
        source = r.header.pins.source
        monitor = "" if source == LIVE else f" --monitor {source}"
        out.append(texts.NO_SCORE.format(command=code(f"aims score {run}{monitor}")))
    else:
        out.append(texts.SCORED_BY.format(score=_run_link(r, p.score), importance=CATCH_IMPORTANCE))
    if not p.rows:
        out.append(texts.NO_PLANTS)
    else:
        head = ["Agent", "Plant", "Kind", "Category", "Reached", "At"]
        if p.score is not None:
            head += ["Signal", "Sweep", "Judge"]
        rows = []
        for t in p.rows:
            row = [
                cell(t.agent),
                cell(t.plant),
                cell(t.kind),
                _category(t.category),
                f"{t.tier}{'' if t.taken else ', not taken'}",
                _link(r, run, t.seq),
            ]
            if p.score is not None:
                row += [_caught(t.matches, layer) for layer in ("signal", "sweep", "judge")]
            rows.append(row)
        out.append(_table(head, rows))
    if p.units:
        head = ["Agent", "Role", "Took", "Said it was tested", "Told the operator", "Reasons held"]
        rows = [
            [
                cell(u.agent),
                cell(u.role),
                cell(", ".join(u.plants)) if u.plants else ("none" if u.positive is None else "-"),
                u.eval_aware or "",
                u.told or "",
                u.cause or "",
            ]
            for u in p.units
        ]
        out.append(_table(head, rows))
    return "\n\n".join(out)


def _flags(r: RunReport) -> str:
    source = r.header.pins.source
    review = source != LIVE
    intro = texts.FLAGS_INTRO[review].format(source=_run_link(r, source) if review else "")
    out = [f"## {texts.HEADINGS['flags']}", f"{intro} {texts.FLAGS_NOTE} {texts.LABELS_NOTE}"]
    if not r.flags:
        return "\n\n".join([*out, texts.NO_FLAGS])
    head = ["Flag", "Agent", "Layer", "Category", "Importance", "Status", "Judge", "Person", "Why"]
    out.append(_table(head, [_flag_row(r, f) for f in r.flags]))
    return "\n\n".join(out)


def _flag_row(r: RunReport, f: Flag) -> list[str]:
    judge = ""
    if f.review is not None:
        judge = f.review.status
        if f.review.reason.strip():
            judge += f": {_clip(f.review.reason)}"
    person = ""
    if f.label is not None:
        person = f"{f.label.label.replace('_', ' ')}, by {cell(f.label.by)}"
        if f.label.note.strip():
            person += f": {_clip(f.label.note)}"
    status = f.status + (", tainted" if f.tainted and f.status != "tainted" else "")
    return [
        _link(r, f.run_id, f.seq),
        cell(f.agent or ""),
        f.layer,
        _category(f.category),
        str(f.importance),
        status,
        judge,
        person,
        _clip(f.justification),
    ]


def _timeline(r: RunReport) -> str:
    out = [f"## {texts.HEADINGS['timeline']}", texts.TIMELINE_INTRO]
    head = ["Seq", "Step", "Agent", "Event", "Categories", "Importance"]
    out.append(_table(head, [_moment_row(r, m) for m in r.timeline]))
    return "\n\n".join(out)


def _moment_row(r: RunReport, m: Moment) -> list[str]:
    what = texts.MOMENT[m.kind]
    if m.what:
        what += f": {code(m.what, in_cell=True)}"
    if m.detail:
        what += f" ({_clip(m.detail)})"
    return [
        _link(r, r.header.run_id, m.seq),
        "" if m.step is None else str(m.step),
        cell(m.agent or ""),
        what,
        ", ".join(_category(c) for c in m.categories),
        "" if m.importance is None else str(m.importance),
    ]


def _facts(r: RunReport) -> str:
    f = r.facts
    out = [f"## {texts.HEADINGS['facts']}"]
    if f.agents:
        head = ["Agent", "Profile", "Model", "Steps", "End", "Cost", "Time"]
        rows = [
            [
                cell(a.agent),
                cell(a.profile),
                code(a.model, in_cell=True),
                str(a.steps),
                a.end or "",
                _usd(a.cost_usd) if a.cost_complete else _at_least(a.cost_usd),
                _duration(a.wall_ms),
            ]
            for a in f.agents
        ]
        out.append(_table(head, rows))
    counts = texts.COUNTS.format(
        events=_n(f.events, "event"), steps=_n(f.steps, "step"), flags=_n(f.flags, "flag")
    )
    costs = texts.COSTS.format(agents=_usd(f.agent_cost_usd), monitor=_usd(f.monitor_cost_usd))
    if r.header.pins.source != LIVE:
        costs += " " + texts.REVIEW_COST.format(review=_usd(f.review_cost_usd))
    out.append(f"{counts} {costs}")
    if f.costs:
        head = ["Caller", "Schema", "Calls", "Cost", "Input tokens", "Output tokens"]
        rows = [
            [
                cell(c.role or "agent"),
                cell(c.schema_name or ""),
                f"{c.calls:,}",
                _usd(c.cost_usd),
                f"{c.input_tokens:,}",
                f"{c.output_tokens:,}",
            ]
            for c in f.costs
        ]
        out.append(_details(texts.COSTS_TITLE, _table(head, rows)))
    if f.kernel:
        rows = [[code(k, in_cell=True), f"{n:,}"] for k, n in f.kernel.items()]
        out.append(_details(texts.KERNEL_TITLE, _table(["Type", "Count"], rows)))
    else:
        out.append(texts.NO_KERNEL)
    return "\n\n".join(out)


def _sources(r: RunReport) -> str:
    out = [f"## {texts.HEADINGS['sources']}"]
    others = [s for s in r.sources if not s.chosen]
    if not others:
        return "\n\n".join([*out, texts.NO_SOURCES])
    head = ["Source", "Preset", "Status", "Importance", "Claim matched", "Cost", "Started"]
    rows = [
        [
            texts.REVIEW_SOURCE.format(run=_run_link(r, s.run_id))
            if s.review
            else texts.LIVE_SOURCE,
            cell(s.preset or ""),
            cell(s.status),
            "" if s.importance is None else f"{s.importance} of 5",
            s.claim_matched or "",
            _usd(s.cost_usd),
            _time(s.started_at),
        ]
        for s in others
    ]
    out.append(_table(head, rows))
    return "\n\n".join(out)


def _appendix(r: RunReport) -> str:
    a = r.appendix
    run = r.header.run_id
    body: list[str] = []
    for t in a.answers:
        body.append(f"### {texts.ANSWER.format(agent=code(t.agent or ''))}")
        body.append(f"{_link(r, run, t.seq)}{_step_text(t.step)}.")
        body.append(quote(t.text, more=_link(r, run, t.seq)))
    for t in a.reports:
        body.append(f"### {texts.REPORT.format(agent=code(t.agent or ''))}")
        body.append(f"{_link(r, run, t.seq)}{_step_text(t.step)}.")
        body.append(quote(t.text, more=_link(r, run, t.seq)))
    if a.disagreements:
        body.append(f"### {texts.DISAGREEMENTS}")
        body += [quote(d) for d in a.disagreements]
    head = f"## {texts.HEADINGS['appendix']}"
    if not body:
        return f"{head}\n\n{texts.NO_APPENDIX}"
    return f"{head}\n\n" + _details(texts.APPENDIX_SUMMARY, "\n\n".join(body))


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    def line(values: Iterable[str]) -> str:
        return "| " + " | ".join(values) + " |"

    top = [line(head), line("---" for _ in head)]
    shown = [line(row) for row in rows[:TABLE_ROWS]]
    table = "\n".join([*top, *shown])
    if len(rows) <= TABLE_ROWS:
        return table
    rest = "\n".join([*top, *(line(row) for row in rows[TABLE_ROWS:])])
    more = texts.MORE_ROWS.format(rows=_n(len(rows) - TABLE_ROWS, "row"))
    return table + "\n\n" + _details(more, rest)


def _details(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def _link(r: RunReport, run: str, seq: int) -> str:
    base = r.header.pins.base_url
    if base is None:
        return f"seq {seq}"
    return f"[seq {seq}]({base}/runs/{urlquote(run, safe='')}?seq={seq})"


def _run_link(r: RunReport, run: str) -> str:
    base = r.header.pins.base_url
    if base is None:
        return code(run)
    return f"[{code(run)}]({base}/runs/{urlquote(run, safe='')})"


def _seqs(r: RunReport, run: str, seqs: Sequence[int]) -> str:
    shown = [_link(r, run, s) for s in seqs[:MAX_LINKS]]
    more = len(seqs) - MAX_LINKS
    return ", ".join(shown) + (f" ({more} more)" if more > 0 else "")


def _first(r: RunReport, seqs: Sequence[int]) -> str:
    return _link(r, r.header.run_id, seqs[0]) if seqs else ""


def _caught(matches: Sequence[ScoreMatch], layer: str) -> str:
    """What `layer` caught of a taken plant, and whether before the harm."""
    for m in matches:
        if m.layer != layer or m.min_importance != CATCH_IMPORTANCE:
            continue
        if m.located:
            level, before = "located", m.located_before
        elif m.category:
            level, before = "category", m.category_before
        elif m.detected:
            level, before = "flagged", m.detected_before
        else:
            return "missed"
        return level + (" by the harm" if before else " after the harm")
    return ""


def _clip(value: str) -> str:
    folded = " ".join(value.split())
    if len(folded) <= CELL_CHARS:
        return cell(folded)
    return cell(folded[:CELL_CHARS]) + f" (cut, {len(folded) - CELL_CHARS:,} more characters)"


def _category(value: str) -> str:
    words = value.replace("_", " ")
    return cell(words[:1].upper() + words[1:])


def _importance(value: int | None) -> str:
    return "importance unknown" if value is None else f"importance {value} of 5"


def _step_text(step: int | None) -> str:
    return "" if step is None else f", step {step}"


def _time(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC")


def _duration(ms: int | None) -> str:
    if ms is None:
        return "unknown"
    s = max(0, ms // 1000)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    h = m // 60
    if h < 24:
        return f"{h}h {m % 60:02d}m"
    return f"{h // 24}d {h % 24}h"


def _at_least(value: float) -> str:
    return texts.UNKNOWN if value == 0 else texts.AT_LEAST.format(usd=_usd(value))


def _n(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _usd(value: float | None) -> str:
    if value is None:
        return texts.UNKNOWN
    if value == 0:
        return "$0"
    if value < 0.01:
        return "<$0.01"
    return f"${value:.2f}"


_SECTIONS = {
    "header": _header,
    "headline": _headline,
    "warnings": _warnings,
    "summary": _summary,
    "findings": _findings,
    "observations": _observations,
    "plants": _plants,
    "flags": _flags,
    "timeline": _timeline,
    "facts": _facts,
    "sources": _sources,
    "appendix": _appendix,
}
