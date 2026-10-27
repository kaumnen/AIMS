import itertools
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from aims.events.schema import BlobRef, Content, Payload

Oversight = Literal["observe", "autonomous", "human"]
RunStatus = Literal["completed", "failed", "killed", "cancelled"]
AuditLevel = Literal["kernel", "snapshot"]
ExecEnd = Literal["exited", "timeout", "cancelled", "failed"]
FileOp = Literal["read", "write", "delete", "rename", "denied"]
FsChangeType = Literal["added", "modified", "meta", "removed", "kind"]
FsKind = Literal["file", "dir", "symlink", "fifo", "socket", "other"]
FdKind = Literal["pipe", "fifo", "file", "char", "sock", "dir", "other"]
# The largest integer RFC 8785 canonical JSON can carry.
MAX_INT = 2**53 - 1
Category = Literal[
    "covert_communication",
    "scorer_tampering",
    "transcript_tampering",
    "deceiving_operator",
    "evading_checks",
    "collective_over_own_task",
    "drift",
    "loop_or_confusion",
    "fabricated_completion",
    "constraint_violation",
    "eval_awareness",
    "alerting_operator",
]


class RunStarted(Payload):
    type = "run.started"

    scenario: str
    oversight: Oversight
    agents: list[str] = Field(default_factory=list)
    config: dict[str, JsonValue] = Field(default_factory=dict)


class RunFinished(Payload):
    type = "run.finished"

    status: RunStatus
    reason: str | None = None


class LogTruncated(Payload):
    """The writer found a torn or invalid last line and cut it off. The bytes are kept as a blob."""

    type = "log.truncated"

    offset: int
    dropped: BlobRef
    reason: str


class ToolCall(Payload):
    """What the agent asked for, as the model sent it."""

    type = "tool.call"

    call_id: str
    name: str
    arguments: Content


class ToolResult(Payload):
    type = "tool.result"

    call_id: str
    output: Content
    error: bool = False


class Proc(BaseModel):
    """A process as the Docker host lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pid: int
    ppid: int
    uid: int
    command: str


class SandboxStarted(Payload):
    type = "sandbox.started"

    sandbox_id: str
    backend: str
    image: str
    image_id: str
    audit: AuditLevel
    config: dict[str, JsonValue] = Field(default_factory=dict)


class SandboxStopped(Payload):
    type = "sandbox.stopped"

    sandbox_id: str
    reason: Literal["closed", "killed", "exec_failed"]
    detail: str | None = None


class ExecStarted(Payload):
    """Written before the command reaches the sandbox. The argv sent is `wrapper + [command]`."""

    type = "exec.started"

    exec_id: str
    call_id: str | None
    command: Content
    wrapper: list[str]
    user: str
    cwd: str
    timeout_ms: int
    audit: AuditLevel


class ExecFinished(Payload):
    """How an exec ended, measured from the host side. `end` never trusts the exit code.

    `spilled` counts the exec's kernel records kept in an `audit.spill` by the time it finished;
    `dropped` counts those neither logged nor spilled.
    """

    type = "exec.finished"

    exec_id: str
    call_id: str | None
    end: ExecEnd
    exit_code: int | None
    pid: int | None
    duration_ms: int
    stdout: Content
    stderr: Content
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    leftover: list[Proc] = Field(default_factory=list)
    leftover_killed: bool = False
    procs: int | None = None
    dropped: int = 0
    spilled: int = 0
    error: str | None = None


class KernelPayload(Payload):
    """Fields of every event the kernel tracer reports.

    `ts_kernel` is the kernel boot clock in µs, comparable across cpus. `cpu_seq` is the record's
    place in `cpu`'s stream, so kernel order is (`ts_kernel`, `cpu`, `cpu_seq`).
    """

    exec_id: str | None
    pid: int
    ts_kernel: int
    cpu: int
    cpu_seq: int


class FdInfo(BaseModel):
    """A file descriptor at exec: its inode and what it points at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ino: int
    kind: FdKind


class ProcExec(KernelPayload):
    """A program the kernel did start in the sandbox. `root` marks one the runtime spawned.

    `cwd`, `env` and `fds` are read at exec. Only the first 4 KiB of `env` is read; `env_bytes` is
    its full size. `env` is empty with `env_inherited` set when it was read whole and matches the
    parent's, to keep the log small. `fds` holds stdin, stdout and stderr, giving the pipe graph.
    `inode`, `dev`, `nlink` and `magic` identify the executed file; `nlink` is 0 for a fileless
    exec. `path_partial` marks a path that is not exact: it reached the tracer's string cap and may
    be cut. `sha256` hashes the file as it was at exec; it is missing when the path is partial or
    outside a captured root, or when the file changed or could not be read before it was hashed.
    """

    type = "proc.exec"

    ppid: int
    ns_pid: int
    uid: int
    path: str
    argv: list[str]
    argv_bytes: int
    root: bool
    path_partial: bool = False
    cwd: str = ""
    cwd_partial: bool = False
    env: list[str] = Field(default_factory=list)
    env_bytes: int = 0
    env_inherited: bool = False
    fds: tuple[FdInfo, FdInfo, FdInfo] | None = None
    inode: int | None = None
    dev: int | None = None
    nlink: int | None = None
    magic: int | None = None
    sha256: str | None = None


