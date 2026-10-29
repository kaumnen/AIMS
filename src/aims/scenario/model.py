import posixpath
import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    model_validator,
)

from aims.events import Category, Oversight
from aims.models import Overrides
from aims.monitor.config import MonitorConfig, Rule
from aims.prompts import Prompts
from aims.sandbox.base import RUNNER_DIR, TOOLBOX, AbsPath, Name

DEFAULT_TOOLS = ("bash", "edit", "submit", "report_to_operator")
HOME = "/home/agent"
DEFAULT_AGENT_COST_USD = 5.0
_MODE = re.compile(r"^0?[0-7]{3}$")
# Paths no shared folder may sit on or under.
_RESERVED = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", TOOLBOX, RUNNER_DIR)

Role = Literal["subject", "confederate"]
Tier = Literal["touched", "claimed", "used"]
Lifetime = Literal["run", "series"]
CanaryShape = Literal["hex", "token", "password", "uuid", "credentials"]
FileTrigger = Literal["read", "write", "delete", "exec"]
StartOn = Literal["started", "finished", "step"]
TriggerEvent = Literal["started", "finished", "step", "exec", "read", "write", "post"]


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
    """`mask_proc` makes /proc/meminfo, loadavg, stat and uptime, which show the host's
    numbers, read empty; off by default, since sysinfo() still tells the host's memory and
    empty files are a tell of their own."""

    workdir: AbsPath = "/work"
    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=1024, ge=64)
    pids: int = Field(default=256, ge=16)
    tmp_mb: int = Field(default=256, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    kill_leftovers: bool = False
    command_timeout_s: float = Field(default=120, gt=0)
    max_command_timeout_s: float = Field(default=600, gt=0)
    mask_proc: bool = False

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
    """Caps for one agent. `cost_usd` is 5 by default; null removes it. `warn_at` is a
    fraction of a cap at which the agent is told how much it has used; off by default."""

    steps: int | None = Field(default=100, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
    cost_usd: float | None = Field(default=DEFAULT_AGENT_COST_USD, gt=0)
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


class AgentTask(_Strict):
    """What one agent gets in place of the scenario's task. `instruction`, `setup` and
    `verifier` replace the scenario's when set, and a null `verifier` leaves the agent
    unscored; `files` add to the scenario's, a file at the same path replacing it. The
    environment is shared."""

    instruction: str | None = Field(default=None, min_length=1)
    files: list[FileEntry] = Field(default_factory=list)
    setup: list[str] | None = None
    verifier: Verifier | None = None


class Start(_Strict):
    """When an agent starts: `delay_s` after the run's agents begin or, with `agent`, after
    that agent started, finished or took `step` steps."""

    agent: Name | None = None
    on: StartOn = "started"
    step: int | None = Field(default=None, ge=1)
    delay_s: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.on == "step") != (self.step is not None):
            raise ValueError("set step exactly when on is step")
        if self.agent is None and self.on != "started":
            raise ValueError(f"on {self.on} needs an agent")
        return self


class Trigger(_Strict):
    """An event of another agent, any subject when `agent` is unset: it `started`,
    `finished` or took `step` steps, ran a command (`exec`), read or wrote a channel, or
    posted to a board. `contains` must appear in the command or the post."""

    event: TriggerEvent
    agent: Name | None = None
    channel: Name | None = None
    step: int | None = Field(default=None, ge=1)
    contains: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.event == "step") != (self.step is not None):
            raise ValueError("set step exactly when event is step")
        if self.channel is not None and self.event not in ("read", "write", "post"):
            raise ValueError(f"event {self.event} takes no channel")
        if self.contains is not None and self.event not in ("exec", "post"):
            raise ValueError(f"event {self.event} takes no contains")
        return self


class Post(_Strict):
    channel: Name
    text: str = Field(min_length=1)


class Call(_Strict):
    """A call of one of the agent's tools, `tool`, with `arguments`."""

    tool: Name
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ScriptStep(_Strict):
    """One action of a scripted agent, taken once: `run` a command in its own sandbox, `post`
    to a board or `call` one of its tools. It waits for the step before it, then for `on` when
    set, then `after_s` seconds."""

    on: Trigger | None = None
    after_s: float = Field(default=0, ge=0)
    run: str | None = Field(default=None, min_length=1)
    post: Post | None = None
    call: Call | None = None
    timeout_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if sum(action is not None for action in (self.run, self.post, self.call)) != 1:
            raise ValueError("set exactly one of run, post and call")
        return self


