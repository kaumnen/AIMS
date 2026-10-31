"""The settings in use, every config layer merged, as TOML that names where each value comes
from. `aims config --merged` prints it and the UI shows it."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.items

from aims.config import Settings
from aims.models.profiles import _layer, builtin_tables
from aims.monitor.config import _merge, layer, merge

BUILTIN = "built in"
USER = "this machine"
PROJECT = "this project"

# A tree shaped like the settings whose leaves name a source; a key it lacks is built in.
type Labels = dict[str, Any]


def _labels(value: Mapping[str, Any], source: str) -> Labels:
    return {k: _labels(v, source) if isinstance(v, Mapping) else source for k, v in value.items()}


def _source(labels: Labels, path: tuple[str, ...]) -> str:
    node: Any = labels
    for part in path:
        if isinstance(node, str):
            return node
        if not isinstance(node, dict) or part not in node:
            return BUILTIN
        node = node[part]
    return node if isinstance(node, str) else BUILTIN


def _files(settings: Settings) -> list[tuple[str, dict[str, Any]]]:
    found = settings.layers
    return [
        (USER if path == found.user else PROJECT, tomllib.loads(path.read_text()))
        for path in found.files()
    ]


def _profiles(files: list[tuple[str, dict[str, Any]]]) -> tuple[Labels, dict[str, str | None]]:
    """Each profile's labels after `extends`, merged the way load_models merges values, and
    what each profile extends."""
    values: dict[str, dict[str, Any]] = {}
    labels: dict[str, Labels] = {}
    for source, data in [(BUILTIN, builtin_tables()), *files]:
        for name, table in data.get("profiles", {}).items():
            mine = _labels(table, source)
            values[name] = _layer(values[name], table) if name in values else dict(table)
            labels[name] = _layer(labels[name], mine) if name in labels else mine
    parents = {name: values[name].get("extends") for name in values}

    def resolve(name: str) -> Labels:
        own = {k: v for k, v in labels[name].items() if k != "extends"}
        parent = parents[name]
        return own if parent is None else _layer(resolve(parent), own)

    return {name: resolve(name) for name in labels}, parents


def _monitor(files: list[tuple[str, dict[str, Any]]], preset: str) -> Labels:
    out: Labels = {}
    for source, data in files:
        if "monitor" in data:
            out = _merge(out, _labels(layer(data["monitor"], "[monitor]"), source))
    if preset in ("off", "signals"):
        for key in ("sweeps", "channels", "judge"):
            out.pop(key, None)
    return out


def _value(value: Any, source: str) -> tomlkit.items.Item:
    item = tomlkit.item(value)
    item.comment(source)
    item.trivia.comment_ws = "  "
    return item


def _table(
    value: Mapping[str, Any], labels: Labels, path: tuple[str, ...], note: str | None = None
) -> tomlkit.items.Table:
    """`value` as a table: `note` as a comment, plain keys, then tables, then arrays of tables,
    each value followed by its source. Empty tables are left out."""
    out = tomlkit.table()
    if note:
        out.add(tomlkit.comment(note))

    def tables(v: object) -> bool:
        return isinstance(v, list) and len(v) > 0 and all(isinstance(x, dict) for x in v)

    plain = [k for k, v in value.items() if not isinstance(v, dict) and not tables(v)]
    for key in plain:
        out.add(key, _value(value[key], _source(labels, (*path, key))))
    for key, v in value.items():
        if isinstance(v, dict) and v:
            out.add(key, _table(v, labels, (*path, key)))
    for key, v in value.items():
        if tables(v):
            aot = tomlkit.aot()
            for x in v:
                aot.append(_table(x, labels, (*path, key)))
            out.add(key, aot)
    return out


def merged(settings: Settings) -> str:
    """The settings `settings` holds, every layer merged, with the source of each value: built
    in, this machine (the user file) or this project (aims.toml). Texts are left out."""
    files = _files(settings)
    found = settings.layers
    doc = tomlkit.document()
    for line in (
        "The settings in use: the built-in defaults, this machine's file over them, then this",
        "project's. The comment after each value names where it comes from. A scenario can",
        "still override them for its agents. Texts are under `aims prompts show`.",
        f"this machine: {_where(found.user)}",
        f"this project: {_where(found.project)}",
    ):
        doc.add(tomlkit.comment(line))

    roles: Labels = {}
    for source, data in [(BUILTIN, builtin_tables()), *files]:
        roles |= _labels(data.get("roles", {}), source)
    doc.add("roles", _table(settings.models.roles.model_dump(mode="json"), roles, ()))

    profile_labels, parents = _profiles(files)
    profiles = tomlkit.table(is_super_table=True)
    for name, profile in settings.models.profiles.items():
        value = profile.model_dump(mode="json", exclude_none=True, exclude={"name"})
        parent = parents.get(name)
        note = f"extends {parent}" if parent else None
        profiles.add(name, _table(value, profile_labels.get(name, {}), (), note))
    doc.add("profiles", profiles)

    monitor = merge(*settings.monitor)
    labels = _monitor(files, monitor.preset)
    doc.add("monitor", _table(monitor.model_dump(mode="json", exclude_none=True), labels, ()))

    credits: Labels = {}
    for source, data in files:
        credits |= {str(k).lower(): source for k in data.get("credits", {})}
    if settings.credits:
        doc.add("credits", _table(settings.credits, credits, ()))

    confirm = BUILTIN
    for source, data in files:
        if "confirm_above_usd" in data.get("estimate", {}):
            confirm = source
    estimate = tomlkit.table()
    estimate.add("confirm_above_usd", _value(settings.confirm_above_usd, confirm))
    doc.add("estimate", estimate)

    named = [s for s, data in files for _ in data.get("scenarios", {}).get("paths", [])]
    paths = tomlkit.array()
    paths.multiline(True)
    for path, source in zip(settings.scenario_paths, named, strict=True):
        paths.add_line(str(path), comment=source)
    if named:
        paths.add_line(indent="")
        scenarios = tomlkit.table()
        scenarios.add("paths", paths)
        doc.add("scenarios", scenarios)
    return tomlkit.dumps(doc)


def _where(path: Path | None) -> str:
    if path is None:
        return "none"
    return str(path) if path.is_file() else f"{path} (no file)"
