"""`/api`: what the UI and scripts call. One endpoint per CLI command, plus run control."""

import asyncio
import hashlib
import json
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from compression import zstd
from pathlib import Path
from typing import IO, Annotated, Literal, Self, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from aims import __version__
from aims.config import Settings
from aims.events import (
    AuditLevel,
    Category,
    Event,
    ExecActivity,
    IngestError,
    MissingBlobs,
    Oversight,
    ResumeThen,
    RunCorrupt,
    RunInfo,
    RunKind,
    SampleWindow,
    StepInfo,
    split_lines,
)
from aims.events.blobs import ZSTD_MAGIC, BlobError, BlobStore
from aims.events.index import monitor_of
from aims.events.schema import blob_refs
from aims.events.store import is_run_id
from aims.models import Profile, ProfileError
from aims.models.check import Verdict
from aims.monitor import texts as monitor_texts
from aims.monitor.config import MonitorConfigError, Preset
from aims.monitor.offline import origin_agents
from aims.monitor.setup import ResolvedMonitor
from aims.prompts import Prompts, TextError, load_prompts
from aims.run.estimate import estimate_runs, needs_confirm
from aims.run.fork import CutAt, check_fork, cut_at, live_profiles, load_origin
from aims.run.job import Job, fork_jobs, monitor_for, scenario_jobs
from aims.run.runner import RunError, agent_profile, check
from aims.scenario import Bundle, Scenario, ScenarioError, folder_tar, load, parse
from aims.server import db as sdb
from aims.server.auth import Principal, require
from aims.server.files import Sources
from aims.server.problems import ProblemError, ProblemException, responses
from aims.server.runner_api import send
from aims.server.state import ServerState
from aims.tools import UnknownTool, registry
from aims.wire import (
    BASE_KINDS,
    CommandAction,
    CommandMessage,
    MonitorJob,
    RunnerInfo,
    RunSummary,
    ScoreJob,
)

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))
Viewer = Annotated[Principal, Depends(require("viewer"))]
Operator = Annotated[Principal, Depends(require("operator"))]
Uploader = Annotated[
    Principal,
    Depends(
        require("operator", bodies=frozenset({"application/x-tar", "application/octet-stream"}))
    ),
]
MAX_PAGE = 1000
MAX_SAMPLES = 100
MAX_STEPS = 500
MAX_EXECS = 100
IMPORT_LIMIT = 4 * 1024**3


def state_of(request: Request) -> ServerState:
    return request.app.state.server


State = Annotated[ServerState, Depends(state_of)]


def sources_of(request: Request) -> Sources:
    return request.app.state.sources


SourcesDep = Annotated[Sources, Depends(sources_of)]


