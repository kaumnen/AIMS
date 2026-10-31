"""A job: one run as the control plane hands it to a runner. Profiles, texts and the monitor
come resolved, so a runner needs no config file; the scenario's inputs are blob refs it fetches;
API keys and base URLs come from the runner's own environment."""

import contextlib
import dataclasses
import io
import tarfile
import tempfile
import uuid
from collections.abc import Generator, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aims.events import AuditLevel, EventStore, Oversight
from aims.models import ModelsConfig, Profile, Roles
from aims.monitor.config import MonitorConfigError, Preset
from aims.monitor.setup import ResolvedMonitor, resolve_config, resolve_monitor
from aims.monitor.signals import signal_versions
from aims.monitor.signals.base import SignalError
from aims.prompts import Prompts
from aims.run.fork import CutAt, Origin, fork_profiles, fork_schedules, live_profiles, planner
from aims.run.runner import Bench, Fork, Options, RunError, Runner, Texts, agent_profile
from aims.run.runner import resolve_texts as texts_of
from aims.scenario import Bundle, Scenario, ScenarioError, from_snapshot, load
from aims.scenario.budget import BudgetOverride

JobKind = Literal["scenario", "models.check"]
INPUTS = ("file", "files", "verifier", "context")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class JobFork(_Strict):
    """Where a fork job cuts its origin; `steps`, `cut`, `agent` and `keep_others` as in
    `Fork`, for a run of several agents."""

    origin: str
    step: int = Field(ge=0)
    sample: int = 0
    group: str | None = None
    strict: bool = False
    inject: str | None = None
    profile: str | None = None
    steps: dict[str, int] | None = None
    cut: int | None = None
    agent: str | None = None
    keep_others: bool = False


class JobOptions(_Strict):
    profile: str | None = None
    keep: bool = False
    group: str | None = None
    sample: int | None = None
    samples: int = Field(default=1, ge=1)
    # the control plane scores the run once its truth file arrives
    score: bool = False
    variant: str | None = None
    # over the scenario's oversight
    oversight: Oversight | None = None
    # over the scenario's caps; a fork's is already laid over its origin's
    budget: BudgetOverride | None = None


class Job(_Strict):
    """`monitor` is the resolved monitor's snapshot, None for a run without one; its profiles
    are in `models` too, so a runner offers their keys."""

    kind: JobKind = "scenario"
    run_id: str
    scenario: dict[str, JsonValue] | None = None
    profiles: dict[str, Profile] = Field(default_factory=dict)
    texts: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    models: dict[str, Profile] = Field(default_factory=dict)
    options: JobOptions = Field(default_factory=JobOptions)
    fork: JobFork | None = None
    audit: AuditLevel = "kernel"
    config_files: dict[str, str | None] = Field(default_factory=dict)
    check: list[str] = Field(default_factory=list)
    monitor: dict[str, JsonValue] | None = None
    # sha256 of the scenario's folder as a tar, which the runner loads the scenario from
    folder: str | None = None

    def blobs(self) -> list[str]:
        """The sha256 of each scenario input the runner must hold before it starts."""
        if self.scenario is None:
            return []
        out: list[str] = []
        for name in INPUTS:
            ref = self.scenario.get(name)
            if isinstance(ref, dict) and isinstance(ref.get("sha256"), str):
                out.append(str(ref["sha256"]))
        if self.folder is not None:
            out.append(self.folder)
        return out

    def envs(self) -> list[str]:
        """The environment variables its profiles read, which the runner must have set."""
        names: set[str] = set()
        for p in (*self.profiles.values(), *self.models.values()):
            names.update(e for e in (p.api_key_env, p.base_url_env) if e)
        return sorted(names)

    def needs(self) -> dict[str, JsonValue]:
        """What a runner must offer to take it."""
        return {
            "envs": list(self.envs()),
            "kernel": self.kind == "scenario" and self.audit == "kernel",
        }

    def config(self) -> ModelsConfig:
        return models_config([*self.profiles.values(), *self.models.values()])

    def watch(self) -> ResolvedMonitor | None:
        """The run's monitor, restored. Raises RunError."""
        if self.monitor is None:
            return None
        try:
            return ResolvedMonitor.restore(self.monitor)
        except MonitorConfigError as e:
            raise RunError(f"the job's monitor does not load: {e}") from None


