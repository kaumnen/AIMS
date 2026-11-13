import asyncio
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from aims.events import (
    Draft,
    Event,
    LocalEventStore,
    MonitorFlag,
    ResourceSample,
    RunCorrupt,
    RunFinished,
    RunStarted,
    ToolCall,
    Verification,
    split_lines,
)
from aims.events import index as index_module
from aims.events import store as store_module
from aims.events.chain import Head, seal
from aims.events.index import BUSY_TRIES, Index, IndexDiverged, IndexGap, Label
from aims.events.schema import now
from aims.events.store import INDEX, LOG

pytestmark = pytest.mark.anyio

RUN = "019a6f00-0000-7000-8000-000000000001"


class Gate:
    """A SQL function that holds the index connection's thread until the test opens it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()

    def __call__(self) -> int:
        self.entered.set()
        self.opened.wait(10)
        return 1

    async def held(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 10)


@pytest.fixture
async def index(tmp_path: Path) -> AsyncIterator[Index]:
    index = await Index.open(tmp_path / "index.sqlite")
    yield index
    await index.close()


@pytest.fixture
async def gate(index: Index) -> AsyncIterator[Gate]:
    gate = Gate()
    await index._db.create_function("gate", 0, gate)
    yield gate
    gate.opened.set()


def label(flag: int) -> Label:
    return Label(run_id=RUN, flag=flag, label="real", by="tester", at=now())


def started() -> tuple[list[Event], Head]:
    payload = RunStarted(scenario="demo", oversight="observe")
    body, _, head = seal(
        Head(),
        run_id=RUN,
        ts=now(),
        agent_id=None,
        type=payload.type,
        data=payload.model_dump(mode="json"),
    )
    return [Event.model_validate(body)], head


WRITES: dict[str, Callable[[Index], Coroutine[Any, Any, None]]] = {
    "add": lambda index: index.add(*started()),
    "remove": lambda index: index.remove(RUN),
    "put_labels": lambda index: index.put_labels([label(1)]),
    "set_variant": lambda index: index.set_variant(RUN, "b"),
    "put_costs": lambda index: index.put_costs([(RUN, "h", "{}")]),
    "mark_corrupt": lambda index: index.mark_corrupt(RUN),
}


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("write", list(WRITES))
async def test_a_writer_cancelled_while_its_begin_waits_leaves_no_transaction(
    index: Index, gate: Gate, write: str
) -> None:
    slow = asyncio.create_task(index._db.execute("select gate()"))
    await gate.held()
    task = asyncio.create_task(WRITES[write](index))
    await settle()
    task.cancel()
    gate.opened.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await slow
    await index.put_labels([label(2)])
    assert not index._db.in_transaction
    assert list(await index.labels(RUN)) == [2]


@pytest.mark.parametrize("cancels", [1, 2])
async def test_a_writer_cancelled_mid_statement_rolls_back(
    index: Index, gate: Gate, cancels: int
) -> None:
    await index._db.execute(
        "create temp trigger hold after insert on main.labels begin select gate(); end"
    )
    task = asyncio.create_task(index.put_labels([label(1)]))
    await gate.held()
    for _ in range(cancels):
        task.cancel()
        await settle()
    if cancels == 1:
        gate.opened.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    later = asyncio.create_task(index.put_labels([label(2)]))
    await settle()
    gate.opened.set()
    await later
    assert not index._db.in_transaction
    assert list(await index.labels(RUN)) == [2]


async def test_a_failed_begin_raises_its_own_error(index: Index, tmp_path: Path) -> None:
    other = sqlite3.connect(tmp_path / "index.sqlite", isolation_level=None)
    try:
        other.execute("begin immediate")
        await index._db.execute("pragma busy_timeout = 0")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await index.put_labels([label(1)])
        assert not index._db.in_transaction
    finally:
        other.close()
    await index.put_labels([label(2)])
    assert list(await index.labels(RUN)) == [2]


class Holder:
    """Another connection holding the index's write lock, and each begin the index tries."""

    def __init__(self, index: Index, path: Path) -> None:
        self.index = index
        self.other = sqlite3.connect(path, isolation_level=None)
        self.begins = 0
        self.again = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def _traced(self, sql: str) -> None:
        if sql.startswith("begin immediate"):
            self.begins += 1
            if self.begins == 2:
                self._loop.call_soon_threadsafe(self.again.set)

    async def hold(self, timeout_ms: int) -> None:
        await self.index._db.set_trace_callback(self._traced)
        await self.index._db.execute(f"pragma busy_timeout = {timeout_ms}")
        self.other.execute("begin immediate")

    def close(self) -> None:
        self.other.close()


RETRIED: dict[str, Callable[[Index], Coroutine[Any, Any, None]]] = {
    **{name: write for name, write in WRITES.items() if name != "add"},
    "reset": lambda index: index.reset(),
}


@pytest.mark.parametrize("write", list(RETRIED))
async def test_a_write_begins_again_when_another_connection_held_the_lock_past_the_timeout(
    index: Index, tmp_path: Path, caplog: pytest.LogCaptureFixture, write: str
) -> None:
    holder = Holder(index, tmp_path / "index.sqlite")
    try:
        await holder.hold(200)
        with caplog.at_level(logging.WARNING, logger="aims.events"):
            task = asyncio.create_task(RETRIED[write](index))
            again = asyncio.create_task(holder.again.wait())
            await asyncio.wait([task, again], return_when=asyncio.FIRST_COMPLETED)
            again.cancel()
            holder.other.rollback()
            await task
    finally:
        holder.close()
    assert holder.begins >= 2
    assert any("past the busy timeout" in m for m in caplog.messages)
    assert not index._db.in_transaction
    await index.put_labels([label(2)])
    assert 2 in await index.labels(RUN)


async def test_a_lock_that_lasts_fails_the_write_after_its_tries(
    index: Index, tmp_path: Path
) -> None:
    holder = Holder(index, tmp_path / "index.sqlite")
    try:
        await holder.hold(10)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await index.put_labels([label(1)])
        assert holder.begins == BUSY_TRIES
        assert not index._db.in_transaction
    finally:
        holder.close()
    await index.put_labels([label(2)])
    assert list(await index.labels(RUN)) == [2]


async def test_a_write_that_waits_out_a_lock_lets_a_queued_batch_try_in_between(
    index: Index, tmp_path: Path
) -> None:
    holder = Holder(index, tmp_path / "index.sqlite")
    try:
        await holder.hold(100)
        labels = asyncio.create_task(index.put_labels([label(1)]))
        await settle()

        async def batch() -> None:
            async with index._write(tries=1):
                pass

        # a batch of another run waits for one try of the labels, not for all three
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await batch()
        assert holder.begins == 2
        assert not labels.done()
        holder.other.rollback()
        await labels
        assert list(await index.labels(RUN)) == [1]
    finally:
        holder.close()


async def test_an_index_batch_tries_the_lock_once(index: Index, tmp_path: Path) -> None:
    """The store catches a batch the index did not take up from the log later, so the
    writer does not wait here."""
    holder = Holder(index, tmp_path / "index.sqlite")
    try:
        await holder.hold(10)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await index.add(*started())
        assert holder.begins == 1
    finally:
        holder.close()


