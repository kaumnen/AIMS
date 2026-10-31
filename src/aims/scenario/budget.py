"""A run's budget override: caps laid over a scenario's agent budgets and run limits, as
`aims run --budget` and the API's `budget` field give them. The scenario stays as written;
the run records the override in `run.started` and each agent's caps in `agent.started`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypedDict, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
)

from aims.sandbox.base import Name
from aims.scenario.model import Scenario

Key = Literal["steps", "output_tokens", "cost_usd", "wall_s"]
KEYS: tuple[Key, ...] = get_args(Key)
"""The caps an override may change, on an agent and on the run."""

Value = int | float | None


class BudgetError(ValueError):
    pass


class GivenCaps(TypedDict, total=False):
    """Caps as an override dumps them: only the ones it changes."""

    steps: int | None
    output_tokens: int | None
    cost_usd: float | None
    wall_s: float | None


class Caps(BaseModel):
    """Caps to change. A field set to None lifts that cap; a field left out keeps it. Only the
    fields given are dumped, so a copy read back changes the same caps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: int | None = Field(default=None, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
    cost_usd: float | None = Field(default=None, gt=0)
    wall_s: float | None = Field(default=None, gt=0)

    @model_serializer(mode="wrap")
    def _given_only(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> GivenCaps:
        data = handler(self)
        return cast(GivenCaps, {k: v for k, v in data.items() if k in self.model_fields_set})

    def given(self) -> dict[str, Value]:
        return {k: getattr(self, k) for k in KEYS if k in self.model_fields_set}


class BudgetOverride(BaseModel):
    """`every` changes every agent's caps, `agents` one agent's, over `every`; `run` changes
    the scenario's `limits`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    every: Caps | None = None
    agents: dict[Name, Caps] = Field(default_factory=dict)
    run: Caps | None = None

    @property
    def empty(self) -> bool:
        return not (self.run_caps() or any(c.given() for c in self._all()))

    def _all(self) -> Iterable[Caps]:
        if self.every is not None:
            yield self.every
        yield from self.agents.values()

    def agent_caps(self, agent_id: str) -> dict[str, Value]:
        """The caps this override changes on agent `agent_id`."""
        out = {} if self.every is None else self.every.given()
        own = self.agents.get(agent_id)
        return out | ({} if own is None else own.given())

    def run_caps(self) -> dict[str, Value]:
        return {} if self.run is None else self.run.given()

    def check(self, agent_ids: Iterable[str]) -> None:
        """Raises BudgetError when the override names an agent not in `agent_ids`."""
        known = list(agent_ids)
        unknown = sorted(set(self.agents) - set(known))
        if unknown:
            names = ", ".join(known)
            raise BudgetError(
                f"no agent {', '.join(unknown)} in the scenario; its agents are {names}"
            )

    def dump(self) -> dict[str, Any] | None:
        """The override as `run.started` records it, None when it changes nothing."""
        if self.empty:
            return None
        data = self.model_dump(mode="json")
        return {k: v for k, v in data.items() if v is not None and v != {}}


def read_json(text: str) -> BudgetOverride | None:
    """An override sent as JSON, as the estimate endpoints take it. Raises BudgetError."""
    try:
        found = BudgetOverride.model_validate_json(text)
    except ValidationError as e:
        raise BudgetError(problem_text(e)) from None
    return None if found.empty else found


def load(value: object) -> BudgetOverride | None:
    """The override a run recorded, None when it has none or it does not read."""
    if not isinstance(value, Mapping):
        return None
    try:
        found = BudgetOverride.model_validate(value)
    except ValidationError:
        return None
    return None if found.empty else found


def parse(text: str) -> BudgetOverride:
    """Read `--budget`: comma separated `key=value` pairs. A bare key (`steps=200`) sets every
    agent, `AGENT.key` one agent and `run.key` the run's limits; `none` lifts the cap. Raises
    BudgetError."""
    every: dict[str, str | None] = {}
    agents: dict[str, dict[str, str | None]] = {}
    run: dict[str, str | None] = {}
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        key, sep, raw = part.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key or not raw:
            raise BudgetError(f"{part!r}: write key=value, such as steps=200 or cost_usd=none")
        scope, dot, cap = key.rpartition(".")
        if cap not in KEYS:
            raise BudgetError(f"{key}: no cap {cap!r}; use {', '.join(KEYS)}")
        target = every if not dot else run if scope == "run" else agents.setdefault(scope, {})
        if cap in target:
            raise BudgetError(f"{key} is given twice")
        target[cap] = None if raw.lower() == "none" else raw
    try:
        return BudgetOverride.model_validate(
            {
                "every": every or None,
                "agents": agents,
                "run": run or None,
            }
        )
    except ValidationError as e:
        raise BudgetError(problem_text(e)) from None


def problem_text(error: ValidationError) -> str:
    """A validation error of an override in one line, each cap named as `--budget` names it."""
    out = []
    for err in error.errors(include_url=False):
        loc = [str(x) for x in err["loc"] if x != "[key]"]
        if loc and loc[0] in ("every", "agents"):
            loc = loc[1:]
        out.append(f"{'.'.join(loc)}: {err['msg']}")
    return "; ".join(out)


def over(
    base: BudgetOverride | None, top: BudgetOverride | None, agent_ids: Iterable[str]
) -> BudgetOverride | None:
    """`top` laid over `base` cap by cap, as a fork's override goes over its origin's: per
    agent, so a fork's `every` beats the origin's caps for one agent. None when neither
    changes anything."""
    if base is None or base.empty:
        return None if top is None or top.empty else top
    if top is None or top.empty:
        return base
    agents = {a: base.agent_caps(a) | top.agent_caps(a) for a in agent_ids}
    run = base.run_caps() | top.run_caps()
    return BudgetOverride(
        agents={a: Caps(**c) for a, c in agents.items() if c},
        run=Caps(**run) if run else None,
    )


def apply(scenario: Scenario, override: BudgetOverride | None) -> Scenario:
    """`scenario` with the override's caps in place of its own. Raises BudgetError when the
    override names an agent the scenario lacks."""
    if override is None or override.empty:
        return scenario
    override.check(a.id for a in scenario.agents)
    agents = [
        a.model_copy(update={"budget": a.budget.model_copy(update=caps)})
        if (caps := override.agent_caps(a.id))
        else a
        for a in scenario.agents
    ]
    update: dict[str, Any] = {"agents": agents}
    if run := override.run_caps():
        update["limits"] = scenario.limits.model_copy(update=run)
    return scenario.model_copy(update=update)


def apply_resolved(resolved: Mapping[str, Any], override: BudgetOverride | None) -> dict[str, Any]:
    """A recorded scenario, as `run.started` keeps it resolved, with the override's caps in
    place of its own."""
    out = dict(resolved)
    if override is None or override.empty:
        return out
    if isinstance(resolved.get("agents"), list):
        out["agents"] = [
            a
            | {"budget": _dict(a.get("budget")) | override.agent_caps(str(a.get("id") or "agent"))}
            for a in _dicts(resolved.get("agents"))
        ]
    out["limits"] = _dict(resolved.get("limits")) | override.run_caps()
    return out


Scope = Literal["agent", "run"]


class BudgetChange(BaseModel):
    """One cap a run's override changed: on `agent`, or on the run when `scope` is run.
    `value` and `scenario` are the cap the run had and the scenario's, None for no cap."""

    scope: Scope
    agent: str | None = None
    key: Key
    value: Value
    scenario: Value


def changes(resolved: Mapping[str, Any], override: BudgetOverride | None) -> list[BudgetChange]:
    """The caps `override` changed over the recorded scenario `resolved`, agents in scenario
    order then the run; caps set to the scenario's own value are left out."""
    if override is None or override.empty:
        return []
    out: list[BudgetChange] = []
    for a in _dicts(resolved.get("agents")):
        agent_id = str(a.get("id") or "agent")
        for key, value, was in _changed(override.agent_caps(agent_id), _dict(a.get("budget"))):
            out.append(
                BudgetChange(scope="agent", agent=agent_id, key=key, value=value, scenario=was)
            )
    for key, value, was in _changed(override.run_caps(), _dict(resolved.get("limits"))):
        out.append(BudgetChange(scope="run", key=key, value=value, scenario=was))
    return out


def _changed(
    caps: Mapping[str, Value], recorded: Mapping[str, Any]
) -> list[tuple[Key, Value, Value]]:
    out: list[tuple[Key, Value, Value]] = []
    for key in KEYS:
        if key not in caps:
            continue
        value = caps[key]
        was = recorded.get(key)
        scenario = was if isinstance(was, int | float) else None
        if value != scenario:
            out.append((key, value, scenario))
    return out


NAMES = {"steps": "steps", "output_tokens": "output tokens", "cost_usd": "cost", "wall_s": "wall"}


def cap_text(key: str, value: Value) -> str:
    """`value` of cap `key` in words: 200 steps reads `200`, a cost `$4`, a wall `30m`."""
    if value is None:
        return "no cap"
    if key == "cost_usd":
        return f"${value:g}"
    if key == "wall_s":
        return _duration(float(value))
    return f"{int(value):,}"


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds / 3600:g}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g}m"
    return f"{seconds:g}s"


def describe(found: Iterable[BudgetChange], agents: int = 1) -> str | None:
    """The changes in one line, as `aims run` prints them and the run page shows them:
    `Budget: steps 200 (scenario 80), run cost $10 (scenario no cap)`. The agent is named
    when the scenario has several."""
    parts = []
    for c in found:
        who = "run " if c.scope == "run" else f"{c.agent} " if agents > 1 else ""
        parts.append(
            f"{who}{NAMES[c.key]} {cap_text(c.key, c.value)}"
            f" (scenario {cap_text(c.key, c.scenario)})"
        )
    return f"Budget: {', '.join(parts)}" if parts else None


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(v) for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []
