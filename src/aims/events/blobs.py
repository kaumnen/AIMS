import codecs
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from aims.events.disk import make_dir, sync_dir
from aims.events.schema import BlobRef, is_sha256


class BlobError(Exception):
    pass


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CHUNK = 1024 * 1024
# the name of the file a blob is written to before it is put in place
TEMP = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{8}")
# The most zstd frames a blob file is read as. Past them it is read as is, as a file that does
# not decode is. A frame costs about 2 µs to decode, so a file of tiny frames takes 8 ms of
# decoding at most, and the rest of it is only hashed. Every store writes one frame; pzstd
# writes one per 4 MiB or so, about 1000 at IMPORT_LIMIT.
MAX_FRAMES = 4096


def compress_bound(size: int) -> int:
    """The most bytes zstd writes for `size` bytes of input, as ZSTD_COMPRESSBOUND gives it. A
    blob file is never larger than this bound of the blob it holds."""
    return size + (size >> 8) + (((128 << 10) - size) >> 11 if size < 128 << 10 else 0)


def decode(raw: bytes) -> bytes:
    """The bytes a blob file holds. Files are zstd frames; older ones are stored as is."""
    data = _frames(raw) if raw.startswith(ZSTD_MAGIC) else None
    return raw if data is None else data


def _frames(raw: bytes, limit: int | None = None) -> bytes | None:
    """What the zstd frames in `raw` decode to. None when they do not decode, are more than
    MAX_FRAMES or would pass `limit` bytes, of which no more than one past it are decoded."""
    view = memoryview(raw)
    parts: list[bytes] = []
    size = 0
    at = 0
    try:
        while at < len(view):
            if len(parts) >= MAX_FRAMES:
                return None
            # Each frame is decoded on its own: a decoder copies the input after its frame.
            rest = view[at:]
            end = zstd.get_frame_size(rest)
            # A frame whose header states a size past the limit is refused undecoded. Python
            # 3.14 holds twice `max_length` while it decodes.
            stated = zstd.get_frame_info(rest).decompressed_size
            if limit is not None and stated is not None and size + stated > limit:
                return None
            frame = zstd.ZstdDecompressor()
            room = -1 if limit is None else min(limit - size + 1, sys.maxsize)
            part = frame.decompress(rest[:end], max_length=room)
            size += len(part)
            if (limit is not None and size > limit) or not frame.eof or frame.unused_data:
                return None
            parts.append(part)
            at += end
    except zstd.ZstdError:
        return None
    return b"".join(parts)


class _Frames:
    """Decodes zstd frames from data fed a piece at a time, CHUNK bytes of output at a time.
    The one decoder every streaming read and write uses. A frame that is whole in the data fed
    is decoded on its own: a decoder copies the input after its frame, so many frames in one
    piece would cost time in the square of its size."""

    def __init__(self) -> None:
        self._frame: zstd.ZstdDecompressor | None = None
        # the frames decoded to their end
        self.count = 0

    def feed(self, data: bytes) -> Iterator[bytes]:
        """What `data` decodes to, after what was fed before. Raises ZstdError when it is not
        zstd, or once it is more than MAX_FRAMES frames."""
        rest = memoryview(data)
        # whether the frame being decoded began in an earlier piece
        carried = self._frame is not None
        while True:
            frame = self._frame
            piece: bytes | memoryview
            if frame is not None and not frame.needs_input:
                piece = b""
            elif not rest:
                return
            elif frame is None:
                if self.count >= MAX_FRAMES:
                    raise zstd.ZstdError(f"more than {MAX_FRAMES} frames")
                frame = self._frame = zstd.ZstdDecompressor()
                carried = False
                piece = rest
                # A frame that goes on past `rest` has no size yet; it is fed all of `rest`.
                with contextlib.suppress(zstd.ZstdError):
                    piece = rest[: zstd.get_frame_size(rest)]
            else:
                piece = rest
            rest = rest[len(piece) :]
            out = frame.decompress(piece, max_length=CHUNK)
            if out:
                yield out
            if frame.eof:
                self._frame = None
                self.count += 1
                if frame.unused_data:
                    # Only a frame begun in an earlier piece ends inside the piece it was fed.
                    if not carried or rest:
                        raise zstd.ZstdError("a frame ends before the size found for it")
                    rest = memoryview(frame.unused_data)
                carried = False

    @property
    def whole(self) -> bool:
        """Whether the data fed so far ends where a frame does."""
        return self._frame is None


