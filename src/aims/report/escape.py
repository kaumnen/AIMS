"""Turn untrusted text into Markdown that renders as exactly the characters it holds."""

import re
import string
import unicodedata
from collections.abc import Callable

PROSE_CHARS = 2000

_NEWLINES = re.compile(r"\r\n?")
_SUSPECT = re.compile(r"[^\t\n -~]+")
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_ESCAPES = str.maketrans({c: "\\" + c for c in string.punctuation})
_TICKS = re.compile(r"`+")
_TRAILING = ".,;!?)\"'"
_LINKY = re.compile(
    r"[a-z][a-z0-9+.-]*://"
    r"|\b(?:https?|ftp|mailto|javascript|vbscript|data|file|xmpp|tel):\S"
    r"|www\.|@\w|:[\w+-]+:|\$",
    re.IGNORECASE,
)


def _code_point(ch: str) -> str:
    n = ord(ch)
    return f"\\u{n:04X}" if n <= 0xFFFF else f"\\U{n:08X}"


def _cut_note(rest: int, more: str) -> str:
    note = f"Cut, {rest:,} more character{'' if rest == 1 else 's'}."
    return f"{note} {more}" if more else note


def reveal(text: str, show: Callable[[str], str]) -> str:
    """`text` with each character visible() hides written as show(character), CR too. The
    one set of hidden characters, for visible() and for writers of other formats."""

    def hidden(match: re.Match[str]) -> str:
        run = match[0]
        if run.isprintable():
            return run
        return "".join(show(c) if unicodedata.category(c) in _HIDDEN else c for c in run)

    return _SUSPECT.sub(hidden, text)


def visible(text: str) -> str:
    """Bidi controls (U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to U+2069), zero-width
    chars (U+200B to U+200D, U+2060, U+FEFF), other format chars (Unicode category Cf), control
    chars (category Cc) except newline and tab, lone surrogates (Cs) plus U+2028 and U+2029,
    shown as the visible text `\\uXXXX` (or `\\UXXXXXXXX` above U+FFFF). CR and CRLF become
    newline first."""
    return reveal(_NEWLINES.sub("\n", text), _code_point)


def _word(word: str, in_cell: bool) -> str:
    core = word.rstrip(_TRAILING)
    if not _LINKY.search(core):
        return code(word, in_cell=in_cell) if _LINKY.search(word) else word.translate(_ESCAPES)
    return code(core, in_cell=in_cell) + word[len(core) :].translate(_ESCAPES)


def _inline(s: str, in_cell: bool) -> str:
    return " ".join(_word(word, in_cell) for word in visible(s).split())


def text(s: str) -> str:
    """One line of inline Markdown that renders as `s` literally: visible(), whitespace runs
    (newlines and tabs too) folded to one space, stripped; every ASCII punctuation char
    backslash-escaped. A word (run without whitespace) holding a URL (http, https, ftp, mailto,
    javascript, data, file schemes or any `scheme://`), a `www.` host, an email, an `@handle`,
    a `:shortcode:` or a `$` goes in a code span instead, trailing punctuation left out."""
    return _inline(s, in_cell=False)


def cell(s: str) -> str:
    """text() for a GFM table cell: also `|` written `\\|` inside code spans. Empty input
    gives an empty string."""
    return _inline(s, in_cell=True)


def code(s: str, *, in_cell: bool = False) -> str:
    """An inline code span holding `s` literally: visible(), newlines to spaces, a backtick
    fence one longer than the longest backtick run inside, padded with a space when `s` starts
    or ends with a backtick or both starts and ends with a space (unless all spaces), `|` as
    `\\|` when `in_cell`. Empty input gives an empty string."""
    s = visible(s).replace("\n", " ")
    if not s:
        return ""
    ticks = "`" * (max(map(len, _TICKS.findall(s)), default=0) + 1)
    if "`" in (s[0], s[-1]) or (s[0] == s[-1] == " " and s.strip(" ")):
        s = f" {s} "
    if in_cell:
        s = s.replace("|", "\\|")
    return f"{ticks}{s}{ticks}"


def quote(s: str, *, more: str = "") -> str:
    """`s` as a Markdown blockquote: each non-blank source line a paragraph of text()
    (blank `>` lines between), cut at PROSE_CHARS characters of `s`. A cut adds a paragraph
    `Cut, N more characters.` after the blockquote with N formatted like 1,234, followed by
    a space plus `more` when `more` is given. `more` is trusted code-written Markdown (a link
    to the step), inserted as is. Empty or blank `s` gives ""."""
    lines = (text(line) for line in visible(s[:PROSE_CHARS]).split("\n"))
    out = "\n>\n".join(f"> {line}" for line in lines if line)
    if len(s) > PROSE_CHARS:
        note = _cut_note(len(s) - PROSE_CHARS, more)
        out = f"{out}\n\n{note}" if out else note
    return out
