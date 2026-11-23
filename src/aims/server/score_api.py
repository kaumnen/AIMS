"""Scoring on `/api`: queue a score of a sealed run, read the rates over scored runs, and label
monitor flags for review. The same code as `aims score`, `aims rates` and `aims label`."""

import asyncio
import dataclasses
import logging
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aims.config import Settings
from aims.events import Label, LabelKind
from aims.models import ProfileError
from aims.models.listprice import Listing
from aims.monitor.offline import origin_agents
from aims.monitor.setup import extractor_for
from aims.monitor.texts import resolve
from aims.prompts import TextError
from aims.run.fork import load_origin
from aims.run.rates import Rates, gather
from aims.run.runner import RunError
from aims.run.score import CheckError, rule_plants
from aims.run.scoring import (
    ScoreError,
    ScoreEstimate,
    checks_off_for,
    classifier_candidates,
    estimate_sealed,
    priced_score,
    scores_confirm,
    unfinished,
)
from aims.server import audit
from aims.server import db as sdb
from aims.server.api import (
    Operator,
    ScenarioRef,
    Seq,
    SourcesDep,
    Started,
    State,
    Viewer,
    _no_run,
    _queue,
    _run_id,
    _shown,
)
from aims.server.files import Sources
from aims.server.problems import ProblemException, responses
from aims.server.state import ServerState
from aims.text import Utf8
from aims.wire import ScoreJob

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))
log = logging.getLogger("aims.server")


class StartScore(BaseModel):
    """`monitor` scores the flags of that monitor run over the run instead of its own.
    `awareness` asks a cheap model whether each agent said it thought it was tested; `answer`
    whether each subject that took a plant told the operator and gave true reasons.
    `plants_from` also scores the rule plants of that scenario the run's truth file lacks, in
    the run's variant, with the plant facts it lacks. The checks stop at `budget_usd`, else at
    `[score] budget_usd`. When the estimate is above `confirm_above_usd`, or nothing bounds the
    cost, the score starts only with `confirm`, like `aims score --yes`."""

    monitor: Utf8 | None = None
    awareness: bool = True
    answer: bool = True
    plants_from: ScenarioRef | None = None
    runner: Utf8 | None = None
    budget_usd: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)
    confirm: bool = False


@router.post(
    "/runs/{run_id}/score", status_code=202, tags=["score"], responses=responses(404, 409, 503)
)
@audit.audited("run.score")
async def score_run(
    run_id: str,
    body: StartScore,
    principal: Operator,
    state: State,
    sources: SourcesDep,
    note: audit.Note,
) -> Started:
    """Queue a score of a sealed run against its truth file. A runner fetches the run, its
    monitor run and its truth file, and writes the score to a new run of kind `score` whose
    `origin` is this run; the run itself never changes."""
    run_id = _run_id(run_id)
    job, settings, listing = await score_job(state, run_id, body, sources)
    if not body.confirm:
        try:
            cost = await estimate_sealed(
                state.store,
                settings,
                run_id,
                awareness=body.awareness,
                answer=body.answer,
                listing=listing,
                budget_usd=job.budget_usd,
            )
        except (ScoreError, ProfileError, TextError) as e:
            raise ProblemException(422, "Cannot score", str(e), code="score") from None
        why = scores_confirm(cost)
        if why is not None:
            raise ProblemException(
                409, "Confirm the cost", f"{why}; set confirm to start anyway", code="confirm"
            )
    started = await _queue(state, principal.name, [job], runner=body.runner, parallel=None)
    note.detail = audit.runs(started.run_ids)
    return started


# How long a score that a run's job asked for waits for the index to hold the run, as while
# another process rebuilds the index. INDEXED_POLL_S is how often it looks. The runner's upload
# of the truth file waits as long; its client gives up after 60 s.
INDEXED_WAIT_S = 30.0
INDEXED_POLL_S = 0.5


