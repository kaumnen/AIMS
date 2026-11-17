"""The control plane's API as the MCP server calls it. A problem it answers becomes a tool
error that says what the API said."""

import asyncio
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from aims import __version__
from aims.net import Outbound

# A 503 with Retry-After asks again while the index catches up with a run's log.
RETRIES = 5
RETRY_MAX_S = 5.0


def seg(value: str) -> str:
    """`value` as one path segment."""
    return quote(value, safe="")


def problem_text(r: httpx2.Response) -> str:
    """The status, title and detail of a problem, then each of its errors on a line."""
    try:
        problem = r.json()
    except ValueError:
        return f"{r.status_code} {r.text[:500]}"
    if not isinstance(problem, dict):
        return f"{r.status_code} {r.text[:500]}"
    head = " ".join(str(problem.get(k)) for k in ("status", "title") if problem.get(k))
    detail = problem.get("detail")
    lines = [f"{head}: {detail}" if detail else head or str(r.status_code)]
    for e in problem.get("errors") or []:
        if not isinstance(e, dict):
            continue
        where = ".".join(str(x) for x in e.get("loc") or [])
        if e.get("line") is not None:
            where = f"line {e['line']}"
        lines.append(f"- {where}: {e.get('msg')}" if where else f"- {e.get('msg')}")
    return "\n".join(lines)


class Problem(ToolError):
    """An answer of the API that is not a success, kept for the caller to read."""

    def __init__(self, r: httpx2.Response) -> None:
        super().__init__(problem_text(r))
        self.status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = None
        self.code = body.get("code") if isinstance(body, dict) else None


class Api:
    """One client of the control plane at `url` for the life of the server."""

    def __init__(
        self, url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}", "User-Agent": f"aims/{__version__}"}
        self._client = httpx2.AsyncClient(
            base_url=self.url,
            headers=headers,
            timeout=60,
            transport=transport or Outbound(),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> httpx2.Response:
        """The answer to `method` on `/api{path}`. Raises Problem for a problem."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        retries = 0
        while True:
            try:
                r = await self._client.request(
                    method, f"/api{path}", params=query, json=body if method != "GET" else None
                )
            except httpx2.TransportError as e:
                raise ToolError(f"cannot reach the control plane at {self.url}: {e}") from None
            wait = r.headers.get("retry-after")
            if r.status_code == 503 and wait is not None and retries < RETRIES:
                retries += 1
                try:
                    seconds = min(max(float(wait), 0.0), RETRY_MAX_S)
                except ValueError:
                    seconds = 1.0
                await asyncio.sleep(seconds)
                continue
            if r.status_code >= 400:
                raise Problem(r)
            return r

    async def get(self, path: str, **params: Any) -> Any:
        return (await self.send("GET", path, params=params)).json()

    async def text(self, path: str, **params: Any) -> str:
        return (await self.send("GET", path, params=params)).text

    async def post(self, path: str, body: Any) -> Any:
        return (await self.send("POST", path, body=body)).json()
