import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest
from children import create_child

pytestmark = pytest.mark.anyio
HERE = Path(__file__).parent

# A stand-in for a test process. It starts a child that would run for a minute and says its
# pid. Then it ends as pytest-timeout ends a hung worker, or waits to be killed.
PARENT = """
import asyncio, os, sys
from children import create_child

async def main():
    child = await create_child(sys.executable, "-c", "import time; time.sleep(60)")
    print(child.pid, flush=True)
    if sys.argv[1] == "exit":
        os._exit(1)
    await asyncio.sleep(60)

asyncio.run(main())
"""

# A stand-in whose child ends at once. It then waits until it has no children left.
ALONE = """
import asyncio, os, sys, time
from children import create_child

async def main():
    child = await create_child(sys.executable, "-c", "pass")
    await child.wait()
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            print("alone")
            return
        await asyncio.sleep(0.02)
    print("not alone")

asyncio.run(main())
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("end", ["exit", "kill"])
async def test_a_child_ends_soon_after_the_test_process_dies(end: str) -> None:
    parent = await create_child(
        sys.executable, "-c", PARENT, end, stdout=asyncio.subprocess.PIPE, cwd=HERE
    )
    child = 0
    try:
        assert parent.stdout is not None
        async with asyncio.timeout(30):
            child = int(await parent.stdout.readline())
        if end == "kill":
            assert alive(child)
            parent.kill()
        await parent.wait()
        # init or launchd takes the child over and reaps it once it ends
        async with asyncio.timeout(5):
            while alive(child):  # noqa: ASYNC110
                await asyncio.sleep(0.02)
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if child:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


async def test_a_watchdog_ends_with_its_child() -> None:
    parent = await create_child(
        sys.executable, "-c", ALONE, stdout=asyncio.subprocess.PIPE, cwd=HERE
    )
    async with asyncio.timeout(30):
        out, _ = await parent.communicate()
    assert out.decode().strip() == "alone"
