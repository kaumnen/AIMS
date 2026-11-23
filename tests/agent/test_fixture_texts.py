"""The texts of the tools and of the agent loop keep the defaults the recorded fixture runs were
made with, byte for byte. Those runs predate most of the texts, so their `run.started` names
few of them: a fork fills in the rest from the defaults. The sandbox here answers each exec with
what the run recorded for it, so a tool gives again the output the run's model was given."""

import json
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script

from aims.agent import Agent, RunBudget, run_tool
from aims.agent.gate import OpenGate
from aims.channels import Board, ChannelSetup
from aims.events import (
    AgentFinished,
    ExecFinished,
    LocalEventStore,
    ReplayDivergence,
    RunStarted,
    ToolResult,
)
from aims.models import ModelClient, ModelsConfig
from aims.models.profiles import Roles
from aims.run.fork import Origin, load_origin, planner
from aims.run.runner import Bench, Fork
from aims.sandbox import AuditedSandbox
from aims.sandbox.audit import ExecResult
from aims.scenario.model import BoardChannel
from aims.tools import Tool, ToolContext, cap, load_tools

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parents[1] / "fixtures" / "runs"
RUNS = {
    "word-count": "01a18280-dc55-749f-9a4b-6aab5fe2986e",
    "misbehave": "01a18d25-cfba-703a-848c-265a144e481b",
    "crew": "01a187e0-6399-7344-aa3c-70e0f3df01b0",
}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    root = tmp_path / "runs"
    root.mkdir()
    for name, run_id in RUNS.items():
        shutil.copytree(FIXTURES / name / run_id, root / run_id)
    async with await LocalEventStore.open(root) as store:
        yield store


