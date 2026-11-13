"""`aims runner`: a long-lived process that takes jobs from the control plane and runs them on
its Docker host with one kernel tracer, keeping every run in its own store."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import httpx2
from pydantic import JsonValue

from aims.aio import settle
from aims.channels import archives
from aims.events import (
    Draft,
    LocalEventStore,
    RunCorrupt,
    RunFinished,
    RunStarted,
    RunStatus,
    SelfTestCase,
)
from aims.events.blobs import decode
from aims.events.chain import MAX_LINE
from aims.events.schema import blob_refs
from aims.events.store import TruthRefused, is_run_id
from aims.models import ModelsConfig, Roles, load_models
from aims.models.check import record_check
from aims.monitor.config import MonitorConfigError
from aims.monitor.offline import monitor_offline, stamped
from aims.monitor.texts import MonitorTexts, resolve
from aims.node.client import ApiError, ControlClient
from aims.node.link import REFUSED_MAX, Link
from aims.node.shipper import Backoff, Streak
from aims.prompts import TextError
from aims.run import selftest as st
from aims.run import truth
from aims.run.fork import load_origin
from aims.run.job import Job, job_bundle, runner_for, selftest_runner
from aims.run.runner import Bench, Options, RunError, Runner, seal_unsealed
from aims.run.score import Added
from aims.run.scoring import ScoreError, score_with
from aims.sandbox import AuditMode, DockerBackend, SandboxError
from aims.sandbox.images import KEEP_DAYS
from aims.scenario import Bundle
from aims.wire import MonitorJob, OfferMessage, ScoreJob, TracerHealth

log = logging.getLogger("aims.node")

TRACER_RETRY_S = 60.0
PRUNE_EVERY_S = 3600.0
# how long a run the control plane deleted gets to seal once cancelled
DROP_WAIT_S = 180.0
# Monitor, score and fork jobs of one run tend to come within days of each other; a pruned copy
# is only fetched again.
COPY_DAYS = 7.0
BLOB_LIMIT = 2 * 1024**3
INGEST_CHUNK = 1000
INGEST_BYTES = 4 * 1024 * 1024


def data_dir(env: dict[str, str] | None = None) -> Path:
    """Where a runner keeps its runs: `%LOCALAPPDATA%\\aims\\runner` on Windows, else
    `$XDG_DATA_HOME/aims/runner` or `~/.local/share/aims/runner`."""
    env = dict(os.environ) if env is None else env
    if os.name == "nt" and (local := env.get("LOCALAPPDATA")):
        return Path(local) / "aims" / "runner"
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aims" / "runner"


@dataclass(slots=True)
class NodeOptions:
    url: str
    token: str
    name: str
    data: Path
    slots: int = 2
    keep_days: float | None = None
    image_keep_days: float = KEEP_DAYS
    verify: bool | str = True
    docker_url: str | None = None
    # snapshot never starts the privileged kernel tracer
    audit: AuditMode = "auto"
    transport: httpx2.AsyncBaseTransport | None = None
    model_transport: httpx2.AsyncBaseTransport | None = None
    backend: DockerBackend | None = None


class Refused(Exception):
    """The control plane kept refusing this runner's token or name, so it stopped."""


@dataclass(slots=True)
class _Open:
    store: LocalEventStore
    origins: LocalEventStore
    backend: DockerBackend
    bench: Bench
    link: Link


