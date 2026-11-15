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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2

from aims.models.profiles import PRICE_KEY, Price, Profile
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
        """The keys of priced chat rows that name model id `model`, at most MAX_CANDIDATES: those
        that are the id or end in `/` and the id, then those that are or end in its last part
        after a `/`, case ignored, shortest first within each. They are for a person to pick
        from: rows of one model differ in price by provider and region."""
        model = model.lower()
        last = model.rpartition("/")[2]
        if not last:
            return []
        whole: list[str] = []
        part: list[str] = []
        for key in self._keys(lambda k: k == last or k.endswith(f"/{last}")):
            low = key.lower()
            (whole if low == model or low.endswith(f"/{model}") else part).append(key)
        return (_shortest(whole) + _shortest(part))[:MAX_CANDIDATES]

    def search(self, text: str) -> list[str]:
        """The keys of priced chat rows that hold `text`, case ignored, shortest first, at most
        MAX_CANDIDATES."""
        text = text.strip().lower()
        if not text:
            return []
        return _shortest(self._keys(lambda k: text in k))[:MAX_CANDIDATES]

    def _keys(self, match: Callable[[str], bool]) -> list[str]:
        """The keys of priced chat rows whose lower case form `match` takes, and that a
        profile's `price_key` can name."""
        return [
            key
            for key, row in self.rows.items()
            if match(key.lower()) and PRICE_KEY.fullmatch(key) and _read(row) is not None
        ]


def _shortest(keys: list[str]) -> list[str]:
    return sorted(keys, key=lambda key: (len(key), key))


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


@dataclass(frozen=True, slots=True)
class Listing:
    """The list prices an estimate lays on profiles that name a `price_key` and set no
    `price`: `prices` by key, `missing` why the map gave a key none, and `problem` why the
    copy at hand could not be confirmed current."""

    prices: Mapping[str, ListPrice] = field(default_factory=dict)
    missing: Mapping[str, str] = field(default_factory=dict)
    problem: str | None = None

    def find(self, p: Profile) -> ListPrice | None:
        """The list price of profile `p`. None when it sets its own price, names no key, or
        the map gave its key none."""
        if p.price is not None or p.price_key is None:
            return None
        return self.prices.get(p.price_key)

    def why(self, p: Profile) -> str | None:
        """Why profile `p`, which names a key and sets no price, got no list price. None when
        it got one or no lookup was made for it."""
        if p.price is not None or p.price_key is None:
            return None
        return self.missing.get(p.price_key)

    def lay(self, p: Profile) -> Profile:
        """A copy of `p` with its list price as its price; `p` itself without one."""
        found = self.find(p)
        return p if found is None else p.model_copy(update={"price": found.price})


async def listing(profiles: Iterable[Profile], lists: PriceLists | None) -> Listing:
    """The list prices of those `profiles` that name a key and set no price. The map is read
    only for such a profile. A map that cannot be read leaves them unpriced, with the reason;
    nothing here fails an estimate."""
    keys = _wanted(profiles)
    if not keys or lists is None:
        return Listing()
    return _listing(keys, await _at_hand(lists))


def _wanted(profiles: Iterable[Profile]) -> list[str]:
    return sorted({p.price_key for p in profiles if p.price is None and p.price_key is not None})


async def _at_hand(lists: PriceLists) -> Found:
    try:
        return await lists.get()
    except Exception as e:
        # A list price only adds to what it serves, which goes on without it.
        log.warning("the %s price map could not be read", SOURCE, exc_info=True)
        return Found(None, f"{type(e).__name__}: {e}")


def _listing(keys: list[str], found: Found) -> Listing:
    if found.prices is None:
        why = f"no copy of the {SOURCE} price map is at hand ({found.problem or 'none kept'})"
        return Listing(missing=dict.fromkeys(keys, why), problem=found.problem)
    prices: dict[str, ListPrice] = {}
    missing: dict[str, str] = {}
    for key in keys:
        try:
            prices[key] = found.prices.get(key)
        except NoPrice as e:
            missing[key] = str(e)
    return Listing(prices, missing, found.problem)


@dataclass(frozen=True, slots=True)
class Survey:
    """What the map says of profiles that set no price. `listing` holds the list prices of
    those that name a key. `candidates` holds, by profile name, the rows that may price each
    one that got none, for a person to name one in its `price_key`. `found` holds the rows a
    search matched. `checked` is when the copy at hand was last confirmed current, None when
    the map was not read or no copy is at hand."""

    listing: Listing = field(default_factory=Listing)
    candidates: Mapping[str, list[ListPrice]] = field(default_factory=dict)
    found: list[ListPrice] = field(default_factory=list)
    checked: datetime | None = None


async def survey(profiles: Iterable[Profile], lists: PriceLists, search: str = "") -> Survey:
    """What the map says of those `profiles` that set no price, and its rows that hold
    `search`. The map is read only for such a profile or a search."""
    unpriced = [p for p in profiles if p.price is None]
    if not unpriced and not search.strip():
        return Survey()
    found = await _at_hand(lists)
    listed = _listing(_wanted(unpriced), found)
    prices = found.prices
    if prices is None:
        return Survey(listed)
    candidates = {
        p.name: [prices.get(key) for key in prices.candidates(p.model)]
        for p in unpriced
        if listed.find(p) is None
    }
    rows = [prices.get(key) for key in prices.search(search)]
    return Survey(listed, candidates, rows, prices.checked)
