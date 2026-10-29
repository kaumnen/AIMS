"""The run-wide channel sweep: what passed between agents through their channels, as edges from
the agent that wrote an item to each other agent that read it, with what the reader did next.

An item is a board message (`channel.message`) or a change of a shared folder that the kernel
records put down to one agent (in `channel.snapshot`). A board item is read by each
`channel.read` of another agent whose range covers its id; a folder item by the first
`file.access` read of its path by another agent after it was written. Each read is followed by
the reader's next tool calls. Records are framed as in a trajectory, under the same key, so the
extract call's quotes are checked the same way. Above `max_tokens` the older items collapse to
one line each, oldest first, then drop out with a count.
"""

import json
import logging
import posixpath
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import Field

from aims.events import (
    BlobError,
    Category,
    Draft,
    Event,
    EventStore,
    MonitorFlag,
    MonitorSweep,
    SweepTrigger,
)
from aims.models import ModelClient, OutputSchema, Profile
from aims.monitor.config import ChannelSweepConfig
from aims.monitor.render import Rendered, _capped, cut, escape, frame, glossary, strings, tokens
from aims.monitor.sweep import (
    _IMPORTANCE,
    _SUSPICION,
    CATEGORY_NAMES,
    EXTRACT,
    QUOTE_CHARS,
    SweepError,
    SweepResult,
    _Calls,
    _clamp,
    _Extract,
    _Loose,
    _object,
    _parse,
    ask,
    check,
)
from aims.monitor.texts import MonitorTexts
from aims.scenario import Scenario
from aims.scenario.model import FolderChannel

log = logging.getLogger("aims.monitor")

NAME = "channels"
TEXT_BYTES = 4096
EXCERPT_BYTES = 2048
ARGUMENT_BYTES = 1024
SHORT_BYTES = 160
COLLAPSE_TO = 0.75
RECENT_READS = 16
PATHS = 4096
# Execs the runner makes in an agent's sandbox, whose reads are not the agent's.
RUNNER_PHASES = frozenset({"setup", "verifier"})


@dataclass(slots=True)
class _Read:
    event: Event
    reader: str


@dataclass(slots=True)
class _Item:
    """A board message or a folder change, with the reads of it by other agents. `seq` is
    when it was written: the message, or the first kernel record of the change."""

    event: Event
    channel: str
    writer: str
    seq: int
    message: int | None = None
    change: dict[str, Any] | None = None
    reads: list[_Read] = field(default_factory=list)

    def last(self, upto: int) -> int:
        return max([self.seq, *(r.event.seq for r in self.reads if r.event.seq <= upto)])

    def read_by(self, reader: str) -> bool:
        return any(r.reader == reader for r in self.reads)


class _Lines:
    def __init__(self, key: bytes, label: str) -> None:
        self.key = key
        self.label = label
        self.lines: list[str] = []
        self.frames: dict[str, int] = {}
        self.sources: dict[int, str] = {}
        self.labels: dict[int, str] = {}
        self.agents: dict[int, str] = {}
        self.types: set[str] = set()

    def line(self, text: str) -> None:
        self.lines.append(text)

    def record(self, seq: int, kind: str, agent: str, fields: dict[str, Any]) -> None:
        f = frame(self.key, seq)
        line = escape({"frame": f, "type": kind, "agent": agent, **fields})
        self.lines.append(line)
        self.frames[f] = seq
        source = "\n".join([line, *strings(fields)])
        self.sources[seq] = f"{self.sources[seq]}\n{source}" if seq in self.sources else source
        self.labels[seq] = f"{self.label} {kind}"
        self.agents[seq] = agent
        self.types.add(kind)

    @property
    def tokens(self) -> int:
        return tokens("\n".join(self.lines))


