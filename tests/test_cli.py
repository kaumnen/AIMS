import asyncio
import re
import shutil
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
