import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from aims.events import AuditLevel, ExecEnd, FdKind, FileOp, FsChangeType, FsKind, Proc

AGENT_UID = 1000
RUNNER_UID = 1001
TOOLBOX = "/.aims"
RUNNER_DIR = "/.aims-runner"

AuditMode = Literal["auto", "kernel", "snapshot"]

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def check_name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"{value!r} is not a valid name: use a-z, 0-9, '_', '.', '-'")
    return value


def _abs(value: str) -> str:
    if not value.startswith("/") or "/../" in f"{value}/":
        raise ValueError(f"{value!r} is not an absolute path")
    return value


Name = Annotated[str, AfterValidator(check_name)]
AbsPath = Annotated[str, AfterValidator(_abs)]


class Mount(BaseModel):
    """A named volume shared by every sandbox of a run that mounts it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    volume: Name
    target: AbsPath
    read_only: bool = False


class SandboxSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str
    workdir: AbsPath = "/work"
    env: dict[str, str] = Field(default_factory=dict)
    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=1024, ge=64)
    pids: int = Field(default=256, ge=16)
    tmp_mb: int = Field(default=256, ge=1)
    writable_root: bool = False
    mounts: list[Mount] = Field(default_factory=list)
    kill_leftovers: bool = False
    output_limit: int = Field(default=4 * 1024 * 1024, ge=0)
    sample_hz: int = Field(default=50, ge=10, le=200)
    runner_mb: int | None = Field(default=None, ge=1)
    """Size of a tmpfs at `RUNNER_DIR` only the runner uid can enter, None for none."""
    keep: bool = False
    """Leave the container running and its volumes in place when the sandbox closes."""


@dataclass(slots=True)
class Capture:
    """Output of one exec: the first `limit` and last `tail` bytes of each stream, and the total
    sizes."""

    limit: int
    tail: int = 8192
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_tail: bytearray = field(default_factory=bytearray)
    stderr_tail: bytearray = field(default_factory=bytearray)
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def add(self, stream: int, data: bytes) -> None:
        if stream == 2:
            self.stderr_bytes += len(data)
            self.stderr += data[: max(0, self.limit - len(self.stderr))]
            self.stderr_tail = (self.stderr_tail + data)[-self.tail :] if self.tail else bytearray()
        else:
            self.stdout_bytes += len(data)
            self.stdout += data[: max(0, self.limit - len(self.stdout))]
            self.stdout_tail = (self.stdout_tail + data)[-self.tail :] if self.tail else bytearray()

    @property
    def truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout) or self.stderr_bytes > len(self.stderr)


@dataclass(frozen=True, slots=True)
class Outcome:
    end: ExecEnd
    exit_code: int | None
    pid: int | None
    duration_ms: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    sandbox_id: str
    backend: str
    image: str
    image_id: str
    config: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Stamp:
    """Where a record sits in the kernel stream: boot clock in µs, cpu, and that cpu's sequence."""

    ts: int
    cpu: int
    seq: int


@dataclass(frozen=True, slots=True)
class Kernel:
    """A kernel record about process `pid`, routed to the sandbox that owns it."""

    pid: int
    at: Stamp = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class Fork(Kernel):
    child: int


@dataclass(frozen=True, slots=True)
class Fd:
    """A file descriptor at exec: its inode and what kind of object it points at."""

    ino: int
    kind: FdKind


@dataclass(frozen=True, slots=True)
class Exec(Kernel):
    ppid: int
    ns_pid: int
    uid: int
    path: str
    argv: tuple[str, ...]
    argv_bytes: int
    root: bool
    path_partial: bool = False
    cwd: str = ""
    cwd_partial: bool = False
    env: tuple[str, ...] = ()
    env_bytes: int = 0
    fds: tuple[Fd, Fd, Fd] | None = None
    inode: int = 0
    dev: int = 0
    nlink: int = 0
    magic: int = 0
    size: int = 0
    mtime_us: int = 0
    ctime_us: int = 0
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PreExec(Kernel):
    """A file about to run, sent before its `Exec` so the hash can start before the program does."""

    ppid: int
    path: str
    inode: int
    dev: int = 0
    size: int = 0
    mtime_us: int = 0
    ctime_us: int = 0


@dataclass(frozen=True, slots=True)
class Exit(Kernel):
    exit_code: int | None
    signal: int | None


@dataclass(frozen=True, slots=True)
class Oom(Kernel):
    pass


@dataclass(frozen=True, slots=True)
class File(Kernel):
    op: FileOp
    path: str
    target: str | None = None
    errno: int | None = None
    partial: bool = False


@dataclass(frozen=True, slots=True)
class Close(Kernel):
    """A file opened for writing was closed, with its size then."""

    size: int
    path: str
    partial: bool = False


