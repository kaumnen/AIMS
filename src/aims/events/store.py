import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC
from functools import partial
from pathlib import Path
from typing import Any, Protocol, Self

from aims.events.blobs import BlobStore, BlobWriter, read_blob, stored_size
from aims.events.chain import ChainError, Head, follow, seal
from aims.events.disk import make_dir, sync_dir, write_all
from aims.events.index import (
    MAX_PAGE,
    CallCost,
    ExecActivity,
    FlagInfo,
    Index,
    Label,
    LabelKind,
    RunInfo,
    SampleWindow,
    StepInfo,
    UnitRow,
)
from aims.events.log import LogFile, checkpoint_ok, read_events, read_lines, tail_only
from aims.events.payloads import LogTruncated, RunFinished, RunStarted
from aims.events.schema import BlobRef, Content, Draft, Event, blob_refs, now
from aims.text import scrub

log = logging.getLogger("aims.events")

LOG = "events.jsonl"
BLOBS = "blobs"
INDEX = "index.sqlite"
TRUTH = "truth.json"
LABELS = "labels.jsonl"
GRAVE = ".deleted."
INLINE_LIMIT = 8 * 1024
CHUNK = 1000
RETRY_S = 1.0
RETRY_MAX_S = 300.0


class RunCorrupt(Exception):
    pass


class TruthRefused(ValueError):
    """A truth file the store will not keep: its run is unknown or not sealed, or its bytes do
    not hash to what run.started committed to."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class IngestError(Exception):
    """A batch of log lines the store refused. `head` is where its copy of the run ends."""

    def __init__(self, message: str, head: Head) -> None:
        super().__init__(message)
        self.head = head


class Gap(IngestError):
    """The batch starts after the store's head."""


class Diverged(IngestError):
    """The batch holds an event at a seq the store has with another hash."""


class Invalid(IngestError):
    """A line is not the valid next event."""


class MissingBlobs(IngestError):
    def __init__(self, missing: list[str], head: Head) -> None:
        super().__init__(f"{len(missing)} referenced blob(s) are not stored", head)
        self.missing = missing


def split_lines(body: bytes) -> list[bytes]:
    """The lines of a log excerpt, each with its newline. A last line without one is kept as is,
    so the chain check reports it."""
    parts = body.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


@dataclass(frozen=True, slots=True)
class Verification:
    """`torn` says the only fault in the chain is a last line still being written."""

    run_id: str
    events: int
    head: str
    finished: bool
    error: str | None = None
    missing_blobs: tuple[str, ...] = ()
    bad_blobs: tuple[str, ...] = ()
    torn: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_blobs and not self.bad_blobs