def monitor_for(
    models: ModelsConfig,
    prompts: Prompts,
    scenario: Scenario,
    agents: Sequence[Profile],
    *,
    layers: Sequence[Mapping[str, Any]] = (),
    preset: Preset | None = None,
) -> ResolvedMonitor | None:
    """The monitor of a run of `scenario` whose agents use `agents`, from these config layers,
    with the versions of the signals it turns on. Raises MonitorConfigError, ProfileError or
    TextError."""
    config = resolve_config(layers, scenario, preset)
    try:
        versions = signal_versions(config)
    except SignalError as e:
        raise MonitorConfigError(str(e)) from None
    return resolve_monitor(
        models, prompts, scenario, agents, layers=layers, preset=preset, signals=versions
    )


def monitor_profiles(monitor: ResolvedMonitor | None) -> dict[str, Profile]:
    """The profiles the monitor calls, by name."""
    if monitor is None:
        return {}
    found = [*monitor.sweeps.values(), monitor.review, monitor.judge]
    return {p.name: p for p in found if p is not None}


def models_config(profiles: Iterable[Profile]) -> ModelsConfig:
    """A config holding `profiles` by name, for a runner that has no config file."""
    by_name = {p.name: p for p in profiles}
    if not by_name:
        raise RunError("the job names no model profile")
    first = next(iter(by_name))
    return ModelsConfig(profiles=by_name, roles=Roles(agent=first, sweep=first, judge=first))


def scenario_jobs(
    bundle: Bundle,
    models: ModelsConfig,
    prompts: Prompts,
    *,
    samples: int = 1,
    profile: str | None = None,
    keep: bool = False,
    audit: AuditLevel = "kernel",
    config_files: dict[str, str | None] | None = None,
    monitor: ResolvedMonitor | None = None,
    score: bool = False,
    folder: str | None = None,
    oversight: Oversight | None = None,
    budget: BudgetOverride | None = None,
) -> list[Job]:
    """One job per sample of a scenario, resolved with these layers, watched by `monitor`.
    `folder` is the sha256 of the scenario folder's tar, when the runner gets it. Raises
    ProfileError, TextError, UnknownTool or BudgetError."""
    if budget is not None:
        budget.check(a.id for a in bundle.scenario.agents)
    profiles = {a.id: agent_profile(models, a, profile) for a in bundle.scenario.agents}
    texts = {a.id: texts_of(prompts, a, bundle.scenario).snapshot() for a in bundle.scenario.agents}
    group = uuid.uuid4().hex[:12] if samples > 1 else None
    return [
        Job(
            run_id=str(uuid.uuid7()),
            scenario=bundle.snapshot(),
            profiles=profiles,
            texts=texts,
            models=monitor_profiles(monitor),
            monitor=None if monitor is None else monitor.snapshot(),
            options=JobOptions(
                profile=profile,
                keep=keep,
                group=group,
                sample=i if group else None,
                samples=samples,
                score=score,
                variant=bundle.variant,
                oversight=oversight,
                budget=None if budget is None or budget.empty else budget,
            ),
            audit=audit,
            config_files=dict(config_files or {}),
            folder=folder,
        )
        for i in range(samples)
    ]


def fork_jobs(
    origin: Origin,
    models: ModelsConfig,
    *,
    step: int,
    samples: int = 1,
    profile: str | None = None,
    strict: bool = False,
    inject: str | None = None,
    keep: bool = False,
    audit: AuditLevel = "kernel",
    config_files: dict[str, str | None] | None = None,
    monitor: ResolvedMonitor | None = None,
    cut: CutAt | None = None,
    keep_others: bool = False,
    oversight: Oversight | None = None,
    budget: BudgetOverride | None = None,
) -> list[Job]:
    """Jobs that fork the run `origin` at `step`, or at `cut` for a run of several agents,
    watched by `monitor`. The runner loads the
    origin itself. Without `profile`, the live steps use the origin's profiles, and only if
    `models` has them with the same endpoint and key variable. `budget` goes over the
    origin's override. Raises RunError, ProfileError or BudgetError."""
    budget = origin.fork_budget(budget)
    if profile is None:
        named = live_profiles(models, origin, step)
    else:
        named = {profile: models.get(profile)}
    group = uuid.uuid4().hex[:12] if samples > 1 else None
    return [
        Job(
            run_id=str(uuid.uuid7()),
            scenario=origin.bundle.snapshot(),
            models=monitor_profiles(monitor) | named,
            monitor=None if monitor is None else monitor.snapshot(),
            options=JobOptions(
                profile=profile,
                keep=keep,
                group=group,
                sample=i if group else None,
                samples=samples,
                oversight=oversight,
                budget=budget,
            ),
            fork=JobFork(
                origin=origin.run_id,
                step=step,
                sample=i,
                group=group,
                strict=strict,
                inject=inject,
                profile=profile,
                steps=None if cut is None else dict(cut.steps),
                cut=None if cut is None else cut.seq,
                agent=None if cut is None else cut.agent,
                keep_others=keep_others,
            ),
            audit=audit,
            config_files=dict(config_files or {}),
        )
        for i in range(samples)
    ]