@dataclass(frozen=True, slots=True)
class Net(Kernel):
    kind: Literal["connect", "sendto"]
    family: Literal["inet", "inet6"]
    address: str
    port: int | None


@dataclass(frozen=True, slots=True)
class Denied(Kernel):
    syscall: str
    errno: int


@dataclass(frozen=True, slots=True)
class Output(Kernel):
    """A write to a pipe or FIFO by `pid`. `prefix` is set on the first write to an inode."""

    inode: int
    nbytes: int
    magic: int | None = None
    prefix: bytes | None = None


@dataclass(frozen=True, slots=True)
class Ipc(Kernel):
    """A unix socket or FIFO channel, or a SysV shared memory segment."""

    kind: Literal["bind", "connect", "fifo", "shmget", "shmat"]
    family: Literal["unix", "sysv"] | None = None
    path: str | None = None
    key: int | None = None
    id: int | None = None
    size: int | None = None
    flags: int | None = None
    partial: bool = False


@dataclass(frozen=True, slots=True)
class Ptrace(Kernel):
    target: int
    request: str
    refused: bool = False
    errno: int | None = None


@dataclass(frozen=True, slots=True)
class Signal(Kernel):
    target: int
    signal: int


@dataclass(frozen=True, slots=True)
class Mem(Kernel):
    kind: Literal["exec", "mmap"]
    path: str
    inode: int
    nlink: int
    prot: int | None = None
    partial: bool = False


@dataclass(frozen=True, slots=True)
class Setattr(Kernel):
    path: str
    partial: bool
    valid: int
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int | None = None
    atime_us: int | None = None
    mtime_us: int | None = None
    ctime_us: int | None = None


@dataclass(frozen=True, slots=True)
class Ns(Kernel):
    kind: Literal["unshare", "setns"]
    flags: int
    namespaces: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Lost:
    """Records that never reached the audit. `count` is None when the number is unknown.

    `ts_from` and `ts_to` bound the loss on the kernel boot clock when it is known.
    """

    count: int | None
    reason: str
    cpus: tuple[int, ...] = ()
    ts_from: int | None = None
    ts_to: int | None = None


Record = Kernel | Lost


@dataclass(frozen=True, slots=True)
class Sample:
    """One reading of a sandbox's cgroup files at `ts`, the kernel boot clock in µs.

    `values` follow `ResourceSample.COLUMNS`; -1 marks a file that could not be read.
    """

    ts: int
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    change: FsChangeType
    kind: FsKind
    size: int
    mode: int
    mtime_us: int
    sha256: str | None
    data: bytes | None = None
    target: str | None = None
    timestomp: bool = False


@dataclass(frozen=True, slots=True)
class Tree:
    """The sandbox's volumes now, with what changed since the previous `Tree`."""

    roots: tuple[str, ...]
    digest: str
    entries: int
    files: int
    bytes: int
    hashed_bytes: int
    unhashed: int
    content_skipped: int
    truncated: bool
    duration_ms: int
    changes: tuple[Change, ...]


class Sandbox(Protocol):
    """One isolated environment for one agent. Backends report facts, `AuditedSandbox` records."""

    @property
    def info(self) -> SandboxInfo: ...

    @property
    def audit(self) -> AuditLevel: ...

    def wrapper(self, timeout_ms: int) -> list[str]: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: str,
        timeout_ms: int,
        capture: Capture,
        on_pid: Callable[[int], None] | None = None,
        stdin: bytes | None = None,
        gid: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Outcome:
        """Run `argv`. `stdin` is written to the process and then closed; `gid` defaults to
        `uid`; `env` adds to the container's environment."""
        ...

    async def processes(self) -> list[Proc]: ...

    def records(self) -> asyncio.Queue[Record] | None: ...

    def on_pre_exec(self, callback: Callable[[PreExec], None]) -> None:
        """Tell the backend where to send a file about to run, before its `Exec`."""
        ...

    def samples(self) -> asyncio.Queue[Sample | Lost] | None: ...

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
        """The sha256 of a file under a captured root, if it is still that version."""
        ...

    async def tree(self) -> Tree | None:
        """Diff the sandbox's volumes against the previous call. None when it cannot read them."""
        ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def put_archive(self, path: str, data: bytes) -> None: ...

    async def get_archive(self, path: str) -> bytes: ...

    async def kill(self) -> None: ...

    async def quiesce(self) -> None:
        """After a kill, wait for the kernel records of the sandbox's last processes."""
        ...

    async def close(self, *, keep_volumes: bool = False) -> None: ...


class SandboxBackend(Protocol):
    name: str

    async def build(self, context: bytes, dockerfile: str = "Dockerfile") -> str:
        """Build an image from a tar build context, reusing one built from the same bytes."""
        ...

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Sandbox: ...

    async def close(self) -> None: ...
