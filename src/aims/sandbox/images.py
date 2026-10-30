import gzip
import hashlib
import io
import logging
import tarfile
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import aiodocker

log = logging.getLogger("aims.sandbox")

ASSETS = files("aims.sandbox") / "assets"


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


async def ensure_image(docker: aiodocker.Docker, name: str) -> str:
    """Build the asset image `name` unless an image with the same content hash exists."""
    tag, data = context(name)
    return await _build(docker, tag, fileobj=io.BytesIO(data), encoding="gzip")


async def _exists(docker: aiodocker.Docker, tag: str) -> bool:
    try:
        await docker.images.inspect(tag)
    except aiodocker.DockerError as e:
        if e.status != 404:
            raise
        return False
    return True


async def _build(docker: aiodocker.Docker, tag: str, **kwargs: Any) -> str:
    """Build `tag` unless it exists. Tags name their content, so when a build fails because
    another process built the same tag at the same time, that image serves."""
    if await _exists(docker, tag):
        return tag
    log.info("building %s", tag)
    try:
        await docker.images.build(tag=tag, labels={"aims.managed": "1"}, **kwargs)
    except aiodocker.DockerError:
        if not await _exists(docker, tag):
            raise
    return tag


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
