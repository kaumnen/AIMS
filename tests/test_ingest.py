import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from compression import zstd
from pathlib import Path
from typing import Any

import hypothesis
import pytest
from hypothesis import strategies as st

from aims.events import (
    AgentInput,
    BlobRef,
    Diverged,
    Draft,
    Gap,
    Invalid,
    LocalEventStore,
    MissingBlobs,
    RunFinished,
    RunStarted,
    ToolCall,
    split_lines,
    verify_run,
)
from aims.events.blobs import BlobError, BlobStore
from aims.events.chain import Head, follow, seal
from aims.events.schema import now
from aims.events.store import LOG

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="demo", oversight="observe", agents=["a1"])


def call(n: int, arguments: str = "{}") -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments=arguments), "a1")


def given(ref: BlobRef) -> Draft:
    return Draft(AgentInput(kind="inject", step=0, text=ref), "a1")


@pytest.fixture
async def source(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "source") as store:
        yield store


@pytest.fixture
async def copy(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "copy", ingest_only=True) as store:
        yield store


def log_bytes(store: LocalEventStore, run_id: str) -> bytes:
    return (store.root / run_id / LOG).read_bytes()


def lines_of(store: LocalEventStore, run_id: str) -> list[bytes]:
    return split_lines(log_bytes(store, run_id))


async def chunks(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def ship_blobs(source: LocalEventStore, copy: LocalEventStore, run_id: str) -> None:
    copy.prepare_run(run_id)
    for path in sorted((source.root / run_id / "blobs").iterdir()):
        await copy.put_blob_stream(run_id, path.name, chunks(path.read_bytes()), limit=1 << 20)


async def test_a_copy_is_byte_identical(source: LocalEventStore, copy: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED, blobs=[b"x" * 10_000])
    big = await source.content(run_id, "y" * 20_000)
    assert isinstance(big, BlobRef)
    await source.append(run_id, [given(big), call(2)])
    await source.append(run_id, [Draft(RunFinished(status="completed"))])
    await ship_blobs(source, copy, run_id)

    lines = lines_of(source, run_id)
    head = await copy.ingest(run_id, lines[:2])
    assert head.seq == 1
    head = await copy.ingest(run_id, lines[2:])
    assert head.seq == 3
    assert head.finished
    assert log_bytes(copy, run_id) == log_bytes(source, run_id)
    assert verify_run(copy.root / run_id).ok
    info = await copy.run(run_id)
    assert info is not None
    assert (info.status, info.events) == ("completed", 4)
    assert await copy.get_blob(run_id, big.sha256) == b"y" * 20_000


async def test_overlap_with_the_same_hash_is_skipped(
    source: LocalEventStore, copy: LocalEventStore
) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1), call(2), call(3)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:3])
    head = await copy.ingest(run_id, lines[1:])
    assert head.seq == 3
    head = await copy.ingest(run_id, lines[2:3])
    assert head.seq == 3
    assert log_bytes(copy, run_id) == log_bytes(source, run_id)


async def test_a_gap_is_refused_with_the_head(
    source: LocalEventStore, copy: LocalEventStore
) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1), call(2), call(3)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:1])
    with pytest.raises(Gap) as caught:
        await copy.ingest(run_id, lines[2:])
    assert caught.value.head.seq == 0


async def test_a_different_event_at_a_stored_seq_is_divergence(
    tmp_path: Path, copy: LocalEventStore
) -> None:
    run_id = str(uuid.uuid7())
    async with (
        await LocalEventStore.open(tmp_path / "a") as a,
        await LocalEventStore.open(tmp_path / "b") as b,
    ):
        await a.create_run(STARTED, run_id=run_id)
        await a.append(run_id, [call(1)])
        await b.create_run(STARTED, run_id=run_id)
        await b.append(run_id, [call(1, '{"tampered": true}'), call(2)])
        await copy.ingest(run_id, lines_of(a, run_id))
        with pytest.raises(Diverged) as overlap:
            await copy.ingest(run_id, lines_of(b, run_id)[1:])
        assert overlap.value.head.seq == 1
        with pytest.raises(Diverged):
            await copy.ingest(run_id, lines_of(b, run_id)[2:])
    assert log_bytes(copy, run_id).count(b"\n") == 2


