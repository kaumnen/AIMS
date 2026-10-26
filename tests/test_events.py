import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from typer.testing import CliRunner

from aims.cli import app
from aims.events import (
    BlobRef,
    ChainError,
    Draft,
    Event,
    EventStore,
    LocalEventStore,
    LogTruncated,
    MonitorFlag,
    Payload,
    RunCorrupt,
    RunFinished,
    RunLocked,
    RunStarted,
    ToolCall,
    verify_run,
)
from aims.events.chain import GENESIS, Head, digest, seal
from aims.events.schema import now
from aims.events.store import INLINE_LIMIT

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="demo", oversight="observe", agents=["a1", "a2"])


def call(n: int, arguments: str = "{}", agent: str = "a1") -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments=arguments), agent)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        protocol: EventStore = store
        yield store
        assert protocol is store


async def finished_run(store: LocalEventStore) -> str:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [call(1, '{"cmd": "ls"}'), call(2, agent="a2"), call(3)])
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    return run_id


def log_path(store: LocalEventStore, run_id: str) -> Path:
    return store.root / run_id / "events.jsonl"


async def test_append_and_read(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    appended = await store.append(run_id, [call(1, '{"cmd": "ls"}')])

    page = await store.events(run_id)
    assert page[1] == appended[0]
    assert [e.type for e in page] == ["run.started", "tool.call"]
    assert page[0].prev_hash == GENESIS
    assert page[1].prev_hash == page[0].hash
    assert page[0].payload() == STARTED
    assert page[1].payload() == call(1, '{"cmd": "ls"}').payload
    assert page[1].agent_id == "a1"

    info = await store.run(run_id)
    assert info is not None
    assert (info.scenario, info.status, info.events, info.head) == (
        "demo",
        "running",
        2,
        page[1].hash,
    )


async def test_lines_are_canonical(store: LocalEventStore) -> None:
    run_id = await finished_run(store)
    lines = log_path(store, run_id).read_bytes().splitlines(keepends=True)
    assert len(lines) == 5
    for line in lines:
        assert rfc8785.dumps(json.loads(line)) + b"\n" == line


async def test_finish_seals_run(store: LocalEventStore) -> None:
    run_id = await finished_run(store)
    info = await store.run(run_id)
    assert info is not None
    assert info.status == "completed"
    assert info.ended_at is not None
    with pytest.raises(ChainError, match="finished"):
        await store.append(run_id, [call(9)])


async def test_run_started_only_first(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    with pytest.raises(ChainError, match="first"):
        await store.append(run_id, [Draft(STARTED)])


async def test_oversized_event_rejected(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    with pytest.raises(ChainError, match="limit"):
        await store.append(run_id, [call(1, "x" * 300_000)])
    assert len(await store.events(run_id)) == 1


def edit(lines: list[bytes]) -> None:
    lines[1] = lines[1].replace(b'\\"ls\\"', b'\\"rm\\"')


def edit_and_rehash(lines: list[bytes]) -> None:
    event = json.loads(lines[1])
    event["data"]["arguments"] = '{"cmd": "rm"}'
    event["hash"] = digest({k: v for k, v in event.items() if k != "hash"})
    lines[1] = rfc8785.dumps(event) + b"\n"


def delete(lines: list[bytes]) -> None:
    del lines[2]


def swap(lines: list[bytes]) -> None:
    lines[2], lines[3] = lines[3], lines[2]


def reformat(lines: list[bytes]) -> None:
    lines[1] = json.dumps(json.loads(lines[1]), indent=1).replace("\n", "").encode() + b"\n"


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (edit, "bad hash"),
        (edit_and_rehash, "prev_hash"),
        (delete, "expected seq 2"),
        (swap, "expected seq 2"),
        (reformat, "not canonical"),
    ],
)
async def test_verify_detects_tampering(
    store: LocalEventStore, tamper: Callable[[list[bytes]], None], reason: str
) -> None:
    run_id = await finished_run(store)
    assert verify_run(store.root / run_id).ok
    path = log_path(store, run_id)
    lines = path.read_bytes().splitlines(keepends=True)
    tamper(lines)
    path.write_bytes(b"".join(lines))
    result = verify_run(store.root / run_id)
    assert not result.ok
    assert result.error is not None
    assert reason in result.error


async def test_verify_detects_transplant(store: LocalEventStore) -> None:
    a = await finished_run(store)
    b = await store.create_run(STARTED)
    await store.append(b, [call(1)])
    stolen = log_path(store, b).read_bytes().splitlines(keepends=True)[1]
    lines = log_path(store, a).read_bytes().splitlines(keepends=True)
    lines[1] = stolen
    log_path(store, a).write_bytes(b"".join(lines))
    result = verify_run(store.root / a)
    assert result.error is not None
    assert "another run" in result.error


async def test_index_anchors_dropped_tail(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await finished_run(store)
    path = root / run_id / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-2]))
    assert verify_run(root / run_id).ok

    for _ in range(2):
        async with await LocalEventStore.open(root) as store:
            result = await store.verify(run_id)
            assert result.error == "log has 3 events, the index recorded 5"
            info = await store.run(run_id)
            assert info is not None
            assert info.status == "corrupt"
            with pytest.raises(RunCorrupt):
                await store.append(run_id, [call(9)])

    async with await LocalEventStore.open(root) as store:
        await store.rebuild()
        assert (await store.verify(run_id)).ok
        await store.append(run_id, [call(9)])
        assert len(await store.events(run_id)) == 4


