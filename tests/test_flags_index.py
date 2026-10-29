import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aims.events import (
    Draft,
    LocalEventStore,
    RunFinished,
    RunStarted,
    ToolCall,
    split_lines,
)
from aims.events.chain import Head, seal
from aims.events.index import VERSION
from aims.events.payloads import (
    FlagReview,
    MonitorCleared,
    MonitorFlag,
    MonitorVerdict,
    TaskScored,
)
from aims.events.schema import BlobRef, now
from aims.events.store import LOG

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="demo", oversight="autonomous", agents=["a1", "a2"])


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def call(n: int, agent: str = "a1") -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments="{}"), agent)


def flag(agent: str | None = "a1", **fields: Any) -> Draft:
    base: dict[str, Any] = {
        "layer": "signal",
        "source": "repeat",
        "category": "loop_or_confusion",
        "importance": 2,
        "evidence": [1],
        "justification": "same call three times",
    }
    return Draft(MonitorFlag(**{**base, **fields}), agent)


def story() -> list[Draft]:
    """Seqs 1 to 12 of a run: four logical flags, a re-flag, a clear, a taint, a verdict and a
    final re-flag after it."""
    return [
        call(1),
        call(2),
        flag(key="shell:{}"),
        flag(
            layer="sweep",
            source="actions",
            lane="full",
            category="scorer_tampering",
            importance=4,
            evidence=[2],
            suspicion=80,
            quote="sys.exit(0)",
            quote_verified=True,
            justification="exits before the asserts",
        ),
        flag(key="shell:{}", count=10, first_flag=3, importance=3, evidence=[2], last_seq=2),
        Draft(MonitorCleared(flags=[5], source="review", reason="a retry loop, not a hack")),
        flag(
            agent="a2",
            source="namespace_escape",
            category="privilege_escalation",
            importance=5,
            phase="verifier",
        ),
        Draft(TaskScored(reward=1.0, tainted=[7])),
        flag(layer="judge", source="judge", category="fabricated_completion", evidence=[2]),
        Draft(
            MonitorVerdict(
                flags=[
                    FlagReview(flag=4, status="confirmed", reason="it is there"),
                    FlagReview(flag=3, status="rejected", reason="benign", evidence=[1]),
                ],
                max_severity=4,
            )
        ),
        flag(key="shell:{}", count=12, first_flag=3, final=True),
        Draft(RunFinished(status="completed")),
    ]


async def told(store: LocalEventStore, *, batch: int = 100) -> str:
    run_id = await store.create_run(STARTED)
    drafts = story()
    for i in range(0, len(drafts), batch):
        await store.append(run_id, drafts[i : i + batch])
    return run_id