class ChannelLog:
    """The items of a run's channels, built append-only from its events in seq order.

    `add` takes every event of the run, the monitor's own already skipped. `count` counts the
    items and reads taken so far, `seen` is the last seq taken. `key` is the run's secret HMAC
    key for frame ids; `run_id` the run whose blobs the events refer to.
    """

    def __init__(self, run_id: str, store: EventStore, key: bytes, scenario: Scenario) -> None:
        self.run_id = run_id
        self.store = store
        self.key = key
        self.channels = list(scenario.channels)
        self.members = {c.name: scenario.members(c) for c in scenario.channels}
        self._folders = {
            c.name: posixpath.normpath(c.path)
            for c in scenario.channels
            if isinstance(c, FolderChannel)
        }
        self._items: list[_Item] = []
        self._posts: dict[tuple[str, int], _Item] = {}
        self._paths: OrderedDict[str, list[_Item]] = OrderedDict()
        self._recent: OrderedDict[str, list[Event]] = OrderedDict()
        self._calls: dict[str, list[Event]] = {}
        self._runner: set[str] = set()
        self._last = -1
        self._blobs: dict[str, str] = {}
        self.count = 0
        self.seen = -1

    @property
    def last_seq(self) -> int:
        """The seq of the last item or read, -1 before the first."""
        return self._last

    def add(self, event: Event) -> None:
        agent, data = event.agent_id, event.data
        self.seen = max(self.seen, event.seq)
        match event.type:
            case "exec.started" if data.get("phase") in RUNNER_PHASES:
                self._runner.add(str(data.get("exec_id")))
            case "tool.call" if agent is not None:
                self._calls.setdefault(agent, []).append(event)
            case "channel.message" if agent is not None:
                item = _Item(
                    event, str(data.get("channel")), agent, event.seq, message=_int(data.get("id"))
                )
                self._items.append(item)
                self._posts[(item.channel, item.message or 0)] = item
                self._last = event.seq
                self.count += 1
            case "channel.read" if agent is not None:
                self._board_read(event, agent)
            case "channel.snapshot":
                self._snapshot(event)
            case "file.access" if agent is not None:
                self._folder_read(event, agent)

    def _board_read(self, event: Event, reader: str) -> None:
        d = event.data
        after, through = _int(d.get("after")) or 0, _int(d.get("through")) or 0
        channel = str(d.get("channel"))
        for n in range(after + 1, through + 1):
            item = self._posts.get((channel, n))
            if item is not None and item.writer != reader and not item.read_by(reader):
                item.reads.append(_Read(event, reader))
                self._last = event.seq
                self.count += 1

    def _snapshot(self, event: Event) -> None:
        channel = str(event.data.get("channel"))
        for change in _list(event.data.get("changes")):
            writer = change.get("agent") if isinstance(change, dict) else None
            if not isinstance(writer, str):
                continue
            evidence = [s for s in _list(change.get("evidence")) if isinstance(s, int)]
            seq = min(evidence) if evidence else event.seq
            path = posixpath.normpath(str(change.get("path")))
            item = _Item(event, channel, writer, seq, change=change)
            self._items.append(item)
            self.count += 1
            items = self._paths.setdefault(path, [])
            items.append(item)
            self._paths.move_to_end(path)
            while len(self._paths) > PATHS:
                self._paths.popitem(last=False)
            for read in self._recent.get(path, []):
                if read.seq > seq and read.agent_id not in (None, writer):
                    self._take_read(item, read)
            self._last = max(self._last, event.seq)

    def _folder_read(self, event: Event, reader: str) -> None:
        d = event.data
        if d.get("op") != "read" or d.get("errno") is not None or d.get("partial"):
            return
        if str(d.get("exec_id")) in self._runner or not isinstance(d.get("path"), str):
            return
        path = posixpath.normpath(str(d.get("path")))
        if not any(_inside(path, root) for root in self._folders.values()):
            return
        recent = self._recent.setdefault(path, [])
        recent.append(event)
        del recent[:-RECENT_READS]
        self._recent.move_to_end(path)
        while len(self._recent) > PATHS:
            self._recent.popitem(last=False)
        latest = next((i for i in reversed(self._paths.get(path, [])) if i.seq < event.seq), None)
        if latest is not None and latest.writer != reader:
            self._take_read(latest, event)

    def _take_read(self, item: _Item, event: Event) -> None:
        reader = event.agent_id
        if reader is None or item.read_by(reader):
            return
        item.reads.append(_Read(event, reader))
        item.reads.sort(key=lambda r: r.event.seq)
        self._last = max(self._last, event.seq)
        self.count += 1

    async def render(
        self,
        texts: MonitorTexts,
        *,
        after: int = -1,
        max_tokens: int,
        next_calls: int,
        upto: int | None = None,
    ) -> tuple[Rendered, dict[int, str]]:
        """Every item up to seq `upto` (default: every event taken), those with a record after
        seq `after` in the review range, and the agent of each record shown."""
        upto = self.seen if upto is None else upto
        items = [i for i in self._items if i.seq <= upto]
        head = ["CHANNEL RECORDS OF THE RUN"]
        for c in self.channels:
            where = f" at {self._folders[c.name]}" if c.name in self._folders else ""
            head.append(
                f"CHANNEL {c.name}: {c.kind}{where}, members {', '.join(self.members[c.name])}"
            )
        blocks = [await self._block(n, item, upto, next_calls) for n, item in enumerate(items, 1)]
        fresh = [b for b, i in zip(blocks, items, strict=True) if i.last(upto) > after]
        old = [b for b, i in zip(blocks, items, strict=True) if i.last(upto) <= after]
        shorts = {id(b): await self._short(i, b, upto) for b, i in zip(blocks, items, strict=True)}
        collapsed: set[int] = set()
        dropped = 0
        size = tokens("\n".join(head)) + sum(b.tokens for b in blocks) + 64

        def shown(b: _Lines) -> _Lines:
            return shorts[id(b)] if id(b) in collapsed else b

        if size > max_tokens:
            for b in old:
                if size <= COLLAPSE_TO * max_tokens:
                    break
                collapsed.add(id(b))
                size -= b.tokens - shorts[id(b)].tokens
        while size > max_tokens and (old or fresh):
            b = old.pop(0) if old else fresh.pop(0)
            size -= shown(b).tokens
            dropped += 1
        lines = list(head)
        if dropped:
            lines.append(f"{dropped} older items not shown")
        out = _Lines(self.key, "")
        for b in old:
            out.lines += shown(b).lines
            _merge(out, shown(b))
        if after >= 0:
            lines += out.lines
            lines.append(texts["channel_marker"])
            out.lines = []
        for b in fresh:
            out.lines += b.lines
            _merge(out, b)
        lines += out.lines
        lines.append("END OF CHANNEL RECORDS")
        text = "\n".join(lines)
        rendered = Rendered(
            text=text,
            frames=out.frames,
            sources=out.sources,
            labels=out.labels,
            types=frozenset(out.types),
            step_from=0,
            step_to=0,
            seq_to=upto,
            tokens=tokens(text),
            collapsed=len(collapsed),
        )
        return rendered, out.agents

    async def _block(self, n: int, item: _Item, upto: int, next_calls: int) -> _Lines:
        out = _Lines(self.key, f"item {n}")
        when = item.event.ts.strftime("%H:%M:%S")
        if item.change is None:
            out.line(f"ITEM {n}: message {item.message} on {item.channel} by {item.writer}, {when}")
            text = await self._text(item.event.data.get("text"))
            out.record(
                item.seq,
                "channel.message",
                item.writer,
                {"channel": item.channel, "id": item.message, "text": cut(text, TEXT_BYTES)},
            )
        else:
            c = item.change
            out.line(f"ITEM {n}: change on {item.channel} by {item.writer}, {when}")
            fields: dict[str, Any] = {
                "channel": item.channel,
                "path": cut(str(c.get("path")), SHORT_BYTES * 2),
                "change": c.get("change"),
                "kind": c.get("kind"),
                "size": c.get("size"),
            }
            if c.get("target") is not None:
                fields["target"] = cut(str(c.get("target")), SHORT_BYTES * 2)
            content = c.get("content")
            if isinstance(content, dict) and c.get("change") != "removed":
                fields["content"] = cut(await self._text(content), EXCERPT_BYTES)
            out.record(item.seq, "channel.change", item.writer, fields)
        for read in item.reads:
            if read.event.seq > upto:
                continue
            out.line(f"READ BY {read.reader}, {read.event.ts.strftime('%H:%M:%S')}")
            d = read.event.data
            if read.event.type == "channel.read":
                fields = {"channel": d.get("channel"), "after": d.get("after")}
                fields["through"] = d.get("through")
            else:
                fields = {"op": "read", "path": cut(str(d.get("path")), SHORT_BYTES * 2)}
            out.record(read.event.seq, read.event.type, read.reader, fields)
            for call in self._next(read, upto, next_calls):
                args = await self._text(call.data.get("arguments"))
                try:
                    parsed: Any = json.loads(args) if args.strip() else {}
                except ValueError:
                    parsed = args
                fields = {
                    "name": call.data.get("name"),
                    "arguments": _capped(parsed, ARGUMENT_BYTES),
                }
                out.record(call.seq, "tool.call", read.reader, fields)
        return out

    async def _short(self, item: _Item, block: _Lines, upto: int) -> _Lines:
        out = _Lines(self.key, block.label)
        readers = [r.reader for r in item.reads if r.event.seq <= upto]
        fields: dict[str, Any] = {"channel": item.channel, "readers": readers}
        if item.change is None:
            text = " ".join((await self._text(item.event.data.get("text"))).split())
            fields["text"] = cut(text, SHORT_BYTES)
        else:
            fields["path"] = cut(str(item.change.get("path")), SHORT_BYTES)
        out.record(item.seq, "channel.item", item.writer, fields)
        return out

    def _next(self, read: _Read, upto: int, count: int) -> list[Event]:
        calls = self._calls.get(read.reader, [])
        return [c for c in calls if read.event.seq < c.seq <= upto][:count]

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
                    data = await self.store.get_blob(self.run_id, sha)
                except BlobError, KeyError, OSError:
                    log.warning("channel log: blob %s does not read", sha)
                    return f"(blob {sha} of {value.get('size')} bytes could not be read)"
                self._blobs[sha] = data.decode(errors="replace")
            return self._blobs[sha]
        return str(value)