class Out(BaseModel):
    """A reply body: fields with defaults are always sent, so the schema lists them as
    required."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class RunPage(Out):
    items: list[RunSummary]
    next: str | None = None


class EventPage(Out):
    items: list[Event]
    next: str | None = None


# Sessions and health


class SessionStart(BaseModel):
    """A token, or a one-shot code from the link `aims ui` prints."""

    token: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.token is None) == (self.code is None):
            raise ValueError("give a token or a code")
        return self


class Whoami(Out):
    name: str
    role: str
    auth: bool


@router.post("/session", status_code=204, tags=["session"])
async def start_session(body: SessionStart, request: Request, state: State) -> Response:
    """Trade a token or a launch code for a session cookie, so the browser never stores the
    token."""
    if body.code is not None:
        name = state.codes.redeem(body.code)
        if name is None:
            raise ProblemException(
                401, "Not signed in", "unknown, used or expired code", code="code"
            )
    else:
        token = await state.auth.token(body.token or "")
        if token is None or token.role == "runner":
            raise ProblemException(401, "Not signed in", "unknown or revoked token", code="token")
        if token.name in {t.name for t in state.auth.ephemeral.values()}:
            raise ProblemException(401, "Not signed in", "that token is for runners", code="token")
        name = token.name
    session = await state.db.create_session(name)
    response = Response(status_code=204)
    response.set_cookie(
        state.auth.cookie,
        session,
        max_age=sdb.SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
        secure=state.auth.tls,
        path="/",
    )
    return response


@router.delete("/session", status_code=204, tags=["session"])
async def end_session(request: Request, principal: Viewer, state: State) -> Response:
    session = request.cookies.get(state.auth.cookie)
    if session:
        await state.db.end_session(session)
    response = Response(status_code=204)
    response.delete_cookie(state.auth.cookie, path="/")
    return response


@router.get("/session", tags=["session"])
async def whoami(principal: Viewer, state: State) -> Whoami:
    return Whoami(name=principal.name, role=principal.role, auth=state.auth.enabled)


class Health(Out):
    ok: bool
    version: str
    uptime_s: float
    runners: int
    tracer: bool


@router.get("/health", tags=["health"])
async def health(principal: Viewer, state: State) -> Health:
    live = [c for c in state.runners.values() if c.streams > 0 and not c.stale]
    return Health(
        ok=True,
        version=__version__,
        uptime_s=round(time.time() - state.started, 1),
        runners=len(live),
        tracer=any(c.tracer_alive for c in live),
    )


# Runs


def _run_id(run_id: str) -> str:
    if not is_run_id(run_id):
        raise ProblemException(404, "Not found", f"not a run id: {run_id}")
    return run_id


def queue_reason(state: ServerState, job: sdb.Job) -> str | None:
    if job.state not in ("queued", "offered"):
        return job.reason
    if job.target is not None and job.target not in state.runners:
        return f"runner {job.target} is not connected"
    live = [c for c in state.runners.values() if c.streams > 0 and not c.stale]
    if not live:
        return "no runner is connected"
    kind = job.needs.get("kind")
    if kind is not None and not any(kind in c.hello.kinds for c in live):
        return f"no connected runner runs {kind} jobs"
    envs = set(job.needs.get("envs") or [])
    if envs and not any(envs <= set(c.envs) for c in live):
        return f"no connected runner has {', '.join(sorted(envs))} set"
    if job.needs.get("kernel") and not any(c.tracer_alive for c in live):
        return "no connected runner has the kernel tracer up"
    if not any(c.free() > 0 for c in live):
        return "every runner is busy"
    return None


def summary(
    state: ServerState,
    run_id: str,
    info: RunInfo | None,
    job: sdb.Job | None,
    meta: sdb.RunMeta | None,
) -> RunSummary:
    runner = (meta.runner if meta else None) or (job.runner if job else None)
    status = info.status if info is not None else (job.state if job is not None else "unknown")
    state_name = job.state if job is not None else ("running" if status == "running" else "done")
    conn = state.runners.get(runner or "")
    stale = status == "running" and (conn is None or conn.stale)
    scenario = info.scenario if info is not None else None
    if scenario is None and job is not None:
        snap = job.payload.get("scenario")
        if isinstance(snap, dict):
            scenario = snap.get("name")
        else:
            scenario = snap if isinstance(snap, str) else job.payload.get("kind")
    planned = monitor_of(job.payload) if job is not None else (None, None)
    if info is not None:
        kind, origin = info.kind, info.origin
    else:
        kind = job.payload.get("kind", "scenario") if job is not None else "scenario"
        origin = job.payload.get("origin") if job is not None else None
    return RunSummary(
        run_id=run_id,
        scenario=None if scenario is None else str(scenario),
        status=status,
        state=state_name,
        started_at=None if info is None else info.started_at.isoformat(),
        ended_at=None if info is None or info.ended_at is None else info.ended_at.isoformat(),
        events=0 if info is None else info.events,
        runner=runner,
        stale=stale,
        diverged=None if meta is None else meta.diverged,
        group=None if job is None else job.group,
        reason=None if job is None else queue_reason(state, job),
        kind=kind,
        origin=origin if isinstance(origin, str) else None,
        flags=0 if info is None else info.flags,
        categories={} if info is None else info.categories,
        max_importance=None if info is None else info.max_importance,
        verdict_severity=None if info is None else info.verdict_severity,
        max_severity=0 if info is None else info.severity,
        series=None if info is None else info.series,
        series_index=None if info is None else info.series_index,
        variant=None if info is None else info.variant,
        steps=0 if info is None else info.steps,
        cost_usd=None if info is None else info.cost_usd,
        reward=None if info is None else info.reward,
        reviews=0 if info is None else info.reviews,
        preset=info.preset if info is not None else planned[0],
        monitor_mode=info.monitor_mode if info is not None else planned[1],
        held=[] if info is None else info.held,
    )


async def summaries(state: ServerState, run_ids: list[str]) -> list[RunSummary]:
    jobs = await state.db.jobs_of(run_ids)
    metas = await state.db.runs_meta(run_ids)
    out = []
    for run_id in run_ids:
        info = await state.store.run(run_id)
        if info is None and run_id not in jobs:
            continue
        out.append(summary(state, run_id, info, jobs.get(run_id), metas.get(run_id)))
    return out


@router.get("/runs", tags=["runs"])
async def list_runs(
    principal: Viewer,
    state: State,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    kind: RunKind | None = None,
    origin: str | None = None,
    severity: Annotated[int | None, Query(ge=1, le=5)] = None,
    category: Category | None = None,
    series: str | None = None,
    variant: str | None = None,
    scenario: str | None = None,
) -> RunPage:
    """Runs newest first, with jobs that have not started yet. `kind` keeps runs of that kind,
    `origin` the monitor runs of that run, `severity` runs whose `max_severity` is at least it,
    `category` runs with a flag in it, whatever its status, `series` the runs of that series,
    `variant` the sealed runs of that variant and `scenario` the runs of that scenario. Jobs
    not started yet have none of `severity`, `category`, `series` and `variant`, so those
    leave them out."""
    infos = await state.store.runs(
        scenario=scenario,
        limit=limit,
        before=before,
        kind=kind,
        origin=origin,
        severity=severity,
        category=category,
        series=series,
        variant=variant,
    )
    if severity is None and category is None and series is None and variant is None:
        jobs = await state.db.jobs_page(
            before=before, limit=limit, kind=kind, origin=origin, scenario=scenario
        )
    else:
        jobs = []
    ids = sorted({i.run_id for i in infos} | {j.run_id for j in jobs}, reverse=True)[:limit]
    items = await summaries(state, ids)
    return RunPage(items=items, next=ids[-1] if len(ids) == limit else None)


# Runs whose content shows a run's truth: a score names the plants, where each was taken and
# the facts the classifier was told. Viewers see them listed, not opened.
TRUTH_KINDS = ("score",)


async def _shown(state: ServerState, principal: Principal, run_id: str) -> None:
    """Raises 403 when a viewer asks for what a run of `TRUTH_KINDS` holds."""
    if principal.role != "viewer":
        return
    info = await state.store.run(run_id)
    if info is not None and info.kind in TRUTH_KINDS:
        raise ProblemException(
            403,
            "Forbidden",
            f"a {info.kind} run shows the truth of the run it scores; this needs the operator role",
            code="truth",
        )


@router.get("/runs/{run_id}/truth", tags=["runs"], responses=responses(404))
async def get_truth(run_id: str, principal: Operator, state: State) -> dict[str, JsonValue]:
    """The truth file of a sealed run: the plants, canaries, roles, scripts and variant it hid
    from its agents, its monitor and its judge. Operators only; not found until the runner
    that wrote it sent it, after the run sealed."""
    path = state.store.truth_path(_run_id(run_id))
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise ProblemException(404, "Not found", f"run {run_id} has no truth file here") from None
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ProblemException(404, "Not found", f"run {run_id} has no truth file here")
    return value


class RunDetail(Out):
    """`verdict` is the judge's `monitor.verdict`; `monitor_runs` are the monitor runs over this
    run and `score_runs` the scores of it, queued ones too, newest first."""

    run: RunSummary
    started: Event | None = None
    finished: Event | None = None
    verdict: Event | None = None
    monitor_runs: list[str] = Field(default_factory=list)
    score_runs: list[str] = Field(default_factory=list)


@router.get("/runs/{run_id}", tags=["runs"], responses=responses(404))
async def get_run(run_id: str, principal: Viewer, state: State) -> RunDetail:
    await _shown(state, principal, _run_id(run_id))
    (found,) = await summaries(state, [_run_id(run_id)]) or [None]
    if found is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    first = await state.store.events(run_id, limit=1)
    last = await state.store.events(run_id, types=["run.finished"], limit=1)
    verdict = await state.store.events(
        run_id, types=["monitor.verdict"], before=found.events, limit=1
    )
    return RunDetail(
        run=found,
        started=first[0] if first else None,
        finished=next(iter(last), None),
        verdict=next(iter(verdict), None),
        monitor_runs=await _over(state, run_id, "monitor"),
        score_runs=await _over(state, run_id, "score"),
    )


async def _over(state: ServerState, run_id: str, kind: Literal["monitor", "score"]) -> list[str]:
    """The runs of `kind` over run `run_id`, queued ones too, newest first."""
    done = await state.store.runs(origin=run_id, kind=kind, limit=MAX_PAGE)
    queued = await state.db.jobs_page(before=None, limit=MAX_PAGE, kind=kind, origin=run_id)
    return sorted({r.run_id for r in done} | {j.run_id for j in queued}, reverse=True)


class ScenarioRef(BaseModel):
    source: str
    name: str


class StartRun(BaseModel):
    """`monitor` picks the monitor's preset over the config files and the scenario; None keeps
    what they say, else `standard`. `variant` applies a named variant of the scenario. When the
    estimate is above `confirm_above_usd`, or nothing bounds the cost, the runs start only with
    `confirm`, like `aims run --yes`. `score` scores each run against its truth file once the
    file arrives, as `aims run --score` does."""

    scenario: ScenarioRef
    variant: str | None = None
    confirm: bool = False
    score: bool = False
    samples: int = Field(default=1, ge=1, le=100)
    parallel: int | None = Field(default=None, ge=1)
    profile: str | None = None
    keep: bool = False
    audit: AuditLevel = "kernel"
    runner: str | None = None
    monitor: Preset | None = None


class StartFork(BaseModel):
    """`monitor` picks the fork's monitor preset over the config files and the scenario. `at`
    cuts a run of several agents: `agent:N` after that agent's step N, or `@seq` after an
    event; with `keep_others` the other agents keep their recorded answers until their own
    inputs change. `step` is the last step replayed of a run of one agent."""

    step: int | None = Field(default=None, ge=0)
    at: str | None = None
    keep_others: bool = False
    samples: int = Field(default=1, ge=1, le=100)
    parallel: int | None = Field(default=None, ge=1)
    profile: str | None = None
    strict: bool = False
    inject: str | None = None
    keep: bool = False
    audit: AuditLevel = "kernel"
    runner: str | None = None
    monitor: Preset | None = None


class Started(Out):
    run_ids: list[str]
    group: str | None = None


def _check_runner(
    state: ServerState, runner: str | None, jobs: Sequence[Job | MonitorJob | ScoreJob]
) -> None:
    known = list(state.runners.values())
    if runner is not None:
        if runner not in state.runners:
            raise ProblemException(422, "Invalid request", f"no runner named {runner}")
        known = [state.runners[runner]]
    if not known:
        raise ProblemException(409, "Conflict", "no runner has said hello", code="no_runner")
    kind = jobs[0].kind if jobs else None
    if kind in ("monitor", "score") and not any(kind in c.hello.kinds for c in known):
        raise ProblemException(422, "Invalid request", f"no runner runs {kind} jobs", code="kind")
    envs = set(jobs[0].envs()) if jobs else set()
    if envs and not any(envs <= set(c.envs) for c in known):
        raise ProblemException(
            422,
            "Invalid request",
            f"no runner has {', '.join(sorted(envs))} set",
            code="envs",
        )


async def _queue(
    state: ServerState,
    token: str | None,
    jobs: Sequence[Job | MonitorJob | ScoreJob],
    *,
    runner: str | None,
    parallel: int | None,
    blobs: dict[str, bytes] | None = None,
    copy_from: str | None = None,
) -> Started:
    _check_runner(state, runner, jobs)
    now = time.time()
    for job in jobs:
        state.store.prepare_run(job.run_id)
        if blobs is not None:
            await state.store.put_blobs(job.run_id, [blobs[sha] for sha in job.blobs()])
        elif copy_from is not None:
            for sha in job.blobs():
                path = state.store.blob_path(copy_from, sha)
                await state.store.put_blob_stream(job.run_id, sha, _file(path), limit=IMPORT_LIMIT)
    rows = [
        sdb.Job(
            run_id=j.run_id,
            group=j.options.group,
            parallel=parallel,
            state="queued",
            payload=j.model_dump(mode="json"),
            needs=dict(j.needs()),
            target=runner,
            runner=None,
            offered_at=None,
            tried=[],
            token=token,
            created=now,
            updated=now,
            reason=None,
        )
        for j in jobs
    ]
    await state.db.add_jobs(rows)
    for j in jobs:
        state.changes.touch(j.run_id)
    state.poke()
    return Started(run_ids=[j.run_id for j in jobs], group=jobs[0].options.group if jobs else None)


async def _file(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as f:
        while chunk := await asyncio.to_thread(f.read, 1024 * 1024):
            yield chunk


def scenario_problems(e: ScenarioError) -> list[ProblemError]:
    out = []
    for line in e.problems:
        number = None
        head, _, rest = line.partition(": ")
        if ":" in head and head.rsplit(":", 1)[1].isdigit():
            number = int(head.rsplit(":", 1)[1])
        out.append(ProblemError(msg=rest or line, line=number))
    return out


@router.post("/runs", status_code=202, tags=["runs"], responses=responses(409))
async def start_runs(
    body: StartRun, principal: Operator, state: State, sources: SourcesDep
) -> Started:
    """Queue `samples` runs of a scenario; the control plane checks them first and assigns
    their run ids."""
    _, path = sources.scenario(body.scenario.source, body.scenario.name)
    settings = state.config.settings()
    try:
        bundle = load(path / "scenario.yaml", body.variant)
    except ScenarioError as e:
        raise ProblemException(
            422, "Scenario does not load", code="scenario", errors=scenario_problems(e)
        ) from None
    problems = check(bundle, settings.models, body.profile, settings.prompts)
    if problems:
        raise ProblemException(
            422,
            "Scenario cannot run",
            code="scenario",
            errors=[ProblemError(msg=p) for p in problems],
        )
    agents = [agent_profile(settings.models, a, body.profile) for a in bundle.scenario.agents]
    monitor = _monitor(settings, bundle.scenario, agents, body.monitor)
    try:
        folder = await asyncio.to_thread(folder_tar, path / "scenario.yaml")
    except OSError as e:
        raise ProblemException(422, "Scenario cannot run", str(e), code="scenario") from None
    try:
        jobs = scenario_jobs(
            bundle,
            settings.models,
            settings.prompts,
            samples=body.samples,
            profile=body.profile,
            keep=body.keep,
            audit=body.audit,
            config_files=settings.layers.digests(),
            monitor=monitor,
            score=body.score,
            folder=hashlib.sha256(folder).hexdigest(),
        )
    except (ProfileError, UnknownTool, ValueError) as e:
        raise ProblemException(422, "Scenario cannot run", str(e), code="scenario") from None
    if not body.confirm:
        try:
            cost = estimate_runs(
                await state.history(),
                settings,
                [bundle],
                profile=body.profile,
                preset=body.monitor,
                samples=body.samples,
            )
        except ProfileError, MonitorConfigError, TextError:
            cost = None
        why = None if cost is None else needs_confirm(cost)
        if why is not None:
            raise ProblemException(
                409, "Confirm the cost", f"{why}; set confirm to start anyway", code="confirm"
            )
    blobs = {hashlib.sha256(b).hexdigest(): b for b in [*bundle.blobs(), folder]}
    return await _queue(
        state, principal.name, jobs, runner=body.runner, parallel=body.parallel, blobs=blobs
    )


@router.post("/runs/{run_id}/forks", status_code=202, tags=["runs"], responses=responses(404, 409))
async def fork_run(run_id: str, body: StartFork, principal: Operator, state: State) -> Started:
    """Replay a recorded run up to a step and go on live from there."""
    run_id = _run_id(run_id)
    settings = state.config.settings()
    if body.at is not None and body.step is not None:
        raise ProblemException(422, "Invalid request", "give step or at, not both")
    if body.keep_others and (body.at is None or ":" not in body.at):
        raise ProblemException(422, "Invalid request", "keep_others needs at agent:N")
    cut = None
    try:
        origin = await load_origin(state.store, run_id)
        if body.at is None and len(origin.bundle.scenario.agents) > 1:
            raise RunError("a run of several agents forks at agent:N or @seq")
        found = cut_at(origin, body.at) if body.at is not None else None
        if isinstance(found, CutAt):
            cut = found
            step = min(found.steps.values(), default=0)
        else:
            step = body.step if found is None else found
            if step is None:
                step = min(origin.steps.values(), default=0)
            check_fork(origin, step)
        if body.profile is not None:
            live = [settings.models.get(body.profile)]
        else:
            live = list(live_profiles(settings.models, origin, step).values())
    except (RunError, ProfileError, KeyError) as e:
        raise ProblemException(422, "Cannot fork", str(e), code="fork") from None
    agents = [*live, *origin_agents(origin)]
    monitor = _monitor(settings, origin.bundle.scenario, agents, body.monitor)
    try:
        jobs = fork_jobs(
            origin,
            settings.models,
            step=step,
            samples=body.samples,
            profile=body.profile,
            strict=body.strict,
            inject=body.inject,
            keep=body.keep,
            audit=body.audit,
            config_files=settings.layers.digests(),
            monitor=monitor,
            cut=cut,
            keep_others=body.keep_others,
        )
    except (RunError, ProfileError, KeyError) as e:
        raise ProblemException(422, "Cannot fork", str(e), code="fork") from None
    return await _queue(
        state, principal.name, jobs, runner=body.runner, parallel=body.parallel, copy_from=run_id
    )


def _monitor(
    settings: Settings, scenario: Scenario, agents: list[Profile], preset: Preset | None
) -> ResolvedMonitor | None:
    """The monitor of runs of `scenario` by `agents`: the config layers, the scenario, then
    `preset`. None when it is off."""
    try:
        return monitor_for(
            settings.models,
            settings.prompts,
            scenario,
            agents,
            layers=settings.monitor,
            preset=preset,
        )
    except (MonitorConfigError, ProfileError, TextError) as e:
        raise ProblemException(422, "Monitor cannot run", str(e), code="monitor") from None


# Run control


class PauseBody(BaseModel):
    agent: str | None = None
    reason: str | None = None


class ResumeBody(BaseModel):
    agent: str | None = None
    then: ResumeThen = "continue"
    text: str | None = None
    reason: str | None = None


class SweepBody(BaseModel):
    agent: str | None = None
    reason: str | None = None


class InjectBody(BaseModel):
    agent: str | None = None
    text: str = Field(min_length=1)


class StopBody(BaseModel):
    reason: str | None = None


class CommandSent(Out):
    command_id: str | None = None
    delivered: bool = False
    state: str


class CommandInfo(Out):
    id: str
    run_id: str
    runner: str
    action: str
    created: float
    acked: float | None
    outcome: str | None


async def command(
    state: ServerState, run_id: str, action: CommandAction, principal: Principal
) -> CommandSent:
    run_id = _run_id(run_id)
    job = await state.db.job(run_id)
    if job is not None and job.state in ("queued", "offered"):
        if action.action in ("cancel", "kill"):
            reason = action.reason or f"{action.action}led by {principal.name}"
            if await state.db.cancel_queued(run_id, reason):
                state.changes.touch(run_id)
                return CommandSent(state="cancelled")
        raise ProblemException(409, "Conflict", "the run has not started yet", code="not_started")
    info = await state.store.run(run_id)
    meta = await state.db.run_meta(run_id)
    runner = (job.runner if job is not None else None) or (meta.runner if meta else None)
    if runner is None or (info is None and job is None):
        raise ProblemException(404, "Not found", f"no run {run_id} to control")
    if info is not None and info.status != "running":
        raise ProblemException(409, "Conflict", "the run has ended", code="ended")
    action = action.model_copy(update={"token": principal.name})
    c = await state.db.add_command(runner, run_id, action.model_dump(mode="json"))
    delivered = send(
        state, runner, CommandMessage(id=c.id, seq=c.seq, run_id=run_id, command=action)
    )
    return CommandSent(command_id=c.id, delivered=delivered, state="sent")


@router.post("/runs/{run_id}/pause", status_code=202, tags=["control"])
async def pause_run(run_id: str, body: PauseBody, principal: Operator, state: State) -> CommandSent:
    """Freeze the run's sandboxes now and hold its agents at their next step."""
    act = CommandAction(action="pause", agent=body.agent, reason=body.reason)
    return await command(state, run_id, act, principal)


