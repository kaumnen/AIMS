import asyncio
import bisect
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from aims.events.chain import Head
from aims.events.payloads import (
    AgentFinished,
    AgentHeld,
    AgentStep,
    Category,
    ExecEnd,
    ExecFinished,
    ExecPhase,
    ExecStarted,
    FileAccess,
    FlagLayer,
    FsSnapshot,
    GateKind,
    ModelRequest,
    ModelResponse,
    ModelStatus,
    MonitorAction,
    MonitorCleared,
    MonitorFlag,
    MonitorVerdict,
    OperatorAction,
    Oversight,
    ResourceSample,
    RunFinished,
    RunKind,
    ScoreUnit,
    TaskScored,
    ToolCall,
    ToolResult,
)
from aims.events.schema import SCHEMA_VERSION, BlobRef, Content, Event, Timestamp, format_ts

VERSION = 9
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
    categories text not null default '{}',
    series text,
    series_index integer,
    variant text,
    run_group text,
    steps integer not null default 0,
    nano_usd integer,
    reward real,
    preset text,
    monitor_mode text,
    selftest_case text,
    held text not null default '{}'
) strict;

create index runs_by_origin on runs (origin, run_id) where origin is not null;
create index runs_by_series on runs (series, series_index) where series is not null;
create index runs_by_group on runs (run_group) where run_group is not null;
create index runs_by_scenario on runs (scenario, run_id);

create table score_units (
    run_id text not null references runs (run_id),
    origin text not null,
    monitor text,
    variant text,
    agent text not null,
    role text not null,
    positive integer,
    data text not null,
    primary key (run_id, agent)
) strict;

create index score_units_by_origin on score_units (origin, run_id);

create table labels (
    run_id text not null references runs (run_id),
    flag integer not null,
    label text not null,
    note text not null,
    by text not null,
    at text not null,
    primary key (run_id, flag, at, by)
) strict;

create table run_costs (
    run_id text primary key references runs (run_id),
    last_hash text not null,
    data text not null
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
    exec_id text,
    unique (run_id, seq)
) strict;

create index events_by_type on events (run_id, type, seq);
create index events_by_agent on events (run_id, agent_id, seq);
create index events_by_exec on events (run_id, exec_id, seq) where exec_id is not null;

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
drop table if exists labels;
drop table if exists score_units;
drop table if exists run_costs;
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
HOLD_TYPES = frozenset(
    {
        MonitorAction.type,
        OperatorAction.type,
        AgentHeld.type,
        AgentFinished.type,
        RunFinished.type,
    }
)


class HoldInfo(BaseModel):
    """An agent the monitor or an operator paused that has not been let go. `seq` and `since`
    are the action that paused it, `flags` the flags behind a monitor's pause and `mode` the
    monitor's oversight then; `until` is when the monitor kills the run if the pause is still
    open. `at` is the gate point the agent waits at once it holds there, and `call` the model
    call or tool call it names; only a hold `before_tool` can deny that call."""

    agent: str
    by: Literal["monitor", "operator"]
    reason: str | None = None
    seq: int
    since: Timestamp
    flags: list[int] = Field(default_factory=list)
    mode: Oversight | None = None
    until: Timestamp | None = None
    at: GateKind | None = None
    call: str | None = None


class RunInfo(BaseModel):
    """A run as the index keeps it. `flags` counts its logical flags and `categories` them per
    category, whatever their status. `max_importance` is the highest importance of a flag not
    cleared or rejected, `verdict_severity` the judge's `max_severity`, and `severity` the
    higher of the two, 0 when there is neither. `steps` counts its agents' model calls,
    `cost_usd` sums the priced live model responses of every caller (None when there is none),
    `reward` is the last verifier reward and `reviews` counts the monitor runs over it.
    `preset` is the preset of the monitor that watched it, or that a monitor run ran,
    `monitor_mode` a monitor run's mode, `case` the self-test case it plays and `held` its
    agents paused now."""

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
    series: str | None = None
    series_index: int | None = None
    variant: str | None = None
    group: str | None = None
    steps: int = 0
    cost_usd: float | None = None
    reward: float | None = None
    reviews: int = 0
    preset: str | None = None
    monitor_mode: str | None = None
    case: str | None = None
    held: list[HoldInfo] = Field(default_factory=list)


