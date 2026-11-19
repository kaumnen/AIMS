import base64
import contextvars
import json
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field

import httpx2

DROPPED_HEADERS = frozenset({"openai-organization", "openai-project"})
# The headers the OpenAI SDK sets for a request, besides its x-stainless- ones. It also adds
# headers from the env, such as OPENAI_CUSTOM_HEADERS, an Authorization header among them.
SDK_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
        "user-agent",
    }
)
UNRECORDED_HEADERS = frozenset({"set-cookie"})


@dataclass(slots=True)
class Exchange:
    """What went over the wire for one attempt. `chunks` are raw body bytes with the ms since
    the request was sent at which each arrived. A replayed attempt sets `origin` to the run
    and event it came from, and `retry` to whether that call retried it."""

    sent: float | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[tuple[int, bytes]] = field(default_factory=list)
    eof: bool = False
    origin: tuple[str, int] | None = None
    retry: bool | None = None

    def ms(self) -> int:
        return 0 if self.sent is None else int((time.monotonic() - self.sent) * 1000)

    def body(self) -> bytes:
        return b"".join(chunk for _, chunk in self.chunks)


current: contextvars.ContextVar[Exchange | None] = contextvars.ContextVar(
    "aims_exchange", default=None
)


def encode_chunks(chunks: Sequence[tuple[int, bytes]]) -> bytes:
    lines = []
    for ms, chunk in chunks:
        try:
            line = {"ms": ms, "text": chunk.decode()}
        except UnicodeDecodeError:
            line = {"ms": ms, "b64": base64.b64encode(chunk).decode()}
        lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    return "".join(lines).encode()


def decode_chunks(data: bytes) -> list[tuple[int, bytes]]:
    out = []
    for line in data.decode().splitlines():
        item = json.loads(line)
        chunk = item["text"].encode() if "text" in item else base64.b64decode(item["b64"])
        out.append((int(item["ms"]), chunk))
    return out


class Recorder(httpx2.AsyncBaseTransport):
    """Sends through `inner` and records the exchange of the attempt the caller set in
    `current`. Asks for an identity encoding so recorded bytes read as sent. A request carries
    only the SDK's own headers, those named in `headers` and `key` as its bearer token."""

    def __init__(
        self, inner: httpx2.AsyncBaseTransport, *, key: str = "none", headers: Iterable[str] = ()
    ) -> None:
        self.inner = inner
        self.authorization = f"Bearer {key}"
        self.allowed = SDK_HEADERS | {name.lower() for name in headers}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        exchange = current.get()
        if exchange is None:
            raise RuntimeError("model requests must go through ModelClient")
        for name in {name.lower() for name in request.headers}:
            if name not in self.allowed and not name.startswith("x-stainless-"):
                del request.headers[name]
        request.headers["authorization"] = self.authorization
        request.headers["accept-encoding"] = "identity"
        exchange.sent = time.monotonic()
        response = await self.inner.handle_async_request(request)
        exchange.status = response.status_code
        exchange.headers = {
            k.lower(): v for k, v in response.headers.items() if k.lower() not in UNRECORDED_HEADERS
        }
        stream = response.stream
        assert isinstance(stream, httpx2.AsyncByteStream)
        response.stream = _Recording(stream, exchange)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class _Recording(httpx2.AsyncByteStream):
    """Single pass: the SDK drains a stream again after `[DONE]`, which must not record or
    serve the bytes twice."""

    def __init__(self, inner: httpx2.AsyncByteStream, exchange: Exchange) -> None:
        self.inner = inner
        self.exchange = exchange
        self._iter: AsyncIterator[bytes] | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iter is None:
            self._iter = self._record()
        return self._iter

    async def _record(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            if chunk:
                self.exchange.chunks.append((self.exchange.ms(), chunk))
            yield chunk
        self.exchange.eof = True

    async def aclose(self) -> None:
        await self.inner.aclose()
