"""Every text a built-in tool, a board tool or the agent loop gives the agent is a text of the
prompts config. Each one is replaced here once, through a sandbox that answers as told, to show
the replacement reaches the agent with its placeholders filled in. The defaults are checked byte
for byte against recorded runs in test_fixture_texts.py."""

import ast
import string
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar, TypeIs, cast

import pytest
from scripted import Script, call, calls
from test_agent import FakeSandbox, make_agent, payloads

from aims.agent import run_tool
from aims.agent.gate import OpenGate
from aims.channels import Board, ChannelSetup
from aims.channels.board import PostTool, ReadTool
from aims.events import BlobRef, ExecEnd, ExecFinished, LocalEventStore, ToolResult
from aims.prompts import PLACEHOLDERS, AgentTexts, Prompts, TextError
from aims.run.runner import resolve_texts, text_problems
from aims.sandbox import AuditedSandbox, SandboxStoppedError
from aims.sandbox.audit import ExecResult
from aims.scenario import Bundle, SandboxConfig, parse
from aims.tools import TextTool, Tool, ToolContext, cap, load_tools, registry
from aims.tools.builtin import EditArgs

pytestmark = pytest.mark.anyio

SRC = Path(__file__).parents[2] / "src" / "aims"
CONFIG = SandboxConfig(command_timeout_s=5, max_command_timeout_s=10)

# stdout, stderr, exit code, end, truncated
Answer = tuple[bytes, bytes, int | None, ExecEnd, bool]


def done(out: bytes = b"", err: bytes = b"", code: int | None = 0) -> Answer:
    return out, err, code, "exited", False


class Stub:
    """Answers the execs in order from `answers`; an exception in it is raised instead."""

    def __init__(self, answers: list[Answer | Exception]) -> None:
        self.answers = list(answers)

    async def exec(self, command: str, **kw: Any) -> ExecResult:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        out, err, code, end, truncated = answer
        finished = ExecFinished(
            exec_id=str(uuid.uuid7()),
            call_id=kw.get("call_id"),
            end=end,
            exit_code=code,
            pid=1,
            duration_ms=1,
            stdout="",
            stderr="",
            stdout_bytes=len(out),
            stderr_bytes=len(err),
            truncated=truncated,
            error="boom" if end == "failed" else None,
        )
        return ExecResult(finished, out, err, out[-8192:], err[-8192:])


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def context(store: LocalEventStore, answers: list[Answer | Exception], **kw: Any) -> ToolContext:
    return ToolContext(
        sandbox=cast(AuditedSandbox, Stub(answers)),
        store=store,
        run_id="r",
        agent_id="a",
        call_id="c1",
        step=1,
        config=CONFIG,
        gate=OpenGate(),
        **kw,
    )


def known_tools() -> list[type[Any]]:
    return [*registry().values(), PostTool, ReadTool]


@pytest.mark.parametrize("cls", known_tools(), ids=lambda c: c.__name__)
def test_each_tool_text_fills_in_from_its_placeholders(cls: type[Any]) -> None:
    assert set(cls.placeholders) <= set(cls.texts)
    for key, text in cls.texts.items():
        names = {n for _, n, _, _ in string.Formatter().parse(text) if n is not None}
        allowed = cls.placeholders.get(key)
        if allowed is None:
            assert not names, f"{cls.__name__}.{key} is sent as written but names {names}"
        else:
            assert names <= allowed, f"{cls.__name__}.{key}"
            text.format(**dict.fromkeys(allowed, "x"))


def test_each_agent_text_fills_in_from_its_placeholders() -> None:
    for key, text in AgentTexts().model_dump().items():
        names = {n for _, n, _, _ in string.Formatter().parse(text) if n is not None}
        assert names <= PLACEHOLDERS.get(key, frozenset()), key


def test_a_text_is_said_with_exactly_its_placeholders() -> None:
    edit = cast(TextTool, load_tools(["edit"])[0])
    assert edit.say("created", path="/p") == "Created /p."
    assert edit.say("old_str_empty") == "old_str is empty."
    with pytest.raises(TypeError, match="text created takes"):
        edit.say("created")
    with pytest.raises(TypeError, match="text old_str_empty takes"):
        edit.say("old_str_empty", path="/p")


def test_a_replaced_tool_text_with_an_unknown_placeholder_is_refused() -> None:
    with pytest.raises(TextError, match=r"tools.edit.created: unknown placeholder size"):
        load_tools(["edit"], {"edit": {"created": "{size}"}})
    with pytest.raises(TextError, match=r"tools.bash.stdout: unknown placeholder code"):
        load_tools(["bash"], {"bash": {"stdout": "{code}"}})
    with pytest.raises(TextError, match="omitted: unknown placeholder count"):
        Prompts(omitted="{count}").agent_texts()


