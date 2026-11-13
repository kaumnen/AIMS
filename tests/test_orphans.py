import asyncio
import contextlib
import gzip
import io
import json
import os
import socket
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiodocker
import pytest

from aims.sandbox import docker as sandbox
from aims.sandbox import images as assets
from aims.sandbox.base import SandboxInfo, SandboxSpec
from aims.sandbox.docker import INSTANCE, _container_id, _own_container, _sweep
from aims.sandbox.images import MIN_AGE_S, context, ensure_image, remove_old

pytestmark = pytest.mark.anyio

ME, OTHER = str(os.getpid()), str(os.getppid())
EARLIER = "0" * 32
MINE, GONE, STOPPED, LIVE, UNKNOWN = (c * 64 for c in "abcde")


class Container(dict[str, Any]):
    def __init__(self, name: str, labels: dict[str, str], gone: list[str]) -> None:
        super().__init__(Labels=labels)
        self.id = name
        self._gone = gone

    async def delete(self, force: bool = False) -> None:
        self._gone.append(self.id)


class Containers:
    def __init__(self, found: Sequence[Container]) -> None:
        self.found = found
        self.asked: list[str] = []

    async def list(self, **_: Any) -> Sequence[Container]:
        return self.found

    async def get(self, container: str) -> dict[str, Any]:
        self.asked.append(container)
        if container == GONE:
            raise aiodocker.DockerError(404, "No such container")
        if container == UNKNOWN:
            raise aiodocker.DockerError(500, "daemon busy")
        return {"State": {"Running": container in (MINE, LIVE)}}


class Volumes:
    def __init__(self, found: Sequence[dict[str, Any]]) -> None:
        self.found = found

    async def list(self, **_: Any) -> dict[str, Any]:
        return {"Volumes": list(self.found)}


class Docker:
    def __init__(self, found: Sequence[Container], volumes: Sequence[dict[str, Any]] = ()) -> None:
        self.containers = Containers(found)
        self.volumes = Volumes(volumes)


