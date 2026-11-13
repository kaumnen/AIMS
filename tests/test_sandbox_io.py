import asyncio
import contextlib
import io
import json
import os
import tarfile
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiodocker.stream
import pytest
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec
from aiodocker.stream import Stream
from aiodocker.volumes import DockerVolume

from aims.aio import shielded
from aims.events import ExecStarted, LocalEventStore, RunStarted, SandboxArchived
from aims.sandbox import (
    AGENT_UID,
    RUNNER_UID,
    AuditedSandbox,
    DockerBackend,
    DockerSandbox,
    Mount,
    SandboxError,
    SandboxSpec,
)
from aims.sandbox import docker as docker_module
from aims.sandbox.base import RUNNER_DIR, TOOLBOX, Capture
from aims.sandbox.docker import INSTANCE, VOLUME_S
from aims.sandbox.execs import open_stream
from aims.sandbox.shared import SharedFolder

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

IMAGE = "python:3.13-slim"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        audit = "kernel" if os.environ.get("AIMS_REQUIRE_TRACER") else "auto"
        backend = await DockerBackend.open(audit=audit)
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        yield backend


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def sandbox(
    backend: DockerBackend, store: LocalEventStore, **spec: Any
) -> tuple[str, AuditedSandbox]:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe", agents=["a1"]))
    spec = {"image": IMAGE, **spec}
    audited = await AuditedSandbox.start(
        backend, SandboxSpec(**spec), store, run_id=run_id, agent_id="a1"
    )
    return run_id, audited


async def test_stdin_reaches_the_command(backend: DockerBackend, store: LocalEventStore) -> None:
    run_id, sb = await sandbox(backend, store)
    big = os.urandom(3 * 1024 * 1024)
    try:
        small = await sb.exec("cat > /work/f && cat /work/f", timeout_s=10, stdin=b"hello")
        large = await sb.exec("cat | wc -c", timeout_s=20, stdin=big)
        early = await sb.exec("exit 3", timeout_s=10, stdin=big)
    finally:
        await sb.close()

    assert small.stdout == b"hello"
    assert large.stdout.strip() == str(len(big)).encode()
    assert (early.finished.end, early.finished.exit_code) == ("exited", 3)
    started = [
        p
        for p in (e.payload() for e in await store.events(run_id, types=["exec.started"]))
        if isinstance(p, ExecStarted)
    ]
    assert started[0].stdin == "hello"
    assert started[0].stdin_bytes == 5
    assert started[1].stdin_bytes == len(big)
    assert (await store.verify(run_id)).ok


