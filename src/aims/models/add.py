"""A profile for a model on any server, drafted from what the person adding it gives.

The server's list of models gives ids only, to check the one asked for. A row of
LiteLLM's price map that the person picks gives the price key, the context size and the output
cap. The check finds out what the model can do and whether it reasons. Last `--unset`
removes fields and `--set` pairs set any field. Server ids and map rows are outside text:
only values of a checked shape reach the profile and its notes.
"""

import asyncio
import difflib
import hashlib
import json
import math
import re
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, get_args

import httpx2
import tomlkit
from pydantic import ValidationError

from aims.events import ModelBackend, ReasoningKind
from aims.models.listprice import SOURCE, Found, NoPrice, PriceList, PriceLists, at_hand
from aims.models.profiles import ROLES, Profile, ProfileError, Role
from aims.net import Guarded, Refused

# A starting value to tune.
OUTPUT_TOKENS = 16384
MAX_NAME = 100
# Lists seen in Nov 2026 hold under 1,000 models and 1 MB.
MAX_BYTES = 32 * 1024 * 1024
MAX_IDS = 5000
FETCH_SECONDS = 15.0
# How long a read list serves. How many servers' lists are kept.
FRESH_SECONDS = 120.0
MAX_LISTS = 32
# How many of a server's ids a lookup offers.
MAX_SUGGESTIONS = 20
# The fields a profile copies from the one it is based on: how to reach the server, not what
# the model is. `extra` holds a router's request object.
CONNECTION = (
    "backend",
    "base_url",
    "base_url_env",
    "api_key_env",
    "cache_key",
    "stream",
    "parallel_tool_calls",
    "headers",
    "extra",
)
GUESS = 'reasoning = "none" is a guess. The check measures what the model sends.'
# Fields `--set` and `--unset` may not name: the name is given apart. AIMS sets `listed` itself.
UNSET = ("name", "extends", "listed")

_ID = re.compile(r"[\x21-\x7e]{1,200}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_SEGMENT = r'(?:[A-Za-z0-9_-]+|"[^"\\=\x00-\x1f\x7f]+")'
_KEY = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})*")
_BACKENDS: tuple[str, ...] = get_args(ModelBackend)

Refusal = Literal["model", "name", "connection", "row", "set", "profile"]


class AddError(Exception):
    """Why no profile came out. `kind` names what is wrong: the model id, the profile name,
    the connection, the LiteLLM row, a `--set` pair, or the profile they make."""

    def __init__(self, kind: Refusal, message: str) -> None:
        super().__init__(message)
        self.kind: Refusal = kind


@dataclass(frozen=True, slots=True)
class ServerList:
    """What a server's list of models gave: the ids it lists, or `problem`, why it gave none."""

    url: str
    ids: tuple[str, ...] = ()
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class Draft:
    """A profile ready to add to a config file. `notes` say what its reader should know.
    `candidates` are the LiteLLM rows that name the model, when none was picked. `guessed` is
    true while `reasoning` is a guess that `measured` can replace."""

    name: str
    model: str
    table: dict[str, Any]
    notes: tuple[str, ...]
    candidates: tuple[str, ...] = ()
    guessed: bool = False

    def measured(self, kind: ReasoningKind) -> Draft:
        """The draft with the reasoning kind a check saw in place of the guess. Unchanged
        when `reasoning` was given."""
        if not self.guessed:
            return self
        if kind == "none":
            note = (
                'reasoning = "none", as the check saw on a short answer. A model may still'
                " reason on harder prompts; each answer of a run records what it carried."
            )
        else:
            note = f'reasoning = "{kind}", as the check measured.'
        notes = tuple(note if n == GUESS else n for n in self.notes)
        table = {**self.table, "reasoning": kind}
        return replace(self, table=table, notes=notes, guessed=False)

    @property
    def profile(self) -> Profile:
        return Profile.model_validate({**self.table, "name": self.name})

    @property
    def fits(self) -> tuple[Role, ...]:
        """The roles it can take as drafted."""
        return fits(self.profile)

    def unfit(self, roles: Iterable[Role]) -> str | None:
        """Why it cannot take one of `roles`, or None when it can take them all."""
        return unfit(self.profile, roles)

    def toml(self) -> str:
        """The `[profiles."name"]` table as config file text."""
        return render(self.name, self.table)


