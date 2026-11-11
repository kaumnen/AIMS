"""What a command writes to its terminal. What it does when the terminal hangs up."""

import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from aims.report.escape import reveal, visible

if TYPE_CHECKING:
    from rich.text import Text


def shown(text: object, style: str = "") -> Text:
    """`text` for a Rich console as the characters it holds, in `style`. visible() turns the
    hidden ones into text. Rich reads a Text as plain text. It reads a str as markup and emoji
    codes, which rich.markup.escape covers only in part: it drops the backslash of `\\[1]`,
    doubles a trailing one and leaves `:smile:` to become an emoji."""
    from rich.text import Text

    return Text(visible(str(text)), style=style)


def one_line(text: object) -> str:
    """visible(`text`) with each run of whitespace, newlines too, as one space: for text that
    must stay on its line, such as a field of a row in a listing."""
    return " ".join(visible(str(text)).split())


def _json_escape(ch: str) -> str:
    # json.dumps writes a non-ASCII character as \uXXXX, a pair of them above U+FFFF, and DEL
    # as \u007f
    return json.dumps(ch)[1:-1]


def json_text(value: object, *, indent: int | None = None) -> str:
    """`value` as JSON text for stdout. Each character visible() would show as text, such as
    U+009B or U+202E, is written as its JSON escape `\\u009b`, so the text parses to the same
    value. Other text stays as it is, so words in any script stay readable. A lone surrogate
    becomes `\\ud800` too, so the print cannot fail to encode it."""
    return reveal(json.dumps(value, ensure_ascii=False, indent=indent), _json_escape)


_BREAKS = str.maketrans({"\n": "\\u000A", "\r": "\\u000D"})


class VisibleLog(logging.Formatter):
    """Writes each log line, its traceback too, through visible(). A line break inside the
    message shows as `\\u000A` or `\\u000D`, so one record is one line and outside text cannot
    forge another; a break at its end is dropped. A traceback keeps its lines."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        return super().formatMessage(record).rstrip("\r\n").translate(_BREAKS)

    def format(self, record: logging.LogRecord) -> str:
        return visible(super().format(record))


def hang_up(fds: Sequence[int]) -> None:
    """Point `fds` at /dev/null. Writes to a terminal that hung up fail with EIO. One failed
    print would cut short the stop that follows."""
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        for fd in fds:
            os.dup2(null, fd)
    finally:
        os.close(null)


def on_hangup(loop: asyncio.AbstractEventLoop, then: Callable[[], object]) -> bool:
    """Handle SIGHUP, which comes when the terminal closes: point stdout and stderr at
    /dev/null if they were on the terminal, then call `then`. Shells pass the hangup on to
    their jobs, zsh twice, so `then` may run more than once. A SIGHUP ignored from the start,
    as under nohup, stays ignored. Returns whether the handler is in."""
    if signal.getsignal(signal.SIGHUP) == signal.SIG_IGN:
        return False
    # Linux stops calling them terminals once the terminal hangs up, so look now
    fds = tuple(fd for fd in (1, 2) if os.isatty(fd))

    def handle() -> None:
        hang_up(fds)
        then()

    loop.add_signal_handler(signal.SIGHUP, handle)
    return True
