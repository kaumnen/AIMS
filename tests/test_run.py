import io
import os
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from scripted import Script, call, models, profile
from test_agent import Gate

from aims.agent import Verdict
from aims.events import (
    AgentFinished,
    Event,
    ExecStarted,
    LocalEventStore,
    ModelResponse,
    RunFinished,
    SandboxArchived,
    TaskScored,
    ToolResult,
)
from aims.run.fork import (
    check_fork,
    fork_profiles,
    fork_schedules,
    fork_step,
    load_origin,
    planner,
)
from aims.run.runner import Bench, Fork, Options, RunError, Runner
from aims.sandbox import DockerBackend
from aims.scenario import load
from aims.scenario.budget import parse as parse_budget

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

SCENARIO = (
    Path(__file__).parent.parent / "src" / "aims" / "scenarios" / "word-count" / "scenario.yaml"
)
FIX_LOWER = call(
    "edit",
    command="str_replace",
    path="wordcount.py",
    old_str='re.findall(r"\\w+", text)',
    new_str='re.findall(r"\\w+", text.lower())',
)
FIX_SORT = call(
    "edit",
    command="str_replace",
    path="/work/wordcount.py",
    old_str="key=lambda item: item[1], reverse=True",
    new_str="key=lambda item: (-item[1], item[0])",
)


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


async def events(store: LocalEventStore, run_id: str, *types: str) -> list[Event]:
    return await store.events(run_id, limit=1000, types=types)


async def payloads[T](store: LocalEventStore, run_id: str, kind: type[T], name: str) -> list[T]:
    return [
        p for p in (e.payload() for e in await events(store, run_id, name)) if isinstance(p, kind)
    ]


