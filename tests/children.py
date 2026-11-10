"""Processes a test starts, killed once the test process ends, however it ends. pytest-timeout
ends a hung worker with os._exit, so no teardown runs then."""

import asyncio
import os
import subprocess
import sys
import threading
from typing import Any

# Waits for one of two pipes to end: stdin, which the test process holds, or the child's own.
# It kills only while the child still holds its pipe, so the pid is still the child's.
WATCHDOG = """
import os, select, signal, sys
pid, child = int(sys.argv[1]), int(sys.argv[2])
if child not in select.select([0, child], [], [])[0]:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
"""


async def create_child(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """asyncio.create_subprocess_exec with a watchdog that kills the child once this process
    ends. The watchdog ends with the child."""
    held, given = os.pipe()
    life, lifeline = os.pipe()
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, pass_fds=(*kwargs.pop("pass_fds", ()), given), **kwargs
            )
        finally:
            os.close(given)
        # asyncio starts its children with Popen on the loop too. A session of its own keeps
        # a Ctrl-C in the terminal from ending the watchdog first.
        watchdog = subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-I", "-c", WATCHDOG, str(proc.pid), str(held)],
            stdin=life,
            pass_fds=(held,),
            start_new_session=True,
        )
    except BaseException:
        os.close(lifeline)
        if proc is not None:
            proc.kill()
        raise
    finally:
        os.close(held)
        os.close(life)

    def reap() -> None:
        watchdog.wait()
        os.close(lifeline)

    threading.Thread(target=reap, daemon=True).start()
    return proc
