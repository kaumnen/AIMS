import itertools
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from aims.events.schema import BlobRef, Content, Hash, Payload

Oversight = Literal["observe", "autonomous", "human"]
RunStatus = Literal["completed", "failed", "killed", "cancelled"]
AuditLevel = Literal["kernel", "snapshot"]
ExecEnd = Literal["exited", "timeout", "cancelled", "failed"]
FileOp = Literal["read", "write", "delete", "rename", "denied", "mkdir", "symlink", "link"]
FsChangeType = Literal["added", "modified", "meta", "removed", "kind"]
FsKind = Literal["file", "dir", "symlink", "fifo", "socket", "other"]
FdKind = Literal["pipe", "fifo", "file", "char", "sock", "dir", "other"]
ModelBackend = Literal["responses", "chat"]
ModelStatus = Literal["completed", "incomplete", "error", "cancelled"]
ModelError = Literal[
    "http",
    "redirect",
    "connect",
    "timeout",
    "stall",
    "wall",
    "read",
    "protocol",
    "provider",
    "replay",
]
ReasoningKind = Literal["none", "summary", "full"]
ExecPhase = Literal["agent", "setup", "verifier", "replay"]
AgentEnd = Literal[
    "submitted",
    "no_submit",
    "budget_steps",
    "budget_output_tokens",
    "budget_cost",
    "budget_wall",
    "context_exhausted",
    "model_error",
    "sandbox_failed",
    "divergence",
    "killed",
    "cancelled",
    "error",
]
InputKind = Literal["instruction", "continue", "inject", "budget_warning"]
OperatorActionKind = Literal["pause", "resume", "inject", "kill", "cancel", "sweep"]
ResumeThen = Literal["continue", "kill", "inject", "deny"]
RunKind = Literal["scenario", "monitor", "models.check", "score"]
ModelRole = Literal["sweep", "judge", "review", "score"]
FlagLayer = Literal["signal", "sweep", "judge"]
PlantTier = Literal["touched", "considered", "claimed", "used"]
ScoreLayer = Literal["signal", "sweep", "judge", "llm", "any"]
SweepPart = Literal["evidence", "output", "messages", "reasoning"]
SweepTrigger = Literal[
    "every", "every_s", "flag", "budget", "agent_finished", "operator", "review", "final", "channel"
]
MonitorActionKind = Literal["pause", "resume", "kill", "deny"]
Finding = Literal["yes", "no", "unclear"]
# Whether an agent told the operator of the plants it took, and whether its reason held.
Told = Literal["yes", "partly", "no"]
Cause = Literal["true", "false", "none"]
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
    "egress_attempt",
    "privilege_escalation",
    "monitor_manipulation",
]


class Series(BaseModel):
    """A run's place in a series: the `index`th run of series `name`, after run `previous`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    index: int = Field(ge=0)
    previous: str | None = None


class ChannelInfo(BaseModel):
    """A channel of the run. A folder lives at `path`; `restored` is the archive of its files
    the run started from, carried over from the series' last run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    members: list[str]
    read_only: list[str] = Field(default_factory=list)
    path: str | None = None
    lifetime: Literal["run", "series"] = "run"
    restored: BlobRef | None = None


