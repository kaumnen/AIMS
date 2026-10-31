import asyncio
import io
import os
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aims.events import ExecStarted, LocalEventStore, RunStarted, SandboxArchived
from aims.sandbox import (
    AGENT_UID,
    RUNNER_UID,
    AuditedSandbox,
    DockerBackend,
    DockerSandbox,
    SandboxSpec,
)
from aims.sandbox.base import RUNNER_DIR

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
