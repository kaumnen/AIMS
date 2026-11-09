"""A cap on request bodies. FastAPI reads and parses a JSON body before the token check runs,
so without a cap a caller with no token could make the server hold a body of any size. The
routes that stream their body count it against their own limit and are left out."""

import asyncio
import contextlib
import re

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aims.server.problems import TITLES, Problem, problem_response

MIB = 1024 * 1024
API_BODY = 8 * MIB
"""Room for the longest text field the API takes: 1 MiB, at most 6 MiB once JSON escapes it."""
RUNNER_BODY = 16 * MIB
"""The size of an event batch on the wire (`runner_api.MAX_WIRE`), for every runner route."""
DRAIN_S = 10.0
"""How long the rest of a refused body is read and dropped before the 413 goes out."""
STREAMED = tuple(
    (method, re.compile(path))
    for method, path in (
        ("POST", r"/api/runs/import"),
        ("PUT", r"/api/scenarios/[^/]+/[^/]+/files/.*"),
        ("PUT", r"/runner/v1/runs/[^/]+/blobs/[^/]+"),
        ("PUT", r"/runner/v1/runs/[^/]+/truth"),
        ("POST", r"/runner/v1/runs/[^/]+/events"),
    )
)
"""The routes that read their body as a stream and stop at a limit of their own."""


def cap(method: str, path: str) -> int | None:
    """The most bytes a request body may hold; None for a route that counts its own."""
    if any(m == method and p.fullmatch(path) for m, p in STREAMED):
        return None
    return RUNNER_BODY if path.startswith("/runner/") else API_BODY


class BodyLimit:
    """Answers 413 to a body over the cap: before reading when its Content-Length says so,
    else as soon as the bytes read pass the cap, so a missing or false length changes
    nothing. Bytes past the cap are dropped as they come, never kept."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = cap(scope["method"], scope["path"]) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        detail = f"a request body is at most {limit} bytes"
        length = _length(scope)
        if length is not None and length > limit:
            if length <= 2 * limit:
                await _drain(receive, length)
            problem = Problem(title=TITLES[413], status=413, detail=detail)
            await problem_response(problem, {"Connection": "close"})(scope, receive, send)
            return
        read = 0

        async def counted() -> Message:
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > limit:
                    if message.get("more_body", False):
                        await _drain(receive, 2 * limit - read)
                    # a Starlette HTTPException, since FastAPI turns any other error raised
                    # while it reads a body into a 400
                    raise HTTPException(413, detail, headers={"Connection": "close"})
            return message

        await self.app(scope, counted, send)


async def _drain(receive: Receive, budget: int) -> None:
    """Read and drop the rest of a refused body, about `budget` bytes at most, for DRAIN_S at
    most. A server that closes while the client still sends makes TCP reset the connection.
    The client then never sees the reply. A body past the budget gets the reset."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(DRAIN_S):
            while budget > 0:
                message = await receive()
                if message["type"] != "http.request":
                    return
                budget -= len(message.get("body", b""))
                if not message.get("more_body", False):
                    return


def _length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    try:
        return None if value is None else int(value)
    except ValueError:
        return None
