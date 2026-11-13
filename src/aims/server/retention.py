"""Deleting runs from the control plane. A run goes with every run made from it, at any depth:
its monitor runs, its scores and the self-test cases of a self-test. With `aims ui --keep-days`
the runs that ended longer ago go too. A run in use stays: one still running, or one that a
queued or active job runs or reads. A run marked running whose runner is offline looks
abandoned: only a forced delete takes it. A deleted run stays deleted, so a runner that still
holds it is refused and drops its copy."""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aims.events import RunCorrupt
from aims.events.chain import ChainError, Head
from aims.events.schema import format_ts
from aims.server.state import ServerState

log = logging.getLogger("aims.server")

REMOVE_TRIES = 50
REMOVE_WAIT_S = 0.1


@dataclass(frozen=True, slots=True)
class Family:
    """Run `run_id` and every run made from it, each with its kind, the run first. `blocked`
    says why a delete is refused now, even forced. `offline` says why it needs force: a run of
    it is marked running, or a job of it held, by a runner that is not connected. `indexing`
    says why `runs` may lack some: a run of it, or one made from it, has a log here the index
    does not hold yet, as while another process rebuilds the index."""

    run_id: str
    runs: dict[str, str]
    blocked: str | None
    offline: str | None = None
    indexing: str | None = None


def reads(payload: Mapping[str, Any]) -> set[str]:
    """The runs a job reads: the run a monitor or score job is over, the monitor run whose
    flags a score reads, the run a fork cuts and the series run it goes on from."""
    out = {v for k in ("origin", "monitor") if isinstance(v := payload.get(k), str)}
    fork = payload.get("fork")
    if isinstance(fork, dict) and isinstance(origin := fork.get("origin"), str):
        out.add(origin)
    options = payload.get("options")
    series = options.get("series") if isinstance(options, dict) else None
    if isinstance(series, dict) and isinstance(previous := series.get("previous"), str):
        out.add(previous)
    return out


async def family(
    state: ServerState, run_id: str, unindexed: Mapping[str, set[str]] | None = None
) -> Family | None:
    """None when the control plane holds no run or job `run_id`. `unindexed` is what
    `_unindexed` read before the call, or is read here first: read before the index is asked,
    a run that a rebuild indexes meanwhile is in it or in what the index lists."""
    if unindexed is None:
        unindexed = await _unindexed(state)
    info = await state.store.run(run_id)
    if info is not None:
        kind: str = info.kind
    else:
        job = await state.db.job(run_id)
        if job is None:
            return None
        kind = str(job.payload.get("kind") or "scenario")
    runs = {run_id: kind}
    wave = [run_id]
    while wave:
        found = [*await state.store.made_from(wave), *await state.db.made_from(wave)]
        wave = list(dict.fromkeys(r for r, _ in found if r not in runs))
        for r, k in found:
            runs.setdefault(r, k)
    blocked, offline = await _check(state, runs)
    behind = [r for r, made in unindexed.items() if r in runs or made & runs.keys()]
    behind += [r for r in runs if await _unheld(state, r)]
    indexing = f"run {behind[0]} is not indexed yet" if behind else None
    return Family(run_id, runs, blocked, offline, indexing)


async def _unheld(state: ServerState, run_id: str) -> bool:
    """Whether the run has a log here the index does not hold."""
    try:
        return await state.store.head(run_id) is None and state.store.logged(run_id)
    except RunCorrupt:
        return False


async def _unindexed(state: ServerState) -> dict[str, set[str]]:
    """Each run with a log here the index does not hold, with the runs it was made from. The
    index is read once, before the logs are listed: a run made in between has a log and is
    looked at on its own."""
    held = await state.store.indexed()
    behind = [r for r in state.store.run_ids() if r not in held]
    return {r: await _made_of(state, r) for r in behind if await _unheld(state, r)}


