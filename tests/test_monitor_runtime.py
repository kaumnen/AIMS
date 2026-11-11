"""The monitor in the runner: its gate, the feed, its place in a run's phases, the actuator,
deny, verifier restore and taint, jobs and the CLI's checks. The sandbox and the monitor are
fakes with the real surfaces, so no Docker and no model is needed."""

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import tarfile
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from scripted import Script, call, profile, raw_call
from typer.testing import CliRunner

from aims.agent import Agent, GatePoint, OpenGate, PauseClock, Verdict
from aims.cli import app
from aims.events import (
    AgentFinished,
    AgentHeld,
    Event,
    ExecFinished,
    LocalEventStore,
    MonitorAction,
    OperatorAction,
    RunFinished,
    RunStarted,
    SandboxArchived,
    SandboxStopped,
    TaskScored,
    ToolResult,
)
from aims.models import ModelsConfig, Profile, Roles
from aims.monitor.config import merge
from aims.monitor.core import HumanRequest, Monitor
from aims.monitor.gate import MonitorGate
from aims.monitor.setup import ResolvedMonitor, identity
from aims.monitor.texts import MonitorTexts
from aims.prompts import AgentTexts, Prompts
from aims.run import runner as runner_module
from aims.run.job import Job, bundle_of, monitor_for, runner_for, scenario_jobs
from aims.run.runner import Bench, Options, Result, RunError, Runner, _status, seeded
from aims.sandbox import DockerBackend
from aims.sandbox.base import Capture, Outcome, SandboxInfo, SandboxSpec
from aims.scenario import Bundle, load
from aims.scenario.model import ScriptStep

pytestmark = pytest.mark.anyio

SCENARIO = """\
version: 1
name: watched
oversight: {oversight}
task:
  instruction: Do what you are told.
  environment: {{image: python:3.13-slim}}
  files:
    - {{path: tests/test_a.py, content: "assert True"}}
    - {{path: notes.txt, content: "keep"}}
  verifier:
    command: echo 1 > "$AIMS_LOGS_DIR/reward.txt"
{verifier}
agents:
  - id: a
"""


def scenario(tmp_path: Path, oversight: str = "observe", verifier: str = "") -> Bundle:
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO.format(oversight=oversight, verifier=verifier))
    return load(path)


# Fakes


class Box:
    """A sandbox that runs nothing: every exec exits 0 and the reward read finds 1."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.stdins: list[bytes | None] = []
        self.pauses = 0
        self.resumes = 0

    @property
    def info(self) -> SandboxInfo:
        return SandboxInfo("sb", "fake", "img", "sha256:" + "0" * 64, {})

    @property
    def audit(self) -> Any:
        return "snapshot"

    def wrapper(self, timeout_ms: int) -> list[str]:
        return ["sh", "-c"]

    async def run(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: str,
        timeout_ms: int,
        capture: Capture,
        on_pid: Callable[[int], None] | None = None,
        stdin: bytes | None = None,
        gid: int | None = None,
        env: Any = None,
    ) -> Outcome:
        self.commands.append(argv[-1])
        self.stdins.append(stdin)
        if "echo J" in argv[-1]:
            capture.add(1, b"T\n1\n")
        return Outcome("exited", 0, 100, 1)

    async def processes(self) -> list[Any]:
        return []

    def records(self) -> None:
        return None

    def on_pre_exec(self, callback: Any) -> None:
        pass

    def samples(self) -> None:
        return None

    async def hash(self, path: str, **kw: Any) -> str | None:
        return None

    async def tree(self) -> None:
        return None

    async def pause(self) -> None:
        self.pauses += 1

    async def resume(self) -> None:
        self.resumes += 1

    async def put_archive(self, path: str, data: bytes) -> None:
        pass

    async def get_archive(self, path: str) -> AsyncGenerator[bytes]:
        out = io.BytesIO()
        tarfile.open(fileobj=out, mode="w").close()
        yield out.getvalue()

    async def kill(self) -> None:
        pass

    async def quiesce(self) -> None:
        pass

    async def close(self, *, keep_volumes: bool = False) -> None:
        pass


class Boxes:
    name = "fake"

    def __init__(self) -> None:
        self.made: dict[str, Box] = {}

    async def build(self, context: bytes, dockerfile: str = "Dockerfile") -> str:
        return "img"

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        self.made[agent_id] = box = Box()
        return box

    async def close(self) -> None:
        pass


class DeadBox(Box):
    """A box Docker cannot freeze or unfreeze, as when its container stopped."""

    async def pause(self) -> None:
        raise RuntimeError("409 container is not running")

    async def resume(self) -> None:
        raise RuntimeError("409 container is not running")


class StuckBox(Box):
    """A box whose freeze waits until it is cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.freezing = asyncio.Event()

    async def pause(self) -> None:
        self.freezing.set()
        await asyncio.Event().wait()


class FirstBoxes(Boxes):
    """Agent a gets a box of `kind`, every other agent a plain one."""

    def __init__(self, kind: type[Box]) -> None:
        super().__init__()
        self.kind = kind

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        self.made[agent_id] = box = self.kind() if agent_id == "a" else Box()
        return box


class HeldBox(Box):
    """A box whose close waits for `release`, and whose `bash hold` exec waits until it is
    cancelled, so a test can cancel the run meanwhile."""

    def __init__(self) -> None:
        super().__init__()
        self.holding = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def run(self, argv: Sequence[str], **kw: Any) -> Outcome:
        if "hold" in argv[-1]:
            self.holding.set()
            await asyncio.Event().wait()
        return await super().run(argv, **kw)

    async def close(self, *, keep_volumes: bool = False) -> None:
        self.closing.set()
        await self.release.wait()
        self.closed = True


class HeldBoxes(Boxes):
    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        self.made[agent_id] = box = HeldBox()
        return box


class EndingBox(Box):
    """A box whose `bash hold` exec runs until cancelled, then ends only on `release`, however
    often it is cancelled meanwhile, so a test can cancel the run while the agents end."""

    def __init__(self) -> None:
        super().__init__()
        self.holding = asyncio.Event()
        self.ending = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def run(self, argv: Sequence[str], **kw: Any) -> Outcome:
        if "hold" not in argv[-1]:
            return await super().run(argv, **kw)
        self.holding.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.ending.set()
        while not self.release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.release.wait()
        raise asyncio.CancelledError

    async def close(self, *, keep_volumes: bool = False) -> None:
        self.closed = True


class EndingBoxes(Boxes):
    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        self.made[agent_id] = box = EndingBox()
        return box


