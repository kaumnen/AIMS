import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from aims.events import (
    BlobRef,
    Event,
    LocalEventStore,
    Proc,
    RunStarted,
)
from aims.events.store import INLINE_LIMIT
from aims.sandbox import audit as audit_module
from aims.sandbox.audit import AuditedSandbox, SandboxStoppedError
from aims.sandbox.base import (
    AGENT_UID,
    RUNNER_UID,
    Capture,
    Denied,
    Exec,
    Exit,
    File,
    Fork,
    Lost,
    Net,
    Oom,
    Outcome,
    Record,
    SandboxInfo,
    SandboxSpec,
    Stamp,
)

pytestmark = pytest.mark.anyio

AT = Stamp(42, 1, 7)
STAMP = {"ts_kernel": 42, "cpu": 1, "cpu_seq": 7}

Step = Callable[["FakeSandbox", Capture, Callable[[int], None] | None], Awaitable[Outcome]]


class FakeSandbox:
    def __init__(self, *, kernel: bool = True) -> None:
        self.queue: asyncio.Queue[Record] = asyncio.Queue()
        self.kernel = kernel
        self.steps: list[Step] = []
        self.runs: list[list[str]] = []
        self.procs: list[Proc] = []
        self.after: list[Proc] | None = None
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


def child(pid: int, ppid: int, *argv: str) -> Exec:
    return Exec(pid, ppid, pid, AGENT_UID, argv[0], argv, 9, False, at=AT)


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


async def events(store: LocalEventStore, run_id: str) -> list[Event]:
    return await store.events(run_id, limit=1000)


def kinds(evs: list[Event]) -> list[str]:
    return [e.type for e in evs]


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
    assert [p.command for p in result.finished.leftover] == ["sleep 30"]
    assert not result.finished.leftover_killed


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


async def test_caps_count_what_they_drop(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(audit_module.CAPS, "procs", 3)
    monkeypatch.setitem(audit_module.CAPS, "files", 2)
    sb = FakeSandbox()
    kids = [child(101 + i, 100, "c") for i in range(4)]
    files = [File(101, "write", f"/work/{i}", at=AT) for i in range(4)]
    sb.steps.append(step(root(100), *kids, *files, Exit(100, 0, None, at=AT)))
    run_id, audited = await start(store, sb)
    result = await audited.exec("make", timeout_s=5)
    await audited.close()

    evs = await events(store, run_id)
    assert kinds(evs).count("proc.exec") == 3
    assert kinds(evs).count("file.access") == 2
    assert result.finished.procs == 3
    assert result.finished.dropped == 4


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
