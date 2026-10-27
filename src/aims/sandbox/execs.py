import asyncio

from aiodocker.containers import DockerContainer

GRACE_S = 5


async def exec_output(container: DockerContainer, argv: list[str], *, timeout_s: int) -> bytes:
    """Run `argv` in `container` and return what it wrote to stdout.

    The command runs under the container's `timeout`, so it is killed when the wait gives up.
    """
    command = ["timeout", "-s", "KILL", str(timeout_s), *argv]
    async with asyncio.timeout(timeout_s + GRACE_S):
        run = await container.exec(command, stdout=True, stderr=True)
        stream = await run.start(detach=False).__aenter__()
        out = bytearray()
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 1:
                    out += message.data
        finally:
            await stream.close()
        return bytes(out)