class FakeMonitor:
    """The `Monitor` surface, recording what the runner asks of it."""

    made: ClassVar[list[FakeMonitor]] = []

    def __init__(
        self,
        *,
        run_id: str,
        scenario: Any,
        monitor: ResolvedMonitor,
        store: Any,
        client: Any,
        actuator: Any,
        simulated: bool = False,
        source: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.watch = monitor
        self.actuator = actuator
        self.simulated = simulated
        self.store = store
        self.client = client
        self.events: list[Event] = []
        self.calls: list[str] = []
        self.barriers = 0
        self.holds: list[GatePoint] = []
        self.on_barrier: Callable[[], Awaitable[None]] | None = None
        self.on_hold: Callable[[GatePoint], Awaitable[None]] | None = None
        self.taint: dict[str, list[int]] = {}
        FakeMonitor.made.append(self)

    def feed(self, events: Sequence[Event]) -> None:
        self.events.extend(events)

    async def start(self) -> None:
        self.calls.append("start")

    async def barrier(self) -> None:
        self.barriers += 1
        if self.on_barrier is not None:
            await self.on_barrier()

    async def hold(self, point: GatePoint) -> None:
        self.holds.append(point)
        if self.on_hold is not None:
            hook, self.on_hold = self.on_hold, None
            await hook(point)

    def request_sweep(self, agent_id: str | None, trigger: str) -> None:
        self.calls.append(f"sweep {trigger}")

    async def agents_done(self) -> None:
        self.calls.append("agents_done")

    def tainted(self, agent_id: str) -> list[int]:
        self.calls.append(f"tainted {agent_id}")
        return list(self.taint.get(agent_id, []))

    async def judge(self, *, skip: bool = False) -> None:
        self.calls.append(f"judge skip={skip}")

    def cut(self) -> None:
        self.calls.append("cut")

    async def close(self) -> None:
        self.calls.append("close")

    @property
    def cost_usd(self) -> float:
        return 0.0


@pytest.fixture(autouse=True)
def fake_monitor(monkeypatch: pytest.MonkeyPatch) -> list[FakeMonitor]:
    FakeMonitor.made = []
    monkeypatch.setattr(runner_module, "Monitor", FakeMonitor)
    return FakeMonitor.made


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


WATCHER = profile(name="watcher", model="mon", weights="mon-w")


def models() -> ModelsConfig:
    agent = profile()
    return ModelsConfig(
        profiles={agent.name: agent, WATCHER.name: WATCHER},
        roles=Roles(agent=agent.name, sweep=WATCHER.name, judge=WATCHER.name),
    )


def watched(oversight: Any = "observe", *, hold: bool = False) -> ResolvedMonitor:
    config = merge({"sweeps": [{"name": "main", "hold": hold}]})
    texts = MonitorTexts()
    sweeps = {"main": WATCHER}
    return ResolvedMonitor(
        config=config,
        texts=texts,
        oversight=oversight,
        budget_usd=5.0,
        sweeps=sweeps,
        review=WATCHER,
        judge=WATCHER,
        identity=identity(config, texts, {"sweep:main": WATCHER}, {}),
    )


def bench(store: LocalEventStore, script: Script, backend: Boxes | None = None) -> Bench:
    return Bench(store, backend or Boxes(), models(), transport=script.transport())


async def held(runner: Runner, agent: str) -> None:
    async with asyncio.timeout(10):
        while not runner.control.is_held(agent):  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def of[T](store: LocalEventStore, run_id: str, kind: type[T], name: str) -> list[T]:
    events = await store.events(run_id, limit=1000, types=[name])
    return [p for p in (e.payload() for e in events) if isinstance(p, kind)]


# The monitor's gate


class Counting:
    def __init__(self) -> None:
        self.checks = 0

    async def check(self, point: GatePoint) -> Verdict:
        self.checks += 1
        return Verdict()

    async def held(self, point: GatePoint) -> Verdict:
        return Verdict("inject", text="from inner")


async def test_the_monitor_gate_waits_at_the_barrier_and_holds_before_tools() -> None:
    monitor = FakeMonitor(
        run_id="r", scenario=None, monitor=watched(), store=None, client=None, actuator=None
    )
    inner = Counting()
    gate = MonitorGate(cast(Monitor, monitor), inner, hold=True)
    kinds = ("before_model", "after_reply", "before_tool", "after_tool")
    for kind in kinds:
        assert (await gate.check(GatePoint(kind, "r", "a", 1, "c1"))).action == "continue"
    assert monitor.barriers == 4
    assert [p.kind for p in monitor.holds] == ["before_tool"]
    assert inner.checks == 4
    assert (await gate.held(GatePoint("before_tool", "r", "a", 1, "c1"))).text == "from inner"

    loose = MonitorGate(cast(Monitor, monitor), OpenGate())
    await loose.check(GatePoint("before_tool", "r", "a", 2, "c2"))
    assert len(monitor.holds) == 1


async def test_a_sweep_hold_does_not_count_against_the_agent_wall() -> None:
    monitor = FakeMonitor(
        run_id="r", scenario=None, monitor=watched(), store=None, client=None, actuator=None
    )
    clock = PauseClock()
    seen: list[bool] = []

    async def hold(point: GatePoint) -> None:
        seen.append(clock.paused)
        await asyncio.sleep(0.05)

    monitor.on_hold = hold
    gate = MonitorGate(cast(Monitor, monitor), OpenGate(), hold=True, clock=lambda agent: clock)
    await gate.check(GatePoint("before_tool", "r", "a", 1, "c1"))
    assert seen == [True]
    assert not clock.paused
    assert clock.paused_s() >= 0.05


# A run with a monitor


async def test_a_watched_run_feeds_its_monitor_and_ends_in_order(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="echo hi"), call("submit", answer="ok"))
    watch = watched()
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watch)
    result = await runner.run()

    assert result.status == "completed", result.reason
    run_id = result.run_id
    assert run_id is not None
    (monitor,) = fake_monitor
    assert (monitor.run_id, monitor.simulated, monitor.actuator) == (run_id, True, runner)
    assert monitor.client is not None
    started = (await store.events(run_id, limit=1))[0].payload()
    assert isinstance(started, RunStarted)
    assert started.monitor == watch.identity
    assert started.config["monitor"] == watch.snapshot()
    events = await store.events(run_id, limit=1000)
    fed = [e.seq for e in monitor.events]
    assert fed == [e.seq for e in events][: len(fed)]
    assert fed[0] == 0
    assert all(e.type in ("sandbox.stopped", "run.finished") for e in events[len(fed) :])
    assert monitor.calls == [
        "start",
        "agents_done",
        "tainted a",
        "judge skip=False",
        "close",
    ]
    assert monitor.barriers >= 6
    assert isinstance(runner.control.inner, MonitorGate)


