"""State of the control plane that the run logs cannot rebuild: who may call it, which runners
it knows, the jobs it hands out and the commands it sends. Kept apart from index.sqlite, which a
rebuild wipes."""

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

VERSION = 2
SESSION_DAYS = 30
TOKEN_PREFIX = "aims_"

Role = Literal["viewer", "operator", "runner"]
JobState = Literal["queued", "offered", "claimed", "running", "done", "cancelled"]
ROLES: tuple[Role, ...] = ("viewer", "operator", "runner")
OPEN_STATES: tuple[JobState, ...] = ("queued", "offered", "claimed", "running")

SCHEMA = """
create table tokens (
    name text primary key,
    sha256 text not null unique,
    role text not null check (role in ('viewer', 'operator', 'runner')),
    created real not null,
    revoked real
) strict;

create table sessions (
    id_sha256 text primary key,
    token text not null references tokens (name) on delete cascade,
    created real not null,
    last_seen real not null,
    expires real not null
) strict;

create table runners (
    name text primary key,
    token text not null,
    info text not null,
    hello real not null,
    seen real not null
) strict;

create table jobs (
    run_id text primary key,
    grp text,
    parallel integer,
    state text not null,
    payload text not null,
    needs text not null,
    target text,
    runner text,
    offered_at real,
    tried text not null default '[]',
    releases integer not null default 0,
    token text,
    created real not null,
    updated real not null,
    reason text
) strict;

create index jobs_by_state on jobs (state, created);

create table commands (
    id text primary key,
    runner text not null,
    seq integer not null,
    run_id text not null,
    payload text not null,
    created real not null,
    acked real,
    outcome text,
    unique (runner, seq)
) strict;

create index commands_by_run on commands (run_id);

create table runs (
    run_id text primary key,
    runner text,
    source text not null,
    token text,
    diverged text,
    created real not null
) strict;
"""


def _private_dir(path: Path) -> None:
    """Make `path` if needed; a directory this creates is readable by its owner only."""
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class Token:
    name: str
    role: Role
    created: float
    revoked: float | None = None


@dataclass(frozen=True, slots=True)
class Runner:
    name: str
    token: str
    info: dict[str, Any]
    hello: float
    seen: float


@dataclass(frozen=True, slots=True)
class Job:
    run_id: str
    group: str | None
    parallel: int | None
    state: JobState
    payload: dict[str, Any]
    needs: dict[str, Any]
    target: str | None
    runner: str | None
    offered_at: float | None
    tried: list[str]
    token: str | None
    created: float
    updated: float
    reason: str | None
    releases: int = 0


@dataclass(frozen=True, slots=True)
class Command:
    id: str
    runner: str
    seq: int
    run_id: str
    payload: dict[str, Any]
    created: float
    acked: float | None
    outcome: str | None


@dataclass(frozen=True, slots=True)
class RunMeta:
    run_id: str
    runner: str | None
    source: str
    token: str | None
    diverged: str | None
    created: float


