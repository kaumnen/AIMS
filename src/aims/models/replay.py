import asyncio
import hashlib
import json
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any, Literal

import httpx2
import rfc8785

from aims.events import EventStore, ModelError, ModelRequest, ModelResponse
from aims.models.transport import current, decode_chunks

Pace = Literal["instant", "recorded"]
_DROP = frozenset({"content-length", "transfer-encoding", "connection"})


class ReplayError(Exception):
    pass


class ReplayMiss(ReplayError):
    """No recorded answer for a request. `path` is the first place it differs from the
    nearest recorded request."""

    def __init__(self, key: str, path: str | None, nearest: str | None, detail: str) -> None:
        super().__init__(detail)
        self.key = key
        self.path = path
        self.nearest = nearest


class ReplayWall(ReplayError):
    """The recorded attempt ran into its wall timeout here."""


@dataclass(frozen=True, slots=True)
class Recorded:
    run_id: str
    seq: int
    status: str
    http_status: int | None
    headers: dict[str, str]
    chunks: list[tuple[int, bytes]]
    latency_ms: int
    error: str | None
    error_kind: ModelError | None


def canonical_key(body: bytes) -> str:
    return hashlib.sha256(rfc8785.dumps(json.loads(body))).hexdigest()


class Recording:
    """The answers a run got, in order, keyed by the canonical request body."""

    def __init__(self, store: EventStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self.entries: dict[str, deque[Recorded]] = {}
        self.served: dict[str, int] = {}
        self.bodies: dict[str, str] = {}

    @classmethod
    async def load(cls, store: EventStore, run_id: str) -> Recording:
        rec = cls(store, run_id)
        requests: dict[tuple[str, int], ModelRequest] = {}
        cursor: int | None = None
        types = ("model.request", "model.response")
        while page := await store.events(run_id, after=cursor, limit=1000, types=types):
            for event in page:
                cursor = event.seq
                payload = event.payload()
                if isinstance(payload, ModelRequest):
                    requests[payload.call_id, payload.attempt] = payload
                elif isinstance(payload, ModelResponse):
                    request = requests.pop((payload.call_id, payload.attempt), None)
                    if request is None:
                        continue
                    entry = await rec._entry(event.seq, payload)
                    rec.entries.setdefault(request.key, deque()).append(entry)
                    rec.bodies[request.key] = request.body.sha256
        return rec

    async def _entry(self, seq: int, r: ModelResponse) -> Recorded:
        if r.stream is not None:
            chunks = decode_chunks(await self.store.get_blob(self.run_id, r.stream.sha256))
        elif r.body is not None and r.http_status is not None:
            chunks = [(r.latency_ms, await self.store.get_blob(self.run_id, r.body.sha256))]
        else:
            chunks = []
        return Recorded(
            run_id=self.run_id,
            seq=seq,
            status=r.status,
            http_status=r.http_status,
            headers=r.headers,
            chunks=chunks,
            latency_ms=r.latency_ms,
            error=r.error,
            error_kind=r.error_kind,
        )

    def remaining(self) -> int:
        return sum(len(q) for q in self.entries.values())

    async def take(self, body: bytes) -> Recorded:
        key = canonical_key(body)
        queue = self.entries.get(key)
        if queue:
            self.served[key] = self.served.get(key, 0) + 1
            return queue.popleft()
        if key in self.bodies:
            n = self.served.get(key, 0)
            detail = f"request {key[:12]} was recorded {n} times and asked again"
            raise ReplayMiss(key, None, key, detail)
        nearest, path = await self._nearest(body)
        where = f"; first difference at {path}" if path else ""
        raise ReplayMiss(key, path, nearest, f"request {key[:12]} was not recorded{where}")

    async def _nearest(self, body: bytes) -> tuple[str | None, str | None]:
        if not self.bodies:
            return None, None
        canonical = rfc8785.dumps(json.loads(body))
        best: tuple[int, str, bytes] | None = None
        for key, sha in self.bodies.items():
            other = await self.store.get_blob(self.run_id, sha)
            common = _common_prefix(canonical, other)
            if best is None or common > best[0]:
                best = (common, key, other)
        assert best is not None
        return best[1], first_difference(json.loads(body), json.loads(best[2]))


def _common_prefix(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    """The JSON path of the first place `a` and `b` differ, keys in sorted order."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(a.keys() | b.keys()):
            if key not in a or key not in b:
                return f"{path}.{key}"
            if (found := first_difference(a[key], b[key], f"{path}.{key}")) is not None:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            if (found := first_difference(x, y, f"{path}[{i}]")) is not None:
                return found
        return None if len(a) == len(b) else f"{path}[{min(len(a), len(b))}]"
    return None if type(a) is type(b) and a == b else path


class Replayer(httpx2.AsyncBaseTransport):
    """Serves recorded answers. A request with no answer left fails with ReplayMiss."""

    def __init__(self, recording: Recording, pace: Pace = "instant") -> None:
        self.recording = recording
        self.pace = pace

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        rec = await self.recording.take(await request.aread())
        exchange = current.get()
        if exchange is not None:
            exchange.origin = (rec.run_id, rec.seq)
        if rec.http_status is None:
            if self.pace == "recorded":
                await asyncio.sleep(rec.latency_ms / 1000)
            raise _failure(rec, request)
        headers = [(k, v) for k, v in rec.headers.items() if k not in _DROP]
        return httpx2.Response(
            rec.http_status, headers=headers, stream=_Served(rec, self.pace, request)
        )


class _Served(httpx2.AsyncByteStream):
    def __init__(self, rec: Recorded, pace: Pace, request: httpx2.Request) -> None:
        self.rec = rec
        self.pace = pace
        self.request = request
        self._iter: AsyncIterator[bytes] | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iter is None:
            self._iter = self._serve()
        return self._iter

    async def _serve(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for ms, chunk in self.rec.chunks:
            if self.pace == "recorded":
                await asyncio.sleep(max(0.0, start + ms / 1000 - loop.time()))
            yield chunk
        if self.rec.error_kind in _CUT or self.rec.status == "cancelled":
            raise _failure(self.rec, self.request)


_CUT: frozenset[str | None] = frozenset({"stall", "read", "protocol", "wall", "timeout"})


def _failure(rec: Recorded, request: httpx2.Request) -> Exception:
    message = rec.error or "recorded failure"
    match rec.error_kind:
        case "connect":
            return httpx2.ConnectError(message, request=request)
        case "timeout":
            return httpx2.ConnectTimeout(message, request=request)
        case "stall":
            return httpx2.ReadTimeout(message, request=request)
        case "read":
            return httpx2.ReadError(message, request=request)
        case "protocol":
            return httpx2.RemoteProtocolError(message, request=request)
        case "wall":
            return ReplayWall(message)
    if rec.status == "cancelled":
        return ReplayError(f"the recorded attempt (event {rec.seq}) was cancelled by its caller")
    return ReplayError(f"the recorded attempt (event {rec.seq}) failed with {message}")


def chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__
