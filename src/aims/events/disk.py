import os
from pathlib import Path


def sync_dir(path: Path) -> None:
    """Make the entries of directory `path` durable: the names its creates, renames and
    mkdirs gave. A file's own fsync covers its bytes, not its name."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_dir(path: Path, *, parents: bool = False) -> None:
    """Make directory `path` if it is missing and sync its parent, so its entry is durable.
    The parent is synced even when `path` existed, since another call may have just made it."""
    path.mkdir(parents=parents, exist_ok=True)
    sync_dir(path.parent)
