import asyncio
import contextlib
import fcntl
import gzip
import importlib.util
import io
import json
import logging
import os
import socket
import sys
import tarfile
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self

import aiodocker
import pytest
from typer.testing import CliRunner

from aims.cli import app
from aims.sandbox import images
from aims.sandbox.images import context, ensure_built, ensure_image, ensure_pulled

pytestmark = pytest.mark.anyio

DEFAULT = {"AIMS_IMAGE_REGISTRY": "ghcr.io/kaumnen"}
DIGEST = "sha256:" + "ab" * 32
ROOT = Path(__file__).resolve().parents[1]


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

    async def pull(self, ref: str, *, tag: str | None = None) -> list[Any]:
        ref = f"{ref}@{tag}" if tag else ref
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


class Ended:
    """The progress of a build or pull that has ended."""

    def __init__(self) -> None:
        self.content = self

    async def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class Docker:
    def __init__(self) -> None:
        self.images = Images()

    @contextlib.asynccontextmanager
    async def _query(
        self, path: str, method: str, *, params: dict[str, str], **_: Any
    ) -> AsyncGenerator[Ended]:
        """The builds and pulls `images` sends, made by the fake image API."""
        if path == "build":
            await self.images.build(tag=params["t"])
        else:
            await self.images.pull(params["fromImage"], tag=params["tag"])
        yield Ended()


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


