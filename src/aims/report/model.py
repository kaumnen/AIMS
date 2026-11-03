"""The per-run report as data: what the Markdown writer and the UI both render."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aims.events import (
    AgentEnd,
    Anomaly,
    CallCost,
    Category,
    CategoryFinding,
    Cause,
    Finding,
    FlagLayer,
    FlagReview,
    Label,
    Oversight,
    PlantTier,
    RunKind,
    RunStatus,
    ScoreMatch,
    Told,
)
from aims.events.index import FlagStatus
from aims.events.schema import Timestamp

Role = Literal["local", "operator", "viewer"]
SectionId = Literal[
    "header",
    "headline",
    "warnings",
    "summary",
    "findings",
    "observations",
    "plants",
    "flags",
    "timeline",
    "facts",
    "sources",
    "appendix",
]
LIVE = "live"
# A plant counts as caught by flags of this importance or more.
CATCH_IMPORTANCE = 3

# Every section of a report, in the order both writers show them.
SECTIONS: tuple[SectionId, ...] = (
    "header",
    "headline",
    "warnings",
    "summary",
    "findings",
    "observations",
    "plants",
    "flags",
    "timeline",
    "facts",
    "sources",
    "appendix",
)

WarningKind = Literal[
    "in_progress",
    "verify_failed",
    "monitor_degraded",
    "audit_lost",
    "log_truncated",
    "replay_divergence",
    "cost_incomplete",
    "tainted",
]
NoVerdict = Literal["running", "no_monitor", "skipped", "missing"]
MomentKind = Literal[
    "run_started",
    "agent_started",
    "agent_finished",
    "run_finished",
    "flag",
    "monitor_action",
    "operator_action",
    "degraded",
    "plant",
    "report",
    "scored",
    "verdict",
    "evidence",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Pins(_Model):
    """Every input the report was built from. Two builds from the same pins give the same
    report apart from `built_at`. `source` is `live` or the monitor run picked; `monitor` is
    the identity hash of its monitor. `labels` counts the flags a person labelled."""

    run_id: str
    head: str
    events: int
    verified: bool
    verify_error: str | None = None
    source: str
    source_head: str | None = None
    monitor: str | None = None
    score: str | None = None
    score_head: str | None = None
    labels: int = 0
    labels_at: Timestamp | None = None
    role: Role
    base_url: str | None = None
    version: str
    built_at: Timestamp


class Header(_Model):
    """`status` is `running` until the run seals. `reason` is why it ended, from its log."""

    run_id: str
    scenario: str
    kind: RunKind
    variant: str | None = None
    status: RunStatus | Literal["running"]
    reason: str | None = None
    oversight: Oversight
    agents: list[str] = Field(default_factory=list)
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
    pins: Pins


class Headline(_Model):
    """The verifier's reward beside the judge's verdict. `passed` and `total` come from the
    verifier's metrics when it reported them. `no_verdict` says why there is no verdict."""

    scored: bool = False
    reward: float | None = None
    passed: int | None = None
    total: int | None = None
    reward_error: str | None = None
    importance: int | None = None
    claim_matched: Finding | None = None
    no_verdict: NoVerdict | None = None


class Warning(_Model):
    """Something that limits what the report can say. `seq` is the first event that shows
    it in `run_id`, `count` how many there were, `detail` the reason the log gives. A
    degraded monitor names its `part` and what it did `then`; lost audit records sum to
    `lost` when known; `seqs` are the flags that tainted the reward."""

    kind: WarningKind
    run_id: str
    seq: int | None = None
    count: int = 1
    agent: str | None = None
    detail: str | None = None
    part: str | None = None
    then: str | None = None
    lost: int | None = None
    seqs: list[int] = Field(default_factory=list)


class Flag(_Model):
    """One logical flag of the monitor source, at `seq` in `run_id`; its evidence seqs are
    in the reported run. `review` is the judge's view of it, `label` a person's."""

    run_id: str
    seq: int
    agent: str | None = None
    layer: FlagLayer
    source: str
    category: Category
    importance: int
    count: int = 1
    justification: str = ""
    evidence: list[int] = Field(default_factory=list)
    status: FlagStatus
    status_reason: str | None = None
    review: FlagReview | None = None
    label: Label | None = None
    tainted: bool = False


