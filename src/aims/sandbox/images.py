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

import aiodocker
from aiodocker.jsonstream import json_stream_list

log = logging.getLogger("aims.sandbox")

ASSETS = files("aims.sandbox") / "assets"
# Written by the release: each published asset image tag and the digest it was pushed as.
PUBLISHED = files("aims.sandbox") / "published.json"

REGISTRY = "ghcr.io/kaumnen"
# aiodocker's status when it cannot reach the Docker daemon at all
DAEMON_DOWN = 900
# Like kubelet's minimum image age: an image another AIMS version just tagged is about to be used.
MIN_AGE_S = 120
# Scenario images no container uses go this many days after their last use.
KEEP_DAYS = 30.0
# Docker's LastTagTime of an image never tagged here, such as one `docker load` made
NEVER = datetime(1, 1, 1, tzinfo=UTC)


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
    return await _build(docker, tag, data, remote=remote)


async def remove_old(
    docker: aiodocker.Docker, keep_days: float = KEEP_DAYS, *, dry_run: bool = False
) -> list[str]:
    """Remove the images no container uses that AIMS needs no more: the asset images of other
    AIMS versions, the scenario images last used more than `keep_days` ago, and the images
    with no tag and no digest that a cut build or a second build of a tag left. An asset image
    that carries a current tag under any registry or namespace stays. Returns the references
    removed, or with `dry_run` those it would remove."""
    current = {context(node.name)[0] for node in ASSETS.iterdir() if node.is_dir()}
    repos = {tag.partition(":")[0] for tag in current}
    used = {c["ImageID"] for c in await docker.containers.list(all=True)}
    removed: list[str] = []
    for image in await docker.images.list(filters={"label": ["aims.managed=1"]}):
        if image["Id"] in used:
            continue
        tags = image.get("RepoTags") or []
        assets = [r for r in (*tags, *(image.get("RepoDigests") or [])) if _repo(r) in repos]
        scenario = [t for t in tags if _repo(t) == ENV_REPO]
        if assets and not current.intersection(_name(t) for t in tags):
            if await _tagged_within(docker, image["Id"], MIN_AGE_S):
                continue
            refs, why = assets, "left by another AIMS version"
        elif scenario:
            if await _used_within(docker, image["Id"], keep_days * 86400):
                continue
            refs, why = scenario, f"unused for {keep_days:g} days"
        elif not tags and not image.get("RepoDigests"):
            # Nothing can name it, so only a process that read its tag before the tag went
            # can still use it. On Docker's containerd image store LastTagTime is when the tag
            # went. An image never tagged, such as a build's own before its tag, stays.
            if await _used_within(docker, image["Id"], MIN_AGE_S):
                continue
            refs, why = [image["Id"]], "untagged"
        else:
            continue
        if dry_run:
            removed += refs
            continue
        log.info("removing %s, %s", " ".join(refs), why)
        for ref in refs:
            try:
                await docker.images.delete(ref)
            except aiodocker.DockerError:
                continue
            removed.append(ref)
    return removed


async def _tagged(docker: aiodocker.Docker, image: str) -> datetime | None:
    """When `image` was last tagged here, NEVER if it never was; None when Docker cannot tell."""
    try:
        tagged = (await docker.images.inspect(image))["Metadata"]["LastTagTime"]
        return datetime.fromisoformat(tagged)
    except aiodocker.DockerError, KeyError, TypeError, ValueError:
        return None


async def _tagged_within(docker: aiodocker.Docker, image: str, seconds: float) -> bool:
    """Whether `image` was tagged here in the last `seconds`; True when Docker cannot tell."""
    tagged = await _tagged(docker, image)
    return tagged is None or (datetime.now(UTC) - tagged).total_seconds() < seconds


