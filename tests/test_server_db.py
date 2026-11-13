import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from aims.server.db import VERSION, AuditRow, Job, Outcome, ServerDB
from aims.server.state import close_state, open_state

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[ServerDB]:
    db = await ServerDB.open(tmp_path / "server.sqlite")
    yield db
    await db.close()


def job(group: str | None = None, parallel: int | None = None) -> Job:
    at = time.time()
    return Job(
        run_id=str(uuid.uuid7()),
        group=group,
        parallel=parallel,
        state="queued",
        payload={"x": 1},
        needs={},
        target=None,
        runner=None,
        offered_at=None,
        tried=[],
        token="op",
        created=at,
        updated=at,
        reason=None,
    )


async def test_tokens_are_stored_hashed_and_revocable(db: ServerDB, tmp_path: Path) -> None:
    secret = await db.create_token("alice", "operator")
    assert secret.startswith("aims_")
    assert len(secret) == 5 + 43
    assert secret.encode() not in (tmp_path / "server.sqlite").read_bytes()
    token = await db.token_for(secret)
    assert token is not None
    assert (token.name, token.role) == ("alice", "operator")
    with pytest.raises(ValueError, match="exists"):
        await db.create_token("alice", "viewer")
    session = await db.create_session("alice")
    assert (await db.session(session)) == token
    assert await db.revoke_token("alice")
    assert await db.token_for(secret) is None
    assert await db.session(session) is None
    assert not await db.revoke_token("alice")
    assert (tmp_path / "server.sqlite").stat().st_mode & 0o777 == 0o600


async def test_sessions_expire(db: ServerDB, monkeypatch: pytest.MonkeyPatch) -> None:
    await db.create_token("bob", "viewer")
    session = await db.create_session("bob")
    later = time.time() + 31 * 86400
    monkeypatch.setattr(time, "time", lambda: later)
    assert await db.session(session) is None
    assert await db.prune_sessions() == 1


async def test_opening_the_server_drops_expired_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    await db.create_token("bob", "viewer")
    long_ago = time.time() - 31 * 86400
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: long_ago)
        await db.create_session("bob")
    live = await db.create_session("bob")
    await db.close()
    state = await open_state(tmp_path)
    try:
        assert await state.db.session(live) is not None
    finally:
        await close_state(state)
    raw = sqlite3.connect(path)
    assert raw.execute("select count(*) from sessions").fetchone() == (1,)
    raw.close()


async def test_a_job_is_offered_claimed_once_and_requeued(db: ServerDB) -> None:
    j = job()
    await db.add_jobs([j])
    assert await db.offer(j.run_id, "r1")
    assert not await db.offer(j.run_id, "r2")
    assert not await db.claim(j.run_id, "r2")
    assert await db.requeue(j.run_id)
    assert await db.offer(j.run_id, "r2")
    assert await db.claim(j.run_id, "r2")
    assert not await db.claim(j.run_id, "r2")
    got = await db.job(j.run_id)
    assert got is not None
    assert (got.state, got.runner, got.tried) == ("claimed", "r2", ["r1", "r2"])
    assert await db.set_job(j.run_id, "running")
    assert await db.set_job(j.run_id, "done", "completed")
    assert not await db.set_job(j.run_id, "running")
    assert [x.run_id for x in await db.jobs()] == []
    meta = await db.run_meta(j.run_id)
    assert meta is not None
    assert (meta.source, meta.token) == ("job", "op")


async def test_group_caps_and_cancelling_queued_jobs(db: ServerDB) -> None:
    a, b = job("g", parallel=1), job("g", parallel=1)
    await db.add_jobs([a, b])
    assert await db.group_active("g") == 0
    await db.offer(a.run_id, "r1")
    assert await db.group_active("g") == 1
    assert await db.cancel_queued(b.run_id, "cancelled by alice")
    assert not await db.cancel_queued(b.run_id, "again")
    got = await db.job(b.run_id)
    assert got is not None
    assert got.state == "cancelled"


