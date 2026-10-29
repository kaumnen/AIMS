"""Facts the signals share, derived from the events of a run in seq order: the execs and the
tool calls behind them, the process tree, and the paths the agents wrote.

Kernel pids are host pids and are reused, so a process is found by its pid and the kernel time
of the record that names it. Records over an exec's caps arrive later as `audit.spill` blobs and
changes past a snapshot's inline ones as its `more` blob; both are read here and handed to the
signals as virtual events that carry the seq of the event that holds them. Each change of an
`fs.snapshot` becomes an `fs.change` virtual event.
"""

import json
import logging
import posixpath
import re
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from pydantic import Field, JsonValue, ValidationError

from aims.events import (
    AgentFinished,
    AgentStep,
    AuditSpill,
    BlobRef,
    ChannelSnapshot,
    Content,
    Event,
    EventStore,
    ExecFinished,
    ExecPhase,
    ExecStarted,
    FileAccess,
    FileSetattr,
    FileWrite,
    FsChange,
    FsSnapshot,
    KernelPayload,
    Payload,
    ProcExec,
    ProcExit,
    ProcFork,
    ProcSignal,
    PtraceEvent,
    TaskScored,
    ToolCall,
    ToolResult,
)

log = logging.getLogger("aims.monitor")

FS_CHANGE = "fs.change"
ATTR_MODE = 1
ATTR_SIZE = 8
ATTR_MTIME_SET = 256
TEXT_CAP = 1024 * 1024
HISTORY = 8
BLOB_CACHE = 32
AGENT_PHASES: frozenset[ExecPhase | None] = frozenset({"agent", "replay"})
BUSYBOX = "busybox"

_PROC_PATH = re.compile(r"^/proc/(self|thread-self|\d+)(?:/|$)")
_INSTALL_WORDS = frozenset({"install", "download", "wheel", "uninstall"})
_UV_WORDS = frozenset({"pip", "sync", "add", "remove", "venv", "tool", "lock"})
_ENV_WORDS = frozenset({"install", "add", "sync", "create", "update", "lock", "remove"})
_PYTHON = re.compile(r"^python[0-9.]*$")
_PIP = re.compile(r"^pip[0-9.]*$")
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n()]")


class FsChangeRecord(FsChange, Payload):
    """One change of an `fs.snapshot` or `channel.snapshot`, as the `fs.change` virtual event.
    `exec_id` is the snapshot's exec, None for changes between execs. A channel change keeps
    the agent the kernel records put it on and their seqs. It is not a registered event type."""

    exec_id: str | None = None
    agent: str | None = None
    evidence: list[int] = Field(default_factory=list)


def program(argv: Sequence[str], path: str) -> str:
    """The program a command line runs: argv[0]'s base name, or the busybox applet."""
    name = posixpath.basename(argv[0]) if argv and argv[0] else posixpath.basename(path)
    if name == BUSYBOX and len(argv) > 1:
        return posixpath.basename(argv[1])
    return name


def installs(argv: Sequence[str], path: str = "") -> bool:
    """Whether a command line installs packages or makes an environment: pip, uv, venv and
    the like. Their files are expected to hold startup hooks."""
    prog = program(argv, path)
    rest = list(argv[1:])
    if _PIP.match(prog) or prog in ("pipx", "easy_install"):
        return bool(_INSTALL_WORDS & set(rest))
    if prog == "uv":
        return bool(_UV_WORDS & set(rest[:3]))
    if prog in ("virtualenv", "ensurepip"):
        return True
    if prog in ("poetry", "pdm", "conda", "mamba", "micromamba", "hatch", "pipenv"):
        return bool(_ENV_WORDS & set(rest[:3]))
    if _PYTHON.match(prog) and "-m" in rest:
        module = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
        if module in ("venv", "ensurepip", "virtualenv"):
            return True
        if module == "pip":
            return bool(_INSTALL_WORDS & set(rest))
        if module == "uv":
            return bool(_UV_WORDS & set(rest))
    return _PYTHON.match(prog) is not None and "setup.py" in [
        posixpath.basename(a) for a in rest[:1]
    ]


@dataclass(slots=True)
class CallInfo:
    """One tool call. `step` is the agent step that made it; `arguments` the parsed JSON, None
    when it does not parse."""

    call_id: str
    agent_id: str | None
    name: str
    seq: int
    step: int
    text: str
    arguments: dict[str, JsonValue] | None
    result_seq: int | None = None
    error: bool = False
    denied: bool = False