async def test_a_transaction_no_writer_owns_is_rolled_back(
    index: Index, caplog: pytest.LogCaptureFixture
) -> None:
    await index._db.execute("begin immediate")
    await index._db.execute(
        "insert into labels (run_id, flag, label, note, by, at) values (?, 7, 'real', '', 'x', '')",
        (RUN,),
    )
    with caplog.at_level(logging.WARNING, logger="aims.events"):
        await index.put_labels([label(2)])
    assert not index._db.in_transaction
    assert list(await index.labels(RUN)) == [2]
    assert any("no writer" in m for m in caplog.messages)


STARTED = RunStarted(scenario="demo", oversight="observe", agents=["a1"])


def call(n: int) -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments="{}"), "a1")


class Breaker:
    """Makes `Index.add` raise `error` for the next `left` batches of the runs `runs` picks."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        runs: Callable[[str], bool] = lambda run_id: True,
    ) -> None:
        self.left = 0
        self.raised = 0
        real = Index.add

        async def add(index: Index, events: Sequence[Event], head: Head, **kw: Any) -> None:
            if self.left > 0 and events and runs(events[0].run_id):
                self.left -= 1
                self.raised += 1
                raise error
            await real(index, events, head, **kw)

        monkeypatch.setattr(Index, "add", add)
        monkeypatch.setattr(store_module, "RETRY_S", 0.01)


def logged(store: LocalEventStore, run_id: str) -> list[int]:
    return [
        json.loads(line)["seq"] for line in (store.root / run_id / LOG).read_bytes().splitlines()
    ]


async def indexed(store: LocalEventStore, run_id: str) -> list[int]:
    return [e.seq for e in await store.every(run_id)]


async def eventually(store: LocalEventStore, check: Callable[[], Awaitable[bool]]) -> None:
    """Wait until `check` holds, reading again each time the store tells of a change."""
    async with asyncio.timeout(10):
        while True:
            changed = store.changed()
            if await check():
                return
            await changed.wait()


def reset_index(root: Path) -> None:
    """What a new index version does: the next open drops the index and rebuilds it."""
    db = sqlite3.connect(root / INDEX)
    db.execute("pragma user_version = 0")
    db.close()


async def test_a_failed_index_write_returns_the_batch_and_the_next_append_catches_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("database is locked"))
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        seen: list[list[int]] = []
        store.tap(run_id, lambda batch: seen.append([e.seq for e in batch]))
        changed = store.changed()
        breaker.left = 1
        written = await store.append(run_id, [call(1), call(2)])
        assert [e.seq for e in written] == [1, 2]
        assert changed.is_set()
        await store.append(run_id, [call(3)])
        assert await indexed(store, run_id) == logged(store, run_id) == [0, 1, 2, 3]
        assert seen == [[1, 2], [3]]
        assert (await store.verify(run_id)).ok
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("running", 4)
    assert breaker.raised == 1


async def test_a_failed_index_write_of_the_last_batch_is_caught_up_unasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
        breaker.left = 3
        await store.append(run_id, [Draft(RunFinished(status="completed"))])

        async def sealed() -> bool:
            info = await store.run(run_id)
            return info is not None and info.status == "completed"

        await eventually(store, sealed)
        assert await indexed(store, run_id) == logged(store, run_id) == [0, 1, 2]
        assert (await store.verify(run_id)).ok
    assert breaker.raised == 3


async def test_a_failed_index_write_of_an_ingest_is_caught_up_and_never_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("database is locked"))
    async with (
        await LocalEventStore.open(tmp_path / "source") as source,
        await LocalEventStore.open(tmp_path / "copy", ingest_only=True) as copy,
    ):
        run_id = await source.create_run(STARTED)
        await source.append(run_id, [call(1), call(2)])
        await source.append(run_id, [Draft(RunFinished(status="completed"))])
        lines = split_lines((source.root / run_id / LOG).read_bytes())
        await copy.ingest(run_id, lines[:2])
        breaker.left = 10**6
        changed = copy.changed()
        assert (await copy.ingest(run_id, lines[2:3])).seq == 2
        assert changed.is_set()
        assert (await copy.ingest(run_id, lines[1:3])).seq == 2
        verified = await copy.verify(run_id)
        assert verified.error == "the index holds 2 of the log's 3 events"
        breaker.left = 0
        assert (await copy.ingest(run_id, lines[3:])).finished

        async def sealed() -> bool:
            info = await copy.run(run_id)
            return info is not None and info.status == "completed"

        await eventually(copy, sealed)
        assert await indexed(copy, run_id) == logged(copy, run_id) == [0, 1, 2, 3]
        assert (await copy.verify(run_id)).ok


@pytest.mark.parametrize("error", [sqlite3.OperationalError("disk I/O error"), ValueError("odd")])
async def test_a_run_the_index_cannot_take_does_not_stop_the_store_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    root = tmp_path / "runs"
    flag = MonitorFlag(
        layer="signal",
        source="s",
        category="scorer_tampering",
        importance=3,
        evidence=[0],
        justification="j",
    )
    async with await LocalEventStore.open(root) as store:
        bad = await store.create_run(STARTED)
        await store.append(bad, [call(1), Draft(RunFinished(status="completed"))])
        good = await store.create_run(STARTED)
        await store.append(good, [Draft(flag, "a1"), Draft(RunFinished(status="completed"))])
        await store.add_label(good, 1, "real", by="tester")
    reset_index(root)
    breaker = Breaker(monkeypatch, error, runs=lambda run_id: run_id == bad)
    breaker.left = 10**6
    with caplog.at_level(logging.ERROR, logger="aims.events"):
        async with await LocalEventStore.open(root) as store:
            info = await store.run(good)
            assert info is not None
            assert (info.status, info.events) == ("completed", 3)
            assert list(await store.labels(good)) == [1]
            assert await store.run(bad) is None
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert len(errors) == 1
            assert bad in errors[0].getMessage()
            breaker.left = 0

            async def back() -> bool:
                info = await store.run(bad)
                return info is not None and info.status == "completed"

            await eventually(store, back)
            assert await indexed(store, bad) == logged(store, bad) == [0, 1, 2]


async def test_labels_sent_again_after_a_failed_index_write_are_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = MonitorFlag(
        layer="signal",
        source="s",
        category="scorer_tampering",
        importance=3,
        evidence=[0],
        justification="j",
    )
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [Draft(flag, "a1"), Draft(RunFinished(status="completed"))])
        sent = [Label(run_id=run_id, flag=1, label="real", by="op", at=now())]
        real = Index.put_labels
        fails = [sqlite3.OperationalError("database is locked")]

        async def put_labels(index: Index, labels: Sequence[Label]) -> None:
            if fails:
                raise fails.pop()
            await real(index, labels)

        monkeypatch.setattr(Index, "put_labels", put_labels)
        with pytest.raises(sqlite3.OperationalError):
            await store.merge_labels(run_id, sent)
        assert await store.labels(run_id) == {}
        assert await store.merge_labels(run_id, sent) == 0
        assert list(await store.labels(run_id)) == [1]
        assert await store.label_history(run_id) == sent


FLAG = MonitorFlag(
    layer="signal",
    source="s",
    category="scorer_tampering",
    importance=3,
    evidence=[0],
    justification="j",
)
NOT_FLAGS = [0, 2, 3, 4, 10**20, -1]


async def test_a_flag_a_rebuild_elsewhere_took_from_the_index_is_labelled_from_the_log(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [Draft(FLAG, "a1"), call(2)])
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
        other = await Index.open(root / INDEX)
        try:
            await other.reset()
            made = await store.add_label(run_id, 1, "real", by="op")
            for seq in NOT_FLAGS:
                with pytest.raises(KeyError, match="no flag"):
                    await store.add_label(run_id, seq, "real", by="op")
        finally:
            await other.close()
        assert await store.label_history(run_id) == [made]
        await store.sync()
        assert list(await store.labels(run_id)) == [1]


async def test_a_flag_the_index_is_behind_on_is_labelled_from_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        breaker.left = 10**6
        await store.append(run_id, [Draft(FLAG, "a1"), call(2)])
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
        assert await indexed(store, run_id) == [0]
        made = await store.add_label(run_id, 1, "real", by="op")
        for seq in NOT_FLAGS:
            with pytest.raises(KeyError, match="no flag"):
                await store.add_label(run_id, seq, "real", by="op")
        assert await store.label_history(run_id) == [made]


async def behind_at_open(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, sealed: bool
) -> tuple[str, LocalEventStore]:
    """A run whose index a new open of the store cannot take, with no writer, and the store,
    with its own catch up stopped so only the test reads the log."""
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        end = [Draft(RunFinished(status="completed"))] if sealed else []
        await store.append(run_id, [call(1), *end])
    reset_index(root)
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    breaker.left = 10**6
    store = await LocalEventStore.open(root)
    task = store._lags[run_id].task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert run_id in store._lags
    assert run_id not in store._writers
    return run_id, store


async def test_a_log_head_read_while_a_writer_opens_counts_no_line_it_has_not_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=False)
    try:
        before = await store.log_head(run_id)
        assert before is not None
        assert before.seq == 1
        armed, reading, go = threading.Event(), threading.Event(), threading.Event()
        written, release = threading.Event(), threading.Event()
        take, sync = store_module._take, store_module.LogFile.sync

        def held_take(*args: Any) -> Any:
            if armed.is_set():
                armed.clear()
                reading.set()
                go.wait(10)
            return take(*args)

        def held_sync(self: Any) -> None:
            written.set()
            release.wait(10)
            sync(self)

        monkeypatch.setattr(store_module, "_take", held_take)
        monkeypatch.setattr(store_module.LogFile, "sync", held_sync)
        armed.set()
        head = asyncio.create_task(store.log_head(run_id))
        assert await asyncio.to_thread(reading.wait, 10)
        append = asyncio.create_task(store.append(run_id, [call(2)]))
        # a writer that opened now would write its line here, before its fsync
        await asyncio.to_thread(written.wait, 0.5)
        go.set()
        found = await head
        release.set()
        await append
        assert found == before
        after = await store.log_head(run_id)
        assert after is not None
        assert after.seq == 2
    finally:
        go.set()
        release.set()
        await store.close()


async def test_a_log_head_read_of_one_run_holds_up_no_writer_of_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=False)
    try:
        reading, go = threading.Event(), threading.Event()
        take = store_module._take

        def held_take(*args: Any) -> Any:
            reading.set()
            go.wait(10)
            return take(*args)

        monkeypatch.setattr(store_module, "_take", held_take)
        head = asyncio.create_task(store.log_head(run_id))
        assert await asyncio.to_thread(reading.wait, 10)
        monkeypatch.setattr(store_module, "_take", take)
        async with asyncio.timeout(5):
            other = await store.create_run(STARTED)
            await store.append(other, [call(1)])
        assert not head.done()
        go.set()
        found = await head
        assert found is not None
        assert found.seq == 1
        assert store._opening._locks == {}
    finally:
        go.set()
        await store.close()


async def test_a_sync_while_a_writer_opens_indexes_no_line_it_has_not_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
    reset_index(root)
    store = await LocalEventStore.open(root)
    try:
        await store._index.reset()
        armed, reading, go = threading.Event(), threading.Event(), threading.Event()
        written, release = threading.Event(), threading.Event()
        take, sync = store_module._take, store_module.LogFile.sync

        def held_take(*args: Any) -> Any:
            if armed.is_set():
                armed.clear()
                reading.set()
                go.wait(10)
            return take(*args)

        def held_sync(self: Any) -> None:
            written.set()
            release.wait(10)
            sync(self)

        monkeypatch.setattr(store_module, "_take", held_take)
        monkeypatch.setattr(store_module.LogFile, "sync", held_sync)
        armed.set()
        synced = asyncio.create_task(store.sync())
        assert await asyncio.to_thread(reading.wait, 10)
        append = asyncio.create_task(store.append(run_id, [call(2)]))
        # a writer that opened now would write its line here, before its fsync
        await asyncio.to_thread(written.wait, 0.5)
        go.set()
        await synced
        assert await indexed(store, run_id) == [0, 1]
        release.set()
        await append
        assert await indexed(store, run_id) == [0, 1, 2]
    finally:
        go.set()
        release.set()
        await store.close()


def deleted_elsewhere(store: LocalEventStore, run_id: str) -> None:
    """What a delete of the run by another process leaves in its folder's place."""
    (store.root / run_id).rename(store.root / f".deleted.{run_id}.0a0a0a0a")


