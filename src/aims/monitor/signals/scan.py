"""Regex scans of text from the sandbox, each with a time budget.

A pattern that backtracks can take hours on text an agent shaped for it, and Python's `re` holds
the interpreter for a whole match. The `regex` package takes a timeout and lets other threads
run while it matches, so a scan here ends at its budget and a large text is scanned off the
event loop. A scan past its budget raises SlowScan. A pattern that keeps doing so rests for a
while, since each try would cost its budget again. A scan asked for in that time raises too.
"""

import asyncio
import time
from typing import Any

import regex

# A scan on the event loop: one a signal makes without awaiting.
SYNC_S = 0.5
# The first try of an awaited scan, on the event loop; a slower one moves to a thread.
INLINE_S = 0.05
# An awaited scan in a thread.
THREAD_S = 2.0
# Text up to this many characters is first tried on the event loop.
INLINE = 64 * 1024
# A pattern that ran past its budget this many times rests for REST_S seconds.
STRIKES = 3
REST_S = 60.0

# A match object of `regex`, which works as one of `re`. The package names no type for it.
type Match = Any

error = regex.error
IGNORECASE = regex.IGNORECASE
MULTILINE = regex.MULTILINE


class SlowScan(Exception):
    """A pattern ran past its time budget on a text, or did so too often to be run again."""


class Pattern:
    """A compiled pattern whose scans are budgeted. `search` and `finditer` run where they are
    called; `find` is awaited and keeps a long scan off the event loop."""

    def __init__(self, source: str, flags: int = 0) -> None:
        self.source = source
        self.raw = regex.compile(source, flags)
        self.slow = 0
        self.rest_until = 0.0

    @property
    def flags(self) -> int:
        return self.raw.flags

    def _check(self) -> None:
        if time.monotonic() < self.rest_until:
            raise SlowScan(f"pattern {self.source[:80]!r} is resting: it was too slow")

    def _run(self, call: Any, text: str, budget: float, last: bool = True, **options: Any) -> Any:
        try:
            return call(text, timeout=budget, **options)
        except TimeoutError:
            if last:
                self.slow += 1
                if self.slow >= STRIKES:
                    self.slow, self.rest_until = 0, time.monotonic() + REST_S
            raise SlowScan(
                f"pattern {self.source[:80]!r} ran past {budget} s on {len(text)} characters"
            ) from None

    def search(self, text: str) -> Match | None:
        """The first match in `text`. Raises SlowScan."""
        self._check()
        return self._run(self.raw.search, text, SYNC_S)

    def finditer(self, text: str) -> list[Match]:
        """Every match in `text`. Raises SlowScan."""
        self._check()
        return self._run(self._all, text, SYNC_S)

    def _all(self, text: str, **options: Any) -> list[Match]:
        return list(self.raw.finditer(text, **options))

    async def find(self, text: str) -> Match | None:
        """The first match in `text`, with a long scan run in a thread. Raises SlowScan."""
        self._check()
        if len(text) <= INLINE:
            try:
                return self._run(self.raw.search, text, INLINE_S, last=False)
            except SlowScan:
                pass
        return await asyncio.to_thread(self._run, self.raw.search, text, THREAD_S, concurrent=True)


def compile(pattern: str, flags: int = 0) -> Pattern:
    """`pattern` compiled for budgeted scans. Raises `scan.error` for a bad pattern."""
    return Pattern(pattern, flags)