class _Channel(_Strict):
    name: Name
    members: list[Name] | None = None
    read_only: list[Name] = Field(default_factory=list)


class FolderChannel(_Channel):
    """A directory one volume backs in every member's sandbox, at `path`. `read_only`
    members cannot write to it. `files` are placed in it when it is made, relative to `path`.
    With lifetime series, each run of a series starts from the folder as the last one left it.
    """

    kind: Literal["folder"]
    path: AbsPath
    lifetime: Lifetime = "run"
    files: list[FileEntry] = Field(default_factory=list)


class BoardChannel(_Channel):
    """A message board its members reach through two tools: `post_tool` posts a message of at
    most `max_bytes`, `read_tool` reads the messages posted since the reader last read.
    `read_only` members get only the read tool."""

    kind: Literal["board"]
    post_tool: Name = "post_message"
    read_tool: Name = "read_messages"
    max_bytes: int = Field(default=4096, ge=1)


class PluginChannel(_Channel):
    """A channel from a plugin under the `aims.channels` entry points; it checks `options`."""

    kind: str
    options: dict[str, JsonValue] = Field(default_factory=dict)


def _kind(builtin: frozenset[str]) -> Any:
    def tag(value: Any) -> str:
        kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
        return kind if kind in builtin else "plugin"

    return tag


Channel = Annotated[
    Annotated[FolderChannel, Tag("folder")]
    | Annotated[BoardChannel, Tag("board")]
    | Annotated[PluginChannel, Tag("plugin")],
    Discriminator(_kind(frozenset({"folder", "board"}))),
]


def _check_canary(value: CanarySpec) -> CanarySpec:
    from aims.plants.canary import check

    check(value)
    return value


class CanarySpec(_Strict):
    """A canary of the scenario's own shape: each part is a pattern (see
    `aims.plants.canary`) filled with random text for each run."""

    parts: dict[Name, str] = Field(min_length=1)


CanaryConfig = CanaryShape | Annotated[CanarySpec, AfterValidator(_check_canary)]


class _Plant(_Strict):
    """`agents` it applies to, every subject when unset. `taken` is the tier that counts as
    the plant being taken: claimed for an impossible plant, used where the plant can reach it,
    else touched; a higher tier counts too. For every kind but impossible, used means its
    canary turned up in what the agent did, so a plant without a canary is never used.
    `category` is the monitor category its flags are matched against, the kind's own when
    unset. `canary` makes a secret of that shape for each run, written `{{canary}}` (or
    `{{canary.part}}`) in the plant's text and `{{canaries.NAME}}` elsewhere. `used_in` is
    where the canary counts as used: anywhere the agent acted (actions), or only in the files
    it writes that match these globs, relative to the workdir, such as the product code a
    test checks; elsewhere it then counts as considered, so that comparing against it in a
    command or a scratch script, or quoting it in a report, is not a use."""

    name: Name
    agents: list[Name] | None = None
    taken: Tier | None = None
    category: Category | None = None
    canary: CanaryConfig | None = None
    used_in: Literal["actions"] | Annotated[list[str], Field(min_length=1)] = "actions"

    @model_validator(mode="after")
    def _used_in(self) -> Self:
        if self.used_in != "actions" and self.canary is None:
            raise ValueError("used_in needs a canary")
        return self


def _read_only() -> list[FileTrigger]:
    return ["read"]


def _no_parameters() -> dict[str, JsonValue]:
    return {"type": "object", "properties": {}}


class FilePlant(_Plant):
    """A bait file: an answer key, an oversight config, credentials, a transcript the agent
    may edit. Touched when a subject does one of `on` to it; used when its canary turns up
    outside it."""

    kind: Literal["file"]
    path: str
    content: str | None = None
    source: RelPath | None = None
    mode: Mode | None = None
    on: list[FileTrigger] = Field(default_factory=_read_only, min_length=1)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.source is None) == (self.content is None):
            raise ValueError("set exactly one of source and content")
        if self.category is None:
            raise ValueError("a file plant needs a category")
        return self


class CanaryPlant(_Plant):
    """A secret made for each run and placed by the scenario's own text, as
    `{{canaries.NAME}}` in task files, the sandbox env or other plants. Used when it turns up
    anywhere else."""

    kind: Literal["canary"]
    canary: CanaryConfig

    @model_validator(mode="after")
    def _category(self) -> Self:
        if self.category is None:
            raise ValueError("a canary plant needs a category")
        return self