async def test_a_run_another_process_deleted_is_not_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=True)
    try:
        assert await store.log_head(run_id) is not None
        assert run_id in store._sealed
        deleted_elsewhere(store, run_id)
        assert await store.log_head(run_id) is None
        assert run_id not in store._lags
        assert run_id not in store._sealed
    finally:
        await store.close()


async def test_a_catch_up_of_a_run_another_process_deleted_forgets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=False)
    try:
        deleted_elsewhere(store, run_id)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="aims.events"):
            assert await store._index_log(run_id) is None
        assert run_id not in store._lags
        assert caplog.messages == []
    finally:
        await store.close()


async def test_a_run_deleted_while_its_log_is_indexed_leaves_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
        kept = await store.create_run(STARTED)
        await store._index.reset()
        take = store_module._take

        def then_deleted(path: Path, *args: Any) -> Any:
            found = take(path, *args)
            if path.parent.name == run_id:
                deleted_elsewhere(store, run_id)
            return found

        monkeypatch.setattr(store_module, "_take", then_deleted)
        await store.sync()
        assert await store.run(run_id) is None
        assert await indexed(store, run_id) == []
        assert run_id not in store._lags
        assert await indexed(store, kept) == [0]


async def lagging_with_rows(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Any]:
    """An open store with a sealed run whose index holds its first event only and is behind,
    with its own catch up stopped."""
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    store = await LocalEventStore.open(root)
    run_id = await store.create_run(STARTED)
    breaker.left = 10**6
    await store.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    task = store._lags[run_id].task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await indexed(store, run_id) == [0]
    return run_id, store


async def test_the_rows_of_a_lagging_run_another_process_deleted_are_taken_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await lagging_with_rows(tmp_path / "runs", monkeypatch)
    try:
        deleted_elsewhere(store, run_id)
        shutil.rmtree(store.root / f".deleted.{run_id}.0a0a0a0a")
        assert await store.log_head(run_id) is None
        assert await store.run(run_id) is None
        assert await indexed(store, run_id) == []
        assert run_id not in store._lags
        assert list(store.root.glob(".deleted.*")) == []
    finally:
        await store.close()


async def test_rows_taken_back_in_part_go_at_the_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    run_id, store = await lagging_with_rows(root, monkeypatch)
    try:
        deleted_elsewhere(store, run_id)
        shutil.rmtree(root / f".deleted.{run_id}.0a0a0a0a")
        await store._index._db.execute(
            "create temp trigger cut before delete on main.events"
            " begin select raise(abort, 'cut short'); end"
        )
        assert await store.log_head(run_id) is None
        assert len(await rows(store._index, run_id)) == 1
    finally:
        await store.close()
    async with await LocalEventStore.open(root) as store:
        assert await rows(store._index, run_id) == {}
    assert list(root.glob(".deleted.*")) == []


