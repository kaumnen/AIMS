import copy
import os
import tomllib
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from aims.events import ModelBackend, ReasoningKind

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
Summary = Literal["auto", "concise", "detailed"]
Role = Literal["agent", "sweep", "judge"]


class ProfileError(Exception):
    pass


def _url(value: str) -> str:
    if not value.startswith(("https://", "http://")):
        raise ValueError(f"{value!r} is not an http or https URL")
    return value.rstrip("/")


Url = Annotated[str, AfterValidator(_url)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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
    summary: Summary | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    context_tokens: int | None = Field(default=None, ge=1)
    stream: bool = True
    parallel_tool_calls: bool = False
    sampling: Sampling = Field(default_factory=Sampling)
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    price: Price | None = None
    limits: Limits = Field(default_factory=Limits)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    retries: Retries = Field(default_factory=Retries)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.base_url is None) == (self.base_url_env is None):
            raise ValueError("set exactly one of base_url and base_url_env")
        if self.summary is not None and self.backend != "responses":
            raise ValueError("summary is only sent by the responses backend")
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
    agent: str
    sweep: str
    judge: str


class ModelsConfig(_Strict):
    profiles: dict[str, Profile]
    roles: Roles

    @model_validator(mode="after")
    def _roles_exist(self) -> Self:
        for role, name in self.roles.model_dump().items():
            if name not in self.profiles:
                raise ValueError(f"role {role} names unknown profile {name!r}")
        return self

    def get(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles))
            raise ProfileError(f"unknown profile {name!r}; known: {known}") from None

    def role(self, role: Role) -> Profile:
        return self.get(getattr(self.roles, role))

    def snapshot(self, names: Iterable[str]) -> dict[str, JsonValue]:
        """The resolved profiles `names`, for the `run.started` config."""
        return {name: self.get(name).snapshot() for name in sorted(set(names))}


class _File(_Strict):
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    roles: dict[str, str] = Field(default_factory=dict)


def builtin_tables() -> dict[str, Any]:
    text = resources.files("aims.models").joinpath("builtins.toml").read_text()
    return tomllib.loads(text)


def load_models(*paths: Path | None) -> ModelsConfig:
    """The built-in profiles, with each of `paths` (aims.toml files) layered on in turn when it
    exists.

    A table whose name matches an earlier one is merged into it key by key. `extends = "name"`
    starts a profile from another one.
    """
    base = _File.model_validate(builtin_tables())
    tables = copy.deepcopy(base.profiles)
    roles = dict(base.roles)
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{path}: {e}") from None
        known = {k: data[k] for k in ("profiles", "roles") if k in data}
        try:
            layer = _File.model_validate(known)
        except ValidationError as e:
            raise ProfileError(f"{path}: {e}") from None
        for name, table in layer.profiles.items():
            tables[name] = _layer(tables[name], table) if name in tables else table
        roles |= layer.roles
    try:
        profiles = {name: _resolve(name, tables, ()) for name in tables}
        return ModelsConfig(profiles=profiles, roles=Roles.model_validate(roles))
    except ValidationError as e:
        raise ProfileError(str(e)) from None


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
        if isinstance(old, Mapping) and isinstance(value, Mapping) and key != "extra":
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