@pytest.fixture
def deleted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the volumes deleted through `DockerVolume`."""
    names: list[str] = []

    class Volume:
        def __init__(self, _: Any, name: str) -> None:
            self.name = name

        async def delete(self) -> None:
            names.append(self.name)

    monkeypatch.setattr(sandbox, "DockerVolume", Volume)
    return names


def made(gone: list[str], name: str, **labels: str) -> Container:
    return Container(name, {"aims.managed": "1", "aims.host": "h", **labels}, gone)


async def test_the_sweep_removes_what_an_earlier_process_with_this_pid_left() -> None:
    gone: list[str] = []
    found = [
        made(gone, "mine", **{"aims.pid": ME, "aims.instance": INSTANCE}),
        made(gone, "earlier", **{"aims.pid": ME, "aims.instance": EARLIER}),
        made(gone, "older-version", **{"aims.pid": ME}),
        made(gone, "live-neighbour", **{"aims.pid": OTHER, "aims.instance": "1" * 32}),
        made(gone, "kept", **{"aims.pid": ME, "aims.instance": "2" * 32, "aims.keep": "1"}),
    ]
    docker: Any = Docker(found)
    await _sweep(docker, "h", None)
    assert gone == ["earlier"]


async def test_the_sweep_removes_what_a_dead_process_left(monkeypatch: pytest.MonkeyPatch) -> None:
    gone: list[str] = []
    labels = {"aims.managed": "1", "aims.host": "h", "aims.pid": "4194000"}
    monkeypatch.setattr(sandbox, "_alive", lambda pid: pid != 4194000)
    docker: Any = Docker([Container("dead", labels, gone)])
    await _sweep(docker, "h", None)
    assert gone == ["dead"]


async def test_a_sweep_in_a_container_judges_pids_only_in_that_container() -> None:
    gone: list[str] = []
    found = [
        made(gone, "mine", **{"aims.pid": ME, "aims.instance": INSTANCE, "aims.container": MINE}),
        made(gone, "earlier", **{"aims.pid": ME, "aims.instance": EARLIER, "aims.container": MINE}),
        made(gone, "neighbour", **{"aims.pid": OTHER, "aims.container": MINE}),
        made(gone, "native", **{"aims.pid": ME, "aims.instance": EARLIER}),
        made(gone, "gone", **{"aims.pid": "1", "aims.container": GONE}),
        made(gone, "gone-too", **{"aims.pid": "1", "aims.container": GONE}),
        made(gone, "stopped", **{"aims.pid": "1", "aims.container": STOPPED}),
        made(gone, "live", **{"aims.pid": "1", "aims.container": LIVE}),
        made(gone, "unknown", **{"aims.pid": "1", "aims.container": UNKNOWN}),
        made(gone, "kept", **{"aims.pid": "1", "aims.container": GONE, "aims.keep": "1"}),
    ]
    docker: Any = Docker(found)
    await _sweep(docker, "h", MINE)
    assert gone == ["earlier", "gone", "gone-too", "stopped"]
    assert docker.containers.asked == [GONE, STOPPED, LIVE, UNKNOWN]


async def test_a_sweep_outside_a_container_leaves_other_hosts_alone() -> None:
    gone: list[str] = []
    found = [
        made(gone, "earlier", **{"aims.pid": ME, "aims.instance": EARLIER}),
        made(gone, "elsewhere", **{"aims.pid": ME, "aims.instance": EARLIER, "aims.host": "x"}),
        made(gone, "in-gone", **{"aims.pid": ME, "aims.instance": EARLIER, "aims.container": GONE}),
        made(gone, "in-live", **{"aims.pid": ME, "aims.instance": EARLIER, "aims.container": LIVE}),
    ]
    docker: Any = Docker(found)
    await _sweep(docker, "h", None)
    assert gone == ["earlier", "in-gone"]


def mountinfo(root: str, target: str = "/etc/hostname") -> str:
    return f"148 139 254:1 {root} {target} rw,relatime - ext4 /dev/vda1 rw,discard\n"


@pytest.mark.parametrize(
    ("root", "target", "expected"),
    [
        (f"/docker/containers/{MINE}/hostname", "/etc/hostname", MINE),
        (f"/var/lib/docker/containers/{MINE}/hostname", "/etc/hostname", MINE),
        (f"/containers/{MINE}/hostname", "/etc/hostname", MINE),
        (f"/home/u/.local/share/docker/containers/{MINE}/hostname", "/etc/hostname", MINE),
        (
            f"/var/lib/containers/storage/overlay-containers/{MINE}/userdata/hostname",
            "/etc/hostname",
            MINE,
        ),
        (f"/docker/containers/{MINE}/hosts", "/etc/hosts", None),
        (f"/docker/containers/{MINE[:12]}/hostname", "/etc/hostname", None),
        ("/var/lib/kubelet/pods/0f2c/etc-hosts", "/etc/hosts", None),
        ("/", "/", None),
    ],
)
def test_the_container_id_comes_from_the_hostname_mount(
    root: str, target: str, expected: str | None
) -> None:
    text = mountinfo("/", "/") + mountinfo(root, target)
    assert _container_id(text) == expected


@pytest.mark.parametrize(("container", "expected"), [(MINE, MINE), (GONE, None), (UNKNOWN, None)])
async def test_a_container_id_counts_only_if_the_daemon_runs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: str, expected: str | None
) -> None:
    path = tmp_path / "mountinfo"
    path.write_text(mountinfo(f"/docker/containers/{container}/hostname"))
    monkeypatch.setattr(sandbox, "MOUNTINFO", str(path))
    docker: Any = Docker([])
    assert await _own_container(docker) == expected


async def test_no_mount_table_means_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "MOUNTINFO", str(tmp_path / "missing"))
    docker: Any = Docker([])
    assert await _own_container(docker) is None
    assert docker.containers.asked == []


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
@pytest.mark.parametrize("network", ["none", "host"])
async def test_a_container_finds_its_own_id(
    docker: aiodocker.Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: str
) -> None:
    image = await ensure_image(docker, "toolbox")
    container = await docker.containers.create(
        config={
            "Image": image,
            "Cmd": ["sh", "-c", "cat /proc/self/mountinfo; sleep 60"],
            "Labels": {"aims.test": "orphans"},
            "HostConfig": {"NetworkMode": network},
        }
    )
    try:
        await container.start()
        text = ""
        for _ in range(50):
            text = "".join(await container.log(stdout=True))
            if "/etc/hostname" in text:
                break
            await asyncio.sleep(0.1)
        assert _container_id(text) == container.id
        path = tmp_path / "mountinfo"
        path.write_text(text)
        monkeypatch.setattr(sandbox, "MOUNTINFO", str(path))
        assert await _own_container(docker) == container.id
        await container.kill()
        assert await _own_container(docker) is None
    finally:
        await container.delete(force=True)


@pytest.mark.docker
async def test_the_sweep_removes_what_a_stopped_container_made(docker: aiodocker.Docker) -> None:
    image = await ensure_image(docker, "toolbox")

    async def create(**labels: str) -> Any:
        return await docker.containers.create(
            config={
                "Image": image,
                "Cmd": ["sleep", "60"],
                "Labels": {"aims.test": "orphans", **labels},
                "HostConfig": {"NetworkMode": "none"},
            }
        )

    live, dead = await create(), await create()
    children: list[Any] = []
    try:
        await live.start()
        await dead.start()
        await dead.kill()
        for parent in (live, dead):
            children.append(
                await create(
                    **{
                        "aims.managed": "1",
                        "aims.host": "elsewhere",
                        "aims.pid": "1",
                        "aims.instance": EARLIER,
                        "aims.container": parent.id,
                    }
                )
            )
        await _sweep(docker, socket.gethostname(), None)
        await docker.containers.get(children[0].id)
        with pytest.raises(aiodocker.DockerError) as e:
            await docker.containers.get(children[1].id)
        assert e.value.status == 404
    finally:
        for container in (live, dead, *children):
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)


@pytest.mark.docker
async def test_a_backend_in_a_container_labels_and_sweeps_as_that_container(
    docker: aiodocker.Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = await ensure_image(docker, "toolbox")
    # A running stand-in, so sweeps in other test workers leave this backend's sampler alone.
    parent = await docker.containers.run(
        config={
            "Image": image,
            "Cmd": ["sleep", "60"],
            "Labels": {"aims.test": "orphans"},
            "HostConfig": {"NetworkMode": "none"},
        }
    )

    async def own(_: Any) -> str:
        return parent.id

    monkeypatch.setattr(sandbox, "_own_container", own)
    labels = {
        "aims.managed": "1",
        "aims.host": socket.gethostname(),
        "aims.pid": ME,
        "aims.instance": EARLIER,
    }
    native = await docker.containers.create(config={"Image": image, "Labels": labels})
    stale = f"aims-toolbox-{uuid.uuid4().hex[:12]}"
    await docker.volumes.create({"Name": stale, "Labels": {"aims.managed": "1"}})
    try:
        async with await sandbox.DockerBackend.open(audit="snapshot"):
            query = {"label": [f"aims.container={parent.id}", f"aims.instance={INSTANCE}"]}
            mine = await docker.containers.list(filters=json.dumps(query))
            assert [c["Labels"]["aims.role"] for c in mine] == ["sampler"]
        await docker.containers.get(native.id)
        with pytest.raises(aiodocker.DockerError):
            await sandbox.DockerVolume(docker, stale).show()
    finally:
        for container in (native, parent):
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)
        with contextlib.suppress(aiodocker.DockerError):
            await sandbox.DockerVolume(docker, stale).delete()


async def test_the_sweep_removes_volumes_by_the_same_rule_after_containers(
    deleted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_alive", lambda pid: pid != 4194000)

    def volume(name: str, **labels: str) -> dict[str, Any]:
        return {"Name": name, "Labels": {"aims.managed": "1", "aims.host": "h", **labels}}

    volumes = [
        volume("dead", **{"aims.pid": "4194000"}),
        volume("live", **{"aims.pid": OTHER}),
        volume("earlier", **{"aims.pid": ME, "aims.instance": EARLIER}),
        volume("elsewhere", **{"aims.pid": "4194000", "aims.host": "x"}),
        volume("in-gone", **{"aims.pid": "1", "aims.container": GONE}),
        volume("in-live", **{"aims.pid": "1", "aims.container": LIVE}),
        volume("kept", **{"aims.pid": "4194000", "aims.keep": "1"}),
        {"Name": "aims-toolbox-0123456789ab", "Labels": {"aims.managed": "1"}},
        {"Name": "unlabelled", "Labels": None},
    ]
    found = [made(deleted, "container", **{"aims.pid": "1", "aims.container": GONE})]
    docker: Any = Docker(found, volumes)
    await _sweep(docker, "h", None)
    assert deleted == ["container", "dead", "earlier", "in-gone"]
    assert docker.containers.asked == [GONE, LIVE]


async def test_a_helper_carries_the_labels_of_its_maker() -> None:
    configs: list[dict[str, Any]] = []

    class Helper:
        async def start(self) -> None: ...

        async def wait(self) -> dict[str, int]:
            return {"StatusCode": 0}

        async def delete(self, force: bool = False) -> None: ...

    class Creator:
        async def create(self, config: dict[str, Any], **_: Any) -> Helper:
            configs.append(config)
            return Helper()

    class Client:
        containers = Creator()

    docker: Any = Client()
    labels = {"aims.managed": "1", "aims.pid": ME, "aims.instance": INSTANCE}
    await sandbox._helper(docker, "image", ["true"], [], labels=labels, caps=[])
    assert configs[0]["Labels"] == {**labels, "aims.role": "helper"}


class Images:
    def __init__(self, found: Sequence[dict[str, Any]], tagged: dict[str, str]) -> None:
        self.found = found
        self.tagged = tagged
        self.deleted: list[str] = []
        self.asked: list[str] = []

    async def list(self, **_: Any) -> Sequence[dict[str, Any]]:
        return self.found

    async def inspect(self, image: str) -> dict[str, Any]:
        self.asked.append(image)
        if image not in self.tagged:
            raise aiodocker.DockerError(404, "No such image")
        if self.tagged[image] == "error":
            raise aiodocker.DockerError(500, "daemon busy")
        return {"Metadata": {"LastTagTime": self.tagged[image]}}

    async def delete(self, ref: str) -> None:
        self.deleted.append(ref)


class Used:
    def __init__(self, images: Sequence[str]) -> None:
        self.images = images

    async def list(self, **_: Any) -> Sequence[dict[str, str]]:
        return [{"ImageID": image} for image in self.images]


async def test_old_asset_images_go_once_unused_for_a_day() -> None:
    """Another checkout or install of AIMS on this host may still use them. One never tagged
    here, as `docker load` leaves it, stays, as a scenario image does."""
    current = context("tracer")[0]
    old = "2026-01-01T00:00:00Z"
    now = datetime.now(UTC)
    found = [
        {
            "Id": "current",
            "RepoTags": [current, f"{current}-used"],
            "RepoDigests": ["aims-tracer@sha256:c"],
        },
        {
            "Id": "old",
            "RepoTags": ["aims-tracer:000000000000", "aims-tracer:000000000000-used"],
            "RepoDigests": ["ghcr.io/kaumnen/aims-tracer@sha256:o", "aims-tracer@sha256:o"],
        },
        {"Id": "shared", "RepoTags": ["aims-toolbox:111111111111", "mine/other:1"]},
        {"Id": "untagged", "RepoTags": None, "RepoDigests": ["127.0.0.1:5000/aims-toolbox@sha"]},
        {"Id": "never-tagged", "RepoTags": ["aims-tracer:666666666666"]},
        {"Id": "used", "RepoTags": ["aims-tracer:222222222222"]},
        {"Id": "fresh", "RepoTags": ["aims-tracer:333333333333"]},
        {"Id": "today", "RepoTags": ["aims-toolbox:777777777777"]},
        {"Id": "yesterday", "RepoTags": ["aims-toolbox:888888888888"]},
        {"Id": "unknown", "RepoTags": ["aims-tracer:444444444444"]},
        {"Id": "renamed", "RepoTags": [f"localhost:5000/keep/{current}"]},
        {"Id": "renamed-old", "RepoTags": ["keep/aims-tracer:555555555555"]},
    ]
    tagged = {str(i["Id"]): old for i in found} | {
        "never-tagged": "0001-01-01T00:00:00Z",
        "fresh": now.isoformat(),
        "today": (now - timedelta(hours=23)).isoformat(),
        "yesterday": (now - timedelta(hours=25)).isoformat(),
        "unknown": "error",
    }
    images = Images(found, tagged)

    class Client:
        containers = Used(["used"])

    docker: Any = Client()
    docker.images = images
    await remove_old(docker)
    assert images.deleted == [
        "aims-tracer:000000000000",
        "aims-tracer:000000000000-used",
        "ghcr.io/kaumnen/aims-tracer@sha256:o",
        "aims-tracer@sha256:o",
        "aims-toolbox:111111111111",
        "127.0.0.1:5000/aims-toolbox@sha",
        "aims-toolbox:888888888888",
        "keep/aims-tracer:555555555555",
    ]


class Refusing(Images):
    """Images whose deletes of `refused` fail, as for an image a new container uses."""

    refused = frozenset({"aims-env:6666666666666666"})

    async def delete(self, ref: str) -> None:
        if ref in self.refused:
            raise aiodocker.DockerError(409, "image is being used by a container")
        await super().delete(ref)


async def test_scenario_images_go_once_unused_for_keep_days() -> None:
    now = datetime.now(UTC)
    found = [
        {
            "Id": "stale",
            "RepoTags": ["aims-env:0000000000000000", "aims-env:0000000000000000-used"],
        },
        {"Id": "month", "RepoTags": ["aims-env:1111111111111111"]},
        {"Id": "fresh", "RepoTags": ["aims-env:2222222222222222"]},
        {"Id": "used", "RepoTags": ["aims-env:3333333333333333"]},
        {"Id": "loaded", "RepoTags": ["aims-env:4444444444444444"]},
        {"Id": "unknown", "RepoTags": ["aims-env:5555555555555555"]},
        {"Id": "shared", "RepoTags": ["mine/env:1", "aims-env:6666666666666666"]},
    ]
    tagged = {str(i["Id"]): (now - timedelta(days=31)).isoformat() for i in found} | {
        "month": (now - timedelta(days=29)).isoformat(),
        "fresh": (now - timedelta(seconds=MIN_AGE_S / 2)).isoformat(),
        "loaded": "0001-01-01T00:00:00Z",
        "unknown": "error",
    }
    images = Refusing(found, tagged)

    class Client:
        containers = Used(["used"])

    docker: Any = Client()
    docker.images = images
    gone = ["aims-env:0000000000000000", "aims-env:0000000000000000-used"]
    assert await remove_old(docker, dry_run=True) == [*gone, "aims-env:6666666666666666"]
    assert images.deleted == []
    assert await remove_old(docker) == gone
    assert images.deleted == gone
    assert await remove_old(docker, 0, dry_run=True) == [
        *gone,
        "aims-env:1111111111111111",
        "aims-env:6666666666666666",
    ]


async def test_untagged_images_go_unless_used_or_just_untagged() -> None:
    """Images a cut build or a second build of a tag left with no name. On Docker's containerd
    image store LastTagTime is when the last tag went."""
    now = datetime.now(UTC)
    found = [
        {"Id": "old", "RepoTags": None, "RepoDigests": None},
        {"Id": "empty", "RepoTags": [], "RepoDigests": []},
        {"Id": "fresh", "RepoTags": []},
        {"Id": "never", "RepoTags": []},
        {"Id": "unknown", "RepoTags": []},
        {"Id": "used", "RepoTags": []},
        {"Id": "digest", "RepoTags": [], "RepoDigests": ["mine/other@sha256:d"]},
    ]
    tagged = {str(i["Id"]): (now - timedelta(seconds=MIN_AGE_S * 2)).isoformat() for i in found}
    tagged |= {
        "fresh": (now - timedelta(seconds=MIN_AGE_S / 2)).isoformat(),
        "never": "0001-01-01T00:00:00Z",
        "unknown": "error",
    }
    images = Images(found, tagged)

    class Client:
        containers = Used(["used"])

    docker: Any = Client()
    docker.images = images
    assert await remove_old(docker, dry_run=True) == ["old", "empty"]
    assert images.deleted == []
    assert await remove_old(docker) == ["old", "empty"]
    assert images.deleted == ["old", "empty"]


async def test_a_toolbox_volume_goes_with_its_image(
    deleted: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An old build lock file goes in the same cleanup."""

    async def nothing(*_: Any) -> list[str]:
        return []

    monkeypatch.setattr(sandbox, "remove_old", nothing)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    lock = assets._lock_path("aims-env:old")
    lock.touch()
    then = time.time() - 2 * 86400
    os.utime(lock, (then, then))
    names = [
        "aims-toolbox-000000000000",
        "aims-toolbox-aaaaaaaaaaaa",
        "aims-toolbox-bbbbbbbbbbbb",
        "aims-toolbox-cccccccccccc",
        "aims-0190-a-work",
    ]
    old = "2026-01-01T00:00:00Z"
    images = Images([], {"aims-toolbox:aaaaaaaaaaaa": old, "aims-toolbox:cccccccccccc": "error"})

    class Client:
        volumes = Volumes([{"Name": name, "Labels": {"aims.managed": "1"}} for name in names])

    docker: Any = Client()
    docker.images = images
    await sandbox._remove_old(docker, "aims-toolbox-000000000000", 30)
    assert deleted == ["aims-toolbox-bbbbbbbbbbbb"]
    assert os.listdir(tmp_path) == []


