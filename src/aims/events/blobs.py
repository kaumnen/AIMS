import contextlib
import hashlib
import os
import re
import secrets
import sys
from collections.abc import Iterable, Iterator, Sequence
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from aims.events.disk import make_dir, sync_dir
from aims.events.schema import BlobRef


class BlobError(Exception):
    pass


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CHUNK = 1024 * 1024
# the name of the file a blob is written to before it is put in place
TEMP = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{8}")


def decode(raw: bytes) -> bytes:
    """The bytes a blob file holds. Files are zstd frames; older ones are stored as is."""
    data = _frames(raw) if raw.startswith(ZSTD_MAGIC) else None
    return raw if data is None else data


def _frames(raw: bytes, limit: int | None = None) -> bytes | None:
    """What the zstd frames in `raw` decode to. None when they do not decode or would pass
    `limit` bytes, of which no more than one past it are decoded."""
    parts: list[bytes] = []
    size = 0
    try:
        while raw:
            frame = zstd.ZstdDecompressor()
            room = -1 if limit is None else min(limit - size + 1, sys.maxsize)
            part = frame.decompress(raw, max_length=room)
            size += len(part)
            if (limit is not None and size > limit) or not frame.eof:
                return None
            parts.append(part)
            raw = frame.unused_data
    except zstd.ZstdError:
        return None
    return b"".join(parts)


