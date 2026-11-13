import fcntl
import json
import os
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from aims.events.chain import MAX_LINE, Head, follow
from aims.events.disk import sync_dir, write_all

LOCK_TRIES = 3
LOCK_WAIT_S = 0.001


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


def held(path: Path) -> bool:
    """Whether a `LogFile` holds the log at `path`, in any process: its writer is alive, so
    the last lines of the log may be ones it has not synced and may still cut. Takes the
    log's lock for a moment to learn it. False for a log that is not there."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def settled(path: Path, head: Head) -> bool:
    """Whether lines read from the log at `path` up to `head` are the log's for good: no
    writer holds the log now, and it still ends those lines where `head` says. Lines a writer
    had not synced when they were read are then either gone, which the second check finds, or
    left in the log by a writer that is gone, which the next one takes as they are."""
    return not held(path) and checkpoint_ok(path, head)


class LogFile:
    """Append handle on a run log. Holds an exclusive flock for its whole life. Opening syncs
    the log's directory, so its entry is durable before any line is. A lock found held is
    tried again for a few milliseconds, since `held` takes it for a moment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        for left in reversed(range(LOCK_TRIES)):
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not left:
                    os.close(self._fd)
                    raise RunLocked(f"{path} is open for writing elsewhere") from None
                time.sleep(LOCK_WAIT_S)
        try:
            sync_dir(path.parent)
        except OSError:
            os.close(self._fd)
            raise

    def write(self, data: bytes) -> None:
        write_all(self._fd, data)

    def sync(self) -> None:
        os.fsync(self._fd)

    def tail(self, offset: int) -> bytes:
        """The bytes from `offset` to the end."""
        return os.pread(self._fd, os.fstat(self._fd).st_size - offset, offset)

    def truncate(self, offset: int) -> bytes:
        dropped = self.tail(offset)
        os.ftruncate(self._fd, offset)
        os.fsync(self._fd)
        return dropped

    def close(self) -> None:
        os.close(self._fd)