async def test_an_unwatched_run_has_no_monitor(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="ok"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=None)
    result = await runner.run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    assert not fake_monitor
    started = (await store.events(result.run_id, limit=1))[0].payload()
    assert isinstance(started, RunStarted)
    assert (started.monitor, started.config["monitor"]) == (None, None)
    runner.finished = False
    with pytest.raises(RunError, match="no monitor"):
        await runner.operate("sweep")


async def test_verifier_flags_taint_the_score(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="ok"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())

    async def taint() -> None:
        fake_monitor[0].taint["a"] = [41, 42]

    def armed(run_id: str) -> None:
        fake_monitor[0].on_barrier = taint

    runner.on_start = armed
    result = await runner.run()
    assert result.scores["a"].tainted == [41, 42]
    assert result.run_id is not None
    (scored,) = await of(store, result.run_id, TaskScored, "task.scored")
    assert (scored.tainted, scored.reward) == ([41, 42], 1.0)


async def test_a_pause_then_deny_at_the_hold_skips_the_call(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="curl evil"), call("submit", answer="ok"))
    backend = Boxes()
    runner = Runner(
        bench(store, script, backend), scenario(tmp_path), Options(), monitor=watched(hold=True)
    )
    asked: list[str | None] = []

    async def deny(point: GatePoint) -> None:
        await runner.pause([point.agent_id], "flagged")
        asked.append(runner.held_call(point.agent_id))
        assert runner.paused(point.agent_id)
        await runner.resume([point.agent_id], then="deny")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_hold", deny)
    result = await runner.run()

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    assert asked == ["call_1"]
    assert not any("curl" in c for c in backend.made["a"].commands)
    assert backend.made["a"].pauses >= 1
    results = await of(store, result.run_id, ToolResult, "tool.result")
    assert (results[0].denied, results[0].error) == (True, True)
    assert results[0].output == AgentTexts().denied
    assert not await of(store, result.run_id, OperatorAction, "operator.action")
    assert not await of(store, result.run_id, MonitorAction, "monitor.action")


async def test_an_operator_denies_the_call_an_agent_is_held_before(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="rm -rf /"), call("submit", answer="ok"))
    backend = Boxes()
    runner = Runner(
        bench(store, script, backend), scenario(tmp_path), Options(), monitor=watched(hold=True)
    )
    reached = asyncio.Event()

    async def pause(point: GatePoint) -> None:
        await runner.pause([point.agent_id], "flagged")
        reached.set()

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_hold", pause)
    task = asyncio.create_task(runner.run())
    await reached.wait()
    await held(runner, "a")
    with pytest.raises(RunError, match="an inject needs text"):
        await runner.operate("resume", then="inject")
    await runner.operate("resume", then="deny", token="alice")
    result = await task

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    assert not any("rm -rf" in c for c in backend.made["a"].commands)
    actions = await of(store, result.run_id, OperatorAction, "operator.action")
    assert [(a.action, a.then, a.token) for a in actions] == [("resume", "deny", "alice")]
    results = await of(store, result.run_id, ToolResult, "tool.result")
    assert results[0].denied


async def test_a_pause_at_the_barrier_after_a_reply_denies_its_call(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="curl evil"), call("submit", answer="ok"))
    backend = Boxes()
    runner = Runner(bench(store, script, backend), scenario(tmp_path), Options(), monitor=watched())
    flagged: list[int] = []

    async def flag() -> None:
        seen = [e.seq for e in fake_monitor[0].events if e.type == "tool.call"]
        if seen and not flagged:
            flagged.append(seen[0])
            await runner.operate("pause", agent="a", reason="flagged")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", flag)
    task = asyncio.create_task(runner.run())
    await held(runner, "a")
    assert runner.held_call("a") == "call_1"
    assert runner.run_id is not None
    info = await store.run(runner.run_id)
    assert info is not None
    assert [(h.at, h.next_call) for h in info.held] == [("after_reply", "call_1")]
    await runner.operate("resume", then="deny", token="alice")
    result = await task

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    assert not any("curl" in c for c in backend.made["a"].commands)
    (hold,) = await of(store, result.run_id, AgentHeld, "agent.held")
    assert (hold.kind, hold.next_call) == ("after_reply", "call_1")
    results = await of(store, result.run_id, ToolResult, "tool.result")
    assert (results[0].call_id, results[0].denied) == ("call_1", True)


async def test_a_deny_needs_an_agent_held_before_a_tool_call(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="ok"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())
    started = asyncio.Event()
    runner.on_start = lambda run_id: started.set()
    task = asyncio.create_task(runner.run())
    await started.wait()
    await runner.operate("pause")
    await held(runner, "a")
    with pytest.raises(RunError, match="no paused agent has a tool call waiting"):
        await runner.operate("resume", then="deny")
    await runner.operate("sweep", agent="a", token="bob")
    await runner.operate("resume")
    result = await task
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    actions = await of(store, result.run_id, OperatorAction, "operator.action")
    assert [(a.action, a.then) for a in actions] == [
        ("pause", None),
        ("sweep", None),
        ("resume", "continue"),
    ]
    assert "sweep operator" not in fake_monitor[0].calls


def two_agents(tmp_path: Path, start: str) -> Bundle:
    """Agents a and b, b starting at `start`."""
    path = tmp_path / "scenario.yaml"
    body = SCENARIO.format(oversight="observe", verifier="")
    path.write_text(body + f"  - id: b\n    start: {start}\n")
    return load(path)


@pytest.mark.parametrize("by", ["monitor", "operator"])
async def test_a_sandbox_that_does_not_freeze_leaves_the_others_paused_and_resumed(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor], by: str
) -> None:
    script = Script(call("submit", answer="a"), call("submit", answer="b"))
    backend = FirstBoxes(DeadBox)
    bundle = two_agents(tmp_path, "{agent: a, on: finished}")
    runner = Runner(bench(store, script, backend), bundle, Options(), monitor=watched())
    seen: list[tuple[bool, bool, int, int]] = []
    notes: list[str | None] = []

    def look() -> None:
        b = backend.made["b"]
        seen.append((runner.paused("a"), runner.paused("b"), b.pauses, b.resumes))

    async def pause_and_resume() -> None:
        fake_monitor[0].on_barrier = None
        if by == "monitor":
            await runner.pause(["a", "b"], "flagged")
            look()
            await runner.resume(["a", "b"])
        else:
            notes.append(await runner.operate("pause"))
            look()
            notes.append(await runner.operate("resume"))
        look()

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", pause_and_resume)
    async with asyncio.timeout(10):
        result = await runner.run()

    assert result.status == "completed", result.reason
    assert seen == [(True, True, 1, 0), (False, False, 1, 1)]
    if by == "operator":
        assert notes == [
            "the sandbox of agent a did not freeze: 409 container is not running",
            "the sandbox of agent a did not unfreeze: 409 container is not running",
        ]


