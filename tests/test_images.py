from typing import Any

import aiodocker
import pytest

from aims.sandbox.images import ensure_built, ensure_image

pytestmark = pytest.mark.anyio


class Images:
    """Docker's image API, with another builder that finishes first."""

    def __init__(self, *, rival: bool) -> None:
        self.rival = rival
        self.tags: set[str] = set()
        self.builds = 0

    async def inspect(self, tag: str) -> dict[str, Any]:
        if tag not in self.tags:
            raise aiodocker.DockerError(404, "no such image")
        return {}

    async def build(self, *, tag: str, **_: Any) -> None:
        self.builds += 1
        if self.rival:
            self.tags.add(tag)
            raise aiodocker.DockerError(500, f"image {tag}: already exists")


class Docker:
    def __init__(self, *, rival: bool) -> None:
        self.images = Images(rival=rival)


async def test_a_build_lost_to_a_rival_builder_uses_its_image() -> None:
    docker: Any = Docker(rival=True)
    tag = await ensure_image(docker, "tracer")
    assert tag.startswith("aims-tracer:")
    assert await ensure_built(docker, b"context") in docker.images.tags
    assert docker.images.builds == 2


async def test_a_failed_build_with_no_image_still_raises() -> None:
    docker: Any = Docker(rival=False)

    async def broken(**_: Any) -> None:
        raise aiodocker.DockerError(500, "no space left")

    docker.images.build = broken
    with pytest.raises(aiodocker.DockerError):
        await ensure_image(docker, "tracer")
