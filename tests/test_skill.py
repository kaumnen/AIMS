"""The agent skill in skills/aims follows the Agent Skills spec, its links resolve, every
command it shows exists with the options it uses, and its starter scenario passes its
self-test."""

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from scripted import models, profile
from typer.main import get_command
from typer.testing import CliRunner

from aims.cli import app
from aims.events import LocalEventStore
from aims.run import selftest as st
from aims.run.runner import Bench
from aims.sandbox import DockerBackend
from aims.scenario import load

SKILL = Path(__file__).parents[1] / "skills" / "aims"
STARTER = SKILL / "assets" / "scenario" / "scenario.yaml"
TEXTS = sorted(SKILL.rglob("*.md"))
FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINK = re.compile(r"\]\(([^)\s]+)\)")
INLINE = re.compile(r"`(aims [^`]+)`")
FENCE = re.compile(r"```sh\n(.*?)```", re.S)


def frontmatter() -> tuple[dict[str, Any], str]:
    head, body = (SKILL / "SKILL.md").read_text().removeprefix("---\n").split("\n---\n", 1)
    return dict(YAML(typ="safe").load(head)), body


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def commands() -> list[tuple[Path, str]]:
    found = []
    for path in TEXTS:
        text = path.read_text()
        for block in FENCE.findall(text):
            found += [(path, line) for line in block.splitlines() if line.startswith("aims ")]
        found += [(path, line) for line in INLINE.findall(text)]
    return found


def resolve(line: str) -> tuple[Any, list[str]]:
    words = line.split(" #")[0].split()[1:]
    command: Any = get_command(app)
    rest = []
    for i, word in enumerate(words):
        if word in getattr(command, "commands", {}):
            command = command.commands[word]
            continue
        rest = words[i:]
        break
    return command, rest


def test_the_frontmatter_follows_the_spec() -> None:
    meta, body = frontmatter()
    assert set(meta) <= FIELDS
    assert NAME.match(meta["name"])
    assert len(meta["name"]) <= 64
    assert meta["name"] == SKILL.name
    assert 0 < len(meta["description"]) <= 1024
    assert len(body.splitlines()) < 500


def test_every_link_resolves() -> None:
    broken = []
    for path in TEXTS:
        text = path.read_text()
        anchors = {slug(h) for h in re.findall(r"^#+ (.+)$", text, re.M)}
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://")):
                continue
            file, _, anchor = target.partition("#")
            if file and not (path.parent / file).exists():
                broken.append(f"{path.name}: {target}")
            if not file and anchor not in anchors:
                broken.append(f"{path.name}: {target}")
    assert broken == []


def test_every_command_exists_with_the_options_it_uses() -> None:
    found = commands()
    assert len(found) > 20
    wrong = []
    for path, line in found:
        command, rest = resolve(line)
        if hasattr(command, "commands") and command.name != "aims":
            wrong.append(f"{path.name}: {line}: names a group, not a command")
        if command.name == "aims" and rest and not rest[0].startswith(("<", "-")):
            wrong.append(f"{path.name}: {line}: no command {rest[0]}")
        known = {"--help"} | {o for p in command.params for o in (*p.opts, *p.secondary_opts)}
        for word in rest:
            option = word.split("=")[0]
            if option.startswith("--") and option not in known:
                wrong.append(f"{path.name}: {line}: no option {option}")
    assert wrong == []


def test_the_starter_scenario_checks_and_loads_every_variant(tmp_path: Path) -> None:
    runs = str(tmp_path / "runs")
    check = ["scenario", "check", str(STARTER.parent), "--no-selftest", "--runs-dir", runs]
    result = CliRunner().invoke(app, check)
    assert result.exit_code == 0, result.output
    bundle = load(STARTER)
    assert set(bundle.scenario.variants) == {"observed", "unobserved"}
    for variant in bundle.scenario.variants:
        assert load(STARTER, variant).scenario.name == "word-count-grader"
    assert bundle.scenario.selftest is not None
    assert set(bundle.scenario.selftest.claims) == {p.name for p in bundle.scenario.plants}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        backend = await DockerBackend.open(audit="kernel")
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER") and os.environ.get("AIMS_REQUIRE_TRACER"):
            raise
        pytest.skip(f"Docker with the kernel tracer is not available: {e}")
    async with backend:
        yield backend


@pytest.mark.docker
@pytest.mark.anyio
async def test_the_starter_scenario_passes_its_selftest(
    backend: DockerBackend, tmp_path: Path
) -> None:
    bundle = load(STARTER)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        bench = Bench(store, backend, models(profile()))
        outcomes = await st.run(bench, bundle, st.cases(bundle))
    assert [(o.case, o.problems) for o in outcomes] == [("reference", []), ("exploit", [])]
    assert outcomes[0].notes == ["agent: reward 1"]
