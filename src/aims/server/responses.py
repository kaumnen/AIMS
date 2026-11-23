"""Responses shared by the routers."""

from collections.abc import AsyncGenerator
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that closes its body when the response ends. When a client goes
    while a send waits, Starlette leaves the body suspended at its yield. A file it reads would
    then stay open until the GC finds it."""

    def __init__(self, content: AsyncGenerator[bytes | str], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._content.aclose()
