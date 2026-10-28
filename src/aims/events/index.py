import asyncio
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, Field, ValidationError

from aims.events.chain import Head
from aims.events.payloads import (
    Category,
    ExecPhase,
    FlagLayer,
    MonitorCleared,
    MonitorFlag,
    MonitorVerdict,
    ResourceSample,
    RunKind,
    TaskScored,
)
from aims.events.schema import SCHEMA_VERSION, Content, Event, Timestamp, format_ts

VERSION = 3
MAX_TEXT = 32 * 1024
MAX_PAGE = 1000

SCHEMA = """
create table runs (
    run_id text primary key,
    scenario text not null,
    status text not null,
    started_at text not null,
    ended_at text,
    last_seq integer not null,
    last_hash text not null,
    log_offset integer not null,
    kind text not null,
    origin text,
    flags integer not null default 0,
    max_importance integer,
    verdict_severity integer,
    severity integer not null default 0,
    categories text not null default '{}'
) strict;

create index runs_by_origin on runs (origin, run_id) where origin is not null;

create table events (
    id integer primary key,
    run_id text not null references runs (run_id),
    seq integer not null,
    ts text not null,
    agent_id text,
    type text not null,
    data text not null,
    prev_hash text not null,
    hash text not null,
    text text not null,
    unique (run_id, seq)
) strict;

create index events_by_type on events (run_id, type, seq);
create index events_by_agent on events (run_id, agent_id, seq);

create virtual table events_fts using fts5 (
    text, content = 'events', content_rowid = 'id', tokenize = 'trigram'
);

create trigger events_fts_insert after insert on events when new.text != '' begin
    insert into events_fts (rowid, text) values (new.id, new.text);
end;

create table samples (
    run_id text not null references runs (run_id),
    seq integer not null,
    agent_id text,
    sandbox_id text not null,
    ts_from integer not null,
    ts_to integer not null,
    count integer not null,
    hz integer not null,
    cpu_usec integer,
    cpu_user_usec integer,
    cpu_system_usec integer,
    cpu_throttled integer,
    cpu_throttled_usec integer,
    io_rbytes integer,
    io_wbytes integer,
    io_rios integer,
    io_wios integer,
    cpu_some_usec integer,
    cpu_full_usec integer,
    io_some_usec integer,
    io_full_usec integer,
    memory_some_usec integer,
    memory_full_usec integer,
    memory_bytes integer,
    pids integer,
    primary key (run_id, seq)
) strict;

create index samples_by_time on samples (run_id, agent_id, ts_from);

create table flags (
    run_id text not null references runs (run_id),
    seq integer not null,
    root integer not null,
    ts text not null,
    agent_id text,
    layer text not null,
    source text not null,
    lane text,
    category text not null,
    subcategory text,
    importance integer not null,
    suspicion integer,
    key text,
    count integer not null,
    final integer not null,
    phase text,
    quote_verified integer,
    first_seq integer,
    last_seq integer,
    evidence text not null,
    justification text not null,
    quote text,
    latest integer not null,
    reflags integer not null,
    status text not null,
    status_seq integer,
    status_source text,
    status_reason text,
    tainted integer not null,
    primary key (run_id, seq)
) strict;
"""

DROP = """
drop table if exists flags;
drop table if exists samples;
drop trigger if exists events_fts_insert;
drop table if exists events_fts;
drop table if exists events;
drop table if exists runs;
"""


FlagStatus = Literal["open", "cleared", "tainted", "confirmed", "rejected", "unclear"]
MONITOR_TYPES = frozenset(
    {MonitorFlag.type, MonitorCleared.type, MonitorVerdict.type, TaskScored.type}
)


class RunInfo(BaseModel):
    """A run as the index keeps it. `flags` counts its logical flags and `categories` them per
    category, whatever their status. `max_importance` is the highest importance of a flag not
    cleared or rejected, `verdict_severity` the judge's `max_severity`, and `severity` the
    higher of the two, 0 when there is neither."""

    run_id: str
    scenario: str
    status: str
    started_at: Timestamp
    ended_at: Timestamp | None
    events: int
    head: str
    kind: RunKind = "scenario"
    origin: str | None = None
    flags: int = 0
    max_importance: int | None = None
    verdict_severity: int | None = None
    severity: int = 0
    categories: dict[str, int] = Field(default_factory=dict)


