import asyncio
import io
import os
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aims.events import (
    Event,
    FileAccess,
    KernelPayload,
    LocalEventStore,
    NetConnect,
    ProcExec,
    ProcExit,
    RunStarted,
    SandboxStarted,
    SysDenied,
)
from aims.sandbox import AuditedSandbox, DockerBackend, Mount, SandboxSpec

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

IMAGE = "python:3.13-slim"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        backend = await DockerBackend.open(audit="auto")
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


@pytest.fixture
def traced(backend: DockerBackend) -> None:
    if backend.tracer is None:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            pytest.fail("the kernel tracer did not start")
        pytest.skip("the kernel tracer is not available here")


async def sandbox(
    backend: DockerBackend, store: LocalEventStore, agent: str = "a1", **spec: Any
) -> tuple[str, AuditedSandbox]:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe", agents=[agent]))
    audited = await AuditedSandbox.start(
        backend, SandboxSpec(image=IMAGE, **spec), store, run_id=run_id, agent_id=agent
    )
    return run_id, audited


async def events(store: LocalEventStore, run_id: str, *types: str) -> list[Event]:
    return await store.events(run_id, limit=1000, types=types)


async def test_exec_reports_output_and_exit(backend: DockerBackend, store: LocalEventStore) -> None:
    run_id, sb = await sandbox(backend, store)
    audit = sb.sandbox.audit
    try:
        result = await sb.exec("echo out; echo err >&2; exit 3", timeout_s=10, call_id="c1")
    finally:
        await sb.close()

    assert (result.stdout, result.stderr) == (b"out\n", b"err\n")
    assert result.finished.end == "exited"
    assert result.finished.exit_code == 3
    assert result.finished.pid is not None
    started = (await events(store, run_id, "sandbox.started"))[0].payload()
    assert isinstance(started, SandboxStarted)
    assert started.image_id.startswith("sha256:")
    assert started.audit == audit
    assert (await store.verify(run_id)).ok


async def test_sandbox_is_locked_down(backend: DockerBackend, store: LocalEventStore) -> None:
    _, sb = await sandbox(backend, store)
    checks = {
        "id -u": b"1000\n",
        "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status | tr -s '\\t' ' '": (
            b"CapEff: 0000000000000000\nNoNewPrivs: 1\n"
        ),
        "touch /etc/x 2>/dev/null || echo ro": b"ro\n",
        "echo ok > /work/f && cat /work/f && echo ok > /tmp/f && cat /tmp/f": b"ok\nok\n",
        "echo $HOME && touch $HOME/f && echo home": b"/home/agent\nhome\n",
        "unshare -Ur true 2>/dev/null || echo denied": b"denied\n",
        (
            "python3 -c \"import socket\ntry: socket.create_connection(('1.1.1.1', 443), 2)\n"
            'except OSError as e: print(e.errno)"'
        ): b"101\n",
    }
    try:
        for command, expected in checks.items():
            result = await sb.exec(command, timeout_s=10)
            assert result.stdout == expected, command
    finally:
        await sb.close()


async def test_timeout_kills_the_process_group(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store)
    try:
        result = await sb.exec("sleep 30 & sleep 30", timeout_s=1)
        processes = await sb.sandbox.processes()
    finally:
        await sb.close()

    assert result.finished.end == "timeout"
    assert result.finished.duration_ms < 5000
    assert result.finished.leftover == []
    assert not any(p.command == "sleep 30" for p in processes)


