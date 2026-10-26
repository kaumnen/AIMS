import asyncio
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

from aims.events.chain import Head
from aims.events.schema import SCHEMA_VERSION, Event, Timestamp, format_ts

VERSION = 1
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
    log_offset integer not null
) strict;

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

create trigger events_fts_insert after insert on events begin
    insert into events_fts (rowid, text) values (new.id, new.text);
end;
"""

DROP = """
drop trigger if exists events_fts_insert;
drop table if exists events_fts;
drop table if exists events;
drop table if exists runs;
"""


class RunInfo(BaseModel):
    run_id: str
    scenario: str
    status: str
    started_at: Timestamp
    ended_at: Timestamp | None
    events: int
    head: str


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
        finished = next((e for e in events if e.type == "run.finished"), None)
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                first = events[0]
                if first.seq == 0:
                    await self._db.execute(
                        "insert into runs values (?, ?, 'running', ?, null, -1, '', 0)"
                        " on conflict do nothing",
                        (run_id, first.data["scenario"], rows[0][2]),
                    )
                await self._db.executemany(
                    "insert into events (run_id, seq, ts, agent_id, type, data, prev_hash, hash,"
                    " text) values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing",
                    rows,
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

    async def run(self, run_id: str) -> RunInfo | None:
        runs = await self._runs("where run_id = ?", (run_id,))
        return runs[0] if runs else None

    async def runs(self, *, limit: int = 50, before: str | None = None) -> list[RunInfo]:
        where, args = ("where run_id < ?", (before,)) if before else ("", ())
        return await self._runs(f"{where} order by run_id desc limit ?", (*args, _cap(limit)))

    async def _runs(self, clause: str, args: Sequence[Any]) -> list[RunInfo]:
        async with self._db.execute(
            "select run_id, scenario, status, started_at, ended_at, last_seq + 1, last_hash"
            f" from runs {clause}",
            args,
        ) as cur:
            rows = await cur.fetchall()
        keys = ("run_id", "scenario", "status", "started_at", "ended_at", "events", "head")
        return [RunInfo.model_validate(dict(zip(keys, row, strict=True))) for row in rows]

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
        return [
            Event.model_validate(
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
            for r in rows
        ]


def _cap(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))
