"""The runner's side of `/runner/v1`."""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from compression import zstd
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from aims import __version__
from aims.events import Label
from aims.events.blobs import compress_bound
from aims.net import Outbound
from aims.run.job import INPUT_LIMIT, Job
from aims.wire import (
    TRUTH_LIMIT,
    Ack,
    Claimed,
    Deltas,
    Head,
    Heads,
    HeadsReply,
    HelloReply,
    Labels,
    MonitorJob,
    Release,
    RunnerFeed,
    RunnerHello,
    RunnerStatus,
    ScoreJob,
    Shas,
)

PREFIX = "/runner/v1"
FEED: TypeAdapter[Any] = TypeAdapter(RunnerFeed)
FILE_CHUNK = 256 * 1024
# The most of each kind of reply a runner reads into memory: it does not fully trust its
# control plane. Each is sized from the largest reply an honest one sends.
INPUT_BYTES = compress_bound(INPUT_LIMIT)
"""A job input: the largest file of a blob of INPUT_LIMIT bytes."""
REPLY_LIMIT = 16 * 1024 * 1024
"""A JSON reply or an error's body. The largest honest ones: a claimed job, most of which its
run.started event records in one log line of at most 256 KiB; the heads of 10,000 runs, about
1.5 MB; the missing blobs of one event batch, about 4 MiB at most."""
FEED_EVENT = 16 * 1024 * 1024
"""One event of the feed: a command whose inject text is as long as an API body may be,
8 MiB."""


class BadReply(ValueError):
    """A reply of the control plane that does not load: past the bytes the runner reads of it,
    not JSON or of another shape. An error of the local side is never one."""


class TooLarge(BadReply):
    """A reply past the bytes the runner reads of it. A caller that retries a reply it cannot
    read, such as one that is not JSON, retries this one too."""


class _Missing(BaseModel):
    missing: list[str]


class _Posted(BaseModel):
    head: Head


def _load[M: BaseModel](model: type[M], body: bytes) -> M:
    """`body` as `model`. Raises BadReply when it is not JSON or of another shape."""
    try:
        return model.model_validate_json(body)
    except ValidationError as e:
        raise BadReply(str(e)) from e


class ApiError(Exception):
    """A reply the control plane sent as problem+json, or another HTTP error."""

    def __init__(self, status: int, problem: dict[str, Any], retry_after: float | None = None):
        title = problem.get("title") or f"HTTP {status}"
        detail = problem.get("detail")
        super().__init__(f"{status} {title}{': ' + detail if detail else ''}")
        self.status = status
        self.problem = problem
        self.retry_after = retry_after

    @property
    def head(self) -> Head | None:
        value = self.problem.get("head")
        return None if value is None else Head.model_validate(value)

    @property
    def missing(self) -> list[str]:
        return list(self.problem.get("missing") or [])

    @property
    def code(self) -> str | None:
        return self.problem.get("code")

    @property
    def retryable(self) -> bool:
        return self.status in (408, 425, 429) or self.status >= 500


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def _read(response: httpx2.Response, limit: int) -> bytes:
    """The body of `response`, read a chunk at a time. Raises TooLarge once it passes `limit`
    bytes, of which no more than a chunk past it is read."""
    out = bytearray()
    async for chunk in response.aiter_bytes(FILE_CHUNK):
        out += chunk
        if len(out) > limit:
            request = response.request
            raise TooLarge(f"the reply to {request.method} {request.url.path} passes {limit} bytes")
    return bytes(out)


async def _check(response: httpx2.Response) -> httpx2.Response:
    if response.status_code < 400:
        return response
    try:
        body = await _read(response, REPLY_LIMIT)
    except TooLarge as e:
        raise ApiError(response.status_code, {"detail": str(e)}, _retry_after(response)) from None
    try:
        problem = json.loads(body)
        if not isinstance(problem, dict):
            problem = {"detail": str(problem)}
    except ValueError:
        problem = {"detail": body[:2000].decode(errors="replace")[:500]}
    raise ApiError(response.status_code, problem, _retry_after(response))


class ControlClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        runner: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        verify: bool | str = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.runner = runner
        self._http = httpx2.AsyncClient(
            base_url=self.url + PREFIX,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Aims-Runner": runner,
                "User-Agent": f"aims/{__version__}",
            },
            timeout=httpx2.Timeout(30.0, connect=10.0),
            transport=transport or Outbound(verify),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, limit: int | None = None, **kw: Any
    ) -> bytes:
        """Send a request and return the body of its reply, read no further than `limit` bytes,
        REPLY_LIMIT by default; see `_read`. An error status raises ApiError."""
        async with self._http.stream(method, path, **kw) as response:
            await _check(response)
            return await _read(response, REPLY_LIMIT if limit is None else limit)

    async def _post(self, path: str, body: Any = None) -> bytes:
        content = None if body is None else body.model_dump_json().encode()
        headers = {"Content-Type": "application/json"} if content is not None else {}
        return await self._request("POST", path, content=content, headers=headers)

    async def hello(self, hello: RunnerHello) -> HelloReply:
        return _load(HelloReply, await self._post("/hello", hello))

    async def status(self, status: RunnerStatus) -> None:
        await self._post("/status", status)

    @asynccontextmanager
    async def stream(self, last_id: str | None = None) -> AsyncGenerator[httpx2.EventSource]:
        headers = {} if last_id is None else {"Last-Event-ID": last_id}
        timeout = httpx2.Timeout(10.0, read=60.0)
        async with self._http.sse(
            "/stream", headers=headers, timeout=timeout, max_event_size=FEED_EVENT
        ) as source:
            await _check(source.response)
            yield source

    async def ack(self, command_id: str, ack: Ack) -> None:
        await self._post(f"/commands/{command_id}/ack", ack)

    async def claim(self, run_id: str) -> Job | MonitorJob | ScoreJob | None:
        """The job offered as `run_id`, a scenario or models.check Job, a MonitorJob or a
        ScoreJob; None when it went to another runner."""
        try:
            body = await self._request("POST", f"/jobs/{run_id}/claim")
        except ApiError as e:
            if e.status == 409:
                return None
            raise
        return _load(Claimed, body).job

    async def release(self, run_id: str, reason: str) -> None:
        await self._post(f"/jobs/{run_id}/release", Release(reason=reason))

    async def job_blob(self, run_id: str, sha256: str, limit: int = INPUT_BYTES) -> bytes:
        """A blob of a job's scenario, as the control plane stores it. Raises TooLarge once the
        reply passes `limit` bytes, of which no more than a chunk past it is read."""
        return await self._request("GET", f"/jobs/{run_id}/blobs/{sha256}", limit=limit)

    async def heads(self, run_ids: Sequence[str]) -> dict[str, Head]:
        body = await self._post("/heads", Heads(run_ids=list(run_ids)))
        return _load(HeadsReply, body).heads

    async def missing(self, run_id: str, shas: Sequence[str]) -> list[str]:
        body = await self._post(f"/runs/{run_id}/blobs/missing", Shas(shas=list(shas)))
        return _load(_Missing, body).missing

    async def put_blob(self, run_id: str, sha256: str, path: Path) -> None:
        """Send a blob file as stored, without reading it into memory."""

        async def body() -> AsyncIterator[bytes]:
            with path.open("rb") as f:
                while chunk := f.read(FILE_CHUNK):
                    yield chunk

        await self._request(
            "PUT",
            f"/runs/{run_id}/blobs/{sha256}",
            content=body(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx2.Timeout(300.0, connect=10.0),
        )

    async def put_truth(self, run_id: str, data: bytes) -> None:
        await self._request(
            "PUT",
            f"/runs/{run_id}/truth",
            content=data,
            headers={"Content-Type": "application/json"},
            timeout=httpx2.Timeout(60.0, connect=10.0),
        )

    async def post_events(self, run_id: str, lines: Sequence[bytes]) -> Head:
        body = zstd.compress(b"".join(lines))
        reply = await self._request(
            "POST",
            f"/runs/{run_id}/events",
            content=body,
            headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "zstd"},
            timeout=httpx2.Timeout(120.0, connect=10.0),
        )
        return _load(_Posted, reply).head

    async def post_deltas(self, deltas: Deltas) -> None:
        await self._post("/deltas", deltas)

    @asynccontextmanager
    async def run_log(self, run_id: str) -> AsyncGenerator[AsyncIterator[bytes]]:
        """The exact log of a run another runner wrote, to fork it here, in chunks as it
        arrives."""
        timeout = httpx2.Timeout(300.0, connect=10.0)
        async with self._http.stream("GET", f"/runs/{run_id}/log", timeout=timeout) as response:
            yield (await _check(response)).aiter_bytes(FILE_CHUNK)

    async def run_truth(self, run_id: str) -> bytes:
        """The truth file of a sealed run, for a score job of it."""
        return await self._request("GET", f"/runs/{run_id}/truth", limit=TRUTH_LIMIT)

    async def post_labels(self, run_id: str, labels: Sequence[Label]) -> None:
        await self._post(f"/runs/{run_id}/labels", Labels(labels=list(labels)))

    @asynccontextmanager
    async def run_blob(self, run_id: str, sha256: str) -> AsyncGenerator[AsyncIterator[bytes]]:
        """A blob file of a run another runner wrote, as stored, in chunks as it arrives."""
        async with self._http.stream("GET", f"/runs/{run_id}/blobs/{sha256}") as response:
            yield (await _check(response)).aiter_bytes(FILE_CHUNK)
