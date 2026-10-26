from pathlib import Path
from typing import Annotated

import typer

from aims import __version__

app = typer.Typer(name="aims", no_args_is_help=True, add_completion=False)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"aims {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = None,
) -> None:
    """Run alignment experiments on model agents and monitor what they do."""


@app.command()
def verify(
    run: Annotated[str, typer.Argument(help="Run id or run directory.")],
    runs_dir: Annotated[Path, typer.Option(help="Directory that holds the runs.")] = Path("runs"),
) -> None:
    """Check the hash chain of a run and the blobs it references."""
    from aims.events import verify_run

    path = Path(run) if Path(run).is_dir() else runs_dir / run
    result = verify_run(path)
    problems = [
        *([result.error] if result.error else []),
        *(f"missing blob {sha}" for sha in result.missing_blobs),
        *(f"blob {sha} does not match its hash" for sha in result.bad_blobs),
    ]
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(1)
    state = "finished" if result.finished else "not finished"
    typer.echo(f"ok: {result.events} events, {state}, head {result.head}")