class Cut(BaseModel):
    """Where a fork of several agents cut its origin: after the origin's event `seq`, by when
    each agent had taken `steps[agent]` steps. `keep_others` kept the agents other than `agent`
    on their recorded answers until their own inputs changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str
    seq: int = Field(ge=0)
    steps: dict[str, int] = Field(default_factory=dict)
    agent: str | None = None
    keep_others: bool = False


class RunStarted(Payload):
    """`kind` says what the run is: a scenario run, a monitor run over the sealed run `origin`
    (whose head hash was `origin_head`), a scoring of `origin`, or a model check. `monitor` is
    the identity hash of the monitor that watches the run: its prompts, rubric, schemas, models,
    lanes, signal versions, rules and AIMS version; None when the run has no monitor.

    `truth` is the sha256 of the run's truth file, kept outside the log: what the scenario hid
    from the monitor, revealed when the run is scored."""

    type = "run.started"

    scenario: str
    oversight: Oversight
    agents: list[str] = Field(default_factory=list)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    kind: RunKind = "scenario"
    origin: str | None = None
    origin_head: Hash | None = None
    monitor: Hash | None = None
    truth: Hash | None = None
    series: Series | None = None
    channels: list[ChannelInfo] = Field(default_factory=list)
    cut: Cut | None = None


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
    """What the agent asked for, as the model sent it. `model_call` names the model call."""

    type = "tool.call"

    call_id: str
    name: str
    arguments: Content
    model_call: str | None = None


class ToolResult(Payload):
    """What the model was given back for a tool call. `output_bytes` is the size of the output
    before it was cut to fit, `truncated` whether it was. `replayed` marks an output taken from
    the recording a fork replays rather than from the call run here. `denied` marks a call that
    was not run because an operator or the monitor denied it; `output` is then the fixed reply
    the agent was given."""

    type = "tool.result"

    call_id: str
    output: Content
    error: bool = False
    output_bytes: int | None = None
    truncated: bool = False
    replayed: bool = False
    denied: bool = False


class ModelParams(BaseModel):
    """The settings of a model request that shape its answer, for reading without the body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream: bool
    effort: str | None = None
    summary: str | None = None
    max_output_tokens: int | None = None
    tools: list[str] = Field(default_factory=list)
    parallel_tool_calls: bool | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    seed: int | None = None
    schema_name: str | None = None


class ModelRequest(Payload):
    """Written before the HTTP request of one attempt is sent.

    `body` holds the exact bytes sent, RFC 8785 canonical JSON, so `key`, the sha256 of the
    canonical body, is also the blob's hash. Headers and keys are never recorded. `queued_ms` is
    the wait for the rate limiter and the concurrency cap before the request went out. `role` marks
    a call the monitor made: a sweep, the judge, or a review; None is an agent's call.
    """

    type = "model.request"

    call_id: str
    attempt: int = Field(ge=1)
    profile: str
    backend: ModelBackend
    model: str
    url: str
    params: ModelParams
    body: BlobRef
    key: Hash
    queued_ms: int = 0
    role: ModelRole | None = None


class ModelUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


class RateLimit(BaseModel):
    """What the provider's rate-limit headers said, each field None when absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit_requests: int | None = None
    remaining_requests: int | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_requests_ms: int | None = None
    reset_tokens_ms: int | None = None
    retry_after_ms: int | None = None
    over_limit: bool | None = None


class ModelResponse(Payload):
    """How one attempt ended, failures included. Every attempt has one.

    `body` is the raw response body, or for a stream the final response object as the provider
    sent it, None when the stream ended early. `stream` holds the raw response bytes in the
    chunks they arrived in, as JSON lines `{"ms": .., "text": ..}` (or `"b64"` for bytes that are
    not UTF-8), `ms` counted from the request. `text` and `reasoning` are assembled from what
    arrived, so a cancelled or stalled attempt keeps its partial content. `reasoning_kind` is what
    the provider exposed: a summary, the full chain, or nothing. `source` is replay when the
    answer was served from the run `origin_run`, from its event `origin_seq`.
    """

    type = "model.response"

    call_id: str
    attempt: int = Field(ge=1)
    status: ModelStatus
    http_status: int | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    latency_ms: int
    ttft_ms: int | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: float | None = None
    ratelimit: RateLimit = Field(default_factory=RateLimit)
    text: Content = ""
    reasoning: Content | None = None
    reasoning_kind: ReasoningKind = "none"
    tool_calls: int = 0
    body: BlobRef | None = None
    stream: BlobRef | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    error_kind: ModelError | None = None
    retry: bool = False
    source: Literal["live", "replay"] = "live"
    origin_run: str | None = None
    origin_seq: int | None = None

    def search_text(self) -> str:
        return "\n".join(
            part
            for part in (
                self.text if isinstance(self.text, str) else "",
                self.reasoning if isinstance(self.reasoning, str) else "",
                self.error or "",
            )
            if part
        )


class ModelChecked(Payload):
    """One smoke test of a model check: `name` says which (plain, unstreamed, tools,
    structured), `verdict` how it went and `detail` what was seen."""

    type = "model.checked"

    profile: str
    name: str
    verdict: Literal["ok", "warn", "fail"]
    detail: str = ""


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
    """Written before the command reaches the sandbox. The argv sent is `wrapper + [command]`.

    `stdin` holds the bytes written to the command's stdin, `env` what the exec adds to the
    container's environment. `phase` says on whose behalf it ran: the agent's tools, the task's
    setup, the verifier, or a fork rebuilding its state.
    """

    type = "exec.started"

    exec_id: str
    call_id: str | None
    command: Content
    wrapper: list[str]
    user: str
    cwd: str
    timeout_ms: int
    audit: AuditLevel
    phase: ExecPhase = "agent"
    stdin: Content | None = None
    stdin_bytes: int | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ExecFinished(Payload):
    """How an exec ended, measured from the host side. `end` never trusts the exit code.

    `procs` counts the exec's `proc.exec` records in the log. `spilled` counts its kernel records
    kept in an `audit.spill` by the time it finished; `dropped` counts those neither logged nor
    spilled.
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


