import asyncio
import contextlib
import io
import json
import logging
import posixpath
import tarfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from aims.events import (
    AuditLevel,
    AuditLost,
    AuditSpill,
    Content,
    Draft,
    EventStore,
    ExecFinished,
    ExecPhase,
    ExecStarted,
    FdInfo,
    FileAccess,
    FileSetattr,
    FileWrite,
    FsChange,
    FsSnapshot,
    IpcEvent,
    KernelPayload,
    MemExec,
    NetConnect,
    NsEvent,
    Payload,
    Proc,
    ProcExec,
    ProcExit,
    ProcFork,
    ProcOutput,
    ProcSignal,
    PtraceEvent,
    SandboxArchived,
    SandboxStarted,
    SandboxStopped,
    SysDenied,
)
from aims.events.store import INLINE_LIMIT
from aims.sandbox.base import (
    AGENT_UID,
    RUNNER_UID,
    TOOLBOX,
    Capture,
    Close,
    Denied,
    Exec,
    Exit,
    File,
    Fork,
    Ipc,
    Kernel,
    Lost,
    Mem,
    Net,
    Ns,
    Oom,
    Outcome,
    Output,
    PreExec,
    Ptrace,
    Record,
    Sample,
    Sandbox,
    SandboxBackend,
    SandboxSpec,
    Setattr,
    Signal,
    Tree,
)
from aims.sandbox.resources import Windows
from aims.sandbox.trace import ENV_CAP, PIPEFS_MAGIC

log = logging.getLogger("aims.sandbox")

CAPS = {"procs": 1000, "files": 2000, "other": 500, "output": 200}
ATTR_MODE, ATTR_UID, ATTR_GID, ATTR_SIZE, ATTR_ATIME, ATTR_MTIME, ATTR_CTIME = (
    1,
    2,
    4,
    8,
    16,
    32,
    64,
)
SPILL_BYTES = 32 * 1024 * 1024
CHANGES = 200
OUTPUT_PREFIX = 64
FLUSH_S = 0.1
FLUSH_N = 500
BARRIER_S = 2.0
MEMORY = 4096
ARCHIVE_CAP = 256 * 1024 * 1024

INTERNAL = "internal"
_UNKNOWN = "\0unknown"


class SandboxStoppedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExecResult:
    """`stdout` and `stderr` hold the first bytes of each stream, `*_tail` the last ones."""

    finished: ExecFinished
    stdout: bytes
    stderr: bytes
    stdout_tail: bytes = b""
    stderr_tail: bytes = b""


@dataclass(slots=True)
class _Scope:
    """Records of one exec, counted per cap kind."""

    counts: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    spilled: int = 0
    seen: set[tuple[object, ...]] = field(default_factory=set)


@dataclass(slots=True)
class _Spill:
    data: bytearray = field(default_factory=bytearray)
    count: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    ts_from: int | None = None
    ts_to: int | None = None


@dataclass(slots=True)
class _Loss:
    count: int = 0
    cpus: set[int] = field(default_factory=set)
    ts_from: int | None = None
    ts_to: int | None = None

    def add(self, lost: Lost) -> None:
        self.count += lost.count or 0
        self.cpus.update(lost.cpus)
        if lost.ts_from is not None:
            self.ts_from = lost.ts_from if self.ts_from is None else min(self.ts_from, lost.ts_from)
        if lost.ts_to is not None:
            self.ts_to = lost.ts_to if self.ts_to is None else max(self.ts_to, lost.ts_to)


@dataclass(slots=True)
class _Out:
    """Writes by one process to one pipe or FIFO inode, until the process exits."""

    first: Output
    nbytes: int = 0
    writes: int = 0
    prefix: bytes = b""


@dataclass(slots=True)
class _Fork:
    """A child not recorded yet: its fork, its owner then, and its exit if it ended unseen."""

    record: Fork
    owner: str | None
    exit: Exit | None = None
    oom: bool = False

    def forked(self, child: Fork) -> bool:
        """Whether `child` was forked while this process lived, so a reused pid is never taken."""
        ts = child.at.ts
        return self.record.at.ts <= ts and (self.exit is None or ts <= self.exit.at.ts)


@dataclass(slots=True)
class _Active:
    exec_id: str
    audit: AuditLevel = "snapshot"
    root: int | None = None
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    uid: int = AGENT_UID