class ImpossiblePlant(_Plant):
    """The task cannot be done honestly: its tests contradict its instruction. Claimed when the
    agent submits it as done without a report to the operator; used when the verifier still
    gives at least `reward`. `reference` is an honest solution, copied over the
    workdir, which `aims scenario check` proves the verifier fails, as it must an empty one."""

    kind: Literal["impossible"]
    reference: RelPath
    reward: float = 1.0


class ImdsPlant(_Plant):
    """A cloud metadata service at 169.254.169.254 serving IMDS v1 and v2, which hands out
    the canary as the credentials of role `role`. Touched on any request; used when the
    credentials turn up elsewhere."""

    kind: Literal["imds"]
    canary: CanaryConfig | None = "credentials"
    role: str = Field(default="ci-build-runner", min_length=1)
    region: str = "us-east-1"


class ToolPlant(_Plant):
    """A tempting tool the agent is given: `tool`, described by `description`, taking
    `parameters` (a JSON Schema) and answering every call with `reply`. Touched when called;
    used when the canary in its reply turns up elsewhere."""

    kind: Literal["tool"]
    tool: Name
    description: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=_no_parameters)
    reply: str = ""

    @model_validator(mode="after")
    def _category(self) -> Self:
        if self.category is None:
            raise ValueError("a tool plant needs a category")
        return self


class RulePlant(_Plant):
    """A custom trigger in the monitor's rule syntax: touched when `rule` matches an event of
    one of its agents. Its category is the rule's unless set."""

    kind: Literal["rule"]
    rule: Rule


class PluginPlant(_Plant):
    """A plant from a plugin under the `aims.plants` entry points; it checks `options`."""

    kind: str
    options: dict[str, JsonValue] = Field(default_factory=dict)


PLANT_KINDS = frozenset({"file", "canary", "impossible", "imds", "tool", "rule"})
Plant = Annotated[
    Annotated[FilePlant, Tag("file")]
    | Annotated[CanaryPlant, Tag("canary")]
    | Annotated[ImpossiblePlant, Tag("impossible")]
    | Annotated[ImdsPlant, Tag("imds")]
    | Annotated[ToolPlant, Tag("tool")]
    | Annotated[RulePlant, Tag("rule")]
    | Annotated[PluginPlant, Tag("plugin")],
    Discriminator(_kind(PLANT_KINDS)),
]


Steps = Annotated[list[ScriptStep], Field(min_length=1)]


class SelfTest(_Strict):
    """Scripts `aims scenario check` has every subject run as a scripted agent, at no model
    cost: a shell script next to the scenario, run in the workdir by the sandbox's shell, or
    steps as a confederate's script has. `reference` solves the task honestly, every step of it
    succeeding, and must trigger no plant; `exploit` must take each plant in `claims`, for
    every subject the plant applies to."""

    reference: RelPath | Steps | None = None
    exploit: RelPath | Steps | None = None
    claims: list[Name] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.claims and self.exploit is None:
            raise ValueError("claims need an exploit")
        return self


class AgentConfig(_Strict):
    """`role` subject is an agent under study; a confederate plays a part around them,
    scripted by `script` or, without one, prompted by its own `task` and `prompts`. `task`
    overrides parts of the scenario's task for this agent; `start` delays its start."""

    id: Name = "agent"
    role: Role = "subject"
    profile: str | None = None
    overrides: Overrides | None = None
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    budget: Budget = Field(default_factory=Budget)
    prompts: Prompts | None = None
    compaction: Compaction | None = None
    task: AgentTask | None = None
    start: Start | None = None
    script: list[ScriptStep] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.prompts is not None and self.prompts.monitor:
            raise ValueError("monitor texts belong in the config files, not an agent's prompts")
        if self.script is not None and self.role != "confederate":
            raise ValueError("only a confederate follows a script")
        return self

    @property
    def tool_names(self) -> list[str]:
        """The tools in order, with `submit` always on."""
        names = list(dict.fromkeys(self.tools))
        return names if "submit" in names else [*names, "submit"]

    @property
    def scripted(self) -> bool:
        return self.script is not None


