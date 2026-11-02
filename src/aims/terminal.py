"""What a command does when its terminal hangs up."""

import asyncio
import os
import signal
from collections.abc import Callable, Sequence


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
