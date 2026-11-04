"""The run report as one HTML page: no scripts, nothing loaded, hostile log text kept as
text, every <details> open and the mermaid gantt left out."""

import asyncio
import itertools
import re
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from markdown_it import MarkdownIt
from test_report import AT, HOSTILE, agent_events, monitored, review, verdict
from test_report_escape import CORPUS
from test_scoring import call, flag, sealed, took
from typer.testing import CliRunner

from aims.cli import app
from aims.events import (
    AgentFinished,
    AgentReport,
    AgentStarted,
    Anomaly,
    CategoryFinding,
    Draft,
    FlagReview,
    LocalEventStore,
    MonitorDegraded,
    RunFinished,
    RunStarted,
    ToolCall,
)
from aims.report import build as build_module
from aims.report import html as html_module
from aims.report import texts
from aims.report.build import build
from aims.report.html import CSP, page_title, to_html
from aims.report.markdown import render

pytestmark = pytest.mark.anyio

BASE = "https://aims.example"


def _encodes(s: str) -> bool:
    try:
        s.encode()
    except UnicodeEncodeError:
        return False
    return True


# Lone surrogates cannot go into the log. The escape tests cover them.
BLOB = "\n".join(s for s in [*HOSTILE, *CORPUS] if _encodes(s))
NAME = BLOB
TAGS = {
    "html",
    "head",
    "meta",
    "title",
    "style",
    "body",
    "main",
    "h1",
    "h2",
    "h3",
    "p",
    "ul",
    "li",
    "strong",
    "code",
    "a",
    "blockquote",
    "div",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "details",
    "summary",
}
ATTRS = {
    ("html", "lang"),
    ("meta", "charset"),
    ("meta", "name"),
    ("meta", "content"),
    ("meta", "http-equiv"),
    ("div", "class"),
    ("p", "class"),
    ("a", "href"),
    ("details", "open"),
}
LOADS = (
    "<script",
    "<iframe",
    "<frame",
    "<object",
    "<embed",
    "<link",
    "<img",
    "<svg",
    "<video",
    "<audio",
    "<base",
    "<form",
)
FENCES = """# Report

<!-- aims:section timeline -->

```mermaid
gantt
  title Run
```

~~~ mermaid
gantt two
~~~

````text
```mermaid
not a chart
```
````

```Mermaid
kept as code
```
"""


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, attrs))


