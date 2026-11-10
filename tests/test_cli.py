import asyncio
import re
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
