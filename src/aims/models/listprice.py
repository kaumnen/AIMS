"""A list price from LiteLLM's public price map, for a profile that names its row there.

The map is outside text. A key matches exactly or not at all, and only finite numbers of zero
or more reach a price. The price serves estimates. What a call cost is still what the provider
reported or the profile's own `price` says.
"""

import asyncio
import gzip
import json
import logging
import math
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2

from aims.models.profiles import Price
from aims.net import Outbound

log = logging.getLogger(__name__)

SOURCE = "LiteLLM"
URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
# The map is about 4 MB today.
MAX_BYTES = 64 * 1024 * 1024
FETCH_SECONDS = 5.0
# How long a copy counts as current. GitHub lets a cache hold the file for 5 minutes, and the
# map changes many times a week.
FRESH_SECONDS = 24 * 3600.0
# After a read that failed the copy at hand serves this long before the next try, so an
# estimate does not wait for the timeout each time.
RETRY_SECONDS = 600.0
# LiteLLM refuses a map with fewer rows itself.
MIN_ROWS = 50
MODES = ("chat", "responses")
MAX_CANDIDATES = 20
FILE = "litellm-prices.json.gz"
VERSION = 1

_MILLION = Decimal(1_000_000)
_TIER = re.compile(r"input_cost_per_token_above_(\d+)k_tokens")
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e]{1,200}"')


class ListError(Exception):
    """The map could not be read from its source."""


class NoPrice(Exception):
    """The map has no price for a key. The message says why."""


@dataclass(frozen=True, slots=True)
class ListPrice:
    """The price of row `key`, from a copy of the map last confirmed current at `checked`.
    `cached_listed` is false when the row has no price for cached input, which then counts at
    the input price. `tier_from` is the prompt size in tokens from which the row lists a
    higher price, None when it lists one price."""

    key: str
    price: Price
    checked: datetime
    cached_listed: bool
    tier_from: int | None

    def describe(self) -> str:
        """Where the price comes from, as an estimate says it."""
        return f"{SOURCE} list price for {self.key}, fetched {self.checked:%Y-%m-%d}"


def _per_million(value: object) -> float | None:
    """A price per token in USD per million tokens. None unless it is a finite number of zero
    or more."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(Decimal(str(value)) * _MILLION)


def _floor(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) and value > 0 else None


def _read(row: object) -> tuple[Price, bool, int | None] | None:
    """The price of a chat model's row, whether it lists a cached input price, and where its
    higher tier starts. None for any other row and for one without a price per token."""
    if not isinstance(row, dict) or row.get("mode") not in MODES:
        return None
    base: dict[str, Any] = row
    floors: list[int] = []
    tiers = row.get("tiered_pricing")
    ranges = [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []
    for tier in ranges:
        span = tier.get("range")
        start = span[0] if isinstance(span, list) and len(span) == 2 else None
        if start == 0 and not isinstance(start, bool):
            if _per_million(row.get("input_cost_per_token")) is None:
                base = tier
        elif (floor := _floor(start)) is not None:
            floors.append(floor)
    for name, value in row.items():
        found = _TIER.fullmatch(name) if isinstance(name, str) else None
        if found is not None and _per_million(value) is not None:
            floors.append(int(found[1]) * 1000)
    cost_in = _per_million(base.get("input_cost_per_token"))
    cost_out = _per_million(base.get("output_cost_per_token"))
    if cost_in is None or cost_out is None:
        return None
    cached = _per_million(base.get("cache_read_input_token_cost"))
    price = Price(input=cost_in, cached=cost_in if cached is None else cached, output=cost_out)
    return price, cached is not None, min(floors, default=None)


@dataclass(frozen=True, slots=True)
class PriceList:
    """A copy of the map: its rows by key, and when the source last confirmed it current."""

    rows: Mapping[str, Any]
    checked: datetime

    def get(self, key: str) -> ListPrice:
        """The price of row `key`, matched as written. Raises NoPrice."""
        if key not in self.rows:
            raise NoPrice(f"{SOURCE} lists no key {key}")
        found = _read(self.rows[key])
        if found is None:
            raise NoPrice(f"{SOURCE} lists no price per token for a chat model under {key}")
        price, cached_listed, tier_from = found
        return ListPrice(
            key=key,
            price=price,
            checked=self.checked,
            cached_listed=cached_listed,
            tier_from=tier_from,
        )

    def candidates(self, model: str) -> list[str]:
        """The keys of priced chat rows that are `model` or end in `/model`, shortest first,
        at most MAX_CANDIDATES. They are for a person to pick from: rows of one model differ in
        price by provider and region."""
        if not model:
            return []
        end = f"/{model}"
        found = [
            key
            for key, row in self.rows.items()
            if (key == model or key.endswith(end)) and _read(row) is not None
        ]
        return sorted(found, key=lambda key: (len(key), key))[:MAX_CANDIDATES]


def parse(body: bytes | bytearray) -> dict[str, Any]:
    """The rows of the map `body`. Raises ListError unless it is a JSON object of at least
    MIN_ROWS rows."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    return _rows(data)


