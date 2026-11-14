"""The suite's own fixtures."""

import signal
from typing import Any

LEFT = [signal.SIGTERM, signal.SIGHUP]
before: list[Any] = []


def test_a_test_may_leave_signals_ignored() -> None:
    before.extend(signal.getsignal(sig) for sig in LEFT)
    for sig in LEFT:
        signal.signal(sig, signal.SIG_IGN)


def test_the_next_test_starts_with_the_handlers_from_before() -> None:
    # the test above ran first on this worker, as `--dist loadfile` keeps a file together
    if before:
        assert [signal.getsignal(sig) for sig in LEFT] == before
