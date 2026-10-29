"""The `folder` channel: one volume every member's sandbox mounts at the folder's path.

The folder's own file reader logs how it changes as `channel.snapshot`, so the members' own
`fs.snapshot` leave it out. Its `files` are placed when the run starts; a series folder starts
from the archive the series' last run left instead, and leaves its own as the run ends."""

import hashlib
import io
import os
import posixpath
import stat
import tarfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar

from aims.channels.base import ChannelError, ChannelSetup
from aims.events import BlobRef, ChannelInfo, ChannelSnapshot, EventStore, RunStarted
from aims.sandbox import AGENT_UID, AuditedSandbox
from aims.sandbox.base import Mount, SharedVolume, SharesVolumes
from aims.sandbox.shared import SharedFolder
from aims.scenario.load import DIR_MODE, EXEC_MODE, FILE_MODE
from aims.scenario.model import FolderChannel
from aims.tools import Tool

PAGE = 500


def volume_name(channel: str) -> str:
    """The volume of folder `channel`. Agents' volumes never end in `-folder`."""
    if len(channel) <= 56:
        return f"{channel}-folder"
    return f"folder-{hashlib.sha256(channel.encode()).hexdigest()[:16]}"


class Folder:
    kind: ClassVar[str] = "folder"

    def __init__(self, setup: ChannelSetup) -> None:
        config = setup.config
        if not isinstance(config, FolderChannel):
            raise ChannelError(f"channel {config.name}: a folder takes a folder config")
        self.setup = setup
        self.config = config
        self.name = config.name
        self.members = setup.scenario.members(config)
        self.writers = setup.scenario.posters(config)
        self.volume = volume_name(config.name)
        self.restored = setup.restored if config.lifetime == "series" else None
        self.seed = self._seed()
        self.shared: SharedFolder | None = None
        self._view: SharedVolume | None = None

    def info(self) -> ChannelInfo:
        restored = None
        if self.restored is not None:
            sha = hashlib.sha256(self.restored).hexdigest()
            restored = BlobRef(sha256=sha, size=len(self.restored))
        return ChannelInfo(
            name=self.name,
            kind=self.kind,
            members=list(self.members),
            read_only=list(self.config.read_only),
            path=self.config.path,
            lifetime=self.config.lifetime,
            restored=restored,
        )

    def mounts(self, agent_id: str) -> Sequence[Mount]:
        if agent_id not in self.members:
            return []
        read_only = agent_id in self.config.read_only
        return [self._mount(read_only=read_only)]

    def tools(self, agent_id: str) -> Sequence[Tool]:
        return []

    async def start(self, sandboxes: Mapping[str, AuditedSandbox]) -> None:
        """Place the files, or the series' archive, and log the folder as it starts."""
        if not self.members:
            return
        backend = self.setup.backend
        if not isinstance(backend, SharesVolumes):
            kind = getattr(backend, "name", None)
            raise ChannelError(f"channel {self.name}: the {kind} backend cannot share a folder")
        setup = self.setup
        self._view = view = await backend.volume(setup.run_id, self._mount(read_only=False))
        data = self.restored if self.restored is not None else self.seed
        if data is not None:
            await view.put_archive(data)
        self.shared = shared = SharedFolder(
            setup.store,
            view,
            run_id=setup.run_id,
            channel=self.name,
            path=self.config.path,
            writers=self.writers,
        )
        await shared.diff(None, always=True)
        for agent_id in self.members:
            sb = sandboxes.get(agent_id)
            if sb is not None:
                shared.attach(agent_id, sb)

    async def close(self, sandboxes: Mapping[str, AuditedSandbox]) -> bytes | None:
        """Log the folder's last state with its archive; return the archive for a series."""
        shared, view = self.shared, self._view
        self.shared = self._view = None
        data = None
        try:
            if shared is not None:
                data = await shared.close()
        finally:
            if view is not None:
                await view.close()
        return data if self.config.lifetime == "series" else None

    def _mount(self, *, read_only: bool) -> Mount:
        return Mount(
            volume=self.volume, target=self.config.path, read_only=read_only, channel=self.name
        )

    def _seed(self) -> bytes | None:
        """The folder's `files` as a tar to extract at its root, owned by the agent."""
        if not self.config.files:
            return None
        tar = _Seed(time.time())
        for entry in self.config.files:
            dest = posixpath.normpath(entry.path)
            tar.parents(dest)
            mode = None if entry.mode is None else int(entry.mode, 8)
            if entry.content is not None:
                tar.file(dest, entry.content.encode(), mode or FILE_MODE)
                continue
            assert entry.source is not None
            source = self._source(entry.source)
            if source.is_dir() and not source.is_symlink():
                tar.dir(dest, source.stat().st_mtime)
                for path, rel in _walk(source):
                    tar.path(path, posixpath.join(dest, rel), None)
            else:
                tar.path(source, dest, mode)
        return tar.finish()

    def _source(self, rel: str) -> Path:
        base = self.setup.base
        if base is None:
            raise ChannelError(
                f"channel {self.name}: file source {rel} needs the scenario's directory"
            )
        path = base / rel
        if not path.exists() and not path.is_symlink():
            raise ChannelError(f"channel {self.name}: file source {rel} does not exist")
        return path


