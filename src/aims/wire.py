"""What the control plane, its runners and the UI send each other, besides log lines and blobs."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aims.events import Event, HoldInfo, Label, OperatorActionKind, ResumeThen, RunKind
from aims.models import Profile
from aims.monitor.setup import ResolvedMonitor
from aims.run.job import Job, JobOptions

RUNNER_API = 1
HEARTBEAT_S = 10.0
STALE_S = 60.0
CLAIM_S = 10.0


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Head(_Wire):
    """Where a copy of a run's log ends: the last seq and its hash, seq -1 for none. `truth`
    says the control plane holds the run's truth file."""

    seq: int = -1
    hash: str | None = None
    truth: bool = False


class TracerHealth(_Wire):
    alive: bool
    detail: str | None = None
    restarts: int = 0


JobKindName = Literal["scenario", "models.check", "monitor", "score", "selftest"]
MonitorMode = Literal["full", "signals_only", "judge_only"]
BASE_KINDS: tuple[JobKindName, ...] = ("scenario", "models.check")


class RunnerHello(_Wire):
    """`kinds` are the job kinds the runner can run; it is offered no other."""

    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._@-]+$")
    version: str
    api: int = RUNNER_API
    arch: str
    tracer: TracerHealth
    envs: list[str] = Field(default_factory=list)
    slots: int = Field(default=2, ge=0)
    jobs: list[str] = Field(default_factory=list)
    kinds: list[JobKindName] = Field(default_factory=lambda: list(BASE_KINDS))


class HelloReply(_Wire):
    """`envs` names the variables the control plane's profiles read; the runner reports which
    of them it has set, never their values."""

    name: str
    heartbeat_s: float = HEARTBEAT_S
    requeued: list[str] = Field(default_factory=list)
    envs: list[str] = Field(default_factory=list)


class RunnerStatus(_Wire):
    tracer: TracerHealth
    envs: list[str] = Field(default_factory=list)
    free: int = Field(ge=0)
    running: list[str] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)


class CommandAction(_Wire):
    """An operator request for one run, as the runner gets it. `then` belongs to a resume:
    continue, kill, inject `text`, or deny the tool call the agent is held at. `sweep` asks the
    run's monitor for a sweep now, of `agent` or of every agent."""

    action: OperatorActionKind
    agent: str | None = None
    then: ResumeThen | None = None
    text: str | None = None
    reason: str | None = None
    token: str | None = None


class CommandMessage(_Wire):
    kind: Literal["command"] = "command"
    id: str
    seq: int
    run_id: str
    command: CommandAction


class OfferMessage(_Wire):
    kind: Literal["offer"] = "offer"
    run_id: str
    needs: dict[str, JsonValue] = Field(default_factory=dict)


RunnerFeed = Annotated[CommandMessage | OfferMessage, Field(discriminator="kind")]


class Ack(_Wire):
    ok: bool
    outcome: str


class MonitorJob(_Wire):
    """An offline monitor run over the sealed run `origin`, whose head hash was `origin_head`
    when the job was queued. The runner imports the origin like a fork does, feeds its events
    through the monitor and writes the findings into the new run `run_id`. `monitor` is the
    resolved monitor (`ResolvedMonitor.snapshot`, profiles hold env names only); `mode` drops
    sweeps and the judge (`signals_only`) or signals and sweeps (`judge_only`)."""

    kind: Literal["monitor"] = "monitor"
    run_id: str
    origin: str
    origin_head: str
    scenario: str
    mode: MonitorMode = "full"
    monitor: dict[str, JsonValue]
    options: JobOptions = Field(default_factory=JobOptions)
    config_files: dict[str, str | None] = Field(default_factory=dict)

    def resolved(self) -> ResolvedMonitor:
        """Raises MonitorConfigError."""
        return ResolvedMonitor.restore(self.monitor)

    def blobs(self) -> list[str]:
        return []

    def envs(self) -> list[str]:
        m = self.resolved()
        names: set[str] = set()
        for p in (*m.sweeps.values(), m.review, m.judge):
            if p is not None:
                names.update(e for e in (p.api_key_env, p.base_url_env) if e)
        return sorted(names)

    def needs(self) -> dict[str, JsonValue]:
        return {"envs": list(self.envs()), "kernel": False, "kind": self.kind}


