import asyncio
import hashlib
import io
import os
import tarfile
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, Turn, call, models, profile

from aims.agent import DENY, GateKind, GatePoint, Verdict
from aims.events import (
    AgentFinished,
    AgentInput,
    Draft,
    Event,
    LocalEventStore,
    OperatorAction,
    RunFinished,
    RunStarted,
    SandboxArchived,
    SandboxStopped,
    ToolResult,
)
from aims.prompts import Prompts
from aims.run.control import ControlGate
from aims.run.job import Job, bundle_of, runner_for, scenario_jobs
from aims.run.runner import RESTARTED, Bench, Options, Runner, seal_unsealed
from aims.sandbox import DockerBackend
from aims.scenario import load

pytestmark = pytest.mark.anyio

SCENARIO = """\
version: 1
name: control
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
  verifier:
    command: echo 1 > "$AIMS_LOGS_DIR/reward.txt"
agents:
"""


def scenario(tmp_path: Path, *agents: str) -> Path:
    path = tmp_path / "control.yaml"
    body = "".join(f"  - id: {a}\n    prompts: {{system: 'agent {a}'}}\n" for a in agents)
    path.write_text(SCENARIO + body)
    return path


def per_agent(turns: dict[str, list[Turn]]) -> Script:
    """A script whose answers depend on the agent asking, told apart by its system prompt."""

    def answer(body: dict[str, Any]) -> Turn:
        system = body["messages"][0]["content"]
        return turns[system.removeprefix("agent ")].pop(0)

    return Script(*[answer] * sum(len(t) for t in turns.values()))


# Gate, no sandbox


def point(agent: str = "a", kind: GateKind = "before_model", call: str | None = None) -> GatePoint:
    return GatePoint(kind, "run", agent, 0, call)


async def test_a_paused_agent_holds_until_resumed() -> None:
    gate = ControlGate()
    assert (await gate.check(point())).action == "continue"
    gate.pause(["a"])
    assert (await gate.check(point())).action == "pause"
    assert (await gate.check(point("b"))).action == "continue"
    held = asyncio.create_task(gate.held(point()))
    await asyncio.sleep(0)
    assert gate.is_held("a")
    assert gate.resume(["a"], Verdict("inject", text="hi")) == []
    verdict = await held
    assert (verdict.action, verdict.text) == ("inject", "hi")
    assert not gate.is_held("a")
    assert (await gate.check(point())).action == "continue"


async def test_a_resume_before_the_hold_reaches_the_next_point() -> None:
    gate = ControlGate()
    gate.pause(["a"])
    assert gate.resume(["a"], Verdict("inject", text="later")) == ["a"]
    assert (await gate.held(point())).action == "continue"
    first = await gate.check(point())
    assert (first.action, first.text) == ("inject", "later")
    with pytest.raises(ValueError, match="continue, inject or deny"):
        gate.resume(["a"], Verdict("kill"))


async def test_kill_answers_every_point() -> None:
    gate = ControlGate()
    gate.inject(["a"], "x")
    gate.kill("enough")
    assert await gate.check(point()) == Verdict("kill", reason="enough")
    assert (await gate.held(point("b"))).action == "kill"


class Inner:
    """An inner gate that runs `act` while it decides, as the monitor's barrier does."""

    def __init__(self) -> None:
        self.act: Callable[[GatePoint], object] | None = None
        self.checks: list[GatePoint] = []

    async def check(self, point: GatePoint) -> Verdict:
        self.checks.append(point)
        await asyncio.sleep(0)
        if self.act is not None:
            act, self.act = self.act, None
            act(point)
        return Verdict()

    async def held(self, point: GatePoint) -> Verdict:
        return Verdict()


async def test_a_pause_asked_while_the_inner_gate_decides_holds_at_that_point() -> None:
    inner = Inner()
    gate = ControlGate(inner)
    inner.act = lambda p: gate.pause([p.agent_id], "flagged")
    at = point(kind="before_tool", call="c1")
    assert await gate.check(at) == Verdict("pause", reason="flagged")
    assert gate.held_call("a") == "c1"
    assert gate.clock("a").paused
    held = asyncio.create_task(gate.held(at))
    await asyncio.sleep(0)
    assert gate.is_held("a")
    assert gate.resume(["a"]) == []
    assert (await held).action == "continue"
    assert len(inner.checks) == 2
    assert gate.held_call("a") is None
    assert not gate.clock("a").paused


async def test_a_deny_right_after_a_pause_refuses_the_call_it_waits_before() -> None:
    inner = Inner()
    gate = ControlGate(inner)

    def deny(p: GatePoint) -> None:
        gate.pause([p.agent_id], "flagged")
        assert gate.held_call(p.agent_id) == "c1"
        assert gate.resume([p.agent_id], DENY) == ["a"]

    inner.act = deny
    assert await gate.check(point(kind="before_tool", call="c1")) == DENY
    assert gate.held_call("a") is None
    assert (await gate.check(point(kind="before_tool", call="c2"))).action == "continue"