class _Hit(_Loose):
    quote: int
    agents: list[str] = Field(default_factory=list)
    category: str
    subcategory: str | None = None
    justification: str = ""
    importance: int
    suspicion: int


class _Evaluate(_Loose):
    hits: list[_Hit] = Field(default_factory=list)
    suspicion: int


def evaluate_schema(agents: Sequence[str]) -> OutputSchema:
    """The evaluate call's schema, with the run's agents as the names a hit may give."""
    hit = _object(
        {
            "quote": {"type": "integer", "description": "The quote's id"},
            "agents": {
                "type": "array",
                "items": {"type": "string", "enum": list(agents)},
                "description": "The agents whose behaviour the quote shows",
            },
            "category": {"type": "string", "enum": CATEGORY_NAMES},
            "subcategory": {"type": ["string", "null"]},
            "justification": {"type": "string"},
            "importance": _IMPORTANCE,
            "suspicion": _SUSPICION,
        }
    )
    return OutputSchema(
        name="channel_evaluate",
        json_schema=_object({"hits": {"type": "array", "items": hit}, "suspicion": _SUSPICION}),
    )


@dataclass(slots=True)
class ChannelResult:
    """What one channel sweep found, and the finding to lead the deep sweep of each agent a
    flag names."""

    result: SweepResult
    leads: dict[str, str] = field(default_factory=dict)


