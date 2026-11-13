import hashlib
import io
import json
import os
import re
import tarfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from scripted import models, profile
from typer.testing import CliRunner

from aims.channels import ChannelSetup
from aims.channels.folder import Folder, _Seed
from aims.cli import app
from aims.events import ExecFinished, TaskScored
from aims.events.chain import Head, seal
from aims.monitor.config import JUDGE_MIN_OUTPUT, MAX_HOLD_S, MonitorConfig, merge
from aims.run import selftest, truth
from aims.run.runner import METRICS_BYTES, REWARD_BYTES, Bench, Options, Runner, _reward
from aims.sandbox import AuditedSandbox
from aims.sandbox.audit import ExecResult
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
from aims.scenario.load import _add, _Tar, read_regular

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
        (
            "  verifier: {command: x, programs: [main.py, '']}\n",
            "s.yaml:7: task.verifier.programs[1]: Value error, '' is not a sandbox path",
        ),
        ("  verifier: {command: x, programs: [../x.py]}\n", "'../x.py' is not a sandbox path"),
    ],
)
def test_errors_name_the_line(extra: str, message: str) -> None:
    with pytest.raises(ScenarioError) as e:
        parse(BASE + extra, source="s.yaml")
    assert any(message in p for p in e.value.problems), e.value.problems


def test_the_programs_of_a_verifier_join_the_workdir_per_agent() -> None:
    s = parse(
        BASE
        + "  verifier: {command: x, programs: [main.py, ./bin/*, /opt/app/run]}\n"
        + "sandbox: {workdir: /src}\n"
        + "agents:\n  - id: a\n"
        + "  - id: b\n    task: {verifier: {command: y, programs: [b.py]}}\n"
        + "  - id: c\n    task: {verifier: {command: y}}\n"
        + "  - id: d\n    task: {verifier: null}\n"
    )
    assert [s.programs_of(a) for a in "abcd"] == [
        ["/src/main.py", "/src/bin/*", "/opt/app/run"],
        ["/src/b.py"],
        [],
        [],
    ]
    assert parse(BASE + "  verifier: {command: x}\n").programs_of("agent") == []


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


LONE = "holds a lone surrogate, an escape such as \\ud800 without its pair"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (BASE.replace("do it", '"do \\ud800 it"'), f"s.yaml:5: task.instruction: {LONE}"),
        (
            BASE + 'agents:\n  - id: a\n    prompts: {system: "be \\ud800"}\n',
            f"s.yaml:9: agents[0].prompts.system: {LONE}",
        ),
        (
            BASE + 'agents:\n  - id: a\n  - id: b\n    role: "\\udfff"\n',
            f"s.yaml:10: agents[1].role: {LONE}",
        ),
        (BASE + 'variants:\n  "v\\ud800": {}\n', f"s.yaml:8: variants.v\\ud800: {LONE}"),
        (
            BASE + 'variants:\n  v:\n    task: {instruction: "\\uDC00"}\n',
            f"s.yaml:9: variants.v.task.instruction: {LONE}",
        ),
    ],
)
def test_a_lone_surrogate_escape_is_refused_with_its_line(text: str, problem: str) -> None:
    with pytest.raises(ScenarioError) as e:
        parse(text, source="s.yaml")
    assert e.value.problems == [problem]


def test_an_escape_of_a_whole_character_or_a_quoted_backslash_still_loads() -> None:
    whole = parse(BASE.replace("do it", '"do \\U0001F600 it"'))
    assert whole.task.instruction == "do \U0001f600 it"
    quoted = parse(BASE.replace("do it", "'do \\ud800 it'"))
    assert quoted.task.instruction == "do \\ud800 it"


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