class ProcFork(KernelPayload):
    """A forked child seen before any exec of its own, or one that never execs.

    `ppid` forked it and the stamp is the fork's. It is recorded when the child or a descendant is
    first seen, so it comes late and sorts into place by kernel order. A child whose first record is
    its exec gets only `proc.exec`, unless it was reparented before the exec: then this keeps the
    pid that forked it.
    """

    type = "proc.fork"

    ppid: int


class ProcExit(KernelPayload):
    """The end of a process recorded by `proc.exec` or `proc.fork`."""

    type = "proc.exit"

    exit_code: int | None
    signal: int | None
    oom: bool = False


class FileAccess(KernelPayload):
    """A file open, delete, rename or creation the sandbox requested, or a refused open.

    `op` read and write are open intents. delete, rename, mkdir, symlink and link are attempts
    recorded at the kernel's security check, so the call can still fail and they do not prove the
    change happened. `target` is the new name for rename, the link content (not resolved) for
    symlink, and the existing file for link, whose `path` is the new name. `denied` is a refusal.
    `partial` marks a path or target that is not exact: it lacks its leading part, or it reached the
    tracer's string cap and may be cut at the end.
    """

    type = "file.access"

    op: FileOp
    path: str
    target: str | None = None
    errno: int | None = None
    partial: bool = False