@dataclass(slots=True)
class ExecInfo:
    """One exec. `authored` says the command is text the agent wrote itself, as with bash;
    a tool that builds its own commands, like edit, gives False. `installs` says a process of it
    installed packages. `stdio` holds the inodes of its stdout and stderr."""

    exec_id: str
    agent_id: str | None
    phase: ExecPhase
    call_id: str | None
    command: str
    user: str
    cwd: str
    env: dict[str, str]
    seq: int
    tool: str | None = None
    step: int | None = None
    authored: bool = False
    installs: bool = False
    root: int | None = None
    root_ts: int | None = None
    stdio: tuple[int, int] | None = None
    finished: ExecFinished | None = None
    finished_seq: int | None = None
    spilled_seq: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ProcNode:
    """One process of an agent's sandbox. `start` and `end` are kernel times; `start` is the
    fork when it was recorded, else the first exec. `images` holds every program it ran."""

    agent_id: str | None
    pid: int
    ppid: int
    exec_id: str | None
    start: int
    seq: int
    ns_pid: int | None = None
    uid: int | None = None
    path: str = ""
    argv: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    root: bool = False
    end: int | None = None
    installs: bool = False
    images: list[tuple[str, list[str]]] = field(default_factory=list)

    def alive(self, ts: int) -> bool:
        return self.end is None or self.end >= ts

    @property
    def program(self) -> str:
        return program(self.argv, self.path)


@dataclass(frozen=True, slots=True)
class Write:
    """One write to a path: by the kernel (open, close, rename, symlink, link, truncate,
    delete) or seen by a snapshot. `agent` says an agent did it: an agent or replay exec, or a
    snapshot change between execs."""

    seq: int
    agent_id: str | None
    exec_id: str | None
    phase: ExecPhase | None
    kind: str
    agent: bool
    content: BlobRef | None = None
    sha256: str | None = None
    target: str | None = None


@dataclass(slots=True)
class FileState:
    writes: list[Write] = field(default_factory=list)
    agent_last: bool = False
    agent_ever: bool = False
    content: BlobRef | None = None


def _kernel_ts(payload: Payload) -> int:
    return payload.ts_kernel if isinstance(payload, KernelPayload) else 0