@pytest.mark.parametrize("cap", [256, JUDGE_MIN_OUTPUT - 1])
def test_a_recorded_judge_output_cap_below_the_least_loads_at_the_least(cap: int) -> None:
    text = BASE + f"monitor: {{judge: {{max_output_tokens: {cap}}}}}\n"
    with pytest.raises(ScenarioError, match=r"judge\.max_output_tokens: Input should be greater"):
        parse(text)
    # A run recorded it before the least was raised.
    bundle = Bundle(parse(BASE + "monitor: {judge: {}}\n"), text.encode())
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    restored = from_snapshot(json.loads(json.dumps(bundle.snapshot())), blobs)
    assert restored.digest == bundle.digest
    assert restored.scenario.monitor is not None
    assert restored.scenario.monitor.judge is not None
    assert restored.scenario.monitor.judge.max_output_tokens == JUDGE_MIN_OUTPUT
    assert restored.scenario.monitor.model_fields_set == {"judge"}


@pytest.mark.parametrize("hold", [MAX_HOLD_S + 1, 60 * 86_400, 1e12])
def test_a_recorded_hold_past_thirty_days_loads_at_thirty_days(hold: float) -> None:
    text = BASE + f"monitor: {{policy: {{pause_timeout_s: {hold}, human_timeout_s: {hold}}}}}\n"
    with pytest.raises(ScenarioError, match=r"policy\.pause_timeout_s: Input should be less"):
        parse(text)
    # A run recorded it before a hold was bounded.
    bundle = Bundle(parse(BASE), text.encode())
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    restored = from_snapshot(json.loads(json.dumps(bundle.snapshot())), blobs)
    assert restored.digest == bundle.digest
    assert restored.scenario.monitor is not None
    policy = restored.scenario.monitor.policy
    assert policy.pause_timeout_s == policy.human_timeout_s == MAX_HOLD_S
    assert restored.scenario.monitor.model_fields_set == {"policy"}


UNKNOWN = (
    BASE.replace("name: demo\n", "name: demo\ndescription: A field since removed.\n").replace(
        "  instruction: do it\n", "  instruction: do it\n  notes: gone\n"
    )
    + "agents:\n  - id: agent\n    gone: 1\n    prompts: {system: Hi., gone: 2}\n"
    + "    overrides: {effort: low, gone: 3}\n"
    + "monitor: {judge: {rollouts: 2, gone: 4}, policy: {gone: 5}}\n"
)


def test_a_recorded_scenario_drops_keys_no_field_takes_any_more() -> None:
    with pytest.raises(ScenarioError) as e:
        parse(UNKNOWN)
    assert len(e.value.problems) == 7
    assert all(p.endswith("Extra inputs are not permitted") for p in e.value.problems)
    # A run recorded it while those fields were known.
    bundle = Bundle(parse(BASE), UNKNOWN.encode())
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    restored = from_snapshot(json.loads(json.dumps(bundle.snapshot())), blobs)
    assert restored.digest == bundle.digest
    s = restored.scenario
    assert "description" not in s.model_dump()
    (agent,) = s.agents
    assert agent.prompts is not None
    assert agent.prompts.system == "Hi."
    assert agent.overrides is not None
    assert agent.overrides.effort == "low"
    assert s.monitor is not None
    assert s.monitor.judge is not None
    assert s.monitor.judge.rollouts == 2
    assert s.monitor.model_fields_set == {"judge", "policy"}


NUMBERS = {
    "limits.cost_usd": "limits: {{cost_usd: {}}}\n",
    "limits.wall_s": "limits: {{wall_s: {}}}\n",
    "limits.monitor_cost_usd": "limits: {{monitor_cost_usd: {}}}\n",
    "agents[0].budget.cost_usd": "agents:\n  - budget: {{cost_usd: {}}}\n",
    "agents[0].budget.wall_s": "agents:\n  - budget: {{wall_s: {}}}\n",
    "agents[0].start.delay_s": "agents:\n  - start: {{delay_s: {}}}\n",
    "sandbox.cpus": "sandbox: {{cpus: {}}}\n",
    "plants[0].impossible.reward": (
        "plants: [{{name: p, kind: impossible, reference: r, reward: {}}}]\n"
    ),
    "monitor.judge.verdict_margin_s": "monitor: {{judge: {{verdict_margin_s: {}}}}}\n",
}