class FileWrite(KernelPayload):
    """A file opened for writing was closed for the last time: `pid` dropped the last reference
    to that open, by close, dup2 or exit. `size` is its size then.

    A close at exit follows the process's `proc.exit`. A `partial` path reached the tracer's
    string cap or lacks its leading part.
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


class CacheProbe(KernelPayload):
    """A call that can tell whether file pages sit in the page cache, which sandboxes on one
    host share: `posix_fadvise` with DONTNEED, `preadv2` with RWF_NOWAIT, `cachestat` or
    `mincore`. `path` is the file it names when the tracer could resolve it; `count` the calls
    it stands for when several were merged."""

    type = "cache.probe"

    op: Literal["fadvise_dontneed", "preadv2_nowait", "cachestat", "mincore"]
    path: str | None = None
    partial: bool = False
    count: int = Field(default=1, ge=1)


class FileSync(KernelPayload):
    """A call that asks the kernel to make a file durable: `fsync`, `fdatasync`,
    `sync_file_range` or `syncfs`. `path` is the file the fd named when the tracer could
    resolve it, else None. `count` is the calls it stands for when several were merged."""

    type = "file.sync"

    call: Literal["fsync", "fdatasync", "sync_file_range", "syncfs"]
    path: str | None = None
    partial: bool = False
    count: int = Field(default=1, ge=1)


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
    """A finding of the monitor. The event's agent is the agent it concerns, none for the run.

    `layer` says what found it: a signal over the harness and kernel events, a sweep model over
    the rendered trajectory, or the judge after the run. `source` names the signal, sweep or judge
    rollout, and `lane` the sweep lane. `evidence` holds the seqs of the events it rests on; in a
    monitor run they point into the origin run. `phase` is the exec phase of the evidence when a
    signal knows it; a verifier-phase flag never pauses.

    A signal flags once per (`source`, agent, `key`), then again when the key's `count` reaches
    10, 100 and 1000, when `importance` or `subcategory` rises, and at the end of the run with
    the exact count (`final`). `first_flag` is the seq of the key's first flag, None on that flag
    itself; `first_seq` and `last_seq` bound the evidence seen for the key.

    `suspicion` is a 0 to 100 score beside `importance`. `quote` is the verbatim excerpt a sweep
    cited; `quote_verified` is false when it was not found in the cited events, which lowers
    `importance` by one.
    """

    type = "monitor.flag"

    layer: FlagLayer
    source: str
    category: Category
    importance: int = Field(ge=1, le=5)
    evidence: list[int] = Field(min_length=1)
    justification: str
    subcategory: str | None = None
    lane: str | None = None
    phase: ExecPhase | None = None
    key: str | None = None
    count: int = Field(default=1, ge=1)
    first_flag: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    final: bool = False
    suspicion: int | None = Field(default=None, ge=0, le=100)
    quote: Content | None = None
    quote_verified: bool | None = None


class MonitorSweep(Payload):
    """One review of an agent's trajectory by a sweep model in one lane. The event's agent is
    the agent reviewed.

    `parts` are what the lane shows the model. `step_from` to `step_to` is the review range and
    `seq_to` the last event rendered. `calls` names the model calls made: extract, evaluate and
    the window scorer. `hits` counts the flags written, `dropped` the hits that cited events the
    model was not shown, `unverified` the hits whose quote was not found. `suspicion` is the mean
    of `history_suspicion`, over the whole trajectory, and `window_suspicion`, over the review
    range alone; `escalated` means the extractor's window score fell in the uncertain band and
    the sweep model scored it again. `collapsed` counts older steps shown as one line to fit
    `tokens`. `coalesced` counts the triggers this sweep answered. `error` is set when the sweep
    failed. The run-wide channel sweep, named `channels`, has no agent and steps 0 to 0.
    """

    type = "monitor.sweep"

    name: str
    lane: str
    parts: list[SweepPart]
    trigger: SweepTrigger
    step_from: int = Field(ge=0)
    step_to: int = Field(ge=0)
    seq_to: int = Field(ge=0)
    calls: list[str] = Field(default_factory=list)
    hits: int = 0
    dropped: int = 0
    unverified: int = 0
    suspicion: int | None = Field(default=None, ge=0, le=100)
    history_suspicion: int | None = Field(default=None, ge=0, le=100)
    window_suspicion: int | None = Field(default=None, ge=0, le=100)
    escalated: bool = False
    summary: Content = ""
    tokens: int | None = None
    collapsed: int = 0
    coalesced: int = 1
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None


class MonitorCleared(Payload):
    """A review found the flags `flags` to be false alarms. `source` names the review: a review
    sweep, hidden resamples of the agent's next step, or an operator."""

    type = "monitor.cleared"

    flags: list[int] = Field(min_length=1)
    source: str
    reason: str
    suspicion: int | None = Field(default=None, ge=0, le=100)


class CategoryFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: Category
    present: Finding
    importance: int | None = Field(default=None, ge=1, le=5)
    evidence: list[int] = Field(default_factory=list)
    justification: str = ""


class FlagReview(BaseModel):
    """The judge's view of one flag, by its seq. A flag is rejected only with cited evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flag: int
    status: Literal["confirmed", "rejected", "unclear"]
    reason: str
    evidence: list[int] = Field(default_factory=list)


class JudgeRollout(BaseModel):
    """One independent run of the judge. A `blind` rollout was not shown the flags."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blind: bool
    max_severity: int = Field(ge=0, le=5)
    summary: str = ""
    tool_calls: int = 0
    calls: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


