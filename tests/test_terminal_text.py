"""Text from agents, runs, scenario files, servers and requests reaches the operator's terminal
as the characters it holds: never read as Rich markup, never as escape bytes."""

import asyncio
import io
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cp import control_plane
from rich.console import Console
from typer.testing import CliRunner

from aims.cli import _command_line, _problem, app
from aims.events import (
    AgentReport,
    Draft,
    LocalEventStore,
    MonitorAction,
    MonitorDegraded,
    MonitorFlag,
    MonitorVerdict,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.models import Delta, DeltaBus
from aims.monitor.core import HumanRequest
from aims.run.runner import Result, Runner
from aims.run.view import HumanPrompt, LiveView, digest, summary
from aims.terminal import VisibleLog, one_line, shown

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "word-count"
# An OSC title write, a screen clear and a cursor move, as a tool might print them
ESCAPES = "\x1b]0;owned\x07\x1b[2J\x1b[H"


def invoke(*args: str, env: dict[str, str] | None = None) -> Any:
    # color=True keeps escape bytes in the output, as a terminal would get them
    return CliRunner().invoke(app, list(args), env=env, color=True)


def printed(*items: object, width: int = 300) -> str:
    """What a terminal gets for `items` printed on one line, with no colour codes of Rich's
    own."""
    out = io.StringIO()
    Console(file=out, force_terminal=True, color_system=None, width=width).print(*items)
    return out.getvalue()


async def flagged_run(store: LocalEventStore, text: str) -> str:
    """A run whose agent wrote `text`. Its monitor quoted it in each thing it wrote."""
    run_id = await store.create_run(RunStarted(scenario="s", oversight="human", agents=["a"]))
    flag = MonitorFlag(
        layer="signal",
        source="monitor_manipulation",
        category="monitor_manipulation",
        importance=4,
        evidence=[0],
        justification=f"the agent wrote {text}",
    )
    await store.append(
        run_id,
        [
            Draft(ToolCall(call_id="c1", name="bash", arguments=f"echo {text}"), "a"),
            Draft(ToolResult(call_id="c1", output=f"out {text}"), "a"),
            Draft(AgentReport(call_id="c2", step=1, message=f"report {text}"), "a"),
            Draft(flag, "a"),
            Draft(
                MonitorAction(action="pause", agents=["a"], flags=[4], mode="human", reason=text),
                "a",
            ),
            Draft(MonitorDegraded(part="judge", reason=f"judge said {text}", then="continue")),
            Draft(MonitorVerdict(max_severity=4, summary=f"summary {text}")),
            Draft(RunFinished(status="completed")),
        ],
    )
    return run_id


# The helpers

# Each reads as markup, or trips rich.markup.escape: it drops the backslash of `\[1]`, doubles
# a trailing one and leaves emoji codes alone.
TRICKY = [
    "[/x]",
    "[bold]b[/bold]",
    "[#12] a flag",
    "[link=https://example.invalid]y[/link]",
    "[@click=quit]",
    "\\[1] and \\[x]",
    "C:\\dir\\[x]",
    "ends in a backslash\\",
    "two at the end\\\\",
    ":smile: :thumbs_up:",
]


@pytest.mark.parametrize("text", TRICKY)
def test_shown_text_reads_as_written(text: str) -> None:
    assert printed(shown(text)) == f"{text}\n"
    assert printed("[red]error:[/]", shown(text)) == f"error: {text}\n"
    assert printed(shown(text, "bold red")) == f"{text}\n"


def test_shown_text_shows_hidden_characters_and_keeps_its_lines() -> None:
    text = "a\x1b[2J\x07\x00\x9b\u202e\u200b\ud800 b\r\nc\td"
    hidden = "\\u001B[2J\\u0007\\u0000\\u009B\\u202E\\u200B\\uD800"
    assert printed(shown(text)) == f"a{hidden} b\nc       d\n"
    assert printed(shown(3.5)) == "3.5\n"


def test_one_line_folds_whitespace_and_shows_hidden_characters() -> None:
    assert one_line(" a\n\tb  \x1b[1A c\r\n\rd \u2028 ") == "a b \\u001B[1A c d \\u2028"
    assert one_line(None) == "None"


# 72: markup in agent text in the end tables


async def test_the_end_tables_show_markup_in_agent_text_as_written(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await flagged_run(store, "[/x] and [bold]b")
        found = await digest(store, run_id)
    result = Result(run_id, "failed", "the verifier said [/x]")
    table = printed(summary([result], [found]))
    assert "the verifier said [/x]" in table
    assert "the agent wrote [/x] and [bold]b" in table
    assert "judge: judge said [/x] and [bold]b" in table
    assert "severity 4: summary [/x] and [bold]b" in table
    assert "pause a by monitor: [/x] and [bold]b" in table


@pytest.fixture
def runs(tmp_path: Path) -> tuple[Path, str]:
    (origin,) = FIXTURE.iterdir()
    root = tmp_path / "runs"
    root.mkdir()
    shutil.copytree(origin, root / origin.name)
    return root, origin.name


def test_aims_monitor_ships_its_runs_even_when_printing_fails(
    runs: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, origin = runs
    pushed: list[list[str] | None] = []

    async def push(root: Path, only: list[str] | None, url: str, token: str, **kw: Any) -> int:
        pushed.append(only)
        return 0

    def broken(digests: Any) -> None:
        raise RuntimeError("the table does not print")

    monkeypatch.setattr("aims.cli._push", push)
    monkeypatch.setattr("aims.run.view.monitor_table", broken)
    args = ["monitor", origin, "--runs-dir", str(root), "--signals-only"]
    result = invoke(*args, "--server", "http://127.0.0.1:9", env={"AIMS_TOKEN": "t"})
    assert len(pushed) == 1
    assert result.exit_code == 0, result.output
    assert "the results could not be printed" in result.stderr


# 73: escape bytes in the live view and the pause prompt


async def test_the_live_view_shows_escape_bytes_as_text(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await flagged_run(store, ESCAPES)
        view = LiveView(store, DeltaBus())
        view.add(run_id)
        async with asyncio.timeout(5):
            while view.runs[run_id].phase != "done":  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        await view.stop()
    for kind in ("start", "text", "reasoning"):
        view._on_delta(Delta(run_id, "a", "m1", 0, kind, f"model {ESCAPES}"))
    shown = printed(view)
    assert "\x1b" not in shown
    assert "\x07" not in shown
    assert "< out \\u001B]0;owned\\u0007\\u001B[2J\\u001B[H" in shown
    assert "> bash echo \\u001B]0;owned" in shown
    assert "report: report \\u001B]0;owned" in shown
    assert "model \\u001B]0;owned" in shown
    assert "monitor judge: judge said \\u001B]0;owned" in shown
    assert "the agent wrote \\u001B]0;owned" in shown


class Held:
    """The Runner surface the pause prompt uses: one paused agent, until an answer."""

    finished = killed = False

    def __init__(self) -> None:
        self.held = True

    def paused(self, agent_id: str) -> bool:
        return self.held

    async def operate(self, action: str, **kw: Any) -> None:
        self.held = False


async def test_the_pause_prompt_shows_escape_bytes_and_brackets_as_text(tmp_path: Path) -> None:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await flagged_run(store, ESCAPES)
        flags = await store.events(run_id, types=["monitor.flag"])
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, color_system=None, width=300)
    prompt = HumanPrompt(console, read=lambda: "kill\n")
    advice = f"look {ESCAPES}"
    request = HumanRequest(
        run_id=run_id, agents=["a"], flags=flags, advice=advice, held_call={"a": "c[/x]"}
    )
    await prompt.hook(cast(Runner, Held()))(request)
    async with asyncio.timeout(5):
        while prompt._tasks:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    shown = out.getvalue()
    assert "\x1b" not in shown
    assert "  [#4] monitor_manipulation 4 signal/monitor_manipulation a: the agent wrote" in shown
    assert "  advice: look \\u001B]0;owned\\u0007" in shown
    assert "  a waits before tool call c[/x]" in shown
    assert "resume, deny, inject, kill? [resume] " in shown


# 74: request bytes in aims audit


def test_aims_audit_shows_request_bytes_as_text(tmp_path: Path) -> None:
    # cursor up, erase that line, then a forged row
    forged = "%1B%5B1A%1B%5B2K%0D2026-11-10%2000:00:00%20%20op%20%20run.pause%20%20x%20%20ok"

    async def fill() -> None:
        async with control_plane(tmp_path) as cp, cp.client(cp.viewer) as viewer:
            r = await viewer.post(f"/api/runs/{forged}/pause", json={})
            assert r.status_code == 403

    asyncio.run(fill())
    data = ["--data-dir", str(tmp_path / "server")]
    out = invoke("audit", *data)
    assert out.exit_code == 0, out.output
    (line,) = out.output.splitlines()
    assert "\x1b" not in line
    target = "\\u001B[1A\\u001B[2K 2026-11-10 00:00:00 op run.pause x ok"
    assert f"  viewer  run.pause  {target}  refused 403  127.0.0.1" in line
    exact = invoke("audit", *data, "--jsonl")
    assert "\\u001b[1A\\u001b[2K\\r2026-11-10" in exact.output


# 76: markup in the message of an aims run error

SCENARIO = """\
version: 1
name: s
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
agents:
  - id: a
"""


def test_aims_run_shows_markup_in_an_error_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = str(tmp_path / "runs")
    replay = invoke("run", "--replay", "[/x]", "--no-server", "--runs-dir", runs)
    assert replay.exit_code == 1
    assert "error: unknown run [/x]" in replay.stderr
    odd = tmp_path / "odd.yaml"
    odd.write_text(SCENARIO + "'[/x]\\': 1\n")
    loaded = invoke("run", str(odd), "--no-server", "--runs-dir", runs)
    assert loaded.exit_code == 1
    assert "error:" in loaded.stderr
    assert "[/x]\\:" in loaded.stderr
    # a message the code wrote with brackets of its own shows them once, unescaped
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    config = tmp_path / "aims.toml"
    config.write_text(
        '[profiles.agent]\nbackend = "chat"\nbase_url = "http://agent.invalid/v1"\n'
        'model = "m"\nweights = "w"\nreasoning = "full"\n[roles]\nagent = "agent"\n'
    )
    good = tmp_path / "good.yaml"
    good.write_text(SCENARIO)
    args = ["run", str(good), "--config", str(config), "--no-server", "--runs-dir", runs]
    keyless = invoke(*args, "--monitor", "standard")
    assert keyless.exit_code == 1
    assert 'preset = "signals" under [monitor] in aims.toml' in " ".join(keyless.stderr.split())


# The rest of the CLI


def test_log_lines_show_hidden_characters_and_keep_their_lines() -> None:
    try:
        raise ValueError(f"the model said {ESCAPES}")
    except ValueError:
        record = logging.LogRecord(
            "aims", logging.ERROR, __file__, 1, "run %s: %s", ("r1", ESCAPES), sys.exc_info()
        )
    line = VisibleLog("%(levelname)s %(message)s").format(record)
    assert "\x1b" not in line
    assert "\x07" not in line
    assert line.startswith("ERROR run r1: \\u001B]0;owned\\u0007\\u001B[2J\\u001B[H\nTraceback")
    assert line.endswith("ValueError: the model said \\u001B]0;owned\\u0007\\u001B[2J\\u001B[H")


def test_argv_and_tokens_come_back_as_text(tmp_path: Path) -> None:
    data = ["--data-dir", str(tmp_path / "server")]
    name = f"a\nb{ESCAPES}"
    assert invoke("token", "create", name, *data).exit_code == 0
    listed = invoke("token", "list", *data)
    assert listed.exit_code == 0, listed.output
    (line,) = listed.output.splitlines()
    assert line.startswith("a b\\u001B]0;owned\\u0007\\u001B[2J\\u001B[H  viewer  ")
    revoked = invoke("token", "revoke", f"x{ESCAPES}", *data)
    assert revoked.exit_code == 1
    assert revoked.stderr == "error: no live token x\\u001B]0;owned\\u0007\\u001B[2J\\u001B[H\n"
    label = invoke("label", f"r{ESCAPES}", "1", "real", "--runs-dir", str(tmp_path / "runs"))
    assert label.exit_code == 1
    assert "\x1b" not in label.stderr
    assert "r\\u001B]0;owned" in label.stderr
    usage = invoke("run", f"nosuch{ESCAPES}")
    assert usage.exit_code == 2
    assert "\x1b" not in usage.output.replace("\x1b[", "")
    assert "nosuch\\u001B]0;owned" in usage.output


def test_aims_control_list_keeps_each_command_on_its_lines() -> None:
    command = {
        "created": 0.0,
        "acked": 0.25,
        "action": "inject",
        "agent": f"a{ESCAPES}",
        "then": "continue",
        "state": "done",
        "outcome": "ok\r\n2026-01-01 00:00:00  op  kill  ok",
        "by": "op\n",
        "text": f"stop\nnow{ESCAPES}",
        "reason": None,
    }
    assert _command_line(command).splitlines() == [
        "1970-01-01 00:00:00  op  inject a\\u001B]0;owned\\u0007\\u001B[2J\\u001B[H"
        "  ok 2026-01-01 00:00:00 op kill ok  250 ms",
        "    text: stop now\\u001B]0;owned\\u0007\\u001B[2J\\u001B[H",
    ]


def test_a_server_error_body_comes_back_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    answer = SimpleNamespace(status_code=409, json=lambda: {"detail": f"busy {ESCAPES}"}, text="")
    assert _problem(answer) == 1
    assert capsys.readouterr().err == "error: 409 busy \\u001B]0;owned\\u0007\\u001B[2J\\u001B[H\n"
