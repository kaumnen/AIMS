"""The run report as one HTML page to open in a browser and print to PDF. The page holds no
scripts and loads nothing. Log text was escaped by the Markdown writer already."""

import html
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from aims.report import texts
from aims.report.escape import visible
from aims.report.model import RunReport

# html stays on for the writer's own <details> and section marks; nothing else is enabled.
_MD = MarkdownIt("gfm-like2", {"html": True}).disable("linkify")

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'"
)
TITLE = "{title}, run {run}"

STYLE = """
:root {
  color-scheme: light;
  --text: #1f2328;
  --muted: #59636e;
  --line: #d1d9e0;
  --soft: #f6f8fa;
  --link: #0969da;
  --note: #0969da;
  --tip: #1a7f37;
  --important: #8250df;
  --warning: #9a6700;
  --caution: #cf222e;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0;
  background: #fff;
  color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial,
    sans-serif;
}
main { max-width: 60rem; margin: 0 auto; padding: 2rem 16px 4rem; overflow-wrap: break-word; }
h1, h2, h3 { line-height: 1.25; margin: 1.75rem 0 0.75rem; break-after: avoid; }
h2 + p, h3 + p { break-after: avoid; }
h1 { font-size: 1.75rem; margin-top: 0; }
h2 { font-size: 1.3rem; padding-bottom: 0.3rem; border-bottom: 1px solid var(--line); }
h3 { font-size: 1.05rem; }
p, ul, ol, table, blockquote, pre, details, .markdown-alert { margin: 0 0 1rem; }
ul { padding-left: 1.5rem; }
li + li { margin-top: 0.15rem; }
a { color: var(--link); }
code, pre {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono",
    monospace;
  font-size: 0.85em;
}
code {
  padding: 0.1em 0.3em;
  border-radius: 4px;
  background: var(--soft);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
pre {
  padding: 0.75rem 1rem;
  border-radius: 6px;
  background: var(--soft);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
pre code { padding: 0; background: none; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; break-inside: avoid; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td {
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th { background: var(--soft); font-weight: 600; }
blockquote {
  margin-left: 0;
  margin-right: 0;
  padding: 0.25rem 1rem;
  border-left: 4px solid var(--line);
  break-inside: avoid;
}
.markdown-alert {
  padding: 0.6rem 1rem;
  border-left: 4px solid var(--note);
  background: var(--soft);
  break-inside: avoid;
}
.markdown-alert-title { margin-bottom: 0.25rem; font-weight: 600; color: var(--note); }
.markdown-alert-tip { border-left-color: var(--tip); }
.markdown-alert-tip .markdown-alert-title { color: var(--tip); }
.markdown-alert-important { border-left-color: var(--important); }
.markdown-alert-important .markdown-alert-title { color: var(--important); }
.markdown-alert-warning { border-left-color: var(--warning); }
.markdown-alert-warning .markdown-alert-title { color: var(--warning); }
.markdown-alert-caution { border-left-color: var(--caution); }
.markdown-alert-caution .markdown-alert-title { color: var(--caution); }
details {
  padding: 0.6rem 1rem;
  border: 1px solid var(--line);
  border-radius: 6px;
  break-inside: avoid;
}
summary { margin-bottom: 0.75rem; font-weight: 600; cursor: pointer; break-after: avoid; }
/* Short blocks stay whole on a page. Long ones split by row so no page is left blank. */
table:has(> tbody > tr:nth-child(13)),
details:has(tbody > tr:nth-child(13), blockquote) {
  break-inside: auto;
}
blockquote > :last-child, .markdown-alert > :last-child, details > :last-child {
  margin-bottom: 0;
}
@media screen and (max-width: 40rem) {
  table { display: block; overflow-x: auto; }
}
@page { margin: 15mm; }
@media print {
  body { font-size: 10pt; }
  main { max-width: none; padding: 0; }
  a { color: inherit; }
  a[href]::after {
    content: " (" attr(href) ")";
    color: var(--muted);
    font-size: 0.85em;
    overflow-wrap: anywhere;
  }
  summary { list-style: none; cursor: auto; }
  summary::-webkit-details-marker { display: none; }
  code, pre, th, .markdown-alert {
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
}
"""


def page_title(report: RunReport) -> str:
    """The report heading plus the run id, so saved PDFs of two runs get two names."""
    heading = texts.TITLE.format(scenario=report.header.scenario)
    return TITLE.format(title=heading, run=report.header.run_id)


def to_html(markdown: str, *, title: str) -> str:
    """The report Markdown from `render` as one HTML page. The gantt with its legend and any
    other mermaid fence are left out. Every `<details>` the writer made comes out open, so a
    printed page shows its content."""
    env: dict[str, Any] = {}
    tokens = _no_chart(_MD.parse(markdown, env))
    for t in tokens:
        # Only the writer makes html blocks. Escaped log text cannot start one.
        if t.type == "html_block":
            t.content = t.content.replace("<details>", "<details open>")
    body = _MD.renderer.render(tokens, _MD.options, env)
    shown = html.escape(" ".join(visible(title).split()))
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
            f"<title>{shown}</title>",
            f"<style>{STYLE}</style>",
            "</head>",
            "<body>",
            "<main>",
            body.rstrip("\n"),
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _no_chart(tokens: list[Token]) -> list[Token]:
    """`tokens` without the writer's chart, from its mark to its end mark, and without any
    other mermaid fence. Only the writer makes html blocks, so log text holds no mark."""
    out: list[Token] = []
    inside = False
    for t in tokens:
        mark = t.content.strip() if t.type == "html_block" else ""
        if mark == texts.CHART_MARK:
            inside = True
        elif mark == texts.CHART_END:
            inside = False
        elif not inside and not _mermaid(t):
            out.append(t)
    return out


def _mermaid(t: Token) -> bool:
    return t.type == "fence" and t.info.strip() == "mermaid"
