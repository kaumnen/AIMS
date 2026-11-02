import asyncio
import errno
import os
import signal

import pytest

from aims.terminal import hang_up, on_hangup

pytestmark = pytest.mark.anyio


def test_a_stream_on_a_terminal_that_hung_up_takes_writes_again() -> None:
    main, tty = os.openpty()
    try:
        os.write(tty, b"before\n")
        # what closing the window does: the terminal hangs up and writes to it fail
        os.close(main)
        with pytest.raises(OSError, match="Input/output error") as e:
            os.write(tty, b"after\n")
        assert e.value.errno == errno.EIO
        hang_up((tty,))
        assert os.write(tty, b"after\n") == len(b"after\n")
    finally:
        os.close(tty)


async def test_a_hangup_quiets_the_streams_then_calls_its_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    called = asyncio.Event()
    monkeypatch.setattr("aims.terminal.hang_up", lambda fds: seen.append("quiet"))
    guard = signal.signal(signal.SIGHUP, lambda *_: None)
    loop = asyncio.get_running_loop()

    def then() -> None:
        seen.append("stop")
        called.set()

    try:
        assert on_hangup(loop, then)
        os.kill(os.getpid(), signal.SIGHUP)
        async with asyncio.timeout(5):
            await called.wait()
        assert seen == ["quiet", "stop"]
    finally:
        loop.remove_signal_handler(signal.SIGHUP)
        signal.signal(signal.SIGHUP, guard)


async def test_a_hangup_ignored_from_the_start_stays_ignored() -> None:
    # nohup starts a command with SIGHUP ignored, so it outlives the terminal
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert not on_hangup(asyncio.get_running_loop(), lambda: None)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)