async def test_commands_have_a_seq_per_runner_and_ack_once(db: ServerDB) -> None:
    run = str(uuid.uuid7())
    c1 = await db.add_command("r1", run, {"action": "pause"})
    c2 = await db.add_command("r1", run, {"action": "resume"})
    other = await db.add_command("r2", run, {"action": "kill"})
    assert (c1.seq, c2.seq, other.seq) == (1, 2, 1)
    assert [c.id for c in await db.pending_commands("r1")] == [c1.id, c2.id]
    assert [c.id for c in await db.pending_commands("r1", after=1)] == [c2.id]
    assert not await db.ack(c1.id, "r2", "ok")
    assert await db.ack(c1.id, "r1", "failed: a note", done=False)
    assert not await db.ack(c1.id, "r1", "ok")
    refused = await db.command(c1.id)
    assert refused is not None
    assert (refused.state, refused.outcome) == ("failed", "failed: a note")
    assert await db.drop_commands(run) == 2
    assert await db.pending_commands("r1") == []
    late = await db.add_command("r1", run, {"action": "kill"})
    assert await db.drop_runner_commands("r1", "dropped: gone") == [run]
    settled = await db.command(late.id)
    assert settled is not None
    assert (settled.state, settled.outcome) == ("dropped", "dropped: gone")
    assert await db.drop_runner_commands("r1", "dropped: gone") == []


async def test_a_done_answer_after_the_run_ended_replaces_the_drop(db: ServerDB) -> None:
    run = str(uuid.uuid7())
    done, refused, answered, unanswered = [
        await db.add_command("r1", run, {"action": "pause"}) for _ in range(4)
    ]
    assert await db.ack(answered.id, "r1", "done")
    assert await db.drop_commands(run) == 3
    # the run's last events came before the runner's answers
    assert not await db.ack(done.id, "r2", "done")
    assert await db.ack(done.id, "r1", "the sandbox of agent a did not freeze: boom", in_part=True)
    assert await db.ack(refused.id, "r1", "failed: the run is not running here", done=False)
    assert not await db.ack(refused.id, "r1", "done")
    assert not await db.ack(answered.id, "r1", "again")
    assert not await db.ack(done.id, "r1", "again")
    got = {c.id: (c.state, c.outcome, c.in_part, c.run_ended) for c in await db.commands_of(run)}
    assert got == {
        done.id: ("done", "the sandbox of agent a did not freeze: boom", True, False),
        refused.id: ("dropped", "dropped: the run ended", False, False),
        answered.id: ("done", "done", False, False),
        unanswered.id: ("dropped", "dropped: the run ended", False, True),
    }
    assert await db.command_counts([run]) == {run: (4, 0, 2)}
    # another token took the runner name: no answer changes the old runner's commands
    assert await db.drop_runner_commands("r1", "dropped: gone") == []
    assert not await db.ack(unanswered.id, "r1", "done")
    left = await db.command(unanswered.id)
    assert left is not None
    assert (left.outcome, left.run_ended) == ("dropped: the run ended", False)
    gone = await db.add_command("r1", run, {"action": "kill"})
    assert await db.drop_runner_commands("r1", "dropped: gone") == [run]
    assert not await db.ack(gone.id, "r1", "done")
    settled = await db.command(gone.id)
    assert settled is not None
    assert settled.outcome == "dropped: gone"


async def test_runs_keep_their_first_runner_and_divergence(db: ServerDB) -> None:
    run = str(uuid.uuid7())
    meta = await db.attribute(run, "r1", "push", "alice", shipper="t1")
    assert (meta.runner, meta.source, meta.shipper) == ("r1", "push", "t1")
    again = await db.attribute(run, "r2", "push", shipper="t2")
    assert (again.runner, again.shipper) == ("r1", "t1")
    queued = job()
    await db.add_jobs([queued])
    fresh = await db.run_meta(queued.run_id)
    assert fresh is not None
    assert (fresh.runner, fresh.shipper) == (None, None)
    claimed = await db.attribute(queued.run_id, "r2", "job", shipper="t2")
    assert (claimed.runner, claimed.token, claimed.shipper) == ("r2", "op", "t2")
    await db.mark_diverged(run, "event 3 differs")
    await db.mark_diverged(run, "later")
    got = await db.run_meta(run)
    assert got is not None
    assert got.diverged == "event 3 differs"


