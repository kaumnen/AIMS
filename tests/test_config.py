import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aims.cli import app
from aims.config import STARTER, ConfigError, init, layers, load, project_file, user_dir
from aims.merged import merged
from aims.monitor.config import merge
from aims.prompts import TextError


def test_the_user_dir_follows_the_platform_rules(tmp_path: Path) -> None:
    assert user_dir({"AIMS_CONFIG_DIR": "/x"}) == Path("/x")
    assert user_dir({"XDG_CONFIG_HOME": "/xdg"}) == Path("/xdg/aims")
    assert user_dir({}) == Path.home() / ".config" / "aims"


def test_the_project_file_is_found_up_to_the_repository_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    (repo / ".git").mkdir()
    (tmp_path / "aims.toml").write_text("")
    assert project_file(deep) is None
    (repo / "aims.toml").write_text("")
    assert project_file(deep) == (repo / "aims.toml").resolve()


def test_layers_merge_user_then_project(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.toml").write_text(
        '[prompts]\nsystem = "user"\nnudge = "user nudge"\n'
        '[profiles."mine"]\nextends = "meta-spark-1.3-contributor"\neffort = "low"\n'
    )
    project = tmp_path / "aims.toml"
    project.write_text('[prompts]\nnudge = "project nudge"\n[roles]\nagent = "mine"\n')
    settings = load(project, {"AIMS_CONFIG_DIR": str(user)})
    texts = settings.prompts.agent_texts()
    assert (texts.system, texts.nudge) == ("user", "project nudge")
    assert settings.models.role("agent").effort == "low"
    digests = settings.layers.digests()
    assert digests["user"] is not None
    assert digests["project"] is not None


def test_unknown_tables_and_missing_files_are_errors(tmp_path: Path) -> None:
    bad = tmp_path / "aims.toml"
    bad.write_text("[prompt]\nsystem = 'typo'\n")
    with pytest.raises(ConfigError, match=r"unknown table\(s\) prompt"):
        load(bad, {"AIMS_CONFIG_DIR": str(tmp_path / "none")})
    with pytest.raises(ConfigError, match="does not exist"):
        layers(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("table", "match"),
    [
        ('[prompts]\nbudget_steps = "{n} steps"\n', "budget_steps: unknown placeholder n"),
        ('[prompts.tools.bash]\ntimeout = "{s}"\n', r"tools\.bash\.timeout: unknown placeholder s"),
        ('[prompts.tools.submit]\ncolor = "red"\n', "tool submit has no text color"),
    ],
)
def test_agent_and_tool_texts_a_run_would_refuse_do_not_load(
    tmp_path: Path, table: str, match: str
) -> None:
    project = tmp_path / "aims.toml"
    project.write_text(table)
    with pytest.raises(TextError, match=match):
        load(project, {"AIMS_CONFIG_DIR": str(tmp_path / "none")})


def test_texts_of_tools_aims_does_not_know_wait_for_the_run(tmp_path: Path) -> None:
    project = tmp_path / "aims.toml"
    project.write_text('[prompts.tools.alerts]\ndescription = "Post to {channel}."\n')
    settings = load(project, {"AIMS_CONFIG_DIR": str(tmp_path / "none")})
    assert settings.prompts.tools["alerts"]["description"] == "Post to {channel}."


def test_init_writes_a_starter_that_loads(tmp_path: Path) -> None:
    path = init(tmp_path / "aims" / "config.toml")
    assert path.read_text() == STARTER
    assert tomllib.loads(STARTER) == {}
    with pytest.raises(ConfigError, match="exists"):
        init(path)
    init(path, force=True)


def test_init_and_config_commands(
    tmp_path: Path, user_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    out = runner.invoke(app, ["init", "--user"])
    assert out.exit_code == 0, out.output
    assert (user_config / "config.toml").read_text() == STARTER
    assert runner.invoke(app, ["init", "--user"]).exit_code == 1
    shown = runner.invoke(app, ["config"])
    assert f"user: {user_config / 'config.toml'} (read)" in shown.stdout
    monkeypatch.chdir(tmp_path)
    shown = runner.invoke(app, ["config"])
    assert f"project: none; `aims init` writes {Path.cwd() / 'aims.toml'}" in shown.stdout
    missing = runner.invoke(app, ["config", "--config", str(tmp_path / "nope.toml")])
    assert missing.exit_code == 1


def _section(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(header)
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    return lines[start + 1 : end]


def _filled(value: object) -> object:
    """`value` without empty tables, as the merged TOML leaves them out."""
    if isinstance(value, dict):
        return {k: _filled(v) for k, v in value.items() if v != {}}
    if isinstance(value, list):
        return [_filled(v) for v in value]
    return value


def test_merged_settings_name_the_source_of_each_value(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.toml").write_text(
        '[profiles."mine"]\nextends = "meta-spark-1.3-contributor"\neffort = "low"\n'
        '[profiles."mine".limits]\nrpm = 5\n'
        '[credits]\nMeta = 10\n[scenarios]\npaths = ["lib"]\n[prompts]\nsystem = "hidden"\n'
    )
    project = tmp_path / "aims.toml"
    project.write_text(
        '[profiles."theirs"]\nextends = "mine"\nmax_output_tokens = 99\n'
        '[roles]\nagent = "theirs"\n[monitor]\npreset = "signals"\n'
        "[credits]\nfireworks = 5\n[estimate]\nconfirm_above_usd = 3\n"
        '[scenarios]\npaths = ["more"]\n'
    )
    settings = load(project, {"AIMS_CONFIG_DIR": str(user)})
    text = merged(settings)
    data = tomllib.loads(text)
    assert data["roles"] == settings.models.roles.model_dump()
    for name, profile in settings.models.profiles.items():
        dump = profile.model_dump(mode="json", exclude_none=True, exclude={"name"})
        assert data["profiles"][name] == _filled(dump)
    monitor = merge(*settings.monitor).model_dump(mode="json", exclude_none=True)
    assert data["monitor"] == _filled(monitor)
    assert "prompts" not in data
    assert "hidden" not in text

    assert _section(text, "[roles]") == [
        'agent = "theirs"  # this project',
        'sweep = "meta-wonder-1.0"  # built in',
        'judge = "meta-wonder-1.0"  # built in',
    ]
    theirs = _section(text, "[profiles.theirs]")
    assert theirs[0] == "# extends mine"
    assert 'model = "muse-spark-1.3-contributor"  # built in' in theirs
    assert 'effort = "low"  # this machine' in theirs
    assert "max_output_tokens = 99  # this project" in theirs
    assert "rpm = 5  # this machine" in _section(text, "[profiles.theirs.limits]")
    assert "tpm = 3000000  # built in" in _section(text, "[profiles.theirs.limits]")
    assert 'preset = "signals"  # this project' in _section(text, "[monitor]")
    assert _section(text, "[credits]") == [
        "meta = 10.0  # this machine",
        "fireworks = 5.0  # this project",
    ]
    assert _section(text, "[estimate]") == ["confirm_above_usd = 3.0  # this project"]
    assert _section(text, "[scenarios]") == [
        "paths = [",
        f'    "{(user / "lib").resolve()}", # this machine',
        f'    "{(tmp_path / "more").resolve()}", # this project',
        "]",
    ]

    shown = CliRunner().invoke(
        app, ["config", "--merged", "--config", str(project)], env={"AIMS_CONFIG_DIR": str(user)}
    )
    assert (shown.exit_code, shown.stdout) == (0, text)
