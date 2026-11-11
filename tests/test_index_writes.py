import asyncio
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from aims.events import Event, RunStarted
from aims.events.chain import Head, seal
from aims.events.index import Index, Label
from aims.events.schema import now

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
