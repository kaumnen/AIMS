import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from typer.testing import CliRunner

from aims.channels import ChannelSetup
from aims.channels.folder import Folder
from aims.cli import app
from aims.monitor.config import MonitorConfig, merge
from aims.run import selftest, truth
from aims.run.runner import _reward
from aims.scenario import (
    Bundle,
    Scenario,
    ScenarioError,
    builtin_names,
    builtin_root,
    find,
    from_snapshot,
    load,
    parse,
)

BASE = """\
version: 1
name: demo
oversight: observe
task:
  instruction: do it
  environment: {image: python:3.13-slim}
"""
WORD_COUNT = (
    Path(__file__).parent.parent / "src" / "aims" / "scenarios" / "word-count" / "scenario.yaml"
)


def test_defaults_fill_an_agent() -> None:
    s = parse(BASE)
    (agent,) = s.agents
    assert agent.id == "agent"
    assert agent.tool_names == ["bash", "edit", "submit", "report_to_operator"]
    assert (agent.budget.steps, agent.budget.wall_s) == (100, 3600)
    assert s.sandbox.command_timeout_s == 120


def test_yaml_1_2_keeps_strings_and_numbers() -> None:
    s = parse(BASE + "  setup: [no, on, '1:30']\n  setup_timeout_s: 010\n")
    assert s.task.setup == ["no", "on", "1:30"]
    assert s.task.setup_timeout_s == 10


def test_submit_is_always_on() -> None:
    s = parse(BASE + "agents:\n  - id: a\n    tools: [bash]\n")
    assert s.agents[0].tool_names == ["bash", "submit"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("  bogus: 1\n", "s.yaml:7: task.bogus: Extra inputs are not permitted"),
        (
            "  files:\n    - path: a\n      content: x\n      mode: 0755\n",
            's.yaml:10: task.files[0].mode: Value error, quote the mode, as in "0644"',
        ),
        (
            "  files:\n    - path: /etc/passwd\n      content: x\n",
            "is outside /work, /tmp, /home/agent, /dev/shm",
        ),
        ("agents:\n  - id: a\n  - id: a\n", "agent ids must be unique"),
        ("agents:\n  - id: Bad Name\n", "s.yaml:8: agents[0].id: Value error"),
        (
            "plants:\n  - {name: k, kind: file, path: k, content: x, category: drift,"
            " used_in: [src/**]}\n",
            "used_in needs a canary",
        ),
    ],
)
def test_errors_name_the_line(extra: str, message: str) -> None:
    with pytest.raises(ScenarioError) as e:
        parse(BASE + extra, source="s.yaml")
    assert any(message in p for p in e.value.problems), e.value.problems


def test_oversight_is_required_and_one_of_three() -> None:
    with pytest.raises(ScenarioError, match="oversight"):
        parse(BASE.replace("oversight: observe\n", ""))
    with pytest.raises(ScenarioError, match="oversight"):
        parse(BASE.replace("observe", "watch"))
    for mode in ("autonomous", "human"):
        assert parse(BASE.replace("observe", mode)).oversight == mode


def test_bad_yaml_has_a_line() -> None:
    with pytest.raises(ScenarioError, match=r"s.yaml:3"):
        parse("version: 1\nname: x\n  bad: [\n", source="s.yaml")


