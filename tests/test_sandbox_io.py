import asyncio
import io
import json
import os
import tarfile
import tracemalloc
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec
from aiodocker.volumes import DockerVolume

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
from aims.sandbox.base import RUNNER_DIR
from aims.sandbox.docker import INSTANCE
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


def hold_create(
    backend: DockerBackend, run_id: str, where: str, monkeypatch: pytest.MonkeyPatch
) -> asyncio.Event:
    """Make the create of agent a1 of `run_id` stop for good once it reaches `where`: the wait
    for the helper that fills the work volume, the second tmpfs volume, or that helper or the
    sandbox container made by the daemon but not yet handed back."""
    reached = asyncio.Event()

    async def stop() -> None:
        reached.set()
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
            container = await create(**kw)
            if kw["config"]["Labels"].get("aims.role") == "helper":
                helpers += 1
                if where == "helper" and helpers == 2:
                    await stop()
            elif where == "create" and kw.get("name") == f"aims-{run_id}-a1":
                await stop()
            return container

        monkeypatch.setattr(backend.docker.containers, "create", held_create)
    return reached


SHARED = SandboxSpec(image=IMAGE, runner_mb=8, mounts=[Mount(volume="shared", target="/shared")])


@pytest.mark.parametrize("where", ["fill", "helper", "scratch", "create"])
async def test_a_cancelled_create_removes_what_it_made(
    backend: DockerBackend, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    run_id = f"test-{uuid.uuid4().hex[:12]}"
    before, _ = await leftovers(backend, run_id)
    reached = hold_create(backend, run_id, where, monkeypatch)
    task = asyncio.create_task(backend.create(SHARED, run_id=run_id, agent_id="a1"))
    async with asyncio.timeout(60):
        await reached.wait()
    assert (await leftovers(backend, run_id))[1]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(60):
            await task
    assert await leftovers(backend, run_id) == (before, set())


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
    reached = hold_create(backend, run_id, "create", monkeypatch)
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
        await reached.wait()
    cancelled = True
    task.cancel()
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


async def sleeping(sb: AuditedSandbox, command: str = SLEEPS) -> asyncio.Task[Any]:
    """`command`, which runs `SLEEPS`, as an exec, once both sleeps are up."""
    task = asyncio.create_task(sb.exec(command, timeout_s=60))
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