@pytest.mark.parametrize("where", NUMBERS)
@pytest.mark.parametrize("value", [".inf", "'inf'", "1e400"])
def test_a_number_in_a_scenario_is_finite(where: str, value: str) -> None:
    with pytest.raises(ScenarioError) as e:
        parse(BASE + NUMBERS[where].format(value))
    assert f": {where}: Input should be a finite number" in str(e.value)


def test_a_reward_is_a_number() -> None:
    reward = NUMBERS["plants[0].impossible.reward"]
    with pytest.raises(ScenarioError, match=r"reward: Input should be a finite number"):
        parse(BASE + reward.format(".nan"))
    assert getattr(parse(BASE + reward.format("0.5")).plants[0], "reward", None) == 0.5


TIMEOUTS = {
    "task.setup_timeout_s": "  setup_timeout_s: {}\n",
    "task.verifier.timeout_s": "  verifier: {{command: 'true', timeout_s: {}}}\n",
    "sandbox.command_timeout_s": (
        "sandbox: {{command_timeout_s: {}, max_command_timeout_s: 86400}}\n"
    ),
    "sandbox.max_command_timeout_s": "sandbox: {{max_command_timeout_s: {}}}\n",
    "agents[1].script[0].timeout_s": (
        "agents:\n  - id: agent\n  - id: helper\n    role: confederate\n"
        "    script: [{{run: 'true', timeout_s: {}}}]\n"
    ),
    "monitor.judge.timeout_s": "monitor: {{judge: {{timeout_s: {}}}}}\n",
}


def _timeout(scenario: Scenario, where: str) -> float | None:
    if where.startswith("monitor"):
        assert scenario.monitor is not None
        assert scenario.monitor.judge is not None
        return scenario.monitor.judge.timeout_s
    if where.startswith("agents"):
        script = scenario.agent("helper").script
        assert script is not None
        return script[0].timeout_s
    if where == "task.verifier.timeout_s":
        assert scenario.task.verifier is not None
        return scenario.task.verifier.timeout_s
    if where == "task.setup_timeout_s":
        return scenario.task.setup_timeout_s
    return getattr(scenario.sandbox, where.split(".")[1])


@pytest.mark.parametrize("where", TIMEOUTS)
@pytest.mark.parametrize("value", [".inf", ".nan", "-.inf", "'inf'", "1.8e305", "1e13", "86401"])
def test_a_timeout_past_a_day_or_not_finite_is_refused(where: str, value: str) -> None:
    with pytest.raises(ScenarioError) as e:
        parse(BASE + TIMEOUTS[where].format(value))
    assert f": {where}: Input should be" in str(e.value)


@pytest.mark.parametrize("where", TIMEOUTS)
def test_a_timeout_of_a_day_is_taken(where: str) -> None:
    assert _timeout(parse(BASE + TIMEOUTS[where].format(86_400)), where) == 86_400


@pytest.mark.parametrize("where", TIMEOUTS)
@pytest.mark.parametrize("value", ["86401", "1e13", ".inf"])
def test_a_recorded_timeout_past_a_day_loads_at_a_day(where: str, value: str) -> None:
    text = BASE + TIMEOUTS[where].format(value)
    # A run or a job recorded it before a timeout was bounded.
    bundle = Bundle(parse(BASE), text.encode())
    blobs = {hashlib.sha256(b).hexdigest(): b for b in bundle.blobs()}
    restored = from_snapshot(json.loads(json.dumps(bundle.snapshot())), blobs)
    assert restored.digest == bundle.digest
    assert _timeout(restored.scenario, where) == 86_400


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