def downgrade(path: Path, version: int) -> None:
    """Make the database at `path` look like one of schema `version`."""
    raw = sqlite3.connect(path)
    raw.execute("alter table commands drop column in_part")
    if version < 6:
        raw.execute("alter table commands drop column state")
    if version < 5:
        raw.execute("alter table commands drop column run_ended")
    if version < 4:
        raw.execute("drop table audit")
        raw.execute("alter table tokens drop column last_used")
        raw.execute("drop table deleted_runs")
        raw.execute("drop index jobs_by_origin")
        raw.execute("drop index jobs_by_score_monitor")
    if version < 3:
        raw.execute("alter table runs drop column shipper")
    raw.execute(f"pragma user_version = {version}")
    raw.commit()
    raw.close()


async def test_a_v2_db_learns_which_token_shipped_each_run(tmp_path: Path) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    pushed, imported = str(uuid.uuid7()), str(uuid.uuid7())
    await db.hello("box", "t1", {})
    await db.attribute(pushed, "box", "push")
    await db.attribute(imported, "import:op", "import", "op")
    await db.close()
    downgrade(path, 2)
    db = await ServerDB.open(path)
    got = await db.runs_meta([pushed, imported])
    assert (got[pushed].shipper, got[imported].shipper) == ("t1", None)
    await db.add_audit(AuditRow(at=time.time(), action="token.create", outcome="ok"))
    assert [t.last_used for t in await db.tokens()] == []
    await db.close()
    raw = sqlite3.connect(path)
    assert raw.execute("pragma user_version").fetchone() == (VERSION,)
    raw.close()


async def test_a_v3_db_gets_the_audit_log_and_token_use(tmp_path: Path) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    secret = await db.create_token("alice", "operator")
    await db.close()
    downgrade(path, 3)
    db = await ServerDB.open(path)
    try:
        assert [(t.name, t.last_used) for t in await db.tokens()] == [("alice", None)]
        assert await db.token_for(secret) is not None
        assert [t.last_used is not None for t in await db.tokens()] == [True]
        await db.add_audit(AuditRow(at=time.time(), action="token.create", outcome="ok"))
        assert [r.action for r in await db.audit_rows(limit=10)] == ["token.create"]
    finally:
        await db.close()
    raw = sqlite3.connect(path)
    assert raw.execute("pragma user_version").fetchone() == (VERSION,)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("update audit set action = 'x'")
    raw.close()