async def test_a_bad_line_is_invalid(source: LocalEventStore, copy: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:1])
    with pytest.raises(Invalid):
        await copy.ingest(run_id, [lines[1].replace(b'"c1"', b'"c9"')])
    with pytest.raises(Invalid):
        await copy.ingest(run_id, [b"not json\n"])
    assert (await copy.ingest(run_id, lines[1:])).seq == 1


def sealed(run_id: str, *events: tuple[str, str, dict[str, Any]]) -> list[bytes]:
    """Log lines of `events`, each a time, type and data, chained from the start."""
    out, head = [], Head()
    for ts, type_, data in events:
        _, line, head = seal(head, run_id=run_id, ts=ts, agent_id="a1", type=type_, data=data)
        out.append(line)
    return out


START = ("2026-11-10T00:00:00.000000Z", "run.started", STARTED.model_dump(mode="json"))


async def test_a_time_past_what_utc_holds_is_invalid(copy: LocalEventStore) -> None:
    run_id = str(uuid.uuid7())
    tool = {"call_id": "c", "name": "n", "arguments": "{}"}
    lines = sealed(
        run_id,
        START,
        ("2026-11-10T00:00:01.000000Z", "tool.call", tool),
        ("9999-12-31T23:59:59.000000-01:00", "tool.call", tool),
    )
    await copy.ingest(run_id, lines[:2])
    with pytest.raises(Invalid, match="UTC"):
        await copy.ingest(run_id, lines[2:])
    assert log_bytes(copy, run_id) == b"".join(lines[:2])
    assert [e.seq for e in await copy.events(run_id)] == [0, 1]


async def test_a_pause_whose_deadline_is_past_year_9999_indexes(copy: LocalEventStore) -> None:
    run_id = str(uuid.uuid7())
    config = {"monitor": {"config": {"policy": {"human_timeout_s": 86_400}}}}
    started = STARTED.model_copy(update={"oversight": "human", "config": config})
    pause = {"action": "pause", "agents": ["a1"], "flags": [], "mode": "human"}
    lines = sealed(
        run_id,
        (START[0], "run.started", started.model_dump(mode="json")),
        ("9999-12-31T23:59:59.000000Z", "monitor.action", pause),
    )
    assert (await copy.ingest(run_id, lines)).seq == 1
    info = await copy.run(run_id)
    assert info is not None
    (hold,) = info.held
    assert (hold.agent, hold.until) == ("a1", None)


async def test_the_first_batch_needs_the_bundle_blobs(
    source: LocalEventStore, copy: LocalEventStore
) -> None:
    sha = hashlib.sha256(b"bundle").hexdigest()
    ref = {"sha256": sha, "size": 6}
    started = RunStarted(scenario="demo", oversight="observe", config={"scenario": {"file": ref}})
    run_id = await source.create_run(started, blobs=[b"bundle"])
    lines = lines_of(source, run_id)
    with pytest.raises(MissingBlobs) as caught:
        await copy.ingest(run_id, lines)
    assert caught.value.missing == [sha]
    assert caught.value.head == Head()
    await ship_blobs(source, copy, run_id)
    assert (await copy.ingest(run_id, lines)).seq == 0


async def test_missing_blobs_are_named(source: LocalEventStore, copy: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED)
    ref = await source.content(run_id, "z" * 9000)
    assert isinstance(ref, BlobRef)
    await source.append(run_id, [given(ref)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:1])
    with pytest.raises(MissingBlobs) as caught:
        await copy.ingest(run_id, lines[1:])
    assert caught.value.missing == [ref.sha256]
    assert await copy.missing_blobs(run_id, [ref.sha256, ref.sha256]) == [ref.sha256]
    await ship_blobs(source, copy, run_id)
    assert await copy.missing_blobs(run_id, [ref.sha256]) == []
    assert (await copy.ingest(run_id, lines[1:])).seq == 1


