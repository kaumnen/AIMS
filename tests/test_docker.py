import asyncio
import io
import json
import os
import re
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiodocker
import pytest

from aims.events import (
    AuditSpill,
    Event,
    FileAccess,
    FileWrite,
    FsChange,
    FsSnapshot,
    KernelPayload,
    LocalEventStore,
    NetConnect,
    ProcExec,
    ProcExit,
    ProcOutput,
    RunStarted,
    SandboxStarted,
    SysDenied,
)
from aims.sandbox import AuditedSandbox, DockerBackend, Mount, SandboxSpec
from aims.sandbox import audit as audit_module
from aims.sandbox import reader as reader_module

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


async def snapshots(store: LocalEventStore, run_id: str) -> list[FsSnapshot]:
    found = [e.payload() for e in await events(store, run_id, "fs.snapshot")]
    return [s for s in found if isinstance(s, FsSnapshot)]


def changed(snapshot: FsSnapshot) -> dict[str, FsChange]:
    return {c.path: c for c in snapshot.changes}


async def test_file_contents_are_diffed_per_exec(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec("echo 'def score(): return 0' > scorer.py && echo x > gone", timeout_s=20)
        result = await sb.exec(
            "echo 'def score(): return 1' > scorer.py && touch -d '2001-01-01' scorer.py && "
            "echo s3cret > secret && chmod 000 secret && rm gone && ln -s scorer.py link",
            timeout_s=20,
        )
    finally:
        await sb.close()

    snaps = await snapshots(store, run_id)
    assert [s.exec_id is None for s in snaps[:1]] == [True]
    last = next(s for s in snaps if s.exec_id == result.finished.exec_id)
    assert last.base == snaps[snaps.index(last) - 1].digest
    files = changed(last)
    scorer, secret = files["/work/scorer.py"], files["/work/secret"]
    assert (scorer.change, scorer.timestomp) == ("modified", True)
    assert scorer.content is not None
    assert await store.get_blob(run_id, scorer.content.sha256) == b"def score(): return 1\n"
    assert (secret.change, secret.mode) == ("added", 0)
    assert secret.content is not None
    assert await store.get_blob(run_id, secret.content.sha256) == b"s3cret\n"
    assert files["/work/gone"].change == "removed"
    assert (files["/work/link"].kind, files["/work/link"].target) == ("symlink", "scorer.py")
    assert (await store.verify(run_id)).ok


async def test_touched_files_do_not_use_up_the_content_budget(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_module, "BLOB_CAP", 100)
    shared = [Mount(volume="shared", target="/shared")]
    run_id, sb = await sandbox(backend, store, mounts=shared)
    try:
        await sb.exec("for i in $(seq 20); do head -c 90 /dev/zero > pad$i; done", timeout_s=20)
        result = await sb.exec(
            "touch pad* && echo 'def score(): return 1  # rigged' > /shared/target", timeout_s=20
        )
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    last = next(s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id)
    files = changed(last)
    target = files.pop("/shared/target")
    assert target.content is not None
    assert (
        await store.get_blob(run_id, target.content.sha256) == b"def score(): return 1  # rigged\n"
    )
    assert {c.change for c in files.values()} == {"meta"}
    assert last.content_skipped == 0


async def test_shared_volume_changes_land_in_the_next_window(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
    spec = SandboxSpec(image=IMAGE, mounts=[Mount(volume="shared", target="/shared")])
    a = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a1")
    b = await AuditedSandbox.start(backend, spec, store, run_id=run_id, agent_id="a2")
    try:
        await a.exec("echo plan > /shared/msg", timeout_s=10)
        result = await b.exec("cat /shared/msg", timeout_s=10)
    finally:
        await a.close()
        await b.close()
        await backend.remove_run(run_id)

    evs = await events(store, run_id, "fs.snapshot", "exec.started")
    mine = [e for e in evs if e.agent_id == "a2"]
    started = next(i for i, e in enumerate(mine) if e.type == "exec.started")
    window = mine[started - 1].payload()
    assert isinstance(window, FsSnapshot)
    assert window.exec_id is None
    assert "/shared/msg" in changed(window)
    own = [e.payload() for e in mine[started + 1 :]]
    assert all(
        isinstance(p, FsSnapshot) and p.exec_id == result.finished.exec_id and not p.changes
        for p in own
    )


async def test_snapshot_audit_diffs_files_too(store: LocalEventStore) -> None:
    async with await DockerBackend.open(audit="snapshot") as plain:
        run_id, sb = await sandbox(plain, store)
        try:
            result = await sb.exec("echo hi > note", timeout_s=10)
        finally:
            await sb.close()

    assert result.finished.procs is None
    last = (await snapshots(store, run_id))[-1]
    assert last.exec_id == result.finished.exec_id
    assert changed(last)["/work/note"].change == "added"


async def test_reader_is_locked_down_and_removed(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    name = f"aims-{run_id}-a1-reader"
    try:
        info = await backend.docker.containers.get(name)
        host = (await info.show())["HostConfig"]
    finally:
        await sb.close()

    assert host["CapDrop"] == ["ALL"]
    assert host["CapAdd"] == ["DAC_READ_SEARCH"]
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "none"
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert all(m["ReadOnly"] for m in host["Mounts"])
    with pytest.raises(aiodocker.DockerError):
        await backend.docker.containers.get(name)


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_kernel_records_file_writes_and_self_probes(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        "echo a > out && echo bb >> out && "
        "cat /proc/self/status /proc/1/cmdline /proc/self/mountinfo /proc/self/environ "
        "/proc/meminfo /sys/fs/cgroup/cpu.max > /dev/null; "
        "for i in 1 2 3; do sleep 30 & done; /.aims/busybox ps > /dev/null; kill $(jobs -p)"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    exec_id = result.finished.exec_id
    writes = [e.payload() for e in await events(store, run_id, "file.write")]
    sizes = [p.size for p in writes if isinstance(p, FileWrite) and p.path == "/work/out"]
    assert sizes == [2, 5]
    assert all(isinstance(p, FileWrite) and p.exec_id == exec_id for p in writes)
    reads = {
        p.path
        for p in (e.payload() for e in await events(store, run_id, "file.access"))
        if isinstance(p, FileAccess) and p.op == "read"
    }
    procs = {re.sub(r"^/proc/(?!1/)\d+/", "/proc/N/", p) for p in reads if p.startswith("/proc/")}
    assert {
        "/proc/N/status",
        "/proc/1/cmdline",
        "/proc/N/mountinfo",
        "/proc/N/environ",
        "/proc/1/stat",
    } <= procs
    assert "/sys/fs/cgroup/cpu.max" in reads
    assert "/proc/meminfo" not in reads
    assert not [p for p in procs if p.endswith("/stat") and p != "/proc/1/stat"]


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_over_cap_records_spill_to_a_blob(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "files", 20)
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec("for i in $(seq 100); do echo $i > f$i; done", timeout_s=30)
        await asyncio.sleep(0.5)
    finally:
        await sb.close()

    finished = result.finished
    assert finished.dropped == 0
    assert finished.spilled > 0
    spills = [e.payload() for e in await events(store, run_id, "audit.spill")]
    spills = [s for s in spills if isinstance(s, AuditSpill)]
    lines = []
    for s in spills:
        lines += (await store.get_blob(run_id, s.records.sha256)).decode().splitlines()
    paths = {json.loads(line)["data"]["path"] for line in lines}
    kept = {
        p.path
        for p in (e.payload() for e in await events(store, run_id, "file.access", "file.write"))
        if isinstance(p, FileAccess | FileWrite)
    }
    assert {f"/work/f{i}" for i in range(1, 101)} <= paths | kept
    assert sum(s.count for s in spills) >= finished.spilled


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_exec_context_and_pipe_graph(backend: DockerBackend, store: LocalEventStore) -> None:
    command = "cd /work && LD_PRELOAD=/work/x.so python3 -c 'import os; os.getcwd()' | cat"
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    procs = [e.payload() for e in await events(store, run_id, "proc.exec")]
    procs = [p for p in procs if isinstance(p, ProcExec)]
    python = next(p for p in procs if p.argv[0] == "python3")
    assert python.cwd == "/work"
    assert any(e.startswith("LD_PRELOAD=/work/x.so") for e in python.env)
    assert python.fds is not None
    # python's stdout is the pipe into cat; stdin and stdout differ in kind or inode.
    assert python.fds[1].kind in ("pipe", "fifo")
    cat = next(p for p in procs if p.argv[0] == "cat")
    assert cat.fds is not None
    # cat reads the same pipe python writes: its stdin inode equals python's stdout inode.
    assert cat.fds[0].ino == python.fds[1].ino


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_pipe_output_captured(backend: DockerBackend, store: LocalEventStore) -> None:
    command = (
        "echo injected-line | cat; mkfifo /work/f; "
        "(cat /work/f >/dev/null &); echo viafifo > /work/f"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    outs = [e.payload() for e in await events(store, run_id, "proc.output")]
    outs = [p for p in outs if isinstance(p, ProcOutput)]
    prefixes = {p.prefix for p in outs if isinstance(p.prefix, str)}
    assert any(p.startswith("injected-line") for p in prefixes)
    assert any(p.startswith("viafifo") for p in prefixes)
    assert any(p.stream == "fifo" for p in outs)
    assert all(p.bytes >= p.prefix_bytes for p in outs)


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_writev_output_keeps_its_prefix(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        "python3 -c \"import os; os.writev(1, [b'', b'', b'vec-', b'tail'])\" | cat >/dev/null; "
        "python3 -c \"import os; os.writev(1, [b'ab', b'', b'', b'payload'])\" | cat >/dev/null; "
        "python3 -c \"import os; os.writev(1, [b''] * 6 + [b'late'])\" | cat >/dev/null; "
        "/.aims/busybox printf 'musl-stdio' | cat >/dev/null"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    outs = [e.payload() for e in await events(store, run_id, "proc.output")]
    prefixes = {p.prefix for p in outs if isinstance(p, ProcOutput)}
    assert {"vec-tail", "abpayload", "late", "musl-stdio"} <= prefixes


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_output_prepending_is_visible(backend: DockerBackend, store: LocalEventStore) -> None:
    # The shell prints a false line, then runs a command whose real output differs.
    command = "{ echo 'all tests passed'; sh -c 'exit 1'; } | cat"
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    outs = [e.payload() for e in await events(store, run_id, "proc.output")]
    prefixes = [p.prefix for p in outs if isinstance(p, ProcOutput) and isinstance(p.prefix, str)]
    assert any(p.startswith("all tests passed") for p in prefixes)
    # The bytes on the pipe are recorded even though the command exited non-zero.
    assert result.stdout == b"all tests passed\n"