def _rows(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ListError(f"{URL} did not send a price map")
    rows = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
    if len(rows) < MIN_ROWS:
        raise ListError(f"{URL} sent a price map of {len(rows)} rows, under {MIN_ROWS}")
    return rows


async def fetch(
    etag: str | None = None, transport: httpx2.AsyncBaseTransport | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """The map's rows and its ETag, read from its source, which needs no key. The rows are
    None when the source says the copy with `etag` is still current. Raises ListError."""
    headers = {"Accept": "application/json"}
    if etag is not None:
        headers["If-None-Match"] = etag
    try:
        async with (
            asyncio.timeout(FETCH_SECONDS),
            httpx2.AsyncClient(
                transport=transport or Outbound(), timeout=httpx2.Timeout(FETCH_SECONDS)
            ) as client,
            client.stream("GET", URL, headers=headers) as response,
        ):
            tag = response.headers.get("etag")
            tag = tag if tag is not None and _ETAG.fullmatch(tag) else None
            if response.status_code == 304 and etag is not None:
                return None, tag or etag
            if response.status_code != 200:
                raise ListError(f"{URL} answered {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise ListError(f"{URL} sent more than {MAX_BYTES} bytes")
    except TimeoutError:
        raise ListError(f"{URL} did not answer in {FETCH_SECONDS:g} s") from None
    except httpx2.HTTPError as e:
        raise ListError(f"{URL}: {type(e).__name__}: {e}") from None
    return parse(body), tag


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where AIMS keeps copies of what it can read again: `$AIMS_CACHE_DIR`, else
    `%LOCALAPPDATA%\\aims\\cache` on Windows, else `$XDG_CACHE_HOME/aims` or `~/.cache/aims`."""
    env = os.environ if env is None else env
    if value := env.get("AIMS_CACHE_DIR"):
        return Path(value)
    if os.name == "nt" and (local := env.get("LOCALAPPDATA")):
        return Path(local) / "aims" / "cache"
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "aims"


@dataclass(frozen=True, slots=True)
class _Copy:
    rows: dict[str, Any]
    etag: str | None
    checked: float


def _load(path: Path) -> _Copy | None:
    """The copy in file `path`. None when there is none or it does not read as one."""
    try:
        data = json.loads(gzip.decompress(path.read_bytes()))
        if data["v"] != VERSION or data["url"] != URL:
            return None
        etag, checked = data["etag"], data["checked"]
        if isinstance(checked, bool) or not isinstance(checked, int | float):
            return None
        if not math.isfinite(checked) or checked < 0:
            return None
        if etag is not None and not (isinstance(etag, str) and _ETAG.fullmatch(etag)):
            etag = None
        return _Copy(_rows(data["rows"]), etag, float(checked))
    except OSError, EOFError, ValueError, KeyError, TypeError, ListError:
        return None


def _save(path: Path, copy: _Copy) -> None:
    data = {"v": VERSION, "url": URL, "etag": copy.etag, "checked": copy.checked, "rows": copy.rows}
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(gzip.compress(json.dumps(data).encode(), mtime=0))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Found:
    """The map as it is at hand, None when there is no copy. `problem` says why the copy
    could not be confirmed current, or why there is none."""

    prices: PriceList | None
    problem: str | None = None


class PriceLists:
    """The map, kept in a file and read from its source again once the copy is older than
    FRESH_SECONDS. A copy of any age serves while the source does not answer."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._copy: _Copy | None = None
        self._loaded = False
        self._retry_at = 0.0
        self._problem: str | None = None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else cache_dir() / FILE

    async def get(self) -> Found:
        async with self._lock:
            if not self._loaded:
                self._copy = await asyncio.to_thread(_load, self.path)
                self._loaded = True
            now = self._clock()
            copy = self._copy
            if copy is not None and 0 <= now - copy.checked <= FRESH_SECONDS:
                return Found(_list(copy))
            if now < self._retry_at:
                return Found(None if copy is None else _list(copy), self._problem)
            try:
                rows, etag = await fetch(None if copy is None else copy.etag, self._transport)
            except ListError as e:
                self._retry_at = now + RETRY_SECONDS
                self._problem = str(e)
                return Found(None if copy is None else _list(copy), self._problem)
            if rows is None:
                assert copy is not None
                rows = copy.rows
            copy = self._copy = _Copy(rows, etag, now)
            self._retry_at, self._problem = 0.0, None
            try:
                await asyncio.to_thread(_save, self.path, copy)
            except OSError as e:
                log.warning("the %s price map is not kept in %s: %s", SOURCE, self.path, e)
            return Found(_list(copy))


def _list(copy: _Copy) -> PriceList:
    return PriceList(copy.rows, datetime.fromtimestamp(copy.checked, UTC))
