"""What a command writes to its terminal. What it does when the terminal hangs up."""

import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import sys
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


def _finite(value: object) -> object:
    """`value` with each NaN and infinity as None. JSON has no word for them, and `NaN` in the
    output would not parse."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def json_text(value: object, *, indent: int | None = None) -> str:
    """`value` as JSON text for stdout. Each character visible() would show as text, such as
    U+009B or U+202E, is written as its JSON escape `\\u009b`, so the text parses to the same
    value. Other text stays as it is, so words in any script stay readable. A lone surrogate
    becomes `\\ud800` too, so the print cannot fail to encode it. NaN and the infinities are
    written as null."""
    try:
        text = json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False)
    except ValueError:
        text = json.dumps(_finite(value), ensure_ascii=False, indent=indent, allow_nan=False)
    return reveal(text, _json_escape)


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


class Stdout:
    """Writes a command's output with `echo`, such as typer.echo. Once the reader of its pipe
    has closed it, as `| head` does, the rest goes to /dev/null, so the command goes on and
    ends as it would, with one short note on stderr. `closed` says it was."""

    def __init__(self, echo: Callable[..., object]) -> None:
        self.echo = echo
        self.closed = False

    def __call__(self, text: str, *, nl: bool = True) -> None:
        if self.closed:
            return
        try:
            self.echo(text, nl=nl)
        except BrokenPipeError:
            self.close()

    def close(self) -> None:
        """After a write to the closed pipe failed: point stdout at /dev/null, so no later
        write and not the flush at exit fails. Python would end that with exit code 120."""
        if self.closed:
            return
        self.closed = True
        hang_up((1,))
        with contextlib.suppress(OSError):
            print("note: stdout was closed; the rest of the output is dropped", file=sys.stderr)


QUIT_AT = 3
AGAIN = "another Ctrl-C quits at once and skips the cleanup"
_SHOW_CURSOR = b"\x1b[?25h"


def quit_now() -> None:
    """End the process at once with exit code 130 and no cleanup. The words hold whether a
    cleanup was under way or none had begun, as when three signals came before the loop
    turned. A live view hides the cursor, so a terminal gets it back."""
    with contextlib.suppress(OSError):
        cursor = _SHOW_CURSOR if os.isatty(2) else b""
        os.write(2, cursor + b"quit at once and skipped the cleanup\n")
    os._exit(130)


def _say(text: str) -> None:
    # a terminal that hung up fails the write
    with contextlib.suppress(OSError):
        print(text, file=sys.stderr)


def on_stop(
    loop: asyncio.AbstractEventLoop,
    then: Callable[[], object],
    *,
    signals: Sequence[signal.Signals] = (signal.SIGINT, signal.SIGTERM),
    say: Callable[[str], object] | None = _say,
    last: Callable[[], object] | None = None,
) -> None:
    """Handle Ctrl-C and SIGTERM: each calls `then` on the loop. The second also gives AGAIN
    to `say`. The third calls `last` and ends the process through quit_now, so a cleanup that
    hangs needs no SIGKILL. That count is kept where Python runs signal handlers and not on
    the loop, as a cleanup that blocks the loop would never run a callback. Signals of one
    kind sent with no gap can arrive as one, so a supervisor that must end the process sends
    SIGKILL. The handlers stay in until the loop closes. Main thread only."""
    count = 0
    told = 0

    def on_loop() -> None:
        nonlocal told
        told += 1
        then()
        if told == QUIT_AT - 1 and say is not None:
            say(AGAIN)

    def on_signal(signum: int, frame: object) -> None:
        nonlocal count
        count += 1
        if count >= QUIT_AT:
            if last is not None:
                with contextlib.suppress(Exception):
                    last()
            quit_now()

    for sig in signals:
        # The loop hears of a signal through its wakeup fd and calls on_loop. The handler
        # it gives Python does nothing, so on_signal can take its place.
        loop.add_signal_handler(sig, on_loop)
        signal.signal(sig, on_signal)


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
