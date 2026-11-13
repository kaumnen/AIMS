"""asyncio helpers shared by the layers."""

import asyncio
import contextlib
import io
import queue
from collections.abc import AsyncIterator, Awaitable, Buffer, Callable, Iterable


async def shielded[T](fut: asyncio.Future[T]) -> T:
    """Await `fut`; a cancel of the caller leaves it running. Same as asyncio.shield, minus
    the error log Python 3.14.0 to 3.14.8 write when `fut` fails after that cancel, even if
    it is read later (gh-156321). Drop it once requires-python is past 3.14.8."""
    await asyncio.wait((fut,))
    return fut.result()


async def settle[T](aw: Awaitable[T]) -> T:
    """Await `aw` to its end however often the caller is cancelled meanwhile, then raise the
    cancel. A bare shield gives up at the next cancel and leaves `aw` running unawaited."""
    task = asyncio.ensure_future(aw)
    cancelled = False
    while True:
        try:
            value = await shielded(task)
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return value


async def every[T](aws: Iterable[Awaitable[T]]) -> list[T]:
    """Await all of `aws` to their end, then return their results in order, or raise the first
    error among them. A bare asyncio.gather raises as soon as one fails or ends cancelled,
    while the others still run: a caller that then closes what they use cuts their ends."""
    found = await asyncio.gather(*aws, return_exceptions=True)
    results: list[T] = []
    for one in found:
        if isinstance(one, BaseException):
            raise one
        results.append(one)
    return results


class Pipe(io.RawIOBase):
    """A file a worker thread reads, which the event loop fills one chunk at a time."""

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.get_running_loop()
        self._asks: asyncio.Queue[bool] = asyncio.Queue()
        self._chunks: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._rest = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        if not self._rest:
            self._ask(True)
            chunk = self._chunks.get()
            if chunk is None:
                raise EOFError("the stream was stopped")
            self._rest = memoryview(chunk)
        with memoryview(buffer) as out:
            n = min(len(out), len(self._rest))
            out[:n] = self._rest[:n]
        self._rest = self._rest[n:]
        return n

    def done(self) -> None:
        """In the thread: no more reads, so the loop stops serving."""
        self._ask(False)

    async def serve(self, chunks: AsyncIterator[bytes]) -> None:
        """On the loop: answer each read with the next chunk, until the thread is done."""
        while await self._asks.get():
            self._chunks.put(await anext(chunks, b""))

    def stop(self) -> None:
        """Make the thread's next read fail."""
        self._chunks.put(None)

    def _ask(self, more: bool) -> None:
        self._loop.call_soon_threadsafe(self._asks.put_nowait, more)


async def read_in_thread[T](chunks: AsyncIterator[bytes], fn: Callable[[Pipe], T]) -> T:
    """Run `fn` in a worker thread on a pipe that reads `chunks` as the thread asks.

    A cancel, or an error of `chunks`, makes the thread's next read fail. Either way the
    thread is awaited to its end, so nothing reads `chunks` once this returns.
    """
    pipe = Pipe()

    def run() -> T:
        try:
            return fn(pipe)
        finally:
            pipe.done()

    work = asyncio.ensure_future(asyncio.to_thread(run))
    try:
        await pipe.serve(chunks)
    except BaseException:
        pipe.stop()
        with contextlib.suppress(Exception):
            await settle(work)
        raise
    return await settle(work)