class EventStore(Protocol):
    async def create_run(
        self, started: RunStarted, *, blobs: Sequence[bytes] = (), run_id: str | None = None
    ) -> str:
        """Start a run with `started` as its first event, under `run_id` when given. `blobs` are
        stored before it, so refs to them in `started` resolve from the start."""
        ...

    async def append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]: ...

    async def put_blob(self, run_id: str, data: bytes) -> BlobRef: ...

    async def put_blobs(self, run_id: str, datas: Sequence[bytes]) -> list[BlobRef]: ...

    async def get_blob(self, run_id: str, sha256: str) -> bytes: ...

    async def content(self, run_id: str, text: str) -> Content: ...

    def stream(self, run_id: str, *, after: int = -1) -> AsyncIterator[Event]: ...

    async def run(self, run_id: str) -> RunInfo | None: ...

    async def runs(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        severity: int | None = None,
        category: str | None = None,
    ) -> list[RunInfo]: ...

    async def events(
        self,
        run_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
        types: Sequence[str] = (),
        agent_id: str | None = None,
        query: str | None = None,
    ) -> list[Event]: ...

    async def flags(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        layer: str | None = None,
        category: str | None = None,
        min_importance: int | None = None,
        status: Sequence[str] = (),
        agent_id: str | None = None,
        tainted: bool | None = None,
    ) -> list[FlagInfo]: ...

    async def samples(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
        after: int | None = None,
        limit: int = MAX_PAGE,
    ) -> list[SampleWindow]: ...

    async def verify(self, run_id: str) -> Verification: ...

    async def wait_indexed(self, run_id: str, seq: int) -> None: ...

    async def rebuild(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _Writer:
    """`cut` says lines of a batch that failed may be in the log past `head`."""

    file: LogFile
    head: Head
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    cut: bool = False


@dataclass(slots=True)
class _Lag:
    """A run whose index stopped short of its log: its failed tries in a row, the wait before
    the next one, the loop time it is due and the task that makes it."""

    tries: int = 1
    wait: float = 0.0
    due: float = 0.0
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Sink:
    """Write handle on a run another process writes, whose lines arrive by `ingest`."""

    file: LogFile
    head: Head
    blobs: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class LocalEventStore:
    """Run logs under `root/<run_id>/`, indexed in `root/index.sqlite`.

    One store instance is the only writer of a run: it holds a flock on the log while open.
    A run is written here by `append`, or copied here line by line from its writer by `ingest`.
    An `ingest_only` store never appends, so every copy it holds stays byte for byte the
    writer's.

    On Linux, once `append` or `ingest` returns, its lines, the run directory and log they sit
    in, and every blob they reference survive a crash or power loss, provided the blobs were
    stored before the lines were written. On macOS `os.fsync` does not flush the drive cache,
    so the same holds after a process crash or kernel panic but not after power loss.
    F_FULLFSYNC would flush it, at about 4 ms per append against 0.02 ms.
    """

    def __init__(self, root: Path, index: Index, *, ingest_only: bool = False) -> None:
        self.root = root
        self.ingest_only = ingest_only
        self._index = index
        self._writers: dict[str, _Writer] = {}
        self._sinks: dict[str, _Sink] = {}
        self._ingested: set[str] = set()
        self._signals: dict[str, asyncio.Event] = {}
        self._changed = asyncio.Event()
        self._opening = asyncio.Lock()
        self.listeners: list[Callable[[str], None]] = []
        self._taps: dict[str, list[Callable[[Sequence[Event]], None]]] = {}
        self._lags: dict[str, _Lag] = {}
        self._truths = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, root: Path, *, ingest_only: bool = False) -> LocalEventStore:
        await asyncio.to_thread(make_dir, root, parents=True)
        store = cls(root, await Index.open(root / INDEX), ingest_only=ingest_only)
        try:
            await store._sweep()
            await store.sync()
        except BaseException:
            await store.close()
            raise
        return store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True
        for run_id in list(self._lags):
            await self._drop_lag(run_id)
        for run_id, writer in self._writers.items():
            if writer.cut:
                # the next open would take in what a failed batch left in the log
                try:
                    writer.file.truncate(writer.head.offset)
                except OSError as e:
                    log.error("run %s: a failed batch may stay in the log: %s", run_id, e)
            _close(writer)
        self._writers.clear()
        for sink in self._sinks.values():
            _close(sink)
        self._sinks.clear()
        await self._index.close()

    async def create_run(
        self, started: RunStarted, *, blobs: Sequence[bytes] = (), run_id: str | None = None
    ) -> str:
        """Start a run, under `run_id` when given. Its directory may already hold blobs."""
        if self.ingest_only:
            raise RuntimeError("this store only ingests runs")
        run_id = run_id or str(uuid.uuid7())
        run_dir = self._dir(run_id)
        make_dir(run_dir)
        if blobs:
            await self.put_blobs(run_id, blobs)
        self._log(run_id).touch(exist_ok=False)
        await self.append(run_id, [Draft(started)])
        return run_id

    def prepare_run(self, run_id: str) -> None:
        """Make the directory of a run that is still to start, so its blobs can land first."""
        make_dir(self._dir(run_id))

    async def head(self, run_id: str) -> Head | None:
        """Where the indexed part of a run's log ends."""
        indexed = await self._index.head(run_id)
        if indexed is None:
            return None
        head, status = indexed
        if status == "corrupt":
            raise RunCorrupt(f"run {run_id} is marked corrupt")
        return head

    async def log_head(self, run_id: str) -> Head | None:
        """Where the synced part of a run's log ends: the index head, or past it while the
        index is behind the log."""
        head = await self.head(run_id)
        if run_id not in self._lags:
            return head
        handle = self._writers.get(run_id) or self._sinks.get(run_id)
        if handle is not None:
            # a write sets the handle's head before its fsync, and holds the lock past it
            async with handle.lock:
                if not handle.closed:
                    return handle.head

        def read() -> Head:
            after = head or Head()
            while True:
                chunk, error = _take(self._log(run_id), run_id, after, CHUNK)
                if chunk:
                    after = chunk[-1][1]
                if error is not None or len(chunk) < CHUNK:
                    return after

        found = await asyncio.to_thread(read)
        return found if found.seq >= 0 else head

    async def wait_indexed(self, run_id: str, seq: int) -> None:
        """Wait until the run's index holds event `seq`, while it is behind the log."""
        while True:
            changed = self.changed()
            indexed = await self._index.head(run_id)
            if (-1 if indexed is None else indexed[0].seq) >= seq:
                return
            await changed.wait()

    async def remove(self, run_id: str) -> None:
        """Delete a sealed run's directory and its index rows. An ingest-only store also drops
        a copy whose last lines never came, unless an ingest is writing it.

        The directory is first renamed to a grave, a name no run has, so a crash at any later
        point leaves nothing that reads as the run; the next open finishes the delete. A grave
        that will not go is logged and left for that open."""
        sink = self._sinks.get(run_id)
        if run_id in self._writers or (
            sink is not None and (not self.ingest_only or sink.lock.locked())
        ):
            raise RuntimeError(f"run {run_id} is open for writing")
        if sink is not None:
            self._drop_sink(run_id, sink)
        if not self.ingest_only:
            info = await self._index.run(run_id)
            if info is not None and info.status == "running":
                raise RuntimeError(f"run {run_id} is not sealed")
        self._ingested.discard(run_id)
        await self._drop_lag(run_id)
        grave = await asyncio.to_thread(self._bury, run_id)
        await self._index.remove(run_id)
        if grave is not None:
            await asyncio.to_thread(_clear, grave)

    def _bury(self, run_id: str) -> Path | None:
        """Rename a run's directory to a new grave, durably. None when it has no directory."""
        grave = self.root / f"{GRAVE}{run_id}.{uuid.uuid4().hex[:8]}"
        try:
            self._dir(run_id).rename(grave)
        except FileNotFoundError:
            return None
        sync_dir(self.root)
        return grave

    async def _sweep(self) -> None:
        """Finish the deletes a crash cut short: forget the index rows of each grave's run,
        unless the run was made again since, then delete the grave."""

        def graves() -> list[tuple[Path, str | None]]:
            """Each grave with the run whose index rows go: None when the run is here again."""
            out: list[tuple[Path, str | None]] = []
            for path in sorted(self.root.iterdir()):
                if path.name.startswith(GRAVE):
                    run_id = path.name.removeprefix(GRAVE).rpartition(".")[0]
                    gone = is_run_id(run_id) and not (self.root / run_id).exists()
                    out.append((path, run_id if gone else None))
            return out

        for grave, run_id in await asyncio.to_thread(graves):
            if run_id is not None:
                try:
                    await self._index.remove(run_id)
                except Exception as e:
                    log.warning("%s stays until the next open: %s", grave, e)
                    continue
            await asyncio.to_thread(_clear, grave)

    def run_ids(self) -> list[str]:
        """Every run with a log here."""
        return self._run_ids()

    def tap(self, run_id: str, fn: Callable[[Sequence[Event]], None]) -> Callable[[], None]:
        """Call `fn` with each batch this store appends to `run_id`, in seq order, as soon as
        it is written and before `append` returns, whether or not the index took it. `fn` must
        not block. Returns a function that removes the tap."""
        taps = self._taps.setdefault(run_id, [])
        taps.append(fn)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                taps.remove(fn)
            if not taps and self._taps.get(run_id) is taps:
                del self._taps[run_id]

        return remove

    def changed(self) -> asyncio.Event:
        """Set once any run gets events after this call. Take it before reading, then wait."""
        return self._changed

    async def lines(
        self,
        run_id: str,
        head: Head,
        *,
        upto: int,
        max_events: int = CHUNK,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> tuple[list[bytes], Head]:
        """The exact log lines after `head`, up to seq `upto`, as many as fit the caps, and the
        head after the last one."""

        def read() -> tuple[list[bytes], Head]:
            out: list[bytes] = []
            size = 0
            after = head
            if after.seq >= upto:
                return out, after
            for line, _, next_head in read_lines(self._log(run_id), run_id, head):
                if out and size + len(line) > max_bytes:
                    break
                out.append(line)
                size += len(line)
                after = next_head
                if after.seq >= upto or len(out) >= max_events:
                    break
            return out, after

        return await asyncio.to_thread(read)

    async def seek(self, run_id: str, seq: int) -> Head:
        """The verified head of a run's log after event `seq`, scanning from the start."""

        def find() -> Head:
            head = Head()
            if seq < 0:
                return head
            for _, after in read_events(self._log(run_id), run_id, Head()):
                head = after
                if head.seq >= seq:
                    return head
            raise KeyError(f"run {run_id} has no event {seq}")

        return await asyncio.to_thread(find)

    async def ingest(self, run_id: str, lines: Sequence[bytes]) -> Head:
        """Copy log lines written by another store. Lines this store already holds are checked
        by hash and skipped; the rest must continue the chain at its head, and every blob they
        reference must be stored first. Completes even if cancelled."""
        return await asyncio.shield(self._ingest(run_id, lines))

    async def _ingest(self, run_id: str, lines: Sequence[bytes]) -> Head:
        if run_id in self._writers:
            raise RuntimeError(f"run {run_id} is written here, not ingested")
        sink = await self._sink(run_id)
        async with sink.lock:
            if sink.closed:
                raise RuntimeError(f"ingest handle for run {run_id} was closed after an I/O error")
            head = sink.head
            if not lines:
                return head
            fresh = await self._overlap(run_id, head, lines)
            if not fresh:
                return head
            if head.finished:
                raise Invalid("the run is already finished", head)
            events, after, error = await asyncio.to_thread(_follow_all, head, fresh, run_id)
            if error is not None:
                raise error
            refs = {sha for e in events for sha in blob_refs(e.data)} - sink.blobs
            if refs:
                missing = await asyncio.to_thread(self._blobs(run_id).missing, sorted(refs))
                sink.blobs.update(refs - set(missing))
                if missing:
                    raise MissingBlobs(missing, head)
            try:
                sink.file.write(b"".join(fresh))
                await asyncio.to_thread(sink.file.sync)
            except OSError:
                with contextlib.suppress(OSError):
                    sink.file.truncate(head.offset)
                self._drop_sink(run_id, sink)
                raise
            sink.head = after
            await self._keep_up(run_id, events, after)
            if after.finished:
                self._drop_sink(run_id, sink)
        self._notify(run_id)
        return after

    async def _overlap(self, run_id: str, head: Head, lines: Sequence[bytes]) -> list[bytes]:
        """The lines past `head`, once those at or before it match what is stored."""
        try:
            first = json.loads(lines[0])["seq"]
        except ValueError, KeyError, TypeError:
            raise Invalid("the first line is not an event", head) from None
        if not isinstance(first, int) or first < 0:
            raise Invalid("the first line has no seq", head)
        if first > head.seq + 1:
            raise Gap(f"the batch starts at seq {first}, this copy ends at {head.seq}", head)
        known = min(len(lines), head.seq + 1 - first)
        if known <= 0:
            return list(lines)
        claimed: dict[int, str] = {}
        for i, line in enumerate(lines[:known]):
            try:
                event = json.loads(line)
                seq, hash_ = event["seq"], event["hash"]
            except ValueError, KeyError, TypeError:
                raise Invalid(f"line {i} is not an event", head) from None
            if seq != first + i:
                raise Invalid(f"line {i} has seq {seq}, expected {first + i}", head)
            claimed[seq] = hash_
        stored = await self._hashes(run_id, first, first + known - 1)
        for seq, hash_ in claimed.items():
            if stored.get(seq) != hash_:
                raise Diverged(
                    f"event {seq} differs from the copy here (sent {str(hash_)[:16]},"
                    f" stored {str(stored.get(seq))[:16]})",
                    head,
                )
        return list(lines[known:])

    async def _sink(self, run_id: str) -> _Sink:
        async with self._opening:
            sink = self._sinks.get(run_id)
            if sink is not None:
                return sink
            run_dir = self._dir(run_id)
            await asyncio.to_thread(make_dir, run_dir)
            path = self._log(run_id)
            file = LogFile(path)
            try:
                head, refs, error = await asyncio.to_thread(scan, path, run_id)
                await self._index_log(run_id)
                if error is not None:
                    if not await asyncio.to_thread(tail_only, path, error.offset):
                        raise RunCorrupt(f"run {run_id}: {error}")
                    dropped = await asyncio.to_thread(file.truncate, error.offset)
                    log.info("run %s: cut %d unfinished bytes off the copy", run_id, len(dropped))
            except BaseException:
                file.close()
                raise
            sink = _Sink(file, head, refs - set(self._blobs(run_id).missing(sorted(refs))))
            self._sinks[run_id] = sink
            self._ingested.add(run_id)
            return sink

    def _drop_sink(self, run_id: str, sink: _Sink) -> None:
        _close(sink)
        if self._sinks.get(run_id) is sink:
            del self._sinks[run_id]

    async def append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        """Chain, write, fsync and index `drafts` as one batch. Completes even if cancelled.
        Once the batch is in the log it is returned, even when the index failed to take it: the
        log is the record, and the index catches up from it."""
        if self.ingest_only:
            raise RuntimeError("this store only ingests runs")
        if not drafts:
            return []
        return await asyncio.shield(self._append(run_id, drafts))

    async def _append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        writer = await self._writer(run_id)
        async with writer.lock:
            if writer.cut and not writer.closed:
                await self._cut(run_id, writer)
            if writer.closed:
                raise RuntimeError(f"writer for run {run_id} was closed after an I/O error")
            start = head = writer.head
            events: list[Event] = []
            lines: list[bytes] = []
            for draft in drafts:
                kind, data = draft.payload.type, draft.payload.model_dump(mode="json")
                chain = partial(
                    seal, head, run_id=run_id, ts=now(), agent_id=draft.agent_id, type=kind
                )
                try:
                    body, line, head = chain(data=data)
                except ValueError:
                    # canonical JSON refuses a lone surrogate; seal the same event with it as
                    # text, so the hash covers what is written. Any other error comes again.
                    body, line, head = chain(data=scrub(data))
                    log.warning("run %s: %s held a lone surrogate, written as text", run_id, kind)
                events.append(Event.model_validate(body))
                lines.append(line)
            try:
                writer.file.write(b"".join(lines))
                writer.head = head
                await asyncio.to_thread(writer.file.sync)
            except OSError:
                # lines of the batch may be in the log: the next open would take them in, and a
                # retry would write them twice. A log that will not cut keeps its writer, which
                # cuts it before it writes again. Not in a thread, where a store close could
                # close the file under the cut
                writer.head, writer.cut = start, True
                if not writer.closed:
                    try:
                        writer.file.truncate(start.offset)
                    except OSError as e:
                        log.error("run %s: a failed batch is not cut off the log: %s", run_id, e)
                    else:
                        self._discard(run_id, writer)
                raise
            await self._keep_up(run_id, events, head)
            if head.finished:
                self._discard(run_id, writer)
            for fn in list(self._taps.get(run_id, ())):
                try:
                    fn(events)
                except Exception:
                    log.exception("an append tap of run %s failed", run_id)
        self._notify(run_id)
        return events

    async def _cut(self, run_id: str, writer: _Writer) -> None:
        """Cut the lines of a batch that failed off the open log of `writer`, before it writes
        again. RunCorrupt when another process indexed them while they were there. The cut is
        not run in a thread, where a store close could close the file under it."""
        writer.file.truncate(writer.head.offset)
        await self._index_log(run_id)
        writer.cut = False

    async def put_blob(self, run_id: str, data: bytes) -> BlobRef:
        return await asyncio.to_thread(self._blobs(run_id).put, data)

    async def put_blobs(self, run_id: str, datas: Sequence[bytes]) -> list[BlobRef]:
        return await asyncio.to_thread(self._blobs(run_id).put_many, datas)

    async def put_blob_stream(
        self, run_id: str, sha256: str, chunks: AsyncIterator[bytes], *, limit: int
    ) -> BlobRef:
        """Store a blob file sent as another store holds it, checked against `sha256`."""
        blobs = self._blobs(run_id)
        path = blobs.path(sha256)
        if path.is_file():
            async for _ in chunks:
                pass
            await asyncio.to_thread(sync_dir, blobs.root)
            return BlobRef(sha256=sha256, size=await asyncio.to_thread(stored_size, path))
        writer: BlobWriter = await asyncio.to_thread(blobs.writer, sha256, limit=limit)
        try:
            pending: list[bytes] = []
            size = 0
            async for chunk in chunks:
                pending.append(chunk)
                size += len(chunk)
                if size >= 1024 * 1024:
                    await asyncio.to_thread(writer.write, b"".join(pending))
                    pending, size = [], 0
            if pending:
                await asyncio.to_thread(writer.write, b"".join(pending))
            ref = await asyncio.to_thread(writer.commit)
        except BaseException:
            await asyncio.to_thread(writer.abort)
            raise
        sink = self._sinks.get(run_id)
        if sink is not None:
            sink.blobs.add(sha256)
        return ref

    async def missing_blobs(self, run_id: str, shas: Sequence[str]) -> list[str]:
        return await asyncio.to_thread(self._blobs(run_id).missing, shas)

    def blob_path(self, run_id: str, sha256: str) -> Path:
        """The file of a stored blob, as `put_blob` wrote it. KeyError if it is not stored."""
        path = self._blobs(run_id).path(sha256)
        if not path.is_file():
            raise KeyError(f"run {run_id} has no blob {sha256}")
        return path

    async def get_blob(self, run_id: str, sha256: str) -> bytes:
        return await asyncio.to_thread(self._blobs(run_id).get, sha256)

    async def content(self, run_id: str, text: str) -> Content:
        """Inline short text, store long text as a blob. A lone surrogate is written `\\ud800`."""
        text = scrub(text)
        data = text.encode()
        return text if len(data) <= INLINE_LIMIT else await self.put_blob(run_id, data)

    async def stream(
        self, run_id: str, *, after: int = -1, poll: float = 0.5
    ) -> AsyncIterator[Event]:
        """Events after `after`, then live ones as they land. Ends after `run.finished`."""
        cursor = after
        while True:
            signal = self._signals.setdefault(run_id, asyncio.Event())
            batch = await self._index.events(run_id, after=cursor, limit=MAX_PAGE)
            for event in batch:
                yield event
                cursor = event.seq
                if event.type == "run.finished":
                    return
            if batch:
                continue
            info = await self._index.run(run_id)
            if info is None:
                raise KeyError(f"unknown run {run_id}")
            if info.status != "running" and cursor >= info.events - 1:
                return
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(poll):
                    await signal.wait()

    async def _indexed(self, run_id: str, events: Sequence[Event], head: Head) -> None:
        """Index `events`; the batch that seals the run names its variant from its truth file
        in the same write. A review's first batch also notifies the run it reviews, whose
        `reviews` count just changed."""
        variant = None
        if head.finished and any(e.type == RunFinished.type for e in events):
            truth = await asyncio.to_thread(_read_truth, self._dir(run_id))
            variant = None if truth is None else _variant(truth)
        await self._index.add(events, head, variant=variant)
        first = events[0] if events else None
        if first is not None and first.seq == 0 and isinstance(o := first.data.get("origin"), str):
            for listener in self.listeners:
                listener(o)

    def truth_path(self, run_id: str) -> Path:
        return self._dir(run_id) / TRUTH

    async def put_truth(self, run_id: str, data: bytes) -> bool:
        """Keep the truth file of the sealed run `run_id`, outside its log and blobs, mode 0600,
        and name the run's variant from it. It must hash to the truth run.started committed to.
        Returns True from the one call that kept it, False when the same file was kept before.
        Raises TruthRefused.

        The variant is named before the file is written, so a call that fails leaves no file
        and the next one keeps it, names the variant and returns True."""
        async with self._truths:
            return await self._put_truth(run_id, data)

    async def _put_truth(self, run_id: str, data: bytes) -> bool:
        info = await self._index.run(run_id)
        if info is None:
            raise TruthRefused(f"no run {run_id}", status=404)
        if info.status == "running":
            raise TruthRefused(f"run {run_id} is not sealed yet", status=409)
        [first] = await self._index.events(run_id, limit=1)
        committed = first.data.get("truth")
        digest = hashlib.sha256(data).hexdigest()
        if committed is None:
            raise TruthRefused(f"run {run_id} committed to no truth file", status=409)
        if digest != committed:
            raise TruthRefused(
                f"the truth file hashes to {digest}, run.started committed to {committed}",
                status=422,
            )
        try:
            truth = json.loads(data)
        except ValueError:
            raise TruthRefused("the truth file is not JSON", status=422) from None
        if not isinstance(truth, dict):
            raise TruthRefused("the truth file is not a JSON object", status=422)
        target = self.truth_path(run_id)
        variant = _variant(truth)
        if await asyncio.to_thread(_same_file, target, data):
            # a power cut can lose the index write that named it
            if info.variant != variant:
                await self._index.set_variant(run_id, variant)
            return False
        await self._index.set_variant(run_id, variant)
        await asyncio.to_thread(_write_private, target, data)
        return True

    async def add_label(
        self, run_id: str, flag: int, label: LabelKind, *, by: str, note: str = ""
    ) -> Label:
        """Label the flag at seq `flag` of run `run_id`, in the run's labels file beside its
        log, never in the log itself. Raises KeyError when there is no such flag."""
        events = await self._index.events(run_id, after=flag - 1, before=flag + 1, limit=1)
        if not events or events[0].seq != flag or events[0].type != "monitor.flag":
            raise KeyError(f"run {run_id} has no flag at seq {flag}")
        made = Label(run_id=run_id, flag=flag, label=label, note=note, by=by, at=now())
        line = made.model_dump_json().encode() + b"\n"
        await asyncio.to_thread(_append_line, self._dir(run_id) / LABELS, line)
        await self._index.put_labels([made])
        return made

    async def merge_labels(self, run_id: str, labels: Sequence[Label]) -> int:
        """Keep the labels of `run_id` another store made that this one lacks; returns how
        many were new."""
        path = self._dir(run_id) / LABELS
        held = {(x.flag, x.at, x.by) for x in await asyncio.to_thread(_read_labels, path)}
        fresh = [x for x in labels if (x.flag, x.at, x.by) not in held]
        if fresh:
            data = b"".join(x.model_dump_json().encode() + b"\n" for x in fresh)
            await asyncio.to_thread(_append_line, path, data)
            await self._index.put_labels(fresh)
        return len(fresh)

    def labels_path(self, run_id: str) -> Path:
        return self._dir(run_id) / LABELS

    async def label_history(self, run_id: str) -> list[Label]:
        """Every label the run's labels file holds, oldest first."""
        return await asyncio.to_thread(_read_labels, self.labels_path(run_id))

    async def labels(self, run_id: str) -> dict[int, Label]:
        """The latest label of each labelled flag of `run_id`, by seq."""
        return await self._index.labels(run_id)

    async def score_units(
        self, origins: Sequence[str] | None = None, *, latest: bool = True
    ) -> list[UnitRow]:
        return await self._index.score_units(origins, latest=latest)

    async def made_from(self, run_ids: Sequence[str]) -> list[tuple[str, str]]:
        """The runs made from `run_ids` with their kinds: those whose origin is one of them and
        the scores of the flags of a monitor run among them."""
        return await self._index.made_from(run_ids)

    async def ended_before(self, at: str) -> list[str]:
        """The runs that ended before the timestamp `at`."""
        return await self._index.ended_before(at)

    async def costs(self) -> dict[str, tuple[str, str]]:
        """Each run's cost row for the estimate: the head hash it was read at and its data."""
        return await self._index.costs()

    async def put_costs(self, rows: Sequence[tuple[str, str, str]]) -> None:
        await self._index.put_costs(rows)

    async def run(self, run_id: str) -> RunInfo | None:
        return await self._index.run(run_id)

    async def runs(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        severity: int | None = None,
        category: str | None = None,
        series: str | None = None,
        variant: str | None = None,
        group: str | None = None,
        scenario: str | None = None,
        skip_kinds: Sequence[str] = (),
    ) -> list[RunInfo]:
        """Runs newest first, filtered as `Index.runs` describes."""
        return await self._index.runs(
            scenario=scenario,
            group=group,
            skip_kinds=skip_kinds,
            limit=limit,
            before=before,
            kind=kind,
            origin=origin,
            severity=severity,
            category=category,
            series=series,
            variant=variant,
        )

    async def flags(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        layer: str | None = None,
        category: str | None = None,
        min_importance: int | None = None,
        status: Sequence[str] = (),
        agent_id: str | None = None,
        tainted: bool | None = None,
    ) -> list[FlagInfo]:
        """A page of a run's logical flags with their statuses, in seq order."""
        return await self._index.flags(
            run_id,
            after=after,
            limit=limit,
            layer=layer,
            category=category,
            min_importance=min_importance,
            status=status,
            agent_id=agent_id,
            tainted=tainted,
        )

    async def events(
        self,
        run_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
        types: Sequence[str] = (),
        agent_id: str | None = None,
        query: str | None = None,
        execs: Sequence[str] = (),
    ) -> list[Event]:
        return await self._index.events(
            run_id,
            after=after,
            before=before,
            limit=limit,
            types=types,
            agent_id=agent_id,
            query=query,
            execs=execs,
        )

    async def steps(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        agent_id: str | None = None,
    ) -> list[StepInfo]:
        """A page of a run's agent steps with their tool calls, in seq order."""
        return await self._index.steps(run_id, after=after, limit=limit, agent_id=agent_id)

    async def activity(self, run_id: str, execs: Sequence[str]) -> list[ExecActivity]:
        """What each exec left in the log, counted per event type and file op."""
        return await self._index.activity(run_id, execs)

    async def type_counts(self, run_id: str) -> dict[str, int]:
        """How many events of each type the run holds."""
        return await self._index.type_counts(run_id)

    async def call_costs(self, run_id: str) -> list[CallCost]:
        """What the run's live model calls cost, per role and output schema."""
        return await self._index.call_costs(run_id)

    async def every(self, run_id: str, *, types: Sequence[str] = ()) -> list[Event]:
        """Every event of a run in seq order, read page by page past the index's page cap."""
        out: list[Event] = []
        cursor: int | None = None
        while page := await self.events(run_id, after=cursor, limit=MAX_PAGE, types=types):
            out.extend(page)
            cursor = page[-1].seq
        return out

    async def samples(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
        after: int | None = None,
        limit: int = MAX_PAGE,
    ) -> list[SampleWindow]:
        """The resource timeline of a run, one row per sandbox second, by start then seq.
        `after` is the seq of the last window of the page before."""
        return await self._index.samples(
            run_id, agent_id=agent_id, ts_from=ts_from, ts_to=ts_to, after=after, limit=limit
        )

    async def verify(self, run_id: str) -> Verification:
        """Check a run's log and blobs as `verify_run` does, then its index: every event up to
        the last it holds, and all the log holds once an index write failed here. A shorter
        index is not an error otherwise, since a write may be under way.

        The index head is read before the log. A store writes each batch to the log before it
        indexes it, so the log of a live run is at or past that head, whichever process
        writes it, and must hold the head's event where the index says."""
        indexed = await self._index.head(run_id)
        head = None if indexed is None else indexed[0]
        result = await asyncio.to_thread(
            verify_run, self._dir(run_id), where=f"the folder of run {run_id}"
        )
        if result.error is not None:
            return result
        held = 0 if head is None else head.seq + 1
        error = None
        if held > result.events:
            error = f"log has {result.events} events, the index recorded {held}"
        elif head is not None and not await asyncio.to_thread(
            checkpoint_ok, self._log(run_id), head
        ):
            error = "the last event the index holds is not the log's"
        elif (count := await self._index.count(run_id, held - 1)) != held:
            error = f"the index has {count} of the {held} events up to its last: it has a gap"
        elif held < result.events and run_id in self._lags:
            error = f"the index holds {held} of the log's {result.events} events"
        return result if error is None else replace(result, error=error)

    async def rebuild(self) -> None:
        """Drop the index and rebuild it from every run log, accepting each log as it is."""
        await self._index.reset()
        await self.sync()

    async def sync(self) -> None:
        """Index whatever the run logs hold beyond the index, and name each variant it lost. A
        run the index cannot take is logged and caught up later, and the other runs go on."""
        await self._lost_variants()
        for run_id in self._run_ids():
            try:
                async with self._lock_of(run_id):
                    caught = await self._index_log(run_id)
            except RunCorrupt as e:
                log.error("%s", e)
                continue
            error = None if caught is None else caught[1]
            if error is not None and not (
                error.reason == "incomplete line" and tail_only(self._log(run_id), error.offset)
            ):
                log.warning("run %s: %s", run_id, error)
            labels = await asyncio.to_thread(_read_labels, self._dir(run_id) / LABELS)
            try:
                await self._index.put_labels(labels)
            except Exception as e:
                log.error("run %s: its labels are not indexed: %s", run_id, e)

    async def _lost_variants(self) -> None:
        """Name the variant of each run the index holds sealed with none, from its truth file.
        `put_truth` names it in an index write after the seal, which a power cut can lose, and
        a catch-up does not index the seal again. A variant the index names came from that
        file, which the truth hash in run.started fixes, so those files are not read."""
        unnamed = await self._index.unnamed()

        def read() -> dict[str, str]:
            found: dict[str, str] = {}
            for run_id in unnamed:
                truth = _read_truth(self._dir(run_id)) if is_run_id(run_id) else None
                variant = None if truth is None else _variant(truth)
                if variant is not None:
                    found[run_id] = variant
            return found

        for run_id, variant in (await asyncio.to_thread(read)).items():
            try:
                await self._index.set_variant(run_id, variant)
            except Exception as e:
                log.error("run %s: its variant is not indexed: %s", run_id, e)

    async def _writer(self, run_id: str) -> _Writer:
        async with self._opening:
            writer = self._writers.get(run_id)
            if writer is not None:
                return writer
            if run_id in self._ingested:
                raise RuntimeError(f"run {run_id} is ingested here, not written")
            path = self._log(run_id)
            if not path.is_file():
                raise KeyError(f"unknown run {run_id}")
            file = LogFile(path)
            truncated: LogTruncated | None = None
            try:
                head, _, error = await asyncio.to_thread(scan, path, run_id)
                await self._index_log(run_id)
                if error is not None:
                    if not await asyncio.to_thread(tail_only, path, error.offset):
                        raise error
                    # stored before they are cut, so a full disk or a crash in between loses
                    # nothing: the next open finds the same tail
                    dropped = await asyncio.to_thread(file.tail, error.offset)
                    ref = await self.put_blob(run_id, dropped)
                    await asyncio.to_thread(file.truncate, error.offset)
                    log.warning("run %s: cut %d bytes off the log: %s", run_id, len(dropped), error)
                    truncated = LogTruncated(offset=error.offset, dropped=ref, reason=error.reason)
            except BaseException:
                file.close()
                raise
            writer = _Writer(file, head)
            self._writers[run_id] = writer
        if truncated is not None and head.seq >= 0 and not head.finished:
            await self._append(run_id, [Draft(truncated)])
        return writer

    async def _catch_up(self, run_id: str) -> tuple[Head, ChainError | None]:
        """Index verified events past the index head, up to the first one the index cannot take.
        RunCorrupt if the log lost that head."""
        path = self._log(run_id)
        head = Head()
        indexed = await self._index.head(run_id)
        if indexed is not None:
            head, status = indexed
            if status == "corrupt" or not await asyncio.to_thread(checkpoint_ok, path, head):
                if status != "corrupt":
                    await self._index.mark_corrupt(run_id)
                raise RunCorrupt(
                    f"run {run_id}: the log no longer holds event {head.seq} recorded by the index;"
                    " rebuild the index to accept the log as it is"
                )
        while True:
            chunk, error = await asyncio.to_thread(_take, path, run_id, head, CHUNK)
            if chunk:
                try:
                    events = [Event.model_validate(e) for e, _ in chunk]
                    await self._indexed(run_id, events, chunk[-1][1])
                    whole = True
                except Exception:
                    if len(chunk) == 1:
                        raise
                    whole = False
                if not whole:
                    # one event the index cannot take would hold back those before it
                    for e, after in chunk:
                        await self._indexed(run_id, [Event.model_validate(e)], after)
                head = chunk[-1][1]
            if error is not None or len(chunk) < CHUNK:
                return head, error

    async def _keep_up(self, run_id: str, events: Sequence[Event], head: Head) -> None:
        """Index a batch that reached the log. A run whose index is behind is caught up from the
        log instead, when a try is due, so no batch is indexed past a gap. Never raises: a
        failure leaves the run behind, and the log holds the batch either way."""
        lag = self._lags.get(run_id)
        if lag is None:
            try:
                await self._indexed(run_id, events, head)
            except Exception as e:
                self._behind(run_id, e)
        elif asyncio.get_running_loop().time() >= lag.due:
            try:
                await self._index_log(run_id)
            except RunCorrupt as e:
                log.error("%s", e)

    async def _index_log(self, run_id: str) -> tuple[Head, ChainError | None] | None:
        """`_catch_up`, where a failure leaves the run behind instead of raising and gives None.
        RunCorrupt still raises: the log lost what the index holds."""
        try:
            caught = await self._catch_up(run_id)
        except RunCorrupt:
            self._caught_up(run_id)
            raise
        except Exception as e:
            self._behind(run_id, e)
            return None
        lag = self._caught_up(run_id)
        if lag is not None:
            log.warning(
                "run %s: the index caught up with the log after %d tries", run_id, lag.tries
            )
            self._notify(run_id)
        return caught

    def _behind(self, run_id: str, error: Exception) -> None:
        """Note that a run's index stopped short of its log. The run's next write catches it up
        from the log, and so does a task: at once, then after waits that double from RETRY_S to
        RETRY_MAX_S. The first failure in a row logs an error, the later ones a warning each
        time their count doubles."""
        if self._closed:
            return
        lag = self._lags.get(run_id)
        if lag is None:
            log.error(
                "run %s: the index stopped short of the log, which holds every event; it catches"
                " up from the log",
                run_id,
                exc_info=error,
            )
            lag = self._lags[run_id] = _Lag()
            lag.task = asyncio.create_task(self._heal(run_id, lag))
            return
        lag.tries += 1
        lag.wait = min(max(lag.wait * 2, RETRY_S), RETRY_MAX_S)
        lag.due = asyncio.get_running_loop().time() + lag.wait
        if lag.tries & (lag.tries - 1) == 0:
            log.warning(
                "run %s: the index is still behind the log after %d tries: %s",
                run_id,
                lag.tries,
                error,
            )

    async def _heal(self, run_id: str, lag: _Lag) -> None:
        """Catch a lagging run's index up from its log at each time `lag` is due, under the lock
        of its writer or sink, until it works or the lag is dropped."""
        loop = asyncio.get_running_loop()
        while self._lags.get(run_id) is lag:
            await asyncio.sleep(max(0.0, lag.due - loop.time()))
            if self._lags.get(run_id) is not lag or loop.time() < lag.due:
                continue
            async with self._lock_of(run_id):
                if self._lags.get(run_id) is lag:
                    try:
                        await self._index_log(run_id)
                    except RunCorrupt as e:
                        log.error("%s", e)

    def _caught_up(self, run_id: str) -> _Lag | None:
        """Forget a run's lag and stop its task, unless that task is the caller."""
        lag = self._lags.pop(run_id, None)
        if lag is not None and lag.task is not None and lag.task is not asyncio.current_task():
            lag.task.cancel()
        return lag

    async def _drop_lag(self, run_id: str) -> None:
        """Forget a run's lag and wait until its task has stopped."""
        lag = self._caught_up(run_id)
        if lag is not None and lag.task is not None and lag.task is not asyncio.current_task():
            await asyncio.gather(lag.task, return_exceptions=True)

    def _lock_of(self, run_id: str) -> contextlib.AbstractAsyncContextManager[Any]:
        """The lock of the run's writer or sink, which orders a catch-up with its writes."""
        handle = self._writers.get(run_id) or self._sinks.get(run_id)
        return contextlib.nullcontext() if handle is None else handle.lock

    async def _hashes(self, run_id: str, first: int, last: int) -> dict[int, str]:
        """The hash of each logged event of a run from seq `first` to `last`: from the index, and
        from the log past the index head while the index is behind."""
        stored = await self._index.hashes(run_id, first, last)
        if len(stored) > last - first:
            return stored
        indexed = await self._index.head(run_id)
        start = Head() if indexed is None else indexed[0]

        def read() -> dict[int, str]:
            out: dict[int, str] = {}
            after = start
            while after.seq < last:
                chunk, error = _take(self._log(run_id), run_id, after, CHUNK)
                for _, after in chunk:
                    if first <= after.seq <= last:
                        out[after.seq] = after.hash
                if error is not None or len(chunk) < CHUNK:
                    break
            return out

        return await asyncio.to_thread(read) | stored

    def _notify(self, run_id: str) -> None:
        signal = self._signals.pop(run_id, None)
        if signal is not None:
            signal.set()
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        for listener in self.listeners:
            listener(run_id)

    def _discard(self, run_id: str, writer: _Writer) -> None:
        _close(writer)
        if self._writers.get(run_id) is writer:
            del self._writers[run_id]

    def _run_ids(self) -> list[str]:
        names = (p.name for p in self.root.iterdir() if (p / LOG).is_file())
        return sorted(name for name in names if is_run_id(name))

    def _dir(self, run_id: str) -> Path:
        if not is_run_id(run_id):
            raise KeyError(f"not a run id: {run_id!r}")
        return self.root / run_id

    def _log(self, run_id: str) -> Path:
        return self._dir(run_id) / LOG

    def blobs(self, run_id: str) -> BlobStore:
        """The blob store of a run whose directory exists."""
        return self._blobs(run_id)

    def _blobs(self, run_id: str) -> BlobStore:
        run_dir = self._dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(f"unknown run {run_id}")
        return BlobStore(run_dir / BLOBS)

    async def search(
        self,
        query: str,
        *,
        limit: int = 100,
        before: int | None = None,
        skip_kinds: Sequence[str] = (),
    ) -> tuple[list[Event], int | None]:
        """Events of every run whose text holds `query`, newest first, and the cursor of the
        next page, leaving out runs of `skip_kinds`."""
        return await self._index.search(query, limit=limit, before=before, skip_kinds=skip_kinds)


def verify_run(run_dir: Path, *, where: str | None = None) -> Verification:
    """Check the whole chain of one run and every blob it references. `where` names the folder
    in an error, for a reader who must not learn the path."""
    run_id = run_dir.name
    try:
        head, refs, chain_error = scan(run_dir / LOG, run_id)
    except FileNotFoundError:
        head, refs, chain_error = Head(), set(), None
        error: str | None = f"no {LOG} in {where or run_dir}"
    else:
        error = None if chain_error is None else str(chain_error)
    torn = (
        chain_error is not None
        and chain_error.reason == "incomplete line"
        and tail_only(run_dir / LOG, chain_error.offset)
    )
    if error is None and head.seq < 0:
        error = "log is empty"
    missing: list[str] = []
    bad: list[str] = []
    for sha256 in sorted(refs):
        path = run_dir / BLOBS / sha256
        if not path.is_file():
            missing.append(sha256)
        elif read_blob(path) is None:
            bad.append(sha256)
    return Verification(
        run_id=run_id,
        events=head.seq + 1,
        head=head.hash,
        finished=head.finished,
        error=error,
        missing_blobs=tuple(missing),
        bad_blobs=tuple(bad),
        torn=torn,
    )


def scan(path: Path, run_id: str) -> tuple[Head, set[str], ChainError | None]:
    """Verify a whole log. Returns the last good head, the blobs referenced and the first error."""
    head = Head()
    refs: set[str] = set()
    try:
        for event, after in read_events(path, run_id, Head()):
            refs.update(blob_refs(event["data"]))
            head = after
    except ChainError as e:
        return head, refs, e
    return head, refs, None


def _follow_all(
    head: Head, lines: Sequence[bytes], run_id: str
) -> tuple[list[Event], Head, IngestError | None]:
    start = head
    events: list[Event] = []
    for line in lines:
        try:
            body, head = follow(head, line, run_id=run_id)
            event = Event.model_validate(body)
            _check_known(event)
        except ChainError as e:
            kind = Diverged if e.reason.startswith("prev_hash") else Invalid
            return [], start, kind(f"event {e.seq}: {e.reason}", start)
        except ValueError as e:
            return [], start, Invalid(f"event {head.seq}: {e}", start)
        events.append(event)
    return events, head, None


def _check_known(event: Event) -> None:
    """The index reads the time of every event in UTC and these two payloads, so they must be
    valid."""
    try:
        event.ts.astimezone(UTC)
    except OverflowError:
        raise ValueError("its time is past what UTC holds") from None
    if event.type == RunStarted.type:
        RunStarted.model_validate(event.data)
    elif event.type == RunFinished.type:
        RunFinished.model_validate(event.data)


def _take(
    path: Path, run_id: str, head: Head, n: int
) -> tuple[list[tuple[dict[str, Any], Head]], ChainError | None]:
    """Up to `n` verified events of a log after `head`, and the error that cut them short."""
    out: list[tuple[dict[str, Any], Head]] = []
    events = read_events(path, run_id, head)
    try:
        for item in events:
            out.append(item)
            if len(out) == n:
                break
    except ChainError as e:
        return out, e
    finally:
        events.close()
    return out, None


def _clear(grave: Path) -> None:
    """Delete a grave; one that will not go is logged and left for the next open."""
    try:
        shutil.rmtree(grave)
    except OSError as e:
        if grave.exists():
            log.warning("%s stays until the next open: %s", grave, e)


def _close(writer: _Writer | _Sink) -> None:
    if not writer.closed:
        writer.closed = True
        writer.file.close()


def is_run_id(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


def _read_truth(run_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((run_dir / TRUTH).read_bytes())
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _variant(truth: dict[str, Any]) -> str | None:
    variant = truth.get("variant")
    return variant if isinstance(variant, str) else None


def _same_file(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def _write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path` mode 0600, whole or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sync_dir(path.parent)


def _append_line(path: Path, line: bytes) -> None:
    """Append `line`, which ends with a newline, to `path`, whole or not at all. A tail an
    earlier crash left without its newline gets one first, so `line` stays a line of its own."""
    new = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        try:
            write_all(fd, line)
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, size)
            raise
    finally:
        os.close(fd)
    if new:
        sync_dir(path.parent)


def _read_labels(path: Path) -> list[Label]:
    """The labels a run's labels file holds; lines that do not read are skipped."""
    try:
        text = path.read_text()
    except OSError:
        return []
    out: list[Label] = []
    for line in text.splitlines():
        try:
            out.append(Label.model_validate_json(line))
        except ValueError:
            log.warning("%s: a line does not read as a label", path)
    return out