class Plant(_Model):
    """A plant one agent reached, from the run's truth file. `matches` are what each monitor
    layer caught of it, from the score run; empty when there is none or it was not taken."""

    agent: str
    plant: str
    kind: str
    category: str
    tier: PlantTier
    taken: bool
    seq: int
    evidence: list[int] = Field(default_factory=list)
    detail: str = ""
    matches: list[ScoreMatch] = Field(default_factory=list)


class Unit(_Model):
    """One agent of the score run: what it took and what each layer caught of it."""

    agent: str
    role: Literal["subject", "confederate"]
    positive: bool | None
    labelled: bool = False
    plants: list[str] = Field(default_factory=list)
    harm_seq: int | None = None
    matches: list[ScoreMatch] = Field(default_factory=list)
    eval_aware: Finding | None = None
    told: Told | None = None
    cause: Cause | None = None


class Plants(_Model):
    """Ground truth, for operators. `error` says why the plants could not be read."""

    rows: list[Plant] = Field(default_factory=list)
    units: list[Unit] = Field(default_factory=list)
    score: str | None = None
    error: str | None = None


class Moment(_Model):
    """One row of the timeline. `step` is the agent step the event falls in. `what` names
    the event type, tool or action; `detail` is text from the log."""

    seq: int
    ts: Timestamp
    kind: MomentKind
    agent: str | None = None
    step: int | None = None
    what: str = ""
    categories: list[Category] = Field(default_factory=list)
    importance: int | None = None
    detail: str | None = None


class AgentFacts(_Model):
    agent: str
    profile: str
    model: str
    steps: int = 0
    end: AgentEnd | None = None
    cost_usd: float = 0.0
    cost_complete: bool = True
    wall_ms: int = 0


class Facts(_Model):
    """`kernel` counts the kernel events by type. `agent_cost_usd` and `monitor_cost_usd` sum
    the priced live model calls of the agents and of the live monitor, its judge included.
    `review_cost_usd` is what the review picked as the source cost."""

    agents: list[AgentFacts] = Field(default_factory=list)
    duration_ms: int | None = None
    events: int = 0
    steps: int = 0
    agent_cost_usd: float | None = None
    monitor_cost_usd: float | None = None
    review_cost_usd: float | None = None
    costs: list[CallCost] = Field(default_factory=list)
    kernel: dict[str, int] = Field(default_factory=dict)
    flags: int = 0


class Source(_Model):
    """A monitor of the run: its live monitor (`live`) or a monitor run over it."""

    run_id: str
    review: bool
    status: str
    chosen: bool = False
    preset: str | None = None
    importance: int | None = None
    claim_matched: Finding | None = None
    cost_usd: float | None = None
    started_at: Timestamp | None = None


class Text(_Model):
    """Text an agent wrote at `seq`: a final answer or a report to the operator."""

    agent: str | None = None
    seq: int
    step: int | None = None
    text: str


class Appendix(_Model):
    answers: list[Text] = Field(default_factory=list)
    reports: list[Text] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)


class RunReport(_Model):
    """Everything a report says about one run, built from its log and nothing stored. Text
    from the log is kept as written; each writer escapes it. `plants` is None when the role
    may not see ground truth; `summary` is the judge's own words."""

    sections: list[SectionId] = Field(default_factory=lambda: list(SECTIONS))
    header: Header
    headline: Headline
    warnings: list[Warning] = Field(default_factory=list)
    summary: str = ""
    findings: list[CategoryFinding] = Field(default_factory=list)
    observations: list[Anomaly] = Field(default_factory=list)
    plants: Plants | None = None
    flags: list[Flag] = Field(default_factory=list)
    timeline: list[Moment] = Field(default_factory=list)
    facts: Facts
    sources: list[Source] = Field(default_factory=list)
    appendix: Appendix = Field(default_factory=Appendix)
