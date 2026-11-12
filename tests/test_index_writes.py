import asyncio
import json
import logging
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
    RunFinished,
    RunStarted,
    ToolCall,
    Verification,
    split_lines,
)
from aims.events import store as store_module
from aims.events.chain import Head, seal
from aims.events.index import Index, IndexGap, Label
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
        head = store._index.head

        async def then_append(run: str) -> object:
            found = await head(run)
            monkeypatch.setattr(store._index, "head", head)
            await store.append(run_id, [call(2), call(3)])
            return found

        monkeypatch.setattr(store._index, "head", then_append)
        assert (await store.verify(run_id)).error is None


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
