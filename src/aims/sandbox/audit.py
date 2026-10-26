import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from aims.events import (
    AuditLevel,
    AuditLost,
    Content,
    Draft,
    EventStore,
    ExecFinished,
    ExecStarted,
    FileAccess,
    NetConnect,
    Payload,
    Proc,
    ProcExec,
    ProcExit,
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
    Denied,
    Exec,
    Exit,
    File,
    Fork,
    Lost,
    Net,
    Oom,
    Outcome,
    Record,
    Sandbox,
    SandboxBackend,
    SandboxSpec,
)

log = logging.getLogger("aims.sandbox")

PROCS = 1000
FILES = 2000
OTHER = 500
FLUSH_S = 0.1
FLUSH_N = 500
BARRIER_S = 2.0
MEMORY = 4096

INTERNAL = "internal"
_UNKNOWN = "\0unknown"


class SandboxStoppedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExecResult:
    finished: ExecFinished
    stdout: bytes
    stderr: bytes


@dataclass(slots=True)
class _Scope:
    procs: int = 0
    files: int = 0
    other: int = 0
    dropped: int = 0
    seen: set[tuple[str, str, str | None]] = field(default_factory=set)


@dataclass(slots=True)
class _Active:
    exec_id: str
    audit: AuditLevel = "snapshot"
    root: int | None = None
    exited: asyncio.Event = field(default_factory=asyncio.Event)


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
        self._roots: OrderedDict[int, str] = OrderedDict()
        self._shown: set[int] = set()
        self._oom: set[int] = set()
        self._scopes: OrderedDict[str | None, _Scope] = OrderedDict()
        self._lost = 0
        self._lost_reasons: list[str] = []
        self._active: _Active | None = None
        self._consumer: asyncio.Task[None] | None = None
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
        except BaseException:
            await sandbox.close()
            raise
        queue = sandbox.records()
        if queue is not None:
            audited._consumer = asyncio.create_task(audited._consume(queue))
        return audited

    async def exec(
        self, command: str, *, timeout_s: float, call_id: str | None = None
    ) -> ExecResult:
        async with self._lock:
            if self._stopped:
                raise SandboxStoppedError("the sandbox is stopped")
            exec_id = str(uuid.uuid7())
            timeout_ms = max(1, int(timeout_s * 1000))
            audit = self.sandbox.audit
            wrapper = self.sandbox.wrapper(timeout_ms)
            before = await self._snapshot()
            await self._append(
                [
                    ExecStarted(
                        exec_id=exec_id,
                        call_id=call_id,
                        command=await self._store.content(self._run_id, command),
                        wrapper=wrapper,
                        user=str(AGENT_UID),
                        cwd=self.spec.workdir,
                        timeout_ms=timeout_ms,
                        audit=audit,
                    )
                ]
            )
            active = _Active(exec_id, audit)
            self._active = active
            capture = Capture(self.spec.output_limit)
            start = time.monotonic()
            try:
                outcome = await self.sandbox.run(
                    [*wrapper, command],
                    uid=AGENT_UID,
                    cwd=self.spec.workdir,
                    timeout_ms=timeout_ms,
                    capture=capture,
                    on_pid=lambda pid: self._root(pid, active),
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

    async def stop(
        self, reason: Literal["killed", "exec_failed"] = "killed", detail: str | None = None
    ) -> None:
        """Kill the sandbox. Its volumes stay until `close`."""
        if self._stopped:
            return
        self._stopped = True
        await self.sandbox.kill()
        await self._append(
            [SandboxStopped(sandbox_id=self.sandbox.info.sandbox_id, reason=reason, detail=detail)]
        )

    async def close(self, *, keep_volumes: bool = False) -> None:
        try:
            if self._consumer is not None:
                self._consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer
                self._consumer = None
            if not self._stopped:
                self._stopped = True
                sandbox_id = self.sandbox.info.sandbox_id
                await self._append([SandboxStopped(sandbox_id=sandbox_id, reason="closed")])
            else:
                await self._flush()
        finally:
            await self.sandbox.close(keep_volumes=keep_volumes)

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
                leftover = [p for p in after if p.pid not in known and p.uid != RUNNER_UID]
            if leftover and self.spec.kill_leftovers:
                await self._internal([f"{TOOLBOX}/busybox", "kill", "-KILL", "-1"], AGENT_UID)
                killed = True
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
            procs=(scope.procs if scope else 0) if active.audit == "kernel" else None,
            dropped=scope.dropped if scope else 0,
            error=outcome.error,
        )
        self._active = None
        await self._append([finished])
        if outcome.end == "failed":
            await self.stop("exec_failed", outcome.error)
        return ExecResult(finished, bytes(capture.stdout), bytes(capture.stderr))

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
        self._roots[pid] = active.exec_id
        if len(self._roots) > MEMORY:
            self._roots.popitem(last=False)
        if active.root is None:
            active.root = pid

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
        if self._lost:
            reasons = "; ".join(dict.fromkeys(self._lost_reasons))
            lost = AuditLost(count=self._lost, reason=reasons)
            drafts.append(Draft(lost, agent_id=self._agent_id))
            self._lost, self._lost_reasons = 0, []
        return drafts

    async def _consume(self, queue: asyncio.Queue[Record]) -> None:
        while True:
            try:
                async with asyncio.timeout(FLUSH_S if self._buffer or self._lost else None):
                    record = await queue.get()
            except TimeoutError:
                await self._flush()
                continue
            try:
                self._handle(record)
            except Exception:
                log.exception("could not record %r", record)
            if len(self._buffer) >= FLUSH_N:
                await self._flush()

    def _handle(self, record: Record) -> None:
        match record:
            case Fork(parent, child):
                owner = self._owner.get(parent, _UNKNOWN)
                if owner != _UNKNOWN:
                    self._owner[child] = owner
            case Exec():
                owner = self._attribute(record)
                self._owner[record.pid] = owner
                if owner != INTERNAL and self._allow(owner, "procs"):
                    self._shown.add(record.pid)
                    self._emit_payload(
                        ProcExec(
                            exec_id=owner,
                            pid=record.pid,
                            ppid=record.ppid,
                            ns_pid=record.ns_pid,
                            uid=record.uid,
                            path=record.path,
                            argv=list(record.argv),
                            argv_bytes=record.argv_bytes,
                            root=record.root,
                        )
                    )
            case Exit(pid, code, signal):
                owner = self._owner.pop(pid, None)
                active = self._active
                if active is not None and pid == active.root:
                    active.exited.set()
                if pid in self._shown:
                    self._shown.discard(pid)
                    self._emit_payload(
                        ProcExit(
                            exec_id=owner,
                            pid=pid,
                            exit_code=code,
                            signal=signal,
                            oom=pid in self._oom,
                        )
                    )
                self._oom.discard(pid)
            case Oom(pid):
                self._oom.add(pid)
            case File(pid, op, path, target, errno, partial):
                owner = self._owner.get(pid)
                if owner == INTERNAL:
                    return
                scope = self._scope(owner)
                key = (op, path, target)
                if key in scope.seen or not self._allow(owner, "files"):
                    return
                scope.seen.add(key)
                self._emit_payload(
                    FileAccess(
                        exec_id=owner,
                        pid=pid,
                        op=op,
                        path=path,
                        target=target,
                        errno=errno,
                        partial=partial,
                    )
                )
            case Net(pid, kind, family, address, port):
                owner = self._owner.get(pid)
                if owner != INTERNAL and self._allow(owner, "other"):
                    self._emit_payload(
                        NetConnect(
                            exec_id=owner,
                            pid=pid,
                            kind=kind,
                            family=family,
                            address=address,
                            port=port,
                        )
                    )
            case Denied(pid, syscall, errno):
                owner = self._owner.get(pid)
                if owner != INTERNAL and self._allow(owner, "other"):
                    self._emit_payload(
                        SysDenied(exec_id=owner, pid=pid, syscall=syscall, errno=errno)
                    )
            case Lost(count, reason):
                if count is None:
                    self._emit_payload(AuditLost(count=None, reason=reason))
                else:
                    self._lost += count
                    self._lost_reasons.append(reason)

    def _attribute(self, record: Exec) -> str | None:
        if not record.root:
            owner = self._owner.get(record.pid, _UNKNOWN)
            return self._owner.get(record.ppid) if owner == _UNKNOWN else owner
        if record.pid in self._roots:
            return self._roots[record.pid]
        active = self._active
        if active is None or active.exec_id == INTERNAL or record.uid != AGENT_UID:
            return INTERNAL
        if active.root is None:
            self._root(record.pid, active)
            return active.exec_id
        return INTERNAL

    def _scope(self, owner: str | None) -> _Scope:
        scope = self._scopes.get(owner)
        if scope is None:
            scope = self._scopes[owner] = _Scope()
            if len(self._scopes) > 256:
                self._scopes.popitem(last=False)
        return scope

    def _allow(self, owner: str | None, kind: str) -> bool:
        scope = self._scope(owner)
        limit = {"procs": PROCS, "files": FILES, "other": OTHER}[kind]
        count = getattr(scope, kind)
        if count >= limit:
            scope.dropped += 1
            return False
        setattr(scope, kind, count + 1)
        return True

    def _emit_payload(self, payload: Payload) -> None:
        self._buffer.append(Draft(payload, agent_id=self._agent_id))
