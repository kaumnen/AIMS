import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator, Generator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, Self

from aims.events.blobs import BlobStore
from aims.events.chain import ChainError, Head, seal
from aims.events.index import MAX_PAGE, Index, RunInfo
from aims.events.log import LogFile, checkpoint_ok, read_events, tail_only
from aims.events.payloads import LogTruncated, RunStarted
from aims.events.schema import BlobRef, Content, Draft, Event, blob_refs, now

log = logging.getLogger("aims.events")

LOG = "events.jsonl"
BLOBS = "blobs"
INDEX = "index.sqlite"
INLINE_LIMIT = 8 * 1024
CHUNK = 1000


class RunCorrupt(Exception):
    pass


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
    async def create_run(self, started: RunStarted) -> str: ...

    async def append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]: ...

    async def put_blob(self, run_id: str, data: bytes) -> BlobRef: ...

    async def get_blob(self, run_id: str, sha256: str) -> bytes: ...

    async def content(self, run_id: str, text: str) -> Content: ...

    def stream(self, run_id: str, *, after: int = -1) -> AsyncIterator[Event]: ...

    async def run(self, run_id: str) -> RunInfo | None: ...

    async def runs(self, *, limit: int = 50, before: str | None = None) -> list[RunInfo]: ...

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

    async def verify(self, run_id: str) -> Verification: ...

    async def rebuild(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _Writer:
    file: LogFile
    head: Head
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class LocalEventStore:
    """Run logs under `root/<run_id>/`, indexed in `root/index.sqlite`.

    One store instance is the only writer of a run: it holds a flock on the log while open.
    """

    def __init__(self, root: Path, index: Index) -> None:
        self.root = root
        self._index = index
        self._writers: dict[str, _Writer] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._opening = asyncio.Lock()

    @classmethod
    async def open(cls, root: Path) -> LocalEventStore:
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        store = cls(root, await Index.open(root / INDEX))
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
        await self._index.close()

    async def create_run(self, started: RunStarted) -> str:
        run_id = str(uuid.uuid7())
        self._dir(run_id).mkdir()
        self._log(run_id).touch()
        await self.append(run_id, [Draft(started)])
        return run_id

    async def append(self, run_id: str, drafts: Sequence[Draft]) -> list[Event]:
        """Chain, write, fsync and index `drafts` as one batch. Completes even if cancelled."""
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
        self._notify(run_id)
        return events

    async def put_blob(self, run_id: str, data: bytes) -> BlobRef:
        return await asyncio.to_thread(self._blobs(run_id).put, data)

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

    async def runs(self, *, limit: int = 50, before: str | None = None) -> list[RunInfo]:
        return await self._index.runs(limit=limit, before=before)

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

    def _discard(self, run_id: str, writer: _Writer) -> None:
        _close(writer)
        if self._writers.get(run_id) is writer:
            del self._writers[run_id]

    def _run_ids(self) -> list[str]:
        names = (p.name for p in self.root.iterdir() if (p / LOG).is_file())
        return sorted(name for name in names if _is_run_id(name))

    def _dir(self, run_id: str) -> Path:
        if not _is_run_id(run_id):
            raise KeyError(f"not a run id: {run_id!r}")
        return self.root / run_id

    def _log(self, run_id: str) -> Path:
        return self._dir(run_id) / LOG

    def _blobs(self, run_id: str) -> BlobStore:
        run_dir = self._dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(f"unknown run {run_id}")
        return BlobStore(run_dir / BLOBS)


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
        elif hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
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


def _close(writer: _Writer) -> None:
    if not writer.closed:
        writer.closed = True
        writer.file.close()


def _is_run_id(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False
