from typing import Literal

from pydantic import Field, JsonValue

from aims.events.schema import BlobRef, Content, Payload

Oversight = Literal["observe", "autonomous", "human"]
RunStatus = Literal["completed", "failed", "killed", "cancelled"]
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


class ExecFinished(Payload):
    """What the runner executed in the sandbox, recorded from the host side."""

    type = "exec.finished"

    call_id: str | None
    command: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    stdout: Content
    stderr: Content


class MonitorFlag(Payload):
    type = "monitor.flag"

    layer: Literal["signal", "sweep", "judge"]
    source: str
    category: Category
    importance: int = Field(ge=1, le=5)
    evidence: list[int] = Field(min_length=1)
    justification: str