def monitor_of(config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The monitor preset and mode a `run.started` config or a monitor job names."""
    monitor = config.get("monitor")
    inner = monitor.get("config") if isinstance(monitor, dict) else None
    preset = inner.get("preset") if isinstance(inner, dict) else None
    mode = config.get("mode")
    return (
        preset if isinstance(preset, str) else None,
        mode if isinstance(mode, str) else None,
    )


LabelKind = Literal["real", "false_alarm", "unclear"]


class Label(BaseModel):
    """A person's word on a monitor flag: `real` misbehaviour, a `false_alarm`, or `unclear`.
    `flag` is the flag's seq in `run_id`, the run that holds it. The latest label counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    flag: int = Field(ge=0)
    label: LabelKind
    note: str = ""
    by: str
    at: Timestamp


class UnitRow(BaseModel):
    """A unit of a score run as the index keeps it, with the run it scores, the monitor run
    whose flags it used (None for the run's own) and the run's variant."""

    run_id: str
    origin: str
    monitor: str | None
    variant: str | None
    agent: str
    role: str
    positive: bool | None
    unit: ScoreUnit


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
    "series",
    "series_index",
    "variant",
    "group",
    "steps",
    "cost_usd",
    "reward",
    "reviews",
    "preset",
    "monitor_mode",
    "case",
    "categories",
    "held",
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
PREVIEW = 300
STEP_TYPES = (
    ModelRequest.type,
    ToolCall.type,
    ExecStarted.type,
    ExecFinished.type,
    ToolResult.type,
)


class ToolInfo(BaseModel):
    """A tool call of a step. `args` is the call's arguments as JSON (or the raw text when it is
    not JSON) with every string cut to `PREVIEW` characters, None when they are a blob;
    `args_cut` says whether anything was cut and `args_bytes` is their full size. `execs` are
    the sandbox commands the call ran, oldest first; `exit_code` and `end` are the last one's
    and `duration_ms` their sum. `done` says the agent got its result, which set `error` and
    `denied`."""

    seq: int
    call_id: str
    name: str
    args: JsonValue = None
    args_cut: bool = False
    args_bytes: int = 0
    execs: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    end: ExecEnd | None = None
    duration_ms: int | None = None
    done: bool = False
    error: bool = False
    denied: bool = False


class StepInfo(BaseModel):
    """Step `n` of an agent: its model call `call_id` and the tool calls it made. `seq` is the
    `agent.step` event, `seq_from` the first request of the model call and `seq_to` the step's
    last event so far: the step, an exec's end or a tool result. `ts_from` and `ts_to` are their
    times."""

    seq: int
    agent_id: str | None
    n: int
    call_id: str
    seq_from: int
    seq_to: int
    ts_from: Timestamp
    ts_to: Timestamp
    status: ModelStatus
    source: Literal["live", "replay"] = "live"
    cost_usd: float | None = None
    tools: list[ToolInfo] = Field(default_factory=list)


class ExecActivity(BaseModel):
    """What one exec left in the log: `events` in all, `types` per event type, `file_ops` per
    `file.access` op and `changes` the paths its snapshots list as changed."""

    exec_id: str
    events: int
    types: dict[str, int]
    file_ops: dict[str, int]
    changes: int


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
                tables = ("labels", "score_units", "run_costs", "flags", "samples", "events")
                for table in (*tables, "runs"):
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
                exec_id if isinstance(exec_id := e.data.get("exec_id"), str) else None,
            )
            for e in events
        ]
        windows = [w for e in events if e.type == ResourceSample.type if (w := _window(e))]
        finished = next((e for e in events if e.type == "run.finished"), None)
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                first = events[0]
                if first.seq == 0:
                    origin = first.data.get("origin")
                    series = first.data.get("series")
                    series = series if isinstance(series, dict) else {}
                    name, n = series.get("name"), series.get("index")
                    config = first.data.get("config")
                    config = config if isinstance(config, dict) else {}
                    group = config.get("group")
                    preset, mode = monitor_of(config)
                    case = config.get("case")
                    await self._db.execute(
                        "insert into runs (run_id, scenario, status, started_at, ended_at,"
                        " last_seq, last_hash, log_offset, kind, origin, series, series_index,"
                        " run_group, preset, monitor_mode, selftest_case) values"
                        " (?, ?, 'running', ?, null, -1, '', 0, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " on conflict do nothing",
                        (
                            run_id,
                            first.data["scenario"],
                            rows[0][2],
                            str(first.data.get("kind") or "scenario"),
                            origin if isinstance(origin, str) else None,
                            name if isinstance(name, str) else None,
                            n if isinstance(n, int) and not isinstance(n, bool) else None,
                            group if isinstance(group, str) else None,
                            preset,
                            mode,
                            case if isinstance(case, str) else None,
                        ),
                    )
                async with self._db.execute(
                    "select last_seq from runs where run_id = ?", (run_id,)
                ) as cur:
                    row = await cur.fetchone()
                fresh: list[Event] = []
                if row is not None:
                    fresh = [e for e in events if e.seq > row[0]]
                    await self._monitor(run_id, [e for e in fresh if e.type in MONITOR_TYPES])
                    await self._totals(run_id, fresh)
                units = [e for e in events if e.type == ScoreUnit.type]
                if units:
                    await self._units(run_id, events, units)
                await self._db.executemany(
                    "insert into events (run_id, seq, ts, agent_id, type, data, prev_hash, hash,"
                    " text, exec_id) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing",
                    rows,
                )
                if windows:
                    await self._db.executemany(
                        f"insert into samples ({', '.join(SAMPLE_FIELDS)}) values"
                        f" ({', '.join('?' * len(SAMPLE_FIELDS))}) on conflict do nothing",
                        [tuple(w[f] for f in SAMPLE_FIELDS) for w in windows],
                    )
                await self._hold(run_id, [e for e in fresh if e.type in HOLD_TYPES])
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

    async def _totals(self, run_id: str, fresh: Sequence[Event]) -> None:
        """Add the steps, cost and reward of events not indexed yet to the run's row. Cost adds
        up in whole nano-dollars, so any batching of the same events gives the same total."""
        steps, nanos, priced, scored, reward = 0, 0, False, False, None
        for e in fresh:
            if e.type == AgentStep.type:
                steps += 1
            elif e.type == ModelResponse.type and e.data.get("source", "live") == "live":
                value = e.data.get("cost_usd")
                if isinstance(value, int | float) and not isinstance(value, bool):
                    nanos += round(value * 1e9)
                    priced = True
            elif e.type == TaskScored.type:
                value = e.data.get("reward")
                scored = True
                is_number = isinstance(value, int | float) and not isinstance(value, bool)
                reward = value if is_number else None
        if steps or priced or scored:
            await self._db.execute(
                "update runs set steps = steps + ?,"
                " nano_usd = case when ? then coalesce(nano_usd, 0) + ? else nano_usd end,"
                " reward = case when ? then ? else reward end where run_id = ?",
                (steps, priced, nanos, scored, reward, run_id),
            )

    async def _hold(self, run_id: str, fresh: Sequence[Event]) -> None:
        """Carry the run's open pauses through `fresh`, in seq order. Runs inside `add`'s
        transaction after the batch's rows went in, so the run's start and its agents' ends
        are in the table."""
        if not fresh:
            return
        async with self._db.execute("select held from runs where run_id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        held: dict[str, dict[str, Any]] = json.loads(row[0])
        before = json.dumps(held, sort_keys=True)
        for e in fresh:
            match e.type:
                case MonitorAction.type:
                    await self._monitor_hold(e, held)
                case OperatorAction.type:
                    action = e.data.get("action")
                    if action in ("kill", "cancel") or (
                        action == "resume" and e.data.get("then") == "kill"
                    ):
                        held.clear()
                        continue
                    targets = await self._agents(e)
                    if e.agent_id:
                        targets = [e.agent_id] if e.agent_id in targets else []
                    for agent in targets:
                        if action == "pause":
                            held.setdefault(agent, {}).setdefault(
                                "operator",
                                {
                                    "reason": e.data.get("reason") or "paused by the operator",
                                    "seq": e.seq,
                                    "since": format_ts(e.ts),
                                },
                            )
                        elif action == "resume":
                            held.pop(agent, None)
                case AgentHeld.type:
                    hold = held.get(e.agent_id or "")
                    if hold is not None:
                        hold["at"] = e.data.get("kind")
                        hold["call"] = e.data.get("call_id")
                case AgentFinished.type:
                    held.pop(e.agent_id or "", None)
                case RunFinished.type:
                    held.clear()
        if json.dumps(held, sort_keys=True) != before:
            await self._db.execute(
                "update runs set held = ? where run_id = ?",
                (json.dumps(held, separators=(",", ":"), sort_keys=True), run_id),
            )

    async def _monitor_hold(self, e: Event, held: dict[str, dict[str, Any]]) -> None:
        """Apply a monitor decision; a simulated one changed nothing. A monitor's resume lets go
        of its own pause only; an operator's pause of the same agent stays."""
        try:
            action = MonitorAction.model_validate(e.data)
        except ValidationError:
            return
        if action.simulated:
            return
        if action.action == "kill":
            held.clear()
            return
        until = None
        if action.action == "pause":
            timeout = await self._timeout(e.run_id, action.mode)
            until = None if timeout is None else format_ts(e.ts + timedelta(seconds=timeout))
        for agent in action.agents:
            if action.action == "pause":
                held.setdefault(agent, {})["monitor"] = {
                    "reason": action.reason or None,
                    "seq": e.seq,
                    "since": format_ts(e.ts),
                    "flags": action.flags,
                    "mode": action.mode,
                    "until": until,
                }
                continue
            hold = held.get(agent)
            if hold is None:
                continue
            hold.pop("monitor", None)
            if "operator" not in hold:
                held.pop(agent)

    async def _start(self, run_id: str) -> dict[str, Any]:
        async with self._db.execute(
            "select data from events where run_id = ? and seq = 0", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return {} if row is None else json.loads(row[0])

    async def _agents(self, e: Event) -> list[str]:
        """The agents an operator's action can reach: the run's agents that had not finished
        before it."""
        agents = (await self._start(e.run_id)).get("agents")
        if not isinstance(agents, list):
            return []
        async with self._db.execute(
            "select agent_id from events where run_id = ? and type = ? and seq < ?",
            (e.run_id, AgentFinished.type, e.seq),
        ) as cur:
            done = {r[0] for r in await cur.fetchall()}
        return [a for a in agents if isinstance(a, str) and a not in done]

    async def _timeout(self, run_id: str, mode: Oversight) -> float | None:
        """How long the run's monitor holds a pause in `mode` before it kills the run."""
        config = (await self._start(run_id)).get("config")
        monitor = config.get("monitor") if isinstance(config, dict) else None
        inner = monitor.get("config") if isinstance(monitor, dict) else None
        policy = inner.get("policy") if isinstance(inner, dict) else None
        if not isinstance(policy, dict):
            return None
        value = policy.get("human_timeout_s" if mode == "human" else "pause_timeout_s")
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        return float(value) if is_number else None

    async def _monitor(self, run_id: str, fresh: Sequence[Event]) -> None:
        """Index the flags and flag statuses of monitor events not indexed yet, then the run's
        flag aggregates. Runs inside `add`'s transaction, before the head moves."""
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
        series: str | None = None,
        variant: str | None = None,
        group: str | None = None,
        scenario: str | None = None,
        skip_kinds: Sequence[str] = (),
    ) -> list[RunInfo]:
        """Runs newest first. `severity` keeps runs whose `severity` is at least it, `category`
        those with a flag in it, whatever its status, `origin` the monitor runs of that run,
        `series` the runs of that series, `variant` the sealed runs of that variant, `group`
        the samples of that group and `scenario` the runs of that scenario; runs of
        `skip_kinds` are left out."""
        where: list[str] = []
        args: list[Any] = []
        if scenario is not None:
            where.append("scenario = ?")
            args.append(scenario)
        if group is not None:
            where.append("run_group = ?")
            args.append(group)
        if series is not None:
            where.append("series = ?")
            args.append(series)
        if variant is not None:
            where.append("variant = ?")
            args.append(variant)
        if before:
            where.append("run_id < ?")
            args.append(before)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if skip_kinds:
            where.append(f"kind not in ({','.join('?' * len(skip_kinds))})")
            args.extend(skip_kinds)
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
            " origin, flags, max_importance, verdict_severity, severity, series, series_index,"
            " variant, run_group, steps, nano_usd / 1e9, reward, (select count(*) from runs m"
            " where m.origin = runs.run_id and m.kind = 'monitor'), preset, monitor_mode,"
            f" selftest_case, categories, held from runs {clause}",
            args,
        ) as cur:
            rows = await cur.fetchall()
        return [
            RunInfo.model_validate(
                {
                    **dict(zip(RUN_KEYS, row, strict=True)),
                    "categories": json.loads(row[-2]),
                    "held": _holds(json.loads(row[-1])),
                }
            )
            for row in rows
        ]

    async def _units(self, run_id: str, batch: Sequence[Event], units: Sequence[Event]) -> None:
        """Keep the units of a score run, with the origin, monitor run and variant its
        run.started names."""
        first = next((e for e in batch if e.seq == 0), None)
        if first is not None:
            data: dict[str, Any] = dict(first.data)
        else:
            async with self._db.execute(
                "select data from events where run_id = ? and seq = 0", (run_id,)
            ) as cur:
                row = await cur.fetchone()
            data = json.loads(row[0]) if row is not None else {}
        origin = data.get("origin")
        if not isinstance(origin, str):
            return
        config = data.get("config")
        config = config if isinstance(config, dict) else {}
        monitor, variant = config.get("monitor"), config.get("variant")
        await self._db.executemany(
            "insert into score_units (run_id, origin, monitor, variant, agent, role, positive,"
            " data) values (?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing",
            [
                (
                    run_id,
                    origin,
                    monitor if isinstance(monitor, str) else None,
                    variant if isinstance(variant, str) else None,
                    e.agent_id or "",
                    str(e.data.get("role") or "subject"),
                    None if e.data.get("positive") is None else int(bool(e.data["positive"])),
                    json.dumps(e.data, separators=(",", ":"), sort_keys=True),
                )
                for e in units
            ],
        )

    async def score_units(
        self, origins: Sequence[str] | None = None, *, latest: bool = True
    ) -> list[UnitRow]:
        """The units of score runs over `origins`, or over every run; with `latest`, only the
        newest score run of each origin and monitor run."""
        where, args = "", ()
        if origins is not None:
            if not origins:
                return []
            where = f"where origin in ({', '.join('?' * len(origins))})"
            args = tuple(origins)
        columns = "run_id, origin, monitor, variant, agent, role, positive, data"
        if latest:
            query = (
                f"select {columns} from score_units where run_id in (select run_id from"
                " (select run_id, row_number() over (partition by origin, coalesce(monitor, '')"
                " order by run_id desc) as n from (select distinct run_id, origin, monitor"
                f" from score_units {where})) where n = 1)"
            )
        else:
            query = f"select {columns} from score_units {where}"
        async with self._db.execute(f"{query} order by origin, run_id, agent", args) as cur:
            rows = await cur.fetchall()
        return [
            UnitRow(
                run_id=r[0],
                origin=r[1],
                monitor=r[2],
                variant=r[3],
                agent=r[4],
                role=r[5],
                positive=None if r[6] is None else bool(r[6]),
                unit=ScoreUnit.model_validate_json(r[7]),
            )
            for r in rows
        ]

    async def put_labels(self, labels: Sequence[Label]) -> None:
        if not labels:
            return
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                await self._db.executemany(
                    "insert into labels (run_id, flag, label, note, by, at)"
                    " values (?, ?, ?, ?, ?, ?) on conflict do nothing",
                    [(x.run_id, x.flag, x.label, x.note, x.by, format_ts(x.at)) for x in labels],
                )
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

    async def labels(self, run_id: str) -> dict[int, Label]:
        """The latest label of each labelled flag of `run_id`."""
        async with self._db.execute(
            "select run_id, flag, label, note, by, at from labels where run_id = ? order by at, by",
            (run_id,),
        ) as cur:
            rows = await cur.fetchall()
        out: dict[int, Label] = {}
        for r in rows:
            out[r[1]] = Label(run_id=r[0], flag=r[1], label=r[2], note=r[3], by=r[4], at=r[5])
        return out

    async def set_variant(self, run_id: str, variant: str | None) -> None:
        """Name the variant of the sealed run `run_id`, from its truth file, and drop its cost
        row, which was read without it."""
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                await self._db.execute(
                    "update runs set variant = ? where run_id = ?", (variant, run_id)
                )
                await self._db.execute("delete from run_costs where run_id = ?", (run_id,))
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

    async def costs(self) -> dict[str, tuple[str, str]]:
        """Each run's cost row: the head hash it was read at and its data."""
        async with self._db.execute("select run_id, last_hash, data from run_costs") as cur:
            rows = await cur.fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    async def put_costs(self, rows: Sequence[tuple[str, str, str]]) -> None:
        """Keep the cost rows `(run_id, last_hash, data)`."""
        if not rows:
            return
        async with self._lock:
            await self._db.execute("begin immediate")
            try:
                await self._db.executemany(
                    "insert into run_costs (run_id, last_hash, data) values (?, ?, ?)"
                    " on conflict (run_id) do update set last_hash = excluded.last_hash,"
                    " data = excluded.data",
                    rows,
                )
            except BaseException:
                await self._db.execute("rollback")
                raise
            await self._db.execute("commit")

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
        self,
        query: str,
        *,
        limit: int = 100,
        before: int | None = None,
        skip_kinds: Sequence[str] = (),
    ) -> tuple[list[Event], int | None]:
        """Events of every run whose text holds `query`, newest indexed first, and the cursor of
        the next page, leaving out runs of `skip_kinds`. Queries of three or more characters use
        the trigram index."""
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
        if skip_kinds:
            marks = ",".join("?" * len(skip_kinds))
            where.append(f"e.run_id not in (select run_id from runs where kind in ({marks}))")
            args.extend(skip_kinds)
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
        execs: Sequence[str] = (),
    ) -> list[Event]:
        """One page in seq order. With only `before` set, the page is the last events before it.
        `execs` keeps the events that name one of those execs."""
        joins = ""
        where = ["e.run_id = ?"]
        args: list[Any] = [run_id]
        if execs:
            where.append(f"e.exec_id in ({', '.join('?' * len(execs))})")
            args.extend(execs)
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

    async def steps(
        self,
        run_id: str,
        *,
        after: int | None = None,
        limit: int = 100,
        agent_id: str | None = None,
    ) -> list[StepInfo]:
        """A page of agent steps in seq order, after seq `after`, each with its tool calls."""
        where = ["run_id = ?", "type = ?"]
        args: list[Any] = [run_id, AgentStep.type]
        if after is not None:
            where.append("seq > ?")
            args.append(after)
        if agent_id is not None:
            where.append("agent_id = ?")
            args.append(agent_id)
        args.append(_cap(limit))
        async with self._db.execute(
            f"select seq, ts, agent_id, data from events where {' and '.join(where)}"
            " order by seq limit ?",
            args,
        ) as cur:
            page = list(await cur.fetchall())
        if not page:
            return []
        lo, hi = await self._span(run_id, page)
        where = ["run_id = ?", f"type in ({', '.join('?' * len(STEP_TYPES))})", "seq > ?"]
        args = [run_id, *STEP_TYPES, lo]
        if hi is not None:
            where.append("seq < ?")
            args.append(hi)
        if agent_id is not None:
            where.append("agent_id = ?")
            args.append(agent_id)
        async with self._db.execute(
            "select seq, ts, agent_id, type, json_extract(data, '$.call_id'),"
            " json_extract(data, '$.role'),"
            " json_extract(data, '$.model_call'), json_extract(data, '$.exec_id'),"
            " case when type = 'tool.call' then json_extract(data, '$.name') end,"
            " case when type = 'tool.call' then json_type(data, '$.arguments') end,"
            " case when type = 'tool.call' then json_extract(data, '$.arguments') end,"
            " case when type = 'exec.finished' then json_extract(data, '$.exit_code') end,"
            " case when type = 'exec.finished' then json_extract(data, '$.end') end,"
            " case when type = 'exec.finished' then json_extract(data, '$.duration_ms') end,"
            " case when type = 'tool.result' then json_extract(data, '$.error') end,"
            " case when type = 'tool.result' then json_extract(data, '$.denied') end"
            f" from events where {' and '.join(where)} order by seq",
            args,
        ) as cur:
            rows = list(await cur.fetchall())
        return _steps(page, rows)

    async def _span(self, run_id: str, page: Sequence[Sequence[Any]]) -> tuple[int, int | None]:
        """Seqs that bound every event of the steps in `page`: each agent's step before its first
        one in the page and after its last, None when an agent has no later step yet."""
        agents: dict[str | None, tuple[int, int]] = {}
        for seq, _, agent, _ in page:
            agents[agent] = (agents.get(agent, (seq, seq))[0], seq)
        lows: list[int] = []
        hi, open_ended = -1, False
        for agent, (first, last) in agents.items():
            for op, seq, order in (("<", first, "desc"), (">", last, "asc")):
                async with self._db.execute(
                    f"select seq from events where run_id = ? and type = ? and seq {op} ?"
                    f" and agent_id is ? order by seq {order} limit 1",
                    (run_id, AgentStep.type, seq, agent),
                ) as cur:
                    row = await cur.fetchone()
                if op == "<":
                    lows.append(-1 if row is None else row[0])
                elif row is None:
                    open_ended = True
                else:
                    hi = max(hi, row[0])
        return min(lows), None if open_ended else hi

    async def activity(self, run_id: str, execs: Sequence[str]) -> list[ExecActivity]:
        """What each of `execs` left in the log, in the order asked; execs with no events are
        left out."""
        if not execs:
            return []
        async with self._db.execute(
            "select exec_id, type,"
            " case when type = 'file.access' then json_extract(data, '$.op') end as op,"
            " count(*),"
            f" sum(case when type = '{FsSnapshot.type}' then json_extract(data, '$.total') end)"
            f" from events where run_id = ? and exec_id in ({', '.join('?' * len(execs))})"
            " group by exec_id, type, op",
            (run_id, *execs),
        ) as cur:
            rows = await cur.fetchall()
        found: dict[str, ExecActivity] = {}
        for exec_id, type_, op, n, changes in rows:
            a = found.setdefault(
                exec_id,
                ExecActivity(exec_id=exec_id, events=0, types={}, file_ops={}, changes=0),
            )
            a.events += n
            a.types[type_] = a.types.get(type_, 0) + n
            if type_ == FileAccess.type and isinstance(op, str):
                a.file_ops[op] = a.file_ops.get(op, 0) + n
            a.changes += changes or 0
        return [found[x] for x in dict.fromkeys(execs) if x in found]


