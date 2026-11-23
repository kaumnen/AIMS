"""The monitor's texts, resolved: the defaults of the sweep, review and judge modules with the
`[prompts.monitor]` tables of the config files laid over them. `categories` holds the category
definitions, `glossary` what each record type is."""

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from aims.events import Category
from aims.monitor import (
    channel_texts,
    glossary_texts,
    judge_texts,
    review_texts,
    score_texts,
    sweep_texts,
)
from aims.monitor.config import CATEGORIES
from aims.prompts import Prompts, TextError, check_format

MODULES = (sweep_texts, review_texts, judge_texts, channel_texts, score_texts, glossary_texts)
TABLES = frozenset({"categories", "glossary"})


def defaults() -> dict[str, tuple[str, frozenset[str]]]:
    return dict(_defaults())


@functools.cache
def _defaults() -> dict[str, tuple[str, frozenset[str]]]:
    out: dict[str, tuple[str, frozenset[str]]] = {}
    for module in MODULES:
        for name, value in module.TEXTS.items():
            if name in out or name in TABLES:
                raise RuntimeError(f"monitor text {name!r} is defined twice")
            out[name] = value
    return out


def added() -> frozenset[str]:
    """The texts added after monitor identities were first stamped."""
    return frozenset(name for module in MODULES for name in getattr(module, "ADDED", {}))


@dataclass(frozen=True, slots=True)
class MonitorTexts:
    """The monitor's texts by name, its category definitions and its glossary by record type.
    A name `texts` lacks reads its default, as does a type `glossary` lacks."""

    texts: dict[str, str] = field(default_factory=dict)
    categories: dict[Category, str] = field(default_factory=dict)
    glossary: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, name: str) -> str:
        text = self.texts.get(name)
        return _defaults()[name][0] if text is None else text

    def gloss(self, kind: str) -> str:
        """What record type `kind` is, empty when the glossary leaves it out."""
        text = self.glossary.get(kind)
        return glossary_texts.default(kind) if text is None else text

    def snapshot(self) -> dict[str, JsonValue]:
        categories: dict[str, JsonValue] = {str(k): v for k, v in self.categories.items()}
        glossary: dict[str, JsonValue] = dict(self.glossary)
        return {"texts": dict(self.texts), "categories": categories, "glossary": glossary}

    def hashed(self) -> dict[str, JsonValue]:
        """The snapshot a monitor's identity hashes: without the texts added since identities
        were first stamped that hold their defaults, so a monitor that sets none of them keeps
        the identity it had before they were added. The glossary counts the same way."""
        known = _defaults()
        new = added()
        texts: dict[str, JsonValue] = {
            k: v for k, v in self.texts.items() if k not in new or v != known[k][0]
        }
        out = {**self.snapshot(), "texts": texts}
        glossary: dict[str, JsonValue] = {
            k: v for k, v in self.glossary.items() if v != glossary_texts.default(k)
        }
        if glossary:
            out["glossary"] = glossary
        else:
            del out["glossary"]
        return out

    @classmethod
    def restore(cls, value: Mapping[str, JsonValue]) -> MonitorTexts:
        """Texts a run or a job stored. A text added since takes its default."""
        texts = value.get("texts")
        categories = value.get("categories")
        glossary = value.get("glossary") or {}
        if (
            not isinstance(texts, dict)
            or not isinstance(categories, dict)
            or not isinstance(glossary, dict)
        ):
            raise TextError("monitor texts snapshot is malformed")
        by_category: dict[Category, str] = {
            c: str(categories[c]) for c in CATEGORIES if c in categories
        }
        stored = {str(k): str(v) for k, v in texts.items()}
        glossed = {str(k): str(v) for k, v in glossary.items()}
        return cls(
            {name: text for name, (text, _) in _defaults().items()} | stored,
            by_category,
            glossary_texts.defaults() | glossed,
        )


def resolve(prompts: Prompts) -> MonitorTexts:
    """The monitor texts with `prompts.monitor` laid over the defaults. Raises TextError."""
    known = defaults()
    texts = {name: text for name, (text, _) in known.items()}
    categories: dict[Category, str] = dict(sweep_texts.CATEGORIES)
    glossary = glossary_texts.defaults()
    for name, value in prompts.monitor.items():
        if name == "categories":
            if not isinstance(value, dict):
                raise TextError("prompts.monitor.categories must be a table")
            unknown = sorted(set(value) - set(CATEGORIES))
            if unknown:
                raise TextError(f"prompts.monitor.categories: unknown category {unknown[0]!r}")
            for category in CATEGORIES:
                if category in value:
                    categories[category] = value[category]
            continue
        if name == "glossary":
            if not isinstance(value, dict):
                raise TextError("prompts.monitor.glossary must be a table")
            unknown = sorted(set(value) - set(glossary))
            if unknown:
                raise TextError(f"prompts.monitor.glossary: unknown record type {unknown[0]!r}")
            glossary |= value
            continue
        if name not in known:
            names = ", ".join(sorted(known)) or "none"
            raise TextError(f"prompts.monitor: unknown text {name!r}; known: {names}")
        if not isinstance(value, str):
            raise TextError(f"prompts.monitor.{name} must be a string")
        check_format(value, known[name][1], f"prompts.monitor.{name}")
        texts[name] = value
    return MonitorTexts(texts, categories, glossary)
