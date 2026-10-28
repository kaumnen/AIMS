"""The control plane and a runner over loopback HTTP, with real sandboxes and scripted models."""

import asyncio
import contextlib
import inspect
import os
import shutil
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane, start, stop
from scripted import Script, call, models, profile

from aims.events import LocalEventStore, OperatorAction
from aims.node.client import ControlClient
from aims.node.link import Link
from aims.node.service import Node, NodeOptions
from aims.run.control import ControlGate
from aims.run.runner import Bench, Options, Runner
from aims.sandbox import DockerBackend
from aims.scenario import load
from aims.server.app import create_app

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

WORD_COUNT = Path(__file__).parent.parent / "scenarios" / "word-count"


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
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@contextlib.asynccontextmanager
async def running_node(
    cp: CP, backend: DockerBackend, data: Path, script: Script | None, name: str = "n1"
) -> AsyncGenerator[Node]:
    stop_event = asyncio.Event()
    node = Node(
        NodeOptions(
            url=cp.url,
            token=cp.runner,
            name=name,
            data=data,
            slots=2,
            model_transport=None if script is None else script.transport(),
            backend=backend,
        )
    )
    task = asyncio.create_task(node.run(stop_event))
    async with asyncio.timeout(30):
        await node.ready.wait()
        await node.ctx.link.connected.wait()
    await until(lambda: name in cp.state.runners and cp.state.runners[name].status is not None)
    try:
        yield node
    finally:
        stop_event.set()
        async with asyncio.timeout(180):
            await task


async def until(test: Callable[[], Any], within: float = 30) -> None:
    """Poll `test()`, awaiting it when it returns an awaitable, until it is true."""
    async with asyncio.timeout(within):
        while True:
            value = test()
            if inspect.isawaitable(value):
                value = await value
            if value:
                return
            await asyncio.sleep(0.1)