def held(tag: str) -> bool:
    """Whether a build of `tag` holds the host lock now."""
    try:
        fd = os.open(images._lock_path(tag), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


class Endless:
    """The progress of a build or pull that never ends. Each close records whether the build
    lock of `tag` is held then."""

    def __init__(self, tag: str) -> None:
        self.content = self
        self.tag = tag
        self.reading = asyncio.Event()
        self.closes: list[bool] = []

    async def readline(self) -> bytes:
        self.reading.set()
        await asyncio.Event().wait()
        return b""

    def close(self) -> None:
        self.closes.append(held(self.tag))


@pytest.mark.parametrize("call", ["build", "pull"])
async def test_a_cancelled_build_or_pull_hangs_up_before_the_lock_goes(
    call: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker goes on with a build or pull while its connection is open."""
    context = uuid.uuid4().bytes
    tag = images.built_tag(context)
    progress = Endless(tag)
    docker = aiodocker.Docker(url="unix:///nonexistent/docker.sock")

    async def query(path: str, method: str = "GET", **_: Any) -> Any:
        if path in ("build", "images/create"):
            return progress
        raise aiodocker.DockerError(404, "no such image")

    monkeypatch.setattr(docker, "_do_query", query)
    work = ensure_built(docker, context) if call == "build" else ensure_pulled(docker, tag)
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(progress.reading.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(progress.closes) == 1
        assert progress.closes[0] == (call == "build")
        assert not held(tag)
    finally:
        await docker.close()


@pytest.fixture
def pin(monkeypatch: pytest.MonkeyPatch) -> Callable[..., str]:
    """Pin asset images as a release would; returns the first image's tag."""

    def pin(*names: str, pins: dict[str, str] | None = None) -> str:
        tags = [context(name)[0] for name in names]
        table = dict.fromkeys(tags, DIGEST) if pins is None else pins
        monkeypatch.setattr(images, "published", lambda: table)
        return tags[0] if tags else ""

    return pin


def by_digest(tag: str, prefix: str = "ghcr.io/kaumnen", digest: str = DIGEST) -> str:
    return f"{prefix}/{tag.partition(':')[0]}@{digest}"


async def test_a_published_asset_image_is_pulled_by_digest_under_the_tag_a_build_would_get(
    pin: Callable[..., str],
) -> None:
    docker: Any = Docker()
    tag = pin("tracer")
    docker.images.published.add(by_digest(tag))
    assert await ensure_image(docker, "tracer", env={}) == tag
    assert tag.startswith("aims-tracer:")
    assert docker.images.pulls == [by_digest(tag)]
    assert tag in docker.images.tags
    assert docker.images.builds == 0


async def test_without_a_pin_nothing_is_pulled_even_when_the_registry_has_the_tag(
    pin: Callable[..., str],
) -> None:
    docker: Any = Docker()
    pin()
    tag = context("tracer")[0]
    docker.images.published.update({f"ghcr.io/kaumnen/{tag}", by_digest(tag)})
    assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert docker.images.pulls == []
    assert docker.images.builds == 1


async def test_a_pin_for_other_asset_files_is_not_used(pin: Callable[..., str]) -> None:
    docker: Any = Docker()
    stale = "aims-tracer:000000000000"
    pin(pins={stale: DIGEST})
    docker.images.published.add(by_digest(stale))
    assert await ensure_image(docker, "tracer", env=DEFAULT) == context("tracer")[0]
    assert docker.images.pulls == []
    assert docker.images.builds == 1


def test_the_pins_come_from_the_release_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "PUBLISHED", tmp_path / "published.json")
    assert images.published() == {}
    (tmp_path / "published.json").write_text(json.dumps({"aims-tracer:abc": DIGEST}))
    assert images.published() == {"aims-tracer:abc": DIGEST}


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
    error: aiodocker.DockerError,
    level: int,
    pin: Callable[..., str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    docker: Any = Docker()
    docker.images.pull_error = error
    tag = pin("tracer")
    with caplog.at_level(logging.INFO, logger="aims.sandbox"):
        assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert docker.images.pulls == [by_digest(tag)]
    assert docker.images.builds == 1
    assert tag in docker.images.tags
    (record,) = [r for r in caplog.records if r.getMessage().startswith("cannot use")]
    assert record.levelno == level
    assert "\n" not in record.getMessage()


async def test_a_pull_that_leaves_no_image_builds(pin: Callable[..., str]) -> None:
    docker: Any = Docker()

    async def cut(ref: str, **_: Any) -> list[Any]:
        docker.images.pulls.append(ref)
        return []

    docker.images.pull = cut
    tag = pin("toolbox")
    assert await ensure_image(docker, "toolbox", env=DEFAULT) == tag
    assert len(docker.images.pulls) == 1
    assert docker.images.builds == 1


async def test_an_unreachable_daemon_is_not_mistaken_for_a_missing_image(
    pin: Callable[..., str],
) -> None:
    docker: Any = Docker()
    docker.images.pull_error = aiodocker.DockerError(900, "Cannot connect to Docker Engine")
    pin("toolbox")
    with pytest.raises(aiodocker.DockerError) as caught:
        await ensure_image(docker, "toolbox", env=DEFAULT)
    assert caught.value.status == 900
    assert docker.images.builds == 0


async def test_an_empty_registry_never_pulls(pin: Callable[..., str]) -> None:
    docker: Any = Docker()
    tag = pin("toolbox")
    docker.images.published.add(by_digest(tag))
    assert await ensure_image(docker, "toolbox", env={"AIMS_IMAGE_REGISTRY": " "}) == tag
    assert docker.images.pulls == []
    assert docker.images.builds == 1


async def test_a_mirror_names_the_pull(pin: Callable[..., str]) -> None:
    docker: Any = Docker()
    tag = pin("toolbox")
    mirror = "mirror.internal:5000/aims"
    docker.images.published.add(by_digest(tag, mirror))
    assert await ensure_image(docker, "toolbox", env={"AIMS_IMAGE_REGISTRY": f"{mirror}/"}) == tag
    assert docker.images.pulls == [by_digest(tag, mirror)]
    assert docker.images.builds == 0


async def test_a_loaded_published_image_is_used_without_a_registry(
    pin: Callable[..., str],
) -> None:
    docker: Any = Docker()
    tag = pin("tracer")
    docker.images.tags.add(by_digest(tag))
    assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert tag in docker.images.tags
    assert docker.images.pulls == []
    assert docker.images.builds == 0


async def test_an_asset_image_already_here_is_left_alone(pin: Callable[..., str]) -> None:
    docker: Any = Docker()
    tag = pin("tracer")
    docker.images.tags.add(tag)
    docker.images.published.add(by_digest(tag))
    assert await ensure_image(docker, "tracer", env=DEFAULT) == tag
    assert docker.images.pulls == []
    assert docker.images.builds == 0


async def test_starts_at_once_pull_once(pin: Callable[..., str]) -> None:
    docker: Any = Docker()
    tag = pin("tracer")
    docker.images.published.add(by_digest(tag))
    tags = await asyncio.gather(*(ensure_image(docker, "tracer", env=DEFAULT) for _ in range(3)))
    assert set(tags) == {tag}
    assert len(docker.images.pulls) == 1
    assert docker.images.builds == 0


def test_the_release_names_and_checks_the_helpers_as_a_runner_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("helpers", ROOT / "scripts" / "helpers.py")
    assert spec is not None
    assert spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    toolbox, tracer = context("toolbox")[0], context("tracer")[0]
    assert helpers.names() == ["toolbox", "tracer"]

    def check(pins: dict[str, str]) -> list[str]:
        monkeypatch.setattr(helpers, "published", lambda: pins)
        return helpers.check()

    assert check({}) == [f"no pin for {toolbox}", f"no pin for {tracer}"]
    assert check({toolbox: DIGEST, tracer: DIGEST}) == []
    assert check({toolbox: DIGEST, tracer: "latest", "aims-tracer:0": DIGEST}) == [
        "pin for aims-tracer:0, which no helper has",
        f"{tracer} is pinned to 'latest'",
    ]
    out = tmp_path / "ctx.tar.gz"
    monkeypatch.setattr(sys, "argv", ["helpers.py", "context", "tracer", str(out)])
    assert helpers.main() == 0
    assert out.read_bytes() == context("tracer")[1]


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
    source = f"127.0.0.1:{port}/{repo}"
    refs = [f"{source}:{version}", tag]
    pins: dict[str, str] = {tag: "sha256:" + "0" * 64}
    monkeypatch.setattr(images, "published", lambda: pins)
    post = images._post
    builds: list[str] = []

    async def counted(docker: aiodocker.Docker, path: str, params: Any, **kwargs: Any) -> None:
        if path == "build":
            builds.append(params["t"])
        await post(docker, path, params, **kwargs)

    monkeypatch.setattr(images, "_post", counted)

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
        assert builds == [tag]

        await docker.images.tag(tag, source, tag=version)
        for attempt in range(50):
            try:
                await docker.images.push(source, tag=version)
                break
            except aiodocker.DockerError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.2)
        pushed = (await docker.images.inspect(f"{source}:{version}"))["RepoDigests"]
        (digest,) = [d.partition("@")[2] for d in pushed if d.startswith(f"{source}@")]
        remote = f"{source}@{digest}"
        refs.append(remote)
        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref)

        local = {"AIMS_IMAGE_REGISTRY": f"127.0.0.1:{port}"}
        pins[tag] = digest
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="aims.sandbox"):
            assert await ensure_image(docker, "probe", env=local) == tag
        assert builds == [tag]
        image = await docker.images.inspect(tag)
        assert remote in image["RepoDigests"]
        messages = [r.getMessage() for r in caplog.records]
        assert f"pulling {remote}" in messages
        assert f"{tag} is {remote}" in messages

        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref)
        pins[tag] = "sha256:" + "1" * 64
        assert await ensure_image(docker, "probe", env=local) == tag
        assert builds == [tag, tag]
    finally:
        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref, force=True)
        with contextlib.suppress(aiodocker.DockerError):
            await registry.stop()