def _decoded(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """What the zstd frames in `chunks` decode to, CHUNK bytes at a time. Raises ZstdError
    when they are not whole frames."""
    frames = _Frames()
    for data in chunks:
        yield from frames.feed(data)
    if not frames.whole:
        raise zstd.ZstdError("the data ends inside a frame")


def decoded(path: Path) -> Generator[bytes]:
    """What the zstd frames in the file at `path` decode to, CHUNK bytes at a time. Raises
    ZstdError when they are not whole frames."""
    with path.open("rb") as f:
        yield from _decoded(_chunks(f))


def _chunks(f: IO[bytes], seen: hashlib._Hash | None = None) -> Iterator[bytes]:
    while data := f.read(CHUNK):
        if seen is not None:
            seen.update(data)
        yield data


def holds_blob(path: Path) -> int | None:
    """The size of the blob the file at `path` is named by, None when the file does not hold
    it. Reads it a chunk at a time."""
    held = _held(path)
    return None if held is None else held[1]


def framed(path: Path) -> bool | None:
    """Whether the file at `path` holds the blob it is named by as zstd frames, True, or as
    is, False; None when it does not hold it. A blob may be a zstd frame itself, so only a
    read of the whole file tells."""
    held = _held(path)
    return None if held is None else held[0]


def _held(path: Path) -> tuple[bool, int] | None:
    raw = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        stored = os.fstat(f.fileno()).st_size
        zstd_magic = f.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC
        f.seek(0)
        chunks = _chunks(f, raw)
        if zstd_magic:
            seen = hashlib.sha256()
            with contextlib.suppress(zstd.ZstdError):
                for out in _decoded(chunks):
                    seen.update(out)
                    size += len(out)
                if seen.hexdigest() == path.name and stored <= compress_bound(size):
                    return True, size
        for _ in chunks:
            pass
    return (False, stored) if raw.hexdigest() == path.name else None


def _passes(path: Path, size: int) -> bool:
    """Whether the file at `path` decodes to more than `size` bytes, or holds more as is when it
    does not decode. Decodes no more than one byte past `size`. A file larger than any file of
    `size` bytes passes it unread."""
    if path.stat().st_size > compress_bound(size):
        return True
    with path.open("rb") as f:
        if f.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC:
            f.seek(0)
            total = 0
            with contextlib.suppress(zstd.ZstdError):
                for out in _decoded(_chunks(f)):
                    total += len(out)
                    if total > size:
                        return True
                return False
    return path.stat().st_size > size


def read_blob(path: Path, size: int | None = None) -> bytes | None:
    """The bytes of the blob at `path` if they match the sha256 it is named by, else None.
    With `size`, also None when they are longer; no more than that is decoded. A file larger
    than the compress bound of `size` is not read."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    if size is not None and path.stat().st_size > compress_bound(size):
        return None
    return blob_bytes(path.read_bytes(), path.name, size)


def blob_bytes(raw: bytes, sha256: str, size: int | None = None) -> bytes | None:
    """The blob that `raw`, a blob file as a store holds it, carries if it matches `sha256`,
    else None. With `size`, also None when the blob is longer; no more than that is decoded.
    Frames larger than the compress bound of what they decode to are not the blob."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    if size is not None and len(raw) > compress_bound(size):
        return None
    data = _frames(raw, size) if raw.startswith(ZSTD_MAGIC) else None
    if (
        data is not None
        and len(raw) <= compress_bound(len(data))
        and hashlib.sha256(data).hexdigest() == sha256
    ):
        return data
    if (size is None or len(raw) <= size) and hashlib.sha256(raw).hexdigest() == sha256:
        return raw
    return None


@dataclass(frozen=True, slots=True)
class Part:
    """`data` holds the bytes of a blob from `start`; `size` is the length of the whole blob."""

    data: bytes
    start: int
    size: int


@dataclass(frozen=True, slots=True)
class Ends:
    """A blob read as UTF-8 text, kept at its two ends. `size` is the length of the whole text
    written as UTF-8 again, a byte that does not decode counting as U+FFFD; `chars` is its
    number of characters. `head` and `tail` are its first and last whole characters within the
    bytes asked for; `head` is the whole text when `size` is within them."""

    head: str
    tail: str
    size: int
    chars: int


class _Keeper(Protocol):
    def take(self, data: bytes) -> None: ...

    def end(self) -> None: ...


class _Window:
    """Keeps `length` bytes from `start`, or the last `length` when `start` is None."""

    def __init__(self, start: int | None, length: int) -> None:
        self.start = start
        self.length = length
        self.kept = bytearray()
        self.total = 0

    def take(self, data: bytes) -> None:
        if self.start is None:
            if self.length:
                self.kept += data[-self.length :]
                del self.kept[: max(0, len(self.kept) - self.length)]
        elif self.total + len(data) > self.start and len(self.kept) < self.length:
            at = max(0, self.start - self.total)
            self.kept += data[at : at + self.length - len(self.kept)]
        self.total += len(data)

    def end(self) -> None:
        pass


class _Text:
    """Keeps the first `head` and last `tail` bytes of a blob's text, written as UTF-8 again,
    and counts the whole of it."""

    def __init__(self, head: int, tail: int) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.head_max = head
        self.tail_max = tail
        self.head = bytearray()
        self.tail = bytearray()
        self.size = 0
        self.chars = 0

    def take(self, data: bytes, final: bool = False) -> None:
        text = self.decoder.decode(data, final)
        # decoded with replace, so it holds no lone surrogate and encodes as is
        data = text.encode()
        self.chars += len(text)
        self.size += len(data)
        if len(self.head) < self.head_max:
            self.head += data[: self.head_max - len(self.head)]
        if self.tail_max:
            self.tail += data[-self.tail_max :]
            del self.tail[: max(0, len(self.tail) - self.tail_max)]

    def end(self) -> None:
        self.take(b"", final=True)


class _JsonLines:
    """Keeps the JSON object on each line of a blob, read a line at a time. Other lines are
    skipped, as is a line over `cap` bytes, which is never held whole."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.items: list[dict[str, Any]] = []
        self.line = bytearray()
        # whether the line being read passed the cap
        self.long = False

    def take(self, data: bytes) -> None:
        start = 0
        while (end := data.find(b"\n", start)) != -1:
            self._add(data[start:end])
            self._close()
            start = end + 1
        self._add(data[start:])

    def end(self) -> None:
        self._close()

    def _add(self, data: bytes) -> None:
        if self.long:
            return
        self.line += data
        if len(self.line) > self.cap:
            self.long = True
            self.line.clear()

    def _close(self) -> None:
        if not self.long and self.line:
            try:
                item = json.loads(self.line)
            except ValueError, RecursionError:
                item = None
            if isinstance(item, dict):
                self.items.append(item)
        self.line.clear()
        self.long = False


def _scan[K: _Keeper](path: Path, size: int | None, keeper: Callable[[], K]) -> K | None:
    """Feed the blob at `path` to a new keeper a chunk at a time, read as zstd frames and else
    as is. The keeper once the whole blob went by; None when the file does not hold the blob it
    is named by, or holds more than `size` bytes."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    stored = path.stat().st_size
    if size is not None and stored > compress_bound(size):
        return None
    with path.open("rb") as f:
        if f.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC:
            f.seek(0)
            with contextlib.suppress(zstd.ZstdError):
                kept = _feed(_decoded(_chunks(f)), keeper(), size, path.name, stored)
                if kept is not None:
                    return kept
        f.seek(0)
        return _feed(_chunks(f), keeper(), size, path.name, stored)


def _feed[K: _Keeper](
    chunks: Iterable[bytes], kept: K, limit: int | None, name: str, stored: int
) -> K | None:
    seen = hashlib.sha256()
    total = 0
    for data in chunks:
        seen.update(data)
        total += len(data)
        if limit is not None and total > limit:
            return None
        kept.take(data)
    if seen.hexdigest() != name or stored > compress_bound(total):
        return None
    kept.end()
    return kept


def read_part(path: Path, start: int | None, length: int, size: int | None = None) -> Part | None:
    """`length` bytes of the blob at `path` from `start`, or its last `length` bytes when `start`
    is None. None when the file does not hold the blob it is named by, or holds more than `size`
    bytes. It reads the whole file to check it, a chunk at a time, and keeps only the part."""
    window = _scan(path, size, lambda: _Window(start, length))
    if window is None:
        return None
    total, kept = window.total, window.kept
    at = total - len(kept) if start is None else min(start, total)
    return Part(bytes(kept), at, total)


def read_ends(path: Path, head: int, tail: int, size: int | None = None) -> Ends | None:
    """The blob at `path` read as UTF-8 text and kept at its two ends: its first `head` and last
    `tail` bytes, as `Ends` tells. None as `read_part` gives it. It reads the whole file to
    check it and to count the text, a chunk at a time."""
    text = _scan(path, size, lambda: _Text(head, tail))
    if text is None:
        return None
    return Ends(
        bytes(text.head).decode(errors="ignore"),
        bytes(text.tail).decode(errors="ignore"),
        text.size,
        text.chars,
    )


def read_json_lines(path: Path, cap: int, size: int | None = None) -> list[dict[str, Any]] | None:
    """The JSON object on each line of the blob at `path`, read a line at a time; see
    `_JsonLines`. None as `read_part` gives it."""
    kept = _scan(path, size, lambda: _JsonLines(cap))
    return None if kept is None else kept.items


class BlobStore:
    """Content-addressed files named by the sha256 of their uncompressed bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, sha256: str) -> Path:
        """The file of blob `sha256`. Raises BlobError when it is no sha256, as a reference
        read from a log may hold."""
        if not is_sha256(sha256):
            raise BlobError(f"not a sha256: {sha256!r}")
        return self.root / sha256

    def put(self, data: bytes) -> BlobRef:
        return self.put_many([data])[0]

    def put_many(self, datas: Sequence[bytes]) -> list[BlobRef]:
        """Store `datas` durably, with one directory fsync for the whole batch. Blobs already
        here are synced too: another call may have renamed them in and not synced yet."""
        refs = [BlobRef(sha256=hashlib.sha256(d).hexdigest(), size=len(d)) for d in datas]
        todo = {r.sha256: d for r, d in zip(refs, datas, strict=True)}
        todo = {sha: d for sha, d in todo.items() if not self.path(sha).exists()}
        if not todo:
            if refs:
                sync_dir(self.root)
            return refs
        make_dir(self.root)
        written: list[tuple[Path, Path]] = []
        placed = 0
        try:
            for sha, data in todo.items():
                tmp = self.root / f".{sha}.{secrets.token_hex(4)}"
                with tmp.open("xb") as f:
                    written.append((tmp, self.path(sha)))
                    f.write(zstd.compress(data))
                    f.flush()
                    os.fsync(f.fileno())
            for tmp, path in written:
                tmp.replace(path)
                placed += 1
        finally:
            for tmp, _ in written:
                tmp.unlink(missing_ok=True)
            if placed:
                sync_dir(self.root)
        return refs

    def missing(self, shas: Sequence[str]) -> list[str]:
        return [sha for sha in dict.fromkeys(shas) if not self.path(sha).is_file()]

    def writer(self, sha256: str, *, limit: int) -> BlobWriter:
        return BlobWriter(self, sha256, limit=limit)

    def get(self, sha256: str, size: int | None = None) -> bytes:
        """The blob's bytes. With `size`, the most its reference says it holds."""
        path = self.path(sha256)
        try:
            data = read_blob(path, size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if data is None:
            raise self._unread(path, size)
        return data

    def part(self, sha256: str, start: int | None, length: int, size: int | None = None) -> Part:
        """`length` bytes of the blob from `start`, its last ones when `start` is None. The
        whole blob is checked against its hash; only the part is kept."""
        path = self.path(sha256)
        try:
            part = read_part(path, start, length, size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if part is None:
            raise self._unread(path, size)
        return part

    def ends(self, sha256: str, head: int, tail: int, size: int | None = None) -> Ends:
        """The blob's text kept at its first `head` and last `tail` bytes; see `Ends`. The
        whole blob is checked against its hash."""
        path = self.path(sha256)
        try:
            ends = read_ends(path, head, tail, size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if ends is None:
            raise self._unread(path, size)
        return ends

    def json_lines(self, sha256: str, cap: int, size: int | None = None) -> list[dict[str, Any]]:
        """The JSON object on each line of the blob, read a line at a time. A line over `cap`
        bytes is skipped. The whole blob is checked against its hash."""
        path = self.path(sha256)
        try:
            items = read_json_lines(path, cap, size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if items is None:
            raise self._unread(path, size)
        return items

    @staticmethod
    def _unread(path: Path, size: int | None) -> BlobError:
        """Why the blob at `path` did not read through a reference stating `size`."""
        if type(size) is int and size >= 0 and _passes(path, size):
            return BlobError(
                f"blob {path.name} holds more than the {size} bytes its reference states"
            )
        return BlobError(f"blob {path.name} does not match its hash")


class BlobWriter:
    """Receives a blob file as another store holds it, zstd frames or plain bytes. It checks the
    file against its name while writing. `commit` puts it in place as one zstd frame: as sent
    when it came as one frame within the compress bound of the blob, else packed again; nothing
    is kept otherwise. `limit` caps the uncompressed size. Its compress bound caps the bytes
    sent."""

    def __init__(self, store: BlobStore, sha256: str, *, limit: int) -> None:
        self.path = store.path(sha256)
        self.sha256 = sha256
        self.limit = limit
        self.size = 0
        make_dir(store.root)
        self._tmp = store.root / f".{sha256}.{secrets.token_hex(4)}"
        self._file = self._tmp.open("xb")
        self._raw = hashlib.sha256()
        self._decoded: hashlib._Hash | None = None
        self._decoded_size = 0
        self._frames: _Frames | None = None
        self._start = b""
        self._done = False

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > compress_bound(self.limit):
            raise BlobError(
                f"the file sent for blob {self.sha256} is over {compress_bound(self.limit)} bytes"
            )
        self._file.write(chunk)
        self._raw.update(chunk)
        if self._done:
            return
        if self._frames is None:
            self._start += chunk
            if len(self._start) < len(ZSTD_MAGIC):
                return
            if not self._start.startswith(ZSTD_MAGIC):
                self._done = True
                return
            self._decoded = hashlib.sha256()
            self._frames = _Frames()
            chunk, self._start = self._start, b""
        self._feed(chunk)

    def _feed(self, data: bytes) -> None:
        assert self._decoded is not None
        assert self._frames is not None
        try:
            for out in self._frames.feed(data):
                self._decoded_size += len(out)
                if self._decoded_size > self.limit:
                    raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
                self._decoded.update(out)
        except zstd.ZstdError:
            self._decoded = None
            self._done = True

    def commit(self) -> BlobRef:
        """Check the bytes against the name and store them. Raises BlobError on a mismatch."""
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            decoded = self._decoded
            frames = self._frames
            whole = not self._done and frames is not None and frames.whole
            if whole and decoded is not None and decoded.hexdigest() == self.sha256:
                size = self._decoded_size
                one = frames is not None and frames.count == 1
                if not one or self.size > compress_bound(size):
                    self._pack(size, framed=True)
            elif self._raw.hexdigest() == self.sha256:
                size = self.size
                if size > self.limit:
                    raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
                self._pack(size, framed=False)
            else:
                raise BlobError(f"the bytes sent do not match blob {self.sha256}")
            if size > self.limit:
                raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
            self._tmp.replace(self.path)
            sync_dir(self.path.parent)
            return BlobRef(sha256=self.sha256, size=size)
        finally:
            self.abort()

    def _pack(self, size: int, *, framed: bool) -> None:
        """Make the temp file one zstd frame of the `size` bytes blob it holds: as it came, or
        as the frames it holds decode to. Readers then never meet a blob that is itself a zstd
        frame stored as is, or a file larger than the compress bound of its blob."""
        packed = self.path.parent / f".{self.sha256}.{secrets.token_hex(4)}"
        try:
            squeeze = zstd.ZstdCompressor()
            squeeze.set_pledged_input_size(size)
            with self._tmp.open("rb") as src, packed.open("xb") as dst:
                chunks = _chunks(src)
                for data in _decoded(chunks) if framed else chunks:
                    dst.write(squeeze.compress(data))
                dst.write(squeeze.flush())
                dst.flush()
                os.fsync(dst.fileno())
            packed.replace(self._tmp)
        finally:
            packed.unlink(missing_ok=True)

    def abort(self) -> None:
        if not self._file.closed:
            self._file.close()
        self._tmp.unlink(missing_ok=True)