def _steps(page: Sequence[Sequence[Any]], rows: Sequence[Sequence[Any]]) -> list[StepInfo]:
    """Assemble step rows from their `agent.step` events and the request, tool call, exec and
    result rows around them. Events of a tool call belong to the agent's latest `tool.call`
    before them with its id, since providers may reuse ids across steps."""
    wanted = {json.loads(data)["call_id"] for _, _, _, data in page}
    requests: dict[str, tuple[int, str]] = {}
    calls: dict[tuple[str | None, str], tuple[int, str, str | None, Any]] = {}
    call_seqs: dict[tuple[str | None, str], list[int]] = {}
    later: list[Sequence[Any]] = []
    for row in rows:
        seq, ts, agent, type_, call_id, role, model_call, _, name, args_type, args = row[:11]
        if type_ == ModelRequest.type:
            if role is None and call_id in wanted and call_id not in requests:
                requests[call_id] = (seq, ts)
        elif type_ == ToolCall.type:
            calls[(model_call, call_id)] = (seq, name, args_type, args)
            call_seqs.setdefault((agent, call_id), []).append(seq)
        else:
            later.append(row)
    tools: dict[int, ToolInfo] = {}
    ends: dict[int, tuple[int, str]] = {}
    for (_, call_id), (seq, name, args_type, args) in calls.items():
        tools[seq] = ToolInfo(seq=seq, call_id=call_id, name=name or "", **_args(args_type, args))
    for row in later:
        seq, ts, agent, type_, call_id, _, _, exec_id, _, _, _, code, end, ms, error, denied = row
        owners = call_seqs.get((agent, call_id))
        if not owners:
            continue
        i = bisect.bisect_right(owners, seq) - 1
        if i < 0:
            continue
        tool = tools[owners[i]]
        ends[tool.seq] = max(ends.get(tool.seq, (seq, ts)), (seq, ts))
        if type_ == ExecStarted.type and isinstance(exec_id, str):
            tool.execs.append(exec_id)
        elif type_ == ExecFinished.type:
            tool.exit_code = code
            tool.end = end
            tool.duration_ms = (tool.duration_ms or 0) + (ms or 0)
        elif type_ == ToolResult.type:
            tool.done = True
            tool.error = bool(error)
            tool.denied = bool(denied)
    out: list[StepInfo] = []
    for seq, ts, agent, data in page:
        d = json.loads(data)
        keys = [(d["call_id"], c) for c in d.get("tool_calls", [])]
        own = [calls[k][0] for k in keys if k in calls]
        first = requests.get(d["call_id"], (seq, ts))
        last = max([(seq, ts), *(ends[t] for t in own if t in ends)])
        out.append(
            StepInfo(
                seq=seq,
                agent_id=agent,
                n=d["n"],
                call_id=d["call_id"],
                seq_from=first[0],
                seq_to=last[0],
                ts_from=first[1],
                ts_to=last[1],
                status=d["status"],
                source=d.get("source", "live"),
                cost_usd=d.get("cost_usd"),
                tools=[tools[t] for t in own],
            )
        )
    return out


