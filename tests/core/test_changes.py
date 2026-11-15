"""The script that picks the CI jobs a change needs (.github/changes.py)."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
NOTHING: set[str] = set()
PYTHON = {"python"}
SANDBOX = {"python", "containers"}
WEB = {"web", "image", "bench"}
EVERYTHING = {"python", "containers", "web", "image", "bench"}
FULL = EVERYTHING | {"full"}


@pytest.fixture(scope="module")
def changes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changes", ROOT / ".github" / "changes.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A repo with one container test, a helper it imports and a test that needs no container."""
    files = {
        "tests/conftest.py": "",
        "tests/scripted.py": "import os\n",
        "tests/cp.py": "",
        "tests/run/test_run.py": "import pytest\nfrom scripted import x\n\n@pytest.mark.docker\n",
        "tests/sandbox/test_io.py": "import pytest\n\npytestmark = [pytest.mark.tracer]\n",
        "tests/sandbox/test_far.py": "def f():\n    from test_io import y\n    import cp\n",
        "tests/models/test_models.py": "# @pytest.mark.docker would need a daemon\n",
        "tests/fixtures/runs/a.jsonl": "{}\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


@pytest.mark.parametrize(
    ("paths", "on"),
    [
        (["README.md", "docs/monitor.md", "docs/images/run-dark.png", "LICENSE"], NOTHING),
        (["scripts/README.md", "CHANGELOG.md", ".gitignore"], NOTHING),
        (["web/src/routes/index.tsx", "web/package.json"], WEB),
        (["web/README.md"], WEB),
        (["web/openapi.json"], WEB | PYTHON),
        (["web/src/lib/budget-numbers.json"], WEB | PYTHON),
        (["src/aims/models/client.py", "src/aims/cli.py"], PYTHON),
        (["src/aims/models/defaults.toml"], PYTHON),
        (["src/aims/server/app.py"], PYTHON | {"bench"}),
        (["src/aims/sandbox/assets/tracer/Dockerfile"], SANDBOX),
        (["src/aims/node/runner.py"], SANDBOX),
        (["src/aims/run/core.py"], SANDBOX),
        (["src/aims/agent/loop.py"], SANDBOX),
        (["src/aims/tools/shell.py"], SANDBOX),
        (["src/aims/channels/files.py"], SANDBOX),
        (["src/aims/scenarios/word-count/README.md"], SANDBOX),
        (["skills/aims/SKILL.md"], SANDBOX),
        (["src/aims/events/store.py"], EVERYTHING),
        (["src/aims/text.py"], EVERYTHING),
        (["src/aims/aio.py"], EVERYTHING),
        (["pyproject.toml"], EVERYTHING),
        (["tests/conftest.py"], EVERYTHING),
        ([".github/workflows/ci.yml"], EVERYTHING),
        ([".github/changes.py"], EVERYTHING),
        (["uv.lock"], FULL),
        (["web/bun.lock"], FULL),
        (["Dockerfile"], EVERYTHING),
        (["scripts/seed.py"], EVERYTHING),
        (["a/new/place.txt"], EVERYTHING),
        (["docs/runs.md", "web/src/app.css"], WEB),
        (["docs/runs.md", "src/aims/models/client.py", "web/src/app.css"], WEB | PYTHON),
        (["compose.yaml", "uv.lock"], FULL),
        (["uv.lock", "compose.yaml"], FULL),
    ],
)
def test_the_rules_turn_on_what_a_path_can_break(
    changes: ModuleType, tree: Path, paths: list[str], on: set[str]
) -> None:
    assert changes.decide(paths, tree).on == on


@pytest.mark.parametrize(
    ("path", "on"),
    [
        ("tests/models/test_models.py", PYTHON),
        ("tests/models/test_gone.py", PYTHON),
        ("tests/run/test_run.py", SANDBOX),
        ("tests/sandbox/test_io.py", SANDBOX),
        ("tests/scripted.py", SANDBOX),
        ("tests/cp.py", PYTHON),
        ("tests/fixtures/runs/a.jsonl", SANDBOX),
    ],
)
def test_a_test_file_adds_the_container_tests_only_when_one_reads_it(
    changes: ModuleType, tree: Path, path: str, on: set[str]
) -> None:
    assert changes.decide([path], tree).on == on


def test_the_container_side_follows_imports_from_the_marked_files(
    changes: ModuleType, tree: Path
) -> None:
    assert changes.container_side(tree) == {
        "tests/run/test_run.py",
        "tests/sandbox/test_io.py",
        "tests/scripted.py",
    }
    (tree / "tests/sandbox/test_far.py").write_text("import pytest\n\n@pytest.mark.tracer\n")
    (tree / "tests/sandbox/test_io.py").write_text("from test_far import z\nimport cp\n")
    assert changes.container_side(tree) == {
        "tests/run/test_run.py",
        "tests/sandbox/test_far.py",
        "tests/scripted.py",
    }


def test_no_changed_path_is_a_doubt(changes: ModuleType, tree: Path) -> None:
    assert changes.decide([], tree).on == EVERYTHING


def test_the_real_tree_has_a_rule_for_every_area(changes: ModuleType) -> None:
    """A new package under src/aims or a new top-level folder gets its rule on purpose. The ones
    listed here are left to the catch-all."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    unnamed = {
        path.split("/")[0] for path in listed.split("\0") if path and changes.lookup(path) is None
    }
    assert unnamed == {
        ".dockerignore",
        ".python-version",
        "Caddyfile",
        "Dockerfile",
        "compose.public.yaml",
        "compose.yaml",
        "scripts",
    }
    side = changes.container_side(ROOT)
    assert "tests/sandbox/test_docker.py" in side
    assert "tests/scripted.py" in side
    assert "tests/core/test_text.py" not in side


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tree: Path) -> Path:
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "first")
    return tree


def commit(repo: Path, **files: str | None) -> str:
    for name, text in files.items():
        path = repo / name.replace("__", "/")
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "next")
    return git(repo, "rev-parse", "HEAD")


def test_a_diff_of_two_commits_gives_the_paths_of_both(changes: ModuleType, repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, **{"docs__a b.md": "one\n"})
    head = commit(repo, **{"src__aims__models__é.py": "x = 1\n"})
    assert changes.changed(base, head, repo) == ["docs/a b.md", "src/aims/models/é.py"]
    assert changes.run(base, head, repo).on == PYTHON
    assert changes.run(f"{head}~1", head, repo).on == PYTHON
    assert changes.run(base, f"{head}~1", repo).on == NOTHING


def test_a_rename_counts_at_both_names(changes: ModuleType, repo: Path) -> None:
    base = commit(repo, **{"web__notes.txt": "the same text\n" * 20})
    (repo / "docs").mkdir()
    git(repo, "mv", "web/notes.txt", "docs/notes.txt")
    head = commit(repo)
    assert changes.changed(base, head, repo) == ["docs/notes.txt", "web/notes.txt"]
    assert changes.run(base, head, repo).on == WEB


def test_a_removed_file_counts(changes: ModuleType, repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, **{"tests__cp.py": None})
    assert changes.run(base, head, repo).on == PYTHON


@pytest.mark.parametrize("base", ["", "0" * 40, "0" * 64, "1" * 40, "no-such-ref"])
def test_a_base_git_cannot_use_turns_everything_on(
    changes: ModuleType, repo: Path, base: str
) -> None:
    commit(repo, **{"docs__a.md": "one\n"})
    assert changes.run(base, "HEAD", repo).on == EVERYTHING


def test_a_folder_that_is_no_repo_turns_everything_on(changes: ModuleType, tree: Path) -> None:
    assert changes.changed("HEAD~1", "HEAD", tree) is None
    assert changes.run("HEAD~1", "HEAD", tree).on == EVERYTHING


def test_a_force_push_and_a_full_run_skip_the_diff(changes: ModuleType, repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, **{"docs__a.md": "one\n"})
    assert changes.run(base, head, repo).on == NOTHING
    assert changes.run(base, head, repo, forced=True).on == EVERYTHING
    assert changes.run(base, head, repo, full=True).on == FULL
    assert changes.run(base, head, repo, forced=True, full=True).on == FULL


def test_main_prints_every_switch_and_writes_the_job_output(
    changes: ModuleType,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, **{"web__src__app.css": "a {}\n"})
    output = tmp_path / "output"
    output.write_text("before=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["--base", base, "--root", str(repo), "--forced", "false", "--full", ""]
    assert changes.main(args) == 0
    lines = "python=false\ncontainers=false\nweb=true\nimage=true\nbench=true\nfull=false\n"
    out = capsys.readouterr()
    assert out.out == lines
    assert out.err == "1 changed paths\n"
    assert output.read_text() == "before=1\n" + lines

    monkeypatch.delenv("GITHUB_OUTPUT")
    assert changes.main([*args[:4], "--forced", "true"]) == 0
    out = capsys.readouterr()
    assert out.out == lines.replace("false", "true").replace("full=true", "full=false")
    assert out.err == "a force push\n"
    with pytest.raises(SystemExit):
        changes.main([*args[:4], "--full", "yes"])
