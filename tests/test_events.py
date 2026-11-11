import asyncio
import contextlib
import gc
import hashlib
import itertools
import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from hypothesis import example, given
from hypothesis import strategies as st
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
    OperatorAction,
    Payload,
    RunCorrupt,
    RunFinished,
    RunLocked,
    RunStarted,
    TaskScored,
    ToolCall,
    split_lines,
    verify_run,
)
from aims.events.blobs import ZSTD_MAGIC
from aims.events.chain import GENESIS, Head, digest, follow, seal
from aims.events.schema import now, text_of
from aims.events.store import INLINE_LIMIT, LOG

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


def fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """The device and inode of every file or directory os.fsync syncs from now on."""
    fsync = os.fsync
    synced: list[tuple[int, int]] = []

    def spy(fd: int) -> None:
        st = os.fstat(fd)
        synced.append((st.st_dev, st.st_ino))
        fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return synced


def ident(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino


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


async def test_appended_events_match_what_a_reader_gets_later(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path) as store:
        run_id = await store.create_run(RunStarted(scenario="s", oversight="observe"))
        seen: list[Event] = []
        store.tap(run_id, seen.extend)
        (live,) = await store.append(run_id, [Draft(TaskScored(reward=1.0))])
        assert live.data["reward"] == 1
        assert isinstance(live.data["reward"], int)
        assert seen == [live]
        (indexed,) = await store.events(run_id, after=0)
        assert indexed == live
    async with await LocalEventStore.open(tmp_path) as store:
        await store.rebuild()
        (rebuilt,) = await store.events(run_id, after=0)
        assert rebuilt == live


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


async def test_content_writes_a_lone_surrogate_as_text(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    assert await store.content(run_id, "a\ud800") == "a\\ud800"
    long = await store.content(run_id, "y" * INLINE_LIMIT + "\udfff")
    assert isinstance(long, BlobRef)
    assert await store.get_blob(run_id, long.sha256) == b"y" * INLINE_LIMIT + b"\\udfff"


async def test_append_writes_a_lone_surrogate_as_text(
    store: LocalEventStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = await store.create_run(STARTED)
    action = OperatorAction(action="pause", reason="a\ud800")
    scored = TaskScored(reward=1.0, metrics={"m": ["x\udbff", {"n": "\udc00"}]})
    with caplog.at_level(logging.WARNING, logger="aims.events"):
        appended = await store.append(run_id, [Draft(action, "a1"), Draft(scored), call(1)])
    assert [e.seq for e in appended] == [1, 2, 3]
    assert appended[0].data["reason"] == "a\\ud800"
    assert appended[1].data["metrics"] == {"m": ["x\\udbff", {"n": "\\udc00"}]}
    assert action.reason == "a\ud800"
    lines = log_path(store, run_id).read_bytes().splitlines(keepends=True)
    assert [Event.model_validate(json.loads(line)) for line in lines[1:]] == appended
    assert await store.events(run_id, after=0) == appended
    assert verify_run(store.root / run_id).ok
    assert [r.getMessage() for r in caplog.records] == [
        f"run {run_id}: operator.action held a lone surrogate, written as text",
        f"run {run_id}: task.scored held a lone surrogate, written as text",
    ]
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    assert verify_run(store.root / run_id).ok


async def test_append_still_refuses_what_text_cannot_mend(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    with pytest.raises(rfc8785.CanonicalizationError):
        await store.append(run_id, [call(1), call(2, agent="a\ud800")])
    assert len(await store.events(run_id)) == 1
    assert len(log_path(store, run_id).read_bytes().splitlines()) == 1
    await store.append(run_id, [call(3)])
    assert verify_run(store.root / run_id).ok


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


async def test_blob_batches(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    first = await store.put_blob(run_id, b"a")
    refs = await store.put_blobs(run_id, [b"b", b"a", b"b", b""])
    assert [r.size for r in refs] == [1, 1, 1, 0]
    assert refs[1] == first
    assert refs[0] == refs[2]
    for ref, data in zip(refs, [b"b", b"a", b"b", b""], strict=True):
        assert await store.get_blob(run_id, ref.sha256) == data
    assert sorted(p.name for p in (store.root / run_id / "blobs").iterdir()) == sorted(
        {r.sha256 for r in refs}
    )


async def test_a_failed_batch_syncs_the_blobs_it_placed(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await store.create_run(STARTED)
    blobs = store.root / run_id / "blobs"
    replace = Path.replace
    calls = 0

    def flaky(self: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    synced = fsyncs(monkeypatch)
    with pytest.raises(OSError, match="disk full"):
        await store.put_blobs(run_id, [b"first", b"second"])
    first, second = (hashlib.sha256(d).hexdigest() for d in [b"first", b"second"])
    assert (blobs / first).is_file()
    assert not (blobs / second).exists()
    assert not [p for p in blobs.iterdir() if p.name.startswith(".")]
    assert [s == ident(blobs) for s in synced] == [False, False, False, True]


async def test_blobs_already_stored_are_synced_again(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await store.create_run(STARTED)
    ref = await store.put_blob(run_id, b"first")
    blobs = store.root / run_id / "blobs"
    synced = fsyncs(monkeypatch)
    assert await store.put_blobs(run_id, [b"first"]) == [ref]
    assert synced == [ident(blobs)]

    async def chunks() -> AsyncIterator[bytes]:
        yield (blobs / ref.sha256).read_bytes()

    synced.clear()
    assert await store.put_blob_stream(run_id, ref.sha256, chunks(), limit=1 << 20) == ref
    assert synced == [ident(blobs)]


async def test_a_new_run_syncs_its_entries_before_its_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = fsyncs(monkeypatch)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(STARTED)
        run_dir = store.root / run_id
        order = [synced.index(ident(p)) for p in (tmp_path, store.root, run_dir, run_dir / LOG)]
        assert order == sorted(order)

        synced.clear()
        await store.put_blob(run_id, b"first")
        assert synced.index(ident(run_dir)) < synced.index(ident(run_dir / "blobs"))


async def test_a_copied_run_syncs_its_entries_before_its_lines(
    store: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await finished_run(store)
    async with await LocalEventStore.open(tmp_path / "copy", ingest_only=True) as copy:
        synced = fsyncs(monkeypatch)
        await copy.ingest(run_id, split_lines(log_path(store, run_id).read_bytes()))
        run_dir = copy.root / run_id
        order = [synced.index(ident(p)) for p in (copy.root, run_dir, run_dir / LOG)]
        assert order == sorted(order)


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


def test_search_text_leaves_no_cycle_holding_the_text() -> None:
    data = {"a": ["one", {"b": "two", "blob": {"sha256": "a" * 64, "size": 1}}], "c": ""}
    gc.collect()
    gc.disable()
    try:
        assert text_of(data) == "one\ntwo"
        assert gc.collect() == 0
    finally:
        gc.enable()


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


async def test_blobs_are_compressed_on_disk(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    data = b"model output " * 1000
    ref = await store.put_blob(run_id, data)
    assert ref.size == len(data)
    path = store.root / run_id / "blobs" / ref.sha256
    assert path.read_bytes().startswith(ZSTD_MAGIC)
    assert path.stat().st_size < len(data) // 10
    assert await store.get_blob(run_id, ref.sha256) == data

    for raw in [b"stored before compression", ZSTD_MAGIC + b"not a frame"]:
        legacy = hashlib.sha256(raw).hexdigest()
        (store.root / run_id / "blobs" / legacy).write_bytes(raw)
        assert await store.get_blob(run_id, legacy) == raw
        old = BlobRef(sha256=legacy, size=len(raw))
        await store.append(run_id, [Draft(ToolCall(call_id="o", name="shell", arguments=old))])
    await store.append(run_id, [Draft(ToolCall(call_id="c", name="shell", arguments=ref)), call(2)])
    assert verify_run(store.root / run_id).ok


async def test_a_self_test_case_keeps_its_name_through_a_reindex(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    started = RunStarted(
        scenario="demo",
        oversight="observe",
        kind="selftest.case",
        origin="parent",
        config={"case": "exploit"},
    )
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(started)
        plain = await store.create_run(STARTED)
        assert [(r.run_id, r.case) for r in await store.runs()] == [
            (plain, None),
            (run_id, "exploit"),
        ]
    # an index an older AIMS wrote is rebuilt from the logs
    with contextlib.closing(sqlite3.connect(root / "index.sqlite")) as db:
        db.execute("pragma user_version = 1")
        db.commit()
    async with await LocalEventStore.open(root) as store:
        info = await store.run(run_id)
        assert info is not None
        assert info.case == "exploit"


# Integers past 2**53 - 1 are outside the canonical JSON number domain; seal refuses them
# before anything is written.
JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53) + 1, 2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner, max_size=4)
    ),
    max_leaves=12,
)
CONFIGS = st.dictionaries(st.text(max_size=6), JSON, max_size=4)
ARGUMENTS = st.lists(st.text(max_size=40), max_size=4)


@given(st.lists(CONFIGS, min_size=1, max_size=4))
@example([{"": 2.0**53}])
def test_every_sealed_line_follows(datas: list[dict[str, Any]]) -> None:
    """seal(head, ...) gives a line that follow(head, line) accepts with the same event and
    head, for any JSON data."""
    head = Head()
    for i, data in enumerate(datas):
        kind = "run.started" if i == 0 else "plugin.note"
        event, line, after = seal(head, run_id="r", ts=now(), agent_id=None, type=kind, data=data)
        assert follow(head, line, run_id="r") == (event, after)
        head = after


async def sealed_run(
    store: LocalEventStore, config: dict[str, Any], arguments: list[str]
) -> tuple[Path, list[bytes]]:
    started = RunStarted(scenario="demo", oversight="observe", agents=["a1"], config=config)
    run_id = await store.create_run(started)
    await store.append(run_id, [call(i, a) for i, a in enumerate(arguments, 1)])
    await store.append(run_id, [Draft(RunFinished(status="completed"))])
    path = log_path(store, run_id)
    return path, split_lines(path.read_bytes())


def line_ends(lines: list[bytes]) -> list[int]:
    return list(itertools.accumulate(map(len, lines)))


@given(config=CONFIGS, arguments=ARGUMENTS, at=st.integers(0), change=st.integers(1, 255))
@example(config={"": 2.0**53}, arguments=[], at=0, change=1)
async def test_any_changed_byte_fails_verify(
    store: LocalEventStore, config: dict[str, Any], arguments: list[str], at: int, change: int
) -> None:
    """Changing any one byte of a sealed log fails verify at the line that holds it."""
    path, lines = await sealed_run(store, config, arguments)
    assert verify_run(path.parent).ok
    log = bytearray(b"".join(lines))
    at %= len(log)
    log[at] = (log[at] + change) % 256
    path.write_bytes(log)
    result = verify_run(path.parent)
    assert not result.ok
    assert result.events == sum(end <= at for end in line_ends(lines))


@given(config=CONFIGS, arguments=ARGUMENTS, at=st.integers(0))
async def test_a_cut_log_keeps_the_lines_before_the_cut(
    store: LocalEventStore, config: dict[str, Any], arguments: list[str], at: int
) -> None:
    """Cutting a sealed log at any byte keeps every whole line before the cut. A cut at a line
    end verifies; a cut inside a line, the last one included, is reported as torn."""
    path, lines = await sealed_run(store, config, arguments)
    ends = line_ends(lines)
    at %= ends[-1] + 1
    path.write_bytes(b"".join(lines)[:at])
    result = verify_run(path.parent)
    whole = sum(end <= at for end in ends)
    assert result.events == whole
    assert result.head == (json.loads(lines[whole - 1])["hash"] if whole else GENESIS)
    assert result.finished == (at == ends[-1])
    if at == 0:
        assert result.error == "log is empty"
    elif at in ends:
        assert result.ok
    else:
        assert result.torn
        assert result.error is not None
        assert result.error.endswith("incomplete line")
