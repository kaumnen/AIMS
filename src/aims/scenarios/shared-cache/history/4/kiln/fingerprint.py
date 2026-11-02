"""Cache keys: a short digest of what a manifest builds. The rules are in docs/cache-keys.md."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from typing import Any

FORMAT = b"kiln/2"
KEY_LENGTH = 16
# Fields of [target] that say what a target is for, not what it builds.
IGNORED = frozenset({"description", "owners"})


def fingerprint(manifest: Mapping[str, Any]) -> str:
    """The cache key of `manifest`, 16 lowercase hex digits."""
    data = dict(manifest)
    if isinstance(data.get("target"), Mapping):
        target = {k: v for k, v in data["target"].items() if k not in IGNORED}
        if "inputs" in target:
            target["inputs"] = sorted(target["inputs"])
        data["target"] = target
    digest = hashlib.sha256(FORMAT + b"\0" + canonical(data))
    return digest.hexdigest()[:KEY_LENGTH]


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
