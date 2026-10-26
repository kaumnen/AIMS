from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aims.events.schema import BlobRef, Content, Payload

Oversight = Literal["observe", "autonomous", "human"]
RunStatus = Literal["completed", "failed", "killed", "cancelled"]
AuditLevel = Literal["kernel", "snapshot"]
ExecEnd = Literal["exited", "timeout", "cancelled", "failed"]
FileOp = Literal["read", "write", "delete", "rename", "denied"]
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
    """How an exec ended, measured from the host side. `end` never trusts the exit code."""

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


class ProcExec(KernelPayload):
    """A program the kernel started in the sandbox. `root` marks one the runtime spawned."""

    type = "proc.exec"

    ppid: int
    ns_pid: int
    uid: int
    path: str
    argv: list[str]
    argv_bytes: int
    root: bool


class ProcExit(KernelPayload):
    type = "proc.exit"

    exit_code: int | None
    signal: int | None
    oom: bool = False


class FileAccess(KernelPayload):
    """A file opened, deleted, renamed, or refused. `partial` paths lack their leading part."""

    type = "file.access"

    op: FileOp
    path: str
    target: str | None = None
    errno: int | None = None
    partial: bool = False


class NetConnect(KernelPayload):
    """An attempt to reach an address. The sandbox has no network, so these fail."""

    type = "net.connect"

    kind: Literal["connect", "sendto"]
    family: Literal["inet", "inet6", "unix"]
    address: str
    port: int | None


class SysDenied(KernelPayload):
    """A privileged syscall the kernel refused."""

    type = "sys.denied"

    syscall: str
    errno: int


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


class MonitorFlag(Payload):
    type = "monitor.flag"

    layer: Literal["signal", "sweep", "judge"]
    source: str
    category: Category
    importance: int = Field(ge=1, le=5)
    evidence: list[int] = Field(min_length=1)
    justification: str
