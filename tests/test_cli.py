import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from proxy import Server, certificate, environ, serving
from typer.main import get_command
from typer.testing import CliRunner

from aims import __version__
from aims.cli import app


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"aims {__version__}"


def test_help_texts_keep_their_brackets() -> None:
    # Rich reads `[word]` as markup and drops it from --help; `\[word]` prints the brackets.
    markup = re.compile(r"(?<!\\)\[[a-z#/@][^\[\]]*\]")
    found = []

    def walk(command: Any, path: str) -> None:
        texts = [command.help or ""] + [getattr(p, "help", None) or "" for p in command.params]
        found.extend(f"{path}: {m}" for text in texts for m in markup.findall(text))
        for name, sub in getattr(command, "commands", {}).items():
            walk(sub, f"{path} {name}")

    walk(get_command(app), "aims")
    assert found == []


def test_help_offers_what_each_command_takes() -> None:
    root: Any = get_command(app)

    def helps(path: str, name: str) -> str:
        command = root
        for word in path.split():
            command = command.commands[word]
        return next(p.help for p in command.params if p.name == name)

    assert "off" not in helps("monitor", "monitor")
    assert "off" in helps("run", "monitor")
    for path in ("control", "delete", "open", "scenario delete"):
        assert "an operator token" in helps(path, "token_file"), path
    for path in ("run", "runner", "push", "monitor"):
        assert "the runner token" in helps(path, "token_file"), path


def test_options_that_would_be_ignored_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def done(**kwargs: Any) -> int:
        return 0

    class Idle:
        def __init__(self, options: Any) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            pass

    # What each command does once its options pass is not under test.
    monkeypatch.setattr("aims.cli._run", done)
    monkeypatch.setattr("aims.cli._monitor_runs", done)
    monkeypatch.setattr("aims.node.service.Node", Idle)
    token = tmp_path / "token"
    token.write_text("aims_x\n")
    file = ["--token-file", str(token)]
    server = ["--server", "http://127.0.0.1:9"]
    check = ["scenario", "check", "builtin/word-count", "--no-selftest"]
    cases = {
        "push --token-file alone": ["push", str(tmp_path), *file],
        "runner --token-file alone": ["runner", *file],
        "run --server --no-server": ["run", "builtin/word-count", "--no-server", *server],
        "monitor --token-file --no-server": ["monitor", "r1", "--no-server", *file],
        "monitor standard --signals-only": [
            "monitor",
            "r1",
            "--monitor",
            "standard",
            "--signals-only",
        ],
        "runner --keep-days -1": ["runner", *server, "--keep-days", "-1"],
        "--selftest-runs --no-selftest": [*check, "--selftest-runs", str(tmp_path / "kept")],
    }
    failed = []
    for name, args in cases.items():
        r = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "aims_x"})
        if r.exit_code != 2:
            failed.append(f"{name}: exit {r.exit_code} {r.exception!r} {r.output[-300:]!r}")
    assert failed == []


@pytest.mark.anyio
async def test_the_cli_reaches_the_control_plane_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert, tls = certificate(tmp_path, "cp.example")
    async with serving(Server(), tls) as origin, serving(Server(tunnel=origin.port)) as proxy:
        environ(monkeypatch, HTTPS_PROXY=proxy.url, SSL_CERT_FILE=str(cert))
        args = ["control", "r1", "list", "--server", "https://cp.example"]
        result = await asyncio.to_thread(CliRunner().invoke, app, args, env={"AIMS_TOKEN": "t"})
    assert result.exit_code == 0, result.output
    assert proxy.lines == ["CONNECT cp.example:443 HTTP/1.1"]
    assert origin.lines == ["GET /api/runs/r1/commands HTTP/1.1"]