@router.post("/runs/{run_id}/resume", status_code=202, tags=["control"])
async def resume_run(
    run_id: str, body: ResumeBody, principal: Operator, state: State
) -> CommandSent:
    """Let paused agents go on. `then` is what comes first: `continue`, `kill`, `inject` the
    `text`, or `deny` the tool call the agent is held at, which does not run; the agent gets
    the configured `denied` reply, never `text` or `reason`."""
    if body.then == "inject" and not body.text:
        raise ProblemException(422, "Invalid request", "an inject needs text")
    if body.then == "deny" and body.text:
        raise ProblemException(
            422, "Invalid request", "a deny takes no text; the agent gets the `denied` reply"
        )
    act = CommandAction(
        action="resume", agent=body.agent, then=body.then, text=body.text, reason=body.reason
    )
    return await command(state, run_id, act, principal)


@router.post("/runs/{run_id}/sweep", status_code=202, tags=["control"])
async def sweep_run(run_id: str, body: SweepBody, principal: Operator, state: State) -> CommandSent:
    """Ask the run's monitor for a sweep now, of one agent or of all."""
    first = await state.store.events(_run_id(run_id), limit=1)
    if first and first[0].type == "run.started":
        kind = first[0].data.get("kind") or "scenario"
        if kind != "scenario":
            raise ProblemException(409, "Conflict", f"a {kind} run has no agents", code="kind")
        if first[0].data.get("monitor") is None:
            raise ProblemException(409, "Conflict", "the run has no monitor", code="no_monitor")
    act = CommandAction(action="sweep", agent=body.agent, reason=body.reason)
    return await command(state, run_id, act, principal)


