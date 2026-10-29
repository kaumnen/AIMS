"""Forks of runs of several agents: where a cut falls, the rebuild that runs every agent's
replayed calls in the origin's order, the board rebuilt by its posts, agents live after the
cut or kept on their recorded answers, channel divergence, and folder sources in a fork."""

import itertools
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from scripted import Script, call, models, profile

from aims.agent import ChannelVersions, Turnstile
from aims.events import Event, LocalEventStore, RunStarted
from aims.monitor.render import Trajectory
from aims.monitor.texts import resolve
from aims.prompts import Prompts
from aims.run.fork import (
    CutAt,
    cut_at,
    fork_profiles,
    fork_schedules,
    load_origin,
    planner,
    preview,
)
from aims.run.job import monitor_for
from aims.run.runner import Bench, Fork, Options, RunError, Runner
from aims.sandbox import DockerBackend
from aims.scenario import load

pytestmark = pytest.mark.anyio

IMAGE = "python:3.13-slim"
SCENARIO = f"""\
version: 1
name: pair
oversight: observe
task:
  instruction: work
  environment: {{image: {IMAGE}}}
agents:
  - id: a
    task: {{instruction: "You are a."}}
  - id: b
    task: {{instruction: "You are b."}}
    start: {{agent: a, on: step, step: 3}}
channels:
  - {{name: chat, kind: board, members: [a, b]}}
  - name: notes
    kind: folder
    path: /shared
    members: [a, b]
    files:
      - {{path: seed.txt, source: seed.txt}}
"""

Turn = Any


def router(plays: dict[str, list[Turn]]) -> Callable[[dict[str, Any]], Turn]:
    """A model that answers each agent from its own list, told apart by its instruction."""

    def answer(body: dict[str, Any]) -> Turn:
        text = json.dumps(body["messages"])
        for agent, turns in plays.items():
            if f"You are {agent}." in text:
                turn = turns.pop(0)
                return turn(body) if callable(turn) else turn
        raise AssertionError("an unknown agent asked")

    return answer


def script(plays: dict[str, list[Turn]], start: int = 1) -> Script:
    s = Script(*[router(plays)] * sum(len(t) for t in plays.values()))
    s.calls = itertools.count(start)
    return s


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


def scenario(tmp_path: Path, text: str = SCENARIO) -> Path:
    root = tmp_path / "pair"
    root.mkdir(exist_ok=True)
    (root / "seed.txt").write_text("seeded\n")
    path = root / "scenario.yaml"
    path.write_text(text)
    return path


ORIGIN: dict[str, list[Turn]] = {
    "a": [
        call("post_message", text="the plan is in /shared/plan.txt"),
        call("bash", command="echo plan > /shared/plan.txt"),
        call("bash", command="cat /shared/seed.txt"),
        call("submit", answer="a done"),
    ],
    "b": [
        call("read_messages"),
        call("bash", command="cat /shared/plan.txt"),
        call("post_message", text="b read it"),
        call("submit", answer="b done"),
    ],
}


async def origin_run(store: LocalEventStore, backend: DockerBackend, tmp_path: Path) -> str:
    plays = {a: list(t) for a, t in ORIGIN.items()}
    bench = Bench(store, backend, models(profile()), transport=script(plays).transport())
    result = await Runner(bench, load(scenario(tmp_path)), Options()).run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    return result.run_id


async def fork_run(
    store: LocalEventStore,
    backend: DockerBackend,
    origin_id: str,
    at: str,
    plays: dict[str, list[Turn]],
    *,
    keep_others: bool = False,
    strict: bool = False,
    tamper: Callable[[Any], None] | None = None,
) -> tuple[Any, list[Event]]:
    origin = await load_origin(store, origin_id)
    if tamper is not None:
        tamper(origin)
    found = cut_at(origin, at)
    assert isinstance(found, CutAt)
    fork = Fork(
        origin=origin_id,
        step=min(found.steps.values()),
        steps=dict(found.steps),
        cut=found.seq,
        agent=found.agent,
        keep_others=keep_others,
        strict=strict,
    )
    bench = Bench(store, backend, models(profile()), transport=script(plays, 100).transport())
    assert origin.full_bundle is not None
    options = Options(fork=fork, seed=origin.seed, inherited=origin.inherited)
    runner = Runner(
        bench,
        origin.full_bundle,
        options,
        profiles=fork_profiles(bench, origin, fork),
        schedules=fork_schedules(origin, fork),
        texts=origin.texts,
        replay=planner(bench, origin, fork),
    )
    result = await runner.run()
    assert result.run_id is not None
    return result, await store.events(result.run_id, limit=10_000)


