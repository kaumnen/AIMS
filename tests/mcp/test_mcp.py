"""`aims mcp`: the tools an MCP client gets over the control plane's API, what a viewer and an
operator see, the cost question before runs that spend and the server over stdio."""

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from mcp import Client, StdioServerParameters
from mcp import types as mt
from test_monitor_api import MonitorRunner, call, flagged, live, pushed, seal
from test_scoring import took
from test_server import FakeRunner, push_forged
from typer.testing import CliRunner

from aims.cli import app
from aims.events import Draft, LocalEventStore, MonitorAction
from aims.mcp.client import Api
from aims.mcp.server import build
from aims.server import api as routes
from aims.wire import Ack, CommandMessage

pytestmark = pytest.mark.anyio

READS = {
    "check",
    "control_list",
    "estimate",
    "event_show",
    "events",
    "flags",
    "models_list",
    "prompts_show",
    "report",
    "rates",
    "run_show",
    "runs",
    "scenario_check",
    "scenario_schema",
    "scenario_show",
    "scenarios",
    "search",
    "spend",
    "steps",
    "verify",
}
ACTIONS = {"control", "fork", "monitor", "run", "score"}
# Five runs whose agent may spend $5 each: the caps allow $25, above the $20 default.
COSTLY = 5


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


class Person:
    """Answers each question with `answer`. Keeps the questions."""

    def __init__(self, answer: mt.ElicitResult) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: mt.ElicitRequestParams) -> mt.ElicitResult:
        self.asked.append(params.message)
        return self.answer


YES = mt.ElicitResult(action="accept", content={"start": True})
NO = mt.ElicitResult(action="decline")


@contextlib.asynccontextmanager
async def client(
    cp: CP,
    token: str,
    *,
    act: bool = False,
    person: Person | None = None,
    mode: Any = "auto",
) -> AsyncGenerator[Client]:
    async with Api(cp.url, token) as api:
        server = build(api, act=act)
        async with Client(server, elicitation_callback=person, mode=mode) as c:
            yield c


def data(result: mt.CallToolResult) -> Any:
    assert not result.is_error, text(result)
    found = result.structured_content
    return found["result"] if isinstance(found, dict) and set(found) == {"result"} else found


def text(result: mt.CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, mt.TextContent))


def error(result: mt.CallToolResult) -> str:
    """The text of a tool error, after the name of the tool the SDK puts first."""
    assert result.is_error, text(result)
    return text(result).split(": ", 1)[1]


async def test_reads_come_alone_and_actions_only_with_act(cp: CP) -> None:
    async with client(cp, cp.operator) as c:
        tools = (await c.list_tools()).tools
        assert {t.name for t in tools} == READS
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        assert all(t.description for t in tools)
    async with client(cp, cp.operator, act=True) as c:
        tools = (await c.list_tools()).tools
        assert {t.name for t in tools} == READS | ACTIONS
        acting = [t for t in tools if t.name in ACTIONS]
        assert not any(t.annotations and t.annotations.read_only_hint for t in acting)
        run = next(t for t in tools if t.name == "run")
        # the person's answer is the server's to get, never the model's to give
        assert "answer" not in run.input_schema["properties"]
        assert "confirm" not in run.input_schema["properties"]
        control = next(t for t in tools if t.name == "control")
        assert control.input_schema["properties"]["action"]["enum"] == [
            "pause",
            "resume",
            "kill",
            "cancel",
        ]


