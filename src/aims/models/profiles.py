import copy
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

from aims.events import ModelBackend, ReasoningKind
from aims.models.transport import DROPPED_HEADERS
from aims.stored import Known

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
Summary = Literal["auto", "concise", "detailed"]
Role = Literal["agent", "sweep", "judge"]
SortKey = Literal["price", "throughput", "latency"]

# A number kept as written, so 50 goes out as 50. Strict, so `true` is not 1.
Positive = (
    Annotated[StrictInt, Field(gt=0)] | Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
)
NonNegative = (
    Annotated[StrictInt, Field(ge=0)] | Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
)
Names = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class ProfileError(Exception):
    pass


def _url(value: str) -> str:
    if not value.startswith(("https://", "http://")):
        raise ValueError(f"{value!r} is not an http or https URL")
    return value.rstrip("/")


Url = Annotated[str, AfterValidator(_url)]

_TOKEN = re.compile(r"[-!#$%&'*+.^_`|~0-9A-Za-z]+")
_FIELD = re.compile(r"[\x21-\x7e]+(?:[ \t]+[\x21-\x7e]+)*")
# Headers that the HTTP client, the SDK or the recorder sets or drops, the hop-by-hop headers
# of the connection and those that carry a secret.
_OWNED_HEADERS = DROPPED_HEADERS | {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _headers(value: dict[str, str]) -> dict[str, str]:
    seen: dict[str, str] = {}
    for name, text in value.items():
        if not _TOKEN.fullmatch(name):
            raise ValueError(f"header name {name!r} is not an HTTP token")
        low = name.lower()
        if low in _OWNED_HEADERS or low.startswith("x-stainless-"):
            raise ValueError(f"header {name} is set by AIMS or carries a secret")
        if low in seen:
            raise ValueError(f"headers {seen[low]} and {name} differ only by case")
        seen[low] = name
        if not _FIELD.fullmatch(text):
            raise ValueError(
                f"header {name}: the value must be printable ASCII with no space at either end"
            )
    return value


Headers = Annotated[dict[str, str], AfterValidator(_headers)]

PRICE_KEY = re.compile(r"[\x21-\x7e]{1,200}")


def _price_key(value: str) -> str:
    if not PRICE_KEY.fullmatch(value):
        raise ValueError("price_key takes 1 to 200 printable ASCII characters and no space")
    return value


PriceKey = Annotated[str, AfterValidator(_price_key)]


class _Strict(Known):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _SomeSet(_Strict):
    @model_validator(mode="after")
    def _some(self) -> Self:
        if all(getattr(self, k) is None for k in type(self).model_fields):
            raise ValueError(f"set at least one of {', '.join(type(self).model_fields)}")
        return self


class SortBy(_Strict):
    by: SortKey
    partition: Literal["model", "none"] | None = None


class Percentiles(_SomeSet):
    p50: Positive | None = None
    p75: Positive | None = None
    p90: Positive | None = None
    p99: Positive | None = None


class MaxPrice(_SomeSet):
    prompt: NonNegative | None = None
    completion: NonNegative | None = None
    request: NonNegative | None = None
    image: NonNegative | None = None


class Routing(_Strict):
    """Where a router such as OpenRouter sends a request, sent as its `provider` object.
    Providers go by their slugs."""

    order: Names | None = None
    only: Names | None = None
    ignore: Names | None = None
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: Literal["allow", "deny"] | None = None
    zdr: bool | None = None
    enforce_distillable_text: bool | None = None
    # Free text: providers add new ones.
    quantizations: Names | None = None
    sort: SortKey | SortBy | None = None
    preferred_min_throughput: Positive | Percentiles | None = None
    preferred_max_latency: Positive | Percentiles | None = None
    max_price: MaxPrice | None = None

    def body(self) -> dict[str, JsonValue]:
        """The `provider` object: what is set, at every depth. Empty when nothing is."""
        return self.model_dump(mode="json", exclude_none=True)


class Sampling(_Strict):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    seed: int | None = None


class Price(_Strict):
    """USD per million tokens. `input` is charged on uncached input tokens only."""

    input: float = Field(ge=0)
    cached: float = Field(ge=0)
    output: float = Field(ge=0)


class Listed(_Strict):
    """Says a profile's `price` is the list price of its `price_key` in LiteLLM's public price
    map, which AIMS laid on it for a run. `checked` is when the copy it came from was last
    confirmed current. `cached_listed` is false when the row lists no price for cached input,
    which then costs the input price. `tier_from` is the prompt size in tokens from which the
    row lists a higher price, None when it lists one price."""

    checked: AwareDatetime
    cached_listed: bool = True
    tier_from: int | None = Field(default=None, ge=1)


class Limits(_Strict):
    """Client-side caps shared by every profile in the same `group`, which defaults to the
    profile's name. The provider's rate-limit headers lower them when they say less is left."""

    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    concurrency: int = Field(default=8, ge=1)
    group: str | None = None


class Timeouts(_Strict):
    """Seconds. `stall` is the longest wait for the next bytes, `wall` caps one attempt.

    Without streaming nothing arrives until the answer is done, so `stall` then has to cover
    the whole generation.
    """

    connect: float = Field(default=10, gt=0)
    stall: float = Field(default=300, gt=0)
    wall: float = Field(default=1800, gt=0)


class Retries(_Strict):
    """`attempts` counts the first try. Waits double from `backoff` up to `max` seconds, with
    jitter, unless the provider says how long to wait."""

    attempts: int = Field(default=4, ge=1)
    backoff: float = Field(default=1.0, ge=0)
    max: float = Field(default=60, ge=0)


class Overrides(_Strict):
    """What a scenario may change on the profile it names for an agent."""

    effort: Effort | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    sampling: Sampling | None = None


class Profile(_Strict):
    """How to reach one model. `weights` names the trained model behind it, so two profiles
    that serve the same model under different terms share it."""

    name: str
    backend: ModelBackend
    base_url: Url | None = None
    base_url_env: str | None = None
    model: str
    weights: str
    api_key_env: str | None = None
    reasoning: ReasoningKind
    effort: Effort | None = None
    # A chat template kwarg the model reads its effort from, sent instead of reasoning_effort.
    effort_kwarg: str | None = None
    summary: Summary | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    context_tokens: int | None = Field(default=None, ge=1)
    stream: bool = True
    # None sends no `parallel_tool_calls`, which leaves it to the provider.
    parallel_tool_calls: bool | None = None
    # False where the server cannot hold an answer to a schema, so the model serves no role
    # that needs one.
    structured_output: bool = True
    # Whether a call carries `prompt_cache_key`, which sends a run's calls with one prefix to
    # one cache at providers that route by it. None: yes on the responses backend, whose API
    # has the field, and no on chat, where a strict server may refuse a field it does not know.
    cache_key: bool | None = None
    sampling: Sampling = Field(default_factory=Sampling)
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    routing: Routing = Field(default_factory=Routing)
    # Sent with every request. The run's profile snapshot records them, so they are not for
    # secrets.
    headers: Headers = Field(default_factory=dict)
    price: Price | None = None
    # The profile's row in LiteLLM's public price map, as the map writes its key. A run, a fork
    # and their estimates take the list price from that row when the profile has no `price`.
    # The key is never guessed: one model sits under several keys there, at different prices.
    price_key: PriceKey | None = None
    # Set by AIMS, never in a config file: `price` is the list price of `price_key`.
    listed: Listed | None = None
    limits: Limits = Field(default_factory=Limits)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    retries: Retries = Field(default_factory=Retries)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.base_url is None) == (self.base_url_env is None):
            raise ValueError("set exactly one of base_url and base_url_env")
        if self.summary is not None and self.backend != "responses":
            raise ValueError("summary is only sent by the responses backend")
        if self.effort_kwarg is not None and self.backend != "chat":
            raise ValueError("effort_kwarg is only sent by the chat backend")
        if "provider" in self.extra and self.routing.body():
            raise ValueError("routing and extra.provider both set the provider object; keep one")
        if self.listed is not None and (self.price is None or self.price_key is None):
            raise ValueError("listed marks a list price, so it needs price and price_key")
        return self

    @property
    def group(self) -> str:
        return self.limits.group or self.name

    def url(self, env: Mapping[str, str] = os.environ) -> str:
        if self.base_url is not None:
            return self.base_url
        assert self.base_url_env is not None
        value = env.get(self.base_url_env)
        if not value:
            raise ProfileError(f"profile {self.name}: {self.base_url_env} is not set")
        return _url(value)

    def api_key(self, env: Mapping[str, str] = os.environ) -> str | None:
        if self.api_key_env is None:
            return None
        value = env.get(self.api_key_env)
        if not value:
            raise ProfileError(f"profile {self.name}: {self.api_key_env} is not set")
        return value

    def apply(self, overrides: Overrides | None) -> Profile:
        if overrides is None:
            return self
        update: dict[str, Any] = {}
        if overrides.effort is not None:
            update["effort"] = overrides.effort
        if overrides.max_output_tokens is not None:
            update["max_output_tokens"] = overrides.max_output_tokens
        if overrides.sampling is not None:
            merged = self.sampling.model_dump() | overrides.sampling.model_dump(exclude_unset=True)
            update["sampling"] = Sampling.model_validate(merged)
        return self.model_copy(update=update)

    def snapshot(self) -> dict[str, JsonValue]:
        """The profile as resolved, for `run.started`. It holds env names, never their values."""
        return self.model_dump(mode="json")


