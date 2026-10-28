import contextlib
import hashlib
import os
import secrets
from collections.abc import Sequence
from compression import zstd
from pathlib import Path

from aims.events.schema import BlobRef


class BlobError(Exception):
    pass


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CHUNK = 1024 * 1024


def decode(raw: bytes) -> bytes:
    """The bytes a blob file holds. Files are zstd frames; older ones are stored as is."""
    if raw.startswith(ZSTD_MAGIC):
        try:
            return zstd.decompress(raw)
        except zstd.ZstdError:
            pass
    return raw


def stored_size(path: Path) -> int:
    """The uncompressed size of a stored blob, from its frame header when it has one."""
    with path.open("rb") as f:
        head = f.read(18)
    if head.startswith(ZSTD_MAGIC):
        with contextlib.suppress(zstd.ZstdError):
            size = zstd.get_frame_info(head).decompressed_size
            if size is not None:
                return size
        return len(decode(path.read_bytes()))
    return path.stat().st_size


def read_blob(path: Path) -> bytes | None:
    """The bytes of the blob at `path` if they match the sha256 it is named by, else None."""
    raw = path.read_bytes()
    data = decode(raw)
    if hashlib.sha256(data).hexdigest() == path.name:
        return data
    if data is not raw and hashlib.sha256(raw).hexdigest() == path.name:
        return raw
    return None


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
        """Store `datas` durably, with one directory fsync for the whole batch."""
        refs = [BlobRef(sha256=hashlib.sha256(d).hexdigest(), size=len(d)) for d in datas]
        todo = {r.sha256: d for r, d in zip(refs, datas, strict=True)}
        todo = {sha: d for sha, d in todo.items() if not self.path(sha).exists()}
        if not todo:
            return refs
        self.root.mkdir(parents=True, exist_ok=True)
        written: list[tuple[Path, Path]] = []
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
        finally:
            for tmp, _ in written:
                tmp.unlink(missing_ok=True)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return refs

    def missing(self, shas: Sequence[str]) -> list[str]:
        return [sha for sha in dict.fromkeys(shas) if not self.path(sha).is_file()]

    def writer(self, sha256: str, *, limit: int) -> BlobWriter:
        return BlobWriter(self, sha256, limit=limit)

    def get(self, sha256: str) -> bytes:
        try:
            data = read_blob(self.path(sha256))
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if data is None:
            raise BlobError(f"blob {sha256} does not match its hash")
        return data


class BlobWriter:
    """Receives a blob file as another store holds it, a zstd frame or plain bytes, and checks
    it against its name while writing. `commit` puts it in place; nothing is kept otherwise.
    `limit` caps the uncompressed size."""

    def __init__(self, store: BlobStore, sha256: str, *, limit: int) -> None:
        self.path = store.path(sha256)
        self.sha256 = sha256
        self.limit = limit
        self.size = 0
        store.root.mkdir(parents=True, exist_ok=True)
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
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return BlobRef(sha256=self.sha256, size=size)
        finally:
            self.abort()

    def abort(self) -> None:
        if not self._file.closed:
            self._file.close()
        self._tmp.unlink(missing_ok=True)