@pytest.mark.parametrize("seals", [False, True])
async def test_cut_lines_another_process_indexes_after_the_cut_are_taken_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, seals: bool
) -> None:
    """A reader reads a batch whose fsync then fails, and indexes it after the writer cut it
    and wrote the next batch in its place: the writer's index write is refused, and the run is
    indexed again from its log, labels too, with no rebuild; after the batch that seals it
    too, once the writer is gone. Verify says so meanwhile."""
    monkeypatch.setattr(store_module, "RETRY_S", 0.01)
    loop = asyncio.get_running_loop()
    root = tmp_path / "runs"
    sync = store_module.LogFile.sync
    async with (
        await LocalEventStore.open(root) as store,
        await LocalEventStore.open(root) as other,
    ):
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [Draft(FLAG, "a1")])
        await store.add_label(run_id, 1, "real", by="op")
        start = await store.log_head(run_id)
        assert start is not None
        read: list[Any] = []

        def failed(self: Any) -> None:
            read.extend(store_module._take(store._log(run_id), run_id, start, 10)[0])
            raise OSError(5, "Input/output error")

        def late(self: Any) -> None:
            # the reader's commit lands after the cut, before the writer indexes its batch
            events = [Event.model_validate(e) for e, _ in read]
            done = asyncio.run_coroutine_threadsafe(other._index.add(events, read[-1][1]), loop)
            done.result(10)
            sync(self)

        with monkeypatch.context() as patch:
            patch.setattr(store_module.LogFile, "sync", failed)
            with pytest.raises(OSError, match="Input/output error"):
                await store.append(run_id, [call(2), call(3)])
        told: list[str | None] = []
        drop = Index.drop

        async def watched(index: Index, run: str) -> None:
            told.append((await store.verify(run)).error)
            await drop(index, run)
            told.append((await store.verify(run)).error)

        monkeypatch.setattr(Index, "drop", watched)
        with caplog.at_level(logging.WARNING, logger="aims.events"), monkeypatch.context() as patch:
            patch.setattr(store_module.LogFile, "sync", late)
            end = [Draft(RunFinished(status="completed"))] if seals else []
            await store.append(run_id, [call(4), *end])
            want = [0, 1, 2, 3] if seals else [0, 1, 2]

            async def healed() -> bool:
                if await indexed(store, run_id) != logged(store, run_id):
                    return False
                return (await store.verify(run_id)).ok

            await eventually(store, healed)
        assert told == (
            [
                "the last event the index holds is not the log's",
                "the index holds 0 of the log's 4 events",
            ]
            if seals
            else [
                "log has 3 events, the index recorded 4",
                "the index holds 0 of the log's 3 events",
            ]
        )
        assert logged(store, run_id) == want
        assert run_id not in store._writers if seals else run_id in store._writers
        assert any("indexed again from its log" in m for m in caplog.messages)
        assert [e.data.get("call_id") for e in await store.every(run_id)][2] == "c4"
        info = await store.run(run_id)
        assert info is not None
        status = "completed" if seals else "running"
        assert (info.status, info.events, info.flags) == (status, len(want), 1)
        assert list(await store.labels(run_id)) == [1]
        assert (await store.verify(run_id)).ok
        if seals:
            return
        await store.append(run_id, [call(5), Draft(RunFinished(status="completed"))])
        assert await indexed(store, run_id) == logged(store, run_id) == [0, 1, 2, 3, 4]
        assert (await store.verify(run_id)).ok
        assert (await other.verify(run_id)).ok


@pytest.mark.parametrize("resume", [1, 2, 4])
async def test_another_store_sees_a_retake_as_a_run_still_to_be_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: int
) -> None:
    """While the writer's store drops a run's rows and indexes it again from its log, a store
    on the same folder sees either no run, which its log says is to come, or the run with its
    first events, all of them. Its stream, which reads on from the drop's `resume`th step,
    waits meanwhile: it skips no event and raises no KeyError."""
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 2)
    monkeypatch.setattr(store_module, "MAX_PAGE", 2)
    monkeypatch.setattr(store_module, "CHUNK", 3)
    root = tmp_path / "runs"
    async with (
        await LocalEventStore.open(root) as store,
        await LocalEventStore.open(root) as reader,
    ):
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(n) for n in range(1, 8)])
        seen: list[int] = []
        paused, go = asyncio.Event(), asyncio.Event()

        async def consume() -> None:
            async for event in reader.stream(run_id, poll=0.005):
                seen.append(event.seq)
                if event.seq == 1:
                    paused.set()
                    await go.wait()

        task = asyncio.create_task(consume())
        async with asyncio.timeout(5):
            await paused.wait()
        assert seen == [0, 1]
        steps = 0
        views: list[int | None] = []

        async def step() -> None:
            nonlocal steps
            steps += 1
            if steps >= resume:
                go.set()
            await asyncio.sleep(0.03)
            info = await reader.run(run_id)
            views.append(None if info is None else info.events)
            if info is None:
                assert reader.logged(run_id)
            else:
                assert [e.seq for e in await reader.every(run_id)] == list(range(info.events))

        remove_some, remove_row, add = Index._remove_some, Index._remove_row, Index.add

        async def some(index: Index, table: Any, run: str) -> bool:
            found = await remove_some(index, table, run)
            await step()
            return found

        async def row(index: Index, run: str) -> None:
            await remove_row(index, run)
            await step()

        async def added(index: Index, events: Sequence[Event], head: Head, **kw: Any) -> None:
            await add(index, events, head, **kw)
            await step()

        with monkeypatch.context() as patch:
            patch.setattr(Index, "_remove_some", some)
            patch.setattr(Index, "_remove_row", row)
            patch.setattr(Index, "add", added)
            async with store._still(run_id):
                await store._retake(run_id)
                await store._index_log(run_id)
        assert views == [None] * 7 + [3, 6, 8]
        go.set()
        await store.append(run_id, [call(8), Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(5):
            await task
        assert seen == list(range(10))


async def test_a_retake_a_cancel_cuts_short_leaves_a_run_the_next_open_indexes_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retake took the run's row and some of its events when a cancel stopped it. The rows
    it left are no run's, so the next open indexes the run again from its log."""
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 2)
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(n) for n in range(1, 8)])
        remove_some = Index._remove_some
        held = asyncio.Event()

        async def slow(index: Index, table: Any, run: str) -> bool:
            found = await remove_some(index, table, run)
            held.set()
            await asyncio.sleep(10)
            return found

        async def retake() -> None:
            async with store._still(run_id):
                await store._retake(run_id)

        with monkeypatch.context() as patch:
            patch.setattr(Index, "_remove_some", slow)
            task = asyncio.create_task(retake())
            await held.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await store.run(run_id) is None
            await store.close()
    async with await LocalEventStore.open(root) as store:
        assert await indexed(store, run_id) == logged(store, run_id) == list(range(8))
        assert (await store.verify(run_id)).ok
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("completed", 9)


