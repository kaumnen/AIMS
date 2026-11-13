import asyncio
import contextlib
from collections.abc import Callable, Generator
from typing import Any

import aiodocker
import aiohttp
from aiodocker.containers import DockerContainer
from aiodocker.stream import Stream
from aiohttp import BytesPayload
from aiohttp.abc import AbstractStreamWriter

GRACE_S = 5


@contextlib.contextmanager
def streamed(what: str) -> Generator[None]:
    """Raise a DockerStreamError when the Docker daemon cuts `what`, a stream it sends, as one
    that dies or restarts does. aiodocker turns the errors of a call the daemon drops into its
    own, but lets aiohttp's and the socket's through once the stream is under way."""
    try:
        yield
    except TimeoutError:
        # aiohttp's read timeouts are TimeoutErrors, which callers tell from a failure.
        raise
    except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError, OSError) as e:
        raise aiodocker.DockerStreamError(f"{what} was cut: {e}") from e


async def open_stream(stream: Stream, on_send: Callable[[], None] | None = None) -> Stream:
    """Open `stream`, an exec start or a container attach not yet sent. One that fails or is
    cancelled part way is closed before it raises, so its connection does not stay open. One
    the daemon cuts as it opens raises a DockerStreamError.

    `on_send` is called just before aiohttp writes the request body. The daemon reads the
    whole body before it starts an exec, so until then the exec cannot have started."""
    if on_send is not None:
        setup = stream._setup

        async def marked() -> tuple[Any, Any, bool]:
            url, body, tty = await setup()
            if not body:
                on_send()
                return url, body, tty
            return url, _Marked(body, on_send), tty

        stream._setup = marked
    try:
        with streamed("a new exec or attach stream"):
            return await stream.__aenter__()
    except BaseException:
        _drop(stream)
        raise


class _Marked(BytesPayload):
    """A request body that calls `on_write` before any of it is written."""

    def __init__(self, value: bytes, on_write: Callable[[], None]) -> None:
        super().__init__(value)
        self._on_write = on_write

    async def write(self, writer: AbstractStreamWriter) -> None:
        self._on_write()
        await super().write(writer)

    async def write_with_length(
        self, writer: AbstractStreamWriter, content_length: int | None
    ) -> None:
        self._on_write()
        await super().write_with_length(writer, content_length)


def _drop(stream: Stream) -> None:
    """Close what a failed open left. `Stream.close` asserts a connection, which a response
    whose socket the daemon closed no longer has."""
    response = stream._resp
    stream._closed = True
    if response is not None:
        response.close()


async def exec_output(container: DockerContainer, argv: list[str], *, timeout_s: int) -> bytes:
    """Run `argv` in `container` and return what it wrote to stdout.

    The command runs under the container's `timeout`, so it is killed when the wait gives up.
    """
    command = ["timeout", "-s", "KILL", str(timeout_s), *argv]
    async with asyncio.timeout(timeout_s + GRACE_S):
        run = await container.exec(command, stdout=True, stderr=True)
        stream = await open_stream(run.start(detach=False))
        out = bytearray()
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 1:
                    out += message.data
        finally:
            await stream.close()
        return bytes(out)