@pytest.mark.docker
async def test_the_sweep_removes_the_volumes_a_stopped_container_made_unless_in_use(
    docker: aiodocker.Docker,
) -> None:
    image = await ensure_image(docker, "toolbox")
    live = await docker.containers.run(
        config={"Image": image, "Cmd": ["sleep", "60"], "Labels": {"aims.test": "orphans"}}
    )
    dead = await docker.containers.run(
        config={"Image": image, "Cmd": ["sleep", "60"], "Labels": {"aims.test": "orphans"}}
    )
    await dead.kill()
    tag = uuid.uuid4().hex[:12]
    names = {k: f"aims-test-{tag}-{k}" for k in ("live", "dead", "mounted")}
    owners = {"live": live.id, "dead": dead.id, "mounted": dead.id}
    for key, name in names.items():
        labels = {"aims.managed": "1", "aims.pid": "1", "aims.container": owners[key]}
        await docker.volumes.create({"Name": name, "Labels": labels})
    kept = await docker.containers.create(
        config={
            "Image": image,
            "Labels": {"aims.test": "orphans", "aims.keep": "1"},
            "HostConfig": {
                "Mounts": [{"Type": "volume", "Source": names["mounted"], "Target": "/v"}]
            },
        }
    )
    try:
        await _sweep(docker, socket.gethostname(), None)
        listing = await docker.volumes.list(filters={"name": [f"aims-test-{tag}-"]})
        left = {v["Name"] for v in listing.get("Volumes") or []}
        assert left == {names["live"], names["mounted"]}
    finally:
        for container in (kept, live, dead):
            with contextlib.suppress(aiodocker.DockerError):
                await container.delete(force=True)
        for name in names.values():
            with contextlib.suppress(aiodocker.DockerError):
                await sandbox.DockerVolume(docker, name).delete()