@router.post("/runs/{run_id}/inject", status_code=202, tags=["control"])
async def inject_run(
    run_id: str, body: InjectBody, principal: Operator, state: State
) -> CommandSent:
    """Give an agent a message at its next step."""
    act = CommandAction(action="inject", agent=body.agent, text=body.text)
    return await command(state, run_id, act, principal)


@router.post("/runs/{run_id}/kill", status_code=202, tags=["control"])
async def kill_run(run_id: str, body: StopBody, principal: Operator, state: State) -> CommandSent:
    """Freeze, archive and kill every sandbox; the verifier does not run."""
    return await command(state, run_id, CommandAction(action="kill", reason=body.reason), principal)


@router.post("/runs/{run_id}/cancel", status_code=202, tags=["control"])
async def cancel_run(run_id: str, body: StopBody, principal: Operator, state: State) -> CommandSent:
    """Stop the agents, skip the verifier, archive and seal the run as cancelled."""
    act = CommandAction(action="cancel", reason=body.reason)
    return await command(state, run_id, act, principal)


@router.get("/runs/{run_id}/commands", tags=["control"])
async def list_commands(run_id: str, principal: Viewer, state: State) -> list[CommandInfo]:
    return [
        CommandInfo(
            id=c.id,
            run_id=c.run_id,
            runner=c.runner,
            action=str(c.payload.get("action")),
            created=c.created,
            acked=c.acked,
            outcome=c.outcome,
        )
        for c in await state.db.commands_of(_run_id(run_id))
    ]


