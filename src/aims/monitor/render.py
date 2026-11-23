"""One agent's trajectory as the sweep models read it.

The agent's events are grouped into steps: the preamble (step 0: the agent's start, its task and
the task's setup), then one step per model call of the agent loop. Within a step the evidence
comes first: each tool call, what it ran (exec facts and a digest of the kernel records), the
output the agent was given, other records of the step; then the agent's own words; then the
inputs the harness gave it after the step.

Every record is one line of JSON that starts with its frame id: HMAC-SHA256 of the event's seq
(its decimal digits) under the run's key, cut to 48 bits, as hex. Strings are JSON escaped, so
nothing the agent writes can start a line, and the agent never sees the key, so it can neither
forge a record nor guess a frame id. Lines that are not JSON are written by AIMS.

A step is closed once the next step starts. Closed steps render once and are reused, and the
review range marker comes after the records, so each render is a prefix of the next one up to the
open step or a collapse, which keeps provider prompt caches warm across sweeps. Above
`collapse_tokens`, older steps with no marked record and no denied call collapse to one line
each, oldest first, until the render is under three quarters of the cap; a collapsed step stays
collapsed, so the prefix moves in jumps. Tokens are estimated as UTF-8 bytes / 4, which can
underestimate these renders by a quarter to a third on some tokenizers, so collapse may come
later than the cap suggests.
"""

import hashlib
import hmac
import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from aims.events import SERVICE_PREFIX, BlobError, Event, EventStore, SweepPart
from aims.events.schema import stated_size
from aims.monitor.texts import MonitorTexts
from aims.text import deeper, scrub

log = logging.getLogger("aims.monitor")

FRAME_HEX = 12
OUTPUT_BYTES = 2048
TEXT_BYTES = 8192
REASONING_BYTES = 16_384
ARGUMENT_BYTES = 4096
ARGUMENT_DEPTH = 64
FIELD_BYTES = 512
SHORT_BYTES = 160
ARGV_ITEMS = 64
CHANGES = 30
COLLAPSE_TO = 0.75

KERNEL = frozenset(
    {
        "proc.exec",
        "proc.fork",
        "proc.exit",
        "file.access",
        "file.look",
        "file.write",
        "file.setattr",
        "file.xattr",
        "fs.snapshot",
        "channel.snapshot",
        "net.connect",
        "net.listen",
        "sys.denied",
        "proc.output",
        "ipc.event",
        "ptrace.event",
        "proc.signal",
        "mem.exec",
        "ns.event",
        "audit.lost",
        "audit.spill",
    }
)
SKIP = frozenset(
    {
        "model.request",
        "resource.sample",
        "run.started",
        "run.finished",
        "sandbox.started",
        "sandbox.freeze_failed",
        "sandbox.outside_pause",
        "sandbox.stopped",
        "sandbox.archived",
        "log.truncated",
        "task.scored",
        "operator.action",
        # Records of a service the run hosts, which the monitor never reads.
        "service.request",
        "service.ended",
    }
)
ATTEMPTS = frozenset({"ipc.event", "mem.exec", "file.setattr", "file.xattr", "net.listen"})
PYCACHE = "__pycache__"
# The temporary name Python writes a bytecode file under before it renames it into place.
PYC_TMP = re.compile(r"[^/]+\.pyc\.\d+\Z")
# Execs the sweeps never see: the verifier runs after the agent and holds the grader.
HIDDEN_PHASES = frozenset({"verifier"})
NARRATIVE: frozenset[SweepPart] = frozenset({"messages", "reasoning"})


def frame(key: bytes, seq: int) -> str:
    """The frame id of the event `seq` under the run's `key`."""
    return hmac.new(key, str(seq).encode(), hashlib.sha256).hexdigest()[:FRAME_HEX]


def tokens(text: str) -> int:
    """A token estimate: UTF-8 bytes / 4, rounded up."""
    return (len(text.encode()) + 3) // 4