def fits(profile: Profile) -> tuple[Role, ...]:
    """The roles `profile` can take. The sweep and judge need structured output. The check
    finds out whether the model makes tool calls, which an agent needs."""
    return tuple(r for r in ROLES if r == "agent" or profile.structured_output)


def unfit(profile: Profile, roles: Iterable[Role]) -> str | None:
    """Why `profile` cannot take one of `roles`, or None when it can take them all."""
    for role in roles:
        if role not in fits(profile):
            return f"{profile.name} cannot take the {role} role: structured_output is false"
    return None


def render(name: str, table: Mapping[str, Any]) -> str:
    """Profile `name` with fields `table` as a `[profiles."name"]` table of config file text.
    It starts with its header, so no key of it joins a table that comes before it."""
    out = tomlkit.table()
    for key, value in table.items():
        out.add(key, _item(value))
    profiles = tomlkit.table(is_super_table=True)
    profiles.add(name, out)
    doc = tomlkit.document()
    doc.add("profiles", profiles)
    return tomlkit.dumps(doc)


def fingerprint(table: Mapping[str, Any]) -> str:
    """A hash of a profile's fields as a config file holds them, to tell whether they
    changed."""
    text = json.dumps(table, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def one_profile(text: str) -> tuple[str, dict[str, Any]]:
    """The name and fields of the one profile config text `text` holds, as `render` writes
    it. Raises AddError for text that is not TOML, that holds anything but one
    `[profiles."name"]` table, whose name breaks the name rule, that sets a field this way
    cannot set, or whose fields make no profile."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise AddError("profile", f"not TOML: {e}") from None
    except RecursionError:
        raise AddError("profile", "not TOML: it nests too deep") from None
    profiles = doc.get("profiles")
    if doc.keys() != {"profiles"} or not isinstance(profiles, dict) or len(profiles) != 1:
        raise AddError("profile", 'give one [profiles."name"] table and nothing else')
    ((name, table),) = profiles.items()
    if not isinstance(table, dict):
        raise AddError("profile", f"profiles.{name} is not a table")
    check_name(name)
    if barred := [k for k in UNSET if k in table]:
        raise AddError("profile", f"{', '.join(barred)} cannot be set this way")
    try:
        Profile.model_validate({**table, "name": name})
    except ValidationError as e:
        raise AddError("profile", _problems(e)) from None
    return name, table


def suggest(text: str, ids: Sequence[str], most: int = MAX_SUGGESTIONS) -> list[str]:
    """At most `most` of `ids` for a person typing `text`: those that hold it, case ignored,
    shortest first, then the close ones."""
    low = text.lower()
    held = sorted((i for i in ids if low in i.lower()), key=lambda i: (len(i), i))[:most]
    seen = set(held)
    return held + [i for i in _close(text, ids) if i not in seen][: most - len(held)]


def _item(value: Any) -> Any:
    """`value` for tomlkit, with each table in it inline."""
    if isinstance(value, dict):
        inline = tomlkit.inline_table()
        for key, item in value.items():
            inline.append(key, _item(item))
        return inline
    if isinstance(value, list):
        array = tomlkit.array()
        array.extend(_item(v) for v in value)
        return array
    return value


def default_name(model: str) -> str | None:
    """The model id's last part after a `/`, in the characters a profile name takes. None when
    nothing of it is left."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", model.rpartition("/")[2])
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)[:MAX_NAME]
    return name or None


def _model(value: str) -> str:
    model = value.strip()
    if not _ID.fullmatch(model):
        raise AddError(
            "model", f"{value!r} is not a model id: use 1 to 200 printable ASCII characters"
        )
    return model