def only(
    monkeypatch: pytest.MonkeyPatch, docker: aiodocker.Docker, refs: set[str], volumes: set[str]
) -> None:
    """Show `_remove_old` only the images that carry one of `refs` and the volumes named in
    `volumes`, the test's own. The others on this daemon are other checkouts' and other
    tests', which the test's loosened ages must not reach."""
    images, listing = docker.images.list, docker.volumes.list

    async def my_images(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            image
            for image in await images(**kwargs)
            if {*(image.get("RepoTags") or []), *(image.get("RepoDigests") or [])} & refs
        ]

    async def my_volumes(**kwargs: Any) -> dict[str, Any]:
        found = await listing(**kwargs)
        return {**found, "Volumes": [v for v in found.get("Volumes") or [] if v["Name"] in volumes]}

    monkeypatch.setattr(docker.images, "list", my_images)
    monkeypatch.setattr(docker.volumes, "list", my_volumes)


@pytest.mark.docker
async def test_an_old_asset_image_and_its_toolbox_volume_go_once_unused(
    docker: aiodocker.Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolbox = (await ensure_image(docker, "toolbox")).replace(":", "-")
    marker = uuid.uuid4().hex
    old, other, gone = marker[:12], marker[12:24], marker[24:]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in [
            ("Dockerfile", b"FROM scratch\nCOPY marker /marker\n"),
            ("marker", marker.encode()),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    await docker.images.build(
        fileobj=io.BytesIO(gzip.compress(buf.getvalue(), mtime=0)),
        encoding="gzip",
        tag=f"aims-tracer:{old}",
        labels={"aims.managed": "1"},
    )
    await docker.images.tag(f"aims-tracer:{old}", "aims-toolbox", tag=other)
    volumes = {f"aims-toolbox-{other}", f"aims-toolbox-{gone}"}
    for name in volumes:
        await docker.volumes.create({"Name": name, "Labels": {"aims.managed": "1"}})

    async def present() -> tuple[set[str], set[str]]:
        tags = set()
        for ref in (f"aims-tracer:{old}", f"aims-toolbox:{other}"):
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.inspect(ref)
                tags.add(ref)
        listing = await docker.volumes.list(filters={"name": ["aims-toolbox-"]})
        return tags, {v["Name"] for v in listing.get("Volumes") or []} & volumes

    both = {f"aims-tracer:{old}", f"aims-toolbox:{other}"}
    only(monkeypatch, docker, both, volumes)
    user: Any = None
    try:
        await sandbox._remove_old(docker, toolbox, 30)
        assert await present() == (both, {f"aims-toolbox-{other}"})

        monkeypatch.setattr(assets, "MIN_AGE_S", 0)
        await sandbox._remove_old(docker, toolbox, 30)
        assert await present() == (both, {f"aims-toolbox-{other}"})

        monkeypatch.setattr(assets, "ASSET_KEEP_DAYS", 0)
        user = await docker.containers.create(
            config={"Image": f"aims-tracer:{old}", "Cmd": ["/marker"]}
        )
        await sandbox._remove_old(docker, toolbox, 30)
        assert await present() == (both, {f"aims-toolbox-{other}"})

        await user.delete(force=True)
        await sandbox._remove_old(docker, toolbox, 30)
        assert await present() == (set(), set())
    finally:
        if user is not None:
            with contextlib.suppress(aiodocker.DockerError):
                await user.delete(force=True)
        for ref in both:
            with contextlib.suppress(aiodocker.DockerError):
                await docker.images.delete(ref, force=True)
        for name in volumes:
            with contextlib.suppress(aiodocker.DockerError):
                await sandbox.DockerVolume(docker, name).delete()


@pytest.mark.docker
async def test_the_helpers_of_a_backend_carry_its_labels(
    docker: aiodocker.Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []
    helper = sandbox._helper

    async def recording(*args: Any, labels: dict[str, str], **kwargs: Any) -> None:
        seen.append(labels)
        await helper(*args, labels=labels, **kwargs)

    monkeypatch.setattr(sandbox, "_helper", recording)
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    async with await sandbox.DockerBackend.open(audit="snapshot") as backend:
        box = await backend.create(
            SandboxSpec(image="python:3.13-slim"), run_id=run_id, agent_id="a"
        )
        try:
            await box.close()
        finally:
            await backend.remove_run(run_id)
    assert len(seen) == 2
    assert all(s["aims.pid"] == ME and s["aims.instance"] == INSTANCE for s in seen)


class Helper:
    """A tracer or sampler that notes its stop in `done`. With `hold` its stop waits for
    `release`, so a test can cancel the caller meanwhile."""

    def __init__(self, name: str, done: list[str], *, hold: bool = False, fail: bool = False):
        self.name = name
        self.done = done
        self.hold = hold
        self.fail = fail
        self.starting = asyncio.Event()
        self.stopping = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        self.starting.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopping.set()
        if self.hold:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("the daemon went away")
        self.done.append(self.name)


class Client:
    def __init__(self, done: list[str]) -> None:
        self.done = done

    async def close(self) -> None:
        self.done.append("docker")


def backend(tracer: Helper, sampler: Helper, done: list[str]) -> sandbox.DockerBackend:
    docker: Any = Client(done)
    helpers: Any = (tracer, sampler)
    return sandbox.DockerBackend(
        docker,
        arch="arm64",
        labels={},
        toolbox=("toolbox", "volume"),
        tracer=helpers[0],
        sampler=helpers[1],
    )


async def test_cancels_while_the_backend_closes_still_remove_both_helpers() -> None:
    """Ctrl-C again lands while the tracer is being removed: the sampler must still go and
    the Docker client close."""
    done: list[str] = []
    tracer, sampler = Helper("tracer", done, hold=True), Helper("sampler", done)
    closing = asyncio.create_task(backend(tracer, sampler, done).close())
    await tracer.stopping.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    tracer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert done == ["tracer", "sampler", "docker"]


async def test_a_tracer_that_fails_to_stop_still_lets_the_sampler_go() -> None:
    done: list[str] = []
    closing = backend(Helper("tracer", done, fail=True), Helper("sampler", done), done)
    with pytest.raises(RuntimeError):
        await closing.close()
    assert done == ["sampler", "docker"]


async def test_cancels_while_the_backend_opens_still_remove_both_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C lands while the tracer starts, then again while it is removed."""
    done: list[str] = []
    tracer, sampler = Helper("tracer", done, hold=True), Helper("sampler", done)

    class System:
        async def info(self) -> dict[str, str]:
            return {"Architecture": "aarch64"}

    class Opened(Client):
        def __init__(self, url: str | None = None) -> None:
            super().__init__(done)
            self.system = System()

    async def nothing(*_: Any) -> None:
        return None

    async def toolbox(*_: Any) -> tuple[str, str]:
        return ("toolbox", "volume")

    async def started(*_: Any) -> Helper:
        return sampler

    monkeypatch.setattr(sandbox.aiodocker, "Docker", Opened)
    monkeypatch.setattr(sandbox, "_own_container", nothing)
    monkeypatch.setattr(sandbox, "_sweep", nothing)
    monkeypatch.setattr(sandbox, "_toolbox", toolbox)
    monkeypatch.setattr(sandbox, "_remove_old", nothing)
    monkeypatch.setattr(sandbox, "_sampler", started)
    monkeypatch.setattr(sandbox, "Tracer", lambda *_, **__: tracer)
    opening = asyncio.create_task(sandbox.DockerBackend.open(audit="auto"))
    await tracer.starting.wait()
    opening.cancel()
    await tracer.stopping.wait()
    opening.cancel()
    tracer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert done == ["tracer", "sampler", "docker"]


class HeldContainer:
    """A container whose delete waits for `release`, so a test can cancel the close meanwhile."""

    def __init__(self) -> None:
        self.deleting = asyncio.Event()
        self.release = asyncio.Event()
        self.gone = False

    async def delete(self, force: bool = False) -> None:
        self.deleting.set()
        await self.release.wait()
        self.gone = True


async def test_cancels_while_a_sandbox_closes_still_remove_its_volumes(deleted: list[str]) -> None:
    """Ctrl-C twice lands while the container is removed, as when a start is cut short: its
    volumes must still go."""

    class Backend:
        tracer = None
        docker = None

    container = HeldContainer()
    owner: Any = Backend()
    held: Any = container
    info = SandboxInfo("sb", "docker", "img", "sha256:" + "0" * 64, {})
    box = sandbox.DockerSandbox(
        owner,
        held,
        SandboxSpec(image="img"),
        info,
        "work",
        [],
        None,
        scratch=("tmp", "home"),
        shared=("shared",),
    )
    closing = asyncio.create_task(box.close())
    await container.deleting.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    container.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert container.gone
    assert deleted == ["tmp", "home", "work", "shared"]
