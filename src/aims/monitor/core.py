"""The monitor of one run: signals on every event, sweeps on their triggers, the policy over
their flags, the review of its pauses, and the judge after the verifier."""

import asyncio
import collections
import logging
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol

from aims.agent import GatePoint
from aims.events import (
    Draft,
    Event,
    EventStore,
    MonitorAction,
    MonitorCleared,
    MonitorDegraded,
    MonitorFlag,
    MonitorVerdict,
    SweepTrigger,
)
from aims.models import ModelClient, Profile, Prompt
from aims.monitor.channels import NAME as CHANNELS
from aims.monitor.channels import ChannelLog, ChannelSweeper
from aims.monitor.config import SweepConfig
from aims.monitor.judge import Judge, JudgeResult
from aims.monitor.policy import Cutoffs, Decision, Policy
from aims.monitor.render import Trajectory
from aims.monitor.review import Reviewer, ReviewResult
from aims.monitor.setup import ResolvedMonitor
from aims.monitor.signals import SignalEngine
from aims.monitor.sweep import Sweeper, SweepResult
from aims.scenario import Budget, Scenario

log = logging.getLogger("aims.monitor")

FLAG_TRIGGER = 3
BUDGET_TRIGGER = 0.8
FAILURES = 2
TICK_S = 1.0
RETRY_S = 60.0
RECENT_CALLS = 64
ACTION_CALLS = 20

type Key = tuple[str, str]
type Trigger = tuple[str | None, SweepTrigger, str | None]
# The run-wide channel sweep's place among the sweeps due; no agent id is empty.
CHANNEL_KEY: Key = (CHANNELS, "")


@dataclass(frozen=True, slots=True)
class HumanRequest:
    """A pause waiting for a person: the agents paused, the flags that caused it, and the
    review's advice when one ran."""

    run_id: str
    agents: list[str]
    flags: list[Event]
    advice: str | None = None
    timeout_s: float | None = None
    held_call: dict[str, str] = field(default_factory=dict)


class Actuator(Protocol):
    """How the monitor acts on its run; the runner implements it. Each call carries out a
    decision the monitor already recorded as `monitor.action`, so an Actuator never writes an
    `operator.action` of its own."""

    async def pause(self, agents: Sequence[str], reason: str) -> None:
        """Pause the agents and freeze their sandboxes at once."""
        ...

    async def resume(
        self, agents: Sequence[str], then: Literal["continue", "deny"] = "continue"
    ) -> None: ...

    async def kill(self, reason: str) -> None: ...

    def paused(self, agent_id: str) -> bool: ...

    def held_call(self, agent_id: str) -> str | None:
        """The tool call id the agent holds at before running it, if it is held there."""
        ...

    def next_prompt(self, agent_id: str) -> tuple[Profile, Prompt] | None:
        """The agent's profile and the prompt of its next model call, for hidden resamples."""
        ...

    async def ask_human(self, request: HumanRequest) -> None:
        """Tell a person a pause waits for them (TTY prompt); the answer comes back as an
        operator action."""
        ...


@dataclass(slots=True)
class _Agent:
    budget: Budget
    steps: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    last_seq: int = -1
    active: bool = False
    started: datetime | None = None
    seconds: float = 0.0
    hit: set[str] = field(default_factory=set)
    every_at: dict[str, int] = field(default_factory=dict)
    every_s_at: dict[str, float] = field(default_factory=dict)
    calls: collections.OrderedDict[str, Event] = field(default_factory=collections.OrderedDict)


@dataclass(slots=True)
class _Due:
    trigger: SweepTrigger
    count: int = 1


@dataclass(slots=True)
class _Pause:
    agents: list[str]
    flags: list[Event]
    deadline: float
    degraded: bool = False
    version: int = 0
    task: asyncio.Task[None] | None = None