async def _used_within(docker: aiodocker.Docker, image: str, seconds: float) -> bool:
    """Whether `image` was built, used or untagged here in the last `seconds`, or in the last
    MIN_AGE_S. True when Docker cannot tell or it was never tagged, as for an image loaded and
    not used since."""
    tagged = await _tagged(docker, image)
    if tagged is None or tagged == NEVER:
        return True
    return (datetime.now(UTC) - tagged).total_seconds() < max(seconds, MIN_AGE_S)


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
    docker: aiodocker.Docker,
    tag: str,
    data: bytes,
    *,
    dockerfile: str = "Dockerfile",
    remote: str | None = None,
) -> str:
    """Build `tag` from the gzipped tar build context `data` unless it exists or the image
    `remote` can stand in for it, one build of a tag at a time on this host: on Docker's
    containerd image store a second build of a tag deletes the image the first one made. A
    cancel stops Docker's build before the lock goes. A build that fails or is cut leaves no
    step container."""
    if await _exists(docker, tag):
        return tag
    async with _host_lock(tag):
        if await _exists(docker, tag):
            return tag
        if remote is not None and await _pull(docker, remote, tag):
            return tag
        log.info("building %s", tag)
        params = {
            "t": tag,
            "dockerfile": dockerfile,
            "labels": json.dumps({"aims.managed": "1"}),
            "forcerm": "1",
        }
        headers = {"Content-Type": "application/x-tar", "Content-Encoding": "gzip"}
        await _post(docker, "build", params, data=data, headers=headers)
    return tag


async def _post(
    docker: aiodocker.Docker,
    path: str,
    params: dict[str, str],
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Make the Docker call `path`, a build or a pull, and read its progress to the end; an
    error in the progress raises. A cancel or an error closes the call's connection, which
    makes Docker stop the work. aiodocker's own build and pull keep it open, so Docker goes
    on to the end."""
    # aiodocker has no public call that closes it, so this uses its private one.
    async with docker._query(
        path, "POST", params=params, data=data, headers=headers, timeout=None
    ) as response:
        try:
            await json_stream_list(response)
        finally:
            response.close()


async def _pull(docker: aiodocker.Docker, remote: str, tag: str) -> bool:
    """Tag the image `remote` (`repo@digest`) as `tag`, pulling it unless it is here already
    (`docker load`). False when it cannot be had."""
    repo, _, version = tag.rpartition(":")
    source, _, digest = remote.partition("@")
    try:
        if not await _exists(docker, remote):
            log.info("pulling %s", remote)
            await _post(docker, "images/create", {"fromImage": source, "tag": digest})
        await docker.images.tag(remote, repo, tag=version)
    except aiodocker.DockerError as e:
        if e.status == DAEMON_DOWN:
            raise
        level = logging.INFO if e.status == 404 else logging.WARNING
        log.log(level, "cannot use %s, building it here: %s", remote, e.message.partition("\n")[0])
        return False
    log.info("%s is %s", tag, remote)
    return True


def _lock_path(tag: str) -> Path:
    name = tag.replace(":", "-").replace("/", "-")
    return Path(tempfile.gettempdir()) / f"aims-build-{name}.lock"


@contextlib.asynccontextmanager
async def _host_lock(tag: str, poll: float = 0.2) -> AsyncGenerator[None]:
    fd = os.open(_lock_path(tag), os.O_RDWR | os.O_CREAT, 0o600)
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
    await _post(docker, "images/create", {"fromImage": name, "tag": tag})
    return await docker.images.inspect(image)


ENV_REPO = "aims-env"


def built_tag(context: bytes, dockerfile: str = "Dockerfile") -> str:
    """The tag of the image built from the tar build context `context`."""
    digest = hashlib.sha256(context + b"\0" + dockerfile.encode()).hexdigest()
    return f"{ENV_REPO}:{digest[:16]}"


async def ensure_built(
    docker: aiodocker.Docker, context: bytes, dockerfile: str = "Dockerfile"
) -> str:
    """Build the tar build context `context` unless an image built from the same bytes exists.
    An image that exists counts as used now."""
    tag = built_tag(context, dockerfile)
    if await _exists(docker, tag) and await _touch(docker, tag):
        return tag
    return await _build(docker, tag, gzip.compress(context, mtime=0), dockerfile=dockerfile)


async def _touch(docker: aiodocker.Docker, tag: str) -> bool:
    """Count the image `tag` as used now. Docker's containerd image store moves LastTagTime
    only when an image gets a new name, so this drops and remakes a second name, `<tag>-used`.
    False when `tag` is gone."""
    repo, _, version = tag.rpartition(":")
    with contextlib.suppress(aiodocker.DockerError):
        await docker.images.delete(f"{tag}-used")
    try:
        await docker.images.tag(tag, repo, tag=f"{version}-used")
    except aiodocker.DockerError as e:
        if e.status == DAEMON_DOWN:
            raise
        return False
    return True
