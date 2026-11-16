import math
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from pydantic import JsonValue

from aims.events import ModelBackend, ModelParams, ModelStatus, ModelUsage, ReasoningKind
from aims.models.profiles import Profile
from aims.models.types import Message, Prompt, ToolOutput, ToolRequest, Turn

DeltaKind = Literal["text", "reasoning", "tool_call", "tool_args"]


@dataclass(frozen=True, slots=True)
class Piece:
    kind: DeltaKind
    text: str
    index: int | None = None


@dataclass(slots=True)
class Assembled:
    """What an attempt produced, complete or not."""

    status: ModelStatus | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    text: str = ""
    reasoning: str = ""
    reasoning_kind: ReasoningKind = "none"
    tool_calls: list[ToolRequest] = field(default_factory=list)
    usage: ModelUsage = field(default_factory=ModelUsage)
    native: list[JsonValue] = field(default_factory=list)
    final: dict[str, Any] | None = None
    error: str | None = None
    error_code: int | None = None
    cost_usd: float | None = None
    provider: str | None = None
    model: str | None = None
    service_tier: str | None = None

    def turn(self, backend: ModelBackend) -> Turn:
        return Turn(
            text=self.text,
            reasoning=self.reasoning or None,
            tool_calls=self.tool_calls,
            backend=backend,
            native=self.native,
        )


class Accumulator(Protocol):
    def feed(self, event: dict[str, Any]) -> list[Piece]: ...

    def finish(self, body: dict[str, Any]) -> None: ...

    def result(self) -> Assembled: ...


class Backend(Protocol):
    name: ClassVar[ModelBackend]
    path: ClassVar[str]

    def body(self, profile: Profile, prompt: Prompt) -> dict[str, JsonValue]: ...

    def accumulator(self) -> Accumulator: ...


def params(profile: Profile, prompt: Prompt) -> ModelParams:
    s = profile.sampling
    return ModelParams(
        stream=profile.stream,
        effort=profile.effort,
        summary=profile.summary,
        max_output_tokens=profile.max_output_tokens,
        tools=[t.name for t in prompt.tools],
        parallel_tool_calls=profile.parallel_tool_calls if prompt.tools else None,
        temperature=s.temperature,
        top_p=s.top_p,
        top_k=s.top_k,
        seed=s.seed,
        schema_name=prompt.output_schema.name if prompt.output_schema else None,
    )


def _sampling(profile: Profile) -> dict[str, JsonValue]:
    return {k: v for k, v in profile.sampling.model_dump().items() if v is not None}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _reported_cost(usage: dict[str, Any]) -> float | None:
    """What the provider says the attempt cost in USD, as OpenRouter's `usage.cost` does. None
    when it says nothing. `cost` is what the router charged. With the caller's own provider
    key that is the router's fee alone, so the provider's bill is added to it."""
    cost = _amount(usage.get("cost"))
    if cost is None:
        return None
    if usage.get("is_byok") is True:
        cost += _amount(_get(usage, "cost_details", "upstream_inference_cost")) or 0.0
    return round(cost, 10)


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _fail(out: Assembled, error: object, default: str) -> None:
    """Mark `out` failed with a provider's error: `{"code": .., "message": ..}`, a bare
    message, or anything else, which says nothing more than that it failed."""
    out.status = "error"
    out.error = _str(error) or _str(_get(error, "message")) or default
    out.error_code = _int(_get(error, "code"))


def _served(out: Assembled, body: dict[str, Any]) -> None:
    """Keep what `body`, a response or a chunk of one, says about who answered: the provider a
    router picked, the model and the service tier."""
    out.provider = _str(body.get("provider")) or out.provider
    out.model = _str(body.get("model")) or out.model
    out.service_tier = _str(body.get("service_tier")) or out.service_tier


def _get(obj: object, *path: str) -> object:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