def tags(page: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    parser = _Tags()
    parser.feed(page)
    parser.close()
    return parser.found


def check_page(page: str, base: str | None) -> None:
    """Only the tags and attributes a report needs, nothing that loads or runs, every link
    into the app."""
    lower = page.lower()
    for bad in LOADS:
        assert bad not in lower, bad
    assert lower.count("<style") == 1
    assert page.count(f'<meta http-equiv="Content-Security-Policy" content="{CSP}">') == 1
    found = tags(page)
    assert {tag for tag, _ in found} <= TAGS, {tag for tag, _ in found} - TAGS
    hrefs = []
    for tag, attrs in found:
        for name, value in attrs:
            assert (tag, name) in ATTRS, (tag, name, value)
            if name == "href":
                hrefs.append(value or "")
    if base is None:
        assert hrefs == []
    else:
        assert hrefs
        assert all(h.startswith(base + "/runs/") for h in hrefs), hrefs
    assert page.count("<details") == page.count("<details open>")


def body(page: str) -> str:
    return page.split("<main>\n", 1)[1].rsplit("\n</main>", 1)[0]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as s:
        yield s


@pytest.fixture(autouse=True)
def no_plants(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
        return []

    monkeypatch.setattr(build_module, "check_run", check)


def test_a_page_is_one_whole_document() -> None:
    page = to_html("# x", title="t")
    assert page.startswith('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n')
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    for directive in ("default-src 'none'", "style-src 'unsafe-inline'", "img-src data:"):
        assert directive in CSP
    assert "<title>t</title>" in page
    assert "<main>\n<h1>x</h1>\n</main>\n</body>\n</html>\n" in page
    assert page.endswith("</html>\n")
    assert to_html("# x", title="t") == page
    check_page(page, None)


def test_the_title_is_escaped() -> None:
    page = to_html("# x", title='a <b> & "c"\n\t\u202e')
    assert "<title>a &lt;b&gt; &amp; &quot;c&quot; \\u202E</title>" in page
    assert "\u202e" not in page


def test_the_parser_has_no_linkify() -> None:
    rules = html_module._MD.get_active_rules()
    assert all("linkify" not in names for names in rules.values())


async def test_a_report_renders_with_nothing_that_loads(store: LocalEventStore) -> None:
    run_id = await monitored(store)
    for base in (None, BASE):
        report = await build(store, run_id, base_url=base, now=AT)
        md = render(report)
        page = to_html(md, title=page_title(report))
        check_page(page, base)
        for bad in ("src=", "javascript:", "<pre"):
            assert bad not in page, bad
        assert f"<title>Run report: s, run {run_id}</title>" in page
        assert "<h2>Judge summary</h2>" in page
        assert '<div class="markdown-alert ' not in page
        # The page renders the Markdown as markdown-it does, details opened, chart gone.
        start, end = md.index(texts.CHART_MARK), md.index(texts.CHART_END) + len(texts.CHART_END)
        plain = (
            MarkdownIt("gfm-like2", {"html": True}).disable("linkify").render(md[:start] + md[end:])
        )
        plain = plain.replace("<details>", "<details open>")
        assert body(page) == plain.rstrip("\n")


async def test_hostile_text_from_the_log_stays_text(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = AgentStarted(profile=NAME, model=NAME, backend="chat", tools=["bash"], system="s")
    drafts = [
        Draft(agent, NAME),
        Draft(ToolCall(call_id="c1", name=NAME, arguments="{}"), NAME),
        Draft(call(2), NAME),
        Draft(flag("sweep", [2], importance=4, justification=BLOB), NAME),
        Draft(AgentReport(call_id="c9", step=2, message=BLOB), NAME),
        Draft(AgentFinished(reason="submitted", answer=BLOB, steps=2), NAME),
        Draft(MonitorDegraded(part="judge", reason=BLOB, then="continue")),
        Draft(
            verdict(
                summary=BLOB,
                categories=[
                    CategoryFinding(
                        category="scorer_tampering",
                        present="yes",
                        importance=4,
                        evidence=[2],
                        justification=BLOB,
                    )
                ],
                flags=[FlagReview(flag=4, status="rejected", reason=BLOB, evidence=[2])],
                anomalies=[Anomaly(kind="behaviour", agent=NAME, note=BLOB, evidence=[2])],
                disagreements=[BLOB],
            )
        ),
        Draft(RunFinished(status="killed", reason=BLOB)),
    ]
    started = RunStarted(scenario=NAME, oversight="observe", agents=[NAME])
    run_id = await store.create_run(started)
    await store.append(run_id, drafts)
    await store.add_label(run_id, 4, "real", by=NAME, note=BLOB)
    held = await review(
        store,
        run_id,
        drafts=[
            Draft(flag("judge", [2], importance=4, justification=BLOB), NAME),
            Draft(verdict(summary=BLOB, flags=[], anomalies=[], disagreements=[BLOB])),
        ],
    )
    await store.add_label(held, 1, "unclear", by=NAME, note=BLOB)

    async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
        return [took(2, (2,), agent=NAME, plant=NAME, kind=NAME)]

    monkeypatch.setattr(build_module, "check_run", check)
    for source, base in itertools.product(("live", held), (None, BASE)):
        report = await build(store, run_id, source=source, base_url=base, now=AT)
        md = render(report)
        page = to_html(md, title=page_title(report))
        check_page(page, base)
        assert page.count("<title>") == page.count("</title>") == 1
        assert "\u202e" not in page
        assert page.count("<details open>") == md.count("<details>\n<summary>") > 0
        assert page.count('<div class="markdown-alert ') == len(report.warnings) == 1


def test_a_mermaid_fence_is_left_out() -> None:
    page = to_html(FENCES, title="t")
    assert "gantt" not in page
    assert "language-mermaid" not in page
    assert '<code class="language-text">```mermaid\nnot a chart\n```\n</code>' in page
    assert '<code class="language-Mermaid">kept as code\n</code>' in page
    assert page.count("<pre>") == 2


async def test_the_gantt_and_its_legend_are_left_out(store: LocalEventStore) -> None:
    report = await build(store, await monitored(store), now=AT)
    md = render(report)
    start, end = md.index(texts.CHART_MARK), md.index(texts.CHART_END) + len(texts.CHART_END)
    chart = md[start:end]
    assert "```mermaid" in chart
    assert "Agent 1" in chart
    title = page_title(report)
    page = to_html(md, title=title)
    assert page == to_html(md[:start] + md[end:], title=title)
    assert "aims:chart" not in page
    assert "language-mermaid" not in page
    assert "Agent 1" not in page


async def test_every_details_comes_out_open(store: LocalEventStore) -> None:
    many = [Draft(flag("sweep", [2], importance=3, justification=f"f{n}"), "a") for n in range(35)]
    run_id = await sealed(store, [*agent_events(), *many])
    md = render(await build(store, run_id, now=AT))
    assert "<details open" not in md
    page = to_html(md, title="t")
    opened = page.count("<details open>")
    assert opened == page.count("<details") == md.count("<details>\n<summary>") >= 3
    assert page.count("<summary>") == opened


async def test_the_cli_writes_html(store: LocalEventStore, tmp_path: Path) -> None:
    run_id = await monitored(store)
    cli = CliRunner()

    def invoke(*args: str) -> Any:
        return cli.invoke(app, ["report", run_id, "--runs-dir", str(store.root), *args])

    dest = tmp_path / "report.html"
    out = await asyncio.to_thread(invoke, "--html", str(dest))
    assert out.exit_code == 0, out.output
    assert out.stdout == ""
    assert out.stderr == f"wrote {dest}\n"
    page = dest.read_text(encoding="utf-8")
    assert f"<title>Run report: s, run {run_id}</title>" in page
    check_page(page, None)

    both, md = tmp_path / "both.html", tmp_path / "both.md"
    out = await asyncio.to_thread(invoke, "--html", str(both), "--out", str(md), "--base-url", BASE)
    assert out.exit_code == 0, out.output
    assert out.stderr == f"wrote {md}\nwrote {both}\n"
    assert f"({BASE}/runs/{run_id}?seq=2)" in md.read_text(encoding="utf-8")
    check_page(both.read_text(encoding="utf-8"), BASE)

    bad = tmp_path / "bad.html"
    out = await asyncio.to_thread(invoke, "--html", str(bad), "--json")
    assert out.exit_code == 2
    assert "use --html or --json" in re.sub(r"\x1b\[[0-9;]*m", "", out.stderr)
    out = await asyncio.to_thread(invoke, "--html", str(bad), "--out", str(bad))
    assert out.exit_code == 2
    assert not bad.exists()