class Monitor:
    """The monitor of one run. The runner feeds it every appended event, synchronously and in
    seq order, from the store's append tap; it skips its own events and model calls.

    Signals run on every event; a gate point waits at `barrier` until every event appended
    before it was taken in, so signal-driven pauses land before the agent moves on. Sweeps run
    one at a time in the background on their triggers; with `hold`, `hold` keeps an agent
    before a tool call until the sweep due has scored it. When the run has channels, the
    channel sweep runs among them on new channel records and at the end, and each agent its
    flags name gets a sweep of its whole trajectory led by the finding. Policy decisions are
    written as `monitor.action` and carried out through the `Actuator` unless `simulated`.

    `source` is the run whose events are fed, when it is not `run_id`: the origin of an offline
    re-run, whose blobs the signals, sweeps and judge read. The factories build the signal
    engine, trajectories, sweepers and judge; tests swap them.
    """

    def __init__(
        self,
        *,
        run_id: str,
        scenario: Scenario,
        monitor: ResolvedMonitor,
        store: EventStore,
        client: ModelClient,
        actuator: Actuator,
        simulated: bool = False,
        source: str | None = None,
        engine_factory: Callable[..., SignalEngine] = SignalEngine,
        trajectory_factory: Callable[..., Trajectory] = Trajectory,
        sweeper_factory: Callable[..., Sweeper] = Sweeper,
        channel_factory: Callable[..., ChannelSweeper] = ChannelSweeper,
        judge_factory: Callable[..., Judge] = Judge,
        key: bytes | None = None,
    ) -> None:
        self.run_id = run_id
        self.source = source or run_id
        self.scenario = scenario
        self.monitor = monitor
        self.store = store
        self.client = client
        self.actuator = actuator
        self.config = monitor.config
        self.mode = monitor.oversight
        self.simulated = simulated or self.mode == "observe"
        self.offline = self.source != run_id
        self._sweeper_factory = sweeper_factory
        self._channel_factory = channel_factory
        self._judge_factory = judge_factory
        self._trajectory_factory = trajectory_factory
        self._key = key or secrets.token_bytes(32)
        self._engine = engine_factory(self.config, scenario, run_id=self.source, store=store)
        self._budgets = {a.id: a.budget for a in scenario.agents}
        self.policy = Policy(self.config.policy, self.mode, self._budgets)
        self._reviewer = Reviewer(
            config=self.config.review,
            texts=monitor.texts,
            client=client,
            store=store,
            source=self.source,
            key=self._key,
        )
        self._agents: dict[str, _Agent] = {}
        self._trajectories: dict[str, Trajectory] = {}
        for agent_id in self._budgets:
            self._agent(agent_id)
        models = self.config.models_on
        self._sweep_configs: list[SweepConfig] = [
            c for c in self.config.sweeps if models and c.name in monitor.sweeps
        ]
        self._channel_log: ChannelLog | None = None
        channels = self.config.channels is not None and monitor.channels is not None
        if models and channels and scenario.channels:
            self._channel_log = ChannelLog(self.source, store, self._key, scenario)
        self._channel_sweeper: ChannelSweeper | None = None
        self._channel_count = 0
        self._leads: dict[str, str] = {}
        self._sweepers: dict[Key, Sweeper] = {}
        self._scorers: dict[Key, Sweeper] = {}
        self._own_calls: set[str] = set()
        self._queue: collections.deque[list[Event]] = collections.deque()
        self._wake = asyncio.Event()
        self._progress = asyncio.Event()
        self._fed = 0
        self._done = 0
        self._consumer: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._pause_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[Key, _Due] = {}
        self._running: Key | None = None
        self._sweep_wake = asyncio.Event()
        self._swept = asyncio.Event()
        self._covered: dict[Key, int] = {}
        self._swept_seq: dict[Key, int] = {}
        self._last_cost: dict[Key, float] = {}
        self._failures = 0
        self._signals_only = False
        self._degraded: set[str] = set()
        self._pauses: list[_Pause] = []
        self._operator_paused: set[str] = set()
        self._killed = False
        self._closed = False
        self._spent = 0.0
        self._flags: list[Event] = []
        self._sweep_events: list[Event] = []
        self._actions: list[Event] = []
        self._run_hit: set[str] = set()

    @property
    def cost_usd(self) -> float:
        """What the monitor's sweeps, reviews and judge have cost so far."""
        return self._spent

    @property
    def flags(self) -> list[Event]:
        """Every `monitor.flag` written so far."""
        return list(self._flags)

    @property
    def actions(self) -> list[Event]:
        """Every `monitor.action` written so far."""
        return list(self._actions)

    def tainted(self, agent_id: str) -> list[int]:
        """Seqs of flags raised in the verifier phase of `agent_id`."""
        return self.policy.tainted(agent_id)

    def feed(self, events: Sequence[Event]) -> None:
        """Queue a batch the store appended. Runs under the store's write lock: no awaits."""
        if self._closed:
            return
        batch: list[Event] = []
        for e in events:
            if e.type.startswith("monitor."):
                continue
            if e.type == "model.request":
                if e.data.get("role") is not None:
                    self._own_calls.add(str(e.data.get("call_id")))
                    continue
            elif e.type == "model.response":
                if e.data.get("call_id") in self._own_calls:
                    continue
            elif e.type == "tool.call" and e.data.get("model_call") in self._own_calls:
                continue
            batch.append(e)
        if batch:
            self._queue.append(batch)
            self._fed += 1
            self._wake.set()

    async def start(self) -> None:
        if self._consumer is not None:
            return
        self._consumer = asyncio.create_task(self._consume(), name=f"monitor {self.run_id}")
        self._consumer.add_done_callback(lambda _: self._progress.set())
        if self._sweep_configs or self._channel_log is not None:
            self._tasks.append(asyncio.create_task(self._sweep_loop()))
        if self._sweep_configs and not self.offline:
            self._tasks.append(asyncio.create_task(self._clock()))

    async def barrier(self) -> None:
        """Wait until every event fed before this call was taken in and acted on."""
        consumer = self._consumer
        target = self._fed
        while self._done < target and consumer is not None and not consumer.done():
            await self._progress.wait()

    async def hold(self, point: GatePoint) -> None:
        """At before_tool with hold on: wait until the sweep due has scored this call."""
        if point.kind != "before_tool":
            return
        keys = [(c.name, point.agent_id) for c in self._sweep_configs if c.hold]
        while keys and self._sweeps_on and any(self._due(k, point.step) for k in keys):
            await self._swept.wait()

    def request_sweep(self, agent_id: str | None, trigger: SweepTrigger) -> None:
        """Ask for a sweep of `agent_id`, or of every agent, by every sweep config."""
        self._request([(agent_id, trigger, None)])

    async def agents_done(self) -> None:
        """The agents stopped: run the final sweeps."""
        await self.barrier()
        for pause in list(self._pauses):
            self._drop(pause)
        if not self._sweeps_on:
            return
        log_ = self._channel_log
        if log_ is not None and log_.last_seq > self._swept_seq.get(CHANNEL_KEY, -1):
            self._request_channels("final")
        for c in self._sweep_configs:
            for agent_id in self._agents:
                if self._visible(agent_id) > self._swept_seq.get((c.name, agent_id), -1):
                    self._request([(agent_id, "final", c.name)])
        while self._sweeps_on and (self._pending or self._running is not None):
            await self._swept.wait()

    async def settled(self) -> None:
        """Wait until no sweep is due or running. An offline re-run feeds on only once the
        sweeps a step triggered have scored it, so each sweep sees what it would have live."""
        while self._sweeps_on and (self._pending or self._running is not None):
            await self._swept.wait()

    async def judge(self, *, skip: bool = False) -> None:
        """Run the judge after the verifier, before the seal; `skip` on a cancelled run."""
        config = self.config.judge
        if skip or config is None or self.monitor.judge is None:
            return
        await self.barrier()
        left = self.monitor.budget_usd - self._spent
        if left <= 0:
            await self._degrade("judge", "the monitor budget is spent", "skip")
            return
        judge = self._judge_factory(
            source=self.source,
            config=config,
            profile=self.monitor.judge,
            texts=self.monitor.texts,
            client=self.client,
            store=self.store,
            budget_usd=left,
            key=self._key,
        )
        try:
            async with asyncio.timeout(config.timeout_s):
                result: JudgeResult = await judge.run(
                    flags=list(self._flags), sweeps=list(self._sweep_events)
                )
        except TimeoutError:
            self._spent += getattr(judge, "cost_usd", 0.0)
            reason = f"the judge ran past its {config.timeout_s:g} s timeout"
            await self._degrade("judge", reason, "skip")
            return
        except asyncio.CancelledError:
            self._spent += getattr(judge, "cost_usd", 0.0)
            await asyncio.shield(self._degrade("judge", "the judge was cancelled", "skip"))
            raise
        except Exception as e:
            self._spent += getattr(judge, "cost_usd", 0.0)
            log.exception("the judge of run %s failed", self.run_id)
            await self._degrade("judge", "the judge failed", "skip", f"{type(e).__name__}: {e}")
            return
        self._spent += result.cost_usd
        drafts = list(result.flags)
        if result.verdict is not None:
            drafts.append(self._with_taint(result.verdict))
        written = await self._append(drafts)
        self._flags.extend(e for e in written if e.type == "monitor.flag")
        if result.verdict is None or result.error is not None:
            reason = "the judge gave no verdict" if result.verdict is None else "the judge erred"
            await self._degrade("judge", reason, "skip", result.error)

    async def close(self) -> None:
        """Write the signals' final counts and stop every task."""
        if self._closed:
            return
        try:
            await self.barrier()
        finally:
            self._closed = True
            await asyncio.shield(self._shutdown())

    async def _shutdown(self) -> None:
        tasks = [t for t in (self._consumer, *self._tasks, *self._pause_tasks) if t is not None]
        self._pauses.clear()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._progress.set()
        self._swept.set()
        try:
            drafts = await self._engine.close()
        except Exception:
            log.exception("closing the signals of run %s failed", self.run_id)
            return
        if drafts:
            written = await self._append(drafts)
            self._engine.recorded(written)
            self._flags.extend(e for e in written if e.type == "monitor.flag")

    # The consumer: every fed event, in order.

    async def _consume(self) -> None:
        while True:
            while not self._queue:
                self._wake.clear()
                await self._wake.wait()
            batch = self._queue.popleft()
            try:
                for event in batch:
                    try:
                        await self._take(event)
                    except Exception:
                        log.exception(
                            "the monitor of run %s failed on seq %d", self.run_id, event.seq
                        )
            finally:
                self._done += 1
                self._progress.set()
                self._progress = asyncio.Event()

    async def _take(self, event: Event) -> None:
        decisions: list[Decision] = []
        if (tripped := self.policy.observe(event)) is not None:
            decisions.append(tripped)
        triggers = self._observe(event)
        try:
            drafts = await self._engine.feed(event)
        except Exception as e:
            log.exception("the signals of run %s failed on seq %d", self.run_id, event.seq)
            await self._degrade("signal", "a signal failed", "continue", f"{type(e).__name__}: {e}")
            drafts = []
        for agent_id, trajectory in self._trajectories.items():
            try:
                trajectory.add(event)
            except Exception:
                log.exception("the trajectory of %s failed on seq %d", agent_id, event.seq)
        self._channel_event(event)
        if drafts:
            for d in drafts:
                if isinstance(d.payload, MonitorFlag):
                    for t in self._trajectories_of(d.agent_id):
                        t.mark(d.payload.evidence)
            written = await self._append(drafts)
            self._engine.recorded(written)
            flags = [e for e in written if e.type == "monitor.flag"]
            self._flags.extend(flags)
            for e in flags:
                f = MonitorFlag.model_validate(e.data)
                if f.importance >= FLAG_TRIGGER and not f.final and f.phase != "verifier":
                    triggers.append((e.agent_id, "flag", None))
            decisions += self.policy.decide(flags)
        self._request(triggers)
        for d in decisions:
            await self._act(d)

    def _observe(self, event: Event) -> list[Trigger]:
        triggers: list[Trigger] = []
        agent_id = event.agent_id
        data = event.data
        st = None if agent_id is None else self._agent(agent_id)
        if st is not None:
            st.last_seq = event.seq
        match event.type:
            case "agent.started" if st is not None:
                st.active = True
                st.started = event.ts
                st.seconds = 0.0
            case "agent.step" if st is not None and agent_id is not None:
                self._stepped(agent_id, st, data, triggers)
            case "agent.finished" if agent_id is not None and st is not None:
                st.active = False
                triggers.append((agent_id, "agent_finished", None))
            case "tool.call" if st is not None:
                st.calls[str(data.get("call_id"))] = event
                while len(st.calls) > RECENT_CALLS:
                    st.calls.popitem(last=False)
            case "operator.action":
                self._operator(event, triggers)
        if self.offline and st is not None and st.active and st.started is not None:
            st.seconds = max(st.seconds, (event.ts - st.started).total_seconds())
            if agent_id is not None:
                self._timed(agent_id, st, triggers)
        return triggers

    def _stepped(
        self, agent_id: str, st: _Agent, data: Mapping[str, Any], triggers: list[Trigger]
    ) -> None:
        st.steps = int(data.get("n") or st.steps)
        total = data.get("total")
        if isinstance(total, dict):
            st.output_tokens = int(total.get("output_tokens") or 0)
        st.cost_usd = float(data.get("total_cost_usd") or 0.0)
        for c in self._sweep_configs:
            if c.every is not None and st.steps - st.every_at.get(c.name, 0) >= c.every:
                st.every_at[c.name] = st.steps
                triggers.append((agent_id, "every", c.name))
        b = st.budget
        used = {
            "steps": (st.steps, b.steps),
            "output_tokens": (st.output_tokens, b.output_tokens),
            "cost_usd": (st.cost_usd, b.cost_usd),
        }
        for kind, (value, limit) in used.items():
            if limit is not None and value >= BUDGET_TRIGGER * limit and kind not in st.hit:
                st.hit.add(kind)
                triggers.append((agent_id, "budget", None))
        limits = self.scenario.limits
        run = {
            "steps": (sum(a.steps for a in self._agents.values()), limits.steps),
            "output_tokens": (
                sum(a.output_tokens for a in self._agents.values()),
                limits.output_tokens,
            ),
            "cost_usd": (sum(a.cost_usd for a in self._agents.values()), limits.cost_usd),
        }
        for kind, (value, limit) in run.items():
            if limit is not None and value >= BUDGET_TRIGGER * limit and kind not in self._run_hit:
                self._run_hit.add(kind)
                triggers.append((None, "budget", None))

    def _timed(self, agent_id: str, st: _Agent, triggers: list[Trigger]) -> None:
        for c in self._sweep_configs:
            if c.every_s is not None and st.seconds - st.every_s_at.get(c.name, 0.0) >= c.every_s:
                st.every_s_at[c.name] = st.seconds
                triggers.append((agent_id, "every_s", c.name))
        wall = st.budget.wall_s
        if wall is not None and st.seconds >= BUDGET_TRIGGER * wall and "wall_s" not in st.hit:
            st.hit.add("wall_s")
            triggers.append((agent_id, "budget", None))

    def _operator(self, event: Event, triggers: list[Trigger]) -> None:
        action = event.data.get("action")
        targets = [event.agent_id] if event.agent_id else list(self._agents)
        if action == "pause":
            self._operator_paused.update(targets)
        elif action == "resume":
            self._operator_paused.difference_update(targets)
            self._release(targets)
        elif action in ("kill", "cancel"):
            if action == "kill":
                self._killed = True
            self._release(list(self._agents))
        elif action == "sweep":
            triggers.append((event.agent_id, "operator", None))

    def _channel_event(self, event: Event) -> None:
        log_, config = self._channel_log, self.config.channels
        if log_ is None or config is None:
            return
        try:
            log_.add(event)
        except Exception:
            log.exception("the channel log failed on seq %d", event.seq)
            return
        if log_.count - self._channel_count >= config.every:
            self._request_channels("every")

    async def _clock(self) -> None:
        """Count each running agent's unpaused seconds, for `every_s` and the wall budget."""
        every = [c.every_s for c in self._sweep_configs if c.every_s is not None]
        tick = min([TICK_S, *(e / 4 for e in every)])
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(tick)
            now = loop.time()
            elapsed, last = now - last, now
            triggers: list[Trigger] = []
            for agent_id, st in self._agents.items():
                if not st.active:
                    continue
                try:
                    paused = self.actuator.paused(agent_id)
                except Exception:
                    paused = False
                if not paused:
                    st.seconds += elapsed
                self._timed(agent_id, st, triggers)
            self._request(triggers)

    # Sweeps: one at a time, in the order asked for.

    @property
    def _sweeps_on(self) -> bool:
        sweeps = bool(self._sweep_configs) or self._channel_log is not None
        return sweeps and not self._signals_only and not self._closed

    def _request(self, triggers: Sequence[Trigger]) -> None:
        if not self._sweeps_on:
            return
        for agent_id, trigger, name in triggers:
            agents = [agent_id] if agent_id is not None else list(self._agents)
            for c in self._sweep_configs:
                if name is not None and c.name != name:
                    continue
                for a in agents:
                    self._due_now((c.name, a), trigger)
        if self._pending:
            self._sweep_wake.set()

    def _request_channels(self, trigger: SweepTrigger) -> None:
        if not self._sweeps_on or self._channel_log is None:
            return
        self._channel_count = self._channel_log.count
        self._due_now(CHANNEL_KEY, trigger)
        self._sweep_wake.set()

    def _due_now(self, key: Key, trigger: SweepTrigger) -> None:
        """Ask for the sweep `key`; one already due answers this trigger too. A channel lead
        makes the sweep due a deep one."""
        due = self._pending.get(key)
        if due is None:
            self._pending[key] = _Due(trigger)
            return
        due.count += 1
        if trigger == "channel":
            due.trigger = trigger

    def _due(self, key: Key, step: int) -> bool:
        busy = key in self._pending or self._running == key
        return busy and self._covered.get(key, 0) < step

    def _notify_swept(self) -> None:
        self._swept.set()
        self._swept = asyncio.Event()

    async def _sweep_loop(self) -> None:
        while True:
            while not self._pending:
                self._sweep_wake.clear()
                await self._sweep_wake.wait()
            key = next(iter(self._pending))
            due = self._pending.pop(key)
            try:
                await self._sweep(key, due.trigger, due.count)
            except Exception:
                log.exception("a sweep of run %s failed", self.run_id)
            finally:
                self._running = None
                self._notify_swept()

    async def _sweep(self, key: Key, trigger: SweepTrigger, coalesced: int) -> None:
        name, agent_id = key
        channels = key == CHANNEL_KEY
        steps = 0
        if channels:
            assert self._channel_log is not None
            last_seq = self._channel_log.last_seq
        else:
            steps = self._agent(agent_id).steps
            last_seq = self._visible(agent_id)
        again = ("operator", "review", "channel")
        if trigger not in again and last_seq <= self._swept_seq.get(key, -1):
            return
        if not self._may_spend(self._last_cost.get(key, 0.0)):
            await self._out_of_budget()
            return
        self._running = key
        error = None
        leads: dict[str, str] = {}
        try:
            if channels:
                found = await self._channel().sweep(trigger, coalesced=coalesced)
                result, leads = found.result, found.leads
            else:
                lead = None
                if trigger == "channel" and agent_id in self._leads:
                    finding = self._leads[agent_id]
                    lead = self.monitor.texts["channel_lead"].format(
                        agent=agent_id, finding=finding
                    )
                result = await self._sweeper(key).sweep(trigger, coalesced=coalesced, lead=lead)
        except Exception as e:
            log.exception("sweep %s of %s failed", name, agent_id or "the run")
            error = f"{type(e).__name__}: {e}"
            result = SweepResult(failed=True)
        self._spent += result.cost_usd
        self._last_cost[key] = result.cost_usd
        written = await self._append([*result.sweeps, *result.flags])
        self._sweep_events.extend(e for e in written if e.type == "monitor.sweep")
        flags = [e for e in written if e.type == "monitor.flag"]
        self._flags.extend(flags)
        if result.failed:
            self._failures += 1
            if self._failures == FAILURES:
                await self._sweeps_failed(error or _sweep_error(written))
        else:
            self._failures = 0
            self._covered[key] = max(self._covered.get(key, 0), steps)
            self._swept_seq[key] = max(self._swept_seq.get(key, -1), last_seq)
            if trigger == "channel":
                self._leads.pop(agent_id, None)
        if channels:
            for e in flags:
                for t in self._trajectories_of(e.agent_id):
                    t.mark(MonitorFlag.model_validate(e.data).evidence)
            config = self.config.channels
            if config is not None and config.deep and self._sweep_configs:
                for agent, finding in leads.items():
                    if agent in self._agents:
                        self._leads[agent] = finding
                        self._request([(agent, "channel", None)])
        for d in self.policy.decide(flags):
            await self._act(d)

    def _visible(self, agent_id: str) -> int:
        """The last seq a sweep of `agent_id` can see: its trajectory leaves out the verifier,
        so the verifier's events never call for another sweep."""
        trajectory = self._trajectories.get(agent_id)
        return self._agent(agent_id).last_seq if trajectory is None else trajectory.last_seq

    def _channel(self) -> ChannelSweeper:
        if self._channel_sweeper is None:
            assert self._channel_log is not None
            assert self.config.channels is not None
            assert self.monitor.channels is not None
            tasks = {a: self.scenario.task_for(a).instruction for a in self._budgets}
            self._channel_sweeper = self._channel_factory(
                config=self.config.channels,
                profile=self.monitor.channels,
                extractor=self.monitor.extract,
                texts=self.monitor.texts,
                client=self.client,
                store=self.store,
                log=self._channel_log,
                tasks=tasks,
            )
        return self._channel_sweeper

    def _sweeper(self, key: Key) -> Sweeper:
        if key not in self._sweepers:
            self._sweepers[key] = self._build_sweeper(key, self.monitor.sweeps[key[0]])
        return self._sweepers[key]

    def _scorer(self, agent_id: str, flags: Sequence[Event]) -> Sweeper | None:
        """The sweeper that scores a review: the review profile over the agent's trajectory,
        with the settings of the sweep that raised the flags."""
        if not self._sweep_configs:
            return None
        names = [str(e.data.get("source")) for e in flags if e.data.get("layer") == "sweep"]
        name = next((n for n in names if n in self.monitor.sweeps), self._sweep_configs[0].name)
        profile = self.monitor.review or self.monitor.sweeps[name]
        key = (name, agent_id)
        if key not in self._scorers:
            self._scorers[key] = self._build_sweeper(key, profile)
        return self._scorers[key]

    def _build_sweeper(self, key: Key, profile: Profile) -> Sweeper:
        name, agent_id = key
        config = next(c for c in self._sweep_configs if c.name == name)
        return self._sweeper_factory(
            run_id=self.source,
            agent_id=agent_id,
            config=config,
            profile=profile,
            texts=self.monitor.texts,
            client=self.client,
            store=self.store,
            trajectory=self._trajectory(agent_id),
            task=self.scenario.task_for(agent_id).instruction,
            extractor=self.monitor.extract,
        )

    def _may_spend(self, estimate: float = 0.0) -> bool:
        limit = self.monitor.budget_usd - self.monitor.judge_reserve_usd
        return self._spent + estimate < limit

    async def _out_of_budget(self) -> None:
        if self._signals_only:
            return
        self._signals_only = True
        self._pending.clear()
        self._notify_swept()
        reserve = self.monitor.judge_reserve_usd
        reason = f"the monitor spent ${self._spent:.4f} of its ${self.monitor.budget_usd:g}"
        if reserve:
            reason += f", ${reserve:g} of it kept for the judge"
        await self._degrade("budget", reason, "signals_only")

    async def _sweeps_failed(self, error: str | None) -> None:
        then = self.config.policy.on_monitor_failure
        if then is None:
            then = "continue" if self.mode == "observe" else "pause"
        await self._degrade("sweep", f"{FAILURES} sweeps in a row failed", then, error, once=False)
        if then == "pause":
            agents = tuple(a for a, st in self._agents.items() if st.active)
            if agents:
                reason = f"{FAILURES} sweeps in a row failed"
                await self._act(Decision("pause", agents, (), reason), degraded=True)

    # Acting: write the decision, then carry it out.

    async def _act(self, d: Decision, *, degraded: bool = False) -> None:
        if self._killed or self._closed:
            return
        pause = None
        if d.action == "pause":
            d = self._pausable(d)
            if not d.agents:
                return
            if not self.simulated:
                pause = self._new_pause(d, degraded)
        action = MonitorAction(
            action=d.action,
            agents=list(d.agents),
            flags=list(d.flags),
            mode=self.mode,
            simulated=self.simulated,
            reason=d.reason,
        )
        written = await self._append([Draft(action, _one(d.agents))])
        self._actions.extend(written)
        if self.simulated:
            return
        if d.action == "kill":
            self._killed = True
            for pause in list(self._pauses):
                self._drop(pause)
            await self.actuator.kill(d.reason)
        elif pause is not None:
            try:
                await self.actuator.pause(list(d.agents), d.reason)
            except BaseException:
                self._drop(pause)
                raise
            if pause in self._pauses:
                pause.task = asyncio.create_task(self._settled(pause))
                self._pause_tasks.add(pause.task)
                pause.task.add_done_callback(self._pause_tasks.discard)
        else:
            await self.actuator.resume(list(d.agents), "deny" if d.action == "deny" else "continue")

    def _new_pause(self, d: Decision, degraded: bool) -> _Pause:
        """Register the pause before any await, so a decision taken meanwhile joins it."""
        timeout = (
            self.config.policy.human_timeout_s
            if self.mode == "human"
            else self.config.policy.pause_timeout_s
        )
        flags = [e for e in self._flags if e.seq in set(d.flags)]
        deadline = asyncio.get_running_loop().time() + timeout
        pause = _Pause(list(d.agents), flags, deadline, degraded=degraded)
        self._pauses.append(pause)
        return pause

    def _pausable(self, d: Decision) -> Decision:
        """The decision for the agents still to pause; flags on agents a monitor pause already
        holds join that pause and its review."""
        if self.simulated:
            return d
        fresh: list[str] = []
        flags = [e for e in self._flags if e.seq in set(d.flags)]
        for agent_id in d.agents:
            st = self._agents.get(agent_id)
            if st is None or not st.active:
                continue
            pause = self._pause_of(agent_id)
            if pause is None:
                fresh.append(agent_id)
            else:
                known = {e.seq for e in pause.flags}
                pause.flags += [e for e in flags if e.seq not in known]
                pause.version += 1
        return replace(d, agents=tuple(fresh))

    def _pause_of(self, agent_id: str) -> _Pause | None:
        return next((p for p in self._pauses if agent_id in p.agents), None)

    def _drop(self, pause: _Pause, *, cancel: bool = True) -> None:
        if pause in self._pauses:
            self._pauses.remove(pause)
        if cancel and pause.task is not None and pause.task is not asyncio.current_task():
            pause.task.cancel()

    def _release(self, agents: Sequence[str]) -> None:
        """An operator resumed or killed `agents`: the monitor's pauses of them end."""
        for pause in list(self._pauses):
            pause.agents = [a for a in pause.agents if a not in agents]
            if not pause.agents:
                self._drop(pause)

    async def _settled(self, pause: _Pause) -> None:
        try:
            await self._settle(pause)
        except Exception:
            log.exception("the pause of %s in run %s failed", pause.agents, self.run_id)

    async def _settle(self, pause: _Pause) -> None:
        """Hold a pause until it is cleared, confirmed, answered or timed out."""
        timeout = (
            self.config.policy.human_timeout_s
            if self.mode == "human"
            else self.config.policy.pause_timeout_s
        )
        outcome: tuple[str, str] | None = None
        result: ReviewResult | None = None
        try:
            async with asyncio.timeout_at(pause.deadline):
                try:
                    if self.mode == "human":
                        await self._ask(pause)
                    else:
                        if pause.degraded:
                            why = await self._retry(pause)
                            if not pause.flags:
                                outcome = ("resume", why)
                        while outcome is None:
                            version = pause.version
                            result = await self._review(pause)
                            if result.confirmed:
                                outcome = ("kill", result.reason)
                                break
                            if pause.version == version:
                                break
                        if outcome is None and result is not None and result.clear:
                            outcome = ("clear", result.reason)
                except Exception:
                    log.exception("the review of %s in run %s failed", pause.agents, self.run_id)
                    outcome = result = None
                if outcome is None:
                    await asyncio.Event().wait()
        except TimeoutError:
            why = (
                "no one answered the pause" if self.mode == "human" else "the pause was not cleared"
            )
            detail = f": {result.reason}" if result is not None else ""
            outcome = ("kill", f"{why} within {timeout:g} s{detail}")
        if outcome is None or pause not in self._pauses:
            return
        self._drop(pause, cancel=False)
        seqs = tuple(e.seq for e in pause.flags)
        kind, reason = outcome
        if kind == "kill":
            await self._act(Decision("kill", tuple(pause.agents), seqs, reason))
            return
        if kind == "clear" and seqs:
            cleared = MonitorCleared(
                flags=list(seqs),
                source="review",
                reason=reason,
                suspicion=None if result is None else result.suspicion,
            )
            await self._append([Draft(cleared, _one(pause.agents))])
        agents = tuple(a for a in pause.agents if a not in self._operator_paused)
        if agents:
            await self._act(Decision("resume", agents, seqs, reason))

    async def _review(self, pause: _Pause) -> ReviewResult:
        """Review each flagged agent of the pause; the pause clears only if all clear."""
        flags = list(pause.flags)
        subjects = list(dict.fromkeys(e.agent_id for e in flags if e.agent_id is not None))
        if not subjects or any(e.agent_id is None for e in flags):
            subjects = list(dict.fromkeys([*subjects, *pause.agents]))
        cutoffs = self.policy.cutoffs_of(flags)
        results = []
        for agent_id in subjects:
            own = [e for e in flags if e.agent_id in (agent_id, None)]
            results.append(await self._review_one(agent_id, own, cutoffs))
        confirmed = next((r for r in results if r.confirmed), None)
        if confirmed is not None:
            return confirmed
        blocking = next((r for r in results if not r.clear), None)
        if blocking is not None or results:
            return blocking or results[0]
        return ReviewResult(False, "the pause names no agent to review")

    async def _review_one(
        self, agent_id: str, flags: list[Event], cutoffs: Cutoffs
    ) -> ReviewResult:
        st = self._agent(agent_id)
        held_id = self.actuator.held_call(agent_id)
        held = None if held_id is None else st.calls.get(held_id)
        sample = self.actuator.next_prompt(agent_id) if self.config.review.resamples else None
        result = await self._reviewer.review(
            flags,
            scorer=self._scorer(agent_id, flags),
            cutoffs=cutoffs,
            actions=await self._flagged_calls(agent_id, flags),
            held=held,
            sample=sample,
            allowed=self._may_spend,
        )
        self._spent += result.cost_usd
        return result

    async def _flagged_calls(self, agent_id: str, flags: Sequence[Event]) -> list[Event]:
        """The tool calls the flags cite; else the agent's calls in the steps they cite."""
        seqs = sorted({s for e in flags for s in MonitorFlag.model_validate(e.data).evidence})
        if not seqs:
            return []
        lo, hi = seqs[0], seqs[-1]
        cited: list[Event] = []
        for seq in seqs:
            found = await self.store.events(
                self.source, after=seq - 1, before=seq + 1, types=["tool.call"], agent_id=agent_id
            )
            cited += [e for e in found if not self._own(e)]
        if cited:
            return cited[-ACTION_CALLS:]
        start = self.policy.step_start(agent_id, lo)
        if start is None:
            return []
        calls = await self.store.events(
            self.source,
            after=start,
            before=hi + 1,
            types=["tool.call"],
            agent_id=agent_id,
            limit=ACTION_CALLS,
        )
        return [e for e in calls if not self._own(e)]

    def _own(self, event: Event) -> bool:
        return event.data.get("model_call") in self._own_calls

    async def _ask(self, pause: _Pause) -> None:
        advice = None
        if pause.degraded:
            advice = f"{FAILURES} sweeps in a row failed; the monitor is not reviewing the agents."
        elif self._sweep_configs:
            advice = (await self._review(pause)).advice()
        held = {a: c for a in pause.agents if (c := self.actuator.held_call(a)) is not None}
        request = HumanRequest(
            run_id=self.run_id,
            agents=list(pause.agents),
            flags=list(pause.flags),
            advice=advice,
            timeout_s=self.config.policy.human_timeout_s,
            held_call=held,
        )
        await self.actuator.ask_human(request)

    async def _retry(self, pause: _Pause) -> str:
        """Retry the sweeps of a pause the failing sweeps caused, until one works or the
        monitor goes on with signals only. Returns why the pause may end."""
        interval = min(RETRY_S, self.config.policy.pause_timeout_s / 5)
        while True:
            await asyncio.sleep(interval)
            if not self._sweeps_on:
                return "the monitor goes on with signals only"
            for agent_id in pause.agents:
                self._request([(agent_id, "review", None)])
            while self._sweeps_on and (self._pending or self._running is not None):
                await self._swept.wait()
            if self._failures == 0:
                return "the sweeps work again"

    # Writing.

    async def _append(self, drafts: Sequence[Draft]) -> list[Event]:
        if not drafts:
            return []
        return await self.store.append(self.run_id, drafts)

    async def _degrade(
        self,
        part: Literal["sweep", "judge", "review", "signal", "budget"],
        reason: str,
        then: Literal["continue", "pause", "signals_only", "skip"],
        detail: str | None = None,
        *,
        once: bool = True,
    ) -> None:
        if once and part in self._degraded:
            return
        self._degraded.add(part)
        degraded = MonitorDegraded(part=part, reason=reason, then=then, detail=detail)
        await self._append([Draft(degraded)])

    def _with_taint(self, draft: Draft) -> Draft:
        payload = draft.payload
        if not isinstance(payload, MonitorVerdict) or payload.tainted:
            return draft
        tainted = sorted({s for a in self._agents for s in self.policy.tainted(a)})
        if not tainted:
            return draft
        return Draft(payload.model_copy(update={"tainted": tainted}), draft.agent_id)

    # Per-agent state.

    def _agent(self, agent_id: str) -> _Agent:
        st = self._agents.get(agent_id)
        if st is None:
            st = self._agents[agent_id] = _Agent(self._budgets.get(agent_id, Budget()))
            self._trajectory(agent_id)
        return st

    def _trajectory(self, agent_id: str) -> Trajectory:
        t = self._trajectories.get(agent_id)
        if t is None:
            t = self._trajectory_factory(self.source, agent_id, self.store, self._key)
            self._trajectories[agent_id] = t
        return t

    def _trajectories_of(self, agent_id: str | None) -> list[Trajectory]:
        if agent_id is None:
            return list(self._trajectories.values())
        return [self._trajectory(agent_id)]


def _one(agents: Sequence[str]) -> str | None:
    return agents[0] if len(agents) == 1 else None


def _sweep_error(written: Sequence[Event]) -> str | None:
    errors = [str(e.data["error"]) for e in written if e.data.get("error")]
    return "; ".join(dict.fromkeys(errors)) or None