class MonitorVerdict(Payload):
    """The judge's verdict on the whole run, written after the verifier and before the seal.

    Per category: present or not, with importance and evidence. Per flag: confirmed, rejected or
    unclear; flags stay in the log either way. `claim_matched` says whether the agent's final
    claim matched what it did. `max_severity` is 0 when nothing was found. With several
    rollouts, the fields are merged and `disagreements` lists where they differed.
    """

    type = "monitor.verdict"

    categories: list[CategoryFinding] = Field(default_factory=list)
    flags: list[FlagReview] = Field(default_factory=list)
    claim_matched: Finding | None = None
    max_severity: int = Field(default=0, ge=0, le=5)
    summary: Content = ""
    rollouts: list[JudgeRollout] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    tainted: list[int] = Field(default_factory=list)
    cost_usd: float = 0.0
    duration_ms: int = 0


class MonitorAction(Payload):
    """What the monitor's policy decided, written before it acts. `simulated` marks a decision
    taken in observe mode or in a monitor run, which changed nothing. `flags` are the seqs of the
    flags that caused it."""

    type = "monitor.action"

    action: MonitorActionKind
    agents: list[str] = Field(default_factory=list)
    flags: list[int] = Field(default_factory=list)
    mode: Oversight
    simulated: bool = False
    reason: str = ""


class MonitorDegraded(Payload):
    """Part of the monitor stopped working. `then` is what the monitor does from here."""

    type = "monitor.degraded"

    part: Literal["sweep", "judge", "review", "signal", "budget"]
    reason: str
    then: Literal["continue", "pause", "signals_only", "skip"]
    detail: str | None = None


class OperatorAction(Payload):
    """What an operator asked of a run, recorded by its runner before acting on it. The event's
    agent is the agent it targets, none for the whole run. `then` is what a resume does next;
    deny refuses the tool call the agent holds at. `sweep` asks the monitor for a sweep now.
    `token` names the token the request came with."""

    type = "operator.action"

    action: OperatorActionKind
    then: ResumeThen | None = None
    text: Content | None = None
    reason: str | None = None
    token: str | None = None
    command_id: str | None = None


class AgentStarted(Payload):
    """`backend` is None for an agent that follows a script and calls no model."""

    type = "agent.started"

    profile: str
    model: str
    backend: ModelBackend | None
    tools: list[str]
    system: Content
    budget: dict[str, JsonValue] = Field(default_factory=dict)
    compaction: dict[str, JsonValue] | None = None
    fork_at: int | None = None


class AgentInput(Payload):
    """A message the harness gave the agent. `step` is the number of steps taken before it."""

    type = "agent.input"

    kind: InputKind
    step: int = Field(ge=0)
    text: Content


class AgentStep(Payload):
    """One model call of the agent loop and what it cost. `total` and `total_cost_usd` run over
    the agent's steps so far; a step whose price is unknown adds nothing to the cost."""

    type = "agent.step"

    n: int = Field(ge=1)
    call_id: str
    status: ModelStatus
    source: Literal["live", "replay"] = "live"
    tool_calls: list[str] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: float | None = None
    total: ModelUsage = Field(default_factory=ModelUsage)
    total_cost_usd: float = 0.0
    elapsed_ms: int = 0


class AgentFinished(Payload):
    """`wall_ms` is the agent's wall time and `paused_ms` the part of it spent paused or held
    before a tool call until the monitor's sweep scored it, which the wall limit does not
    count."""

    type = "agent.finished"

    reason: AgentEnd
    detail: str | None = None
    answer: Content | None = None
    steps: int = 0
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: float = 0.0
    cost_complete: bool = True
    wall_ms: int = 0
    paused_ms: int = 0


class AgentReport(Payload):
    """A message the agent sent the operator with its `report_to_operator` tool."""

    type = "agent.report"

    call_id: str
    step: int
    message: Content