async def test_two_pauses_at_once_freeze_once(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        await asyncio.gather(inner.pause(), inner.pause())
        assert inner.paused
        await asyncio.gather(inner.resume(), inner.resume())
        assert not inner.paused
        assert (await sb.exec("echo ok", timeout_s=10)).stdout == b"ok\n"
    finally:
        await sb.close()


async def test_runner_dir_and_exec_identity(backend: DockerBackend, store: LocalEventStore) -> None:
    run_id, sb = await sandbox(backend, store, runner_mb=8)
    probe = f"id -u; id -g; echo $X; mkdir {RUNNER_DIR}/t && echo made"
    try:
        runner = await sb.exec(
            probe, timeout_s=10, uid=RUNNER_UID, gid=AGENT_UID, env={"X": "y"}, phase="verifier"
        )
        agent = await sb.exec(f"ls {RUNNER_DIR}; touch {RUNNER_DIR}/x", timeout_s=10)
    finally:
        await sb.close()

    assert runner.stdout == f"{RUNNER_UID}\n{AGENT_UID}\ny\nmade\n".encode()
    assert agent.finished.exit_code != 0
    assert b"Permission denied" in agent.stderr
    started = [
        p
        for p in (e.payload() for e in await store.events(run_id, types=["exec.started"]))
        if isinstance(p, ExecStarted)
    ]
    assert (started[0].user, started[0].phase, started[0].env) == (
        f"{RUNNER_UID}:{AGENT_UID}",
        "verifier",
        {"X": "y"},
    )
    assert (started[1].user, started[1].phase) == (str(AGENT_UID), "agent")


async def test_archive_keeps_work_tmp_and_home(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(
            "echo a > /work/a; mkdir -p /work/d && echo b > /work/d/b; echo t > /tmp/t; "
            "echo h > $HOME/h; ln /work/a /work/hard",
            timeout_s=10,
        )
        archived = await sb.archive(["/work", "/tmp", "/home/agent"])
        tiny = await sb.archive(["/work"], cap=2048)
    finally:
        await sb.close()

    assert isinstance(archived, SandboxArchived)
    assert archived.errors == []
    assert archived.archive is not None
    data = await store.get_blob(run_id, archived.archive.sha256)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
        assert {"work/a", "work/d/b", "tmp/t", "home/agent/h"} <= names
        member = tar.getmember("work/d/b")
        file = tar.extractfile(member)
        assert file is not None
        assert file.read() == b"b\n"
        hard = tar.getmember("work/hard")
        assert not hard.islnk() or hard.linkname.startswith("work/")
    assert tiny is not None
    assert tiny.truncated
    assert tiny.skipped > 0


async def test_archive_streams_past_a_file_too_big_to_keep(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(
            "truncate -s 300M /work/big; echo s > /work/small; echo t > /tmp/t", timeout_s=30
        )
        tracemalloc.start()
        try:
            archived = await sb.archive(["/work", "/tmp"])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    finally:
        await sb.close()

    assert peak < 32 * 1024 * 1024
    assert archived is not None
    assert (archived.skipped, archived.truncated, archived.errors) == (1, True, [])
    assert archived.archive is not None
    data = await store.get_blob(run_id, archived.archive.sha256)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
        assert {"work/small", "tmp/t"} <= names
        assert "work/big" not in names
        small = tar.extractfile("work/small")
        assert small is not None
        assert small.read() == b"s\n"


async def test_a_shared_folder_archive_stops_at_a_file_too_big_to_keep(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    mount = Mount(volume="shared", target="/shared")
    run_id, sb = await sandbox(backend, store, mounts=[mount])
    try:
        await sb.exec("truncate -s 300M /shared/big; echo s > /shared/small", timeout_s=30)
        view = await backend.volume(run_id, mount)
        try:
            folder = SharedFolder(
                store, view, run_id=run_id, channel="notes", path="/shared", writers=[]
            )
            tracemalloc.start()
            try:
                archive = await folder.close()
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        finally:
            await view.close()
    finally:
        await sb.close()

    assert peak < 32 * 1024 * 1024
    assert archive is None
    lost = await store.events(run_id, types=["audit.lost"])
    assert [e.data["reason"] for e in lost] == [
        "channel notes not archived: its files are over 256 MiB"
    ]


async def test_built_image_is_reused(backend: DockerBackend, store: LocalEventStore) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dockerfile = f"FROM {IMAGE}\nRUN echo built > /built\n".encode()
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(dockerfile)
        tar.addfile(info, io.BytesIO(dockerfile))
    context = buf.getvalue()
    tag = await backend.build(context)
    assert tag == await backend.build(context)
    assert tag.startswith("aims-env:")
    _, sb = await sandbox(backend, store, image=tag)
    try:
        result = await sb.exec("cat /built", timeout_s=10)
    finally:
        await sb.close()
    assert result.stdout == b"built\n"


async def leftovers(backend: DockerBackend, run_id: str) -> tuple[set[str], set[str]]:
    """The containers this process made, and the volumes of run `run_id`, labelled or not."""
    query = {"label": [f"aims.instance={INSTANCE}"]}
    found = await backend.docker.containers.list(all=True, filters=json.dumps(query))
    listing = await backend.docker.volumes.list(filters={"name": [f"aims-{run_id}-"]})
    return {c.id for c in found}, {v["Name"] for v in listing.get("Volumes") or []}


async def scrub(backend: DockerBackend, run_id: str) -> None:
    """Remove what a failed test left of run `run_id`, labelled or not."""
    await backend.remove_run(run_id)
    listing = await backend.docker.volumes.list(filters={"name": [f"aims-{run_id}-"]})
    for volume in listing.get("Volumes") or []:
        with contextlib.suppress(aiodocker.DockerError):
            await DockerVolume(backend.docker, volume["Name"]).delete()


@dataclass
class Hold:
    """Where a create stopped. A held Docker call goes on in the daemon whatever its caller
    does, and ends once `release` is set."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[asyncio.Future[Any]] = field(default_factory=list)

    def call(self, work: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
        """Send `work` to the daemon: it runs once released, even if the caller is cancelled."""

        async def daemon() -> Any:
            await self.release.wait()
            return await work()

        self.calls.append(asyncio.ensure_future(daemon()))
        self.reached.set()
        return shielded(self.calls[-1])

    async def settled(self) -> None:
        """Release the daemon and wait for the calls it still carries out."""
        self.release.set()
        await asyncio.gather(*self.calls, return_exceptions=True)


def hold_create(
    backend: DockerBackend, run_id: str, where: str, monkeypatch: pytest.MonkeyPatch
) -> Hold:
    """Make the create of agent a1 of `run_id` stop once it reaches `where`: for good at the
    wait for the helper that fills the work volume or at the second tmpfs volume; or in the
    create call of that helper, of the sandbox container or of its reader, which the daemon
    carries out once released."""
    hold = Hold()

    async def stop() -> None:
        hold.reached.set()
        await asyncio.Event().wait()

    if where == "fill":
        wait, helpers = DockerContainer.wait, 0

        async def held_wait(self: DockerContainer, **kw: Any) -> Any:
            nonlocal helpers
            result = await wait(self, **kw)
            helpers += 1
            if helpers == 2:
                await stop()
            return result

        monkeypatch.setattr(DockerContainer, "wait", held_wait)
    elif where == "scratch":
        scratch, made = backend._scratch, 0

        async def held_scratch(*args: Any) -> None:
            nonlocal made
            await scratch(*args)
            made += 1
            if made == 2:
                await stop()

        monkeypatch.setattr(backend, "_scratch", held_scratch)
    else:
        create, helpers = backend.docker.containers.create, 0

        async def held_create(**kw: Any) -> DockerContainer:
            nonlocal helpers
            labels = kw["config"]["Labels"]
            role = labels.get("aims.role") if labels.get("aims.run") == run_id else "other"
            helpers += role == "helper"
            if (
                (where == "helper" and role == "helper" and helpers == 2)
                or (where == "create" and kw.get("name") == f"aims-{run_id}-a1")
                or (where == "reader" and role == "reader")
            ):
                return await hold.call(lambda: create(**kw))
            return await create(**kw)

        monkeypatch.setattr(backend.docker.containers, "create", held_create)
    return hold


SHARED = SandboxSpec(image=IMAGE, runner_mb=8, mounts=[Mount(volume="shared", target="/shared")])


@pytest.mark.parametrize(
    ("where", "keep"),
    [
        ("fill", False),
        ("helper", False),
        ("scratch", False),
        ("create", False),
        ("create", True),
        ("reader", True),
    ],
)
async def test_a_cancelled_create_removes_what_it_made(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch, where: str, keep: bool
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    hold = hold_create(backend, run_id, where, monkeypatch)
    spec = SHARED.model_copy(update={"keep": keep})
    task = asyncio.create_task(backend.create(spec, run_id=run_id, agent_id="a1"))
    try:
        async with asyncio.timeout(60):
            await hold.reached.wait()
        assert (await leftovers(backend, run_id))[1]
        task.cancel()
        # A cleanup that does not wait for the call in flight is over before the daemon makes
        # the container, which then mounts volumes the cleanup removed.
        await asyncio.wait({task}, timeout=1)
        hold.release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(60):
                await task
        await hold.settled()
        assert await leftovers(backend, run_id) == (before, set())
    finally:
        await hold.settled()
        await scrub(backend, run_id)


@pytest.mark.parametrize("volume", ["a1-work", "a1-tmp"])
async def test_a_volume_create_cut_by_a_cancel_lands_before_the_cleanup(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch, volume: str
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    hold = Hold()
    create = backend.docker.volumes.create

    async def held_create(config: dict[str, Any]) -> Any:
        if config["Name"] == f"aims-{run_id}-{volume}":
            return await hold.call(lambda: create(config))
        return await create(config)

    monkeypatch.setattr(backend.docker.volumes, "create", held_create)
    task = asyncio.create_task(backend.create(SHARED, run_id=run_id, agent_id="a1"))
    try:
        async with asyncio.timeout(60):
            await hold.reached.wait()
        task.cancel()
        await asyncio.wait({task}, timeout=1)
        waited = not task.done()
        hold.release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(60):
                await task
        await hold.settled()
        assert waited
        assert await leftovers(backend, run_id) == (before, set())
    finally:
        await hold.settled()
        await scrub(backend, run_id)


async def test_a_create_that_fails_removes_what_it_made(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any) -> DockerSandbox:
        raise SandboxError("sandbox probe failed")

    monkeypatch.setattr(DockerSandbox, "attach", broken)
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    with pytest.raises(SandboxError, match="probe failed"):
        async with asyncio.timeout(60):
            await backend.create(SHARED, run_id=run_id, agent_id="a1")
    assert await leftovers(backend, run_id) == (before, set())


async def test_a_second_cancel_while_a_create_cleans_up_still_removes_all(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    hold = hold_create(backend, run_id, "create", monkeypatch)
    cancelled, undoing, release = False, asyncio.Event(), asyncio.Event()
    delete = DockerVolume.delete

    async def held_delete(self: DockerVolume) -> None:
        if cancelled and not undoing.is_set():
            undoing.set()
            await release.wait()
        await delete(self)

    monkeypatch.setattr(DockerVolume, "delete", held_delete)
    task = asyncio.create_task(backend.create(SHARED, run_id=run_id, agent_id="a1"))
    async with asyncio.timeout(60):
        await hold.reached.wait()
    cancelled = True
    task.cancel()
    hold.release.set()
    waiter = asyncio.ensure_future(undoing.wait())
    await asyncio.wait({task, waiter}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if undoing.is_set():
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(60):
            await task
    assert await leftovers(backend, run_id) == (before, set())
    assert undoing.is_set()


async def test_a_cancel_while_a_helper_is_deleted_waits_for_the_delete(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    hold, helpers = Hold(), set[str]()
    create, delete = backend.docker.containers.create, DockerContainer.delete

    async def noted_create(**kw: Any) -> DockerContainer:
        container = await create(**kw)
        labels = kw["config"]["Labels"]
        if labels.get("aims.run") == run_id and labels.get("aims.role") == "helper":
            helpers.add(container.id)
        return container

    async def held_delete(self: DockerContainer, **kw: Any) -> None:
        if self.id in helpers and not hold.calls:
            await hold.call(lambda: delete(self, **kw))
        else:
            await delete(self, **kw)

    monkeypatch.setattr(backend.docker.containers, "create", noted_create)
    monkeypatch.setattr(DockerContainer, "delete", held_delete)
    task = asyncio.create_task(backend.create(SHARED, run_id=run_id, agent_id="a1"))
    try:
        async with asyncio.timeout(60):
            await hold.reached.wait()
        task.cancel()
        await asyncio.wait({task}, timeout=1)
        # The cleanup would find the helper's volumes still in use.
        assert not task.done()
        hold.release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(60):
                await task
        assert await leftovers(backend, run_id) == (before, set())
    finally:
        await hold.settled()
        await scrub(backend, run_id)


async def test_undo_waits_for_a_volume_only_while_its_container_goes(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    busy, free = f"aims-{run_id}-busy", f"aims-{run_id}-free"
    for name in (busy, free):
        await backend.docker.volumes.create({"Name": name, "Labels": {"aims.run": run_id}})
    other = await backend.docker.containers.create(
        config={
            "Image": IMAGE,
            "Cmd": ["true"],
            "Labels": {"aims.instance": INSTANCE},
            "HostConfig": {
                "NetworkMode": "none",
                "Mounts": [{"Type": "volume", "Source": busy, "Target": "/v"}],
            },
        }
    )
    delete, refused = DockerVolume.delete, 0

    async def going(self: DockerVolume) -> None:
        # As Docker answers while the container that mounted the volume is still going.
        nonlocal refused
        if self.name == free and refused < 2:
            refused += 1
            raise aiodocker.DockerError(409, f"remove {free}: volume is in use")
        await delete(self)

    monkeypatch.setattr(DockerVolume, "delete", going)
    loop = asyncio.get_running_loop()
    try:
        start = loop.time()
        await backend._undo({"aims.run": run_id}, None, [busy, free])
        took = loop.time() - start
        assert (await leftovers(backend, run_id))[1] == {busy}
        assert refused == 2
        assert took < VOLUME_S
    finally:
        with contextlib.suppress(aiodocker.DockerError):
            await other.delete(force=True)
        await scrub(backend, run_id)


async def test_a_put_archive_cancelled_in_its_create_call_leaves_no_helper(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    name = f"aims-{run_id}-shared"
    await backend.docker.volumes.create({"Name": name, "Labels": {"aims.run": run_id}})
    view = await backend.volume(run_id, Mount(volume="shared", target="/shared"))
    hold, create = Hold(), backend.docker.containers.create

    async def held_create(**kw: Any) -> DockerContainer:
        if kw["config"]["Labels"].get("aims.role") == "helper":
            return await hold.call(lambda: create(**kw))
        return await create(**kw)

    monkeypatch.setattr(backend.docker.containers, "create", held_create)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w"):
        pass
    try:
        before = await leftovers(backend, run_id)
        task = asyncio.create_task(view.put_archive(data.getvalue()))
        async with asyncio.timeout(60):
            await hold.reached.wait()
        task.cancel()
        await asyncio.wait({task}, timeout=1)
        hold.release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(60):
                await task
        await hold.settled()
        assert await leftovers(backend, run_id) == before
    finally:
        await hold.settled()
        await view.close()
        await scrub(backend, run_id)


async def test_a_reader_cancelled_before_its_delete_is_sent_still_goes(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    name = f"aims-{run_id}-shared"
    await backend.docker.volumes.create({"Name": name, "Labels": {"aims.run": run_id}})
    view = await backend.volume(run_id, Mount(volume="shared", target="/shared"))
    reached, release, delete = asyncio.Event(), asyncio.Event(), DockerContainer.delete

    async def slow_delete(self: DockerContainer, **kw: Any) -> None:
        # Stands for a slow connect: the request is not sent yet.
        reached.set()
        await release.wait()
        await delete(self, **kw)

    monkeypatch.setattr(DockerContainer, "delete", slow_delete)
    try:
        before, _ = await leftovers(backend, run_id)
        task = asyncio.create_task(view.close())
        async with asyncio.timeout(60):
            await reached.wait()
        task.cancel()
        await asyncio.wait({task}, timeout=1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(60):
                await task
        reader = view._reader._container.id
        assert reader in before
        assert (await leftovers(backend, run_id))[0] == before - {reader}
    finally:
        release.set()
        await scrub(backend, run_id)


class HeldTracer:
    """A live tracer whose confirm never answers."""

    alive = True
    clock = pmu = preemption = None

    def __init__(self) -> None:
        self.routes: list[object] = []
        self.confirming = asyncio.Event()

    def register(self, *_: Any) -> object:
        route = object()
        self.routes.append(route)
        return route

    def unregister(self, route: object) -> None:
        self.routes.remove(route)

    async def confirm(self, *_: Any) -> bool:
        self.confirming.set()
        await asyncio.Event().wait()
        return True


async def test_a_create_cancelled_as_the_tracer_confirms_unregisters_the_sandbox(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = HeldTracer()
    monkeypatch.setattr(backend, "tracer", tracer)
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    task = asyncio.create_task(backend.create(SHARED, run_id=run_id, agent_id="a1"))
    async with asyncio.timeout(60):
        await tracer.confirming.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(60):
            await task
    assert tracer.routes == []
    assert await leftovers(backend, run_id) == (before, set())


SLEEPS = "sleep 271 & sleep 272; echo never"


async def sleeps(sb: AuditedSandbox) -> list[str]:
    """The processes of `SLEEPS` still in the sandbox, its shell and wrapper among them."""
    return [p.command for p in await sb.sandbox.processes() if "sleep 27" in p.command]


async def sleeping(
    sb: AuditedSandbox, command: str = SLEEPS, timeout_s: float = 60
) -> asyncio.Task[Any]:
    """`command`, which runs `SLEEPS`, as an exec, once both sleeps are up."""
    task = asyncio.create_task(sb.exec(command, timeout_s=timeout_s))
    async with asyncio.timeout(30):
        while True:
            if {"sleep 271", "sleep 272"} <= set(await sleeps(sb)):
                return task
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("command", [SLEEPS, f"{SLEEPS} \ud800 é"])
async def test_a_cancelled_exec_ends_what_it_started(
    backend: DockerBackend, store: LocalEventStore, command: str
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        task = await sleeping(sb, command)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await task
        left = await sleeps(sb)
    finally:
        await sb.close()

    assert left == []
    finished = (await store.events(run_id, types=["exec.finished"]))[0].data
    assert (finished["end"], finished["leftover"]) == ("cancelled", [])


async def test_a_second_cancel_while_an_exec_ends_its_processes_lets_it_finish(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, sb = await sandbox(backend, store)
    cancelled, ending, release = False, asyncio.Event(), asyncio.Event()
    inspect = Exec.inspect

    async def held_inspect(self: Exec) -> dict[str, Any]:
        if cancelled and not ending.is_set():
            ending.set()
            await release.wait()
        return await inspect(self)

    monkeypatch.setattr(Exec, "inspect", held_inspect)
    try:
        task = await sleeping(sb)
        cancelled = True
        task.cancel()
        waiter = asyncio.ensure_future(ending.wait())
        await asyncio.wait({task, waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if ending.is_set():
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await task
        left = await sleeps(sb)
    finally:
        release.set()
        await sb.close()

    assert left == []
    assert ending.is_set()
    finished = (await store.events(run_id, types=["exec.finished"]))[0].data
    assert finished["end"] == "cancelled"


async def test_a_cancelled_exec_in_a_paused_sandbox_ends_at_once(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """A frozen sandbox cannot run the kill, so the exec stays frozen with it."""
    run_id, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        task = await sleeping(sb)
        await inner.pause()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(10):
                await task
        await inner.resume()
    finally:
        await sb.close()

    finished = (await store.events(run_id, types=["exec.finished"]))[0].data
    assert finished["end"] == "cancelled"
    assert finished["error"] == "its command may still run: the sandbox is paused"


async def test_an_exec_past_its_overall_deadline_is_ended(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command with no aims-exec around it has nothing in the sandbox that ends it."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    monkeypatch.setattr(docker_module, "DRAIN_S", 0.2)
    try:
        async with asyncio.timeout(30):
            outcome = await inner.run(
                [f"{TOOLBOX}/busybox", "sleep", "273"],
                uid=AGENT_UID,
                cwd="/",
                timeout_ms=200,
                capture=Capture(limit=1024),
            )
        left = [p.command for p in await inner.processes() if "sleep 273" in p.command]
    finally:
        await sb.close()

    assert left == []
    assert (outcome.end, outcome.exit_code, outcome.error) == ("timeout", 137, None)


async def test_an_exec_in_a_paused_sandbox_waits_for_the_resume(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """Docker refuses an exec create in a paused container. The exec waits, and the wait does
    not count against its timeout."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        await inner.pause()
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=1))
        await asyncio.sleep(2)
        assert not task.done()
        await inner.resume()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)
    assert result.stdout == b"ok\n"
    assert result.finished.duration_ms >= 2000


async def test_an_exec_paused_before_its_start_waits_for_the_resume(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker also refuses the start of an exec made before the pause."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    container = inner._container
    create = container.exec
    made = asyncio.Event()

    async def create_then_pause(*args: Any, **kw: Any) -> Exec:
        exe = await create(*args, **kw)
        monkeypatch.setattr(container, "exec", create)
        await inner.pause()
        made.set()
        return exe

    try:
        monkeypatch.setattr(container, "exec", create_then_pause)
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=1))
        async with asyncio.timeout(10):
            await made.wait()
        await asyncio.sleep(2)
        assert not task.done()
        await inner.resume()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)
    assert result.stdout == b"ok\n"


