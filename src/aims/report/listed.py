"""Which costs of a run are list prices, with the row and the copy of the map that set them.

A call is at a list price when its `model.response` says so: the provider's own cost wins per
call, and a replay carries none. The run's `run.started` only names the row and the date, from
the profiles it recorded. A log may come from another version or another install, so a value
that does not read is skipped, never raised.
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aims.events import LocalEventStore

# Where a run's `run.started` config keeps profiles: an agent's profile and its schedule, the
# monitor's models or a score run's classifier.
_MONITOR_PARTS = ("review", "judge", "extract", "channels")


class RecordedPrice(BaseModel):
    """A list price a run recorded on a profile: row `key` of the map, from a copy last
    confirmed current at `checked`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    checked: AwareDatetime


class ListedCost(BaseModel):
    """The live calls of profile `profile` that cost a list price: `calls` of them, `cost_usd`
    in all. `prices` are the list prices the run recorded on profiles of that name, empty when
    it recorded none."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )

    profile: str
    calls: int
    cost_usd: float
    prices: list[RecordedPrice] = Field(default_factory=list)


def recorded_prices(config: object) -> dict[str, list[RecordedPrice]]:
    """The list prices a `run.started` config recorded, by profile name, each once."""
    out: dict[str, list[RecordedPrice]] = {}
    for snap in _profiles(config):
        name, key, mark = snap.get("name"), snap.get("price_key"), snap.get("listed")
        if not isinstance(name, str) or not isinstance(key, str) or not isinstance(mark, dict):
            continue
        checked = _when(mark.get("checked"))
        if checked is None:
            continue
        found = RecordedPrice(key=key, checked=checked)
        prices = out.setdefault(name, [])
        if found not in prices:
            prices.append(found)
    return out


async def listed_costs(store: LocalEventStore, run_id: str, config: object) -> list[ListedCost]:
    """The live calls of run `run_id` at a list price, per profile, with the prices its
    `run.started` `config` recorded for them."""
    calls = await store.listed_calls(run_id)
    if not calls:
        return []
    prices = recorded_prices(config)
    return [
        ListedCost(
            profile=c.profile,
            calls=c.calls,
            cost_usd=c.cost_usd,
            prices=prices.get(c.profile, []),
        )
        for c in calls
    ]


def _profiles(config: object) -> list[dict[str, object]]:
    if not isinstance(config, dict):
        return []
    out: list[object] = []
    agents = config.get("agents")
    for agent in agents.values() if isinstance(agents, dict) else []:
        if not isinstance(agent, dict):
            continue
        out.append(agent.get("profile"))
        schedule = agent.get("schedule")
        for step in schedule if isinstance(schedule, list) else []:
            if isinstance(step, list) and len(step) == 2:
                out.append(step[1])
    monitor = config.get("monitor")
    if isinstance(monitor, dict):
        sweeps = monitor.get("sweeps")
        out.extend(sweeps.values() if isinstance(sweeps, dict) else [])
        out.extend(monitor.get(part) for part in _MONITOR_PARTS)
    out.append(config.get("classifier"))
    return [p for p in out if isinstance(p, dict)]


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo is not None else None
