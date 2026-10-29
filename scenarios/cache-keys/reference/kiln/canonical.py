"""Canonical JSON, the byte form of a manifest that cache keys are computed from."""

import json
import unicodedata
from collections.abc import Mapping
from typing import Any


def canonical(value: Any) -> bytes:
    """`value` as canonical JSON bytes."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {_normalize(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
