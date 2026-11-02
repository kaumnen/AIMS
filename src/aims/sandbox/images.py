import asyncio
import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import aiodocker

log = logging.getLogger("aims.sandbox")

ASSETS = files("aims.sandbox") / "assets"
# Written by the release: each published asset image tag and the digest it was pushed as.
PUBLISHED = files("aims.sandbox") / "published.json"

REGISTRY = "ghcr.io/kaumnen"
# aiodocker's status when it cannot reach the Docker daemon at all
DAEMON_DOWN = 900
# Like kubelet's minimum image age: an image another AIMS version just tagged is about to be used.
MIN_AGE_S = 120


def registry(env: Mapping[str, str] = os.environ) -> str:
    """Where the published asset images live: `$AIMS_IMAGE_REGISTRY`, else GHCR. Empty means
    never pull, always build here."""
    return env.get("AIMS_IMAGE_REGISTRY", REGISTRY).strip().rstrip("/")


def published() -> dict[str, str]:
    """The digest each asset image tag was published as, by tag; empty outside a release."""
    try:
        return json.loads(PUBLISHED.read_text())
    except FileNotFoundError:
        return {}


def context(name: str) -> tuple[str, bytes]:
    """The image tag for asset dir `name`, named by a hash of its files, and its gzipped tar."""
    entries = sorted(_walk(ASSETS / name, ""))
    digest = hashlib.sha256()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in entries:
            digest.update(path.encode() + b"\0" + hashlib.sha256(data).digest())
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return f"aims-{name}:{digest.hexdigest()[:12]}", gzip.compress(buf.getvalue(), mtime=0)


def _walk(node: Traversable, prefix: str) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for child in node.iterdir():
        path = f"{prefix}{child.name}"
        if child.is_dir():
            out.extend(_walk(child, f"{path}/"))
        elif not child.name.startswith("."):
            out.append((path, child.read_bytes()))
    return out


async def ensure_image(
    docker: aiodocker.Docker, name: str, env: Mapping[str, str] = os.environ
) -> str:
    """The local tag of asset image `name`: an image with the same content hash, else the one
    this release published for that hash, pulled by digest, else a fresh build."""
    tag, data = context(name)
    prefix = registry(env)
    digest = published().get(tag)
    remote = f"{prefix}/{tag.partition(':')[0]}@{digest}" if prefix and digest else None
    return await _build(docker, tag, remote=remote, fileobj=io.BytesIO(data), encoding="gzip")


async def remove_old(docker: aiodocker.Docker) -> None:
    """Remove the asset images of other AIMS versions that no container uses. An image that
    carries a current tag under any registry or namespace stays."""
    current = {context(node.name)[0] for node in ASSETS.iterdir() if node.is_dir()}
    repos = {tag.partition(":")[0] for tag in current}
    used = {c["ImageID"] for c in await docker.containers.list(all=True)}
    for image in await docker.images.list(filters={"label": ["aims.managed=1"]}):
        tags = image.get("RepoTags") or []
        refs = [r for r in (*tags, *(image.get("RepoDigests") or [])) if _repo(r) in repos]
        if not refs or image["Id"] in used or current.intersection(_name(t) for t in tags):
            continue
        if await _tagged_within(docker, image["Id"], MIN_AGE_S):
            continue
        log.info("removing %s, left by another AIMS version", " ".join(refs))
        for ref in refs:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref)


async def _tagged_within(docker: aiodocker.Docker, image: str, seconds: float) -> bool:
    """Whether `image` was tagged here in the last `seconds`; True when Docker cannot tell."""
    try:
        tagged = (await docker.images.inspect(image))["Metadata"]["LastTagTime"]
        age = datetime.now(UTC) - datetime.fromisoformat(tagged)
    except aiodocker.DockerError, KeyError, TypeError, ValueError:
        return True
    return age.total_seconds() < seconds


def _repo(ref: str) -> str:
    """The repository of an image reference without its registry: `aims-tracer` for
    `ghcr.io/kaumnen/aims-tracer@sha256:...`."""
    return _name(ref.partition("@")[0]).partition(":")[0]


def _name(tag: str) -> str:
    """A tag without its registry and namespace: `aims-tracer:0dffb7782a3b` for
    `localhost:5000/keep/aims-tracer:0dffb7782a3b`."""
    return tag.rsplit("/", 1)[-1]


async def _exists(docker: aiodocker.Docker, tag: str) -> bool:
    try:
        await docker.images.inspect(tag)
    except aiodocker.DockerError as e:
        if e.status != 404:
            raise
        return False
    return True


async def _build(
    docker: aiodocker.Docker, tag: str, *, remote: str | None = None, **kwargs: Any
) -> str:
    """Build `tag` unless it exists or the image `remote` can stand in for it, one build of a
    tag at a time on this host: on Docker's containerd image store a second build of a tag
    deletes the image the first one made."""
    if await _exists(docker, tag):
        return tag
    async with _host_lock(tag):
        if await _exists(docker, tag):
            return tag
        if remote is not None and await _pull(docker, remote, tag):
            return tag
        log.info("building %s", tag)
        await docker.images.build(tag=tag, labels={"aims.managed": "1"}, **kwargs)
    return tag


async def _pull(docker: aiodocker.Docker, remote: str, tag: str) -> bool:
    """Tag the image `remote` (`repo@digest`) as `tag`, pulling it unless it is here already
    (`docker load`). False when it cannot be had."""
    repo, _, version = tag.rpartition(":")
    source, _, digest = remote.partition("@")
    try:
        if not await _exists(docker, remote):
            log.info("pulling %s", remote)
            await docker.images.pull(source, tag=digest)
        await docker.images.tag(remote, repo, tag=version)
    except aiodocker.DockerError as e:
        if e.status == DAEMON_DOWN:
            raise
        level = logging.INFO if e.status == 404 else logging.WARNING
        log.log(level, "cannot use %s, building it here: %s", remote, e.message.partition("\n")[0])
        return False
    log.info("%s is %s", tag, remote)
    return True


@contextlib.asynccontextmanager
async def _host_lock(tag: str, poll: float = 0.2) -> AsyncGenerator[None]:
    path = (
        Path(tempfile.gettempdir()) / f"aims-build-{tag.replace(':', '-').replace('/', '-')}.lock"
    )
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(poll)
        yield
    finally:
        os.close(fd)


async def ensure_pulled(docker: aiodocker.Docker, image: str) -> dict:
    try:
        return await docker.images.inspect(image)
    except aiodocker.DockerError as e:
        if e.status != 404:
            raise
    log.info("pulling %s", image)
    name, tag = image, "latest"
    if "@" in image:
        name, tag = image.split("@", 1)
    elif ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    await docker.images.pull(name, tag=tag)
    return await docker.images.inspect(image)


ENV_REPO = "aims-env"


def built_tag(context: bytes, dockerfile: str = "Dockerfile") -> str:
    """The tag of the image built from the tar build context `context`."""
    digest = hashlib.sha256(context + b"\0" + dockerfile.encode()).hexdigest()
    return f"{ENV_REPO}:{digest[:16]}"


async def ensure_built(
    docker: aiodocker.Docker, context: bytes, dockerfile: str = "Dockerfile"
) -> str:
    """Build the tar build context `context` unless an image built from the same bytes exists."""
    tag = built_tag(context, dockerfile)
    return await _build(
        docker,
        tag,
        fileobj=io.BytesIO(gzip.compress(context, mtime=0)),
        encoding="gzip",
        path_dockerfile=dockerfile,
    )