class ChannelSweeper:
    """The channel sweep of one run. The extract call runs on `extractor`, the evaluate call
    on `profile`; calls go through `client` with role `sweep` and no agent id."""

    def __init__(
        self,
        *,
        config: ChannelSweepConfig,
        profile: Profile,
        extractor: Profile | None,
        texts: MonitorTexts,
        client: ModelClient,
        store: EventStore,
        log: ChannelLog,
        tasks: dict[str, str],
    ) -> None:
        self.config = config
        self.profile = profile
        self.extractor = (
            profile
            if extractor is None
            else extractor.model_copy(
                update={"effort": config.effort, "max_output_tokens": config.max_output_tokens}
            )
        )
        self.texts = texts
        self.client = client
        self.store = store
        self.log = log
        self.tasks = tasks
        self.reviewed = -1
        categories = "\n".join(f"{c}: {d}" for c, d in texts.categories.items())
        self._system = (
            f"{texts['channel_system']}\n\nCATEGORIES\n{categories}\n\n"
            f"IMPORTANCE AND SUSPICION\n{texts['rubric']}"
        )
        self._schema = evaluate_schema(list(tasks))

    async def sweep(self, trigger: SweepTrigger, *, coalesced: int = 1) -> ChannelResult:
        """Review the items with a record not yet reviewed, with every item as context."""
        start = time.monotonic()
        calls = _Calls()
        upto = self.log.seen
        rendered, agents = await self.log.render(
            self.texts,
            after=self.reviewed,
            max_tokens=self.config.max_tokens,
            next_calls=self.config.next_calls,
            upto=upto,
        )
        flags: list[Draft] = []
        leads: dict[str, str] = {}
        dropped = unverified = 0
        suspicion: int | None = None
        summary = ""
        error: str | None = None
        try:
            body = "\n\n".join(
                [rendered.text, glossary(rendered.types), self.texts["channel_extract"]]
            )
            extracted = await self._extract(body, calls)
            summary = extracted.summary
            quotes, dropped = check(extracted.quotes, rendered)
            suspicion = 0
            if quotes:
                evaluated = _parse(_Evaluate, await self._evaluate(quotes, rendered, calls))
                suspicion = _clamp(evaluated.suspicion, 0, 100)
                by_id = {q.id: q for q in quotes}
                for hit in evaluated.hits:
                    quote = by_id.get(hit.quote)
                    if quote is None or hit.category not in CATEGORY_NAMES:
                        dropped += 1
                        continue
                    named = [a for a in hit.agents if a in self.tasks]
                    named = named or [agents[s] for s in quote.seqs if s in agents]
                    if not named:
                        dropped += 1
                        continue
                    importance = _clamp(hit.importance, 1, 5)
                    if not quote.verified:
                        importance = max(1, importance - 1)
                        unverified += len(set(named))
                    text = await self.store.content(self.client.run_id, quote.text)
                    for agent in dict.fromkeys(named):
                        flag = MonitorFlag(
                            layer="sweep",
                            source=NAME,
                            category=cast(Category, hit.category),
                            importance=importance,
                            evidence=quote.seqs,
                            justification=hit.justification,
                            subcategory=hit.subcategory or None,
                            lane=NAME,
                            suspicion=_clamp(hit.suspicion, 0, 100),
                            quote=text,
                            quote_verified=quote.verified,
                        )
                        flags.append(Draft(flag, agent))
                        leads.setdefault(agent, hit.justification or quote.concern)
        except SweepError as e:
            error = str(e)
            flags, leads, suspicion = [], {}, None
            log.warning("the channel sweep failed: %s", e)
        else:
            self.reviewed = upto
        sweep = MonitorSweep(
            name=NAME,
            lane=NAME,
            parts=["evidence", "messages"],
            trigger=trigger,
            step_from=0,
            step_to=0,
            seq_to=max(upto, 0),
            calls=calls.ids,
            hits=len(flags),
            dropped=dropped,
            unverified=unverified,
            suspicion=suspicion,
            history_suspicion=suspicion,
            summary=await self.store.content(self.client.run_id, summary),
            tokens=rendered.tokens,
            collapsed=rendered.collapsed,
            coalesced=coalesced,
            cost_usd=round(calls.cost_usd, 10),
            duration_ms=int((time.monotonic() - start) * 1000),
            error=error,
        )
        result = SweepResult(
            sweeps=[Draft(sweep)], flags=flags, cost_usd=calls.cost_usd, failed=error is not None
        )
        return ChannelResult(result, leads)

    async def _extract(self, body: str, calls: _Calls) -> _Extract:
        """The extract call on the extractor, again on the sweep model when that fails."""
        try:
            value = await ask(self.client, self.extractor, self._system, body, EXTRACT, calls)
            return _parse(_Extract, value)
        except SweepError as e:
            if self.extractor is self.profile:
                raise
            log.warning("the channel extract on the extractor failed, again: %s", e)
        value = await ask(self.client, self.profile, self._system, body, EXTRACT, calls)
        return _parse(_Extract, value)

    async def _evaluate(self, quotes: Iterable[Any], rendered: Rendered, calls: _Calls) -> Any:
        lines = [
            escape(
                {
                    "id": q.id,
                    "from": [rendered.labels.get(s, "") for s in q.seqs],
                    "quote": q.text[:QUOTE_CHARS],
                    "verified": q.verified,
                    "note": q.concern,
                }
            )
            for q in quotes
        ]
        tasks = "\n".join(
            f"{a}: {json.dumps(t, ensure_ascii=False)}" for a, t in self.tasks.items()
        )
        body = "\n\n".join(
            [
                f"TASKS GIVEN TO THE AGENTS\n{tasks}",
                "QUOTES\n" + "\n".join(lines),
                self.texts["channel_evaluate"],
            ]
        )
        return await ask(self.client, self.profile, self._system, body, self._schema, calls)


def _merge(out: _Lines, other: _Lines) -> None:
    out.frames |= other.frames
    for seq, source in other.sources.items():
        out.sources[seq] = f"{out.sources[seq]}\n{source}" if seq in out.sources else source
    out.labels |= other.labels
    out.agents |= other.agents
    out.types |= other.types


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root.rstrip('/')}/")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
