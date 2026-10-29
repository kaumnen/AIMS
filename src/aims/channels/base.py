"""The channel interface. A channel is a way agents of one run may reach each other, a class
registered under the `aims.channels` entry point group by its scenario `kind`, built once per
run with a `ChannelSetup`.

The runner asks it, in order: `info` for `run.started`; `mounts` and `tools` for each member
before its sandbox starts; `start` once every sandbox runs (seed files, restore a series
state); `close` at the end, which returns the state the next run of a series starts from.

A channel class whose tools have agent-visible texts also offers `tool_texts`, so the run can
resolve and record those texts with the agent's other texts before the channel is built."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from aims.events import ChannelInfo, EventStore

if TYPE_CHECKING:
    from aims.sandbox import AuditedSandbox, SandboxBackend
    from aims.sandbox.base import Mount
    from aims.scenario import Scenario
    from aims.scenario.model import Channel as ChannelConfig
    from aims.tools import Tool

GROUP = "aims.channels"


class ChannelError(ValueError):
    """A channel's config does not hold, or it failed to start."""


@dataclass(frozen=True, slots=True)
class ChannelSetup:
    """What a channel is built with. `restored` is the archive of its state the last run of
    the series left, None for a fresh one. `texts` are each member's resolved tool texts, by
    agent id and then tool name (see `tool_texts`). `backend` runs the sandboxes; `base` is
    the scenario's directory, which `files` sources are read from. Without one, as in a fork,
    `sources` reads a source from what the run's truth file recorded: its members, each with
    `name` (relative to the source, "" for itself), `kind`, `mode`, `mtime`, `data` and
    `target`."""

    config: ChannelConfig
    scenario: Scenario
    run_id: str
    store: EventStore
    restored: bytes | None = None
    texts: Mapping[str, Mapping[str, Mapping[str, str]]] = field(default_factory=dict)
    backend: SandboxBackend | None = None
    base: Path | None = None
    sources: Callable[[str], Sequence[Any]] | None = None


class Channel(Protocol):
    kind: ClassVar[str]

    def __init__(self, setup: ChannelSetup) -> None: ...

    def info(self) -> ChannelInfo: ...

    def mounts(self, agent_id: str) -> Sequence[Mount]: ...

    def tools(self, agent_id: str) -> Sequence[Tool]: ...

    async def start(self, sandboxes: Mapping[str, AuditedSandbox]) -> None: ...

    async def close(self, sandboxes: Mapping[str, AuditedSandbox]) -> bytes | None: ...


def registry() -> dict[str, type[Channel]]:
    """Channel classes by kind, from the `aims.channels` entry point group."""
    out: dict[str, type[Channel]] = {}
    for ep in entry_points(group=GROUP):
        cls = ep.load()
        out[getattr(cls, "kind", ep.name)] = cls
    return out


def tool_texts(
    scenario: Scenario, agent_id: str, layered: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]]:
    """The texts of every channel tool `agent_id` gets, by tool name: each tool's defaults
    with the replacements `layered` holds under its name. Raises TextError."""
    known = registry()
    out: dict[str, dict[str, str]] = {}
    for config in scenario.channels_of(agent_id):
        texts = getattr(known.get(config.kind), "tool_texts", None)
        if texts is not None:
            out.update(texts(config, scenario, agent_id, layered))
    return out


def tool_names(scenario: Scenario) -> set[str]:
    """The name of every channel tool any agent of `scenario` gets."""
    return {name for a in scenario.agents for name in tool_texts(scenario, a.id, {})}


def build(setup: ChannelSetup) -> Channel:
    known = registry()
    cls = known.get(setup.config.kind)
    if cls is None:
        raise ChannelError(
            f"channel {setup.config.name}: no channel kind {setup.config.kind!r};"
            f" known: {', '.join(sorted(known))}"
        )
    return cls(setup)