async def test_an_exec_waits_for_a_pause_docker_has_not_answered_yet(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker freezes the container before the pause call returns. An exec refused in that
    gap waits for the pause to land, then for the resume."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    container = inner._container
    pause = container.pause
    frozen, answer = asyncio.Event(), asyncio.Event()

    async def slow_pause() -> None:
        await pause()
        frozen.set()
        await answer.wait()

    try:
        monkeypatch.setattr(container, "pause", slow_pause)
        pausing = asyncio.create_task(inner.pause())
        async with asyncio.timeout(10):
            await frozen.wait()
        assert not inner.paused
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=10))
        await asyncio.sleep(0.5)
        assert not task.done()
        answer.set()
        await pausing
        await asyncio.sleep(0.5)
        assert inner.paused
        assert not task.done()
        await inner.resume()
        async with asyncio.timeout(10):
            result = await task
    finally:
        answer.set()
        await sb.close()

    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)


async def test_an_exec_waits_out_a_pause_made_outside_aims(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """With no pause of this sandbox to wait for, the exec waits until Docker says the
    container runs again. The wait does not count against its timeout, and a resume of the
    sandbox leaves the pause alone."""
    run_id, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    container = inner._container
    try:
        await container.pause()
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=1))
        await asyncio.sleep(2)
        assert inner.paused
        assert await sb.resume() is None
        await asyncio.sleep(0.5)
        still = (await container.show())["State"]["Paused"]
        assert (still, inner.paused, task.done()) == (True, True, False)
        await container.unpause()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert not inner.paused
    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)
    assert result.stdout == b"ok\n"
    assert result.finished.duration_ms >= 2500
    kinds = ["exec.started", "sandbox.outside_pause", "exec.finished"]
    logged = await store.events(run_id, types=kinds)
    assert [(e.type, e.data.get("state")) for e in logged] == [
        ("exec.started", None),
        ("sandbox.outside_pause", "paused"),
        ("sandbox.outside_pause", "running"),
        ("exec.finished", None),
    ]
    noticed, ended = logged[1].ts, logged[2].ts
    assert (ended - noticed).total_seconds() >= 1.5