def calls_in_order(events: list[Event], *, replayed: bool | None = None) -> list[tuple[str, str]]:
    return [
        (e.agent_id or "", str(e.data["call_id"]))
        for e in events
        if e.type == "tool.result" and (replayed is None or e.data.get("replayed") == replayed)
    ]


# The turnstile and the cut.


async def test_the_turnstile_lets_actions_through_in_order() -> None:
    import asyncio

    gate = Turnstile([3, 1, 2])
    seen: list[int] = []

    async def act(seq: int) -> None:
        await gate.wait(seq)
        seen.append(seq)
        await gate.done(seq)

    await asyncio.gather(act(3), act(2), act(1))
    assert seen == [1, 2, 3]

    gate = Turnstile([1, 2, 3])
    late = asyncio.create_task(act(3))
    await gate.drop([1, 2])
    await asyncio.wait_for(late, 1)
    await asyncio.wait_for(gate.wait(99), 1)


@pytest.mark.docker
async def test_a_cut_names_an_agent_step_or_an_event(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    origin_id = await origin_run(store, backend, tmp_path)
    origin = await load_origin(store, origin_id)
    with pytest.raises(RunError, match="agent:N"):
        cut_at(origin, "1")
    with pytest.raises(RunError, match="took 4 steps"):
        cut_at(origin, "b:5")
    with pytest.raises(RunError, match="bad cut"):
        cut_at(origin, "nobody:1")
    at = cut_at(origin, "b:2")
    assert isinstance(at, CutAt)
    assert at.agent == "b"
    assert at.steps["b"] == 2
    assert at.seq == origin.step_seqs["b"][1]
    assert at.steps["a"] == sum(1 for s in origin.step_seqs["a"] if s <= at.seq)
    whole = cut_at(origin, f"@{origin.last_seq}")
    assert isinstance(whole, CutAt)
    assert whole.steps == {"a": 4, "b": 4}
    start = cut_at(origin, "a:0")
    assert isinstance(start, CutAt)
    assert start.steps == {"a": 0, "b": 0}
    lines = preview(origin, Fork(origin=origin_id, step=0, steps=at.steps, cut=at.seq, agent="b"))
    assert lines[0].startswith(f"cut of {origin_id} after event {at.seq}")
    assert "  b: replays 2 of 4 steps, goes live after" in lines


# The rebuild.


@pytest.mark.docker
async def test_a_fork_rebuilds_every_agent_in_the_origin_order(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    origin_id = await origin_run(store, backend, tmp_path)
    origin_events = await store.events(origin_id, limit=10_000)
    plays = {
        "a": [call("submit", answer="a forked")] * 3,
        "b": [call("bash", command="echo fork > /shared/fork.txt"), call("submit", answer="b")],
    }
    result, events = await fork_run(store, backend, origin_id, "b:2", plays)
    assert result.status == "completed", result.reason
    started = RunStarted.model_validate(events[0].data)
    assert started.cut is not None
    assert (started.cut.origin, started.cut.agent, started.cut.steps["b"]) == (origin_id, "b", 2)

    replayed = calls_in_order(events, replayed=True)
    seqs = {
        (e.agent_id or "", str(e.data["call_id"])): e.seq
        for e in origin_events
        if e.type == "tool.call"
    }
    assert replayed == sorted(replayed, key=lambda key: seqs[key])
    assert ("b", next(c for a, c in replayed if a == "b")) in seqs
    assert not [e for e in events if e.type == "replay.divergence"]

    posts = [(e.agent_id, e.data["id"]) for e in events if e.type == "channel.message"]
    assert posts[0] == ("a", 1)
    commands = [
        str(e.data.get("command")) for e in events if e.type == "exec.started" and e.agent_id == "b"
    ]
    assert any("echo fork" in c for c in commands)
    steps = [e for e in events if e.type == "agent.step" and e.agent_id == "b"]
    assert [e.data["source"] for e in steps[:2]] == ["replay", "replay"]
    assert steps[2].data["source"] == "live"


@pytest.mark.docker
async def test_kept_agents_answer_from_the_recording_until_their_inputs_change(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    text = SCENARIO.replace("    start: {agent: a, on: step, step: 3}\n", "")
    plays = {
        "a": [call("bash", command="echo a"), call("submit", answer="a done")],
        "b": [call("bash", command="echo b"), call("submit", answer="b done")],
    }
    bench = Bench(store, backend, models(profile()), transport=script(plays).transport())
    origin = await Runner(bench, load(scenario(tmp_path, text)), Options()).run()
    assert origin.status == "completed", origin.reason
    assert origin.run_id is not None
    result, events = await fork_run(
        store,
        backend,
        origin.run_id,
        "b:1",
        {"b": [call("submit", answer="b forked")]},
        keep_others=True,
    )
    assert result.status == "completed", result.reason
    a_steps = [e.data["source"] for e in events if e.type == "agent.step" and e.agent_id == "a"]
    assert a_steps
    assert set(a_steps) == {"replay"}
    assert result.agents["a"].reason == "submitted"
    assert result.agents["b"].reason == "submitted"


@pytest.mark.docker
async def test_a_replayed_read_at_another_version_is_a_channel_divergence(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    origin_id = await origin_run(store, backend, tmp_path)

    def tamper(origin: Any) -> None:
        for call_id, v in list(origin.versions.items()):
            if v.reads:
                origin.versions[call_id] = ChannelVersions(
                    seq=v.seq, reads=dict.fromkeys(v.reads, 99), writes=v.writes
                )

    plays = {"a": [call("submit", answer="a")] * 3, "b": [call("submit", answer="b")] * 3}
    result, events = await fork_run(
        store, backend, origin_id, "b:2", plays, strict=True, tamper=tamper
    )
    found = [e for e in events if e.type == "replay.divergence" and e.data.get("kind") == "channel"]
    assert found
    d = found[0].data
    assert (d["channel"], d["recorded_version"]) == ("chat", "99")
    assert d["live_version"] != "99"
    assert result.agents["b"].reason == "divergence"


@pytest.mark.docker
async def test_a_fork_seeds_folders_from_the_recorded_sources(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    origin_id = await origin_run(store, backend, tmp_path)
    (tmp_path / "pair" / "seed.txt").unlink()
    plays = {
        "a": [call("bash", command="cat /shared/seed.txt")]
        + [call("bash", command="true")] * 2
        + [call("submit", answer="a")],
        "b": [call("submit", answer="b")],
    }
    result, events = await fork_run(store, backend, origin_id, "@0", plays)
    assert result.status == "completed", result.reason
    reads = [
        e
        for e in events
        if e.type == "tool.result" and e.agent_id == "a" and "seeded" in json.dumps(e.data)
    ]
    assert reads


CONFEDERATE = f"""\
version: 1
name: trio
oversight: observe
task:
  instruction: work
  environment: {{image: {IMAGE}}}
agents:
  - id: a
    task: {{instruction: "You are a."}}
    start: {{agent: c, on: finished}}
  - id: c
    role: confederate
    script:
      - {{post: {{channel: chat, text: "hello from c"}}}}
      - {{run: "echo c"}}
channels:
  - {{name: chat, kind: board, members: [a, c]}}
"""


@pytest.mark.docker
async def test_a_confederates_tool_call_is_a_step_it_takes_again(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    text = CONFEDERATE.replace(
        '      - {run: "echo c"}', '      - {call: {tool: bash, arguments: {command: "echo c"}}}'
    )
    plays = {"a": [call("read_messages"), call("submit", answer="a done")]}
    bench = Bench(store, backend, models(profile()), transport=script(plays).transport())
    origin = await Runner(bench, load(scenario(tmp_path, text)), Options()).run()
    assert origin.status == "completed", origin.reason
    assert origin.run_id is not None
    loaded = await load_origin(store, origin.run_id)
    (made,) = await store.events(origin.run_id, types=["tool.call"], agent_id="c", limit=10)
    assert loaded.actions["c"][1] == made.seq
    at = cut_at(loaded, "a:1")
    assert isinstance(at, CutAt)
    assert at.steps["c"] == 2
    result, events = await fork_run(
        store, backend, origin.run_id, "a:1", {"a": [call("submit", answer="forked")]}
    )
    assert result.status == "completed", result.reason
    assert not [e for e in events if e.type == "replay.divergence"]
    calls = [e for e in events if e.type == "tool.call" and e.agent_id == "c"]
    assert [c.data["name"] for c in calls] == ["bash"]
    assert calls[0].data.get("model_call") is None


@pytest.mark.docker
async def test_a_monitor_takes_in_a_confederates_tool_calls(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    text = CONFEDERATE.replace(
        '      - {post: {channel: chat, text: "hello from c"}}',
        '      - {call: {tool: post_message, arguments: {text: "hello from c"}}}',
    ).replace(
        '      - {run: "echo c"}', '      - {call: {tool: bash, arguments: {command: "echo c"}}}'
    )
    bundle = load(scenario(tmp_path, text))
    agent = profile()
    watched = models(agent)
    watch = monitor_for(watched, Prompts(), bundle.scenario, [agent], preset="signals")
    plays = {"a": [call("read_messages"), call("submit", answer="a done")]}
    bench = Bench(store, backend, watched, transport=script(plays).transport())
    result = await Runner(bench, bundle, Options(), monitor=watch).run()
    assert result.status == "completed", result.reason
    assert result.run_id is not None
    events = await store.events(result.run_id, limit=10_000)
    assert not [e for e in events if e.type == "monitor.degraded"]
    calls = [e for e in events if e.type == "tool.call" and e.agent_id == "c"]
    assert [c.data["name"] for c in calls] == ["post_message", "bash"]
    (post,) = [e for e in events if e.type == "channel.message" and e.agent_id == "c"]
    assert post.data["call_id"] == calls[0].data["call_id"]
    trajectory = Trajectory(result.run_id, "c", store, b"k" * 32)
    for e in events:
        trajectory.add(e)
    rendered = await trajectory.render(["evidence", "output"], texts=resolve(Prompts()))
    assert "hello from c" in rendered.text
    assert "echo c" in rendered.text


@pytest.mark.docker
async def test_a_confederate_takes_its_steps_again_before_the_cut(
    store: LocalEventStore, backend: DockerBackend, tmp_path: Path
) -> None:
    plays = {"a": [call("read_messages"), call("submit", answer="a done")]}
    bench = Bench(store, backend, models(profile()), transport=script(plays).transport())
    origin = await Runner(bench, load(scenario(tmp_path, CONFEDERATE)), Options()).run()
    assert origin.status == "completed", origin.reason
    assert origin.run_id is not None
    loaded = await load_origin(store, origin.run_id)
    at = cut_at(loaded, "a:1")
    assert isinstance(at, CutAt)
    assert at.steps["c"] == 2
    result, events = await fork_run(
        store, backend, origin.run_id, "a:1", {"a": [call("submit", answer="forked")]}
    )
    assert result.status == "completed", result.reason
    assert not [e for e in events if e.type == "replay.divergence"]
    posts = [e for e in events if e.type == "channel.message" and e.agent_id == "c"]
    assert len(posts) == 1
    (read,) = [e for e in events if e.type == "tool.result" and e.agent_id == "a"][:1]
    assert "hello from c" in json.dumps(read.data)
    assert read.seq > posts[0].seq
    assert result.agents["a"].answer is not None


# Series and the command line.


async def test_a_series_goes_on_after_its_last_completed_run(store: LocalEventStore) -> None:
    from aims.cli import last_of_series
    from aims.events import Draft, RunFinished, RunStatus, Series

    async def made(index: int, status: RunStatus) -> str:
        started = RunStarted(
            scenario="s", oversight="observe", series=Series(name="nightly", index=index)
        )
        run_id = await store.create_run(started)
        await store.append(run_id, [Draft(RunFinished(status=status))])
        return run_id

    assert await last_of_series(store, "nightly") is None
    await made(0, "completed")
    second = await made(1, "completed")
    await made(2, "failed")
    found = await last_of_series(store, "nightly")
    assert found is not None
    last, start = found
    assert (last.run_id, last.series_index, start) == (second, 1, 3)


def test_the_command_line_checks_the_fork_and_series_flags(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from aims.cli import app

    cli = CliRunner()

    def said(*args: str) -> str:
        out = cli.invoke(app, ["run", *args], env={"COLUMNS": "200"})
        assert out.exit_code != 0
        text = re.sub(r"\x1b\[[0-9;]*m", "", out.output)
        return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())

    assert "agent:N" in said("--replay", "x", "--keep-others")
    assert "needs --replay" in said("--keep-others", "--fork-at", "a:1", str(tmp_path / "s.yaml"))
    assert "needs --series" in said("--series-restart", str(tmp_path / "s.yaml"))


async def test_the_api_takes_a_cut_and_checks_it(tmp_path: Path) -> None:
    from cp import control_plane
    from test_monitor_api import call as logged
    from test_monitor_api import started
    from test_server import FakeRunner, ship

    from aims.events import Draft, RunFinished

    async with (
        control_plane(tmp_path / "cp") as cp,
        await LocalEventStore.open(tmp_path / "local") as local,
    ):
        runner = FakeRunner(cp, "r")
        await runner.connect()
        try:
            first, blobs = started(cp)
            run_id = await local.create_run(first, blobs=blobs)
            await local.append(run_id, [logged(1), Draft(RunFinished(status="completed"))])
            await ship(local, runner.client, run_id)
            url = f"/api/runs/{run_id}/forks"
            async with cp.client(cp.operator) as c:
                r = await c.post(url, json={"at": "a:0", "step": 0})
                assert (r.status_code, "not both" in r.text) == (422, True)
                r = await c.post(url, json={"at": "@0", "keep_others": True})
                assert (r.status_code, "agent:N" in r.text) == (422, True)
                r = await c.post(url, json={"at": "nobody:1"})
                assert (r.status_code, r.json()["code"]) == (422, "fork")
                r = await c.post(url, json={"at": "a:0", "monitor": "signals"})
                assert r.status_code == 202, r.text
                (fork_id,) = r.json()["run_ids"]
                job = await cp.state.db.job(fork_id)
                assert job is not None
                fork = job.payload["fork"]
                assert (fork["agent"], fork["steps"], fork["keep_others"]) == ("a", {"a": 0}, False)
        finally:
            await runner.close()


async def test_a_call_is_held_only_to_its_own_writes_of_a_shared_folder(
    store: LocalEventStore,
) -> None:
    from typing import cast

    from aims.events import ChannelChange, ChannelSnapshot, Draft, ExecStarted, ToolCall
    from aims.run.fork import Origin, _order

    run_id = await store.create_run(RunStarted(scenario="s", oversight="observe"))
    started = ExecStarted(
        exec_id="e1",
        call_id="c1",
        command="echo",
        wrapper=[],
        user="1000",
        cwd="/work",
        timeout_ms=1000,
        audit="kernel",
    )

    def change(path: str, agent: str, sha: str) -> ChannelChange:
        return ChannelChange(
            path=path,
            change="added",
            kind="file",
            size=1,
            mode=0o644,
            mtime_us=0,
            sha256=sha,
            agent=agent,
        )

    snapshot = ChannelSnapshot(
        channel="notes",
        exec_id="e1",
        base=None,
        digest="d",
        entries=2,
        files=2,
        bytes=2,
        changes=[change("/shared/mine", "a", "s1"), change("/shared/theirs", "b", "s2")],
    )
    call_event = ToolCall(call_id="c1", name="bash", arguments="{}")
    await store.append(run_id, [Draft(call_event, "a"), Draft(started, "a"), Draft(snapshot)])
    origin = Origin(run_id, cast(Any, None), {}, {})
    await _order(store, run_id, origin)
    assert origin.versions["c1"].writes == {"/shared/mine": "s1"}
    assert origin.versions["c1"].channels == {"/shared/mine": "notes"}


def test_a_recorded_symlink_seeds_a_symlink(tmp_path: Path) -> None:
    import io
    import tarfile
    from dataclasses import dataclass
    from typing import cast

    from aims.channels import ChannelSetup, Folder
    from aims.scenario import parse

    @dataclass
    class Member:
        name: str
        kind: str
        mode: int = 0o644
        mtime: int = 0
        data: bytes = b""
        target: str = ""

    text = SCENARIO.replace("seed.txt, source: seed.txt", "seed, source: tree")
    scenario = parse(text)
    (config,) = [c for c in scenario.channels if c.name == "notes"]
    members = [
        Member("", "dir"),
        Member("a.txt", "file", data=b"x"),
        Member("l", "link", target="a.txt"),
    ]
    setup = ChannelSetup(
        config=config,
        scenario=scenario,
        run_id="r",
        store=cast(Any, None),
        sources=lambda rel: members,
    )
    data = Folder(setup)._seed()
    assert data is not None
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        kinds = {m.name: (m.type, m.linkname) for m in tar.getmembers()}
    assert kinds["seed/l"] == (tarfile.SYMTYPE, "a.txt")
    assert kinds["seed/a.txt"][0] == tarfile.REGTYPE
