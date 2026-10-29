"""The runner's side of `/runner/v1`."""

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from compression import zstd
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from pydantic import TypeAdapter

from aims import __version__
from aims.events import Label
from aims.run.job import Job
from aims.wire import (
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


async def _check(response: httpx2.Response) -> httpx2.Response:
    if response.status_code < 400:
        return response
    await response.aread()
    try:
        problem = response.json()
        if not isinstance(problem, dict):
            problem = {"detail": str(problem)}
    except ValueError:
        problem = {"detail": response.text[:500]}
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
            transport=transport,
            verify=verify,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: Any = None) -> httpx2.Response:
        content = None if body is None else body.model_dump_json().encode()
        headers = {"Content-Type": "application/json"} if content is not None else {}
        return await _check(await self._http.post(path, content=content, headers=headers))

    async def hello(self, hello: RunnerHello) -> HelloReply:
        return HelloReply.model_validate_json((await self._post("/hello", hello)).content)

    async def status(self, status: RunnerStatus) -> None:
        await self._post("/status", status)

    @asynccontextmanager
    async def stream(self, last_id: str | None = None) -> AsyncGenerator[httpx2.EventSource]:
        headers = {} if last_id is None else {"Last-Event-ID": last_id}
        timeout = httpx2.Timeout(10.0, read=60.0)
        async with self._http.sse("/stream", headers=headers, timeout=timeout) as source:
            await _check(source.response)
            yield source

    async def ack(self, command_id: str, ack: Ack) -> None:
        await self._post(f"/commands/{command_id}/ack", ack)

    async def claim(self, run_id: str) -> Job | MonitorJob | ScoreJob | None:
        """The job offered as `run_id`, a scenario or models.check Job, a MonitorJob or a
        ScoreJob; None when it went to another runner."""
        response = await self._http.post(f"/jobs/{run_id}/claim")
        if response.status_code == 409:
            return None
        await _check(response)
        return Claimed.model_validate_json(response.content).job

    async def release(self, run_id: str, reason: str) -> None:
        await self._post(f"/jobs/{run_id}/release", Release(reason=reason))

    async def job_blob(self, run_id: str, sha256: str) -> bytes:
        """A blob of a job's scenario, as the control plane stores it."""
        response = await _check(await self._http.get(f"/jobs/{run_id}/blobs/{sha256}"))
        return response.content

    async def heads(self, run_ids: Sequence[str]) -> dict[str, Head]:
        response = await self._post("/heads", Heads(run_ids=list(run_ids)))
        return HeadsReply.model_validate_json(response.content).heads

    async def missing(self, run_id: str, shas: Sequence[str]) -> list[str]:
        response = await self._post(f"/runs/{run_id}/blobs/missing", Shas(shas=list(shas)))
        return list(response.json()["missing"])

    async def put_blob(self, run_id: str, sha256: str, path: Path) -> None:
        """Send a blob file as stored, without reading it into memory."""

        async def body() -> AsyncIterator[bytes]:
            with path.open("rb") as f:
                while chunk := f.read(FILE_CHUNK):
                    yield chunk

        response = await self._http.put(
            f"/runs/{run_id}/blobs/{sha256}",
            content=body(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx2.Timeout(300.0, connect=10.0),
        )
        await _check(response)

    async def put_truth(self, run_id: str, data: bytes) -> None:
        response = await self._http.put(
            f"/runs/{run_id}/truth",
            content=data,
            headers={"Content-Type": "application/json"},
            timeout=httpx2.Timeout(60.0, connect=10.0),
        )
        await _check(response)

    async def post_events(self, run_id: str, lines: Sequence[bytes]) -> Head:
        body = zstd.compress(b"".join(lines))
        response = await self._http.post(
            f"/runs/{run_id}/events",
            content=body,
            headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "zstd"},
            timeout=httpx2.Timeout(120.0, connect=10.0),
        )
        await _check(response)
        return Head.model_validate(response.json()["head"])

    async def post_deltas(self, deltas: Deltas) -> None:
        await self._post("/deltas", deltas)

    async def run_log(self, run_id: str) -> bytes:
        """The exact log of a run another runner wrote, to fork it here."""
        response = await self._http.get(
            f"/runs/{run_id}/log", timeout=httpx2.Timeout(300.0, connect=10.0)
        )
        return (await _check(response)).content

    async def run_truth(self, run_id: str) -> bytes:
        """The truth file of a sealed run, for a score job of it."""
        return (await _check(await self._http.get(f"/runs/{run_id}/truth"))).content

    async def post_labels(self, run_id: str, labels: Sequence[Label]) -> None:
        await self._post(f"/runs/{run_id}/labels", Labels(labels=list(labels)))

    async def run_blob(self, run_id: str, sha256: str) -> bytes:
        response = await _check(await self._http.get(f"/runs/{run_id}/blobs/{sha256}"))
        return response.content
