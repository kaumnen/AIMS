"""asyncio helpers shared by the layers."""

import asyncio
from collections.abc import Awaitable


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