class Roles(_Strict):
    """The profile each role falls back to; a role may be unset."""

    agent: str | None = None
    sweep: str | None = None
    judge: str | None = None

    def unset(self) -> list[Role]:
        return [r for r in ROLES if getattr(self, r) is None]


ROLES: tuple[Role, ...] = ("agent", "sweep", "judge")

# How to fix a role that is unset, for the error of the thing that needs it.
UNSET_FIX: dict[Role, str] = {
    "agent": "set agent under [roles] in aims.toml, or give the run a profile",
    "sweep": "set sweep under [roles] in aims.toml, or use a signals-only monitor"
    " (--monitor signals)",
    "judge": "set judge under [roles] in aims.toml, or use a signals-only monitor"
    " (--monitor signals)",
}


class ModelsConfig(_Strict):
    """Profiles by name and the profile of each role. `load_models` also holds the roles of a
    config file to what the monitor asks of them. A config a job builds sets no role, since
    the job names each profile it calls."""

    profiles: dict[str, Profile]
    roles: Roles = Field(default_factory=Roles)

    @model_validator(mode="after")
    def _roles_exist(self) -> Self:
        for role, name in self.roles.model_dump().items():
            if name is not None and name not in self.profiles:
                raise ValueError(f"role {role} names unknown profile {name!r}")
        return self

    def get(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "none"
            raise ProfileError(f"unknown profile {name!r}; known: {known}") from None

    def role(self, role: Role) -> Profile:
        """The profile of `role`. Raises ProfileError naming the fix when the role is unset."""
        name = getattr(self.roles, role)
        if name is None:
            raise ProfileError(f"no profile has the {role} role; {UNSET_FIX[role]}")
        return self.get(name)

    def snapshot(self, names: Iterable[str]) -> dict[str, JsonValue]:
        """The resolved profiles `names`, for the `run.started` config."""
        return {name: self.get(name).snapshot() for name in sorted(set(names))}


class _File(_Strict):
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    roles: dict[str, str] = Field(default_factory=dict)


def builtin_tables() -> dict[str, Any]:
    text = resources.files("aims.models").joinpath("builtins.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def load_models(*paths: Path | None) -> ModelsConfig:
    """The built-in profiles, with each of `paths` (aims.toml files) layered on in turn when it
    exists.

    A table whose name matches an earlier one is merged into it key by key. `extends = "name"`
    starts a profile from another one. A role may be unset; what needs it raises then. A set
    sweep or judge role must name a profile with structured output, since the monitor asks it
    for answers to a schema.
    """
    base = _File.model_validate(builtin_tables())
    tables = copy.deepcopy(base.profiles)
    roles = dict(base.roles)
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            raise ProfileError(f"{path} is not UTF-8") from None
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{path}: {e}") from None
        known = {k: data[k] for k in ("profiles", "roles") if k in data}
        try:
            layer = _File.model_validate(known)
        except ValidationError as e:
            raise ProfileError(f"{path}: {e}") from None
        for name, table in layer.profiles.items():
            if "listed" in table:
                raise ProfileError(
                    f"{path}: profile {name!r} sets listed, which AIMS sets itself when it"
                    " takes a list price; name the row in price_key instead"
                )
            tables[name] = _layer(tables[name], table) if name in tables else table
        roles |= layer.roles
    try:
        profiles = {name: _resolve(name, tables, ()) for name in tables}
        models = ModelsConfig(profiles=profiles, roles=Roles.model_validate(roles))
    except ValidationError as e:
        raise ProfileError(str(e)) from None
    for role in ("sweep", "judge"):
        if getattr(models.roles, role) is None:
            continue
        profile = models.role(role)
        if not profile.structured_output:
            raise ProfileError(f"role {role} needs structured output, which {profile.name} lacks")
    return models


def _resolve(name: str, tables: Mapping[str, Mapping[str, Any]], seen: tuple[str, ...]) -> Profile:
    if name in seen:
        raise ProfileError(f"profile {name!r} extends itself through {' -> '.join(seen)}")
    table = dict(tables[name])
    parent = table.pop("extends", None)
    if parent is not None:
        if parent not in tables:
            raise ProfileError(f"profile {name!r} extends unknown profile {parent!r}")
        base = _resolve(parent, tables, (*seen, name)).model_dump(exclude={"name"})
        table = _layer(base, table)
    return Profile.model_validate({**table, "name": name})


def _layer(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    out = _merge(base, top)
    for key, other in (("base_url", "base_url_env"), ("base_url_env", "base_url")):
        if key in top:
            out.pop(other, None)
    return out


def _merge(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        old = out.get(key)
        if key == "headers" and isinstance(old, Mapping) and isinstance(value, Mapping):
            # A header's name ignores case, so a layer's name replaces it in any case.
            names = {str(k).lower() for k in value}
            out[key] = {k: v for k, v in old.items() if str(k).lower() not in names} | dict(value)
        elif isinstance(old, Mapping) and isinstance(value, Mapping) and key != "extra":
            out[key] = _merge(old, value)
        else:
            out[key] = value
    return out


def check_weights(
    agents: Iterable[Profile], overseers: Iterable[Profile], *, allow_same_model: bool = False
) -> None:
    """Refuse a monitor or judge that runs the same model as an agent it watches."""
    if allow_same_model:
        return
    by_weights = {p.weights: p.name for p in agents}
    for overseer in overseers:
        agent = by_weights.get(overseer.weights)
        if agent is not None:
            raise ProfileError(
                f"profile {overseer.name} watches agent profile {agent}, and both run"
                f" {overseer.weights}; pick another model or set allow_same_model = true"
            )