def bundle_of(job: Job, blobs: dict[str, bytes]) -> Bundle:
    if job.scenario is None:
        raise RunError("the job has no scenario")
    try:
        return from_snapshot(dict(job.scenario), blobs)
    except (ScenarioError, KeyError) as e:
        raise RunError(f"the job's scenario does not load: {e}") from None


@contextlib.contextmanager
def job_bundle(job: Job, blobs: dict[str, bytes]) -> Generator[Bundle]:
    """The job's scenario. A job that carries its scenario's folder is loaded from a copy of
    it, as `aims run` loads the scenario, so the plants read their files and the variant keeps
    its name; the copy lasts until the block ends."""
    if job.folder is None:
        yield bundle_of(job, blobs)
        return
    if job.scenario is None:
        raise RunError("the job has no scenario")
    with tempfile.TemporaryDirectory(prefix="aims-scenario-") as tmp:
        root = Path(tmp)
        try:
            with tarfile.open(fileobj=io.BytesIO(blobs[job.folder]), mode="r:") as tar:
                tar.extractall(root, filter="data")
            bundle = load(root / "scenario.yaml", job.options.variant)
        except (ScenarioError, KeyError, tarfile.TarError, OSError) as e:
            raise RunError(f"the job's scenario folder does not load: {e}") from None
        if bundle.digest != job.scenario.get("digest"):
            raise RunError("the job's scenario folder does not match its scenario")
        yield bundle


def runner_for(
    bench: Bench,
    job: Job,
    bundle: Bundle,
    *,
    origin: Origin | None = None,
    origins: EventStore | None = None,
) -> Runner:
    """The Runner of a scenario job, on a bench of its own that shares `bench`'s store,
    backend, limiters, delta bus and gate, watched by the job's monitor."""
    run_bench = dataclasses.replace(
        bench,
        models=job.config(),
        audit=job.audit,
        config_files=dict(job.config_files),
    )
    monitor = job.watch()
    o = job.options
    options = Options(
        profile=o.profile,
        keep=o.keep,
        group=o.group,
        sample=o.sample,
        samples=o.samples,
        variant=o.variant,
        oversight=o.oversight,
        budget=o.budget,
    )
    if job.fork is None:
        try:
            texts = {a: Texts.restore(t) for a, t in job.texts.items()}
        except (KeyError, TypeError, ValueError) as e:
            raise RunError(f"the job's texts do not load: {e}") from None
        missing = {a.id for a in bundle.scenario.agents} - set(job.profiles)
        if missing:
            raise RunError(f"the job has no profile for agent(s) {', '.join(sorted(missing))}")
        return Runner(
            run_bench,
            bundle,
            options,
            profiles=dict(job.profiles),
            texts=texts,
            run_id=job.run_id,
            monitor=monitor,
        )
    if origin is None:
        raise RunError("a fork job needs its origin loaded")
    bundle = origin.full_bundle or bundle
    options.seed = origin.seed
    options.inherited = origin.inherited
    options.restored = dict(origin.restored) or None
    f = job.fork
    fork = Fork(
        origin=f.origin,
        step=f.step,
        sample=f.sample,
        group=f.group,
        strict=f.strict,
        inject=f.inject,
        profile=f.profile,
        steps=f.steps,
        cut=f.cut,
        agent=f.agent,
        keep_others=f.keep_others,
    )
    options.fork = fork
    return Runner(
        run_bench,
        bundle,
        options,
        profiles=fork_profiles(run_bench, origin, fork),
        schedules=fork_schedules(origin, fork),
        texts=origin.texts,
        replay=planner(run_bench, origin, fork, source=origins),
        run_id=job.run_id,
        monitor=monitor,
    )