# Events, samples, blobs, verification


@router.get("/runs/{run_id}/events", tags=["events"])
async def list_events(
    run_id: str,
    principal: Viewer,
    state: State,
    after: int | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    types: Annotated[list[str] | None, Query()] = None,
    agent: str | None = None,
    q: Annotated[str | None, Query(max_length=500)] = None,
    execs: Annotated[list[str] | None, Query(alias="exec")] = None,
) -> EventPage:
    """One page in seq order. With only `before`, the page is the events just before it and
    `next` pages further back. `exec` keeps the events of those execs: their start and end and
    every kernel record, spill and snapshot that names them."""
    await _shown(state, principal, _run_id(run_id))
    items = await state.store.events(
        _run_id(run_id),
        after=after,
        before=before,
        limit=limit,
        types=types or (),
        agent_id=agent,
        query=q,
        execs=_execs(execs),
    )
    nxt = None
    if len(items) == limit:
        backwards = before is not None and after is None
        nxt = str(items[0].seq if backwards else items[-1].seq)
    return EventPage(items=items, next=nxt)


@router.get("/runs/{run_id}/events/{seq}", tags=["events"], responses=responses(404))
async def get_event(run_id: str, seq: int, principal: Viewer, state: State) -> Event:
    await _shown(state, principal, _run_id(run_id))
    items = await state.store.events(_run_id(run_id), after=seq - 1, limit=1)
    if not items or items[0].seq != seq:
        raise ProblemException(404, "Not found", f"run {run_id} has no event {seq}")
    return items[0]


@router.get("/events", tags=["events"])
async def search_events(
    principal: Viewer,
    state: State,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
) -> EventPage:
    """Events of every run whose text holds `q`, newest first; a viewer's leave out the runs
    that show a run's truth."""
    skip = TRUTH_KINDS if principal.role == "viewer" else ()
    items, cursor = await state.store.search(q, limit=limit, before=before, skip_kinds=skip)
    return EventPage(items=items, next=None if cursor is None else str(cursor))