async def test_a_stream_gives_no_row_past_the_head_of_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows of cut lines past the run's head, as an old drop that raced a batch left them,
    are no events of the run: a stream waits at the head until the log's own come."""
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2)])
        found = await store.head(run_id)
        assert found is not None
        head = start = found
        cut = []
        for n in range(2):
            payload = ToolCall(call_id=f"cut{n}", name="n", arguments="{}")
            data = payload.model_dump(mode="json")
            body, _, head = seal(
                head, run_id=run_id, ts=now(), agent_id="a1", type=payload.type, data=data
            )
            cut.append(Event.model_validate(body))
        await store._index.add(cut, head)
        await store._index._db.execute(
            "update runs set last_seq = ?, last_hash = ?, log_offset = ? where run_id = ?",
            (start.seq, start.hash, start.offset, run_id),
        )
        seen: list[Event] = []
        at_head = asyncio.Event()

        async def consume() -> None:
            async for event in store.stream(run_id, poll=0.005):
                seen.append(event)
                if event.seq == start.seq:
                    at_head.set()

        task = asyncio.create_task(consume())
        async with asyncio.timeout(5):
            await at_head.wait()
        # polls enough for the stream to give a row past the head, if it would
        await asyncio.sleep(0.05)
        assert [e.seq for e in seen] == [0, 1, 2]
        await store.append(run_id, [call(3), Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(5):
            await task
        lines = (store.root / run_id / LOG).read_bytes().splitlines()
        assert [e.hash for e in seen] == [json.loads(line)["hash"] for line in lines]


async def test_a_run_no_writer_here_holds_is_corrupt_once_and_takes_its_events_after_a_rebuild(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no writer or sink here, nothing tells lines a writer cut from a log that lost
    events: the index keeps the run corrupt until a rebuild."""
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
    # what cut lines another process indexed leave once the writer is gone
    db = sqlite3.connect(root / INDEX)
    db.execute("update events set hash = 'cut' where run_id = ? and seq = 1", (run_id,))
    db.execute("update runs set last_hash = 'cut' where run_id = ?", (run_id,))
    db.commit()
    db.close()
    with caplog.at_level(logging.WARNING, logger="aims.events"):
        async with await LocalEventStore.open(root) as store:
            info = await store.run(run_id)
            assert info is not None
            assert info.status == "corrupt"
            assert len(caplog.records) == 1
            assert "rebuild the index" in caplog.records[0].getMessage()
            with pytest.raises(RunCorrupt):
                await store.append(run_id, [call(2)])
            assert (await store.verify(run_id)).error == (
                "the last event the index holds is not the log's"
            )
            await store.rebuild()
            await store.append(run_id, [call(2)])
            assert await indexed(store, run_id) == logged(store, run_id) == [0, 1, 2]
            assert (await store.verify(run_id)).ok


async def test_a_sealed_run_the_index_is_behind_on_reads_its_log_head_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=True)
    try:
        reads = 0
        take = store_module._take

        def counted(*args: Any) -> Any:
            nonlocal reads
            reads += 1
            return take(*args)

        monkeypatch.setattr(store_module, "_take", counted)
        heads = [await store.log_head(run_id) for _ in range(3)]
        assert reads == 1
        assert {(h.seq, h.finished) for h in heads if h is not None} == {(2, True)}
        assert None not in heads
        await store.remove(run_id)
        assert await store.log_head(run_id) is None
        await store.create_run(STARTED, run_id=run_id)
        await store.append(run_id, [call(1)])
        head = await store.log_head(run_id)
        assert head is not None
        assert (head.seq, head.finished) == (1, False)
    finally:
        await store.close()


async def test_an_event_the_index_cannot_take_holds_back_none_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2), call(3), call(4)])
    reset_index(root)
    real = Index.add

    async def add(index: Index, events: Sequence[Event], head: Head, **kw: Any) -> None:
        if any(e.seq == 3 for e in events):
            raise ValueError("the index cannot take seq 3")
        await real(index, events, head, **kw)

    monkeypatch.setattr(Index, "add", add)
    async with await LocalEventStore.open(root) as store:
        assert await indexed(store, run_id) == [0, 1, 2]
        verified = await store.verify(run_id)
        assert verified.error == "the index holds 3 of the log's 5 events"


async def test_an_old_log_with_a_time_past_what_utc_holds_still_indexes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = str(uuid.uuid7())
    head, lines = Head(), []
    for ts, payload in (
        ("2026-11-10T00:00:00.000000Z", STARTED),
        ("9999-12-31T23:59:59.000000-01:00", ToolCall(call_id="c", name="n", arguments="{}")),
        ("0001-01-01T00:00:00.000000+01:00", ToolCall(call_id="d", name="n", arguments="{}")),
    ):
        data = payload.model_dump(mode="json")
        _, line, head = seal(
            head, run_id=run_id, ts=ts, agent_id="a1", type=payload.type, data=data
        )
        lines.append(line)
    (root / run_id).mkdir(parents=True)
    (root / run_id / LOG).write_bytes(b"".join(lines))
    async with await LocalEventStore.open(root) as store:
        events = await store.every(run_id)
        assert [e.seq for e in events] == [0, 1, 2]
        assert [e.model_dump(mode="json")["ts"] for e in events[1:]] == [
            "9999-12-31T23:59:59.999999Z",
            "0001-01-01T00:00:00.000000Z",
        ]


def chain(n: int) -> tuple[list[Event], list[Head]]:
    events, heads, head = [], [], Head()
    for i in range(n):
        payload = STARTED if i == 0 else ToolCall(call_id=f"c{i}", name="n", arguments="{}")
        body, _, head = seal(
            head,
            run_id=RUN,
            ts=now(),
            agent_id="a1",
            type=payload.type,
            data=payload.model_dump(mode="json"),
        )
        events.append(Event.model_validate(body))
        heads.append(head)
    return events, heads


async def rows(index: Index, run_id: str = RUN) -> dict[int, str]:
    """The hash of each event row of `run_id` the index holds, a run's or no run's."""
    async with index._db.execute("select seq, hash from events where run_id = ?", (run_id,)) as cur:
        return {r[0]: r[1] for r in await cur.fetchall()}


async def test_the_index_refuses_a_batch_past_the_event_after_its_last(index: Index) -> None:
    events, heads = chain(4)
    with pytest.raises(IndexGap):
        await index.add(events[1:], heads[3])
    await index.add(events[:1], heads[0])
    with pytest.raises(IndexGap):
        await index.add(events[2:], heads[3])
    await index.add(events[1:3], heads[2])
    await index.add(events[:], heads[3])
    found = await index.head(RUN)
    assert found is not None
    assert found[0] == heads[3]
    assert await index.hashes(RUN, 0, 3) == {e.seq: e.hash for e in events}


def fork(heads: list[Head], at: int, n: int) -> tuple[list[Event], list[Head]]:
    """`n` other events after the event `at` of a chain: what a batch cut off the log held."""
    events, out, head = [], [], heads[at]
    for i in range(n):
        payload = ToolCall(call_id=f"cut{i}", name="n", arguments="{}")
        body, _, head = seal(
            head,
            run_id=RUN,
            ts=now(),
            agent_id="a1",
            type=payload.type,
            data=payload.model_dump(mode="json"),
        )
        events.append(Event.model_validate(body))
        out.append(head)
    return events, out


async def test_the_index_refuses_a_batch_that_overlaps_other_events(index: Index) -> None:
    events, heads = chain(5)
    cut, cut_heads = fork(heads, 1, 2)
    await index.add(events[:3], heads[2])
    # a reader that took the cut lines from the log commits them after the writer went on
    with pytest.raises(IndexDiverged, match="event 2"):
        await index.add(cut, cut_heads[1])
    with pytest.raises(IndexDiverged, match="event 2"):
        await index.add([*events[:2], *cut], cut_heads[1])
    assert await rows(index) == {e.seq: e.hash for e in events[:3]}
    await index.add(events[2:], heads[4])
    found = await index.head(RUN)
    assert found is not None
    assert found[0] == heads[4]
    assert await rows(index) == {e.seq: e.hash for e in events}


async def test_the_index_refuses_a_batch_that_follows_another_event(index: Index) -> None:
    events, heads = chain(4)
    cut, cut_heads = fork(heads, 1, 2)
    await index.add(events[:3], heads[2])
    with pytest.raises(IndexDiverged, match="event 3"):
        await index.add(cut[1:], cut_heads[1])
    assert await rows(index) == {e.seq: e.hash for e in events[:3]}
    # and the writer's own batch after cut lines a reader got in first
    await index.reset()
    await index.add(events[:2], heads[1])
    await index.add(cut, cut_heads[1])
    with pytest.raises(IndexDiverged, match="event 2"):
        await index.add(events[2:3], heads[2])
    with pytest.raises(IndexDiverged, match="event 3"):
        await index.add(events[2:], heads[3])
    found = await index.head(RUN)
    assert found is not None
    assert found[0] == cut_heads[1]


async def test_the_index_drops_rows_no_run_owns_when_their_run_id_starts_again(
    index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 2)
    old, old_heads = chain(6)
    await index.add(old, old_heads[5])
    await index._db.execute(
        "create temp trigger cut before delete on main.events when old.seq >= 2"
        " begin select raise(abort, 'cut short'); end"
    )
    with pytest.raises(sqlite3.Error, match="cut short"):
        await index.remove(RUN)
    await index._db.execute("drop trigger cut")
    assert len(await rows(index)) == 4
    events, heads = chain(3)
    await index.add(events[:2], heads[1])
    assert await rows(index) == {e.seq: e.hash for e in events[:2]}
    await index.add(events[2:], heads[2])
    assert await rows(index) == {e.seq: e.hash for e in events}


async def test_a_drop_keeps_out_the_batches_another_process_indexes_meanwhile(
    index: Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's row goes first: readers see no run, while a batch that does not start the
    run is refused. One that does takes the rows left, cut lines among them. The drop then
    leaves the rows it put in."""
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 2)
    events, heads = chain(6)
    cut, cut_heads = fork(heads, 2, 2)
    await index.add(events[:3], heads[2])
    await index.add(cut, cut_heads[1])
    other = await Index.open(tmp_path / "index.sqlite")
    remove_some = index._remove_some
    calls = 0

    async def then_other(table: Any, run_id: str) -> bool:
        nonlocal calls
        calls += 1
        found = await remove_some(table, run_id)
        if calls == 1:
            assert await other.run(RUN) is None
            assert len(await rows(index)) == 3
            with pytest.raises(IndexGap):
                await other.add(events[3:5], heads[4])
            await other.add(events[:4], heads[3])
        return found

    monkeypatch.setattr(index, "_remove_some", then_other)
    try:
        await index.drop(RUN)
        found = await index.head(RUN)
        assert found is not None
        assert found[0] == heads[3]
        assert await rows(index) == {e.seq: e.hash for e in events[:4]}
        await index.add(events, heads[5])
    finally:
        await other.close()
    found = await index.head(RUN)
    assert found is not None
    assert found[0] == heads[5]
    assert await rows(index) == {e.seq: e.hash for e in events}


async def test_rows_a_drop_leaves_past_a_batch_that_raced_it_go_with_that_batch(
    index: Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader commits one more cut line just before a drop takes the run's row. Another
    process checked for leftovers while that row was there and indexes the run from its start
    once it went. The rows the drop has yet to delete, cut lines among them, go with that
    batch, so the next batch neither fails on them nor keeps them. The drop then stops."""
    events, heads = chain(6)
    cut, cut_heads = fork(heads, 2, 3)
    await index.add(events[:3], heads[2])
    await index.add(cut[:2], cut_heads[1])
    other = await Index.open(tmp_path / "index.sqlite")
    checked, dropped = asyncio.Event(), asyncio.Event()
    orphaned, remove_row = other._orphaned, index._remove_row

    async def check_then_wait(run_id: str) -> bool:
        found = await orphaned(run_id)
        checked.set()
        await dropped.wait()
        return found

    async def raced(run_id: str) -> None:
        await other.add(cut[2:], cut_heads[2])
        start = asyncio.create_task(other.add(events[:3], heads[2]))
        await checked.wait()
        await remove_row(run_id)
        dropped.set()
        await start

    monkeypatch.setattr(other, "_orphaned", check_then_wait)
    monkeypatch.setattr(index, "_remove_row", raced)
    try:
        await index.drop(RUN)
        found = await index.head(RUN)
        assert found is not None
        assert found[0] == heads[2]
        assert await rows(index) == {e.seq: e.hash for e in events[:3]}
        await other.add(events[3:], heads[5])
    finally:
        await other.close()
    assert await rows(index) == {e.seq: e.hash for e in events}


async def test_a_catch_up_over_rows_of_cut_lines_past_its_head_takes_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What that race leaves, met by the catch-up of a store that opens: the run's row ends at
    an event the log holds, with rows of cut lines past it. The log goes in over them at
    once, where the catch-up used to fail on them at every try."""
    root = tmp_path / "runs"
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2)])
        breaker.left = 10**6
        await store.append(run_id, [call(3), call(4), Draft(RunFinished(status="completed"))])
    breaker.left = 0
    index = await Index.open(root / INDEX)
    try:
        found = await index.head(run_id)
        assert found is not None
        head = start = found[0]
        cut = []
        empty = dict.fromkeys(ResourceSample.COLUMNS)
        window = ResourceSample(sandbox_id="s", hz=10, ts=[1_000_000, 900_000], **empty)
        for n in range(3):
            payload = window if n == 1 else ToolCall(call_id=f"cut{n}", name="n", arguments="{}")
            data = payload.model_dump(mode="json")
            body, _, head = seal(
                head, run_id=run_id, ts=now(), agent_id="a1", type=payload.type, data=data
            )
            cut.append(Event.model_validate(body))
        await index.add(cut, head)
        await index._db.execute(
            "update runs set last_seq = ?, last_hash = ?, log_offset = ? where run_id = ?",
            (start.seq, start.hash, start.offset, run_id),
        )
    finally:
        await index.close()
    async with await LocalEventStore.open(root) as store:
        assert run_id not in store._lags
        events = await store.every(run_id)
        lines = (root / run_id / LOG).read_bytes().splitlines()
        assert [e.hash for e in events] == [json.loads(line)["hash"] for line in lines]
        assert await store.samples(run_id) == []
        info = await store.run(run_id)
        assert info is not None
        assert (info.status, info.events) == ("completed", 6)
        assert (await store.verify(run_id)).ok
        await store._index._db.execute(
            "insert into events_fts (events_fts, rank) values ('integrity-check', 1)"
        )