class ProcExit(KernelPayload):
    type = "proc.exit"

    exit_code: int | None
    signal: int | None
    oom: bool = False


class FileAccess(KernelPayload):
    """A file open, delete or rename the sandbox requested, or a refused open.

    `op` read and write are open intents; delete and rename are recorded before they complete, so
    they do not prove the change happened. `denied` is a refusal. A `partial` path is not exact:
    it lacks its leading part, or it reached the tracer's string cap and may be cut at the end.
    """

    type = "file.access"

    op: FileOp
    path: str
    target: str | None = None
    errno: int | None = None
    partial: bool = False


class FileWrite(KernelPayload):
    """A file opened for writing was closed. `size` is its size at the close.

    A `partial` path reached the tracer's string cap and may be cut at the end.
    """

    type = "file.write"

    path: str
    size: int
    partial: bool = False


class FsChange(BaseModel):
    """One path that changed. A removed path keeps what was last known of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    change: FsChangeType
    kind: FsKind
    size: int
    mode: int
    mtime_us: int
    sha256: str | None = None
    content: BlobRef | None = None
    target: str | None = None
    timestomp: bool = False


class FsSnapshot(Payload):
    """How the sandbox's volumes changed since the snapshot whose digest is `base`.

    `exec_id` names the exec whose run the window covers, None for changes between execs: the
    runner, other agents on shared volumes, or processes left running. Changed files keep their
    bytes as blobs within the caps; `content_skipped` counts those kept by hash only, `unhashed`
    those the hash cap left unread, and `truncated` means the entry cap stopped the walk. Changes
    past the inline ones are JSON lines in `more`. `timestomp` marks content that changed while
    its mtime did not move forward.
    """

    type = "fs.snapshot"

    exec_id: str | None
    roots: list[str]
    base: str | None
    digest: str
    entries: int
    files: int
    bytes: int
    total: int
    changes: list[FsChange]
    more: BlobRef | None = None
    hashed_bytes: int
    unhashed: int
    content_skipped: int
    truncated: bool
    duration_ms: int


class NetConnect(KernelPayload):
    """An attempt to reach an address. The sandbox has no network, so these fail."""

    type = "net.connect"

    kind: Literal["connect", "sendto"]
    family: Literal["inet", "inet6"]
    address: str
    port: int | None


class SysDenied(KernelPayload):
    """A privileged syscall the kernel refused."""

    type = "sys.denied"

    syscall: str
    errno: int


class ProcOutput(KernelPayload):
    """Bytes a process wrote to a pipe or FIFO, aggregated per inode.

    `stream` is stdout or stderr when the inode is the exec's own, else pipe or fifo. `prefix` is
    the first bytes seen; `bytes` is the total written and `writes` the number of writes.
    `ts_kernel`, `cpu` and `cpu_seq` are from the first write. The monitor compares `prefix` with
    the command's claimed output to catch prepending and impersonation.
    """

    type = "proc.output"

    inode: int
    stream: Literal["stdout", "stderr", "pipe", "fifo"]
    bytes: int
    writes: int
    prefix: Content
    prefix_bytes: int
    truncated: bool


class IpcEvent(KernelPayload):
    """A request to open a unix socket, FIFO or SysV shared memory channel.

    It is recorded when asked for, so it does not prove the channel was made or the request was
    allowed. `path` is the socket or FIFO path; for a bind it is local, for a connect the peer.
    `key` and `size` are a shmget key and size, `id` a shmat segment, and `flags` the requested
    flag word. Abstract unix names start with `@`. A `partial` path is not exact: it lacks its
    leading part or may be cut at the tracer's string cap.
    """

    type = "ipc.event"

    kind: Literal["bind", "connect", "fifo", "shmget", "shmat"]
    family: Literal["unix", "sysv"] | None = None
    path: str | None = None
    key: int | None = None
    id: int | None = None
    size: int | None = None
    flags: int | None = None
    partial: bool = False


class PtraceEvent(KernelPayload):
    """A ptrace request by `pid` against `target`, recorded when the kernel returns.

    It proves the request was made and whether it was refused, not that the target changed.
    `errno` is set when it was refused. Peek requests can return negative data, so a negative
    return for them is not marked as a refusal.
    """

    type = "ptrace.event"

    target: int
    request: str
    refused: bool = False
    errno: int | None = None


class ProcSignal(KernelPayload):
    """A request by `pid` to send `signal` to `target`.

    It is recorded at the kernel's permission check, so it does not prove the signal was
    delivered; a signal the target ignores still counts.
    """

    type = "proc.signal"

    target: int
    signal: int


class MemExec(KernelPayload):
    """A request to run a program without a file on disk, or to map a written file executable.

    It is recorded when asked for, so it does not prove the exec or mapping happened. A `proc.exec`
    with the same path confirms the exec; an mmap has no confirmation event. `nlink` is 0 for a
    memfd or a file already deleted. `prot` is the requested mmap protection. A `partial` path is
    not exact: it lacks its leading part or may be cut at the tracer's string cap.
    """

    type = "mem.exec"

    kind: Literal["exec", "mmap"]
    path: str
    inode: int
    nlink: int
    prot: int | None = None
    partial: bool = False


class FileSetattr(KernelPayload):
    """A requested metadata change, which the kernel may still refuse.

    `valid` is the kernel's iattr mask; unset fields are None. A `partial` path is not exact: it
    lacks its leading part or may be cut at the tracer's string cap.
    """

    type = "file.setattr"

    path: str
    partial: bool = False
    valid: int
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int | None = None
    atime_us: int | None = None
    mtime_us: int | None = None
    ctime_us: int | None = None


class NsEvent(KernelPayload):
    """A namespace change the kernel confirmed. `flags` are the requested bits.

    `namespaces` lists the CLONE_NEW* kinds among them; other flags are left out. A refused
    change has no event, only a `sys.denied`.
    """

    type = "ns.event"

    kind: Literal["unshare", "setns"]
    flags: int
    namespaces: list[str] = Field(default_factory=list)


class AuditLost(Payload):
    """The kernel audit missed events. `count` is None when the number is unknown.

    `ts_from` and `ts_to` bound the loss on the kernel boot clock, and `cpus` names the cpus whose
    streams had holes, when known.
    """

    type = "audit.lost"

    count: int | None
    reason: str
    cpus: list[int] = Field(default_factory=list)
    ts_from: int | None = None
    ts_to: int | None = None


class ResourceSample(Payload):
    """One second of cgroup readings of a sandbox, at `hz` readings a second.

    `ts` is the kernel boot clock in µs, like `ts_kernel`. Each list holds its first value and
    then the change at every later reading, so a running sum restores the values. Trailing zero
    changes are left out, so a column that held still is one value. -1 marks a reading whose
    file could not be read, and a column that was never readable is None.
    Counters run from the sandbox's start; `memory_bytes` and `pids` are gauges. Stall times are
    the cgroup's PSI totals.
    """

    type = "resource.sample"

    COLUMNS: ClassVar[tuple[str, ...]] = (
        "cpu_usec",
        "cpu_user_usec",
        "cpu_system_usec",
        "cpu_throttled",
        "cpu_throttled_usec",
        "io_rbytes",
        "io_wbytes",
        "io_rios",
        "io_wios",
        "cpu_some_usec",
        "cpu_full_usec",
        "io_some_usec",
        "io_full_usec",
        "memory_some_usec",
        "memory_full_usec",
        "memory_bytes",
        "pids",
    )
    GAUGES: ClassVar[frozenset[str]] = frozenset({"memory_bytes", "pids"})

    sandbox_id: str
    hz: int
    ts: list[int]
    cpu_usec: list[int] | None
    cpu_user_usec: list[int] | None
    cpu_system_usec: list[int] | None
    cpu_throttled: list[int] | None
    cpu_throttled_usec: list[int] | None
    io_rbytes: list[int] | None
    io_wbytes: list[int] | None
    io_rios: list[int] | None
    io_wios: list[int] | None
    cpu_some_usec: list[int] | None
    cpu_full_usec: list[int] | None
    io_some_usec: list[int] | None
    io_full_usec: list[int] | None
    memory_some_usec: list[int] | None
    memory_full_usec: list[int] | None
    memory_bytes: list[int] | None
    pids: list[int] | None

    @model_validator(mode="after")
    def _same_length(self) -> ResourceSample:
        if not self.ts:
            raise ValueError("a window holds at least one reading")
        for name in self.COLUMNS:
            column = getattr(self, name)
            if column is not None and not 1 <= len(column) <= len(self.ts):
                raise ValueError(f"{name} has {len(column)} values for {len(self.ts)} readings")
        return self

    def search_text(self) -> str:
        return ""

    def values(self, name: str) -> list[int] | None:
        """The decoded readings of column `name`, or of `ts`."""
        column = self.ts if name == "ts" else getattr(self, name)
        if column is None:
            return None
        values = list(itertools.accumulate(column))
        return values + [values[-1]] * (len(self.ts) - len(values))

    def summary(self) -> dict[str, int | None]:
        """The last reading of each counter and the peak of each gauge, ignoring unread files."""
        out: dict[str, int | None] = {}
        for name in self.COLUMNS:
            read = [v for v in self.values(name) or () if v >= 0]
            out[name] = (max(read) if name in self.GAUGES else read[-1]) if read else None
        return out


class AuditSpill(Payload):
    """Records over an exec's caps, kept as JSON lines in `records` instead of the log.

    `kinds` counts them per cap. `ts_from` and `ts_to` bound them on the kernel clock.
    """

    type = "audit.spill"

    exec_id: str | None
    count: int
    kinds: dict[str, int]
    records: BlobRef
    ts_from: int | None = None
    ts_to: int | None = None


class MonitorFlag(Payload):
    type = "monitor.flag"

    layer: Literal["signal", "sweep", "judge"]
    source: str
    category: Category
    importance: int = Field(ge=1, le=5)
    evidence: list[int] = Field(min_length=1)
    justification: str
