"""The change log of a folder several sandboxes mount: `channel.snapshot` events from the
folder's own file reader, each change put down to the member whose kernel records wrote it."""

import asyncio
import io
import logging
import posixpath
import tarfile
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aims.events import (
    AuditLost,
    BlobRef,
    ChannelChange,
    ChannelSnapshot,
    Draft,
    Event,
    EventStore,
    Payload,
)
from aims.sandbox.base import SharedVolume, Tree

if TYPE_CHECKING:
    from aims.sandbox.audit import AuditedSandbox

log = logging.getLogger("aims.sandbox")

CHANGES = 200
EVIDENCE = 64
MEMORY = 4096
ARCHIVE_CAP = 256 * 1024 * 1024
WRITES = frozenset({"write", "delete", "rename", "mkdir", "symlink", "link"})
# Execs the runner makes in an agent's sandbox, whose writes are not the agent's.
RUNNER_PHASES = frozenset({"setup", "verifier"})


@dataclass(frozen=True, slots=True)
class _Touch:
    agent: str
    seq: int
    gen: int


class SharedFolder:
    """Diffs one shared folder and logs how it changed as `channel.snapshot`.

    Members feed it their logged events (`note`); a change is put down to the one member
    whose `file.access` write, delete, rename, mkdir, symlink or link, `file.write` or
    `file.setattr` records name its path since the change before it, with their seqs as
    evidence. Writes by setup and verifier execs count for nobody. A record that has matched
    no change by the second diff after it is dropped.
    """

    def __init__(
        self,
        store: EventStore,
        volume: SharedVolume,
        *,
        run_id: str,
        channel: str,
        path: str,
        writers: Sequence[str],
    ) -> None:
        self._store = store
        self._volume = volume
        self._run_id = run_id
        self.channel = channel
        self.path = posixpath.normpath(path)
        self._writers = frozenset(writers)
        self._sandboxes: dict[str, AuditedSandbox] = {}
        self._lock = asyncio.Lock()
        self._digest: str | None = None
        self._exact: dict[str, list[_Touch]] = {}
        self._moved: list[tuple[str, _Touch]] = []
        self._runner: OrderedDict[str, None] = OrderedDict()
        self._gen = 0
        self.closed = False

    def attach(self, agent_id: str, sandbox: AuditedSandbox) -> None:
        self._sandboxes[agent_id] = sandbox
        sandbox.share(self, writes=agent_id in self._writers)

    def note(self, events: Sequence[Event]) -> None:
        """Take in events a member logged: its runner execs and its writes in the folder."""
        for e in events:
            agent = e.agent_id
            if agent is None or agent not in self._writers:
                continue
            d = e.data
            if e.type == "exec.started":
                if d.get("phase") in RUNNER_PHASES:
                    self._runner[str(d.get("exec_id"))] = None
                    if len(self._runner) > MEMORY:
                        self._runner.popitem(last=False)
                continue
            if e.type not in ("file.access", "file.write", "file.setattr") or d.get("partial"):
                continue
            if d.get("exec_id") in self._runner:
                continue
            op = d.get("op")
            if e.type == "file.access" and (op not in WRITES or d.get("errno") is not None):
                continue
            touch = _Touch(agent, e.seq, self._gen)
            if op == "rename":
                for path in (d.get("path"), d.get("target")):
                    if isinstance(path, str) and self._inside(path):
                        self._moved.append((posixpath.normpath(path), touch))
            elif isinstance(path := d.get("path"), str) and self._inside(path):
                self._exact.setdefault(posixpath.normpath(path), []).append(touch)

    async def diff(
        self, exec_id: str | None, *, always: bool = False, archive: bool = False
    ) -> bytes | None:
        """Log what changed since the last diff, after logging every member's queued kernel
        records. With `archive`, the folder's files go in the log as a tar too, and come back.
        It never raises but for cancellation, so an exec's own events are still logged."""
        async with self._lock:
            if self.closed:
                return None
            try:
                for sb in list(self._sandboxes.values()):
                    await sb.settle()
                payloads, data = await self._diff(exec_id, always=always, archive=archive)
                if payloads:
                    await self._store.append(self._run_id, [Draft(p) for p in payloads])
            except Exception:
                log.exception("channel %s snapshot was not logged", self.channel)
                return None
            return data

    async def close(self) -> bytes | None:
        """Log the last diff with the folder's archive, stop taking in events and return the
        archive, None when it could not be read."""
        data = await self.diff(None, always=True, archive=True)
        self.closed = True
        for sb in self._sandboxes.values():
            sb.unshare(self)
        return data

    def _inside(self, path: str) -> bool:
        path = posixpath.normpath(path)
        return path == self.path or path.startswith(f"{self.path.rstrip('/')}/")

    async def _diff(
        self, exec_id: str | None, *, always: bool, archive: bool
    ) -> tuple[list[Payload], bytes | None]:
        name = f"channel {self.channel}"
        try:
            tree = await self._volume.tree()
        except Exception as e:
            log.warning("%s snapshot failed: %s", name, e)
            tree = None
            lost: list[Payload] = [AuditLost(count=None, reason=f"{name} snapshot failed: {e}")]
        else:
            lost = []
        data = await self._archive() if archive else None
        if archive and data is None:
            lost.append(AuditLost(count=None, reason=f"{name} not archived"))
        if tree is None:
            return lost, data
        base, self._digest = self._digest, tree.digest
        try:
            ref = None if data is None else await self._store.put_blob(self._run_id, data)
            snapshot = await self._snapshot(tree, base, exec_id, ref)
        except Exception as e:
            log.warning("%s snapshot not stored: %s", name, e)
            return [*lost, AuditLost(count=None, reason=f"{name} snapshot not stored: {e}")], data
        if snapshot.changes or always or ref is not None:
            return [snapshot, *lost], data
        return lost, data

    async def _snapshot(
        self, tree: Tree, base: str | None, exec_id: str | None, archive: BlobRef | None
    ) -> ChannelSnapshot:
        datas = [c.data for c in tree.changes if c.data is not None]
        refs = iter(await self._store.put_blobs(self._run_id, datas) if datas else [])
        changes: list[ChannelChange] = []
        used: set[int] = set()
        for c in tree.changes:
            touches = [*self._exact.get(c.path, ()), *self._moves(c.path)]
            agents = {t.agent for t in touches}
            used.update(id(t) for t in touches)
            changes.append(
                ChannelChange(
                    path=c.path,
                    change=c.change,
                    kind=c.kind,
                    size=c.size,
                    mode=c.mode,
                    mtime_us=c.mtime_us,
                    sha256=c.sha256,
                    content=None if c.data is None else next(refs),
                    target=c.target,
                    timestomp=c.timestomp,
                    agent=next(iter(agents)) if len(agents) == 1 else None,
                    evidence=sorted({t.seq for t in touches})[:EVIDENCE],
                )
            )
        self._age(used)
        more = None
        if len(changes) > CHANGES:
            lines = "".join(f"{c.model_dump_json()}\n" for c in changes[CHANGES:])
            more = await self._store.put_blob(self._run_id, lines.encode())
        return ChannelSnapshot(
            channel=self.channel,
            exec_id=exec_id,
            base=base,
            digest=tree.digest,
            entries=tree.entries,
            files=tree.files,
            bytes=tree.bytes,
            changes=changes[:CHANGES],
            more=more,
            truncated=tree.truncated,
            duration_ms=tree.duration_ms,
            archive=archive,
        )

    def _moves(self, path: str) -> list[_Touch]:
        return [t for root, t in self._moved if path == root or path.startswith(f"{root}/")]

    def _age(self, used: set[int]) -> None:
        """Drop the records a change used, and those that have matched none for two diffs."""

        def keep(t: _Touch) -> bool:
            return id(t) not in used and t.gen >= self._gen

        exact: dict[str, list[_Touch]] = {}
        for path, touches in self._exact.items():
            kept = [t for t in touches if keep(t)]
            if kept:
                exact[path] = kept
        self._exact = exact
        self._moved = [(root, t) for root, t in self._moved if keep(t)]
        self._gen += 1

    async def _archive(self) -> bytes | None:
        try:
            raw = await self._volume.get_archive()
            return repack(raw, cap=ARCHIVE_CAP)
        except Exception as e:
            log.warning("channel %s not archived: %s", self.channel, e)
            return None


def repack(data: bytes, *, cap: int = ARCHIVE_CAP) -> bytes | None:
    """Docker's tar of a directory as a deterministic tar of what is under it: names relative
    to it, sorted, whole-second mtimes, no owner names or extended headers. None past `cap`
    bytes of file content."""
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
        for m in source:
            top, _, rest = m.name.partition("/")
            if not rest or not top:
                continue
            m.name = rest
            if m.islnk():
                m.linkname = m.linkname.partition("/")[2]
            body = None
            if m.isfile():
                f = source.extractfile(m)
                body = f.read() if f is not None else b""
                total += len(body)
                if total > cap:
                    return None
            members.append((m, body))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for m, body in sorted(members, key=lambda item: item[0].name):
            info = tarfile.TarInfo(m.name)
            info.type = m.type
            info.mode = m.mode
            info.uid, info.gid = m.uid, m.gid
            info.mtime = int(m.mtime)
            info.linkname = m.linkname
            info.size = len(body) if body is not None else 0
            info.devmajor, info.devminor = m.devmajor, m.devminor
            tar.addfile(info, None if body is None else io.BytesIO(body))
    return out.getvalue()
