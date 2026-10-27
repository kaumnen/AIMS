import hashlib
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from aims.events.schema import BlobRef


class BlobError(Exception):
    pass


class BlobStore:
    """Content-addressed files named by the sha256 of their bytes."""

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
                    f.write(data)
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

    def get(self, sha256: str) -> bytes:
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BlobError(f"blob {sha256} does not match its hash")
        return data
