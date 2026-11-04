"""Pack a folder of runs into a reproducible tar.gz and write its SHA-256 beside it.

    uv run python -m organism.pack organism/build/pack/aims-organism-runs

Same input, same bytes: names sorted, times, owners and gzip header fixed. Run indexes
(index.sqlite) are left out, since the store rebuilds them.
"""

import gzip
import hashlib
import sys
import tarfile
from pathlib import Path

MTIME = 1793750400  # 2026-11-04 00:00 UTC
SKIP = {"index.sqlite", "index.sqlite-wal", "index.sqlite-shm", ".DS_Store"}


def members(root: Path):
    yield root
    for path in sorted(root.rglob("*")):
        if path.name in SKIP or any(p.name in SKIP for p in path.parents):
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise SystemExit(f"not a plain file or folder: {path}")
        yield path


def info(tar: tarfile.TarFile, path: Path, base: Path) -> tarfile.TarInfo:
    ti = tar.gettarinfo(str(path), arcname=str(path.relative_to(base)))
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = MTIME
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    out = root.with_name(f"{root.name}.tar.gz")
    with (
        out.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in members(root):
            ti = info(tar, path, root.parent)
            if ti.isdir():
                tar.addfile(ti)
            else:
                with path.open("rb") as f:
                    tar.addfile(ti, f)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n")
    print(f"{out} {out.stat().st_size} bytes sha256 {digest}")


if __name__ == "__main__":
    main()
