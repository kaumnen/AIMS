from typer.testing import CliRunner

from aims import __version__
from aims.cli import app


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"aims {__version__}"