async def score_after(state: ServerState, run_id: str) -> None:
    """Queue a score of a run whose job asked for one, now that its truth file is here, as
    `aims run --score` does; a cancelled run is not scored. While the run's log is here and the
    index does not hold it, the score waits up to INDEXED_WAIT_S. A score that cannot queue is
    kept as a score job that ended before it started, with the reason."""
    job = await state.db.job(run_id)
    if job is None or not job.payload.get("options", {}).get("score"):
        return
    end = time.monotonic() + INDEXED_WAIT_S
    while True:
        info = await state.store.run(run_id)
        if info is None and not state.store.logged(run_id):
            return
        if info is not None and info.status == "cancelled":
            return
        try:
            if info is None:
                raise _no_run(state, run_id)
            await queue_score(state, job.token, run_id, StartScore())
            return
        except ProblemException as e:
            late = time.monotonic() >= end or state.stopping.is_set()
            if e.problem.code != "indexing" or late:
                scenario = info.scenario if info is not None else _scenario_name(job)
                await _unqueued(state, job, run_id, scenario, e)
                return
        except Exception:
            log.exception("scoring run %s failed", run_id)
            return
        await asyncio.sleep(INDEXED_POLL_S)


async def _unqueued(
    state: ServerState, job: sdb.Job, run_id: str, scenario: str | None, e: ProblemException
) -> None:
    """Keep a score of run `run_id` that could not queue as a score job that ended before it
    started, with the reason."""
    log.warning("run %s was not scored: %s", run_id, e)
    now = time.time()
    row = sdb.Job(
        run_id=str(uuid.uuid7()),
        group=None,
        parallel=None,
        state="done",
        payload={"kind": "score", "origin": run_id, "scenario": scenario},
        needs={},
        target=None,
        runner=None,
        offered_at=None,
        tried=[],
        token=job.token,
        created=now,
        updated=now,
        reason=str(e),
    )
    await state.db.add_jobs([row])
    state.touch_job(row)


def _scenario_name(job: sdb.Job) -> str | None:
    """The name of the scenario a job runs, from its payload."""
    snap = job.payload.get("scenario")
    name = snap.get("name") if isinstance(snap, dict) else snap
    return name if isinstance(name, str) else None


async def queue_score(
    state: ServerState,
    token: str | None,
    run_id: str,
    body: StartScore,
    sources: Sources | None = None,
) -> Started:
    """Queue the score `body` asks for with no question of its cost, as a run's own score."""
    job, _, _ = await score_job(state, run_id, body, sources)
    return await _queue(state, token, [job], runner=body.runner, parallel=None)


async def score_job(
    state: ServerState, run_id: str, body: StartScore, sources: Sources | None = None
) -> tuple[ScoreJob, Settings, Listing]:
    """The job of the score `body` asks for, checked, with the settings its classifier's list
    price is laid on and the listing it came from."""
    info = await state.store.run(run_id)
    head = await state.store.head(run_id) if info is not None else None
    if info is None or head is None:
        raise _no_run(state, run_id)
    if info.status == "running":
        raise ProblemException(409, "Conflict", "the run is not sealed yet", code="not_sealed")
    if info.kind != "scenario":
        raise ProblemException(
            422, "Invalid request", f"a {info.kind} run has no agents to score", code="kind"
        )
    if not state.store.truth_path(run_id).is_file():
        raise ProblemException(
            409, "Conflict", "the run's truth file has not reached this control plane", code="truth"
        )
    if body.monitor is not None:
        watched = await state.store.run(_run_id(body.monitor))
        if watched is None or watched.kind != "monitor" or watched.origin != run_id:
            raise ProblemException(
                422, "Invalid request", f"run {body.monitor} is not a monitor run of {run_id}"
            )
        if watched.status != "completed":
            raise ProblemException(
                409, "Conflict", unfinished(watched.run_id, watched.status), code="review"
            )
    added = None
    if body.plants_from is not None:
        if sources is None:
            raise ProblemException(422, "Cannot score", "no scenarios to take plants from")
        ref = body.plants_from
        _, path = sources.scenario(ref.source, ref.name)
        run_dir = state.store.truth_path(run_id).parent
        try:
            found = await asyncio.to_thread(
                rule_plants, path / "scenario.yaml", run_dir, info.scenario
            )
        except CheckError as e:
            raise ProblemException(422, "Cannot score", str(e), code="plants_from") from None
        added = dataclasses.replace(found, path=f"{ref.source}/{ref.name}")
    settings = state.config.settings()
    try:
        # the classifier's list price goes on it before the job is made, so the runner gets it
        settings, listing = await priced_score(
            state.store,
            settings,
            run_id,
            awareness=body.awareness,
            answer=body.answer,
            lists=state.prices,
        )
        origin = await load_origin(state.store, run_id, full=False, replay=False)
        classifier = off = None
        if body.awareness or body.answer:
            names = classifier_candidates(settings)
            classifier = extractor_for(settings.models, names, origin_agents(origin))
            if classifier is None:
                off = checks_off_for(
                    settings.models, names, awareness=body.awareness, answer=body.answer
                )
        texts = resolve(settings.prompts)
    except (ScoreError, RunError, ProfileError, TextError) as e:
        raise ProblemException(422, "Cannot score", str(e), code="score") from None
    job = ScoreJob(
        run_id=str(uuid.uuid7()),
        origin=run_id,
        origin_head=head.hash,
        scenario=info.scenario,
        monitor=body.monitor,
        classifier=None if classifier is None else classifier.snapshot(),
        checks_off=off,
        awareness=body.awareness,
        answer=body.answer,
        budget_usd=settings.score_budget_usd if body.budget_usd is None else body.budget_usd,
        added=None if added is None else added.dump(),
        texts=texts.snapshot(),
        config_files=settings.layers.digests(),
    )
    return job, settings, listing