async def test_an_exec_that_times_out_in_a_pause_made_outside_aims_says_so(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing tells AIMS of a pause that lands on a running exec. The exec's deadline runs
    on, and the kill at its end is what Docker refuses."""
    run_id, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    container = inner._container
    monkeypatch.setattr(docker_module, "DRAIN_S", 0.2)
    try:
        task = await sleeping(sb, timeout_s=3)
        await container.pause()
        async with asyncio.timeout(30):
            result = await task
        paused = inner.paused
        await container.unpause()
    finally:
        with contextlib.suppress(aiodocker.DockerError):
            await container.unpause()
        await sb.close()

    assert paused
    f = result.finished
    assert (f.end, f.error) == (
        "timeout",
        "its command may still run: the sandbox was paused outside AIMS",
    )
    states = [e.data["state"] for e in await store.events(run_id, types=["sandbox.outside_pause"])]
    assert states == ["paused"]


async def test_an_exec_paused_outside_aims_before_its_start_waits_too(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exec's deadline stops while it waits, so the wait is not its timeout."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    container = inner._container
    create = container.exec
    made = asyncio.Event()

    async def create_then_pause(*args: Any, **kw: Any) -> Exec:
        exe = await create(*args, **kw)
        monkeypatch.setattr(container, "exec", create)
        await container.pause()
        made.set()
        return exe

    try:
        monkeypatch.setattr(container, "exec", create_then_pause)
        monkeypatch.setattr(docker_module, "DRAIN_S", 0)
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=1))
        async with asyncio.timeout(10):
            await made.wait()
        await asyncio.sleep(4)
        assert not task.done()
        await container.unpause()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)
    assert result.stdout == b"ok\n"


async def test_a_pause_of_the_sandbox_takes_over_one_made_outside_aims(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        await inner._container.pause()
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=1))
        await asyncio.sleep(1)
        assert inner.paused
        await inner.pause()
        await asyncio.sleep(1.5)
        assert not task.done()
        await inner.resume()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert (result.finished.end, result.finished.exit_code) == ("exited", 0)


async def test_a_kill_ends_an_exec_that_waits_out_a_pause_made_outside_aims(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        await inner._container.pause()
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=10))
        await asyncio.sleep(1)
        assert (inner.paused, task.done()) == (True, False)
        await inner.kill()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert result.finished.end == "failed"


async def test_a_kill_ends_an_exec_that_waits_for_a_resume(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    try:
        await inner.pause()
        task = asyncio.create_task(sb.exec("echo ok", timeout_s=10))
        await asyncio.sleep(0.5)
        assert not task.done()
        await inner.kill()
        async with asyncio.timeout(10):
            result = await task
    finally:
        await sb.close()

    assert result.finished.end == "failed"


async def test_an_exec_stream_that_fails_to_open_is_closed(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that fails after the daemon answered, such as in the socket set-up, closes the
    connection it opened."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    streams: list[Stream] = []
    start = Exec.start

    def kept(self: Exec, **kw: Any) -> Any:
        stream = start(self, **kw)
        streams.append(stream)
        return stream

    def broken(*args: Any, **kw: Any) -> Any:
        raise OSError("socket set-up failed")

    try:
        with monkeypatch.context() as m:
            m.setattr(Exec, "start", kept)
            m.setattr(aiodocker.stream, "FlowControlDataQueue", broken)
            with pytest.raises(OSError, match="socket set-up failed"):
                await inner.run(
                    [f"{TOOLBOX}/busybox", "true"],
                    uid=AGENT_UID,
                    cwd="/",
                    timeout_ms=10_000,
                    capture=Capture(1024),
                )
    finally:
        await sb.close()

    (stream,) = streams
    assert stream._resp is not None
    assert stream._resp.closed


async def test_a_cancel_before_the_exec_start_is_sent_ends_at_once(
    backend: DockerBackend,
    store: LocalEventStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exec whose start request never went out never starts, so a cancel then does not wait
    for it."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    docker = backend.docker
    query = docker._do_query
    held = asyncio.Event()
    execs: list[Exec] = []
    start = Exec.start

    def kept(self: Exec, **kw: Any) -> Any:
        execs.append(self)
        return start(self, **kw)

    async def slow_connect(path: Any, *args: Any, **kw: Any) -> Any:
        if str(path).startswith("exec/") and str(path).endswith("/start"):
            held.set()
            await asyncio.sleep(30)
        return await query(path, *args, **kw)

    try:
        with monkeypatch.context() as m:
            m.setattr(Exec, "start", kept)
            m.setattr(docker, "_do_query", slow_connect)
            task = asyncio.create_task(
                inner.run(
                    [f"{TOOLBOX}/busybox", "sleep", "285"],
                    uid=AGENT_UID,
                    cwd="/",
                    timeout_ms=60_000,
                    capture=Capture(1024),
                )
            )
            async with asyncio.timeout(10):
                await held.wait()
            begun = asyncio.get_running_loop().time()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                async with asyncio.timeout(30):
                    await task
            took = asyncio.get_running_loop().time() - begun
        await asyncio.sleep(0.5)
        (exe,) = execs
        state = await exe.inspect()
        left = [p for p in await inner.processes() if p.command == "sleep 285"]
    finally:
        await sb.close()

    assert took < 1
    assert "did not end" not in caplog.text
    assert (state["Running"], state["ExitCode"], state["Pid"]) == (False, None, 0)
    assert left == []


async def test_an_exec_start_says_when_its_request_goes_out(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """A cancel tells an exec that never started from one still starting by this call, so it
    must come for every start that reaches the daemon."""
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    sent: list[bool] = []
    try:
        exe = await inner._container.exec(
            [f"{TOOLBOX}/busybox", "echo", "hi"], user=f"{AGENT_UID}:{AGENT_UID}"
        )
        stream = await open_stream(exe.start(detach=False), on_send=lambda: sent.append(True))
        assert sent == [True]
        out = b""
        try:
            while (message := await stream.read_out()) is not None:
                out += message.data
        finally:
            await stream.close()
    finally:
        await sb.close()

    assert out == b"hi\n"
    assert sent == [True]


async def test_a_cancel_ends_only_its_own_exec_of_a_command_running_twice(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)

    def running() -> asyncio.Task[Any]:
        return asyncio.create_task(
            inner.run(
                [*inner.wrapper(60_000), "sleep 286"],
                uid=AGENT_UID,
                cwd="/",
                timeout_ms=60_000,
                capture=Capture(1024),
            )
        )

    async def sleeps() -> int:
        return sum(p.command == "sleep 286" for p in await inner.processes())

    first, second = running(), running()
    try:
        async with asyncio.timeout(30):
            while True:
                if await sleeps() == 2:
                    break
                await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await first
        await asyncio.sleep(0.5)
        left = await sleeps()
        on = not second.done()
    finally:
        second.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second
        await sb.close()

    assert (left, on) == (1, True)


async def test_the_exec_id_shows_only_in_the_wrapper(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """The command sees the same argv, environment and parent it did before the ID."""
    run_id, sb = await sandbox(backend, store)
    command = 'tr "\\0" " " < /proc/$$/cmdline; echo; tr "\\0" " " < /proc/$PPID/cmdline; echo; env'
    try:
        result = await sb.exec(command, timeout_s=10)
    finally:
        await sb.close()

    (started,) = (await store.events(run_id, types=["exec.started"]))[-1:]
    payload = started.payload()
    assert isinstance(payload, ExecStarted)
    wrapper = payload.wrapper
    assert wrapper[:2] == ["/.aims/aims-exec", "--id"]
    exec_id = wrapper[2]
    own, parent, *env = result.stdout.decode().split("\n")
    assert own == f"/bin/bash -c {command} "
    assert parent == " ".join([*wrapper, command]) + " "
    assert exec_id not in own
    assert not any(exec_id in line for line in env)


async def test_a_cancelled_exec_ends_in_a_sandbox_at_its_pids_limit(
    backend: DockerBackend, store: LocalEventStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The kill forks nothing, so a command that took every pid still ends."""
    pids = 32
    run_id, sb = await sandbox(backend, store, pids=pids)
    inner = sb.sandbox
    assert isinstance(inner, DockerSandbox)
    fill = "while :; do sleep 287 & done"
    try:
        task = asyncio.create_task(sb.exec(fill, timeout_s=60))
        async with asyncio.timeout(30):
            while True:
                if len(await inner.processes()) >= pids:
                    break
                await asyncio.sleep(0.1)
        begun = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await task
        took = asyncio.get_running_loop().time() - begun
        left = [p for p in await inner.processes() if p.command == "sleep 287"]
    finally:
        await sb.close()

    assert left == []
    assert took < 3
    assert "did not end" not in caplog.text
    finished = (await store.events(run_id, types=["exec.finished"]))[0].data
    assert (finished["end"], finished["leftover"]) == ("cancelled", [])


async def test_a_cancelled_exec_that_does_not_end_says_why(
    backend: DockerBackend,
    store: LocalEventStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_id, sb = await sandbox(backend, store)

    async def failed_kill(*args: Any) -> int:
        return 2

    try:
        task = await sleeping(sb)
        with monkeypatch.context() as m:
            m.setattr(docker_module, "_call", failed_kill)
            m.setattr(docker_module, "END_S", 0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                async with asyncio.timeout(30):
                    await task
    finally:
        await sb.close()

    assert "its command may still run: the kill exited 2" in caplog.text
    finished = (await store.events(run_id, types=["exec.finished"]))[0].data
    assert finished["error"] == "its command may still run: the kill exited 2"