# The edit tool

FILE = done(b"F\na\nb\n")
EDIT_CASES: list[tuple[str, str, dict[str, Any], list[Answer | Exception], str, bool, bool]] = [
    # key, replacement, arguments, answers, output, error, failed
    ("needs_file_text", "no text", {"command": "create"}, [], "no text", True, False),
    ("needs_old_str", "no old", {"command": "str_replace"}, [], "no old", True, False),
    ("needs_insert", "no line", {"command": "insert", "new_str": "x"}, [], "no line", True, False),
    ("not_found", "{path}?", {"command": "view"}, [done(b"N\n")], "/work/f?", True, False),
    (
        "not_found",
        "{path}?",
        {"command": "str_replace", "old_str": "a"},
        [done(b"N\n")],
        "/work/f?",
        True,
        False,
    ),
    (
        "is_directory",
        "{path}/",
        {"command": "str_replace", "old_str": "a"},
        [done(b"D\n/work/f\n")],
        "/work/f/",
        True,
        False,
    ),
    (
        "unreadable",
        "R {path}: {error}",
        {"command": "view"},
        [done(b"", b"denied\n", 1)],
        "R /work/f: denied",
        True,
        False,
    ),
    (
        "unreadable",
        "R {path}: {error}",
        {"command": "str_replace", "old_str": "a"},
        [(b"", b"", None, "failed", False)],
        "R /work/f: boom",
        True,
        True,
    ),
    (
        "exit_code",
        "code={code}",
        {"command": "view"},
        [done(b"", b"", 2)],
        "Could not read /work/f: code=2",
        True,
        False,
    ),
    (
        "too_large",
        "{path} {bytes}",
        {"command": "str_replace", "old_str": "a"},
        [(b"F\nabc", b"", 0, "exited", True)],
        "/work/f 3",
        True,
        False,
    ),
    (
        "not_utf8",
        "{path} bin",
        {"command": "str_replace", "old_str": "a"},
        [done(b"F\n\xff")],
        "/work/f bin",
        True,
        False,
    ),
    (
        "listing",
        "L {path}\n{text}",
        {"command": "view"},
        [done(b"D\n/work/f\n/work/f/x\n")],
        "L /work/f\n/work/f\n/work/f/x\n",
        False,
        False,
    ),
    ("empty", "E {path}", {"command": "view"}, [done(b"F\n")], "E /work/f", False, False),
    (
        "view",
        "V {path} {start}-{end}\n{text}",
        {"command": "view"},
        [FILE],
        "V /work/f 1-2\n     1\ta\n     2\tb\n",
        False,
        False,
    ),
    (
        "view_cut",
        "C {path} {start}-{end}\n{text}",
        {"command": "view"},
        [(b"F\na\nb\n", b"", 0, "exited", True)],
        "C /work/f 1-2\n     1\ta\n     2\tb\n",
        False,
        False,
    ),
    (
        "outside_range",
        "O {view_range} {count}",
        {"command": "view", "view_range": [3, 4]},
        [FILE],
        "O [3, 4] 2",
        True,
        False,
    ),
    (
        "uncreatable",
        "U {path}: {error}",
        {"command": "create", "file_text": "x"},
        [done(b"", b"no space", 1)],
        "U /work/f: no space",
        True,
        False,
    ),
    (
        "uncreatable",
        "U {path}: {error}",
        {"command": "create", "file_text": "x"},
        [(b"", b"", None, "failed", False)],
        "U /work/f: boom",
        True,
        True,
    ),
    (
        "exists",
        "X {path}",
        {"command": "create", "file_text": "x"},
        [done(b"exists\n")],
        "X /work/f",
        True,
        False,
    ),
    (
        "created",
        "C {path}",
        {"command": "create", "file_text": "x"},
        [done(b"created\n")],
        "C /work/f",
        False,
        False,
    ),
    (
        "old_str_empty",
        "empty old",
        {"command": "str_replace", "old_str": ""},
        [FILE],
        "empty old",
        True,
        False,
    ),
    (
        "no_match",
        "NM {path}",
        {"command": "str_replace", "old_str": "zz"},
        [FILE],
        "NM /work/f",
        True,
        False,
    ),
    (
        "many_matches",
        "MM {count} {path} {lines}",
        {"command": "str_replace", "old_str": "a"},
        [done(b"F\na\na\n")],
        "MM 2 /work/f [1, 2]",
        True,
        False,
    ),
    (
        "edited",
        "ED {path} {start} {end}\n{text}",
        {"command": "str_replace", "old_str": "a", "new_str": "c"},
        [FILE, done()],
        "ED /work/f 1 2\n     1\tc\n     2\tb\n",
        False,
        False,
    ),
    (
        "unwritable",
        "W {path}: {error}",
        {"command": "insert", "insert_line": 0, "new_str": "x"},
        [FILE, done(b"", b"read-only", 1)],
        "W /work/f: read-only",
        True,
        False,
    ),
    (
        "unwritable",
        "W {path}: {error}",
        {"command": "insert", "insert_line": 0, "new_str": "x"},
        [FILE, (b"", b"", None, "failed", False)],
        "W /work/f: boom",
        True,
        True,
    ),
    (
        "past_end",
        "P {line} {count}",
        {"command": "insert", "insert_line": 5, "new_str": "x"},
        [FILE],
        "P 5 2",
        True,
        False,
    ),
    (
        "failed",
        "S {error}",
        {"command": "view"},
        [SandboxStoppedError("the sandbox is stopped")],
        "S the sandbox is stopped",
        True,
        True,
    ),
]


