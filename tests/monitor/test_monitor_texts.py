"""Every text a monitor model reads is a text of `[prompts.monitor]`: the descriptions of its
tools and schemas, the judge's tool errors and cut markers, and the headers that mark the parts
of its prompts. The defaults are what the recorded monitor runs were given; the replay tests
check that byte for byte. The texts added since identities were first stamped leave a monitor's
identity as it was while they hold their defaults."""

import ast
import json
import re
import string
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from scripted import Script, models, profile
from test_judge import make_run, tool
from test_render import AGENT, FULL, Run

from aims.events import LocalEventStore
from aims.models import ModelClient, ToolRequest, ToolSpec
from aims.monitor import glossary_texts
from aims.monitor.channels import evaluate_schema
from aims.monitor.config import JudgeConfig, MonitorConfig
from aims.monitor.judge import Judge, Rollout, clip
from aims.monitor.render import glossary
from aims.monitor.setup import identity
from aims.monitor.sweep import schemas, system
from aims.monitor.texts import MonitorTexts, added, defaults, resolve
from aims.prompts import Prompts, TextError

pytestmark = pytest.mark.anyio

SRC = Path(__file__).parents[2] / "src" / "aims"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def texts(**monitor: str) -> MonitorTexts:
    return resolve(Prompts(monitor=dict(monitor)))


def test_each_added_text_fills_in_from_its_placeholders() -> None:
    known = defaults()
    for name in added():
        text, allowed = known[name]
        names = {n for _, n, _, _ in string.Formatter().parse(text) if n is not None}
        assert names == allowed, name


# Identity


def test_the_added_texts_at_their_defaults_leave_the_identity_as_it_was() -> None:
    now = texts()
    before = MonitorTexts(
        {k: v for k, v in now.texts.items() if k not in added()}, dict(now.categories)
    )
    stamped = identity(MonitorConfig(), before, {}, {})
    assert identity(MonitorConfig(), now, {}, {}) == stamped
    for name in sorted(added()):
        assert identity(MonitorConfig(), texts(**{name: "Changed."}), {}, {}) != stamped, name


def test_texts_stored_before_the_added_ones_restore_with_their_defaults() -> None:
    now = texts(rubric="Rate it.")
    stored = now.snapshot()
    old: dict[str, JsonValue] = {k: v for k, v in now.texts.items() if k not in added()}
    back = MonitorTexts.restore({**stored, "texts": old})
    assert back.texts == now.texts
    assert back["rubric"] == "Rate it."
    assert back["judge_too_long"] == "too long to show"


def test_a_text_left_out_reads_its_default() -> None:
    assert MonitorTexts()["header_quotes"] == "QUOTES"
    assert MonitorTexts({"header_quotes": "Q"})["header_quotes"] == "Q"


# The judge


def judge_with(store: LocalEventStore, run_id: str, given: MonitorTexts) -> Judge:
    p = profile()
    client = ModelClient(store, run_id, models(p), transport=Script().transport(), env={})
    return Judge(
        source=run_id,
        config=JudgeConfig(),
        profile=p,
        texts=given,
        client=client,
        store=store,
        budget_usd=5.0,
    )


def descriptions(value: Any) -> list[str]:
    """Every description in a JSON schema, depth first."""
    out: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("description"), str):
            out.append(value["description"])
        for item in value.values():
            out += descriptions(item)
    elif isinstance(value, list):
        for item in value:
            out += descriptions(item)
    return out


async def test_the_judge_s_tool_and_verdict_descriptions_come_from_its_texts(
    store: LocalEventStore,
) -> None:
    names = [n for n in added() if n.startswith(("judge_param_", "judge_verdict_"))]
    run = await make_run(store)
    judge = judge_with(store, run.id, texts(**{n: f"<{n}>" for n in names}))
    specs: list[ToolSpec] = judge._specs(False)
    found = {d for s in specs for d in descriptions(s.parameters)}
    found |= set(descriptions(judge._format.json_schema))
    assert found == {f"<{n}>" for n in names}
    default = judge_with(store, run.id, MonitorTexts())
    assert {d for s in default._specs(False) for d in descriptions(s.parameters)} == {
        defaults()[n][0] for n in names
    }


async def test_the_judge_s_tool_errors_come_from_its_texts(store: LocalEventStore) -> None:
    run = await make_run(store)
    judge = judge_with(
        store,
        run.id,
        texts(
            judge_error_unknown_tool="no {name} in {tools}",
            judge_error_arguments="bad: {problems}",
            judge_error_event_types="types {types}",
            judge_error_no_blob="no blob {sha256}",
            judge_error_no_exec="no exec {exec_id}",
        ),
    )
    assert (await tool(judge, "nope"))["error"].startswith("no nope in flags, events")
    assert (await tool(judge, "events", from_seq=-1))["error"].startswith("bad: from_seq")
    assert (await tool(judge, "events", types=["no.such"]))["error"] == "types no.such"
    blob = "0" * 64
    assert (await tool(judge, "blob", sha256=blob))["error"] == f"no blob {blob}"
    assert (await tool(judge, "proc_tree", exec_id="x"))["error"] == "no exec x"


async def test_the_judge_s_cut_markers_come_from_its_texts(store: LocalEventStore) -> None:
    run = await make_run(store)
    given = texts(
        judge_chars_cut="<{chars} gone>",
        judge_blob_missing="<no {sha256}>",
        judge_too_long="<long>",
    )
    reader = judge_with(store, run.id, given).reader
    assert reader.clip("a" * 30, 9) == "aaaaaa<21 gone>aaa"
    assert clip("a" * 30, 9) == "aaaaaa[21 chars cut]aaa"
    missing = {"sha256": "1" * 64, "size": 3}
    assert await reader.text(run.id, missing, 100) == f"<no {'1' * 64}>"
    assert reader.too_long == "<long>"


async def test_the_judge_s_blob_cut_note_comes_from_its_texts(store: LocalEventStore) -> None:
    run = await make_run(store)
    # control bytes print as escapes, so a page of them passes the output cap and is cut
    ref = await store.put_blob(run.id, bytes(range(32)) * 600)
    judge = judge_with(store, run.id, texts(judge_blob_cut="<cut>"))
    await judge.reader.load()
    await judge._prepare()
    args = json.dumps({"sha256": ref.sha256})
    out = await judge._tool(
        Rollout(1, False, 1.0), ToolRequest(id="t", name="blob", arguments=args)
    )
    assert json.loads(out)["cut"] == "<cut>"


# The sweeps


def test_the_sweep_schemas_and_system_prompt_come_from_their_texts() -> None:
    names = [n for n in added() if n.startswith("sweep_schema_")]
    given = texts(**{n: f"<{n}>" for n in names})
    found = {d for s in schemas(given).values() for d in descriptions(s.json_schema)}
    assert found == {f"<{n}>" for n in names}
    channel = descriptions(
        evaluate_schema(["a", "b"], texts(channel_schema_agents="who")).json_schema
    )
    assert "who" in channel
    prompt = system(texts(header_categories="KINDS", header_rubric="SCALE"), "Intro.", "c: d")
    assert prompt.startswith("Intro.\n\nKINDS\nc: d\n\nSCALE\n")


async def test_the_render_headers_come_from_their_texts(store: LocalEventStore) -> None:
    run = await Run.open(store)
    await run.preamble()
    await run.step(1)
    await run.step(2)
    given = texts(
        header_records="AGENT {agent}",
        header_records_end="DONE",
        header_preamble="START",
        header_step="S{n}@{time}",
    )
    text = (await run.trajectory.render(FULL, texts=given)).text
    lines = text.split("\n")
    assert lines[0] == f"AGENT {AGENT}"
    assert lines[1] == "START"
    assert any(re.fullmatch(r"S1@\d\d:\d\d:\d\d", line) for line in lines)
    assert "DONE" in lines
    assert "RUN RECORDS OF AGENT" not in text


# The glossary


def test_the_glossary_at_its_defaults_leaves_the_identity_as_it_was() -> None:
    now = texts()
    stamped = identity(MonitorConfig(), now, {}, {})
    assert "glossary" not in now.hashed()
    bare = MonitorTexts(dict(now.texts), dict(now.categories))
    assert identity(MonitorConfig(), bare, {}, {}) == stamped
    changed = resolve(Prompts(monitor={"glossary": {"proc.exec": "A program."}}))
    assert identity(MonitorConfig(), changed, {}, {}) != stamped
    assert changed.hashed()["glossary"] == {"proc.exec": "A program."}


def test_a_glossary_stored_before_it_was_kept_restores_with_its_defaults() -> None:
    stored = texts().snapshot()
    del stored["glossary"]
    back = MonitorTexts.restore(stored)
    assert back.glossary == glossary_texts.defaults()
    assert back.gloss("proc.exec").startswith("A program the kernel did start")


def test_the_glossary_is_read_from_the_texts() -> None:
    default = glossary({"proc.exec", "mem.exec", "tool.call"})
    assert glossary({"proc.exec", "mem.exec", "tool.call"}, texts()) == default
    given = resolve(
        Prompts(
            monitor={
                "header_glossary": "TYPES",
                "glossary_format": "One record a line.",
                "glossary_attempt": "Tried: {doc}",
                "glossary": {
                    "proc.exec": "A program.",
                    "mem.exec": "Memory made runnable.",
                    "tool.call": "",
                },
            }
        )
    )
    assert glossary({"proc.exec", "mem.exec", "tool.call"}, given).split("\n") == [
        "TYPES",
        "One record a line.",
        "mem.exec: Tried: Memory made runnable.",
        "proc.exec: A program.",
    ]


def test_a_glossary_entry_for_an_unknown_type_is_refused() -> None:
    with pytest.raises(TextError, match=r"unknown record type 'no\.such'"):
        resolve(Prompts(monitor={"glossary": {"no.such": "x"}}))


# The guard

GUARDED = [
    "monitor/judge.py",
    "monitor/sweep.py",
    "monitor/channels.py",
    "monitor/render.py",
    "run/scoring.py",
]
# A header: two or more words in capitals, such as "END OF RUN RECORDS".
HEADER = re.compile(r"\b[A-Z]{2,}(?: [A-Z]{2,})+\b")


def _literal(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _literal(node.left) or _literal(node.right)
    return False


def _words(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
    return []


@pytest.mark.parametrize("path", GUARDED)
def test_no_monitor_model_text_is_written_in_the_code(path: str) -> None:
    """Text a monitor model reads comes from the monitor's texts: a schema or parameter
    description, a tool error, a section header or a rendered line, never one written here."""
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    found: list[str] = []
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {
                k.value: v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if "type" in keys and _literal(keys.get("description")):
                found.append(f"line {node.lineno}: a description")
            if _literal(keys.get("error")):
                found.append(f"line {node.lineno}: an error")
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "line" and node.args and _literal(node.args[0]):
                found.append(f"line {node.lineno}: a rendered line")
            if name == "_obj" and len(node.args) > 1 and _literal(node.args[1]):
                found.append(f"line {node.lineno}: a description")
        if id(node) not in docstrings and any(HEADER.search(w) for w in _words(node)):
            found.append(f"line {getattr(node, 'lineno', '?')}: a header")
    assert not found, f"{path}: text written in the code at {', '.join(found)}"
