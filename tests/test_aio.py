"""asyncio helpers: waits that outlast a cancel of the caller."""

import asyncio
import gc
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from aims.aio import every, in_turn, settle, shielded, stopped


@contextmanager
def loop_errors() -> Generator[list[str]]:
    """Messages the running loop's exception handler gets during the test."""
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    errors: list[str] = []
    loop.set_exception_handler(lambda _, context: errors.append(context["message"]))
    try:
        yield errors
    finally:
        loop.set_exception_handler(handler)


@pytest.mark.anyio
async def test_a_cancel_of_the_caller_leaves_the_future_running() -> None:
    fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(shielded(fut))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not fut.done()
    fut.set_result(7)
    assert await shielded(fut) == 7


@pytest.mark.anyio
async def test_a_cancelled_future_raises_cancelled() -> None:
    fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(shielded(fut))
    await asyncio.sleep(0)
    fut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.anyio
async def test_a_failure_read_after_the_cancel_is_not_logged() -> None:
    # Python 3.14.0 to 3.14.8 log it from asyncio.shield even when it is read (gh-156321).
    with loop_errors() as errors:
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        waiter = asyncio.create_task(shielded(fut))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        fut.set_exception(RuntimeError("late"))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="late"):
            await shielded(fut)
        await asyncio.sleep(0)
    assert errors == []


@pytest.mark.anyio
async def test_settle_outlasts_two_cancels_and_logs_nothing() -> None:
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()
        raise RuntimeError("close failed")

    with loop_errors() as errors:
        task = asyncio.create_task(settle(work()))
        for _ in range(2):
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        gate.set()
        with pytest.raises(RuntimeError, match="close failed"):
            await task
        await asyncio.sleep(0)
    assert errors == []


@pytest.mark.anyio
@pytest.mark.parametrize("first", ["cancelled", "failed"])
async def test_every_waits_for_the_rest_when_one_ends_cancelled_or_fails(first: str) -> None:
    """asyncio.gather would raise at the first end, while the slow one still seals."""
    gate = asyncio.Event()
    began = asyncio.Event()
    sealed: list[str] = []

    async def quick() -> str:
        await began.wait()
        if first == "failed":
            raise RuntimeError("quick failed")
        await asyncio.Event().wait()
        return "quick"

    async def slow() -> str:
        try:
            began.set()
            await asyncio.Event().wait() if first == "cancelled" else await gate.wait()
        finally:
            await settle(gate.wait())
            sealed.append("slow")
        return "slow"

    task = asyncio.create_task(every([quick(), slow()]))
    await began.wait()
    await asyncio.sleep(0)
    if first == "cancelled":
        task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError if first == "cancelled" else RuntimeError):
        await task
    assert sealed == ["slow"]


@pytest.mark.anyio
async def test_every_returns_the_results_in_order() -> None:
    async def one(n: int) -> int:
        await asyncio.sleep(0.01 * (3 - n))
        return n

    assert await every(one(n) for n in range(3)) == [0, 1, 2]


async def _slow_to_end(ended: list[str], name: str, error: Exception | None = None) -> None:
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await asyncio.sleep(0.05)
        ended.append(name)
        if error is not None:
            raise error from None
        raise


@pytest.mark.anyio
async def test_stopped_raises_a_cancel_of_the_caller_once_every_task_has_ended() -> None:
    ended: list[str] = []
    tasks = [asyncio.create_task(_slow_to_end(ended, name)) for name in ("a", "b")]
    await asyncio.sleep(0)
    after: list[str] = []

    async def close() -> None:
        await stopped(*tasks)
        after.append("went on")

    closing = asyncio.create_task(close())
    await asyncio.sleep(0.01)
    closing.cancel()
    await asyncio.sleep(0.01)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert sorted(ended) == ["a", "b"]
    assert after == []


@pytest.mark.anyio
async def test_stopped_takes_a_task_s_own_cancel_for_its_end() -> None:
    ended: list[str] = []
    task = asyncio.create_task(_slow_to_end(ended, "a"))
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    await asyncio.sleep(0)
    await stopped(task, done)
    await stopped()
    assert ended == ["a"]
    assert task.cancelled()


@pytest.mark.anyio
async def test_stopped_raises_the_first_error_after_all_ended_and_logs_none() -> None:
    ended: list[str] = []
    tasks = [
        asyncio.create_task(_slow_to_end(ended, "a", ValueError("a"))),
        asyncio.create_task(_slow_to_end(ended, "b", ValueError("b"))),
        asyncio.create_task(_slow_to_end(ended, "c")),
    ]
    await asyncio.sleep(0)
    with loop_errors() as errors:
        with pytest.raises(ValueError, match="a"):
            await stopped(*tasks)
        assert sorted(ended) == ["a", "b", "c"]
        del tasks
        gc.collect()
        await asyncio.sleep(0)
    assert errors == []


@pytest.mark.anyio
async def test_stopped_puts_the_caller_s_cancel_ahead_of_a_task_s_error() -> None:
    ended: list[str] = []
    task = asyncio.create_task(_slow_to_end(ended, "a", ValueError("a")))
    await asyncio.sleep(0)
    with loop_errors() as errors:
        closing = asyncio.create_task(stopped(task))
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert ended == ["a"]
        del task
        gc.collect()
        await asyncio.sleep(0)
    assert errors == []


@pytest.mark.anyio
async def test_in_turn_runs_the_steps_after_one_that_fails_then_raises_the_first_error() -> None:
    ran: list[str] = []

    async def step(name: str, error: Exception | None = None) -> None:
        ran.append(name)
        if error is not None:
            raise error

    with pytest.raises(ValueError, match="first"):
        await in_turn(
            lambda: step("a"),
            None,
            lambda: step("b", ValueError("first")),
            lambda: step("c", KeyError("second")),
            lambda: step("d"),
        )
    assert ran == ["a", "b", "c", "d"]


@pytest.mark.anyio
async def test_in_turn_ends_every_step_under_a_cancel_then_raises_it() -> None:
    ran: list[str] = []
    began = asyncio.Event()

    async def slow() -> None:
        began.set()
        await asyncio.sleep(0.05)
        ran.append("slow")

    async def last() -> None:
        ran.append("last")
        raise ValueError("an error after the cancel")

    task = asyncio.create_task(in_turn(slow, last))
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ran == ["slow", "last"]
