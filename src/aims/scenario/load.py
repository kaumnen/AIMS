import hashlib
import io
import os
import posixpath
import stat
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rfc8785
from pydantic import JsonValue, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from aims.events import BlobRef
from aims.scenario.model import HOME, Scenario

AGENT_UID = 1000
FILE_MODE = 0o664
EXEC_MODE = 0o775
DIR_MODE = 0o775
SKIP = frozenset({".git", "__pycache__", ".DS_Store"})


class ScenarioError(Exception):
    """A scenario that cannot be loaded. `problems` are lines `file:line: where: what`."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Bundle:
    """A scenario and every input it reads, as bytes.

    `files` and `verifier` are tars of the seeded files (named from the sandbox root) and of the
    verifier's files; `context` is the Docker build context. `digest` covers their contents and
    modes, not their timestamps, so an unchanged scenario keeps its digest.
    """

    scenario: Scenario
    text: bytes
    files: bytes | None = None
    verifier: bytes | None = None
    context: bytes | None = None
    dockerfile: str | None = None

    @property
    def digest(self) -> str:
        parts = {
            "scenario": hashlib.sha256(self.text).hexdigest(),
            "files": _manifest(self.files),
            "verifier": _manifest(self.verifier),
            "context": _manifest(self.context),
            "dockerfile": self.dockerfile,
        }
        return hashlib.sha256(rfc8785.dumps(parts)).hexdigest()

    def blobs(self) -> list[bytes]:
        return [b for b in (self.text, self.files, self.verifier, self.context) if b is not None]

    def snapshot(self) -> dict[str, JsonValue]:
        """What `run.started` records: the resolved scenario, its inputs as blobs, the digest."""

        def ref(data: bytes | None) -> JsonValue:
            if data is None:
                return None
            sha = hashlib.sha256(data).hexdigest()
            return BlobRef(sha256=sha, size=len(data)).model_dump()

        return {
            "name": self.scenario.name,
            "digest": self.digest,
            "file": ref(self.text),
            "files": ref(self.files),
            "verifier": ref(self.verifier),
            "context": ref(self.context),
            "dockerfile": self.dockerfile,
            "resolved": self.scenario.model_dump(mode="json"),
        }


def parse(text: str, *, source: str = "scenario") -> Scenario:
    """Validate scenario YAML (1.2, so `no` and `010` stay a string and 10)."""
    yaml = YAML(typ="rt", pure=True)
    try:
        node = yaml.load(text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f":{mark.line + 1}" if mark is not None else ""
        raise ScenarioError([f"{source}{line}: {getattr(e, 'problem', None) or e}"]) from None
    if not isinstance(node, CommentedMap):
        raise ScenarioError([f"{source}: a scenario is a mapping"])
    try:
        return Scenario.model_validate(_plain(node))
    except ValidationError as e:
        problems = []
        for err in e.errors(include_url=False):
            loc = err["loc"]
            line = _line(node, loc)
            where = _where(loc)
            at = f"{source}:{line}" if line is not None else source
            problems.append(f"{at}: {where + ': ' if where else ''}{err['msg']}")
        raise ScenarioError(problems) from None


def load(path: Path) -> Bundle:
    """Read the scenario at `path` and the files it names, relative to its directory."""
    try:
        text = path.read_bytes()
    except OSError as e:
        raise ScenarioError([f"{path}: {e.strerror or e}"]) from None
    try:
        decoded = text.decode()
    except UnicodeDecodeError:
        raise ScenarioError([f"{path}: not UTF-8"]) from None
    scenario = parse(decoded, source=str(path))
    base = path.parent
    problems: list[str] = []
    files = verifier = context = None
    dockerfile = None
    try:
        files = _seed_tar(scenario, base, os.stat(path).st_mtime)
    except OSError as e:
        problems.append(f"{path}: task.files: {e.filename}: {e.strerror}")
    v = scenario.task.verifier
    if v is not None and v.files is not None:
        try:
            verifier = _dir_tar(base / v.files, "tests", deterministic=False)
        except OSError as e:
            problems.append(f"{path}: task.verifier.files: {e.filename}: {e.strerror}")
    env = scenario.task.environment
    if env.dockerfile is not None:
        file = base / env.dockerfile
        dockerfile = file.name
        try:
            if not file.is_file():
                raise FileNotFoundError(2, "No such file", str(file))
            context = _dir_tar(file.parent, "", deterministic=True)
        except OSError as e:
            problems.append(f"{path}: task.environment.dockerfile: {e.filename}: {e.strerror}")
    if problems:
        raise ScenarioError(problems)
    return Bundle(scenario, text, files, verifier, context, dockerfile)


def from_snapshot(snapshot: dict[str, Any], blobs: dict[str, bytes]) -> Bundle:
    """Rebuild a bundle from what `run.started` recorded and the blobs it names."""

    def get(name: str) -> bytes | None:
        ref = snapshot.get(name)
        return None if ref is None else blobs[ref["sha256"]]

    text = get("file")
    if text is None:
        raise ScenarioError(["the run did not record its scenario file"])
    scenario = parse(text.decode(), source=f"recorded {snapshot.get('name')}")
    bundle = Bundle(
        scenario, text, get("files"), get("verifier"), get("context"), snapshot.get("dockerfile")
    )
    if bundle.digest != snapshot.get("digest"):
        raise ScenarioError(["the recorded scenario does not match its digest"])
    return bundle


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, bool):
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def _line(node: Any, loc: tuple[int | str, ...]) -> int | None:
    line = None
    for key in loc:
        if isinstance(node, CommentedMap) and key in node:
            line = node.lc.key(key)[0] + 1
            node = node[key]
        elif isinstance(node, CommentedSeq) and isinstance(key, int) and key < len(node):
            line = node.lc.item(key)[0] + 1
            node = node[key]
        else:
            break
    if line is None and isinstance(node, CommentedMap | CommentedSeq):
        line = node.lc.line + 1
    return line


def _where(loc: tuple[int | str, ...]) -> str:
    out = ""
    for key in loc:
        out += f"[{key}]" if isinstance(key, int) else f".{key}" if out else str(key)
    return out


def _manifest(data: bytes | None) -> str | None:
    if data is None:
        return None
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for m in sorted(tar.getmembers(), key=lambda m: m.name):
            body = b""
            if m.isfile():
                f = tar.extractfile(m)
                body = hashlib.sha256(f.read() if f else b"").digest()
            digest.update(f"{m.name}\0{m.type!r}\0{m.mode:o}\0{m.linkname}\0".encode() + body)
    return digest.hexdigest()


class _Tar:
    def __init__(self) -> None:
        self.buf = io.BytesIO()
        self.tar = tarfile.open(fileobj=self.buf, mode="w", format=tarfile.PAX_FORMAT)  # noqa: SIM115
        self.names: set[str] = set()

    def _info(self, name: str, kind: bytes, mode: int, mtime: float) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = mode
        info.mtime = int(mtime)
        info.uid = info.gid = AGENT_UID
        return info

    def dir(self, name: str, mtime: float) -> None:
        if name not in self.names:
            self.names.add(name)
            self.tar.addfile(self._info(name, tarfile.DIRTYPE, DIR_MODE, mtime))

    def file(self, name: str, data: bytes, mode: int, mtime: float) -> None:
        self.names.add(name)
        info = self._info(name, tarfile.REGTYPE, mode, mtime)
        info.size = len(data)
        self.tar.addfile(info, io.BytesIO(data))

    def link(self, name: str, target: str, mtime: float) -> None:
        self.names.add(name)
        info = self._info(name, tarfile.SYMTYPE, 0o777, mtime)
        info.linkname = target
        self.tar.addfile(info)

    def finish(self) -> bytes:
        self.tar.close()
        return self.buf.getvalue()


def _seed_tar(scenario: Scenario, base: Path, mtime: float) -> bytes | None:
    entries = scenario.task.files
    if not entries:
        return None
    tar = _Tar()
    workdir = scenario.sandbox.workdir
    for entry in entries:
        dest = scenario.sandbox_path(entry.path)
        root = next(r for r in (workdir, "/tmp", HOME) if dest.startswith(f"{r}/"))
        parents = []
        parent = posixpath.dirname(dest)
        while parent != root and parent.startswith(f"{root}/"):
            parents.append(parent)
            parent = posixpath.dirname(parent)
        # The workdir itself turns group writable too; /tmp and the home dir keep their modes.
        if root == workdir:
            parents.append(workdir)
        for d in sorted(parents, key=len):
            tar.dir(d.lstrip("/"), mtime)
        mode = None if entry.mode is None else int(entry.mode, 8)
        if entry.content is not None:
            tar.file(dest.lstrip("/"), entry.content.encode(), mode or FILE_MODE, mtime)
            continue
        assert entry.source is not None
        source = base / entry.source
        st = os.lstat(source)
        if stat.S_ISDIR(st.st_mode):
            tar.dir(dest.lstrip("/"), st.st_mtime)
            for path, rel in _walk(source):
                _add(tar, path, posixpath.join(dest, rel).lstrip("/"), None)
        else:
            _add(tar, source, dest.lstrip("/"), mode)
    return tar.finish()


def _add(tar: _Tar, path: Path, name: str, mode: int | None) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        tar.link(name, os.readlink(path), st.st_mtime)
    elif stat.S_ISDIR(st.st_mode):
        tar.dir(name, st.st_mtime)
    elif stat.S_ISREG(st.st_mode):
        default = EXEC_MODE if st.st_mode & 0o111 else FILE_MODE
        tar.file(name, path.read_bytes(), mode or default, st.st_mtime)


def _walk(root: Path) -> Iterator[tuple[Path, str]]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        for d in dirnames:
            yield here / d, posixpath.join(rel, d) if rel != "." else d
        for f in sorted(n for n in filenames if n not in SKIP):
            yield here / f, posixpath.join(rel, f) if rel != "." else f


def _dir_tar(root: Path, prefix: str, *, deterministic: bool) -> bytes:
    if not root.is_dir():
        raise NotADirectoryError(20, "Not a directory", str(root))
    tar = _Tar()
    if prefix:
        tar.dir(prefix, 0 if deterministic else os.stat(root).st_mtime)
    for path, rel in _walk(root):
        name = posixpath.join(prefix, rel) if prefix else rel
        if deterministic:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                tar.link(name, os.readlink(path), 0)
            elif stat.S_ISDIR(st.st_mode):
                tar.dir(name, 0)
            elif stat.S_ISREG(st.st_mode):
                mode = 0o755 if st.st_mode & 0o111 else 0o644
                tar.file(name, path.read_bytes(), mode, 0)
        else:
            _add(tar, path, name, None)
    return tar.finish()
