import asyncio
import io
import json
import os
import re
import shlex
import tarfile
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiodocker
import pytest

from aims.events import (
    AuditSpill,
    Event,
    FileAccess,
    FileSetattr,
    FileWrite,
    FsChange,
    FsSnapshot,
    IpcEvent,
    KernelPayload,
    LocalEventStore,
    MemExec,
    NetConnect,
    ProcExec,
    ProcExit,
    ProcFork,
    ProcOutput,
    ProcSignal,
    PtraceEvent,
    RunStarted,
    SandboxStarted,
    SysDenied,
)
from aims.sandbox import AuditedSandbox, DockerBackend, Mount, SandboxSpec
from aims.sandbox import audit as audit_module
from aims.sandbox import reader as reader_module
from aims.sandbox.trace import STRLEN

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
    _, sb = await sandbox(backend, store, tmp_mb=4)
    checks = {
        "id -u": b"1000\n",
        "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status | tr -s '\\t' ' '": (
            b"CapEff: 0000000000000000\nNoNewPrivs: 1\n"
        ),
        "touch /etc/x 2>/dev/null || echo ro": b"ro\n",
        "echo ok > /work/f && cat /work/f && echo ok > /tmp/f && cat /tmp/f": b"ok\nok\n",
        "echo $HOME && touch $HOME/f && echo home": b"/home/agent\nhome\n",
        "grep -cE ' (/tmp|/home/agent) tmpfs rw,nosuid,nodev' /proc/mounts": b"2\n",
        "stat -c '%u %a' /tmp /home/agent": b"0 1777\n1000 755\n",
        "head -c 8M /dev/zero > /tmp/big 2>/dev/null || echo full": b"full\n",
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
    assert all(p.inode and p.dev and p.magic for p in procs)
    assert all(p.nlink is not None and p.nlink >= 1 for p in procs)
    assert ["sh", "-c", "exit 7"] in argvs
    assert ["cat", "/etc/shadow"] in argvs
    files = {(p.op, p.path, p.target) for p in payloads if isinstance(p, FileAccess)}
    expected = {
        ("write", "/work/scorer.py", None),
        ("read", "/work/scorer.py", None),
        ("rename", "/work/scorer.py", "/work/d/s.py"),
        ("delete", "/work/d/s.py", None),
        ("denied", "/etc/shadow", None),
    }
    if not expected <= files:
        lost = [e.data for e in evs if e.type == "audit.lost"]
        lines = backend.tracer.unrouted_lines if backend.tracer else ()
        print(sorted(files, key=str), lost, *lines, sep="\n")
    assert expected <= files
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
SIGNAL_FLOOD = 4000


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
        # Every cpu ticks once a second, so a hole is found and ripe within two.
        await asyncio.sleep(2.5)
        tracer.settle()

    assert tracer.kernel_lost > 0
    assert tracer.located == tracer.kernel_lost


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_tracer_sees_only_sandboxes(backend: DockerBackend, store: LocalEventStore) -> None:
    tracer = backend.tracer
    assert tracer is not None
    run_id, sb = await sandbox(backend, store)
    other = None
    try:
        before = tracer.unrouted
        await sb.exec("for i in $(seq 200); do echo $i > f$i; done", timeout_s=30)
        loop = "for i in $(seq 200); do echo $i > /tmp/f$i; cat /tmp/f$i; done"
        other = await backend.docker.containers.run(
            config={"Image": IMAGE, "Cmd": ["sh", "-c", loop], "NetworkDisabled": True}
        )
        await other.wait()
        await asyncio.sleep(1)
        unrouted = tracer.unrouted - before
    finally:
        if other is not None:
            await other.delete(force=True)
        await sb.close()
        await backend.remove_run(run_id)

    late = list(tracer.unrouted_lines)[-unrouted:] if unrouted else []
    print("\n".join(late))
    assert unrouted == 0
    assert any(e.type == "file.write" for e in await events(store, run_id, "file.write"))


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_renamed_processes_stay_visible(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        'python3 -c "import ctypes, os\n'
        "assert ctypes.CDLL(None).prctl(15, b'runc:x', 0, 0, 0) == 0\n"
        "assert open('/proc/self/comm').read() == 'runc:x\\n'\n"
        "open('/work/a', 'w').write('a')\n"
        "os.unlink('/work/a')\" && "
        'python3 -c "import os\n'
        "open('/proc/self/comm', 'w').write('runc:y')\n"
        "assert open('/proc/self/comm').read() == 'runc:y\\n'\n"
        "open('/work/b', 'w').write('bb')\n"
        "os.unlink('/work/b')\""
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    assert result.finished.exit_code == 0
    kernel = by_kernel_order(await events(store, run_id))
    first, second = [p.pid for p in kernel if isinstance(p, ProcExec) and p.argv[0] == "python3"]
    work = [
        (p.pid, p.op if isinstance(p, FileAccess) else "close", p.path)
        for p in kernel
        if isinstance(p, FileAccess | FileWrite) and p.path.startswith("/work/")
    ]
    assert work == [
        (first, "write", "/work/a"),
        (first, "close", "/work/a"),
        (first, "delete", "/work/a"),
        (second, "write", "/work/b"),
        (second, "close", "/work/b"),
        (second, "delete", "/work/b"),
    ]
    assert all(p.exec_id == result.finished.exec_id for p in kernel if p.pid in (first, second))


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


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_exec_hash_matches_the_snapshot(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = "cp /bin/true /work/true && chmod +x /work/true && ./true; sha256sum /work/true"
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    expected = result.stdout.split()[0].decode()
    evs = await events(store, run_id, "proc.exec")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    procs = [p for p in (e.payload() for e in evs) if isinstance(p, ProcExec)]
    assert all(p.exec_id == result.finished.exec_id for p in procs)
    proc = next(p for p in procs if p.path == "./true")
    assert proc.sha256 == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    root = next(p for p in procs if p.root)
    assert root.sha256 is None
    last = next(s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id)
    assert changed(last)["/work/true"].sha256 == expected


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_exec_hash_of_every_captured_root(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    shared = [Mount(volume="shared", target="/shared")]
    run_id, sb = await sandbox(backend, store, mounts=shared)
    command = (
        "for p in /tmp/t $HOME/h /shared/s; do "
        "cp /bin/true $p && chmod +x $p && $p; done; "
        "sha256sum /tmp/t $HOME/h /shared/s"
    )
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    expected = {line.split()[1]: line.split()[0] for line in result.stdout.decode().splitlines()}
    evs = await events(store, run_id, "proc.exec")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    procs = [p for p in (e.payload() for e in evs) if isinstance(p, ProcExec)]
    assert all(p.exec_id == result.finished.exec_id for p in procs)
    for path, digest in expected.items():
        proc = next(p for p in procs if p.path == path)
        assert (proc.sha256, proc.path_partial) == (digest, False), path
    root = next(p for p in procs if p.root)
    assert root.sha256 is None
    last = next(s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id)
    changes = changed(last)
    for path, digest in expected.items():
        assert changes[path].sha256 == digest, path


THREAD_EXEC = (
    'cp /bin/ls /tmp/evil && python3 -c "import os, threading; t = threading.Thread('
    "target=lambda: os.execv('/tmp/evil', ['/usr/bin/ls', '-d', '/work'])); t.start(); t.join()\"; "
    "sha256sum /tmp/evil"
)


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_an_exec_from_a_thread_logs_the_file_it_runs(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """The old leader dies in the exec, but the process lives on: no exit before the exec."""
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(THREAD_EXEC, timeout_s=20)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    digest = result.stdout.split()[-2].decode()
    evs = await events(store, run_id, "proc.exec", "proc.exit")
    payloads = [e.payload() for e in evs]
    ls = next(p for p in payloads if isinstance(p, ProcExec) and p.argv[0] == "/usr/bin/ls")
    assert (ls.path, ls.sha256) == ("/tmp/evil", digest)
    assert ls.inode is not None
    mine = [p for p in payloads if isinstance(p, KernelPayload) and p.pid == ls.pid]
    assert [type(p) for p in mine] == [ProcExec, ProcExec, ProcExit]
    assert isinstance(mine[-1], ProcExit)
    assert mine[-1].exit_code == 0


THREAD_EXITS = {
    # exit_group from a thread
    "group": (
        "import os, threading; threading.Thread(target=lambda: os._exit(3)).start(); "
        "threading.Event().wait()"
    ),
    # the leader exits first, then the last thread calls exit_group
    "leader": (
        "import ctypes, os, threading, time; threading.Thread(target=lambda: (time.sleep(0.3), "
        "open('/work/after', 'w').write('x'), os._exit(7))).start(); "
        "ctypes.CDLL(None).pthread_exit(None)"
    ),
    # the leader exits first, then the last thread ends with a plain exit
    "thread": (
        "import ctypes, os, threading, time; libc = ctypes.CDLL(None); "
        "nr = {'x86_64': 60, 'aarch64': 93}[os.uname().machine]; "
        "threading.Thread(target=lambda: (time.sleep(0.3), libc.syscall(nr, 5))).start(); "
        "libc.pthread_exit(None)"
    ),
    # a thread kills the process
    "killed": (
        "import os, threading; threading.Thread(target=lambda: os.kill(os.getpid(), 9)).start(); "
        "threading.Event().wait()"
    ),
}


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_threaded_exits_report_what_wait_sees(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = "; ".join(
        f"python3 -c {shlex.quote(code)} {name}; echo {name} $?"
        for name, code in THREAD_EXITS.items()
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=30)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    waited = dict(line.split() for line in result.stdout.decode().splitlines())
    assert waited == {"group": "3", "leader": "7", "thread": "5", "killed": "137"}
    payloads = [e.payload() for e in await events(store, run_id)]
    pids = {
        p.argv[-1]: p.pid for p in payloads if isinstance(p, ProcExec) and p.argv[:1] == ["python3"]
    }
    exits = {p.pid: (p.exit_code, p.signal) for p in payloads if isinstance(p, ProcExit)}
    got = {name: exits.get(pid) for name, pid in pids.items()}
    assert got == {
        "group": (3, None),
        "leader": (7, None),
        "thread": (5, None),
        "killed": (None, 9),
    }
    writes = [p for p in payloads if isinstance(p, FileAccess) and p.path == "/work/after"]
    assert [(p.pid, p.exec_id) for p in writes] == [(pids["leader"], result.finished.exec_id)]
    leader = [p for p in payloads if isinstance(p, KernelPayload) and p.pid == pids["leader"]]
    assert sum(isinstance(p, ProcExit) for p in leader) == 1


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_close_records_the_last_exits(backend: DockerBackend, store: LocalEventStore) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec("sleep 60 >/dev/null 2>&1 & echo $!", timeout_s=10)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    evs = await events(store, run_id)
    payloads = [e.payload() for e in evs]
    sleep = next(p for p in payloads if isinstance(p, ProcExec) and p.argv[:1] == ["sleep"])
    exits = [p for p in payloads if isinstance(p, ProcExit) and p.pid == sleep.pid]
    assert [(p.exit_code, p.signal) for p in exits] == [(None, 9)]
    assert evs[-1].type == "sandbox.stopped"


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


async def test_tmp_and_home_are_diffed_with_content(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    scratch = {f"aims-{run_id}-a1-tmp", f"aims-{run_id}-a1-home"}
    try:
        result = await sb.exec(
            "printf 'sleep 999 &\\n' > /tmp/trip.sh && echo 'export X=1' > $HOME/.profile",
            timeout_s=10,
        )
    finally:
        await sb.close()
    listing = await backend.docker.volumes.list()
    left = {v["Name"] for v in listing.get("Volumes") or []} & scratch
    await backend.remove_run(run_id)

    last = next(s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id)
    assert {"/tmp", "/home/agent"} <= set(last.roots)
    files = changed(last)
    expected = {"/tmp/trip.sh": b"sleep 999 &\n", "/home/agent/.profile": b"export X=1\n"}
    for path, data in expected.items():
        content = files[path].content
        assert content is not None, path
        assert await store.get_blob(run_id, content.sha256) == data
    assert left == set()


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


async def test_hash_budget_goes_to_the_smallest_files_first(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_module, "HASH_CAP", 1000)
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(
            "for i in $(seq 10); do head -c 980 /dev/zero > pad$i; done && "
            "echo 'def score(): return 1  # rigged' > scorer.py",
            timeout_s=20,
        )
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    last = next(s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id)
    files = changed(last)
    scorer = files.pop("/work/scorer.py")
    assert scorer.sha256 is not None
    assert scorer.content is not None
    assert [c.sha256 for c in files.values()] == [None] * 10
    assert last.unhashed == 10


async def test_a_failed_hash_keeps_the_reader(
    backend: DockerBackend, store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_module, "HASH_CAP", 1000)
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(
            "mkdir dir && ln -s /bin/true link && head -c 2000 /dev/zero > big", timeout_s=20
        )
        for path in ("/work/gone", "/work/dir", "/work/link", "/work/big"):
            assert await sb.sandbox.hash(path, inode=0) is None, path
        result = await sb.exec("echo hi > note", timeout_s=20)
    finally:
        await sb.close()
        await backend.remove_run(run_id)

    last = [s for s in await snapshots(store, run_id) if s.exec_id == result.finished.exec_id]
    assert [changed(s)["/work/note"].change for s in last] == ["added"]


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
async def test_file_writes_are_recorded_on_every_close(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        'python3 -c "import os\n'
        "f = open('/work/c', 'w'); f.write('abc'); f.close()\n"
        "if os.fork() == 0:\n"
        "    os.write(os.open('/work/e', os.O_WRONLY | os.O_CREAT), b'xy')\n"
        "    os._exit(0)\n"
        "os.wait()\n"
        "os.write(os.open('/work/p', os.O_WRONLY | os.O_CREAT), b'wxyz')\n"
        'os._exit(0)"'
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    kernel = by_kernel_order(await events(store, run_id))
    python = next(p for p in kernel if isinstance(p, ProcExec) and p.argv[0] == "python3")
    (fork,) = [p for p in kernel if isinstance(p, ProcFork)]
    writes = [p for p in kernel if isinstance(p, FileWrite) and p.path.startswith("/work/")]
    assert [(p.pid, p.path, p.size, p.partial) for p in writes] == [
        (python.pid, "/work/c", 3, False),
        (fork.pid, "/work/e", 2, False),
        (python.pid, "/work/p", 4, False),
    ]
    assert all(p.exec_id == result.finished.exec_id for p in writes)
    exits = {p.pid: p.ts_kernel for p in kernel if isinstance(p, ProcExit)}
    assert exits[fork.pid] < writes[1].ts_kernel
    assert exits[python.pid] < writes[2].ts_kernel


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_writable_root_records_each_change_once(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        "mkdir /var/tmp/d && echo hi > /var/tmp/d/f && mv /var/tmp/d/f /var/tmp/d/g && "
        "ln -s /var/tmp/d/g /var/tmp/d/s && ln /var/tmp/d/g /var/tmp/d/h && "
        "chmod 600 /var/tmp/d/g && mkfifo /var/tmp/d/p && rm -r /var/tmp/d"
    )
    run_id, sb = await sandbox(backend, store, writable_root=True)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    assert result.finished.exit_code == 0
    kernel = by_kernel_order(await events(store, run_id))
    assert not [p for p in kernel if getattr(p, "partial", False)]
    d = "/var/tmp/d"
    seen = Counter(
        (p.type, getattr(p, "op", None) or getattr(p, "kind", None), path)
        for p in kernel
        if isinstance(path := getattr(p, "path", None), str) and path.startswith(d)
    )
    assert seen == {
        ("file.access", "mkdir", d): 1,
        ("file.access", "write", f"{d}/f"): 1,
        ("file.write", None, f"{d}/f"): 1,
        ("file.access", "rename", f"{d}/f"): 1,
        ("file.access", "symlink", f"{d}/s"): 1,
        ("file.access", "link", f"{d}/h"): 1,
        ("file.setattr", None, f"{d}/g"): 1,
        ("ipc.event", "fifo", f"{d}/p"): 1,
        **{("file.access", "delete", f"{d}{n}"): 1 for n in ("/s", "/h", "/p", "/g", "")},
    }


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
async def test_signal_flood_spills_without_losing_records(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    tracer = backend.tracer
    assert tracer is not None
    before = (tracer.garbled, tracer.kernel_lost, tracer.located)
    command = (
        "sleep 60 & p=$!; i=0; "
        f"while [ $i -lt {SIGNAL_FLOOD} ]; do kill -CONT $p; i=$((i+1)); done; "
        "echo $i; kill -KILL $p; wait $p 2>/dev/null; true"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=60)
        await asyncio.sleep(0.5)
    finally:
        await sb.close()

    assert result.stdout == f"{SIGNAL_FLOOD}\n".encode()
    finished = result.finished
    assert (finished.end, finished.exit_code, finished.dropped) == ("exited", 0, 0)
    cap = audit_module.CAPS["other"]
    # Every loop iteration sends SIGCONT; the final SIGKILL ends the target.
    expected_spilled = SIGNAL_FLOOD + 1 - cap
    assert finished.spilled == expected_spilled

    evs = await events(store, run_id)
    assert not [e for e in evs if e.type == "audit.lost"]
    signals = [e for e in evs if e.type == "proc.signal"]
    assert len(signals) == cap
    assert all(e.agent_id == "a1" and e.data["exec_id"] == finished.exec_id for e in signals)
    assert [e.data["signal"] for e in signals] == [18] * cap

    spills = [e.payload() for e in evs if e.type == "audit.spill"]
    spills = [s for s in spills if isinstance(s, AuditSpill)]
    assert spills
    assert all(s.exec_id == finished.exec_id for s in spills)
    assert sum(s.count for s in spills) == expected_spilled
    assert sum(s.kinds.get("other", 0) for s in spills) == expected_spilled

    lines = []
    for spill in spills:
        records = (await store.get_blob(run_id, spill.records.sha256)).decode().splitlines()
        assert all(record.startswith("{") and record.endswith("}") for record in records)
        lines += [json.loads(record) for record in records]
    assert len(lines) == expected_spilled
    assert {line["type"] for line in lines} == {"proc.signal"}
    assert all(
        line["data"]["exec_id"] == finished.exec_id
        and line["data"]["pid"] > 0
        and line["data"]["target"] != line["data"]["pid"]
        for line in lines
    )
    assert sorted({line["data"]["signal"] for line in lines}) == [9, 18]
    assert tracer.garbled == before[0]
    assert (tracer.kernel_lost, tracer.located) == before[1:]
    assert (await store.verify(run_id)).ok


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


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_ipc_events(backend: DockerBackend, store: LocalEventStore) -> None:
    command = (
        "python3 - <<'EOF'\n"
        "import ctypes, os, socket\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "s.bind('/work/s.sock')\n"
        "s.listen()\n"
        "c = socket.socket(socket.AF_UNIX)\n"
        "c.connect('/work/s.sock')\n"
        "c.close(); s.close()\n"
        "os.mkfifo('/work/f')\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]\n"
        "libc.shmget.restype = ctypes.c_int\n"
        "libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]\n"
        "libc.shmat.restype = ctypes.c_void_p\n"
        "i = libc.shmget(1234, 4096, 0o1600)\n"
        "print('shm', i, flush=True)\n"
        "print('mapped', bool(libc.shmat(i, None, 0)), flush=True)\n"
        "EOF"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    lines = result.stdout.decode().splitlines()
    assert lines[0].startswith("shm ")
    assert lines[1] == "mapped True"
    shmid = int(lines[0].split()[1])
    evs = await events(store, run_id, "ipc.event")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    ipc = [p for p in (e.payload() for e in evs) if isinstance(p, IpcEvent)]
    assert all(p.exec_id == result.finished.exec_id for p in ipc)
    seen = {(p.kind, p.family, p.path, p.partial) for p in ipc}
    assert {
        ("bind", "unix", "/work/s.sock", False),
        ("connect", "unix", "/work/s.sock", False),
        ("fifo", None, "/work/f", False),
    } <= seen
    assert any(
        p.kind == "shmget"
        and p.family == "sysv"
        and p.key == 1234
        and p.size == 4096
        and p.flags == 0o1600
        for p in ipc
    )
    assert any(
        p.kind == "shmat" and p.family == "sysv" and p.id == shmid and p.flags == 0 for p in ipc
    )


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_ptrace_event(backend: DockerBackend, store: LocalEventStore) -> None:
    command = (
        "python3 - <<'EOF'\n"
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]\n"
        "libc.ptrace.restype = ctypes.c_long\n"
        "import os\n"
        "print(os.getpid(), flush=True)\n"
        "print(libc.ptrace(16, 1, None, None), ctypes.get_errno(), flush=True)\n"
        "EOF"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    sender, ret, errno = (int(x) for x in result.stdout.split())
    assert (ret, errno) == (-1, 1)
    procs = [
        p
        for p in (e.payload() for e in await events(store, run_id, "proc.exec"))
        if isinstance(p, ProcExec) and p.ns_pid == sender
    ]
    assert len(procs) == 1
    assert procs[0].exec_id == result.finished.exec_id
    host_pid = procs[0].pid
    evs = await events(store, run_id, "ptrace.event")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    events_ = [p for p in (e.payload() for e in evs) if isinstance(p, PtraceEvent)]
    assert any(
        p.exec_id == result.finished.exec_id
        and p.pid == host_pid
        and p.target == 1
        and p.request == "attach"
        and p.refused
        and p.errno == 1
        for p in events_
    )


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_signal_event(backend: DockerBackend, store: LocalEventStore) -> None:
    command = "sleep 30 & p=$!; sleep 0.2; echo $p; kill -TERM $p; wait $p 2>/dev/null; true"
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    target_ns = int(result.stdout)
    procs = [
        p
        for p in (e.payload() for e in await events(store, run_id, "proc.exec"))
        if isinstance(p, ProcExec) and p.ns_pid == target_ns and p.argv[:1] == ["sleep"]
    ]
    assert len(procs) == 1
    assert procs[0].exec_id == result.finished.exec_id
    target = procs[0].pid
    evs = await events(store, run_id, "proc.signal")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    signals = [p for p in (e.payload() for e in evs) if isinstance(p, ProcSignal)]
    assert any(
        p.exec_id == result.finished.exec_id
        and p.target == target
        and p.signal == 15
        and p.pid != p.target
        for p in signals
    )


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_mem_exec(backend: DockerBackend, store: LocalEventStore) -> None:
    command = (
        "python3 - <<'EOF'\n"
        "import mmap, os\n"
        "open('/work/code', 'wb').write(b'\\x90' * 16)\n"
        "print('code', os.stat('/work/code').st_ino, flush=True)\n"
        "fd = os.open('/work/code', os.O_RDONLY)\n"
        "mmap.mmap(fd, 16, prot=mmap.PROT_READ | mmap.PROT_EXEC)\n"
        "os.close(fd)\n"
        "EOF\n"
        "python3 - <<'EOF'\n"
        "import os, shutil\n"
        "shutil.copy('/bin/true', '/work/x')\n"
        "fd = os.open('/work/x', os.O_RDONLY)\n"
        "print('exec', fd, os.stat('/work/x').st_ino, flush=True)\n"
        "os.unlink('/work/x')\n"
        "os.set_inheritable(fd, True)\n"
        "os.execve(f'/proc/self/fd/{fd}', ['x'], {})\n"
        "EOF"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    code_line, exec_line = result.stdout.decode().splitlines()
    code_inode = int(code_line.split()[1])
    fd, exec_inode = (int(x) for x in exec_line.split()[1:])
    evs = await events(store, run_id, "mem.exec")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    mem = [p for p in (e.payload() for e in evs) if isinstance(p, MemExec)]
    assert any(
        p.exec_id == result.finished.exec_id
        and p.kind == "exec"
        and p.path == f"/proc/self/fd/{fd}"
        and p.inode == exec_inode
        and p.nlink == 0
        and not p.partial
        for p in mem
    )
    assert any(
        p.exec_id == result.finished.exec_id
        and p.kind == "mmap"
        and p.path == "/work/code"
        and p.inode == code_inode
        and p.nlink == 1
        and p.prot == 5
        and not p.partial
        for p in mem
    )
    procs = [e.payload() for e in await events(store, run_id, "proc.exec")]
    assert any(
        p.exec_id == result.finished.exec_id
        and p.path == f"/proc/self/fd/{fd}"
        and p.inode == exec_inode
        and p.nlink == 0
        for p in procs
        if isinstance(p, ProcExec)
    )


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_file_setattr(backend: DockerBackend, store: LocalEventStore) -> None:
    command = (
        "echo hi > f; chmod 600 f; touch -d '2001-01-01' f; truncate -s 1 f; "
        'python3 -c "import os\n'
        "r, w = os.pipe()\n"
        "os.fchmod(r, 0o600)\n"
        "try: os.fchmod(os.eventfd(0), 0o600)\n"
        'except PermissionError: pass"'
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    evs = await events(store, run_id, "file.setattr")
    assert evs
    assert all(e.agent_id == "a1" for e in evs)
    attrs = [p for p in (e.payload() for e in evs) if isinstance(p, FileSetattr)]
    pseudo = {(p.path, p.partial, p.exec_id) for p in attrs if p.path.startswith(".../")}
    exec_id = result.finished.exec_id
    assert pseudo == {(".../", True, exec_id), (".../[eventfd]", True, exec_id)}
    assert not await events(store, run_id, "audit.lost")
    attrs = [p for p in attrs if p.path == "/work/f"]
    assert all(p.exec_id == result.finished.exec_id and not p.partial for p in attrs)
    assert any(
        p.valid & audit_module.ATTR_MODE
        and p.mode == 0o100600
        and p.uid is None
        and p.gid is None
        and p.size is None
        for p in attrs
    )
    assert any(p.valid & audit_module.ATTR_SIZE and p.size == 1 and p.mode is None for p in attrs)
    assert any(
        p.valid & (audit_module.ATTR_ATIME | audit_module.ATTR_MTIME)
        and p.atime_us == 978307200000000
        and p.mtime_us == 978307200000000
        for p in attrs
    )


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_refused_namespace_change_is_not_recorded(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = "unshare -Ur true 2>/dev/null; true"
    run_id, sb = await sandbox(backend, store)
    try:
        await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    assert not await events(store, run_id, "ns.event")
    denied = [e.payload() for e in await events(store, run_id, "sys.denied")]
    assert any(isinstance(p, SysDenied) and p.syscall == "unshare" for p in denied)


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_file_creation_records(backend: DockerBackend, store: LocalEventStore) -> None:
    long = "a" * 400
    command = (
        "mkdir -p /work/d/e && ln -s /etc/shadow /work/s && echo x > /work/f && ln /work/f /work/h"
        f" && ln -s {long} /work/long && rm -r /work/d /work/s /work/h /work/f /work/long"
    )
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(command, timeout_s=20)
    finally:
        await sb.close()

    assert result.finished.exit_code == 0
    exec_id = result.finished.exec_id
    payloads = [e.payload() for e in await events(store, run_id, "proc.exec", "file.access")]
    assert all(isinstance(p, KernelPayload) and p.exec_id == exec_id for p in payloads)
    pids = {p.pid: p.argv[0] for p in payloads if isinstance(p, ProcExec)}
    made = [
        (pids.get(p.pid), p.op, p.path, p.target, p.partial)
        for p in payloads
        if isinstance(p, FileAccess) and p.op in ("mkdir", "symlink", "link")
    ]
    assert made == [
        ("mkdir", "mkdir", "/work/d", None, False),
        ("mkdir", "mkdir", "/work/d/e", None, False),
        ("ln", "symlink", "/work/s", "/etc/shadow", False),
        ("ln", "link", "/work/h", "/work/f", False),
        ("ln", "symlink", "/work/long", long[: STRLEN - 4], True),
    ]
    deleted = {p.path for p in payloads if isinstance(p, FileAccess) and p.op == "delete"}
    assert {"/work/d/e", "/work/d", "/work/s", "/work/h", "/work/f", "/work/long"} <= deleted


def by_kernel_order(evs: list[Event]) -> list[KernelPayload]:
    kernel = [p for p in (e.payload() for e in evs) if isinstance(p, KernelPayload)]
    return sorted(kernel, key=lambda p: (p.ts_kernel, p.cpu, p.cpu_seq))


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_fork_only_child_outlives_its_exec(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    command = (
        'python3 -c "import os, time\n'
        "if os.fork() == 0:\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2): os.dup2(null, fd)\n"
        "    time.sleep(0.5)\n"
        "    with open('/work/late', 'w') as f: f.write('x')\n"
        '    os._exit(4)"'
    )
    run_id, sb = await sandbox(backend, store)
    try:
        first = await sb.exec(command, timeout_s=20)
        await sb.exec("sleep 1", timeout_s=20)
    finally:
        await sb.close()

    exec_id = first.finished.exec_id
    assert any(p.command.startswith("python3 -c") for p in first.finished.leftover)
    kernel = by_kernel_order(await events(store, run_id))
    python = next(p for p in kernel if isinstance(p, ProcExec) and p.argv[0] == "python3")
    (fork,) = [p for p in kernel if isinstance(p, ProcFork)]
    assert (fork.ppid, fork.exec_id) == (python.pid, exec_id)
    assert python.ts_kernel < fork.ts_kernel
    child = [p for p in kernel if p.pid == fork.pid]
    assert all(p.exec_id == exec_id for p in child)
    assert [type(p) for p in child] == [ProcFork, FileAccess, FileWrite, ProcExit]
    _, access, write, end = child
    assert isinstance(access, FileAccess)
    assert (access.op, access.path, access.partial) == ("write", "/work/late", False)
    assert isinstance(write, FileWrite)
    assert (write.path, write.size, write.partial) == ("/work/late", 1, False)
    assert isinstance(end, ProcExit)
    assert (end.exit_code, end.signal) == (4, None)
    assert (await store.verify(run_id)).ok


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_subshell_output_is_recorded_as_a_fork(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec("echo $(echo hi)", timeout_s=20)
    finally:
        await sb.close()

    assert result.stdout == b"hi\n"
    exec_id = result.finished.exec_id
    kernel = by_kernel_order(await events(store, run_id))
    sh = next(p for p in kernel if isinstance(p, ProcExec) and p.argv[:2] == ["/bin/sh", "-c"])
    (fork,) = [p for p in kernel if isinstance(p, ProcFork)]
    assert (fork.ppid, fork.exec_id) == (sh.pid, exec_id)
    child = [p for p in kernel if p.pid == fork.pid]
    assert [type(p) for p in child] == [ProcFork, ProcOutput, ProcExit]
    _, output, end = child
    assert isinstance(output, ProcOutput)
    assert (output.stream, output.prefix, output.bytes) == ("pipe", "hi\n", 3)
    assert isinstance(end, ProcExit)
    assert (end.exec_id, end.exit_code) == (exec_id, 0)