class ServerDB:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> ServerDB:
        await asyncio.to_thread(_private_dir, path.parent)
        db = await aiosqlite.connect(path, isolation_level=None)
        try:
            await db.executescript(
                "pragma journal_mode = wal; pragma synchronous = normal;"
                " pragma busy_timeout = 5000; pragma foreign_keys = on;"
            )
            async with db.execute("pragma user_version") as cur:
                row = await cur.fetchone()
            version = 0 if row is None else row[0]
            if version == 0:
                await db.executescript(
                    f"begin immediate; {SCHEMA} pragma user_version = {VERSION}; commit;"
                )
            elif version != VERSION:
                raise RuntimeError(f"{path} has schema version {version}, not {VERSION}")
            await asyncio.to_thread(path.chmod, 0o600)
        except BaseException:
            await db.close()
            raise
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def _all(self, sql: str, args: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        async with self._db.execute(sql, args) as cur:
            return [tuple(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, args: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        rows = await self._all(sql, args)
        return rows[0] if rows else None

    async def _write(self, sql: str, args: Sequence[Any] = ()) -> int:
        async with self._lock:
            cur = await self._db.execute(sql, args)
            return cur.rowcount

    # Tokens and sessions

    async def create_token(self, name: str, role: Role) -> str:
        """A new token; only its sha256 is kept, so this is the one time it is seen."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        token = new_token()
        try:
            await self._write(
                "insert into tokens values (?, ?, ?, ?, null)",
                (name, hash_secret(token), role, time.time()),
            )
        except aiosqlite.IntegrityError:
            raise ValueError(f"a token named {name!r} exists") from None
        return token

    async def rotate_token(self, name: str, role: Role) -> str:
        """A new value for token `name`, made if missing. The old value stops working; sessions
        made with the token stay."""
        token = new_token()
        await self._write(
            "insert into tokens values (?, ?, ?, ?, null) on conflict (name) do update set"
            " sha256 = excluded.sha256, role = excluded.role, revoked = null",
            (name, hash_secret(token), role, time.time()),
        )
        return token

    async def tokens(self) -> list[Token]:
        rows = await self._all("select name, role, created, revoked from tokens order by created")
        return [Token(*r) for r in rows]

    async def revoke_token(self, name: str) -> bool:
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                cur = await self._db.execute(
                    "update tokens set revoked = ? where name = ? and revoked is null",
                    (time.time(), name),
                )
                await self._db.execute("delete from sessions where token = ?", (name,))
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")
        return cur.rowcount > 0

    async def token_for(self, secret: str) -> Token | None:
        row = await self._one(
            "select name, role, created, revoked from tokens where sha256 = ? and revoked is null",
            (hash_secret(secret),),
        )
        return None if row is None else Token(*row)

    async def create_session(self, token: str) -> str:
        session = secrets.token_urlsafe(32)
        at = time.time()
        await self._write(
            "insert into sessions values (?, ?, ?, ?, ?)",
            (hash_secret(session), token, at, at, at + SESSION_DAYS * 86400),
        )
        return session

    async def session(self, session: str) -> Token | None:
        """The token behind a live session; seeing it extends the session."""
        key = hash_secret(session)
        at = time.time()
        row = await self._one(
            "select t.name, t.role, t.created, t.revoked, s.last_seen from sessions s"
            " join tokens t on t.name = s.token"
            " where s.id_sha256 = ? and s.expires > ? and t.revoked is null",
            (key, at),
        )
        if row is None:
            return None
        if at - row[4] > 60:
            await self._write(
                "update sessions set last_seen = ?, expires = ? where id_sha256 = ?",
                (at, at + SESSION_DAYS * 86400, key),
            )
        return Token(*row[:4])

    async def end_session(self, session: str) -> None:
        await self._write("delete from sessions where id_sha256 = ?", (hash_secret(session),))

    async def prune_sessions(self) -> int:
        return await self._write("delete from sessions where expires <= ?", (time.time(),))

    # Runners

    async def hello(self, name: str, token: str, info: dict[str, Any]) -> None:
        at = time.time()
        await self._write(
            "insert into runners values (?, ?, ?, ?, ?) on conflict (name) do update set"
            " token = excluded.token, info = excluded.info, hello = excluded.hello,"
            " seen = excluded.seen",
            (name, token, json.dumps(info), at, at),
        )

    async def seen(self, name: str, info: dict[str, Any] | None = None) -> None:
        if info is None:
            await self._write("update runners set seen = ? where name = ?", (time.time(), name))
        else:
            await self._write(
                "update runners set seen = ?, info = json_patch(info, ?) where name = ?",
                (time.time(), json.dumps(info), name),
            )

    async def runners(self) -> list[Runner]:
        rows = await self._all("select name, token, info, hello, seen from runners order by name")
        return [Runner(r[0], r[1], json.loads(r[2]), r[3], r[4]) for r in rows]

    async def runner(self, name: str) -> Runner | None:
        row = await self._one(
            "select name, token, info, hello, seen from runners where name = ?", (name,)
        )
        return None if row is None else Runner(row[0], row[1], json.loads(row[2]), row[3], row[4])

    # Jobs

    _JOB = (
        "select run_id, grp, parallel, state, payload, needs, target, runner, offered_at, tried,"
        " token, created, updated, reason, releases from jobs"
    )

    @staticmethod
    def _job(r: tuple[Any, ...]) -> Job:
        return Job(
            r[0],
            r[1],
            r[2],
            r[3],
            json.loads(r[4]),
            json.loads(r[5]),
            r[6],
            r[7],
            r[8],
            json.loads(r[9]),
            r[10],
            r[11],
            r[12],
            r[13],
            r[14],
        )

    async def add_jobs(self, jobs: Sequence[Job]) -> None:
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                await self._db.executemany(
                    "insert into jobs (run_id, grp, parallel, state, payload, needs, target,"
                    " runner, offered_at, tried, token, created, updated, reason)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            j.run_id,
                            j.group,
                            j.parallel,
                            j.state,
                            json.dumps(j.payload),
                            json.dumps(j.needs),
                            j.target,
                            j.runner,
                            j.offered_at,
                            json.dumps(j.tried),
                            j.token,
                            j.created,
                            j.updated,
                            j.reason,
                        )
                        for j in jobs
                    ],
                )
                await self._db.executemany(
                    "insert into runs values (?, null, 'job', ?, null, ?) on conflict do nothing",
                    [(j.run_id, j.token, j.created) for j in jobs],
                )
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

    async def job(self, run_id: str) -> Job | None:
        row = await self._one(f"{self._JOB} where run_id = ?", (run_id,))
        return None if row is None else self._job(row)

    async def jobs(self, states: Sequence[JobState] = OPEN_STATES) -> list[Job]:
        marks = ", ".join("?" * len(states))
        rows = await self._all(
            f"{self._JOB} where state in ({marks}) order by created, run_id", tuple(states)
        )
        return [self._job(r) for r in rows]

    async def jobs_page(
        self,
        *,
        before: str | None,
        limit: int,
        kind: str | None = None,
        origin: str | None = None,
    ) -> list[Job]:
        """Jobs of every state, newest run id first; with `kind` only jobs of that kind, with
        `origin` only monitor jobs over that run."""
        where: list[str] = []
        args: list[Any] = []
        if before:
            where.append("run_id < ?")
            args.append(before)
        if kind is not None:
            where.append("coalesce(json_extract(payload, '$.kind'), 'scenario') = ?")
            args.append(kind)
        if origin is not None:
            where.append("json_extract(payload, '$.origin') = ?")
            args.append(origin)
        clause = f"where {' and '.join(where)}" if where else ""
        rows = await self._all(f"{self._JOB} {clause} order by run_id desc limit ?", (*args, limit))
        return [self._job(r) for r in rows]

    async def jobs_of(self, run_ids: Sequence[str]) -> dict[str, Job]:
        if not run_ids:
            return {}
        marks = ", ".join("?" * len(run_ids))
        rows = await self._all(f"{self._JOB} where run_id in ({marks})", tuple(run_ids))
        return {r[0]: self._job(r) for r in rows}

    async def commands_of(self, run_id: str) -> list[Command]:
        rows = await self._all(f"{self._COMMAND} where run_id = ? order by created", (run_id,))
        return [self._command(r) for r in rows]

    async def group_active(self, group: str) -> int:
        row = await self._one(
            "select count(*) from jobs where grp = ?"
            " and state in ('offered', 'claimed', 'running')",
            (group,),
        )
        return 0 if row is None else row[0]

    async def offer(self, run_id: str, runner: str) -> bool:
        """Offer a queued job to `runner`. False if it was not queued."""
        at = time.time()
        async with self._lock:
            row = await self._one("select tried from jobs where run_id = ?", (run_id,))
            if row is None:
                return False
            tried = [*json.loads(row[0]), runner]
            cur = await self._db.execute(
                "update jobs set state = 'offered', runner = ?, offered_at = ?, tried = ?,"
                " updated = ? where run_id = ? and state = 'queued'",
                (runner, at, json.dumps(tried), at, run_id),
            )
            return cur.rowcount > 0

    async def claim(self, run_id: str, runner: str) -> bool:
        """Take an offered job; only the runner it was offered to can, and only once."""
        return (
            await self._write(
                "update jobs set state = 'claimed', updated = ? where run_id = ?"
                " and state = 'offered' and runner = ?",
                (time.time(), run_id, runner),
            )
            > 0
        )

    async def released(self, run_id: str) -> int:
        """Count a release of a job and give the count."""
        await self._write("update jobs set releases = releases + 1 where run_id = ?", (run_id,))
        row = await self._one("select releases from jobs where run_id = ?", (run_id,))
        return 0 if row is None else row[0]

    async def requeue(
        self,
        run_id: str,
        *,
        states: Sequence[JobState] = ("offered",),
        runner: str | None = None,
    ) -> bool:
        """Put a job back in the queue, when it is in `states` (and held by `runner`)."""
        marks = ", ".join("?" * len(states))
        held, args = ("", ()) if runner is None else (" and runner = ?", (runner,))
        return (
            await self._write(
                "update jobs set state = 'queued', runner = null, offered_at = null, updated = ?"
                f" where run_id = ? and state in ({marks}){held}",
                (time.time(), run_id, *states, *args),
            )
            > 0
        )

    async def set_job(self, run_id: str, state: JobState, reason: str | None = None) -> bool:
        """Move a job on; a job never leaves `done` or `cancelled`."""
        return (
            await self._write(
                "update jobs set state = ?, reason = coalesce(?, reason), updated = ?"
                " where run_id = ? and state not in ('done', 'cancelled')",
                (state, reason, time.time(), run_id),
            )
            > 0
        )

    async def cancel_queued(self, run_id: str, reason: str) -> bool:
        return (
            await self._write(
                "update jobs set state = 'cancelled', reason = ?, updated = ?"
                " where run_id = ? and state in ('queued', 'offered')",
                (reason, time.time(), run_id),
            )
            > 0
        )

    # Commands

    async def add_command(self, runner: str, run_id: str, payload: dict[str, Any]) -> Command:
        at = time.time()
        command_id = secrets.token_hex(12)
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                async with self._db.execute(
                    "select coalesce(max(seq), 0) + 1 from commands where runner = ?", (runner,)
                ) as cur:
                    row = await cur.fetchone()
                seq = 1 if row is None else row[0]
                await self._db.execute(
                    "insert into commands values (?, ?, ?, ?, ?, ?, null, null)",
                    (command_id, runner, seq, run_id, json.dumps(payload), at),
                )
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")
        return Command(command_id, runner, seq, run_id, payload, at, None, None)

    _COMMAND = "select id, runner, seq, run_id, payload, created, acked, outcome from commands"

    @staticmethod
    def _command(r: tuple[Any, ...]) -> Command:
        return Command(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7])

    async def pending_commands(self, runner: str, after: int = 0) -> list[Command]:
        rows = await self._all(
            f"{self._COMMAND} where runner = ? and acked is null and seq > ? order by seq",
            (runner, after),
        )
        return [self._command(r) for r in rows]

    async def command(self, command_id: str) -> Command | None:
        row = await self._one(f"{self._COMMAND} where id = ?", (command_id,))
        return None if row is None else self._command(row)

    async def ack(self, command_id: str, runner: str, outcome: str) -> bool:
        return (
            await self._write(
                "update commands set acked = ?, outcome = ? where id = ? and runner = ?"
                " and acked is null",
                (time.time(), outcome, command_id, runner),
            )
            > 0
        )

    async def drop_commands(self, run_id: str, outcome: str = "dropped: the run ended") -> int:
        """Settle the commands still waiting for a run that has ended."""
        return await self._write(
            "update commands set acked = ?, outcome = ? where run_id = ? and acked is null",
            (time.time(), outcome, run_id),
        )

    # Run attribution

    async def attribute(
        self, run_id: str, runner: str, source: str, token: str | None = None
    ) -> RunMeta:
        """Record which runner writes a run, the first time it ships. A run keeps its runner."""
        async with self._lock:
            await self._db.execute(
                "insert into runs values (?, ?, ?, ?, null, ?) on conflict (run_id) do update"
                " set runner = coalesce(runs.runner, excluded.runner)",
                (run_id, runner, source, token, time.time()),
            )
        meta = await self.run_meta(run_id)
        assert meta is not None
        return meta

    async def run_meta(self, run_id: str) -> RunMeta | None:
        row = await self._one(
            "select run_id, runner, source, token, diverged, created from runs where run_id = ?",
            (run_id,),
        )
        return None if row is None else RunMeta(*row)

    async def runs_meta(self, run_ids: Sequence[str]) -> dict[str, RunMeta]:
        if not run_ids:
            return {}
        marks = ", ".join("?" * len(run_ids))
        rows = await self._all(
            "select run_id, runner, source, token, diverged, created from runs"
            f" where run_id in ({marks})",
            tuple(run_ids),
        )
        return {r[0]: RunMeta(*r) for r in rows}

    async def mark_diverged(self, run_id: str, detail: str) -> None:
        await self._write(
            "update runs set diverged = coalesce(diverged, ?) where run_id = ?", (detail, run_id)
        )