def _name(model: str, name: str | None) -> str:
    if name is None:
        name = default_name(model)
        if name is None:
            raise AddError("name", f"no profile name follows from {model!r}; give one")
    return check_name(name)


def check_name(name: str) -> str:
    """`name`. Raises AddError unless it keeps to the name rule."""
    if not _NAME.fullmatch(name) or len(name) > MAX_NAME:
        raise AddError(
            "name",
            f"profile name {name!r}: use letters, digits, `.`, `_` and `-`, at most {MAX_NAME}",
        )
    return name


def check_base_url(value: str) -> str:
    """`value` without a trailing `/`. Raises AddError unless it is an http or https URL with
    a host and no user name, password, query or fragment."""
    url = value.strip()
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL, TypeError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        raise AddError("connection", f"{value!r} is not an http or https URL")
    if parsed.userinfo:
        raise AddError(
            "connection",
            "the base URL holds a user name or password, which a run records; name an env var"
            " that holds the key instead",
        )
    if parsed.query or parsed.fragment:
        raise AddError("connection", "the base URL takes no query or fragment")
    return url.rstrip("/")


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) and value >= 1 else None


def _connection(base: Profile) -> dict[str, Any]:
    """The CONNECTION fields `base` sets to other than their defaults."""
    data = base.model_dump(mode="json", include=set(CONNECTION), exclude_defaults=True)
    return {k: data[k] for k in CONNECTION if k in data}


def parse_key(key: str) -> tuple[str, ...]:
    """The field path of TOML key `key`, dotted for a field in a table. Raises AddError."""
    key = key.strip()
    if not _KEY.fullmatch(key):
        raise AddError("set", f"{key!r} is not a TOML key such as extra.a.b")
    node: Any = tomllib.loads(f"{key} = 0")
    path: list[str] = []
    while isinstance(node, dict):
        ((step, node),) = node.items()
        path.append(step)
    if path[0] in UNSET:
        raise AddError("set", f"{path[0]} cannot be set this way")
    return tuple(path)


def parse_set(pair: str) -> tuple[tuple[str, ...], Any]:
    """The field path and value of `key=value`. The key is as `parse_key` takes it. The value
    is a TOML value, or the text as a string when it is none, so `effort=low` works. Raises
    AddError."""
    if any(c in pair for c in "\r\n"):
        raise AddError("set", f"{pair!r}: a pair takes one line")
    key, sep, text = pair.partition("=")
    text = text.strip()
    if not sep:
        raise AddError("set", f"{pair!r} is not key=value")
    path = parse_key(key)
    if not text:
        raise AddError("set", f"{pair!r} gives {key.strip()} no value")
    try:
        parsed = tomllib.loads(f"v = {text}")
    except tomllib.TOMLDecodeError:
        parsed = {}
    value = parsed["v"] if parsed.keys() == {"v"} else text
    return path, value


def _remove(table: dict[str, Any], path: tuple[str, ...]) -> None:
    node: Any = table
    for step in path[:-1]:
        node = node.get(step) if isinstance(node, dict) else None
    if not isinstance(node, dict) or path[-1] not in node:
        raise AddError("set", f"{'.'.join(path)} is not set, so it cannot be removed")
    del node[path[-1]]


def _apply(table: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = table
    for depth, step in enumerate(path[:-1]):
        nested = node.setdefault(step, {})
        if not isinstance(nested, dict):
            raise AddError("set", f"{'.'.join(path[: depth + 1])} is not a table")
        node = nested
    node[path[-1]] = value


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'profile'}: {e['msg']}"
        for e in error.errors(include_url=False)
    )


def _close(model: str, ids: Sequence[str]) -> list[str]:
    by_low = {i.lower(): i for i in reversed(ids)}
    return [by_low[m] for m in difflib.get_close_matches(model.lower(), list(by_low), n=3)]