async def test_a_torn_tail_of_the_copy_is_cut_silently(
    tmp_path: Path, source: LocalEventStore
) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1), call(2)])
    lines = lines_of(source, run_id)
    root = tmp_path / "copy"
    async with await LocalEventStore.open(root, ingest_only=True) as copy:
        await copy.ingest(run_id, lines[:2])
    with (root / run_id / LOG).open("ab") as f:
        f.write(lines[2][:10])
    async with await LocalEventStore.open(root, ingest_only=True) as copy:
        head = await copy.head(run_id)
        assert head is not None
        assert head.seq == 1
        assert (await copy.ingest(run_id, lines[2:])).seq == 2
        assert log_bytes(copy, run_id) == log_bytes(source, run_id)
        types = [e.type for e in await copy.events(run_id)]
        assert "log.truncated" not in types


async def test_an_ingest_only_store_never_appends(copy: LocalEventStore) -> None:
    with pytest.raises(RuntimeError):
        await copy.create_run(STARTED)
    with pytest.raises(RuntimeError):
        await copy.append(str(uuid.uuid7()), [call(1)])


async def test_a_run_written_here_is_not_ingested(source: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED)
    with pytest.raises(RuntimeError):
        await source.ingest(run_id, lines_of(source, run_id))


async def test_a_writer_store_ingests_another_run(source: LocalEventStore, tmp_path: Path) -> None:
    run_id = await source.create_run(STARTED)
    async with await LocalEventStore.open(tmp_path / "other") as other:
        await other.ingest(run_id, lines_of(source, run_id))
        with pytest.raises(RuntimeError):
            await other.append(run_id, [call(1)])
        assert (await other.run(run_id)) is not None


async def test_an_ingest_only_store_removes_a_copy_it_never_finished(
    source: LocalEventStore, copy: LocalEventStore, tmp_path: Path
) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:1])
    await copy.remove(run_id)
    assert await copy.run(run_id) is None
    assert not (copy.root / run_id).exists()
    assert (await copy.ingest(run_id, lines)).seq == 1
    async with await LocalEventStore.open(tmp_path / "other") as other:
        await other.ingest(run_id, lines)
        with pytest.raises(RuntimeError, match="open for writing"):
            await other.remove(run_id)


async def test_stream_wakes_on_ingest(source: LocalEventStore, copy: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(1)])
    lines = lines_of(source, run_id)
    await copy.ingest(run_id, lines[:1])
    seen: list[int] = []

    async def read() -> None:
        async for event in copy.stream(run_id, poll=30):
            seen.append(event.seq)

    changed = copy.changed()
    task = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    await copy.ingest(run_id, lines[1:])
    assert changed.is_set()
    await source.append(run_id, [Draft(RunFinished(status="completed"))])
    await copy.ingest(run_id, lines_of(source, run_id)[2:])
    async with asyncio.timeout(2):
        await task
    assert seen == [0, 1, 2]


async def test_given_run_ids_and_prepared_dirs(source: LocalEventStore) -> None:
    run_id = str(uuid.uuid7())
    source.prepare_run(run_id)
    ref = await source.put_blob(run_id, b"bundle")
    assert await source.create_run(STARTED, run_id=run_id) == run_id
    assert await source.get_blob(run_id, ref.sha256) == b"bundle"
    with pytest.raises(FileExistsError):
        await source.create_run(STARTED, run_id=run_id)
    with pytest.raises(KeyError):
        await source.create_run(STARTED, run_id="../x")


async def test_lines_and_seek(source: LocalEventStore) -> None:
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(n) for n in range(1, 10)])
    all_lines = lines_of(source, run_id)
    head = await source.seek(run_id, 3)
    assert head.seq == 3
    assert head.offset == sum(len(x) for x in all_lines[:4])
    assert await source.seek(run_id, -1) == Head()
    with pytest.raises(KeyError):
        await source.seek(run_id, 99)
    got, after = await source.lines(run_id, head, upto=7)
    assert got == all_lines[4:8]
    assert after.seq == 7
    got, after = await source.lines(run_id, Head(), upto=9, max_events=2)
    assert got == all_lines[:2]
    got, _ = await source.lines(run_id, Head(), upto=9, max_bytes=len(all_lines[0]) + 1)
    assert got == all_lines[:1]
    got, same = await source.lines(run_id, after, upto=after.seq)
    assert (got, same) == ([], after)


