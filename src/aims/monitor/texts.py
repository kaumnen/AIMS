"""The monitor's texts, resolved: the defaults of the sweep, review and judge modules with the
`[prompts.monitor]` tables of the config files laid over them."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from aims.events import Category
from aims.monitor import judge_texts, review_texts, sweep_texts
from aims.monitor.config import CATEGORIES
from aims.prompts import Prompts, TextError, check_format


def defaults() -> dict[str, tuple[str, frozenset[str]]]:
    out: dict[str, tuple[str, frozenset[str]]] = {}
    for module in (sweep_texts, review_texts, judge_texts):
        for name, value in module.TEXTS.items():
            if name in out or name == "categories":
                raise RuntimeError(f"monitor text {name!r} is defined twice")
            out[name] = value
    return out


@dataclass(frozen=True, slots=True)
class MonitorTexts:
    texts: dict[str, str] = field(default_factory=dict)
    categories: dict[Category, str] = field(default_factory=dict)

    def __getitem__(self, name: str) -> str:
        return self.texts[name]

    def snapshot(self) -> dict[str, JsonValue]:
        categories: dict[str, JsonValue] = {str(k): v for k, v in self.categories.items()}
        return {"texts": dict(self.texts), "categories": categories}

    @classmethod
    def restore(cls, value: Mapping[str, JsonValue]) -> MonitorTexts:
        texts = value.get("texts")
        categories = value.get("categories")
        if not isinstance(texts, dict) or not isinstance(categories, dict):
            raise TextError("monitor texts snapshot is malformed")
        by_category: dict[Category, str] = {
            c: str(categories[c]) for c in CATEGORIES if c in categories
        }
        return cls({str(k): str(v) for k, v in texts.items()}, by_category)


def resolve(prompts: Prompts) -> MonitorTexts:
    """The monitor texts with `prompts.monitor` laid over the defaults. Raises TextError."""
    known = defaults()
    texts = {name: text for name, (text, _) in known.items()}
    categories: dict[Category, str] = dict(sweep_texts.CATEGORIES)
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
        if name not in known:
            names = ", ".join(sorted(known)) or "none"
            raise TextError(f"prompts.monitor: unknown text {name!r}; known: {names}")
        if not isinstance(value, str):
            raise TextError(f"prompts.monitor.{name} must be a string")
        check_format(value, known[name][1], f"prompts.monitor.{name}")
        texts[name] = value
    return MonitorTexts(texts, categories)
