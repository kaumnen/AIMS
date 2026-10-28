"""Offline monitor runs: the events of a sealed run fed through the monitor again, into a new run
of kind `monitor`. Its evidence seqs point into the origin, its policy only simulates, and the
origin never changes."""

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from pydantic import JsonValue, ValidationError

from aims import __version__
from aims.events import (
    BlobError,
    BlobRef,
    Content,
    Draft,
    Event,
    EventStore,
    RunFinished,
    RunInfo,
    RunStarted,
    RunStatus,
    SampleWindow,
    Verification,
)
from aims.events.index import FlagInfo
from aims.models import DeltaBus, Limiters, ModelClient, ModelsConfig, Profile, Prompt
from aims.models.replay import Recording
from aims.monitor.config import MonitorConfig, MonitorConfigError, Preset, layer, merge
from aims.monitor.core import HumanRequest, Monitor
from aims.monitor.setup import ResolvedMonitor, identity, resolve_monitor
from aims.monitor.signals import SignalEngine, signal_versions
from aims.monitor.signals.base import SignalError
from aims.prompts import Prompts
from aims.run.fork import Origin, load_origin
from aims.run.job import JobOptions
from aims.run.runner import RunError
from aims.scenario import Scenario
from aims.wire import MonitorMode

log = logging.getLogger("aims.monitor")

PAGE = 1000


def offline_config(
    layers: Sequence[Mapping[str, Any]],
    scenario: Scenario,
    models: ModelsConfig,
    *,
    config: Mapping[str, Any] | None = None,
    preset: Preset | None = None,
    signals_only: bool = False,
    judge_only: bool = False,
    profile: str | None = None,
) -> MonitorConfig:
    """The monitor config of an offline run over a run of `scenario`: the config layers, the
    scenario's `monitor:`, the `config` layer, then the preset, which `signals_only` sets to
    signals. `profile` replaces the model of every sweep, the review and the judge, and
    `judge_only` drops the sweeps. Raises MonitorConfigError or ProfileError."""
    if signals_only and judge_only:
        raise MonitorConfigError("pick signals_only or judge_only, not both")
    tops: list[Mapping[str, Any]] = list(layers)
    if scenario.monitor is not None:
        tops.append(scenario.monitor.model_dump(mode="json", exclude_unset=True))
    if config is not None:
        tops.append(layer(config, "config"))
    chosen = "signals" if signals_only else preset
    if chosen is not None:
        tops.append({"preset": chosen})
    try:
        merged = merge(*tops)
        if profile is not None:
            models.get(profile)
            over: dict[str, Any] = {
                "sweeps": [
                    s.model_copy(update={"profile": profile}).model_dump(mode="json")
                    for s in merged.sweeps
                ],
                "review": {"profile": profile},
            }
            if merged.judge is not None:
                over["judge"] = {"profile": profile}
            merged = merge(merged.model_dump(mode="json"), over)
        if judge_only:
            merged = merge(merged.model_dump(mode="json"), {"sweeps": []})
    except ValidationError as e:
        raise MonitorConfigError(str(e)) from None
    if merged.preset == "off":
        raise MonitorConfigError("the monitor is off; name a preset")
    if judge_only and merged.judge is None:
        raise MonitorConfigError("judge_only needs a judge; the preset has none")
    return merged


def offline_monitor(
    models: ModelsConfig,
    prompts: Prompts,
    scenario: Scenario,
    agents: Sequence[Profile],
    *,
    layers: Sequence[Mapping[str, Any]] = (),
    config: Mapping[str, Any] | None = None,
    preset: Preset | None = None,
    signals_only: bool = False,
    judge_only: bool = False,
    profile: str | None = None,
    samples: int = 1,
) -> ResolvedMonitor:
    """The monitor of an offline run over a run of `scenario` whose agents used `agents`,
    resolved as `offline_config` says, with the signals installed here. Raises
    MonitorConfigError, ProfileError or TextError."""
    merged = offline_config(
        layers,
        scenario,
        models,
        config=config,
        preset=preset,
        signals_only=signals_only,
        judge_only=judge_only,
        profile=profile,
    )
    bare = scenario.model_copy(update={"monitor": None})
    resolved = resolve_monitor(
        models, prompts, bare, agents, layers=[merged.model_dump(mode="json")]
    )
    if resolved is None:
        raise MonitorConfigError("the monitor is off; name a preset")
    if samples > 1:
        resolved = varied(resolved)
    return stamped(resolved, judge_only=judge_only)