def test_bundle_tars_files_with_group_write(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "run.sh").write_text("#!/bin/sh")
    os.chmod(tmp_path / "src" / "run.sh", 0o755)
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "x.pyc").write_text("x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.sh").write_text("exit 0")
    path = tmp_path / "s.yaml"
    path.write_text(
        BASE
        + "  files:\n    - path: pkg\n      source: src\n"
        + "    - path: deep/note.txt\n      content: hi\n      mode: '0600'\n"
        + "  verifier:\n    command: sh $AIMS_TESTS_DIR/t.sh\n    files: tests\n"
    )
    bundle = load(path)
    assert bundle.files is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.files)) as tar:
        modes = {m.name: (m.type, oct(m.mode)) for m in tar.getmembers()}
    assert modes == {
        "work": (tarfile.DIRTYPE, "0o775"),
        "work/pkg": (tarfile.DIRTYPE, "0o775"),
        "work/pkg/a.py": (tarfile.REGTYPE, "0o664"),
        "work/pkg/run.sh": (tarfile.REGTYPE, "0o775"),
        "work/deep": (tarfile.DIRTYPE, "0o775"),
        "work/deep/note.txt": (tarfile.REGTYPE, "0o600"),
    }
    assert bundle.verifier is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.verifier)) as tar:
        assert tar.getnames() == ["tests", "tests/t.sh"]

    digest = bundle.digest
    os.utime(tmp_path / "src" / "a.py", (1, 1))
    assert load(path).digest == digest
    (tmp_path / "src" / "a.py").write_text("b")
    assert load(path).digest != digest


def test_a_task_file_under_dev_shm_is_seeded_below_it(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(BASE + "  files:\n    - path: /dev/shm/cache/blob.txt\n      content: hi\n")
    bundle = load(path)
    assert bundle.files is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.files)) as tar:
        modes = {m.name: (m.type, oct(m.mode)) for m in tar.getmembers()}
    assert modes == {
        "dev/shm/cache": (tarfile.DIRTYPE, "0o775"),
        "dev/shm/cache/blob.txt": (tarfile.REGTYPE, "0o664"),
    }


def test_snapshot_restores_the_bundle() -> None:
    bundle = load(WORD_COUNT)
    snapshot = bundle.snapshot()
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    restored = from_snapshot(json.loads(json.dumps(snapshot)), blobs)
    assert restored.digest == bundle.digest
    assert restored.scenario == bundle.scenario
    assert restored.dockerfile == "Dockerfile"
    snapshot["digest"] = "0" * 64
    with pytest.raises(ScenarioError, match="digest"):
        from_snapshot(snapshot, blobs)


def test_a_recorded_monitor_naming_every_field_keeps_only_its_changes() -> None:
    bundle = load(WORD_COUNT)
    named = {"preset": "signals", "paths": {"tests": ["t/**"]}, "policy": {"pause_timeout_s": 20}}
    scenario = bundle.scenario.model_copy(update={"monitor": MonitorConfig.model_validate(named)})
    text = rfc8785.dumps(scenario.model_dump(mode="json"))
    old = Bundle(scenario, text, bundle.files, bundle.verifier, bundle.context, bundle.dockerfile)
    blobs = {hashlib.sha256(b).hexdigest(): b for b in old.blobs()}
    restored = from_snapshot(old.snapshot(), blobs).scenario.monitor
    assert restored is not None
    assert restored.model_dump(mode="json", exclude_unset=True) == named
    layer = {"extract": ["cheap"], "paths": {"grader": ["g"]}}
    merged = merge(layer, restored.model_dump(mode="json", exclude_unset=True))
    assert merged.extract == ["cheap"]
    assert (merged.paths.tests, merged.paths.grader) == (["t/**"], ["g"])


def test_missing_sources_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        BASE.replace("{image: python:3.13-slim}", "{dockerfile: env/Dockerfile}")
        + "  files:\n    - path: a\n      source: nope\n"
    )
    with pytest.raises(ScenarioError) as e:
        load(path)
    assert len(e.value.problems) == 2


def test_reward_files_are_read() -> None:
    assert _reward(b"J", b'{"reward": 0.5, "passed": 1}') == (
        0.5,
        {"reward": 0.5, "passed": 1},
        "reward.json",
        None,
    )
    assert _reward(b"J", b'{"accuracy": 0.25}')[0] == 0.25
    assert _reward(b"J", b'{"a": 1, "b": 2}')[0] is None
    assert _reward(b"T", b" 1\n") == (1.0, {"reward": 1.0}, "reward.txt", None)
    assert _reward(b"T", b"nan")[3] == "reward.txt is not finite: nan"
    assert _reward(b"N", b"")[2:] == ("none", "the verifier wrote no reward")