def draft(
    model: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    key_env: str | None = None,
    base: Profile | None = None,
    backend: ModelBackend | None = None,
    listed: ServerList | None = None,
    prices: PriceList | None = None,
    row: str | None = None,
    unset: Sequence[str] = (),
    sets: Sequence[str] = (),
) -> Draft:
    """The profile for `model`, reached at `base_url` with the key in env var `key_env`, or
    the way profile `base` reaches its server. `listed` is the server's list of models, which
    checks the id. `row` names the LiteLLM row in `prices` that prices it. Last the fields
    `unset` names are removed, then the `sets` pairs apply. Raises AddError."""
    model = _model(model)
    name = _name(model, name)
    notes: list[str] = []
    table: dict[str, Any] = {}
    if base is not None:
        if base_url is not None or key_env is not None:
            raise AddError("connection", "give a base profile or a base URL and key env, not both")
        table = _connection(base)
        notes.append(f"From {base.name} it takes {', '.join(table)}.")
    elif base_url is not None:
        table["backend"] = "chat"
        table["base_url"] = check_base_url(base_url)
        if key_env is not None:
            if not _ENV.fullmatch(key_env):
                raise AddError("connection", f"{key_env!r} is not an env var name")
            table["api_key_env"] = key_env
        else:
            notes.append("It sends no key: no key env is set.")
    else:
        raise AddError("connection", "give a base URL, or a profile to take the server from")
    if backend is not None:
        if backend not in _BACKENDS:
            raise AddError("connection", f"backend {backend!r}: use {' or '.join(_BACKENDS)}")
        table["backend"] = backend
    connection = table
    table = {
        "backend": connection.pop("backend", "chat"),
        **{k: connection.pop(k) for k in ("base_url", "base_url_env") if k in connection},
        "model": model,
        "weights": model,
    }
    if "api_key_env" in connection:
        table["api_key_env"] = connection.pop("api_key_env")
    table["reasoning"] = "none"

    if listed is not None:
        if listed.problem is not None:
            notes.append(f"The server's list of models was not read: {listed.problem}.")
        elif model not in listed.ids:
            near = _close(model, listed.ids)
            hint = f" Close: {', '.join(near)}." if near else ""
            notes.append(f"The server's list of models has no {model}.{hint}")

    output = OUTPUT_TOKENS
    candidates: tuple[str, ...] = ()
    if row is not None:
        if prices is None:
            raise AddError("row", f"no copy of the {SOURCE} price map is at hand to read {row}")
        try:
            prices.get(row)
        except NoPrice as e:
            raise AddError("row", str(e)) from None
        found: dict[str, Any] = prices.rows[row]
        table["price_key"] = row
        if (most := _count(found.get("max_output_tokens"))) is not None:
            output = min(OUTPUT_TOKENS, most)
        if (context := _count(found.get("max_input_tokens"))) is not None:
            table["context_tokens"] = context
        for flag, text in (
            ("supports_function_calling", "no tool calls, which an agent needs"),
            ("supports_response_schema", "no structured output, which the sweep and judge need"),
        ):
            if found.get(flag) is False:
                notes.append(f"The {SOURCE} row lists {text}. The check finds out.")
        if found.get("supports_reasoning") is True:
            notes.append(f"The {SOURCE} row says it can reason.")
    elif prices is not None:
        candidates = tuple(prices.candidates(model))
    table["max_output_tokens"] = output
    table |= connection

    changed: set[str] = set()
    for key in unset:
        path = parse_key(key)
        _remove(table, path)
        changed.add(path[0])
    for pair in sets:
        path, value = parse_set(pair)
        _apply(table, path, value)
        changed.add(path[0])
    try:
        Profile.model_validate({**table, "name": name})
    except ValidationError as e:
        raise AddError("profile", _problems(e)) from None
    if table.get("weights") == model and "weights" not in changed:
        notes.append(
            "weights is the model id. When this model also runs under another id, give both"
            " profiles one weights name, so the monitor refuses to watch it with itself."
        )
    guessed = "reasoning" not in changed
    if guessed:
        notes.append(GUESS)
    if "price" not in table and "price_key" not in table:
        notes.append(
            f"No {SOURCE} row is picked, so it has no price. A run records what each call"
            " cost when the server reports it."
        )
    if "context_tokens" not in table:
        notes.append("Its context size is not known, so context_tokens is unset.")
    return Draft(
        name=name,
        model=model,
        table=table,
        notes=tuple(notes),
        candidates=candidates,
        guessed=guessed,
    )


