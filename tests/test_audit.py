import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from aims.events import (
    AuditSpill,
    BlobRef,
    Draft,
    Event,
    FileSetattr,
    FsSnapshot,
    IpcEvent,
    LocalEventStore,
    MemExec,
    NsEvent,
    Proc,
    ProcExec,
    ProcSignal,
    PtraceEvent,
    ResourceSample,
    RunStarted,
)
from aims.events.store import INLINE_LIMIT
from aims.sandbox import audit as audit_module
from aims.sandbox.audit import AuditedSandbox, SandboxStoppedError
from aims.sandbox.base import (
    AGENT_UID,
    RUNNER_UID,
    Capture,
    Change,
    Close,
    Denied,
    Exec,
    Exit,
    Fd,
    File,
    Fork,
    Ipc,
    Lost,
    Mem,
    Net,
    Ns,
    Oom,
    Outcome,
    Output,
    PreExec,
    Ptrace,
    Record,
    Sample,
    SandboxInfo,
    SandboxSpec,
    Setattr,
    Signal,
    Stamp,
    Tree,
)

PIPEFS = 0x50495045

pytestmark = pytest.mark.anyio

AT = Stamp(42, 1, 7)
STAMP = {"ts_kernel": 42, "cpu": 1, "cpu_seq": 7}

Step = Callable[["FakeSandbox", Capture, Callable[[int], None] | None], Awaitable[Outcome]]


class FakeSandbox:
    def __init__(self, *, kernel: bool = True) -> None:
        self.queue: asyncio.Queue[Record] = asyncio.Queue()
        self.sample_queue: asyncio.Queue[Sample | Lost] | None = None
        self.kernel = kernel
        self.steps: list[Step] = []
        self.runs: list[list[str]] = []
        self.procs: list[Proc] = []
        self.hashes: dict[str, str] = {}
        self.after: list[Proc] | None = None
        self.trees: list[Tree | Exception] | None = None
        self.tree_hold: asyncio.Event | None = None
        self.tree_entered = asyncio.Event()
        self.killed = False
        self.closed = False

    @property
    def info(self) -> SandboxInfo:
        return SandboxInfo("sb1", "fake", "img", "sha256:" + "0" * 64, {"network": "none"})

    @property
    def audit(self) -> Any:
        return "kernel" if self.kernel else "snapshot"

    def wrapper(self, timeout_ms: int) -> list[str]:
        return ["wrap", str(timeout_ms), "sh", "-c"]

    async def run(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: str,
        timeout_ms: int,
        capture: Capture,
        on_pid: Callable[[int], None] | None = None,
    ) -> Outcome:
        self.runs.append(list(argv))
        step = self.steps.pop(0)
        outcome = await step(self, capture, on_pid)
        if self.after is not None:
            self.procs, self.after = self.after, None
        return outcome

    async def processes(self) -> list[Proc]:
        return list(self.procs)

    def records(self) -> asyncio.Queue[Record] | None:
        return self.queue if self.kernel else None

    def samples(self) -> asyncio.Queue[Sample | Lost] | None:
        return self.sample_queue

    async def tree(self) -> Tree | None:
        if self.trees is None:
            return None
        if self.tree_hold is not None:
            self.tree_entered.set()
            await self.tree_hold.wait()
        tree = self.trees.pop(0)
        if isinstance(tree, Exception):
            raise tree
        return tree

    async def hash(
        self,
        path: str,
        *,
        inode: int,
        dev: int = 0,
        size: int = 0,
        mtime_us: int = 0,
        ctime_us: int = 0,
    ) -> str | None:
        return self.hashes.get(path)

    def on_pre_exec(self, callback: Callable[[PreExec], None]) -> None:
        pass

    async def pause(self) -> None:
        pass

    async def resume(self) -> None:
        pass

    async def put_archive(self, path: str, data: bytes) -> None:
        pass

    async def get_archive(self, path: str) -> bytes:
        return b""

    async def kill(self) -> None:
        self.killed = True

    async def close(self, *, keep_volumes: bool = False) -> None:
        self.closed = True


class FakeBackend:
    name = "fake"

    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> FakeSandbox:
        return self.sandbox

    async def close(self) -> None:
        pass


def root(pid: int, uid: int = AGENT_UID) -> Exec:
    return Exec(pid, 1, pid, uid, "wrap", ("wrap", "sh"), 7, True, at=AT)


def tick(ts: int) -> Stamp:
    return Stamp(ts, 0, ts)


def tick_data(ts: int) -> dict[str, int]:
    return {"ts_kernel": ts, "cpu": 0, "cpu_seq": ts}


def child(pid: int, ppid: int, *argv: str) -> Exec:
    return Exec(pid, ppid, pid, AGENT_UID, argv[0], argv, 9, False, at=AT)


def with_context(e: Exec, *, cwd: str, env: tuple[str, ...], fds: tuple[Fd, Fd, Fd]) -> Exec:
    return dataclasses.replace(e, cwd=cwd, env=env, env_bytes=sum(len(x) + 1 for x in env), fds=fds)


def step(
    *records: Record,
    pid: int | None = 100,
    end: Any = "exited",
    code: int | None = 0,
    out: bytes = b"",
    error: str | None = None,
) -> Step:
    async def run(sb: FakeSandbox, capture: Capture, on_pid: Callable[[int], None] | None):
        if pid is not None and on_pid is not None:
            on_pid(pid)
        for record in records:
            sb.queue.put_nowait(record)
        capture.add(1, out)
        return Outcome(end, code, pid, 5, error)

    return run


