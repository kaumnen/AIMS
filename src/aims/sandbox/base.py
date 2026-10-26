import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from aims.events import AuditLevel, ExecEnd, FileOp, Proc

AGENT_UID = 1000
RUNNER_UID = 1001
TOOLBOX = "/.aims"

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


@dataclass(slots=True)
class Capture:
    """Output of one exec: the first `limit` bytes of each stream and the total sizes."""

    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def add(self, stream: int, data: bytes) -> None:
        if stream == 2:
            self.stderr_bytes += len(data)
            self.stderr += data[: max(0, self.limit - len(self.stderr))]
        else:
            self.stdout_bytes += len(data)
            self.stdout += data[: max(0, self.limit - len(self.stdout))]

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
class Exec(Kernel):
    ppid: int
    ns_pid: int
    uid: int
    path: str
    argv: tuple[str, ...]
    argv_bytes: int
    root: bool


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
class Net(Kernel):
    kind: Literal["connect", "sendto"]
    family: Literal["inet", "inet6", "unix"]
    address: str
    port: int | None


@dataclass(frozen=True, slots=True)
class Denied(Kernel):
    syscall: str
    errno: int


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
    ) -> Outcome: ...

    async def processes(self) -> list[Proc]: ...

    def records(self) -> asyncio.Queue[Record] | None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def put_archive(self, path: str, data: bytes) -> None: ...

    async def get_archive(self, path: str) -> bytes: ...

    async def kill(self) -> None: ...

    async def close(self, *, keep_volumes: bool = False) -> None: ...


class SandboxBackend(Protocol):
    name: str

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Sandbox: ...

    async def close(self) -> None: ...