def _decoded(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """What the zstd frames in `chunks` decode to, CHUNK bytes at a time. Raises ZstdError
    when they are not whole frames."""
    frame: zstd.ZstdDecompressor | None = None
    for data in chunks:
        while data or (frame is not None and not frame.needs_input):
            if frame is None:
                frame = zstd.ZstdDecompressor()
            out = frame.decompress(data, max_length=CHUNK)
            data = b""
            if out:
                yield out
            if frame.eof:
                data, frame = frame.unused_data, None
    if frame is not None:
        raise zstd.ZstdError("the data ends inside a frame")


def _chunks(f: IO[bytes], seen: hashlib._Hash | None = None) -> Iterator[bytes]:
    while data := f.read(CHUNK):
        if seen is not None:
            seen.update(data)
        yield data


def stored_size(path: Path) -> int:
    """The uncompressed size of a stored blob, from its frame header when it has one."""
    with path.open("rb") as f:
        head = f.read(18)
    if head.startswith(ZSTD_MAGIC):
        with contextlib.suppress(zstd.ZstdError):
            size = zstd.get_frame_info(head).decompressed_size
            if size is not None:
                return size
        with contextlib.suppress(zstd.ZstdError), path.open("rb") as f:
            return sum(len(out) for out in _decoded(_chunks(f)))
    return path.stat().st_size


def holds_blob(path: Path) -> bool:
    """Whether the file at `path` holds the blob it is named by. Reads it a chunk at a time."""
    raw = hashlib.sha256()
    with path.open("rb") as f:
        framed = f.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC
        f.seek(0)
        chunks = _chunks(f, raw)
        if framed:
            decoded = hashlib.sha256()
            with contextlib.suppress(zstd.ZstdError):
                for out in _decoded(chunks):
                    decoded.update(out)
                if decoded.hexdigest() == path.name:
                    return True
        for _ in chunks:
            pass
    return raw.hexdigest() == path.name


def read_blob(path: Path, size: int | None = None) -> bytes | None:
    """The bytes of the blob at `path` if they match the sha256 it is named by, else None.
    With `size`, also None when they are longer; no more than that is decoded."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    return blob_bytes(path.read_bytes(), path.name, size)


def blob_bytes(raw: bytes, sha256: str, size: int | None = None) -> bytes | None:
    """The blob that `raw`, a blob file as a store holds it, carries if it matches `sha256`,
    else None. With `size`, also None when the blob is longer; no more than that is decoded."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    data = _frames(raw, size) if raw.startswith(ZSTD_MAGIC) else None
    if data is not None and hashlib.sha256(data).hexdigest() == sha256:
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


def read_part(path: Path, start: int | None, length: int, size: int | None = None) -> Part | None:
    """`length` bytes of the blob at `path` from `start`, or its last `length` bytes when `start`
    is None. None when the file does not hold the blob it is named by, or holds more than `size`
    bytes. It reads the whole file to check it, a chunk at a time, and keeps only the part."""
    if size is not None and not (type(size) is int and size >= 0):
        return None
    with path.open("rb") as f:
        if f.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC:
            f.seek(0)
            with contextlib.suppress(zstd.ZstdError):
                part = _part(_decoded(_chunks(f)), start, length, size, path.name)
                if part is not None:
                    return part
        f.seek(0)
        return _part(_chunks(f), start, length, size, path.name)


def _part(
    chunks: Iterable[bytes], start: int | None, length: int, limit: int | None, name: str
) -> Part | None:
    seen = hashlib.sha256()
    kept = bytearray()
    total = 0
    for data in chunks:
        seen.update(data)
        if start is None:
            if length:
                kept += data[-length:]
                del kept[: max(0, len(kept) - length)]
        elif total + len(data) > start and len(kept) < length:
            at = max(0, start - total)
            kept += data[at : at + length - len(kept)]
        total += len(data)
        if limit is not None and total > limit:
            return None
    if seen.hexdigest() != name:
        return None
    at = total - len(kept) if start is None else min(start, total)
    return Part(bytes(kept), at, total)


class BlobStore:
    """Content-addressed files named by the sha256 of their uncompressed bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
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
        try:
            data = read_blob(self.path(sha256), size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if data is None:
            raise BlobError(f"blob {sha256} does not match its hash")
        return data

    def part(self, sha256: str, start: int | None, length: int, size: int | None = None) -> Part:
        """`length` bytes of the blob from `start`, its last ones when `start` is None. The
        whole blob is checked against its hash; only the part is kept."""
        try:
            part = read_part(self.path(sha256), start, length, size)
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if part is None:
            raise BlobError(f"blob {sha256} does not match its hash")
        return part


class BlobWriter:
    """Receives a blob file as another store holds it, a zstd frame or plain bytes, and checks
    it against its name while writing. `commit` puts it in place; nothing is kept otherwise.
    `limit` caps the uncompressed size."""

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
        self._frame: zstd.ZstdDecompressor | None = None
        self._start = b""
        self._done = False

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.limit + 1024 * 1024 and self._decoded is None:
            raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
        self._file.write(chunk)
        self._raw.update(chunk)
        if self._done:
            return
        if self._decoded is None and self._frame is None:
            self._start += chunk
            if len(self._start) < len(ZSTD_MAGIC):
                return
            if not self._start.startswith(ZSTD_MAGIC):
                self._done = True
                return
            self._decoded = hashlib.sha256()
            self._frame = zstd.ZstdDecompressor()
            chunk, self._start = self._start, b""
        self._feed(chunk)

    def _feed(self, data: bytes) -> None:
        assert self._decoded is not None
        try:
            while True:
                if self._frame is None or self._frame.eof:
                    rest = b"" if self._frame is None else self._frame.unused_data
                    if not rest and not data:
                        return
                    self._frame = zstd.ZstdDecompressor()
                    data = rest + data
                out = self._frame.decompress(data, max_length=CHUNK)
                data = b""
                self._decoded_size += len(out)
                if self._decoded_size > self.limit:
                    raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
                self._decoded.update(out)
                if self._frame.needs_input and not self._frame.eof:
                    return
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
            frame = self._frame
            whole = not self._done and frame is not None and frame.eof and not frame.unused_data
            if whole and decoded is not None and decoded.hexdigest() == self.sha256:
                size = self._decoded_size
            elif self._raw.hexdigest() == self.sha256:
                size = self.size
            else:
                raise BlobError(f"the bytes sent do not match blob {self.sha256}")
            if size > self.limit:
                raise BlobError(f"blob {self.sha256} is over {self.limit} bytes")
            self._tmp.replace(self.path)
            sync_dir(self.path.parent)
            return BlobRef(sha256=self.sha256, size=size)
        finally:
            self.abort()

    def abort(self) -> None:
        if not self._file.closed:
            self._file.close()
        self._tmp.unlink(missing_ok=True)
