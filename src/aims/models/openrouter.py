"""A profile for a model on OpenRouter, filled in from OpenRouter's public list of models.

The list is outside text. Only values of a checked shape reach the profile and its notes: the
ids, numbers, dates and the effort levels AIMS knows.
"""

import asyncio
import difflib
import json
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, get_args

import httpx2
import tomlkit

from aims.models.profiles import Effort, Profile
from aims.net import Outbound

BASE_URL = "https://openrouter.ai/api/v1"
KEY_ENV = "OPENROUTER_API_KEY"
CHAT = "/api/v1/chat/completions"
# A starting value to tune, as in the built-in profiles.
OUTPUT_TOKENS = 16384
# The list is under 1 MB today.
MAX_BYTES = 32 * 1024 * 1024
FETCH_SECONDS = 60.0
# OpenRouter lets a cache hold the list this long.
FRESH_SECONDS = 120.0
MAX_NAME = 100

_ID = re.compile(r"~?[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?::[a-z0-9-]+)?")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EFFORTS: tuple[str, ...] = get_args(Effort)
_MILLION = Decimal(1_000_000)

Refusal = Literal["slug", "name", "missing", "refused", "unreachable"]


class CatalogError(Exception):
    """Why no profile came out. `kind` is `unreachable` when OpenRouter did not answer with
    its list, `missing` when the list has no such model, else what is wrong with the ask."""

    def __init__(self, kind: Refusal, message: str) -> None:
        super().__init__(message)
        self.kind: Refusal = kind


@dataclass(frozen=True, slots=True)
class Draft:
    """A profile ready to add to a config file. `notes` say what its reader should know."""

    name: str
    slug: str
    table: dict[str, Any]
    efforts: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def profile(self) -> Profile:
        return Profile.model_validate({**self.table, "name": self.name})

    def toml(self) -> str:
        """The `[profiles."name"]` table as config file text."""
        table = tomlkit.table()
        for key, value in self.table.items():
            if isinstance(value, dict):
                inline = tomlkit.inline_table()
                inline.update(value)
                table.add(key, inline)
            else:
                table.add(key, value)
            if key == "effort" and self.efforts:
                table.add(tomlkit.comment(f"OpenRouter lists: {', '.join(self.efforts)}"))
        profiles = tomlkit.table(is_super_table=True)
        profiles.add(self.name, table)
        doc = tomlkit.document()
        doc.add("profiles", profiles)
        return tomlkit.dumps(doc)


def default_name(slug: str) -> str:
    """`openrouter-` and the model's name without its author."""
    return "openrouter-" + slug.split("/", 1)[1].replace(":", "-")


def _slug(value: str) -> str:
    slug = value.strip().lower()
    if not _ID.fullmatch(slug):
        raise CatalogError(
            "slug", f"{value!r} is not an OpenRouter model id such as author/model-name"
        )
    return slug