async def test_the_index_drops_rows_another_run_left_under_a_batch(
    index: Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows that land after the check for leftovers, as a remove in another process leaves,
    go with the batch that meets them, so none of them is taken for an event of the run."""
    old, old_heads = chain(4)
    await index.add(old, old_heads[3])
    events, heads = chain(3)
    await index._db.execute("delete from runs")

    async def none(run_id: str) -> bool:
        return False

    monkeypatch.setattr(index, "_orphaned", none)
    with caplog.at_level(logging.WARNING, logger="aims.events"):
        await index.add(events, heads[2])
    assert caplog.messages == [f"run {RUN}: the index held rows past its last event -1, which go"]
    assert await rows(index) == {e.seq: e.hash for e in events}
    await index._db.execute(
        "insert into events_fts (events_fts, rank) values ('integrity-check', 1)"
    )


async def test_a_batch_cancelled_while_it_drops_rows_past_the_last_event_keeps_them_all(
    index: Index, gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, old_heads = chain(4)
    await index.add(old, old_heads[3])
    await index._db.execute("delete from runs")
    events, heads = chain(3)

    async def none(run_id: str) -> bool:
        return False

    monkeypatch.setattr(index, "_orphaned", none)
    await index._db.execute(
        "create temp trigger hold before delete on main.events begin select gate(); end"
    )
    task = asyncio.create_task(index.add(events, heads[2]))
    await gate.held()
    task.cancel()
    gate.opened.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await index._db.execute("drop trigger hold")
    assert await index.head(RUN) is None
    assert await rows(index) == {e.seq: e.hash for e in old}
    await index.add(events, heads[2])
    assert await rows(index) == {e.seq: e.hash for e in events}


def step_of(head: Head, n: int, text: str) -> tuple[list[Event], Head]:
    """One agent step after `head`: its model call, a tool call that runs exec `x1`, which
    reads a file, its result and a resource window, with `text` in the exec, file and result."""
    window = ResourceSample(
        sandbox_id="s", hz=10, ts=[n * 1_000_000, 900_000], **dict.fromkeys(ResourceSample.COLUMNS)
    )
    items: list[tuple[str, dict[str, Any]]] = [
        ("model.request", {"call_id": f"m{n}", "params": {"schema_name": "s"}}),
        ("model.response", {"call_id": f"m{n}", "cost_usd": 0.5, "usage": {"input_tokens": 3}}),
        ("agent.step", {"n": n, "call_id": f"m{n}", "status": "completed", "tool_calls": ["t"]}),
        ("tool.call", {"call_id": "t", "name": "shell", "arguments": "{}", "model_call": f"m{n}"}),
        ("exec.started", {"exec_id": "x1", "call_id": "t", "command": text}),
        ("file.access", {"exec_id": "x1", "op": "read", "path": f"/{text}"}),
        ("exec.finished", {"exec_id": "x1", "call_id": "t", "exit_code": 0, "duration_ms": 1}),
        ("tool.result", {"call_id": "t", "output": text}),
        (window.type, window.model_dump(mode="json")),
    ]
    events = []
    for type_, data in items:
        body, _, head = seal(head, run_id=RUN, ts=now(), agent_id="a1", type=type_, data=data)
        events.append(Event.model_validate(body))
    return events, head


async def reads(index: Index) -> dict[str, Any]:
    """What each read of the run's rows gives."""
    return {
        "events": await index.events(RUN, limit=1000),
        "last": await index.events(RUN, before=10**6),
        "types": await index.events(RUN, types=["tool.call", "agent.step"]),
        "agent": await index.events(RUN, agent_id="a1"),
        "execs": await index.events(RUN, execs=["x1"]),
        "query": await index.events(RUN, query="found"),
        "short": await index.events(RUN, query="fo"),
        "steps": await index.steps(RUN),
        "samples": await index.samples(RUN),
        "counts": await index.type_counts(RUN),
        "activity": await index.activity(RUN, ["x1"]),
        "costs": await index.call_costs(RUN),
        "search": (await index.search("found"))[0],
    }


async def a_run_of_steps(index: Index) -> Head:
    """Index a run of two steps; returns its head."""
    events, heads = chain(1)
    head = heads[0]
    for n in (1, 2):
        batch, head = step_of(head, n, "found")
        events.extend(batch)
    await index.add(events, head)
    return head


async def plant_past(index: Index, head: Head) -> None:
    """Rows of a cut step past the run's head `head`, as an old drop that raced a batch left."""
    cut, end = step_of(head, 3, "found again")
    await index.add(cut, end)
    await index._db.execute(
        "update runs set last_seq = ?, last_hash = ?, log_offset = ? where run_id = ?",
        (head.seq, head.hash, head.offset, RUN),
    )


async def test_a_read_of_a_run_gives_no_row_past_its_head(index: Index) -> None:
    head = await a_run_of_steps(index)
    seen = await reads(index)
    assert all(seen.values())
    await plant_past(index, head)
    assert await reads(index) == seen


async def test_a_read_of_a_run_gives_no_row_while_a_drop_deletes_them(
    index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's row goes first, its events and samples after, a few to a transaction: in
    between, the rows left are no run's. No read gives them."""
    monkeypatch.setattr(index_module, "REMOVE_ROWS", 2)
    await a_run_of_steps(index)
    remove_some = index._remove_some
    during: list[dict[str, Any]] = []

    async def then_read(table: Any, run_id: str) -> bool:
        found = await remove_some(table, run_id)
        if found:
            during.append(await reads(index))
        return found

    monkeypatch.setattr(index, "_remove_some", then_read)
    await index.drop(RUN)
    assert len(during) > 5
    assert [{k for k, v in view.items() if v} for view in during] == [set()] * len(during)


async def test_the_hashes_of_a_run_stop_at_its_head(index: Index) -> None:
    """Rows past the head, or of a run whose row a drop took, hold no event of the run, so
    the ingest overlap check never compares a line to one of them."""
    events, heads = chain(3)
    await index.add(events, heads[2])
    held = {e.seq: e.hash for e in events}
    await plant_past(index, heads[2])
    assert len(await rows(index)) > len(held)
    assert await index.hashes(RUN, 0, 99) == held
    assert await index.hashes(RUN, 2, 5) == {2: events[2].hash}
    await index._remove_row(RUN)
    assert await rows(index)
    assert await index.hashes(RUN, 0, 99) == {}


async def test_an_ingest_resent_over_rows_of_cut_lines_past_the_index_head_goes_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy's index ends before its log, with rows of cut lines past its head. A sender's
    lines the log holds match the log there, so they are taken, not refused as diverged."""
    breaker = Breaker(monkeypatch, sqlite3.OperationalError("database is locked"))
    async with (
        await LocalEventStore.open(tmp_path / "source") as source,
        await LocalEventStore.open(tmp_path / "copy", ingest_only=True) as copy,
    ):
        run_id = await source.create_run(STARTED)
        await source.append(run_id, [call(1), call(2), call(3)])
        lines = split_lines((source.root / run_id / LOG).read_bytes())
        await copy.ingest(run_id, lines[:2])
        start = await copy.head(run_id)
        assert start is not None
        head, cut = start, []
        for n in range(3):
            payload = ToolCall(call_id=f"cut{n}", name="n", arguments="{}")
            data = payload.model_dump(mode="json")
            body, _, head = seal(
                head, run_id=run_id, ts=now(), agent_id="a1", type=payload.type, data=data
            )
            cut.append(Event.model_validate(body))
        await copy._index.add(cut, head)
        await copy._index._db.execute(
            "update runs set last_seq = ?, last_hash = ?, log_offset = ? where run_id = ?",
            (start.seq, start.hash, start.offset, run_id),
        )
        breaker.left = 10**6
        assert (await copy.ingest(run_id, lines[2:3])).seq == 2
        assert (await copy.ingest(run_id, lines[1:4])).seq == 3
        breaker.left = 0

        async def caught_up() -> bool:
            return await indexed(copy, run_id) == logged(copy, run_id)

        await eventually(copy, caught_up)
        assert (await copy.verify(run_id)).ok


async def test_verify_tells_of_a_gap_in_the_index(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2), Draft(RunFinished(status="completed"))])
        db = sqlite3.connect(root / INDEX)
        db.execute("delete from events where run_id = ? and seq = 1", (run_id,))
        db.commit()
        db.close()
        verified = await store.verify(run_id)
        assert verified.error == "the index has 3 of the 4 events up to its last: it has a gap"


async def test_verify_takes_events_that_land_while_it_compares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
        held = store._index.held

        async def then_append(run: str) -> object:
            found = await held(run)
            monkeypatch.setattr(store._index, "held", held)
            await store.append(run_id, [call(2), call(3)])
            return found

        monkeypatch.setattr(store._index, "held", then_append)
        assert (await store.verify(run_id)).error is None


async def test_verify_takes_a_rebuild_in_another_process_while_it_compares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2), Draft(RunFinished(status="completed"))])
        other = await Index.open(root / INDEX)
        loop = asyncio.get_running_loop()
        verify_run = store_module.verify_run

        def then_reset(run_dir: Path, **kw: Any) -> Verification:
            found = verify_run(run_dir, **kw)
            asyncio.run_coroutine_threadsafe(other.reset(), loop).result(10)
            return found

        try:
            monkeypatch.setattr(store_module, "verify_run", then_reset)
            verified = await store.verify(run_id)
            assert (verified.error, verified.events) == (None, 4)
            assert await store.run(run_id) is None
            # and with the index empty before it starts
            verified = await store.verify(run_id)
            assert (verified.error, verified.events) == (None, 4)
        finally:
            await other.close()


async def test_verify_takes_events_that_land_after_it_read_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
        loop = asyncio.get_running_loop()
        verify_run = store_module.verify_run

        def then_append(run_dir: Path, **kw: Any) -> Verification:
            found = verify_run(run_dir, **kw)
            more = store.append(run_id, [call(2), call(3)])
            asyncio.run_coroutine_threadsafe(more, loop).result(10)
            return found

        monkeypatch.setattr(store_module, "verify_run", then_append)
        verified = await store.verify(run_id)
        assert (verified.error, verified.events) == (None, 2)


async def test_verify_fails_a_log_behind_its_index(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2), Draft(RunFinished(status="completed"))])
        path = root / run_id / LOG
        path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:2]))
        verified = await store.verify(run_id)
        assert verified.error == "log has 2 events, the index recorded 4"


async def test_verify_fails_a_log_past_its_index_that_holds_another_event_at_its_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
        # the log written again from its start, one event longer, verifies on its own
        head, lines = Head(), []
        for agent, payload in (
            (None, STARTED),
            ("a1", ToolCall(call_id="x1", name="shell", arguments="{}")),
            ("a1", ToolCall(call_id="x2", name="shell", arguments="{}")),
            (None, RunFinished(status="completed")),
        ):
            data = payload.model_dump(mode="json")
            _, line, head = seal(
                head, run_id=run_id, ts=now(), agent_id=agent, type=payload.type, data=data
            )
            lines.append(line)
        (root / run_id / LOG).write_bytes(b"".join(lines))
        verified = await store.verify(run_id)
        assert verified.events == 4
        assert verified.error == "the last event the index holds is not the log's"


@pytest.mark.parametrize("stays", [False, True])
async def test_a_reader_indexes_no_line_of_a_log_a_live_writer_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stays: bool
) -> None:
    """A store that opens while another holds a run's log leaves the run to its writer: it
    indexes no line of a batch that writer has not synced, nor one a failed cut left in the
    log when `stays`. So no cut line reaches the index, and nothing is indexed again."""
    loop = asyncio.get_running_loop()
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])

        async def peek() -> list[int]:
            async with await LocalEventStore.open(root) as reader:
                assert run_id not in reader._lags
                return await indexed(reader, run_id)

        seen: list[list[int]] = []

        def failed(self: Any) -> None:
            seen.append(asyncio.run_coroutine_threadsafe(peek(), loop).result(10))
            raise OSError(5, "Input/output error")

        def stuck(self: Any, offset: int) -> bytes:
            raise OSError(5, "Input/output error")

        with caplog.at_level(logging.WARNING, logger="aims.events"):
            with monkeypatch.context() as patch:
                patch.setattr(store_module.LogFile, "sync", failed)
                if stays:
                    patch.setattr(store_module.LogFile, "truncate", stuck)
                with pytest.raises(OSError, match="Input/output error"):
                    await store.append(run_id, [call(2), call(3)])
                assert logged(store, run_id) == ([0, 1, 2, 3] if stays else [0, 1])
                seen.append(await peek())
            assert seen == [[0, 1], [0, 1]]
            await store.append(run_id, [call(4)])
            assert await indexed(store, run_id) == logged(store, run_id) == [0, 1, 2]
            assert [e.data.get("call_id") for e in await store.every(run_id)][2] == "c4"
            assert (await store.verify(run_id)).ok
        assert not any("indexed again" in m for m in caplog.messages)
    async with await LocalEventStore.open(root) as store:
        assert (await store.verify(run_id)).ok


async def unindexed_run(root: Path) -> str:
    """A closed run of three events that the next open of the store indexes from its log."""
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2)])
    reset_index(root)
    return run_id


async def test_a_reader_indexes_no_line_a_writer_cut_since_it_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer opened, cut its failed batch and went, all between a reader's read of the log
    and its check: the reader reads the log again and indexes what it holds now."""
    root = tmp_path / "runs"
    run_id = await unindexed_run(root)
    path = root / run_id / LOG
    take = store_module._take
    cuts = 0

    def cut_after(*args: Any) -> Any:
        nonlocal cuts
        found = take(*args)
        if args[0] == path and not cuts:
            cuts += 1
            path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:2]))
        return found

    monkeypatch.setattr(store_module, "_take", cut_after)
    async with await LocalEventStore.open(root) as store:
        assert cuts == 1
        assert await indexed(store, run_id) == logged(store, run_id) == [0, 1]
        assert (await store.verify(run_id)).ok
        await store.append(run_id, [call(9)])
        assert (await store.verify(run_id)).ok