@pytest.mark.docker
async def test_an_old_pulled_asset_image_goes_with_its_registry_reference(
    docker: aiodocker.Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "assets" / "probe"
    probe.mkdir(parents=True)
    (probe / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
    (probe / "marker").write_text(uuid.uuid4().hex)
    monkeypatch.setattr(images, "ASSETS", tmp_path / "assets")
    monkeypatch.setattr(images, "MIN_AGE_S", 0)
    tag = await ensure_image(docker, "probe", env={"AIMS_IMAGE_REGISTRY": ""})
    repo, _, version = tag.rpartition(":")
    port = 20000 + uuid.uuid4().int % 30000
    source = f"127.0.0.1:{port}/{repo}"
    refs = [tag, f"{source}:{version}"]

    await ensure_pulled(docker, REGISTRY_IMAGE)
    registry = await docker.containers.run(
        config={
            "Image": REGISTRY_IMAGE,
            "Env": [f"REGISTRY_HTTP_ADDR=127.0.0.1:{port}"],
            "HostConfig": {"NetworkMode": "host", "AutoRemove": True},
        },
        name=f"aims-test-registry-{port}",
    )
    try:
        await docker.images.tag(tag, source, tag=version)
        for attempt in range(50):
            try:
                await docker.images.push(source, tag=version)
                break
            except aiodocker.DockerError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.2)
        pushed = (await docker.images.inspect(f"{source}:{version}"))["RepoDigests"]
        (digest,) = [d.partition("@")[2] for d in pushed if d.startswith(f"{source}@")]
        remote = f"{source}@{digest}"
        refs.append(remote)
        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref)
        assert await images._pull(docker, remote, tag)
        assert remote in (await docker.images.inspect(tag))["RepoDigests"]

        (probe / "marker").write_text(uuid.uuid4().hex)
        await images.remove_old(docker)
        for ref in (tag, remote):
            with pytest.raises(aiodocker.DockerError) as e:
                await docker.images.inspect(ref)
            assert e.value.status == 404
    finally:
        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref, force=True)
        with contextlib.suppress(aiodocker.DockerError):
            await registry.stop()


