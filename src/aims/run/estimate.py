"""What runs of a scenario cost, before they start and while they run.

Three figures each for the agents and the monitor, at list prices: what the caps allow, the
typical cost (the median of matching past runs) and the high one (their 90th percentile, or
twice the median under 10 runs). Past runs match on scenario, variant, models, effort and agent
count; failing that on the models and effort alone, from any scenario, scaled by the step
budget; failing that only the caps speak. Past token counts are priced at today's list prices;
where a provider did not say how much input was cached, the model's own cached share from its
other calls stands in, else 90%.

The variant of a past run comes from its truth file, which stays on the operator's machine; a
run without one counts as the base scenario.
"""

import asyncio
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from aims.config import CONFIRM_ABOVE_USD, Settings
from aims.events import LocalEventStore, RunInfo
from aims.events.store import TRUTH
from aims.models import Profile, ProfileError
from aims.models.profiles import Price
from aims.monitor.config import Preset
from aims.monitor.setup import ResolvedMonitor
from aims.run.job import monitor_for
from aims.run.runner import agent_profile
from aims.scenario import AgentConfig, Bundle, Scenario

Tier = Literal["scenario", "models", "cap"]
MonitorKey = tuple[str, tuple[tuple[str, str | None], ...]]
MIN_RUNS = 10
CACHED_SHARE = 0.9
PROJECT_AFTER = 3
STATUSES = frozenset({"completed", "killed"})
TYPES = ("run.started", "agent.finished", "model.request", "model.response", "monitor.degraded")
PAGE = 1000
# The shape of a run's cost row in the index; a row of another shape is read again.
ROW = 1
MILLION = 1_000_000