def test_a_path_that_does_not_fit_ends_in_an_error_not_a_traceback(tmp_path: Path) -> None:
    (run,) = (Path(__file__).parent / "fixtures" / "runs" / "word-count").iterdir()
    runs = tmp_path / "runs"
    shutil.copytree(run, runs / run.name)
    a_file = tmp_path / "a-file"
    a_file.write_text("x\n")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "server.sqlite").write_text("not a database\n")
    (broken / "server.json").write_text("{not json\n")
    nowhere = tmp_path / "no-such-dir" / "out"
    missing = tmp_path / "missing"
    report = ["report", run.name, "--runs-dir", str(runs)]
    ui = ["ui", "--no-open", "--no-runner", "--data-dir", str(tmp_path / "ui")]
    runner = ["runner", "--server", "https://127.0.0.1:9", "--tls-ca"]
    cases = {
        "verify a file": (["verify", str(a_file)], 1),
        "rates --runs-dir a file": (["rates", "--runs-dir", str(a_file)], 2),
        "report --out nowhere": ([*report, "--out", str(nowhere)], 1),
        "report --html a directory": ([*report, "--html", str(runs)], 1),
        "openapi --out nowhere": (["openapi", "--out", str(nowhere)], 1),
        "audit --data-dir a file": (["audit", "--data-dir", str(a_file)], 2),
        "token list --data-dir a file": (["token", "list", "--data-dir", str(a_file)], 2),
        "token list, not a database": (["token", "list", "--data-dir", str(broken)], 1),
        "ui --tls-cert missing": ([*ui, "--tls-cert", str(missing), "--tls-key", str(a_file)], 2),
        "ui --tls-cert not PEM": ([*ui, "--tls-cert", str(a_file), "--tls-key", str(a_file)], 1),
        "runner --tls-ca missing": ([*runner, str(missing)], 2),
        "runner --tls-ca not PEM": ([*runner, str(a_file)], 1),
    }
    failed = []
    for name, (args, code) in cases.items():
        r = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "aims_x"})
        if (r.exit_code, type(r.exception)) != (code, SystemExit):
            failed.append(f"{name}: exit {r.exit_code} {r.exception!r} {r.output[-300:]!r}")
    for args in (["control", "r1", "list"], ["push", str(runs)], ["open", "--no-open"]):
        r = CliRunner().invoke(app, args, env={"AIMS_SERVER_DIR": str(broken)})
        if (r.exit_code, type(r.exception)) != (1, SystemExit) or "not JSON" not in r.output:
            failed.append(f"{args[0]}, server.json not JSON: {r.exception!r} {r.output!r}")
    assert failed == []


def test_text_arguments_utf8_cannot_encode_are_refused(tmp_path: Path) -> None:
    """A byte in argv that is not UTF-8 reaches Python as a lone surrogate. Text that the log,
    a model, the control plane or SQLite would take is refused before the command runs."""
    bad = "a\udcff"
    server = ["--server", "http://127.0.0.1:9"]
    runs = ["--runs-dir", str(tmp_path / "runs")]
    cases = {
        "--text": ["control", "r1", "inject", "--text", bad, *server],
        "--reason": ["control", "r1", "kill", "--reason", bad, *server],
        "run": ["control", bad, "pause", *server],
        "--inject": ["run", "--replay", "r1", "--inject", bad, *runs],
        "--series": ["run", "builtin/word-count", "--series", bad, *runs, "--replay", "r1"],
        "--variant": ["run", "--replay", "r1", "--variant", bad, *runs],
        "--profile": ["run", "--replay", "r1", "--profile", bad, *runs],
        "runs": ["monitor", "r1", bad, *runs],
        "--note": ["label", "r1", "3", "real", "--note", bad, *runs],
    }
    failed = []
    for hint, args in cases.items():
        r = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "aims_x"})
        said = " ".join(r.output.split())
        refused = f"Invalid value for '{hint}': holds a byte that is not UTF-8." in said
        if (r.exit_code, type(r.exception), refused) != (2, SystemExit, True):
            failed.append(f"{hint}: exit {r.exit_code} {r.exception!r} {r.output[-300:]!r}")
    assert failed == []


