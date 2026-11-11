"""Text from agents, runs, scenario files, servers and requests reaches the operator's terminal
as the characters it holds: never read as Rich markup, never as escape bytes."""

import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from aims.cli import app
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
from aims.run.runner import Result
from aims.run.view import digest, summary
from aims.terminal import one_line, shown

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "word-count"


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
