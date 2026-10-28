import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aims.server.db import Job, ServerDB

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
    assert await db.ack(c1.id, "r1", "ok")
    assert not await db.ack(c1.id, "r1", "ok")
    assert await db.drop_commands(run) == 2
    assert await db.pending_commands("r1") == []


async def test_runs_keep_their_first_runner_and_divergence(db: ServerDB) -> None:
    run = str(uuid.uuid7())
    meta = await db.attribute(run, "r1", "push", "alice")
    assert (meta.runner, meta.source) == ("r1", "push")
    assert (await db.attribute(run, "r2", "push")).runner == "r1"
    await db.mark_diverged(run, "event 3 differs")
    await db.mark_diverged(run, "later")
    got = await db.run_meta(run)
    assert got is not None
    assert got.diverged == "event 3 differs"


async def test_runners_hello_and_status(db: ServerDB) -> None:
    await db.hello("r1", "tok", {"version": "0.1.0", "slots": 2})
    await db.seen("r1", {"free": 1})
    runner = await db.runner("r1")
    assert runner is not None
    assert runner.info == {"version": "0.1.0", "slots": 2, "free": 1}
