"""The runner reads its command stream with httpx2's SSE client; FastAPI writes it."""

import json
import random
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi.sse import format_sse_event

pytestmark = pytest.mark.anyio


class _Chunks(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def read(chunks: list[bytes]) -> list[httpx2.ServerSentEvent]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        headers = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=headers, stream=_Chunks(chunks))

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client,
        client.sse("http://cp.invalid/stream") as source,
    ):
        return [e async for e in source]


@pytest.mark.parametrize("seed", range(20))
async def test_reads_what_fastapi_writes_in_any_split(seed: int) -> None:
    rng = random.Random(seed)
    sent = []
    wire = b": keepalive\n\n"
    for i in range(30):
        data = json.dumps({"n": i, "text": "line one\nline two" if i % 3 else "é ☃ \u2028"})
        event = rng.choice([None, "command", "offer"])
        wire += format_sse_event(data_str=data, event=event, id=str(i), comment="x" * (i % 2))
        sent.append((event or "message", data, str(i)))
    wire += format_sse_event(data_str="1", retry=2000)
    cuts = sorted(rng.sample(range(1, len(wire)), 25))
    chunks = [wire[a:b] for a, b in zip([0, *cuts], [*cuts, len(wire)], strict=True)]
    got = await read(chunks)
    assert [(e.event, e.data, e.id) for e in got[:-1]] == sent
    assert (got[-1].data, got[-1].retry, got[-1].id) == ("1", 2000, "29")


async def test_line_endings_and_multibyte_splits() -> None:
    wire = b"data: a\r\ndata:b\r\nid: 7\r\r" + "data: ☃\n\n".encode()
    got = await read([wire[:9], wire[9:22], wire[22:25], wire[25:]])
    assert [(e.data, e.id) for e in got] == [("a\nb", "7"), ("☃", "7")]
