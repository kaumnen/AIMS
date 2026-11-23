from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from aims.events import Draft, EventStore, ExecPhase, Payload
from aims.models import ToolSpec
from aims.prompts import OMITTED, TextError, check_format
from aims.scenario import SandboxConfig

if TYPE_CHECKING:
    from aims.agent.gate import Gate
    from aims.sandbox import AuditedSandbox

GROUP = "aims.tools"
OUTPUT_LIMIT = 16 * 1024


class Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool may use for one call. Execs go through `sandbox`, which records them.
    `omitted` is the agent's text for the middle of an output cut to size."""

    sandbox: AuditedSandbox
    store: EventStore
    run_id: str
    agent_id: str
    call_id: str
    step: int
    config: SandboxConfig
    gate: Gate
    phase: ExecPhase = "agent"
    omitted: str = OMITTED

    async def emit(self, payload: Payload) -> None:
        await self.store.append(self.run_id, [Draft(payload, self.agent_id)])


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What the model is given back. `answer` is set when the call ends the agent's task;
    `failed` when the sandbox could not run the call and is gone."""

    output: str
    error: bool = False
    answer: str | None = None
    failed: bool = False


class Tool(Protocol):
    """A tool the agent can call. `texts` are its agent-visible texts by key, the defaults a
    deployment or scenario may replace; `placeholders` names what each formatted text is given.
    An instance is built with the replacements and keeps the result in `text`."""

    name: ClassVar[str]
    args: ClassVar[type[Args]]
    texts: ClassVar[dict[str, str]]
    placeholders: ClassVar[dict[str, frozenset[str]]]
    text: dict[str, str]

    def __init__(self, texts: Mapping[str, str] | None = None) -> None: ...

    def spec(self, config: SandboxConfig) -> ToolSpec: ...

    async def run(self, ctx: ToolContext, args: Any) -> ToolOutcome: ...


class TextTool:
    """Keeps a tool's texts: its defaults with the replacements it was built with. `say` gives
    one of them, filled in with exactly the names its `placeholders` entry lists, or as written
    when it has none."""

    name: ClassVar[str]
    texts: ClassVar[dict[str, str]] = {}
    placeholders: ClassVar[dict[str, frozenset[str]]] = {}

    def __init__(self, texts: Mapping[str, str] | None = None) -> None:
        self.text = resolve(type(self), self.name, texts)

    def say(self, key: str, /, **values: object) -> str:
        allowed = self.placeholders.get(key)
        if set(values) != set(allowed or ()):
            raise TypeError(f"tool {self.name}: text {key} takes {sorted(allowed or ())}")
        return self.text[key] if allowed is None else self.text[key].format(**values)


class Texts(Protocol):
    """A class of tool with texts: their defaults by key and the names each formatted one takes."""

    texts: ClassVar[dict[str, str]]
    placeholders: ClassVar[dict[str, frozenset[str]]]


def resolve(cls: type[Texts], name: str, texts: Mapping[str, str] | None) -> dict[str, str]:
    """The texts of tool `name` of class `cls`: its defaults with `texts` laid over them.
    Raises TextError for a key it has no text for or a placeholder it is not given."""
    texts = dict(texts or {})
    unknown = sorted(set(texts) - set(cls.texts))
    if unknown:
        known = ", ".join(sorted(cls.texts))
        raise TextError(f"tool {name} has no text {', '.join(unknown)}; known: {known}")
    text = {**cls.texts, **texts}
    for key, allowed in cls.placeholders.items():
        check_format(text[key], allowed, f"tools.{name}.{key}")
    return text


class UnknownTool(Exception):
    pass


def registry() -> dict[str, type[Tool]]:
    """Tools by name, from the `aims.tools` entry point group."""
    out: dict[str, type[Tool]] = {}
    for ep in entry_points(group=GROUP):
        cls = ep.load()
        out[getattr(cls, "name", ep.name)] = cls
    return out


def load_tools(
    names: list[str], texts: Mapping[str, Mapping[str, str]] | None = None
) -> list[Tool]:
    """The tools `names`, each with its texts from `texts`. Texts for a known tool the agent
    does not use are ignored; for an unknown one they are an error."""
    known = registry()
    texts = texts or {}
    missing = [n for n in [*names, *texts] if n not in known]
    if missing:
        unknown = ", ".join(dict.fromkeys(missing))
        raise UnknownTool(f"unknown tool(s) {unknown}; known: {', '.join(sorted(known))}")
    return [known[n](texts.get(n)) for n in names]


def cap(data: bytes, limit: int = OUTPUT_LIMIT, marker: str = OMITTED) -> tuple[str, bool]:
    """`data` as text, cut to its first and last `limit / 2` bytes when longer than `limit`."""
    return cap_stream(data, data[-(limit // 2) :], len(data), limit, marker)


def cap_stream(
    head: bytes, tail: bytes, total: int, limit: int = OUTPUT_LIMIT, marker: str = OMITTED
) -> tuple[str, bool]:
    """A stream of `total` bytes as text, from its first bytes `head` and last bytes `tail`,
    cut to its first and last `limit / 2` bytes when longer than `limit`. `marker` stands in
    for the bytes left out, its `{bytes}` their count, on a line of its own."""
    if total <= limit and len(head) >= total:
        return head[:total].decode(errors="replace"), False
    half = limit // 2
    first = head[:half].decode(errors="ignore")
    last = tail[-half:].decode(errors="ignore") if total > half else ""
    omitted = total - len(first.encode()) - len(last.encode())
    return f"{first}\n{marker.format(bytes=omitted)}\n{last}", True
