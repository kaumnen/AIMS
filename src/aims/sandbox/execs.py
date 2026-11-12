import asyncio

from aiodocker.containers import DockerContainer
from aiodocker.stream import Stream

GRACE_S = 5


async def open_stream(stream: Stream) -> Stream:
    """Open `stream`, an exec start or a container attach not yet sent. One that fails or is
    cancelled part way is closed before it raises, so its connection does not stay open."""
    try:
        return await stream.__aenter__()
    except BaseException:
        _drop(stream)
        raise


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