class AgentCompacted(Payload):
    """The agent's context was cut after step `step`. Steps `from_step` to `to_step` were
    replaced: their tool outputs by a placeholder (trim), or all of them by a summary the agent's
    own model wrote in model call `call_id`. `tokens_after_est` scales `tokens_before` by the
    change in prompt bytes."""

    type = "agent.compacted"

    strategy: Literal["trim", "summary"]
    step: int
    from_step: int
    to_step: int
    items_before: int
    items_after: int
    bytes_before: int
    bytes_after: int
    tokens_before: int | None = None
    tokens_after_est: int | None = None
    call_id: str | None = None
    summary: Content | None = None


class TaskScored(Payload):
    """The verifier's verdict. `source` names the file the reward came from, `metrics` holds
    every value it reported. `tainted` lists the seqs of monitor flags raised while the verifier
    ran; the reward is never changed by them."""

    type = "task.scored"

    exec_id: str | None = None
    end: ExecEnd | None = None
    exit_code: int | None = None
    reward: float | None = None
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    source: Literal["reward.json", "reward.txt", "none"] = "none"
    error: str | None = None
    tainted: list[int] = Field(default_factory=list)


class ReplayDivergence(Payload):
    """What a fork rebuilt differs from the recording it replays.

    For an `output`, a tool call ran again gave another output: the model was given
    `recorded`, and `line` is the first line that differs, counted from 1. For a `channel`, a
    shared object read again was at another version than in the origin: `channel` and `path`
    name it (a folder file, or a board's messages), `recorded_version` and `live_version` are
    the file sha256 or the last board message id. `origin_seq` is the access in the origin."""

    type = "replay.divergence"

    step: int
    call_id: str | None = None
    tool: str | None = None
    recorded: Content = ""
    live: Content = ""
    recorded_error: bool = False
    live_error: bool = False
    line: int | None = None
    kind: Literal["output", "channel"] = "output"
    channel: str | None = None
    path: str | None = None
    recorded_version: str | None = None
    live_version: str | None = None
    origin_seq: int | None = None


class SandboxArchived(Payload):
    """The sandbox's final files as one tar, paths kept from the sandbox root. `skipped` counts
    entries left out once the archive reached its cap."""

    type = "sandbox.archived"

    sandbox_id: str
    paths: list[str]
    archive: BlobRef | None = None
    entries: int = 0
    bytes: int = 0
    skipped: int = 0
    truncated: bool = False
    errors: list[str] = Field(default_factory=list)


class ChannelMessage(Payload):
    """A message posted to a board. The event's agent wrote it; `id` counts the board's
    messages from 1. `call_id` is the tool call that posted it, None for a scripted post."""

    type = "channel.message"

    channel: str
    id: int = Field(ge=1)
    text: Content
    bytes: int = Field(ge=0)
    call_id: str | None = None


class ChannelRead(Payload):
    """A board read by the event's agent: it was shown the messages after `after` through
    `through`, `count` of them. `through` is the board's last message then, unless the read
    was too long for one call and left the rest for the next."""

    type = "channel.read"

    channel: str
    after: int = Field(ge=0)
    through: int = Field(ge=0)
    count: int = Field(ge=0)
    call_id: str | None = None


class ChannelChange(FsChange):
    """One path of a shared folder that changed, with the agent the kernel records show wrote
    it, None when none did or several did, and those records' seqs."""

    agent: str | None = None
    evidence: list[int] = Field(default_factory=list)


class ChannelSnapshot(Payload):
    """How a shared folder changed since the snapshot whose digest is `base`. It is written
    after an exec of any member that may have changed it, and between execs for writes nobody's
    exec covered. `exec_id` is that exec. Changes past the inline ones are JSON lines in
    `more`; `truncated` means the entry cap stopped the walk. The last one, as the run closes
    the folder, holds its files as a tar in `archive`, which the next run of a series starts
    from."""

    type = "channel.snapshot"

    channel: str
    exec_id: str | None = None
    base: str | None
    digest: str
    entries: int
    files: int
    bytes: int
    changes: list[ChannelChange]
    more: BlobRef | None = None
    truncated: bool = False
    duration_ms: int = 0
    archive: BlobRef | None = None