async def test_a_deny_needs_the_agent_paused() -> None:
    inner = Inner()
    gate = ControlGate(inner)
    inner.act = lambda p: gate.resume([p.agent_id], DENY)
    assert (await gate.check(point(kind="before_tool", call="c1"))).action == "continue"


async def test_a_held_agent_is_denied_only_at_a_tool_call() -> None:
    gate = ControlGate()
    gate.pause(["a", "b"])
    at_tool = point("a", "before_tool", "c1")
    at_reply = point("b", "after_reply")
    assert (await gate.check(at_tool)).action == "pause"
    assert (await gate.check(at_reply)).action == "pause"
    held = [asyncio.create_task(gate.held(p)) for p in (at_tool, at_reply)]
    await asyncio.sleep(0)
    assert (gate.held_call("a"), gate.held_call("b")) == ("c1", None)
    assert gate.resume(["a", "b"], DENY) == []
    assert [v.action for v in await asyncio.gather(*held)] == ["deny", "continue"]


# Sealing runs a dead process left open


async def test_runs_left_open_are_sealed_failed(tmp_path: Path) -> None:
    started = RunStarted(scenario="x", oversight="observe")
    async with await LocalEventStore.open(tmp_path) as store:
        left = await store.create_run(started)
        done = await store.create_run(started)
        await store.append(done, [Draft(RunFinished(status="completed"))])
    async with await LocalEventStore.open(tmp_path) as store:
        live = await store.create_run(started)
        async with await LocalEventStore.open(tmp_path) as second:
            assert await seal_unsealed(second) == [left]
        info = await store.run(left)
        assert info is not None
        assert info.status == "failed"
        (sealed,) = [e for e in await store.events(left) if e.type == "run.finished"]
        assert sealed.data["reason"] == RESTARTED
        live_info = await store.run(live)
        assert live_info is not None
        assert live_info.status == "running"


# Real sandboxes


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        audit = "kernel" if os.environ.get("AIMS_REQUIRE_TRACER") else "auto"
        backend = await DockerBackend.open(audit=audit)
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        yield backend


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def until(test: Callable[[], bool], within: float = 120) -> None:
    async with asyncio.timeout(within):
        while True:
            if test():
                return
            await asyncio.sleep(0.05)


async def wait_for(
    store: LocalEventStore, runner: Runner, test: Callable[[Event], bool], within: float = 120
) -> Event:
    async with asyncio.timeout(within):
        await until(lambda: runner.run_id is not None)
        assert runner.run_id is not None
        while True:
            for event in await store.events(runner.run_id, limit=1000):
                if test(event):
                    return event
            await asyncio.sleep(0.1)


def control_bench(store: LocalEventStore, backend: DockerBackend, script: Script) -> Bench:
    return Bench(
        store, backend, models(profile()), transport=script.transport(), gate=ControlGate()
    )


async def of[T](store: LocalEventStore, run_id: str, kind: type[T], name: str) -> list[T]:
    events = await store.events(run_id, limit=1000, types=[name])
    return [p for p in (e.payload() for e in events) if isinstance(p, kind)]


