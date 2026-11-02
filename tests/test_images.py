import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiodocker
import pytest

from aims.sandbox import images
from aims.sandbox.images import context, ensure_built, ensure_image, ensure_pulled

pytestmark = pytest.mark.anyio

DEFAULT = {"AIMS_IMAGE_REGISTRY": "ghcr.io/kaumnen"}


class Images:
    """Docker's image API, with builds and pulls that take a while. `published` is what the
    registry holds."""

    def __init__(self) -> None:
        self.tags: set[str] = set()
        self.published: set[str] = set()
        self.builds = 0
        self.pulls: list[str] = []
        self.pull_error: aiodocker.DockerError | None = None

    async def inspect(self, tag: str) -> dict[str, Any]:
        if tag not in self.tags:
            raise aiodocker.DockerError(404, "no such image")
        return {"Id": "sha256:local", "RepoDigests": []}

    async def build(self, *, tag: str, **_: Any) -> None:
        self.builds += 1
        await asyncio.sleep(0.3)
        self.tags.add(tag)

    async def pull(self, ref: str, **_: Any) -> list[Any]:
        self.pulls.append(ref)
        await asyncio.sleep(0.3)
        if self.pull_error is not None:
            raise self.pull_error
        if ref not in self.published:
            raise aiodocker.DockerError(404, f"{ref}: not found")
        self.tags.add(ref)
        return []

    async def tag(self, name: str, repo: str, *, tag: str | None = None) -> bool:
        if name not in self.tags:
            raise aiodocker.DockerError(404, f"No such image: {name}")
        self.tags.add(f"{repo}:{tag}")
        return True


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
    assert docker.images.pulls == []


async def test_a_failed_build_raises() -> None:
    docker: Any = Docker()

    async def broken(**_: Any) -> None:
        raise aiodocker.DockerError(500, "no space left")

    docker.images.build = broken
    with pytest.raises(aiodocker.DockerError):
        await ensure_built(docker, uuid.uuid4().bytes)


async def test_a_published_asset_image_is_pulled_under_the_tag_a_build_would_get() -> None:
    docker: Any = Docker()
    tag = context("tracer")[0]
    docker.images.published.add(f"ghcr.io/kaumnen/{tag}")
    assert await ensure_image(docker, "tracer", env={}) == tag
    assert tag.startswith("aims-tracer:")
    assert docker.images.pulls == [f"ghcr.io/kaumnen/{tag}"]
    assert tag in docker.images.tags
    assert docker.images.builds == 0


@pytest.mark.parametrize(
    ("error", "level"),
    [
        (aiodocker.DockerError(404, "ghcr.io/kaumnen/aims-tracer: not found"), logging.INFO),
        (aiodocker.DockerError(500, "error from registry: denied\ndenied"), logging.WARNING),
        (aiodocker.DockerError(500, "dial tcp: connection refused"), logging.WARNING),
        (aiodocker.DockerStreamError("unexpected EOF"), logging.WARNING),
    ],
)
async def test_an_asset_image_the_registry_cannot_give_is_built(
    error: aiodocker.DockerError, level: int, caplog: pytest.LogCaptureFixture
) -> None:
    docker: Any = Docker()
    docker.images.pull_error = error
    tag = context("tracer")[0]
    with caplog.at_level(logging.INFO, logger="aims.sandbox"):
        assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert docker.images.pulls == [f"ghcr.io/kaumnen/{tag}"]
    assert docker.images.builds == 1
    assert tag in docker.images.tags
    (record,) = [r for r in caplog.records if r.getMessage().startswith("cannot use")]
    assert record.levelno == level
    assert "\n" not in record.getMessage()


async def test_a_pull_that_leaves_no_image_builds() -> None:
    docker: Any = Docker()

    async def cut(ref: str, **_: Any) -> list[Any]:
        docker.images.pulls.append(ref)
        return []

    docker.images.pull = cut
    tag = context("toolbox")[0]
    assert await ensure_image(docker, "toolbox", env=DEFAULT) == tag
    assert len(docker.images.pulls) == 1
    assert docker.images.builds == 1


async def test_an_unreachable_daemon_is_not_mistaken_for_a_missing_image() -> None:
    docker: Any = Docker()
    docker.images.pull_error = aiodocker.DockerError(900, "Cannot connect to Docker Engine")
    with pytest.raises(aiodocker.DockerError) as caught:
        await ensure_image(docker, "toolbox", env=DEFAULT)
    assert caught.value.status == 900
    assert docker.images.builds == 0


async def test_an_empty_registry_never_pulls() -> None:
    docker: Any = Docker()
    tag = context("toolbox")[0]
    docker.images.published.add(f"ghcr.io/kaumnen/{tag}")
    assert await ensure_image(docker, "toolbox", env={"AIMS_IMAGE_REGISTRY": " "}) == tag
    assert docker.images.pulls == []
    assert docker.images.builds == 1


