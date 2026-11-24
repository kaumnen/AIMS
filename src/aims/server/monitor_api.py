"""The monitor's part of `/api`: a run's flags, and offline monitor runs over sealed runs."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, JsonValue

from aims.config import Settings
from aims.events import Category, FlagLayer, Head
from aims.events.index import FlagInfo, FlagStatus
from aims.models import ProfileError
from aims.models.listprice import Listing, PriceLists
from aims.monitor.config import MonitorConfig, MonitorConfigError
from aims.monitor.offline import offline_config, offline_monitor, origin_agents
from aims.monitor.setup import ResolvedMonitor
from aims.prompts import TextError
from aims.run.estimate import ReviewEstimate, estimate_review, priced_review, reviews_confirm
from aims.run.fork import Origin, load_origin
from aims.run.job import JobOptions
from aims.run.runner import RunError
from aims.server import audit
from aims.server.api import (
    MAX_PAGE,
    Cursor,
    Operator,
    Out,
    Seq,
    Started,
    State,
    Viewer,
    _no_run,
    _queue,
    _run_id,
    _shown,
)
from aims.server.problems import ProblemException, responses
from aims.server.state import ServerState
from aims.text import NoLoneSurrogate, Utf8
from aims.wire import MonitorJob, MonitorMode

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))


class FlagPage(Out):
    """`origin` is the run a monitor run watched: the evidence seqs of its flags point there.
    `seq` and `hash` name the run's last event the index held before the page was read."""

    items: list[FlagInfo]
    next: str | None = None
    origin: str | None = None
    seq: int
    hash: str


@router.get("/runs/{run_id}/flags", tags=["monitor"], responses=responses(404, 409, 503))
async def list_flags(
    run_id: str,
    principal: Viewer,
    state: State,
    after: Cursor | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    layer: FlagLayer | None = None,
    category: Category | None = None,
    min_importance: Annotated[int | None, Query(ge=1, le=5)] = None,
    status: Annotated[list[FlagStatus] | None, Query()] = None,
    agent: str | None = None,
    tainted: bool | None = None,
    since: Seq | None = None,
    since_hash: Annotated[str | None, Query(alias="hash", max_length=64)] = None,
) -> FlagPage:
    """A run's flags in seq order, re-flags of a key folded into its first flag, each with the
    status later events gave it. Page on with `after=next`.

    A reader that keeps the flags it has reads on with `since` and `hash`, the `seq` and `hash`
    of its last read. It gets each flag an event past `since` made or changed, to put in place
    of the one it holds. 409 when the run's event at `since` no longer has that hash, as after
    its log was cut and written again: read the flags again from the start."""
    await _shown(state, principal, _run_id(run_id))
    info = await state.store.run(run_id)
    if info is None:
        raise _no_run(state, run_id)
    if since is not None and since_hash is not None:
        at = await state.store.events(run_id, after=since - 1, limit=1)
        if not at or at[0].seq != since or at[0].hash != since_hash:
            raise ProblemException(
                409,
                "Conflict",
                f"the run's event {since} is not the one read before: read the flags again",
                code="moved",
            )
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
        since=since,
    )
    nxt = str(items[-1].seq) if len(items) == limit else None
    return FlagPage(items=items, next=nxt, origin=info.origin, seq=info.events - 1, hash=info.head)


class StartMonitor(BaseModel):
    """`preset` replaces the preset the config files and the run's scenario name. `signals_only`
    runs the signals alone, `judge_only` the judge alone. `profile` replaces the model of every
    sweep, the review and the judge. `config` is one more `[monitor]` layer, laid over the
    config files and the scenario. `budget_usd` replaces the spend cap of each monitor run that
    the scenario's limits or the preset give. When the estimate is above `confirm_above_usd`,
    the monitor runs start only with `confirm`, like `aims monitor --yes`."""

    preset: Literal["signals", "standard", "research"] | None = None
    signals_only: bool = False
    judge_only: bool = False
    profile: Utf8 | None = None
    config: Annotated[dict[str, JsonValue] | None, NoLoneSurrogate] = None
    budget_usd: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)
    samples: int = Field(default=1, ge=1, le=100)
    parallel: int | None = Field(default=None, ge=1)
    runner: Utf8 | None = None
    confirm: bool = False


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


def resolve(origin: Origin, settings: Settings, body: StartMonitor) -> ResolvedMonitor:
    """The monitor `body` asks for over the sealed run `origin`. Raises MonitorConfigError,
    ProfileError or TextError."""
    return offline_monitor(
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
        steps=sum(origin.steps.values()),
    )


async def review_prices(
    origin: Origin, settings: Settings, body: StartMonitor, lists: PriceLists | None
) -> tuple[Settings, Listing]:
    """`settings` with the list prices from `lists` laid for the monitor `body` asks for, as its
    runs count them. Raises MonitorConfigError, ProfileError or TextError."""
    # one sample is enough to find the profiles, and logs no extra warning of a fixed seed
    once = body.model_copy(update={"samples": 1})
    return await priced_review(settings, resolve(origin, settings, once), lists)


