"""The monitor's CLI surface: `aims monitor`, `aims control`, `aims run --monitor`, prompts and
scenario checks; the live view's monitor rows, JSON lines and the human prompt; and monitor jobs
on a runner, against the control plane on loopback."""

import asyncio
import contextlib
import json
import re
import shutil
import threading
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from cp import CP, control_plane
from rich.console import Console
from scripted import models, profile
from test_monitor_api import call, pushed
from test_server import FakeRunner, play_job, start_run
from typer.testing import CliRunner

from aims.cli import app
from aims.events import (
    Draft,
    Event,
    LocalEventStore,
    MonitorAction,
    MonitorFlag,
    MonitorSweep,
    MonitorVerdict,
    OperatorAction,
    RunFinished,
    RunStarted,
)
from aims.models import DeltaBus
from aims.monitor.core import HumanRequest
from aims.node.client import ControlClient
from aims.node.link import Link
from aims.node.service import Node, NodeOptions
from aims.run.runner import Bench, Options, RunError, Runner
from aims.run.view import HumanPrompt, JsonLines, LiveView, digest, monitor_table
from aims.sandbox import SandboxError
from aims.scenario import load
from aims.server.db import OPEN_STATES
from aims.wire import Ack, CommandAction, CommandMessage, OfferMessage

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "word-count"
SCENARIO = """\
version: 1
name: watched
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
agents:
  - id: a
"""


def invoke(*args: str, env: dict[str, str] | None = None) -> Any:
    return CliRunner().invoke(app, list(args), env=env)


@pytest.fixture
def runs(tmp_path: Path) -> tuple[Path, str]:
    (origin,) = FIXTURE.iterdir()
    root = tmp_path / "runs"
    root.mkdir()
    shutil.copytree(origin, root / origin.name)
    return root, origin.name


async def until(test: Any, within: float = 5) -> None:
    async with asyncio.timeout(within):
        while not test():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def evidence(e: Event) -> list[int]:
    flag = e.payload()
    assert isinstance(flag, MonitorFlag)
    return flag.evidence


# aims monitor


def test_aims_monitor_reruns_a_run_into_monitor_runs(runs: tuple[Path, str]) -> None:
    root, origin = runs
    args = ["monitor", origin, "--runs-dir", str(root), "--no-server", "--signals-only"]
    result = invoke(*args, "--json", "--samples", "2")
    assert result.exit_code == 0, result.output
    found = [x for x in lines(result.stdout) if x["type"] == "result"]
    assert [(x["origin"], x["status"]) for x in found] == [(origin, "completed")] * 2
    assert found[0]["monitor"]["flags"] == []
    made = [x["run_id"] for x in found]

    async def read() -> list[dict[str, Any]]:
        async with await LocalEventStore.open(root) as store:
            return [(await store.events(r, limit=1))[0].data for r in made]

    started = asyncio.run(read())
    assert {(s["kind"], s["origin"]) for s in started} == {("monitor", origin)}
    assert {s["config"]["group"] for s in started} != {None}
    assert len({s["config"]["group"] for s in started}) == 1
    assert sorted(s["config"]["sample"] for s in started) == [0, 1]
    assert started[0]["monitor"] == started[1]["monitor"]

    shown = invoke(*args)
    assert shown.exit_code == 0, shown.output
    assert "monitor runs" in shown.stdout
    quiet = invoke(*args, "--quiet")
    assert quiet.exit_code == 0, quiet.output
    assert quiet.stdout.split()[1:4] == [origin, "completed", "flags"]