async def _made_of(state: ServerState, run_id: str) -> set[str]:
    """The runs the first event in a run's log names it made from, as `made_from` reads them
    from the index: its origin and the monitor run a score reads the flags of. Empty for a
    first line that does not read, as the index cannot take that run either."""
    try:
        lines, _ = await state.store.lines(run_id, Head(), upto=0, max_events=1)
        data = json.loads(lines[0])["data"] if lines else None
    except OSError, KeyError, TypeError, ValueError, ChainError:
        return set()
    if not isinstance(data, dict):
        return set()
    config = data.get("config")
    named = [data.get("origin"), config.get("monitor") if isinstance(config, dict) else None]
    return {r for r in named if isinstance(r, str)}


def holder(state: ServerState, run_id: str, runner: str | None) -> str | None:
    """The connected runner that holds a run: the one that writes it, or one that says it runs
    it. None when no connected runner does."""
    for conn in state.runners.values():
        if conn.streams <= 0:
            continue
        held = conn.status.running if conn.status is not None else conn.hello.jobs
        if conn.name == runner or run_id in held:
            return conn.name
    return None


async def _check(state: ServerState, runs: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Why a delete of `runs` is refused, and why it needs force."""
    offline = None
    metas = await state.db.runs_meta(list(runs))
    jobs = await state.db.jobs_of(list(runs))
    for run_id in runs:
        info = await state.store.run(run_id)
        if info is None or info.status != "running":
            continue
        meta, job = metas.get(run_id), jobs.get(run_id)
        runner = (meta.runner if meta else None) or (job.runner if job else None)
        if held := holder(state, run_id, runner):
            return f"run {run_id} is still running on runner {held}", None
        if offline is None:
            who = f"its runner {runner} is offline" if runner else "no runner holds it"
            offline = f"run {run_id} is marked running but {who}"
    for job in await state.db.jobs():
        if job.run_id in runs:
            what = f"run {job.run_id} is {job.state}"
        elif used := sorted(reads(job.payload) & runs.keys()):
            what = f"the {job.state} job {job.run_id} reads run {used[0]}"
        else:
            continue
        if job.state not in ("claimed", "running") or holder(state, job.run_id, job.runner):
            return what, None
        offline = offline or f"{what} on runner {job.runner}, which is offline"
    return None, offline


async def delete(state: ServerState, runs: Mapping[str, str]) -> None:
    """Delete `runs`, made-from runs before what they were made from: their logs, blobs, truth
    files and labels, their index rows and their rows in server.sqlite. Callers hold
    `state.delete_lock` and checked that none is in use."""
    await state.db.forget(list(runs))
    for run_id, kind in reversed(list(runs.items())):
        await _remove(state, run_id)
        state.check_runs.pop(run_id, None)
        state.note_deleted(run_id, kind)


async def _remove(state: ServerState, run_id: str) -> None:
    """An ingest that began before the run was kept as deleted may still be writing it."""
    for _ in range(REMOVE_TRIES):
        try:
            await state.store.remove(run_id)
            return
        except RuntimeError:
            await asyncio.sleep(REMOVE_WAIT_S)
    log.warning("run %s is still being written; it stays until it is deleted again", run_id)


async def prune(state: ServerState, days: float) -> list[str]:
    """Delete the runs that ended more than `days` ago, each with its family. The rows of runs
    that never started go once they are as old. A family in use stays. Returns what went."""
    unindexed = await _unindexed(state)
    cutoff = time.time() - days * 86400
    roots = await state.store.ended_before(format_ts(datetime.fromtimestamp(cutoff, UTC)))
    for run_id in await state.db.ended_before(cutoff):
        if await state.store.run(run_id) is None:
            roots.append(run_id)
    out: dict[str, None] = {}
    for root in dict.fromkeys(roots):
        if root in out:
            continue
        async with state.delete_lock:
            found = await family(state, root, unindexed)
            if found is None:
                # a run with a log started, though the index does not hold it yet
                why = f"run {root} is not indexed yet" if state.store.logged(root) else None
            else:
                why = found.blocked or found.offline or found.indexing
            if why is not None:
                log.info("kept run %s: %s", root, why)
                continue
            gone = {root: ""} if found is None else found.runs
            await delete(state, gone)
        out.update(dict.fromkeys(gone))
    if out:
        log.info("deleted %d run(s) that ended more than %g days ago", len(out), days)
    return list(out)