@pytest.fixture(autouse=True)
def short_barrier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_module, "BARRIER_S", 0.2)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def start(
    store: LocalEventStore, sandbox: FakeSandbox, **spec: Any
) -> tuple[str, AuditedSandbox]:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe", agents=["a1"]))
    audited = await AuditedSandbox.start(
        FakeBackend(sandbox),
        SandboxSpec(image="img", **spec),
        store,
        run_id=run_id,
        agent_id="a1",
    )
    return run_id, audited


async def events(store: LocalEventStore, run_id: str, *types: str) -> list[Event]:
    return await store.events(run_id, limit=1000, types=types)


def kinds(evs: list[Event]) -> list[str]:
    return [e.type for e in evs]


def lineage(evs: list[Event]) -> list[tuple[str, object, object]]:
    kernel = [e for e in evs if "ts_kernel" in e.data]
    return [(e.type, e.data["pid"], e.data.get("ppid")) for e in kernel]


async def test_exec_writes_ahead_and_attributes_kernel_records(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=AT),
            child(101, 100, "python3", "x.py"),
            File(101, "write", "/work/x", at=AT),
            File(101, "write", "/work/x", at=AT),
            File(101, "read", "/work/scorer.py", at=AT),
            Net(101, "connect", "inet", "169.254.169.254", 80, at=AT),
            Denied(101, "unshare", 1, at=AT),
            Oom(101, at=AT),
            Exit(101, None, 9, at=AT),
            Exit(100, 0, None, at=AT),
            out=b"done\n",
        )
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("python3 x.py", timeout_s=5, call_id="c1")
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs) == [
        "run.started",
        "sandbox.started",
        "exec.started",
        "proc.exec",
        "proc.exec",
        "file.access",
        "file.access",
        "net.connect",
        "sys.denied",
        "proc.exit",
        "proc.exit",
        "exec.finished",
        "sandbox.stopped",
    ]
    started, finished = evs[2].data, evs[11].data
    exec_id = started["exec_id"]
    assert started["wrapper"] == ["wrap", "5000", "sh", "-c"]
    assert started["audit"] == "kernel"
    assert sb.runs == [["wrap", "5000", "sh", "-c", "python3 x.py"]]
    assert all(e.data["exec_id"] == exec_id for e in evs[3:12])
    assert all(e.agent_id == "a1" for e in evs[1:])
    assert evs[9].data == {
        "exec_id": exec_id,
        "pid": 101,
        "exit_code": None,
        "signal": 9,
        "oom": True,
        **STAMP,
    }
    assert all(e.data["ts_kernel"] == 42 for e in evs[3:11])
    assert finished["procs"] == 2
    assert finished["stdout"] == "done\n"
    assert finished["call_id"] == "c1"
    assert result.stdout == b"done\n"
    assert result.finished.end == "exited"
    assert (await store.verify(run_id)).ok


async def test_orphaned_child_keeps_its_exec(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.after = [Proc(pid=101, ppid=1, uid=AGENT_UID, command="sleep 30")]
    sb.steps.append(
        step(root(100), Fork(100, 101, at=AT), Exit(100, 0, None, at=AT), child(101, 1, "sleep"))
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("setsid sleep 30 &", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    procs = [e for e in evs if e.type == "proc.exec"]
    assert [e.data["exec_id"] for e in procs] == [result.finished.exec_id] * 2
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("proc.exit", 100, None),
        ("proc.fork", 101, 100),
        ("proc.exec", 101, 1),
    ]
    assert [p.command for p in result.finished.leftover] == ["sleep 30"]
    assert not result.finished.leftover_killed


async def test_fork_only_children_are_recorded_when_seen(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=tick(1)),
            Fork(101, 102, at=tick(2)),
            File(102, "write", "/work/x", at=tick(3)),
            Fork(100, 103, at=tick(4)),
            child(103, 100, "ls"),
            Fork(100, 104, at=tick(5)),
            Exit(104, 0, None, at=tick(6)),
            Fork(100, 105, at=tick(7)),
            Fork(105, 106, at=tick(8)),
            child(106, 105, "cat"),
            Fork(100, 107, at=tick(9)),
            Output(107, 7, 3, PIPEFS, b"hi\n", at=tick(10)),
            Exit(107, 0, None, at=tick(11)),
            *(Exit(pid, 0, None, at=AT) for pid in (102, 101, 103, 106, 105, 100)),
        )
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("proc.fork", 101, 100),
        ("proc.fork", 102, 101),
        ("file.access", 102, None),
        ("proc.exec", 103, 100),
        ("proc.fork", 105, 100),
        ("proc.exec", 106, 105),
        ("proc.fork", 107, 100),
        ("proc.output", 107, None),
        ("proc.exit", 107, None),
        ("proc.exit", 102, None),
        ("proc.exit", 101, None),
        ("proc.exit", 103, None),
        ("proc.exit", 106, None),
        ("proc.exit", 105, None),
        ("proc.exit", 100, None),
    ]
    exec_id = result.finished.exec_id
    fork = next(e for e in evs if e.type == "proc.fork")
    assert fork.data == {"exec_id": exec_id, "pid": 101, "ppid": 100, **tick_data(1)}
    assert all(e.data["exec_id"] == exec_id for e in evs if "ts_kernel" in e.data)
    assert result.finished.procs == 3
    assert (await store.verify(run_id)).ok


async def test_a_dead_parent_is_recorded_before_its_child(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=tick(1)),
            Fork(101, 102, at=tick(2)),
            Exit(101, 3, None, at=tick(3)),
            File(102, "write", "/work/late", at=tick(4)),
            Fork(100, 103, at=tick(5)),
            Fork(103, 104, at=tick(6)),
            Exit(103, 0, None, at=tick(7)),
            Fork(100, 103, at=tick(8)),
            File(104, "write", "/work/y", at=tick(9)),
            File(103, "write", "/work/z", at=tick(10)),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("proc.fork", 101, 100),
        ("proc.exit", 101, None),
        ("proc.fork", 102, 101),
        ("file.access", 102, None),
        ("proc.fork", 104, 103),
        ("file.access", 104, None),
        ("proc.fork", 103, 100),
        ("file.access", 103, None),
    ]
    dead = [e.data for e in evs if e.data.get("pid") == 101]
    assert [(d["ts_kernel"], d.get("exit_code")) for d in dead] == [(1, None), (3, 3)]
    reused = next(e for e in evs if e.type == "proc.fork" and e.data["pid"] == 103)
    assert reused.data["ts_kernel"] == 8


async def test_files_closed_after_an_exit_keep_their_exec(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=tick(1)),
            Exit(101, 0, None, at=tick(2)),
            Close(101, 3, "/work/a", at=tick(3)),
            Close(101, 4, "/work/a", at=tick(4)),
            Fork(100, 102, at=tick(5)),
            File(102, "write", "/work/b", at=tick(6)),
            Exit(102, 0, None, at=tick(7)),
            Close(102, 2, "/work/b", at=tick(8)),
            Exit(100, 0, None, at=tick(9)),
            Close(100, 1, "/work/c", at=tick(10)),
            Fork(100, 103, at=tick(11)),
            File(103, "write", "/work/d", at=tick(12)),
        )
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("proc.fork", 101, 100),
        ("proc.exit", 101, None),
        ("file.write", 101, None),
        ("file.write", 101, None),
        ("proc.fork", 102, 100),
        ("file.access", 102, None),
        ("proc.exit", 102, None),
        ("file.write", 102, None),
        ("proc.exit", 100, None),
        ("file.write", 100, None),
        ("proc.fork", 103, 100),
        ("file.access", 103, None),
    ]
    exec_id = result.finished.exec_id
    assert all(e.data["exec_id"] == exec_id for e in evs if "ts_kernel" in e.data)
    assert [e.data["size"] for e in evs if e.type == "file.write"] == [3, 4, 2, 1]


async def test_forks_follow_their_event_into_the_spill(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "procs", 1)
    monkeypatch.setitem(audit_module.CAPS, "files", 2)
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            File(100, "write", "/work/a", at=AT),
            Fork(100, 101, at=tick(1)),
            File(101, "write", "/work/b", at=tick(2)),
            Fork(100, 102, at=tick(3)),
            File(102, "write", "/work/c", at=tick(4)),
            Exit(101, 0, None, at=tick(5)),
            Exit(102, 0, None, at=tick(6)),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("file.access", 100, None),
        ("proc.fork", 101, 100),
        ("file.access", 101, None),
        ("proc.exit", 101, None),
        ("proc.exit", 100, None),
    ]
    assert (result.finished.procs, result.finished.spilled, result.finished.dropped) == (1, 3, 0)
    spill = next(e.payload() for e in evs if e.type == "audit.spill")
    assert isinstance(spill, AuditSpill)
    assert spill.kinds == {"procs": 2, "files": 1}
    records = spilled(await store.get_blob(run_id, spill.records.sha256))
    assert [(t, d["pid"], d.get("ppid")) for t, d in records] == [
        ("proc.fork", 102, 100),
        ("file.access", 102, None),
        ("proc.exit", 102, None),
    ]


async def test_an_exec_keeps_its_exit_in_the_log_after_a_spilled_fork(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "files", 1)
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            File(100, "write", "/work/a", at=AT),
            Fork(100, 101, at=tick(1)),
            Close(101, 3, "/tmp/out", at=tick(2)),
            child(101, 100, "true"),
            Exit(101, 0, None, at=tick(3)),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert lineage(evs) == [
        ("proc.exec", 100, 1),
        ("file.access", 100, None),
        ("proc.exec", 101, 100),
        ("proc.exit", 101, None),
        ("proc.exit", 100, None),
    ]
    spill = next(e.payload() for e in evs if e.type == "audit.spill")
    assert isinstance(spill, AuditSpill)
    records = spilled(await store.get_blob(run_id, spill.records.sha256))
    assert [(t, d["pid"]) for t, d in records] == [("proc.fork", 101), ("file.write", 101)]