def test_reward_json_numbers_the_log_cannot_carry_become_text() -> None:
    body = b'{"reward": 1, "steps": ' + b"9" * 400 + b', "x": NaN, "n": [1e999]}'
    reward, metrics, _, error = _reward(b"J", body)
    assert (reward, error) == (1.0, None)
    assert metrics == {"reward": 1, "steps": "9" * 400, "x": "nan", "n": ["inf"]}
    assert _reward(b"J", b'{"total": ' + b"9" * 400 + b"}")[0] is None


def test_reward_json_keeps_a_lone_surrogate_as_text() -> None:
    body = b'{"reward": 1, "note": "\\ud83d", "\\udfff": ["\\ud800"]}'
    reward, metrics, _, error = _reward(b"J", body)
    assert (reward, error) == (1.0, None)
    assert metrics == {"reward": 1, "note": "\\ud83d", "\\udfff": ["\\ud800"]}
    rfc8785.dumps(metrics)


def test_reward_json_nested_deeper_than_an_event_holds_is_unreadable() -> None:
    assert _reward(b"J", b'{"a": ' + b"[" * 63 + b"]" * 63 + b"}")[3] is None
    for depth in (64, 300, 5_000, 200_000):
        body = b'{"a": ' + b"[" * depth + b"]" * depth + b"}"
        reward, metrics, source, error = _reward(b"J", body)
        assert (reward, metrics, source) == (None, {}, "reward.json")
        assert (error or "").startswith("reward.json is not JSON: ")
    assert _reward(b"J", b"[" * 200_000)[3] is not None


def test_cli_checks_and_exports() -> None:
    runner = CliRunner()
    ok = runner.invoke(app, ["scenario", "check", str(WORD_COUNT)])
    assert ok.exit_code == 0, ok.output
    assert ok.stdout.startswith("ok: word-count")
    schema = runner.invoke(app, ["scenario", "schema"])
    assert json.loads(schema.stdout) == Scenario.model_json_schema()
    bad = runner.invoke(app, ["scenario", "check", str(WORD_COUNT), "--profile", "missing"])
    assert bad.exit_code == 1
    assert "unknown profile 'missing'" in bad.stderr
    usage = runner.invoke(app, ["run", "--fork-at", "3", str(WORD_COUNT)])
    assert usage.exit_code == 2
    assert runner.invoke(app, ["run"]).exit_code == 2


def test_a_scenario_is_named_by_file_directory_or_built_in_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = builtin_root()
    assert root is not None
    shipped = root / "cache-keys" / "scenario.yaml"
    assert find(Path("cache-keys")) == shipped
    assert find(Path("builtin/cache-keys")) == shipped
    assert find(Path("elsewhere/cache-keys")) == Path("elsewhere/cache-keys")
    assert find(Path("builtin/missing")) == Path("builtin/missing")
    assert find(Path("word-count")) == root / "word-count" / "scenario.yaml"
    (tmp_path / "word-count").mkdir()
    assert find(Path("word-count")) == Path("word-count/scenario.yaml")
    (tmp_path / "cache-keys").write_text("a file wins over a built-in name")
    assert find(Path("cache-keys")) == Path("cache-keys")
    assert builtin_names() == ["cache-keys", "shared-cache", "word-count"]

    runner = CliRunner()
    ok = runner.invoke(app, ["scenario", "check", "builtin/word-count", "--no-selftest"])
    assert ok.exit_code == 0, ok.output
    assert ok.stdout.startswith("ok: word-count")
    unknown = runner.invoke(app, ["scenario", "check", "nope"])
    assert unknown.exit_code == 2
    assert "builtin/cache-keys" in unknown.stderr
    (tmp_path / "shared-cache").mkdir()
    shadowed = runner.invoke(app, ["scenario", "check", "shared-cache", "--no-selftest"])
    assert shadowed.exit_code == 1
    assert "shared-cache/scenario.yaml: No such file" in shadowed.stderr


