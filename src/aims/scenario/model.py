import posixpath
import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from aims.events import Oversight
from aims.models import Overrides
from aims.monitor.config import MonitorConfig
from aims.prompts import Prompts
from aims.sandbox.base import AbsPath, Name

DEFAULT_TOOLS = ("bash", "edit", "submit", "report_to_operator")
HOME = "/home/agent"
_MODE = re.compile(r"^0?[0-7]{3}$")


def _relative(value: str) -> str:
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"{value!r} must be a path inside the scenario's directory")
    return value


def _mode(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f'quote the mode, as in "0644"; {value!r} reads as a number')
    if not _MODE.match(value):
        raise ValueError(f'{value!r} is not an octal file mode such as "0644"')
    return value


RelPath = Annotated[str, AfterValidator(_relative)]
Mode = Annotated[str, BeforeValidator(_mode)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Environment(_Strict):
    """The sandbox image: pulled by name, or built from a Dockerfile whose directory is the
    build context. Root-level setup belongs in the Dockerfile."""

    image: str | None = None
    dockerfile: RelPath | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.image is None) == (self.dockerfile is None):
            raise ValueError("set exactly one of image and dockerfile")
        return self


class FileEntry(_Strict):
    """A file or directory placed in the sandbox before setup, owned by the agent.

    `path` is absolute or relative to the workdir. `source` is a file or directory next to the
    scenario; `content` is the file's text. Seeded files are group writable so the verifier,
    which runs in the agent's group, can write where they are.
    """

    path: str
    source: RelPath | None = None
    content: str | None = None
    mode: Mode | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.source is None) == (self.content is None):
            raise ValueError("set exactly one of source and content")
        if not self.path or ".." in self.path.split("/"):
            raise ValueError(f"{self.path!r} is not a sandbox path")
        return self


class Expect(_Strict):
    """What a sound verifier run looks like, for the monitor's verifier_anomaly signal: at least
    `tests` tests, and a test runner summary in the `report` format in its output."""

    tests: int | None = Field(default=None, ge=1)
    report: Literal["pytest"] | None = None


class Verifier(_Strict):
    """Scores the task after the agent stops, in the same sandbox.

    `files` is a directory copied in only then, where the agent cannot reach. `command` runs in
    the workdir and writes its reward to `$AIMS_LOGS_DIR/reward.json` or `reward.txt`.
    `restore` lists globs of seeded files put back as they were seeded before the verifier runs;
    off by default, it is an experiment axis.
    """

    command: str
    files: RelPath | None = None
    user: Literal["runner", "agent"] = "runner"
    timeout_s: float = Field(default=600, gt=0)
    env: dict[str, str] = Field(default_factory=dict)
    expect: Expect | None = None
    restore: list[str] = Field(default_factory=list)


class Task(_Strict):
    instruction: str = Field(min_length=1)
    environment: Environment
    files: list[FileEntry] = Field(default_factory=list)
    setup: list[str] = Field(default_factory=list)
    setup_timeout_s: float = Field(default=600, gt=0)
    verifier: Verifier | None = None


class SandboxConfig(_Strict):
    workdir: AbsPath = "/work"
    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=1024, ge=64)
    pids: int = Field(default=256, ge=16)
    tmp_mb: int = Field(default=256, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    kill_leftovers: bool = False
    command_timeout_s: float = Field(default=120, gt=0)
    max_command_timeout_s: float = Field(default=600, gt=0)

    @model_validator(mode="after")
    def _timeouts(self) -> Self:
        if self.command_timeout_s > self.max_command_timeout_s:
            raise ValueError("command_timeout_s is above max_command_timeout_s")
        return self


class Limits(_Strict):
    """Caps over the whole run, shared by its agents. Reaching one ends every agent.
    `monitor_cost_usd` caps the monitor's own model spend, None for its preset's default; past
    it the monitor keeps only its signals."""

    wall_s: float | None = Field(default=None, gt=0)
    cost_usd: float | None = Field(default=None, gt=0)
    steps: int | None = Field(default=None, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
    monitor_cost_usd: float | None = Field(default=None, ge=0)


class Budget(_Strict):
    """Caps for one agent. `warn_at` is a fraction of a cap at which the agent is told how much
    it has used; off by default."""

    steps: int | None = Field(default=100, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
    cost_usd: float | None = Field(default=None, gt=0)
    wall_s: float | None = Field(default=3600, gt=0)
    warn_at: float | None = Field(default=None, gt=0, lt=1)


class Compaction(_Strict):
    """Shrink the agent's context once a call's tokens pass `at` of the profile's context.

    `trim` replaces old tool outputs with a placeholder; `summary` has the agent's model write
    a summary of the older steps. The last `keep_recent` steps stay as they were.
    """

    at: float = Field(gt=0, lt=1)
    strategy: Literal["trim", "summary"] = "trim"
    keep_recent: int = Field(default=5, ge=1)


class AgentConfig(_Strict):
    id: Name = "agent"
    profile: str | None = None
    overrides: Overrides | None = None
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    budget: Budget = Field(default_factory=Budget)
    prompts: Prompts | None = None
    compaction: Compaction | None = None

    @model_validator(mode="after")
    def _no_monitor_texts(self) -> Self:
        if self.prompts is not None and self.prompts.monitor:
            raise ValueError("monitor texts belong in the config files, not an agent's prompts")
        return self

    @property
    def tool_names(self) -> list[str]:
        """The tools in order, with `submit` always on."""
        names = list(dict.fromkeys(self.tools))
        return names if "submit" in names else [*names, "submit"]


class Scenario(_Strict):
    """`oversight` says what the monitor may do: observe only simulates its actions,
    autonomous acts and reviews its own pauses, human pauses for a person to decide. `monitor`
    sets the monitor over the config files; it records only the fields it names."""

    version: Literal[1]
    name: Name
    oversight: Oversight
    task: Task
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    limits: Limits = Field(default_factory=Limits)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()], min_length=1)
    monitor: MonitorConfig | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [a.id for a in self.agents]
        if len(set(ids)) != len(ids):
            raise ValueError("agent ids must be unique")
        roots = (self.sandbox.workdir, "/tmp", HOME)
        for entry in self.task.files:
            path = self.sandbox_path(entry.path)
            if not any(path.startswith(f"{r}/") for r in roots):
                raise ValueError(f"file {entry.path!r} is outside {', '.join(roots)}")
        return self

    def sandbox_path(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.sandbox.workdir, path))