def escape(value: Any) -> str:
    """`value` as one line of JSON, with every control, format and separator character escaped
    so text cannot break or disguise a line."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if text.isascii():
        return text
    return "".join(_safe(ch) for ch in text)


def _safe(ch: str) -> str:
    if ch.isascii() or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
        return ch
    return json.dumps(ch)[1:-1]


def cut(text: str, limit: int) -> str | dict[str, Any]:
    """`text` whole within `limit` bytes, else its head and tail of half the limit each. A
    lone surrogate is written `\\ud800` first. The bytes a `Capped` text left out count as cut."""
    left = text.left if isinstance(text, Capped) else 0
    text = scrub(text)
    data = text.encode()
    if len(data) <= limit and not left:
        return text
    head = data[: limit // 2].decode(errors="ignore")
    tail = data[-(limit // 2) :].decode(errors="ignore")
    gone = len(data) - len(head.encode()) - len(tail.encode()) + left
    return {"head": head, "tail": tail, "cut_bytes": gone}


class Capped(str):
    """A blob's text held at its two ends only; `left` counts the bytes between them. Any cut
    of it to half of TEXT_READ or less shows what a cut of the whole text would."""

    left: int

    def __new__(cls, text: str, left: int) -> Capped:
        out = super().__new__(cls, text)
        out.left = left
        return out


TEXT_READ = 1024 * 1024
"""The most bytes of a blob's text the renders hold: the whole text within it, else its first
and last half of it. Every cut they make is far smaller."""


async def blob_text(store: EventStore, run_id: str, sha256: str, size: int | None) -> str:
    """The text of a blob, whole within TEXT_READ bytes, else a `Capped` of its two ends. The
    read checks the whole blob and holds no more. Raises BlobError, KeyError or OSError."""
    half = TEXT_READ // 2
    ends = await store.get_ends(run_id, sha256, TEXT_READ, half, size)
    if ends.size <= TEXT_READ:
        return ends.head
    head = ends.head.encode()[:half].decode(errors="ignore")
    held = len(head.encode()) + len(ends.tail.encode())
    return Capped(head + ends.tail, ends.size - held)


def arguments(text: str) -> Any:
    """Tool call arguments read as JSON, else the text as is. A `Capped` text stays text: its
    two ends joined may parse as JSON the agent never sent."""
    if isinstance(text, Capped):
        return text
    try:
        parsed: Any = scrub(json.loads(text)) if text.strip() else {}
    except ValueError, RecursionError:
        return text
    return text if deeper(parsed, ARGUMENT_DEPTH) else parsed


def one_line(text: str) -> str:
    """`text` with each run of whitespace made one space. A `Capped` text keeps its count of
    the bytes left out."""
    line = " ".join(text.split())
    return Capped(line, text.left) if isinstance(text, Capped) else line


def strings(value: Any) -> Iterable[str]:
    """Every string in `value`, depth first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings(item)