async def test_a_mirror_names_the_pull() -> None:
    docker: Any = Docker()
    tag = context("toolbox")[0]
    mirror = "mirror.internal:5000/aims"
    docker.images.published.add(f"{mirror}/{tag}")
    assert await ensure_image(docker, "toolbox", env={"AIMS_IMAGE_REGISTRY": f"{mirror}/"}) == tag
    assert docker.images.pulls == [f"{mirror}/{tag}"]
    assert docker.images.builds == 0


async def test_a_loaded_published_image_is_used_without_a_registry() -> None:
    docker: Any = Docker()
    tag = context("tracer")[0]
    docker.images.tags.add(f"ghcr.io/kaumnen/{tag}")
    assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert tag in docker.images.tags
    assert docker.images.pulls == []
    assert docker.images.builds == 0


async def test_an_asset_image_already_here_is_left_alone() -> None:
    docker: Any = Docker()
    tag = context("tracer")[0]
    docker.images.tags.add(tag)
    docker.images.published.add(f"ghcr.io/kaumnen/{tag}")
    assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert docker.images.pulls == []
    assert docker.images.builds == 0


async def test_starts_at_once_pull_once() -> None:
    docker: Any = Docker()
    tag = context("tracer")[0]
    docker.images.published.add(f"ghcr.io/kaumnen/{tag}")
    tags = await asyncio.gather(*(ensure_image(docker, "tracer", env=DEFAULT) for _ in range(3)))
    assert set(tags) == {tag}
    assert len(docker.images.pulls) == 1
    assert docker.images.builds == 0


# A registry on the daemon's own network: the daemon pulls from 127.0.0.0/8 over plain HTTP,
# and with host networking that address is the daemon's, on Linux and in Docker Desktop's VM.
REGISTRY_IMAGE = (
    "registry:3@sha256:d4032d4cc323c1731c707931ecada07a53076eac38e3b61ae57533f170f2c6c3"
)


@pytest.fixture
async def docker() -> AsyncIterator[aiodocker.Docker]:
    client = aiodocker.Docker()
    try:
        await client.system.info()
    except Exception as e:
        await client.close()
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.docker
async def test_an_asset_image_comes_from_a_registry_and_is_built_without_one(
    docker: aiodocker.Docker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = tmp_path / "assets" / "probe"
    probe.mkdir(parents=True)
    (probe / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
    (probe / "marker").write_text(uuid.uuid4().hex)
    monkeypatch.setattr(images, "ASSETS", tmp_path / "assets")
    tag = context("probe")[0]
    repo, _, version = tag.rpartition(":")
    port = 20000 + uuid.uuid4().int % 30000
    remote = f"127.0.0.1:{port}/{tag}"

    await ensure_pulled(docker, REGISTRY_IMAGE)
    registry = await docker.containers.run(
        config={
            "Image": REGISTRY_IMAGE,
            "Env": [f"REGISTRY_HTTP_ADDR=127.0.0.1:{port}"],
            "Labels": {
                "aims.managed": "1",
                "aims.host": socket.gethostname(),
                "aims.pid": str(os.getpid()),
            },
            "HostConfig": {"NetworkMode": "host", "AutoRemove": True},
        },
        name=f"aims-test-registry-{port}",
    )
    try:
        closed = {"AIMS_IMAGE_REGISTRY": "127.0.0.1:1"}
        with caplog.at_level(logging.INFO, logger="aims.sandbox"):
            assert await ensure_image(docker, "probe", env=closed) == tag
        assert any(r.getMessage().startswith("cannot use 127.0.0.1:1/") for r in caplog.records)
        assert any(r.getMessage() == f"building {tag}" for r in caplog.records)

        await docker.images.tag(tag, f"127.0.0.1:{port}/{repo}", tag=version)
        for attempt in range(50):
            try:
                await docker.images.push(f"127.0.0.1:{port}/{repo}", tag=version)
                break
            except aiodocker.DockerError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.2)
        await docker.images.delete(remote)
        await docker.images.delete(tag)

        async def no_build(**_: Any) -> None:
            raise AssertionError("built an image the registry has")

        monkeypatch.setattr(docker.images, "build", no_build)
        caplog.clear()
        local = {"AIMS_IMAGE_REGISTRY": f"127.0.0.1:{port}"}
        with caplog.at_level(logging.INFO, logger="aims.sandbox"):
            assert await ensure_image(docker, "probe", env=local) == tag
        image = await docker.images.inspect(tag)
        assert any(d.startswith(f"127.0.0.1:{port}/{repo}@sha256:") for d in image["RepoDigests"])
        assert f"pulling {remote}" in [r.getMessage() for r in caplog.records]
    finally:
        for ref in (remote, tag):
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref, force=True)
        with contextlib.suppress(aiodocker.DockerError):
            await registry.stop()