def mode_of(body: StartMonitor) -> MonitorMode:
    return "signals_only" if body.signals_only else "judge_only" if body.judge_only else "full"


def monitor_jobs(
    origin: Origin, settings: Settings, head: str, body: StartMonitor
) -> list[MonitorJob]:
    """`body.samples` jobs that monitor the sealed run `origin` again, resolved here like
    scenario jobs. Raises MonitorConfigError, ProfileError or TextError."""
    resolved = resolve(origin, settings, body)
    scenario = origin.bundle.scenario
    mode = mode_of(body)
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
    "/runs/{run_id}/monitor", status_code=202, tags=["monitor"], responses=responses(404, 409, 503)
)
@audit.audited("run.monitor")
async def monitor_run(
    run_id: str, body: StartMonitor, principal: Operator, state: State, note: audit.Note
) -> Started:
    """Queue offline monitor runs over a sealed run. A runner imports the run, feeds its events
    through the monitor again, skipping the monitor events and calls it holds, and writes the
    findings to a new run of kind `monitor` whose `origin` is this run. Actions are simulated;
    the run itself never changes."""
    run_id = _run_id(run_id)
    head = (await _sealed(state, run_id, body)).hash
    settings = state.config.settings()
    try:
        origin = await load_origin(state.store, run_id, full=False, replay=False)
        # the list prices go on the profiles before the jobs are made, confirmed or not, so the
        # runner gets them and never reads the map
        settings, listing = await review_prices(origin, settings, body, state.prices)
        jobs = monitor_jobs(origin, settings, head, body)
        cost = None
        if not body.confirm:
            cost = await estimate_review(
                state.store,
                run_id,
                resolve(origin, settings, body),
                mode_of(body),
                samples=body.samples,
                confirm_above_usd=settings.confirm_above_usd,
                listing=listing,
            )
    except (RunError, ProfileError, MonitorConfigError, TextError) as e:
        raise ProblemException(422, "Cannot monitor", str(e), code="monitor") from None
    why = None if cost is None else reviews_confirm([cost])
    if why is not None:
        raise ProblemException(
            409, "Confirm the cost", f"{why}; set confirm to start anyway", code="confirm"
        )
    started = await _queue(state, principal.name, jobs, runner=body.runner, parallel=body.parallel)
    note.detail = audit.runs(started.run_ids)
    return started


async def _sealed(state: ServerState, run_id: str, body: StartMonitor) -> Head:
    if body.signals_only and body.judge_only:
        raise ProblemException(422, "Invalid request", "pick signals_only or judge_only, not both")
    info = await state.store.run(run_id)
    head = await state.store.head(run_id) if info is not None else None
    if info is None or head is None:
        raise _no_run(state, run_id)
    if info.status == "running":
        raise ProblemException(409, "Conflict", "the run is not sealed yet", code="not_sealed")
    if info.kind != "scenario":
        raise ProblemException(
            422, "Invalid request", f"a {info.kind} run has no agents to monitor", code="kind"
        )
    return head


@router.get(
    "/runs/{run_id}/monitor/estimate",
    tags=["monitor"],
    responses=responses(404, 409, 503),
)
async def estimate_monitor(
    run_id: str,
    principal: Operator,
    state: State,
    preset: Literal["signals", "standard", "research"] | None = None,
    signals_only: bool = False,
    judge_only: bool = False,
    profile: str | None = None,
    budget_usd: Annotated[float | None, Query(ge=0, allow_inf_nan=False)] = None,
    samples: Annotated[int, Query(ge=1, le=100)] = 1,
) -> ReviewEstimate:
    """What monitor runs over a sealed run cost, as `aims monitor` prints it before they start:
    the typical and high cost of past monitor runs with the same monitor, per agent step, and
    each one's spend cap. Takes the fields of the request that starts them, but `config`."""
    run_id = _run_id(run_id)
    body = StartMonitor(
        preset=preset,
        signals_only=signals_only,
        judge_only=judge_only,
        profile=profile,
        budget_usd=budget_usd,
        samples=samples,
    )
    await _sealed(state, run_id, body)
    settings = state.config.settings()
    try:
        origin = await load_origin(state.store, run_id, full=False, replay=False)
        settings, listing = await review_prices(origin, settings, body, state.prices)
        return await estimate_review(
            state.store,
            run_id,
            resolve(origin, settings, body),
            mode_of(body),
            samples=samples,
            confirm_above_usd=settings.confirm_above_usd,
            listing=listing,
        )
    except (RunError, ProfileError, MonitorConfigError, TextError) as e:
        raise ProblemException(422, "Cannot estimate", str(e), code="estimate") from None
