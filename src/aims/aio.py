"""asyncio helpers shared by the layers."""

import asyncio
from collections.abc import Awaitable


async def settle[T](aw: Awaitable[T]) -> T:
    """Await `aw` to its end however often the caller is cancelled meanwhile, then raise the
    cancel. A bare shield gives up at the next cancel and leaves `aw` running unawaited."""
    task = asyncio.ensure_future(aw)
    cancelled = False
    while True:
        try:
            value = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return value