class ScoreJob(_Wire):
    """A score of the sealed run `origin`, whose head hash was `origin_head`, against its truth
    file and the flags of the monitor run `monitor` (None for its own), into the new run
    `run_id`. The runner fetches the run, the monitor run and the truth file when it lacks
    them. `classifier` is the model of the `awareness` and `answer` checks (`Profile.snapshot`,
    env names only), None to skip both; `texts` the monitor texts it asks with. `added` is
    what a scenario file adds to the truth file (`Added.dump`), read by the control plane."""

    kind: Literal["score"] = "score"
    run_id: str
    origin: str
    origin_head: str
    scenario: str
    monitor: str | None = None
    classifier: dict[str, JsonValue] | None = None
    awareness: bool = True
    answer: bool = True
    added: dict[str, JsonValue] | None = None
    texts: dict[str, JsonValue] = Field(default_factory=dict)
    options: JobOptions = Field(default_factory=JobOptions)
    config_files: dict[str, str | None] = Field(default_factory=dict)

    def profile(self) -> Profile | None:
        return None if self.classifier is None else Profile.model_validate(self.classifier)

    def blobs(self) -> list[str]:
        return []

    def envs(self) -> list[str]:
        p = self.profile()
        return [] if p is None else sorted(e for e in (p.api_key_env, p.base_url_env) if e)

    def needs(self) -> dict[str, JsonValue]:
        return {"envs": list(self.envs()), "kernel": False, "kind": self.kind}


AnyJob = Annotated[Job | MonitorJob | ScoreJob, Field(discriminator="kind")]


class Claimed(_Wire):
    job: AnyJob


class Release(_Wire):
    reason: str


class Shas(_Wire):
    shas: list[str] = Field(max_length=10_000)


class Heads(_Wire):
    run_ids: list[str] = Field(max_length=10_000)


class HeadsReply(_Wire):
    heads: dict[str, Head]


class Labels(_Wire):
    labels: list[Label] = Field(max_length=10_000)


class WireDelta(_Wire):
    run_id: str
    agent_id: str | None
    call_id: str
    attempt: int
    kind: Literal["start", "text", "reasoning", "tool_call", "tool_args", "end"]
    text: str = ""
    index: int | None = None
    ms: int = 0


class Deltas(_Wire):
    deltas: list[WireDelta] = Field(max_length=10_000)
    dropped: int = 0


# The UI's live stream


class EventsMessage(_Wire):
    """New events of one run, in seq order."""

    kind: Literal["events"] = "events"
    run_id: str
    events: list[Event]


class RunSummary(_Wire):
    """`kind` says what the run is and `origin` names the run a monitor run watched. `flags`
    counts its logical flags and `categories` them per category, whatever their status;
    `max_importance` is the highest importance of a flag not cleared or rejected,
    `verdict_severity` the judge's, and `max_severity` the higher of the two, 0 for neither.
    `series` and `series_index` place a run in its series; `variant` is the scenario variant
    it ran, known once the run is sealed and its truth file arrived. `steps` counts its agents'
    model calls, `cost_usd` sums its priced live model responses, monitor calls included (None
    when there is none), `reward` is the verifier's last reward and `reviews` counts the
    monitor runs over it, queued ones too. `preset` is the preset of the monitor that watches
    the run, or that a monitor run runs, and `monitor_mode` a monitor run's mode. `held` lists
    its agents paused now, by the monitor or an operator, one entry per pause. `commands`
    counts the commands sent to it through the control plane and `commands_waiting` those the
    runner has not answered yet."""

    run_id: str
    scenario: str | None
    status: str
    state: str
    started_at: str | None = None
    ended_at: str | None = None
    events: int = 0
    runner: str | None = None
    stale: bool = False
    diverged: str | None = None
    group: str | None = None
    reason: str | None = None
    kind: RunKind = "scenario"
    origin: str | None = None
    flags: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    max_importance: int | None = None
    verdict_severity: int | None = None
    max_severity: int = 0
    series: str | None = None
    series_index: int | None = None
    variant: str | None = None
    steps: int = 0
    cost_usd: float | None = None
    reward: float | None = None
    reviews: int = 0
    preset: str | None = None
    monitor_mode: str | None = None
    held: list[HoldInfo] = Field(default_factory=list)
    commands: int = 0
    commands_waiting: int = 0


class RunMessage(_Wire):
    kind: Literal["run"] = "run"
    run: RunSummary


class RunnerInfo(_Wire):
    name: str
    version: str | None = None
    arch: str | None = None
    tracer: TracerHealth | None = None
    envs: list[str] = Field(default_factory=list)
    slots: int = 0
    free: int = 0
    running: list[str] = Field(default_factory=list)
    connected: bool = False
    stale: bool = True
    seen_s: float | None = None
    local: bool = False
    kinds: list[JobKindName] = Field(default_factory=lambda: list(BASE_KINDS))


class RunnerHealthMessage(_Wire):
    kind: Literal["runner"] = "runner"
    runner: RunnerInfo


class DeltaMessage(_Wire):
    kind: Literal["delta"] = "delta"
    deltas: list[WireDelta]


class DeltasDroppedMessage(_Wire):
    kind: Literal["deltas_dropped"] = "deltas_dropped"
    run_id: str | None = None
    count: int


class EndMessage(_Wire):
    """A subscribed run is finished and every event of it was sent."""

    kind: Literal["end"] = "end"
    run_id: str


StreamMessage = Annotated[
    EventsMessage
    | RunMessage
    | RunnerHealthMessage
    | DeltaMessage
    | DeltasDroppedMessage
    | EndMessage,
    Field(discriminator="kind"),
]