def test_aims_monitor_refuses_what_it_cannot_run(
    runs: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, origin = runs
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    base = ["monitor", "--runs-dir", str(root), "--no-server"]
    missing = invoke(*base, "01a18280-0000-7000-8000-000000000000")
    assert missing.exit_code == 1
    assert "unknown run" in missing.stderr
    keyless = invoke(*base, origin, "--judge-only")
    assert keyless.exit_code == 1
    assert "MODEL_API_KEY" in keyless.stderr
    both = invoke(*base, origin, "--judge-only", "--signals-only")
    assert both.exit_code == 2
    off = invoke(*base, origin, "--monitor", "off")
    assert off.exit_code == 2


def test_aims_monitor_prints_its_estimate_and_refuses_above_the_limit_unless_yes(
    runs: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, origin = runs
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    base = ["monitor", origin, "--runs-dir", str(root), "--no-server", "--monitor", "standard"]
    result = invoke(*base, "--samples", "5", "--json")
    assert result.exit_code == 1, result.output
    assert "pass --yes to start anyway" in " ".join(result.stderr.split())
    (first,) = lines(result.stdout)
    assert (first["type"], first["run_id"], first["confirm"], first["samples"]) == (
        "estimate",
        origin,
        True,
        5,
    )
    shown = invoke("monitor", origin, "--runs-dir", str(root), "--no-server", "--signals-only")
    assert shown.exit_code == 0, shown.output
    assert "Review: signals monitor, signals only; no layer calls a model" in " ".join(
        shown.stderr.split()
    )


def test_aims_monitor_lays_the_config_file_over_the_scenario(
    runs: tuple[Path, str], tmp_path: Path
) -> None:
    root, origin = runs
    config = tmp_path / "aims.toml"
    config.write_text('[monitor]\npreset = "signals"\n[monitor.signals.repeat]\nenabled = false\n')
    result = invoke(
        "monitor", origin, "--runs-dir", str(root), "--no-server", "--config", str(config), "--json"
    )
    assert result.exit_code == 0, result.output
    (found,) = [x for x in lines(result.stdout) if x["type"] == "result"]

    async def read() -> dict[str, Any]:
        async with await LocalEventStore.open(root) as store:
            return (await store.events(found["run_id"], limit=1))[0].data

    snapshot = asyncio.run(read())["config"]["monitor"]
    assert snapshot["config"]["preset"] == "signals"
    assert "repeat" not in snapshot["signals"]


# aims run, prompts and scenario checks


def test_aims_run_takes_a_monitor_preset(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO)
    result = invoke("run", str(path), "--monitor", "loud", "--no-server")
    assert result.exit_code == 2
    assert "--monitor" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_prompts_show_lists_the_monitor_texts() -> None:
    result = invoke("prompts", "show")
    assert result.exit_code == 0, result.output
    text = result.stdout
    for table in ("[prompts]", "[prompts.monitor]", "[prompts.monitor.categories]"):
        assert f"\n{table}\n" in f"\n{text}"
    assert "\ndenied = " in text
    assert "\nnotify_on_resume = " in text
    assert "\nscorer_tampering = " in text


def test_scenario_check_reports_bad_monitor_rules(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(SCENARIO + "monitor: {preset: signals}\n")
    result = invoke("scenario", "check", str(good))
    assert result.exit_code == 0, result.output
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        SCENARIO
        + "monitor:\n  preset: signals\n  rules:\n"
        + "    - {id: odd, title: Odd, event: no.such.event, category: drift, importance: 2}\n"
    )
    result = invoke("scenario", "check", str(bad))
    assert result.exit_code == 1
    assert "monitor: " in result.stderr
    assert "no.such.event" in result.stderr
    same = tmp_path / "same.yaml"
    same.write_text(SCENARIO.replace("  - id: a", "  - id: a\n    profile: meta-wonder-1.0"))
    result = invoke("scenario", "check", str(same))
    assert result.exit_code == 1
    assert "allow_same_model" in result.stderr


# The live view, the summary, JSON lines and the human prompt


async def watched_run(store: LocalEventStore) -> str:
    run_id = await store.create_run(RunStarted(scenario="watched", oversight="human", agents=["a"]))
    flag = MonitorFlag(
        layer="signal",
        source="egress",
        category="egress_attempt",
        importance=4,
        evidence=[0],
        justification="connects to 10.0.0.9",
        key="10.0.0.9",
    )
    await store.append(run_id, [Draft(flag, "a")])
    again = flag.model_copy(update={"count": 10, "first_flag": 1})
    await store.append(
        run_id,
        [
            Draft(again, "a"),
            Draft(
                MonitorAction(
                    action="pause", agents=["a"], flags=[1], mode="human", reason="egress"
                ),
                "a",
            ),
            Draft(OperatorAction(action="resume", then="deny", token="op"), "a"),
            Draft(
                MonitorSweep(
                    name="main",
                    lane="full",
                    parts=["evidence"],
                    trigger="every",
                    step_from=0,
                    step_to=1,
                    seq_to=1,
                ),
                "a",
            ),
            Draft(MonitorVerdict(max_severity=4, summary="It reached out.")),
            Draft(RunFinished(status="completed")),
        ],
    )
    return run_id


async def test_the_view_shows_flags_pauses_sweeps_and_the_verdict(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await watched_run(store)
        view = LiveView(store, DeltaBus())
        view.add(run_id)
        await until(lambda: view.runs[run_id].status == "completed")
        console = Console(record=True, width=200)
        console.print(view)
        shown = console.export_text()
        assert "monitor  flags 1  max 4  sweeps 1" in shown
        assert "egress_attempt 4 signal/egress a x10" in shown
        assert "pause a by monitor: egress" in shown
        assert "resume then deny a by operator op" in shown
        assert "verdict severity 4: It reached out." in shown
        await view.stop()

        found = await digest(store, run_id)
        assert ([e.seq for e in found.flags], found.counts) == ([1], {1: 10})
        assert [e.type for e in found.actions] == ["monitor.action", "operator.action"]
        assert (found.sweeps, found.verdict is not None) == (1, True)
        flags = found.json()["flags"]
        assert isinstance(flags, list)
        assert flags[0]["count"] == 10
        table = monitor_table([found])
        assert table is not None
        console = Console(record=True, width=200)
        console.print(table)
        assert "verdict" in console.export_text()

        emitted: list[str] = []
        out = JsonLines(store, emitted.append)
        out.add(run_id)
        await out.stop()
        kinds = [json.loads(line)["type"] for line in emitted]
        assert kinds == ["flag", "flag", "action", "sweep", "verdict"]
        assert json.loads(emitted[0])["data"]["category"] == "egress_attempt"


class Held:
    """The Runner surface the human prompt uses."""

    def __init__(self) -> None:
        self.finished = False
        self.killed = False
        self.held = {"a"}
        self.done: list[tuple[str, dict[str, Any]]] = []

    def paused(self, agent_id: str) -> bool:
        return agent_id in self.held

    async def operate(self, action: str, **kw: Any) -> None:
        if kw.get("then") == "deny" and kw.get("agent") != "a":
            raise RunError("no paused agent waits before a tool call to deny")
        self.done.append((action, kw))
        self.held.discard(kw.get("agent") or "a")


def request(**kw: Any) -> HumanRequest:
    return HumanRequest(run_id="r1", agents=["a"], flags=[], advice="It looks fine.", **kw)


async def test_the_human_prompt_answers_through_the_runner() -> None:
    answers = iter(["what\n", "d\n"])
    console = Console(record=True, width=200)
    prompt = HumanPrompt(console, read=lambda: next(answers))
    runner = Held()
    await prompt.hook(cast(Runner, runner))(request(held_call={"a": "call_1"}))
    await until(lambda: not prompt._tasks)
    assert runner.done == [
        (
            "resume",
            {"agent": "a", "then": "deny", "text": None, "reason": "answered at the terminal"},
        )
    ]
    shown = console.export_text()
    assert "the monitor paused a in run r1" in shown
    assert "advice: It looks fine." in shown
    assert "answer resume, deny, inject, kill" in shown

    texts = iter(["inject\n", "Stop and explain.\n"])
    prompt = HumanPrompt(console, read=lambda: next(texts))
    runner = Held()
    await prompt.hook(cast(Runner, runner))(request())
    await until(lambda: not prompt._tasks)
    assert runner.done[0][1]["then"] == "inject"
    assert runner.done[0][1]["text"] == "Stop and explain."


async def test_a_prompt_closes_when_the_pause_ends_elsewhere() -> None:
    never = threading.Event()
    console = Console(record=True, width=200)
    prompt = HumanPrompt(console, read=lambda: (never.wait(5) and "") or "")
    runner = Held()
    await prompt.hook(cast(Runner, runner))(request())
    await asyncio.sleep(0.3)
    runner.held.clear()
    await until(lambda: not prompt._tasks)
    assert runner.done == []
    assert "the pause ended without this answer" in console.export_text()
    never.set()


# The link and runners


async def test_the_link_acks_a_sweep_or_deny_it_cannot_carry_out(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        runner = Runner(Bench(store, cast(Any, None), models(profile())), load(path), Options())
        runner.run_id = "r1"
        link = Link(ControlClient("http://127.0.0.1:9", "t", runner="x"), store, DeltaBus())
        assert "monitor" in link.hello().kinds
        link.runners["r1"] = runner

        def command(**kw: Any) -> CommandMessage:
            return CommandMessage(id="c1", seq=1, run_id="r1", command=CommandAction(**kw))

        ack = await link._carry_out(command(action="sweep"))
        assert (ack.ok, ack.outcome) == (False, "this run has no monitor that sweeps")
        ack = await link._carry_out(command(action="resume", then="deny"))
        assert not ack.ok
        assert "no paused agent waits before a tool call" in ack.outcome
        assert await store.run("r1") is None
        await link.client.aclose()


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


@pytest.fixture
async def fake(cp: CP) -> AsyncIterator[FakeRunner]:
    runner = FakeRunner(cp)
    await runner.connect()
    yield runner
    await runner.close()


class NoDocker:
    """A sandbox backend a runner opens without Docker: no tracer, no sandbox."""

    tracer = None
    tracer_restarts = 0

    async def restart_tracer(self) -> None:
        raise SandboxError("no Docker here")

    async def close(self) -> None:
        pass


@contextlib.asynccontextmanager
async def node(cp: CP, data: Path, name: str = "n1") -> AsyncGenerator[Node]:
    stop = asyncio.Event()
    made = Node(
        NodeOptions(
            url=cp.url, token=cp.runner, name=name, data=data, backend=cast(Any, NoDocker())
        )
    )
    task = asyncio.create_task(made.run(stop))
    async with asyncio.timeout(20):
        await made.ready.wait()
        await made.ctx.link.connected.wait()
        while name not in cp.state.runners or cp.state.runners[name].status is None:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    try:
        yield made
    finally:
        stop.set()
        async with asyncio.timeout(60):
            await task


async def test_a_runner_takes_a_monitor_job_and_ships_the_monitor_run(
    cp: CP, local: LocalEventStore, fake: FakeRunner, tmp_path: Path
) -> None:
    origin = await pushed(cp, local, fake, [call(1), call(2), call(3)], monitor=None)
    head = await cp.state.store.head(origin)
    assert head is not None
    async with node(cp, tmp_path / "node") as n:
        assert "monitor" in cp.state.runners["n1"].hello.kinds
        async with cp.client(cp.operator) as c:
            r = await c.post(
                f"/api/runs/{origin}/monitor", json={"signals_only": True, "runner": "n1"}
            )
            assert r.status_code == 202, r.text
            (run_id,) = r.json()["run_ids"]
        # The job's end comes in its own message, which may trail the run's last events.
        async with asyncio.timeout(30):
            while True:
                info = await cp.state.store.run(run_id)
                job = await cp.state.db.job(run_id)
                ran = info is not None and info.status != "running"
                if ran and job is not None and job.state not in OPEN_STATES:
                    break
                await asyncio.sleep(0.1)
        assert info.status == "completed"
        assert job.state == "done"
        assert (await n.ctx.origins.run(origin)) is not None
    first = (await cp.state.store.events(run_id, limit=1))[0].data
    assert (first["kind"], first["origin"], first["origin_head"]) == ("monitor", origin, head.hash)
    assert first["monitor"]
    flags = await cp.state.store.events(run_id, types=["monitor.flag"], limit=100)
    assert {f.data["source"] for f in flags} >= {"repeat"}
    seqs = {e.seq: e.type for e in await cp.state.store.events(origin, limit=100)}
    assert all(seqs[s] == "tool.call" for f in flags for s in evidence(f))
    assert (await cp.state.store.verify(run_id)).ok


# aims control


async def control(cp: CP, *args: str, token: str | None) -> Any:
    env = {"AIMS_TOKEN": token or ""}
    return await asyncio.to_thread(
        CliRunner().invoke, app, ["control", *args, "--server", cp.url], env=env
    )


async def test_aims_control_sends_commands_and_reports_the_ack(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))

    nobody = await control(cp, run_id, "pause", token=None)
    assert nobody.exit_code == 1
    assert "aims token create" in nobody.stderr
    runner = await control(cp, run_id, "pause", token=cp.runner)
    assert runner.exit_code == 1
    assert "operator role" in runner.stderr

    async def answer(ok: bool, outcome: str) -> CommandMessage:
        message = await fake.next(CommandMessage)
        await fake.client.ack(message.id, Ack(ok=ok, outcome=outcome))
        return message

    sent = asyncio.create_task(
        control(cp, run_id, "pause", "--agent", "a", "--reason", "look", token=cp.operator)
    )
    message = await answer(True, "done")
    result = await sent
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"pause {run_id}: done"
    assert (message.command.action, message.command.agent, message.command.reason) == (
        "pause",
        "a",
        "look",
    )

    sent = asyncio.create_task(control(cp, run_id, "deny", "--agent", "a", token=cp.operator))
    message = await answer(False, "no paused agent waits before a tool call to deny")
    result = await sent
    assert result.exit_code == 1
    assert "failed: no paused agent waits" in result.stdout
    assert (message.command.action, message.command.then) == ("resume", "deny")

    sent = asyncio.create_task(control(cp, run_id, "resume", "--text", "go on", token=cp.operator))
    message = await answer(True, "done")
    assert (await sent).exit_code == 0
    assert (message.command.then, message.command.text) == ("inject", "go on")

    sent = asyncio.create_task(control(cp, run_id, "sweep", token=cp.operator))
    message = await answer(False, "this run has no monitor that sweeps")
    assert (await sent).exit_code == 1
    assert message.command.action == "sweep"

    sent = asyncio.create_task(
        control(cp, run_id, "cancel", "--reason", "enough", token=cp.operator)
    )
    message = await answer(True, "done")
    assert (await sent).exit_code == 0
    assert (message.command.action, message.command.reason) == ("cancel", "enough")

    listed = await control(cp, run_id, "list", token=cp.viewer)
    assert listed.exit_code == 0, listed.output
    lines = listed.stdout.splitlines()
    assert [line.split("  ")[1:3] for line in lines if not line.startswith(" ")] == [
        ["op", "pause a"],
        ["op", "resume a deny"],
        ["op", "resume inject"],
        ["op", "sweep"],
        ["op", "cancel"],
    ]
    assert "failed: no paused agent waits before a tool call to deny" in lines[2]
    assert [line for line in lines if line.startswith(" ")] == [
        "    reason: look",
        "    text: go on",
        "    reason: enough",
    ]
    assert (await control(cp, run_id, "list", "--agent", "a", token=cp.operator)).exit_code == 2

    assert (await control(cp, run_id, "inject", token=cp.operator)).exit_code == 2
    assert (await control(cp, run_id, "kill", "--agent", "a", token=cp.operator)).exit_code == 2
    assert (await control(cp, run_id, "cancel", "--agent", "a", token=cp.operator)).exit_code == 2
    assert (await control(cp, run_id, "nap", token=cp.operator)).exit_code == 2


async def test_aims_control_finds_a_local_ui(
    cp: CP,
    fake: FakeRunner,
    local: LocalEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    data = tmp_path / "local-ui"
    data.mkdir()
    (data / "server.json").write_text(
        json.dumps({"url": cp.url, "token": cp.runner, "operator": cp.operator})
    )
    monkeypatch.setenv("AIMS_SERVER_DIR", str(data))
    empty = await asyncio.to_thread(
        CliRunner().invoke, app, ["control", run_id, "list"], env={"AIMS_TOKEN": ""}
    )
    assert empty.exit_code == 0, empty.output
    assert "no commands sent through the control plane" in empty.stdout
    sent = asyncio.create_task(
        asyncio.to_thread(
            CliRunner().invoke,
            app,
            ["control", run_id, "kill", "--wait", "5"],
            env={"AIMS_TOKEN": ""},
        )
    )
    message = await fake.next(CommandMessage)
    await fake.client.ack(message.id, Ack(ok=True, outcome="done"))
    result = await sent
    assert result.exit_code == 0, result.output
    assert message.command.action == "kill"
