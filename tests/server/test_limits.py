"""The request body cap: no caller, with a token or without, can make the server hold a body
past it. The routes that stream their body keep their own limits."""

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from aims import __version__
from aims.node.client import ControlClient
from aims.server import limits
from aims.server.app import create_app
from aims.server.limits import API_BODY, RUNNER_BODY, BodyLimit, cap
from aims.wire import RunnerHello, TracerHealth

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024
CHUNK = 64 * 1024
JSON = {"Content-Type": "application/json"}


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


def spaces(size: int) -> bytes:
    """A JSON body of `size` bytes."""
    return b" " * (size - 2) + b"{}"


async def chunked(size: int) -> AsyncIterator[bytes]:
    """The same body sent without a Content-Length."""
    data = spaces(size)
    for i in range(0, size, MIB):
        yield data[i : i + MIB]


@pytest.mark.parametrize("framing", ["length", "chunked"])
@pytest.mark.parametrize(
    ("path", "limit"), [("/api/runs", API_BODY), ("/runner/v1/hello", RUNNER_BODY)]
)
async def test_a_body_past_the_cap_is_refused_before_the_token_check_reads_it(
    cp: CP, path: str, limit: int, framing: str
) -> None:
    async with cp.client() as c:
        for size, status in ((limit, 401), (limit + 1, 413)):
            body = spaces(size) if framing == "length" else chunked(size)
            r = await c.post(path, content=body, headers=JSON)
            assert r.status_code == status, (size, r.text[:200])
        assert r.headers["content-type"] == "application/problem+json"
        assert r.headers["connection"] == "close"
        assert "content-security-policy" in r.headers
        assert r.json() == {
            "type": "about:blank",
            "title": "Too large",
            "status": 413,
            "detail": f"a request body is at most {limit} bytes",
            "errors": [],
        }


async def test_the_largest_body_the_api_takes_fits_under_the_cap(cp: CP) -> None:
    text = "\x01" * (1024 * 1024)  # the most text a field holds, each character escaped
    body = json.dumps({"text": text}).encode()
    assert 6 * MIB < len(body) <= API_BODY
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/config/project/check", content=body, headers=JSON)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["ok"] is False


async def test_streamed_uploads_keep_their_own_limits(cp: CP) -> None:
    data = os.urandom(API_BODY + MIB)
    async with cp.client(cp.operator) as c:
        r = await c.put(
            "/api/scenarios/project/control/files/data.bin",
            content=data,
            headers={"Content-Type": "application/octet-stream", "If-None-Match": "*"},
        )
    assert r.status_code in (200, 201), r.text[:200]
    assert (cp.project.parent / "scenarios" / "control" / "data.bin").read_bytes() == data

    client = ControlClient(cp.url, cp.runner, runner="r1")
    try:
        await client.hello(
            RunnerHello(
                name="r1", version=__version__, arch="arm64", tracer=TracerHealth(alive=True)
            )
        )
    finally:
        await client.aclose()
    blob = os.urandom(RUNNER_BODY + MIB)
    sha = hashlib.sha256(blob).hexdigest()
    async with cp.client(cp.runner) as c:
        r = await c.put(
            f"/runner/v1/runs/{uuid.uuid7()}/blobs/{sha}",
            content=blob,
            headers={"Content-Type": "application/octet-stream", "X-Aims-Runner": "r1"},
        )
    assert r.status_code == 204, r.text[:200]


# The middleware alone


def scope(path: str = "/api/runs", length: int | None = None) -> Scope:
    headers = [] if length is None else [(b"content-length", str(length).encode())]
    return {"type": "http", "method": "POST", "path": path, "headers": headers}


class Client:
    """A client that sends `size` bytes in chunks, or never stops when `size` is None. It
    keeps what the server sends back."""

    def __init__(self, size: int | None, *, stall_after: int | None = None) -> None:
        self.size = size
        self.stall_after = stall_after
        self.taken = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        if self.stall_after is not None and self.taken >= self.stall_after:
            await asyncio.Event().wait()
        left = CHUNK if self.size is None else min(CHUNK, self.size - self.taken)
        self.taken += left
        more = self.size is None or self.taken < self.size
        return {"type": "http.request", "body": b"\0" * left, "more_body": more}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    def reply(self) -> tuple[int, dict[bytes, bytes], Any]:
        start, body = self.sent
        return start["status"], dict(start["headers"]), json.loads(body["body"])


