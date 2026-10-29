"""Canary values: the random secrets plants hand out, made fresh for each run."""

import hashlib
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass

from aims.scenario import pattern
from aims.scenario.model import CanaryShape, CanarySpec

# Neutral shapes. A scenario that wants a provider's format writes its own patterns.
BUILTIN: dict[str, CanarySpec] = {
    "hex": CanarySpec(parts={"value": "[0-9a-f]{32}"}),
    "token": CanarySpec(parts={"value": "[A-Za-z0-9_-]{32}"}),
    "password": CanarySpec(parts={"value": "[A-Za-z0-9!@#$%^&*_-]{24}"}),
    "uuid": CanarySpec(
        parts={"value": "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"}
    ),
    "credentials": CanarySpec(
        parts={
            "id": "[A-Z0-9]{20}",
            "secret": "[A-Za-z0-9/+]{40}",
            "token": "[A-Za-z0-9/+=]{64}",
        }
    ),
}

MIN_RANDOM = 8

_CANARY = re.compile(r"\{\{canary(?:\.([A-Za-z0-9_.-]+))?\}\}")
_CANARIES = re.compile(r"\{\{canaries\.(.+?)\}\}")


@dataclass(frozen=True, slots=True)
class Canary:
    """The values one plant's canary took in one run, by part name."""

    plant: str
    parts: dict[str, str]

    @property
    def value(self) -> str:
        """What `{{canary}}` stands for: the part named `value`, else the first part."""
        return self.parts.get("value") or next(iter(self.parts.values()))


def check(spec: CanarySpec) -> None:
    """Raise ValueError when a part's pattern is not one `make` can fill."""
    if isinstance(spec, str):
        if spec not in BUILTIN:
            known = ", ".join(sorted(BUILTIN))
            raise ValueError(f"unknown canary shape {spec!r}; known: {known}")
        return
    for name, value in spec.parts.items():
        if not value:
            raise ValueError(f"canary part {name!r} is an empty pattern")
        try:
            drawn = pattern.random_chars(value)
        except pattern.PatternError as e:
            raise ValueError(f"canary part {name!r}: {e}") from None
        if drawn < MIN_RANDOM:
            raise ValueError(
                f"canary part {name!r} draws {drawn} random characters; at least {MIN_RANDOM}"
                " are needed so ordinary text does not match it by chance"
            )


def make(spec: CanaryShape | CanarySpec, seed: bytes, plant: str) -> Canary:
    """The canary of plant `plant` for the run whose canary seed is `seed`."""
    if isinstance(spec, str):
        try:
            chosen = BUILTIN[spec]
        except KeyError:
            known = ", ".join(sorted(BUILTIN))
            raise ValueError(f"unknown canary shape {spec!r}; known: {known}") from None
    else:
        chosen = spec
    parts: dict[str, str] = {}
    for name, value in chosen.parts.items():
        rng = _rng(seed, plant, name)
        parts[name] = pattern.fill(value, rng)
    return Canary(plant=plant, parts=parts)


def render(text: str, canaries: Mapping[str, Canary], plant: str | None = None) -> str:
    """`text` with `{{canary}}`, `{{canary.part}}` and `{{canaries.NAME}}` filled in.

    `{{canary}}` means the canary of `plant`; `{{canaries.NAME}}` names another plant's.
    """

    def own(match: re.Match[str]) -> str:
        if plant is None or plant not in canaries:
            raise ValueError(f"{match.group(0)} has no plant canary to stand for")
        return _part(canaries[plant], match.group(1))

    def other(match: re.Match[str]) -> str:
        expr = match.group(1)
        for name in sorted(canaries, key=len, reverse=True):
            if expr == name:
                return _part(canaries[name], None)
            if expr.startswith(f"{name}."):
                return _part(canaries[name], expr[len(name) + 1 :])
        raise ValueError(f"no canary for {expr!r}")

    return _CANARIES.sub(other, _CANARY.sub(own, text))


def _part(canary: Canary, name: str | None) -> str:
    if name is None:
        return canary.value
    try:
        return canary.parts[name]
    except KeyError:
        known = ", ".join(canary.parts)
        raise ValueError(
            f"canary of {canary.plant!r} has no part {name!r}; known: {known}"
        ) from None


def _rng(seed: bytes, plant: str, part: str) -> random.Random:
    data = seed + b"\0" + plant.encode() + b"\0" + part.encode()
    return random.Random(int.from_bytes(hashlib.sha256(data).digest(), "big"))
