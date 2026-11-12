"""Container create and delete calls that a cancel cannot cut short."""

import asyncio
import contextlib
import logging
from typing import Any

import aiodocker
from aiodocker.containers import DockerContainer

from aims.aio import settle, shielded

log = logging.getLogger("aims.sandbox")

# A cancelled create says why it still waits once the daemon has not answered for this long.
SLOW_S = 10.0


async def create_container(
    docker: aiodocker.Docker, *, config: dict[str, Any], name: str | None = None
) -> DockerContainer:
    """Create a container. The daemon makes it even when the call is cut short, so a cancel
    waits for the daemon's answer and deletes the container it made before it raises.

    The wait has no bound, like the other cleanup waits here. With a bound, a late container
    could land after the caller's cleanup. Docker would then make its named volumes again,
    without labels."""
    call = asyncio.ensure_future(docker.containers.create(config=config, name=name))
    try:
        return await shielded(call)
    except asyncio.CancelledError:
        what = name or "a helper"
        try:
            await settle(_discard(call, what))
        except Exception as e:
            log.warning("could not remove %s after a cancelled create: %s", what, e)
        raise


async def _delete(container: DockerContainer) -> None:
    with contextlib.suppress(aiodocker.DockerError):
        await container.delete(force=True)


async def _discard(call: asyncio.Future[DockerContainer], what: str) -> None:
    """Wait for the daemon to answer a cancelled create, then delete what it made."""
    done, _ = await asyncio.wait((call,), timeout=SLOW_S)
    if not done:
        log.warning("a cancel waits for Docker to answer the create of %s", what)
        await asyncio.wait((call,))
    if call.cancelled() or call.exception() is not None:
        return
    await _delete(call.result())