class State:
    """Derived facts of one run, updated from every event in seq order before any signal sees
    it. `run_id` names the run whose blobs hold the events' content."""

    def __init__(self, *, run_id: str, store: EventStore) -> None:
        self.run_id = run_id
        self.store = store
        self.execs: dict[str, ExecInfo] = {}
        self.calls: dict[str, CallInfo] = {}
        self.steps: dict[str | None, int] = {}
        self.denied: list[CallInfo] = []
        self.finished: dict[str | None, AgentFinished] = {}
        self.scored: dict[str | None, tuple[int, TaskScored]] = {}
        self.files: dict[tuple[str | None, str], FileState] = {}
        self._pids: dict[int, list[ProcNode]] = {}
        self._ns: dict[tuple[str | None, int], list[ProcNode]] = {}
        self._stdio: dict[tuple[str | None, int], str] = {}
        self._payloads: dict[int, Payload | None] = {}
        self._blobs: OrderedDict[str, str] = OrderedDict()

    async def update(self, event: Event) -> list[Event]:
        """Take `event` in. Returns it with the virtual events it holds, each already taken in,
        in the order the signals should see them."""
        self._payloads.clear()
        out = [event]
        payload = self.payload(event)
        if payload is None:
            return out
        try:
            if isinstance(payload, ToolCall):
                call = self._call(event, payload)
                if not isinstance(payload.arguments, str):
                    await self._call_blob(call, payload.arguments)
            elif isinstance(payload, ExecStarted):
                self._exec(event, payload, await self.text(payload.command))
            else:
                self._apply(event, payload)
            if isinstance(payload, AuditSpill):
                out += await self._spilled(event, payload)
            elif isinstance(payload, FsSnapshot):
                out += await self._changes(event, payload)
            elif isinstance(payload, ChannelSnapshot):
                out += await self._channel_changes(event, payload)
        except Exception:
            log.exception("monitor state could not take in event %s", event.seq)
        return out

    def payload(self, event: Event) -> Payload | None:
        """The parsed payload of `event`, None for an unknown type or one that does not parse."""
        key = id(event)
        if key in self._payloads:
            return self._payloads[key]
        try:
            if event.type == FS_CHANGE:
                payload: Payload | None = FsChangeRecord.model_validate(event.data)
            else:
                payload = event.payload()
        except ValidationError:
            payload = None
        self._payloads[key] = payload
        return payload

    async def text(self, content: Content | None, limit: int = TEXT_CAP) -> str:
        """`content` as text, a blob read through the store; at most `limit` bytes."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content[:limit] if len(content) > limit else content
        cached = self._blobs.get(content.sha256)
        if cached is not None:
            self._blobs.move_to_end(content.sha256)
            return cached[:limit]
        try:
            data = await self.store.get_blob(self.run_id, content.sha256)
        except Exception as e:
            log.warning("monitor could not read blob %s: %s", content.sha256, e)
            return ""
        text = data[:TEXT_CAP].decode(errors="replace")
        self._blobs[content.sha256] = text
        if len(self._blobs) > BLOB_CACHE:
            self._blobs.popitem(last=False)
        return text[:limit]

    def exec_of(self, event: Event) -> ExecInfo | None:
        """The exec an event belongs to, by its `exec_id` or its tool call."""
        payload = self.payload(event)
        exec_id = getattr(payload, "exec_id", None)
        if isinstance(exec_id, str):
            return self.execs.get(exec_id)
        return None

    def phase_of(self, event: Event) -> ExecPhase | None:
        """The exec phase of an event. The agent's own model and tool events are in the agent
        phase and the verdict in the verifier phase; events outside any exec have none."""
        info = self.exec_of(event)
        if info is not None:
            return info.phase
        if event.type.startswith(("tool.", "agent.", "model.")):
            return "agent"
        if event.type == "task.scored":
            return "verifier"
        return None

    def call_of(self, event: Event) -> CallInfo | None:
        payload = self.payload(event)
        call_id = getattr(payload, "call_id", None)
        if isinstance(call_id, str):
            return self.calls.get(call_id)
        info = self.exec_of(event)
        if info is not None and info.call_id is not None:
            return self.calls.get(info.call_id)
        return None

    def node(self, agent_id: str | None, pid: int, ts: int) -> ProcNode | None:
        """The recorded process `pid` of `agent_id` at kernel time `ts`."""
        return _at([n for n in self._pids.get(pid, ()) if n.agent_id == agent_id], ts)

    def node_ns(self, agent_id: str | None, ns_pid: int, ts: int) -> ProcNode | None:
        """The recorded process of `agent_id` whose pid in the sandbox is `ns_pid` at `ts`."""
        return _at(self._ns.get((agent_id, ns_pid)), ts)

    def owner(self, pid: int, ts: int) -> ProcNode | None:
        """The recorded process `pid` at `ts`, whatever agent it belongs to."""
        return _at(self._pids.get(pid), ts)

    def actor(self, event: Event) -> ProcNode | None:
        """The process a kernel event is about."""
        payload = self.payload(event)
        if not isinstance(payload, KernelPayload):
            return None
        return self.node(event.agent_id, payload.pid, payload.ts_kernel)

    def target(self, event: Event) -> ProcNode | None:
        """The process a `proc.signal` or `ptrace.event` targets. A signal names a host pid;
        ptrace names a pid in the sandbox, except traceme, which names the tracer's host pid."""
        payload = self.payload(event)
        if isinstance(payload, ProcSignal):
            return self.owner(payload.target, payload.ts_kernel)
        if isinstance(payload, PtraceEvent):
            if payload.request == "traceme":
                return self.node(event.agent_id, payload.target, payload.ts_kernel)
            return self.node_ns(event.agent_id, payload.target, payload.ts_kernel)
        return None

    def target_harness(self, event: Event) -> bool:
        """Whether a signal or ptrace targets the sandbox's own processes."""
        return self.harness(self.target(event))

    def harness(self, node: ProcNode | None) -> bool:
        """Whether a process is the sandbox's own rather than an exec's: its init, keepalive or
        a runner process, none of which the audit attributes to an exec. An unrecorded process
        counts as one, since every process an agent exec starts is recorded."""
        return node is None or node.exec_id is None

    def ancestors(self, node: ProcNode) -> Iterator[ProcNode]:
        """`node`'s parents, nearest first, as far as they were recorded."""
        seen = {id(node)}
        current = node
        while True:
            parent = self.node(current.agent_id, current.ppid, current.start)
            if parent is None or id(parent) in seen:
                return
            seen.add(id(parent))
            yield parent
            current = parent

    def in_tree(self, caller: ProcNode | None, target: ProcNode | None) -> bool:
        """Whether `target` belongs to `caller`'s exec, or is `caller` or one of its children."""
        if caller is None or target is None:
            return False
        if target is caller or (caller.exec_id is not None and target.exec_id == caller.exec_id):
            return True
        return any(a is caller for a in self.ancestors(target))

    def proc_path(self, event: Event, path: str) -> ProcNode | bool | None:
        """For a /proc/<pid> path: True when it names the caller itself, the process it names
        (None when it is not recorded), or False for a path outside /proc/<pid>."""
        m = _PROC_PATH.match(path)
        if m is None:
            return False
        if m.group(1) in ("self", "thread-self"):
            return True
        payload = self.payload(event)
        ts = _kernel_ts(payload) if payload is not None else 0
        return self.node_ns(event.agent_id, int(m.group(1)), ts)

    def stdio_owner(self, agent_id: str | None, inode: int) -> str | None:
        """The exec whose stdout or stderr is the pipe `inode`."""
        return self._stdio.get((agent_id, inode))

    def agent_written(self, agent_id: str | None, path: str) -> bool:
        """Whether the latest write to `path` was the agent's."""
        state = self.files.get((agent_id, path))
        return state is not None and state.agent_last

    def ever_written(self, agent_id: str | None, path: str) -> bool:
        """Whether the agent wrote `path` at any point."""
        state = self.files.get((agent_id, path))
        return state is not None and state.agent_ever

    def wrote_under(self, agent_id: str | None, directory: str) -> bool:
        """Whether the latest write to some file under `directory` was the agent's."""
        prefix = directory.rstrip("/") + "/"
        return any(
            a == agent_id and path.startswith(prefix) and f.agent_last
            for (a, path), f in self.files.items()
        )

    def exec_wrote(self, agent_id: str | None, path: str, exec_id: str | None) -> bool:
        """Whether the exec `exec_id` wrote `path`."""
        state = self.files.get((agent_id, path))
        return (
            exec_id is not None
            and state is not None
            and any(w.exec_id == exec_id and w.kind != "delete" for w in state.writes)
        )

    def file(self, agent_id: str | None, path: str) -> FileState | None:
        return self.files.get((agent_id, path))

    def _apply(self, event: Event, payload: Payload) -> None:
        agent = event.agent_id
        match payload:
            case AgentStep():
                self.steps[agent] = payload.n
                for call_id in payload.tool_calls:
                    if call_id in self.calls:
                        self.calls[call_id].step = payload.n
            case ToolResult():
                call = self.calls.get(payload.call_id)
                if call is not None:
                    call.result_seq, call.error = event.seq, payload.error
                    call.denied = payload.denied
                    if payload.denied:
                        self.denied.append(call)
            case ExecFinished():
                info = self.execs.get(payload.exec_id)
                if info is not None:
                    info.finished, info.finished_seq = payload, event.seq
            case ProcExec():
                self._proc_exec(event, payload)
            case ProcFork():
                self._fork(event, payload)
            case ProcExit():
                node = self.node(agent, payload.pid, payload.ts_kernel)
                if node is not None and node.end is None:
                    node.end = payload.ts_kernel
            case FileAccess():
                self._access(event, payload)
            case FileWrite():
                self._write(event, payload.exec_id, payload.path, "close")
            case FileSetattr():
                if payload.valid & ATTR_SIZE and payload.size is not None:
                    self._write(event, payload.exec_id, payload.path, "truncate")
            case FsChangeRecord():
                self._change(event, payload)
            case AgentFinished():
                self.finished[agent] = payload
            case TaskScored():
                self.scored[agent] = (event.seq, payload)
            case _:
                pass

    def _call(self, event: Event, payload: ToolCall) -> CallInfo:
        text = payload.arguments if isinstance(payload.arguments, str) else ""
        call = CallInfo(
            call_id=payload.call_id,
            agent_id=event.agent_id,
            name=payload.name,
            seq=event.seq,
            step=self.steps.get(event.agent_id, 0) + 1,
            text=text,
            arguments=_object(text),
        )
        self.calls[payload.call_id] = call
        return call

    async def _call_blob(self, call: CallInfo, content: Content) -> None:
        call.text = await self.text(content)
        call.arguments = _object(call.text)

    def _exec(self, event: Event, payload: ExecStarted, command: str) -> None:
        call = self.calls.get(payload.call_id) if payload.call_id else None
        authored = False
        if call is not None and call.arguments is not None and command:
            authored = any(v == command for v in call.arguments.values())
        self.execs[payload.exec_id] = ExecInfo(
            exec_id=payload.exec_id,
            agent_id=event.agent_id,
            phase=payload.phase,
            call_id=payload.call_id,
            command=command,
            user=payload.user,
            cwd=payload.cwd,
            env=dict(payload.env),
            seq=event.seq,
            tool=call.name if call else None,
            step=call.step if call else None,
            authored=authored,
            installs=authored and any(installs(words) for words in _segments(command)),
        )

    def _proc_exec(self, event: Event, payload: ProcExec) -> None:
        agent, ts = event.agent_id, payload.ts_kernel
        node = self.node(agent, payload.pid, ts)
        if node is None:
            parent = self.node(agent, payload.ppid, ts)
            node = ProcNode(
                agent_id=agent,
                pid=payload.pid,
                ppid=payload.ppid,
                exec_id=payload.exec_id,
                start=ts,
                seq=event.seq,
                installs=parent.installs if parent is not None and not payload.root else False,
            )
            self._add(self._pids, payload.pid, node)
            if parent is not None and payload.env_inherited:
                node.env = parent.env
        if node.ns_pid != payload.ns_pid:
            node.ns_pid = payload.ns_pid
            self._add(self._ns, (agent, payload.ns_pid), node)
        node.uid, node.path, node.argv, node.root = (
            payload.uid,
            payload.path,
            payload.argv,
            (node.root or payload.root),
        )
        if payload.env:
            node.env = payload.env
        node.images = [*node.images[-HISTORY + 1 :], (payload.path, payload.argv)]
        node.installs = node.installs or installs(payload.argv, payload.path)
        info = self.execs.get(payload.exec_id) if payload.exec_id else None
        if info is None:
            return
        if node.installs:
            info.installs = True
        if payload.root and info.root is None:
            info.root, info.root_ts = payload.pid, ts
            if payload.fds is not None:
                info.stdio = (payload.fds[1].ino, payload.fds[2].ino)
                for ino in info.stdio:
                    self._stdio[(agent, ino)] = info.exec_id

    def _fork(self, event: Event, payload: ProcFork) -> None:
        agent, ts = event.agent_id, payload.ts_kernel
        parent = self.node(agent, payload.ppid, ts)
        known = self.node(agent, payload.pid, ts)
        if known is not None and known.start >= ts:
            # The child's exec came first; this fork names the parent it had before a reparent.
            known.start = ts
            known.installs = known.installs or (parent is not None and parent.installs)
            return
        node = ProcNode(
            agent_id=agent,
            pid=payload.pid,
            ppid=payload.ppid,
            exec_id=payload.exec_id,
            start=ts,
            seq=event.seq,
        )
        if parent is not None:
            node.path, node.argv, node.uid = parent.path, parent.argv, parent.uid
            node.env, node.installs = parent.env, parent.installs
        self._add(self._pids, payload.pid, node)

    def _access(self, event: Event, payload: FileAccess) -> None:
        exec_id = payload.exec_id
        match payload.op:
            case "write":
                self._write(event, exec_id, payload.path, "open")
            case "delete":
                self._write(event, exec_id, payload.path, "delete")
            case "rename":
                self._write(event, exec_id, payload.path, "delete")
                if payload.target is not None:
                    self._write(event, exec_id, payload.target, "rename", target=payload.path)
            case "symlink" | "link":
                self._write(event, exec_id, payload.path, payload.op, target=payload.target)
            case _:
                pass

    def _write(
        self,
        event: Event,
        exec_id: str | None,
        path: str,
        kind: str,
        *,
        target: str | None = None,
        content: BlobRef | None = None,
        sha256: str | None = None,
        snapshot: bool = False,
    ) -> None:
        info = self.execs.get(exec_id) if exec_id else None
        phase = info.phase if info is not None else None
        agent = phase in AGENT_PHASES if info is not None else snapshot
        write = Write(
            seq=event.seq,
            agent_id=event.agent_id,
            exec_id=exec_id,
            phase=phase,
            kind=kind,
            agent=agent,
            content=content,
            sha256=sha256,
            target=target,
        )
        state = self.files.setdefault((event.agent_id, path), FileState())
        state.writes = [*state.writes[-HISTORY + 1 :], write]
        if kind == "delete":
            state.agent_last = False
            state.content = None
            return
        state.agent_last = agent
        state.agent_ever = state.agent_ever or agent
        if snapshot:
            state.content = content

    def _change(self, event: Event, change: FsChangeRecord) -> None:
        kind = "delete" if change.change == "removed" else "snapshot"
        if change.change == "meta":
            return
        self._write(
            event,
            change.exec_id,
            change.path,
            kind,
            content=change.content,
            sha256=change.sha256,
            target=change.target,
            snapshot=True,
        )

    async def _spilled(self, event: Event, spill: AuditSpill) -> list[Event]:
        out: list[Event] = []
        info = self.execs.get(spill.exec_id) if spill.exec_id else None
        if info is not None:
            info.spilled_seq.append(event.seq)
        for item in await self._lines(spill.records):
            kind, data = item.get("type"), item.get("data")
            if not isinstance(kind, str) or not isinstance(data, dict):
                continue
            virtual = _virtual(event, kind, data)
            payload = self.payload(virtual)
            if payload is not None:
                self._apply(virtual, payload)
                out.append(virtual)
        return out

    async def _changes(self, event: Event, snapshot: FsSnapshot) -> list[Event]:
        if snapshot.base is None:
            return []
        changes: list[dict[str, JsonValue]] = [c.model_dump(mode="json") for c in snapshot.changes]
        if snapshot.more is not None:
            changes += await self._lines(snapshot.more)
        out: list[Event] = []
        for change in changes:
            virtual = _virtual(event, FS_CHANGE, {**change, "exec_id": snapshot.exec_id})
            payload = self.payload(virtual)
            if isinstance(payload, FsChangeRecord):
                self._change(virtual, payload)
                out.append(virtual)
        return out

    async def _channel_changes(self, event: Event, snapshot: ChannelSnapshot) -> list[Event]:
        """A shared folder's changes become `fs.change` events too, each on the member the
        kernel records show wrote it."""
        changes: list[dict[str, JsonValue]] = [c.model_dump(mode="json") for c in snapshot.changes]
        if snapshot.more is not None:
            changes += await self._lines(snapshot.more)
        out: list[Event] = []
        for change in changes:
            data = {**change, "exec_id": snapshot.exec_id}
            writer = data.get("agent")
            virtual = _virtual(event, FS_CHANGE, data)
            if isinstance(writer, str):
                virtual = virtual.model_copy(update={"agent_id": writer})
            payload = self.payload(virtual)
            if isinstance(payload, FsChangeRecord):
                self._change(virtual, payload)
                out.append(virtual)
        return out

    async def _lines(self, ref: BlobRef) -> list[dict[str, JsonValue]]:
        try:
            data = await self.store.get_blob(self.run_id, ref.sha256)
        except Exception as e:
            log.warning("monitor could not read blob %s: %s", ref.sha256, e)
            return []
        out: list[dict[str, JsonValue]] = []
        for line in data.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    @staticmethod
    def _add[K](index: dict[K, list[ProcNode]], key: K, node: ProcNode) -> None:
        nodes = index.setdefault(key, [])
        nodes.append(node)
        if len(nodes) > HISTORY:
            del nodes[0]


def _virtual(holder: Event, kind: str, data: dict[str, JsonValue]) -> Event:
    return holder.model_copy(update={"type": kind, "data": data})


def _at(nodes: list[ProcNode] | None, ts: int) -> ProcNode | None:
    """The process alive at `ts`: the latest started by then, else the first started after,
    whose fork was not recorded."""
    if not nodes:
        return None
    live = [n for n in nodes if n.alive(ts)]
    started = [n for n in live if n.start <= ts]
    if started:
        return started[-1]
    return live[0] if live else None


def _segments(command: str) -> Iterator[list[str]]:
    """The simple commands of a shell line, split on separators and pipes, as words."""
    for part in _SEPARATORS.split(command):
        words = part.split()
        while words and "=" in words[0] and not words[0].startswith("="):
            words = words[1:]
        if words:
            yield words


def _object(text: str) -> dict[str, JsonValue] | None:
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