async def test_leftovers_are_recorded_then_killed(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    _, sb = await sandbox(backend, store, kill_leftovers=True)
    try:
        result = await sb.exec("setsid sleep 300 >/dev/null 2>&1 < /dev/null &", timeout_s=10)
        processes = await sb.sandbox.processes()
    finally:
        await sb.close()

    assert [p.command for p in result.finished.leftover] == ["sleep 300"]
    assert result.finished.leftover_killed
    assert not any(p.command == "sleep 300" for p in processes)


async def test_output_is_capped_and_counted(backend: DockerBackend, store: LocalEventStore) -> None:
    _, sb = await sandbox(backend, store, output_limit=1000)
    try:
        result = await sb.exec("head -c 50000 /dev/zero | tr '\\0' a", timeout_s=10)
    finally:
        await sb.close()

    assert result.stdout == b"a" * 1000
    assert result.finished.stdout_bytes == 50000
    assert result.finished.truncated


async def test_shared_volume_reaches_other_sandboxes(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    spec = SandboxSpec(image=IMAGE, mounts=[Mount(volume="shared", target="/shared")])
    a = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a1")
    b = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a2")
    try:
        await a.exec("echo from-a > /shared/msg", timeout_s=10)
        result = await b.exec("cat /shared/msg; ls /work", timeout_s=10)
    finally:
        await a.close()
        await b.close()
        await backend.remove_run(run_id)

    assert result.stdout == b"from-a\n"


async def test_archives_round_trip(backend: DockerBackend, store: LocalEventStore) -> None:
    _, sb = await sandbox(backend, store)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("task.txt")
        info.size, info.uid, info.gid = 5, 1000, 1000
        tar.addfile(info, io.BytesIO(b"hello"))
    try:
        await sb.sandbox.put_archive("/work", buf.getvalue())
        result = await sb.exec("cat task.txt && echo ' world' >> task.txt", timeout_s=10)
        data = await sb.sandbox.get_archive("/work/task.txt")
    finally:
        await sb.close()

    assert result.stdout == b"hello"
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.extractfile("task.txt")
        assert member is not None
        assert member.read() == b"hello world\n"


async def test_cancel_stops_waiting(backend: DockerBackend, store: LocalEventStore) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        task = asyncio.create_task(sb.exec("echo started; sleep 30", timeout_s=60))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await sb.close()

    finished = (await events(store, run_id, "exec.finished"))[0].data
    assert finished["end"] == "cancelled"
    assert finished["stdout"] == "started\n"


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_kernel_audit_sees_what_ran(backend: DockerBackend, store: LocalEventStore) -> None:
    command = "\n".join(
        [
            "echo s > scorer.py",
            "cat scorer.py > /dev/null",
            "mkdir -p d && mv scorer.py d/s.py && rm d/s.py",
            "cat /etc/shadow 2>/dev/null",
            "unshare -Ur true 2>/dev/null",
            "sh -c 'exit 7' | cat",
            "python3 -c \"import socket\ntry: socket.create_connection(('169.254.169.254', 80), 1)"
            '\nexcept OSError: pass"',
            "true",
        ]
    )
    run_id, sb = await sandbox(backend, store)
    try:
        assert sb.sandbox.audit == "kernel"
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    exec_id = result.finished.exec_id
    evs = await events(store, run_id)
    mine = [e for e in evs if e.data.get("exec_id") == exec_id]
    assert all(e.agent_id == "a1" for e in mine)
    payloads = [e.payload() for e in mine]
    procs = [p for p in payloads if isinstance(p, ProcExec)]
    argvs = [p.argv for p in procs]
    assert argvs[0][0] == "/.aims/aims-exec"
    assert argvs[1] == ["/bin/sh", "-c", command]
    assert ["sh", "-c", "exit 7"] in argvs
    assert ["cat", "/etc/shadow"] in argvs
    files = {(p.op, p.path, p.target) for p in payloads if isinstance(p, FileAccess)}
    assert {
        ("write", "/work/scorer.py", None),
        ("read", "/work/scorer.py", None),
        ("rename", "/work/scorer.py", "/work/d/s.py"),
        ("delete", "/work/d/s.py", None),
        ("denied", "/etc/shadow", None),
    } <= files
    assert "unshare" in [p.syscall for p in payloads if isinstance(p, SysDenied)]
    connects = [(p.address, p.port) for p in payloads if isinstance(p, NetConnect)]
    assert ("169.254.169.254", 80) in connects
    exits = {p.pid: p.exit_code for p in payloads if isinstance(p, ProcExit)}
    assert [exits[p.pid] for p in procs if p.argv == ["sh", "-c", "exit 7"]] == [7]
    assert result.finished.exit_code == 0
    assert result.finished.procs == len(procs)
    finished = next(e for e in evs if e.type == "exec.finished")
    assert all(e.seq < finished.seq for e in mine if e.type != "exec.finished")
    assert (await store.verify(run_id)).ok

    kernel = [p for p in payloads if isinstance(p, KernelPayload)]
    by_cpu: dict[int, list[KernelPayload]] = {}
    for p in kernel:
        by_cpu.setdefault(p.cpu, []).append(p)
    for stream in by_cpu.values():
        assert [p.cpu_seq for p in stream] == sorted({p.cpu_seq for p in stream})
        assert [p.ts_kernel for p in stream] == sorted(p.ts_kernel for p in stream)
    started_at = {p.pid: p.ts_kernel for p in procs}
    assert all(started_at[p.ppid] < p.ts_kernel for p in procs if p.ppid in started_at)
    exited_at = {p.pid: p.ts_kernel for p in payloads if isinstance(p, ProcExit)}
    assert all(started_at[pid] < ts for pid, ts in exited_at.items() if pid in started_at)
    clock = (await events(store, run_id, "sandbox.started"))[0].data["config"]
    assert isinstance(clock, dict)
    kernel_clock = clock["kernel_clock"]
    assert isinstance(kernel_clock, dict)
    assert 0 < int(str(kernel_clock["boot_us"])) < int(str(kernel_clock["tai_us"]))


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_long_commands_keep_their_exec(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = f"cat > /dev/null <<'EOF'\n{'x' * 5000}\nEOF\nls /work"
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    procs = [e.payload() for e in await events(store, run_id, "proc.exec")]
    procs = [p for p in procs if isinstance(p, ProcExec)]
    assert [p.exec_id for p in procs] == [result.finished.exec_id] * len(procs)
    root = procs[0]
    assert root.root
    assert root.argv_bytes > 5000
    assert sum(len(a) + 1 for a in root.argv) < root.argv_bytes
    assert any(p.argv == ["ls", "/work"] for p in procs)


FLOOD = (
    "mkdir -p /work/f && cd /work/f && i=0; while [ $i -lt 1500 ]; do : > f$i; i=$((i+1)); done; "
    "cd /work && rm -rf /work/f"
)
EVERY_CPU = "for c in $(seq 0 $(($(nproc) - 1))); do taskset -c $c true; done"


async def flood(backend: DockerBackend, store: LocalEventStore, count: int) -> None:
    boxes = [(await sandbox(backend, store, f"a{i}"))[1] for i in range(count)]
    try:
        await asyncio.gather(*(sb.exec(FLOOD, timeout_s=120) for sb in boxes))
        await asyncio.sleep(1.5)
        await asyncio.gather(*(sb.exec(EVERY_CPU, timeout_s=60) for sb in boxes))
        await asyncio.sleep(1.5)
    finally:
        for sb in boxes:
            await sb.close()


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_parallel_floods_lose_nothing(backend: DockerBackend, store: LocalEventStore) -> None:
    tracer = backend.tracer
    assert tracer is not None
    before = (tracer.kernel_lost, tracer.located)
    await flood(backend, store, 3)
    tracer.settle()

    assert (tracer.kernel_lost, tracer.located) == before


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_sequence_locates_every_lost_record(store: LocalEventStore) -> None:
    async with await DockerBackend.open(audit="kernel", ring_pages=8) as small:
        tracer = small.tracer
        assert tracer is not None
        await flood(small, store, 3)
        tracer.settle()

    assert tracer.kernel_lost > 0
    assert tracer.located == tracer.kernel_lost


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_odd_file_names_keep_their_bytes(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        "python3 -c \"import os\np = b'/work/a b\\xff'\nopen(p, 'w').close()\n"
        "os.rename(p, b'/work/c\\nd')\nos.unlink(b'/work/c\\nd')\""
    )
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    files = [e.payload() for e in await events(store, run_id, "file.access")]
    seen = {(p.op, p.path, p.target) for p in files if isinstance(p, FileAccess)}
    assert {
        ("write", "/work/a b\\xff", None),
        ("rename", "/work/a b\\xff", "/work/c\nd"),
        ("delete", "/work/c\nd", None),
    } <= seen