class Responses:
    """The Responses API, stateless: `store` is off and encrypted reasoning comes back so the
    next request can carry it."""

    name: ClassVar[ModelBackend] = "responses"
    path: ClassVar[str] = "/responses"

    def body(self, profile: Profile, prompt: Prompt) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "model": profile.model,
            "input": self.render(prompt),
            "store": False,
            "stream": profile.stream,
            "include": ["reasoning.encrypted_content"],
        }
        if prompt.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in prompt.tools
            ]
            if profile.parallel_tool_calls is not None:
                body["parallel_tool_calls"] = profile.parallel_tool_calls
        reasoning: dict[str, JsonValue] = {}
        if profile.effort is not None:
            reasoning["effort"] = profile.effort
        if profile.summary is not None:
            reasoning["summary"] = profile.summary
        if reasoning:
            body["reasoning"] = reasoning
        if profile.max_output_tokens is not None:
            body["max_output_tokens"] = profile.max_output_tokens
        body |= _sampling(profile)
        if prompt.output_schema is not None:
            o = prompt.output_schema
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": o.name,
                    "schema": o.json_schema,
                    "strict": o.strict,
                }
            }
        return body | profile.extra

    def render(self, prompt: Prompt) -> list[JsonValue]:
        out: list[JsonValue] = []
        for item in prompt.items:
            match item:
                case Message():
                    if out and _get(out[-1], "type") == "reasoning" and item.role == "user":
                        out.append({"role": "assistant", "content": "."})
                    out.append({"role": item.role, "content": item.content})
                case Turn() if item.backend == self.name and item.native:
                    out.extend(item.native)
                case Turn():
                    if item.text:
                        out.append({"role": "assistant", "content": item.text})
                    out.extend(
                        {
                            "type": "function_call",
                            "call_id": c.id,
                            "name": c.name,
                            "arguments": c.arguments,
                        }
                        for c in item.tool_calls
                    )
                case ToolOutput():
                    out.append(
                        {
                            "type": "function_call_output",
                            "call_id": item.call_id,
                            "output": item.output,
                        }
                    )
        return out

    def accumulator(self) -> Accumulator:
        return _ResponsesAccumulator()


class _ResponsesAccumulator:
    def __init__(self) -> None:
        self.out = Assembled()
        self.items: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self.summary_parts = 0

    def feed(self, event: dict[str, Any]) -> list[Piece]:
        kind = event.get("type")
        out = self.out
        match kind:
            case "response.created" | "response.in_progress":
                out.provider_id = _str(_get(event, "response", "id")) or out.provider_id
                if isinstance(response := event.get("response"), dict):
                    _served(out, response)
            case "response.output_text.delta":
                delta = str(event.get("delta", ""))
                out.text += delta
                return [Piece("text", delta)]
            case "response.reasoning_summary_part.added":
                self.summary_parts += 1
                if self.summary_parts > 1 and out.reasoning:
                    out.reasoning += "\n\n"
                    return [Piece("reasoning", "\n\n")]
            case "response.reasoning_summary_text.delta" | "response.reasoning_text.delta":
                delta = str(event.get("delta", ""))
                out.reasoning += delta
                full = kind == "response.reasoning_text.delta"
                out.reasoning_kind = "full" if full else "summary"
                return [Piece("reasoning", delta)]
            case "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    index = len(self.calls)
                    self.calls[str(item.get("id"))] = index
                    return [Piece("tool_call", str(item.get("name", "")), index)]
            case "response.function_call_arguments.delta":
                index = self.calls.get(str(event.get("item_id")))
                return [Piece("tool_args", str(event.get("delta", "")), index)]
            case "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    self.items.append(item)
            case "response.completed" | "response.incomplete" | "response.failed":
                response = event.get("response")
                if isinstance(response, dict):
                    self.finish(response)
            case "error":
                _fail(out, event, _str(event.get("code")) or "stream error")
        return []

    def finish(self, body: dict[str, Any]) -> None:
        out = self.out
        out.final = body
        out.provider_id = _str(body.get("id")) or out.provider_id
        _served(out, body)
        status = body.get("status")
        if status == "completed":
            out.status = "completed"
        elif status == "incomplete":
            out.status = "incomplete"
            out.finish_reason = _str(_get(body, "incomplete_details", "reason"))
        else:
            _fail(out, body.get("error"), f"response status {status}")
        output = body.get("output")
        if isinstance(output, list):
            self.items = [i for i in output if isinstance(i, dict)]
        self._from_items()
        usage = body.get("usage")
        if isinstance(usage, dict):
            out.usage = ModelUsage(
                input_tokens=_int(usage.get("input_tokens")),
                cached_tokens=_int(_get(usage, "input_tokens_details", "cached_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                reasoning_tokens=_int(_get(usage, "output_tokens_details", "reasoning_tokens")),
            )
            out.cost_usd = _reported_cost(usage)
        if out.status == "completed" and out.finish_reason is None:
            out.finish_reason = "tool_calls" if out.tool_calls else "stop"

    def _from_items(self) -> None:
        out = self.out
        text: list[str] = []
        summaries: list[str] = []
        full: list[str] = []
        calls: list[ToolRequest] = []
        for item in self.items:
            match item.get("type"):
                case "message":
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            text.append(str(part.get("text", "")))
                case "reasoning":
                    for part in item.get("summary") or []:
                        if isinstance(part, dict) and part.get("text"):
                            summaries.append(str(part["text"]))
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "reasoning_text":
                            full.append(str(part.get("text", "")))
                case "function_call":
                    calls.append(
                        ToolRequest(
                            id=str(item.get("call_id", "")),
                            name=str(item.get("name", "")),
                            arguments=str(item.get("arguments", "")),
                        )
                    )
        out.text = "".join(text)
        out.reasoning = "\n\n".join(full or summaries)
        out.reasoning_kind = "full" if full else "summary" if summaries else "none"
        out.tool_calls = calls
        out.native = list(self.items)

    def result(self) -> Assembled:
        if self.out.final is None and self.items:
            text, reasoning, kind = self.out.text, self.out.reasoning, self.out.reasoning_kind
            self._from_items()
            self.out.text = self.out.text or text
            self.out.reasoning = self.out.reasoning or reasoning
            self.out.reasoning_kind = self.out.reasoning_kind if self.out.reasoning else kind
        return self.out


class Chat:
    """Chat Completions, as served by Fireworks, vLLM and other OpenAI-compatible servers."""

    name: ClassVar[ModelBackend] = "chat"
    path: ClassVar[str] = "/chat/completions"

    def body(self, profile: Profile, prompt: Prompt) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "model": profile.model,
            "messages": self.render(prompt),
            "stream": profile.stream,
        }
        if profile.stream:
            body["stream_options"] = {"include_usage": True}
        if prompt.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in prompt.tools
            ]
            if profile.parallel_tool_calls is not None:
                body["parallel_tool_calls"] = profile.parallel_tool_calls
        if profile.effort is not None and profile.effort_kwarg is not None:
            body["chat_template_kwargs"] = {profile.effort_kwarg: profile.effort}
        elif profile.effort is not None:
            body["reasoning_effort"] = profile.effort
        if profile.max_output_tokens is not None:
            body["max_tokens"] = profile.max_output_tokens
        body |= _sampling(profile)
        if prompt.output_schema is not None:
            o = prompt.output_schema
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": o.name, "schema": o.json_schema, "strict": o.strict},
            }
        return body | profile.extra

    def render(self, prompt: Prompt) -> list[JsonValue]:
        out: list[JsonValue] = []
        for item in prompt.items:
            match item:
                case Message():
                    out.append({"role": item.role, "content": item.content})
                case Turn() if item.backend == self.name and item.native:
                    out.extend(item.native)
                case Turn():
                    message: dict[str, JsonValue] = {"role": "assistant", "content": item.text}
                    if item.tool_calls:
                        message["tool_calls"] = [_chat_call(c) for c in item.tool_calls]
                    out.append(message)
                case ToolOutput():
                    out.append(
                        {"role": "tool", "tool_call_id": item.call_id, "content": item.output}
                    )
        return out

    def accumulator(self) -> Accumulator:
        return _ChatAccumulator()