async def test_a_read_gives_what_the_api_gives(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id = await pushed(cp, local, fake, flagged())
    async with cp.client(cp.operator) as api, client(cp, cp.operator) as c:
        page = data(await c.call_tool("runs", {"limit": 5}))
        assert [r["run_id"] for r in page["items"]] == [run_id]
        shown = data(await c.call_tool("run_show", {"run_id": run_id}))
        assert shown == (await api.get(f"/api/runs/{run_id}")).json()
        found = data(await c.call_tool("flags", {"run_id": run_id, "min_importance": 4}))
        want = await api.get(f"/api/runs/{run_id}/flags", params={"min_importance": 4})
        assert found == want.json()
        assert found["items"]
        steps = data(await c.call_tool("steps", {"run_id": run_id}))
        assert steps == (await api.get(f"/api/runs/{run_id}/steps", params={"limit": 20})).json()
        events = data(await c.call_tool("events", {"run_id": run_id, "types": ["monitor.flag"]}))
        assert {e["type"] for e in events["items"]} == {"monitor.flag"}
        one = data(await c.call_tool("event_show", {"run_id": run_id, "seq": 0}))
        assert one["type"] == "run.started"
        verified = data(await c.call_tool("verify", {"run_id": run_id}))
        assert verified["ok"]
        report = text(await c.call_tool("report", {"run_id": run_id}))
        assert report == (await api.get(f"/api/runs/{run_id}/report?format=md")).text
        listed = data(await c.call_tool("scenarios", {}))
        assert {(s["source"], s["name"]) for s in listed} >= {("project", "control")}
        shown = data(await c.call_tool("scenario_show", {"scenario": "project/control"}))
        assert shown["truth"]
        checked = data(await c.call_tool("scenario_check", {"scenario": "project/control"}))
        assert checked["ok"]
        body = {"scenario": "project/control", "monitor": "signals"}
        cost = data(await c.call_tool("estimate", body))
        assert cost["lines"][0].startswith("Cost estimate for control")
        for name in ("models_list", "prompts_show", "spend", "rates", "scenario_schema"):
            assert data(await c.call_tool(name, {})), name
        assert data(await c.call_tool("control_list", {"run_id": run_id})) == []


async def test_a_viewer_reads_no_plants_and_no_scores(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id = await pushed(cp, local, fake, flagged())
    async with client(cp, cp.viewer) as c:
        said = error(await c.call_tool("check", {"run_id": run_id}))
        assert said.startswith("403 Forbidden: this needs the operator role")
        shown = data(await c.call_tool("scenario_show", {"scenario": "project/control"}))
        assert (shown["truth"], shown["plants"]) == (False, None)
        assert error(await c.call_tool("rates", {})).startswith("403 Forbidden")


async def test_a_problem_comes_back_as_a_tool_error_in_the_api_words(cp: CP) -> None:
    async with client(cp, cp.operator) as c:
        said = error(await c.call_tool("run_show", {"run_id": "nope"}))
        assert said == "404 Not found: not a run id: nope"
        said = error(await c.call_tool("estimate", {"scenario": "control"}))
        assert said.startswith("name a scenario as source/name, such as builtin/cache-keys")
        said = error(await c.call_tool("scenario_check", {"scenario": "project/nope"}))
        assert said.startswith("404 Not found")
        assert (await c.call_tool("search", {"q": "ab"})).is_error
    async with Api("http://127.0.0.1:9", cp.operator) as api, Client(build(api)) as c:
        said = error(await c.call_tool("runs", {}))
        assert said.startswith("cannot reach the control plane at http://127.0.0.1:9")


async def test_check_lists_the_plants_taken(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await pushed(cp, local, fake, flagged())
    async with client(cp, cp.operator) as c:
        said = error(await c.call_tool("check", {"run_id": run_id}))
        assert said == f"409 Cannot check: run {run_id} has no truth file"

        async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
            return [took(2, (2,))]

        monkeypatch.setattr(routes, "check_run", check)
        rows = data(await c.call_tool("check", {"run_id": run_id}))
        assert rows == [took(2, (2,)).as_dict()]


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_run_under_the_threshold_starts_without_a_question(cp: CP, mode: str) -> None:
    person = Person(NO)
    async with client(cp, cp.operator, act=True, person=person, mode=mode) as c:
        found = data(
            await c.call_tool("run", {"scenario": "project/control", "monitor": "signals"})
        )
    assert person.asked == []
    assert found["started"]
    (run_id,) = found["run_ids"]
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert job.payload["options"]["score"]


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_costly_run_starts_only_after_the_persons_yes(cp: CP, mode: str) -> None:
    body = {"scenario": "project/control", "monitor": "signals", "samples": COSTLY}
    no = Person(NO)
    async with client(cp, cp.operator, act=True, person=no, mode=mode) as c:
        found = data(await c.call_tool("run", body))
    assert found == {"started": False, "reason": "the person did not confirm the cost"}
    (question,) = no.asked
    assert question.startswith("Cost estimate for control, 5 runs, at list prices.\n")
    assert question.endswith(", above confirm_above_usd $20.00. Start anyway?")
    assert await cp.state.db.jobs() == []

    yes = Person(YES)
    async with client(cp, cp.operator, act=True, person=yes, mode=mode) as c:
        found = data(await c.call_tool("run", body))
    assert yes.asked == no.asked
    assert len(found["run_ids"]) == COSTLY
    assert len(await cp.state.db.jobs()) == COSTLY


async def test_a_client_that_cannot_ask_gets_the_estimate_and_no_run(cp: CP) -> None:
    body = {"scenario": "project/control", "monitor": "signals", "samples": COSTLY}
    async with client(cp, cp.operator, act=True) as c:
        said = error(await c.call_tool("run", body))
    assert said.startswith("Cost estimate for control, 5 runs, at list prices.\n")
    assert "This MCP client cannot ask the person, so nothing started." in said
    assert await cp.state.db.jobs() == []


async def test_a_viewer_token_cannot_act(cp: CP) -> None:
    async with client(cp, cp.viewer, act=True, person=Person(YES)) as c:
        result = await c.call_tool("run", {"scenario": "project/control", "monitor": "signals"})
    assert error(result).startswith("403 Forbidden: this needs the operator role")
    assert await cp.state.db.jobs() == []


@pytest.fixture
async def reviewer(cp: CP) -> AsyncIterator[FakeRunner]:
    runner = MonitorRunner(cp, "reviewer")
    await runner.connect()
    yield runner
    await runner.close()


@pytest.mark.usefixtures("reviewer")
async def test_fork_and_monitor_ask_before_they_spend(cp: CP, local: LocalEventStore) -> None:
    origin = await push_forged(cp, local, "http://model.invalid/v1")
    person = Person(NO)
    async with client(cp, cp.operator, act=True, person=person) as c:
        body = {"run_id": origin, "samples": COSTLY, "monitor": "signals"}
        found = data(await c.call_tool("fork", body))
        assert found == {"started": False, "reason": "the person did not confirm the cost"}
        (question,) = person.asked
        assert "\nA fork after step 0: the agents pay only for the steps after it;" in question
        assert await cp.state.db.jobs() == []
        found = data(await c.call_tool("fork", {"run_id": origin, "monitor": "signals"}))
        (fork,) = found["run_ids"]
        found = data(await c.call_tool("monitor", {"run_id": origin, "preset": "signals"}))
        (review,) = found["run_ids"]
        result = await c.call_tool("monitor", {"run_id": origin, "preset": "standard"})
        assert error(result).startswith("422 Cannot estimate: no profile has the sweep role")
    assert len(person.asked) == 1
    jobs = {j.run_id: j.payload for j in await cp.state.db.jobs()}
    assert set(jobs) == {fork, review}
    assert jobs[review]["origin"] == origin


async def test_control_cancels_a_queued_run_and_waits_for_a_runners_answer(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    async with client(cp, cp.operator, act=True, person=Person(YES)) as c:
        started = data(await c.call_tool("run", {"scenario": "project/control", "monitor": "off"}))
        (queued,) = started["run_ids"]
        cancelled = data(await c.call_tool("control", {"run_id": queued, "action": "cancel"}))
        assert cancelled["state"] == "cancelled"

        run_id, task = await live(cp, local, fake, [call(1)])

        async def answer() -> None:
            command = await fake.next(CommandMessage)
            await fake.client.ack(command.id, Ack(ok=True, outcome="done"))

        answering = asyncio.create_task(answer())
        paused = data(await c.call_tool("control", {"run_id": run_id, "action": "pause"}))
        await answering
        assert (paused["action"], paused["state"], paused["by"]) == ("pause", "done", "op")
        result = await c.call_tool("control", {"run_id": run_id, "action": "kill", "agent": "a"})
        assert error(result) == "a kill stops the whole run; leave out agent"
        await seal(local, run_id, task)


async def test_resume_leaves_a_monitors_pause_to_the_person(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    pause = Draft(
        MonitorAction(action="pause", agents=["a"], flags=[1], mode="human", reason="flagged")
    )
    run_id, task = await live(cp, local, fake, [call(1), pause])
    async with cp.client(cp.operator) as api:
        held = (await api.get(f"/api/runs/{run_id}")).json()["run"]["held"]
        assert [(h["agent"], h["by"]) for h in held] == [("a", "monitor")]
    async with client(cp, cp.operator, act=True) as c:
        for agent in (None, "a"):
            args = {"run_id": run_id, "action": "resume"} | ({"agent": agent} if agent else {})
            said = error(await c.call_tool("control", args))
            assert said.startswith("agent a waits on the monitor's pause.")
    async with cp.client(cp.operator) as api:
        assert (await api.get(f"/api/runs/{run_id}/commands")).json() == []
    await seal(local, run_id, task)


def test_the_command_says_how_to_add_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in sys.modules if n.partition(".")[0] in ("mcp", "mcp_types")]:
        monkeypatch.setitem(sys.modules, name, None)
    for name in ("aims.mcp.server", "aims.mcp.client"):
        monkeypatch.delitem(sys.modules, name)
    r = CliRunner().invoke(app, ["mcp", "--server", "http://127.0.0.1:9"], env={"AIMS_TOKEN": "t"})
    assert r.exit_code == 1
    assert "aims mcp needs the mcp extra: uv tool install 'aims-bench[mcp]'" in r.output


async def test_aims_mcp_serves_over_stdio(cp: CP, local: LocalEventStore, fake: FakeRunner) -> None:
    run_id = await pushed(cp, local, fake, flagged())
    env = {**os.environ, "AIMS_TOKEN": cp.viewer, "NO_PROXY": "*"}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "aims", "mcp", "--server", cp.url], env=env
    )
    async with Client(params) as c:
        assert {t.name for t in (await c.list_tools()).tools} == READS
        page = data(await c.call_tool("runs", {}))
        assert [r["run_id"] for r in page["items"]] == [run_id]
        said = error(await c.call_tool("check", {"run_id": run_id}))
        assert said.startswith("403 Forbidden: this needs the operator role")
