"""The monitor in the runner: its gate, the feed, its place in a run's phases, the actuator,
deny, verifier restore and taint, jobs and the CLI's checks. The sandbox and the monitor are
fakes with the real surfaces, so no Docker and no model is needed."""

import asyncio
import hashlib
import io
import os
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from scripted import Script, call, profile
from typer.testing import CliRunner

from aims.agent import GatePoint, OpenGate, PauseClock, Verdict
from aims.cli import app
from aims.events import (
    Event,
    LocalEventStore,
    MonitorAction,
    OperatorAction,
    RunStarted,
    SandboxArchived,
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
from aims.run.runner import Bench, Options, RunError, Runner, seeded
from aims.sandbox import DockerBackend
from aims.sandbox.base import Capture, Outcome, SandboxInfo, SandboxSpec
from aims.scenario import Bundle, load

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
        pass

    async def put_archive(self, path: str, data: bytes) -> None:
        pass

    async def get_archive(self, path: str) -> bytes:
        out = io.BytesIO()
        tarfile.open(fileobj=out, mode="w").close()
        return out.getvalue()

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
    with pytest.raises(RunError, match="no paused agent waits before a tool call"):
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
    assert fake_monitor[0].calls == ["start", "judge skip=True", "close"]


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
