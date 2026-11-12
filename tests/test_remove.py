import asyncio
import contextlib
import itertools
import logging
import shutil
import sqlite3
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest

from aims.events import Draft, LocalEventStore, ResourceSample, RunFinished, RunStarted, ToolCall
from aims.events import index as index_module
from aims.events.index import Index
from aims.events.store import INDEX, LOG

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


def window(n: int) -> Draft:
    empty = dict.fromkeys(ResourceSample.COLUMNS)
    return Draft(ResourceSample(sandbox_id="s", hz=10, ts=[n * 1_000_000, 900_000], **empty), "s")


async def long_run(store: LocalEventStore) -> str:
    """A sealed run of ten tool calls whose text holds `needle` and five resource windows."""
    run_id = await store.create_run(STARTED)
    calls = [Draft(ToolCall(call_id=f"c{n}", name="n", arguments=f"needle {n}")) for n in range(10)]
    await store.append(run_id, [*calls, *(window(n) for n in range(5))])
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    return run_id


async def rows(index: Index, run_id: str) -> int:
    """The event and sample rows of `run_id` the index holds."""
    count = 0
    for table in ("events", "samples"):
        async with index._db.execute(
            f"select count(*) from {table} where run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        count += 0 if row is None else row[0]
    return count


def text_index_ok(root: Path) -> None:
    """Check the text index against the events it holds, those with text. Rows with no text,
    such as resource windows, are left out of both."""
    db = sqlite3.connect(root / INDEX)
    try:
        db.execute("insert into events_fts (events_fts, rank) values ('integrity-check', 1)")
    finally:
        db.close()


async def test_a_remove_deletes_a_few_rows_per_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 3)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        kept = await sealed_run(store)
        run_id = await long_run(store)
        index = store._index
        write = index._write
        left = [await rows(index, run_id)]

        @contextlib.asynccontextmanager
        async def counted(**kw: Any) -> AsyncGenerator[None]:
            async with write(**kw):
                yield
                left.append(await rows(index, run_id))

        monkeypatch.setattr(index, "_write", counted)
        await store.remove(run_id)
        assert left[0] == 22
        assert left[-1] == 0
        assert all(a - b <= 3 for a, b in itertools.pairwise(left))
    text_index_ok(root)
    async with await LocalEventStore.open(root) as store:
        assert (await store.verify(kept)).ok
        assert store.run_ids() == [kept]


async def test_a_remove_cut_short_between_batches_shows_no_run_and_the_next_open_ends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 3)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        kept = await sealed_run(store)
        run_id = await long_run(store)
        await store._index._db.execute(
            "create temp trigger cut before delete on main.events when old.seq >= 5"
            " begin select raise(abort, 'cut short'); end"
        )
        with pytest.raises(sqlite3.Error, match="cut short"):
            await store.remove(run_id)
        assert await rows(store._index, run_id) > 0
        assert await store.run(run_id) is None
        assert [r.run_id for r in await store.runs()] == [kept]
        assert await store.search("needle") == ([], None)
        assert await store.search("needle", skip_kinds=["score"]) == ([], None)
    async with await LocalEventStore.open(root) as store:
        assert await rows(store._index, run_id) == 0
        assert await store.run(run_id) is None
        assert (await store.verify(kept)).ok
    text_index_ok(root)
    assert entries(root) == [kept]


async def test_the_text_index_passes_its_check_while_runs_hold_resource_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 3)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        kept = await long_run(store)
        removed = await long_run(store)
        cut = await long_run(store)
        text_index_ok(root)
        await store.remove(removed)
        text_index_ok(root)
        await store._index._db.execute(
            "create temp trigger cut before delete on main.events when old.seq >= 5"
            " begin select raise(abort, 'cut short'); end"
        )
        with pytest.raises(sqlite3.Error, match="cut short"):
            await store.remove(cut)
        assert await rows(store._index, cut) > 0
        text_index_ok(root)
    async with await LocalEventStore.open(root) as store:
        assert await rows(store._index, cut) == 0
        assert store.run_ids() == [kept]
        assert len(await store.events(kept, query="needle")) == 10
    text_index_ok(root)


CUT = (
    "create temp trigger cut before delete on main.events when old.seq >= 5"
    " begin select raise(abort, 'cut short'); end"
)


async def test_a_run_id_made_again_after_a_remove_cut_short_keeps_the_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 3)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await long_run(store)
        await store._index._db.execute(CUT)
        with pytest.raises(sqlite3.Error, match="cut short"):
            await store.remove(run_id)
        await store._index._db.execute("drop trigger cut")
        assert await rows(store._index, run_id) > 0
        await store.create_run(STARTED, run_id=run_id)
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("completed", 2)
        assert await rows(store._index, run_id) == 2
        assert [e.type for e in await store.every(run_id)] == ["run.started", "run.finished"]
        assert await store.search("needle") == ([], None)
        assert (await store.verify(run_id)).ok
    async with await LocalEventStore.open(root) as store:
        assert (await store.verify(run_id)).ok
    assert entries(root) == [run_id]


async def test_a_remove_cut_short_of_a_run_with_no_folder_is_ended_by_the_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 3)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await long_run(store)
        shutil.rmtree(root / run_id)
        await store._index._db.execute(CUT)
        with pytest.raises(sqlite3.Error, match="cut short"):
            await store.remove(run_id)
        assert await rows(store._index, run_id) > 0
    async with await LocalEventStore.open(root) as store:
        assert await rows(store._index, run_id) == 0
    assert entries(root) == []


async def test_a_remove_of_a_run_with_no_folder_and_no_rows_leaves_nothing(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        await store.remove("019a6f00-0000-7000-8000-00000000000f")
    assert entries(root) == []


async def test_a_run_id_made_again_during_its_remove_starts_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await long_run(store)
        removing, go = asyncio.Event(), asyncio.Event()
        remove = Index.remove

        async def slow(self: Index, run: str) -> None:
            removing.set()
            await go.wait()
            await remove(self, run)

        async def again() -> None:
            await store.create_run(STARTED, run_id=run_id)
            await store.append(run_id, [Draft(RunFinished(status="completed"))])

        monkeypatch.setattr(Index, "remove", slow)
        removed = asyncio.create_task(store.remove(run_id))
        await removing.wait()
        made = asyncio.create_task(again())
        await asyncio.sleep(0.1)
        assert not made.done()
        go.set()
        await removed
        await made
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("completed", 2)
        assert await rows(store._index, run_id) == 2
        assert (await store.verify(run_id)).ok
    async with await LocalEventStore.open(root) as store:
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
