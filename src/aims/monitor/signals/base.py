"""The signal interface. A signal is a class registered under the `aims.signals` entry point
group, built once per run with a `SignalSetup` and fed every event of its types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from aims.events import Category, Event, ExecPhase

if TYPE_CHECKING:
    from aims.monitor.config import MonitorConfig
    from aims.monitor.state import State
    from aims.scenario import Scenario

GROUP = "aims.signals"


class SignalError(ValueError):
    """A signal's options or a rule do not hold."""


@dataclass(frozen=True, slots=True)
class Hit:
    """One detection by a signal, before deduplication. `agent_id` is the agent it concerns,
    `evidence` the seqs it rests on, `key` what deduplicates it within the signal."""

    category: Category
    importance: int
    evidence: Sequence[int]
    justification: str
    agent_id: str | None = None
    key: str = ""
    subcategory: str | None = None
    phase: ExecPhase | None = None


@dataclass(frozen=True, slots=True)
class SignalSetup:
    """What a signal is built with: the run's monitor config and scenario, and its own
    `options` from `monitor.signals.<name>.options`."""

    config: MonitorConfig
    scenario: Scenario
    options: Mapping[str, JsonValue] = field(default_factory=dict)


class Signal(Protocol):
    """A detector fed every event of the types in `types` (all when empty), in seq order,
    after the shared `State` has taken the event in. `close` runs once at the end of the run.

    It is built with a `SignalSetup` and raises SignalError for options it does not accept.
    `key_cap` bounds the keys it may flag per agent before the rest share one overflow key."""

    name: ClassVar[str]
    version: ClassVar[str]
    types: ClassVar[frozenset[str]]

    def __init__(self, setup: SignalSetup) -> None: ...

    async def feed(self, event: Event, state: State) -> Sequence[Hit]: ...

    async def close(self, state: State) -> Sequence[Hit]: ...


class Options(BaseModel):
    """Base of a signal's options model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Base:
    """Shared plumbing for built-in signals: options parsed into `Opts`, and no hits at close
    unless overridden."""

    name: ClassVar[str]
    version: ClassVar[str] = "1"
    types: ClassVar[frozenset[str]] = frozenset()
    key_cap: ClassVar[int | None] = None
    Opts: ClassVar[type[Options]] = Options

    def __init__(self, setup: SignalSetup) -> None:
        self.setup = setup
        self.opts: Any = parse_options(self.name, self.Opts, setup.options)

    async def feed(self, event: Event, state: State) -> Sequence[Hit]:
        return ()

    async def close(self, state: State) -> Sequence[Hit]:
        return ()


def parse_options[O: BaseModel](name: str, model: type[O], options: Mapping[str, Any]) -> O:
    """`options` as `model`. Raises SignalError naming the signal."""
    try:
        return model.model_validate(dict(options))
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'options'}: {err['msg']}"
            for err in e.errors(include_url=False)
        )
        raise SignalError(f"signal {name}: {problems}") from None


def registry() -> dict[str, type[Signal]]:
    """Signals by name, from the `aims.signals` entry point group."""
    out: dict[str, type[Signal]] = {}
    for ep in entry_points(group=GROUP):
        cls = ep.load()
        out[getattr(cls, "name", ep.name)] = cls
    return out