async def test_a_token_notes_its_last_use_at_most_once_a_minute(
    db: ServerDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = await db.create_token("alice", "operator")
    await db.create_token("bob", "viewer")

    async def used() -> dict[str, float | None]:
        return {t.name: t.last_used for t in await db.tokens()}

    assert await used() == {"alice": None, "bob": None}
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    assert await db.token_for(secret) is not None
    assert await used() == {"alice": now, "bob": None}
    first = now
    now += 59
    await db.token_for(secret)
    assert (await used())["alice"] == first
    now += 2
    await db.token_for(secret)
    assert (await used())["alice"] == now
    session = await db.create_session("alice")
    seen = now
    now += 30
    assert await db.session(session) is not None
    assert (await used())["alice"] == seen
    now += 31
    await db.session(session)
    assert (await used())["alice"] == now
    seen = now
    now += 600
    assert await db.token_for(secret, used=False) is not None
    assert (await used())["alice"] == seen


async def test_audit_rows_page_filter_and_prune(db: ServerDB) -> None:
    base = 1_800_000_000.0
    written: list[tuple[str, str | None, Outcome]] = [
        ("session.start", None, "refused"),
        ("token.create", "op", "ok"),
        ("token.revoke", "op", "refused"),
        ("run.create", "viewer", "refused"),
        ("run.cancel", "op", "ok"),
    ]
    for i, (action, token, outcome) in enumerate(written):
        await db.add_audit(
            AuditRow(at=base + i, action=action, token=token, outcome=outcome, target=str(i))
        )
    every = await db.audit_rows(limit=10)
    assert [r.target for r in every] == ["4", "3", "2", "1", "0"]
    assert [r.at for r in every] == [base + 4, base + 3, base + 2, base + 1, base]
    ids = [r.id for r in every]
    assert [r.target for r in await db.audit_rows(limit=2, before=ids[1])] == ["2", "1"]
    assert [r.target for r in await db.audit_rows(limit=10, action="token")] == ["2", "1"]
    assert [r.target for r in await db.audit_rows(limit=10, action="run.cancel")] == ["4"]
    assert [r.target for r in await db.audit_rows(limit=10, token="op")] == ["4", "2", "1"]
    assert [r.target for r in await db.audit_rows(limit=10, outcome="refused")] == ["3", "2", "0"]
    got = await db.audit_rows(limit=10, since=base + 1, until=base + 3)
    assert [r.target for r in got] == ["3", "2", "1"]
    assert await db.prune_audit(base + 2) == 2
    assert [r.target for r in await db.audit_rows(limit=10)] == ["4", "3", "2"]


@pytest.mark.parametrize("version", [2, 3])
async def test_an_older_db_learns_to_keep_deleted_runs(tmp_path: Path, version: int) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    await db.close()
    downgrade(path, version)
    db = await ServerDB.open(path)
    run = str(uuid.uuid7())
    await db.forget([run])
    assert await db.deleted([run, str(uuid.uuid7())]) == {run}
    await db.close()
    raw = sqlite3.connect(path)
    assert raw.execute("pragma user_version").fetchone() == (VERSION,)
    plan = raw.execute(
        "explain query plan select run_id from jobs where json_extract(payload, '$.origin') = ?"
        " or (json_extract(payload, '$.kind') = 'score'"
        " and json_extract(payload, '$.monitor') = ?)",
        ("a", "b"),
    ).fetchall()
    used = " ".join(r[-1] for r in plan)
    assert "jobs_by_origin" in used
    assert "jobs_by_score_monitor" in used
    raw.close()


@pytest.mark.parametrize("version", [2, 3, 4, 5])
async def test_an_older_db_keeps_its_drops_and_takes_late_answers_after(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    run = str(uuid.uuid7())
    old = await db.add_command("r1", run, {"action": "kill"})
    await db.close()
    downgrade(path, version)
    raw = sqlite3.connect(path)
    raw.execute("update commands set acked = 1, outcome = 'dropped: the run ended'")
    raw.commit()
    raw.close()
    db = await ServerDB.open(path)
    try:
        assert not await db.ack(old.id, "r1", "done")
        new = await db.add_command("r1", run, {"action": "kill"})
        assert await db.drop_commands(run) == 1
        assert await db.ack(new.id, "r1", "done")
        got = {c.id: c.outcome for c in await db.commands_of(run)}
        assert got == {old.id: "dropped: the run ended", new.id: "done"}
    finally:
        await db.close()
    raw = sqlite3.connect(path)
    assert raw.execute("pragma user_version").fetchone() == (VERSION,) == (7,)
    raw.close()


@pytest.mark.parametrize("version", [4, 5, 6])
async def test_an_older_db_keeps_the_state_of_each_command_its_outcome_said(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "server.sqlite"
    db = await ServerDB.open(path)
    run = str(uuid.uuid7())
    sent = [await db.add_command("r1", run, {"action": "pause"}) for _ in range(5)]
    await db.close()
    downgrade(path, version)
    raw = sqlite3.connect(path)
    outcomes = ["done", "the sandbox of agent a did not freeze", "failed: no agent b"]
    outcomes.append("dropped: the run ended")
    for c, outcome in zip(sent, outcomes, strict=False):
        raw.execute("update commands set acked = 1, outcome = ? where id = ?", (outcome, c.id))
    if version == 6:
        # v6 kept each command's state; it read done in part from the outcome text
        states = ["done", "done", "failed", "dropped"]
        for c, state in zip(sent, states, strict=False):
            raw.execute("update commands set state = ? where id = ?", (state, c.id))
    raw.commit()
    raw.close()
    db = await ServerDB.open(path)
    try:
        got = [(c.state, c.outcome, c.in_part) for c in await db.commands_of(run)]
        assert got == [
            ("done", "done", False),
            ("done", "the sandbox of agent a did not freeze", True),
            ("failed", "failed: no agent b", False),
            ("dropped", "dropped: the run ended", False),
            ("waiting", None, False),
        ]
        assert await db.command_counts([run]) == {run: (5, 1, 1)}
    finally:
        await db.close()


async def test_forgetting_runs_drops_their_rows_and_keeps_them_deleted(db: ServerDB) -> None:
    gone, kept = job(), job()
    await db.add_jobs([gone, kept])
    await db.add_command("r1", gone.run_id, {"action": "pause"})
    await db.add_command("r1", kept.run_id, {"action": "pause"})
    await db.forget([gone.run_id])
    assert await db.job(gone.run_id) is None
    assert await db.run_meta(gone.run_id) is None
    assert await db.commands_of(gone.run_id) == []
    assert await db.job(kept.run_id) is not None
    assert len(await db.commands_of(kept.run_id)) == 1
    assert await db.deleted([gone.run_id, kept.run_id]) == {gone.run_id}
    await db.undelete(gone.run_id)
    assert await db.deleted([gone.run_id]) == set()


async def test_runners_hello_and_status(db: ServerDB) -> None:
    await db.hello("r1", "tok", {"version": "0.1.0", "slots": 2})
    await db.seen("r1", {"free": 1})
    runner = await db.runner("r1")
    assert runner is not None
    assert runner.info == {"version": "0.1.0", "slots": 2, "free": 1}


class Gate:
    """A SQL function that holds the connection's thread until the test opens it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()

    def __call__(self) -> int:
        self.entered.set()
        self.opened.wait(10)
        return 1

    async def held(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 10)


@pytest.fixture
async def gate(db: ServerDB) -> AsyncIterator[Gate]:
    gate = Gate()
    await db._db.create_function("gate", 0, gate)
    yield gate
    gate.opened.set()


RUN = "019a6f00-0000-7000-8000-000000000001"

TRANSACTIONS: dict[str, Callable[[ServerDB], Coroutine[Any, Any, object]]] = {
    "revoke_token": lambda db: db.revoke_token("op"),
    "add_jobs": lambda db: db.add_jobs([job()]),
    "add_command": lambda db: db.add_command("r1", RUN, {"action": "pause"}),
    "drop_runner_commands": lambda db: db.drop_runner_commands("r1", "dropped: gone"),
    "forget": lambda db: db.forget([RUN]),
}


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("write", list(TRANSACTIONS))
async def test_a_writer_cancelled_while_its_begin_waits_leaves_no_transaction(
    db: ServerDB, gate: Gate, write: str
) -> None:
    await db.create_token("op", "operator")
    slow = asyncio.create_task(db._db.execute("select gate()"))
    await gate.held()
    task = asyncio.create_task(TRANSACTIONS[write](db))
    await settle()
    task.cancel()
    gate.opened.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await slow
    await db.add_command("r2", RUN, {"action": "kill"})
    assert not db._db.in_transaction
    assert [t.revoked for t in await db.tokens()] == [None]
    assert [c.runner for c in await db.commands_of(RUN)] == ["r2"]


@pytest.mark.parametrize("cancels", [1, 2])
async def test_a_writer_cancelled_mid_statement_rolls_back(
    db: ServerDB, gate: Gate, cancels: int
) -> None:
    await db._db.execute(
        "create temp trigger hold after insert on main.commands begin select gate(); end"
    )
    task = asyncio.create_task(db.add_command("r1", RUN, {"action": "pause"}))
    await gate.held()
    for _ in range(cancels):
        task.cancel()
        await settle()
    if cancels == 1:
        gate.opened.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    later = asyncio.create_task(db.add_jobs([job()]))
    await settle()
    gate.opened.set()
    await later
    assert not db._db.in_transaction
    assert await db.commands_of(RUN) == []


async def test_a_failed_begin_raises_its_own_error(db: ServerDB, tmp_path: Path) -> None:
    other = sqlite3.connect(tmp_path / "server.sqlite", isolation_level=None)
    try:
        other.execute("begin immediate")
        await db._db.execute("pragma busy_timeout = 0")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await db.add_jobs([job()])
        assert not db._db.in_transaction
    finally:
        other.close()
    await db.add_jobs([job()])


async def test_a_transaction_no_writer_owns_is_rolled_back(
    db: ServerDB, caplog: pytest.LogCaptureFixture
) -> None:
    await db._db.execute("begin immediate")
    await db._db.execute(
        "insert into commands (id, runner, seq, run_id, payload, created)"
        " values ('x', 'r9', 1, ?, '{}', 0)",
        (RUN,),
    )
    with caplog.at_level(logging.WARNING, logger="aims.server"):
        await db.add_command("r1", RUN, {"action": "pause"})
    assert not db._db.in_transaction
    assert [c.runner for c in await db.commands_of(RUN)] == ["r1"]
    assert any("no writer" in m for m in caplog.messages)
