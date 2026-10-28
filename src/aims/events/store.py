import asyncio
import contextlib
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Generator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, Self

from aims.events.blobs import BlobStore, BlobWriter, read_blob, stored_size
from aims.events.chain import ChainError, Head, follow, seal
from aims.events.index import MAX_PAGE, FlagInfo, Index, RunInfo, SampleWindow
from aims.events.log import LogFile, checkpoint_ok, read_events, read_lines, tail_only
from aims.events.payloads import LogTruncated, RunFinished, RunStarted
from aims.events.schema import BlobRef, Content, Draft, Event, blob_refs, now

log = logging.getLogger("aims.events")

LOG = "events.jsonl"
BLOBS = "blobs"
INDEX = "index.sqlite"
INLINE_LIMIT = 8 * 1024
CHUNK = 1000


class RunCorrupt(Exception):
    pass


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
    run_id: str
    events: int
    head: str
    finished: bool
    error: str | None = None
    missing_blobs: tuple[str, ...] = ()
    bad_blobs: tuple[str, ...] = ()

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
        limit: int = MAX_PAGE,
    ) -> list[SampleWindow]: ...

    async def verify(self, run_id: str) -> Verification: ...

    async def rebuild(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _Writer:
    file: LogFile
    head: Head
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


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

    @classmethod
    async def open(cls, root: Path, *, ingest_only: bool = False) -> LocalEventStore:
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        store = cls(root, await Index.open(root / INDEX), ingest_only=ingest_only)
        try:
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
        for writer in self._writers.values():
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
        run_dir.mkdir(exist_ok=True)
        if blobs:
            await self.put_blobs(run_id, blobs)
        self._log(run_id).touch(exist_ok=False)
        await self.append(run_id, [Draft(started)])
        return run_id

    def prepare_run(self, run_id: str) -> None:
        """Make the directory of a run that is still to start, so its blobs can land first."""
        self._dir(run_id).mkdir(exist_ok=True)

    async def head(self, run_id: str) -> Head | None:
        """Where the indexed part of a run's log ends."""
        indexed = await self._index.head(run_id)
        if indexed is None:
            return None
        head, status = indexed
        if status == "corrupt":
            raise RunCorrupt(f"run {run_id} is marked corrupt")
        return head

    async def remove(self, run_id: str) -> None:
        """Delete a sealed run's directory and its index rows."""
        if run_id in self._writers or run_id in self._sinks:
            raise RuntimeError(f"run {run_id} is open for writing")
        info = await self._index.run(run_id)
        if info is not None and info.status == "running":
            raise RuntimeError(f"run {run_id} is not sealed")
        await self._index.remove(run_id)
        await asyncio.to_thread(shutil.rmtree, self._dir(run_id), True)

    def run_ids(self) -> list[str]:
        """Every run with a log here."""
        return self._run_ids()

    def tap(self, run_id: str, fn: Callable[[Sequence[Event]], None]) -> Callable[[], None]:
        """Call `fn` with each batch this store appends to `run_id`, in seq order, as soon as
        it is written and indexed and before `append` returns. `fn` must not block. Returns a
        function that removes the tap."""
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
            await self._index.add(events, after)
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
        stored = await self._index.hashes(run_id, first, first + known - 1)
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
            await asyncio.to_thread(run_dir.mkdir, exist_ok=True)
            path = self._log(run_id)
            file = LogFile(path)
            try:
                head, refs, error = await asyncio.to_thread(scan, path, run_id)
                await self._catch_up(run_id)
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
        """Chain, write, fsync and index `drafts` as one batch. Completes even if cancelled."""
        if self.ingest_only:
            raise RuntimeError("this store only ingests runs")
        if not drafts:
            return []
        return await asyncio.shield(self._append(run_id, drafts))

    async def _append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        writer = await self._writer(run_id)
        async with writer.lock:
            if writer.closed:
                raise RuntimeError(f"writer for run {run_id} was closed after an I/O error")
            head = writer.head
            events: list[Event] = []
            lines: list[bytes] = []
            for draft in drafts:
                body, line, head = seal(
                    head,
                    run_id=run_id,
                    ts=now(),
                    agent_id=draft.agent_id,
                    type=draft.payload.type,
                    data=draft.payload.model_dump(mode="json"),
                )
                events.append(Event.model_validate(body))
                lines.append(line)
            try:
                writer.file.write(b"".join(lines))
            except OSError:
                with contextlib.suppress(OSError):
                    writer.file.truncate(writer.head.offset)
                self._discard(run_id, writer)
                raise
            writer.head = head
            try:
                await asyncio.to_thread(writer.file.sync)
            except OSError:
                self._discard(run_id, writer)
                raise
            await self._index.add(events, head)
            if head.finished:
                self._discard(run_id, writer)
            for fn in list(self._taps.get(run_id, ())):
                try:
                    fn(events)
                except Exception:
                    log.exception("an append tap of run %s failed", run_id)
        self._notify(run_id)
        return events

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
        """Inline short text, store long text as a blob."""
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
    ) -> list[RunInfo]:
        """Runs newest first, filtered as `Index.runs` describes."""
        return await self._index.runs(
            limit=limit,
            before=before,
            kind=kind,
            origin=origin,
            severity=severity,
            category=category,
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
    ) -> list[Event]:
        return await self._index.events(
            run_id,
            after=after,
            before=before,
            limit=limit,
            types=types,
            agent_id=agent_id,
            query=query,
        )

    async def samples(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
        limit: int = MAX_PAGE,
    ) -> list[SampleWindow]:
        """The resource timeline of a run, one row per sandbox second."""
        return await self._index.samples(
            run_id, agent_id=agent_id, ts_from=ts_from, ts_to=ts_to, limit=limit
        )

    async def verify(self, run_id: str) -> Verification:
        result = await asyncio.to_thread(verify_run, self._dir(run_id))
        indexed = await self._index.head(run_id)
        if result.error is None and indexed is not None and indexed[0].seq + 1 > result.events:
            error = f"log has {result.events} events, the index recorded {indexed[0].seq + 1}"
            return replace(result, error=error)
        return result

    async def rebuild(self) -> None:
        """Drop the index and rebuild it from every run log, accepting each log as it is."""
        await self._index.reset()
        await self.sync()

    async def sync(self) -> None:
        """Index whatever the run logs hold beyond the index."""
        for run_id in self._run_ids():
            writer = self._writers.get(run_id)
            try:
                if writer is None:
                    _, error = await self._catch_up(run_id)
                else:
                    async with writer.lock:
                        _, error = await self._catch_up(run_id)
            except RunCorrupt as e:
                log.error("%s", e)
                continue
            if error is not None and not (
                error.reason == "incomplete line" and tail_only(self._log(run_id), error.offset)
            ):
                log.warning("run %s: %s", run_id, error)

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
                await self._catch_up(run_id)
                if error is not None:
                    if not await asyncio.to_thread(tail_only, path, error.offset):
                        raise error
                    dropped = await asyncio.to_thread(file.truncate, error.offset)
                    ref = await self.put_blob(run_id, dropped)
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
        """Index verified events past the index head. RunCorrupt if the log lost that head."""
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
        events = read_events(path, run_id, head)
        try:
            while True:
                chunk, error = await asyncio.to_thread(_take, events, CHUNK)
                if chunk:
                    head = chunk[-1][1]
                    await self._index.add([Event.model_validate(e) for e, _ in chunk], head)
                if error is not None or len(chunk) < CHUNK:
                    return head, error
        finally:
            events.close()

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
        self, query: str, *, limit: int = 100, before: int | None = None
    ) -> tuple[list[Event], int | None]:
        """Events of every run whose text holds `query`, newest first, and the cursor of the
        next page."""
        return await self._index.search(query, limit=limit, before=before)


def verify_run(run_dir: Path) -> Verification:
    """Check the whole chain of one run and every blob it references."""
    run_id = run_dir.name
    try:
        head, refs, chain_error = scan(run_dir / LOG, run_id)
    except FileNotFoundError:
        head, refs, chain_error = Head(), set(), None
        error: str | None = f"no {LOG} in {run_dir}"
    else:
        error = None if chain_error is None else str(chain_error)
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
    """The index reads these two payloads, so they must be valid."""
    if event.type == RunStarted.type:
        RunStarted.model_validate(event.data)
    elif event.type == RunFinished.type:
        RunFinished.model_validate(event.data)


def _take(
    events: Generator[tuple[dict[str, Any], Head]], n: int
) -> tuple[list[tuple[dict[str, Any], Head]], ChainError | None]:
    out: list[tuple[dict[str, Any], Head]] = []
    try:
        for item in events:
            out.append(item)
            if len(out) == n:
                break
    except ChainError as e:
        return out, e
    return out, None


def _close(writer: _Writer | _Sink) -> None:
    if not writer.closed:
        writer.closed = True
        writer.file.close()


def is_run_id(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False