def installed_signals(config: MonitorConfig) -> dict[str, str]:
    """The version of each signal `config` turns on, as installed here. Raises
    MonitorConfigError."""
    try:
        return signal_versions(config)
    except SignalError as e:
        raise MonitorConfigError(str(e)) from None


def stamped(resolved: ResolvedMonitor, *, judge_only: bool = False) -> ResolvedMonitor:
    """`resolved` with the versions of the signals installed here, none for a judge-only run,
    and its identity hash over them. Raises MonitorConfigError."""
    versions = {} if judge_only else installed_signals(resolved.config)
    profiles: dict[str, Profile | None] = {f"sweep:{n}": p for n, p in resolved.sweeps.items()}
    profiles |= {"review": resolved.review, "judge": resolved.judge}
    digest = identity(resolved.config, resolved.texts, profiles, versions)
    return dataclasses.replace(resolved, signals=versions, identity=digest)


def varied(resolved: ResolvedMonitor) -> ResolvedMonitor:
    """`resolved` without the sampling seeds its profiles fix, so samples differ."""

    def free(p: Profile | None) -> Profile | None:
        if p is None or p.sampling.seed is None:
            return p
        log.warning("monitor profile %s fixes the sampling seed; clearing it", p.name)
        return p.model_copy(update={"sampling": p.sampling.model_copy(update={"seed": None})})

    sweeps = {n: p for n, p in ((n, free(p)) for n, p in resolved.sweeps.items()) if p}
    return dataclasses.replace(
        resolved, sweeps=sweeps, review=free(resolved.review), judge=free(resolved.judge)
    )


async def monitored(store: EventStore, run_id: str) -> tuple[Origin, RunInfo]:
    """The sealed scenario run `run_id`, loaded to monitor again. Raises RunError."""
    info = await store.run(run_id)
    if info is None:
        raise RunError(f"unknown run {run_id}")
    if info.status == "running":
        raise RunError(f"run {run_id} is not sealed yet")
    if info.kind != "scenario":
        raise RunError(f"run {run_id} is a {info.kind} run, with no agents to monitor")
    return await load_origin(store, run_id), info


def origin_agents(origin: Origin) -> list[Profile]:
    """Every profile the origin's agents used."""
    return [p for schedule in origin.schedules.values() for _, p in schedule]


@dataclass(slots=True)
class OfflineResult:
    run_id: str
    origin: str
    status: RunStatus
    reason: str | None = None
    flags: list[Event] = field(default_factory=list)
    actions: list[Event] = field(default_factory=list)
    cost_usd: float = 0.0


