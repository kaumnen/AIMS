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
    KernelPayload,
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
    Kernel,
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

CAPS = {"procs": 1000, "files": 2000, "other": 500}
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
    """Records of one exec, counted per cap kind."""

    counts: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    seen: set[tuple[str, str, str | None]] = field(default_factory=set)


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
        self._losses: dict[str, _Loss] = {}
        self._queue: asyncio.Queue[Record] | None = None
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
        audited._queue = sandbox.records()
        if audited._queue is not None:
            audited._consumer = asyncio.create_task(audited._consume(audited._queue))
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
            self._drain()
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
            procs=(scope.counts.get("procs", 0) if scope else 0)
            if active.audit == "kernel"
            else None,
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
        while True:
            try:
                async with asyncio.timeout(FLUSH_S if self._buffer or self._losses else None):
                    record = await queue.get()
            except TimeoutError:
                await self._flush()
                continue
            self._handle(record)
            if len(self._buffer) >= FLUSH_N:
                await self._flush()

    def _drain(self) -> None:
        """Record what is still queued when the sandbox closes."""
        while self._queue is not None and not self._queue.empty():
            self._handle(self._queue.get_nowait())

    def _handle(self, record: Record) -> None:
        try:
            match record:
                case Fork():
                    self._on_fork(record)
                case Exec():
                    self._on_exec(record)
                case Exit():
                    self._on_exit(record)
                case Oom(pid):
                    self._oom.add(pid)
                case File():
                    self._on_file(record)
                case Net():
                    self._on_net(record)
                case Denied():
                    self._on_denied(record)
                case Lost():
                    self._on_lost(record)
                case _:
                    log.warning("no audit handler for %r", record)
        except Exception:
            log.exception("could not record %r", record)

    def _on_fork(self, record: Fork) -> None:
        owner = self._owner.get(record.pid, _UNKNOWN)
        if owner != _UNKNOWN:
            self._owner[record.child] = owner

    def _on_exec(self, record: Exec) -> None:
        owner = self._attribute(record)
        self._owner[record.pid] = owner
        payload = self._kernel(
            ProcExec,
            record,
            owner,
            ppid=record.ppid,
            ns_pid=record.ns_pid,
            uid=record.uid,
            path=record.path,
            argv=list(record.argv),
            argv_bytes=record.argv_bytes,
            root=record.root,
        )
        if self._admit(owner, "procs", payload):
            self._shown.add(record.pid)

    def _on_exit(self, record: Exit) -> None:
        owner = self._owner.pop(record.pid, None)
        active = self._active
        if active is not None and record.pid == active.root:
            active.exited.set()
        if record.pid in self._shown:
            self._shown.discard(record.pid)
            self._emit_payload(
                self._kernel(
                    ProcExit,
                    record,
                    owner,
                    exit_code=record.exit_code,
                    signal=record.signal,
                    oom=record.pid in self._oom,
                )
            )
        self._oom.discard(record.pid)

    def _on_file(self, record: File) -> None:
        owner = self._owner.get(record.pid)
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

    def _on_net(self, record: Net) -> None:
        owner = self._owner.get(record.pid)
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
        owner = self._owner.get(record.pid)
        payload = self._kernel(SysDenied, record, owner, syscall=record.syscall, errno=record.errno)
        self._admit(owner, "other", payload)

    def _on_lost(self, record: Lost) -> None:
        if record.count is None:
            self._emit_payload(AuditLost(count=None, reason=record.reason))
        else:
            self._losses.setdefault(record.reason, _Loss()).add(record)

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

    def _admit(self, owner: str | None, kind: str, payload: Payload) -> bool:
        """Record `payload` unless its exec is internal or has used up its cap of `kind`."""
        if owner == INTERNAL:
            return False
        scope = self._scope(owner)
        count = scope.counts.get(kind, 0)
        if count >= CAPS[kind]:
            self._over_cap(scope, kind, payload)
            return False
        scope.counts[kind] = count + 1
        self._emit_payload(payload)
        return True

    def _over_cap(self, scope: _Scope, kind: str, payload: Payload) -> None:
        scope.dropped += 1

    def _emit_payload(self, payload: Payload) -> None:
        self._buffer.append(Draft(payload, agent_id=self._agent_id))
