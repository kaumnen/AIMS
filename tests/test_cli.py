import re
from typing import Any

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
