import contextlib
import os
import re
from pathlib import Path


def sync_dir(path: Path) -> None:
    """Make the entries of directory `path` durable: the names its creates, renames and
    mkdirs gave. A file's own fsync covers its bytes, not its name."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """Write all of `data` to `fd`. One `os.write` may take only a part with no error, as on a
    disk that fills up; the next call then raises."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def make_dir(path: Path, *, parents: bool = False) -> None:
    """Make directory `path` if it is missing and sync its parent, so its entry is durable.
    The parent is synced even when `path` existed, since another call may have just made it.
    With `parents`, each directory made above `path` is synced too, as it holds the entry of
    the next one, and so is the directory above the topmost one made."""
    made = [path]
    while parents and made[-1].parent != made[-1] and not made[-1].parent.exists():
        made.append(made[-1].parent)
    path.mkdir(parents=parents, exist_ok=True)
    for new in reversed(made):
        sync_dir(new.parent)


def sweep(path: Path, name: re.Pattern[str], before: float) -> int:
    """Delete the files in directory `path` whose whole name matches `name` and that were last
    written before the time `before`. Returns how many went."""
    gone = 0
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except FileNotFoundError:
        return 0
    for entry in entries:
        if not name.fullmatch(entry.name):
            continue
        with contextlib.suppress(FileNotFoundError):
            if entry.stat(follow_symlinks=False).st_mtime < before:
                os.unlink(entry.path)
                gone += 1
    return gone