def _per_million(value: object) -> float | None:
    """A price per token, which OpenRouter writes as a string, in USD per million tokens. None
    when it is absent, not a number or below zero, as the -1 of a model that routes to others."""
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value) * _MILLION
    except InvalidOperation:
        return None
    if not price.is_finite() or price < 0:
        return None
    return float(price)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) and value >= 1 else None


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def draft(
    models: list[dict[str, Any]], slug: str, *, name: str | None = None, alias: bool = False
) -> Draft:
    """The profile for model `slug` of OpenRouter's list `models`. An alias that OpenRouter
    moves to newer models, such as `~author/model-latest`, is refused unless `alias`. Raises
    CatalogError."""
    slug = _slug(slug)
    if name is None:
        name = default_name(slug)
    if not _NAME.fullmatch(name) or len(name) > MAX_NAME:
        raise CatalogError(
            "name",
            f"profile name {name!r}: use letters, digits, `.`, `_` and `-`, at most {MAX_NAME}",
        )
    by_id = {m["id"]: m for m in models if isinstance(m.get("id"), str)}
    model = by_id.get(slug)
    if model is None:
        near = difflib.get_close_matches(slug, [i for i in by_id if _ID.fullmatch(i)], n=3)
        hint = f"; close: {', '.join(near)}" if near else ""
        raise CatalogError("missing", f"OpenRouter lists no model {slug}{hint}")
    notes: list[str] = []

    target = _dict(model.get("alias_target")).get("slug")
    if slug.startswith("~") or target is not None:
        points = f", which now points at {target}" if _is_id(target) else ""
        if not alias:
            raise CatalogError(
                "refused",
                f"{slug} is an alias{points}. OpenRouter moves it to newer models, so a run"
                " would not say which model it used. Name the model, or pass --alias to use"
                " the alias",
            )
        notes.append(f"{slug} is an alias{points}. OpenRouter moves it to newer models.")
    if CHAT not in _strings(model.get("endpoints")):
        raise CatalogError("refused", f"OpenRouter does not serve {slug} on chat completions")

    aimed = by_id.get(target, model) if isinstance(target, str) else model
    weights = aimed.get("canonical_slug")
    if not _is_id(weights):
        weights = target if _is_id(target) else slug

    supported = set(_strings(model.get("supported_parameters")))
    reasoning = model.get("reasoning")
    efforts: tuple[str, ...] = ()
    effort = None
    kind = "none"
    if isinstance(reasoning, dict):
        listed = _strings(reasoning.get("supported_efforts"))
        efforts = tuple(e for e in listed if e in _EFFORTS)
        default = reasoning.get("default_effort")
        effort = default if default in efforts else None
        on = (
            reasoning.get("mandatory") is True
            or effort not in (None, "none")
            or (effort is None and reasoning.get("default_enabled") is True)
        )
        if on:
            kind = "full"
            notes.append(
                'reasoning = "full" is a guess. `aims models check` warns if the model sends'
                " a summary."
            )
        else:
            notes.append(
                "OpenRouter does not say that it reasons by default, so reasoning is"
                ' "none". To turn it on set effort and reasoning.'
            )

    table: dict[str, Any] = {
        "backend": "chat",
        "base_url": BASE_URL,
        "model": slug,
        "weights": weights,
        "api_key_env": KEY_ENV,
        "reasoning": kind,
    }
    if effort is not None:
        table["effort"] = effort
    top = _dict(model.get("top_provider"))
    most = _count(top.get("max_completion_tokens"))
    table["max_output_tokens"] = min(OUTPUT_TOKENS, most) if most else OUTPUT_TOKENS
    context = _count(model.get("context_length"))
    if context is not None:
        table["context_tokens"] = context
    else:
        notes.append("OpenRouter lists no context size for it, so context_tokens is unset.")
    if "tools" not in supported:
        notes.append("OpenRouter lists no tool calls for it, so it cannot run an agent.")
    if "structured_outputs" not in supported:
        table["structured_output"] = False
        notes.append(
            "OpenRouter lists no structured output for it, so structured_output is false and"
            " it cannot be the sweep or judge model."
        )
    table["cache_key"] = True
    pricing = _dict(model.get("pricing"))
    if _per_million(pricing.get("input_cache_write")) is not None:
        # The key only routes. Some models that bill a cache write cache a prompt only when
        # the request asks, and the others take the field without effect.
        table["extra"] = {"cache_control": {"type": "ephemeral"}}
        notes.append(
            "OpenRouter lists a price for a cache write, so extra.cache_control asks for the"
            " prompt cache. Some models cache only when asked."
        )
    table["routing"] = {"sort": "price", "require_parameters": True}
    table["headers"] = {"X-OpenRouter-Metadata": "enabled"}

    cost_in, cost_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
    if cost_in is None or cost_out is None:
        notes.append(
            "OpenRouter lists no fixed price for it, so the profile has none. A run still"
            " records what each call cost."
        )
    else:
        cached = _per_million(pricing.get("input_cache_read"))
        table["price"] = {
            "input": cost_in,
            "cached": cost_in if cached is None else cached,
            "output": cost_out,
        }
        notes.append(
            "The price is the lowest among its providers. Estimates and the monitor's budget"
            " use it. A run records what OpenRouter reports each call cost, or the cost at this"
            " price when it reports none."
        )
        if cached is None and cost_in > 0:
            notes.append(
                "OpenRouter lists no price for cached input, so it counts at the full price."
            )
        tiers = pricing.get("overrides")
        floors = [_count(_dict(t).get("min_prompt_tokens")) for t in tiers or []]
        if floor := min((f for f in floors if f is not None), default=None):
            notes.append(f"From {floor} prompt tokens on the price is higher.")
    retires = model.get("expiration_date")
    if isinstance(retires, str) and _DATE.fullmatch(retires):
        notes.append(f"OpenRouter retires it on {retires}.")

    found = Draft(name=name, slug=slug, table=table, efforts=efforts, notes=tuple(notes))
    found.profile  # noqa: B018  the table must load as a profile
    return found


def _is_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


async def fetch(transport: httpx2.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """OpenRouter's list of models, which needs no key. Raises CatalogError."""
    url = f"{BASE_URL}/models"
    try:
        async with (
            asyncio.timeout(FETCH_SECONDS),
            httpx2.AsyncClient(
                transport=transport or Outbound(), timeout=httpx2.Timeout(30, connect=10)
            ) as client,
            client.stream("GET", url, headers={"Accept": "application/json"}) as response,
        ):
            if response.status_code != 200:
                raise CatalogError("unreachable", f"{url} answered {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise CatalogError("unreachable", f"{url} sent more than {MAX_BYTES} bytes")
    except TimeoutError:
        raise CatalogError("unreachable", f"{url} did not answer in {FETCH_SECONDS:g} s") from None
    except httpx2.HTTPError as e:
        raise CatalogError("unreachable", f"{url}: {type(e).__name__}: {e}") from None
    try:
        data = json.loads(body)["data"]
    except ValueError, KeyError, TypeError:
        data = None
    if not isinstance(data, list):
        raise CatalogError("unreachable", f"{url} did not send a list of models")
    return [m for m in data if isinstance(m, dict)]


class Catalog:
    """OpenRouter's list of models, read again once it is older than FRESH_SECONDS."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._lock = asyncio.Lock()
        self._held: tuple[float, list[dict[str, Any]]] | None = None

    async def models(self) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            if self._held is None or now - self._held[0] > FRESH_SECONDS:
                self._held = (now, await fetch(self._transport))
            return self._held[1]