def _execs(execs: list[str] | None) -> list[str]:
    if execs is not None and len(execs) > MAX_EXECS:
        raise ProblemException(422, "Invalid request", f"at most {MAX_EXECS} execs at a time")
    return execs or []


class StepPage(Out):
    items: list[StepInfo]
    next: int | None = None


@router.get("/runs/{run_id}/steps", tags=["events"])
async def list_steps(
    run_id: str,
    principal: Viewer,
    state: State,
    after: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_STEPS)] = 200,
    agent: str | None = None,
) -> StepPage:
    """Agent steps in seq order, one row per model call with the tool calls it made, their
    commands' ends and results. Page on with `after=next`."""
    items = await state.store.steps(_run_id(run_id), after=after, limit=limit, agent_id=agent)
    return StepPage(items=items, next=items[-1].seq if len(items) == limit else None)


@router.get("/runs/{run_id}/activity", tags=["events"])
async def list_activity(
    run_id: str,
    principal: Viewer,
    state: State,
    execs: Annotated[list[str], Query(alias="exec")],
) -> list[ExecActivity]:
    """What each exec left in the log, per event type and file op, in the order asked. Execs
    with no events are left out."""
    return await state.store.activity(_run_id(run_id), _execs(execs))


class SamplePage(Out):
    items: list[SampleWindow]
    next: int | None = None


@router.get("/runs/{run_id}/samples", tags=["events"])
async def list_samples(
    run_id: str,
    principal: Viewer,
    state: State,
    agent: str | None = None,
    ts_from: int | None = None,
    ts_to: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
) -> SamplePage:
    """The resource timeline, one window per sandbox second. Page on with `ts_from=next`."""
    items = await state.store.samples(
        _run_id(run_id), agent_id=agent, ts_from=ts_from, ts_to=ts_to, limit=limit
    )
    return SamplePage(items=items, next=items[-1].ts_to + 1 if len(items) == limit else None)


