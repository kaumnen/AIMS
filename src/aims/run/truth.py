"""The hidden side of a run: its plants, canaries, roles, scripts and variant.

The truth record is written beside the event log as `truth.json`, never through the blob
store, and only its hash goes into `run.started`. The scenario the log and the monitor see is
a copy with those labels removed and canaries filled in with their real values, so it matches
what the agents' sandboxes hold. The record also keeps every file the scenario names by
`source`, so a fork rebuilds the same sandboxes from it.
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import posixpath
import secrets
import stat
import tarfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rfc8785
from pydantic import JsonValue

from aims.events.store import TRUTH
from aims.plants import PlacedFile, Placement, Plant, PlantSetup, build
from aims.plants.canary import Canary, make, render
from aims.scenario import Bundle, Scenario, ScenarioError
from aims.scenario.load import EXEC_MODE, FILE_MODE, SKIP, _parse, _Tar
from aims.scenario.model import HOME, FileEntry, FilePlant, FolderChannel, ImpossiblePlant
from aims.tools import Tool

VERSION = 1
HIDDEN = ("plants", "variants", "selftest")
HIDDEN_AGENT = ("role", "script")


@dataclass(slots=True)
class Prepared:
    """What a run starts with. `scenario` and `bundle` are the public copy; the rest is per
    agent: its seed tar, env, setup commands, instruction and plant tools."""

    truth: dict[str, JsonValue]
    scenario: Scenario
    bundle: Bundle
    full_bundle: Bundle
    seed: bytes
    variant: str | None
    canaries: dict[str, Canary] = field(default_factory=dict)
    plants: dict[str, Plant] = field(default_factory=dict)
    files: dict[str, bytes | None] = field(default_factory=dict)
    env: dict[str, dict[str, str]] = field(default_factory=dict)
    setup: dict[str, list[str]] = field(default_factory=dict)
    instructions: dict[str, str] = field(default_factory=dict)
    tools: dict[str, list[Tool]] = field(default_factory=dict)
    sources: Callable[[str], list[Any]] | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    """One member of a source: `name` is relative to the source, "" for the source itself."""

    name: str
    kind: str
    mode: int
    mtime: int
    data: bytes = b""
    target: str = ""


def prepare(
    bundle: Bundle,
    variant: str | None = None,
    seed: bytes | None = None,
    inherited: Sequence[Mapping[str, Any]] | None = None,
) -> Prepared:
    """The truth, public copy and per-agent sandbox setup for `bundle`.

    `seed` makes the canaries reproducible; a fresh one is drawn when it is None. `inherited`
    is what `inherit` returned for the series' last run.
    """
    return _prepare(bundle, variant, seed, inherited, None)


def restore(
    snapshot: Mapping[str, Any], blobs: Mapping[str, bytes], record: Mapping[str, Any]
) -> Prepared:
    """The run whose public snapshot, blobs and truth record are given, prepared again with its
    own seed, variant and inherited canaries."""
    if record.get("version") != VERSION:
        raise ValueError(f"truth record version {record.get('version')!r} is not {VERSION}")
    text = str(record["scenario"])

    def blob(name: str) -> bytes | None:
        ref = snapshot.get(name)
        return None if not isinstance(ref, Mapping) else blobs.get(str(ref.get("sha256")))

    dockerfile = snapshot.get("dockerfile")
    full = Bundle(
        scenario=_parse(text, source="truth record")[0],
        text=text.encode(),
        verifier=blob("verifier"),
        context=blob("context"),
        dockerfile=str(dockerfile) if dockerfile is not None else None,
        full_text=text.encode(),
    )
    return _prepare(
        full,
        record.get("variant"),
        bytes.fromhex(str(record["seed"])),
        record.get("inherited") or [],
        record.get("sources") or {},
    )


def inherit(run_dir: Path | str) -> list[dict[str, JsonValue]]:
    """The canaries the next run of a series carries over from the run at `run_dir`: the ones
    it inherited and its own, each as `{run, plant, parts}`. Pass the list to `prepare`."""
    record = read(run_dir)
    own: list[dict[str, JsonValue]] = [
        {"run": Path(run_dir).name, "plant": name, "parts": dict(value["parts"])}
        for name, value in (record.get("canaries") or {}).items()
    ]
    return [*(record.get("inherited") or []), *own]


def encode(record: Mapping[str, Any]) -> bytes:
    """The bytes of a truth file, canonical and stable for the same record."""
    return rfc8785.dumps(dict(record)) + b"\n"


def digest(record: Mapping[str, Any]) -> str:
    """The sha256 of the bytes `write` puts in the truth file."""
    return hashlib.sha256(encode(record)).hexdigest()


def write(run_dir: Path | str, record: Mapping[str, Any]) -> str:
    """Write `record` as the truth file of the run at `run_dir`, mode 0600. Returns its sha256."""
    target = path(run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = encode(record)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(target, 0o600)
    return hashlib.sha256(data).hexdigest()


def read(run_dir: Path | str) -> dict[str, Any]:
    """The truth record of the run at `run_dir`."""
    return json.loads(path(run_dir).read_text())


def path(run_dir: Path | str) -> Path:
    return Path(run_dir) / TRUTH


def _prepare(
    bundle: Bundle,
    variant: str | None,
    seed: bytes | None,
    inherited: Sequence[Mapping[str, Any]] | None,
    recorded: Mapping[str, Any] | None,
) -> Prepared:
    full, full_text = _full(bundle, variant)
    seed = secrets.token_bytes(32) if seed is None else seed
    base = bundle.base or (bundle.path.parent if bundle.path is not None else None)
    sources = _Sources(full, bundle, base, recorded if recorded is not None else bundle.sources)
    carried = _carried(inherited or [])
    canaries: dict[str, Canary] = {}
    for config in full.plants:
        if config.canary is not None:
            canaries[config.name] = make(config.canary, seed, config.name)
    for name, canary in carried.items():
        canaries.setdefault(name, canary)

    plants: dict[str, Plant] = {}
    for config in full.plants:
        rel = _plant_source(config)
        source = None
        if rel is not None:
            found = sources.entries(rel)
            source = found[0].data if len(found) == 1 and found[0].kind == "file" else None
        setup = PlantSetup(
            config=config, scenario=full, canaries=canaries, source=source, seed=seed
        )
        plants[config.name] = build(setup)
    for channel in full.channels:
        if isinstance(channel, FolderChannel):
            for entry in channel.files:
                if entry.source is not None:
                    with contextlib.suppress(FileNotFoundError):
                        sources.entries(entry.source)

    placements = {a.id: _placement(full, a.id, plants) for a in full.agents}
    out = Prepared(
        truth={},
        scenario=full,
        bundle=bundle,
        full_bundle=bundle,
        seed=seed,
        sources=sources.entries,
        variant=variant,
        canaries=canaries,
        plants=plants,
    )
    for agent in full.agents:
        task = full.task_for(agent.id)
        placement = placements[agent.id]
        where = f"agent {agent.id}"
        out.files[agent.id] = _tar(full, task.files, canaries, sources, placement.files)
        out.env[agent.id] = {
            k: _render(v, canaries, f"{where} env {k}")
            for k, v in {**full.sandbox.env, **placement.env}.items()
        }
        out.setup[agent.id] = [_render(c, canaries, f"{where} setup") for c in task.setup]
        out.instructions[agent.id] = _render(task.instruction, canaries, f"{where} instruction")
        out.tools[agent.id] = list(placement.tools)

    out.scenario = _public_scenario(full, canaries, placements)
    out.bundle = _public_bundle(bundle, full, out.scenario, canaries, sources)
    out.full_bundle = Bundle(
        scenario=full,
        text=full_text.encode(),
        files=bundle.files,
        verifier=bundle.verifier,
        context=bundle.context,
        dockerfile=bundle.dockerfile,
        variant=variant,
        base=base,
        path=bundle.path,
        full_text=full_text.encode(),
        sources=sources.record() if base is None else None,
    )
    out.truth = {
        "version": VERSION,
        "name": full.name,
        "scenario": full_text,
        "variant": variant,
        "seed": seed.hex(),
        "canaries": {name: {"parts": dict(c.parts)} for name, c in canaries.items()},
        "inherited": [dict(item) for item in inherited or []],
        "roles": {a.id: a.role for a in full.agents},
        "scripts": {
            a.id: [step.model_dump(mode="json") for step in a.script]
            for a in full.agents
            if a.script is not None
        },
        "plants": {name: plant.record() for name, plant in plants.items()},
        "sources": sources.record(),
    }
    return out


def _full(bundle: Bundle, variant: str | None) -> tuple[Scenario, str]:
    """The full scenario with `variant` applied, and the source text it was read from."""
    text = (bundle.full_text or bundle.text).decode()
    if variant is None and bundle.full_text is None:
        return bundle.scenario, text
    try:
        return _parse(text, source=bundle.scenario.name, variant=variant)[0], text
    except ScenarioError as e:
        raise ValueError(str(e)) from None


def _carried(inherited: Sequence[Mapping[str, Any]]) -> dict[str, Canary]:
    """The latest inherited canary of each plant, for `{{canaries.NAME}}` of plants this run
    no longer has."""
    out: dict[str, Canary] = {}
    for item in inherited:
        name = str(item["plant"])
        out[name] = Canary(name, {str(k): str(v) for k, v in item["parts"].items()})
    return out


def _placement(scenario: Scenario, agent_id: str, plants: Mapping[str, Plant]) -> Placement:
    out = Placement()
    for config in scenario.plants_of(agent_id):
        placed = plants[config.name].place(agent_id)
        out.files.extend(placed.files)
        out.env.update(placed.env)
        out.tools.extend(placed.tools)
    return out


def _plant_source(config: Any) -> str | None:
    if isinstance(config, FilePlant):
        return config.source
    if isinstance(config, ImpossiblePlant):
        return config.reference
    return None


def _render(text: str, canaries: Mapping[str, Canary], where: str) -> str:
    try:
        return render(text, canaries)
    except ValueError as e:
        raise ValueError(f"{where}: {e}") from None


def _render_data(data: bytes, canaries: Mapping[str, Canary], where: str) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return data
    return _render(text, canaries, where).encode()


def _render_tree(value: Any, canaries: Mapping[str, Canary]) -> Any:
    if isinstance(value, dict):
        return {k: _render_tree(v, canaries) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_tree(v, canaries) for v in value]
    if isinstance(value, str):
        return _render(value, canaries, "scenario")
    return value


class _Sources:
    """The bytes the scenario names by `source`: read from its directory when there is one,
    else from a truth record, else from the files tar of a bundle without a directory."""

    def __init__(
        self,
        scenario: Scenario,
        bundle: Bundle,
        base: Path | None,
        recorded: Mapping[str, Any] | None,
    ) -> None:
        self.scenario = scenario
        self.bundle = bundle
        self.base = base
        self.recorded = recorded
        self.used: dict[str, list[_Entry]] = {}

    def entries(self, rel: str) -> list[_Entry]:
        rel = posixpath.normpath(rel)
        found = self.used.get(rel)
        if found is None:
            found = self._read(rel)
            if not found:
                raise FileNotFoundError(f"the scenario has no file {rel}")
            self.used[rel] = found
        return found

    def record(self) -> dict[str, JsonValue]:
        return {
            rel: [
                {
                    "name": e.name,
                    "kind": e.kind,
                    "mode": e.mode,
                    "mtime": e.mtime,
                    "data": base64.b64encode(e.data).decode(),
                    "target": e.target,
                }
                for e in entries
            ]
            for rel, entries in sorted(self.used.items())
        }

    def _read(self, rel: str) -> list[_Entry]:
        if self.base is not None and os.path.lexists(self.base / rel):
            return list(_from_disk(self.base / rel))
        if self.recorded is not None:
            return [
                _Entry(
                    name=str(e["name"]),
                    kind=str(e["kind"]),
                    mode=int(e["mode"]),
                    mtime=int(e["mtime"]),
                    data=base64.b64decode(str(e["data"])),
                    target=str(e["target"]),
                )
                for e in self.recorded.get(rel) or []
            ]
        return self._from_files(rel)

    def _from_files(self, rel: str) -> list[_Entry]:
        dest = next(
            (
                self.scenario.sandbox_path(e.path).lstrip("/")
                for e in self.scenario.task.files
                if e.source is not None and posixpath.normpath(e.source) == rel
            ),
            None,
        )
        if dest is None or self.bundle.files is None:
            return []
        out: list[_Entry] = []
        with tarfile.open(fileobj=io.BytesIO(self.bundle.files), mode="r:") as tar:
            for member in tar.getmembers():
                if member.name == dest:
                    out.append(_member(tar, member, ""))
                elif member.name.startswith(f"{dest}/"):
                    out.append(_member(tar, member, member.name[len(dest) + 1 :]))
        return out


def _from_disk(root: Path) -> Iterator[_Entry]:
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        yield _disk_entry(root, "", st)
        return
    yield _Entry("", "dir", 0, int(st.st_mtime))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        for name in [*dirnames, *sorted(n for n in filenames if n not in SKIP)]:
            child = here / name
            yield _disk_entry(child, name if rel == "." else f"{rel}/{name}", os.lstat(child))


def _disk_entry(path: Path, name: str, st: os.stat_result) -> _Entry:
    mtime = int(st.st_mtime)
    if stat.S_ISLNK(st.st_mode):
        return _Entry(name, "link", 0o777, mtime, target=os.readlink(path))
    if stat.S_ISDIR(st.st_mode):
        return _Entry(name, "dir", 0, mtime)
    mode = EXEC_MODE if st.st_mode & 0o111 else FILE_MODE
    return _Entry(name, "file", mode, mtime, data=path.read_bytes())


def _member(tar: tarfile.TarFile, member: tarfile.TarInfo, name: str) -> _Entry:
    mtime = int(member.mtime)
    if member.issym():
        return _Entry(name, "link", 0o777, mtime, target=member.linkname)
    if member.isdir():
        return _Entry(name, "dir", 0, mtime)
    fileobj = tar.extractfile(member)
    data = fileobj.read() if fileobj is not None else b""
    return _Entry(name, "file", member.mode, mtime, data=data)


def _tar(
    scenario: Scenario,
    entries: Sequence[FileEntry],
    canaries: Mapping[str, Canary],
    sources: _Sources,
    placed: Sequence[PlacedFile] = (),
) -> bytes | None:
    """The seed tar of `entries` and `placed`, laid out as `aims.scenario.load` lays out the
    scenario's own files, with canaries filled in."""
    if not entries and not placed:
        return None
    tar = _Tar()
    for entry in entries:
        dest = scenario.sandbox_path(entry.path)
        _parents(tar, scenario, dest)
        mode = None if entry.mode is None else int(entry.mode, 8)
        name = dest.lstrip("/")
        if entry.content is not None:
            data = _render(entry.content, canaries, f"file {entry.path}").encode()
            tar.file(name, data, mode or FILE_MODE, 0)
            continue
        assert entry.source is not None
        for e in sources.entries(entry.source):
            member = posixpath.join(name, e.name) if e.name else name
            if e.kind == "dir":
                tar.dir(member, e.mtime)
            elif e.kind == "link":
                tar.link(member, e.target, e.mtime)
            else:
                data = _render_data(e.data, canaries, f"file {entry.source}/{e.name}")
                tar.file(member, data, (mode if not e.name else None) or e.mode, e.mtime)
    for p in placed:
        dest = p.path if p.path.startswith("/") else scenario.sandbox_path(p.path)
        _parents(tar, scenario, dest)
        tar.file(dest.lstrip("/"), p.data, p.mode, 0)
    return tar.finish()


