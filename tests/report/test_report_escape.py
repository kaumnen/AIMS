"""Untrusted text renders as its literal characters through every report escape helper."""

import html
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from markdown_it import MarkdownIt
from markdown_it.token import Token

from aims.report.escape import (
    PROSE_CHARS,
    cell,
    code,
    quote,
    text,
    visible,
)

MD = MarkdownIt("gfm-like2", {"html": True}).disable("linkify")

CORPUS = [
    "# Heading",
    "###### h6 #",
    "Title\n=====",
    "Title\n---",
    "> [!WARNING]\n> Danger ahead",
    "> [!NOTE]",
    "![x](https://evil.example/a.png)",
    "[a](javascript:alert(1))",
    "[b](<javascript:alert(1)>) [c][ref]",
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "<style>body{display:none}</style>",
    "<details open><summary>s</summary>body</details>",
    '<a href="https://evil.example">x</a>',
    "<!-- hidden -->",
    "<?php echo 1; ?> <![CDATA[x]]> <!DOCTYPE html>",
    "<div>\n\n**x**\n\n</div>",
    "html <b>bold</b> <span style=x>y</span>",
    "&lt; &#x3C; &#60; &amp; &copy; &NotAnEntity; &#0;",
    "- a\n* b\n+ c",
    "1. a\n1) b\n10. c",
    "- [ ] task\n- [x] done",
    "| a | b |\n| --- | --- |\n| 1 | 2 |",
    "a | b\n--|--\nc | d",
    r"pipe \| and \\| in text",
    "**bold** __b__ *em* _em_ ~~strike~~ ~s~ ***both***",
    "`a` ``b`` ```c``` ````d```` `````e`````",
    "```\ncode\n```",
    "~~~python\nx\n~~~",
    "    indented code\n\tand tab",
    "two spaces  \nnext",
    "trailing backslash\\\nnext\\",
    "---\n***\n___\n- - -",
    "[x]: http://evil.example/ref\n\n[x]",
    "see https://evil.example/path?q=1&r=2 now",
    "<https://evil.example> <me@example.com>",
    "www.example.com and WWW.EXAMPLE.COM/x",
    "mail me@example.com or mailto:me@example.com",
    "@someone hi @org/team",
    ":smile: :+1: :-1: 12:30:45 std::vector::push",
    "note[^1]\n\n[^1]: the footnote",
    "$x$ and $$y$$ and $`z`$ costs $5",
    "data:text/html;base64,PHNjcmlwdD4= file:///etc/passwd ftp://x.example vbscript:msgbox(1)",
    "abc\u202edcba \u2066iso\u2069 \u200emark \u061c",
    "zero\u200bwidth\u200c\u200d\u2060\ufeff",
    "nul\x00 bell\x07 esc\x1b[31m del\x7f nel\x85",
    "vt\x0b ff\x0c fs\x1c gs\x1d rs\x1e us\x1f",
    "ls\u2028ps\u2029end",
    "tags\U000e0041\U000e0042 lone\ud800",
    "crlf\r\nline\rnext\r\n",
    "x" * 5000 + "<script>alert(1)</script>",
    "\n".join(["<b>row</b> | `c` | ~~d~~ | **e**"] * 100),
    "",
    "   \n\t  ",
    "`",
    "``",
    " ` ",
    " a ",
    "\\",
    "\\*already escaped\\*",
    "\u0661. arabic digit, caf\xe9 \u65e5\u672c \U0001f600",
]

FORBIDDEN = re.compile(
    r"<(?:a|img|script|style|h[1-6]|details|summary|li|ul|ol|em|strong|del|s|hr|br|input)\b"
    r"|markdown-alert|<!--"
)
TAG = re.compile(r"</?([a-z][a-z0-9]*)[^>]*>")
CODE_ELEMENT = re.compile(r"<code[^>]*>.*?</code>", re.DOTALL)
GITHUB_LINKY = re.compile(
    r"://|www\.|@\w|\$|:[\w+-]+:|(?:javascript|mailto|data|file|vbscript):\S", re.IGNORECASE
)
TABLE_TAGS = {"table", "thead", "tbody", "tr", "th", "td", "code"}


def plain(fragment: str) -> str:
    return html.unescape(TAG.sub("", fragment))


def folded(s: str) -> str:
    return " ".join(visible(s).split())


def check_safe(out: str, allowed: set[str]) -> None:
    assert not FORBIDDEN.search(out), out
    assert {m[1] for m in TAG.finditer(out)} <= allowed, out
    assert not GITHUB_LINKY.search(plain(CODE_ELEMENT.sub("", out))), out


def table(first: str) -> str:
    return MD.render(f"| a | b |\n| --- | --- |\n| {first} | x |\n")