@router.get("/runs/{run_id}/score/estimate", tags=["score"], responses=responses(404, 503))
async def estimate_score(
    run_id: str,
    principal: Operator,
    state: State,
    awareness: bool = True,
    answer: bool = True,
    budget_usd: Annotated[float | None, Query(ge=0, allow_inf_nan=False)] = None,
) -> ScoreEstimate:
    """What a score of the run costs, as `aims score` prints it before it starts: the most
    classifier calls its checks make, the typical and high cost of past scores and the most
    its spend cap and the output cap allow. `budget_usd` replaces `[score] budget_usd`.
    `confirm` says the score starts only with a yes."""
    run_id = _run_id(run_id)
    if await state.store.run(run_id) is None:
        raise _no_run(state, run_id)
    try:
        settings, listing = await priced_score(
            state.store,
            state.config.settings(),
            run_id,
            awareness=awareness,
            answer=answer,
            lists=state.prices,
        )
        return await estimate_sealed(
            state.store,
            settings,
            run_id,
            awareness=awareness,
            answer=answer,
            listing=listing,
            budget_usd=budget_usd,
        )
    except (ScoreError, ProfileError, TextError) as e:
        raise ProblemException(422, "Cannot estimate", str(e), code="estimate") from None


@router.get("/rates", tags=["score"])
async def get_rates(
    principal: Operator,
    state: State,
    runs: Annotated[list[str] | None, Query()] = None,
    series: str | None = None,
    group: str | None = None,
) -> Rates:
    """How the monitor did over the newest score of each run: of `runs`, of the runs of
    `series` or the samples of `group`, or of every scored run when none is given."""
    origins: list[str] | None = None
    if runs or series or group:
        picked = set(runs or [])
        if series is not None:
            picked |= {i.run_id for i in await state.store.runs(series=series, limit=1000)}
        if group is not None:
            picked |= {i.run_id for i in await state.store.runs(group=group, limit=1000)}
        origins = []
        for run_id in sorted(picked):
            info = await state.store.run(run_id)
            origins.append(info.origin if info and info.kind == "score" and info.origin else run_id)
    return await gather(state.store, origins)


class LabelBody(BaseModel):
    flag: Seq
    label: LabelKind
    note: Utf8 = Field(default="", max_length=4000)


@router.get("/runs/{run_id}/labels", tags=["score"], responses=responses(404, 503))
async def get_labels(run_id: str, principal: Viewer, state: State) -> list[Label]:
    """The latest label of each labelled flag of the run, by flag seq."""
    await _shown(state, principal, _run_id(run_id))
    if await state.store.run(run_id) is None:
        raise _no_run(state, run_id)
    return list((await state.store.labels(run_id)).values())


@router.post("/runs/{run_id}/labels", status_code=201, tags=["score"], responses=responses(404))
@audit.audited("run.label")
async def post_label(
    run_id: str, body: LabelBody, principal: Operator, state: State, note: audit.Note
) -> Label:
    """Label a monitor flag of the run for review, beside the run's log; the newest counts."""
    note.detail = f"flag {body.flag} {body.label}"
    # a rebuild in another process empties the index for a while; the run is still here
    if await state.store.run(_run_id(run_id)) is None and not state.store.holds(run_id):
        raise ProblemException(404, "Not found", f"no run {run_id}")
    try:
        made = await state.store.add_label(
            run_id, body.flag, body.label, by=principal.name, note=body.note
        )
    except KeyError as e:
        raise ProblemException(404, "Not found", str(e.args[0] if e.args else e)) from None
    state.changes.touch(run_id)
    return made
