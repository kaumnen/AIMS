"""`GET /api/runs/{run_id}/check`: the plants each agent of a sealed run took, as `aims check`
gives them, for operators only."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_monitor_api import call, live, pushed, seal
from test_scoring import took
from test_server import FakeRunner, ship
from test_truth_server import sealed

from aims.events import LocalEventStore
from aims.server import api

pytestmark = pytest.mark.anyio


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


async def test_a_sealed_run_with_its_truth_lists_what_its_agents_took(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await sealed(local)
    await ship(local, fake.client, run_id)
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{run_id}/check")
        assert (r.status_code, r.json()) == (200, [])

        async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
            assert store is cp.state.store
            return [took(2, (2, 5), detail="read it"), took(7, (7,), agent="b", inherited=True)]

        monkeypatch.setattr(api, "check_run", check)
        r = await c.get(f"/api/runs/{run_id}/check")
        assert r.status_code == 200, r.text
        assert r.json() == [
            took(2, (2, 5), detail="read it").as_dict(),
            took(7, (7,), agent="b", inherited=True).as_dict(),
        ]
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{run_id}/check")
        assert (r.status_code, r.json()["code"]) == (403, "role")


async def test_a_run_without_its_truth_or_not_sealed_cannot_be_checked(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    bare = await pushed(cp, local, fake, [call(1)])
    running, task = await live(cp, local, fake, [call(1)])
    async with cp.client(cp.operator) as c:
        r = await c.get(f"/api/runs/{bare}/check")
        assert (r.status_code, r.json()["code"]) == (409, "check")
        assert r.json()["detail"] == f"run {bare} has no truth file"
        r = await c.get(f"/api/runs/{running}/check")
        assert (r.status_code, r.json()["code"]) == (409, "not_sealed")
        r = await c.get("/api/runs/01a18000-0000-7000-8000-000000000000/check")
        assert r.status_code == 404
        r = await c.get("/api/runs/nope/check")
        assert r.status_code == 404
    await seal(local, running, task)