def test_a_path_utf8_cannot_encode_reaches_the_file_system(tmp_path: Path) -> None:
    """A file name that is not UTF-8 is legal on Linux, and the file system takes it back as the
    same bytes, so a path argument is let through."""
    r = CliRunner().invoke(app, ["verify", "r1", "--runs-dir", str(tmp_path / "nope\udcff")])
    assert (r.exit_code, type(r.exception)) == (1, SystemExit)
    assert "error: no events.jsonl in " in r.output
    assert "nope\\uDCFF/r1" in r.output
    r = CliRunner().invoke(app, ["scenario", "check", "nope\udcff"])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit)
    assert "no file nope\\uDCFF and no built-in scenario" in " ".join(r.output.split())


def test_a_byte_in_argv_that_is_not_utf8_is_refused() -> None:
    args = [b"control", b"r1", b"inject", b"--text", b"a\xff", b"--server", b"http://127.0.0.1:9"]
    env = {**os.environ, "AIMS_TOKEN": "aims_x"}
    p = subprocess.run(
        [os.fsencode(sys.executable), b"-m", b"aims", *args],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 2, p.stderr[-500:]
    assert b"Invalid value for '--text': holds a byte that is not UTF-8." in p.stderr


def test_every_command_checks_its_text_arguments() -> None:
    from aims.cli import _Utf8Command

    leaves: list[str] = []

    def walk(command: Any, path: str) -> None:
        subs = getattr(command, "commands", None)
        if subs is None and not isinstance(command, _Utf8Command):
            leaves.append(path)
        for name, sub in (subs or {}).items():
            walk(sub, f"{path} {name}")

    walk(get_command(app), "aims")
    assert leaves == []


RUN = Path(__file__).parent / "fixtures/runs/misbehave/01a1a36a-d65d-7623-83e4-476ce247a487"


def test_verify_takes_a_dot_inside_a_run_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's id is the folder's name, which `.` has only once it is resolved."""
    run = tmp_path / "runs" / RUN.name
    shutil.copytree(RUN, run)
    monkeypatch.chdir(run)
    for arg in (".", "./", str(run)):
        r = CliRunner().invoke(app, ["verify", arg])
        assert r.exit_code == 0, (arg, r.output)
        assert r.output.startswith("ok: 93 events, finished"), arg
    monkeypatch.chdir(run.parent)
    r = CliRunner().invoke(app, ["verify", f"{RUN.name}/"])
    assert r.exit_code == 0, r.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only folder")
@pytest.mark.parametrize("indexed", [True, False])
def test_a_runs_folder_that_cannot_be_written_ends_in_one_line(
    tmp_path: Path, indexed: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every command that reads runs opens the index to write. A folder it may not write, such
    as runs another user recorded, is the person's to fix: no traceback."""
    runs = tmp_path / "runs"
    shutil.copytree(RUN, runs / RUN.name)
    if indexed:
        assert CliRunner().invoke(app, ["spend", "--runs-dir", str(runs)]).exit_code == 0
    paths = [runs, *runs.rglob("*")]
    for path in paths:
        path.chmod(path.stat().st_mode & ~0o222)
    try:
        for args in (["report", RUN.name], ["check", RUN.name], ["spend"], ["rates"]):
            capsys.readouterr()
            # as the `aims` command calls it: CliRunner goes past the app's own error handling
            with pytest.raises(SystemExit) as end:
                app([*args, "--runs-dir", str(runs)])
            assert end.value.code == 1, args
            out = capsys.readouterr()
            assert out.out == "", args
            [line] = out.err.splitlines()
            assert line.startswith(f"error: {runs} cannot be written, and aims keeps its index")
        # verify reads the log and its chain with no index
        r = CliRunner().invoke(app, ["verify", RUN.name, "--runs-dir", str(runs)])
        assert r.exit_code == 0, r.output
        assert "ok: 93 events, finished" in r.output
    finally:
        for path in paths:
            path.chmod(path.stat().st_mode | 0o200)
