import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aims.cli import app
from aims.run.runner import _reward
from aims.scenario import Scenario, ScenarioError, from_snapshot, load, parse

BASE = """\
version: 1
name: demo
oversight: observe
task:
  instruction: do it
  environment: {image: python:3.13-slim}
"""
WORD_COUNT = Path(__file__).parent.parent / "scenarios" / "word-count" / "scenario.yaml"


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
            "is outside /work, /tmp, /home/agent",
        ),
        ("agents:\n  - id: a\n  - id: a\n", "agent ids must be unique"),
        ("agents:\n  - id: Bad Name\n", "s.yaml:8: agents[0].id: Value error"),
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