def body_cell(out: str) -> str:
    assert out.count("<table>") == 1
    rows = re.findall(r"<tr>(.*?)</tr>", out, re.DOTALL)
    assert len(rows) == 2
    assert [len(re.findall(r"<t[hd]>", row)) for row in rows] == [2, 2]
    first, second = re.findall(r"<td>(.*?)</td>", rows[1], re.DOTALL)
    assert second == "x"
    return plain(first)


def same_code(shown: str, want: str) -> bool:
    # markdown-it-py keeps the padding of a whitespace-only span holding a tab; GitHub strips it.
    return shown == want or (not want.strip() and shown == f" {want} ")


@pytest.mark.parametrize("s", CORPUS)
def test_text_renders_literally(s: str) -> None:
    out = MD.render(text(s))
    if not folded(s):
        assert out == ""
        return
    check_safe(out, {"p", "code"})
    assert out.startswith("<p>")
    assert out.endswith("</p>\n")
    assert out.count("<p>") == 1
    assert plain(out) == folded(s) + "\n"


@pytest.mark.parametrize("s", CORPUS)
def test_cell_renders_literally(s: str) -> None:
    out = table(cell(s))
    check_safe(out, TABLE_TAGS)
    assert body_cell(out) == folded(s)


@pytest.mark.parametrize("s", CORPUS)
def test_code_renders_literally(s: str) -> None:
    want = visible(s).replace("\n", " ")
    if not want:
        assert code(s) == ""
        return
    out = MD.render(code(s))
    check_safe(out, {"p", "code"})
    match = re.fullmatch(r"<p><code>(.*)</code></p>\n", out, re.DOTALL)
    assert match, out
    assert same_code(html.unescape(match[1]), want)


@pytest.mark.parametrize("s", CORPUS)
def test_code_in_cell_renders_literally(s: str) -> None:
    out = table(code(s, in_cell=True))
    check_safe(out, TABLE_TAGS)
    assert same_code(body_cell(out), visible(s).replace("\n", " "))


@pytest.mark.parametrize("s", CORPUS)
def test_quote_renders_literally(s: str) -> None:
    lines = [folded(line) for line in visible(s[:PROSE_CHARS]).split("\n")]
    want = [line for line in lines if line]
    if not want:
        assert quote(s) == ""
        return
    out = MD.render(quote(s))
    check_safe(out, {"blockquote", "p", "code"})
    assert out.count("<blockquote>") == 1
    match = re.fullmatch(r"<blockquote>\n(.*)</blockquote>\n(.*)", out, re.DOTALL)
    assert match, out
    inside, after = match.groups()
    assert [plain(p) for p in re.findall(r"<p>(.*?)</p>", inside, re.DOTALL)] == want
    if len(s) > PROSE_CHARS:
        assert after == f"<p>Cut, {len(s) - PROSE_CHARS:,} more characters.</p>\n"
    else:
        assert after == ""


HIDDEN = [
    "\u061c",
    "\u200e",
    "\u200f",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)),
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\xad",
    "\U000e0041",
    "\x00",
    "\x07",
    "\x1b",
    "\x7f",
    "\x85",
    "\x0b",
    "\x0c",
    "\x1c",
    "\u2028",
    "\u2029",
    "\ud800",
]


@pytest.mark.parametrize("ch", HIDDEN)
def test_visible_shows_hidden_chars(ch: str) -> None:
    n = ord(ch)
    shown = f"\\u{n:04X}" if n <= 0xFFFF else f"\\U{n:08X}"
    assert visible(f"a{ch}b") == f"a{shown}b"


def test_visible_escape_format() -> None:
    assert visible("\u202e") == "\\u202E"
    assert visible("\x00") == "\\u0000"
    assert visible("\U000e0041") == "\\U000E0041"
    assert visible("x\ud800").encode() == b"x\\uD800"


def test_visible_keeps_text() -> None:
    kept = "a\nb\tc caf\xe9 \xfc\xdf \u65e5\u672c\u8a9e \U0001f600 \u2764\ufe0f \xa0 \u03a9"
    assert visible(kept) == kept
    assert visible("a\r\nb\rc\n") == "a\nb\nc\n"
    for s in CORPUS:
        assert visible(visible(s)) == visible(s)


def test_text_examples() -> None:
    assert text("  Hello,\n\tworld!  ") == "Hello\\, world\\!"
    assert text("1. a") == "1\\. a"
    assert text("see https://x.example/a now") == "see `https://x.example/a` now"
    assert text("cost $5 at 12:30:45") == "cost `$5` at `12:30:45`"
    assert text("ping @someone") == "ping `@someone`"
    assert text("a\\") == "a\\\\"
    assert text("") == ""