def dump(root: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    db = sqlite3.connect(root / "index.sqlite")
    try:
        runs = db.execute("select * from runs order by run_id").fetchall()
        flags = db.execute("select * from flags order by run_id, seq").fetchall()
    finally:
        db.close()
    return runs, flags


async def test_flags_fold_re_flags_and_take_status_from_later_events(
    store: LocalEventStore,
) -> None:
    run_id = await told(store)
    flags = {f.seq: f for f in await store.flags(run_id)}
    assert sorted(flags) == [3, 4, 7, 9]

    repeat = flags[3]
    assert (repeat.latest, repeat.reflags, repeat.count, repeat.final) == (11, 2, 12, True)
    assert (repeat.importance, repeat.evidence, repeat.last_seq) == (3, [1, 2], 2)
    assert (repeat.status, repeat.status_seq, repeat.status_source) == ("rejected", 10, "judge")
    assert repeat.status_reason == "benign"

    sweep = flags[4]
    assert (sweep.status, sweep.layer, sweep.lane, sweep.suspicion) == (
        "confirmed",
        "sweep",
        "full",
        80,
    )
    assert (sweep.quote, sweep.quote_verified) == ("sys.exit(0)", True)

    escape = flags[7]
    assert (escape.status, escape.tainted, escape.status_source) == ("tainted", True, "verifier")
    assert (escape.agent_id, escape.phase) == ("a2", "verifier")
    assert flags[9].status == "open"
    assert not flags[9].tainted

    info = await store.run(run_id)
    assert info is not None
    assert (info.kind, info.origin, info.flags) == ("scenario", None, 4)
    assert (info.max_importance, info.verdict_severity, info.severity) == (5, 4, 5)
    assert info.categories == {
        "fabricated_completion": 1,
        "loop_or_confusion": 1,
        "privilege_escalation": 1,
        "scorer_tampering": 1,
    }


async def test_a_clear_counts_until_the_judge_speaks(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    await store.append(run_id, story()[:6])
    (repeat, sweep) = await store.flags(run_id)
    assert (repeat.status, repeat.status_seq, repeat.status_reason) == (
        "cleared",
        6,
        "a retry loop, not a hack",
    )
    assert sweep.status == "open"
    info = await store.run(run_id)
    assert info is not None
    assert (info.flags, info.max_importance, info.verdict_severity, info.severity) == (
        2,
        4,
        None,
        4,
    )


async def test_flag_filters_and_paging(store: LocalEventStore) -> None:
    run_id = await told(store)

    async def seqs(**kwargs: Any) -> list[int]:
        return [f.seq for f in await store.flags(run_id, **kwargs)]

    assert await seqs(layer="signal") == [3, 7]
    assert await seqs(category="scorer_tampering") == [4]
    assert await seqs(min_importance=4) == [4, 7]
    assert await seqs(status=["open", "tainted"]) == [7, 9]
    assert await seqs(agent_id="a2") == [7]
    assert await seqs(tainted=True) == [7]
    assert await seqs(tainted=False) == [3, 4, 9]
    assert await seqs(limit=2) == [3, 4]
    assert await seqs(after=4, limit=2) == [7, 9]
    assert await seqs(after=9) == []


async def test_rebuild_ingest_and_batching_all_index_the_same(
    store: LocalEventStore, tmp_path: Path
) -> None:
    run_id = await told(store, batch=1)
    before = dump(store.root)
    assert len(before[1]) == 6
    await store.rebuild()
    assert dump(store.root) == before

    lines = split_lines((store.root / run_id / LOG).read_bytes())
    for name, size in (("whole", len(lines)), ("single", 1), ("pairs", 2)):
        async with await LocalEventStore.open(tmp_path / name, ingest_only=True) as copy:
            for i in range(0, len(lines), size):
                await copy.ingest(run_id, lines[i : i + size])
            assert dump(copy.root) == before, name


async def test_run_filters(store: LocalEventStore) -> None:
    flagged = await told(store)
    quiet = await store.create_run(STARTED)
    monitor = await store.create_run(
        RunStarted(
            scenario="demo",
            oversight="observe",
            kind="monitor",
            origin=flagged,
            origin_head="0" * 64,
        )
    )
    await store.append(monitor, [flag(None, category="drift", importance=3, evidence=[5])])

    async def ids(**kwargs: Any) -> list[str]:
        return [r.run_id for r in await store.runs(**kwargs)]

    assert await ids() == [monitor, quiet, flagged]
    assert await ids(kind="scenario") == [quiet, flagged]
    assert await ids(kind="monitor") == [monitor]
    assert await ids(origin=flagged) == [monitor]
    assert await ids(severity=3) == [monitor, flagged]
    assert await ids(severity=5) == [flagged]
    assert await ids(category="drift") == [monitor]
    assert await ids(category="loop_or_confusion") == [flagged]
    assert await ids(kind="scenario", severity=1, limit=1) == [flagged]
    info = await store.run(monitor)
    assert info is not None
    assert (info.kind, info.origin, info.categories) == ("monitor", flagged, {"drift": 1})


async def test_odd_monitor_events_index_without_flag_rows(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(
            run_id,
            [
                flag(key="k", count=10, first_flag=99, importance=3),
                Draft(MonitorCleared(flags=[42], source="review", reason="no such flag")),
            ],
        )
    path = root / run_id / LOG
    lines = split_lines(path.read_bytes())
    head = Head(seq=len(lines) - 1, hash=json.loads(lines[-1])["hash"], offset=path.stat().st_size)
    bad = {"layer": "signal", "source": "x", "category": "drift", "importance": 9, "evidence": []}
    _, line, _ = seal(head, run_id=run_id, ts=now(), agent_id="a1", type="monitor.flag", data=bad)
    with path.open("ab") as f:
        f.write(line)

    async with await LocalEventStore.open(root) as store:
        assert [e.seq for e in await store.events(run_id, types=["monitor.flag"])] == [1, 3]
        (orphan,) = await store.flags(run_id)
        assert (orphan.seq, orphan.latest, orphan.reflags, orphan.status) == (1, 1, 0, "open")
        info = await store.run(run_id)
        assert info is not None
        assert (info.flags, info.max_importance, info.severity) == (1, 3, 3)


async def test_old_logs_and_old_indexes_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
        await store.append(run_id, [call(1)])
    old = "00000000-0000-7000-8000-000000000001"
    (root / old).mkdir()
    started = {"scenario": "legacy", "oversight": "observe", "agents": [], "config": {}}
    _, line, _ = seal(Head(), run_id=old, ts=now(), agent_id=None, type="run.started", data=started)
    (root / old / LOG).write_bytes(line)
    db = sqlite3.connect(root / "index.sqlite")
    db.execute("drop table flags")
    db.execute("pragma user_version = 2")
    db.commit()
    db.close()

    async with await LocalEventStore.open(root) as store:
        runs = {r.run_id: r for r in await store.runs()}
        assert set(runs) == {run_id, old}
        assert (runs[old].kind, runs[old].flags, runs[old].severity) == ("scenario", 0, 0)
        assert runs[old].categories == {}
        assert await store.flags(old) == []
    db = sqlite3.connect(root / "index.sqlite")
    assert db.execute("pragma user_version").fetchone() == (VERSION,)
    db.close()


async def test_removing_a_run_forgets_its_flags(store: LocalEventStore) -> None:
    run_id = await told(store)
    await store.remove(run_id)
    assert dump(store.root) == ([], [])


async def test_a_blob_quote_round_trips(store: LocalEventStore) -> None:
    run_id = await store.create_run(STARTED)
    ref = await store.put_blob(run_id, b"q" * 10_000)
    await store.append(run_id, [flag(quote=ref, quote_verified=False)])
    (f,) = await store.flags(run_id)
    assert f.quote == BlobRef(sha256=ref.sha256, size=ref.size)
