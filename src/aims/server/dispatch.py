"""Hands queued jobs to runners: the one a job names, else the local runner, else the least
loaded one that can take it. An offer not claimed in 10 s goes to the next runner. As it starts
and about once a day it also drops what the server no longer keeps."""

import asyncio
import contextlib
import logging
import time

from aims.server import audit, retention
from aims.server.db import Job
from aims.server.runner_api import send
from aims.server.series import settle
from aims.server.state import Connected, ServerState
from aims.wire import CLAIM_S, OfferMessage

log = logging.getLogger("aims.server")

TICK_S = 1.0
HOUSEKEEPING_S = 86400.0


def can_take(conn: Connected, job: Job) -> bool:
    if conn.streams <= 0 or conn.stale or conn.free() <= 0:
        return False
    needs = job.needs
    kind = needs.get("kind")
    if kind is not None and kind not in conn.hello.kinds:
        return False
    if needs.get("kernel") and not conn.tracer_alive:
        return False
    envs = needs.get("envs") or []
    return set(envs) <= set(conn.envs)


def pick(state: ServerState, job: Job) -> Connected | None:
    runners = list(state.runners.values())
    if job.target is not None:
        conn = state.runners.get(job.target)
        return conn if conn is not None and can_take(conn, job) else None
    able = [c for c in runners if can_take(c, job)]
    if not able:
        return None
    fresh = [c for c in able if c.name not in job.tried] or able
    local = [c for c in fresh if c.local]
    if local:
        return local[0]
    return max(fresh, key=lambda c: (c.free(), c.name))


async def dispatch(state: ServerState) -> None:
    db = state.db
    await settle(state)
    now = time.time()
    open_jobs = await db.jobs(("queued", "offered"))
    for job in open_jobs:
        if job.state != "offered":
            continue
        conn = state.runners.get(job.runner or "")
        lapsed = job.offered_at is None or now - job.offered_at > CLAIM_S
        gone = conn is None or conn.streams <= 0
        if (lapsed or gone) and await db.requeue(job.run_id):
            if conn is not None:
                conn.offered.discard(job.run_id)
            state.changes.touch(job.run_id)
    active: dict[str, int] = {}
    for job in await db.jobs(("queued",)):
        if job.group is not None and job.parallel is not None:
            if job.group not in active:
                active[job.group] = await db.group_active(job.group)
            if active[job.group] >= job.parallel:
                continue
        conn = pick(state, job)
        if conn is None:
            continue
        if not await db.offer(job.run_id, conn.name):
            continue
        conn.offered.add(job.run_id)
        if job.group is not None:
            active[job.group] = active.get(job.group, 0) + 1
        if not send(state, conn.name, OfferMessage(run_id=job.run_id, needs=job.needs)):
            await db.requeue(job.run_id)
            conn.offered.discard(job.run_id)
        state.changes.touch(job.run_id)


async def housekeeping(state: ServerState) -> None:
    """Drop expired sessions and audit rows past `[audit] keep_days`. With `keep_days`, also
    delete the runs that ended longer ago."""
    if pruned := await state.db.prune_sessions():
        log.info("pruned %d expired session(s)", pruned)
    await audit.prune(state.db, state.config.layers)
    if state.keep_days is not None and (gone := await retention.prune(state, state.keep_days)):
        detail = f"ended over {state.keep_days:g} days ago: {audit.runs(gone)}"
        await audit.record(state.db, "run.prune", detail=detail)


async def _keep(state: ServerState) -> None:
    try:
        await housekeeping(state)
    except Exception:
        log.exception("housekeeping failed")


async def run(state: ServerState) -> None:
    """Dispatch every tick. Housekeeping runs beside it, so deleting old runs never holds up
    the queue."""
    kept = time.monotonic() - HOUSEKEEPING_S
    keeping: asyncio.Task[None] | None = None
    try:
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(TICK_S):
                    await state.wake.wait()
            state.wake.clear()
            try:
                await dispatch(state)
            except Exception:
                log.exception("dispatch failed")
            idle = keeping is None or keeping.done()
            if idle and time.monotonic() - kept >= HOUSEKEEPING_S:
                kept = time.monotonic()
                keeping = asyncio.create_task(_keep(state))
    finally:
        if keeping is not None:
            keeping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeping
