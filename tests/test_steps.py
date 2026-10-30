import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane

from aims import __version__
from aims.events import (
    AgentFinished,
    AgentStep,
    Draft,
    ExecFinished,
    ExecStarted,
    FileAccess,
    FsSnapshot,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    RunStarted,
    TaskScored,
    ToolCall,
    ToolResult,
    split_lines,
)
from aims.events.schema import BlobRef
from aims.events.store import LOG
from aims.node.client import ControlClient
from aims.wire import RunnerHello, TracerHealth

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="demo", oversight="autonomous", agents=["a1", "a2"])
BODY = b"{}"
BODY_REF = BlobRef(
    sha256="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", size=2
)
ANSWER = b'{"answer": "' + b"y" * 9000 + b'"}'


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path / "cp") as cp:
        yield cp


def request(call: str, agent: str, attempt: int = 1, role: Any = None) -> Draft:
    payload = ModelRequest(
        call_id=call,
        attempt=attempt,
        profile="p",
        backend="chat",
        model="m",
        url="http://model",
        params=ModelParams(stream=False),
        body=BODY_REF,
        key=BODY_REF.sha256,
        role=role,
    )
    return Draft(payload, agent)


def response(call: str, agent: str, cost: float | None, **kw: Any) -> Draft:
    fields: dict[str, Any] = {"attempt": 1, "status": "completed", "latency_ms": 1, **kw}
    return Draft(ModelResponse(call_id=call, cost_usd=cost, **fields), agent)


def tool(call: str, model_call: str, agent: str, name: str, arguments: Any) -> Draft:
    return Draft(
        ToolCall(call_id=call, name=name, arguments=arguments, model_call=model_call), agent
    )


def step(n: int, call: str, agent: str, tools: list[str], cost: float | None) -> Draft:
    return Draft(
        AgentStep(n=n, call_id=call, status="completed", tool_calls=tools, cost_usd=cost), agent
    )


def started(exec_id: str, call: str, agent: str) -> Draft:
    payload = ExecStarted(
        exec_id=exec_id,
        call_id=call,
        command="true",
        wrapper=[],
        user="agent",
        cwd="/work",
        timeout_ms=1000,
        audit="kernel",
    )
    return Draft(payload, agent)


def finished(exec_id: str, call: str, agent: str, code: int, ms: int = 5) -> Draft:
    payload = ExecFinished(
        exec_id=exec_id,
        call_id=call,
        end="exited",
        exit_code=code,
        pid=1,
        duration_ms=ms,
        stdout="",
        stderr="",
        stdout_bytes=0,
        stderr_bytes=0,
        truncated=False,
    )
    return Draft(payload, agent)


def access(exec_id: str, op: Any, path: str) -> Draft:
    payload = FileAccess(exec_id=exec_id, pid=2, ts_kernel=1, cpu=0, cpu_seq=1, op=op, path=path)
    return Draft(payload, "a1")


def snapshot(exec_id: str, total: int) -> Draft:
    payload = FsSnapshot(
        exec_id=exec_id,
        roots=["/work"],
        base=None,
        digest="d",
        entries=total,
        files=total,
        bytes=0,
        total=total,
        changes=[],
        hashed_bytes=0,
        unhashed=0,
        content_skipped=0,
        truncated=False,
        duration_ms=1,
    )
    return Draft(payload, "a1")


def story(blob: BlobRef) -> list[Draft]:
    """Seqs 1 to 35 of a run: two agents whose events interleave and whose provider reuses the
    tool call id t1. a1 retries its first model call, runs one call with two execs and submits
    blob arguments; a2's only call has run its exec but has no result yet. A judge call, a
    replayed answer and two verifier rewards come last."""
    return [
        request("c1", "a1"),
        response("c1", "a1", None, status="error", error="boom", retry=True),
        request("c1", "a1", attempt=2),
        request("d1", "a2"),
        response("c1", "a1", 0.01, attempt=2),
        tool("t1", "c1", "a1", "bash", '{"command": "ls"}'),
        tool("t2", "c1", "a1", "edit", '{"command": "create", "file_text": "' + "x" * 400 + '"}'),
        step(1, "c1", "a1", ["t1", "t2"], 0.01),
        started("e1", "t1", "a1"),
        access("e1", "read", "/etc/x"),
        access("e1", "read", "/etc/y"),
        access("e1", "write", "/work/out"),
        response("d1", "a2", 0.02),
        tool("t1", "d1", "a2", "bash", '{"command": "pwd"}'),
        step(1, "d1", "a2", ["t1"], 0.02),
        snapshot("e1", 2),
        finished("e1", "t1", "a1", 1),
        Draft(ToolResult(call_id="t1", output="exit code: 1", error=True), "a1"),
        started("e2", "t2", "a1"),
        finished("e2", "t2", "a1", 0),
        started("e3", "t2", "a1"),
        finished("e3", "t2", "a1", 0, ms=7),
        Draft(ToolResult(call_id="t2", output="ok"), "a1"),
        started("e4", "t1", "a2"),
        finished("e4", "t1", "a2", 0),
        request("c2", "a1"),
        response("c2", "a1", 0.03),
        tool("t3", "c2", "a1", "submit", blob),
        step(2, "c2", "a1", ["t3"], 0.03),
        Draft(ToolResult(call_id="t3", output="submitted"), "a1"),
        Draft(AgentFinished(reason="submitted", steps=2), "a1"),
        request("j1", "a1", role="judge"),
        response("j1", "a1", 0.04),
        response("r1", "a2", 0.5, source="replay"),
        Draft(TaskScored(reward=0.5)),
        Draft(TaskScored(reward=0.75)),
    ]


async def told(store: LocalEventStore, *, batch: int = 100) -> str:
    run_id = await store.create_run(STARTED)
    assert await store.put_blob(run_id, BODY) == BODY_REF
    blob = await store.put_blob(run_id, ANSWER)
    drafts = story(blob)
    for i in range(0, len(drafts), batch):
        await store.append(run_id, drafts[i : i + batch])
    return run_id


def runs_table(root: Path) -> list[tuple[object, ...]]:
    db = sqlite3.connect(root / "index.sqlite")
    try:
        return db.execute("select * from runs order by run_id").fetchall()
    finally:
        db.close()


async def test_a_run_counts_its_steps_cost_and_last_reward(store: LocalEventStore) -> None:
    run_id = await told(store)
    info = await store.run(run_id)
    assert info is not None
    assert (info.steps, info.cost_usd, info.reward, info.reviews) == (3, 0.1, 0.75, 0)

    quiet = await store.create_run(STARTED)
    info = await store.run(quiet)
    assert info is not None
    assert (info.steps, info.cost_usd, info.reward) == (0, None, None)


async def test_any_batching_and_a_rebuild_give_the_same_totals(
    store: LocalEventStore, tmp_path: Path
) -> None:
    run_id = await told(store, batch=1)
    before = runs_table(store.root)
    await store.rebuild()
    assert runs_table(store.root) == before
    lines = split_lines((store.root / run_id / LOG).read_bytes())
    for name, size in (("whole", len(lines)), ("single", 1), ("threes", 3)):
        async with await LocalEventStore.open(tmp_path / name, ingest_only=True) as copy:
            await copy.ingest(run_id, lines[:1])
            await copy.put_blobs(run_id, [BODY, ANSWER])
            for i in range(1, len(lines), size):
                await copy.ingest(run_id, lines[i : i + size])
            assert runs_table(copy.root) == before, name


async def test_steps_carry_their_tool_calls_execs_and_results(store: LocalEventStore) -> None:
    run_id = await told(store)
    first, other, last = await store.steps(run_id)

    assert (first.agent_id, first.n, first.seq, first.seq_from, first.seq_to) == ("a1", 1, 8, 1, 23)
    assert first.cost_usd == 0.01
    ls, create = first.tools
    assert (ls.name, ls.args, ls.args_cut, ls.execs) == ("bash", {"command": "ls"}, False, ["e1"])
    assert (ls.exit_code, ls.end, ls.done, ls.error) == (1, "exited", True, True)
    assert create.execs == ["e2", "e3"]
    assert (create.exit_code, create.duration_ms, create.error) == (0, 12, False)
    assert create.args == {"command": "create", "file_text": "x" * 300}
    assert create.args_cut
    assert create.args_bytes == len('{"command": "create", "file_text": ""}') + 400

    assert (other.agent_id, other.seq, other.seq_from, other.seq_to) == ("a2", 15, 4, 25)
    (pwd,) = other.tools
    assert (pwd.call_id, pwd.args, pwd.execs, pwd.exit_code) == (
        "t1",
        {"command": "pwd"},
        ["e4"],
        0,
    )
    assert not pwd.done

    assert (last.n, last.seq_from, last.seq_to, last.ts_to > last.ts_from) == (2, 26, 30, True)
    (submit,) = last.tools
    assert (submit.name, submit.args, submit.args_bytes, submit.done) == (
        "submit",
        None,
        9000 + len('{"answer": ""}'),
        True,
    )


async def test_step_pages_match_the_whole_list(store: LocalEventStore) -> None:
    run_id = await told(store)
    whole = await store.steps(run_id)
    paged, cursor = [], None
    while page := await store.steps(run_id, after=cursor, limit=1):
        paged += page
        cursor = page[-1].seq
    assert paged == whole
    assert await store.steps(run_id, agent_id="a2") == [whole[1]]
    assert await store.steps(run_id, agent_id="a1", after=8) == [whole[2]]
    assert await store.steps(run_id, after=29) == []


async def test_activity_counts_an_execs_records(store: LocalEventStore) -> None:
    run_id = await told(store)
    e1, e3 = await store.activity(run_id, ["e1", "nope", "e3"])
    assert (e1.exec_id, e1.events, e1.changes) == ("e1", 6, 2)
    assert e1.types == {
        "exec.started": 1,
        "exec.finished": 1,
        "file.access": 3,
        "fs.snapshot": 1,
    }
    assert e1.file_ops == {"read": 2, "write": 1}
    assert (e3.exec_id, e3.events, e3.file_ops, e3.changes) == ("e3", 2, {}, 0)
    assert await store.activity(run_id, []) == []

    seqs = [e.seq for e in await store.events(run_id, execs=["e1"])]
    assert seqs == [9, 10, 11, 12, 16, 17]
    assert [e.seq for e in await store.events(run_id, execs=["e2", "e4"])] == [19, 20, 24, 25]
    assert [e.seq for e in await store.events(run_id, execs=["e1"], types=["file.access"])] == [
        10,
        11,
        12,
    ]


async def test_runs_filter_by_scenario_and_count_their_reviews(store: LocalEventStore) -> None:
    watched = await store.create_run(STARTED)
    other = await store.create_run(RunStarted(scenario="other", oversight="observe", agents=[]))
    for kind in ("monitor", "monitor", "score"):
        await store.create_run(
            RunStarted(
                scenario="demo",
                oversight="observe",
                kind=kind,
                origin=watched,
                origin_head="0" * 64,
            )
        )
    found = {r.run_id: r for r in await store.runs(scenario="demo", kind="scenario")}
    assert list(found) == [watched]
    assert found[watched].reviews == 2
    assert [r.run_id for r in await store.runs(scenario="other")] == [other]
    assert await store.runs(scenario="none") == []


async def test_the_api_serves_steps_activity_and_exec_events(
    cp: CP, store: LocalEventStore
) -> None:
    run_id = await told(store)
    lines = split_lines((store.root / run_id / LOG).read_bytes())
    await cp.state.store.ingest(run_id, lines[:1])
    await cp.state.store.put_blobs(run_id, [BODY, ANSWER])
    await cp.state.store.ingest(run_id, lines[1:])
    runner = ControlClient(cp.url, cp.runner, runner="r")
    try:
        await runner.hello(
            RunnerHello(
                name="r",
                version=__version__,
                arch="arm64",
                tracer=TracerHealth(alive=True),
                slots=1,
            )
        )
    finally:
        await runner.aclose()
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={"scenario": {"source": "project", "name": "control"}, "monitor": "signals"},
        )
        assert r.status_code == 202, r.text
        (queued,) = r.json()["run_ids"]

        async def listed(**params: Any) -> list[str]:
            r = await c.get("/api/runs", params=params)
            assert r.status_code == 200, r.text
            return [x["run_id"] for x in r.json()["items"]]

        assert await listed(scenario="demo") == [run_id]
        assert await listed(scenario="control") == [queued]
        assert await listed(scenario="none") == []
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["steps"], run["cost_usd"], run["reward"], run["reviews"]) == (3, 0.1, 0.75, 0)

        page = (await c.get(f"/api/runs/{run_id}/steps", params={"limit": 2})).json()
        assert [(s["agent_id"], s["n"]) for s in page["items"]] == [("a1", 1), ("a2", 1)]
        assert page["next"] == 15
        rest = (await c.get(f"/api/runs/{run_id}/steps", params={"after": 15})).json()
        assert ([s["seq"] for s in rest["items"]], rest["next"]) == ([29], None)
        a2 = (await c.get(f"/api/runs/{run_id}/steps", params={"agent": "a2"})).json()
        assert a2["items"][0]["tools"][0]["execs"] == ["e4"]

        r = await c.get(f"/api/runs/{run_id}/activity", params={"exec": ["e1", "nope", "e3"]})
        assert [(a["exec_id"], a["events"]) for a in r.json()] == [("e1", 6), ("e3", 2)]
        r = await c.get(
            f"/api/runs/{run_id}/activity", params={"exec": [f"x{i}" for i in range(101)]}
        )
        assert r.status_code == 422
        r = await c.get(f"/api/runs/{run_id}/activity")
        assert r.status_code == 422

        r = await c.get(f"/api/runs/{run_id}/events", params={"exec": ["e2", "e4"]})
        assert [e["seq"] for e in r.json()["items"]] == [19, 20, 24, 25]
        r = await c.get(f"/api/runs/{run_id}/events", params={"exec": "e1", "limit": 2})
        assert ([e["seq"] for e in r.json()["items"]], r.json()["next"]) == ([9, 10], "10")