def scored_event(reward: Any, metrics: Any, source: Any, error: Any) -> dict[str, Any]:
    """The `task.scored` event the reward makes, sealed as the log seals it: ChainError when it
    is over the event size limit."""
    data = TaskScored(reward=reward, metrics=metrics, source=source, error=error)
    head = Head(seq=0, hash="0" * 64)
    dump = data.model_dump(mode="json")
    event, _, _ = seal(head, run_id="r", ts="t", agent_id="agent", type="task.scored", data=dump)
    return event


def test_reward_json_too_large_for_an_event_keeps_the_reward_and_its_top_level_numbers() -> None:
    value = {"reward": 0.5, "log": "x" * 300_000, "passed": 3, "total": 4, "cases": [{"a": 1}]}
    body = json.dumps(value).encode()
    reward, metrics, source, error = _reward(b"J", body)
    scored_event(reward, metrics, source, error)
    assert (reward, source) == (0.5, "reward.json")
    assert metrics == {"reward": 0.5, "passed": 3, "total": 4}
    size = len(rfc8785.dumps(value))
    assert error == (
        f"reward.json is {size} bytes as JSON, over the {METRICS_BYTES} a score keeps;"
        " only its top-level numbers were kept"
    )


def test_reward_json_of_many_numbers_keeps_reward_passed_and_total_first() -> None:
    value: dict[str, Any] = {f"test_{i:05d}": i % 2 for i in range(30_000)}
    value |= {"passed": 15_000, "total": 30_000, "reward": 0.5}
    reward, metrics, source, error = _reward(b"J", json.dumps(value).encode())
    scored_event(reward, metrics, source, error)
    assert reward == 0.5
    assert {"reward": 0.5, "passed": 15_000, "total": 30_000}.items() <= metrics.items()
    assert METRICS_BYTES - 32 < len(rfc8785.dumps(metrics)) <= METRICS_BYTES
    assert (error or "").endswith("only its top-level numbers were kept")
    assert _reward(b"J", json.dumps(dict(list(value.items())[:100])).encode())[3] is None


class Verifier:
    """A sandbox for the verifier: every exec exits 0, and the read of the reward file gives
    `data` as `cat` or `head -c N` would. It keeps the first `output_limit` bytes of an exec's
    output, or of its own `limit`, as the sandbox does."""

    def __init__(self, data: bytes, output_limit: int = 4 * 1024 * 1024) -> None:
        self.data = data
        self.output_limit = output_limit
        self.reads: list[str] = []

    async def exec(self, command: str, **kw: Any) -> ExecResult:
        out = b""
        if "reward.json" in command:
            self.reads.append(command)
            n = re.search(r"head -c (\d+) '?[^ ]*reward\.json", command)
            out = b"J\n" + (self.data if n is None else self.data[: int(n[1])])
        limit = kw.get("limit") or self.output_limit
        finished = ExecFinished(
            exec_id="e1",
            call_id=None,
            end="exited",
            exit_code=0,
            pid=1,
            duration_ms=1,
            stdout="",
            stderr="",
            stdout_bytes=len(out),
            stderr_bytes=0,
            truncated=len(out) > limit,
        )
        return ExecResult(finished, out[:limit], b"")


async def verify(sandbox: Verifier) -> TaskScored:
    bench = Bench(cast(Any, None), cast(Any, None), models(profile()))
    runner = Runner(bench, load(WORD_COUNT), Options())
    return await runner._verify(cast(AuditedSandbox, sandbox), "agent", "submitted", "done")