@router.get(
    "/runs/{run_id}/blobs/{sha256}",
    tags=["events"],
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}, **responses(404)},
)
async def get_blob(
    run_id: str,
    sha256: str,
    principal: Viewer,
    state: State,
    accept_encoding: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """A blob's bytes, never rendered: always an attachment of type octet-stream."""
    await _shown(state, principal, _run_id(run_id))
    try:
        path = state.store.blob_path(_run_id(run_id), sha256)
    except KeyError, BlobError:
        raise ProblemException(404, "Not found", f"no blob {sha256}") from None
    headers = {
        "ETag": f'"{sha256}"',
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": f'attachment; filename="{sha256}"',
    }
    if if_none_match is not None and sha256 in if_none_match:
        return Response(status_code=304, headers=headers)
    with path.open("rb") as f:
        compressed = f.read(4) == ZSTD_MAGIC
    if not compressed:
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
    wants = {p.split(";")[0].strip().lower() for p in (accept_encoding or "").split(",")}
    if "zstd" in wants:
        headers["Content-Encoding"] = "zstd"
        headers["Vary"] = "Accept-Encoding"
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
    headers["Vary"] = "Accept-Encoding"
    return StreamingResponse(
        _decompressed(path), media_type="application/octet-stream", headers=headers
    )


async def _decompressed(path: Path) -> AsyncIterator[bytes]:
    def chunks() -> Iterator[bytes]:
        frame = zstd.ZstdDecompressor()
        with path.open("rb") as f:
            while data := f.read(256 * 1024):
                while True:
                    if frame.eof:
                        rest = frame.unused_data
                        frame = zstd.ZstdDecompressor()
                        data = rest + data
                        if not data:
                            break
                    out = frame.decompress(data, max_length=1024 * 1024)
                    data = b""
                    if out:
                        yield out
                    if frame.needs_input and not frame.eof:
                        break

    iterator = chunks()

    def step() -> bytes | None:
        return next(iterator, None)

    while (chunk := await asyncio.to_thread(step)) is not None:
        yield chunk


class VerificationOut(Out):
    run_id: str
    ok: bool
    events: int
    head: str
    finished: bool
    error: str | None
    missing_blobs: list[str]
    bad_blobs: list[str]


@router.get("/runs/{run_id}/verification", tags=["events"], responses=responses(404))
async def verify_run(run_id: str, principal: Viewer, state: State) -> VerificationOut:
    """Check the hash chain of the control plane's copy and every blob it references."""
    if await state.store.run(_run_id(run_id)) is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    v = await state.store.verify(run_id)
    return VerificationOut(
        run_id=v.run_id,
        ok=v.ok,
        events=v.events,
        head=v.head,
        finished=v.finished,
        error=v.error,
        missing_blobs=list(v.missing_blobs),
        bad_blobs=list(v.bad_blobs),
    )


@router.post(
    "/runs/import",
    status_code=201,
    tags=["runs"],
    openapi_extra={"requestBody": {"content": {"application/x-tar": {}}, "required": True}},
    responses=responses(409, 413),
)
async def import_run(request: Request, principal: Uploader, state: State) -> Started:
    """Take a run directory as a tar (`events.jsonl` and `blobs/`), checked like ingest."""
    with tempfile.TemporaryFile() as tmp:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > IMPORT_LIMIT:
                raise ProblemException(413, "Too large", f"an import is at most {IMPORT_LIMIT}")
            await asyncio.to_thread(tmp.write, chunk)
        try:
            log = await asyncio.to_thread(_tar_log, tmp)
        except tarfile.TarError as e:
            raise ProblemException(422, "Invalid request", f"not a tar: {e}") from None
        lines = split_lines(log)
        if not lines:
            raise ProblemException(422, "Invalid request", "the tar holds no events.jsonl")
        try:
            run_id = str(json.loads(lines[0])["run_id"])
            wanted = {sha for line in lines for sha in blob_refs(json.loads(line)["data"])}
        except ValueError, KeyError, TypeError:
            raise ProblemException(422, "Invalid request", "a line is not an event") from None
        run_id = _run_id(run_id)
        meta = await state.db.run_meta(run_id)
        if meta is not None and meta.runner is not None and meta.source != "import":
            raise ProblemException(409, "Conflict", f"run {run_id} is shipped by {meta.runner}")
        state.store.prepare_run(run_id)
        await state.db.attribute(run_id, f"import:{principal.name}", "import", principal.name)
        try:
            await asyncio.to_thread(_tar_blobs, tmp, state.store.blobs(run_id), wanted)
        except (BlobError, tarfile.TarError) as e:
            raise ProblemException(422, "Invalid request", str(e), code="blob") from None
    try:
        for i in range(0, len(lines), MAX_PAGE):
            await state.store.ingest(run_id, lines[i : i + MAX_PAGE])
    except MissingBlobs as e:
        raise ProblemException(
            422, "Blobs missing", str(e), code="missing_blobs", missing=e.missing
        ) from None
    except (IngestError, RunCorrupt) as e:
        raise ProblemException(409, "Conflict", str(e), code="ingest") from None
    return Started(run_ids=[run_id])


def _tar_log(file: IO[bytes]) -> bytes:
    file.seek(0)
    with tarfile.open(fileobj=file, mode="r:*") as tar:
        for member in tar:
            f = tar.extractfile(member) if member.isfile() else None
            if f is not None and Path(member.name).name == "events.jsonl":
                return f.read()
    return b""


def _tar_blobs(file: IO[bytes], blobs: BlobStore, wanted: set[str]) -> None:
    """Copy the blobs the log names from the tar into the store, checked as they stream."""
    file.seek(0)
    with tarfile.open(fileobj=file, mode="r:*") as tar:
        for member in tar:
            parts = Path(member.name).parts
            sha = parts[-1] if parts else ""
            if not member.isfile() or len(parts) < 2 or parts[-2] != "blobs" or sha not in wanted:
                continue
            f = tar.extractfile(member)
            if f is None or blobs.path(sha).is_file():
                continue
            writer = blobs.writer(sha, limit=IMPORT_LIMIT)
            try:
                while chunk := f.read(1024 * 1024):
                    writer.write(chunk)
                writer.commit()
            finally:
                writer.abort()


# Models, prompts, runners


class CheckLine(Out):
    name: str
    verdict: Verdict
    detail: str


class ProfileCheck(Out):
    """The newest model check of a profile: its run, when it started, the run's status and
    each smoke test; `checks` is empty while it runs."""

    run_id: str
    started_at: str
    status: str
    checks: list[CheckLine]


class ProfileInfo(Out):
    name: str
    backend: str
    model: str
    weights: str
    reasoning: str
    roles: list[str]
    envs: list[str]
    runners: list[str]
    check: ProfileCheck | None = None


class ModelsInfo(Out):
    profiles: list[ProfileInfo]
    roles: dict[str, str]


@router.get("/models", tags=["models"])
async def list_models(principal: Viewer, state: State) -> ModelsInfo:
    """Profiles, their roles, which runners have the variables each one reads, and each
    profile's newest model check."""
    models = state.config.settings().models
    roles: dict[str, list[str]] = {}
    for role, name in models.roles.model_dump().items():
        roles.setdefault(name, []).append(role)
    checks = await _checks(state, set(models.profiles))
    out = []
    for name, p in sorted(models.profiles.items()):
        envs = [e for e in (p.api_key_env, p.base_url_env) if e]
        ready = [c.name for c in state.runners.values() if set(envs) <= set(c.envs)]
        out.append(
            ProfileInfo(
                name=name,
                backend=p.backend,
                model=p.model,
                weights=p.weights,
                reasoning=p.reasoning,
                roles=roles.get(name, []),
                envs=envs,
                runners=sorted(ready),
                check=checks.get(name),
            )
        )
    return ModelsInfo(profiles=out, roles=models.roles.model_dump())


CHECK_RUNS = 100


async def _checks(state: ServerState, wanted: set[str]) -> dict[str, ProfileCheck]:
    """The newest check of each profile in `wanted`, among the last CHECK_RUNS model checks."""
    found: dict[str, ProfileCheck] = {}
    for run in await state.store.runs(kind="models.check", limit=CHECK_RUNS):
        if not wanted - found.keys():
            break
        events = await state.store.events(
            run.run_id, types=["run.started", "model.checked"], limit=1000
        )
        if not events or events[0].type != "run.started":
            continue
        config = events[0].data.get("config")
        profiles = config.get("models") if isinstance(config, dict) else None
        for name in profiles if isinstance(profiles, dict) else ():
            if name not in wanted or name in found:
                continue
            found[name] = ProfileCheck(
                run_id=run.run_id,
                started_at=run.started_at.isoformat(),
                status=run.status,
                checks=[
                    CheckLine(
                        name=str(e.data["name"]),
                        verdict=cast(Verdict, e.data["verdict"]),
                        detail=str(e.data.get("detail", "")),
                    )
                    for e in events
                    if e.type == "model.checked" and e.data.get("profile") == name
                ],
            )
    return found


class ModelsCheck(BaseModel):
    profiles: list[str] = Field(default_factory=list)
    runner: str | None = None


@router.post("/models/check", status_code=202, tags=["models"])
async def check_models(body: ModelsCheck, principal: Operator, state: State) -> Started:
    """Small requests to each profile's provider, run on a runner and recorded as a run."""
    models = state.config.settings().models
    try:
        chosen = [models.get(n) for n in body.profiles] or [models.role("agent")]
    except ProfileError as e:
        raise ProblemException(422, "Invalid request", str(e), code="profile") from None
    job = Job(
        kind="models.check",
        run_id=str(uuid.uuid7()),
        models={p.name: p for p in chosen},
        check=[p.name for p in chosen],
    )
    return await _queue(state, principal.name, [job], runner=body.runner, parallel=None, blobs={})


class Text(Out):
    text: str
    source: Literal["default", "user", "project"]


class PromptsInfo(Out):
    """`agent` and `tools` are what the agent reads from the harness, `monitor` the monitor's
    own texts (`[prompts.monitor]`) and `categories` its category definitions
    (`[prompts.monitor.categories]`)."""

    agent: dict[str, Text]
    tools: dict[str, dict[str, Text]]
    monitor: dict[str, Text]
    categories: dict[str, Text]


def _monitor_source(
    user: Prompts, project: Prompts, name: str, category: str | None = None
) -> Literal["default", "user", "project"]:
    for layer, label in ((project, "project"), (user, "user")):
        value = layer.monitor.get(name)
        if category is None and isinstance(value, str):
            return label
        if category is not None and isinstance(value, dict) and category in value:
            return label
    return "default"


@router.get("/prompts", tags=["prompts"])
async def list_prompts(principal: Viewer, state: State) -> PromptsInfo:
    """Every text AIMS writes for the agent or its monitor, and the layer it comes from."""
    settings = state.config.settings()
    found = settings.layers
    user = load_prompts(found.user)
    project = load_prompts(found.project) if found.project is not None else Prompts()
    agent = settings.prompts.agent_texts().model_dump()
    resolved = monitor_texts.resolve(settings.prompts)

    def source(key: str) -> Literal["default", "user", "project"]:
        if getattr(project, key) is not None:
            return "project"
        if getattr(user, key) is not None:
            return "user"
        return "default"

    tools: dict[str, dict[str, Text]] = {}
    for name, cls in sorted(registry().items()):
        texts = cls(settings.prompts.tools.get(name)).text
        tools[name] = {
            k: Text(
                text=v,
                source="project"
                if k in project.tools.get(name, {})
                else "user"
                if k in user.tools.get(name, {})
                else "default",
            )
            for k, v in texts.items()
        }
    return PromptsInfo(
        agent={k: Text(text=v, source=source(k)) for k, v in agent.items()},
        tools=tools,
        monitor={
            k: Text(text=v, source=_monitor_source(user, project, k))
            for k, v in sorted(resolved.texts.items())
        },
        categories={
            str(c): Text(text=v, source=_monitor_source(user, project, "categories", str(c)))
            for c, v in resolved.categories.items()
        },
    )


@router.get("/runners", tags=["runners"])
async def list_runners(principal: Viewer, state: State) -> list[RunnerInfo]:
    out = {name: c.info() for name, c in state.runners.items()}
    for r in await state.db.runners():
        if r.name not in out:
            out[r.name] = RunnerInfo(
                name=r.name,
                version=r.info.get("version"),
                arch=r.info.get("arch"),
                seen_s=round(time.time() - r.seen, 1),
                kinds=r.info.get("kinds") or list(BASE_KINDS),
            )
    return sorted(out.values(), key=lambda r: r.name)


# Scenarios


class ScenarioInfo(Out):
    """`oversight` is what the scenario lets its monitor do, `agents` how many agents it has,
    `profiles` the profiles they run unless a run picks one, and `variants` the names of its
    variants; all are missing when it does not load."""

    source: str
    name: str
    title: str | None = None
    writable: bool
    error: str | None = None
    oversight: Oversight | None = None
    agents: int | None = None
    profiles: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)


