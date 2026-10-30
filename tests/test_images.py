import asyncio
import uuid
from typing import Any

import aiodocker
import pytest

from aims.sandbox.images import ensure_built

pytestmark = pytest.mark.anyio


class Images:
    """Docker's image API, with builds that take a while."""

    def __init__(self) -> None:
        self.tags: set[str] = set()
        self.builds = 0

    async def inspect(self, tag: str) -> dict[str, Any]:
        if tag not in self.tags:
            raise aiodocker.DockerError(404, "no such image")
        return {}

    async def build(self, *, tag: str, **_: Any) -> None:
        self.builds += 1
        await asyncio.sleep(0.3)
        self.tags.add(tag)


class Docker:
    def __init__(self) -> None:
        self.images = Images()


async def test_builds_of_one_image_at_once_build_it_once() -> None:
    docker: Any = Docker()
    context = uuid.uuid4().bytes
    tags = await asyncio.gather(*(ensure_built(docker, context) for _ in range(3)))
    assert len(set(tags)) == 1
    assert tags[0] in docker.images.tags
    assert docker.images.builds == 1


async def test_a_failed_build_raises() -> None:
    docker: Any = Docker()

    async def broken(**_: Any) -> None:
        raise aiodocker.DockerError(500, "no space left")

    docker.images.build = broken
    with pytest.raises(aiodocker.DockerError):
        await ensure_built(docker, uuid.uuid4().bytes)