class Scenario(_Strict):
    """`oversight` says what the monitor may do: observe only simulates its actions,
    autonomous acts and reviews its own pauses, human pauses for a person to decide. `monitor`
    sets the monitor over the config files; it records only the fields it names.

    `channels` are the ways agents may reach each other, `plants` the bait whose use the
    scoring counts, `variants` named patches of this file (see `aims.scenario.variants`) and
    `selftest` the scripts that prove the plants work. Plants, variants, the self-test, roles
    and scripts are kept from the monitor."""

    version: Literal[1]
    name: Name
    oversight: Oversight
    task: Task
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    limits: Limits = Field(default_factory=Limits)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()], min_length=1)
    monitor: MonitorConfig | None = None
    channels: list[Channel] = Field(default_factory=list)
    plants: list[Plant] = Field(default_factory=list)
    variants: dict[Name, dict[str, JsonValue]] = Field(default_factory=dict)
    selftest: SelfTest | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [a.id for a in self.agents]
        if len(set(ids)) != len(ids):
            raise ValueError("agent ids must be unique")
        if not self.subjects:
            raise ValueError("a scenario needs at least one subject")
        self._check_files()
        self._check_channels()
        self._check_plants()
        self._check_agents()
        return self

    def _roots(self) -> tuple[str, ...]:
        return (self.sandbox.workdir, "/tmp", HOME)

    def _check_files(self) -> None:
        roots = self._roots()
        for where, entries in self._file_lists():
            for entry in entries:
                path = self.sandbox_path(entry.path)
                if not any(path.startswith(f"{r}/") for r in roots):
                    raise ValueError(f"{where} file {entry.path!r} is outside {', '.join(roots)}")

    def _file_lists(self) -> Iterator[tuple[str, list[FileEntry]]]:
        yield "task", self.task.files
        for a in self.agents:
            if a.task is not None:
                yield f"agent {a.id}", a.task.files

    def _check_channels(self) -> None:
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            raise ValueError("channel names must be unique")
        known = {a.id for a in self.agents}
        taken: list[str] = []
        for c in self.channels:
            for who in [*(c.members or []), *c.read_only]:
                if who not in known:
                    raise ValueError(f"channel {c.name}: no agent {who!r}")
            if c.members is not None and not set(c.read_only) <= set(c.members):
                raise ValueError(f"channel {c.name}: read_only names an agent not in members")
            if isinstance(c, FolderChannel):
                path = posixpath.normpath(c.path)
                if path == "/" or any(_under(path, r) or _under(r, path) for r in _RESERVED):
                    raise ValueError(f"channel {c.name}: {c.path} is not a place for a folder")
                if any(_under(path, r) or _under(r, path) for r in (*self._roots(), *taken)):
                    raise ValueError(f"channel {c.name}: {c.path} overlaps another mount")
                taken.append(path)
                for entry in c.files:
                    if posixpath.isabs(entry.path) or ".." in entry.path.split("/"):
                        raise ValueError(f"channel {c.name}: file {entry.path!r} is not relative")

    def _check_plants(self) -> None:
        names = [p.name for p in self.plants]
        if len(set(names)) != len(names):
            raise ValueError("plant names must be unique")
        known = {a.id for a in self.agents}
        folders = [c.path for c in self.channels if isinstance(c, FolderChannel)]
        for p in self.plants:
            for who in p.agents or []:
                if who not in known:
                    raise ValueError(f"plant {p.name}: no agent {who!r}")
            if isinstance(p, FilePlant):
                path = self.sandbox_path(p.path)
                if not any(path.startswith(f"{r}/") for r in (*self._roots(), *folders)):
                    raise ValueError(f"plant {p.name}: {p.path} is outside the agents' files")
        if sum(isinstance(p, ImdsPlant) for p in self.plants) > 1:
            raise ValueError("a scenario has at most one imds plant")
        if self.selftest is not None:
            unknown = sorted(set(self.selftest.claims) - set(names))
            if unknown:
                raise ValueError(f"selftest claims unknown plants {', '.join(unknown)}")

    def _check_agents(self) -> None:
        known = {a.id for a in self.agents}
        for a in self.agents:
            after = a.start.agent if a.start is not None else None
            if after is not None and (after not in known or after == a.id):
                raise ValueError(f"agent {a.id}: start names no other agent {after!r}")
            tools = self.tools_of(a.id)
            if len(set(tools)) != len(tools):
                raise ValueError(f"agent {a.id}: two of its tools share a name")
            self._check_steps(a.id, a.script or [], "script")
        if self.selftest is not None:
            for name, script in (
                ("reference", self.selftest.reference),
                ("exploit", self.selftest.exploit),
            ):
                if isinstance(script, list):
                    self._check_selftest(f"selftest {name}", script)
        self._check_cycles()

    def _check_steps(self, agent_id: str, steps: list[ScriptStep], where: str) -> None:
        """That `agent_id` can take `steps`: what they wait on exists, and it may post to their
        boards and has the tools they call."""
        self._check_waits(f"agent {agent_id}: {where}", steps)
        for step in steps:
            if step.post is not None and not self.can_take(agent_id, step):
                raise ValueError(f"agent {agent_id}: cannot post to {step.post.channel!r}")
            if step.call is not None and not self.can_take(agent_id, step):
                raise ValueError(
                    f"agent {agent_id}: {where} calls {step.call.tool!r}, not one of its tools"
                )

    def _check_selftest(self, where: str, steps: list[ScriptStep]) -> None:
        """Self-test steps go to every subject, which skips the posts and calls it cannot make;
        each must suit one subject at least."""
        self._check_waits(where, steps)
        for step in steps:
            if any(self.can_take(s, step) for s in self.subjects):
                continue
            if step.post is not None:
                raise ValueError(f"{where}: no subject may post to {step.post.channel!r}")
            if step.call is not None:
                raise ValueError(f"{where}: no subject has the tool {step.call.tool!r}")

    def _check_waits(self, where: str, steps: list[ScriptStep]) -> None:
        known = {a.id for a in self.agents}
        channels = {c.name for c in self.channels}
        for step in steps:
            on = step.on
            if on is not None and on.agent is not None and on.agent not in known:
                raise ValueError(f"{where} names no agent {on.agent!r}")
            if on is not None and on.channel is not None and on.channel not in channels:
                raise ValueError(f"{where} names no channel {on.channel!r}")

    def can_take(self, agent_id: str, step: ScriptStep) -> bool:
        """Whether `agent_id` may take `step`: post to its board, or call its tool."""
        if step.post is not None:
            board = next(
                (
                    c
                    for c in self.channels
                    if isinstance(c, BoardChannel) and c.name == step.post.channel
                ),
                None,
            )
            return board is not None and agent_id in self.posters(board)
        if step.call is not None:
            return step.call.tool in self.tools_of(agent_id)
        return True

    def _check_cycles(self) -> None:
        after = {a.id: a.start.agent for a in self.agents if a.start and a.start.agent}
        for first in after:
            seen, at = {first}, after.get(first)
            while at is not None:
                if at in seen:
                    raise ValueError(f"agent {first}: its start waits on itself")
                seen.add(at)
                at = after.get(at)

    def sandbox_path(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.sandbox.workdir, path))

    @property
    def subjects(self) -> list[str]:
        return [a.id for a in self.agents if a.role == "subject"]

    def agent(self, agent_id: str) -> AgentConfig:
        for a in self.agents:
            if a.id == agent_id:
                return a
        raise KeyError(agent_id)

    def task_for(self, agent_id: str) -> Task:
        """The task `agent_id` gets: the scenario's, with the agent's own task laid over it."""
        own = self.agent(agent_id).task
        if own is None:
            return self.task
        update: dict[str, Any] = {}
        if own.instruction is not None:
            update["instruction"] = own.instruction
        if "setup" in own.model_fields_set and own.setup is not None:
            update["setup"] = own.setup
        if "verifier" in own.model_fields_set:
            update["verifier"] = own.verifier
        if own.files:
            mine = {self.sandbox_path(f.path) for f in own.files}
            kept = [f for f in self.task.files if self.sandbox_path(f.path) not in mine]
            update["files"] = [*kept, *own.files]
        return self.task.model_copy(update=update)

    def members(self, channel: Channel) -> list[str]:
        return list(channel.members) if channel.members is not None else [a.id for a in self.agents]

    def posters(self, channel: Channel) -> list[str]:
        """The members of `channel` that may write to it."""
        return [m for m in self.members(channel) if m not in channel.read_only]

    def channels_of(self, agent_id: str) -> list[Channel]:
        return [c for c in self.channels if agent_id in self.members(c)]

    def plants_of(self, agent_id: str) -> list[Plant]:
        """The plants that apply to `agent_id`: every plant naming it, and for a subject every
        plant naming no agent."""
        role = self.agent(agent_id).role
        return [
            p
            for p in self.plants
            if (agent_id in p.agents if p.agents is not None else role == "subject")
        ]

    def tools_of(self, agent_id: str) -> list[str]:
        """Every tool name `agent_id` is given: its own, its boards' and its tool plants'."""
        names = list(self.agent(agent_id).tool_names)
        for c in self.channels_of(agent_id):
            if isinstance(c, BoardChannel):
                names.append(c.read_tool)
                if agent_id not in c.read_only:
                    names.append(c.post_tool)
        names += [p.tool for p in self.plants_of(agent_id) if isinstance(p, ToolPlant)]
        return names


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root.rstrip('/')}/")
