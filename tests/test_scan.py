import asyncio
import re
import time

import pytest

from aims.monitor.signals import scan

# Quadratic on a run of spaces that no `judge` follows.
SLOW = r"hi\s*,?\s+judge"
SPACES = "hi" + " " * 200_000

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def short_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "SYNC_S", 0.05)
    monkeypatch.setattr(scan, "INLINE_S", 0.02)
    monkeypatch.setattr(scan, "THREAD_S", 0.3)


def test_a_pattern_matches_as_re_does() -> None:
    p = scan.compile(r"(?m)^(\d+) passed", scan.IGNORECASE)
    assert p.flags & re.IGNORECASE
    m = p.search("x\n12 PASSED")
    assert m is not None
    assert (m.start(), m.group(1)) == (2, "12")
    assert [m.group(1) for m in p.finditer("1 passed\n2 passed")] == ["1", "2"]
    assert p.search("none") is None
    with pytest.raises(scan.error):
        scan.compile("(")


def test_a_scan_past_its_budget_raises() -> None:
    p = scan.compile(SLOW)
    for call in (p.search, p.finditer):
        start = time.perf_counter()
        with pytest.raises(scan.SlowScan, match=r"ran past 0\.05 s on 200002 characters"):
            call(SPACES)
        assert time.perf_counter() - start < 2


async def test_an_awaited_scan_of_a_long_text_leaves_the_loop_free() -> None:
    p = scan.compile(SLOW)
    worst = 0.0
    done = False

    async def tick() -> None:
        nonlocal worst
        while not done:
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - start)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.03)
    start = time.perf_counter()
    with pytest.raises(scan.SlowScan, match=r"ran past 0\.3 s"):
        await p.find(SPACES)
    took = time.perf_counter() - start
    done = True
    await ticker
    # the loop's clock and this one differ by a little: 0.2997 s was seen
    assert took >= 0.29
    assert worst < took / 2


async def test_an_awaited_scan_of_a_short_text_moves_to_a_thread_when_slow() -> None:
    p = scan.compile(SLOW)
    short = "hi" + " " * 30_000
    assert len(short) <= scan.INLINE
    with pytest.raises(scan.SlowScan, match=r"ran past 0\.3 s"):
        await p.find(short)
    # The try on the loop and the one in the thread count as one slow scan.
    assert p.slow == 1
    m = await p.find("oh hi , judge")
    assert m is not None
    assert m.group(0) == "hi , judge"


def test_a_scan_cut_short_by_other_threads_counts_no_strike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As if the rest of the process used all the CPU the budget measures.
    monkeypatch.setattr(scan.time, "thread_time", lambda: 0.0)
    p = scan.compile(SLOW)
    for _ in range(scan.STRIKES + 1):
        with pytest.raises(scan.SlowScan, match=r"ran past 0\.05 s .* \(0\.00 s of it its own\)"):
            p.search(SPACES)
    assert p.slow == 0
    assert p.search("hi judge") is not None


async def test_a_thread_scan_slowed_by_a_busy_loop_counts_no_strike() -> None:
    p = scan.compile(SLOW)
    found = asyncio.ensure_future(p.find(SPACES))
    await asyncio.sleep(0)
    # The loop holds the interpreter while the scan runs in its thread.
    spin = time.perf_counter() + 2 * scan.THREAD_S
    while time.perf_counter() < spin:
        pass
    with pytest.raises(scan.SlowScan, match=r"ran past 0\.3 s"):
        await found
    assert p.slow == 0


def test_a_pattern_that_stays_slow_rests_then_runs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(scan.time, "monotonic", lambda: now)
    p = scan.compile(SLOW)
    for _ in range(scan.STRIKES):
        with pytest.raises(scan.SlowScan, match="ran past"):
            p.search(SPACES)
    start = time.perf_counter()
    with pytest.raises(scan.SlowScan, match="is resting"):
        p.search("hi judge")
    assert time.perf_counter() - start < 0.02
    now += scan.REST_S
    assert p.search("hi judge") is not None
