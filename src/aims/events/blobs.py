import hashlib
import os
import secrets
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
        ref = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        path = self.path(ref.sha256)
        if path.exists():
            return ref
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{ref.sha256}.{secrets.token_hex(4)}"
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return ref

    def get(self, sha256: str) -> bytes:
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            raise BlobError(f"missing blob {sha256}") from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BlobError(f"blob {sha256} does not match its hash")
        return data
