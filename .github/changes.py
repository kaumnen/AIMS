"""Which CI jobs a change needs. Reads the paths that differ between two commits, looks each up in
RULES and prints one `name=true|false` line per switch, also to $GITHUB_OUTPUT when it is set.
Every doubt turns everything on: no base commit, a force push, a failed diff, an empty diff or a
path no rule names. Standard library only, so the job runs it with the runner's Python."""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

# python: lint, types and the tests that need no container. containers: the docker and tracer
# tests. web, image, bench: the jobs of those names. full: the second Python version too.
SWITCHES = ("python", "containers", "web", "image", "bench", "full")

NONE: frozenset[str] = frozenset()
PYTHON = frozenset({"python"})
SANDBOX = frozenset({"python", "containers"})
WEB = frozenset({"web", "image", "bench"})
EVERYTHING = frozenset(SWITCHES) - {"full"}
FULL = frozenset(SWITCHES)

# The first pattern that matches a path decides. `*` also matches `/`. A path that matches none
# turns everything on.
RULES: tuple[tuple[str, frozenset[str]], ...] = (
    # A new lock can break anything on either Python.
    ("uv.lock", FULL),
    ("web/bun.lock", FULL),
    # Shared by every area.
    ("pyproject.toml", EVERYTHING),
    (".github/*", EVERYTHING),
    ("tests/conftest.py", EVERYTHING),
    ("src/aims/events/*", EVERYTHING),
    ("src/aims/text.py", EVERYTHING),
    ("src/aims/aio.py", EVERYTHING),
    # Web files a Python test reads.
    ("web/openapi.json", WEB | PYTHON),
    ("web/src/lib/budget-numbers.json", WEB | PYTHON),
    ("web/*", WEB),
    # The sandbox side: what the container tests run.
    ("src/aims/sandbox/*", SANDBOX),
    ("src/aims/node/*", SANDBOX),
    ("src/aims/run/*", SANDBOX),
    ("src/aims/agent/*", SANDBOX),
    ("src/aims/tools/*", SANDBOX),
    ("src/aims/channels/*", SANDBOX),
    ("src/aims/scenarios/*", SANDBOX),
    # The skill's scenario runs in a container test.
    ("skills/*", SANDBOX),
    # The bench drives the server.
    ("src/aims/server/*", PYTHON | {"bench"}),
    ("src/aims/*", PYTHON),
    # A test file adds the container tests when one of them is or imports it (see `decide`).
    ("tests/*", PYTHON),
    # Text nothing runs.
    ("docs/*", NONE),
    ("*.md", NONE),
    ("LICENSE", NONE),
    ("NOTICE", NONE),
    (".gitignore", NONE),
    (".editorconfig", NONE),
)

ZERO = re.compile(r"0*")
MARKED = re.compile(
    r"^\s*(?:@pytest\.mark\.(?:docker|tracer)\b|pytestmark\s*=.*\bmark\.(?:docker|tracer)\b)", re.M
)
IMPORT = re.compile(r"^\s*(?:from|import)\s+(\w+)", re.M)


@dataclass(frozen=True)
class Decision:
    on: frozenset[str]
    why: str

    def lines(self) -> list[str]:
        return [f"{name}={'true' if name in self.on else 'false'}" for name in SWITCHES]


def lookup(path: str) -> frozenset[str] | None:
    """The switches of the first rule that matches, None when no rule names the path."""
    for pattern, switches in RULES:
        if fnmatchcase(path, pattern):
            return switches
    return None


def container_side(root: Path) -> frozenset[str]:
    """The files under tests/ a container test is or imports, as repo paths. Test modules import
    each other by bare name, so the module name is the file name."""
    tests = root / "tests"
    texts = {path: path.read_text(encoding="utf-8") for path in tests.rglob("*.py")}
    by_name = {path.stem: path for path in texts}
    todo = [path for path, text in texts.items() if MARKED.search(text)]
    seen = set(todo)
    while todo:
        for name in IMPORT.findall(texts[todo.pop()]):
            found = by_name.get(name)
            if found is not None and found not in seen:
                seen.add(found)
                todo.append(found)
    return frozenset(path.relative_to(root).as_posix() for path in seen)


def decide(paths: Iterable[str], root: Path) -> Decision:
    on: set[str] = set()
    side: frozenset[str] | None = None
    unnamed: str | None = None
    count = 0
    for path in paths:
        count += 1
        switches = lookup(path)
        if switches is None:
            unnamed = unnamed or path
            switches = EVERYTHING
        on |= switches
        if path.startswith("tests/"):
            if side is None:
                side = container_side(root)
            # A fixture has no imports to follow, so it counts as read by a container test.
            if path in side or not path.endswith(".py"):
                on.add("containers")
    if not count:
        return Decision(EVERYTHING, "no changed paths")
    if unnamed is not None:
        return Decision(frozenset(on), f"no rule names {unnamed}")
    return Decision(frozenset(on), f"{count} changed paths")


def changed(base: str, head: str, root: Path) -> list[str] | None:
    """The paths that differ, both names of a rename. None when git cannot tell."""
    command = ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"]
    try:
        done = subprocess.run(command, cwd=root, capture_output=True, check=False)
    except OSError:
        return None
    if done.returncode:
        return None
    return [path for path in done.stdout.decode("utf-8", "surrogateescape").split("\0") if path]


def run(base: str, head: str, root: Path, forced: bool = False, full: bool = False) -> Decision:
    if full:
        return Decision(FULL, "a full run was asked for")
    if forced:
        return Decision(EVERYTHING, "a force push")
    if ZERO.fullmatch(base):
        return Decision(EVERYTHING, "no base commit")
    paths = changed(base, head, root)
    if paths is None:
        return Decision(EVERYTHING, f"git could not diff {base} and {head}")
    return decide(paths, root)


def flag(value: str) -> bool:
    """A GitHub expression prints a boolean as true or false and a missing one as nothing."""
    if value in ("", "false"):
        return False
    if value == "true":
        return True
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="the commit to compare with; empty when new")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--forced", type=flag, default=False, metavar="true|false")
    parser.add_argument("--full", type=flag, default=False, metavar="true|false")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    decision = run(args.base, args.head, args.root, forced=args.forced, full=args.full)
    text = "\n".join(decision.lines()) + "\n"
    print(decision.why, file=sys.stderr)
    sys.stdout.write(text)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
