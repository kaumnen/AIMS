"""A series queued on the control plane: all its runs at once, each after the first waiting
until the run before it completed and its truth file arrived, the rest cancelled when one ends
otherwise. The dispatcher settles the waiting runs on every tick, so a restart loses nothing."""

from dataclasses import dataclass
from typing import Literal

from aims.events import Series
from aims.run.series import last_of_series
from aims.server.db import OPEN_STATES, Job
from aims.server.state import ServerState

MAX_RUNS = 1000


@dataclass(frozen=True, slots=True)
class Place:
    """Where a series goes on: the index its next run takes, the completed run it starts from
    (None for a new series), and a run of it not ended yet, which a new start waits for."""

    next_index: int
    previous: str | None
    busy: str | None


def place_of(job: Job) -> Series | None:
    """The job's place in its series, None for a job of no series."""
    options = job.payload.get("options")
    raw = options.get("series") if isinstance(options, dict) else None
    return Series.model_validate(raw) if isinstance(raw, dict) else None


async def series_jobs(state: ServerState, name: str) -> list[Job]:
    return await state.db.jobs_page(before=None, limit=MAX_RUNS, series=name)


async def where(state: ServerState, name: str) -> Place:
    """Where series `name` goes on, with the runs pushed to the store and the jobs queued
    here; indexes are never taken twice."""
    jobs = await series_jobs(state, name)
    indexes = [p.index for j in jobs if (p := place_of(j)) is not None]
    found = await last_of_series(state.store, name, indexes)
    last, next_index = found if found is not None else (None, 0)
    open_jobs = sorted(
        (p.index, j.run_id)
        for j in jobs
        if j.state in OPEN_STATES and (p := place_of(j)) is not None
    )
    running = sorted(
        (i.series_index or 0, i.run_id)
        for i in await state.store.runs(series=name, kind="scenario", limit=MAX_RUNS)
        if i.status == "running"
    )
    busy = (open_jobs or running or [(0, None)])[0][1]
    return Place(next_index, None if last is None else last.run_id, busy)


Verdict = Literal["go", "wait", "stop"]


async def _after(state: ServerState, previous: str, index: int) -> tuple[Verdict, str | None]:
    """Whether the run after run `previous`, the `index`th of its series, may start, and why it
    waits or stops. Reasons lead with the index: the runs of a series share their short ids."""
    info = await state.store.run(previous)
    if info is None:
        job = await state.db.job(previous)
        if job is None:
            return "stop", "is gone"
        if job.state == "cancelled":
            return "stop", "cancelled"
        if job.state == "done":
            return "stop", "did not start"
        return "wait", None
    if info.status == "running":
        return "wait", None
    if info.status != "completed":
        return "stop", info.status
    if state.store.truth_path(previous).is_file():
        return "go", None
    first = await state.store.events(previous, types=["run.started"], limit=1)
    if first and first[0].data.get("truth") is None:
        return "go", None
    return "wait", f"waits for the truth file of #{index} (run {previous})"


async def settle(state: ServerState) -> None:
    """Queue each waiting run whose previous run completed with its truth file here; cancel
    it, and so the ones after it, when that run ended any other way."""
    for job in await state.db.jobs(("waiting",)):
        place = place_of(job)
        previous = None if place is None else place.previous
        if place is None or previous is None:
            verdict, why = "go", None
        else:
            verdict, why = await _after(state, previous, place.index - 1)
        changed = False
        if verdict == "go":
            changed = await state.db.release(job.run_id)
        elif verdict == "stop" and place is not None:
            reason = f"series stopped: #{place.index - 1} {why} (run {previous})"
            changed = await state.db.cancel_queued(job.run_id, reason)
        elif why is not None and why != job.reason:
            changed = await state.db.note_waiting(job.run_id, why)
        if changed:
            state.changes.touch(job.run_id)


def waiting_reason(job: Job) -> str:
    place = place_of(job)
    if place is None or place.previous is None:
        return "waits for the run before it"
    return f"waits for #{place.index - 1} (run {place.previous})"