async def test_search_spans_runs(source: LocalEventStore) -> None:
    a = await source.create_run(STARTED)
    b = await source.create_run(STARTED)
    await source.append(a, [call(1, '{"cmd": "needle one"}')])
    await source.append(b, [call(2, '{"cmd": "needle two"}'), call(3, '{"cmd": "hay"}')])
    found, cursor = await source.search("needle")
    assert [(e.run_id, e.seq) for e in found] == [(b, 1), (a, 1)]
    assert cursor is None
    page, cursor = await source.search("needle", limit=1)
    assert [(e.run_id, e.seq) for e in page] == [(b, 1)]
    assert cursor is not None
    page, cursor = await source.search("needle", limit=1, before=cursor)
    assert [(e.run_id, e.seq) for e in page] == [(a, 1)]
    assert cursor is None
    short, _ = await source.search("ne")
    assert len(short) == 2


def test_split_lines_keeps_a_torn_last_line() -> None:
    assert split_lines(b"a\nb\n") == [b"a\n", b"b\n"]
    assert split_lines(b"a\nb") == [b"a\n", b"b"]
    assert split_lines(b"") == []


BODIES = st.lists(st.sampled_from([b"\n", b"\r", b"{}"]) | st.binary(max_size=8)).map(b"".join)


@hypothesis.given(BODIES)
def test_split_lines_keeps_every_byte(body: bytes) -> None:
    """The lines join back to the body. None is empty. Each ends with its only newline,
    except a last line without one."""
    lines = split_lines(body)
    assert b"".join(lines) == body
    assert all(lines)
    assert all(line.find(b"\n") == len(line) - 1 for line in lines[:-1])
    assert all(b"\n" not in line[:-1] for line in lines[-1:])


ARGUMENTS = st.lists(st.text(max_size=30), max_size=8)
PICKS = st.tuples(st.integers(0), st.integers(0), st.integers(0), st.integers(0))


async def source_run(
    source: LocalEventStore, arguments: list[str], finish: bool
) -> tuple[str, list[bytes], list[Head]]:
    """A run written by `source`, its lines and the head after each count of lines."""
    run_id = await source.create_run(STARTED)
    await source.append(run_id, [call(i, a) for i, a in enumerate(arguments, 1)])
    if finish:
        await source.append(run_id, [Draft(RunFinished(status="completed"))])
    lines = lines_of(source, run_id)
    heads = [Head()]
    for line in lines:
        heads.append(follow(heads[-1], line, run_id=run_id)[1])
    return run_id, lines, heads


def fork(run_id: str, head: Head) -> bytes:
    """Another valid event at the seq after `head`."""
    payload = (
        RunStarted(scenario="fork", oversight="observe")
        if head.seq < 0
        else ToolCall(call_id="fork", name="shell", arguments="{}")
    )
    data = payload.model_dump(mode="json")
    _, line, _ = seal(head, run_id=run_id, ts=now(), agent_id=None, type=payload.type, data=data)
    return line


@hypothesis.given(
    arguments=ARGUMENTS,
    finish=st.booleans(),
    batches=st.lists(st.tuples(st.integers(0, 12), st.integers(0, 12)), max_size=8),
)
async def test_overlapping_batches_give_a_byte_identical_copy(
    source: LocalEventStore,
    copy: LocalEventStore,
    arguments: list[str],
    finish: bool,
    batches: list[tuple[int, int]],
) -> None:
    """Cutting a log into batches in any way, each starting at or before the end of the copy,
    gives a byte identical copy. Each ingest returns the head after the last line sent so far."""
    run_id, lines, heads = await source_run(source, arguments, finish)
    have = 0
    for back, size in [*batches, (0, len(lines))]:
        start = max(0, have - back)
        end = min(len(lines), start + size)
        assert await copy.ingest(run_id, lines[start:end]) == heads[max(have, end)]
        have = max(have, end)
    assert log_bytes(copy, run_id) == log_bytes(source, run_id)
    assert verify_run(copy.root / run_id).ok
    assert await copy.head(run_id) == heads[-1]


