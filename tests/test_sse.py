"""The runner reads its command stream with httpx2's SSE client; FastAPI writes it. The UI's
stream keeps going while runners come and go."""

import asyncio
import json
import random
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast

import httpx2
import pytest
from fastapi import Request
from fastapi.sse import ServerSentEvent, format_sse_event

from aims import __version__
from aims.server.auth import Principal
from aims.server.state import Connected, close_state, open_state
from aims.server.stream import stream
from aims.wire import RunnerHealthMessage, RunnerHello, RunnerInfo, TracerHealth

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


async def test_a_runner_that_says_hello_while_the_stream_sends_health_keeps_it_open(
    tmp_path: Path,
) -> None:
    def runner(name: str) -> Connected:
        hello = RunnerHello(
            name=name, version=__version__, arch="arm64", tracer=TracerHealth(alive=True)
        )
        return Connected(name, "r1", hello)

    state = await open_state(tmp_path / "server")
    state.runners["a"] = runner("a")
    viewer = Principal("viewer", "viewer", "bearer")
    events = cast(
        AsyncGenerator[ServerSentEvent],
        stream(Request({"type": "http"}), viewer, state, None, runs=True),
    )

    async def health() -> RunnerInfo:
        async with asyncio.timeout(5):
            while True:
                sse = await anext(events)
                if isinstance(sse.data, RunnerHealthMessage):
                    return sse.data.runner

    try:
        assert (await health()).name == "a"
        state.runners["a"].streams = 1
        state.runner_changed()
        assert (await health()).connected
        # the stream now waits inside its walk over the runners
        state.runners["b"] = runner("b")
        state.runner_changed()
        assert (await health()).name == "b"
    finally:
        await events.aclose()
        await close_state(state)