async def post(cp: CP, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    async with cp.client(cp.operator) as c:
        r = await c.post(path, json=body or {})
        assert r.status_code in (200, 201, 202), r.text
        return r.json()


async def start_control(cp: CP) -> list[str]:
    body = {"scenario": {"source": "project", "name": "control"}, "monitor": "signals"}
    return (await post(cp, "/api/runs", body))["run_ids"]


async def run_status(cp: CP, run_id: str) -> dict[str, Any]:
    async with cp.client(cp.viewer) as c:
        return (await c.get(f"/api/runs/{run_id}")).json()["run"]


async def until_status(cp: CP, run_id: str, *wanted: str, within: float = 180) -> dict[str, Any]:
    async with asyncio.timeout(within):
        while True:
            run = await run_status(cp, run_id)
            if run["status"] in wanted:
                return run
            await asyncio.sleep(0.2)


async def until_event(cp: CP, run_id: str, kind: str, within: float = 120) -> None:
    async with asyncio.timeout(within), cp.client(cp.viewer) as c:
        while True:
            r = await c.get(f"/api/runs/{run_id}/events", params={"types": kind, "limit": 1})
            if r.status_code == 200 and r.json()["items"]:
                return
            await asyncio.sleep(0.2)


def same_log(cp: CP, node: Node, run_id: str) -> bool:
    ours = node.options.data / "runs" / run_id / "events.jsonl"
    theirs = cp.state.store.root / run_id / "events.jsonl"
    return ours.read_bytes() == theirs.read_bytes()


async def test_a_run_started_from_the_api_runs_on_the_runner_and_ships(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    script = Script(call("bash", command="echo hello"), call("submit", answer="done"))
    async with running_node(cp, backend, tmp_path / "node", script) as node:
        (run_id,) = await start_control(cp)
        run = await until_status(cp, run_id, "completed", "failed")
        assert run["status"] == "completed", run
        assert run["runner"] == "n1"
        await until(lambda: same_log(cp, node, run_id), 10)
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert (job.state, job.reason) == ("done", "completed")
    assert (await cp.state.store.verify(run_id)).ok
    async with cp.client(cp.viewer) as c:
        scored = (await c.get(f"/api/runs/{run_id}/events", params={"types": "task.scored"})).json()
        assert scored["items"][0]["data"]["reward"] == 1.0


async def test_kill_pause_and_inject_through_the_api(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    script = Script(call("bash", command="sleep 60", timeout_s=120), call("submit", answer="x"))
    async with running_node(cp, backend, tmp_path / "node", script) as node:
        (run_id,) = await start_control(cp)
        await until_event(cp, run_id, "exec.started")
        await asyncio.sleep(0.5)
        sent = await post(cp, f"/api/runs/{run_id}/kill", {"reason": "seen enough"})
        assert sent["delivered"]
        run = await until_status(cp, run_id, "killed", "completed", "failed")
        assert run["status"] == "killed"
        await until(lambda: same_log(cp, node, run_id), 10)
        async with cp.client(cp.viewer) as c:
            params = {"types": "operator.action"}
            events = (await c.get(f"/api/runs/{run_id}/events", params=params)).json()
            (action,) = events["items"]
            assert (action["data"]["action"], action["data"]["token"]) == ("kill", "op")
            commands = (await c.get(f"/api/runs/{run_id}/commands")).json()
            assert [x["outcome"] for x in commands] == ["done"]

        script.turns[:] = [call("submit", answer="after the hint")]
        (run_id,) = await start_control(cp)
        await until_event(cp, run_id, "run.started")
        await post(cp, f"/api/runs/{run_id}/pause", {"reason": "hold"})

        def held() -> bool:
            runner = node.ctx.link.runners.get(run_id)
            control = None if runner is None else runner.control
            return control is not None and control.is_held("a")

        await until(held, 60)
        assert len(script.requests) == 1, "the held agent made no model call"
        await post(cp, f"/api/runs/{run_id}/resume", {"then": "inject", "text": "use the hint"})
        run = await until_status(cp, run_id, "completed", "failed", "killed")
        assert run["status"] == "completed", run
    sent_messages = [m["content"] for m in script.requests[-1]["messages"]]
    assert "use the hint" in sent_messages


async def test_runs_catch_up_after_the_control_plane_was_down(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    script = Script(
        call("bash", command="sleep 4; echo slow"),
        call("bash", command="echo more"),
        call("submit", answer="done"),
    )
    async with running_node(cp, backend, tmp_path / "node", script) as node:
        (run_id,) = await start_control(cp)
        await until_event(cp, run_id, "exec.started")
        port = int(cp.url.rsplit(":", 1)[1])
        await stop(cp.server, cp.task)
        await asyncio.sleep(6)
        local = await node.ctx.store.run(run_id)
        assert local is not None
        cp.server, cp.task, _ = await start(create_app(cp.state, ui=None), port)
        run = await until_status(cp, run_id, "completed", "failed", within=120)
        assert run["status"] == "completed", run
        await until(lambda: same_log(cp, node, run_id), 20)
    assert (await cp.state.store.verify(run_id)).ok


async def test_aims_run_ships_and_takes_commands(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    script = Script(call("bash", command="sleep 60", timeout_s=120), call("submit", answer="x"))
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        bench = Bench(
            store, backend, models(profile()), transport=script.transport(), gate=ControlGate()
        )
        link = Link(ControlClient(cp.url, cp.runner, runner="run-test"), store, bench.bus)
        await link.start()
        runner = Runner(
            bench, load(cp.project.parent / "scenarios/control/scenario.yaml"), Options()
        )
        link.track(runner)
        task = asyncio.create_task(runner.run())
        await until(lambda: runner.run_id is not None, 60)
        run_id = runner.run_id
        assert run_id is not None
        await until(lambda: cp.state.store.run(run_id), 60)
        await until_event(cp, run_id, "exec.started")
        await post(cp, f"/api/runs/{run_id}/kill", {"reason": "from the ui"})
        result = await task
        assert (result.status, result.reason) == ("killed", "from the ui")
        assert await link.drain(30)
        await link.close()
        actions = await store.events(run_id, types=["operator.action"])
        payload = actions[0].payload()
        assert isinstance(payload, OperatorAction)
        assert payload.token == "op"
    run = await run_status(cp, run_id)
    assert (run["status"], run["runner"]) == ("killed", "run-test")


async def test_a_runner_forks_a_run_it_does_not_hold(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    script = Script(call("bash", command="echo one"), call("submit", answer="first"))
    async with running_node(cp, backend, tmp_path / "a", script, name="a"):
        (origin,) = await start_control(cp)
        assert (await until_status(cp, origin, "completed", "failed"))["status"] == "completed"
    live = Script(call("submit", answer="forked"))
    async with running_node(cp, backend, tmp_path / "b", live, name="b") as b:
        started = await post(
            cp, f"/api/runs/{origin}/forks", {"step": 1, "runner": "b", "monitor": "signals"}
        )
        (fork,) = started["run_ids"]
        run = await until_status(cp, fork, "completed", "failed")
        assert run["status"] == "completed", run
        assert (b.options.data / "origins" / origin / "events.jsonl").is_file()
        assert not (b.options.data / "runs" / origin).exists()
    assert len(live.requests) == 1
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{fork}/events", params={"types": "model.response"})
        sources = [e["data"]["source"] for e in r.json()["items"]]
        assert sources == ["replay", "live"]


@pytest.mark.skipif(not os.environ.get("AIMS_LIVE_MODELS"), reason="set AIMS_LIVE_MODELS")
async def test_a_live_run_through_the_control_plane(
    cp: CP, backend: DockerBackend, tmp_path: Path
) -> None:
    """The word-count scenario on the default agent profile, started from the API."""
    shutil.copytree(WORD_COUNT, cp.project.parent / "scenarios" / "word-count")
    settings = cp.state.config.settings()
    agent = "meta-spark-1.3-contributor"
    settings.models.get(agent)
    async with running_node(cp, backend, tmp_path / "node", None):
        body = {
            "scenario": {"source": "project", "name": "word-count"},
            "profile": agent,
            "monitor": "signals",
        }
        (run_id,) = (await post(cp, "/api/runs", body))["run_ids"]
        run = await until_status(cp, run_id, "completed", "failed", "killed", within=900)
    assert run["status"] == "completed", run
    async with cp.client(cp.viewer) as c:
        scored = (await c.get(f"/api/runs/{run_id}/events", params={"types": "task.scored"})).json()
    assert scored["items"][0]["data"]["reward"] == 1.0
    assert (await cp.state.store.verify(run_id)).ok