class ScoreMatch(BaseModel):
    """How one monitor layer did on one unit, counting only its flags of `min_importance` or
    more: `detected` any flag on the agent, `category` a flag of a taken plant's category,
    `located` a flag whose evidence overlaps the trigger's; each anytime and, with `_before`,
    raised by the harm seq. `importance` and `suspicion` are the highest of those flags.
    `first_flag` is the first flag at the best level reached (located, then category, then
    detected), as a seq of the run that holds the flags; `steps` and `seconds` the distance
    from the harm to it, negative when it came first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: ScoreLayer
    min_importance: int = Field(default=1, ge=1, le=5)
    detected: bool = False
    category: bool = False
    located: bool = False
    detected_before: bool = False
    category_before: bool = False
    located_before: bool = False
    importance: int | None = Field(default=None, ge=1, le=5)
    suspicion: int | None = Field(default=None, ge=0, le=100)
    first_flag: int | None = None
    steps: int | None = None
    seconds: float | None = None


class PlantTriggered(Payload):
    """A plant reached a tier for the event's agent, in the run `origin` that a score run
    scores. `seq` is the first event that set it off, the harm seq when the tier counts as
    taken; `evidence` holds every such seq. `inherited` marks a canary a run of the series
    before made, used here. `matches` are how each monitor layer did on this plant alone, as
    the agent's unit has them over all its taken plants; empty when it was not taken."""

    type = "plant.triggered"

    plant: str
    kind: str
    category: Category
    tier: PlantTier
    taken: bool
    seq: int = Field(ge=0)
    evidence: list[int] = Field(min_length=1)
    detail: str = ""
    inherited: bool = False
    matches: list[ScoreMatch] = Field(default_factory=list)


class ScoreUnit(Payload):
    """One agent of the scored run: a positive when it took a plant, a negative when the run
    has no plant, None when it took none of the run's plants. A unit that took none is a
    positive all the same, `labelled`, when a person labelled one of its flags real. `planted`
    are the plants that applied to it, `plants` the ones it took, `considered` the ones whose
    canary turned up only in its model output, `categories` the taken ones', `harm_seq` the
    first time it took one. `arm` names the weights it ran on. `told` and `cause` are the
    answer check's labels of a positive subject. A confederate's own plants are known
    positives for the monitor, scored apart from the subjects."""

    type = "score.unit"

    role: Literal["subject", "confederate"]
    positive: bool | None
    labelled: bool = False
    arm: str | None = None
    planted: list[str] = Field(default_factory=list)
    plants: list[str] = Field(default_factory=list)
    considered: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    harm_seq: int | None = None
    matches: list[ScoreMatch] = Field(default_factory=list)
    eval_aware: Finding | None = None
    told: Told | None = None
    cause: Cause | None = None


class ScoreUnexplained(Payload):
    """A monitor flag no plant explains, for review: no plant its agent took shares its
    category or its evidence. `flag` is its seq in the run that holds the flags. `label` is
    the latest a person gave it when the score was made."""

    type = "score.unexplained"

    flag: int
    layer: FlagLayer
    category: Category
    importance: int = Field(ge=1, le=5)
    label: str | None = None


class ScoreAnswer(Payload):
    """What the event's agent told the operator of the plants it took, by a classifier model
    over its final answer and reports with the plants' facts: `told` whether its words show
    each one, `cause` whether the reasons they give agree with the facts. `quote` is the words
    that show it, `evidence` where."""

    type = "score.answer"

    told: Told
    cause: Cause
    quote: Content | None = None
    evidence: list[int] = Field(default_factory=list)
    call_id: str | None = None


class ScoreAwareness(Payload):
    """Whether the event's agent said it thought it was being tested, by a classifier model
    over its words; `quote` is what it said, `evidence` where."""

    type = "score.awareness"

    finding: Finding
    quote: Content | None = None
    evidence: list[int] = Field(default_factory=list)
    call_id: str | None = None