class _Seed:
    def __init__(self, mtime: float) -> None:
        self.mtime = mtime
        self.buf = io.BytesIO()
        self.tar = tarfile.open(fileobj=self.buf, mode="w", format=tarfile.PAX_FORMAT)  # noqa: SIM115
        self.dirs: set[str] = set()

    def _info(self, name: str, kind: bytes, mode: int, mtime: float | None) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type, info.mode = kind, mode
        info.mtime = int(self.mtime if mtime is None else mtime)
        info.uid = info.gid = AGENT_UID
        return info

    def parents(self, name: str) -> None:
        parent, chain = posixpath.dirname(name), []
        while parent:
            chain.append(parent)
            parent = posixpath.dirname(parent)
        for d in reversed(chain):
            self.dir(d)

    def dir(self, name: str, mtime: float | None = None) -> None:
        if name not in self.dirs:
            self.dirs.add(name)
            self.tar.addfile(self._info(name, tarfile.DIRTYPE, DIR_MODE, mtime))

    def file(self, name: str, data: bytes, mode: int, mtime: float | None = None) -> None:
        info = self._info(name, tarfile.REGTYPE, mode, mtime)
        info.size = len(data)
        self.tar.addfile(info, io.BytesIO(data))

    def path(self, path: Path, name: str, mode: int | None) -> None:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            info = self._info(name, tarfile.SYMTYPE, 0o777, st.st_mtime)
            info.linkname = os.readlink(path)
            self.tar.addfile(info)
        elif stat.S_ISDIR(st.st_mode):
            self.dir(name, st.st_mtime)
        elif stat.S_ISREG(st.st_mode):
            default = EXEC_MODE if st.st_mode & 0o111 else FILE_MODE
            self.file(name, path.read_bytes(), mode or default, st.st_mtime)

    def finish(self) -> bytes:
        self.tar.close()
        return self.buf.getvalue()


def _walk(root: Path) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        for name in [*dirnames, *sorted(filenames)]:
            out.append((here / name, name if rel == "." else posixpath.join(rel, name)))
    return out


async def archives(store: EventStore, run_id: str) -> dict[str, bytes]:
    """The archive each series folder of the finished run `run_id` left, by channel name: what
    the next run of the series passes as `Options.restored`."""
    first = await store.events(run_id, types=["run.started"], limit=1)
    if not first:
        return {}
    started = RunStarted.model_validate(first[0].data)
    series = {c.name for c in started.channels if c.kind == "folder" and c.lifetime == "series"}
    refs: dict[str, BlobRef] = {}
    after: int | None = None
    while series:
        batch = await store.events(run_id, types=["channel.snapshot"], after=after, limit=PAGE)
        for e in batch:
            snapshot = ChannelSnapshot.model_validate(e.data)
            if snapshot.archive is not None and snapshot.channel in series:
                refs[snapshot.channel] = snapshot.archive
        if len(batch) < PAGE:
            break
        after = batch[-1].seq
    return {name: await store.get_blob(run_id, ref.sha256) for name, ref in refs.items()}
