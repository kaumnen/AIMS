"""A run starts its agents' sandboxes together. A start that fails or is cancelled still
closes every sandbox that came up."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import Script, call, models, profile
from test_audit import FakeSandbox

from aims.events import LocalEventStore, RunFinished
from aims.run.runner import Bench, Options, Runner
from aims.sandbox import AuditedSandbox, DockerBackend, SandboxBackend, SandboxError
from aims.sandbox.base import SandboxSpec
from aims.scenario import Bundle, load

pytestmark = pytest.mark.anyio

SCENARIO = """\
version: 1
name: start
oversight: observe
task:
  instruction: x
  environment: {image: python:3.13-slim}
agents:
  - id: a
  - id: b
  - id: c
"""
WAIT_S = 0.5


class Starts:
    """Makes fake sandboxes. Each create waits up to WAIT_S for every create to begin, then
    finishes in reverse agent order. `fail` raises, `hold` waits until cancelled and then takes
    until `release` to clean up."""

    name = "fake"

    def __init__(self, *, fail: str | None = None, hold: str | None = None) -> None:
        self.fail = fail
        self.hold = hold
        self.begun: list[str] = []
        self.all_begun = asyncio.Event()
        self.together = 0
        self.made: dict[str, FakeSandbox] = {}
        self.on_made: Callable[[], None] | None = None
        self.holding = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned: list[str] = []

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> FakeSandbox:
        self.begun.append(agent_id)
        if len(self.begun) == 3:
            self.all_begun.set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(WAIT_S):
                await self.all_begun.wait()
        self.together += self.all_begun.is_set()
        if agent_id == self.fail:
            raise RuntimeError("no room")
        if agent_id == self.hold:
            self.holding.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleaning.set()
                await self.release.wait()
                self.cleaned.append(agent_id)
                raise
        await asyncio.sleep(0.02 * (3 - self.begun.index(agent_id)))
        self.made[agent_id] = made = FakeSandbox(kernel=False)
        if self.on_made is not None:
            self.on_made()
        return made


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def written(tmp_path: Path, text: str) -> Bundle:
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    return load(path)


def runner(
    store: LocalEventStore, starts: Starts, tmp_path: Path, script: Script | None = None
) -> Runner:
    transport = (script or Script()).transport()
    bench = Bench(store, cast("SandboxBackend", starts), models(profile()), transport=transport)
    return Runner(bench, written(tmp_path, SCENARIO), Options())


async def stopped(store: LocalEventStore, run_id: str | None) -> list[str | None]:
    assert run_id is not None
    return [e.agent_id for e in await store.events(run_id, types=["sandbox.stopped"])]


async def test_the_sandboxes_start_together_and_close_in_agent_order(
    store: LocalEventStore, tmp_path: Path
) -> None:
    starts = Starts()
    script = Script(*[call("submit", answer="done")] * 3)
    result = await runner(store, starts, tmp_path, script).run()

    assert starts.together == 3
    assert result.status == "completed", result.reason
    assert list(starts.made) == ["c", "b", "a"]
    assert all(sb.closed for sb in starts.made.values())
    assert await stopped(store, result.run_id) == ["a", "b", "c"]


async def test_a_sandbox_that_fails_to_start_fails_the_run_and_closes_the_others(
    store: LocalEventStore, tmp_path: Path
) -> None:
    starts = Starts(fail="b")
    result = await runner(store, starts, tmp_path).run()

    assert result.status == "failed"
    assert result.reason == "sandbox for agent b did not start: no room"
    assert set(starts.made) == {"a", "c"}
    assert all(sb.closed for sb in starts.made.values())
    assert await stopped(store, result.run_id) == ["a", "c"]


async def test_a_cancel_during_the_start_waits_for_every_start_and_closes_them(
    store: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C twice while a sandbox starts: its own cleanup runs to the end and the sandboxes
    that came up are killed and closed."""
    up: set[str] = set()
    start = AuditedSandbox.start

    async def counted(*args: Any, agent_id: str, **kwargs: Any) -> AuditedSandbox:
        sb = await start(*args, agent_id=agent_id, **kwargs)
        up.add(agent_id)
        return sb

    monkeypatch.setattr(AuditedSandbox, "start", counted)
    starts = Starts(hold="b")
    run = runner(store, starts, tmp_path)
    task = asyncio.create_task(run.run())
    await starts.holding.wait()
    async with asyncio.timeout(5):
        while up != {"a", "c"}:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    task.cancel()
    await starts.cleaning.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    starts.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert starts.cleaned == ["b"]
    assert set(starts.made) == {"a", "c"}
    assert all(sb.killed and sb.closed for sb in starts.made.values())
    assert run.run_id is not None
    assert await stopped(store, run.run_id) == ["a", "c"]
    (sealed,) = await store.events(run.run_id, types=["run.finished"])
    assert cast("RunFinished", sealed.payload()).status == "cancelled"