def test_texts_layer_defaults_config_and_scenario(tmp_path: Path) -> None:
    from aims.models import load_models
    from aims.prompts import load_prompts
    from aims.run.runner import check, resolve_texts

    config = tmp_path / "aims.toml"
    config.write_text(
        '[prompts]\nsystem = "from config"\nnudge = "config nudge"\n'
        '[prompts.tools.report_to_operator]\nreply = "noted"\n'
    )
    s = parse(
        BASE
        + "agents:\n  - id: a\n    prompts:\n      nudge: scenario nudge\n"
        + "      tools:\n        submit: {reply: bye}\n"
    )
    texts = resolve_texts(load_prompts(config), s.agents[0])
    assert (texts.agent.system, texts.agent.nudge) == ("from config", "scenario nudge")
    assert texts.tools["submit"]["reply"] == "bye"
    assert texts.tools["report_to_operator"]["reply"] == "noted"
    assert texts.tools["bash"]["description"].startswith("Run a command with bash in {workdir}")

    bad = parse(BASE + "agents:\n  - id: a\n    prompts:\n      budget_steps: '{n} steps'\n")
    problems = check(load(WORD_COUNT), load_models(None), None, None)
    assert problems == []
    from aims.scenario import Bundle

    problems = check(Bundle(bad, b""), load_models(None))
    assert len(problems) == 1
    assert problems[0].startswith("agent a: budget_steps: unknown placeholder n")


def test_prompts_show_prints_an_editable_table(tmp_path: Path) -> None:
    import tomllib

    config = tmp_path / "aims.toml"
    config.write_text('[prompts]\nnudge = "carry on"\n[prompts.tools.bash]\ntimeout = "slow"\n')
    out = CliRunner().invoke(app, ["prompts", "show", "--config", str(config)])
    assert out.exit_code == 0, out.output
    table = tomllib.loads(out.stdout)["prompts"]
    assert table["nudge"] == "carry on"
    assert table["tools"]["bash"]["timeout"] == "slow"
    assert set(table["tools"]) == {"bash", "edit", "submit", "report_to_operator"}
    config.write_text("[prompts]\nbogus = 1\n")
    bad = CliRunner().invoke(app, ["prompts", "show", "--config", str(config)])
    assert bad.exit_code == 1
    assert "bogus" in bad.stderr


@pytest.fixture
def linked(tmp_path: Path) -> Path:
    """A scenario folder whose `link` is a symlink to a sibling folder outside it."""
    outside = tmp_path / "outside"
    (outside / "tests").mkdir(parents=True)
    (outside / "secret.txt").write_text("secret")
    (outside / "tests" / "t.sh").write_text("exit 0")
    (outside / "Dockerfile").write_text("FROM scratch\n")
    (outside / "ref.sh").write_text("true\n")
    base = tmp_path / "scenario"
    base.mkdir()
    (base / "link").symlink_to("../outside")
    return base


def _out(field: str, rel: str) -> str:
    return rf"{re.escape(field)}: {re.escape(rel)} leads outside the scenario's directory"


@pytest.mark.parametrize(
    ("extra", "field", "rel"),
    [
        (
            "  files:\n    - path: a\n      source: link/secret.txt\n",
            "task.files[0].source",
            "link/secret.txt",
        ),
        (
            "  verifier:\n    command: x\n    files: link/tests\n",
            "task.verifier.files",
            "link/tests",
        ),
        ("  verifier:\n    command: x\n    files: link\n", "task.verifier.files", "link"),
    ],
)
def test_load_refuses_a_path_through_a_symlink_out(
    linked: Path, extra: str, field: str, rel: str
) -> None:
    path = linked / "scenario.yaml"
    path.write_text(BASE + extra)
    with pytest.raises(ScenarioError, match=_out(field, rel)):
        load(path)