class Node:
    def __init__(self, options: NodeOptions) -> None:
        self.options = options
        self.claimed: dict[str, Job | MonitorJob | ScoreJob | None] = {}
        self.stopping = False
        # set once the runner stops its jobs for good and closes; a stop then does nothing
        self.closing = False
        self.ready = asyncio.Event()
        self.backend: DockerBackend | None = None
        self._open: _Open | None = None
        self._jobs: dict[str, asyncio.Task[None]] = {}
        # monitor and score jobs under way, which the first stop cancels
        self._offline: set[str] = set()
        self._tracer_tried = 0.0
        # job run id -> the runs it reads, which are not pruned until it ends
        self._reads: dict[str, set[str]] = {}
        self._pruning = asyncio.Lock()

    @property
    def ctx(self) -> _Open:
        if self._open is None:
            raise RuntimeError("the runner is not running")
        return self._open

    def tracer(self) -> TracerHealth:
        backend = self.backend
        if backend is None:
            return TracerHealth(alive=False, detail="the sandbox backend is not open")
        tracer = backend.tracer
        alive = tracer is not None and tracer.alive
        if alive:
            detail = None
        elif self.options.audit == "snapshot":
            detail = "this runner has no kernel tracer (--audit snapshot); runs use snapshot audit"
        else:
            detail = "the kernel tracer is down; runs use snapshot audit"
        return TracerHealth(
            alive=alive,
            detail=detail,
            restarts=backend.tracer_restarts,
        )

    async def run(self, stop: asyncio.Event) -> None:
        o = self.options
        await asyncio.to_thread(o.data.mkdir, parents=True, exist_ok=True)
        async with (
            await LocalEventStore.open(o.data / "runs") as store,
            await LocalEventStore.open(o.data / "origins", ingest_only=True) as origins,
        ):
            sealed = await seal_unsealed(store)
            if sealed:
                log.warning("sealed %d run(s) left open by an earlier runner", len(sealed))
            given = o.backend is not None
            backend = o.backend or await DockerBackend.open(
                audit=o.audit, url=o.docker_url, image_keep_days=o.image_keep_days
            )
            self.backend = backend
            async with contextlib.AsyncExitStack() as stack:
                if not given:
                    stack.push_async_callback(backend.close)
                bench = Bench(store, backend, load_models(), transport=o.model_transport)
                client = ControlClient(
                    o.url, o.token, runner=o.name, transport=o.transport, verify=o.verify
                )
                refused: list[ApiError] = []

                def on_refused(error: ApiError) -> None:
                    # With no commands from the control plane nobody can stop a run here, so
                    # the runner stops as for a first Ctrl-C. A stop under way stays as it is.
                    if not stop.is_set():
                        refused.append(error)
                        log.error("the control plane refuses this runner (%s); stopping", error)
                        stop.set()

                link = Link(
                    client,
                    store,
                    bench.bus,
                    slots=o.slots,
                    tracer=self.tracer,
                    on_offer=self._offer,
                    jobs=lambda: list(self.claimed),
                    on_deleted=self._drop,
                    on_refused=on_refused,
                )
                self._open = _Open(store, origins, backend, bench, link)
                await link.start()
                chores = [
                    asyncio.create_task(self._ship_all()),
                    asyncio.create_task(self._watch_tracer()),
                    asyncio.create_task(self._prune()),
                ]
                self.ready.set()
                try:
                    await stop.wait()
                    await self._stop()
                finally:
                    # What is left is bounded. A cut store close can leave the store open,
                    # which holds the process at exit.
                    self.closing = True
                    await settle(self._end(chores, link))
                if refused:
                    raise Refused(
                        f"the control plane at {client.url} refused this runner {REFUSED_MAX}"
                        f" times in a row: {refused[0]}"
                    )

    async def _end(self, chores: list[asyncio.Task[None]], link: Link) -> None:
        for task in chores:
            task.cancel()
        for task in chores:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("a background task of the runner had failed")
        for task in self._jobs.values():
            task.cancel()
        for task in list(self._jobs.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await link.close()
        self._open = None

    async def _stop(self) -> None:
        """Like a first Ctrl-C for every run: agents stop, files are archived, runs are sealed
        as cancelled and shipped. Monitor and score jobs stop at once, as `_monitor_or_score`
        says."""
        self.stopping = True
        link = self.ctx.link
        for runner in list(link.runners.values()):
            runner.cancel()
        for run_id in self._offline:
            self._jobs[run_id].cancel()
        if self._jobs:
            await asyncio.wait(list(self._jobs.values()), timeout=120)
        if not await link.drain(30):
            log.warning("some runs were not fully shipped; they ship when the runner restarts")

    async def _ship_all(self) -> None:
        """Ship what earlier runs of this runner left, trying again until every run was looked
        at. An error of another kind, such as a locked index, is tried again too and warned of
        when it lasts."""
        backoff, streak = Backoff(), Streak("shipping the runs kept here")
        while True:
            try:
                await self.ctx.link.ship_all()
                streak.works()
                return
            except (ApiError, httpx2.TransportError, OSError) as e:
                log.debug("listing what to ship failed: %s", e)
                await backoff.wait(getattr(e, "retry_after", None))
            except Exception as e:
                streak.failed(e)
                await backoff.wait()

    async def _offer(self, offer: OfferMessage) -> None:
        if self.stopping or offer.run_id in self.claimed:
            return
        if len(self.claimed) >= self.options.slots:
            return
        needs = offer.needs
        if needs.get("kernel") and not self.tracer().alive:
            return
        wanted = needs.get("envs") or []
        if not isinstance(wanted, list) or any(not os.environ.get(str(n)) for n in wanted):
            return
        self.claimed[offer.run_id] = None
        try:
            job = await self.ctx.link.client.claim(offer.run_id)
        except (ApiError, httpx2.TransportError, OSError) as e:
            log.debug("claim of %s failed: %s", offer.run_id, e)
            job = None
        if job is None:
            self.claimed.pop(offer.run_id, None)
            return
        self.claimed[offer.run_id] = job
        link = self.ctx.link
        task = asyncio.create_task(self._execute(job))
        # Not in a finally of _execute: a task cancelled before its first step, as by a delete
        # of its run, never runs its body.
        task.add_done_callback(lambda _: self._free(job, link))
        self._jobs[offer.run_id] = task

    def _free(self, job: Job | MonitorJob | ScoreJob, link: Link) -> None:
        """Free the slot of a job whose task ended, however it ended."""
        self.claimed.pop(job.run_id, None)
        self._jobs.pop(job.run_id, None)
        self._reads.pop(job.run_id, None)
        link.untrack(job.run_id)

    async def _execute(self, job: Job | MonitorJob | ScoreJob) -> None:
        ctx = self.ctx
        link, store = ctx.link, ctx.store
        try:
            if isinstance(job, MonitorJob | ScoreJob):
                await self._monitor_or_score(job)
                return
            blobs: dict[str, bytes] = {}
            for sha in job.blobs():
                data = decode(await link.client.job_blob(job.run_id, sha))
                if hashlib.sha256(data).hexdigest() != sha:
                    raise RuntimeError(f"blob {sha} came back with other bytes")
                blobs[sha] = data
            if job.kind == "models.check":
                models = job.config()
                link.ship(job.run_id)
                await record_check(
                    store,
                    models,
                    [models.get(n) for n in job.check],
                    run_id=job.run_id,
                    transport=self.options.model_transport,
                )
                return
            with job_bundle(job, blobs) as bundle:
                if job.kind == "selftest":
                    await self._selftest(job, bundle)
                    return
                origin = source = None
                if job.fork is not None:
                    source = await self._origin(job.fork.origin, job.run_id)
                    origin = await load_origin(source, job.fork.origin)
                inherited, restored = await self._previous(job)
                runner = runner_for(
                    ctx.bench,
                    job,
                    bundle,
                    origin=origin,
                    origins=None if source is store else source,
                    inherited=inherited,
                    restored=restored,
                )
                link.track(runner)
                if self.stopping:
                    runner.cancel()
                await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("job %s failed", job.run_id)
            if not await self._began(job.run_id):
                # A score job and a model check ship their run from before it begins. With
                # no line written that shipper would wait for one without end.
                link.unship(job.run_id)
                with contextlib.suppress(ApiError, httpx2.TransportError, OSError):
                    await link.client.release(job.run_id, f"{type(e).__name__}: {e}")

    async def _began(self, run_id: str) -> bool:
        """Whether a job's run has a line here, read from its log: the index can lag behind it.
        A run the index marks corrupt has lines."""
        try:
            return await self.ctx.store.log_head(run_id) is not None
        except RunCorrupt:
            return True

    async def _selftest(self, job: Job, bundle: Bundle) -> None:
        """Run the self-test of the job's scenario into the run `job.run_id`: each case a
        `selftest.case` run of its own under it, shipped and open to commands, and one
        `selftest.case` event per case as it ends. It completes when every case proved what it
        should. A command to the self-test's run goes to the case running."""
        ctx = self.ctx
        link, store = ctx.link, ctx.store
        try:
            plan = await asyncio.to_thread(st.cases, bundle)
        except st.SelfTestError as e:
            raise RunError("; ".join(e.problems)) from None
        started = RunStarted(
            scenario=bundle.scenario.name,
            oversight="observe",
            config={"cases": [c.name for c in plan], "digest": bundle.digest},
            kind="selftest",
        )
        run_id = await store.create_run(started, run_id=job.run_id)
        link.ship(run_id)

        def make(options: Options) -> Runner:
            runner = selftest_runner(ctx.bench, job, bundle, options)
            link.track(runner)
            link.runners[run_id] = runner
            if self.stopping:
                runner.cancel()
            return runner

        async def record(o: st.Outcome) -> None:
            if o.run_id is not None:
                link.untrack(o.run_id)
            case = SelfTestCase(
                case=o.kind,
                name=o.case,
                run_id=o.run_id,
                ok=o.ok,
                problems=o.problems,
                notes=o.notes,
            )
            await store.append(run_id, [Draft(case)])

        status: RunStatus = "failed"
        reason: str | None = None
        try:
            outcomes = await st.run(
                ctx.bench,
                bundle,
                plan,
                profile=job.options.profile,
                on_outcome=record,
                origin=run_id,
                runner=make,
            )
            stopped = next((o for o in outcomes if o.status in ("cancelled", "killed")), None)
            failed = [o.case for o in outcomes if not o.ok]
            if stopped is not None and stopped.status is not None:
                status, reason = stopped.status, f"{stopped.case} was {stopped.status}"
            elif failed:
                reason = f"{len(failed)} of {len(plan)} cases failed: {', '.join(failed)}"
            else:
                status = "completed"
        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled before the cases finished"
            raise
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            raise
        finally:
            # a case that a cancel or an error cut short has no outcome to untrack it
            case = link.runners.get(run_id)
            if case is not None and case.run_id is not None:
                link.untrack(case.run_id)
            link.untrack(run_id)
            # The seal lands even when a second cancel, such as the runner's kill, comes meanwhile.
            await settle(store.append(run_id, [Draft(RunFinished(status=status, reason=reason))]))

    async def _monitor_or_score(self, job: MonitorJob | ScoreJob) -> None:
        """Run a monitor or score job. The first stop cancels it at once, so it calls no model
        after that. A run it began is sealed as cancelled and ships. The control plane then ends
        the job as it ends a stopped run's. A job stopped before its run began goes back to the
        control plane for the next runner."""
        if self.stopping:
            await self._hand_back(job)
            return
        self._offline.add(job.run_id)
        try:
            if isinstance(job, MonitorJob):
                await self._monitor(job)
            else:
                await self._score(job)
        except asyncio.CancelledError:
            if self.stopping and not await self._began(job.run_id):
                await self._hand_back(job)
            raise
        finally:
            self._offline.discard(job.run_id)

    async def _hand_back(self, job: MonitorJob | ScoreJob) -> None:
        """Give a job stopped before its run began back to the control plane."""
        link = self.ctx.link
        # a score job ships its run from before the run begins
        link.unship(job.run_id)
        with contextlib.suppress(ApiError, httpx2.TransportError, OSError):
            await link.client.release(job.run_id, "the runner stopped before the job began")

    async def _monitor(self, job: MonitorJob) -> None:
        """Monitor a sealed run again: import it like a fork's origin, check it is still the run
        the job was queued for, and feed it through the job's monitor with the signals installed
        here, into the new run `job.run_id`. No sandbox; keys come from this runner's env."""
        ctx = self.ctx
        try:
            resolved = stamped(job.resolved(), judge_only=job.mode == "judge_only")
        except MonitorConfigError as e:
            raise RunError(f"the job's monitor does not load: {e}") from None
        source = await self._origin(job.origin, job.run_id)
        origin = await load_origin(source, job.origin, full=False)
        await monitor_offline(
            ctx.store,
            origin,
            resolved,
            models=ctx.bench.models,
            source=source,
            run_id=job.run_id,
            mode=job.mode,
            origin_head=job.origin_head,
            options=job.options,
            config_files=job.config_files,
            bus=ctx.bench.bus,
            limiters=ctx.bench.limiters,
            transport=self.options.model_transport,
            on_start=ctx.link.ship,
        )

    async def _score(self, job: ScoreJob) -> None:
        """Score a sealed run into the new run `job.run_id`: fetch the run, its monitor run and
        its truth file when this runner lacks them, then score as `aims score` does."""
        ctx = self.ctx
        source = await self._origin(job.origin, job.run_id)
        if not source.truth_path(job.origin).is_file():
            data = await ctx.link.client.run_truth(job.origin)
            try:
                await source.put_truth(job.origin, data)
            except TruthRefused as e:
                raise RunError(f"the truth file of {job.origin} does not check: {e}") from None
        held = None if job.monitor is None else await self._origin(job.monitor, job.run_id)
        profile = job.profile()
        models = ctx.bench.models
        if profile is not None:
            roles = Roles(agent=profile.name, sweep=profile.name, judge=profile.name)
            models = ModelsConfig(profiles={profile.name: profile}, roles=roles)
        try:
            texts = MonitorTexts.restore(job.texts) if job.texts else resolve(ctx.bench.prompts)
        except TextError as e:
            raise RunError(f"the job's texts do not load: {e}") from None
        ctx.link.ship(job.run_id)
        try:
            await score_with(
                ctx.store,
                job.origin,
                models=models,
                texts=texts,
                candidates=[] if profile is None else [profile.name],
                monitor=job.monitor,
                transport=self.options.model_transport,
                run_id=job.run_id,
                source=None if source is ctx.store else source,
                monitor_source=None if held is None or held is ctx.store else held,
                added=None if job.added is None else Added.load(job.added),
                awareness=job.awareness,
                answer=job.answer,
            )
        except ScoreError as e:
            raise RunError(str(e)) from None

    async def _previous(
        self, job: Job
    ) -> tuple[list[dict[str, JsonValue]] | None, dict[str, bytes] | None]:
        """The canaries and folder archives the series run before `job` left, fetched with its
        truth file when another runner ran it; (None, None) for a run that starts a series."""
        place = job.options.series
        if place is None or place.previous is None:
            return None, None
        previous = place.previous
        source = await self._origin(previous, job.run_id)
        if not source.truth_path(previous).is_file():
            try:
                data = await self.ctx.link.client.run_truth(previous)
            except ApiError as e:
                if e.status != 404:
                    raise
            else:
                try:
                    await source.put_truth(previous, data)
                except TruthRefused as e:
                    raise RunError(f"the truth file of {previous} does not check: {e}") from None
        found = source.truth_path(previous).is_file()
        inherited = truth.inherit(source.root / previous) if found else []
        return inherited, await archives(source, previous)

    async def _origin(self, run_id: str, job_id: str) -> LocalEventStore:
        """The store holding run `run_id` for job `job_id`: this runner's own, or a copy from
        the control plane. A sealed copy is used as it is; any other is fetched again. Neither
        is pruned until the job ends."""
        ctx = self.ctx
        async with self._pruning:
            self._reads.setdefault(job_id, set()).add(run_id)
        if await ctx.store.run(run_id) is not None:
            return ctx.store
        origins = ctx.origins
        head = await origins.head(run_id)
        if head is None or not head.finished:
            await self._fetch(run_id)
        await asyncio.to_thread(os.utime, origins.root / run_id)
        return origins

    async def _fetch(self, run_id: str) -> None:
        """Copy run `run_id` from the control plane through the same checks as ingest. The log
        is spooled to disk, then ingested a batch at a time once the blobs the batch references
        are stored."""
        origins, client = self.ctx.origins, self.ctx.link.client
        origins.prepare_run(run_id)
        with tempfile.TemporaryFile(dir=origins.root) as spool:
            async with client.run_log(run_id) as chunks:
                async for chunk in chunks:
                    await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            while lines := await asyncio.to_thread(_batch, spool):
                refs = dict.fromkeys(sha for line in lines for sha in blob_refs(_data(line)))
                for sha in await origins.missing_blobs(run_id, list(refs)):
                    async with client.run_blob(run_id, sha) as blob:
                        await origins.put_blob_stream(run_id, sha, blob, limit=BLOB_LIMIT)
                await origins.ingest(run_id, lines)

    async def _watch_tracer(self) -> None:
        """Bring the tracer back once no sandbox is open; until then kernel jobs are refused."""
        if self.options.audit == "snapshot":
            return
        while True:
            await asyncio.sleep(5)
            backend = self.backend
            if backend is None or self.tracer().alive or self.claimed:
                continue
            if time.monotonic() - self._tracer_tried < TRACER_RETRY_S:
                continue
            self._tracer_tried = time.monotonic()
            try:
                await backend.restart_tracer()
                log.warning("kernel tracer restarted")
            except SandboxError as e:
                log.warning("kernel tracer did not restart: %s", e)

    async def _prune(self) -> None:
        """Every hour, drop copies of other runners' runs unused for `COPY_DAYS`, or for
        `keep_days` when that is shorter, then runs sealed more than `keep_days` ago once the
        control plane holds or deleted them. A run a job reads stays."""
        days = self.options.keep_days
        while True:
            try:
                await self._prune_copies(COPY_DAYS if days is None else min(days, COPY_DAYS))
                if days is not None:
                    await self._prune_runs(days)
            except (ApiError, httpx2.TransportError, OSError) as e:
                log.debug("prune skipped: %s", e)
            except Exception as e:
                # such as a locked index: the next hour tries again
                log.warning("prune failed: %s", e)
            await asyncio.sleep(PRUNE_EVERY_S)
            await self._prune_images()

    async def _prune_images(self) -> None:
        """Remove what opening the backend removed, for a runner that stays up. A backend
        given in the options is the giver's to clean."""
        if self.options.backend is not None or self.backend is None:
            return
        try:
            await self.backend.remove_old(self.options.image_keep_days)
        except Exception as e:
            log.warning("image cleanup failed: %s", e)

    async def _prune_copies(self, days: float) -> None:
        origins = self.ctx.origins
        cutoff = time.time() - days * 86400
        for run_id in await asyncio.to_thread(_copies, origins.root):
            async with self._pruning:
                if self._read(run_id):
                    continue
                if not await asyncio.to_thread(_unused, origins.root / run_id, cutoff):
                    continue
                await origins.remove(run_id)
            log.info("pruned the copy of run %s", run_id)

    async def _prune_runs(self, days: float) -> None:
        store, client = self.ctx.store, self.ctx.link.client
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        old = []
        for run_id in store.run_ids():
            info = await store.run(run_id)
            if info is not None and info.ended_at and info.ended_at.timestamp() < cutoff:
                old.append((run_id, info.head, info.events))
        heads = {}
        for i in range(0, len(old), 1000):
            heads.update(await client.heads([r for r, _, _ in old[i : i + 1000]]))
        for run_id, head, events in old:
            server = heads.get(run_id)
            if server is not None and server.deleted:
                await self._drop(run_id)
                continue
            if server is None or server.seq != events - 1 or server.hash != head:
                continue
            async with self._pruning:
                if self._read(run_id):
                    continue
                await store.remove(run_id)
            log.info("pruned run %s", run_id)

    async def _drop(self, run_id: str) -> None:
        """Delete the copy of a run the control plane deleted, unless a job reads it. A run still
        running here, which a forced delete takes, is cancelled first: through its job's task
        for a monitor or score job or a model check, which have no runner, and for a self-test,
        whose runner is only its case's, finished between cases; else through its runner."""
        if self._open is None:
            return
        runner = self.ctx.link.runners.get(run_id)
        task = self._jobs.get(run_id)
        job = self.claimed.get(run_id)
        if task is not None and (
            isinstance(job, MonitorJob | ScoreJob)
            or (isinstance(job, Job) and job.kind in ("models.check", "selftest"))
        ):
            task.cancel()
        elif runner is not None and not runner.finished:
            runner.cancel()
        else:
            task = None
        if task is not None:
            await asyncio.wait([task], timeout=DROP_WAIT_S)
        async with self._pruning:
            if self._read(run_id):
                return
            try:
                await self.ctx.store.remove(run_id)
            except RuntimeError as e:
                log.warning("run %s was deleted on the control plane but stays here: %s", run_id, e)
                return
        log.info("dropped run %s: the control plane deleted it", run_id)

    def _read(self, run_id: str) -> bool:
        return any(run_id in runs for runs in self._reads.values())


def _data(line: bytes) -> object:
    return json.loads(line)["data"]


def _batch(spool: IO[bytes]) -> list[bytes]:
    """The next lines of a spooled log: at most INGEST_CHUNK, ending once INGEST_BYTES is
    reached."""
    lines: list[bytes] = []
    size = 0
    while len(lines) < INGEST_CHUNK and size < INGEST_BYTES:
        line = spool.readline(MAX_LINE + 1)
        if not line:
            break
        lines.append(line)
        size += len(line)
    return lines


def _copies(root: Path) -> list[str]:
    return [p.name for p in root.iterdir() if is_run_id(p.name) and p.is_dir()]


def _unused(path: Path, cutoff: float) -> bool:
    """Whether the copy at `path` was last fetched, used or written before `cutoff`."""
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False
