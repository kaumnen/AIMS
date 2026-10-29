"""The monitor of one run, resolved from the config layers, the scenario and the flags."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue, ValidationError

from aims import __version__
from aims.events import Oversight
from aims.models import ModelsConfig, Profile, ProfileError
from aims.models.profiles import check_weights
from aims.monitor.config import (
    JUDGE_RESERVE_USD,
    MonitorConfig,
    MonitorConfigError,
    Preset,
    budget_usd,
    digest,
    merge,
)
from aims.monitor.texts import MonitorTexts, resolve
from aims.prompts import Prompts
from aims.scenario import Scenario


@dataclass(frozen=True, slots=True)
class ResolvedMonitor:
    """Everything a run's monitor needs, with its profiles and texts resolved. `sweeps` maps a
    sweep name to its profile; `extract` is the cheaper model the sweeps extract with, None
    when they extract with their own; `channels` runs the channel sweep. `identity` hashes what
    decides the monitor's findings."""

    config: MonitorConfig
    texts: MonitorTexts
    oversight: Oversight
    budget_usd: float
    sweeps: dict[str, Profile] = field(default_factory=dict)
    review: Profile | None = None
    judge: Profile | None = None
    signals: dict[str, str] = field(default_factory=dict)
    identity: str = ""
    extract: Profile | None = None
    channels: Profile | None = None

    @property
    def judge_reserve_usd(self) -> float:
        """What sweeps leave unspent for the judge."""
        return min(JUDGE_RESERVE_USD, self.budget_usd) if self.config.judge is not None else 0.0

    def snapshot(self) -> dict[str, JsonValue]:
        """The resolved monitor for `run.started` config; profiles hold env names only."""
        sweeps: dict[str, JsonValue] = {n: p.snapshot() for n, p in self.sweeps.items()}
        return {
            "config": self.config.model_dump(mode="json"),
            "texts": self.texts.snapshot(),
            "oversight": self.oversight,
            "budget_usd": self.budget_usd,
            "sweeps": sweeps,
            "review": None if self.review is None else self.review.snapshot(),
            "judge": None if self.judge is None else self.judge.snapshot(),
            "signals": dict(self.signals),
            "identity": self.identity,
            "extract": None if self.extract is None else self.extract.snapshot(),
            "channels": None if self.channels is None else self.channels.snapshot(),
        }

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> ResolvedMonitor:
        """A monitor from its `snapshot`. Raises MonitorConfigError."""
        try:

            def profile(v: Any) -> Profile | None:
                return None if v is None else Profile.model_validate(v)

            sweeps = {n: Profile.model_validate(p) for n, p in value["sweeps"].items()}
            return cls(
                config=MonitorConfig.model_validate(value["config"]),
                texts=MonitorTexts.restore(value["texts"]),
                oversight=value["oversight"],
                budget_usd=float(value["budget_usd"]),
                sweeps=sweeps,
                review=profile(value["review"]),
                judge=profile(value["judge"]),
                signals=dict(value["signals"]),
                identity=str(value["identity"]),
                extract=profile(value.get("extract")),
                channels=profile(value.get("channels")),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise MonitorConfigError(f"the monitor snapshot does not load: {e}") from None


def identity(
    config: MonitorConfig,
    texts: MonitorTexts,
    profiles: Mapping[str, Profile | None],
    signals: Mapping[str, str],
) -> str:
    """sha256 over the prompts, rubric, schemas (via the AIMS version), models, effort, lanes,
    signal versions, rules and AIMS version."""
    value: dict[str, JsonValue] = {
        "aims": __version__,
        "config": config.model_dump(mode="json"),
        "texts": texts.snapshot(),
        "profiles": {
            k: None if p is None else {"model": p.model, "weights": p.weights, "effort": p.effort}
            for k, p in sorted(profiles.items())
        },
        "signals": dict(sorted(signals.items())),
    }
    return digest(value)


def resolve_config(
    layers: Sequence[Mapping[str, Any]],
    scenario: Scenario,
    preset: Preset | None = None,
) -> MonitorConfig:
    """The config layers, then the scenario's `monitor:`, then the `--monitor` preset."""
    tops: list[Mapping[str, Any]] = list(layers)
    if scenario.monitor is not None:
        tops.append(scenario.monitor.model_dump(mode="json", exclude_unset=True))
    if preset is not None:
        tops.append({"preset": preset})
    try:
        return merge(*tops)
    except ValidationError as e:
        raise MonitorConfigError(str(e)) from None


def resolve_monitor(
    models: ModelsConfig,
    prompts: Prompts,
    scenario: Scenario,
    agents: Sequence[Profile],
    *,
    layers: Sequence[Mapping[str, Any]] = (),
    preset: Preset | None = None,
    signals: Mapping[str, str] | None = None,
) -> ResolvedMonitor | None:
    """The monitor of a run of `scenario` whose agents use `agents`; None when it is off.
    `signals` maps each signal to its version. The extractor is the first of `config.extract`
    whose weights no agent runs. Raises MonitorConfigError, ProfileError or TextError,
    including when a monitor model is an agent's model and the config does not allow it."""
    config = resolve_config(layers, scenario, preset)
    if config.preset == "off":
        return None
    texts = resolve(prompts)
    sweeps: dict[str, Profile] = {}
    review = judge = extract = channels = None
    if config.models_on:
        for s in config.sweeps:
            base = models.get(s.profile) if s.profile else models.role("sweep")
            sweeps[s.name] = base.model_copy(
                update={"effort": s.effort, "max_output_tokens": s.max_output_tokens}
            )
        review = models.get(config.review.profile) if config.review.profile else None
        if review is None:
            review = models.role("sweep")
        if config.judge is not None:
            j = config.judge
            base = models.get(j.profile) if j.profile else models.role("judge")
            judge = base.model_copy(
                update={"effort": j.effort, "max_output_tokens": j.max_output_tokens}
            )
        if config.channels is not None and scenario.channels:
            c = config.channels
            base = models.get(c.profile) if c.profile else models.role("sweep")
            channels = base.model_copy(
                update={"effort": c.effort, "max_output_tokens": c.max_output_tokens}
            )
        if sweeps or channels is not None:
            extract = _extractor(models, config, agents)
        overseers = [*sweeps.values(), review, *([judge] if judge else [])]
        overseers += [p for p in (extract, channels) if p is not None]
        check_weights(agents, overseers, allow_same_model=config.allow_same_model)
    versions = dict(signals or {})
    profiles: dict[str, Profile | None] = {f"sweep:{n}": p for n, p in sweeps.items()}
    profiles |= {"review": review, "judge": judge}
    if extract is not None:
        profiles["extract"] = extract
    if channels is not None:
        profiles["channels"] = channels
    budget = budget_usd(
        config,
        scenario.limits.monitor_cost_usd,
        agents=len(scenario.agents),
        channels=bool(scenario.channels),
    )
    return ResolvedMonitor(
        config=config,
        texts=texts,
        oversight=scenario.oversight,
        budget_usd=budget,
        sweeps=sweeps,
        review=review,
        judge=judge,
        signals=versions,
        identity=identity(config, texts, profiles, versions),
        extract=extract,
        channels=channels,
    )


def _extractor(
    models: ModelsConfig, config: MonitorConfig, agents: Sequence[Profile]
) -> Profile | None:
    return extractor_for(models, config.extract, agents)


def extractor_for(
    models: ModelsConfig, candidates: Sequence[str], agents: Sequence[Profile]
) -> Profile | None:
    """The first of `candidates` the models know whose weights none of `agents` runs."""
    for name in candidates:
        try:
            candidate = models.get(name)
            check_weights(agents, [candidate], allow_same_model=False)
        except ProfileError:
            continue
        return candidate
    return None
