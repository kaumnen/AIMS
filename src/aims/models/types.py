from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aims.events import ModelBackend, ModelStatus, ModelUsage, ReasoningKind


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Message(_Strict):
    kind: Literal["message"] = "message"
    role: Literal["system", "developer", "user"]
    content: str


class ToolSpec(_Strict):
    name: str
    description: str = ""
    parameters: dict[str, JsonValue]


class ToolRequest(_Strict):
    """A function call the model asked for. `arguments` is the JSON text as the model sent it."""

    id: str
    name: str
    arguments: str


class Turn(_Strict):
    """One assistant answer. `native` holds the provider's own items for sending it back; it is
    used when the next request goes to the same backend, else the turn is rebuilt from its text
    and tool calls, which drops encrypted reasoning."""

    kind: Literal["turn"] = "turn"
    text: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolRequest] = Field(default_factory=list)
    backend: ModelBackend | None = None
    native: list[JsonValue] = Field(default_factory=list)


class ToolOutput(_Strict):
    kind: Literal["tool_output"] = "tool_output"
    call_id: str
    output: str


Item = Annotated[Message | Turn | ToolOutput, Field(discriminator="kind")]


class OutputSchema(_Strict):
    """Structured output: the answer must be JSON matching `json_schema`."""

    name: str
    json_schema: dict[str, JsonValue]
    strict: bool = True


class Prompt(_Strict):
    items: list[Item]
    tools: list[ToolSpec] = Field(default_factory=list)
    output_schema: OutputSchema | None = None


class Reply(_Strict):
    """The outcome of a model call, from its last attempt."""

    call_id: str
    profile: str
    status: ModelStatus
    attempts: int
    turn: Turn
    reasoning_kind: ReasoningKind = "none"
    finish_reason: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    cost_usd: float | None = None
    error: str | None = None

    @property
    def text(self) -> str:
        return self.turn.text

    @property
    def tool_calls(self) -> list[ToolRequest]:
        return self.turn.tool_calls