def events(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / name / RUNS[name] / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def execs(log: list[dict[str, Any]], agent_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Each exec the agent's tool calls made, in order: its command and how it finished."""
    started = {e["data"]["exec_id"]: e for e in log if e["type"] == "exec.started"}
    out = []
    for e in log:
        if e["type"] != "exec.finished":
            continue
        s = started[e["data"]["exec_id"]]
        if s["data"]["phase"] == "agent" and s["agent_id"] == agent_id:
            out.append((s["data"]["command"], e["data"]))
    return out


class Recorded:
    """Answers each exec with the next one the run recorded, which must be the same command."""

    def __init__(self, store: LocalEventStore, run_id: str, execs: list[tuple[str, Any]]) -> None:
        self.store = store
        self.run_id = run_id
        self.execs = list(execs)

    async def pause(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def _bytes(self, value: Any) -> bytes:
        if isinstance(value, str):
            return value.encode()
        return await self.store.get_blob(self.run_id, value["sha256"], value["size"])

    async def exec(self, command: str, **kw: Any) -> ExecResult:
        recorded, data = self.execs.pop(0)
        assert command == recorded
        out = await self._bytes(data["stdout"])
        err = await self._bytes(data["stderr"])
        blank = {"exec_id": str(uuid.uuid7()), "call_id": kw.get("call_id")}
        finished = ExecFinished.model_validate({**data, **blank, "stdout": "", "stderr": ""})
        return ExecResult(finished, out, err, out[-262144:], err[-262144:])


def profiles(origin: Origin) -> ModelsConfig:
    found = {p.name: p for schedule in origin.schedules.values() for _, p in schedule}
    first = next(iter(found))
    return ModelsConfig(profiles=found, roles=Roles(agent=first, sweep=first, judge=first))


def instruction(log: list[dict[str, Any]], agent_id: str) -> str:
    return next(
        e["data"]["text"]
        for e in log
        if e["type"] == "agent.input"
        and e["agent_id"] == agent_id
        and e["data"]["kind"] == "instruction"
    )


@pytest.mark.parametrize("name", ["word-count", "misbehave"])
async def test_a_run_recorded_before_the_new_texts_forks_with_no_divergence(
    store: LocalEventStore, name: str
) -> None:
    """Every step replays: each request must match a recorded one, tool descriptions and
    parameters included, and each tool must give again the output the run recorded."""
    run_id = RUNS[name]
    log = events(name)
    origin = await load_origin(store, run_id)
    (agent_id,) = origin.steps
    texts = origin.texts[agent_id]
    assert "param_path" not in texts.tools["edit"]
    fork = Fork(origin=run_id, step=origin.replayable[agent_id], strict=True)
    models = profiles(origin)
    bench = Bench(store, cast(Any, None), models)
    fork_id = await store.create_run(RunStarted(scenario=name, oversight="observe"))
    plan = (await planner(bench, origin, fork)(fork_id))[agent_id]
    scenario = origin.bundle.scenario
    config = scenario.agent(agent_id)
    agent = Agent(
        config=config,
        sandbox_config=scenario.sandbox,
        instruction=instruction(log, agent_id),
        texts=texts.agent,
        profile=plan.schedule[-1][1],
        client=ModelClient(store, fork_id, models, transport=Script().transport(), env={}),
        sandbox=cast(AuditedSandbox, Recorded(store, run_id, execs(log, agent_id))),
        store=store,
        run_id=fork_id,
        tools=load_tools(config.tool_names, texts.tools),
        run_budget=RunBudget(scenario.limits),
        replay=plan,
    )
    finished = await agent.run()
    assert (finished.reason, finished.detail) == ("submitted", None)
    payloads = [e.payload() for e in await store.events(fork_id, limit=10000)]
    assert not [p for p in payloads if isinstance(p, ReplayDivergence)]
    results = [p for p in payloads if isinstance(p, ToolResult)]
    assert len(results) == origin.steps[agent_id]
    assert all(r.replayed for r in results)
    assert [p.reason for p in payloads if isinstance(p, AgentFinished)] == ["submitted"]


async def recorded_text(store: LocalEventStore, run_id: str, value: Any) -> str:
    if isinstance(value, str):
        return value
    return (await store.get_blob(run_id, value["sha256"], value["size"])).decode()


@pytest.mark.parametrize("name", RUNS)
async def test_each_tool_gives_again_the_output_its_run_recorded(
    store: LocalEventStore, name: str
) -> None:
    """Board tools are left out: their outputs hang on the board's state."""
    run_id = RUNS[name]
    log = events(name)
    origin = await load_origin(store, run_id)
    scenario = origin.bundle.scenario
    results = {e["data"]["call_id"]: e["data"] for e in log if e["type"] == "tool.result"}
    checked = 0
    for agent_id, texts in origin.texts.items():
        tools = {t.name: t for t in load_tools(scenario.agent(agent_id).tool_names, texts.tools)}
        mine = execs(log, agent_id)
        for e in log:
            if e["type"] != "tool.call" or e["agent_id"] != agent_id:
                continue
            call_id = e["data"]["call_id"]
            if e["data"]["name"] not in tools:
                continue
            arguments = await recorded_text(store, run_id, e["data"]["arguments"])
            commands = sum(1 for c, d in mine if d["call_id"] == call_id)
            sandbox = Recorded(store, run_id, [x for x in mine if x[1]["call_id"] == call_id])
            assert len(sandbox.execs) == commands
            ctx = ToolContext(
                sandbox=cast(AuditedSandbox, sandbox),
                store=store,
                run_id=run_id,
                agent_id=agent_id,
                call_id=call_id,
                step=1,
                config=scenario.sandbox,
                gate=OpenGate(),
                omitted=texts.agent.omitted,
            )
            outcome = await run_tool(tools, ctx, e["data"]["name"], arguments, texts.agent)
            text, _ = cap(outcome.output.encode(), marker=texts.agent.omitted)
            result = results[call_id]
            assert text == await recorded_text(store, run_id, result["output"]), call_id
            assert outcome.error == result["error"]
            checked += 1
    assert checked >= 5


def described(tool: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A tool of a request body as its name, description and parameters, in either shape."""
    f = tool.get("function", tool)
    return f["name"], {"description": f["description"], "parameters": f["parameters"]}


@pytest.mark.parametrize("name", RUNS)
async def test_each_tool_is_described_as_its_run_described_it(
    store: LocalEventStore, name: str
) -> None:
    run_id = RUNS[name]
    origin = await load_origin(store, run_id)
    scenario = origin.bundle.scenario
    requests = await store.events(run_id, types=["model.request"], limit=1000)
    first: dict[str, dict[str, Any]] = {}
    for event in requests:
        if event.agent_id is not None and event.agent_id not in first:
            body = cast(dict[str, Any], event.data["body"])
            first[event.agent_id] = json.loads(
                await store.get_blob(run_id, body["sha256"], body["size"])
            )
    assert first
    for agent_id, body in first.items():
        texts = origin.texts[agent_id]
        tools: list[Tool] = load_tools(scenario.agent(agent_id).tool_names, texts.tools)
        for channel in scenario.channels_of(agent_id):
            if isinstance(channel, BoardChannel):
                setup = ChannelSetup(
                    config=channel,
                    scenario=scenario,
                    run_id=run_id,
                    store=store,
                    texts={agent_id: texts.channels},
                )
                tools += Board(setup).tools(agent_id)
        want = dict(described(t) for t in body["tools"])
        made = {
            t.name: {"description": s.description, "parameters": s.parameters}
            for t in tools
            if (s := t.spec(scenario.sandbox))
        }
        assert made == want, agent_id