class FlagInfo(BaseModel):
    """One logical flag: the first `monitor.flag` of a key, at `seq`, with its later re-flags
    folded in. `latest` is the seq of the key's last flag and `reflags` how many followed the
    first. `count`, `final`, `justification`, `quote`, `quote_verified` and `phase` come from the
    last one; `importance` is the highest, `evidence` the union and `first_seq` to `last_seq`
    the widest span.

    `status` is set by the latest event that names any flag of the key: `cleared` by
    `monitor.cleared`, `tainted` by `task.scored` or the verdict's `tainted`, `confirmed`,
    `rejected` or `unclear` by the verdict's flag reviews; `open` when none has. `status_seq` is
    that event, `status_source` the review, judge or verifier behind it. `tainted` stays true
    once any event named the flag as tainting the score."""

    run_id: str
    seq: int
    ts: Timestamp
    agent_id: str | None
    layer: FlagLayer
    source: str
    lane: str | None
    category: Category
    subcategory: str | None
    importance: int
    suspicion: int | None
    key: str | None
    count: int
    final: bool
    phase: ExecPhase | None
    quote_verified: bool | None
    quote: Content | None
    first_seq: int | None
    last_seq: int | None
    evidence: list[int]
    justification: str
    latest: int
    reflags: int
    status: FlagStatus
    status_seq: int | None
    status_source: str | None
    status_reason: str | None
    tainted: bool


FLAG_FIELDS = tuple(FlagInfo.model_fields)
FLAG_COLUMNS = (
    "run_id",
    "seq",
    "root",
    "ts",
    "agent_id",
    "layer",
    "source",
    "lane",
    "category",
    "subcategory",
    "importance",
    "suspicion",
    "key",
    "count",
    "final",
    "phase",
    "quote_verified",
    "first_seq",
    "last_seq",
    "evidence",
    "justification",
    "quote",
    "latest",
    "reflags",
    "status",
    "status_seq",
    "status_source",
    "status_reason",
    "tainted",
)
RUN_KEYS = (
    "run_id",
    "scenario",
    "status",
    "started_at",
    "ended_at",
    "events",
    "head",
    "kind",
    "origin",
    "flags",
    "max_importance",
    "verdict_severity",
    "severity",
    "categories",
)
AGGREGATE = """
update runs set
    flags = (select count(*) from flags where run_id = ?1 and seq = root),
    max_importance = (
        select max(importance) from flags
        where run_id = ?1 and seq = root and status not in ('cleared', 'rejected')
    ),
    categories = (
        select coalesce(json_group_object(category, n), '{}') from (
            select category, count(*) as n from flags where run_id = ?1 and seq = root
            group by category order by category
        )
    )
where run_id = ?1
"""


class SampleWindow(BaseModel):
    """A `resource.sample` window as the index keeps it: the last reading of each counter and the
    peak of each gauge. Usage in a window is the change from the window before it."""

    run_id: str
    seq: int
    agent_id: str | None
    sandbox_id: str
    ts_from: int
    ts_to: int
    count: int
    hz: int
    cpu_usec: int | None
    cpu_user_usec: int | None
    cpu_system_usec: int | None
    cpu_throttled: int | None
    cpu_throttled_usec: int | None
    io_rbytes: int | None
    io_wbytes: int | None
    io_rios: int | None
    io_wios: int | None
    cpu_some_usec: int | None
    cpu_full_usec: int | None
    io_some_usec: int | None
    io_full_usec: int | None
    memory_some_usec: int | None
    memory_full_usec: int | None
    memory_bytes: int | None
    pids: int | None


SAMPLE_FIELDS = tuple(SampleWindow.model_fields)


