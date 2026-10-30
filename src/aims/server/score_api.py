"""Scoring on `/api`: queue a score of a sealed run, read the report over scored runs, and label
monitor flags for review. The same code as `aims score`, `aims report` and `aims label`."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aims.events import Label, LabelKind
from aims.models import ProfileError
from aims.monitor.offline import origin_agents
from aims.monitor.setup import extractor_for
from aims.monitor.texts import resolve
from aims.prompts import TextError
from aims.run.fork import load_origin
from aims.run.report import Report, gather
from aims.run.runner import RunError
from aims.run.scoring import CLASSIFIERS
from aims.server.api import Operator, Started, State, Viewer, _queue, _run_id
from aims.server.problems import ProblemException, responses
from aims.wire import ScoreJob

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))


class StartScore(BaseModel):
    """`monitor` scores the flags of that monitor run over the run instead of its own.
    `awareness` asks a cheap model whether each agent said it thought it was tested."""

    monitor: str | None = None
    awareness: bool = True
    runner: str | None = None


@router.post("/runs/{run_id}/score", status_code=202, tags=["score"], responses=responses(404, 409))
async def score_run(run_id: str, body: StartScore, principal: Operator, state: State) -> Started:
    """Queue a score of a sealed run against its truth file. A runner fetches the run, its
    monitor run and its truth file, and writes the score to a new run of kind `score` whose
    `origin` is this run; the run itself never changes."""
    run_id = _run_id(run_id)
    info = await state.store.run(run_id)
    head = await state.store.head(run_id) if info is not None else None
    if info is None or head is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
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
    settings = state.config.settings()
    try:
        origin = await load_origin(state.store, run_id, full=False)
        classifier = None
        if body.awareness:
            classifier = extractor_for(settings.models, CLASSIFIERS, origin_agents(origin))
        texts = resolve(settings.prompts)
    except (RunError, ProfileError, TextError) as e:
        raise ProblemException(422, "Cannot score", str(e), code="score") from None
    job = ScoreJob(
        run_id=str(uuid.uuid7()),
        origin=run_id,
        origin_head=head.hash,
        scenario=info.scenario,
        monitor=body.monitor,
        classifier=None if classifier is None else classifier.snapshot(),
        texts=texts.snapshot(),
        config_files=settings.layers.digests(),
    )
    return await _queue(state, principal, [job], runner=body.runner, parallel=None)


@router.get("/report", tags=["score"])
async def get_report(
    principal: Operator,
    state: State,
    runs: Annotated[list[str] | None, Query()] = None,
    series: str | None = None,
    group: str | None = None,
) -> Report:
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
    flag: int = Field(ge=0)
    label: LabelKind
    note: str = Field(default="", max_length=4000)


@router.get("/runs/{run_id}/labels", tags=["score"], responses=responses(404))
async def get_labels(run_id: str, principal: Viewer, state: State) -> list[Label]:
    """The latest label of each labelled flag of the run, by flag seq."""
    if await state.store.run(_run_id(run_id)) is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    return list((await state.store.labels(run_id)).values())


@router.post("/runs/{run_id}/labels", status_code=201, tags=["score"], responses=responses(404))
async def post_label(run_id: str, body: LabelBody, principal: Operator, state: State) -> Label:
    """Label a monitor flag of the run for review, beside the run's log; the newest counts."""
    if await state.store.run(_run_id(run_id)) is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    try:
        made = await state.store.add_label(
            run_id, body.flag, body.label, by=principal.name, note=body.note
        )
    except KeyError as e:
        raise ProblemException(404, "Not found", str(e.args[0] if e.args else e)) from None
    state.changes.touch(run_id)
    return made