@pytest.mark.parametrize(
    ("key", "text", "args", "answers", "output", "error", "failed"),
    EDIT_CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(EDIT_CASES)],
)
async def test_each_edit_reply_comes_from_its_text(
    store: LocalEventStore,
    key: str,
    text: str,
    args: dict[str, Any],
    answers: list[Answer | Exception],
    output: str,
    error: bool,
    failed: bool,
) -> None:
    (edit,) = load_tools(["edit"], {"edit": {key: text}})
    out = await edit.run(context(store, answers), EditArgs(path="f", **args))
    assert (out.output, out.error, out.failed) == (output, error, failed)


def test_every_edit_reply_text_is_tried() -> None:
    (edit,) = load_tools(["edit"])
    tried = {c[0] for c in EDIT_CASES}
    replies = {k for k in edit.text if k != "description" and not k.startswith("param_")}
    assert replies == tried


# The bash tool


async def run_bash(
    store: LocalEventStore, texts: dict[str, str], answers: list[Answer | Exception], **kw: Any
) -> tuple[str, bool, bool]:
    (bash,) = load_tools(["bash"], {"bash": texts})
    out = await bash.run(context(store, answers, **kw), bash.args.model_validate({"command": "x"}))
    return out.output, out.error, out.failed


async def test_each_bash_label_comes_from_its_text(store: LocalEventStore) -> None:
    labels = {"stdout": "O[{text}]", "stderr": "E[{text}]", "exit_code": "={code}"}
    assert await run_bash(store, labels, [done(b"hi\n", b"oops", 3)]) == (
        "O[hi\n]E[oops\n]=3",
        False,
        False,
    )
    timed = (b"", b"", None, "timeout", False)
    texts = {"timeout": "T{timeout_s}", "no_exit_code": "killed"}
    assert await run_bash(store, texts, [timed]) == ("T5\nkilled", False, False)
    assert await run_bash(store, {"exit_code": "={code}"}, [done()]) == ("=0", False, False)


async def test_a_cut_bash_stream_comes_from_its_text(store: LocalEventStore) -> None:
    out, err = b"a" * 5000 + b"b" * 5000, b"c" * 5000 + b"d" * 5000
    texts = {"stdout_cut": "O{bytes}\n{text}", "stderr_cut": "E{bytes}\n{text}"}
    got, _, _ = await run_bash(store, texts, [done(out, err)], omitted="<{bytes} cut>")
    first = "a" * 4096 + "\n<1808 cut>\n" + "b" * 4096 + "\n"
    second = "c" * 4096 + "\n<1808 cut>\n" + "d" * 4096 + "\n"
    assert got == f"O10000\n{first}E10000\n{second}exit code: 0"


async def test_a_bash_command_that_cannot_run_says_so_from_its_text(
    store: LocalEventStore,
) -> None:
    failed = (b"", b"", None, "failed", False)
    texts = {"failed": "F {error}"}
    assert await run_bash(store, texts, [failed]) == ("F boom", True, True)
    stopped = SandboxStoppedError("the sandbox is stopped")
    assert await run_bash(store, texts, [stopped]) == ("F the sandbox is stopped", True, True)