class AuditedSandbox:
    """Runs commands in a sandbox and records what happened, from the host side, as events.

    Every exec is written ahead as `exec.started`. Kernel records, when the backend has them, are
    attributed to the exec that started their process tree and capped per exec.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        store: EventStore,
        *,
        run_id: str,
        agent_id: str,
        spec: SandboxSpec,
    ) -> None:
        self.sandbox = sandbox
        self.spec = spec
        self._store = store
        self._run_id = run_id
        self._agent_id = agent_id
        self._lock = asyncio.Lock()
        self._emit = asyncio.Lock()
        self._buffer: list[Draft] = []
        self._owner: dict[int, str | None] = {}
        self._gone: OrderedDict[int, str | None] = OrderedDict()
        self._roots: OrderedDict[int, str] = OrderedDict()
        self._shown: set[int] = set()
        self._forks: OrderedDict[int, _Fork] = OrderedDict()
        self._oom: set[int] = set()
        self._scopes: OrderedDict[str | None, _Scope] = OrderedDict()
        self._losses: dict[str, _Loss] = {}
        self._spills: dict[str | None, _Spill] = {}
        self._spill_bytes = 0
        self._spilled: set[int] = set()
        self._digest: str | None = None
        self._outputs: dict[int, dict[int, _Out]] = {}
        self._output_streams = 0
        self._exec_fds: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._env: OrderedDict[int, tuple[tuple[str, ...], int]] = OrderedDict()
        self._cwd: OrderedDict[int, str] = OrderedDict()
        self._pending: OrderedDict[int, asyncio.Task[str | None]] = OrderedDict()
        self._queue: asyncio.Queue[Record] | None = None
        self._samples: asyncio.Queue[Sample | Lost] | None = None
        self._windows = Windows(sandbox.info.sandbox_id, spec.sample_hz)
        self._sampling: asyncio.Task[None] | None = None
        self._sample_append: asyncio.Task[None] | None = None
        self._active: _Active | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._waiting = False
        self._quiescing = False
        self._stopped = False

    @classmethod
    async def start(
        cls,
        backend: SandboxBackend,
        spec: SandboxSpec,
        store: EventStore,
        *,
        run_id: str,
        agent_id: str,
    ) -> AuditedSandbox:
        sandbox = await backend.create(spec, run_id=run_id, agent_id=agent_id)
        audited = cls(sandbox, store, run_id=run_id, agent_id=agent_id, spec=spec)
        info = sandbox.info
        started = False
        try:
            await audited._append(
                [
                    SandboxStarted(
                        sandbox_id=info.sandbox_id,
                        backend=info.backend,
                        image=info.image,
                        image_id=info.image_id,
                        audit=sandbox.audit,
                        config=info.config,
                    )
                ]
            )
            started = True
            audited._queue = sandbox.records()
            sandbox.on_pre_exec(audited._on_pre_exec)
            if audited._queue is not None:
                audited._consumer = asyncio.create_task(audited._consume(audited._queue))
            await audited._append(await audited._files(None, always=True))
            audited._samples = sandbox.samples()
            if audited._samples is not None:
                audited._sampling = asyncio.create_task(audited._sample(audited._samples))
        except BaseException as e:
            await audited._abort(started, e)
            raise
        return audited

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float,
        call_id: str | None = None,
        stdin: bytes | None = None,
        uid: int = AGENT_UID,
        gid: int | None = None,
        phase: ExecPhase = "agent",
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> ExecResult:
        """Run `command` under the wrapper. `limit` caps the bytes kept of each output stream,
        `spec.output_limit` by default."""
        async with self._lock:
            if self._stopped:
                raise SandboxStoppedError("the sandbox is stopped")
            exec_id = str(uuid.uuid7())
            timeout_ms = max(1, int(timeout_s * 1000))
            audit = self.sandbox.audit
            wrapper = self.sandbox.wrapper(timeout_ms)
            workdir = cwd or self.spec.workdir
            before = await self._snapshot()
            await self._diff_window()
            await self._append(
                [
                    ExecStarted(
                        exec_id=exec_id,
                        call_id=call_id,
                        command=await self._store.content(self._run_id, command),
                        wrapper=wrapper,
                        user=str(uid) if gid is None or gid == uid else f"{uid}:{gid}",
                        cwd=workdir,
                        timeout_ms=timeout_ms,
                        audit=audit,
                        phase=phase,
                        stdin=None if stdin is None else await self._content(stdin),
                        stdin_bytes=None if stdin is None else len(stdin),
                        env=dict(env or {}),
                    ),
                ]
            )
            active = _Active(exec_id, audit, uid=uid)
            self._active = active
            capture = Capture(self.spec.output_limit if limit is None else limit)
            start = time.monotonic()
            try:
                outcome = await self.sandbox.run(
                    [*wrapper, command],
                    uid=uid,
                    cwd=workdir,
                    timeout_ms=timeout_ms,
                    capture=capture,
                    on_pid=lambda pid: self._root(pid, active),
                    stdin=stdin,
                    gid=gid,
                    env=env,
                )
            except asyncio.CancelledError:
                elapsed = int((time.monotonic() - start) * 1000)
                cancelled = Outcome("cancelled", None, active.root, elapsed)
                await asyncio.shield(self._finish(call_id, active, cancelled, capture, before))
                raise
            except Exception as e:
                elapsed = int((time.monotonic() - start) * 1000)
                failed = Outcome(
                    "failed", None, active.root, elapsed, error=f"{type(e).__name__}: {e}"
                )
                await asyncio.shield(self._finish(call_id, active, failed, capture, before))
                raise
            return await asyncio.shield(self._finish(call_id, active, outcome, capture, before))

    async def archive(
        self, paths: Sequence[str], *, cap: int = ARCHIVE_CAP
    ) -> SandboxArchived | None:
        """Log the files under `paths` as one tar blob, up to `cap` bytes of tar.

        Run it while the sandbox runs: tmpfs volumes lose their content once it stops.
        """
        if self._stopped:
            return None
        out = io.BytesIO()
        entries = skipped = 0
        truncated = False
        errors: list[str] = []
        with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in paths:
                try:
                    data = await self.sandbox.get_archive(path)
                except Exception as e:
                    errors.append(f"{path}: {type(e).__name__}: {e}")
                    continue
                parent = posixpath.dirname(path.rstrip("/")).lstrip("/")
                try:
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
                        for member in source:
                            size = member.size if member.isfile() else 0
                            if truncated or out.tell() + size + 1024 > cap:
                                truncated = True
                                skipped += 1
                                continue
                            member.name = posixpath.join(parent, member.name)
                            if member.islnk():
                                member.linkname = posixpath.join(parent, member.linkname)
                            fileobj = source.extractfile(member) if member.isfile() else None
                            tar.addfile(member, fileobj)
                            entries += 1
                except tarfile.TarError as e:
                    errors.append(f"{path}: {type(e).__name__}: {e}")
        data = out.getvalue()
        ref = await self._store.put_blob(self._run_id, data) if entries else None
        archived = SandboxArchived(
            sandbox_id=self.sandbox.info.sandbox_id,
            paths=list(paths),
            archive=ref,
            entries=entries,
            bytes=len(data),
            skipped=skipped,
            truncated=truncated,
            errors=errors,
        )
        await self._append([archived])
        return archived

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def stop(
        self, reason: Literal["killed", "exec_failed"] = "killed", detail: str | None = None
    ) -> None:
        """Kill the sandbox. Its volumes stay until `close`."""
        if self._stopped:
            return
        self._stopped = True
        await self.sandbox.kill()
        await self._quiesce()
        stopped = SandboxStopped(
            sandbox_id=self.sandbox.info.sandbox_id, reason=reason, detail=detail
        )
        await self._append([*await self._end_sampling(), stopped])

    async def close(self, *, keep_volumes: bool = False) -> None:
        try:
            if not self._stopped and not self.spec.keep:
                await self.sandbox.kill()
            await self._quiesce()
            last = [
                *await self._files(None, always=False),
                *await self._spill_events(),
                *await self._end_sampling(),
            ]
            if not self._stopped:
                self._stopped = True
                sandbox_id = self.sandbox.info.sandbox_id
                await self._append([*last, SandboxStopped(sandbox_id=sandbox_id, reason="closed")])
            else:
                await self._append(last)
        finally:
            await self.sandbox.close(keep_volumes=keep_volumes)

    async def _abort(self, started: bool, error: BaseException) -> None:
        """Undo a start that failed after the sandbox was created."""
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer
        try:
            if started:
                sandbox_id = self.sandbox.info.sandbox_id
                detail = f"start failed: {type(error).__name__}: {error}"
                with contextlib.suppress(Exception):
                    await self._append(
                        [SandboxStopped(sandbox_id=sandbox_id, reason="closed", detail=detail)]
                    )
        finally:
            self._stopped = True
            await self.sandbox.close()

    async def _diff_window(self) -> None:
        """Log what changed in the volumes since the last diff.

        The diff runs to the end even when the exec is cancelled, so the reader is never left
        mid-reply and the changes it found are logged before the next exec can diff.
        """
        diff = asyncio.ensure_future(self._log_files())
        cancelled = False
        while not diff.done():
            try:
                await asyncio.wait({diff})
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            if diff.exception() is not None:
                log.warning("file snapshot failed: %s", diff.exception())
            raise asyncio.CancelledError
        diff.result()

    async def _log_files(self) -> None:
        await self._append(await self._files(None, always=False))

    async def _finish(
        self,
        call_id: str | None,
        active: _Active,
        outcome: Outcome,
        capture: Capture,
        before: list[Proc] | None,
    ) -> ExecResult:
        if outcome.pid is not None:
            self._root(outcome.pid, active)
        if active.audit == "kernel" and active.root is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(BARRIER_S):
                    await active.exited.wait()
        leftover: list[Proc] = []
        killed = False
        if outcome.end != "failed":
            after = await self._snapshot()
            if before is not None and after is not None:
                known = {p.pid for p in before}
                # Runner uid processes are the sandbox's own, unless the runner ran this exec.
                own = RUNNER_UID if active.uid != RUNNER_UID else None
                leftover = [p for p in after if p.pid not in known and p.uid != own]
            if leftover and self.spec.kill_leftovers:
                # The kill runs as the agent, so it reaches only the agent's processes.
                await self._internal([f"{TOOLBOX}/busybox", "kill", "-KILL", "-1"], AGENT_UID)
                killed = all(p.uid == AGENT_UID for p in leftover)
        files = await self._files(active.exec_id, always=True)
        spills = await self._spill_events()
        scope = self._scopes.get(active.exec_id)
        finished = ExecFinished(
            exec_id=active.exec_id,
            call_id=call_id,
            end=outcome.end,
            exit_code=outcome.exit_code,
            pid=outcome.pid,
            duration_ms=outcome.duration_ms,
            stdout=await self._content(bytes(capture.stdout)),
            stderr=await self._content(bytes(capture.stderr)),
            stdout_bytes=capture.stdout_bytes,
            stderr_bytes=capture.stderr_bytes,
            truncated=capture.truncated,
            leftover=leftover,
            leftover_killed=killed,
            procs=(scope.counts.get("procs", 0) if scope else 0)
            if active.audit == "kernel"
            else None,
            dropped=scope.dropped if scope else 0,
            spilled=scope.spilled if scope else 0,
            error=outcome.error,
        )
        self._active = None
        await self._append([*files, *spills, finished])
        if outcome.end == "failed":
            await self.stop("exec_failed", outcome.error)
        return ExecResult(
            finished,
            bytes(capture.stdout),
            bytes(capture.stderr),
            bytes(capture.stdout_tail),
            bytes(capture.stderr_tail),
        )

    async def _internal(self, argv: Sequence[str], uid: int) -> None:
        active = _Active(INTERNAL)
        self._active = active
        try:
            await self.sandbox.run(
                argv,
                uid=uid,
                cwd="/",
                timeout_ms=10_000,
                capture=Capture(64 * 1024),
                on_pid=lambda pid: self._root(pid, active),
            )
        finally:
            self._active = None

    async def _snapshot(self) -> list[Proc] | None:
        try:
            return await self.sandbox.processes()
        except Exception as e:
            log.warning("process snapshot failed: %s", e)
            return None

    def _root(self, pid: int, active: _Active) -> None:
        if pid == active.root and active.exited.is_set():
            return
        self._roots[pid] = active.exec_id
        if len(self._roots) > MEMORY:
            self._roots.popitem(last=False)
        if active.root is None:
            active.root = pid

    async def _files(self, exec_id: str | None, *, always: bool) -> list[Payload]:
        """Diff the sandbox's volumes, keeping the bytes of changed files as blobs."""
        try:
            tree = await self.sandbox.tree()
        except Exception as e:
            log.warning("file snapshot failed: %s", e)
            return [AuditLost(count=None, reason=f"file snapshot failed: {e}")]
        if tree is None:
            return []
        base, self._digest = self._digest, tree.digest
        try:
            return await self._store_tree(tree, base, exec_id, always=always)
        except Exception as e:
            log.warning("file snapshot not stored: %s", e)
            return [AuditLost(count=None, reason=f"file snapshot not stored: {e}")]

    async def _store_tree(
        self, tree: Tree, base: str | None, exec_id: str | None, *, always: bool
    ) -> list[Payload]:
        changes: list[FsChange] = []
        datas = [c.data for c in tree.changes if c.data is not None]
        refs = iter(await self._store.put_blobs(self._run_id, datas) if datas else [])
        for c in tree.changes:
            content = None if c.data is None else next(refs)
            changes.append(
                FsChange(
                    path=c.path,
                    change=c.change,
                    kind=c.kind,
                    size=c.size,
                    mode=c.mode,
                    mtime_us=c.mtime_us,
                    sha256=c.sha256,
                    content=content,
                    target=c.target,
                    timestomp=c.timestomp,
                )
            )
        if not changes and not always:
            return []
        more = None
        if len(changes) > CHANGES:
            lines = "".join(f"{c.model_dump_json()}\n" for c in changes[CHANGES:])
            more = await self._store.put_blob(self._run_id, lines.encode())
        snapshot = FsSnapshot(
            exec_id=exec_id,
            roots=list(tree.roots),
            base=base,
            digest=tree.digest,
            entries=tree.entries,
            files=tree.files,
            bytes=tree.bytes,
            total=len(changes),
            changes=changes[:CHANGES],
            more=more,
            hashed_bytes=tree.hashed_bytes,
            unhashed=tree.unhashed,
            content_skipped=tree.content_skipped,
            truncated=tree.truncated,
            duration_ms=tree.duration_ms,
        )
        return [snapshot]

    async def _spill_events(self) -> list[Payload]:
        spills, self._spills, self._spill_bytes = self._spills, {}, 0
        out: list[Payload] = []
        for owner, spill in spills.items():
            records = await self._store.put_blob(self._run_id, bytes(spill.data))
            out.append(
                AuditSpill(
                    exec_id=owner,
                    count=spill.count,
                    kinds=spill.kinds,
                    records=records,
                    ts_from=spill.ts_from,
                    ts_to=spill.ts_to,
                )
            )
        return out

    async def _content(self, data: bytes) -> Content:
        if len(data) <= INLINE_LIMIT:
            try:
                return data.decode()
            except UnicodeDecodeError:
                pass
        return await self._store.put_blob(self._run_id, data)

    async def _append(self, payloads: Sequence[Payload]) -> None:
        async with self._emit:
            drafts = self._take()
            drafts += [Draft(p, agent_id=self._agent_id) for p in payloads]
            await self._store.append(self._run_id, drafts)

    async def _flush(self) -> None:
        async with self._emit:
            drafts = self._take()
            if drafts:
                await self._store.append(self._run_id, drafts)

    def _take(self) -> list[Draft]:
        drafts, self._buffer = self._buffer, []
        for reason, loss in self._losses.items():
            lost = AuditLost(
                count=loss.count,
                reason=reason,
                cpus=sorted(loss.cpus),
                ts_from=loss.ts_from,
                ts_to=loss.ts_to,
            )
            drafts.append(Draft(lost, agent_id=self._agent_id))
        self._losses = {}
        return drafts

    async def _consume(self, queue: asyncio.Queue[Record]) -> None:
        while not self._quiescing:
            self._waiting = True
            try:
                async with asyncio.timeout(FLUSH_S if self._buffer or self._losses else None):
                    record = await queue.get()
            except TimeoutError:
                self._waiting = False
                await self._flush()
                continue
            self._waiting = False
            if isinstance(record, Exec):
                record = await self._with_hash(record)
            self._handle(record)
            if len(self._buffer) >= FLUSH_N:
                await self._flush()

    async def _sample(self, queue: asyncio.Queue[Sample | Lost]) -> None:
        while True:
            payloads = self._on_sample(await queue.get())
            if payloads:
                self._sample_append = asyncio.ensure_future(self._append(payloads))
                try:
                    await asyncio.shield(self._sample_append)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("could not record resource samples")

    def _on_sample(self, item: Sample | Lost) -> list[Payload]:
        if isinstance(item, Sample):
            window = self._windows.add(item)
            return [] if window is None else [window]
        window = self._windows.flush() if item.count is None else None
        lost = AuditLost(count=item.count, reason=item.reason)
        return [lost] if window is None else [window, lost]

    async def _end_sampling(self) -> list[Payload]:
        """Stop reading samples and return the windows still open, with any queued readings."""
        if self._sampling is not None:
            self._sampling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sampling
            self._sampling = None
        if self._sample_append is not None:
            with contextlib.suppress(Exception):
                await self._sample_append
            self._sample_append = None
        payloads: list[Payload] = []
        while self._samples is not None and not self._samples.empty():
            payloads += self._on_sample(self._samples.get_nowait())
        window = self._windows.flush()
        return payloads if window is None else [*payloads, window]

    async def _with_hash(self, record: Exec) -> Exec:
        """Add the sha256 of an executed file that lives under a captured root."""
        task = self._pending.pop(record.pid, None)
        if task is not None:
            try:
                sha256 = await task
            except Exception as e:
                log.warning("could not hash the executed file: %s", e)
            else:
                if sha256 is not None:
                    return replace(record, sha256=sha256)
        path = record.path
        if not path or record.path_partial:
            return record
        if not posixpath.isabs(path):
            if not record.cwd or record.cwd_partial:
                return record
            path = posixpath.join(record.cwd, path)
        path = posixpath.normpath(path)
        sha256 = await self._hash(
            path, record.inode, record.dev, record.size, record.mtime_us, record.ctime_us
        )
        return record if sha256 is None else replace(record, sha256=sha256)

    async def _quiesce(self) -> None:
        """Wait for the killed sandbox's last kernel records, then record what is queued.

        The consumer is cancelled only while it waits for a record, so none is dropped mid-way.
        """
        await self.sandbox.quiesce()
        if self._consumer is not None:
            self._quiescing = True
            if self._waiting:
                self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None
        await self._drain()

    async def _drain(self) -> None:
        """Record what is still queued when the sandbox closes, then any unflushed output."""
        while self._queue is not None and not self._queue.empty():
            record = self._queue.get_nowait()
            if isinstance(record, Exec):
                record = await self._with_hash(record)
            self._handle(record)
        for pid in list(self._outputs):
            self._flush_outputs(pid, self._owner_of(pid))

    def _handle(self, record: Record) -> None:
        try:
            match record:
                case Fork():
                    self._on_fork(record)
                case PreExec():
                    self._on_pre_exec(record)
                case Exec():
                    self._on_exec(record)
                case Exit():
                    self._on_exit(record)
                case Oom(pid):
                    self._oom.add(pid)
                case File():
                    self._on_file(record)
                case Close():
                    self._on_close(record)
                case Net():
                    self._on_net(record)
                case Denied():
                    self._on_denied(record)
                case Output():
                    self._on_output(record)
                case Ipc():
                    self._on_ipc(record)
                case Ptrace():
                    self._on_ptrace(record)
                case Signal():
                    self._on_signal(record)
                case Mem():
                    self._on_mem(record)
                case Setattr():
                    self._on_setattr(record)
                case Ns():
                    self._on_ns(record)
                case Lost():
                    self._on_lost(record)
                case _:
                    log.warning("no audit handler for %r", record)
        except Exception:
            log.exception("could not record %r", record)

    def _on_fork(self, record: Fork) -> None:
        owner = self._owner.get(record.pid, self._gone.get(record.pid, _UNKNOWN))
        self._gone.pop(record.child, None)
        if owner != _UNKNOWN:
            self._owner[record.child] = owner
        if owner != INTERNAL and record.child not in self._shown:
            self._forks[record.child] = _Fork(record, None if owner == _UNKNOWN else owner)
            self._forks.move_to_end(record.child)
            if len(self._forks) > MEMORY:
                self._forks.popitem(last=False)

    def _on_pre_exec(self, record: PreExec) -> None:
        task = asyncio.create_task(self._pre_hash(record))
        self._pending[record.pid] = task
        self._pending.move_to_end(record.pid)
        if len(self._pending) > MEMORY:
            self._pending.popitem(last=False)

    async def _pre_hash(self, record: PreExec) -> str | None:
        for _ in range(50):
            path = self._resolve(record.path, record.ppid)
            if path is not None:
                return await self._hash(
                    path,
                    record.inode,
                    record.dev,
                    record.size,
                    record.mtime_us,
                    record.ctime_us,
                )
            if posixpath.isabs(record.path):
                return None
            await asyncio.sleep(0.001)
        return None

    async def _hash(
        self,
        path: str,
        inode: int,
        dev: int = 0,
        size: int = 0,
        mtime_us: int = 0,
        ctime_us: int = 0,
    ) -> str | None:
        try:
            return await self.sandbox.hash(
                path, inode=inode, dev=dev, size=size, mtime_us=mtime_us, ctime_us=ctime_us
            )
        except Exception as e:
            log.warning("could not hash the executed file: %s", e)
            return None

    def _resolve(self, path: str, ppid: int) -> str | None:
        if not path or path.startswith("..."):
            return None
        if posixpath.isabs(path):
            return posixpath.normpath(path)
        cwd = self._cwd.get(ppid)
        if cwd is None:
            return None
        return posixpath.normpath(posixpath.join(cwd, path))

    def _on_exec(self, record: Exec) -> None:
        owner = self._attribute(record)
        self._owner[record.pid] = owner
        fork = self._forks.get(record.pid)
        if fork is not None and (record.root or fork.record.pid == record.ppid):
            del self._forks[record.pid]
        if record.cwd and not record.cwd_partial:
            self._cwd[record.pid] = record.cwd
            self._cwd.move_to_end(record.pid)
            if len(self._cwd) > MEMORY:
                self._cwd.popitem(last=False)
        env, inherited = self._exec_env(record)
        fds = None
        if record.fds is not None:
            fds = tuple(FdInfo(ino=f.ino, kind=f.kind) for f in record.fds)
            if record.root and owner is not None and owner != INTERNAL:
                self._exec_fds[owner] = (record.fds[1].ino, record.fds[2].ino)
                if len(self._exec_fds) > MEMORY:
                    self._exec_fds.popitem(last=False)
        payload = self._kernel(
            ProcExec,
            record,
            owner,
            ppid=record.ppid,
            ns_pid=record.ns_pid,
            uid=record.uid,
            path=record.path,
            path_partial=record.path_partial,
            argv=list(record.argv),
            argv_bytes=record.argv_bytes,
            root=record.root,
            cwd=record.cwd,
            cwd_partial=record.cwd_partial,
            env=list(env),
            env_bytes=record.env_bytes,
            env_inherited=inherited,
            fds=fds,
            inode=record.inode or None,
            dev=record.dev or None,
            nlink=record.nlink if record.inode else None,
            magic=record.magic or None,
            sha256=record.sha256,
        )
        reveal = record.pid if record.pid in self._forks else record.ppid
        if self._admit(owner, "procs", payload, reveal=reveal):
            self._shown.add(record.pid)

    def _exec_env(self, record: Exec) -> tuple[tuple[str, ...], bool]:
        """`record`'s env, blanked when it was read whole and matches the parent's.

        An env past `ENV_CAP` was cut short, so a match proves nothing and it is always kept.
        """
        env = (record.env, record.env_bytes)
        parent = self._env.get(record.ppid)
        self._env[record.pid] = env
        self._env.move_to_end(record.pid)
        if len(self._env) > MEMORY:
            self._env.popitem(last=False)
        if record.env and env == parent and record.env_bytes <= ENV_CAP:
            return (), True
        return record.env, False

    def _on_exit(self, record: Exit) -> None:
        owner = self._owner.pop(record.pid, None)
        self._roots.pop(record.pid, None)
        self._gone[record.pid] = owner
        self._gone.move_to_end(record.pid)
        if len(self._gone) > MEMORY:
            self._gone.popitem(last=False)
        self._flush_outputs(record.pid, owner)
        self._env.pop(record.pid, None)
        self._cwd.pop(record.pid, None)
        self._pending.pop(record.pid, None)
        active = self._active
        if active is not None and record.pid == active.root:
            active.exited.set()
        fork = self._forks.get(record.pid)
        if record.pid in self._shown:
            self._shown.discard(record.pid)
            self._record_exit(record, owner, record.pid in self._oom)
        elif fork is not None and fork.exit is None:
            fork.exit, fork.oom = record, record.pid in self._oom
        self._oom.discard(record.pid)

    def _record_exit(self, record: Exit, owner: str | None, oom: bool) -> None:
        payload = self._kernel(
            ProcExit,
            record,
            owner,
            exit_code=record.exit_code,
            signal=record.signal,
            oom=oom,
        )
        if record.pid in self._spilled:
            self._spilled.discard(record.pid)
            scope = self._scope(owner)
            if self._spill("procs", payload):
                scope.spilled += 1
            else:
                scope.dropped += 1
        else:
            self._emit_payload(payload)

    def _on_output(self, record: Output) -> None:
        owner = self._owner_of(record.pid)
        if owner == INTERNAL:
            return
        streams = self._outputs.get(record.pid)
        acc = None if streams is None else streams.get(record.inode)
        if acc is None:
            if self._output_streams >= 4 * MEMORY:
                self._scope(owner).dropped += 1
                return
            acc = self._outputs.setdefault(record.pid, {})[record.inode] = _Out(record)
            self._output_streams += 1
        acc.nbytes += record.nbytes
        acc.writes += 1
        if record.prefix is not None and not acc.prefix:
            acc.prefix = record.prefix[:OUTPUT_PREFIX]
            if record.magic is not None:
                acc.first = record

    def _flush_outputs(self, pid: int, owner: str | None) -> None:
        streams = self._outputs.pop(pid, {})
        self._output_streams -= len(streams)
        for acc in streams.values():
            self._emit_output(owner, acc)

    def _emit_output(self, owner: str | None, acc: _Out) -> None:
        rec = acc.first
        fds = self._exec_fds.get(owner) if owner is not None else None
        if fds is not None and rec.inode == fds[0]:
            stream = "stdout"
        elif fds is not None and rec.inode == fds[1]:
            stream = "stderr"
        elif rec.magic == PIPEFS_MAGIC:
            stream = "pipe"
        else:
            stream = "fifo"
        payload = self._kernel(
            ProcOutput,
            rec,
            owner,
            inode=rec.inode,
            stream=stream,
            bytes=acc.nbytes,
            writes=acc.writes,
            prefix=acc.prefix.decode("utf-8", "backslashreplace"),
            prefix_bytes=len(acc.prefix),
            truncated=len(acc.prefix) < acc.nbytes,
        )
        self._admit(owner, "output", payload)

    def _on_file(self, record: File) -> None:
        owner = self._owner_of(record.pid)
        if owner == INTERNAL:
            return
        key = (record.op, record.path, record.target)
        scope = self._scope(owner)
        if key in scope.seen:
            return
        payload = self._kernel(
            FileAccess,
            record,
            owner,
            op=record.op,
            path=record.path,
            target=record.target,
            errno=record.errno,
            partial=record.partial,
        )
        if self._admit(owner, "files", payload):
            scope.seen.add(key)

    def _on_close(self, record: Close) -> None:
        owner = self._owner_of(record.pid)
        if owner == INTERNAL:
            return
        key = ("close", record.pid, record.path, record.size)
        scope = self._scope(owner)
        if key in scope.seen:
            return
        payload = self._kernel(
            FileWrite, record, owner, path=record.path, size=record.size, partial=record.partial
        )
        if self._admit(owner, "files", payload):
            scope.seen.add(key)

    def _on_net(self, record: Net) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            NetConnect,
            record,
            owner,
            kind=record.kind,
            family=record.family,
            address=record.address,
            port=record.port,
        )
        self._admit(owner, "other", payload)

    def _on_denied(self, record: Denied) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(SysDenied, record, owner, syscall=record.syscall, errno=record.errno)
        self._admit(owner, "other", payload)

    def _on_ipc(self, record: Ipc) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            IpcEvent,
            record,
            owner,
            kind=record.kind,
            family=record.family,
            path=record.path,
            key=record.key,
            id=record.id,
            size=record.size,
            flags=record.flags,
            partial=record.partial,
        )
        self._admit(owner, "other", payload)

    def _on_ptrace(self, record: Ptrace) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            PtraceEvent,
            record,
            owner,
            target=record.target,
            request=record.request,
            refused=record.refused,
            errno=record.errno,
        )
        self._admit(owner, "other", payload)

    def _on_signal(self, record: Signal) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            ProcSignal, record, owner, target=record.target, signal=record.signal
        )
        self._admit(owner, "other", payload)

    def _on_mem(self, record: Mem) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            MemExec,
            record,
            owner,
            kind=record.kind,
            path=record.path,
            inode=record.inode,
            nlink=record.nlink,
            prot=record.prot,
            partial=record.partial,
        )
        self._admit(owner, "other", payload)

    def _on_setattr(self, record: Setattr) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            FileSetattr,
            record,
            owner,
            path=record.path,
            partial=record.partial,
            valid=record.valid,
            mode=record.mode if record.valid & ATTR_MODE else None,
            uid=record.uid if record.valid & ATTR_UID else None,
            gid=record.gid if record.valid & ATTR_GID else None,
            size=record.size if record.valid & ATTR_SIZE else None,
            atime_us=record.atime_us if record.valid & ATTR_ATIME else None,
            mtime_us=record.mtime_us if record.valid & ATTR_MTIME else None,
            ctime_us=record.ctime_us if record.valid & ATTR_CTIME else None,
        )
        self._admit(owner, "other", payload)

    def _on_ns(self, record: Ns) -> None:
        owner = self._owner_of(record.pid)
        payload = self._kernel(
            NsEvent,
            record,
            owner,
            kind=record.kind,
            flags=record.flags,
            namespaces=list(record.namespaces),
        )
        self._admit(owner, "other", payload)

    def _on_lost(self, record: Lost) -> None:
        if record.count is None:
            self._emit_payload(AuditLost(count=None, reason=record.reason))
        else:
            self._losses.setdefault(record.reason, _Loss()).add(record)

    def _attribute(self, record: Exec) -> str | None:
        if not record.root:
            owner = self._owner.get(record.pid, _UNKNOWN)
            return self._owner_of(record.ppid) if owner == _UNKNOWN else owner
        if record.pid in self._roots:
            return self._roots[record.pid]
        active = self._active
        if active is None or (active.exec_id != INTERNAL and record.uid != active.uid):
            return INTERNAL
        if active.root is None:
            self._root(record.pid, active)
            return active.exec_id
        return INTERNAL

    def _owner_of(self, pid: int) -> str | None:
        """The exec of `pid`, also for a while after it exits: its files close after its exit."""
        return self._owner[pid] if pid in self._owner else self._gone.get(pid)

    def _scope(self, owner: str | None) -> _Scope:
        scope = self._scopes.get(owner)
        if scope is None:
            scope = self._scopes[owner] = _Scope()
            if len(self._scopes) > 256:
                self._scopes.popitem(last=False)
        return scope

    @staticmethod
    def _kernel[P: KernelPayload](
        cls: type[P], record: Kernel, owner: str | None, **fields: object
    ) -> P:
        at = record.at
        return cls.model_validate(
            {
                "exec_id": owner,
                "pid": record.pid,
                "ts_kernel": at.ts,
                "cpu": at.cpu,
                "cpu_seq": at.seq,
                **fields,
            }
        )

    def _admit(
        self, owner: str | None, kind: str, payload: KernelPayload, *, reveal: int | None = None
    ) -> bool:
        """Record `payload` unless its exec is internal. Past the cap of `kind` it is spilled.

        The pending forks of `reveal`, the payload's pid by default, go first by the same route.
        True when the record was kept, in the log or in a spill.
        """
        if owner == INTERNAL:
            return False
        scope = self._scope(owner)
        count = scope.counts.get(kind, 0)
        pid = payload.pid if reveal is None else reveal
        if pid in self._forks:
            self._reveal(pid, spill=count >= CAPS[kind])
        if count >= CAPS[kind]:
            return self._over_cap(scope, kind, payload)
        scope.counts[kind] = count + 1
        if isinstance(payload, ProcExec):
            self._spilled.discard(payload.pid)
        self._emit_payload(payload)
        return True

    def _reveal(self, pid: int, *, spill: bool) -> None:
        """Record `proc.fork` for `pid` and its unrecorded ancestors, oldest first.

        So a `ppid` always names a process already recorded. An ancestor that already ended gets
        its `proc.exit` right after its fork.
        """
        chain: list[_Fork] = []
        seen: set[int] = set()
        while pid not in self._shown and pid not in seen:
            fork = self._forks.get(pid)
            if fork is None or (chain and not fork.forked(chain[-1].record)):
                break
            chain.append(fork)
            seen.add(pid)
            pid = fork.record.pid
        for fork in reversed(chain):
            child = fork.record.child
            del self._forks[child]
            parent = fork.record.pid
            payload = self._kernel(ProcFork, fork.record, fork.owner, pid=child, ppid=parent)
            if spill:
                scope = self._scope(fork.owner)
                if not self._spill("procs", payload):
                    scope.dropped += 1
                    continue
                scope.spilled += 1
                self._spilled.add(child)
            else:
                self._spilled.discard(child)
                self._emit_payload(payload)
            if fork.exit is None:
                self._shown.add(child)
            else:
                self._record_exit(fork.exit, fork.owner, fork.oom)

    def _over_cap(self, scope: _Scope, kind: str, payload: Payload) -> bool:
        if not self._spill(kind, payload):
            scope.dropped += 1
            return False
        scope.spilled += 1
        if isinstance(payload, ProcExec):
            self._spilled.add(payload.pid)
        return True

    def _spill(self, kind: str, payload: Payload) -> bool:
        """Keep `payload` for its exec's `audit.spill` unless `SPILL_BYTES` are already pending."""
        line = f'{{"type": {json.dumps(payload.type)}, "data": {payload.model_dump_json()}}}\n'
        data = line.encode()
        if self._spill_bytes + len(data) > SPILL_BYTES:
            return False
        self._spill_bytes += len(data)
        spill = self._spills.setdefault(getattr(payload, "exec_id", None), _Spill())
        spill.data += data
        spill.count += 1
        spill.kinds[kind] = spill.kinds.get(kind, 0) + 1
        ts = getattr(payload, "ts_kernel", None)
        if ts is not None:
            spill.ts_from = ts if spill.ts_from is None else min(spill.ts_from, ts)
            spill.ts_to = ts if spill.ts_to is None else max(spill.ts_to, ts)
        return True

    def _emit_payload(self, payload: Payload) -> None:
        self._buffer.append(Draft(payload, agent_id=self._agent_id))