def scenario_context() -> bytes:
    """The build context of a tiny image that no other test builds."""
    return dockerfile_context("FROM scratch\nCOPY marker /marker\n", uuid.uuid4().hex)


def dockerfile_context(dockerfile: str, marker: str | None = None) -> bytes:
    """A tar build context of `dockerfile`, and of a file `marker` when given."""
    files = [("Dockerfile", dockerfile.encode())]
    if marker is not None:
        files.append(("marker", marker.encode()))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def env_repo(docker: aiodocker.Docker, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A scenario image repository of the test's own, so pruning leaves every other scenario
    image on this host alone. Its images go at the end."""
    repo = f"aims-env-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(images, "ENV_REPO", repo)
    try:
        yield repo
    finally:
        for image in await docker.images.list(filters={"reference": [repo]}):
            for ref in image.get("RepoTags") or []:
                with contextlib.suppress(aiodocker.DockerError):
                    await docker.images.delete(ref, force=True)


def days_later(monkeypatch: pytest.MonkeyPatch, days: float) -> None:
    """Move the clock that image pruning reads `days` ahead."""

    class Later(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return super().now(tz) + timedelta(days=days)

    monkeypatch.setattr(images, "datetime", Later)


async def until(test: Callable[[], Awaitable[bool]], within: float) -> None:
    """Return once `test` holds; TimeoutError after `within` seconds."""
    async with asyncio.timeout(within):
        while not await test():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def present(docker: aiodocker.Docker, *refs: str) -> set[str]:
    found = set()
    for ref in refs:
        with contextlib.suppress(aiodocker.DockerError):
            await docker.images.inspect(ref)
            found.add(ref)
    return found


@pytest.mark.docker
async def test_a_scenario_image_goes_30_days_after_its_last_use_unless_a_container_uses_it(
    docker: aiodocker.Docker, env_repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await ensure_built(docker, scenario_context())
    used = await ensure_built(docker, scenario_context())
    assert old.startswith(f"{env_repo}:")
    holder = await docker.containers.create(
        config={"Image": used, "Cmd": ["/marker"], "Labels": {"aims.test": "images"}}
    )
    try:
        await images.remove_old(docker)
        assert await present(docker, old, used) == {old, used}
        days_later(monkeypatch, 31)
        listed = await images.remove_old(docker, dry_run=True)
        assert old in listed
        assert used not in listed
        await images.remove_old(docker)
        assert await present(docker, old, used) == {used}
    finally:
        with contextlib.suppress(aiodocker.DockerError):
            await holder.delete(force=True)


@pytest.mark.docker
async def test_an_image_a_second_build_of_its_tag_untagged_goes_unless_used(
    docker: aiodocker.Docker, env_repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What two builds of one tag at once leave. A dry run, as other images on this daemon are
    not this test's to remove."""
    tag = f"{env_repo}:{uuid.uuid4().hex[:16]}"
    made = []
    try:
        for _ in range(2):
            await docker.images.build(
                fileobj=io.BytesIO(gzip.compress(scenario_context())),
                encoding="gzip",
                tag=tag,
                labels={"aims.managed": "1"},
            )
            made.append((await docker.images.inspect(tag))["Id"])
        first = made[0]
        found = await docker.images.inspect(first)
        assert not found["RepoTags"]
        assert not found["RepoDigests"]
        monkeypatch.setattr(images, "MIN_AGE_S", 0)
        holder = await docker.containers.create(
            config={"Image": first, "Cmd": ["/marker"], "Labels": {"aims.test": "images"}}
        )
        try:
            assert first not in await images.remove_old(docker, dry_run=True)
        finally:
            await holder.delete(force=True)
        assert first in await images.remove_old(docker, dry_run=True)
        assert made[1] not in await images.remove_old(docker, dry_run=True)
    finally:
        for image in made:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(image, force=True)


@pytest.mark.docker
async def test_a_reused_scenario_image_counts_as_used_now(
    docker: aiodocker.Docker, env_repo: str
) -> None:
    context = scenario_context()
    tag = await ensure_built(docker, context)

    async def tagged() -> datetime:
        found = await docker.images.inspect(tag)
        return datetime.fromisoformat(found["Metadata"]["LastTagTime"])

    built = await tagged()
    assert await ensure_built(docker, context) == tag
    used = await tagged()
    assert used > built
    assert await ensure_built(docker, context) == tag
    assert await tagged() > used
    assert await present(docker, tag, f"{tag}-used") == {tag, f"{tag}-used"}


@pytest.mark.docker
async def test_a_cancelled_build_stops_the_daemons_build(
    docker: aiodocker.Docker, env_repo: str
) -> None:
    """A cancel stops the build's step at once, leaves no step container and no tag, and a
    retry at once is the only build of the tag that runs."""
    base = await ensure_image(docker, "toolbox")
    nonce = uuid.uuid4().hex
    context = dockerfile_context(
        f"FROM {base}\nRUN echo {nonce} > /a\nRUN sleep 6 && echo {nonce} > /b\n"
    )
    tag = images.built_tag(context)

    async def steps(**filters: Any) -> list[str]:
        """The `sleep` step containers of this context's builds."""
        found = await docker.containers.list(**filters)
        return [c.id for c in found if nonce in c["Command"] and "sleep" in c["Command"]]

    async def running() -> bool:
        return bool(await steps())

    async def gone() -> bool:
        return not await steps(all=True)

    async def idle() -> bool:
        return not await steps()

    most = 0

    async def watch() -> None:
        nonlocal most
        while True:
            most = max(most, len(await steps()))
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch())
    task = asyncio.create_task(ensure_built(docker, context))
    try:
        await until(running, 60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await until(gone, 3)
        assert await present(docker, tag) == set()
        assert await ensure_built(docker, context) == tag
        assert most == 1
        assert await gone()
    finally:
        watcher.cancel()
        task.cancel()
        # A build the cancel did not stop ends before the images go.
        with contextlib.suppress(TimeoutError):
            await until(idle, 10)


@pytest.mark.docker
async def test_a_failed_build_leaves_no_step_container(
    docker: aiodocker.Docker, env_repo: str
) -> None:
    base = await ensure_image(docker, "toolbox")
    nonce = uuid.uuid4().hex
    with pytest.raises(aiodocker.DockerError):
        await ensure_built(docker, dockerfile_context(f"FROM {base}\nRUN echo {nonce} && exit 3\n"))
    left = [c for c in await docker.containers.list(all=True) if nonce in c["Command"]]
    for c in left:
        await c.delete(force=True)
    assert left == []


@pytest.mark.docker
async def test_a_dry_run_of_images_prune_lists_a_stale_scenario_image_and_keeps_it(
    docker: aiodocker.Docker, env_repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = await ensure_built(docker, scenario_context())
    monkeypatch.setattr(images, "MIN_AGE_S", 0)
    args = ["images", "prune", "--days", "0", "--dry-run"]
    out = await asyncio.to_thread(CliRunner().invoke, app, args)
    assert out.exit_code == 0, out.output
    assert f"would remove {tag}\n" in out.stdout
    assert await present(docker, tag) == {tag}


class NoDocker:
    async def close(self) -> None:
        pass


def test_images_prune_takes_its_days_from_the_flag_else_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[float, bool]] = []

    async def remove_old(docker: Any, keep_days: float, *, dry_run: bool) -> list[str]:
        calls.append((keep_days, dry_run))
        if keep_days > 100:
            raise aiodocker.DockerError(900, "Cannot connect to Docker Engine")
        return ["aims-env:0123456789abcdef"] if keep_days < 10 else []

    monkeypatch.setattr(aiodocker, "Docker", NoDocker)
    monkeypatch.setattr(images, "remove_old", remove_old)
    project = tmp_path / "aims.toml"
    project.write_text("[images]\nkeep_days = 7\n")
    runner = CliRunner()

    def prune(*args: str) -> tuple[int, str]:
        out = runner.invoke(app, ["images", "prune", "--config", str(project), *args])
        return out.exit_code, out.output

    assert prune() == (0, "removed aims-env:0123456789abcdef\n")
    assert prune("--days", "40", "--dry-run") == (0, "nothing to remove\n")
    code, said = prune("--days", "400")
    assert code == 1
    assert "error: cannot prune images: " in said
    assert calls == [(7.0, False), (40.0, True), (400.0, False)]
