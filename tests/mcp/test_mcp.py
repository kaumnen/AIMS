"""`aims mcp`: the tools an MCP client gets over the control plane's API, what a viewer and an
operator see, the cost question before runs that spend and the server over stdio."""

import asyncio
import contextlib
import os
import re
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from mcp import Client, StdioServerParameters
from mcp import types as mt
from test_monitor_api import MonitorRunner, call, flagged, live, pushed, seal, started
from test_score_api import ScoreRunner
from test_scoring import took, words
from test_server import MODEL_KEYS, FakeRunner, push_forged, ship
from typer.testing import CliRunner

from aims.cli import app
from aims.events import Draft, LocalEventStore, RunFinished
from aims.mcp import server as mcp_server
from aims.mcp.client import Api
from aims.mcp.server import build
from aims.report import texts as report_texts
from aims.run import truth
from aims.server import api as routes
from aims.wire import Ack, CommandMessage, OfferMessage, ScoreJob

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


def unbuilt(report: str) -> str:
    """The Markdown report with the time on its Built line masked. Each read builds the report
    anew, so two reads can differ in that time when a second passes between them."""
    label = re.escape(report_texts.PIN_LABELS["built"])
    when = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC"
    masked, n = re.subn(rf"^(- \*\*{label}:\*\* .* at ){when}$", r"\1<time>", report, flags=re.M)
    assert n == 1, report
    return masked


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
        score = next(t for t in tools if t.name == "score")
        assert {"confirmed", "confirm"}.isdisjoint(score.input_schema["properties"])
        assert score.input_schema["properties"]["answer"]["default"] is True
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
        md = (await api.get(f"/api/runs/{run_id}/report?format=md")).text
        assert unbuilt(report) == unbuilt(md)
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
        found = data(await c.call_tool("check", {"run_id": run_id}))
        assert found == {"plants": [took(2, (2,)).as_dict()], "gaps": []}


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
    run_id, task = await live(cp, local, fake, [call(1)])
    refused = "agent a waits on the monitor's pause"

    async def answer() -> CommandMessage:
        command = await fake.next(CommandMessage)
        await fake.client.ack(command.id, Ack(ok=False, outcome=refused))
        return command

    async with client(cp, cp.operator, act=True) as c:
        answering = asyncio.create_task(answer())
        args = {"run_id": run_id, "action": "resume", "agent": "a"}
        said = data(await c.call_tool("control", args))
        command = await answering
        assert (command.command.agent, command.command.operator_only) == ("a", True)
        assert (said["action"], said["state"]) == ("resume", "failed")
        assert said["outcome"] == f"failed: {refused}"
    await seal(local, run_id, task)


# A classifier on other weights than the agent's, priced so a score of a run with words could
# pass $100: the score's spend cap bounds it.
CLASSIFIER = """
[profiles.classify]
extends = "scripted"
weights = "w-classify"
api_key_env = "MODEL_API_KEY"
max_output_tokens = 100000

[profiles.classify.price]
input = 1000.0
cached = 100.0
output = 1000.0

[monitor]
extract = ["classify"]
"""
RECORD: dict[str, Any] = {"roles": {"a": "subject"}, "plants": {"p1": {}}, "variant": None}


@pytest.fixture
async def scorer(cp: CP) -> AsyncIterator[FakeRunner]:
    cp.project.write_text(cp.project.read_text() + CLASSIFIER)
    runner = ScoreRunner(cp, "scorer", keys=MODEL_KEYS)
    await runner.connect()
    yield runner
    await runner.close()


async def with_words(
    cp: CP, local: LocalEventStore, runner: FakeRunner, run_id: str | None = None
) -> str:
    """A sealed run of the control scenario whose agent said three things, with a truth file,
    shipped by `runner`."""
    first, blobs = started(cp)
    first = first.model_copy(update={"truth": truth.digest(RECORD)})
    run_id = await local.create_run(first, blobs=blobs, run_id=run_id)
    truth.write(local.root / run_id, RECORD)
    await local.append(run_id, [*await words(local, 3), Draft(RunFinished(status="completed"))])
    await ship(local, runner.client, run_id)
    return run_id


async def score_jobs(cp: CP) -> list[ScoreJob]:
    rows = await cp.state.db.jobs_page(before=None, limit=50, kind="score")
    return [ScoreJob.model_validate(j.payload) for j in rows]