@pytest.mark.docker
async def test_kill_mid_exec_archives_and_seals_killed(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    script = Script(
        call("bash", command="echo kept > /work/note.txt; sleep 60", timeout_s=120),
        call("submit", answer="never"),
    )
    runner = Runner(control_bench(store, backend, script), load(scenario(tmp_path, "a")), Options())
    task = asyncio.create_task(runner.run())
    await wait_for(store, runner, lambda e: e.type == "exec.started" and e.data["phase"] == "agent")
    await asyncio.sleep(1)
    start = time.monotonic()
    await runner.operate("kill", reason="test kill", token="alice", command_id="c1")
    result = await task
    took = time.monotonic() - start

    assert (result.status, result.reason) == ("killed", "test kill")
    assert took < 30
    run_id = result.run_id
    assert run_id is not None
    assert not await store.events(run_id, types=["task.scored"])
    (action,) = await of(store, run_id, OperatorAction, "operator.action")
    assert (action.action, action.token, action.command_id) == ("kill", "alice", "c1")
    (archived,) = await of(store, run_id, SandboxArchived, "sandbox.archived")
    assert archived.archive is not None
    with tarfile.open(
        fileobj=io.BytesIO(await store.get_blob(run_id, archived.archive.sha256))
    ) as t:
        note = t.extractfile("work/note.txt")
        assert note is not None
        assert note.read() == b"kept\n"
    stopped = await of(store, run_id, SandboxStopped, "sandbox.stopped")
    assert [s.reason for s in stopped] == ["killed"]
    (finished,) = await of(store, run_id, AgentFinished, "agent.finished")
    assert (finished.reason, finished.detail) == ("killed", "test kill")
    assert len(script.requests) == 1
    assert (await store.verify(run_id)).ok


@pytest.mark.docker
async def test_a_pause_mid_exec_does_not_count_against_its_timeout(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    script = Script(
        call("bash", command="sleep 2; echo done", timeout_s=3),
        call("submit", answer="ok"),
    )
    runner = Runner(control_bench(store, backend, script), load(scenario(tmp_path, "a")), Options())
    task = asyncio.create_task(runner.run())
    await wait_for(store, runner, lambda e: e.type == "exec.started" and e.data["phase"] == "agent")
    await asyncio.sleep(0.5)
    await runner.operate("pause")
    # Past the host's own deadline too: 3 s, 1 s grace and 5 s to drain.
    await asyncio.sleep(9)
    await runner.operate("resume")
    result = await task

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    tool = (await of(store, result.run_id, ToolResult, "tool.result"))[0]
    assert "done" in str(tool.output)
    assert "timed out" not in str(tool.output)
    actions = await of(store, result.run_id, OperatorAction, "operator.action")
    assert [(a.action, a.then) for a in actions] == [("pause", None), ("resume", "continue")]
    assert result.scores["a"].reward == 1.0


@pytest.mark.docker
async def test_a_held_agent_takes_an_inject_on_resume(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    script = Script(call("submit", answer="ok"))
    runner = Runner(control_bench(store, backend, script), load(scenario(tmp_path, "a")), Options())
    task = asyncio.create_task(runner.run())
    await wait_for(store, runner, lambda e: e.type == "run.started")
    await runner.operate("pause", reason="look first")
    control = runner.control
    assert control is not None
    await until(lambda: control.is_held("a"))
    assert not script.requests
    await runner.operate("resume", then="inject", text="the operator says hi")
    result = await task

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    inputs = await of(store, result.run_id, AgentInput, "agent.input")
    assert [(i.kind, i.text) for i in inputs][-1] == ("inject", "the operator says hi")
    sent = [m["content"] for m in script.requests[0]["messages"]]
    assert "the operator says hi" in sent


@pytest.mark.docker
async def test_pausing_one_agent_lets_the_other_finish(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    script = per_agent(
        {
            "a": [call("bash", command="echo a"), call("submit", answer="a")],
            "b": [call("bash", command="echo b"), call("submit", answer="b")],
        }
    )
    runner = Runner(
        control_bench(store, backend, script), load(scenario(tmp_path, "a", "b")), Options()
    )
    task = asyncio.create_task(runner.run())
    await wait_for(store, runner, lambda e: e.type == "run.started")
    await runner.operate("pause", agent="a")
    await wait_for(
        store, runner, lambda e: e.type == "agent.finished" and e.agent_id == "b", within=180
    )
    control = runner.control
    assert control is not None
    assert control.is_held("a")
    await runner.operate("resume", agent="a")
    result = await task
    assert result.status == "completed", result.reason
    assert {a: f.reason for a, f in result.agents.items()} == {"a": "submitted", "b": "submitted"}


@pytest.mark.docker
async def test_a_job_runs_as_its_run_id(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    bundle = load(scenario(tmp_path, "a"))
    (job,) = scenario_jobs(bundle, models(profile()), Prompts(), audit="snapshot")
    job = Job.model_validate_json(job.model_dump_json())
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    assert set(job.blobs()) <= set(blobs)
    assert job.needs() == {"envs": [], "kernel": False}
    script = Script(call("submit", answer="ok"))
    bench = Bench(store, backend, models(profile(name="unused")), transport=script.transport())
    runner = runner_for(bench, job, bundle_of(job, blobs))
    result = await runner.run()
    assert result.status == "completed", result.reason
    assert result.run_id == job.run_id
    started = (await store.events(job.run_id, limit=1))[0].payload()
    assert isinstance(started, RunStarted)
    assert started.config["audit"] == "snapshot"


async def test_a_paused_agent_holds_before_it_takes_an_inject() -> None:
    gate = ControlGate()
    gate.pause(["a"])
    gate.inject(["a"], "later")
    assert (await gate.check(point())).action == "pause"
    gate.resume(["a"])
    verdict = await gate.check(point())
    assert (verdict.action, verdict.text) == ("inject", "later")


async def test_a_command_sent_twice_while_running_is_carried_out_once(tmp_path: Path) -> None:
    from aims.models import DeltaBus
    from aims.node.link import Link
    from aims.wire import Ack, CommandAction, CommandMessage

    acks: list[tuple[str, Ack]] = []

    class Client:
        runner = "r"

        async def ack(self, command_id: str, ack: Ack) -> None:
            acks.append((command_id, ack))

    calls: list[str] = []
    release = asyncio.Event()

    class Slow:
        finished = False

        async def operate(self, action: str, **kw: object) -> None:
            calls.append(action)
            await release.wait()

    async with await LocalEventStore.open(tmp_path) as store:
        link = Link(cast(Any, Client()), store, DeltaBus())
        link.runners["run"] = cast(Any, Slow())
        message = CommandMessage(id="c1", seq=1, run_id="run", command=CommandAction(action="kill"))
        first = asyncio.create_task(link._command(message))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(link._command(message))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)
    assert calls == ["kill"]
    assert [c for c, _ in acks] == ["c1", "c1"]
