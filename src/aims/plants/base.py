"""The plant interface. A plant is bait whose use the scoring counts, a class registered under
the `aims.plants` entry point group by its scenario `kind`, built once per run with a
`PlantSetup` that holds the run's canaries.

The runner asks it for each agent it applies to: `place` before the seed (files, env and
tools added to that agent's sandbox), `start` once the sandbox runs and `stop` before it
closes. `record` is what the truth file keeps for the scoring, which the monitor never sees.
`lint` lists what would give the plant away."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, ClassVar, Protocol

from pydantic import JsonValue

from aims.events import Category, PlantTier

if TYPE_CHECKING:
    from aims.plants.canary import Canary
    from aims.sandbox import AuditedSandbox, SandboxBackend
    from aims.scenario import Scenario
    from aims.scenario.model import Plant as PlantConfig
    from aims.tools import Tool

GROUP = "aims.plants"
GIVEAWAY = re.compile(r"\b(unmonitored|honeypot|canary|eval|evaluation)\b", re.IGNORECASE)


class PlantError(ValueError):
    """A plant's config does not hold, or it failed to start."""


@dataclass(frozen=True, slots=True)
class PlacedFile:
    """A file a plant puts in a sandbox before the agent starts, owned by the agent."""

    path: str
    data: bytes
    mode: int = 0o664


@dataclass(slots=True)
class Placement:
    files: list[PlacedFile] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlantSetup:
    """What a plant is built with: its config, the scenario, the run's seed, every canary of
    the run by plant name, and `source`, the bytes its `source` or `reference` names, read with
    the scenario."""

    config: PlantConfig
    scenario: Scenario
    canaries: Mapping[str, Canary] = field(default_factory=dict)
    source: bytes | None = None
    seed: bytes = b""


class Plant(Protocol):
    """`category` is the kind's default monitor category; `tiers` the tiers it can reach."""

    kind: ClassVar[str]
    category: ClassVar[Category | None]
    tiers: ClassVar[frozenset[PlantTier]]

    def __init__(self, setup: PlantSetup) -> None: ...

    def place(self, agent_id: str) -> Placement: ...

    def record(self) -> dict[str, JsonValue]: ...

    async def start(
        self, agent_id: str, sandbox: AuditedSandbox, backend: SandboxBackend
    ) -> None: ...

    async def stop(self) -> None: ...

    def lint(self) -> Sequence[str]: ...


def registry() -> dict[str, type[Plant]]:
    """Plant classes by kind, from the `aims.plants` entry point group."""
    out: dict[str, type[Plant]] = {}
    for ep in entry_points(group=GROUP):
        cls = ep.load()
        out[getattr(cls, "kind", ep.name)] = cls
    return out


def build(setup: PlantSetup) -> Plant:
    known = registry()
    cls = known.get(setup.config.kind)
    if cls is None:
        raise PlantError(
            f"plant {setup.config.name}: no plant kind {setup.config.kind!r};"
            f" known: {', '.join(sorted(known))}"
        )
    return cls(setup)


def category_of(config: PlantConfig, cls: type[Plant]) -> Category:
    """The monitor category flags are matched against for `config`."""
    if config.category is not None:
        return config.category
    rule = getattr(config, "rule", None)
    if rule is not None:
        return rule.category
    if cls.category is None:
        raise PlantError(f"plant {config.name}: kind {config.kind} needs a category")
    return cls.category


def reachable(config: PlantConfig, cls: type[Plant]) -> frozenset[PlantTier]:
    """The tiers `config` can reach. Where its kind's used tier is its canary turning up, as
    for every kind whose `canary_used` is not False, a plant without a canary has none."""
    if config.canary is None and getattr(cls, "canary_used", True):
        return cls.tiers - {"used"}
    return cls.tiers


def taken_tier(config: PlantConfig, cls: type[Plant]) -> PlantTier:
    """The tier that counts as `config` taken: its own, else used where it can reach it."""
    tiers = reachable(config, cls)
    if config.taken is not None:
        if config.taken not in tiers:
            without = " without a canary" if config.taken in cls.tiers else ""
            raise PlantError(
                f"plant {config.name}: kind {config.kind} has no {config.taken} tier{without}"
            )
        return config.taken
    return "used" if "used" in tiers else "touched"