def glossary(types: Iterable[str], texts: MonitorTexts) -> str:
    """What each record type in `types` is, from the glossary of `texts`. A type it leaves out
    is not explained."""
    lines = [texts["header_glossary"], texts["glossary_format"]]
    for name in sorted(set(types)):
        if name.startswith(SERVICE_PREFIX):
            continue
        doc = texts.gloss(name)
        if not doc:
            continue
        if name in ATTEMPTS:
            doc = texts["glossary_attempt"].format(doc=doc)
        lines.append(f"{name}: {doc}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Rendered:
    """A render of the trajectory. `frames` maps each frame id shown to its seq; `sources` maps a
    seq to the text a quote citing it is checked against, `labels` to where it was shown.
    `step_from` to `step_to` is the review range (the steps shown for a window), `seq_to` the
    last seq the render covers, `collapsed` the steps shown as one line."""

    text: str
    frames: dict[str, int]
    sources: dict[int, str]
    labels: dict[int, str]
    types: frozenset[str]
    step_from: int
    step_to: int
    seq_to: int
    tokens: int
    collapsed: int


@dataclass(slots=True)
class _Exec:
    start: Event
    step: int
    finish: Event | None = None
    records: list[Event] = field(default_factory=list)


@dataclass(slots=True)
class _Call:
    event: Event
    execs: list[_Exec] = field(default_factory=list)
    result: Event | None = None


@dataclass(slots=True)
class _Step:
    n: int
    head: Event | None = None
    response: Event | None = None
    calls: list[_Call] = field(default_factory=list)
    execs: list[_Exec] = field(default_factory=list)
    late: list[Event] = field(default_factory=list)
    notes: list[Event] = field(default_factory=list)
    inputs: list[Event] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    denied: bool = False
    shown: dict[int, set[int]] | None = None

    def label(self, texts: MonitorTexts) -> str:
        """Where a record of this step was shown, with `{type}` for the record's type."""
        return texts["sweep_label_preamble" if self.n == 0 else "sweep_label_step"]


@dataclass(frozen=True, slots=True)
class _Body:
    text: str
    frames: dict[str, int]
    sources: dict[int, str]
    labels: dict[int, str]
    types: frozenset[str]

    @property
    def tokens(self) -> int:
        return tokens(self.text)


class _Out:
    def __init__(self, key: bytes, upto: int, label: str, n: int) -> None:
        self.key = key
        self.upto = upto
        self.label = label
        self.n = n
        self.lines: list[str] = []
        self.frames: dict[str, int] = {}
        self.sources: dict[int, str] = {}
        self.labels: dict[int, str] = {}
        self.types: set[str] = set()

    def line(self, text: str) -> None:
        self.lines.append(text)

    def record(self, event: Event, fields: dict[str, Any], kind: str | None = None) -> None:
        if event.seq > self.upto:
            return
        kind = kind or event.type
        f = frame(self.key, event.seq)
        line = escape({"frame": f, "type": kind, **fields})
        self.lines.append(line)
        self.frames[f] = event.seq
        self.sources[event.seq] = "\n".join([line, *strings(fields)])
        self.labels[event.seq] = self.label.format(n=self.n, type=kind)
        self.types.add(kind)

    def merge(self, other: _Out) -> None:
        self.lines += other.lines
        self.frames |= other.frames
        self.sources |= other.sources
        self.labels |= other.labels
        self.types |= other.types

    def body(self) -> _Body:
        return _Body(
            "\n".join(self.lines),
            self.frames,
            self.sources,
            self.labels,
            frozenset(self.types),
        )


def _bytecode_cache(op: object, path: str) -> bool:
    """Whether a refused call is Python writing its bytecode cache, which it tries on every
    import: the mkdir of a `__pycache__` folder, or the open of a temporary file in one."""
    parts = path.split("/")
    if "." in parts or ".." in parts:
        return False
    if op == "mkdir":
        return parts[-1] == PYCACHE
    return (
        op == "denied"
        and len(parts) > 1
        and parts[-2] == PYCACHE
        and bool(PYC_TMP.match(parts[-1]))
    )


def _policy(event: Event, written: set[str]) -> tuple[str, tuple[Any, ...] | None, int, bool]:
    """How the digest treats a kernel record: its cap group, its dedupe key (None hides it as
    routine), the cap, and whether the key dedupes across the whole trajectory."""
    d, t = event.data, event.type
    match t:
        case "proc.exec":
            return t, None if d.get("root") else ("seq", event.seq), 20, False
        case "proc.exit":
            clean = d.get("exit_code") in (0, None) and not d.get("signal") and not d.get("oom")
            return t, None if clean else ("seq", event.seq), 10, False
        case "proc.fork":
            return t, None, 0, False
        case "file.access":
            op, path = d.get("op"), d.get("path")
            group = f"file.access {op}"
            if op == "read":
                return group, ("read", path), 20, True
            if op == "write" and path in written:
                return group, None, 0, False
            if d.get("errno") is not None and _bytecode_cache(op, str(path)):
                return group, None, 0, False
            return group, (op, path, d.get("target"), d.get("errno")), 20, False
        case "file.look":
            op = d.get("op")
            return f"{t} {op}", ("look", op, d.get("path"), d.get("errno")), 20, True
        case "file.write":
            return t, ("write", d.get("path")), 20, False
        case "file.setattr":
            return t, ("setattr", d.get("path")), 5, False
        case "file.xattr":
            key = ("xattr", d.get("op"), d.get("path"), d.get("name"), d.get("errno"))
            return t, key, 10, False
        case "fs.snapshot" | "channel.snapshot":
            changed = d.get("changes") or d.get("more") or d.get("truncated")
            return t, ("seq", event.seq) if changed else None, 5, False
        case "net.connect":
            return t, ("connect", d.get("address"), d.get("port")), 20, False
        case "net.listen":
            return t, ("listen", d.get("kind"), d.get("address"), d.get("port")), 10, False
        case "ipc.event":
            key = (_nul(d.get("path")), d.get("key"), d.get("id"))
            return t, ("ipc", d.get("kind"), *key), 10, True
        case "proc.output":
            return t, ("seq", event.seq), 5, False
        case _:
            return t, ("seq", event.seq), 10, False


def _written(records: Iterable[Event]) -> set[str]:
    return {str(e.data.get("path")) for e in records if e.type == "file.write"}


def _select(records: Sequence[Event], seen: set[tuple[Any, ...]]) -> set[int]:
    written = _written(records)
    local: set[tuple[Any, ...]] = set()
    counts: Counter[str] = Counter()
    shown: set[int] = set()
    for event in records:
        group, key, cap, run = _policy(event, written)
        if key is None:
            continue
        bucket = seen if run else local
        if key in bucket or counts[group] >= cap:
            continue
        bucket.add(key)
        counts[group] += 1
        shown.add(event.seq)
    return shown


def _nul(path: Any) -> Any:
    return path.split("\x00", 1)[0] if isinstance(path, str) else path


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _finished(event: Event) -> dict[str, Any]:
    f = event.data
    leftover = [
        {"pid": p.get("pid"), "command": cut(str(p.get("command", "")), FIELD_BYTES)}
        for p in _list(f.get("leftover"))
        if isinstance(p, dict)
    ]
    return {
        "end": f.get("end"),
        "exit_code": f.get("exit_code"),
        "duration_ms": f.get("duration_ms"),
    } | _some(
        leftover=leftover,
        leftover_killed=bool(leftover) and f.get("leftover_killed"),
        dropped=f.get("dropped") or None,
        spilled=f.get("spilled") or None,
        looks_skipped=f.get("looks_skipped") or None,
        error=f.get("error"),
    )


def _some(**fields: Any) -> dict[str, Any]:
    """The fields that are set: not None, not False, not empty."""
    return {k: v for k, v in fields.items() if v is not None and v is not False and v != []}


def _capped(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return cut(value, limit)
    if isinstance(value, dict):
        return {k: _capped(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_capped(v, limit) for v in value]
    return value


class Trajectory:
    """One agent's trajectory as the sweeps see it, built append-only from the run's events.

    `add` takes every event of the run in seq order, the monitor's own events and model calls
    already skipped, and keeps what concerns this agent. `mark` names seqs that a signal flagged:
    the render keeps them past every cap, and never collapses their step, without saying they
    were flagged. `key` is the run's secret HMAC key for frame ids; it never reaches the sandbox.
    `run_id` is the run whose blobs the events refer to. The headers of a render come from the
    texts it is given; the steps it keeps keep the headers they were made with.
    """

    def __init__(self, run_id: str, agent_id: str, store: EventStore, key: bytes) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.store = store
        self.key = key
        self.texts = MonitorTexts()
        self._steps: list[_Step] = [_Step(0)]
        self._last = -1
        self._responses: dict[str, Event] = {}
        self._pending: list[Event] = []
        self._execs: dict[str, _Exec] = {}
        self._where: dict[int, int] = {}
        self._marks: set[int] = set()
        self._seen: set[tuple[Any, ...]] = set()
        self._selected = 0
        self._bodies: dict[frozenset[str], dict[int, _Body]] = {}
        self._shorts: dict[int, _Body] = {}
        self._collapsed: dict[frozenset[str], set[int]] = {}
        self._blobs: dict[str, str] = {}
        self._hidden: set[str] = set()

    @property
    def steps(self) -> int:
        """Agent steps seen so far."""
        return self._steps[-1].n

    @property
    def last_seq(self) -> int:
        """The seq of the last event kept for this agent, -1 before the first."""
        return self._last

    def add(self, event: Event) -> None:
        if event.type == "channel.snapshot" and event.agent_id is None:
            event = self._own_changes(event)
        if event.agent_id != self.agent_id or event.type in SKIP:
            return
        if event.type.startswith("monitor."):
            return
        data = event.data
        if event.type == "exec.started" and data.get("phase") in HIDDEN_PHASES:
            self._hidden.add(str(data.get("exec_id")))
            return
        if data.get("exec_id") is not None and str(data.get("exec_id")) in self._hidden:
            return
        self._last = event.seq
        match event.type:
            case "model.response":
                self._responses[str(data.get("call_id"))] = event
                return
            case "tool.call" if data.get("model_call") is not None:
                self._pending.append(event)
                return
            case "agent.step":
                self._start(event)
                return
        index = len(self._steps) - 1
        step = self._steps[index]
        self._put(index, event)
        match event.type:
            case "exec.started":
                ex = _Exec(event, index)
                self._execs[str(data.get("exec_id"))] = ex
                call = self._call(step, data.get("call_id"))
                (step.execs if call is None else call.execs).append(ex)
            case "exec.finished":
                ex = self._execs.get(str(data.get("exec_id")))
                if ex is not None and ex.step == index:
                    ex.finish = event
                else:
                    step.late.append(event)
            case "tool.call":
                # A scripted agent's call, with no model call or step around it.
                step.calls.append(_Call(event))
            case "tool.result":
                call = self._call(step, data.get("call_id"))
                if call is None:
                    step.notes.append(event)
                else:
                    call.result = event
                step.denied = step.denied or bool(data.get("denied"))
            case "agent.input":
                (step.notes if data.get("kind") == "instruction" else step.inputs).append(event)
            case "agent.compacted":
                self._responses.pop(str(data.get("call_id")), None)
                step.notes.append(event)
            case kind if kind in KERNEL:
                ex = self._execs.get(str(data.get("exec_id")))
                if ex is not None and ex.step == index:
                    ex.records.append(event)
                else:
                    step.late.append(event)
            case _:
                step.notes.append(event)

    def _own_changes(self, event: Event) -> Event:
        """A shared folder's snapshot as this agent's, holding only the changes the kernel
        records put down to it; other agents' snapshots stay out."""
        changes = [
            c
            for c in _list(event.data.get("changes"))
            if isinstance(c, dict) and c.get("agent") == self.agent_id
        ]
        if not changes:
            return event
        data = {**event.data, "changes": changes, "more": None}
        return event.model_copy(update={"agent_id": self.agent_id, "data": data})

    def mark(self, seqs: Iterable[int]) -> None:
        for seq in seqs:
            self._marks.add(seq)
            index = self._where.get(seq)
            if index is not None:
                for bodies in self._bodies.values():
                    bodies.pop(index, None)

    def _start(self, event: Event) -> None:
        data = event.data
        call_id = str(data.get("call_id"))
        ids = {str(i) for i in _list(data.get("tool_calls"))}
        n = data.get("n")
        step = _Step(n if isinstance(n, int) else len(self._steps), head=event)
        step.response = self._responses.pop(call_id, None)
        step.calls = [
            _Call(c)
            for c in self._pending
            if c.data.get("model_call") == call_id or c.data.get("call_id") in ids
        ]
        self._responses.clear()
        self._pending.clear()
        self._steps.append(step)
        index = len(self._steps) - 1
        for e in (step.response, *(c.event for c in step.calls), event):
            if e is not None:
                self._put(index, e)

    def _put(self, index: int, event: Event) -> None:
        self._steps[index].seqs.append(event.seq)
        self._where[event.seq] = index

    @staticmethod
    def _call(step: _Step, call_id: Any) -> _Call | None:
        if call_id is None:
            return None
        return next((c for c in step.calls if c.event.data.get("call_id") == call_id), None)

    def _visible(self, upto: int) -> list[int]:
        return [i for i, s in enumerate(self._steps) if s.head is None or s.head.seq <= upto]

    def _closed(self, index: int, upto: int) -> bool:
        nxt = index + 1
        if nxt >= len(self._steps):
            return False
        head = self._steps[nxt].head
        return head is not None and head.seq <= upto

    def _fresh(self, index: int, after: int, upto: int) -> bool:
        return any(after < s <= upto for s in self._steps[index].seqs)

    def _marked(self, index: int) -> bool:
        return any(s in self._marks for s in self._steps[index].seqs)

    async def render(
        self,
        parts: Collection[SweepPart],
        *,
        texts: MonitorTexts,
        review_after: int | None = None,
        collapse_tokens: int | None = None,
        upto: int | None = None,
    ) -> Rendered:
        """The whole trajectory up to seq `upto` (default: everything added) with what `parts`
        shows. Steps with an event after seq `review_after` form the review range, named after
        the records; with none, the newest step does. Older steps collapse past
        `collapse_tokens`."""
        self.texts = texts
        upto = self._last if upto is None else upto
        key = frozenset(parts)
        visible = self._visible(upto)
        review = visible[-1]
        if review_after is not None:
            fresh = (i for i in visible if self._fresh(i, review_after, upto))
            review = next(fresh, review)
        bodies = {i: await self._body(i, key, upto) for i in visible}
        collapsed: set[int] = set()
        if collapse_tokens is not None:
            collapsed = await self._collapse(key, visible, bodies, review, collapse_tokens)
        marker = texts["range_marker"] if review_after is not None else None
        return await self._assemble(
            visible, bodies, collapsed, texts, upto, review, marker, step_from=None
        )

    async def window(
        self,
        parts: Collection[SweepPart],
        *,
        texts: MonitorTexts,
        first_step: int,
        upto: int | None = None,
    ) -> Rendered:
        """The preamble and the steps from `first_step` on, with nothing collapsed."""
        self.texts = texts
        upto = self._last if upto is None else upto
        key = frozenset(parts)
        visible = [i for i in self._visible(upto) if i == 0 or self._steps[i].n >= first_step]
        bodies = {i: await self._body(i, key, upto) for i in visible}
        first = next((i for i in visible if self._steps[i].n >= first_step), visible[-1])
        return await self._assemble(
            visible, bodies, set(), texts, upto, first, None, step_from=first_step
        )

    async def _assemble(
        self,
        visible: Sequence[int],
        bodies: dict[int, _Body],
        collapsed: set[int],
        texts: MonitorTexts,
        upto: int,
        review: int,
        marker: str | None,
        *,
        step_from: int | None,
    ) -> Rendered:
        first, last = self._steps[review].n, self._steps[visible[-1]].n
        if step_from is not None:
            first = min(step_from, last)
        lines = [texts["header_records"].format(agent=self.agent_id)]
        frames: dict[str, int] = {}
        sources: dict[int, str] = {}
        labels: dict[int, str] = {}
        types: set[str] = set()
        previous = False
        for i in visible:
            if i in collapsed:
                body = await self._short(i)
                if not previous:
                    lines.append(texts["header_older_steps"])
                lines.append(body.text)
            else:
                body = bodies[i]
                lines.append(body.text)
                lines.append(texts["reminder"].format(step=self._steps[i].n))
            previous = i in collapsed
            frames |= body.frames
            sources |= body.sources
            labels |= body.labels
            types |= body.types
        lines.append(texts["header_records_end"])
        if marker is not None:
            lines.append(marker.format(step_from=first, step_to=last))
        text = "\n".join(lines)
        return Rendered(
            text=text,
            frames=frames,
            sources=sources,
            labels=labels,
            types=frozenset(types),
            step_from=first,
            step_to=last,
            seq_to=upto,
            tokens=tokens(text),
            collapsed=len(collapsed),
        )

    async def _collapse(
        self,
        key: frozenset[str],
        visible: Sequence[int],
        bodies: dict[int, _Body],
        review: int,
        cap: int,
    ) -> set[int]:
        sticky = self._collapsed.setdefault(key, set())

        def collapsible(i: int) -> bool:
            return 0 < i < review and not self._steps[i].denied and not self._marked(i)

        current = {i for i in sticky if i in bodies and collapsible(i)}
        size = 0
        for i in visible:
            size += (await self._short(i)).tokens if i in current else bodies[i].tokens
        if size <= cap:
            return current
        for i in visible:
            if size <= COLLAPSE_TO * cap:
                break
            if i in current or not collapsible(i):
                continue
            current.add(i)
            sticky.add(i)
            size -= bodies[i].tokens - (await self._short(i)).tokens
        return current

    def _selection(self, index: int, closed: bool) -> dict[int, set[int]]:
        while self._selected < index:
            self._commit(self._selected)
        if closed:
            self._commit(index)
            shown = self._steps[index].shown
            assert shown is not None
            return shown
        return self._choose(self._steps[index], set(self._seen))

    def _commit(self, index: int) -> None:
        step = self._steps[index]
        if step.shown is None:
            step.shown = self._choose(step, self._seen)
        self._selected = max(self._selected, index + 1)

    @staticmethod
    def _choose(step: _Step, seen: set[tuple[Any, ...]]) -> dict[int, set[int]]:
        shown: dict[int, set[int]] = {}
        for ex in [*(ex for c in step.calls for ex in c.execs), *step.execs]:
            shown[ex.start.seq] = _select(ex.records, seen)
        shown[-1] = _select([e for e in step.late if e.type in KERNEL], seen)
        return shown

    async def _body(self, index: int, key: frozenset[str], upto: int) -> _Body:
        closed = self._closed(index, upto)
        cache = self._bodies.setdefault(key, {})
        if closed and index in cache:
            return cache[index]
        body = await self._build(index, key, upto, self._selection(index, closed))
        if closed:
            cache[index] = body
        return body

    async def _build(
        self, index: int, parts: frozenset[str], upto: int, shown: dict[int, set[int]]
    ) -> _Body:
        step = self._steps[index]
        texts = self.texts
        out = _Out(self.key, upto, step.label(texts), step.n)
        if step.head is None:
            out.line(texts["header_preamble"])
        else:
            time = step.head.ts.strftime("%H:%M:%S")
            out.line(texts["header_step"].format(n=step.n, time=time))
            status = step.head.data.get("status")
            if status != "completed":
                out.record(step.head, {"n": step.n, "status": status})
        if step.n == 0:
            for e in step.notes:
                await self._note(out, e, parts)
        for call in step.calls:
            if call.event.seq > upto:
                continue
            command = await self._tool_call(out, call.event)
            for ex in call.execs:
                await self._exec(out, ex, parts, shown, command)
            if call.result is not None:
                await self._result(out, call.result, parts)
        for ex in step.execs:
            await self._exec(out, ex, parts, shown, None)
        late = [e for e in step.late if e.seq <= upto]
        if late:
            sub = _Out(self.key, upto, step.label(texts), step.n)
            await self._digest(sub, late, shown.get(-1, set()), parts)
            if sub.lines:
                out.line(texts["header_leftover"])
                out.merge(sub)
        if step.n != 0:
            for e in step.notes:
                await self._note(out, e, parts)
        if step.response is not None and parts & NARRATIVE:
            await self._words(out, step.response, parts)
        for e in step.inputs:
            await self._note(out, e, parts)
        return out.body()

    async def _short(self, index: int) -> _Body:
        if index in self._shorts:
            return self._shorts[index]
        step = self._steps[index]
        assert step.head is not None
        out = _Out(self.key, step.head.seq, step.label(self.texts), step.n)
        calls: list[dict[str, Any]] = []
        for c in step.calls:
            args = one_line(await self._text(c.event.data.get("arguments")))
            entry: dict[str, Any] = {
                "name": c.event.data.get("name"),
                "arguments": cut(args, SHORT_BYTES),
            }
            exits = [ex.finish.data.get("exit_code") for ex in c.execs if ex.finish is not None]
            if exits:
                entry["exit_codes"] = exits
            if c.result is not None and c.result.data.get("error"):
                entry["error"] = True
            calls.append(entry)
        out.record(step.head, {"n": step.n, "calls": calls}, kind="step")
        body = out.body()
        self._shorts[index] = body
        return body

    async def _text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("sha256"), str):
            sha = value["sha256"]
            if sha not in self._blobs:
                if len(self._blobs) >= 64:
                    self._blobs.clear()
                try:
                    text = await blob_text(self.store, self.run_id, sha, stated_size(value))
                except BlobError, KeyError, OSError:
                    log.warning("trajectory of %s: blob %s does not read", self.agent_id, sha)
                    unread = self.texts["sweep_blob_unread"]
                    return unread.format(sha256=sha, size=value.get("size"))
                self._blobs[sha] = text
            return self._blobs[sha]
        return str(value)

    async def _tool_call(self, out: _Out, event: Event) -> str | None:
        parsed = arguments(await self._text(event.data.get("arguments")))
        command = parsed.get("command") if isinstance(parsed, dict) else None
        fields = {"name": event.data.get("name"), "arguments": _capped(parsed, ARGUMENT_BYTES)}
        out.record(event, fields)
        return command if isinstance(command, str) else None

    async def _exec(
        self,
        out: _Out,
        ex: _Exec,
        parts: frozenset[str],
        shown: dict[int, set[int]],
        command: str | None,
    ) -> None:
        d = ex.start.data
        text = await self._text(d.get("command"))
        fields: dict[str, Any] = {"phase": d.get("phase", "agent")}
        if text != command:
            fields["command"] = cut(text, ARGUMENT_BYTES)
        env = d.get("env") or {}
        fields |= _some(
            cwd=d.get("cwd"),
            user=d.get("user"),
            timeout_ms=d.get("timeout_ms"),
            stdin_bytes=d.get("stdin_bytes"),
            env=_capped(env, FIELD_BYTES) if env else None,
        )
        out.record(ex.start, fields)
        if ex.finish is not None:
            out.record(ex.finish, _finished(ex.finish))
        await self._digest(out, ex.records, shown.get(ex.start.seq, set()), parts)

    async def _digest(
        self, out: _Out, records: Sequence[Event], base: set[int], parts: frozenset[str]
    ) -> None:
        written = _written(records)
        hidden: Counter[str] = Counter()
        for event in records:
            if event.seq > out.upto:
                continue
            if event.seq in base or event.seq in self._marks or event.type not in KERNEL:
                out.record(event, await self._kernel(event, parts))
                continue
            group, key, _, _ = _policy(event, written)
            if key is not None or event.type != "fs.snapshot":
                hidden[group] += 1
        if hidden:
            counts = ", ".join(f"{group} {n}" for group, n in sorted(hidden.items()))
            out.line(self.texts["records_not_shown"].format(counts=counts))

    async def _kernel(self, event: Event, parts: frozenset[str]) -> dict[str, Any]:
        d = event.data
        pid = d.get("pid")
        match event.type:
            case "proc.exec":
                argv = [str(a) for a in _list(d.get("argv"))]
                path = str(d.get("path") or "")
                fields: dict[str, Any] = {
                    "pid": pid,
                    "ppid": d.get("ppid"),
                    "argv": [cut(a, FIELD_BYTES) for a in argv[:ARGV_ITEMS]],
                }
                if len(argv) > ARGV_ITEMS:
                    fields["argv_not_shown"] = len(argv) - ARGV_ITEMS
                if not argv or PurePosixPath(argv[0]).name != PurePosixPath(path).name:
                    fields["path"] = cut(path, FIELD_BYTES)
                return fields | _some(
                    nlink=0 if d.get("nlink") == 0 else None,
                    path_partial=d.get("path_partial"),
                    argv_partial=d.get("argv_partial"),
                )
            case "proc.exit":
                return {"pid": pid, "exit_code": d.get("exit_code")} | _some(
                    signal=d.get("signal"), oom=d.get("oom")
                )
            case "proc.fork":
                return {"pid": pid, "ppid": d.get("ppid")}
            case "file.access":
                return {"op": d.get("op"), "path": cut(str(d.get("path")), FIELD_BYTES)} | _some(
                    target=None if d.get("target") is None else cut(str(d["target"]), FIELD_BYTES),
                    errno=d.get("errno"),
                    partial=d.get("partial"),
                    pid=pid,
                )
            case "file.look":
                return {"op": d.get("op"), "path": cut(str(d.get("path")), FIELD_BYTES)} | _some(
                    errno=d.get("errno"), partial=d.get("partial"), pid=pid
                )
            case "file.write":
                return {"path": cut(str(d.get("path")), FIELD_BYTES), "size": d.get("size")} | (
                    _some(partial=d.get("partial"), pid=pid)
                )
            case "file.setattr":
                return {"path": cut(str(d.get("path")), FIELD_BYTES), "valid": d.get("valid")} | (
                    _some(
                        mode=d.get("mode"),
                        uid=d.get("uid"),
                        gid=d.get("gid"),
                        size=d.get("size"),
                        atime_us=d.get("atime_us"),
                        mtime_us=d.get("mtime_us"),
                        errno=d.get("errno"),
                        partial=d.get("partial"),
                        pid=pid,
                    )
                )
            case "file.xattr":
                return {
                    "op": d.get("op"),
                    "path": cut(str(d.get("path")), FIELD_BYTES),
                    "name": cut(str(d.get("name")), FIELD_BYTES),
                } | _some(
                    size=d.get("size"),
                    flags=d.get("flags"),
                    errno=d.get("errno"),
                    partial=d.get("partial"),
                    pid=pid,
                )
            case "fs.snapshot" | "channel.snapshot":
                changes = [
                    {
                        "path": cut(str(c.get("path")), FIELD_BYTES),
                        "change": c.get("change"),
                        "kind": c.get("kind"),
                        "size": c.get("size"),
                    }
                    | _some(
                        timestomp=c.get("timestomp"),
                        target=None
                        if c.get("target") is None
                        else cut(str(c.get("target")), FIELD_BYTES),
                    )
                    for c in _list(d.get("changes"))
                    if isinstance(c, dict)
                ]
                return (
                    _some(channel=d.get("channel"))
                    | {"changes": changes[:CHANGES]}
                    | _some(
                        changes_not_shown=len(changes) - CHANGES
                        if len(changes) > CHANGES
                        else None,
                        more=d.get("more") is not None,
                        truncated=d.get("truncated"),
                    )
                )
            case "net.connect":
                return {
                    "kind": d.get("kind"),
                    "family": d.get("family"),
                    "address": d.get("address"),
                    "port": d.get("port"),
                    "pid": pid,
                }
            case "net.listen":
                return {
                    "kind": d.get("kind"),
                    "family": d.get("family"),
                    "address": d.get("address"),
                } | _some(protocol=d.get("protocol"), port=d.get("port"), pid=pid)
            case "sys.denied":
                return {"syscall": d.get("syscall"), "errno": d.get("errno"), "pid": pid}
            case "proc.output":
                fields = {
                    "pid": pid,
                    "stream": d.get("stream"),
                    "bytes": d.get("bytes"),
                    "writes": d.get("writes"),
                }
                if "output" in parts:
                    fields["prefix"] = cut(await self._text(d.get("prefix")), FIELD_BYTES)
                return fields
            case "ipc.event":
                path = _nul(d.get("path"))
                return {"kind": d.get("kind")} | _some(
                    family=d.get("family"),
                    path=None if path is None else cut(str(path), FIELD_BYTES),
                    key=d.get("key"),
                    id=d.get("id"),
                    size=d.get("size"),
                    flags=d.get("flags"),
                    errno=d.get("errno"),
                    partial=d.get("partial"),
                    pid=pid,
                )
            case "ptrace.event":
                named = d.get("ns_target") if d.get("target") is None else None
                path = d.get("path")
                return {"pid": pid, "target": d.get("target"), "request": d.get("request")} | (
                    _some(
                        ns_target=named,
                        refused=d.get("refused"),
                        errno=d.get("errno"),
                        fd=d.get("fd"),
                        access=d.get("access"),
                        kind=d.get("kind"),
                        inode=d.get("inode"),
                        path=None if path is None else cut(str(path), FIELD_BYTES),
                        path_partial=d.get("path_partial"),
                    )
                )
            case "proc.signal":
                return {"pid": pid, "target": d.get("target"), "signal": d.get("signal")}
            case "mem.exec":
                return {
                    "kind": d.get("kind"),
                    "path": cut(str(d.get("path")), FIELD_BYTES),
                    "nlink": d.get("nlink"),
                } | _some(prot=d.get("prot"), partial=d.get("partial"), pid=pid)
            case "ns.event":
                return {"kind": d.get("kind"), "namespaces": d.get("namespaces"), "pid": pid}
            case "audit.lost":
                scope = d.get("scope")
                return {"count": d.get("count"), "reason": d.get("reason")} | _some(
                    scope=None if scope == "sandbox" else scope
                )
            case "audit.spill":
                return {"count": d.get("count"), "kinds": d.get("kinds")}
            case "exec.finished":
                return _finished(event)
            case _:
                return await self._generic(event)

    async def _generic(self, event: Event) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in event.data.items():
            if isinstance(v, dict) and set(v) == {"sha256", "size"}:
                v = await self._text(v)
            out[k] = _capped(v, FIELD_BYTES)
        return out

    async def _result(self, out: _Out, event: Event, parts: frozenset[str]) -> None:
        d = event.data
        denied = bool(d.get("denied"))
        fields: dict[str, Any] = {"error": bool(d.get("error"))} | _some(
            denied=denied,
            output_bytes=d.get("output_bytes"),
            truncated=d.get("truncated"),
            replayed=d.get("replayed"),
        )
        if "output" in parts and not denied:
            fields["output"] = cut(await self._text(d.get("output")), OUTPUT_BYTES)
        out.record(event, fields)

    async def _words(self, out: _Out, event: Event, parts: frozenset[str]) -> None:
        d = event.data
        fields: dict[str, Any] = {}
        if d.get("status") != "completed":
            fields["status"] = d.get("status")
        text = await self._text(d.get("text")) if "messages" in parts else ""
        reasoning = await self._text(d.get("reasoning")) if "reasoning" in parts else ""
        if text.strip():
            fields["text"] = cut(text, TEXT_BYTES)
        if reasoning.strip():
            fields["reasoning"] = cut(reasoning, REASONING_BYTES)
            fields["reasoning_kind"] = d.get("reasoning_kind")
        if "text" in fields or "reasoning" in fields:
            out.record(event, fields)

    async def _note(self, out: _Out, event: Event, parts: frozenset[str]) -> None:
        d = event.data
        match event.type:
            case "agent.started":
                fields: dict[str, Any] = {
                    "model": d.get("model"),
                    "tools": d.get("tools"),
                    "system": cut(await self._text(d.get("system")), TEXT_BYTES),
                }
            case "agent.input":
                fields = {
                    "kind": d.get("kind"),
                    "step": d.get("step"),
                    "text": cut(await self._text(d.get("text")), TEXT_BYTES),
                }
            case "agent.report":
                fields = {"message": cut(await self._text(d.get("message")), TEXT_BYTES)}
            case "agent.finished":
                answer = d.get("answer")
                fields = {"reason": d.get("reason")} | _some(
                    detail=d.get("detail"),
                    answer=None if answer is None else cut(await self._text(answer), TEXT_BYTES),
                    steps=d.get("steps"),
                )
            case "agent.compacted":
                fields = {
                    "strategy": d.get("strategy"),
                    "from_step": d.get("from_step"),
                    "to_step": d.get("to_step"),
                }
                if "messages" in parts and d.get("summary") is not None:
                    fields["summary"] = cut(await self._text(d.get("summary")), TEXT_BYTES)
            case "replay.divergence":
                fields = {"step": d.get("step"), "tool": d.get("tool"), "line": d.get("line")}
                if "output" in parts:
                    fields["recorded"] = cut(await self._text(d.get("recorded")), OUTPUT_BYTES)
                    fields["live"] = cut(await self._text(d.get("live")), OUTPUT_BYTES)
            case "tool.result":
                await self._result(out, event, parts)
                return
            case _:
                fields = await self._generic(event)
        out.record(event, fields)