@pytest.mark.anyio
async def test_a_reward_file_past_the_read_cap_is_not_read_whole() -> None:
    big = json.dumps({"reward": 1, "log": "x" * (2 * REWARD_BYTES)}).encode()
    sandbox = Verifier(big)
    scored = await verify(sandbox)
    assert (scored.reward, scored.metrics, scored.source) == (None, {}, "reward.json")
    assert scored.error == f"reward.json is over {REWARD_BYTES} bytes, too large to read"
    assert f"head -c {REWARD_BYTES + 1} " in sandbox.reads[0]
    scored_event(scored.reward, scored.metrics, scored.source, scored.error)

    # A scenario's small output limit does not cut the reward file short
    small = await verify(Verifier(json.dumps({"reward": 1, "log": "x" * 5000}).encode(), 1000))
    assert (small.reward, small.metrics["log"], small.error) == (1.0, "x" * 5000, None)
    fits = await verify(Verifier(json.dumps({"reward": 1, "log": "x" * 100_000}).encode()))
    assert (fits.reward, fits.metrics) == (1.0, {"reward": 1})


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


def unblocked[T](pipe: Path, call: Callable[[], T]) -> T:
    """What `call` returns, run in a thread: the test fails when it waits on `pipe`."""
    out: list[T] = []
    failed: list[BaseException] = []

    def go() -> None:
        try:
            out.append(call())
        except BaseException as e:
            failed.append(e)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        # A reader blocks in open() until a writer comes; open and close one so it ends.
        os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(5)
        pytest.fail("the named pipe was opened and waited on")
    if failed:
        raise failed[0]
    return out[0]


def test_read_regular_reads_files_only_and_never_waits_on_a_pipe(tmp_path: Path) -> None:
    file, pipe, link = tmp_path / "f", tmp_path / "p", tmp_path / "l"
    file.write_bytes(b"data")
    os.mkfifo(pipe)
    link.symlink_to("f")
    (tmp_path / "piped").symlink_to("p")
    assert read_regular(file) == b"data"
    assert read_regular(link, follow=True) == b"data"
    with pytest.raises(OSError, match="symbolic links"):
        read_regular(link)
    assert unblocked(pipe, lambda: read_regular(pipe)) is None
    assert unblocked(pipe, lambda: read_regular(tmp_path / "piped", follow=True)) is None
    assert read_regular(tmp_path) is None
    with pytest.raises(FileNotFoundError):
        read_regular(tmp_path / "none")


def test_a_source_swapped_for_a_pipe_after_its_lstat_is_skipped_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file, pipe = tmp_path / "f", tmp_path / "p"
    file.write_bytes(b"data")
    os.mkfifo(pipe)
    was = os.lstat(file)
    lstat = os.lstat
    # The file was regular when it was looked at and is a pipe when it is opened.
    monkeypatch.setattr(os, "lstat", lambda path, **kw: was if path == pipe else lstat(path, **kw))

    def loader() -> list[str]:
        tar = _Tar()
        _add(tar, pipe, "p", None)
        _add(tar, file, "f", None)
        with tarfile.open(fileobj=io.BytesIO(tar.finish())) as made:
            return made.getnames()

    def channel() -> list[str]:
        seed = _Seed(0)
        seed.path(pipe, "p", None)
        seed.path(file, "f", None)
        with tarfile.open(fileobj=io.BytesIO(seed.finish())) as made:
            return made.getnames()

    assert unblocked(pipe, loader) == ["f"]
    assert unblocked(pipe, channel) == ["f"]
    assert unblocked(pipe, lambda: truth._disk_entry(pipe, "p", was)) is None
    entry = truth._disk_entry(file, "f", was)
    assert entry is not None
    assert entry.data == b"data"


def test_a_selftest_script_that_is_a_named_pipe_is_refused_not_read(tmp_path: Path) -> None:
    (tmp_path / "selftest").mkdir()
    pipe = tmp_path / "selftest" / "ref.sh"
    os.mkfifo(pipe)
    path = tmp_path / "scenario.yaml"
    path.write_text(BASE + "selftest: {reference: selftest/ref.sh}\n")
    bundle = load(path)
    with pytest.raises(selftest.SelfTestError) as e:
        unblocked(pipe, lambda: selftest.cases(bundle))
    assert e.value.problems == ["selftest: selftest/ref.sh is not a regular file"]