@pytest.mark.parametrize("dockerfile", ["link/Dockerfile", "Dockerfile"])
def test_load_refuses_a_dockerfile_through_a_symlink_out(linked: Path, dockerfile: str) -> None:
    (linked / "Dockerfile").symlink_to("../outside/Dockerfile")
    path = linked / "scenario.yaml"
    path.write_text(BASE.replace("{image: python:3.13-slim}", f"{{dockerfile: {dockerfile}}}"))
    with pytest.raises(ScenarioError, match=_out("task.environment.dockerfile", dockerfile)):
        load(path)


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        (
            "plants:\n  - {name: k, kind: file, path: k.txt, source: link/secret.txt,"
            " category: drift}\n",
            "plants[0].source",
        ),
        (
            "plants:\n  - {name: i, kind: impossible, reference: link/secret.txt}\n",
            "plants[0].reference",
        ),
        (
            "agents:\n  - id: a\n    task: {files: [{path: b, source: link/secret.txt}]}\n",
            "agents[0].task.files[0].source",
        ),
        (
            "channels:\n  - {name: n, kind: folder, path: /shared,"
            " files: [{path: b, source: link/secret.txt}]}\n",
            "channels[0].files[0].source",
        ),
    ],
)
def test_prepare_refuses_a_source_through_a_symlink_out(
    linked: Path, extra: str, field: str
) -> None:
    path = linked / "scenario.yaml"
    path.write_text(BASE + extra)
    with pytest.raises(ScenarioError, match=_out(field, "link/secret.txt")):
        truth.prepare(load(path), seed=b"s")


def test_a_folder_channel_refuses_a_source_through_a_symlink_out(linked: Path) -> None:
    s = parse(
        BASE + "channels:\n  - {name: n, kind: folder, path: /shared,"
        " files: [{path: b, source: link/secret.txt}]}\n"
    )
    store = cast(Any, None)
    setup = ChannelSetup(config=s.channels[0], scenario=s, run_id="r", store=store, base=linked)
    with pytest.raises(ScenarioError, match=_out("channel n: files[0].source", "link/secret.txt")):
        Folder(setup)


def test_a_selftest_script_through_a_symlink_out_is_refused(linked: Path) -> None:
    path = linked / "scenario.yaml"
    path.write_text(BASE + "selftest: {reference: link/ref.sh}\n")
    with pytest.raises(selftest.SelfTestError, match=_out("selftest.reference", "link/ref.sh")):
        selftest.cases(load(path))


def test_symlinks_that_stay_inside_still_load(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "tests").mkdir(parents=True)
    (real / "a.txt").write_text("a")
    (real / "tests" / "t.sh").write_text("exit 0")
    (real / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "inner").symlink_to("real")
    (tmp_path / "away").symlink_to("../elsewhere")
    path = tmp_path / "scenario.yaml"
    path.write_text(
        BASE.replace("{image: python:3.13-slim}", "{dockerfile: inner/Dockerfile}")
        + "  files:\n    - path: a.txt\n      source: inner/a.txt\n"
        + "    - path: away\n      source: away\n"
        + "  verifier:\n    command: x\n    files: inner/tests\n"
        + "plants:\n  - {name: k, kind: file, path: k.txt, source: inner/a.txt, category: drift}\n"
    )
    bundle = load(path)
    assert bundle.files is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.files)) as tar:
        got = {m.name: (m.type, m.linkname) for m in tar.getmembers()}
    assert got["work/a.txt"] == (tarfile.REGTYPE, "")
    # A last link that leads out is stored as a link, never read.
    assert got["work/away"] == (tarfile.SYMTYPE, "../elsewhere")
    assert bundle.verifier is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.verifier)) as tar:
        assert tar.getnames() == ["tests", "tests/t.sh"]
    assert bundle.context is not None
    with tarfile.open(fileobj=io.BytesIO(bundle.context)) as tar:
        assert "Dockerfile" in tar.getnames()
    assert truth.prepare(bundle, seed=b"s").plants["k"].record()["content"] == "a"
