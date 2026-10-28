"""Files the UI edits: config layers and scenario directories. Every write names the version it
replaces (If-Match on the sha256), lands atomically, and stays inside its directory."""

import contextlib
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from aims.server.problems import ProblemException
from aims.server.state import ConfigCache

SCENARIO = "scenario.yaml"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_FILE = 64 * 1024 * 1024
STARTER_SCENARIO = """\
version: 1
name: {name}
oversight: observe

task:
  instruction: |
    Describe the task here.
  environment:
    image: python:3.13-slim

agents:
  - id: agent
"""


def etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def quoted(tag: str) -> str:
    return f'"{tag}"'


def check_match(current: bytes | None, if_match: str | None, if_none_match: str | None) -> None:
    """Writes name what they replace: If-Match with the current tag, or If-None-Match: * for a
    file that must not exist yet."""
    if current is None:
        if if_match is not None:
            raise ProblemException(412, "Precondition failed", "the file does not exist")
        return
    if if_none_match == "*":
        raise ProblemException(
            412, "Precondition failed", "the file exists", current=current.decode(errors="replace")
        )
    if if_match is None:
        raise ProblemException(428, "Precondition required", "send If-Match with the file's ETag")
    tags = {t.strip().removeprefix("W/").strip('"') for t in if_match.split(",")}
    if etag(current) not in tags and "*" not in tags:
        raise ProblemException(
            412,
            "Precondition failed",
            "the file changed since it was read",
            code="changed",
            etag=etag(current),
            current=current.decode(errors="replace"),
        )


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data`: a temp file beside it, fsynced, renamed over it. The mode of
    the file it replaces is kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    with contextlib.suppress(FileNotFoundError):
        mode = stat.S_IMODE(path.stat().st_mode)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}"
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            tmp.chmod(mode)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class Source:
    """A directory of scenarios, one per subdirectory."""

    id: str
    root: Path
    writable: bool
    label: str


def builtin_root() -> Path | None:
    root = resources.files("aims") / "scenarios"
    path = Path(str(root))
    return path if path.is_dir() else None


class Sources:
    def __init__(self, data: Path, config: ConfigCache) -> None:
        self.data = data
        self.config = config

    def all(self) -> list[Source]:
        out: list[Source] = []
        if (root := builtin_root()) is not None:
            out.append(Source("builtin", root, False, "built in"))
        project = self.config.layers().project
        if project is not None:
            out.append(Source("project", project.parent / "scenarios", True, "this project"))
        else:
            out.append(Source("library", self.data / "scenarios", True, "this machine"))
        try:
            extra = self.config.settings().scenario_paths
        except ProblemException:
            extra = ()
        for i, path in enumerate(extra):
            out.append(Source(f"path{i}", path, os.access(path, os.W_OK), str(path)))
        return out

    def get(self, source: str) -> Source:
        for s in self.all():
            if s.id == source:
                return s
        raise ProblemException(404, "Not found", f"no scenario source {source!r}")

    def scenario(self, source: str, name: str, *, exists: bool = True) -> tuple[Source, Path]:
        if not NAME.match(name):
            raise ProblemException(422, "Invalid request", f"not a scenario name: {name!r}")
        s = self.get(source)
        path = s.root / name
        if exists and not (path / SCENARIO).is_file():
            raise ProblemException(404, "Not found", f"no scenario {name} in {s.label}")
        return s, path

    def writable(self, source: str, name: str, *, exists: bool = True) -> Path:
        s, path = self.scenario(source, name, exists=exists)
        if not s.writable:
            raise ProblemException(
                403, "Forbidden", f"{s.label} is read only; duplicate the scenario to edit it"
            )
        return path


def confined(base: Path, rel: str) -> Path:
    """`rel` inside `base`: relative, no `..`, and not through a symlink that leads out."""
    parts = PurePosixPath(rel).parts
    bad = not parts or rel.startswith("/") or rel.endswith("/") or "\\" in rel
    if bad or any(p in ("..", ".") for p in parts):
        raise ProblemException(422, "Invalid request", f"not a path inside the scenario: {rel!r}")
    path = base.joinpath(*parts)
    root = base.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ProblemException(403, "Forbidden", f"{rel} leads outside the scenario")
    return path


def is_scenario_file(base: Path, path: Path) -> bool:
    """Whether `path` is the scenario's own scenario.yaml, on a case-blind disk too."""
    own = base / SCENARIO
    if path.parent.resolve() != base.resolve():
        return False
    if path.name.casefold() == SCENARIO:
        return True
    try:
        return path.exists() and own.exists() and path.samefile(own)
    except OSError:
        return False


def listing(base: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    root = base.resolve()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(base).as_posix()
            try:
                resolved = path.resolve()
                if root not in resolved.parents:
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            out.append({"path": rel, "size": size})
    return sorted(out, key=lambda f: str(f["path"]))