@router.get("/scenarios", tags=["scenarios"])
async def list_scenarios(
    principal: Viewer, state: State, sources: SourcesDep
) -> list[ScenarioInfo]:
    default = state.config.settings().models.roles.agent
    out = []
    for s in sources.all():
        if not s.root.is_dir():
            continue
        for d in sorted(s.root.iterdir()):
            if not (d / "scenario.yaml").is_file():
                continue
            info = ScenarioInfo(source=s.id, name=d.name, writable=s.writable)
            try:
                sc = parse((d / "scenario.yaml").read_text(), source=d.name)
            except (ScenarioError, OSError, UnicodeDecodeError) as e:
                info.error = str(e).splitlines()[0]
            else:
                info.title = sc.name
                info.oversight = sc.oversight
                info.agents = len(sc.agents)
                info.profiles = sorted({a.profile or default for a in sc.agents})
                info.variants = list(sc.variants)
            out.append(info)
    return out


@router.get("/scenarios/schema", tags=["scenarios"])
async def scenario_schema(principal: Viewer) -> dict[str, object]:
    return Scenario.model_json_schema()


class ScenarioCheck(BaseModel):
    text: str = Field(max_length=1024 * 1024)
    profile: str | None = None


class CheckResult(Out):
    ok: bool
    errors: list[ProblemError]
    warnings: list[ProblemError]


@router.post("/scenarios/check", tags=["scenarios"])
async def check_scenario(body: ScenarioCheck, principal: Viewer, state: State) -> CheckResult:
    """Validate scenario YAML: the schema with YAML lines, then the profiles, tools and texts
    it names as warnings."""
    try:
        scenario = parse(body.text, source="scenario.yaml")
    except ScenarioError as e:
        return CheckResult(ok=False, errors=scenario_problems(e), warnings=[])
    settings = state.config.settings()
    bundle = Bundle(scenario, body.text.encode())
    warnings = check(bundle, settings.models, body.profile, settings.prompts)
    return CheckResult(ok=True, errors=[], warnings=[ProblemError(msg=w) for w in warnings])


# Tokens


class TokenInfo(Out):
    name: str
    role: str
    created: float
    revoked: float | None


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._@-]+$")
    role: Literal["viewer", "operator", "runner"]


class TokenCreated(Out):
    name: str
    role: str
    token: str


@router.get("/tokens", tags=["tokens"])
async def list_tokens(principal: Operator, state: State) -> list[TokenInfo]:
    return [
        TokenInfo(name=t.name, role=t.role, created=t.created, revoked=t.revoked)
        for t in await state.db.tokens()
    ]


@router.post("/tokens", status_code=201, tags=["tokens"], responses=responses(409))
async def create_token(body: TokenCreate, principal: Operator, state: State) -> TokenCreated:
    """A new named token. Its value is shown this once."""
    try:
        secret = await state.db.create_token(body.name, body.role)
    except ValueError as e:
        raise ProblemException(409, "Conflict", str(e)) from None
    return TokenCreated(name=body.name, role=body.role, token=secret)


@router.delete("/tokens/{name}", status_code=204, tags=["tokens"], responses=responses(404))
async def revoke_token(name: str, principal: Operator, state: State) -> None:
    """Revoke a token and end every session made with it."""
    if not await state.db.revoke_token(name):
        raise ProblemException(404, "Not found", f"no live token {name}")
