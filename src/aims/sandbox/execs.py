import asyncio

from aiodocker.containers import DockerContainer


async def exec_output(container: DockerContainer, argv: list[str], *, timeout_s: float) -> bytes:
    """Run `argv` in `container` and return what it wrote to stdout."""
    async with asyncio.timeout(timeout_s):
        run = await container.exec(argv, stdout=True, stderr=True)
        stream = await run.start(detach=False).__aenter__()
        out = b""
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 1:
                    out += message.data
        finally:
            await stream.close()
        return out
