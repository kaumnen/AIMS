"""The monitor's part of `/api`: a run's flags, and offline monitor runs over sealed runs."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, JsonValue

from aims.config import Settings
from aims.events import Category, FlagLayer
from aims.events.index import FlagInfo, FlagStatus
from aims.models import ProfileError
from aims.monitor.config import MonitorConfig, MonitorConfigError
from aims.monitor.offline import offline_config, offline_monitor, origin_agents
from aims.prompts import TextError
from aims.run.fork import Origin, load_origin
from aims.run.job import JobOptions
from aims.run.runner import RunError
from aims.server.api import MAX_PAGE, Operator, Out, Started, State, Viewer, _queue, _run_id
from aims.server.problems import ProblemException, responses
from aims.wire import MonitorJob, MonitorMode

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))


class FlagPage(Out):
    """`origin` is the run a monitor run watched: the evidence seqs of its flags point there."""

    items: list[FlagInfo]
    next: str | None = None
    origin: str | None = None


@router.get("/runs/{run_id}/flags", tags=["monitor"], responses=responses(404))
async def list_flags(
    run_id: str,
    principal: Viewer,
    state: State,
    after: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    layer: FlagLayer | None = None,
    category: Category | None = None,
    min_importance: Annotated[int | None, Query(ge=1, le=5)] = None,
    status: Annotated[list[FlagStatus] | None, Query()] = None,
    agent: str | None = None,
    tainted: bool | None = None,
) -> FlagPage:
    """A run's flags in seq order, re-flags of a key folded into its first flag, each with the
    status later events gave it. Page on with `after=next`."""
    info = await state.store.run(_run_id(run_id))
    if info is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    items = await state.store.flags(
        run_id,
        after=after,
        limit=limit,
        layer=layer,
        category=category,
        min_importance=min_importance,
        status=status or (),
        agent_id=agent,
        tainted=tainted,
    )
    nxt = str(items[-1].seq) if len(items) == limit else None
    return FlagPage(items=items, next=nxt, origin=info.origin)


class StartMonitor(BaseModel):
    """`preset` replaces the preset the config files and the run's scenario name. `signals_only`
    runs the signals alone, `judge_only` the judge alone. `profile` replaces the model of every
    sweep, the review and the judge. `config` is one more `[monitor]` layer, laid over the
    config files and the scenario. `budget_usd` replaces the spend cap of each monitor run that
    the scenario's limits or the preset give."""

    preset: Literal["signals", "standard", "research"] | None = None
    signals_only: bool = False
    judge_only: bool = False
    profile: str | None = None
    config: dict[str, JsonValue] | None = None
    budget_usd: float | None = Field(default=None, ge=0)
    samples: int = Field(default=1, ge=1, le=100)
    parallel: int | None = Field(default=None, ge=1)
    runner: str | None = None


def monitor_config(origin: Origin, settings: Settings, body: StartMonitor) -> MonitorConfig:
    """The config layers, the origin's scenario, `body.config`, then the preset, the profile
    and the mode. Raises MonitorConfigError or ProfileError."""
    return offline_config(
        settings.monitor,
        origin.bundle.scenario,
        settings.models,
        config=body.config,
        preset=body.preset,
        signals_only=body.signals_only,
        judge_only=body.judge_only,
        profile=body.profile,
    )


def monitor_jobs(
    origin: Origin, settings: Settings, head: str, body: StartMonitor
) -> list[MonitorJob]:
    """`body.samples` jobs that monitor the sealed run `origin` again, resolved here like
    scenario jobs. Raises MonitorConfigError, ProfileError or TextError."""
    resolved = offline_monitor(
        settings.models,
        settings.prompts,
        origin.bundle.scenario,
        origin_agents(origin),
        layers=settings.monitor,
        config=body.config,
        preset=body.preset,
        signals_only=body.signals_only,
        judge_only=body.judge_only,
        profile=body.profile,
        samples=body.samples,
        budget_usd=body.budget_usd,
    )
    scenario = origin.bundle.scenario
    mode: MonitorMode = (
        "signals_only" if body.signals_only else "judge_only" if body.judge_only else "full"
    )
    group = uuid.uuid4().hex[:12] if body.samples > 1 else None
    return [
        MonitorJob(
            run_id=str(uuid.uuid7()),
            origin=origin.run_id,
            origin_head=head,
            scenario=scenario.name,
            mode=mode,
            monitor=resolved.snapshot(),
            options=JobOptions(
                profile=body.profile,
                group=group,
                sample=i if group else None,
                samples=body.samples,
            ),
            config_files=settings.layers.digests(),
        )
        for i in range(body.samples)
    ]


@router.post(
    "/runs/{run_id}/monitor", status_code=202, tags=["monitor"], responses=responses(404, 409)
)
async def monitor_run(
    run_id: str, body: StartMonitor, principal: Operator, state: State
) -> Started:
    """Queue offline monitor runs over a sealed run. A runner imports the run, feeds its events
    through the monitor again, skipping the monitor events and calls it holds, and writes the
    findings to a new run of kind `monitor` whose `origin` is this run. Actions are simulated;
    the run itself never changes."""
    run_id = _run_id(run_id)
    if body.signals_only and body.judge_only:
        raise ProblemException(422, "Invalid request", "pick signals_only or judge_only, not both")
    info = await state.store.run(run_id)
    head = await state.store.head(run_id) if info is not None else None
    if info is None or head is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    if info.status == "running":
        raise ProblemException(409, "Conflict", "the run is not sealed yet", code="not_sealed")
    if info.kind != "scenario":
        raise ProblemException(
            422, "Invalid request", f"a {info.kind} run has no agents to monitor", code="kind"
        )
    settings = state.config.settings()
    try:
        origin = await load_origin(state.store, run_id, full=False)
        jobs = monitor_jobs(origin, settings, head.hash, body)
    except (RunError, ProfileError, MonitorConfigError, TextError) as e:
        raise ProblemException(422, "Cannot monitor", str(e), code="monitor") from None
    return await _queue(state, principal, jobs, runner=body.runner, parallel=body.parallel)
