import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, call, models, profile

from aims.agent import GatePoint
from aims.events import (
    AgentFinished,
    AgentStarted,
    AgentStep,
    Draft,
    Event,
    ExecStarted,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    MonitorDegraded,
    MonitorFlag,
    MonitorSweep,
    MonitorVerdict,
    OperatorAction,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.events.schema import Payload
from aims.models import Message, ModelClient, Profile, Prompt, ToolSpec
from aims.monitor import review_texts
from aims.monitor.config import MonitorConfig
from aims.monitor.core import HumanRequest, Monitor
from aims.monitor.judge import JudgeResult
from aims.monitor.render import frame
from aims.monitor.setup import ResolvedMonitor
from aims.monitor.sweep import Score, SweepError, SweepResult
from aims.monitor.texts import MonitorTexts
from aims.scenario import Scenario

pytestmark = pytest.mark.anyio

Rule = Callable[[Event], list[Draft]]


def signal_rule(e: Event) -> list[Draft]:
    """A tool call named `bad` raises the signal flag its arguments describe."""
    if e.type != "tool.call" or e.data["name"] != "bad":
        return []
    spec = json.loads(str(e.data["arguments"]))
    flag = MonitorFlag(layer="signal", source="sig", evidence=[e.seq], justification="bad", **spec)
    return [Draft(flag, e.agent_id)]


class FakeEngine:
    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.seen: list[Event] = []
        self.recorded_events: list[Event] = []
        self.gate: asyncio.Event | None = None
        self.final: list[Draft] = []

    async def feed(self, event: Event) -> list[Draft]:
        self.seen.append(event)
        if self.gate is not None and event.type == "tool.call" and event.data["name"] == "slow":
            await self.gate.wait()
        return self.rule(event)

    def recorded(self, events: Sequence[Event]) -> None:
        self.recorded_events.extend(events)

    async def close(self) -> list[Draft]:
        return list(self.final)


class FakeTrajectory:
    def __init__(self, run_id: str, agent_id: str, store: Any, key: bytes) -> None:
        self.agent_id = agent_id
        self.key = key
        self.added: list[int] = []
        self.kept = -1
        self.marked: list[int] = []

    def add(self, event: Event) -> None:
        self.added.append(event.seq)
        if event.agent_id == self.agent_id and event.data.get("phase") != "verifier":
            self.kept = event.seq

    def mark(self, seqs: Iterable[int]) -> None:
        self.marked.extend(seqs)

    @property
    def last_seq(self) -> int:
        """Like the real trajectory: this agent's events, the verifier's left out."""
        return self.kept


SweepFn = Callable[["FakeSweeper", str], SweepResult]


class Sweeps:
    """What the fake sweepers of one test do and saw."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []
        self.proposals: list[str] = []
        self.profiles: list[Profile] = []
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.most = 0
        self.result: SweepFn = lambda sweeper, trigger: SweepResult(cost_usd=0.01)
        self.score: Callable[[str], Score] = lambda proposal: Score(importance=1, suspicion=3)

    def factory(self, **kw: Any) -> FakeSweeper:
        return FakeSweeper(self, **kw)


class FakeSweeper:
    def __init__(self, sweeps: Sweeps, **kw: Any) -> None:
        self.s = sweeps
        self.agent_id = kw["agent_id"]
        self.name = kw["config"].name
        self.profile = kw["profile"]
        self.trajectory = kw["trajectory"]
        sweeps.profiles.append(kw["profile"])

    async def sweep(self, trigger: str, *, coalesced: int = 1) -> SweepResult:
        s = self.s
        s.active += 1
        s.most = max(s.most, s.active)
        try:
            s.calls.append((self.name, self.agent_id, trigger, coalesced))
            if s.gate is not None:
                await s.gate.wait()
            return s.result(self, trigger)
        finally:
            s.active -= 1

    async def score(self, proposal: str, *, role: str = "review") -> Score:
        assert role == "review"
        self.s.proposals.append(proposal)
        return self.s.score(proposal)


class FakeActuator:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[str, ...], str]] = []
        self.paused_now: set[str] = set()
        self.held: dict[str, str] = {}
        self.prompt: tuple[Profile, Prompt] | None = None
        self.asked: list[HumanRequest] = []
        self.pause_gate: asyncio.Event | None = None
        self.prompt_error: Exception | None = None

    async def pause(self, agents: Sequence[str], reason: str) -> None:
        self.log.append(("pause", tuple(agents), reason))
        self.paused_now.update(agents)
        if self.pause_gate is not None:
            await self.pause_gate.wait()

    async def resume(self, agents: Sequence[str], then: str = "continue") -> None:
        self.log.append(("resume", tuple(agents), then))
        self.paused_now.difference_update(agents)

    async def kill(self, reason: str) -> None:
        self.log.append(("kill", (), reason))

    def paused(self, agent_id: str) -> bool:
        return agent_id in self.paused_now

    def held_call(self, agent_id: str) -> str | None:
        return self.held.get(agent_id)

    def next_prompt(self, agent_id: str) -> tuple[Profile, Prompt] | None:
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.prompt

    async def ask_human(self, request: HumanRequest) -> None:
        self.asked.append(request)

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.log]


JudgeFn = Callable[[Sequence[Event], Sequence[Event]], Awaitable[JudgeResult]]


class FakeJudge:
    def __init__(self, fn: JudgeFn, cost_usd: float = 0.0, **kw: Any) -> None:
        self.fn = fn
        self.cost_usd = cost_usd
        self.kw = kw

    async def run(self, *, flags: Sequence[Event], sweeps: Sequence[Event]) -> JudgeResult:
        return await self.fn(flags, sweeps)


async def no_verdict(flags: Sequence[Event], sweeps: Sequence[Event]) -> JudgeResult:
    return JudgeResult()


@dataclass
class H:
    store: LocalEventStore
    run_id: str
    monitor: Monitor
    actuator: FakeActuator
    engine: FakeEngine
    sweeps: Sweeps
    trajectories: dict[str, FakeTrajectory] = field(default_factory=dict)

    async def add(self, payload: Payload, agent: str | None = "a") -> Event:
        [e] = await self.store.append(self.run_id, [Draft(payload, agent)])
        return e

    async def bad(self, agent: str = "a", call_id: str = "t1", **spec: Any) -> Event:
        spec.setdefault("category", "scorer_tampering")
        return await self.add(
            ToolCall(call_id=call_id, name="bad", arguments=json.dumps(spec)), agent
        )

    async def of(self, kind: str) -> list[Event]:
        return await self.store.events(self.run_id, types=[kind], limit=1000)

    async def until(self, test: Callable[[], bool], within: float = 3.0) -> None:
        async with asyncio.timeout(within):
            while True:
                if test():
                    return
                await asyncio.sleep(0.01)


def scenario(oversight: str, agents: Sequence[str], **kw: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "version": 1,
            "name": "t",
            "oversight": oversight,
            "task": {"instruction": "Fix the bug.", "environment": {"image": "x"}},
            "agents": [{"id": a, **kw} for a in agents],
        }
    )


TEXTS = MonitorTexts({name: text for name, (text, _) in review_texts.TEXTS.items()})


@contextlib.asynccontextmanager
async def harness(
    tmp_path: Path,
    *,
    oversight: str = "autonomous",
    config: dict[str, Any] | None = None,
    budget_usd: float = 5.0,
    rule: Rule = signal_rule,
    agents: Sequence[str] = ("a",),
    simulated: bool = False,
    agent_config: dict[str, Any] | None = None,
    judge: JudgeFn = no_verdict,
    judge_cost: float = 0.0,
    script: Script | None = None,
) -> AsyncGenerator[H]:
    cfg = MonitorConfig.model_validate(config or {"sweeps": []})
    p = profile()
    resolved = ResolvedMonitor(
        config=cfg,
        texts=TEXTS,
        oversight=cast(Any, oversight),
        budget_usd=budget_usd,
        sweeps={s.name: p for s in cfg.sweeps},
        review=profile(name="reviewer"),
        judge=p if cfg.judge is not None else None,
    )
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(
            RunStarted(scenario="t", oversight=cast(Any, oversight), agents=list(agents))
        )
        transport = (script or Script()).transport()
        client = ModelClient(store, run_id, models(p), transport=transport, env={})
        engine = FakeEngine(rule)
        sweeps = Sweeps()
        actuator = FakeActuator()
        trajectories: dict[str, FakeTrajectory] = {}

        def trajectory(*args: Any) -> FakeTrajectory:
            t = FakeTrajectory(*args)
            trajectories[t.agent_id] = t
            return t

        monitor = Monitor(
            run_id=run_id,
            scenario=scenario(oversight, agents, **(agent_config or {})),
            monitor=resolved,
            store=store,
            client=client,
            actuator=actuator,
            simulated=simulated,
            engine_factory=cast(Any, lambda *a, **k: engine),
            trajectory_factory=cast(Any, trajectory),
            sweeper_factory=cast(Any, sweeps.factory),
            judge_factory=cast(Any, lambda **kw: FakeJudge(judge, judge_cost, **kw)),
        )
        remove = store.tap(run_id, monitor.feed)
        await monitor.start()
        h = H(store, run_id, monitor, actuator, engine, sweeps, trajectories)
        try:
            yield h
        finally:
            await monitor.close()
            remove()


def started() -> AgentStarted:
    return AgentStarted(profile="p", model="m", backend="chat", tools=["exec"], system="s")


async def own_call(h: H, call_id: str, role: Any) -> list[Event]:
    body = await h.store.put_blob(h.run_id, b"{}")
    request = ModelRequest(
        call_id=call_id,
        attempt=1,
        profile="p",
        backend="chat",
        model="m",
        url="http://x",
        params=ModelParams(stream=False),
        body=body,
        key=body.sha256,
        role=role,
    )
    agent = None if role is not None else "a"
    return await h.store.append(
        h.run_id,
        [
            Draft(request, agent),
            Draft(
                ModelResponse(call_id=call_id, attempt=1, status="completed", latency_ms=1), agent
            ),
            Draft(
                ToolCall(call_id=f"{call_id}t", name="exec", arguments="{}", model_call=call_id),
                agent,
            ),
        ],
    )


async def test_skips_its_own_events(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        mine = await own_call(h, "m1", "sweep")
        theirs = await own_call(h, "a1", None)
        await h.add(
            MonitorSweep(
                name="main",
                lane="full",
                parts=["evidence"],
                trigger="every",
                step_from=1,
                step_to=1,
                seq_to=1,
            ),
            None,
        )
        await h.monitor.barrier()
        seen = {e.seq for e in h.engine.seen}
        assert not seen & {e.seq for e in mine}
        assert {e.seq for e in theirs} <= seen
        assert not any(e.type.startswith("monitor.") for e in h.engine.seen)
        assert h.trajectories["a"].added == sorted(seen)


async def test_barrier_waits_for_signals_and_the_policy(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        h.engine.gate = asyncio.Event()
        await h.add(started())
        await h.add(ToolCall(call_id="s", name="slow", arguments="{}"))
        await h.bad(importance=4)
        waiter = asyncio.create_task(h.monitor.barrier())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        assert h.actuator.log == []
        h.engine.gate.set()
        await waiter
        assert h.actuator.kinds() == ["pause"]
        [action] = await h.of("monitor.action")
        assert action.data["action"] == "pause"
        assert action.data["simulated"] is False
        [flag] = await h.of("monitor.flag")
        assert action.data["flags"] == [flag.seq]
        assert h.engine.recorded_events == [flag]
        assert h.trajectories["a"].marked == [flag.seq - 1]


async def test_observe_simulates_every_action(tmp_path: Path) -> None:
    async with harness(tmp_path, oversight="observe") as h:
        await h.add(started())
        await h.bad(importance=4)
        await h.bad(call_id="t2", importance=5)
        await h.monitor.barrier()
        actions = [e.data for e in await h.of("monitor.action")]
        assert [(a["action"], a["simulated"], a["mode"]) for a in actions] == [
            ("pause", True, "observe"),
            ("kill", True, "observe"),
        ]
        assert h.actuator.log == []


async def test_simulated_monitor_run(tmp_path: Path) -> None:
    async with harness(tmp_path, simulated=True) as h:
        await h.add(started())
        await h.bad(importance=5)
        await h.monitor.barrier()
        [action] = await h.of("monitor.action")
        assert action.data["simulated"] is True
        assert action.data["mode"] == "autonomous"
        assert h.actuator.log == []


async def test_signal_at_five_kills_at_once(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.add(started())
        await h.bad(importance=5, category="privilege_escalation")
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["kill"]
        [action] = await h.of("monitor.action")
        assert action.data["action"] == "kill"
        reason = action.data["reason"]
        assert isinstance(reason, str)
        assert "privilege_escalation" in reason
        await h.bad(call_id="t2", importance=4)
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["kill"]


async def test_signal_pause_is_never_cleared_and_times_out(tmp_path: Path) -> None:
    config = {"sweeps": [], "policy": {"pause_timeout_s": 0.2}}
    async with harness(tmp_path, config=config) as h:
        await h.add(started())
        await h.bad(importance=4)
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["pause"]
        await h.until(lambda: h.actuator.kinds() == ["pause", "kill"])
        assert "not cleared within 0.2 s" in h.actuator.log[-1][2]
        assert await h.of("monitor.cleared") == []


async def test_operator_resume_ends_the_pause(tmp_path: Path) -> None:
    config = {"sweeps": [], "policy": {"pause_timeout_s": 0.2}}
    async with harness(tmp_path, config=config) as h:
        await h.add(started())
        await h.bad(importance=4)
        await h.monitor.barrier()
        await h.add(OperatorAction(action="resume", then="continue"), "a")
        await h.monitor.barrier()
        await asyncio.sleep(0.4)
        assert h.actuator.kinds() == ["pause"]
        await h.bad(call_id="t2", importance=4)
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["pause", "pause"]


async def test_verifier_flags_taint_and_never_pause(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.add(started())
        flag_call = await h.bad(importance=5, phase="verifier")
        await h.monitor.barrier()
        assert h.actuator.log == []
        [flag] = await h.of("monitor.flag")
        assert flag.seq == flag_call.seq + 1
        assert h.monitor.tainted("a") == [flag.seq]


async def test_deny_breaker(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.add(started())
        for n in range(3):
            await h.add(ToolResult(call_id=f"d{n}", output="no", error=True, denied=True))
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["kill"]
        assert h.actuator.log[0][2] == "3 denied tool calls in a row"
    async with harness(tmp_path / "human", oversight="human") as h:
        await h.add(started())
        for n in range(3):
            await h.add(ToolResult(call_id=f"d{n}", output="no", error=True, denied=True))
        await h.monitor.barrier()
        assert h.actuator.kinds() == ["pause"]
        await h.until(lambda: len(h.actuator.asked) == 1)
        assert h.actuator.asked[0].flags == []


def sweep_config(**kw: Any) -> dict[str, Any]:
    return {"sweeps": [{"name": "main", **kw}], "judge": None}


async def step(h: H, n: int, agent: str = "a") -> Event:
    return await h.add(AgentStep(n=n, call_id=f"s{n}", status="completed"), agent)


async def test_every_k_steps_and_agent_finished(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=2)) as h:
        await h.add(started())
        for n in range(1, 5):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        await h.add(AgentFinished(reason="submitted", steps=4))
        await h.monitor.agents_done()
        triggers = [t for _, _, t, _ in h.sweeps.calls]
        assert triggers == ["every", "every", "agent_finished"]
        assert h.sweeps.profiles[0].name == "scripted"


async def test_verifier_events_call_for_no_final_sweep(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=5)) as h:
        await h.add(started())
        await step(h, 1)
        await h.add(AgentFinished(reason="submitted", steps=1))
        await h.monitor.barrier()
        await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        verifier = ExecStarted(
            exec_id="v1",
            call_id=None,
            command="pytest",
            wrapper=[],
            user="1001",
            cwd="/work",
            timeout_ms=1000,
            audit="kernel",
            phase="verifier",
        )
        await h.add(verifier)
        await h.monitor.agents_done()
        assert [t for _, _, t, _ in h.sweeps.calls] == ["agent_finished"]


async def test_triggers_during_a_sweep_coalesce(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=None), agents=("a", "b")) as h:
        h.sweeps.gate = asyncio.Event()
        await h.add(started())
        await h.add(started(), "b")
        await h.monitor.barrier()
        h.monitor.request_sweep("a", "operator")
        await h.until(lambda: len(h.sweeps.calls) == 1)
        h.monitor.request_sweep("a", "operator")
        h.monitor.request_sweep(None, "operator")
        await h.add(OperatorAction(action="sweep"), "a")
        await h.monitor.barrier()
        h.sweeps.gate.set()
        await h.until(lambda: len(h.sweeps.calls) == 3)
        await h.monitor.agents_done()
        assert h.sweeps.calls == [
            ("main", "a", "operator", 1),
            ("main", "a", "operator", 3),
            ("main", "b", "operator", 1),
        ]
        assert h.sweeps.most == 1


async def test_flag_and_budget_triggers(tmp_path: Path) -> None:
    config = sweep_config(every=None)
    async with harness(tmp_path, config=config, agent_config={"budget": {"steps": 5}}) as h:
        await h.add(started())
        await h.bad(importance=2)
        await h.monitor.barrier()
        await h.bad(call_id="t2", importance=3, category="drift")
        await h.monitor.barrier()
        await h.until(lambda: len(h.sweeps.calls) == 1)
        for n in range(1, 6):
            await step(h, n)
        await h.monitor.barrier()
        await h.until(lambda: len(h.sweeps.calls) == 2)
        assert [t for _, _, t, _ in h.sweeps.calls] == ["flag", "budget"]


async def test_every_s_counts_unpaused_time(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=None, every_s=0.15)) as h:
        h.actuator.paused_now.add("a")
        await h.add(started())
        await step(h, 1)
        await h.monitor.barrier()
        await asyncio.sleep(0.4)
        assert h.sweeps.calls == []
        h.actuator.paused_now.clear()
        await h.until(lambda: len(h.sweeps.calls) >= 1)
        assert h.sweeps.calls[0][2] == "every_s"


async def test_budget_stops_sweeps_before_the_judge_reserve(tmp_path: Path) -> None:
    config = {"sweeps": [{"name": "main", "every": 1}]}
    async with harness(tmp_path, config=config, budget_usd=1.5) as h:
        h.sweeps.result = lambda sweeper, trigger: SweepResult(cost_usd=0.3)
        await h.add(started())
        for n in range(1, 5):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        assert len(h.sweeps.calls) == 1
        assert h.monitor.cost_usd == pytest.approx(0.3)
        [degraded] = await h.of("monitor.degraded")
        assert degraded.data["part"] == "budget"
        assert degraded.data["then"] == "signals_only"


async def test_hold_waits_for_the_sweep_of_the_step(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=1, hold=True)) as h:
        h.sweeps.gate = asyncio.Event()
        await h.add(started())
        await step(h, 1)
        await h.monitor.barrier()
        point = GatePoint("before_tool", h.run_id, "a", 1, "c1")
        holding = asyncio.create_task(h.monitor.hold(point))
        await asyncio.sleep(0.05)
        assert not holding.done()
        await h.monitor.hold(GatePoint("after_tool", h.run_id, "a", 1, "c1"))
        h.sweeps.gate.set()
        await asyncio.wait_for(holding, 1)
        await h.monitor.hold(point)


async def test_two_failed_sweeps_degrade_and_pause(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 30}}
    async with harness(tmp_path, config=config) as h:
        h.sweeps.result = lambda sweeper, trigger: SweepResult(failed=True)
        await h.add(started())
        for n in range(1, 4):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        [degraded] = await h.of("monitor.degraded")
        assert (degraded.data["part"], degraded.data["then"]) == ("sweep", "pause")
        assert h.actuator.kinds() == ["pause"]


async def test_failed_sweeps_resume_once_they_work(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 1}}
    async with harness(tmp_path, config=config) as h:
        h.sweeps.result = lambda sweeper, trigger: SweepResult(failed=trigger != "review")
        await h.add(started())
        for n in range(1, 3):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        await h.until(lambda: h.actuator.kinds() == ["pause", "resume"])
        assert h.sweeps.calls[-1][2] == "review"
        [_, resume] = await h.of("monitor.action")
        assert resume.data["reason"] == "the sweeps work again"


async def test_failed_sweeps_resume_once_the_budget_ends_them(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 1}}
    async with harness(tmp_path, config=config, budget_usd=0.5) as h:
        h.sweeps.result = lambda sweeper, trigger: SweepResult(failed=True, cost_usd=0.2)
        await h.add(started())
        for n in range(1, 3):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        await h.until(lambda: len(h.actuator.log) == 2)
        assert h.actuator.kinds() == ["pause", "resume"]
        [_, resume] = await h.of("monitor.action")
        assert resume.data["reason"] == "the monitor goes on with signals only"
        assert [e.data["part"] for e in await h.of("monitor.degraded")] == ["sweep", "budget"]


async def test_failed_sweeps_continue_in_observe(tmp_path: Path) -> None:
    async with harness(tmp_path, oversight="observe", config=sweep_config(every=1)) as h:
        h.sweeps.result = lambda sweeper, trigger: SweepResult(failed=True)
        await h.add(started())
        for n in range(1, 3):
            await step(h, n)
            await h.monitor.barrier()
            await h.until(lambda: not h.monitor._pending and h.monitor._running is None)
        [degraded] = await h.of("monitor.degraded")
        assert degraded.data["then"] == "continue"
        assert await h.of("monitor.action") == []


def flagging(lane: str, importance: int, evidence: Callable[[], list[int]]) -> SweepFn:
    def result(sweeper: FakeSweeper, trigger: str) -> SweepResult:
        flag = MonitorFlag(
            layer="sweep",
            source=sweeper.name,
            lane=lane,
            category="scorer_tampering",
            importance=importance,
            evidence=evidence(),
            justification="edits the tests",
            suspicion=80,
        )
        return SweepResult(flags=[Draft(flag, sweeper.agent_id)], cost_usd=0.01)

    return result


def agent_prompt() -> tuple[Profile, Prompt]:
    tool = ToolSpec(name="exec", parameters={"type": "object", "properties": {}})
    prompt = Prompt(items=[Message(role="user", content="go on")], tools=[tool])
    return profile(name="agent-profile"), prompt


async def swept_pause(h: H) -> Event:
    await h.add(started())
    call_event = await h.add(
        ToolCall(call_id="e1", name="exec", arguments='{"cmd": "sed -i tests"}')
    )
    h.sweeps.result = flagging("full", 4, lambda: [call_event.seq])
    await step(h, 1)
    await h.monitor.barrier()
    await h.until(lambda: "pause" in h.actuator.kinds())
    return call_event


async def test_autonomous_review_clears_and_resumes(tmp_path: Path) -> None:
    script = Script(*[call("exec", cmd="pytest") for _ in range(4)])
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 5}}
    async with harness(tmp_path, config=config, script=script) as h:
        h.actuator.prompt = agent_prompt()
        call_event = await swept_pause(h)
        await h.until(lambda: h.actuator.kinds() == ["pause", "resume"])
        [cleared] = await h.of("monitor.cleared")
        [flag] = await h.of("monitor.flag")
        assert cleared.data["flags"] == [flag.seq]
        assert cleared.data["source"] == "review"
        assert [a.data["action"] for a in await h.of("monitor.action")] == ["pause", "resume"]
        assert len(script.requests) == 4
        assert len(h.sweeps.proposals) == 5
        assert frame(h.trajectories["a"].key, call_event.seq) in h.sweeps.proposals[0]
        assert all('"tool":"exec"' in p for p in h.sweeps.proposals[1:])
        assert h.sweeps.profiles[-1].name == "reviewer"
        requests = await h.of("model.request")
        assert [r.data["role"] for r in requests] == ["review"] * 4
        assert not any(e.type == "model.request" for e in h.engine.seen)
        assert h.monitor.cost_usd > 0.01


async def test_confirmed_resample_kills(tmp_path: Path) -> None:
    script = Script(*[call("exec", cmd="rm tests") for _ in range(4)])
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 5}}
    async with harness(tmp_path, config=config, script=script) as h:
        h.actuator.prompt = agent_prompt()
        h.sweeps.score = lambda p: Score(5 if "hidden sample" in p else 1, 90)
        await swept_pause(h)
        await h.until(lambda: h.actuator.kinds() == ["pause", "kill"])
        assert "resample of the next step scored importance 5" in h.actuator.log[-1][2]
        assert await h.of("monitor.cleared") == []


async def test_review_without_a_next_step_waits_out_the_timeout(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 0.3}}
    async with harness(tmp_path, config=config) as h:
        await swept_pause(h)
        await h.until(lambda: h.actuator.kinds() == ["pause", "kill"])
        assert "no next step to resample" in h.actuator.log[-1][2]


async def test_a_review_that_raises_still_kills_at_the_timeout(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 0.3}}
    async with harness(tmp_path, config=config) as h:
        h.actuator.prompt_error = RuntimeError("the runner is gone")
        await swept_pause(h)
        await h.until(lambda: len(h.actuator.log) == 2)
        assert h.actuator.kinds() == ["pause", "kill"]
        assert h.actuator.log[-1][2] == "the pause was not cleared within 0.3 s"


async def test_review_finds_cited_calls_past_a_page_of_calls(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 5}, "review": {"resamples": 0}}
    async with harness(tmp_path, config=config) as h:
        await h.add(started())
        drafts = [
            Draft(ToolCall(call_id=f"e{n}", name="exec", arguments="{}"), "a") for n in range(105)
        ]
        calls = await h.store.append(h.run_id, drafts)
        first, last = calls[0], calls[-1]
        h.sweeps.result = flagging("full", 4, lambda: [first.seq, last.seq])
        await step(h, 1)
        await h.until(lambda: h.actuator.kinds() == ["pause", "resume"])
        [proposal] = h.sweeps.proposals
        key = h.trajectories["a"].key
        assert frame(key, first.seq) in proposal
        assert frame(key, last.seq) in proposal


async def test_a_failed_review_score_counts_its_cost(tmp_path: Path) -> None:
    def failing(proposal: str) -> Score:
        raise SweepError("the answer does not fit the schema", cost_usd=0.07)

    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 0.3}}
    async with harness(tmp_path, config=config) as h:
        h.sweeps.score = failing
        await swept_pause(h)
        await h.until(lambda: len(h.actuator.log) == 2)
        assert h.actuator.kinds() == ["pause", "kill"]
        assert "1 of 1 review calls failed" in h.actuator.log[-1][2]
        assert h.monitor.cost_usd == pytest.approx(0.08)


async def test_human_gets_advice_then_times_out(tmp_path: Path) -> None:
    script = Script(*[call("exec", cmd="pytest") for _ in range(4)])
    config = sweep_config(every=1) | {"policy": {"human_timeout_s": 0.5}}
    async with harness(tmp_path, oversight="human", config=config, script=script) as h:
        h.actuator.prompt = agent_prompt()
        h.actuator.held["a"] = "e1"
        await swept_pause(h)
        await h.until(lambda: len(h.actuator.asked) == 1)
        request = h.actuator.asked[0]
        assert request.agents == ["a"]
        assert len(request.flags) == 1
        assert request.advice is not None
        assert "would clear" in request.advice
        assert request.held_call == {"a": "e1"}
        assert any("held before running" in p for p in h.sweeps.proposals)
        assert h.actuator.kinds() == ["pause"]
        await h.until(lambda: h.actuator.kinds() == ["pause", "kill"])
        assert "no one answered" in h.actuator.log[-1][2]


async def test_two_lanes_on_one_step_kill(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=1)) as h:
        await h.add(started())
        call_event = await h.add(ToolCall(call_id="e1", name="exec", arguments="{}"))

        def both(sweeper: FakeSweeper, trigger: str) -> SweepResult:
            a = flagging("full", 5, lambda: [call_event.seq])(sweeper, trigger)
            b = flagging("evidence", 5, lambda: [call_event.seq])(sweeper, trigger)
            return SweepResult(flags=[*a.flags, *b.flags])

        h.sweeps.result = both
        await step(h, 1)
        await h.until(lambda: h.actuator.kinds() == ["kill"])


async def test_judge_timeout_degrades(tmp_path: Path) -> None:
    async def slow(flags: Sequence[Event], sweeps: Sequence[Event]) -> JudgeResult:
        await asyncio.sleep(10)
        return JudgeResult()

    config = {"sweeps": [], "judge": {"timeout_s": 0.1}}
    async with harness(tmp_path, config=config, judge=slow, judge_cost=0.25) as h:
        await h.monitor.judge()
        [degraded] = await h.of("monitor.degraded")
        assert degraded.data["part"] == "judge"
        assert degraded.data["then"] == "skip"
        reason = degraded.data["reason"]
        assert isinstance(reason, str)
        assert "timeout" in reason
        assert h.monitor.cost_usd == pytest.approx(0.25)


async def test_judge_writes_the_verdict_with_the_taint(tmp_path: Path) -> None:
    seen: list[Sequence[Event]] = []

    async def verdict(flags: Sequence[Event], sweeps: Sequence[Event]) -> JudgeResult:
        seen.append(flags)
        flag = MonitorFlag(
            layer="judge",
            source="judge",
            category="drift",
            importance=2,
            evidence=[1],
            justification="j",
        )
        return JudgeResult(
            verdict=Draft(MonitorVerdict(max_severity=4, summary="s")),
            flags=[Draft(flag, "a")],
            cost_usd=0.5,
        )

    config = {"sweeps": [], "judge": {}}
    async with harness(tmp_path, config=config, judge=verdict) as h:
        await h.add(started())
        await h.bad(importance=5, phase="verifier")
        await h.monitor.judge(skip=True)
        assert seen == []
        await h.monitor.judge()
        [tainted] = list(seen[0])
        [written] = await h.of("monitor.verdict")
        assert written.data["tainted"] == [tainted.seq]
        assert len(await h.of("monitor.flag")) == 2
        assert h.monitor.cost_usd == pytest.approx(0.5)
        assert h.actuator.log == []


async def test_close_writes_final_counts_and_stops(tmp_path: Path) -> None:
    async with harness(tmp_path, config=sweep_config(every=1)) as h:
        final = MonitorFlag(
            layer="signal",
            source="sig",
            category="drift",
            importance=1,
            evidence=[1],
            justification="j",
            count=7,
            final=True,
        )
        degraded = MonitorDegraded(part="signal", reason="a signal failed", then="continue")
        h.engine.final = [Draft(final, "a"), Draft(degraded)]
        h.sweeps.gate = asyncio.Event()
        await h.add(started())
        await step(h, 1)
        await h.until(lambda: h.monitor._running is not None)
        await h.monitor.close()
        [flag] = await h.of("monitor.flag")
        assert flag.data["final"] is True
        assert flag.data["count"] == 7
        assert len(await h.of("monitor.degraded")) == 1
        await h.add(started(), "b")
        await h.monitor.barrier()
        assert h.monitor.flags == [flag]


async def test_a_decision_taken_while_pausing_joins_the_pause(tmp_path: Path) -> None:
    config = sweep_config(every=1) | {"policy": {"pause_timeout_s": 30}}
    async with harness(tmp_path, config=config) as h:
        h.actuator.pause_gate = asyncio.Event()
        await swept_pause(h)
        h.sweeps.result = lambda sweeper, trigger: SweepResult()
        await h.bad(importance=4)
        await h.monitor.barrier()
        h.actuator.pause_gate.set()
        await h.until(lambda: h.monitor._running is None and not h.monitor._pending)
        assert len(await h.of("monitor.action")) == 1
        [pause] = h.monitor._pauses
        assert [e.data["layer"] for e in pause.flags] == ["sweep", "signal"]
        assert pause.task is not None


async def test_offline_rerun_reads_the_origin(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        origin = await store.create_run(RunStarted(scenario="t", oversight="autonomous"))
        drafts = [
            Draft(started(), "a"),
            Draft(
                ToolCall(
                    call_id="t1",
                    name="bad",
                    arguments=json.dumps({"category": "drift", "importance": 5}),
                ),
                "a",
            ),
            Draft(AgentStep(n=1, call_id="s1", status="completed"), "a"),
        ]
        await store.append(origin, drafts)
        await own_call_in(store, origin, "m1", "sweep")
        run_id = await store.create_run(
            RunStarted(scenario="t", oversight="autonomous", kind="monitor", origin=origin)
        )
        cfg = MonitorConfig.model_validate(sweep_config(every=1))
        p = profile()
        resolved = ResolvedMonitor(
            config=cfg,
            texts=TEXTS,
            oversight="autonomous",
            budget_usd=5.0,
            sweeps={"main": p},
            review=p,
        )
        engine = FakeEngine(signal_rule)
        built: dict[str, Any] = {}
        sweeps = Sweeps()

        def engine_factory(*args: Any, **kw: Any) -> FakeEngine:
            built["engine"] = kw["run_id"]
            return engine

        def sweeper_factory(**kw: Any) -> FakeSweeper:
            built["sweeper"] = kw["run_id"]
            return sweeps.factory(**kw)

        def trajectory_factory(*args: Any) -> FakeTrajectory:
            built["trajectory"] = args[0]
            return FakeTrajectory(*args)

        actuator = FakeActuator()
        monitor = Monitor(
            run_id=run_id,
            scenario=scenario("autonomous", ["a"]),
            monitor=resolved,
            store=store,
            client=ModelClient(store, run_id, models(p), transport=Script().transport(), env={}),
            actuator=actuator,
            simulated=True,
            source=origin,
            engine_factory=cast(Any, engine_factory),
            trajectory_factory=cast(Any, trajectory_factory),
            sweeper_factory=cast(Any, sweeper_factory),
        )
        await monitor.start()
        assert len(monitor._tasks) == 1
        monitor.feed(await store.events(origin, limit=1000))
        await monitor.agents_done()
        assert built == {"engine": origin, "sweeper": origin, "trajectory": origin}
        assert engine.seen[0].type == "run.started"
        assert not any(e.data.get("role") for e in engine.seen)
        [flag] = await store.events(run_id, types=["monitor.flag"])
        [action] = await store.events(run_id, types=["monitor.action"])
        assert action.data["simulated"] is True
        assert action.data["action"] == "kill"
        assert actuator.log == []
        assert await store.events(origin, types=["monitor.flag"]) == []
        [call_event] = await monitor._flagged_calls("a", [flag])
        assert call_event.run_id == origin
        assert call_event.data["name"] == "bad"
        assert [t for _, _, t, _ in sweeps.calls] == ["flag", "every"]
        await monitor.close()


async def own_call_in(store: LocalEventStore, run_id: str, call_id: str, role: Any) -> None:
    body = await store.put_blob(run_id, b"{}")
    request = ModelRequest(
        call_id=call_id,
        attempt=1,
        profile="p",
        backend="chat",
        model="m",
        url="http://x",
        params=ModelParams(stream=False),
        body=body,
        key=body.sha256,
        role=role,
    )
    await store.append(run_id, [Draft(request)])
