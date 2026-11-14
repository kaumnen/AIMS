import asyncio
import errno
import os
import signal
import subprocess
import sys
import time

import pytest

from aims.terminal import AGAIN, hang_up, on_hangup, on_stop

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


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_the_second_stop_warns_and_the_third_quits(
    quits: list[bool], sig: signal.Signals
) -> None:
    seen: list[str] = []
    loop = asyncio.get_running_loop()
    on_stop(
        loop,
        lambda: seen.append("stop"),
        say=seen.append,
        last=lambda: seen.append(f"last, {len(quits)} quits"),
    )
    try:
        for n in (1, 2):
            os.kill(os.getpid(), sig)
            async with asyncio.timeout(5):
                while seen.count("stop") < n:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        assert seen == ["stop", "stop", AGAIN]
        assert quits == []
        os.kill(os.getpid(), sig)
        # Python runs the handler before the loop turns
        assert seen[3:] == ["last, 0 quits"]
        assert quits == [True]
    finally:
        quits.clear()
        for handled in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(handled)


async def test_a_hangup_does_not_count_as_a_stop(quits: list[bool]) -> None:
    stops: list[str] = []
    loop = asyncio.get_running_loop()
    guard = signal.signal(signal.SIGHUP, lambda *_: None)
    on_stop(loop, lambda: stops.append("stop"), say=stops.append)
    try:
        assert on_hangup(loop, lambda: stops.append("hangup"))
        for _ in range(4):
            os.kill(os.getpid(), signal.SIGHUP)
        async with asyncio.timeout(5):
            while len(stops) < 4:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert stops == ["hangup"] * 4
    finally:
        for handled in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.remove_signal_handler(handled)
        signal.signal(signal.SIGHUP, guard)


BLOCKED = """
import asyncio, sys, time
from aims.terminal import on_stop

async def main():
    on_stop(asyncio.get_running_loop(), lambda: None, last=lambda: print("last", flush=True))
    print("ready", flush=True)
    time.sleep(60)  # a cleanup that blocks the loop

asyncio.run(main())
"""


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_a_third_stop_ends_a_process_whose_loop_is_blocked(sig: signal.Signals) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", BLOCKED], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == "ready\n"
        began = time.monotonic()
        for _ in range(2):
            proc.send_signal(sig)
            time.sleep(0.3)
        assert proc.poll() is None
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=20)
    finally:
        proc.kill()
        proc.wait()
    assert proc.returncode == 130
    assert out == "last\n"
    assert err == QUIT_TEXT
    assert time.monotonic() - began < 20


QUIT_TEXT = "quit at once and skipped the cleanup\n"
BURST = """
import asyncio, os, signal
from aims.terminal import on_stop

async def main():
    on_stop(asyncio.get_running_loop(), lambda: print("stop", flush=True))
    # a burst as from a supervisor, before the loop turns: no stop has begun
    for _ in range(3):
        os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(60)

asyncio.run(main())
"""


def test_a_burst_of_three_stops_before_any_cleanup_quits_with_true_words() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", BURST], capture_output=True, text=True, timeout=20, check=False
    )
    assert proc.returncode == 130
    assert proc.stdout == ""
    assert proc.stderr == QUIT_TEXT