def _ids(body: bytes | bytearray) -> tuple[str, ...] | None:
    """The model ids of a list in the OpenAI layout, `{"data": [{"id": ...}]}`, or a bare
    array of such entries. None when `body` is no such list."""
    try:
        data = json.loads(body)
    except ValueError, RecursionError:
        return None
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        return None
    ids = (m.get("id") for m in data if isinstance(m, dict))
    kept = dict.fromkeys(i for i in ids if isinstance(i, str) and _ID.fullmatch(i))
    return tuple(kept)[:MAX_IDS]


async def read_list(
    base_url: str, transport: httpx2.AsyncBaseTransport | None = None
) -> ServerList:
    """The server's list of models at `base_url`/models, read with no key. A list it does not
    send comes back with the problem. Raises AddError for a base URL `check_base_url` refuses,
    and aims.net.Refused for a host that resolves to a link-local or cloud metadata address."""
    url = f"{check_base_url(base_url)}/models"
    try:
        async with (
            asyncio.timeout(FETCH_SECONDS),
            httpx2.AsyncClient(
                transport=transport or Guarded(),
                timeout=httpx2.Timeout(FETCH_SECONDS),
                follow_redirects=False,
            ) as client,
            client.stream("GET", url, headers={"Accept": "application/json"}) as response,
        ):
            if response.status_code in (401, 403):
                return ServerList(url, problem=f"it needs a key ({response.status_code})")
            if response.status_code != 200:
                return ServerList(url, problem=f"it answered {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return ServerList(url, problem=f"it sent more than {MAX_BYTES} bytes")
    except TimeoutError:
        return ServerList(url, problem=f"no answer in {FETCH_SECONDS:g} s")
    except httpx2.HTTPError as e:
        return ServerList(url, problem=f"{type(e).__name__}")
    ids = _ids(body)
    if ids is None:
        return ServerList(url, problem="it sent no list of models")
    return ServerList(url, ids)


class ServerLists:
    """Servers' lists of models by base URL, each read again once it is older than
    FRESH_SECONDS. A list that was not read is not kept."""

    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._held: dict[str, tuple[float, ServerList]] = {}

    async def get(self, base_url: str) -> ServerList:
        key = check_base_url(base_url)
        held = self._held.get(key)
        if held is not None and self._clock() - held[0] <= FRESH_SECONDS:
            return held[1]
        found = await read_list(key, self._transport)
        if found.problem is None:
            self._held.pop(key, None)
            self._held[key] = (self._clock(), found)
            while len(self._held) > MAX_LISTS:
                del self._held[next(iter(self._held))]
        return found


async def gather(
    prices: PriceLists,
    *,
    base_url: str | None = None,
    base: Profile | None = None,
    lists: ServerLists | None = None,
) -> tuple[ServerList, Found]:
    """The list of models of the server at `base_url`, or of the one profile `base` reaches,
    with LiteLLM's price map, read side by side. `lists` keeps lists read before; without it
    the list is read now. A list that cannot be read comes back with the problem."""

    async def server() -> ServerList:
        url = base_url
        try:
            if url is None:
                if base is None:
                    return ServerList("", problem="no server is given")
                url = base.url()
            return await (read_list(url) if lists is None else lists.get(url))
        except (AddError, ProfileError, Refused) as e:
            return ServerList(url or "", problem=str(e))

    return await asyncio.gather(server(), at_hand(prices))
