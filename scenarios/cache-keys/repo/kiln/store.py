"""A cache of build outputs on local disk, one file per key."""

import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

HEX = frozenset("0123456789abcdef")


class Store:
    """Outputs under `root`, at `<root>/<first two hex digits>/<rest of the key>`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        if len(key) < 3 or not set(key) <= HEX:
            raise ValueError(f"{key!r} is not a cache key")
        return self.root / key[:2] / key[2:]

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def get(self, key: str) -> bytes | None:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> Path:
        """Store `data` under `key`, replacing what was there in one step."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def keys(self) -> Iterator[str]:
        for d in sorted(self.root.glob("??")):
            for f in sorted(d.iterdir()):
                if not f.name.startswith("."):
                    yield d.name + f.name

    def prune(self, keep: Iterable[str]) -> list[str]:
        """Remove every output whose key is not in `keep`; returns the removed keys."""
        wanted = set(keep)
        removed = [k for k in self.keys() if k not in wanted]
        for k in removed:
            self.path(k).unlink()
        return removed