def _chat_call(call: ToolRequest) -> dict[str, JsonValue]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


class _ChatAccumulator:
    def __init__(self) -> None:
        self.out = Assembled()
        self.calls: dict[int, dict[str, str]] = {}
        self.reasoning_field = "reasoning_content"
        self.details: dict[tuple[object, object], dict[str, Any]] = {}
        self.head: dict[str, Any] = {}
        self.billed: dict[str, Any] = {}

    def feed(self, event: dict[str, Any]) -> list[Piece]:
        out = self.out
        pieces: list[Piece] = []
        out.provider_id = _str(event.get("id")) or out.provider_id
        _served(out, event)
        for key in _HEAD:
            if key in event and key not in self.head:
                self.head[key] = event[key]
        usage = event.get("usage")
        if isinstance(usage, dict):
            out.usage = _chat_usage(usage)
            out.cost_usd = _reported_cost(usage)
            self.billed = {k: usage[k] for k in _BILLED if k in usage}
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if text := delta.get("content"):
                out.text += text
                pieces.append(Piece("text", text))
            for name in ("reasoning_content", "reasoning"):
                if (thought := delta.get(name)) and isinstance(thought, str):
                    self.reasoning_field = name
                    out.reasoning += thought
                    out.reasoning_kind = "full"
                    pieces.append(Piece("reasoning", thought))
            for part in delta.get("reasoning_details") or []:
                if isinstance(part, dict):
                    self._detail(part)
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0))
                fn = call.get("function") or {}
                state = self.calls.get(index)
                if state is None:
                    state = self.calls[index] = {"id": "", "name": "", "arguments": ""}
                    pieces.append(Piece("tool_call", str(fn.get("name") or ""), index))
                if call.get("id"):
                    state["id"] = str(call["id"])
                if fn.get("name"):
                    state["name"] = str(fn["name"])
                if args := fn.get("arguments"):
                    state["arguments"] += str(args)
                    pieces.append(Piece("tool_args", str(args), index))
            if choice.get("finish_reason"):
                out.finish_reason = str(choice["finish_reason"])
        return pieces

    def _detail(self, part: dict[str, Any]) -> None:
        """Add a streamed piece of `reasoning_details` to the block it belongs to. A router
        such as OpenRouter sends a block in pieces that share an `index`: its text, summary or
        encrypted data grows, and a field such as the signature comes once."""
        block = self.details.setdefault((part.get("index"), part.get("type")), {})
        for key, value in part.items():
            grown = block.get(key)
            if key in _GROWS and isinstance(value, str) and isinstance(grown, str):
                block[key] = grown + value
            elif value is not None or key not in block:
                block[key] = value

    def finish(self, body: dict[str, Any]) -> None:
        out = self.out
        out.final = body
        out.provider_id = _str(body.get("id"))
        _served(out, body)
        choices = body.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        out.text = str(message.get("content") or "")
        for name in ("reasoning_content", "reasoning"):
            if (thought := message.get(name)) and isinstance(thought, str):
                self.reasoning_field = name
                out.reasoning = thought
                out.reasoning_kind = "full"
        details = message.get("reasoning_details")
        self.details = {
            (n, None): part
            for n, part in enumerate(details if isinstance(details, list) else [])
            if isinstance(part, dict)
        }
        for index, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            self.calls[index] = {
                "id": str(call.get("id", "")),
                "name": str(fn.get("name", "")),
                "arguments": str(fn.get("arguments", "")),
            }
        out.finish_reason = _str(choice.get("finish_reason"))
        usage = body.get("usage")
        if isinstance(usage, dict):
            out.usage = _chat_usage(usage)
            out.cost_usd = _reported_cost(usage)
            self.billed = {k: usage[k] for k in _BILLED if k in usage}
        # A provider behind a router can fail after the router answered 200: the body then
        # holds an error, alone or beside a choice that finished with "error".
        if body.get("error"):
            _fail(out, body["error"], "the answer is an error with no message")
        elif choice.get("error"):
            _fail(out, choice["error"], "the answer ended with an error")

    def result(self) -> Assembled:
        out = self.out
        out.tool_calls = [ToolRequest(**self.calls[i]) for i in sorted(self.calls)]
        message: dict[str, JsonValue] = {"role": "assistant", "content": out.text or None}
        if out.reasoning:
            message[self.reasoning_field] = out.reasoning
        if self.details:
            # The blocks go back as they came, in order. A model with signed or encrypted
            # reasoning reads them from here and drops the plain text above.
            message["reasoning_details"] = list(self.details.values())
            kinds = {d.get("type") for d in self.details.values()}
            if out.reasoning and "reasoning.summary" in kinds and "reasoning.text" not in kinds:
                out.reasoning_kind = "summary"
        if out.tool_calls:
            message["tool_calls"] = [_chat_call(c) for c in out.tool_calls]
        out.native = [message]
        if out.status is None:
            match out.finish_reason:
                case "stop" | "tool_calls":
                    out.status = "completed"
                case "error":
                    out.status = "error"
                    out.error = out.error or "the answer ended with an error"
                case None:
                    pass
                case _:
                    out.status = "incomplete"
        if out.final is None and out.finish_reason is not None:
            usage = {
                "prompt_tokens": out.usage.input_tokens,
                "completion_tokens": out.usage.output_tokens,
                "prompt_tokens_details": {"cached_tokens": out.usage.cached_tokens},
                "completion_tokens_details": {"reasoning_tokens": out.usage.reasoning_tokens},
                **self.billed,
            }
            out.final = {
                **self.head,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": out.finish_reason}],
                "usage": usage,
            }
        return out


# What a stream's chunks say about the answer as a whole, kept in the body assembled from it.
_HEAD = (
    "id",
    "created",
    "model",
    "provider",
    "system_fingerprint",
    "service_tier",
)

# The fields of a reasoning block that arrive in pieces on a stream.
_GROWS = ("text", "summary", "data")

# What a provider's usage says about the bill, kept in the body assembled from a stream.
_BILLED = ("cost", "is_byok", "cost_details")


def _chat_usage(usage: dict[str, Any]) -> ModelUsage:
    return ModelUsage(
        input_tokens=_int(usage.get("prompt_tokens")),
        cached_tokens=_int(_get(usage, "prompt_tokens_details", "cached_tokens")),
        output_tokens=_int(usage.get("completion_tokens")),
        reasoning_tokens=_int(_get(usage, "completion_tokens_details", "reasoning_tokens")),
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


BACKENDS: dict[ModelBackend, Backend] = {"responses": Responses(), "chat": Chat()}
