import asyncio
import inspect
from typing import Any, cast

import aiodocker
import pytest

from aims.sandbox.reader import Reader, ReaderError

pytestmark = pytest.mark.anyio


class SlowStream:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.asked = asyncio.Event()
        self.closed = False
        self.hold = asyncio.Event()

    async def write_in(self, data: bytes) -> None:
        self.written.append(data)
        self.asked.set()

    async def read_out(self) -> Any:
        await self.hold.wait()
        return None

    async def close(self) -> None:
        self.closed = True


async def test_a_cancelled_diff_breaks_the_reader_instead_of_desyncing() -> None:
    stream = SlowStream()
    reader = Reader(cast(Any, None), ("/work",))
    reader._stream = cast(Any, stream)
    task = asyncio.create_task(reader.tree())
    await stream.asked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed
    assert reader.broken == "file reader was cancelled mid-reply"
    with pytest.raises(ReaderError, match="cancelled mid-reply"):
        await reader.tree()


def test_aiodocker_still_has_the_private_call_the_archive_stream_uses() -> None:
    hint = (
        "stream_archive in src/aims/sandbox/reader.py streams a tar through aiodocker's private "
        "Docker._query, which this aiodocker dropped or changed. Use a public call that streams "
        "an archive if it has one, or follow the change."
    )
    query = getattr(aiodocker.Docker, "_query", None)
    assert query is not None, hint
    params = inspect.signature(query).parameters
    assert list(params)[:3] == ["self", "path", "method"], hint
    assert params["method"].default == "GET", hint
    assert "params" in params, hint
    assert inspect.isasyncgenfunction(inspect.unwrap(query)), hint