async def test_old_events_index_after_schema_change(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
    path = root / run_id / "events.jsonl"
    result = verify_run(root / run_id)
    head = Head(seq=result.events - 1, hash=result.head, offset=path.stat().st_size)
    data = {"call_id": "c1", "name": "shell", "arguments": "{}", "retired": "legacy field"}
    _, line, _ = seal(head, run_id=run_id, ts=now(), agent_id="a1", type="tool.call", data=data)
    with path.open("ab") as f:
        f.write(line)

    async with await LocalEventStore.open(root) as store:
        page = await store.events(run_id, query="legacy")
        assert [e.seq for e in page] == [1]
        with pytest.raises(ValueError, match="retired"):
            page[0].payload()


async def test_torn_tail_is_cut_and_recorded(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1), call(2)])
    path = root / run_id / "events.jsonl"
    good = path.read_bytes()
    torn = b'{"agent_id":"a1","data":{"argu'
    path.write_bytes(good + torn)

    async with await LocalEventStore.open(root) as store:
        await store.append(run_id, [call(3)])
        page = await store.events(run_id)
        assert [e.type for e in page[-2:]] == ["log.truncated", "tool.call"]
        truncated = page[-2].payload()
        assert isinstance(truncated, LogTruncated)
        assert truncated.offset == len(good)
        assert truncated.reason == "incomplete line"
        assert await store.get_blob(run_id, truncated.dropped.sha256) == torn
    assert verify_run(root / run_id).ok


async def test_bad_middle_line_blocks_writes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1, '{"cmd": "ls"}'), call(2)])
    path = root / run_id / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    edit(lines)
    path.write_bytes(b"".join(lines))

    async with await LocalEventStore.open(root) as store:
        with pytest.raises(ChainError, match="bad hash"):
            await store.append(run_id, [call(3)])
    assert path.read_bytes() == b"".join(lines)


