import fcntl
import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

from aims.events.chain import MAX_LINE, Head, follow


class RunLocked(Exception):
    pass


def read_events(path: Path, run_id: str, head: Head) -> Generator[tuple[dict[str, Any], Head]]:
    """Yield verified events after `head`. Raises ChainError at the first bad line."""
    for _, event, after in read_lines(path, run_id, head):
        yield event, after


def read_lines(
    path: Path, run_id: str, head: Head
) -> Generator[tuple[bytes, dict[str, Any], Head]]:
    """Yield each verified line after `head` with its event and the head after it."""
    with path.open("rb") as f:
        f.seek(head.offset)
        while line := f.readline(MAX_LINE + 1):
            event, head = follow(head, line, run_id=run_id)
            yield line, event, head


def checkpoint_ok(path: Path, head: Head) -> bool:
    """Whether the line ending at `head.offset` is the event `head` describes."""
    if head.seq < 0:
        return head.offset == 0
    try:
        with path.open("rb") as f:
            start = max(0, head.offset - MAX_LINE)
            f.seek(start)
            chunk = f.read(head.offset - start)
    except OSError:
        return False
    if not chunk.endswith(b"\n"):
        return False
    line = chunk[chunk.rfind(b"\n", 0, len(chunk) - 1) + 1 :]
    try:
        event = json.loads(line)
    except ValueError:
        return False
    return isinstance(event, dict) and (event.get("seq"), event.get("hash")) == (
        head.seq,
        head.hash,
    )


def tail_only(path: Path, offset: int) -> bool:
    """Whether the bytes from `offset` to the end hold at most one line, the last one."""
    size = path.stat().st_size
    if size - offset > MAX_LINE:
        return False
    with path.open("rb") as f:
        f.seek(offset)
        rest = f.read()
    return b"\n" not in rest[:-1]


class LogFile:
    """Append handle on a run log. Holds an exclusive flock for its whole life."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            raise RunLocked(f"{path} is open for writing elsewhere") from None

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]

    def sync(self) -> None:
        os.fsync(self._fd)

    def truncate(self, offset: int) -> bytes:
        size = os.fstat(self._fd).st_size
        dropped = os.pread(self._fd, size - offset, offset)
        os.ftruncate(self._fd, offset)
        os.fsync(self._fd)
        return dropped

    def close(self) -> None:
        os.close(self._fd)