@hypothesis.given(arguments=ARGUMENTS, finish=st.booleans(), picks=PICKS)
async def test_a_changed_line_in_the_overlap_diverges(
    source: LocalEventStore,
    copy: LocalEventStore,
    arguments: list[str],
    finish: bool,
    picks: tuple[int, int, int, int],
) -> None:
    """A batch holding another valid event at a seq the copy has raises Diverged with the
    copy's head. The copy stays as it was."""
    run_id, lines, heads = await source_run(source, arguments, finish)
    n = len(lines)
    have = 1 + picks[0] % n
    changed = picks[1] % have
    start = picks[2] % (changed + 1)
    end = changed + 1 + picks[3] % (n - changed)
    await copy.ingest(run_id, lines[:have])
    batch = lines[start:end]
    batch[changed - start] = fork(run_id, heads[changed])
    with pytest.raises(Diverged) as caught:
        await copy.ingest(run_id, batch)
    assert caught.value.head == heads[have]
    assert log_bytes(copy, run_id) == b"".join(lines[:have])
    assert await copy.ingest(run_id, lines[have:]) == heads[n]
    assert log_bytes(copy, run_id) == log_bytes(source, run_id)


@hypothesis.given(arguments=ARGUMENTS.filter(bool), finish=st.booleans(), picks=PICKS)
async def test_a_missing_line_is_refused(
    source: LocalEventStore,
    copy: LocalEventStore,
    arguments: list[str],
    finish: bool,
    picks: tuple[int, int, int, int],
) -> None:
    """A batch without a line the copy lacks raises Gap when it starts past the end of the
    copy and Invalid when the hole is inside it. Either way the error holds the copy's head.
    The copy stays as it was."""
    run_id, lines, heads = await source_run(source, arguments, finish)
    n = len(lines)
    have = picks[0] % (n - 1)
    missing = have + picks[1] % (n - 1 - have)
    start = picks[2] % (missing + 1)
    end = missing + 2 + picks[3] % (n - missing - 1)
    await copy.ingest(run_id, lines[:have])
    batch = lines[start:missing] + lines[missing + 1 : end]
    first = start if start < missing else missing + 1
    with pytest.raises(Gap if first > have else Invalid) as caught:
        await copy.ingest(run_id, batch)
    assert caught.value.head == heads[have]
    assert log_bytes(copy, run_id) == b"".join(lines[:have])
    assert await copy.ingest(run_id, lines[have:]) == heads[n]
    assert log_bytes(copy, run_id) == log_bytes(source, run_id)


@pytest.mark.parametrize("compressed", [True, False])
def test_blob_writer_checks_the_name(tmp_path: Path, compressed: bool) -> None:
    store = BlobStore(tmp_path)
    data = b"payload " * 5000
    sha = hashlib.sha256(data).hexdigest()
    sent = zstd.compress(data) if compressed else data
    writer = store.writer(sha, limit=1 << 20)
    for i in range(0, len(sent), 1000):
        writer.write(sent[i : i + 1000])
    ref = writer.commit()
    assert ref == BlobRef(sha256=sha, size=len(data))
    assert store.get(sha) == data
    assert store.path(sha).read_bytes() == sent
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_blob_writer_refuses_wrong_bytes_and_big_blobs(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = hashlib.sha256(b"right").hexdigest()
    writer = store.writer(sha, limit=100)
    writer.write(zstd.compress(b"wrong"))
    with pytest.raises(BlobError):
        writer.commit()
    assert not store.path(sha).exists()
    big = b"\0" * 10_000
    writer = store.writer(hashlib.sha256(big).hexdigest(), limit=100)
    with pytest.raises(BlobError):
        writer.write(zstd.compress(big))
    writer.abort()
    assert list(tmp_path.iterdir()) == []


def test_blob_writer_reads_several_frames(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    data = b"one" * 100 + b"two" * 100
    sha = hashlib.sha256(data).hexdigest()
    writer = store.writer(sha, limit=1 << 20)
    writer.write(zstd.compress(b"one" * 100) + zstd.compress(b"two" * 100))
    assert writer.commit().size == len(data)