def _parents(tar: _Tar, scenario: Scenario, dest: str) -> None:
    workdir = scenario.sandbox.workdir
    root = next((r for r in (workdir, "/tmp", HOME) if dest.startswith(f"{r}/")), None)
    if root is None:
        return
    parents: list[str] = []
    parent = posixpath.dirname(dest)
    while parent != root and parent.startswith(f"{root}/"):
        parents.append(parent)
        parent = posixpath.dirname(parent)
    if root == workdir:
        parents.append(workdir)
    for d in sorted(parents, key=len):
        tar.dir(d.lstrip("/"), 0)


def _public_scenario(
    full: Scenario, canaries: Mapping[str, Canary], placements: Mapping[str, Placement]
) -> Scenario:
    """`full` without its hidden labels, with canaries filled in and each agent's plant files
    and tools folded in as if the scenario had given them."""
    raw = full.model_dump(mode="json")
    for key in HIDDEN:
        raw.pop(key, None)
    raw = _render_tree(raw, canaries)
    for agent in raw["agents"]:
        for key in HIDDEN_AGENT:
            agent.pop(key, None)
        placement = placements[agent["id"]]
        if placement.files:
            task = agent.get("task") or {}
            mine = {full.sandbox_path(p.path) for p in placement.files}
            kept = [f for f in task.get("files") or [] if full.sandbox_path(f["path"]) not in mine]
            task["files"] = [
                *kept,
                *(
                    {
                        "path": p.path,
                        "content": p.data.decode(errors="replace"),
                        "mode": f"{p.mode & 0o777:04o}",
                    }
                    for p in placement.files
                ),
            ]
            agent["task"] = task
        names = [t.name for t in placement.tools if t.name not in agent["tools"]]
        agent["tools"] = [*agent["tools"], *names]
    return Scenario.model_validate(raw)


