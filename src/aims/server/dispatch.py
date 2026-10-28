"""Hands queued jobs to runners: the one a job names, else the local runner, else the least
loaded one that can take it. An offer not claimed in 10 s goes to the next runner."""

import asyncio
import contextlib
import logging
import time

from aims.server.db import Job
from aims.server.runner_api import send
from aims.server.state import Connected, ServerState
from aims.wire import CLAIM_S, OfferMessage

log = logging.getLogger("aims.server")

TICK_S = 1.0


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


async def run(state: ServerState) -> None:
    while True:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(TICK_S):
                await state.wake.wait()
        state.wake.clear()
        try:
            await dispatch(state)
        except Exception:
            log.exception("dispatch failed")