async def test_a_costly_score_starts_only_after_the_persons_yes(
    cp: CP, local: LocalEventStore, scorer: FakeRunner
) -> None:
    run_id = await with_words(cp, local, scorer)
    costly = {"run_id": run_id, "budget_usd": 50}
    no = Person(NO)
    async with client(cp, cp.operator, act=True, person=no) as c:
        found = data(await c.call_tool("score", costly))
    assert found == {"started": False, "reason": "the person did not confirm the cost"}
    (question,) = no.asked
    assert question == (
        "Score: checks on classify, up to 1 call. No past scores to go by. Stops at $50.00, its"
        " spend cap.\nThe caps allow $50.00, above confirm_above_usd $20.00. Start anyway?"
    )
    assert await score_jobs(cp) == []

    yes = Person(YES)
    async with client(cp, cp.operator, act=True, person=yes) as c:
        found = data(await c.call_tool("score", costly))
        # under the config's spend cap of $1 nothing asks
        cheap = data(await c.call_tool("score", {"run_id": run_id}))
    assert yes.asked == no.asked
    assert found["started"]
    assert cheap["started"]
    jobs = {j.run_id: j for j in await score_jobs(cp)}
    (asked,) = found["run_ids"]
    (unasked,) = cheap["run_ids"]
    assert (jobs[asked].budget_usd, jobs[unasked].budget_usd) == (50, 1.0)
    profile = jobs[asked].profile()
    assert profile is not None
    assert profile.name == "classify"


async def test_a_client_that_cannot_ask_starts_no_costly_score(
    cp: CP, local: LocalEventStore, scorer: FakeRunner
) -> None:
    run_id = await with_words(cp, local, scorer)
    async with client(cp, cp.operator, act=True) as c:
        said = error(await c.call_tool("score", {"run_id": run_id, "budget_usd": 50}))
    assert said.startswith("Score: checks on classify")
    assert "This MCP client cannot ask the person, so nothing started." in said
    assert await score_jobs(cp) == []


async def test_a_score_whose_cost_changed_since_its_estimate_does_not_start(
    cp: CP, local: LocalEventStore, scorer: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await with_words(cp, local, scorer)
    # the estimate the tool read needed no yes; the control plane decides again as it starts
    monkeypatch.setattr(mcp_server, "why_confirm", lambda found, kind="run": None)
    async with client(cp, cp.operator, act=True, person=Person(YES)) as c:
        said = error(await c.call_tool("score", {"run_id": run_id, "budget_usd": 50}))
    assert said.startswith("409 Confirm the cost: the caps allow $50.00")
    assert "The cost changed since the estimate. Call the tool again to see it." in said
    assert await score_jobs(cp) == []


async def test_a_run_counts_its_score_so_the_score_after_it_asks_nothing(
    cp: CP, local: LocalEventStore, scorer: FakeRunner
) -> None:
    person = Person(NO)
    async with client(cp, cp.operator, act=True, person=person) as c:
        body = {"scenario": "project/control", "monitor": "signals"}
        cost = data(await c.call_tool("estimate", body))
        bare = data(await c.call_tool("estimate", {**body, "score": False}))
        found = data(await c.call_tool("run", body))
        costly = data(await c.call_tool("run", {**body, "samples": COSTLY}))
    # the run's estimate counts its score at the score's spend cap
    assert cost["parts"][0]["score"]["ceiling_usd"] == 1.0
    assert bare["parts"][0]["score"] is None
    assert cost["total"]["ceiling_usd"] == pytest.approx(bare["total"]["ceiling_usd"] + 1.0)
    line = "Score: checks on classify. No past scores to go by. Stops at $1.00, its spend cap."
    assert line in cost["lines"]
    assert costly == {"started": False, "reason": "the person did not confirm the cost"}
    (question,) = person.asked
    assert f"\n  {line}\n" in question
    (run_id,) = found["run_ids"]
    row = await cp.state.db.job(run_id)
    assert row is not None
    assert row.payload["options"]["score"] is True
    # the runner runs it; its truth file reaches the control plane as it ships
    offer = await scorer.next(OfferMessage)
    assert offer.run_id == run_id
    assert await scorer.client.claim(run_id) is not None
    await with_words(cp, local, scorer, run_id=run_id)
    # the control plane queues the score the run's estimate counted, with no question
    (job,) = await score_jobs(cp)
    assert (job.origin, job.awareness, job.answer, job.budget_usd) == (run_id, True, True, 1.0)
    assert job.classifier is not None
    assert len(person.asked) == 1


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
