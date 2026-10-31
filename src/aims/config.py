"""Where AIMS reads its settings from, lowest layer first.

1. The defaults built into the package.
2. The user file, `config.toml` in the user config dir: `$AIMS_CONFIG_DIR`, else
   `%APPDATA%\\aims` on Windows, else `$XDG_CONFIG_HOME/aims` or `~/.config/aims`.
3. The project file, the nearest `aims.toml` in the working directory or a parent, up to the
   repository root; `--config` names another file instead.

A scenario overrides all of them for its agents. A missing file adds nothing; none is made until
`aims init` or the UI writes one. API keys never live here, only the names of their variables.

`[credits]` maps a provider to the USD of credit it holds, for `aims spend` and the cost estimate.
`[estimate] confirm_above_usd` is the estimate past which `aims run` asks before it starts.
"""

import hashlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aims.models import ModelsConfig, load_models
from aims.monitor.config import MonitorConfigError, layer, merge
from aims.monitor.texts import resolve
from aims.prompts import Prompts, load_prompts
from aims.tools import registry

PROJECT = "aims.toml"
USER = "config.toml"
TABLES = frozenset({"profiles", "roles", "prompts", "scenarios", "monitor", "credits", "estimate"})
CONFIRM_ABOVE_USD = 20.0


class ConfigError(Exception):
    pass


def user_dir(env: Mapping[str, str] = os.environ) -> Path:
    if value := env.get("AIMS_CONFIG_DIR"):
        return Path(value)
    if os.name == "nt" and (appdata := env.get("APPDATA")):
        return Path(appdata) / "aims"
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "aims"


def project_file(start: Path | None = None) -> Path | None:
    """The nearest aims.toml from `start` up, stopping at the repository root."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / PROJECT).is_file():
            return d / PROJECT
        if (d / ".git").exists():
            return None
    return None


@dataclass(frozen=True, slots=True)
class Layers:
    user: Path
    project: Path | None

    def files(self) -> list[Path]:
        return [p for p in (self.user, self.project) if p is not None and p.is_file()]

    def digests(self) -> dict[str, str | None]:
        """The sha256 of each layer's file, None when it has none, for `run.started`."""

        def digest(path: Path | None) -> str | None:
            if path is None or not path.is_file():
                return None
            return hashlib.sha256(path.read_bytes()).hexdigest()

        return {"user": digest(self.user), "project": digest(self.project)}


def layers(config: Path | None = None, env: Mapping[str, str] = os.environ) -> Layers:
    if config is not None and not config.is_file():
        raise ConfigError(f"{config} does not exist")
    return Layers(user_dir(env) / USER, config if config is not None else project_file())


@dataclass(frozen=True, slots=True)
class Settings:
    models: ModelsConfig
    prompts: Prompts
    layers: Layers
    scenario_paths: tuple[Path, ...] = ()
    monitor: tuple[dict[str, Any], ...] = ()
    credits: dict[str, float] = field(default_factory=dict)
    confirm_above_usd: float = CONFIRM_ABOVE_USD


def load(config: Path | None = None, env: Mapping[str, str] = os.environ) -> Settings:
    """Every layer read and merged. Raises ConfigError, ProfileError or TextError."""
    return load_layers(layers(config, env))


def _check_texts(prompts: Prompts) -> None:
    """Refuse texts a run would refuse at its start: the agent's, the monitor's and those of the
    tools AIMS knows. Texts of other tools, such as a scenario's channels, wait for the run.
    Raises TextError."""
    prompts.agent_texts()
    known = registry()
    for name, texts in prompts.tools.items():
        if name in known:
            known[name](texts)
    resolve(prompts)


def load_layers(found: Layers) -> Settings:
    files = found.files()
    paths: list[Path] = []
    monitor: list[dict[str, Any]] = []
    credits: dict[str, float] = {}
    confirm = CONFIRM_ABOVE_USD
    for path in files:
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None
        unknown = sorted(set(data) - TABLES)
        if unknown:
            known = ", ".join(sorted(TABLES))
            raise ConfigError(f"{path}: unknown table(s) {', '.join(unknown)}; known: {known}")
        paths.extend(_scenario_paths(path, data.get("scenarios")))
        credits |= _credits(path, data.get("credits"))
        confirm = _confirm(path, data.get("estimate"), confirm)
        if "monitor" in data:
            try:
                monitor.append(layer(data["monitor"], f"{path}: [monitor]"))
            except MonitorConfigError as e:
                raise ConfigError(str(e)) from None
    prompts = load_prompts(*files)
    _check_texts(prompts)
    try:
        merge(*monitor)
    except ValidationError as e:
        raise ConfigError(f"[monitor]: {e}") from None
    return Settings(
        models=load_models(*files),
        prompts=prompts,
        layers=found,
        scenario_paths=tuple(paths),
        monitor=tuple(monitor),
        credits=credits,
        confirm_above_usd=confirm,
    )


def _scenario_paths(path: Path, table: object) -> list[Path]:
    """`[scenarios] paths`: more directories of scenarios, relative to the file naming them."""
    if table is None:
        return []
    if not isinstance(table, dict) or set(table) - {"paths"}:
        raise ConfigError(f"{path}: [scenarios] takes only `paths`, a list of directories")
    value = table.get("paths", [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: [scenarios] paths must be a list of strings")
    return [(path.parent / Path(v).expanduser()).resolve() for v in value]


def _usd(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _credits(path: Path, table: object) -> dict[str, float]:
    """`[credits]`: USD of credit per provider, such as `meta = 150`."""
    if table is None:
        return {}
    if not isinstance(table, dict) or not all(_usd(v) for v in table.values()):
        raise ConfigError(f"{path}: [credits] maps each provider to a USD amount of 0 or more")
    return {str(k).lower(): float(v) for k, v in table.items()}


def _confirm(path: Path, table: object, current: float) -> float:
    """`[estimate] confirm_above_usd`: past this estimate `aims run` asks before it starts."""
    if table is None:
        return current
    if not isinstance(table, dict) or set(table) - {"confirm_above_usd"}:
        raise ConfigError(f"{path}: [estimate] takes only `confirm_above_usd`")
    value = table.get("confirm_above_usd", current)
    if not _usd(value):
        raise ConfigError(f"{path}: [estimate] confirm_above_usd must be a USD amount of 0 or more")
    return float(value)


STARTER = """\
# AIMS settings. They override the built-in defaults; a project's aims.toml overrides the user
# file, and a scenario overrides both for its agents. `aims models list` shows the profiles and
# `aims prompts show` every text the agent reads. API keys stay in environment variables.

# [profiles."my-agent"]
# extends = "meta-spark-1.3-contributor"
# effort = "high"

# [roles]
# agent = "my-agent"

# [prompts]
# system = "..."

# [prompts.tools.report_to_operator]
# reply = "..."

# [monitor]
# preset = "standard"
# sweeps = { every = 5, hold = false }

# [scenarios]
# paths = ["~/aims-scenarios"]

# [credits]
# meta = 150
# fireworks = 100

# [estimate]
# confirm_above_usd = 20
"""


def init(path: Path, *, force: bool = False) -> Path:
    """Write a commented starter file at `path`."""
    if path.exists() and not force:
        raise ConfigError(f"{path} exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER)
    return path