def _public_bundle(
    bundle: Bundle,
    full: Scenario,
    public: Scenario,
    canaries: Mapping[str, Canary],
    sources: _Sources,
) -> Bundle:
    return Bundle(
        scenario=public,
        text=rfc8785.dumps(_public_dump(public)),
        files=_tar(full, full.task.files, canaries, sources),
        verifier=bundle.verifier,
        context=_public_context(bundle, full),
        dockerfile=bundle.dockerfile,
        public=True,
    )


def _public_dump(scenario: Scenario) -> dict[str, Any]:
    raw = scenario.model_dump(mode="json")
    for key in HIDDEN:
        raw.pop(key, None)
    for agent in raw["agents"]:
        for key in HIDDEN_AGENT:
            agent.pop(key, None)
    return raw


def _public_context(bundle: Bundle, full: Scenario) -> bytes | None:
    """The image build context without the scenario file and the files only plants and the
    self-test name. The context is the Dockerfile's directory, so names drop its prefix."""
    dockerfile = full.task.environment.dockerfile
    if bundle.context is None or dockerfile is None:
        return bundle.context
    prefix = posixpath.dirname(posixpath.normpath(dockerfile))
    hidden = [_plant_source(c) for c in full.plants]
    if full.selftest is not None:
        scripts = (full.selftest.reference, full.selftest.exploit)
        hidden += [s for s in scripts if isinstance(s, str)]
    if bundle.path is not None:
        hidden.append(bundle.path.name)
    drop: set[str] = set()
    for rel in hidden:
        if rel is None:
            continue
        rel = posixpath.normpath(rel)
        if not prefix:
            drop.add(rel)
        elif rel.startswith(f"{prefix}/"):
            drop.add(rel[len(prefix) + 1 :])
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle.context), mode="r:") as source,
        tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as target,
    ):
        members = source.getmembers()
        names = [posixpath.normpath(m.name) for m in members]
        gone = {n for n in names if any(n == d or n.startswith(f"{d}/") for d in drop)}
        # A directory left empty by the drop goes too, or its name would stay behind.
        for name in sorted(names, key=len, reverse=True):
            under = [n for n in names if n.startswith(f"{name}/")]
            if under and all(n in gone for n in under):
                gone.add(name)
        for member, name in zip(members, names, strict=True):
            if name not in gone:
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
    return out.getvalue()
