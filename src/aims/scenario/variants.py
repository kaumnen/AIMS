"""Named patches over a scenario mapping.

A variant is a mapping applied over the scenario it is declared in. Mappings merge, and a
`null` removes a key. Lists whose items carry an identity merge item by item: agents by `id`,
channels and plants by `name`, files by `path`; any other list replaces the base list whole.
"""

import copy
from collections.abc import Mapping, Sequence
from typing import Any

KEYS = ("id", "name", "path")


class VariantError(ValueError):
    """A named variant is missing or is not a mapping."""


def apply(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The scenario `raw` with the patch named `name` applied, as a fresh mapping."""
    variants = raw.get("variants")
    if not isinstance(variants, Mapping) or name not in variants:
        raise VariantError(f"no variant {name!r}")
    patch = variants[name]
    if not isinstance(patch, Mapping):
        raise VariantError(f"variant {name!r} is not a mapping")
    merged = _merge(copy.deepcopy(dict(raw)), patch)
    if not isinstance(merged, dict):
        raise VariantError(f"variant {name!r} does not leave a scenario mapping")
    return merged


def names(raw: Mapping[str, Any]) -> list[str]:
    """The variant names declared by `raw`, in order."""
    variants = raw.get("variants")
    return list(variants) if isinstance(variants, Mapping) else []


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, Mapping) and isinstance(patch, Mapping):
        out = copy.deepcopy(dict(base))
        for key, value in patch.items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = _merge(out.get(key), value)
        return out
    if isinstance(base, list) and isinstance(patch, list):
        key = _identity(patch)
        if key is not None:
            return _merge_list(base, patch, key)
        return copy.deepcopy(patch)
    return copy.deepcopy(patch)


def _identity(items: Sequence[Any]) -> str | None:
    if not items or not all(isinstance(item, Mapping) for item in items):
        return None
    for key in KEYS:
        if all(key in item for item in items):
            return key
    return None


def _merge_list(base: Sequence[Any], patch: Sequence[Any], key: str) -> list[Any]:
    out = copy.deepcopy(list(base))
    where: dict[Any, int] = {}
    for index, item in enumerate(out):
        if isinstance(item, Mapping) and key in item:
            where[item[key]] = index
    for item in patch:
        if not isinstance(item, Mapping) or key not in item:
            out.append(copy.deepcopy(item))
            continue
        at = where.get(item[key])
        if at is None:
            where[item[key]] = len(out)
            out.append(copy.deepcopy(dict(item)))
        else:
            out[at] = _merge(out[at], item)
    return out