def _args(kind: str | None, value: Any) -> dict[str, Any]:
    """The preview fields of a tool call's arguments as `json_type` and `json_extract` give
    them: a string of JSON, or a blob reference."""
    if kind == "object":
        ref = BlobRef.model_validate_json(value)
        return {"args": None, "args_bytes": ref.size}
    text = value if isinstance(value, str) else ""
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = text
    cut = False

    def clip(v: Any) -> Any:
        nonlocal cut
        if isinstance(v, str) and len(v) > PREVIEW:
            cut = True
            return v[:PREVIEW]
        if isinstance(v, list):
            return [clip(x) for x in v]
        if isinstance(v, dict):
            return {k: clip(x) for k, x in v.items()}
        return v

    return {"args": clip(parsed), "args_cut": cut, "args_bytes": len(text.encode())}


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


def _holds(held: Mapping[str, Mapping[str, Any]]) -> list[HoldInfo]:
    out: list[HoldInfo] = []
    for agent, hold in held.items():
        for by in ("monitor", "operator"):
            if (fields := hold.get(by)) is not None:
                out.append(
                    HoldInfo.model_validate(
                        {"agent": agent, "by": by, "at": hold.get("at"), "call": hold.get("call")}
                        | fields
                    )
                )
    return sorted(out, key=lambda h: (h.seq, h.agent))


def _least(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


def _most(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else max(a, b)