def test_cell_examples() -> None:
    assert cell("") == ""
    assert cell("a|b") == "a\\|b"
    assert cell("x@y.example|z") == "`x@y.example\\|z`"
    assert text("x@y.example|z") == "`x@y.example|z`"


def test_code_examples() -> None:
    assert code("") == ""
    assert code("a`b") == "``a`b``"
    assert code("`") == "`` ` ``"
    assert code("a``") == "``` a`` ```"
    assert code("   ") == "`   `"
    assert code(" a ") == "`  a  `"
    assert code(" a") == "` a`"
    assert code("a|b", in_cell=True) == "`a\\|b`"
    assert code("a\nb") == "`a b`"
    assert MD.render(code(" a ")) == "<p><code> a </code></p>\n"


def test_quote_paragraphs() -> None:
    assert quote("a\n\n  b  \n\tc d") == "> a\n>\n> b\n>\n> c d"
    assert quote("   \n\t") == ""
    assert quote("hi", more="[step 3](#step-3)") == "> hi"


def test_quote_cut() -> None:
    s = "word " * 1000
    out = quote(s, more="[step 3](#step-3)")
    assert out.endswith("\n\nCut, 3,000 more characters. [step 3](#step-3)")
    rendered = MD.render(out)
    assert rendered.endswith('<p>Cut, 3,000 more characters. <a href="#step-3">step 3</a></p>\n')
    assert plain(rendered.split("</blockquote>")[0]).strip() == ("word " * 400).strip()
    assert "Cut" not in quote("x" * PROSE_CHARS)
    assert quote("x" * (PROSE_CHARS + 1)).endswith("Cut, 1 more character.")


PIECES = [
    "https://x.example/a",
    "www.",
    "@a",
    ":a:",
    "$",
    "mailto:",
    "javascript:",
    "`",
    "``",
    "\\",
    "|",
    "*",
    "_",
    "~",
    "<b>",
    "</b>",
    "<",
    ">",
    "&",
    "&amp;",
    "&#60;",
    "#",
    "[",
    "](",
    ")",
    "!",
    "-",
    "+",
    "=",
    "1.",
    ":",
    ".",
    "\n",
    "\r\n",
    "\r",
    "\t",
    " ",
    "   ",
    "\xa0",
    "\u3000",
    "\u202e",
    "\u200b",
    "\x00",
    "\x85",
    "\u2028",
    "\ud800",
    "\U000e0041",
    "\ufeff",
]
UNTRUSTED = st.lists(st.sampled_from(PIECES) | st.text(max_size=3), max_size=24).map("".join)
TABLE = [
    "table_open",
    "thead_open",
    "tr_open",
    *["th_open", "inline", "th_close"] * 2,
    "tr_close",
    "thead_close",
    "tbody_open",
    "tr_open",
    *["td_open", "inline", "td_close"] * 2,
    "tr_close",
    "tbody_close",
    "table_close",
]


def spans(inline: Token) -> list[Token]:
    """The inline children, checked to be text and code spans only."""
    children = inline.children or []
    assert {c.type for c in children} <= {"text", "code_inline"}, children
    return children


def paragraph(md: str) -> list[Token]:
    tokens = MD.parse(md)
    assert [t.type for t in tokens] == ["paragraph_open", "inline", "paragraph_close"], md
    return spans(tokens[1])


def first_cell(md: str) -> list[Token]:
    tokens = MD.parse(f"| a | b |\n| --- | --- |\n| {md} | x |\n")
    assert [t.type for t in tokens] == TABLE, md
    assert "".join(c.content for c in spans(tokens[17])) == "x"
    return spans(tokens[14])


@given(UNTRUSTED)
def test_text_is_only_text_and_code_spans(s: str) -> None:
    """text(s) parses as text and code spans in at most one paragraph, holding exactly the
    characters of visible(s) with whitespace folded."""
    if not folded(s):
        assert text(s) == ""
        return
    assert "".join(c.content for c in paragraph(text(s))) == folded(s)


@given(UNTRUSTED)
def test_a_cell_is_only_text_and_code_spans(s: str) -> None:
    """cell(s) in a table row parses as text and code spans in that one cell, holding exactly
    the characters of visible(s) with whitespace folded."""
    assert "".join(c.content for c in first_cell(cell(s))) == folded(s)


@given(UNTRUSTED, st.booleans())
def test_code_is_one_code_span(s: str, in_cell: bool) -> None:
    """code(s) parses as one code span, in a paragraph or a table cell, holding exactly the
    characters of visible(s) with newlines as spaces. Other whitespace is kept."""
    want = visible(s).replace("\n", " ")
    md = code(s, in_cell=in_cell)
    if not want:
        assert md == ""
        return
    children = first_cell(md) if in_cell else paragraph(md)
    assert [c.type for c in children] == ["code_inline"]
    assert same_code(children[0].content, want)