async def test_a_scripted_agent_fixes_the_task_and_is_scored(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    script = Script(
        call("bash", command="cat wordcount.py"),
        FIX_LOWER,
        FIX_SORT,
        call("bash", command="python -m pytest -q -p no:cacheprovider"),
        call("submit", answer="lowercased words and sorted ties by word"),
    )
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    result = await Runner(bench, load(SCENARIO), Options()).run()

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    run_id = result.run_id
    finished = result.agents["agent"]
    assert (finished.reason, finished.steps) == ("submitted", 5)
    score = result.scores["agent"]
    assert (score.reward, score.source) == (1.0, "reward.json")
    assert score.metrics == {"reward": 1.0, "passed": 6, "total": 6}

    results = await payloads(store, run_id, ToolResult, "tool.result")
    assert "exit code: 0" in str(results[3].output)
    assert "6 passed" not in str(results[3].output)
    assert "3 passed" in str(results[3].output)
    started = await payloads(store, run_id, ExecStarted, "exec.started")
    phases = [s.phase for s in started]
    assert phases[0] == "setup"
    assert phases.count("verifier") == 4
    verifier = [s for s in started if s.phase == "verifier"]
    assert all(s.user == "1001:1000" for s in verifier)
    (archived,) = await payloads(store, run_id, SandboxArchived, "sandbox.archived")
    assert archived.archive is not None
    data = await store.get_blob(run_id, archived.archive.sha256)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        file = tar.extractfile("work/wordcount.py")
        assert file is not None
        assert b"text.lower()" in file.read()
    (sealed,) = await payloads(store, run_id, RunFinished, "run.finished")
    assert sealed.status == "completed"
    (scored,) = await payloads(store, run_id, TaskScored, "task.scored")
    assert scored.reward == 1.0
    assert (await store.verify(run_id)).ok

    # Fork after step 3: steps 1 to 3 come from the recording, step 4 on is live.
    origin = await load_origin(store, run_id)
    fork = Fork(origin=run_id, step=3)
    check_fork(origin, 3)
    live = Script(call("bash", command="echo forked"), call("submit", answer="forked"))
    bench = Bench(store, backend, models(profile()), transport=live.transport())
    forked = await Runner(
        bench,
        origin.bundle,
        Options(fork=fork),
        profiles=fork_profiles(bench.models, origin, fork),
        schedules=fork_schedules(origin, fork),
        texts=origin.texts,
        replay=planner(bench, origin, fork),
    ).run()

    assert forked.status == "completed", forked.reason
    assert forked.run_id is not None
    fid = forked.run_id
    assert forked.agents["agent"].steps == 5
    assert forked.scores["agent"].reward == 1.0
    responses = await payloads(store, fid, ModelResponse, "model.response")
    assert [r.source for r in responses] == ["replay"] * 3 + ["live"] * 2
    assert all(r.cost_usd == 0 for r in responses[:3])
    results = await payloads(store, fid, ToolResult, "tool.result")
    assert [r.replayed for r in results] == [True] * 3 + [False] * 2
    assert not await events(store, fid, "replay.divergence")
    started = await payloads(store, fid, ExecStarted, "exec.started")
    assert [s.phase for s in started if s.call_id is not None][:3] == ["replay"] * 3
    assert len(live.requests) == 2
    assert (await store.verify(fid)).ok

    finished_events = await payloads(store, fid, AgentFinished, "agent.finished")
    assert finished_events[0].reason == "submitted"


async def test_a_fork_of_a_killed_run_makes_the_killed_step_again_live(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    """The gate kills the agent before the tool call of its step 2 runs, so that step has no
    recorded output. A fork cuts after step 1 by default and asks the model for step 2 again."""
    gate = Gate({("before_tool", 2): Verdict("kill", reason="policy")})
    script = Script(call("bash", command="cat wordcount.py"), call("bash", command="rm -rf /work"))
    bench = Bench(store, backend, models(profile()), transport=script.transport(), gate=gate)
    result = await Runner(bench, load(SCENARIO), Options()).run()
    assert (result.status, result.agents["agent"].reason) == ("killed", "killed")
    assert result.run_id is not None

    origin = await load_origin(store, result.run_id)
    assert (origin.steps, origin.replayable) == ({"agent": 2}, {"agent": 1})
    with pytest.raises(RunError, match="fork at step 1 or before"):
        check_fork(origin, 2)
    fork = Fork(origin=result.run_id, step=fork_step(origin))
    check_fork(origin, fork.step)
    live = Script(call("bash", command="echo again"), call("submit", answer="forked"))
    bench = Bench(store, backend, models(profile()), transport=live.transport())
    forked = await Runner(
        bench,
        origin.bundle,
        Options(fork=fork),
        profiles=fork_profiles(bench.models, origin, fork),
        schedules=fork_schedules(origin, fork),
        texts=origin.texts,
        replay=planner(bench, origin, fork),
    ).run()

    assert forked.status == "completed", forked.reason
    assert forked.run_id is not None
    assert (forked.agents["agent"].reason, forked.agents["agent"].steps) == ("submitted", 3)
    responses = await payloads(store, forked.run_id, ModelResponse, "model.response")
    assert [r.source for r in responses] == ["replay", "live", "live"]
    results = await payloads(store, forked.run_id, ToolResult, "tool.result")
    assert [r.replayed for r in results] == [True, False, False]
    assert len(live.requests) == 2


async def test_setup_failure_fails_the_run_without_an_agent(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "version: 1\nname: broken\noversight: observe\ntask:\n  instruction: x\n"
        "  environment: {image: python:3.13-slim}\n  setup: ['echo nope >&2; exit 4']\n"
    )
    script = Script()
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    result = await Runner(bench, load(path), Options()).run()
    assert result.status == "failed"
    assert result.reason is not None
    assert "setup command 1" in result.reason
    assert "exit code 4" in result.reason
    assert "nope" in result.reason
    assert not script.requests
    assert result.run_id is not None
    assert not await events(store, result.run_id, "agent.started")
    assert (await store.verify(result.run_id)).ok


async def test_dev_shm_is_seeded_and_archived(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "version: 1\nname: shm\noversight: observe\ntask:\n  instruction: x\n"
        "  environment: {image: python:3.13-slim}\n"
        "  files: [{path: /dev/shm/seed/given.txt, content: given}]\n"
    )
    script = Script(call("bash", command="echo note > /dev/shm/note"), call("submit", answer="ok"))
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    result = await Runner(bench, load(path), Options()).run()

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    (archived,) = await payloads(store, result.run_id, SandboxArchived, "sandbox.archived")
    assert archived.paths == ["/work", "/tmp", "/home/agent", "/dev/shm"]
    assert archived.archive is not None
    data = await store.get_blob(result.run_id, archived.archive.sha256)
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for name in ("dev/shm/note", "dev/shm/seed/given.txt"):
            file = tar.extractfile(name)
            assert file is not None, name
            found[name] = file.read()
    assert found == {"dev/shm/note": b"note\n", "dev/shm/seed/given.txt": b"given"}


async def test_a_budget_override_caps_the_run_and_its_forks_keep_or_raise_it(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    script = Script(call("bash", command="true"), call("bash", command="true"))
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    options = Options(budget=parse_budget("steps=2, run.wall_s=600"))
    result = await Runner(bench, load(SCENARIO), options).run()

    assert result.status == "completed", result.reason
    assert result.run_id is not None
    run_id = result.run_id
    assert (result.agents["agent"].reason, result.agents["agent"].steps) == ("budget_steps", 2)
    (started,) = await events(store, run_id, "run.started")
    config: Any = started.data["config"]
    assert config["budget"] == {"every": {"steps": 2}, "run": {"wall_s": 600.0}}
    assert config["scenario"]["resolved"]["agents"][0]["budget"]["steps"] == 30
    (agent,) = await events(store, run_id, "agent.started")
    caps: Any = agent.data["budget"]
    assert (caps["steps"], caps["cost_usd"]) == (2, 0.5)

    origin = await load_origin(store, run_id)
    assert origin.budget is not None
    for top, steps in ((None, 2), (parse_budget("steps=3"), 3)):
        fork = Fork(origin=run_id, step=1)
        live = Script(*[call("bash", command="echo live")] * (steps - 1))
        bench = Bench(store, backend, models(profile()), transport=live.transport())
        forked = await Runner(
            bench,
            origin.bundle,
            Options(fork=fork, budget=origin.fork_budget(top)),
            profiles=fork_profiles(bench.models, origin, fork),
            schedules=fork_schedules(origin, fork),
            texts=origin.texts,
            replay=planner(bench, origin, fork),
        ).run()
        assert forked.status == "completed", forked.reason
        done = forked.agents["agent"]
        assert (done.reason, done.steps, len(live.requests)) == ("budget_steps", steps, steps - 1)


async def test_a_fork_replays_under_its_origin_s_caps_and_goes_live_under_its_own(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    """The origin is told of its budget at step 3 of 5; a fork with 10 steps replays that
    warning, so its replayed requests match, and warns again at its own threshold."""
    path = tmp_path / "warned.yaml"
    path.write_text(
        "version: 1\nname: warned\noversight: observe\ntask:\n  instruction: x\n"
        "  environment: {image: python:3.13-slim}\n"
        "agents:\n  - id: agent\n    budget: {steps: 5, warn_at: 0.6}\n"
    )
    script = Script(*[call("bash", command="true")] * 4, call("submit", answer="done"))
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    result = await Runner(bench, load(path), Options()).run()
    assert result.run_id is not None
    assert (result.agents["agent"].reason, result.agents["agent"].steps) == ("submitted", 5)

    origin = await load_origin(store, result.run_id)
    fork = Fork(origin=result.run_id, step=4)
    live = Script(*[call("bash", command="true")] * 4, call("submit", answer="later"))
    bench = Bench(store, backend, models(profile()), transport=live.transport())
    forked = await Runner(
        bench,
        origin.bundle,
        Options(fork=fork, budget=origin.fork_budget(parse_budget("steps=10"))),
        profiles=fork_profiles(bench.models, origin, fork),
        schedules=fork_schedules(origin, fork),
        texts=origin.texts,
        replay=planner(bench, origin, fork),
    ).run()
    assert forked.run_id is not None
    assert not await events(store, forked.run_id, "replay.divergence")
    done = forked.agents["agent"]
    assert (done.reason, done.steps, len(live.requests)) == ("submitted", 9, 5)
    told = [
        (e.data["step"], e.data["text"])
        for e in await events(store, forked.run_id, "agent.input")
        if e.data["kind"] == "budget_warning"
    ]
    assert told == [
        (3, "You have used 3 of your 5 steps."),
        (6, "You have used 6 of your 10 steps."),
    ]


FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "word-count"


async def test_the_recorded_live_run_replays(backend: DockerBackend, tmp_path: Path) -> None:
    """A real run on Spark 1.3 Contributor, replayed to its submit: every model answer comes
    from the recording and every tool call runs again here, so no key is needed."""
    import shutil

    (origin_dir,) = FIXTURE.iterdir()
    root = tmp_path / "runs"
    root.mkdir()
    shutil.copytree(origin_dir, root / origin_dir.name)
    async with await LocalEventStore.open(root) as store:
        origin = await load_origin(store, origin_dir.name)
        step = origin.steps["agent"]
        fork = Fork(origin=origin.run_id, step=step)
        check_fork(origin, step)
        script = Script()
        recorded = {p.name: p for s in origin.schedules.values() for _, p in s}
        config = models(profile(), *recorded.values())
        bench = Bench(store, backend, config, transport=script.transport())
        result = await Runner(
            bench,
            origin.bundle,
            Options(fork=fork),
            profiles=fork_profiles(bench.models, origin, fork),
            schedules=fork_schedules(origin, fork),
            texts=origin.texts,
            replay=planner(bench, origin, fork),
        ).run()

        assert result.status == "completed", result.reason
        assert result.run_id is not None
        assert result.agents["agent"].reason == "submitted"
        assert result.agents["agent"].steps == step
        assert result.scores["agent"].reward == 1.0
        assert not script.requests
        responses = await payloads(store, result.run_id, ModelResponse, "model.response")
        assert {r.source for r in responses} == {"replay"}
        assert {r.origin_run for r in responses} == {origin.run_id}
        assert (await store.verify(result.run_id)).ok
