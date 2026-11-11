import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall
from aims.events.index import Index
from aims.events.store import LOG

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="demo", oversight="observe")
RMTREE = shutil.rmtree


class Crash(BaseException):
    """The process dies here."""


async def sealed_run(store: LocalEventStore, run_id: str | None = None) -> str:
    run_id = await store.create_run(STARTED, run_id=run_id)
    ref = await store.put_blob(run_id, b"x" * 20000)
    await store.append(run_id, [Draft(ToolCall(call_id="c", name="n", arguments=ref))])
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    return run_id


def entries(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def crash_in_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def remove(self: Index, run_id: str) -> None:
        raise Crash

    monkeypatch.setattr(Index, "remove", remove)


def crash_before_rmtree(monkeypatch: pytest.MonkeyPatch) -> None:
    def rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        raise Crash

    monkeypatch.setattr(shutil, "rmtree", rmtree)


def crash_in_rmtree(monkeypatch: pytest.MonkeyPatch) -> None:
    def rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        RMTREE(Path(path) / "blobs")
        raise Crash

    monkeypatch.setattr(shutil, "rmtree", rmtree)


CRASHES: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "after the rename": crash_in_index,
    "after the index rows went": crash_before_rmtree,
    "during the rmtree": crash_in_rmtree,
}


@pytest.mark.parametrize("crash", list(CRASHES))
async def test_a_delete_cut_short_by_a_crash_stays_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        kept = await sealed_run(store)
        run_id = await sealed_run(store)
        with monkeypatch.context() as m:
            CRASHES[crash](m)
            with pytest.raises(Crash):
                await store.remove(run_id)
        assert run_id not in store.run_ids()
    async with await LocalEventStore.open(root) as store:
        assert await store.run(run_id) is None
        assert await store.events(run_id) == []
        assert store.run_ids() == [kept]
        assert (await store.verify(kept)).ok
    assert entries(root) == [kept]


async def test_a_failed_rmtree_still_deletes_and_the_next_open_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "runs"

    def refuse(path: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    async with await LocalEventStore.open(root) as store:
        run_id = await sealed_run(store)
        with monkeypatch.context() as m, caplog.at_level(logging.WARNING, logger="aims.events"):
            m.setattr(shutil, "rmtree", refuse)
            await store.remove(run_id)
        assert await store.run(run_id) is None
        assert store.run_ids() == []
        assert any("Permission denied" in m for m in caplog.messages)
        assert len(entries(root)) == 1
        assert entries(root)[0] != run_id
    async with await LocalEventStore.open(root) as store:
        assert store.run_ids() == []
    assert entries(root) == []


async def test_a_run_id_deleted_and_made_again_keeps_the_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await sealed_run(store)
        with monkeypatch.context() as m:
            crash_before_rmtree(m)
            with pytest.raises(Crash):
                await store.remove(run_id)
        await store.create_run(STARTED, run_id=run_id)
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
    async with await LocalEventStore.open(root) as store:
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("completed", 2)
        assert (await store.verify(run_id)).ok
    assert entries(root) == [run_id]


async def test_a_delete_of_a_running_run_is_refused_and_moves_nothing(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
    async with await LocalEventStore.open(root) as store:
        with pytest.raises(RuntimeError, match="not sealed"):
            await store.remove(run_id)
        assert (root / run_id / LOG).is_file()
        assert store.run_ids() == [run_id]