async def test_second_writer_is_locked(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as first:
        run_id = await first.create_run(STARTED)
        async with await LocalEventStore.open(root) as second:
            assert len(await second.events(run_id)) == 1
            with pytest.raises(RunLocked):
                await second.append(run_id, [call(1)])


def dump(root: Path) -> tuple[list[tuple[object, ...]], ...]:
    db = sqlite3.connect(root / "index.sqlite")
    try:
        runs = db.execute("select * from runs order by run_id").fetchall()
        events = db.execute(
            "select run_id, seq, ts, agent_id, type, data, prev_hash, hash, text"
            " from events order by run_id, seq"
        ).fetchall()
        hits = db.execute(
            "select e.run_id, e.seq from events_fts f join events e on e.id = f.rowid"
            " where events_fts match 'shell' order by e.run_id, e.seq"
        ).fetchall()
    finally:
        db.close()
    return runs, events, hits


async def test_rebuild_matches_incremental(store: LocalEventStore) -> None:
    await finished_run(store)
    open_run = await store.create_run(STARTED)
    await store.append(open_run, [call(1)])
    before = dump(store.root)
    assert len(before[1]) == 7
    await store.rebuild()
    assert dump(store.root) == before
    await store.append(open_run, [call(2)])
    assert len(dump(store.root)[1]) == 8


async def test_index_rebuilds_from_logs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await finished_run(store)
    before = dump(root)
    for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm"):
        (root / name).unlink(missing_ok=True)
    async with await LocalEventStore.open(root) as store:
        assert [r.run_id for r in await store.runs()] == [run_id]
    assert dump(root) == before


async def test_paging(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [call(n, agent="a1" if n % 2 else "a2") for n in range(1, 26)])

    async def seqs(**kwargs: Any) -> list[int]:
        return [e.seq for e in await store.events(run_id, **kwargs)]

    assert await seqs(limit=10) == list(range(10))
    assert await seqs(after=9, limit=10) == list(range(10, 20))
    assert await seqs(before=10, limit=5) == [5, 6, 7, 8, 9]
    assert await seqs(after=3, before=7) == [4, 5, 6]
    assert await seqs(types=["run.started"]) == [0]
    assert await seqs(agent_id="a2", limit=3) == [2, 4, 6]


async def test_search(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(
        run_id,
        [
            call(1, '{"cmd": "cat /shared/notes.txt"}'),
            call(2, '{"cmd": "PYTEST -q"}'),
            call(3, '{"cmd": "say \\"hi\\""}'),
        ],
    )

    async def hits(query: str) -> list[int]:
        return [e.seq for e in await store.events(run_id, query=query)]

    assert await hits("shared/no") == [1]
    assert await hits("pytest") == [2]
    assert await hits("-q") == [2]
    assert await hits('"hi') == [3]
    assert await hits("100%") == []
    assert await hits("nothing here") == []


async def test_blobs(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    short = await store.content(run_id, "ok")
    long = await store.content(run_id, "y" * (INLINE_LIMIT + 1))
    assert short == "ok"
    assert isinstance(long, BlobRef)
    assert await store.get_blob(run_id, long.sha256) == b"y" * (INLINE_LIMIT + 1)
    await store.append(run_id, [Draft(ToolCall(call_id="c1", name="shell", arguments=long))])

    page = await store.events(run_id)
    payload = page[1].payload()
    assert isinstance(payload, ToolCall)
    assert payload.arguments == long
    assert verify_run(store.root / run_id).ok

    blob = store.root / run_id / "blobs" / long.sha256
    blob.write_bytes(b"z")
    assert verify_run(store.root / run_id).bad_blobs == (long.sha256,)
    blob.unlink()
    assert verify_run(store.root / run_id).missing_blobs == (long.sha256,)


async def test_stream_tails_until_finished(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, [call(1)])
    seen: list[int] = []

    async def consume() -> None:
        async for event in store.stream(run_id, poll=10):
            seen.append(event.seq)

    async with asyncio.timeout(5):
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await store.append(run_id, [call(2)])
        await store.append(run_id, [call(3), Draft(RunFinished(status="killed"))])
        await task
    assert seen == [0, 1, 2, 3, 4]
    assert [e.seq async for e in store.stream(run_id, after=4)] == []


async def test_monitor_flag_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        MonitorFlag(
            layer="signal",
            source="exec-audit",
            category="transcript_tampering",
            importance=4,
            evidence=[],
            justification="claimed ls, ran rm",
        )


def test_unknown_type_reads_leniently() -> None:
    event = Event(
        v=1,
        run_id="r",
        seq=1,
        ts="2026-10-26T20:00:00.000000Z",
        agent_id=None,
        type="plugin.note",
        data={"text": "hello", "blob": {"sha256": "a" * 64, "size": 1}},
        prev_hash=GENESIS,
        hash=GENESIS,
    )
    assert event.payload() is None
    assert event.search_text() == "hello"


def test_duplicate_type_rejected() -> None:
    with pytest.raises(TypeError, match="already registered"):

        class Again(Payload):
            type = "run.started"


async def test_cli_verify(store: LocalEventStore) -> None:
    run_id = await finished_run(store)
    runner = CliRunner()
    ok = runner.invoke(app, ["verify", run_id, "--runs-dir", str(store.root)])
    assert ok.exit_code == 0
    assert ok.stdout.startswith("ok: 5 events, finished, head ")

    path = log_path(store, run_id)
    lines = path.read_bytes().splitlines(keepends=True)
    delete(lines)
    path.write_bytes(b"".join(lines))
    bad = runner.invoke(app, ["verify", str(store.root / run_id)])
    assert bad.exit_code == 1
    assert "expected seq 2" in bad.stderr