async def monitor_offline(
    store: EventStore,
    origin: Origin,
    resolved: ResolvedMonitor,
    *,
    models: ModelsConfig,
    source: EventStore | None = None,
    run_id: str | None = None,
    mode: MonitorMode = "full",
    origin_head: str | None = None,
    options: JobOptions | None = None,
    config_files: Mapping[str, str | None] | None = None,
    bus: DeltaBus | None = None,
    limiters: Limiters | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    on_start: Callable[[str], None] | None = None,
    replay: Recording | None = None,
    key: bytes | None = None,
) -> OfflineResult:
    """Monitor the sealed run `origin` again with `resolved`, writing the findings to a new run
    in `store`. `source` holds the origin when `store` does not. `origin_head` is the head
    hash the origin must still have. `judge_only` mode runs no signal. `replay` serves the
    model answers from a recorded monitor run, and `key` fixes the frame id key so its requests
    match; both are for replaying a recording. Raises RunError before the run is made; after
    that, a failure seals the run as failed."""
    reads = source or store
    info = await reads.run(origin.run_id)
    if info is None:
        raise RunError(f"unknown run {origin.run_id}")
    if info.status == "running":
        raise RunError(f"run {origin.run_id} is not sealed yet")
    if origin_head is not None and info.head != origin_head:
        raise RunError(f"run {origin.run_id} changed since the monitor run was asked for")
    o = options or JobOptions()
    scenario = origin.bundle.scenario
    config: dict[str, JsonValue] = {
        "aims": __version__,
        "mode": mode,
        "monitor": resolved.snapshot(),
        "profile": o.profile,
        "group": o.group,
        "sample": o.sample,
        "config_files": dict(config_files or {}),
    }
    started = RunStarted(
        scenario=scenario.name,
        oversight=scenario.oversight,
        agents=[a.id for a in scenario.agents],
        config=config,
        kind="monitor",
        origin=origin.run_id,
        origin_head=info.head,
        monitor=resolved.identity or None,
    )
    run_id = await store.create_run(started, run_id=run_id or str(uuid.uuid7()))
    if on_start is not None:
        on_start(run_id)
    client = ModelClient(
        store, run_id, models, bus=bus, limiters=limiters, transport=transport, replay=replay
    )
    result = OfflineResult(run_id, origin.run_id, "failed")
    monitor: Monitor | None = None
    try:
        try:
            monitor = Monitor(
                run_id=run_id,
                scenario=scenario,
                monitor=resolved,
                store=OriginStore(store, origin.run_id, run_id, reads),
                client=client,
                actuator=Inert(),
                simulated=True,
                source=origin.run_id,
                engine_factory=_no_signals if mode == "judge_only" else SignalEngine,
                key=key,
            )
            await monitor.start()
            await _feed(monitor, reads, origin.run_id, scenario, info.status)
            result.status = "completed"
        except Exception as e:
            log.exception("monitor run %s over %s failed", run_id, origin.run_id)
            result.status, result.reason = "failed", f"{type(e).__name__}: {e}"
    except asyncio.CancelledError:
        result.status, result.reason = "cancelled", "cancelled before the monitor finished"
        await asyncio.shield(_end(store, monitor, client, result))
        raise
    await _end(store, monitor, client, result)
    return result


async def _feed(
    monitor: Monitor, reads: EventStore, origin: str, scenario: Scenario, status: str
) -> None:
    """Every event of the origin up to its seal, in order. The final sweeps come where the live
    run had them, once every agent finished, and the judge last; a cancelled run gets
    neither, as live."""
    agents = {a.id for a in scenario.agents}
    finished: set[str] = set()
    ran = False
    swept = status == "cancelled"
    cursor: int | None = None
    while page := await reads.events(origin, after=cursor, limit=PAGE):
        cursor = page[-1].seq
        batch: list[Event] = []
        for event in page:
            if event.type == "run.finished":
                break
            batch.append(event)
            if event.type == "agent.started":
                ran = True
            elif event.type == "agent.step":
                await _take(monitor, batch)
                batch = []
                await monitor.settled()
            elif event.type == "agent.finished" and event.agent_id is not None:
                finished.add(event.agent_id)
                if not swept and agents <= finished:
                    await _take(monitor, batch)
                    batch = []
                    swept = True
                    await monitor.agents_done()
        await _take(monitor, batch)
    if ran and not swept:
        await monitor.agents_done()
    await monitor.judge(skip=status == "cancelled" or not ran)


async def _take(monitor: Monitor, batch: list[Event]) -> None:
    if batch:
        monitor.feed(batch)
    await monitor.barrier()


async def _end(
    store: EventStore, monitor: Monitor | None, client: ModelClient, result: OfflineResult
) -> None:
    try:
        if monitor is not None:
            await monitor.close()
            result.flags, result.actions = monitor.flags, monitor.actions
            result.cost_usd = monitor.cost_usd
    except Exception as e:
        log.exception("closing the monitor of run %s failed", result.run_id)
        if result.status == "completed":
            result.status, result.reason = "failed", f"{type(e).__name__}: {e}"
    finally:
        await client.aclose()
        finished = RunFinished(status=result.status, reason=result.reason)
        await store.append(result.run_id, [Draft(finished)])


def _no_signals(config: MonitorConfig, scenario: Scenario, **kw: Any) -> SignalEngine:
    return SignalEngine(config.model_copy(update={"preset": "off"}), scenario, **kw)