class _Out(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class CostFigures(_Out):
    """One run's cost in USD at list prices. `ceiling_usd` is what the caps allow, None when no
    cap applies; `stepped` says it counts one last step past a cost cap, which is checked before
    each step. `typical_usd` is the median of `runs` matching past runs, matched by `tier`: the
    same scenario, or the same models and effort scaled by step budget. `high_usd` is their
    90th percentile, or twice the median when `doubled` (under 10 runs). `capped` of those runs
    hit a cap; `excluded` matched but recorded no cost. `priced` is false when no profile of
    this side has a price, so its cost is not counted."""

    typical_usd: float | None = None
    high_usd: float | None = None
    ceiling_usd: float | None = None
    stepped: bool = False
    tier: Tier = "cap"
    runs: int = 0
    capped: int = 0
    excluded: int = 0
    doubled: bool = False
    priced: bool = True


class VariantCost(_Out):
    """One run of one variant. `monitor` is None when the monitor is off; `steps` is the median
    number of steps each agent took in past runs of the same scenario."""

    variant: str | None = None
    agents: CostFigures
    monitor: CostFigures | None = None
    monitor_preset: Preset | None = None
    steps: dict[str, float] = Field(default_factory=dict)


class CostTotal(_Out):
    """Every run together, agents and monitor: the typical figure, the high one (each run at
    its high figure) and what the caps allow. None when a part of it is unknown."""

    typical_usd: float | None = None
    high_usd: float | None = None
    ceiling_usd: float | None = None


class CachedRate(_Out):
    """The input price of `model` per million tokens with `cached_share` of it cached, the
    share its past calls reported, or 90% when `assumed`."""

    model: str
    cached_share: float
    assumed: bool
    input_usd: float


class ProviderCredit(_Out):
    """What `provider` was paid in the runs on record, and with `[credits]` set for it, what is
    left now and after the estimate's typical and high figures."""

    provider: str
    spent_usd: float
    credit_usd: float | None = None
    left_usd: float | None = None
    left_after_typical_usd: float | None = None
    left_after_high_usd: float | None = None


class Estimate(_Out):
    """The cost of `samples` runs of each variant in `parts`. `confirm` says `aims run` asks
    before it starts: the high figure, or without one the ceiling, is above
    `confirm_above_usd`, or nothing bounds the cost. `lines` is the estimate in words."""

    scenario: str
    samples: int
    parts: list[VariantCost]
    total: CostTotal
    rates: list[CachedRate] = Field(default_factory=list)
    credits: list[ProviderCredit] = Field(default_factory=list)
    unpriced: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confirm_above_usd: float = CONFIRM_ABOVE_USD
    confirm: bool = False
    lines: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class Tokens:
    """The calls to one model in one run. `reported` is the input of the calls that said how
    much of it was cached and `cached` that cached part; `cost` is what the log recorded."""

    provider: str = ""
    input: int = 0
    reported: int = 0
    cached: int = 0
    output: int = 0
    cost: float = 0.0

    def add(self, usage: Mapping[str, Any], cost: float) -> None:
        tokens = _int(usage.get("input_tokens"))
        self.input += tokens
        if usage.get("cached_tokens") is not None:
            self.reported += tokens
            self.cached += _int(usage.get("cached_tokens"))
        self.output += _int(usage.get("output_tokens"))
        self.cost += cost

    def priced(self, price: Price | None, share: float) -> float:
        """The cost at `price`, with `share` of the unreported input cached; the recorded cost
        without a price."""
        if price is None:
            return self.cost
        cached = self.cached + (self.input - self.reported) * share
        uncached = self.input - cached
        return (uncached * price.input + cached * price.cached + self.output * price.output) / (
            MILLION
        )


@dataclass(slots=True)
class Past:
    """A sealed scenario run, as the estimate matches it. `models` holds each agent's model and
    effort, `budget` the step budget of the agents that call a model, `agents` and `watch` the
    agents' and the monitor's calls by model, `steps` the steps each agent took."""

    run_id: str
    scenario: str
    variant: str | None
    models: tuple[tuple[str, str | None], ...]
    budget: int | None
    monitor: MonitorKey | None
    agents: dict[str, Tokens] = field(default_factory=dict)
    watch: dict[str, Tokens] = field(default_factory=dict)
    steps: dict[str, int] = field(default_factory=dict)
    agents_capped: bool = False
    monitor_capped: bool = False


@dataclass(slots=True)
class History:
    """The runs on record: the scenario runs an estimate can match, what each provider was
    paid in every run, and each model's cached input share over the calls that reported it."""

    runs: list[Past] = field(default_factory=list)
    spend: dict[str, float] = field(default_factory=dict)
    shares: dict[str, float] = field(default_factory=dict)

    def share(self, model: str) -> float:
        return self.shares.get(model, CACHED_SHARE)


@dataclass(frozen=True, slots=True)
class Plan:
    """One run to estimate: the scenario with its variant applied, each agent's profile by
    agent id and the resolved monitor, None when it is off."""

    scenario: Scenario
    profiles: Mapping[str, Profile]
    monitor: ResolvedMonitor | None = None
    variant: str | None = None


@dataclass(slots=True)
class _Run:
    """One run as the history reads it: the run an estimate can match, if it is one, what it
    paid each provider, and each model's cached and reported input."""

    past: Past | None = None
    spend: dict[str, float] = field(default_factory=dict)
    cached: dict[str, list[int]] = field(default_factory=dict)

    def dump(self) -> str:
        past = None if self.past is None else asdict(self.past)
        return json.dumps({"v": ROW, "past": past, "spend": self.spend, "cached": self.cached})

    @classmethod
    def load(cls, text: str) -> _Run | None:
        try:
            value = json.loads(text)
            if value.get("v") != ROW:
                return None
            p = value["past"]
            past = None
            if p is not None:
                monitor = p["monitor"]
                past = Past(
                    run_id=p["run_id"],
                    scenario=p["scenario"],
                    variant=p["variant"],
                    models=tuple((m, e) for m, e in p["models"]),
                    budget=p["budget"],
                    monitor=None
                    if monitor is None
                    else (monitor[0], tuple((m, e) for m, e in monitor[1])),
                    agents={k: Tokens(**t) for k, t in p["agents"].items()},
                    watch={k: Tokens(**t) for k, t in p["watch"].items()},
                    steps=dict(p["steps"]),
                    agents_capped=p["agents_capped"],
                    monitor_capped=p["monitor_capped"],
                )
            return cls(past, dict(value["spend"]), {k: list(v) for k, v in value["cached"].items()})
        except KeyError, TypeError, ValueError, AttributeError:
            return None


async def history(store: LocalEventStore) -> History:
    """Every run in `store` for the estimate and the spend. A sealed run is read once; its
    cost row in the index serves it after that, until the index is rebuilt or its truth file
    arrives."""
    out = History()
    spend: dict[str, float] = defaultdict(float)
    cached: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    rows = await store.costs()
    fresh: list[tuple[str, str, str]] = []
    before = None
    while page := await store.runs(limit=PAGE, before=before):
        before = page[-1].run_id
        for info in page:
            kept = rows.get(info.run_id)
            run = _Run.load(kept[1]) if kept is not None and kept[0] == info.head else None
            if run is None:
                run = await _read(store, info)
                if info.status != "running":
                    fresh.append((info.run_id, info.head, run.dump()))
            if run.past is not None:
                out.runs.append(run.past)
            for where, cost in run.spend.items():
                spend[where] += cost
            for model, (part, total) in run.cached.items():
                cached[model][0] += part
                cached[model][1] += total
    await store.put_costs(fresh)
    out.spend = dict(spend)
    out.shares = {m: c / total for m, (c, total) in cached.items() if total > 0}
    return out


async def load_history(runs_dir: Path) -> History:
    """The history of the runs under `runs_dir`; empty, and nothing made, when it is missing."""
    if not await asyncio.to_thread(runs_dir.is_dir):
        return History()
    async with await LocalEventStore.open(runs_dir) as store:
        return await history(store)


async def _read(store: LocalEventStore, info: RunInfo) -> _Run:
    out = _Run()
    spend: dict[str, float] = defaultdict(float)
    cached: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    calls: dict[str, tuple[str, bool, str]] = {}
    sides: tuple[dict[str, Tokens], dict[str, Tokens]] = ({}, {})
    started: dict[str, Any] | None = None
    steps: dict[str, int] = {}
    capped = watch_capped = False
    cursor: int | None = None
    while page := await store.events(info.run_id, after=cursor, limit=PAGE, types=TYPES):
        cursor = page[-1].seq
        for e in page:
            d = e.data
            match e.type:
                case "run.started":
                    started = d
                case "model.request":
                    where = provider(_str(d.get("url")), _str(d.get("profile")))
                    calls[_str(d.get("call_id"))] = (
                        _str(d.get("model")),
                        d.get("role") is not None,
                        where,
                    )
                case "model.response":
                    call = calls.get(_str(d.get("call_id")))
                    if call is None or d.get("source") == "replay":
                        continue
                    model, watching, where = call
                    cost = d.get("cost_usd")
                    if isinstance(cost, int | float):
                        spend[where] += float(cost)
                    usage = _dict(d.get("usage"))
                    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
                        continue
                    if usage.get("cached_tokens") is not None:
                        cached[model][0] += _int(usage.get("cached_tokens"))
                        cached[model][1] += _int(usage.get("input_tokens"))
                    tokens = sides[watching].setdefault(model, Tokens(provider=where))
                    tokens.add(usage, _num(cost))
                case "agent.finished":
                    steps[e.agent_id or ""] = _int(d.get("steps"))
                    capped = capped or _str(d.get("reason")).startswith("budget_")
                case "monitor.degraded":
                    watch_capped = watch_capped or d.get("part") == "budget"
    out.spend, out.cached = dict(spend), dict(cached)
    if info.kind != "scenario" or info.status not in STATUSES or started is None:
        return out
    config = _dict(started.get("config"))
    if config.get("fork") is not None or started.get("cut") is not None:
        return out
    agents = _dict(config.get("agents"))
    if not agents:
        return out
    truth = _truth(store.root / info.run_id)
    variant = truth.get("variant")
    scripted = set(_dict(truth.get("scripts")))
    resolved = _dict(_dict(config.get("scenario")).get("resolved"))
    out.past = Past(
        run_id=info.run_id,
        scenario=info.scenario,
        variant=variant if isinstance(variant, str) else None,
        models=tuple(sorted(_model(_dict(_dict(a).get("profile"))) for a in agents.values())),
        budget=_step_budget(resolved, scripted),
        monitor=_snapshot_key(config.get("monitor")),
        agents=sides[0],
        watch=sides[1],
        steps=steps,
        agents_capped=capped,
        monitor_capped=watch_capped,
    )
    return out


def _truth(run_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((run_dir / TRUTH).read_text())
    except OSError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def plan(
    settings: Settings,
    bundle: Bundle,
    *,
    profile: str | None = None,
    preset: Preset | None = None,
) -> Plan:
    """The plan of one run of `bundle` with these settings, the monitor resolved as `aims run`
    resolves it. Raises ProfileError, MonitorConfigError or TextError."""
    scenario = bundle.scenario
    profiles = {a.id: agent_profile(settings.models, a, profile) for a in scenario.agents}
    monitor = monitor_for(
        settings.models,
        settings.prompts,
        scenario,
        list(profiles.values()),
        layers=settings.monitor,
        preset=preset,
    )
    return Plan(scenario, profiles, monitor, bundle.variant)


def estimate_runs(
    found: History,
    settings: Settings,
    bundles: Sequence[Bundle],
    *,
    profile: str | None = None,
    preset: Preset | None = None,
    samples: int = 1,
) -> Estimate:
    """The estimate of `samples` runs of each bundle, one bundle per variant: what `aims run`,
    `aims estimate`, `aims scenario check` and the UI's run launcher show."""
    plans = [plan(settings, b, profile=profile, preset=preset) for b in bundles]
    return estimate(
        found,
        plans,
        samples=samples,
        credits=settings.credits,
        confirm_above_usd=settings.confirm_above_usd,
    )


def estimate(
    found: History,
    plans: Sequence[Plan],
    *,
    samples: int = 1,
    credits: Mapping[str, float] | None = None,
    confirm_above_usd: float = CONFIRM_ABOVE_USD,
) -> Estimate:
    """The estimate of `samples` runs of each plan from the runs in `found`."""
    if not plans:
        raise ValueError("nothing to estimate")
    parts: list[VariantCost] = []
    typical: dict[str, float] = defaultdict(float)
    high: dict[str, float] = defaultdict(float)
    sums: dict[str, float | None] = {"typical": 0.0, "high": 0.0, "ceiling": 0.0}
    unpriced: set[str] = set()
    warnings: list[str] = []
    rates: dict[str, CachedRate] = {}
    priced = False
    for p in plans:
        part, shares = _part(found, p)
        parts.append(part)
        sides = [(part.agents, shares[0])]
        if part.monitor is not None:
            sides.append((part.monitor, shares[1]))
        for figures, by_provider in sides:
            if not figures.priced:
                continue
            for key, value in (
                ("typical", figures.typical_usd),
                ("high", figures.high_usd),
                ("ceiling", figures.ceiling_usd),
            ):
                now = sums[key]
                sums[key] = None if now is None or value is None else now + value * samples
            for name, share in by_provider.items():
                if figures.typical_usd is not None:
                    typical[name] += figures.typical_usd * share * samples
                if figures.high_usd is not None:
                    high[name] += figures.high_usd * share * samples
        watched = _watch_profiles(p.monitor)
        for prof in [*_model_agents(p), *watched]:
            if prof.price is None:
                unpriced.add(prof.name)
                continue
            priced = True
            share = found.share(prof.model)
            blended = share * prof.price.cached + (1 - share) * prof.price.input
            rates[prof.model] = CachedRate(
                model=prof.model,
                cached_share=share,
                assumed=prof.model not in found.shares,
                input_usd=blended,
            )
        warnings += [w for w in _uncapped(p) if w not in warnings]
    total = CostTotal(
        typical_usd=sums["typical"], high_usd=sums["high"], ceiling_usd=sums["ceiling"]
    )
    figure = total.high_usd if total.high_usd is not None else total.ceiling_usd
    providers = sorted(set(typical) | set(high) | _plan_providers(plans))
    out = Estimate(
        scenario=plans[0].scenario.name,
        samples=samples,
        parts=parts,
        total=total,
        rates=[rates[m] for m in sorted(rates)],
        credits=[
            _credit(
                name,
                found.spend.get(name, 0.0),
                (credits or {}).get(name),
                typical.get(name, 0.0) if total.typical_usd is not None else None,
                high.get(name, 0.0) if total.high_usd is not None else None,
            )
            for name in providers
        ],
        unpriced=sorted(unpriced),
        warnings=warnings,
        confirm_above_usd=confirm_above_usd,
        confirm=priced and (figure is None or figure > confirm_above_usd),
    )
    out.lines = describe(out)
    return out


def spend(found: History, credits: Mapping[str, float] | None = None) -> list[ProviderCredit]:
    """What each provider was paid in the runs on record, against its `[credits]`."""
    credits = credits or {}
    names = sorted(set(found.spend) | set(credits))
    return [_credit(n, found.spend.get(n, 0.0), credits.get(n), None, None) for n in names]


def _credit(
    name: str, spent: float, credit: float | None, typical: float | None, high: float | None
) -> ProviderCredit:
    left = None if credit is None else credit - spent
    return ProviderCredit(
        provider=name,
        spent_usd=spent,
        credit_usd=credit,
        left_usd=left,
        left_after_typical_usd=None if left is None or typical is None else left - typical,
        left_after_high_usd=None if left is None or high is None else left - high,
    )


def provider(url: str | None, profile: str = "") -> str:
    """Who a model call goes to: the name under the top domain of its URL, such as meta for
    api.meta.ai, else the first word of the profile's name."""
    host = urlsplit(url).hostname if url else None
    if host:
        labels = host.lower().split(".")
        if len(labels) >= 2 and not host.replace(".", "").isdigit():
            return labels[-2]
        return host.lower()
    return profile.split("-", 1)[0].lower() or "unknown"


def profile_provider(p: Profile) -> str:
    try:
        url = p.url()
    except ProfileError:
        url = None
    return provider(url, p.name)


def _plan_providers(plans: Iterable[Plan]) -> set[str]:
    out: set[str] = set()
    for p in plans:
        for prof in [*_model_agents(p), *_watch_profiles(p.monitor)]:
            out.add(profile_provider(prof))
    return out


def _part(found: History, p: Plan) -> tuple[VariantCost, tuple[dict[str, float], dict[str, float]]]:
    s = p.scenario
    models = tuple(sorted((prof.model, prof.effort) for prof in p.profiles.values()))
    budget = _step_budget(s.model_dump(mode="json"), {a.id for a in s.agents if a.scripted})
    key = _monitor_key(p.monitor)
    agent_profiles = _model_agents(p)
    prices = {prof.model: prof.price for prof in agent_profiles}
    names = {prof.model: profile_provider(prof) for prof in agent_profiles}

    def first(past: Past) -> bool:
        return past.scenario == s.name and past.variant == p.variant and past.models == models

    def second(past: Past) -> bool:
        return set(past.models) == set(models)

    tiers: list[tuple[Tier, Callable[[Past], bool]]] = [("scenario", first), ("models", second)]
    ceiling, stepped = _ceiling(p)
    if agent_profiles and all(q.price is None for q in agent_profiles):
        agents, agent_shares = CostFigures(priced=False), {}
    else:
        agents, agent_shares = _side(
            found, tiers, budget, prices, names, "agents", ceiling, stepped
        )
    steps: dict[str, float] = {}
    same = [past for past in found.runs if first(past)]
    for agent in s.agents:
        taken = [past.steps[agent.id] for past in same if agent.id in past.steps]
        if taken:
            steps[agent.id] = float(statistics.median(taken))
    monitor = None
    watch_shares: dict[str, float] = {}
    preset = None if p.monitor is None else p.monitor.config.preset
    if p.monitor is not None:
        watched = _watch_profiles(p.monitor)
        if not p.monitor.config.models_on:
            monitor = CostFigures(typical_usd=0.0, high_usd=0.0, ceiling_usd=0.0)
        elif all(q.price is None for q in watched):
            monitor = CostFigures(priced=False)
        else:
            wprices = {prof.model: prof.price for prof in watched}
            wnames = {prof.model: profile_provider(prof) for prof in watched}
            wtiers: list[tuple[Tier, Callable[[Past], bool]]] = [
                (tier, lambda past, test=test: test(past) and past.monitor == key)
                for tier, test in tiers
            ]
            monitor, watch_shares = _side(
                found, wtiers, budget, wprices, wnames, "watch", p.monitor.budget_usd, False
            )
    part = VariantCost(
        variant=p.variant, agents=agents, monitor=monitor, monitor_preset=preset, steps=steps
    )
    return part, (agent_shares, watch_shares)


def _side(
    found: History,
    tiers: Sequence[tuple[Tier, Callable[[Past], bool]]],
    budget: int | None,
    prices: Mapping[str, Price | None],
    names: Mapping[str, str],
    side: Literal["agents", "watch"],
    ceiling: float | None,
    stepped: bool,
) -> tuple[CostFigures, dict[str, float]]:
    for tier, test in tiers:
        matched = [past for past in found.runs if test(past)]
        costs: list[float] = []
        by_provider: dict[str, float] = defaultdict(float)
        capped = 0
        for past in matched:
            scale = 1.0 if tier == "scenario" else _scale(budget, past.budget)
            calls = past.agents if side == "agents" else past.watch
            each = {m: t.priced(prices.get(m), found.share(m)) * scale for m, t in calls.items()}
            cost = sum(each.values())
            if cost <= 0:
                continue
            costs.append(cost)
            for m, value in each.items():
                by_provider[names.get(m) or calls[m].provider] += value
            capped += past.agents_capped if side == "agents" else past.monitor_capped
        if not costs:
            continue
        typical, high, doubled = spread(costs)
        if ceiling is not None:
            typical, high = min(typical, ceiling), min(high, ceiling)
        whole = sum(by_provider.values())
        figures = CostFigures(
            typical_usd=typical,
            high_usd=high,
            ceiling_usd=ceiling,
            stepped=stepped,
            tier=tier,
            runs=len(costs),
            capped=capped,
            excluded=len(matched) - len(costs),
            doubled=doubled,
        )
        return figures, {n: v / whole for n, v in by_provider.items()}
    return CostFigures(ceiling_usd=ceiling, stepped=stepped), {}


def spread(costs: Sequence[float]) -> tuple[float, float, bool]:
    """The median and the 90th percentile of `costs`, taken on their logs; under `MIN_RUNS`
    costs the high figure is twice the median."""
    logs = [math.log(c) for c in costs]
    median = math.exp(statistics.median(logs))
    if len(costs) < MIN_RUNS:
        return median, median * 2, True
    return median, math.exp(statistics.quantiles(logs, n=10, method="inclusive")[8]), False


def _scale(budget: int | None, past: int | None) -> float:
    return budget / past if budget is not None and past else 1.0


def _step_budget(resolved: Mapping[str, Any], skip: Iterable[str] = ()) -> int | None:
    """The steps the agents that call a model may take together: the sum of their step caps,
    lowered by the run's own cap. None when an agent has no cap and the run has none."""
    skipped = set(skip)
    agents = resolved.get("agents")
    total: int | None = None
    if isinstance(agents, list) and agents:
        total = 0
        for a in agents:
            a = _dict(a)
            if a.get("id") in skipped:
                continue
            steps = _dict(a.get("budget")).get("steps")
            if not isinstance(steps, int):
                total = None
                break
            total += steps
    run = _dict(resolved.get("limits")).get("steps")
    if isinstance(run, int):
        return run if total is None else min(total, run)
    return total


def step_usd(p: Profile) -> float | None:
    """The most one call of `p` can cost at list price: a full context of uncached input and
    the longest answer. None without a price, a context size or an output cap."""
    if p.price is None or p.context_tokens is None or p.max_output_tokens is None:
        return None
    return (p.context_tokens * p.price.input + p.max_output_tokens * p.price.output) / MILLION


def _agent_ceiling(agent: AgentConfig, p: Profile) -> tuple[float | None, bool]:
    """What the agent's caps let it spend, and whether that counts one last step past its cost
    cap. The step cap bounds the cost only when a call's cost is bounded."""
    step = step_usd(p)
    b = agent.budget
    caps: list[tuple[float, bool]] = []
    if b.cost_usd is not None:
        caps.append((b.cost_usd + (step or 0.0), step is not None))
    if b.steps is not None and step is not None:
        summary = agent.compaction is not None and agent.compaction.strategy == "summary"
        caps.append((b.steps * (2 if summary else 1) * step, False))
    if not caps:
        return None, False
    value, stepped = min(caps)
    return value, stepped


def _ceiling(p: Plan) -> tuple[float | None, bool]:
    """What the caps let the priced agents spend in one run: the sum of their own ceilings,
    lowered by the run's cost and step caps. None when an agent has no cap and the run none."""
    priced = [a for a in p.scenario.agents if not a.scripted and p.profiles[a.id].price is not None]
    if not priced:
        return 0.0, False
    options: list[tuple[float, bool]] = []
    each = [_agent_ceiling(a, p.profiles[a.id]) for a in priced]
    if all(value is not None for value, _ in each):
        options.append((sum(v for v, _ in each if v is not None), any(s for _, s in each)))
    steps = [step_usd(p.profiles[a.id]) for a in priced]
    known = [s for s in steps if s is not None]
    limits = p.scenario.limits
    if limits.cost_usd is not None:
        options.append((limits.cost_usd + sum(known), bool(known)))
    if limits.steps is not None and len(known) == len(priced):
        options.append(((limits.steps + len(priced) - 1) * max(known), False))
    if not options:
        return None, False
    return min(options)


def _uncapped(p: Plan) -> list[str]:
    if _ceiling(p)[0] is not None:
        return []
    return [
        f"no cap bounds the cost of agent {a.id}; set its budget.cost_usd or the scenario's"
        " limits.cost_usd"
        for a in p.scenario.agents
        if not a.scripted
        and p.profiles[a.id].price is not None
        and _agent_ceiling(a, p.profiles[a.id])[0] is None
    ]


def _model_agents(p: Plan) -> list[Profile]:
    return [p.profiles[a.id] for a in p.scenario.agents if not a.scripted]


def _watch_profiles(m: ResolvedMonitor | None) -> list[Profile]:
    if m is None or not m.config.models_on:
        return []
    return [q for q in (*m.sweeps.values(), m.review, m.judge) if q is not None]


def _monitor_key(m: ResolvedMonitor | None) -> MonitorKey | None:
    if m is None:
        return None
    found = [
        q for q in (*m.sweeps.values(), m.review, m.judge, m.extract, m.channels) if q is not None
    ]
    return m.config.preset, _models((q.model, q.effort) for q in found)


def _snapshot_key(value: object) -> MonitorKey | None:
    m = _dict(value)
    if not m:
        return None
    preset = _str(_dict(m.get("config")).get("preset"))
    found = [*_dict(m.get("sweeps")).values(), m.get("review"), m.get("judge")]
    found += [m.get("extract"), m.get("channels")]
    return preset, _models(_model(q) for q in found if isinstance(q, dict))


def _models(found: Iterable[tuple[str, str | None]]) -> tuple[tuple[str, str | None], ...]:
    return tuple(sorted(found, key=lambda m: (m[0], m[1] or "")))


def _model(snapshot: Mapping[str, Any]) -> tuple[str, str | None]:
    effort = snapshot.get("effort")
    return _str(snapshot.get("model")), effort if isinstance(effort, str) else None


# The estimate in words


def usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.1 or value == 0 else f"${value:.4f}"


def describe(e: Estimate) -> list[str]:
    """The estimate as `aims run`, `aims estimate` and the UI show it."""
    count = len(e.parts) * e.samples
    runs = "one run" if count == 1 else f"{count} runs"
    lines = [f"Cost estimate for {e.scenario}, {runs}, at list prices."]
    several = len(e.parts) > 1 or e.parts[0].variant is not None
    for part in e.parts:
        indent = ""
        if several:
            lines.append(f"Variant {part.variant or '(base)'}, per run:")
            indent = "  "
        elif e.samples > 1:
            lines.append("Per run:")
            indent = "  "
        lines.append(indent + _side_line("Agents", part.agents))
        if part.monitor is None:
            lines.append(indent + "Monitor: off.")
        elif part.monitor_preset == "signals":
            lines.append(indent + "Monitor: signals only, no model calls.")
        else:
            lines.append(indent + _side_line("Monitor", part.monitor))
    lines.append(_total_line(e, count))
    if e.rates and any(_history(p) for p in e.parts):
        rates = ", ".join(
            f"{r.model} {usd(r.input_usd)} ({r.cached_share:.0%} cached"
            + (", assumed)" if r.assumed else ")")
            for r in e.rates
        )
        lines.append(f"Blended input per million tokens: {rates}.")
    lines += [_credit_line(c) for c in e.credits]
    lines += [
        f"warning: profile {n} has no price, so its calls are not counted" for n in e.unpriced
    ]
    lines += [f"warning: {w}" for w in e.warnings]
    return lines


def _history(part: VariantCost) -> bool:
    sides = [part.agents, *([part.monitor] if part.monitor is not None else [])]
    return any(s.runs for s in sides)


def _side_line(name: str, f: CostFigures) -> str:
    if not f.priced:
        return f"{name}: no list price, so not counted."
    words = [f"{name}:"]
    if f.typical_usd is not None and f.high_usd is not None:
        runs = "1 run" if f.runs == 1 else f"{f.runs} runs"
        how = "" if f.tier == "scenario" else " of the same models, scaled to this step budget"
        words.append(f"typically {usd(f.typical_usd)} (median of {runs}{how}).")
        if f.doubled:
            words.append(
                f"1 in 10 runs may exceed {usd(f.high_usd)} (twice the median, under"
                f" {MIN_RUNS} runs)."
            )
        else:
            words.append(f"1 in 10 runs exceeds {usd(f.high_usd)}.")
        if f.capped:
            words.append(f"{f.capped} of them hit a cap.")
    else:
        words.append("no past runs to go by.")
    if f.excluded:
        words.append(f"{f.excluded} past runs without a cost left out.")
    if f.ceiling_usd is None:
        words.append("Cannot exceed: no cap set.")
    else:
        last = " (the caps plus a last step per agent)" if f.stepped else ""
        words.append(f"Cannot exceed {usd(f.ceiling_usd)}{last}.")
    return " ".join(words)


def _total_line(e: Estimate, count: int) -> str:
    t = e.total
    known = t.typical_usd is not None and t.high_usd is not None
    if count == 1:
        words = ["Total:"]
        if known:
            words.append(f"typically {usd(t.typical_usd or 0)}.")
            words.append(f"1 in 10 runs exceeds {usd(t.high_usd or 0)}.")
    else:
        words = [f"Total for {count} runs:"]
        if known:
            typical, high = usd(t.typical_usd or 0), usd(t.high_usd or 0)
            words.append(f"typically {typical}, {high} if every run is high.")
    cap = "exceed: no cap set." if t.ceiling_usd is None else f"exceed {usd(t.ceiling_usd)}."
    words.append(("Cannot " if known else "cannot ") + cap)
    return " ".join(words)


def _credit_line(c: ProviderCredit) -> str:
    name = c.provider.capitalize()
    if c.credit_usd is None:
        return f"{name}: {usd(c.spent_usd)} spent."
    line = f"{name}: {usd(c.spent_usd)} of {usd(c.credit_usd)} spent"
    if c.left_after_typical_usd is not None and c.left_after_high_usd is not None:
        return (
            f"{line}, {usd(c.left_after_typical_usd)} left after the typical cost,"
            f" {usd(c.left_after_high_usd)} after the high one."
        )
    assert c.left_usd is not None
    return f"{line}, {usd(c.left_usd)} left."


def spend_lines(credits: Sequence[ProviderCredit]) -> list[str]:
    """`aims spend` in words: one line per provider."""
    if not credits:
        return ["No model calls on record."]
    return [_credit_line(c) for c in credits]


# Before the run starts


def needs_confirm(e: Estimate) -> str | None:
    """Why the runs need a person's yes before they start, None when they do not."""
    if not e.confirm:
        return None
    t = e.total
    limit = usd(e.confirm_above_usd)
    if t.high_usd is not None:
        return f"the high estimate {usd(t.high_usd)} is above confirm_above_usd {limit}"
    if t.ceiling_usd is not None:
        return f"the caps allow {usd(t.ceiling_usd)}, above confirm_above_usd {limit}"
    return "no cap bounds the cost and no past runs give an estimate"


def guard(e: Estimate, *, yes: bool, tty: bool, ask: Callable[[str], bool]) -> str | None:
    """None when the run may start: the estimate is under the threshold, `yes` was given, or
    the person at the terminal agreed through `ask`. Otherwise why it may not."""
    why = None if yes else needs_confirm(e)
    if why is None:
        return None
    if not tty:
        return f"{why}; pass --yes to start anyway"
    return None if ask(f"{why[0].upper()}{why[1:]}. Start anyway?") else "not started"


# While it runs


@dataclass(frozen=True, slots=True)
class Progress:
    """One agent of a running run: `cost` USD over `steps` steps, its caps, the steps it took
    in past runs of the scenario, and whether it finished."""

    cost: float
    steps: int
    steps_cap: int | None = None
    cost_cap: float | None = None
    typical_steps: float | None = None
    done: bool = False


def project(a: Progress) -> tuple[float, float]:
    """What `a` is on track to spend and the steps that assumes: its cost per step so far over
    the steps it is expected to take (the past runs' median, else its step cap, never fewer
    than it took), bounded by its cost cap."""
    if a.done or a.steps <= 0:
        return a.cost, float(a.steps)
    target = a.typical_steps if a.typical_steps is not None else a.steps_cap
    if target is None:
        return a.cost, float(a.steps)
    if a.steps_cap is not None:
        target = min(target, a.steps_cap)
    target = max(target, float(a.steps))
    value = a.cost / a.steps * target
    if a.cost_cap is not None:
        value = min(value, max(a.cost_cap, a.cost))
    return value, target


def project_run(
    agents: Sequence[Progress],
    *,
    cost_cap: float | None = None,
    monitor_cost: float = 0.0,
    monitor_cap: float | None = None,
) -> tuple[float, float] | None:
    """What the agents and the monitor of a run are on track to spend, the agents bounded by
    the run's cost cap and the monitor, scaled by the agents' progress, by its budget. None
    until an agent still running has taken `PROJECT_AFTER` steps."""
    if not any(not a.done and a.steps >= PROJECT_AFTER for a in agents):
        return None
    projected = [project(a) for a in agents]
    total = sum(v for v, _ in projected)
    spent = sum(a.cost for a in agents)
    if cost_cap is not None:
        total = min(total, max(cost_cap, spent))
    taken = sum(a.steps for a in agents)
    planned = sum(t for _, t in projected)
    monitor = monitor_cost * planned / taken if taken else monitor_cost
    if monitor_cap is not None:
        monitor = min(monitor, max(monitor_cap, monitor_cost))
    return total, monitor


class Projection(_Out):
    """What a run has cost so far, agents and monitor, and once an agent still running has
    taken `PROJECT_AFTER` steps what it is on track to cost, bounded by its caps: each agent's
    cost per step over the steps past runs of the scenario took, else its step cap, and the
    monitor scaled by the agents' progress. The `*_usd` projections are None before that."""

    spent_usd: float
    agents_spent_usd: float
    monitor_spent_usd: float
    on_track_usd: float | None = None
    agents_usd: float | None = None
    monitor_usd: float | None = None


PROJECTION_TYPES = (
    "run.started",
    "agent.started",
    "agent.step",
    "agent.finished",
    "model.request",
    "model.response",
)


async def projection(store: LocalEventStore, run_id: str, found: History) -> Projection | None:
    """The projection of run `run_id` from its log and the past runs in `found`; None when
    the store has no such run."""
    if await store.run(run_id) is None:
        return None
    agents: dict[str, dict[str, Any]] = {}
    watching: set[str] = set()
    monitor = 0.0
    scenario = ""
    models: tuple[tuple[str, str | None], ...] = ()
    cost_cap = monitor_cap = None
    cursor: int | None = None
    while page := await store.events(run_id, after=cursor, limit=PAGE, types=PROJECTION_TYPES):
        cursor = page[-1].seq
        for e in page:
            d = e.data
            a = agents.setdefault(e.agent_id, {}) if e.agent_id is not None else {}
            match e.type:
                case "run.started":
                    scenario = _str(d.get("scenario"))
                    config = _dict(d.get("config"))
                    limits = _dict(
                        _dict(_dict(config.get("scenario")).get("resolved")).get("limits")
                    )
                    cost_cap = _cap(limits.get("cost_usd"))
                    monitor_cap = _cap(_dict(config.get("monitor")).get("budget_usd"))
                    profiles = _dict(config.get("agents")).values()
                    models = tuple(sorted(_model(_dict(_dict(q).get("profile"))) for q in profiles))
                case "agent.started":
                    budget = _dict(d.get("budget"))
                    steps = _cap(budget.get("steps"))
                    a["steps_cap"] = None if steps is None else int(steps)
                    a["cost_cap"] = _cap(budget.get("cost_usd"))
                case "agent.step":
                    a["steps"] = _int(d.get("n"))
                    a["cost"] = _num(d.get("total_cost_usd"))
                case "agent.finished":
                    a["done"] = True
                case "model.request" if d.get("role") is not None:
                    watching.add(_str(d.get("call_id")))
                case "model.response" if d.get("call_id") in watching:
                    monitor += _num(d.get("cost_usd"))
    typical = typical_steps(found, scenario, models)
    progress = [
        Progress(
            cost=a.get("cost", 0.0),
            steps=a.get("steps", 0),
            steps_cap=a.get("steps_cap"),
            cost_cap=a.get("cost_cap"),
            typical_steps=typical.get(agent_id),
            done=bool(a.get("done")),
        )
        for agent_id, a in agents.items()
        if agent_id
    ]
    spent = sum(p.cost for p in progress)
    out = Projection(
        spent_usd=round(spent + monitor, 10),
        agents_spent_usd=round(spent, 10),
        monitor_spent_usd=round(monitor, 10),
    )
    ahead = project_run(progress, cost_cap=cost_cap, monitor_cost=monitor, monitor_cap=monitor_cap)
    if ahead is not None:
        out.agents_usd, out.monitor_usd = round(ahead[0], 10), round(ahead[1], 10)
        out.on_track_usd = round(ahead[0] + ahead[1], 10)
    return out


def typical_steps(
    found: History, scenario: str, models: tuple[tuple[str, str | None], ...]
) -> dict[str, float]:
    """The median steps each agent took in the past runs of `scenario` on the same models."""
    taken: dict[str, list[int]] = defaultdict(list)
    for past in found.runs:
        if past.scenario == scenario and past.models == models:
            for agent_id, n in past.steps.items():
                taken[agent_id].append(n)
    return {a: float(statistics.median(ns)) for a, ns in taken.items() if ns}


def _cap(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _int(value: object) -> int:
    return int(_num(value))