async def test_a_large_edit_view_cuts_with_the_agent_s_marker(store: LocalEventStore) -> None:
    (edit,) = load_tools(["edit"])
    body = b"".join(b"x" * 99 + b"\n" for _ in range(300))
    ctx = context(store, [done(b"F\n" + body)], omitted="<{bytes} cut>")
    out = await edit.run(ctx, EditArgs(command="view", path="f"))
    assert "\n<" in out.output
    assert " cut>\n" in out.output
    assert "bytes omitted" not in out.output


# Parameter descriptions


def parameters(tool: Tool) -> dict[str, str]:
    properties = cast(dict[str, dict[str, str]], tool.spec(CONFIG).parameters["properties"])
    return {name: p["description"] for name, p in properties.items()}


def test_each_parameter_description_comes_from_its_text() -> None:
    tools = load_tools(
        ["bash", "edit", "submit", "report_to_operator"],
        {
            "bash": {"param_command": "cmd", "param_timeout_s": "{timeout_s}/{max_timeout_s}"},
            "edit": {
                f"param_{n}": n.upper()
                for n in ("command", "path", "file_text", "old_str", "new_str")
            }
            | {"param_insert_line": "LINE", "param_view_range": "RANGE"},
            "submit": {"param_answer": "ans"},
            "report_to_operator": {"param_message": "msg"},
        },
    )
    bash, edit, submit, report = (parameters(t) for t in tools)
    assert bash == {"command": "cmd", "timeout_s": "5/10"}
    assert edit == {
        "command": "COMMAND",
        "path": "PATH",
        "file_text": "FILE_TEXT",
        "old_str": "OLD_STR",
        "new_str": "NEW_STR",
        "insert_line": "LINE",
        "view_range": "RANGE",
    }
    assert (submit, report) == ({"answer": "ans"}, {"message": "msg"})


BOARD = """\
version: 1
name: t
oversight: observe
task:
  instruction: Talk.
  environment: {image: python:3.13-slim}
agents:
  - id: a
  - id: b
channels:
  - name: chat
    kind: board
    members: [a, b]
"""


async def test_the_board_post_parameter_comes_from_its_text(store: LocalEventStore) -> None:
    s = parse(BOARD)
    texts = {"a": {"post_message": {"param_text": "What {channel} gets."}}}
    board = Board(
        ChannelSetup(config=s.channels[0], scenario=s, run_id="r", store=store, texts=texts)
    )
    post = next(t for t in board.tools("a") if t.name == "post_message")
    assert parameters(post) == {"text": "What chat gets."}
    default = next(t for t in board.tools("b") if t.name == "post_message")
    assert parameters(default) == {"text": "The message."}


ALONE = BOARD.split("channels:")[0]


@pytest.mark.parametrize("given", [True, False], ids=["with scenario", "without"])
def test_board_texts_in_the_config_wait_for_an_agent_with_a_board(given: bool) -> None:
    """The settings page and `aims prompts show` offer the board tools' texts under their
    default names, so the config files may set them for scenarios without a board."""
    prompts = Prompts(
        tools={"post_message": {"posted": "Sent {id}."}, "read_messages": {"none": "None."}}
    )
    alone = parse(ALONE)
    agent = alone.agent("a")
    texts = resolve_texts(prompts, agent, alone if given else None)
    assert (set(texts.tools), texts.channels) == (set(agent.tool_names), {})
    assert text_problems(Bundle(alone, ALONE.encode()), prompts) == []
    board = parse(BOARD)
    channels = resolve_texts(prompts, board.agent("a"), board).channels
    assert channels["post_message"]["posted"] == "Sent {id}."
    assert channels["read_messages"]["none"] == "None."


def test_an_agent_s_own_texts_for_a_board_it_lacks_are_refused() -> None:
    text = ALONE.replace(
        "  - id: a\n",
        "  - id: a\n    prompts:\n      tools:\n        post_message: {posted: Sent.}\n",
    )
    own = parse(text)
    assert text_problems(Bundle(own, text.encode())) == [
        "agent a: unknown tool(s) post_message; known: bash, edit, report_to_operator, submit"
    ]


# The agent loop


