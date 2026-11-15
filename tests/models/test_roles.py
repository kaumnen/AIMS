"""A role may be unset. What needs an unset role stops before any container starts and names
the fix; what names its profiles itself runs without one."""

import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import cp as cp_module
import pytest
from cp import CP, SCENARIO, control_plane
from typer.testing import CliRunner

from aims.cli import app
from aims.models import ProfileError, load_models
from aims.models import profiles as profiles_module
from aims.sandbox.docker import DockerBackend

PLAIN = """\
[profiles.plain]
backend = "chat"
base_url = "http://plain.invalid/v1"
model = "m"
weights = "plain"
reasoning = "full"
structured_output = false
"""

WATCH = """\
[profiles.watch]
backend = "chat"
base_url = "http://watch.invalid/v1"
model = "w"
weights = "watch"
reasoning = "full"
"""

# every monitor model named, so the monitor reads no role
NAMED = """\
[monitor]
sweeps = [{ profile = "watch" }]
review = { profile = "watch" }
judge = { profile = "watch" }
channels = { profile = "watch" }
"""


@pytest.fixture(autouse=True)
def no_builtin_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = profiles_module.builtin_tables()
    del tables["roles"]
    monkeypatch.setattr(profiles_module, "builtin_tables", lambda: copy.deepcopy(tables))


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a container would start")

    monkeypatch.setattr(DockerBackend, "open", refuse)


def project(tmp_path: Path, config: str) -> tuple[Path, Path]:
    toml = tmp_path / "aims.toml"
    toml.write_text(config)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(SCENARIO)
    return toml, scenario


def invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def flat(text: str) -> str:
    return " ".join(text.split())


def test_a_role_may_be_unset_and_asking_for_it_names_the_fix(tmp_path: Path) -> None:
    toml, _ = project(tmp_path, PLAIN)
    models = load_models(toml)
    assert models.roles.unset() == ["agent", "sweep", "judge"]
    with pytest.raises(ProfileError, match=r"set agent under \[roles\] in aims.toml, or give"):
        models.role("agent")
    for role in ("sweep", "judge"):
        with pytest.raises(ProfileError, match=rf"set {role} under .*--monitor signals"):
            models.role(role)
    # only a role that is set must name a profile with structured output
    toml.write_text(PLAIN + '[roles]\nagent = "plain"\n')
    assert load_models(toml).role("agent").name == "plain"
    toml.write_text(PLAIN + '[roles]\nsweep = "plain"\n')
    with pytest.raises(ProfileError, match="role sweep needs structured output"):
        load_models(toml)


def test_a_run_that_needs_an_unset_role_stops_before_a_container(
    tmp_path: Path, no_docker: None
) -> None:
    toml, scenario = project(tmp_path, PLAIN + WATCH)
    runs = tmp_path / "runs"
    run = ["run", str(scenario), "--config", str(toml), "--no-server", "--runs-dir", str(runs)]
    agentless = invoke(*run)
    assert agentless.exit_code == 1
    assert "no profile has the agent role; set agent under [roles]" in flat(agentless.stderr)
    watchless = invoke(*run, "--profile", "plain")
    assert watchless.exit_code == 1
    assert "no profile has the sweep role" in flat(watchless.stderr)
    assert "--monitor signals" in flat(watchless.stderr)
    estimate = ["estimate", str(scenario), "--config", str(toml), "--runs-dir", str(runs)]
    assert "no profile has the agent role" in flat(invoke(*estimate).stderr)
    # the store's index opens, but no run starts
    assert [p.name for p in runs.iterdir() if p.is_dir()] == []


def test_a_run_that_names_its_profiles_needs_no_role(tmp_path: Path) -> None:
    toml, scenario = project(tmp_path, PLAIN + WATCH)
    runs = str(tmp_path / "runs")
    estimate = ["estimate", str(scenario), "--config", str(toml), "--runs-dir", runs]
    signals = invoke(*estimate, "--profile", "plain", "--monitor", "signals")
    assert signals.exit_code == 0, signals.output
    toml.write_text(PLAIN + WATCH + NAMED)
    named = invoke(*estimate, "--profile", "plain")
    assert named.exit_code == 0, named.output


def test_scenario_check_names_a_missing_agent_role_once(tmp_path: Path) -> None:
    toml, scenario = project(tmp_path, PLAIN)
    result = invoke("scenario", "check", str(scenario), "--config", str(toml), "--no-selftest")
    assert result.exit_code == 1
    assert flat(result.output).count("no profile has the agent role") == 1


def test_models_list_and_the_merged_config_show_unset_roles(tmp_path: Path) -> None:
    toml, _ = project(tmp_path, PLAIN + '[roles]\nagent = "plain"\n')
    listed = invoke("models", "list", "--config", str(toml))
    assert listed.exit_code == 0, listed.output
    lines = listed.stdout.splitlines()
    assert next(line for line in lines if line.startswith("plain ")).endswith("[agent]")
    assert lines[-1] == (
        "unset roles: sweep, judge; set them under [roles] in aims.toml, or add a model with"
        " aims models add openrouter <id> --write --role <role>"
    )
    merged = invoke("config", "--merged", "--config", str(toml))
    assert "[roles]\n# unset: sweep, judge\nagent = " in merged.stdout


ROLES = '[roles]\nagent = "scripted"\n'


@pytest.fixture
def roleless(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ROLES in cp_module.CONFIG
    monkeypatch.setattr(cp_module, "CONFIG", cp_module.CONFIG.replace(ROLES, ""))


@pytest.fixture
async def cp(tmp_path: Path, roleless: None) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.mark.anyio
async def test_the_api_shows_unset_roles_and_refuses_runs_that_need_them(cp: CP) -> None:
    ref = {"source": "project", "name": "control"}
    async with cp.client(cp.operator) as c:
        models = (await c.get("/api/models")).json()
        assert models["roles"] == {"agent": None, "sweep": None, "judge": None}
        assert all(p["roles"] == [] for p in models["profiles"])
        listed = (await c.get("/api/scenarios")).json()
        control = next(s for s in listed if s["name"] == "control")
        assert (control["profiles"], control["needs_profile"]) == ([], True)
        detail = (await c.get("/api/scenarios/project/control")).json()
        assert detail["agents"][0]["profile"] is None

        r = await c.post("/api/runs", json={"scenario": ref, "monitor": "signals"})
        assert (r.status_code, r.json()["code"]) == (422, "scenario")
        assert "no profile has the agent role" in r.json()["errors"][0]["msg"]
        r = await c.post("/api/runs", json={"scenario": ref, "profile": "scripted"})
        assert (r.status_code, r.json()["code"]) == (422, "monitor")
        assert "no profile has the sweep role" in r.json()["detail"]
        r = await c.post("/api/models/check", json={})
        assert (r.status_code, r.json()["code"]) == (422, "profile")
        assert "set agent under [roles]" in r.json()["detail"]
        # a run that names its profile and watches by signals needs no role: it passes every
        # check and waits only for a runner
        named = {"scenario": ref, "profile": "scripted", "monitor": "signals", "confirm": True}
        r = await c.post("/api/runs", json=named)
        assert (r.status_code, r.json()["code"]) == (409, "no_runner"), r.text

    cp.project.write_text(cp.project.read_text() + ROLES)
    async with cp.client(cp.viewer) as c:
        listed = (await c.get("/api/scenarios")).json()
        control = next(s for s in listed if s["name"] == "control")
        assert (control["profiles"], control["needs_profile"]) == (["scripted"], False)