async def test_an_operator_cancel_during_the_start_ends_it_and_closes_what_came_up(
    store: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI's Cancel while a sandbox starts: the start ends without waiting for it, its
    cleanup runs, and the sandboxes that came up are closed. The run returns as cancelled."""
    up: set[str] = set()
    start = AuditedSandbox.start

    async def counted(*args: Any, agent_id: str, **kwargs: Any) -> AuditedSandbox:
        sb = await start(*args, agent_id=agent_id, **kwargs)
        up.add(agent_id)
        return sb

    monkeypatch.setattr(AuditedSandbox, "start", counted)
    starts = Starts(hold="b")
    run = runner(store, starts, tmp_path)
    task = asyncio.create_task(run.run())
    async with asyncio.timeout(5):
        await starts.holding.wait()
        while up != {"a", "c"}:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        run.cancel()
        await starts.cleaning.wait()
        starts.release.set()
        result = await task

    assert (result.status, result.reason) == ("cancelled", "cancelled by the operator")
    assert starts.cleaned == ["b"]
    assert set(starts.made) == {"a", "c"}
    assert all(sb.killed and sb.closed for sb in starts.made.values())
    assert await stopped(store, result.run_id) == ["a", "c"]
    assert result.run_id is not None
    (sealed,) = await store.events(result.run_id, types=["run.finished"])
    assert cast("RunFinished", sealed.payload()).status == "cancelled"


FOLDER = """\
channels:
  - name: notes
    kind: folder
    path: /shared
"""


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


@pytest.mark.docker
async def test_sandboxes_that_share_a_folder_fill_its_volume_once(
    backend: DockerBackend,
    store: LocalEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filled: list[str] = []
    fill = DockerBackend._prepare

    async def prepare(self: DockerBackend, image: str, volumes: Any) -> None:
        filled.extend(name for name, _ in volumes)
        await fill(self, image, volumes)

    monkeypatch.setattr(DockerBackend, "_prepare", prepare)
    script = Script(*[call("submit", answer="done")] * 3)
    bench = Bench(store, backend, models(profile()), transport=script.transport())
    result = await Runner(bench, written(tmp_path, SCENARIO + FOLDER), Options()).run()

    assert result.status == "completed", result.reason
    run_id = result.run_id
    assert sorted(filled) == sorted(
        f"aims-{run_id}-{name}" for name in ("a-work", "b-work", "c-work", "notes-folder")
    )


@pytest.mark.docker
async def test_a_sandbox_that_fails_to_start_leaves_no_container(
    backend: DockerBackend,
    store: LocalEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = DockerBackend._reader

    async def failing(self: DockerBackend, name: str, labels: dict[str, str], *args: Any) -> Any:
        if labels["aims.agent"] == "b":
            raise SandboxError("no room")
        return await reader(self, name, labels, *args)

    monkeypatch.setattr(DockerBackend, "_reader", failing)
    bench = Bench(store, backend, models(profile()), transport=Script().transport())
    result = await Runner(bench, written(tmp_path, SCENARIO), Options()).run()

    assert result.status == "failed"
    assert result.reason == "sandbox for agent b did not start: no room"
    assert await stopped(store, result.run_id) == ["a", "c"]
    query = json.dumps({"label": [f"aims.run={result.run_id}"]})
    assert not await backend.docker.containers.list(all=True, filters=query)
