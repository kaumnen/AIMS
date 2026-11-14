"""The mermaid gantt in the report's timeline: what it draws, and that no text from the log
ever reaches the inside of its fence."""

import itertools
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from markdown_it import MarkdownIt
from test_report import HOSTILE, IDENTITY, LATER, review
from test_scoring import call, flag, took

from aims.events import (
    AgentFinished,
    AgentReport,
    AgentStarted,
    Draft,
    LocalEventStore,
    MonitorAction,
    MonitorDegraded,
    OperatorAction,
    RunFinished,
    RunStarted,
)
from aims.events import store as event_store
from aims.events.schema import format_ts
from aims.report import build as build_module
from aims.report import texts
from aims.report.build import build
from aims.report.escape import cell
from aims.report.markdown import AXIS, CHART_MAX, render

pytestmark = pytest.mark.anyio

AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FENCE = "```mermaid"
# Strings mermaid gantt syntax reacts to: separators, comments, directives, links, fences.
TRICKS = [
    "a:b :crit, x1, 0, 1",
    "#35; ; # end",
    'click a1 href "javascript:alert(1)"',
    "click a1 call alert()",
    "%%{init: {'securityLevel': 'loose'}}%%",
    "``` \nsection Evil\n```",
    "line one\nsection Evil\nEvil task :e1, 0, 1",
    "</code></pre><script>x</script>",
]
STRINGS = [*HOSTILE, *TRICKS]
KEYWORDS = (
    "accDescr",
    "accTitle",
    "axisFormat",
    "call",
    "click",
    "dateFormat",
    "excludes",
    "gantt",
    "href",
    "inclusiveEndDates",
    "includes",
    "section",
    "tickInterval",
    "title",
    "todayMarker",
    "topAxis",
    "weekday",
    "weekend",
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as s:
        yield s


TICKS = [itertools.count()]


def restart() -> None:
    """The next event written gets the time AT; each one after it a second later."""
    TICKS[0] = itertools.count()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event seq n of a run started after `restart()` gets the time AT plus n seconds."""
    restart()
    monkeypatch.setattr(
        event_store, "now", lambda: format_ts(AT + timedelta(seconds=next(TICKS[0])))
    )


@pytest.fixture(autouse=True)
def plants(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The plants the truth file check returns; a test fills it."""
    rows: list[Any] = []

    async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(build_module, "check_run", check)
    return rows


def ms(seq: int) -> int:
    return (AT + timedelta(seconds=seq) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(
        milliseconds=1
    )


def bar(label: str, task: str, start: int, end: int) -> str:
    return f"    {label} :{task}, {ms(start)}, {ms(end)}"


def mark(label: str, task: str, seq: int) -> str:
    return f"    {label} :milestone, {task}, {ms(seq)}, {ms(seq)}"


def started(agent: str, model: str = "m", profile: str = "p") -> Draft:
    agent_started = AgentStarted(
        profile=profile, model=model, backend="chat", tools=["bash"], system="s"
    )
    return Draft(agent_started, agent)


def finished(agent: str) -> Draft:
    return Draft(AgentFinished(reason="submitted", answer="Done.", steps=1), agent)


async def logged(
    store: LocalEventStore,
    drafts: list[Draft],
    *,
    agents: tuple[str, ...] = ("a",),
    monitor: bool = True,
    seal: bool = True,
) -> str:
    """A run of `drafts` from seq 1, sealed at the next seq unless told not to."""
    began = RunStarted(
        scenario="s",
        oversight="observe",
        agents=list(agents),
        monitor=IDENTITY if monitor else None,
    )
    restart()
    run_id = await store.create_run(began)
    end = [Draft(RunFinished(status="completed"))] if seal else []
    await store.append(run_id, [*drafts, *end])
    return run_id


def chart(md: str) -> list[str]:
    """The lines inside the one mermaid fence. It opens once and closes, for this code and for a
    CommonMark parser alike."""
    lines = md.splitlines()
    assert lines.count(FENCE) == 1
    start = lines.index(FENCE) + 1
    end = lines.index("```", start)
    inside = lines[start:end]
    assert not any("`" in line or "~~~" in line for line in inside)
    tokens = MarkdownIt("gfm-like2", {"html": True}).disable("linkify").parse(md)
    fences = [t for t in tokens if t.type == "fence" and t.info.strip() == "mermaid"]
    assert len(fences) == 1
    assert fences[0].content == "\n".join(inside) + "\n"
    return inside


def head(axis: str = "%H:%M:%S") -> list[str]:
    return [
        "gantt",
        f"    accTitle: {texts.CHART_TITLE}",
        f"    accDescr: {texts.CHART_DESCRIPTION}",
        "    dateFormat x",
        f"    axisFormat {axis}",
        "    todayMarker off",
    ]


def section(key: str) -> str:
    return f"    section {texts.CHART_SECTIONS[key]}"


def flag_label(layer: str, importance: int, agent: int | None = None) -> str:
    name = texts.CHART_LAYERS[layer]
    if agent is None:
        return texts.CHART_FLAG_LABEL.format(layer=name, importance=importance)
    on = texts.CHART_AGENT.format(n=agent)
    return texts.CHART_FLAG_ON.format(layer=name, agent=on, importance=importance)


async def test_the_chart_draws_agents_the_monitor_and_plants(
    store: LocalEventStore, plants: list[Any]
) -> None:
    plants.append(took(3, (3,)))
    drafts = [
        started("a"),
        Draft(call(1), "a"),
        Draft(call(2), "a"),
        Draft(flag("sweep", [2], importance=4), "a"),
        Draft(flag("signal", [2], importance=2), "a"),
        finished("a"),
    ]
    run_id = await logged(store, drafts)
    md = render(await build(store, run_id, now=AT))
    assert chart(md) == [
        *head(),
        section("agents"),
        bar("Agent 1", "a1", 1, 6),
        section("monitor"),
        bar(texts.CHART_LIVE, "m1", 0, 7),
        mark(flag_label("sweep", 4), "f1", 4),
        section("plants"),
        mark("Plant 1", "p1", 3),
    ]
    timeline = md.split("<!-- aims:section timeline -->")[1]
    order = [texts.TIMELINE_INTRO, FENCE, "| In the chart | Agent |", texts.CHART_CLOCK, "| Seq |"]
    assert [timeline.index(s) for s in order] == sorted(timeline.index(s) for s in order)
    assert "| Agent 1 | a |" in timeline
    assert "| Plant 1 | p1 | Agent 1 | used | seq 3 |" in timeline

    viewer = chart(render(await build(store, run_id, role="viewer", now=AT)))
    assert section("plants") not in viewer
    assert not any("Plant" in line for line in viewer)

    plants.clear()
    bare = await logged(store, [started("a"), finished("a")], monitor=False)
    lines = chart(render(await build(store, bare, now=AT)))
    assert lines == [*head(), section("agents"), bar("Agent 1", "a1", 1, 2)]


async def test_marks_wrap_the_chart_its_legend_and_notes(
    store: LocalEventStore, plants: list[Any]
) -> None:
    plants.append(took(3, (3,)))
    drafts = [
        started("a"),
        Draft(call(1), "a"),
        Draft(call(2), "a"),
        Draft(flag("sweep", [2], importance=4), "a"),
        finished("a"),
    ]
    run_id = await logged(store, drafts)
    md = render(await build(store, run_id, now=AT))
    assert md.count(texts.CHART_MARK) == md.count(texts.CHART_END) == 1
    timeline = md.split(texts.SECTION_MARK.format(id="timeline"))[1]
    blocks = timeline.split(texts.SECTION_MARK.format(id="facts"))[0].strip().split("\n\n")
    start, end = blocks.index(texts.CHART_MARK), blocks.index(texts.CHART_END)
    assert blocks[:start] == [f"## {texts.HEADINGS['timeline']}", texts.TIMELINE_INTRO]
    assert blocks[start + 1 : end] == [
        "\n".join([FENCE, *chart(md), "```"]),
        "| In the chart | Agent |\n| --- | --- |\n| Agent 1 | a |",
        "| In the chart | Plant | Agent | Reached | At |\n| --- | --- | --- | --- | --- |\n"
        "| Plant 1 | p1 | Agent 1 | used | seq 3 |",
        texts.CHART_CLOCK,
    ]
    assert len(blocks) == end + 2
    assert blocks[-1].startswith("| Seq | Step |")


async def test_a_degraded_monitor_runs_signals_only_to_the_end(store: LocalEventStore) -> None:
    drafts = [
        started("b"),
        started("a"),
        Draft(call(1), "b"),
        Draft(flag("judge", [3], importance=5), "a"),
        Draft(MonitorDegraded(part="budget", reason="spent $4 of $5", then="signals_only")),
        Draft(flag("signal", [3], importance=3), "b"),
        finished("b"),
        finished("a"),
    ]
    run_id = await logged(store, drafts, agents=("a", "b"))
    report = await build(store, run_id, now=AT)
    md = render(report)
    assert [a.agent for a in report.facts.agents] == ["b", "a"]
    assert chart(md) == [
        *head(),
        section("agents"),
        bar("Agent 1", "a1", 1, 7),
        bar("Agent 2", "a2", 2, 8),
        section("monitor"),
        bar(texts.CHART_LIVE, "m1", 0, 5),
        bar(texts.CHART_SIGNALS_ONLY, "m2", 5, 9),
        mark(flag_label("judge", 5, agent=2), "f1", 4),
        mark(flag_label("signal", 3, agent=1), "f2", 6),
    ]
    assert "| Agent 1 | b |\n| Agent 2 | a |" in md

    # A part that stops first leaves the monitor degraded until signals only.
    drafts.insert(4, Draft(MonitorDegraded(part="sweep", reason="timed out", then="continue")))
    run_id = await logged(store, drafts, agents=("a", "b"))
    monitor = [
        line
        for line in chart(render(await build(store, run_id, now=AT)))
        if re.search(r" :m\d", line)
    ]
    assert monitor == [
        bar(texts.CHART_LIVE, "m1", 0, 5),
        bar(texts.CHART_DEGRADED, "m2", 5, 6),
        bar(texts.CHART_SIGNALS_ONLY, "m3", 6, 10),
    ]


async def test_flags_of_a_review_sit_at_their_evidence(store: LocalEventStore) -> None:
    drafts = [started("a"), Draft(call(1), "a"), Draft(call(2), "a"), finished("a")]
    run_id = await logged(store, drafts)
    held = await review(store, run_id)
    md = render(await build(store, run_id, now=AT))
    lines = chart(md)
    assert mark(flag_label("judge", 5), "f1", 2) in lines
    assert texts.CHART_REVIEW.format(run=f"`{held}`") in md


async def test_an_agent_still_at_work_ends_at_the_last_event(store: LocalEventStore) -> None:
    drafts = [
        started("a"),
        started("b"),
        Draft(call(1), "a"),
        finished("b"),
        Draft(call(2), "a"),
        Draft(flag("sweep", [5], importance=4), "a"),
        Draft(call(3), "a"),
    ]
    run_id = await logged(store, drafts, agents=("a", "b"), seal=False)
    report = await build(store, run_id, now=AT)
    assert report.header.status == "running"
    lines = chart(render(report))
    # The tool call at seq 7 is no timeline moment, yet the run was busy until then.
    assert bar("Agent 1", "a1", 1, 7) in lines
    assert bar("Agent 2", "a2", 2, 4) in lines
    assert bar(texts.CHART_LIVE, "m1", 0, 7) in lines


async def test_no_chart_without_an_agent_start(store: LocalEventStore) -> None:
    run_id = await logged(store, [Draft(call(1), "a")])
    md = render(await build(store, run_id, now=AT))
    assert FENCE not in md
    assert texts.CHART_CLOCK not in md
    assert texts.CHART_MARK not in md
    assert texts.CHART_END not in md
    assert "| Seq | Step | Agent | Event | Categories | Importance |" in md


async def test_two_builds_give_the_same_chart(store: LocalEventStore) -> None:
    drafts = [started("a"), Draft(call(1), "a"), Draft(flag("judge", [2], importance=3), "a")]
    run_id = await logged(store, drafts, seal=False)
    builds = (AT + timedelta(days=2), LATER)
    one, two = [render(await build(store, run_id, now=built)) for built in builds]
    assert one != two
    assert chart(one) == chart(two)
    timeline = "<!-- aims:section timeline -->"
    assert one.split(timeline)[1] == two.split(timeline)[1]
    for built in builds:
        stamp = str((built - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1))
        assert stamp not in "\n".join(chart(one))


async def test_the_chart_marks_at_most_ten_flags_and_plants(
    store: LocalEventStore, plants: list[Any]
) -> None:
    importance = [3, 4, 5, 3, 3, 4, 5, 3, 4, 3, 5, 3, 4, 3]
    drafts = [started("a"), *(Draft(call(n), "a") for n in range(12))]
    drafts += [Draft(flag("sweep", [2], importance=i), "a") for i in importance]
    plants.extend(took(seq, (seq,), plant=f"p{seq}") for seq in range(2, 14))
    run_id = await logged(store, drafts)
    md = render(await build(store, run_id, now=AT))
    lines = chart(md)
    marked = [line for line in lines if " :milestone, f" in line]
    assert len(marked) == CHART_MAX
    kept = sorted(range(len(importance)), key=lambda i: (-importance[i], i))[:CHART_MAX]
    assert marked == [
        mark(flag_label("sweep", importance[i]), f"f{n}", 14 + i)
        for n, i in enumerate(sorted(kept), 1)
    ]
    assert sum(" :milestone, p" in line for line in lines) == CHART_MAX
    assert texts.CHART_FLAGS_CUT.format(shown=CHART_MAX, total=14) in md
    assert texts.CHART_PLANTS_CUT.format(shown=CHART_MAX, total=12) in md
    assert "| Plant 10 | p11 | Agent 1 | used | seq 11 |" in md
    assert "| Plant 11 |" not in md


def code_labels(agents: int) -> set[str]:
    """Every task label the chart may write for a run of `agents` agents."""
    layers = [*texts.CHART_LAYERS.values(), texts.CHART_FLAG]
    out = {texts.CHART_LIVE, texts.CHART_DEGRADED, texts.CHART_SIGNALS_ONLY, *layers}
    for n in range(1, max(agents, CHART_MAX) + 1):
        out |= {texts.CHART_AGENT.format(n=n), texts.CHART_PLANT.format(n=n)}
    for layer, importance in itertools.product(layers, range(1, 6)):
        out.add(texts.CHART_FLAG_LABEL.format(layer=layer, importance=importance))
        for n in range(1, agents + 1):
            on = texts.CHART_AGENT.format(n=n)
            out.add(texts.CHART_FLAG_ON.format(layer=layer, agent=on, importance=importance))
    return out


def test_chart_words_hold_no_mermaid_syntax() -> None:
    words = [
        texts.CHART_TITLE,
        texts.CHART_DESCRIPTION,
        *texts.CHART_SECTIONS.values(),
        *code_labels(3),
    ]
    for word in words:
        assert not re.search(r"[:#;%`\n]", word), word
        assert not word.lower().startswith(tuple(k.lower() for k in KEYWORDS)), word


async def test_no_text_from_the_log_reaches_the_chart(
    store: LocalEventStore, plants: list[Any]
) -> None:
    hostile = "\n".join(STRINGS)
    names = [TRICKS[0], "Agent 1", *TRICKS[1:], *HOSTILE[:3]]
    began = RunStarted(scenario=hostile, oversight="observe", agents=names, monitor=IDENTITY)
    run_id = await store.create_run(began)
    drafts = [started(name, model=hostile, profile=hostile) for name in names]
    for i, name in enumerate(names):
        seq = len(drafts) + 1
        drafts += [
            Draft(call(i), name),
            Draft(flag("judge", [seq], importance=4, source=hostile, justification=hostile), name),
        ]
        plants.append(
            took(
                seq, (seq,), agent=name, plant=name, kind=hostile, category=hostile, detail=hostile
            )
        )
    drafts += [
        Draft(MonitorAction(action="pause", agents=names, mode="observe", reason=hostile)),
        Draft(OperatorAction(action="pause", reason=hostile, text=hostile, token=hostile)),
        Draft(MonitorDegraded(part="judge", reason=hostile, then="continue", detail=hostile)),
        Draft(AgentReport(call_id=hostile, step=1, message=hostile), names[0]),
        *(
            Draft(AgentFinished(reason="submitted", answer=hostile, detail=hostile), n)
            for n in names
        ),
        Draft(RunFinished(status="killed", reason=hostile)),
    ]
    await store.append(run_id, drafts)
    report = await build(store, run_id, base_url="https://aims.example", now=AT)
    md = render(report)
    lines = chart(md)
    block = "\n".join(lines)

    allowed = code_labels(len(names))
    axes = "|".join(re.escape(form) for _, form in AXIS)
    syntax = [re.escape(line) for line in head("PLACEHOLDER") if "PLACEHOLDER" not in line] + [
        f"    axisFormat (?:{axes})"
    ]
    sections = {section(key) for key in texts.CHART_SECTIONS}
    task = re.compile(r"    ([^:]+) :(milestone, )?[amfp]\d+, \d+, \d+")
    for line in lines:
        if line in sections or any(re.fullmatch(s, line) for s in syntax):
            continue
        found = task.fullmatch(line)
        assert found, line
        assert found[1] in allowed, line
    assert sum(line.startswith("    Agent ") and ":a" in line for line in lines) == len(names)

    corpus = "\n".join([*head(), *(form for _, form in AXIS), *sections, *allowed])
    corpus += " :milestone, "
    for s in [*STRINGS, *names, hostile]:
        for i in range(len(s) - 3):
            window = s[i : i + 4]
            if window in block and not re.fullmatch(r"[\d, ]+", window):
                assert window in corpus, (s, window)

    legend = md.split(FENCE)[1].split("\n```\n", 1)[1].split("| Seq |")[0]
    assert f"| Agent 1 | {cell(TRICKS[0])} |" in legend
    assert "| Agent 2 | Agent 1 |" in legend
    out = MarkdownIt("gfm-like2", {"html": True}).disable("linkify").render(legend)
    assert "<script" not in out
    hrefs = re.findall(r'<a href="([^"]*)"', out)
    assert hrefs
    assert all(h.startswith("https://aims.example/runs/") for h in hrefs)
    for table in re.findall(r"<table>.*?</table>", out, re.S):
        rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
        assert len({len(re.findall(r"<t[hd][ >]", r)) for r in rows}) == 1