class Inert:
    """The actuator of a monitor run: its policy only simulates, so nothing here acts."""

    async def pause(self, agents: Sequence[str], reason: str) -> None:
        return None

    async def resume(
        self, agents: Sequence[str], then: Literal["continue", "deny"] = "continue"
    ) -> None:
        return None

    async def kill(self, reason: str) -> None:
        return None

    def paused(self, agent_id: str) -> bool:
        return False

    def held_call(self, agent_id: str) -> str | None:
        return None

    def next_prompt(self, agent_id: str) -> tuple[Profile, Prompt] | None:
        return None

    async def ask_human(self, request: HumanRequest) -> None:
        return None


class OriginStore:
    """The store an offline monitor reads and writes through. Reads of the origin go to the
    store that holds it; a write aimed at the sealed origin, such as a sweep's long summary,
    lands in the monitor run, whose events refer to it."""

    def __init__(self, store: EventStore, origin: str, run_id: str, source: EventStore) -> None:
        self.store = store
        self.origin = origin
        self.run_id = run_id
        self.source = source

    def _at(self, run_id: str) -> str:
        return self.run_id if run_id == self.origin else run_id

    def _of(self, run_id: str) -> EventStore:
        return self.source if run_id == self.origin else self.store

    async def create_run(
        self, started: RunStarted, *, blobs: Sequence[bytes] = (), run_id: str | None = None
    ) -> str:
        return await self.store.create_run(started, blobs=blobs, run_id=run_id)

    async def append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        if run_id == self.origin:
            raise RunError(f"run {run_id} is sealed; the monitor writes to {self.run_id}")
        return await self.store.append(run_id, drafts)

    async def put_blob(self, run_id: str, data: bytes) -> BlobRef:
        return await self.store.put_blob(self._at(run_id), data)

    async def put_blobs(self, run_id: str, datas: Sequence[bytes]) -> list[BlobRef]:
        return await self.store.put_blobs(self._at(run_id), datas)

    async def get_blob(self, run_id: str, sha256: str) -> bytes:
        if run_id != self.origin:
            return await self.store.get_blob(run_id, sha256)
        try:
            return await self.source.get_blob(run_id, sha256)
        except BlobError, KeyError:
            return await self.store.get_blob(self.run_id, sha256)

    async def content(self, run_id: str, text: str) -> Content:
        return await self.store.content(self._at(run_id), text)

    def stream(self, run_id: str, *, after: int = -1) -> AsyncIterator[Event]:
        return self._of(run_id).stream(run_id, after=after)

    async def run(self, run_id: str) -> RunInfo | None:
        return await self._of(run_id).run(run_id)

    async def runs(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        severity: int | None = None,
        category: str | None = None,
    ) -> list[RunInfo]:
        return await self.store.runs(
            limit=limit,
            before=before,
            kind=kind,
            origin=origin,
            severity=severity,
            category=category,
        )

    async def events(
        self,
        run_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
        types: Sequence[str] = (),
        agent_id: str | None = None,
        query: str | None = None,
    ) -> list[Event]:
        return await self._of(run_id).events(
            run_id,
            after=after,
            before=before,
            limit=limit,
            types=types,
            agent_id=agent_id,
            query=query,
        )

    async def flags(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        layer: str | None = None,
        category: str | None = None,
        min_importance: int | None = None,
        status: Sequence[str] = (),
        agent_id: str | None = None,
        tainted: bool | None = None,
    ) -> list[FlagInfo]:
        return await self._of(run_id).flags(
            run_id,
            after=after,
            limit=limit,
            layer=layer,
            category=category,
            min_importance=min_importance,
            status=status,
            agent_id=agent_id,
            tainted=tainted,
        )

    async def samples(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
        limit: int = 1000,
    ) -> list[SampleWindow]:
        return await self._of(run_id).samples(
            run_id, agent_id=agent_id, ts_from=ts_from, ts_to=ts_to, limit=limit
        )

    async def verify(self, run_id: str) -> Verification:
        return await self._of(run_id).verify(run_id)

    async def rebuild(self) -> None:
        return None

    async def close(self) -> None:
        return None