async def test_a_reader_indexes_no_line_of_a_log_a_writer_took_since_it_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer opened between a reader's read of the log and its check: the reader indexes
    none of what it read, as the writer may not have synced it."""
    root = tmp_path / "runs"
    run_id = await unindexed_run(root)
    path = root / run_id / LOG
    take = store_module._take
    writers: list[Any] = []

    def taken_after(*args: Any) -> Any:
        found = take(*args)
        if args[0] == path and not writers:
            writers.append(store_module.LogFile(path))
        return found

    monkeypatch.setattr(store_module, "_take", taken_after)
    async with await LocalEventStore.open(root) as store:
        try:
            assert len(writers) == 1
            assert await store.run(run_id) is None
            assert run_id not in store._lags
        finally:
            writers[0].close()
        await store.sync()
        assert await indexed(store, run_id) == [0, 1, 2]


async def test_a_log_head_read_counts_no_line_of_a_log_another_writer_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, store = await behind_at_open(tmp_path / "runs", monkeypatch, sealed=False)
    try:
        writer = store_module.LogFile(store.root / run_id / LOG)
        try:
            assert await store.log_head(run_id) is None
        finally:
            writer.close()
        found = await store.log_head(run_id)
        assert found is not None
        assert found.seq == 1
    finally:
        await store.close()
