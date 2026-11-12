"""Container create and delete calls a cancel cannot cut short, against a fake daemon."""

import asyncio
from typing import Any, cast

import aiodocker
import pytest

from aims.sandbox.containers import create_container

pytestmark = pytest.mark.anyio


class Container:
    def __init__(self, daemon: Daemon, id: str) -> None:
        self.daemon = daemon
        self.id = id

    async def delete(self, *, force: bool = False) -> None:
        self.daemon.asked.set()
        await self.daemon.answer.wait()
        if self.daemon.lost:
            raise ConnectionResetError("the daemon went away")
        self.daemon.made.remove(self.id)


class Daemon:
    """Makes a container once `answer` is set, whatever the caller does meanwhile."""

    def __init__(self, *, refuse: bool = False, lost: bool = False) -> None:
        self.refuse = refuse
        self.lost = lost
        self.asked = asyncio.Event()
        self.answer = asyncio.Event()
        self.made: list[str] = []
        self.containers = self

    async def create(self, *, config: dict[str, Any], name: str | None = None) -> Container:
        self.asked.set()
        await self.answer.wait()
        if self.refuse:
            raise aiodocker.DockerError(409, f"name {name} is in use")
        self.made.append(name or "helper")
        return Container(self, name or "helper")


async def cancelled_twice(task: asyncio.Task[Any], daemon: Daemon) -> None:
    """Cancel `task` twice while the daemon has not answered; it must still be running."""
    await daemon.asked.wait()
    daemon.asked.clear()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0.01)
    assert not task.done()


async def test_a_cancelled_create_deletes_the_container_the_daemon_made() -> None:
    daemon = Daemon()
    docker = cast(aiodocker.Docker, daemon)
    task = asyncio.create_task(create_container(docker, config={}, name="box"))
    await cancelled_twice(task, daemon)
    daemon.answer.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert daemon.made == []


async def test_a_cancelled_create_the_daemon_refused_deletes_nothing() -> None:
    daemon = Daemon(refuse=True)
    task = asyncio.create_task(create_container(cast(aiodocker.Docker, daemon), config={}))
    await cancelled_twice(task, daemon)
    daemon.answer.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not daemon.asked.is_set()


async def test_a_cancelled_create_stays_cancelled_when_its_delete_fails() -> None:
    daemon = Daemon(lost=True)
    task = asyncio.create_task(create_container(cast(aiodocker.Docker, daemon), config={}))
    await cancelled_twice(task, daemon)
    daemon.answer.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert daemon.made == ["helper"]