async def test_runner_and_foreign_roots_are_not_recorded(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(step(root(200, uid=RUNNER_UID), root(100), root(300), child(301, 300, "x")))
    run_id, audited = await start(store, sb)
    await audited.exec("true", timeout_s=5)
    await audited.close()

    procs = [e.data["pid"] for e in await events(store, run_id) if e.type == "proc.exec"]
    assert procs == [100]


async def test_root_seen_before_its_pid_is_known(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(step(root(100), child(101, 100, "ls"), Exit(100, 0, None, at=AT), pid=None))
    run_id, audited = await start(store, sb)
    result = await audited.exec("ls", timeout_s=5)
    await audited.close()

    procs = [e for e in await events(store, run_id) if e.type == "proc.exec"]
    assert [e.data["exec_id"] for e in procs] == [result.finished.exec_id] * 2


def late(*records: Record, pid: int) -> Step:
    """An exec whose records are recorded before the runtime reports its pid."""

    async def run(sb: FakeSandbox, capture: Capture, on_pid: Callable[[int], None] | None):
        for record in records:
            sb.queue.put_nowait(record)
        await asyncio.sleep(0.05)
        if on_pid is not None:
            on_pid(pid)
        return Outcome("exited", 0, pid, 5)

    return run


async def test_a_reused_root_pid_goes_to_the_new_exec(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(late(root(100), Exit(100, 0, None, at=AT), pid=100))
    sb.steps.append(step(root(100), child(101, 100, "ls"), pid=None))
    run_id, audited = await start(store, sb)
    first = await audited.exec("true", timeout_s=5)
    second = await audited.exec("ls", timeout_s=5)
    await audited.close()

    procs = [e.data["exec_id"] for e in await events(store, run_id) if e.type == "proc.exec"]
    ids = (first.finished.exec_id, second.finished.exec_id)
    assert procs == [ids[0], ids[1], ids[1]]


async def test_a_reused_root_pid_of_a_runner_exec_is_recorded(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.after = [Proc(pid=9, ppid=1, uid=AGENT_UID, command="miner")]
    sb.steps.append(step(root(100), Exit(100, 0, None, at=AT)))
    sb.steps.append(late(root(200), Exit(200, 0, None, at=AT), pid=200))
    sb.steps.append(step(root(200), pid=None))
    run_id, audited = await start(store, sb, kill_leftovers=True)
    first = await audited.exec("miner &", timeout_s=5)
    second = await audited.exec("ls", timeout_s=5)
    await audited.close()

    assert first.finished.leftover_killed
    procs = [
        (e.data["pid"], e.data["exec_id"])
        for e in await events(store, run_id)
        if e.type == "proc.exec"
    ]
    assert procs == [(100, first.finished.exec_id), (200, second.finished.exec_id)]


def spilled(data: bytes) -> list[tuple[str, dict]]:
    lines = [json.loads(line) for line in data.decode().splitlines()]
    return [(line["type"], line["data"]) for line in lines]


async def test_caps_spill_what_they_do_not_log(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "procs", 3)
    monkeypatch.setitem(audit_module.CAPS, "files", 2)
    sb = FakeSandbox()
    kids = [child(101 + i, 100, "c") for i in range(4)]
    files = [File(101, "write", f"/work/{i}", at=AT) for i in range(4)]
    exits = [Exit(104, 0, None, at=AT), Exit(100, 0, None, at=AT)]
    sb.steps.append(step(root(100), *kids, *files, File(101, "write", "/work/3", at=AT), *exits))
    run_id, audited = await start(store, sb)
    result = await audited.exec("make", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs).count("proc.exec") == 3
    assert kinds(evs).count("file.access") == 2
    assert kinds(evs)[-3:] == ["audit.spill", "exec.finished", "sandbox.stopped"]
    assert (result.finished.procs, result.finished.spilled, result.finished.dropped) == (3, 5, 0)
    spill = evs[-3].payload()
    assert isinstance(spill, AuditSpill)
    assert spill.exec_id == result.finished.exec_id
    assert (spill.count, spill.kinds, spill.ts_from, spill.ts_to) == (
        5,
        {"procs": 3, "files": 2},
        42,
        42,
    )
    records = spilled(await store.get_blob(run_id, spill.records.sha256))
    assert [(t, d.get("pid"), d.get("path")) for t, d in records] == [
        ("proc.exec", 103, "c"),
        ("proc.exec", 104, "c"),
        ("file.access", 101, "/work/2"),
        ("file.access", 101, "/work/3"),
        ("proc.exit", 104, None),
    ]
    assert all(d["exec_id"] == result.finished.exec_id for _, d in records)


async def test_spill_is_bounded_and_late_records_still_spill(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "files", 1)
    monkeypatch.setattr(audit_module, "SPILL_BYTES", 600)
    sb = FakeSandbox()
    files = [File(100, "write", f"/work/{i}", at=AT) for i in range(6)]
    sb.steps.append(step(root(100), *files))
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    sb.queue.put_nowait(File(100, "write", "/work/late", at=AT))
    await asyncio.sleep(0.2)
    await audited.close()

    finished = result.finished
    assert finished.spilled + finished.dropped == 5
    assert finished.dropped > 0
    spills = [e.payload() for e in await events(store, run_id, "audit.spill")]
    assert [s.count for s in spills if isinstance(s, AuditSpill)] == [finished.spilled, 1]
    assert all(isinstance(s, AuditSpill) and s.exec_id == finished.exec_id for s in spills)


async def test_losses_are_reported_once_per_flush(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(root(100), Lost(3, "kernel buffer overflow"), Lost(2, "kernel buffer overflow"))
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    sb.queue.put_nowait(Lost(None, "tracer stopped"))
    await asyncio.sleep(0.3)
    await audited.close()

    lost = [e.data for e in await events(store, run_id) if e.type == "audit.lost"]
    none = {"cpus": [], "ts_from": None, "ts_to": None}
    assert lost == [
        {"count": 5, "reason": "kernel buffer overflow", **none},
        {"count": None, "reason": "tracer stopped", **none},
    ]


async def test_gaps_keep_their_window(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    sb.steps.append(
        step(
            root(100),
            Lost(3, "kernel record gap", (2,), 500, 900),
            Lost(1, "kernel record gap", (0,), 100, 300),
            Lost(4, "audit queue full", (), 50, 60),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    lost = [e.data for e in await events(store, run_id) if e.type == "audit.lost"]
    assert lost == [
        {"count": 4, "reason": "kernel record gap", "cpus": [0, 2], "ts_from": 100, "ts_to": 900},
        {"count": 4, "reason": "audit queue full", "cpus": [], "ts_from": 50, "ts_to": 60},
    ]


async def test_close_records_what_is_still_queued(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    run_id, audited = await start(store, sb)
    assert audited._consumer is not None
    audited._consumer.cancel()
    sb.queue.put_nowait(File(100, "write", "/work/late", at=AT))
    sb.queue.put_nowait(Lost(2, "kernel record gap", (1,), 1, 2))
    await audited.close()

    evs = await events(store, run_id)
    assert [e.data["path"] for e in evs if e.type == "file.access"] == ["/work/late"]
    assert [e.data["count"] for e in evs if e.type == "audit.lost"] == [2]
    assert kinds(evs)[-1] == "sandbox.stopped"


async def test_snapshot_audit_has_no_kernel_counts(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.steps.append(step(code=3))
    run_id, audited = await start(store, sb)
    result = await audited.exec("exit 3", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs) == [
        "run.started",
        "sandbox.started",
        "exec.started",
        "exec.finished",
        "sandbox.stopped",
    ]
    assert evs[1].data["audit"] == evs[2].data["audit"] == "snapshot"
    assert result.finished.procs is None
    assert result.finished.exit_code == 3


async def test_large_and_binary_output_goes_to_blobs(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    big = b"a" * (INLINE_LIMIT + 1)
    sb.steps += [step(out=big), step(out=b"\xff\xfe")]
    run_id, audited = await start(store, sb, output_limit=INLINE_LIMIT + 1)
    first = await audited.exec("big", timeout_s=5)
    second = await audited.exec("bin", timeout_s=5)
    await audited.close()

    assert isinstance(first.finished.stdout, BlobRef)
    assert await store.get_blob(run_id, first.finished.stdout.sha256) == big
    assert isinstance(second.finished.stdout, BlobRef)
    assert await store.get_blob(run_id, second.finished.stdout.sha256) == b"\xff\xfe"


async def test_cancelled_exec_is_recorded(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    gate = asyncio.Event()

    async def hang(sb: FakeSandbox, capture: Capture, on_pid: Callable[[int], None] | None):
        capture.add(1, b"partial")
        await gate.wait()
        raise AssertionError("not reached")

    sb.steps.append(hang)
    run_id, audited = await start(store, sb)
    task = asyncio.create_task(audited.exec("sleep 99", timeout_s=60))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await audited.close()

    finished = next(e.data for e in await events(store, run_id) if e.type == "exec.finished")
    assert finished["end"] == "cancelled"
    assert finished["stdout"] == "partial"


async def test_failed_exec_stops_the_sandbox(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.steps.append(step(end="failed", code=None, pid=None, error="pids exhausted"))
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    with pytest.raises(SandboxStoppedError):
        await audited.exec("y", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs)[-2:] == ["exec.finished", "sandbox.stopped"]
    assert result.finished.error == "pids exhausted"
    assert evs[-1].data["reason"] == "exec_failed"
    assert sb.killed
    assert sb.closed


async def test_leftovers_killed_when_asked(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.procs = [Proc(pid=5, ppid=1, uid=RUNNER_UID, command="sleep infinity")]
    sb.after = [*sb.procs, Proc(pid=9, ppid=1, uid=AGENT_UID, command="miner")]
    sb.steps += [step(), step(pid=None)]
    _, audited = await start(store, sb, kill_leftovers=True)
    result = await audited.exec("miner &", timeout_s=5)
    await audited.close()

    assert [p.command for p in result.finished.leftover] == ["miner"]
    assert result.finished.leftover_killed
    assert sb.runs[-1] == ["/.aims/busybox", "kill", "-KILL", "-1"]


async def test_unexpected_error_still_finishes_the_exec(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)

    async def boom(sb: FakeSandbox, capture: Capture, on_pid: Callable[[int], None] | None):
        raise ConnectionResetError("stream reset")

    sb.steps.append(boom)
    run_id, audited = await start(store, sb)
    with pytest.raises(ConnectionResetError):
        await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs)[-2:] == ["exec.finished", "sandbox.stopped"]
    finished = next(e.data for e in evs if e.type == "exec.finished")
    assert finished["end"] == "failed"
    assert finished["error"] == "ConnectionResetError: stream reset"
    assert kinds(evs).count("sandbox.stopped") == 1


def reading(ts: int, cpu: int) -> Sample:
    values = [cpu, *range(1, len(ResourceSample.COLUMNS) - 2), 100, 3]
    return Sample(ts, tuple(values))


async def window_events(store: LocalEventStore, run_id: str) -> list[ResourceSample]:
    events = await store.events(run_id, limit=1000, types=["resource.sample"])
    return [ResourceSample.model_validate(e.data) for e in events]


async def test_audit_records_windows_without_a_tracer(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.sample_queue = asyncio.Queue()
    run_id, audited = await start(store, sb, sample_hz=10)
    for ts in (1_000_000, 1_500_000, 2_000_000, 2_100_000):
        sb.sample_queue.put_nowait(reading(ts, ts // 1000))
    sb.sample_queue.put_nowait(Lost(None, "resource sampler stopped: exit 137"))
    sb.sample_queue.put_nowait(reading(3_000_000, 9))
    await asyncio.sleep(0.1)
    sb.sample_queue.put_nowait(reading(3_100_000, 10))
    await audited.close()

    evs = await store.events(run_id, limit=1000)
    kinds = [e.type for e in evs]
    assert kinds[-1] == "sandbox.stopped"
    windows = await window_events(store, run_id)
    assert [w.values("ts") for w in windows] == [
        [1_000_000, 1_500_000],
        [2_000_000, 2_100_000],
        [3_000_000, 3_100_000],
    ]
    assert all(w.hz == 10 and w.sandbox_id == "sb1" for w in windows)
    lost = [e.data for e in evs if e.type == "audit.lost"]
    assert [x["reason"] for x in lost] == ["resource sampler stopped: exit 137"]
    assert kinds.index("audit.lost") > kinds.index("resource.sample")

    rows = await store.samples(run_id)
    assert [(r.ts_from, r.ts_to, r.count, r.agent_id) for r in rows] == [
        (1_000_000, 1_500_000, 2, "a1"),
        (2_000_000, 2_100_000, 2, "a1"),
        (3_000_000, 3_100_000, 2, "a1"),
    ]
    assert rows[1].cpu_usec == 2_100
    assert [r.ts_from for r in await store.samples(run_id, ts_from=2_050_000)] == [
        2_000_000,
        3_000_000,
    ]
    assert await store.samples(run_id, agent_id="nobody") == []
    assert not [e for e in await store.events(run_id, query="sb1") if e.type == "resource.sample"]

    await store.rebuild()
    assert await store.samples(run_id) == rows


async def test_stop_records_the_open_window_first(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.sample_queue = asyncio.Queue()
    run_id, audited = await start(store, sb)
    sb.sample_queue.put_nowait(reading(5_000_000, 1))
    await asyncio.sleep(0.05)
    await audited.stop()
    await audited.close()

    kinds = [e.type for e in await store.events(run_id, limit=1000)]
    assert kinds[-2:] == ["resource.sample", "sandbox.stopped"]
    assert kinds.count("sandbox.stopped") == 1


def tree(digest: str, *changes: Change, **counts: Any) -> Tree:
    fields: dict[str, Any] = {
        "entries": 3,
        "files": 2,
        "bytes": 10,
        "hashed_bytes": 0,
        "unhashed": 0,
        "content_skipped": 0,
        "truncated": False,
        "duration_ms": 1,
    } | counts
    return Tree(roots=("/work",), digest=digest, changes=changes, **fields)


def change(path: str, kind: Any = "modified", data: bytes | None = None) -> Change:
    return Change(path, kind, "file", len(data or b""), 0o644, 1, "ab" * 32, data)


async def test_file_writes_are_recorded_once_per_size(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    closes = [Close(101, size, "/work/log", at=AT) for size in (3, 3, 9)]
    sb.steps.append(step(root(100), Fork(100, 101, at=AT), *closes, Exit(100, 0, None, at=AT)))
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    writes = [e.data for e in await events(store, run_id, "file.write")]
    assert writes == [
        {
            "exec_id": result.finished.exec_id,
            "pid": 101,
            "path": "/work/log",
            "size": s,
            "partial": False,
            **STAMP,
        }
        for s in (3, 9)
    ]


async def test_trees_record_each_window(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.trees = [
        tree("d0", change("/work/task.py", "added", b"task")),
        tree("d0"),
        tree("d1", change("/work/scorer.py", data=b"rigged"), change("/work/x", "removed")),
        tree("d2", change("/work/late", "added", b"bg")),
    ]
    sb.steps.append(step())
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs) == [
        "run.started",
        "sandbox.started",
        "fs.snapshot",
        "exec.started",
        "fs.snapshot",
        "exec.finished",
        "fs.snapshot",
        "sandbox.stopped",
    ]
    snaps = [e.payload() for e in evs if e.type == "fs.snapshot"]
    assert all(isinstance(s, FsSnapshot) for s in snaps)
    first, during, after = (s for s in snaps if isinstance(s, FsSnapshot))
    assert (first.exec_id, first.base, first.digest) == (None, None, "d0")
    assert (during.exec_id, during.base, during.digest) == (result.finished.exec_id, "d0", "d1")
    assert (after.exec_id, after.base) == (None, "d1")
    scorer, removed = during.changes
    assert scorer.content is not None
    assert await store.get_blob(run_id, scorer.content.sha256) == b"rigged"
    assert (removed.change, removed.content) == ("removed", None)
    assert (await store.verify(run_id)).ok


async def test_tree_overflow_and_failures(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_module, "CHANGES", 2)
    sb = FakeSandbox(kernel=False)
    many = [change(f"/work/{i}", "added") for i in range(5)]
    sb.trees = [tree("d0", *many), RuntimeError("reader gone"), tree("d1"), tree("d1")]
    sb.steps.append(step())
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    first = evs[2].payload()
    assert isinstance(first, FsSnapshot)
    assert (first.total, len(first.changes)) == (5, 2)
    assert first.more is not None
    rest = (await store.get_blob(run_id, first.more.sha256)).decode().splitlines()
    assert [json.loads(r)["path"] for r in rest] == ["/work/2", "/work/3", "/work/4"]
    lost = [e.data["reason"] for e in evs if e.type == "audit.lost"]
    assert lost == ["file snapshot failed: reader gone"]
    assert kinds(evs).count("fs.snapshot") == 2


async def test_exec_context_is_recorded_and_env_deduped(store: LocalEventStore) -> None:
    fds = (Fd(0, "char"), Fd(500, "pipe"), Fd(501, "pipe"))
    env = ("A=1", "LD_PRELOAD=/x")
    r = with_context(root(100), cwd="/work", env=env, fds=fds)
    kid = with_context(child(101, 100, "python3"), cwd="/work/sub", env=env, fds=fds)
    changed = with_context(child(102, 100, "sh"), cwd="/work", env=("A=2",), fds=fds)
    sb = FakeSandbox()
    sb.steps.append(
        step(
            r,
            Fork(100, 101, at=AT),
            kid,
            Fork(100, 102, at=AT),
            changed,
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    procs = {e.data["pid"]: e.data for e in await events(store, run_id) if e.type == "proc.exec"}
    assert procs[100]["cwd"] == "/work"
    assert procs[100]["env"] == ["A=1", "LD_PRELOAD=/x"]
    assert procs[100]["fds"] == [
        {"ino": 0, "kind": "char"},
        {"ino": 500, "kind": "pipe"},
        {"ino": 501, "kind": "pipe"},
    ]
    assert procs[101]["env"] == []
    assert procs[101]["env_inherited"] is True
    assert procs[102]["env"] == ["A=2"]
    assert procs[102]["env_inherited"] is False


async def test_pipe_output_is_attributed_and_aggregated(store: LocalEventStore) -> None:
    fds = (Fd(0, "char"), Fd(500, "pipe"), Fd(501, "pipe"))
    r = with_context(root(100), cwd="/work", env=(), fds=fds)
    sb = FakeSandbox()
    sb.steps.append(
        step(
            r,
            Output(100, 500, 20, PIPEFS, b"hello", at=Stamp(50, 0, 3)),
            Output(100, 500, 8, PIPEFS, None, at=Stamp(60, 0, 4)),
            Output(100, 700, 5, 0xEF53, b"fifo!", at=Stamp(70, 0, 5)),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    outs = {e.data["inode"]: e.data for e in await events(store, run_id) if e.type == "proc.output"}
    assert outs[500]["stream"] == "stdout"
    assert (outs[500]["bytes"], outs[500]["writes"]) == (28, 2)
    assert outs[500]["prefix"] == "hello"
    assert outs[500]["truncated"] is True
    assert (outs[500]["ts_kernel"], outs[500]["cpu_seq"]) == (50, 3)
    assert outs[700]["stream"] == "fifo"
    assert outs[700]["prefix"] == "fifo!"


async def test_output_over_cap_spills_before_the_exec_finishes(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "output", 1)
    fds = (Fd(0, "char"), Fd(500, "pipe"), Fd(501, "pipe"))
    r = with_context(root(100), cwd="/work", env=(), fds=fds)
    writes = [Output(100, ino, 4, PIPEFS, b"data", at=AT) for ino in (500, 501, 700)]
    sb = FakeSandbox()
    sb.steps.append(step(r, *writes, Exit(100, 0, None, at=AT)))
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs)[3:] == [
        "proc.exec",
        "proc.output",
        "proc.exit",
        "audit.spill",
        "exec.finished",
        "sandbox.stopped",
    ]
    assert evs[4].data["stream"] == "stdout"
    assert (result.finished.spilled, result.finished.dropped) == (2, 0)
    spill = evs[6].payload()
    assert isinstance(spill, AuditSpill)
    assert spill.kinds == {"output": 2}
    records = spilled(await store.get_blob(run_id, spill.records.sha256))
    assert [(t, d["stream"]) for t, d in records] == [
        ("proc.output", "stderr"),
        ("proc.output", "pipe"),
    ]


async def test_a_cancelled_exec_still_logs_its_diff(store: LocalEventStore) -> None:
    sb = FakeSandbox(kernel=False)
    sb.trees = [tree("d0"), tree("d1", change("/work/plan", "added", b"x")), tree("d1")]
    run_id, audited = await start(store, sb)
    sb.tree_hold = asyncio.Event()
    task = asyncio.create_task(audited.exec("x", timeout_s=5))
    await sb.tree_entered.wait()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    sb.tree_hold.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await events(store, run_id, "fs.snapshot")) == 2
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs) == [
        "run.started",
        "sandbox.started",
        "fs.snapshot",
        "fs.snapshot",
        "sandbox.stopped",
    ]
    diff = evs[3].payload()
    assert isinstance(diff, FsSnapshot)
    assert (diff.base, diff.digest, [c.path for c in diff.changes]) == ("d0", "d1", ["/work/plan"])
    assert sb.runs == []


async def test_a_failed_start_closes_the_sandbox(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    append = store.append

    async def no_snapshots(run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        if any(isinstance(d.payload, FsSnapshot) for d in drafts):
            raise OSError("disk full")
        return await append(run_id, drafts)

    sb = FakeSandbox()
    sb.trees = [tree("d0")]
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe", agents=["a1"]))
    monkeypatch.setattr(store, "append", no_snapshots)
    with pytest.raises(OSError, match="disk full"):
        await AuditedSandbox.start(
            FakeBackend(sb), SandboxSpec(image="img"), store, run_id=run_id, agent_id="a1"
        )

    assert sb.closed
    evs = await events(store, run_id)
    assert kinds(evs) == ["run.started", "sandbox.started", "sandbox.stopped"]
    assert evs[-1].data["detail"] == "start failed: OSError: disk full"


async def test_env_is_only_deduped_when_read_whole(store: LocalEventStore) -> None:
    fds = (Fd(0, "char"), Fd(500, "pipe"), Fd(501, "pipe"))
    head = tuple(f"V{i}={'x' * 500}" for i in range(8))
    parent = with_context(root(100), cwd="/work", env=head, fds=fds)
    parent = dataclasses.replace(parent, env_bytes=6000)
    longer = dataclasses.replace(parent, pid=101, ppid=100, root=False, env_bytes=6100)
    same = dataclasses.replace(parent, pid=102, ppid=100, root=False)
    sb = FakeSandbox()
    sb.steps.append(
        step(
            parent,
            Fork(100, 101, at=AT),
            longer,
            Fork(100, 102, at=AT),
            same,
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    procs = {e.data["pid"]: e.data for e in await events(store, run_id) if e.type == "proc.exec"}
    assert [procs[pid]["env_inherited"] for pid in (100, 101, 102)] == [False, False, False]
    assert all(procs[pid]["env"] == list(head) for pid in (100, 101, 102))


async def test_an_unstored_diff_is_recorded_as_lost(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(run_id: str, datas: Sequence[bytes]) -> list[BlobRef]:
        raise OSError("disk full")

    sb = FakeSandbox(kernel=False)
    sb.trees = [tree("d0"), tree("d1", change("/work/a", "added", b"a")), tree("d1"), tree("d1")]
    sb.steps.append(step())
    run_id, audited = await start(store, sb)
    with monkeypatch.context() as m:
        m.setattr(store, "put_blobs", broken)
        await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs) == [
        "run.started",
        "sandbox.started",
        "fs.snapshot",
        "audit.lost",
        "exec.started",
        "fs.snapshot",
        "exec.finished",
        "sandbox.stopped",
    ]
    assert evs[3].data["reason"] == "file snapshot not stored: disk full"
    assert evs[5].data["base"] == "d1"


async def test_exec_hashes_a_file_under_a_captured_root(store: LocalEventStore) -> None:
    digest = "a" * 64
    sb = FakeSandbox()
    sb.hashes["/work/x"] = digest
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=AT),
            dataclasses.replace(child(101, 100, "./x"), cwd="/work"),
            Exit(101, 0, None, at=AT),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    procs = [e.payload() for e in await events(store, run_id, "proc.exec")]
    proc = next(p for p in procs if isinstance(p, ProcExec) and p.argv == ["./x"])
    assert proc.sha256 == digest
    root_proc = next(p for p in procs if isinstance(p, ProcExec) and p.root)
    assert root_proc.sha256 is None


async def test_pre_exec_hashes_before_the_exec(store: LocalEventStore) -> None:
    digest = "b" * 64
    sb = FakeSandbox()
    sb.hashes["/work/x"] = digest
    root_exec = dataclasses.replace(root(100), cwd="/work")
    child_exec = dataclasses.replace(child(101, 100, "./x"), cwd="/other")
    sb.steps.append(
        step(
            root_exec,
            Fork(100, 101, at=AT),
            PreExec(101, 100, "./x", 0, at=AT),
            child_exec,
            Exit(101, 0, None, at=AT),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    await audited.exec("x", timeout_s=5)
    await audited.close()

    procs = [e.payload() for e in await events(store, run_id, "proc.exec")]
    proc = next(p for p in procs if isinstance(p, ProcExec) and p.argv == ["./x"])
    assert proc.sha256 == digest


async def test_output_streams_stay_within_their_cap(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_module, "MEMORY", 1)
    kids = [
        r
        for pid in range(101, 107)
        for r in (Fork(100, pid, at=AT), Output(pid, 7, 1, PIPEFS, b"x", at=AT))
    ]
    sb = FakeSandbox()
    sb.steps.append(step(root(100), *kids, Exit(100, 0, None, at=AT)))
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)

    assert (len(audited._outputs), audited._output_streams) == (4, 4)
    assert result.finished.dropped == 2
    await audited.close()
    assert len(await events(store, run_id, "proc.output")) == 4


async def test_new_kernel_records_are_mapped(store: LocalEventStore) -> None:
    sb = FakeSandbox()
    exe = dataclasses.replace(child(101, 100, "sh"), inode=7, dev=254, nlink=0, magic=61267)
    sb.steps.append(
        step(
            root(100),
            Fork(100, 101, at=AT),
            exe,
            Ipc(101, "bind", "unix", "/work/s.sock", at=AT),
            Ptrace(101, 1, "attach", True, 1, at=AT),
            Signal(101, 102, 15, at=AT),
            Mem(101, "mmap", "/work/x.so", 8, 1, 5, at=AT),
            Setattr(101, "/work/f", False, 127, 33152, 0, 0, 1, 0, 0, 0, at=AT),
            Ns(101, "unshare", 268435456, ("user",), at=AT),
            Exit(101, 0, None, at=AT),
            Exit(100, 0, None, at=AT),
        )
    )
    run_id, audited = await start(store, sb)
    result = await audited.exec("x", timeout_s=5)
    await audited.close()

    evs = await events(
        store,
        run_id,
        "proc.exec",
        "ipc.event",
        "ptrace.event",
        "proc.signal",
        "mem.exec",
        "file.setattr",
        "ns.event",
    )
    exec_id = result.finished.exec_id
    assert all(e.data["exec_id"] == exec_id for e in evs[1:])
    assert all(e.data["ts_kernel"] == 42 for e in evs[1:])
    procs = [e.payload() for e in evs if e.type == "proc.exec"]
    proc = next(p for p in procs if isinstance(p, ProcExec) and p.pid == 101)
    assert (proc.inode, proc.dev, proc.nlink, proc.magic) == (7, 254, 0, 61267)

    by_type = {e.type: e.payload() for e in evs[1:]}
    ipc, ptrace, signal = by_type["ipc.event"], by_type["ptrace.event"], by_type["proc.signal"]
    mem, setattr, ns = by_type["mem.exec"], by_type["file.setattr"], by_type["ns.event"]
    assert isinstance(ipc, IpcEvent)
    assert (ipc.kind, ipc.family, ipc.path) == ("bind", "unix", "/work/s.sock")
    assert isinstance(ptrace, PtraceEvent)
    assert (ptrace.target, ptrace.request, ptrace.refused, ptrace.errno) == (1, "attach", True, 1)
    assert isinstance(signal, ProcSignal)
    assert (signal.target, signal.signal) == (102, 15)
    assert isinstance(mem, MemExec)
    assert (mem.kind, mem.path, mem.inode, mem.nlink, mem.prot) == (
        "mmap",
        "/work/x.so",
        8,
        1,
        5,
    )
    assert isinstance(setattr, FileSetattr)
    assert (setattr.mode, setattr.uid, setattr.gid, setattr.size) == (33152, 0, 0, 1)
    assert (setattr.atime_us, setattr.mtime_us, setattr.ctime_us) == (0, 0, 0)
    assert isinstance(ns, NsEvent)
    assert (ns.kind, ns.flags, ns.namespaces) == ("unshare", 268435456, ["user"])
    assert (await store.verify(run_id)).ok