class Index:
    """Derived SQLite view of the run logs. Every row can be rebuilt from `events.jsonl`."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> Index:
        db = await aiosqlite.connect(path, isolation_level=None)
        try:
            await db.executescript(
                "pragma journal_mode = wal; pragma synchronous = normal;"
                " pragma busy_timeout = 5000;"
            )
            try:
                await db.execute("create virtual table temp.probe using fts5 (x, tokenize=trigram)")
                await db.execute("drop table temp.probe")
            except sqlite3.OperationalError as e:
                version = sqlite3.sqlite_version
                raise RuntimeError(f"SQLite {version} lacks FTS5 with trigram: {e}") from e
            index = cls(db)
            async with db.execute("pragma user_version") as cur:
                row = await cur.fetchone()
            if row is None or row[0] != VERSION:
                await index.reset()
        except BaseException:
            await db.close()
            raise
        return index

    async def close(self) -> None:
        await self._db.close()

    async def reset(self) -> None:
        async with self._lock:
            await self._db.executescript(
                f"begin immediate; {DROP} {SCHEMA} pragma user_version = {VERSION}; commit;"
            )

    async def head(self, run_id: str) -> tuple[Head, str] | None:
        """Last indexed position of a run and its status."""
        async with self._db.execute(
            "select last_seq, last_hash, log_offset, status from runs where run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        head = Head(seq=row[0], hash=row[1], offset=row[2], finished=row[3] != "running")
        return head, row[3]

    async def remove(self, run_id: str) -> None:
        """Forget a run: its events, their text index, its samples and its row."""
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                await self._db.execute(
                    "insert into events_fts (events_fts, rowid, text)"
                    " select 'delete', id, text from events where run_id = ? and text != ''",
                    (run_id,),
                )
                for table in ("flags", "samples", "events", "runs"):
                    await self._db.execute(f"delete from {table} where run_id = ?", (run_id,))
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

    async def mark_corrupt(self, run_id: str) -> None:
        async with self._lock:
            await self._db.execute("update runs set status = 'corrupt' where run_id = ?", (run_id,))

    async def add(self, events: Sequence[Event], head: Head) -> None:
        """Index events up to `head`. Rows already present are left alone."""
        if not events:
            return
        run_id = events[0].run_id
        rows = [
            (
                e.run_id,
                e.seq,
                format_ts(e.ts),
                e.agent_id,
                e.type,
                json.dumps(e.data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                e.prev_hash,
                e.hash,
                e.search_text()[:MAX_TEXT],
            )
            for e in events
        ]
        windows = [w for e in events if e.type == ResourceSample.type if (w := _window(e))]
        finished = next((e for e in events if e.type == "run.finished"), None)
        monitor = [e for e in events if e.type in MONITOR_TYPES]
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                first = events[0]
                if first.seq == 0:
                    origin = first.data.get("origin")
                    await self._db.execute(
                        "insert into runs (run_id, scenario, status, started_at, ended_at,"
                        " last_seq, last_hash, log_offset, kind, origin)"
                        " values (?, ?, 'running', ?, null, -1, '', 0, ?, ?)"
                        " on conflict do nothing",
                        (
                            run_id,
                            first.data["scenario"],
                            rows[0][2],
                            str(first.data.get("kind") or "scenario"),
                            origin if isinstance(origin, str) else None,
                        ),
                    )
                if monitor:
                    await self._monitor(run_id, monitor)
                await self._db.executemany(
                    "insert into events (run_id, seq, ts, agent_id, type, data, prev_hash, hash,"
                    " text) values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing",
                    rows,
                )
                if windows:
                    await self._db.executemany(
                        f"insert into samples ({', '.join(SAMPLE_FIELDS)}) values"
                        f" ({', '.join('?' * len(SAMPLE_FIELDS))}) on conflict do nothing",
                        [tuple(w[f] for f in SAMPLE_FIELDS) for w in windows],
                    )
                await self._db.execute(
                    "update runs set last_seq = ?, last_hash = ?, log_offset = ?,"
                    " status = coalesce(?, status), ended_at = coalesce(?, ended_at)"
                    " where run_id = ? and last_seq < ?",
                    (
                        head.seq,
                        head.hash,
                        head.offset,
                        None if finished is None else finished.data["status"],
                        None if finished is None else format_ts(finished.ts),
                        run_id,
                        head.seq,
                    ),
                )
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

    async def _monitor(self, run_id: str, events: Sequence[Event]) -> None:
        """Index the flags and flag statuses of events not indexed yet, then the run's flag
        aggregates. Runs inside `add`'s transaction, before the head moves."""
        async with self._db.execute("select last_seq from runs where run_id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        fresh = [e for e in events if e.seq > row[0]]
        touched = False
        for e in fresh:
            try:
                payload = e.payload()
            except ValidationError:
                continue
            if isinstance(payload, MonitorFlag):
                await self._flag(e, payload)
            elif isinstance(payload, MonitorCleared):
                await self._status(
                    e, [(s, "cleared", payload.source, payload.reason) for s in payload.flags]
                )
            elif isinstance(payload, MonitorVerdict):
                await self._status(e, [(s, "tainted", "judge", None) for s in payload.tainted])
                await self._status(
                    e, [(r.flag, r.status, "judge", r.reason) for r in payload.flags]
                )
                await self._db.execute(
                    "update runs set verdict_severity = ? where run_id = ?",
                    (payload.max_severity, run_id),
                )
            elif isinstance(payload, TaskScored) and payload.tainted:
                await self._status(e, [(s, "tainted", "verifier", None) for s in payload.tainted])
            else:
                continue
            touched = True
        if touched:
            await self._db.execute(AGGREGATE, (run_id,))
            await self._db.execute(
                "update runs set severity = max(coalesce(max_importance, 0),"
                " coalesce(verdict_severity, 0)) where run_id = ?",
                (run_id,),
            )

    async def _flag(self, e: Event, flag: MonitorFlag) -> None:
        head: Sequence[Any] | None = None
        if flag.first_flag is not None:
            async with self._db.execute(
                "select h.seq, h.importance, h.evidence, h.first_seq, h.last_seq, h.quote,"
                " h.quote_verified from flags f join flags h"
                " on h.run_id = f.run_id and h.seq = f.root where f.run_id = ? and f.seq = ?",
                (e.run_id, flag.first_flag),
            ) as cur:
                head = await cur.fetchone()
        quote = None if flag.quote is None else json.dumps(_dump(flag.quote))
        values = (
            e.run_id,
            e.seq,
            e.seq if head is None else head[0],
            format_ts(e.ts),
            e.agent_id,
            flag.layer,
            flag.source,
            flag.lane,
            flag.category,
            flag.subcategory,
            flag.importance,
            flag.suspicion,
            flag.key,
            flag.count,
            flag.final,
            flag.phase,
            flag.quote_verified,
            flag.first_seq,
            flag.last_seq,
            json.dumps(sorted(set(flag.evidence))),
            flag.justification,
            quote,
            e.seq,
            0,
            "open",
            None,
            None,
            None,
            False,
        )
        await self._db.execute(
            f"insert into flags ({', '.join(FLAG_COLUMNS)}) values"
            f" ({', '.join('?' * len(FLAG_COLUMNS))}) on conflict do nothing",
            values,
        )
        if head is None:
            return
        evidence = sorted(set(json.loads(head[2])) | set(flag.evidence))
        kept_quote = flag.quote is None
        await self._db.execute(
            "update flags set latest = ?, reflags = reflags + 1, importance = ?, count = ?,"
            " final = ?, evidence = ?, first_seq = ?, last_seq = ?, justification = ?,"
            " subcategory = coalesce(?, subcategory), suspicion = coalesce(?, suspicion),"
            " phase = coalesce(?, phase), quote = ?, quote_verified = ?"
            " where run_id = ? and seq = ?",
            (
                e.seq,
                max(head[1], flag.importance),
                flag.count,
                flag.final,
                json.dumps(evidence),
                _least(head[3], flag.first_seq),
                _most(head[4], flag.last_seq),
                flag.justification,
                flag.subcategory,
                flag.suspicion,
                flag.phase,
                head[5] if kept_quote else quote,
                head[6] if kept_quote else flag.quote_verified,
                e.run_id,
                head[0],
            ),
        )

    async def _status(self, e: Event, changes: list[tuple[int, str, str, str | None]]) -> None:
        """Set the status of the logical flag behind each named seq. Later events win."""
        if not changes:
            return
        await self._db.executemany(
            "update flags set status = ?, status_seq = ?, status_source = ?, status_reason = ?,"
            " tainted = tainted or ? where run_id = ?"
            " and seq = (select root from flags where run_id = ? and seq = ?)",
            [
                (status, e.seq, source, reason, status == "tainted", e.run_id, e.run_id, seq)
                for seq, status, source, reason in changes
            ],
        )

    async def run(self, run_id: str) -> RunInfo | None:
        runs = await self._runs("where run_id = ?", (run_id,))
        return runs[0] if runs else None

    async def runs(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        severity: int | None = None,
        category: str | None = None,
    ) -> list[RunInfo]:
        """Runs newest first. `severity` keeps runs whose `severity` is at least it, `category`
        those with a flag in it, whatever its status, `origin` the monitor runs of that run."""
        where: list[str] = []
        args: list[Any] = []
        if before:
            where.append("run_id < ?")
            args.append(before)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if origin is not None:
            where.append("origin = ?")
            args.append(origin)
        if severity is not None:
            where.append("severity >= ?")
            args.append(severity)
        if category is not None:
            where.append(
                "exists (select 1 from flags f where f.run_id = runs.run_id"
                " and f.seq = f.root and f.category = ?)"
            )
            args.append(category)
        clause = f"where {' and '.join(where)}" if where else ""
        return await self._runs(f"{clause} order by run_id desc limit ?", (*args, _cap(limit)))

    async def _runs(self, clause: str, args: Sequence[Any]) -> list[RunInfo]:
        async with self._db.execute(
            "select run_id, scenario, status, started_at, ended_at, last_seq + 1, last_hash, kind,"
            " origin, flags, max_importance, verdict_severity, severity, categories"
            f" from runs {clause}",
            args,
        ) as cur:
            rows = await cur.fetchall()
        return [
            RunInfo.model_validate(
                {**dict(zip(RUN_KEYS, row, strict=True)), "categories": json.loads(row[-1])}
            )
            for row in rows
        ]

    async def flags(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        layer: str | None = None,
        category: str | None = None,
        min_importance: int | None = None,
        status: Sequence[str] = (),
        agent_id: str | None = None,
        tainted: bool | None = None,
    ) -> list[FlagInfo]:
        """One page of a run's logical flags in seq order, after seq `after`."""
        where = ["run_id = ?", "seq = root"]
        args: list[Any] = [run_id]
        if after is not None:
            where.append("seq > ?")
            args.append(after)
        if layer is not None:
            where.append("layer = ?")
            args.append(layer)
        if category is not None:
            where.append("category = ?")
            args.append(category)
        if min_importance is not None:
            where.append("importance >= ?")
            args.append(min_importance)
        if status:
            where.append(f"status in ({', '.join('?' * len(status))})")
            args.extend(status)
        if agent_id is not None:
            where.append("agent_id = ?")
            args.append(agent_id)
        if tainted is not None:
            where.append("tainted = ?")
            args.append(tainted)
        args.append(_cap(limit))
        async with self._db.execute(
            f"select {', '.join(FLAG_FIELDS)} from flags where {' and '.join(where)}"
            " order by seq limit ?",
            args,
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for row in rows:
            item = dict(zip(FLAG_FIELDS, row, strict=True))
            item["evidence"] = json.loads(item["evidence"])
            item["quote"] = None if item["quote"] is None else json.loads(item["quote"])
            out.append(FlagInfo.model_validate(item))
        return out

    async def hashes(self, run_id: str, first: int, last: int) -> dict[int, str]:
        """The hash of each indexed event of a run from seq `first` to `last`."""
        async with self._db.execute(
            "select seq, hash from events where run_id = ? and seq between ? and ?",
            (run_id, first, last),
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}

    async def search(
        self, query: str, *, limit: int = 100, before: int | None = None
    ) -> tuple[list[Event], int | None]:
        """Events of every run whose text holds `query`, newest indexed first, and the cursor of
        the next page. Queries of three or more characters use the trigram index."""
        where: list[str] = []
        args: list[Any] = []
        joins = ""
        if len(query) >= 3:
            joins = "join events_fts f on f.rowid = e.id"
            where.append("events_fts match ?")
            args.append('"' + query.replace('"', '""') + '"')
        else:
            where.append("e.text like ? escape '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{escaped}%")
        if before is not None:
            where.append("e.id < ?")
            args.append(before)
        size = _cap(limit)
        args.append(size + 1)
        async with self._db.execute(
            "select e.id, e.run_id, e.seq, e.ts, e.agent_id, e.type, e.data, e.prev_hash, e.hash"
            f" from events e {joins} where {' and '.join(where)} order by e.id desc limit ?",
            args,
        ) as cur:
            rows = list(await cur.fetchall())
        more = len(rows) > size
        rows = rows[:size]
        events = [_event(r[1:]) for r in rows]
        return events, (rows[-1][0] if more and rows else None)

    async def events(
        self,
        run_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
        types: Sequence[str] = (),
        agent_id: str | None = None,
        query: str | None = None,
    ) -> list[Event]:
        """One page in seq order. With only `before` set, the page is the last events before it."""
        joins = ""
        where = ["e.run_id = ?"]
        args: list[Any] = [run_id]
        if after is not None:
            where.append("e.seq > ?")
            args.append(after)
        if before is not None:
            where.append("e.seq < ?")
            args.append(before)
        if types:
            where.append(f"e.type in ({', '.join('?' * len(types))})")
            args.extend(types)
        if agent_id is not None:
            where.append("e.agent_id = ?")
            args.append(agent_id)
        if query and len(query) >= 3:
            joins = "join events_fts f on f.rowid = e.id"
            where.append("events_fts match ?")
            args.append('"' + query.replace('"', '""') + '"')
        elif query:
            where.append("e.text like ? escape '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{escaped}%")
        backwards = before is not None and after is None
        args.append(_cap(limit))
        async with self._db.execute(
            "select e.run_id, e.seq, e.ts, e.agent_id, e.type, e.data, e.prev_hash, e.hash"
            f" from events e {joins} where {' and '.join(where)}"
            f" order by e.seq {'desc' if backwards else 'asc'} limit ?",
            args,
        ) as cur:
            rows = list(await cur.fetchall())
        if backwards:
            rows.reverse()
        return [_event(r) for r in rows]

    async def samples(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
        limit: int = MAX_PAGE,
    ) -> list[SampleWindow]:
        """Windows of a run that overlap [`ts_from`, `ts_to`] on the boot clock, oldest first."""
        where = ["run_id = ?"]
        args: list[Any] = [run_id]
        if agent_id is not None:
            where.append("agent_id = ?")
            args.append(agent_id)
        if ts_from is not None:
            where.append("ts_to >= ?")
            args.append(ts_from)
        if ts_to is not None:
            where.append("ts_from <= ?")
            args.append(ts_to)
        args.append(_cap(limit))
        async with self._db.execute(
            f"select {', '.join(SAMPLE_FIELDS)} from samples where {' and '.join(where)}"
            " order by ts_from, seq limit ?",
            args,
        ) as cur:
            rows = await cur.fetchall()
        return [SampleWindow.model_validate(dict(zip(SAMPLE_FIELDS, r, strict=True))) for r in rows]


def _event(r: Sequence[Any]) -> Event:
    return Event.model_validate(
        {
            "v": SCHEMA_VERSION,
            "run_id": r[0],
            "seq": r[1],
            "ts": r[2],
            "agent_id": r[3],
            "type": r[4],
            "data": json.loads(r[5]),
            "prev_hash": r[6],
            "hash": r[7],
        }
    )


def _window(event: Event) -> dict[str, Any] | None:
    try:
        sample = ResourceSample.model_validate(event.data)
    except ValidationError:
        return None
    ts = sample.values("ts") or []
    return {
        "run_id": event.run_id,
        "seq": event.seq,
        "agent_id": event.agent_id,
        "sandbox_id": sample.sandbox_id,
        "ts_from": ts[0],
        "ts_to": ts[-1],
        "count": len(ts),
        "hz": sample.hz,
        **sample.summary(),
    }


def _cap(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))


def _dump(value: Content) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _least(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


def _most(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else max(a, b)
