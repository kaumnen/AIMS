"""`aims runner`: a long-lived process that takes jobs from the control plane and runs them on
its Docker host with one kernel tracer, keeping every run in its own store."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from aims.events import LocalEventStore, split_lines
from aims.events.blobs import decode
from aims.events.schema import blob_refs
from aims.models import load_models
from aims.models.check import record_check
from aims.monitor.config import MonitorConfigError
from aims.monitor.offline import monitor_offline, stamped
from aims.node.client import ApiError, ControlClient
from aims.node.link import Link
from aims.node.shipper import Backoff
from aims.run.fork import load_origin
from aims.run.job import Job, bundle_of, runner_for
from aims.run.runner import Bench, RunError, seal_unsealed
from aims.sandbox import DockerBackend, SandboxError
from aims.wire import MonitorJob, OfferMessage, TracerHealth

log = logging.getLogger("aims.node")

TRACER_RETRY_S = 60.0
PRUNE_EVERY_S = 3600.0
BLOB_LIMIT = 2 * 1024**3
INGEST_CHUNK = 1000


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
    verify: bool | str = True
    docker_url: str | None = None
    transport: httpx2.AsyncBaseTransport | None = None
    model_transport: httpx2.AsyncBaseTransport | None = None
    backend: DockerBackend | None = None


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
        self.claimed: dict[str, Job | MonitorJob | None] = {}
        self.stopping = False
        self.ready = asyncio.Event()
        self.backend: DockerBackend | None = None
        self._open: _Open | None = None
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._tracer_tried = 0.0

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
        return TracerHealth(
            alive=alive,
            detail=None if alive else "the kernel tracer is down; runs use snapshot audit",
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
            backend = o.backend or await DockerBackend.open(audit="auto", url=o.docker_url)
            self.backend = backend
            async with contextlib.AsyncExitStack() as stack:
                if not given:
                    stack.push_async_callback(backend.close)
                bench = Bench(store, backend, load_models(), transport=o.model_transport)
                client = ControlClient(
                    o.url, o.token, runner=o.name, transport=o.transport, verify=o.verify
                )
                link = Link(
                    client,
                    store,
                    bench.bus,
                    slots=o.slots,
                    tracer=self.tracer,
                    on_offer=self._offer,
                    jobs=lambda: list(self.claimed),
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
                    for task in chores:
                        task.cancel()
                    for task in chores:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                    for task in self._jobs.values():
                        task.cancel()
                    for task in list(self._jobs.values()):
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                    await link.close()
                    self._open = None

    async def _stop(self) -> None:
        """Like a first Ctrl-C for every run: agents stop, files are archived, runs are sealed
        as cancelled and shipped."""
        self.stopping = True
        link = self.ctx.link
        for runner in list(link.runners.values()):
            runner.cancel()
        if self._jobs:
            await asyncio.wait(list(self._jobs.values()), timeout=120)
        if not await link.drain(30):
            log.warning("some runs were not fully shipped; they ship when the runner restarts")

    async def _ship_all(self) -> None:
        backoff = Backoff()
        while True:
            try:
                await self.ctx.link.ship_all()
                return
            except (ApiError, httpx2.TransportError, OSError) as e:
                log.debug("listing what to ship failed: %s", e)
                await backoff.wait(getattr(e, "retry_after", None))

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
        self._jobs[offer.run_id] = asyncio.create_task(self._execute(job))

    async def _execute(self, job: Job | MonitorJob) -> None:
        ctx = self.ctx
        link, store = ctx.link, ctx.store
        try:
            if isinstance(job, MonitorJob):
                await self._monitor(job)
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
            bundle = bundle_of(job, blobs)
            origin = source = None
            if job.fork is not None:
                source = await self._origin(job.fork.origin)
                origin = await load_origin(source, job.fork.origin)
            runner = runner_for(
                ctx.bench,
                job,
                bundle,
                origin=origin,
                origins=None if source is store else source,
            )
            link.track(runner)
            if self.stopping:
                runner.cancel()
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("job %s failed", job.run_id)
            if await store.run(job.run_id) is None:
                with contextlib.suppress(ApiError, httpx2.TransportError, OSError):
                    await link.client.release(job.run_id, f"{type(e).__name__}: {e}")
        finally:
            self.claimed.pop(job.run_id, None)
            self._jobs.pop(job.run_id, None)
            link.untrack(job.run_id)

    async def _monitor(self, job: MonitorJob) -> None:
        """Monitor a sealed run again: import it like a fork's origin, check it is still the run
        the job was queued for, and feed it through the job's monitor with the signals installed
        here, into the new run `job.run_id`. No sandbox; keys come from this runner's env."""
        ctx = self.ctx
        try:
            resolved = stamped(job.resolved(), judge_only=job.mode == "judge_only")
        except MonitorConfigError as e:
            raise RunError(f"the job's monitor does not load: {e}") from None
        source = await self._origin(job.origin)
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

    async def _origin(self, run_id: str) -> LocalEventStore:
        """The store holding run `run_id`: this runner's own, or a copy fetched from the
        control plane through the same checks as ingest."""
        ctx = self.ctx
        if await ctx.store.run(run_id) is not None:
            return ctx.store
        origins, client = ctx.origins, ctx.link.client
        lines = split_lines(await client.run_log(run_id))
        origins.prepare_run(run_id)
        refs: dict[str, None] = {}
        for line in lines:
            for sha in blob_refs(_data(line)):
                refs[sha] = None
        for sha in await origins.missing_blobs(run_id, list(refs)):
            raw = await client.run_blob(run_id, sha)
            await origins.put_blob_stream(run_id, sha, _once(raw), limit=BLOB_LIMIT)
        for i in range(0, len(lines), INGEST_CHUNK):
            await origins.ingest(run_id, lines[i : i + INGEST_CHUNK])
        return origins

    async def _watch_tracer(self) -> None:
        """Bring the tracer back once no sandbox is open; until then kernel jobs are refused."""
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
        """Drop runs sealed more than `keep_days` ago once the control plane holds them."""
        days = self.options.keep_days
        if days is None:
            return
        store, client = self.ctx.store, self.ctx.link.client
        while True:
            try:
                cutoff = datetime.now(UTC).timestamp() - days * 86400
                old = []
                for run_id in store.run_ids():
                    info = await store.run(run_id)
                    if info is not None and info.ended_at and info.ended_at.timestamp() < cutoff:
                        old.append((run_id, info.head, info.events))
                heads = {}
                for i in range(0, len(old), 1000):
                    heads.update(await client.heads([r for r, _, _ in old[i : i + 1000]]))
                if old:
                    for run_id, head, events in old:
                        server = heads.get(run_id)
                        if server is not None and server.seq == events - 1 and server.hash == head:
                            await store.remove(run_id)
                            log.info("pruned run %s", run_id)
            except (ApiError, httpx2.TransportError, OSError) as e:
                log.debug("prune skipped: %s", e)
            await asyncio.sleep(PRUNE_EVERY_S)


def _data(line: bytes) -> object:
    return json.loads(line)["data"]


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data
