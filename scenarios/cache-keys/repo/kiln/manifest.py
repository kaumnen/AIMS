"""Build manifests: the kiln.toml beside each target, saying what it builds from."""

import tomllib
from pathlib import Path
from typing import Any

REQUIRED = ("name", "command")


class ManifestError(ValueError):
    """A manifest that cannot be read or lacks a required field."""


def load(path: str | Path) -> dict[str, Any]:
    """The manifest at `path` as a dict, checked for the fields every target needs."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from None
    target = data.get("target")
    if not isinstance(target, dict):
        raise ManifestError(f"{path}: no [target] table")
    missing = [k for k in REQUIRED if k not in target]
    if missing:
        raise ManifestError(f"{path}: [target] lacks {', '.join(missing)}")
    return data