async def test_each_loop_error_comes_from_its_text(store: LocalEventStore) -> None:
    texts = Prompts(
        unknown_tool="no {name} in {tools}",
        arguments_not_json="json: {error}",
        arguments_not_object="object please",
        arguments_invalid="bad: {problems}",
        tool_failed="crashed: {error}",
    ).agent_texts()

    class Raises(TextTool):
        name: ClassVar[str] = "raises"
        args: ClassVar[type[EditArgs]] = EditArgs

        async def run(self, ctx: ToolContext, args: Any) -> Any:
            raise RuntimeError("inside")

    tools = cast(dict[str, Tool], {"bash": load_tools(["bash"])[0], "raises": Raises()})
    ctx = context(store, [])

    async def out(name: str, arguments: str) -> tuple[str, bool]:
        got = await run_tool(tools, ctx, name, arguments, texts)
        return got.output, got.error

    assert await out("nope", "{}") == ("no 'nope' in bash, raises", True)
    assert (await out("bash", "{x"))[0].startswith("json: Expecting property name")
    assert await out("bash", "[1]") == ("object please", True)
    assert await out("bash", "{}") == ("bad: command: Field required", True)
    args = '{"command": "view", "path": "f"}'
    assert await out("raises", args) == ("crashed: RuntimeError: inside", True)


def test_the_cut_marker_comes_from_the_agent_s_text() -> None:
    marker = Prompts(omitted="[{bytes} gone]").agent_texts().omitted
    assert cap(b"a" * 10 + b"b" * 10, limit=8, marker=marker) == ("aaaa\n[12 gone]\nbbbb", True)


async def test_calls_after_a_submit_say_they_did_not_run(store: LocalEventStore) -> None:
    texts = Prompts(not_run="skipped", omitted="<{bytes}>").agent_texts()
    script = Script(calls(call("submit", answer="x"), call("bash", command="ls")))
    agent, run_id = await make_agent(store, script, texts=texts)
    assert (await agent.run()).reason == "submitted"
    results = await payloads(store, run_id, ToolResult)
    assert [(r.output, r.error) for r in results] == [("Submitted.", False), ("skipped", True)]


async def test_a_long_tool_output_is_cut_with_the_agent_s_marker(store: LocalEventStore) -> None:
    def big(command: str, stdin: bytes | None) -> tuple[bytes, int]:
        return b"F\n" + b"x" * 99 + b"\n" + b"y" * 40000, 0

    texts = Prompts(omitted="<{bytes} cut>").agent_texts()
    script = Script(call("edit", command="view", path="f"), call("submit", answer="x"))
    agent, run_id = await make_agent(store, script, texts=texts, sandbox=FakeSandbox(big))
    assert (await agent.run()).reason == "submitted"
    first, _ = await payloads(store, run_id, ToolResult)
    assert first.truncated
    assert isinstance(first.output, BlobRef)
    output = (await store.get_blob(run_id, first.output.sha256, first.output.size)).decode()
    assert " cut>\n" in output
    assert "bytes omitted" not in output


# The guard


def _literal(node: ast.AST | None) -> bool:
    """Whether `node` is text written in the code: a string, an f-string or a sum with one."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _literal(node.left) or _literal(node.right)
    return False


def _named(node: ast.AST | None) -> TypeIs[ast.Constant]:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _first(node: ast.Call, n: int, keyword: str) -> ast.AST | None:
    if len(node.args) > n:
        return node.args[n]
    return next((k.value for k in node.keywords if k.arg == keyword), None)


GUARDED = [
    "tools/builtin.py",
    "tools/base.py",
    "channels/board.py",
    "plants/builtin.py",
    "agent/loop.py",
    "run/script.py",
]


@pytest.mark.parametrize("path", GUARDED)
def test_no_agent_text_is_written_in_the_code(path: str) -> None:
    """Text an agent reads comes from the prompts config: a tool output, a parameter or tool
    description, or a message of the loop names a configured text, never one written here."""
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    found: list[str] = []
    checks: dict[str, Callable[[ast.Call], ast.AST | None]] = {
        "ToolOutcome": lambda c: _first(c, 0, "output"),
        "ToolOutput": lambda c: _first(c, 1, "output"),
        "ToolSpec": lambda c: _first(c, 1, "description"),
        "Message": lambda c: _first(c, 1, "content"),
        "_result": lambda c: _first(c, 1, "text"),
        "_input": lambda c: _first(c, 1, "text"),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            check = checks.get(name or "")
            if check is not None and _literal(check(node)):
                found.append(f"line {node.lineno}: {name}")
        if isinstance(node, ast.Dict):
            # a parameter's JSON schema, which has a type; the texts tables have none
            keys = {k.value: v for k, v in zip(node.keys, node.values, strict=True) if _named(k)}
            if "type" in keys and _literal(keys.get("description")):
                found.append(f"line {node.lineno}: a parameter description")
    assert not found, f"{path}: text written in the code at {', '.join(found)}"
