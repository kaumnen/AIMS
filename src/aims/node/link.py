"""A process's tie to the control plane: it says hello, reports status, carries out operator
commands on its runs, ships every run and forwards model deltas."""

import asyncio
import contextlib
import logging
import os
import platform
from collections.abc import Awaitable, Callable

import httpx2

from aims import __version__
from aims.events import LocalEventStore
from aims.models import Delta, DeltaBus
from aims.node.client import FEED, ApiError, ControlClient
from aims.node.shipper import Backoff, Shipper
from aims.run.runner import RunError, Runner
from aims.wire import (
    BASE_KINDS,
    HEARTBEAT_S,
    Ack,
    CommandMessage,
    Deltas,
    JobKindName,
    OfferMessage,
    RunnerHello,
    RunnerStatus,
    TracerHealth,
    WireDelta,
)

log = logging.getLogger("aims.node")

KINDS: tuple[JobKindName, ...] = (*BASE_KINDS, "monitor", "score", "selftest")
DELTA_EVERY_S = 0.05
DELTA_BATCH = 2000
MAX_DONE = 10_000


def arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)


class Link:
    def __init__(
        self,
        client: ControlClient,
        store: LocalEventStore,
        bus: DeltaBus,
        *,
        slots: int = 0,
        tracer: Callable[[], TracerHealth] = lambda: TracerHealth(alive=False),
        on_offer: Callable[[OfferMessage], Awaitable[None]] | None = None,
        jobs: Callable[[], list[str]] = list,
        kinds: tuple[JobKindName, ...] = KINDS,
    ) -> None:
        self.client = client
        self.kinds = kinds
        self.store = store
        self.bus = bus
        self.slots = slots
        self.wanted: list[str] = []
        self.tracer = tracer
        self.on_offer = on_offer
        self.jobs = jobs
        self.runners: dict[str, Runner] = {}
        self.shippers: dict[str, Shipper] = {}
        self.connected = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._ships: dict[str, asyncio.Task[None]] = {}
        self._done: dict[str, Ack] = {}
        self._doing: dict[str, asyncio.Task[Ack]] = {}
        self._last_id: str | None = None
        self._loops: list[asyncio.Task[None]] = []

    def hello(self) -> RunnerHello:
        return RunnerHello(
            name=self.client.runner,
            version=__version__,
            arch=arch(),
            tracer=self.tracer(),
            envs=set_envs(self.wanted),
            slots=self.slots,
            jobs=sorted({*self.jobs(), *self.runners}),
            kinds=list(self.kinds),
        )

    def status(self) -> RunnerStatus:
        running = sorted(self.runners)
        return RunnerStatus(
            tracer=self.tracer(),
            envs=set_envs(self.wanted),
            free=max(0, self.slots - len(set(self.jobs()) | set(running))),
            running=running,
            jobs=sorted(self.jobs()),
        )

    async def start(self) -> None:
        self._loops = [
            asyncio.create_task(self._stream()),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._deltas()),
        ]

    async def close(self) -> None:
        tasks = [*self._loops, *self._tasks, *self._ships.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._loops.clear()
        await self.client.aclose()

    async def drain(self, within: float) -> bool:
        """Wait until every shipper is done, or `within` seconds. True if all were."""
        pending = [t for t in self._ships.values() if not t.done()]
        if not pending:
            return True
        _, waiting = await asyncio.wait(pending, timeout=within)
        for task in waiting:
            task.cancel()
        return not waiting

    def track(self, runner: Runner) -> None:
        """Take operator commands for `runner`'s run and ship it from the start."""
        previous = runner.on_start

        def started(run_id: str) -> None:
            self.runners[run_id] = runner
            self.ship(run_id)
            if previous is not None:
                previous(run_id)

        runner.on_start = started

    def untrack(self, run_id: str) -> None:
        self.runners.pop(run_id, None)

    def ship(self, run_id: str) -> None:
        task = self._ships.get(run_id)
        if task is not None and not task.done():
            return
        shipper = Shipper(self.store, self.client, run_id)
        self.shippers[run_id] = shipper
        self._ships[run_id] = asyncio.create_task(shipper.run())

    async def ship_all(self) -> None:
        """Ship every local run the server does not hold sealed."""
        run_ids = self.store.run_ids()
        for i in range(0, len(run_ids), 1000):
            chunk = run_ids[i : i + 1000]
            heads = await self.client.heads(chunk)
            for run_id in chunk:
                local = await self.store.head(run_id)
                server = heads.get(run_id)
                if local is None:
                    continue
                if (
                    server is None
                    or server.seq < local.seq
                    or server.hash != local.hash
                    or (
                        local.finished
                        and not server.truth
                        and self.store.truth_path(run_id).is_file()
                    )
                ):
                    self.ship(run_id)
                elif server is not None and server.seq >= 0:
                    await self._labels(run_id)

    async def _labels(self, run_id: str) -> None:
        """Send the labels people gave the flags of a run the server holds; it keeps those it
        lacks."""
        path = self.store.labels_path(run_id)
        if not path.is_file():
            return
        labels = await self.store.label_history(run_id)
        try:
            await self.client.post_labels(run_id, labels)
        except (ApiError, httpx2.TransportError, OSError) as e:
            log.warning("run %s: labels not sent: %s", run_id, e)

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def wrap() -> None:
            await coro

        task = asyncio.create_task(wrap())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _stream(self) -> None:
        backoff = Backoff()
        while True:
            try:
                reply = await self.client.hello(self.hello())
                if sorted(reply.envs) != self.wanted:
                    self.wanted = sorted(reply.envs)
                    with contextlib.suppress(ApiError, httpx2.TransportError, OSError):
                        await self.client.status(self.status())
                if reply.requeued:
                    log.info("the control plane took back jobs %s", ", ".join(reply.requeued))
                async with self.client.stream(self._last_id) as source:
                    self.connected.set()
                    backoff.reset()
                    async for sse in source:
                        if sse.event not in ("command", "offer"):
                            continue
                        message = FEED.validate_json(sse.data)
                        if isinstance(message, CommandMessage):
                            self._last_id = sse.id or self._last_id
                            self._spawn(self._command(message))
                        elif self.on_offer is not None:
                            self._spawn(self.on_offer(message))
            except asyncio.CancelledError:
                raise
            except ApiError as e:
                log.warning("control plane: %s", e)
                self.connected.clear()
                await backoff.wait(e.retry_after)
            except (httpx2.TransportError, OSError, ValueError) as e:
                log.debug("control plane stream ended: %s", e)
                self.connected.clear()
                await backoff.wait()
            else:
                self.connected.clear()
                await backoff.wait()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await self.client.status(self.status())
            except (ApiError, httpx2.TransportError, OSError) as e:
                log.debug("status not sent: %s", e)

    async def _command(self, message: CommandMessage) -> None:
        ack = self._done.get(message.id)
        if ack is None:
            doing = self._doing.get(message.id)
            if doing is None:
                doing = asyncio.create_task(self._carry_out(message))
                self._doing[message.id] = doing
            try:
                ack = await asyncio.shield(doing)
            finally:
                if doing.done():
                    self._doing.pop(message.id, None)
            self._done[message.id] = ack
            while len(self._done) > MAX_DONE:
                self._done.pop(next(iter(self._done)))
        backoff = Backoff()
        for _ in range(8):
            try:
                await self.client.ack(message.id, ack)
                return
            except ApiError as e:
                if not e.retryable:
                    log.warning("ack of command %s refused: %s", message.id, e)
                    return
                await backoff.wait(e.retry_after)
            except httpx2.TransportError, OSError:
                await backoff.wait()

    async def _carry_out(self, message: CommandMessage) -> Ack:
        runner = self.runners.get(message.run_id)
        if runner is None or runner.finished:
            return Ack(ok=False, outcome="the run is not running here")
        c = message.command
        try:
            await runner.operate(
                c.action,
                agent=c.agent,
                then=c.then,
                text=c.text,
                reason=c.reason,
                token=c.token,
                command_id=message.id,
            )
        except RunError as e:
            return Ack(ok=False, outcome=str(e))
        except Exception as e:
            log.exception("command %s on run %s failed", message.id, message.run_id)
            return Ack(ok=False, outcome=f"{type(e).__name__}: {e}")
        return Ack(ok=True, outcome="done")

    async def _deltas(self) -> None:
        """Forward model deltas as they stream, in batches; lossy, never retried."""
        with self.bus.subscribe() as sub:
            sent_dropped = 0
            while True:
                first = await sub.queue.get()
                await asyncio.sleep(DELTA_EVERY_S)
                batch: list[Delta] = [first]
                while len(batch) < DELTA_BATCH:
                    try:
                        batch.append(sub.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                dropped, sent_dropped = sub.dropped - sent_dropped, sub.dropped
                body = Deltas(
                    deltas=[
                        WireDelta(
                            run_id=d.run_id,
                            agent_id=d.agent_id,
                            call_id=d.call_id,
                            attempt=d.attempt,
                            kind=d.kind,
                            text=d.text,
                            index=d.index,
                            ms=d.ms,
                        )
                        for d in batch
                    ],
                    dropped=dropped,
                )
                with contextlib.suppress(ApiError, httpx2.TransportError, OSError):
                    await self.client.post_deltas(body)


def set_envs(names: list[str]) -> list[str]:
    """Which of `names` are set here; names only, never values."""
    return sorted(n for n in set(names) if os.environ.get(n))