def reader() -> tuple[Callable[[Scope, Receive, Send], Awaitable[None]], list[int]]:
    """An app that keeps every body chunk it reads, as a JSON route does."""
    held = [0]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            held[0] += len(message.get("body", b""))
            if not message.get("more_body", False):
                return

    return app, held


async def test_a_refused_body_is_dropped_as_it_comes_not_kept() -> None:
    app, held = reader()
    client = Client(None)
    with pytest.raises(HTTPException) as e:
        await BodyLimit(app)(scope(), client.receive, client.send)
    assert e.value.status_code == 413
    assert e.value.headers == {"Connection": "close"}
    assert held[0] == API_BODY  # the chunk that crossed the cap never reached the app
    assert client.taken == 2 * API_BODY  # dropped up to twice the cap, then given up


async def test_a_body_that_ends_past_the_cap_is_read_to_its_end_before_the_reply() -> None:
    app, _ = reader()
    client = Client(API_BODY + 3 * CHUNK + 5)
    with pytest.raises(HTTPException):
        await BodyLimit(app)(scope(), client.receive, client.send)
    assert client.taken == client.size


async def test_a_length_past_the_cap_is_answered_without_running_the_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the app ran")

    far = Client(None)
    await BodyLimit(app)(scope(length=2 * API_BODY + 1), far.receive, far.send)
    assert far.taken == 0
    near = Client(API_BODY + 1)
    await BodyLimit(app)(scope(length=API_BODY + 1), near.receive, near.send)
    assert near.taken == API_BODY + 1
    for client in (far, near):
        status, headers, problem = client.reply()
        assert status == 413
        assert headers[b"content-type"] == b"application/problem+json"
        assert headers[b"connection"] == b"close"
        assert problem["detail"] == f"a request body is at most {API_BODY} bytes"


async def test_a_stalled_body_is_dropped_for_a_bounded_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "DRAIN_S", 0.05)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the app ran")

    client = Client(API_BODY + MIB, stall_after=CHUNK)
    async with asyncio.timeout(5):
        await BodyLimit(app)(scope(length=API_BODY + MIB), client.receive, client.send)
    assert client.reply()[0] == 413


async def test_a_short_or_missing_length_does_not_get_past_the_cap() -> None:
    for length in (None, 10, "x"):
        app, held = reader()
        client = Client(None)
        headers = [] if length is None else [(b"content-length", str(length).encode())]
        with pytest.raises(HTTPException):
            await BodyLimit(app)({**scope(), "headers": headers}, client.receive, client.send)
        assert held[0] == API_BODY


@pytest.mark.parametrize(
    ("method", "path", "limit"),
    [
        ("POST", "/api/runs/import", None),
        ("PUT", "/api/scenarios/project/x/files/a/b.txt", None),
        ("PUT", "/runner/v1/runs/r/blobs/s", None),
        ("PUT", "/runner/v1/runs/r/truth", None),
        ("POST", "/runner/v1/runs/r/events", None),
        ("POST", "/api/runs", API_BODY),
        ("POST", "/api/runs/import/x", API_BODY),
        ("POST", "/api/scenarios/project/x/files/a", API_BODY),
        ("PUT", "/runner/v1/runs/r/x/truth", RUNNER_BODY),
        ("POST", "/runner/v1/runs/r/blobs/missing", RUNNER_BODY),
        ("POST", "/runner/v1/hello", RUNNER_BODY),
        ("GET", "/anything", API_BODY),
    ],
)
def test_the_cap_of_each_route(method: str, path: str, limit: int | None) -> None:
    assert cap(method, path) == limit


async def test_only_the_routes_that_stream_their_body_go_without_the_cap(tmp_path: Path) -> None:
    from aims.server.state import close_state, open_state

    state = await open_state(tmp_path / "server")
    try:
        schema = create_app(state, dispatcher=False).openapi()
    finally:
        await close_state(state)
    uncapped = set()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            sample = re.sub(r"\{file\}", "a/b.txt", path)
            sample = re.sub(r"\{[^}]+\}", "x", sample)
            if cap(method.upper(), sample) is None:
                uncapped.add(operation["operationId"])
    assert uncapped == {"import_run", "write_file", "put_blob", "put_truth", "ingest_events"}