async def test_a_cancel_while_freezing_propagates_and_leaves_the_agents_paused(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="a"), call("submit", answer="b"))
    backend = FirstBoxes(StuckBox)
    bundle = two_agents(tmp_path, "{agent: a, on: finished}")
    runner = Runner(bench(store, script, backend), bundle, Options(), monitor=watched())
    seen: list[tuple[bool, bool, int]] = []

    async def cancel_the_pause() -> None:
        fake_monitor[0].on_barrier = None
        pausing = asyncio.create_task(runner.pause(["a", "b"], "flagged"))
        box = backend.made["a"]
        assert isinstance(box, StuckBox)
        await box.freezing.wait()
        pausing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pausing
        seen.append((runner.paused("a"), runner.paused("b"), backend.made["b"].pauses))
        await runner.resume(["a", "b"])

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", cancel_the_pause)
    async with asyncio.timeout(10):
        result = await runner.run()

    assert result.status == "completed", result.reason
    assert seen == [(True, True, 0)]


async def test_an_agent_paused_before_it_starts_holds_before_its_first_model_call(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(
        call("bash", command="echo a"), call("submit", answer="a"), call("submit", answer="b")
    )
    backend = Boxes()
    bundle = two_agents(tmp_path, "{agent: a, on: step, step: 1}")
    runner = Runner(bench(store, script, backend), bundle, Options(), monitor=watched())

    async def pause() -> None:
        fake_monitor[0].on_barrier = None
        await runner.pause(["b"], "flagged")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", pause)
    task = asyncio.create_task(runner.run())
    await held(runner, "b")
    run_id = runner.run_id
    assert run_id is not None
    assert await store.events(run_id, types=["model.request"], agent_id="b") == []
    assert backend.made["b"].pauses >= 1
    await runner.resume(["b"])
    result = await task

    assert result.status == "completed", result.reason
    assert {a: f.reason for a, f in result.agents.items()} == {"a": "submitted", "b": "submitted"}
    (hold,) = await of(store, run_id, AgentHeld, "agent.held")
    assert (hold.kind, hold.step) == ("before_model", 0)


class FreezeBox(Box):
    """A box that refuses an exec while it is frozen, as Docker refuses one in a paused
    container."""

    def __init__(self) -> None:
        super().__init__()
        self.frozen = False
        self.refused: list[str] = []

    async def pause(self) -> None:
        await super().pause()
        self.frozen = True

    async def resume(self) -> None:
        await super().resume()
        self.frozen = False

    async def run(self, argv: Sequence[str], **kw: Any) -> Outcome:
        if self.frozen:
            self.refused.append(argv[-1])
            return Outcome("failed", None, None, 1, error="container is paused")
        return await super().run(argv, **kw)


class FreezeBoxes(Boxes):
    def __init__(self) -> None:
        super().__init__()
        self.boxes: dict[str, FreezeBox] = {}

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        self.made[agent_id] = self.boxes[agent_id] = box = FreezeBox()
        return box


def steps(**scripts: list[dict[str, Any]]) -> dict[str, list[ScriptStep]]:
    return {a: [ScriptStep.model_validate(s) for s in steps] for a, steps in scripts.items()}


async def until(test: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not test():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def done(runner: Runner, agent: str) -> bool:
    scripted = runner.scripted.get(agent)
    return scripted is not None and scripted.done


@pytest.mark.parametrize("then", ["resume", "kill", "cancel"])
async def test_a_scripted_agent_that_starts_during_a_pause_holds_until_it_is_let_go(
    store: LocalEventStore, tmp_path: Path, then: Any
) -> None:
    backend = FreezeBoxes()
    bundle = two_agents(tmp_path, "{delay_s: 1}")
    scripts = steps(a=[{"run": "echo a"}], b=[{"run": "echo b"}])
    runner = Runner(bench(store, Script(), backend), bundle, Options(scripts=scripts))
    task = asyncio.create_task(runner.run())
    await until(lambda: done(runner, "a"))
    await runner.operate("pause")
    await until(lambda: runner.control.is_held("b") or done(runner, "b"))
    box = backend.boxes["b"]
    assert box.refused == []
    assert runner.control.is_held("b")
    await runner.operate(then)
    async with asyncio.timeout(5):
        result = await task

    run_id = result.run_id
    assert run_id is not None
    finished = result.agents["b"]
    if then == "resume":
        assert result.status == "completed", result.reason
        assert (finished.reason, box.commands.count("echo b")) == ("no_submit", 1)
        assert finished.paused_ms > 0
        (hold,) = await of(store, run_id, AgentHeld, "agent.held")
        assert (hold.kind, hold.call_id, hold.step) == ("before_tool", None, 0)
    else:
        assert finished.reason == ("killed" if then == "kill" else "cancelled")
        assert "echo b" not in box.commands
    assert box.refused == []


async def test_a_scripted_step_waits_out_its_delay_in_time_its_agent_is_not_paused(
    store: LocalEventStore, tmp_path: Path
) -> None:
    backend = FreezeBoxes()
    scripts = steps(a=[{"after_s": 0.5, "run": "echo a"}])
    runner = Runner(bench(store, Script(), backend), scenario(tmp_path), Options(scripts=scripts))
    task = asyncio.create_task(runner.run())
    await until(lambda: "a" in runner.scripted)
    await runner.operate("pause", agent="a")
    await asyncio.sleep(1)
    box = backend.boxes["a"]
    assert (box.refused, box.commands.count("echo a")) == ([], 0)
    await runner.operate("resume", agent="a")
    resumed = time.monotonic()
    await until(lambda: "echo a" in box.commands)
    waited = time.monotonic() - resumed
    async with asyncio.timeout(5):
        result = await task

    assert result.status == "completed", result.reason
    assert waited >= 0.3
    assert result.agents["a"].paused_ms >= 900
    assert box.refused == []


class NoFreezeBox(Box):
    """A box Docker cannot freeze, though it runs on."""

    async def pause(self) -> None:
        self.pauses += 1
        raise RuntimeError("500 cannot freeze")


async def test_an_agent_whose_sandbox_does_not_freeze_at_its_gate_still_holds(
    store: LocalEventStore,
    tmp_path: Path,
    fake_monitor: list[FakeMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = Script(call("submit", answer="ok"))
    backend = FirstBoxes(NoFreezeBox)
    runner = Runner(bench(store, script, backend), scenario(tmp_path), Options(), monitor=watched())
    notes: list[str | None] = []

    async def pause() -> None:
        fake_monitor[0].on_barrier = None
        notes.append(await runner.operate("pause", agent="a"))

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", pause)
    with caplog.at_level(logging.WARNING, logger="aims.agent"):
        task = asyncio.create_task(runner.run())
        await until(
            lambda: (
                runner.control.is_held("a") or ("a" in runner.agents and runner.agents["a"].done)
            )
        )
        assert runner.control.is_held("a")
        assert not script.requests
        await runner.operate("resume", agent="a")
        async with asyncio.timeout(5):
            result = await task

    assert result.status == "completed", result.reason
    assert result.agents["a"].reason == "submitted"
    assert backend.made["a"].pauses == 2
    assert notes == ["the sandbox of agent a did not freeze: 500 cannot freeze"]
    assert "agent a holds at its gate, but its sandbox did not freeze: 500 cannot freeze" in (
        caplog.text
    )


async def test_a_kill_by_the_monitor_still_gets_the_final_sweeps_and_the_judge(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="x"), call("submit", answer="never"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())

    async def kill() -> None:
        fake_monitor[0].on_barrier = None
        await runner.kill("killed by the monitor")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", kill)
    result = await runner.run()
    assert (result.status, result.reason) == ("killed", "killed by the monitor")
    assert result.agents["a"].reason == "killed"
    assert fake_monitor[0].calls == ["start", "agents_done", "judge skip=False", "close"]


@pytest.mark.parametrize("then", [None, "kill"])
async def test_an_operator_kill_cuts_the_monitor_and_skips_the_judge(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor], then: Any
) -> None:
    script = Script(call("bash", command="x"), call("submit", answer="never"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())

    async def kill() -> None:
        fake_monitor[0].on_barrier = None
        if then is None:
            await runner.operate("kill", reason="stop the spend")
        else:
            await runner.operate("resume", then=then, reason="stop the spend")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", kill)
    result = await runner.run()
    assert (result.status, result.reason) == ("killed", "stop the spend")
    assert fake_monitor[0].calls == ["start", "cut", "judge skip=True", "close"]


async def test_a_run_cancelled_before_it_starts_cuts_its_monitor_at_once(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="ok"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())
    runner.cancel()
    result = await runner.run()
    assert result.status == "cancelled"
    assert fake_monitor[0].calls[:2] == ["cut", "start"]


class SlowJudge(FakeMonitor):
    """A judge that runs until the monitor is cut."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.judging = asyncio.Event()
        self.stopped = asyncio.Event()

    async def judge(self, *, skip: bool = False) -> None:
        await super().judge(skip=skip)
        if not skip:
            self.judging.set()
            await self.stopped.wait()

    def cut(self) -> None:
        super().cut()
        self.stopped.set()


@pytest.mark.parametrize("stop", ["cancel", "kill"])
@pytest.mark.parametrize("end", ["completed", "killed"])
async def test_a_stop_while_the_monitor_finishes_cuts_it_and_keeps_the_outcome(
    store: LocalEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_monitor: list[FakeMonitor],
    stop: Any,
    end: str,
) -> None:
    monkeypatch.setattr(runner_module, "Monitor", SlowJudge)
    answer = "ok" if end == "completed" else "never"
    script = Script(call("bash", command="x"), call("submit", answer=answer))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())

    async def kill() -> None:
        fake_monitor[0].on_barrier = None
        await runner.kill("killed by the monitor")

    if end == "killed":
        runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", kill)
    task = asyncio.ensure_future(runner.run())
    async with asyncio.timeout(10):
        while not fake_monitor:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        monitor = cast(SlowJudge, fake_monitor[0])
        await monitor.judging.wait()
        await runner.operate(stop, reason="stop the judge")
        result = await task
    assert result.status == end, result.reason
    assert not runner.cancelled
    assert runner.killed == (end == "killed")
    assert monitor.calls[-3:] == ["judge skip=False", "cut", "close"]
    assert result.run_id is not None
    actions = await of(store, result.run_id, OperatorAction, "operator.action")
    assert [a.action for a in actions] == [stop]


async def test_a_kill_ends_a_scripted_confederate_as_killed(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    path = tmp_path / "scenario.yaml"
    confederate = (
        "  - id: c\n    role: confederate\n    script:\n      - {after_s: 30, run: echo late}\n"
    )
    path.write_text(SCENARIO.format(oversight="observe", verifier="") + confederate)
    script = Script(call("bash", command="x"), call("submit", answer="never"))
    runner = Runner(bench(store, script), load(path), Options(), monitor=watched())
    barriers = 0

    async def kill() -> None:
        nonlocal barriers
        barriers += 1
        if barriers == 3:
            fake_monitor[0].on_barrier = None
            await runner.kill("stopped by the test")

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", kill)
    result = await runner.run()
    assert result.status == "killed"
    assert (result.agents["c"].reason, result.agents["c"].detail) == (
        "killed",
        "stopped by the test",
    )


async def test_a_cancelled_run_skips_the_final_sweeps_and_the_judge(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("bash", command="x"), call("submit", answer="never"))
    runner = Runner(bench(store, script), scenario(tmp_path), Options(), monitor=watched())

    async def cancel() -> None:
        fake_monitor[0].on_barrier = None
        runner.cancel()

    runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_barrier", cancel)
    result = await runner.run()
    assert result.status == "cancelled"
    assert fake_monitor[0].calls == ["start", "cut", "judge skip=True", "close"]


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        ("while the agents run", "cancelled before the agents finished"),
        ("while the run ends", "cancelled while the run ended"),
    ],
)
async def test_a_second_cancel_while_the_sandboxes_close_waits_for_them_and_seals_the_run(
    store: LocalEventStore, tmp_path: Path, first: str, reason: str
) -> None:
    """Ctrl-C again lands while a sandbox closes: the run must wait for the close, so the
    backend is not shut under it, and then be sealed as cancelled."""
    script = Script(call("bash", command="hold"), call("submit", answer="ok"))
    if first == "while the run ends":
        script = Script(call("submit", answer="ok"))
    backend = HeldBoxes()
    runner = Runner(bench(store, script, backend), scenario(tmp_path), Options(), monitor=None)
    running = asyncio.create_task(runner.run())
    async with asyncio.timeout(10):
        while "a" not in backend.made:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        box = cast(HeldBox, backend.made["a"])
        if first == "while the agents run":
            await box.holding.wait()
            running.cancel()
        await box.closing.wait()
    running.cancel()
    await asyncio.sleep(0.05)
    assert not running.done()
    box.release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert box.closed
    assert runner.run_id is not None
    finished = await of(store, runner.run_id, RunFinished, "run.finished")
    assert [(f.status, f.reason) for f in finished] == [("cancelled", reason)]


@pytest.mark.parametrize(
    ("stop", "end", "kill"),
    [
        ("cancel", "cancelled", "the run was cancelled"),
        ("kill", "killed", "stopped by the test"),
    ],
)
async def test_a_second_cancel_while_the_agents_end_after_a_stop_propagates_and_seals_the_run(
    store: LocalEventStore, tmp_path: Path, stop: str, end: str, kill: str
) -> None:
    """An operator stop, then Ctrl-C again while the agents still end: the run task must
    raise the cancel, not return as if only the stop had landed, and the run is sealed."""
    before = asyncio.all_tasks()
    script = Script(call("bash", command="hold"), call("submit", answer="ok"))
    backend = EndingBoxes()
    runner = Runner(bench(store, script, backend), scenario(tmp_path), Options(), monitor=None)
    running = asyncio.create_task(runner.run())
    async with asyncio.timeout(10):
        while "a" not in backend.made:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        box = cast(EndingBox, backend.made["a"])
        await box.holding.wait()
        if stop == "cancel":
            runner.cancel()
        else:
            await runner.kill("stopped by the test")
        await box.ending.wait()
    running.cancel()
    await asyncio.sleep(0.05)
    assert not running.done()
    box.release.set()
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError):
            await running
    assert box.closed
    assert runner.sandboxes == {}
    assert not asyncio.all_tasks() - before
    assert runner.run_id is not None
    agents = await of(store, runner.run_id, AgentFinished, "agent.finished")
    assert [a.reason for a in agents] == [end]
    stopped = await of(store, runner.run_id, SandboxStopped, "sandbox.stopped")
    assert [(s.reason, s.detail) for s in stopped] == [("killed", kill)]
    finished = await of(store, runner.run_id, RunFinished, "run.finished")
    assert [(f.status, f.reason) for f in finished] == [
        ("cancelled", "cancelled before the agents finished")
    ]


class SetupBox(Box):
    """A box whose `hold` setup command runs until it is cancelled."""

    def __init__(self, holding: asyncio.Event) -> None:
        super().__init__()
        self.holding = holding
        self.killed = False
        self.closed = False

    async def run(self, argv: Sequence[str], **kw: Any) -> Outcome:
        if "hold" in argv[-1]:
            self.holding.set()
            await asyncio.Event().wait()
        return await super().run(argv, **kw)

    async def kill(self) -> None:
        self.killed = True

    async def close(self, *, keep_volumes: bool = False) -> None:
        self.closed = True


class StartBoxes(Boxes):
    """Boxes whose start phase `hold` (the image build, the sandbox start or a setup
    command) runs until it is cancelled."""

    def __init__(self, hold: str) -> None:
        super().__init__()
        self.hold = hold
        self.holding = asyncio.Event()

    async def build(self, context: bytes, dockerfile: str = "Dockerfile") -> str:
        if self.hold == "build":
            self.holding.set()
            await asyncio.Event().wait()
        return "img"

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        if self.hold == "start":
            self.holding.set()
            await asyncio.Event().wait()
        self.made[agent_id] = box = SetupBox(self.holding)
        return box


def with_setup(tmp_path: Path, *, build: bool = False) -> Bundle:
    """The scenario with a `hold` setup command, built from a Dockerfile with `build`."""
    text = SCENARIO.format(oversight="observe", verifier="").replace(
        "  instruction: Do what you are told.\n",
        "  instruction: Do what you are told.\n  setup: [hold]\n",
    )
    if build:
        text = text.replace("{image: python:3.13-slim}", "{dockerfile: Dockerfile}")
        (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim\n")
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    return load(path)


@pytest.mark.parametrize("phase", ["build", "start", "setup"])
async def test_an_operator_cancel_ends_a_start_phase_at_once_and_seals_the_run(
    store: LocalEventStore, tmp_path: Path, phase: str
) -> None:
    """The UI's Cancel or a first Ctrl-C while the image builds, a sandbox starts or a setup
    command runs: the run must not wait for it, and ends as a cancel between phases does."""
    before = asyncio.all_tasks()
    backend = StartBoxes(phase)
    bundle = with_setup(tmp_path, build=phase == "build")
    runner = Runner(bench(store, Script(call("submit", answer="x")), backend), bundle, Options())
    running = asyncio.create_task(runner.run())
    async with asyncio.timeout(5):
        await backend.holding.wait()
        await runner.operate("cancel")
        result = await running
    assert (result.status, result.reason) == ("cancelled", "cancelled by the operator")
    assert runner.sandboxes == {}
    assert not asyncio.all_tasks() - before
    run_id = result.run_id
    assert run_id is not None
    finished = await of(store, run_id, RunFinished, "run.finished")
    assert [(f.status, f.reason) for f in finished] == [("cancelled", "cancelled by the operator")]
    if phase != "setup":
        assert backend.made == {}
        return
    box = cast(SetupBox, backend.made["a"])
    assert box.killed
    assert box.closed
    events = await store.events(run_id, limit=1000, types=["exec.finished"])
    ends = [p.end for p in (e.payload() for e in events) if isinstance(p, ExecFinished)]
    assert ends[-1] == "cancelled"
    stopped = await of(store, run_id, SandboxStopped, "sandbox.stopped")
    assert [s.reason for s in stopped] == ["closed"]
    assert await of(store, run_id, SandboxArchived, "sandbox.archived")


@pytest.mark.parametrize("operator", [True, False])
async def test_a_cancel_of_the_run_task_while_a_setup_command_ends_propagates_and_seals_the_run(
    store: LocalEventStore, tmp_path: Path, operator: bool
) -> None:
    """Ctrl-C while a setup command runs, after the operator's cancel or alone: the run task
    must raise the cancel once the command ended, and the run is sealed once."""
    before = asyncio.all_tasks()
    backend = EndingBoxes()
    runner = Runner(
        bench(store, Script(call("submit", answer="x")), backend), with_setup(tmp_path), Options()
    )
    running = asyncio.create_task(runner.run())
    box: EndingBox | None = None
    try:
        async with asyncio.timeout(5):
            while "a" not in backend.made:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            box = cast(EndingBox, backend.made["a"])
            await box.holding.wait()
            if operator:
                await runner.operate("cancel")
                await box.ending.wait()
            running.cancel()
            await box.ending.wait()
        await asyncio.sleep(0.05)
        assert not running.done()
    finally:
        # The box holds its command until released, so a failed check must not leave the
        # run waiting on it.
        if box is not None:
            box.release.set()
        running.cancel()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await running
    assert box.closed
    assert runner.sandboxes == {}
    assert not asyncio.all_tasks() - before
    assert runner.run_id is not None
    finished = await of(store, runner.run_id, RunFinished, "run.finished")
    assert [(f.status, f.reason) for f in finished] == [
        ("cancelled", "cancelled before the agents finished")
    ]


class StopFails:
    """An audited sandbox whose stop raises, as when Docker refuses the kill."""

    def __init__(self) -> None:
        self.closed = False

    async def stop(self, reason: str = "killed", detail: str | None = None) -> None:
        raise RuntimeError("409 container is restarting")

    async def close(self, *, keep_volumes: bool = False) -> None:
        self.closed = True


async def test_a_sandbox_whose_stop_fails_on_a_cancel_is_still_closed(
    store: LocalEventStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = Runner(bench(store, Script()), scenario(tmp_path), Options())
    boxes = {"a": StopFails(), "b": StopFails()}
    runner.sandboxes = cast(Any, dict(boxes))
    with caplog.at_level(logging.WARNING, logger="aims.run"):
        await runner._close_sandboxes(kill=True)
    assert [b.closed for b in boxes.values()] == [True, True]
    assert runner.sandboxes == {}
    assert "killing the sandbox of agent a failed: 409 container is restarting" in caplog.text
    assert "closing the sandbox" not in caplog.text


async def test_ask_human_reaches_the_human_hook(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    asked: list[HumanRequest] = []

    async def human(request: HumanRequest) -> None:
        asked.append(request)

    script = Script(call("submit", answer="ok"))
    runner = Runner(
        bench(store, script), scenario(tmp_path, "human"), Options(), monitor=watched("human")
    )
    request = HumanRequest(run_id="r", agents=["a"], flags=[])
    await runner.ask_human(request)
    runner.human = human
    await runner.ask_human(request)
    assert asked == [request]
    assert runner.next_prompt("a") is None


async def test_restore_puts_matching_seeded_files_back_before_the_verifier(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    script = Script(call("submit", answer="ok"))
    backend = Boxes()
    bundle = scenario(tmp_path, verifier="    restore: [tests/**]")
    runner = Runner(bench(store, script, backend), bundle, Options(), monitor=None)
    result = await runner.run()
    assert result.status == "completed", result.reason
    box = backend.made["a"]
    tars = [i for i, c in enumerate(box.commands) if "tar -x" in c]
    assert len(tars) == 2
    restore = box.stdins[tars[1]]
    assert restore is not None
    with tarfile.open(fileobj=io.BytesIO(restore)) as tar:
        names = tar.getnames()
    assert "work/tests/test_a.py" in names
    assert "work/notes.txt" not in names
    assert tars[1] < next(i for i, c in enumerate(box.commands) if "mkdir -p -m 700" in c)
    assert result.run_id is not None
    started = await store.events(result.run_id, limit=1000, types=["exec.started"])
    assert [e.data["phase"] for e in started][-5:] == [
        "setup",
        "verifier",
        "verifier",
        "verifier",
        "verifier",
    ]


@pytest.mark.docker
async def test_monitor_pauses_and_denials_hold_a_real_sandbox(
    store: LocalEventStore, tmp_path: Path, fake_monitor: list[FakeMonitor]
) -> None:
    try:
        backend = await DockerBackend.open(audit="auto")
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        script = Script(
            call("bash", command="echo one > /work/one.txt"),
            call("bash", command="echo two > /work/two.txt"),
            call("submit", answer="ok"),
        )
        bench = Bench(store, backend, models(), transport=script.transport())
        bundle = scenario(tmp_path)
        runner = Runner(bench, bundle, Options(), monitor=watched(hold=True))
        reached = asyncio.Event()

        async def hold(point: GatePoint) -> None:
            fake_monitor[0].on_hold = hold
            if point.call_id == "call_1" and not reached.is_set():
                await runner.pause([point.agent_id], "flagged")
                reached.set()
            elif point.call_id == "call_2":
                await runner.pause([point.agent_id], "flagged again")
                await runner.resume([point.agent_id], then="deny")

        runner.on_start = lambda run_id: setattr(fake_monitor[0], "on_hold", hold)
        task = asyncio.create_task(runner.run())
        await reached.wait()
        await held(runner, "a")
        await runner.operate("resume")
        result = await task

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    assert [p.call_id for p in fake_monitor[0].holds] == ["call_1", "call_1", "call_2", "call_3"]
    results = await of(store, result.run_id, ToolResult, "tool.result")
    assert [r.denied for r in results] == [False, True, False]
    (archived,) = await of(store, result.run_id, SandboxArchived, "sandbox.archived")
    assert archived.archive is not None
    data = await store.get_blob(result.run_id, archived.archive.sha256)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    assert "work/one.txt" in names
    assert "work/two.txt" not in names


def test_seeded_picks_files_and_the_directories_above_them(tmp_path: Path) -> None:
    bundle = scenario(tmp_path)
    assert bundle.files is not None
    s = bundle.scenario

    def names(globs: list[str]) -> list[str] | None:
        data = seeded(bundle.files, s, globs)
        if data is None:
            return None
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return sorted(tar.getnames())

    assert names(["tests/**"]) == ["work", "work/tests", "work/tests/test_a.py"]
    assert names(["/work/tests"]) == ["work", "work/tests", "work/tests/test_a.py"]
    assert names(["*.txt"]) == ["work", "work/notes.txt"]
    assert names(["missing/*"]) is None
    assert names([]) is None


# How agents end


async def test_an_answer_with_a_lone_surrogate_completes_the_run(
    store: LocalEventStore, tmp_path: Path
) -> None:
    script = Script(raw_call("submit", '{"answer": "\\ud83d"}'))
    backend = Boxes()
    result = await Runner(bench(store, script, backend), scenario(tmp_path), Options()).run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    (finished,) = await of(store, result.run_id, AgentFinished, "agent.finished")
    assert (finished.reason, finished.answer) == ("submitted", "\\ud83d")
    assert b"\\ud83d" in backend.made["a"].stdins
    (scored,) = await of(store, result.run_id, TaskScored, "task.scored")
    assert scored.reward == 1.0


async def test_an_agent_whose_end_cannot_be_logged_fails_the_run(
    store: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finish = Agent._finish

    async def failing(self: Agent, start: float, reason: Any, *args: Any) -> AgentFinished:
        if reason == "submitted":
            raise OSError("disk full")
        return await finish(self, start, reason, *args)

    monkeypatch.setattr(Agent, "_finish", failing)
    script = Script(call("submit", answer="ok"))
    result = await Runner(bench(store, script), scenario(tmp_path), Options()).run()
    assert (result.status, result.reason) == ("failed", "agent a: OSError: disk full")
    assert result.run_id is not None
    (finished,) = await of(store, result.run_id, AgentFinished, "agent.finished")
    assert (finished.reason, finished.answer) == ("error", None)
    assert not await of(store, result.run_id, TaskScored, "task.scored")


async def test_an_agent_that_raised_without_an_end_fails_the_run(
    store: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Agent) -> AgentFinished:
        raise RuntimeError("broken")

    monkeypatch.setattr(Agent, "run", broken)
    result = await Runner(bench(store, Script()), scenario(tmp_path), Options()).run()
    assert (result.status, result.reason) == ("failed", "agent a: it did not finish")
    assert result.run_id is not None
    assert not await of(store, result.run_id, AgentFinished, "agent.finished")


def test_a_subject_without_an_end_fails_the_run_unless_one_was_killed() -> None:
    ended, killed = AgentFinished(reason="submitted"), AgentFinished(reason="killed")
    assert _status(Result("r", "failed", agents={"a": ended}), ["a"]) == ("completed", None)
    assert _status(Result("r", "failed", agents={"a": ended}), ["a", "b"]) == (
        "failed",
        "agent b: it did not finish",
    )
    assert _status(Result("r", "failed", agents={"a": killed}), ["a", "b"])[0] == "killed"


# Rewards


class RewardBoxes(Boxes):
    """Boxes whose verifier wrote `reward` as reward.json."""

    def __init__(self, reward: bytes) -> None:
        super().__init__()
        self.reward = reward

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> Box:
        reward = self.reward

        class RewardBox(Box):
            async def run(self, argv: Sequence[str], *, capture: Capture, **kw: Any) -> Outcome:
                if "echo J" not in argv[-1]:
                    return await super().run(argv, capture=capture, **kw)
                capture.add(1, b"J\n" + reward)
                return Outcome("exited", 0, 100, 1)

        self.made[agent_id] = box = RewardBox()
        return box


async def test_a_lone_surrogate_in_reward_json_is_scored_as_text(
    store: LocalEventStore, tmp_path: Path
) -> None:
    backend = RewardBoxes(b'{"reward": 1, "note": "\\ud83d"}\n')
    script = Script(call("submit", answer="ok"))
    result = await Runner(bench(store, script, backend), scenario(tmp_path), Options()).run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    (scored,) = await of(store, result.run_id, TaskScored, "task.scored")
    assert (scored.reward, scored.metrics, scored.error) == (
        1.0,
        {"reward": 1, "note": "\\ud83d"},
        None,
    )


async def test_a_reward_json_nested_too_deep_is_scored_as_unreadable(
    store: LocalEventStore, tmp_path: Path
) -> None:
    backend = RewardBoxes(b'{"reward": ' + b"[" * 300 + b"]" * 300 + b"}")
    script = Script(call("submit", answer="ok"))
    result = await Runner(bench(store, script, backend), scenario(tmp_path), Options()).run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    (scored,) = await of(store, result.run_id, TaskScored, "task.scored")
    assert (scored.reward, scored.metrics, scored.source) == (None, {}, "reward.json")
    assert (scored.error or "").startswith("reward.json is not JSON: ")


# Jobs and the CLI


def test_a_job_carries_its_monitor(tmp_path: Path) -> None:
    bundle = scenario(tmp_path)
    keyed = WATCHER.model_copy(update={"api_key_env": "AIMS_TEST_MONITOR_KEY"})
    watch = watched()
    watch = ResolvedMonitor(
        config=watch.config,
        texts=watch.texts,
        oversight=watch.oversight,
        budget_usd=watch.budget_usd,
        sweeps={"main": keyed},
        review=keyed,
        judge=keyed,
        identity=watch.identity,
    )
    (job,) = scenario_jobs(bundle, models(), Prompts(), monitor=watch, audit="snapshot")
    job = Job.model_validate_json(job.model_dump_json())
    assert job.monitor == watch.snapshot()
    assert set(job.models) == {"watcher"}
    assert "AIMS_TEST_MONITOR_KEY" in job.envs()
    restored = job.watch()
    assert restored is not None
    assert restored.identity == watch.identity
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    runner = runner_for(
        Bench(cast(Any, None), Boxes(), models()),
        job,
        bundle_of(job, blobs),
    )
    assert runner.watch is not None
    assert runner.watch.identity == watch.identity
    assert isinstance(runner.control.inner, OpenGate)
    (plain,) = scenario_jobs(bundle, models(), Prompts())
    assert (plain.monitor, plain.models, plain.watch()) == (None, {}, None)


async def test_monitor_for_resolves_a_signals_only_monitor(tmp_path: Path) -> None:
    bundle = scenario(tmp_path)
    agents: list[Profile] = [profile()]
    signals = monitor_for(
        models(), Prompts(), bundle.scenario, agents, layers=[{"preset": "signals"}]
    )
    assert signals is not None
    assert (signals.config.preset, signals.sweeps, signals.judge) == ("signals", {}, None)
    off = monitor_for(models(), Prompts(), bundle.scenario, agents, layers=[{"preset": "off"}])
    assert off is None


def test_the_cli_refuses_a_monitor_whose_key_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIMS_TEST_MONITOR_KEY", raising=False)
    config = tmp_path / "aims.toml"
    config.write_text(
        '[profiles.agent]\nbackend = "chat"\nbase_url = "http://agent.invalid/v1"\n'
        'model = "agent-model"\nweights = "agent-weights"\nreasoning = "full"\n'
        '[profiles.watcher]\nbackend = "chat"\nbase_url = "http://watcher.invalid/v1"\n'
        'model = "watcher-model"\nweights = "watcher-weights"\nreasoning = "full"\n'
        'api_key_env = "AIMS_TEST_MONITOR_KEY"\n'
        '[roles]\nagent = "agent"\nsweep = "watcher"\njudge = "watcher"\n'
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO.format(oversight="observe", verifier=""))
    args = ["run", str(path), "--config", str(config), "--runs-dir", str(tmp_path / "runs")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "AIMS_TEST_MONITOR_KEY is not set" in result.stderr
    assert 'preset = "signals"' in result.stderr

    human = tmp_path / "human.yaml"
    human.write_text(
        SCENARIO.format(oversight="human", verifier="") + "monitor: {preset: signals}\n"
    )
    args = ["run", str(human), "--config", str(config), "--runs-dir", str(tmp_path / "runs")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "human oversight waits for a person" in result.stderr
