from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    ValidationError,
)

SCHEMA_VERSION = 1
GENESIS = "0" * 64


def format_ts(ts: datetime) -> str:
    """`ts` in UTC. A time UTC cannot hold, past year 9999 or before year 1 there, is cut to
    the last or first one it can."""
    try:
        utc = ts.astimezone(UTC)
    except OverflowError:
        utc = datetime.max if ts.year > 1 else datetime.min
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def when(at: float) -> datetime:
    """The UTC time `at` seconds after the epoch. One no date holds, past year 9999 or before
    year 1, from a row written outside AIMS, is cut to the last or first one there is, as
    format_ts does, so the rest of the listing still reads."""
    try:
        return datetime.fromtimestamp(at, UTC)
    except OverflowError, OSError, ValueError:
        return (datetime.max if at > 0 else datetime.min).replace(tzinfo=UTC)


def now() -> str:
    return format_ts(datetime.now(UTC))


Timestamp = Annotated[AwareDatetime, PlainSerializer(format_ts, return_type=str)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def is_sha256(value: object) -> bool:
    """Whether `value` is a sha256 as a blob is named by: 64 lowercase hex digits, a str."""
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class Opaque:
    """Marks a payload field that holds what was written outside AIMS: the metrics of a
    verifier's reward.json, the headers of an HTTP request or answer. What wrote it may shape a
    mapping as a blob reference, so no reader takes one in it for a reference."""


OPAQUE = Opaque()


class BlobRef(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )

    sha256: Hash
    size: int = Field(ge=0)


Content = str | BlobRef

_registry: dict[str, type[Payload]] = {}


class Payload(BaseModel):
    """Typed body of one event type. Subclasses register themselves by setting `type`."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )

    type: ClassVar[str]

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        name = cls.__dict__.get("type")
        if name is None:
            return
        if name in _registry:
            raise TypeError(f"event type {name!r} is already registered")
        _registry[name] = cls

    def search_text(self) -> str:
        return text_of(self.model_dump(mode="json"), self.type)


def payload_type(name: str) -> type[Payload] | None:
    return _registry.get(name)


def opaque_fields(kind: object) -> frozenset[str]:
    """The fields of event type `kind` marked Opaque; none for a type not registered."""
    cls = _registry.get(kind) if type(kind) is str else None
    return frozenset() if cls is None else _opaque(cls)


@cache
def _opaque(cls: type[Payload]) -> frozenset[str]:
    return frozenset(
        name
        for name, field in cls.model_fields.items()
        if any(isinstance(m, Opaque) for m in field.metadata)
    )


def payload_types() -> dict[str, type[Payload]]:
    """Every registered event type and its payload model."""
    return dict(_registry)


def is_blob_ref(value: object) -> bool:
    return isinstance(value, Mapping) and value.keys() == {"sha256", "size"}


LOOSE_CAP = 16 * 1024 * 1024
"""The most bytes read through a mapping that names a blob by its sha256 but is no blob
reference. A snapshot change names the file it captured so, next to the size stat saw, which a
file still being written passes. A snapshot captures no more than 16 MiB in all."""


def stated_size(value: Mapping[str, Any]) -> int:
    """The most bytes a read through `value` takes: the size a blob reference states, else
    LOOSE_CAP."""
    return value["size"] if is_blob_ref(value) else LOOSE_CAP


def blob_refs(kind: object, data: Any) -> Iterator[str]:
    for sha256, _ in blob_sizes(kind, data):
        yield sha256


def blob_sizes(kind: object, data: Any) -> Iterator[tuple[str, Any]]:
    """The sha256 and the stated size of each blob reference in `data`, the data of an event of
    type `kind`. Its Opaque fields hold none. A sha256 or a size of a hand-made log may be any
    JSON value."""
    skip = opaque_fields(kind)
    if skip and isinstance(data, Mapping) and not is_blob_ref(data):
        for key, value in data.items():
            if key not in skip:
                yield from _refs(value)
    else:
        yield from _refs(data)


def _refs(value: Any) -> Iterator[tuple[str, Any]]:
    if is_blob_ref(value):
        yield value["sha256"], value["size"]
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def text_of(value: Any, kind: object = None) -> str:
    """The texts in `value`, the data of an event of type `kind`, less those of its blob
    references. A mapping in an Opaque field is no reference, so its texts count."""
    skip = opaque_fields(kind)
    if skip and isinstance(value, Mapping):
        return "\n".join(t for k, item in value.items() for t in _texts(item, k not in skip))
    return "\n".join(_texts(value))


def _texts(value: Any, refs: bool = True) -> Iterator[str]:
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, Mapping):
        if not (refs and is_blob_ref(value)):
            for item in value.values():
                yield from _texts(item, refs)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item, refs)


class Event(BaseModel):
    """One line of `events.jsonl`. `hash` covers every other field, `prev_hash` links the chain."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )

    v: Literal[1]
    run_id: str
    seq: int = Field(ge=0)
    ts: Timestamp
    agent_id: str | None
    type: str
    data: dict[str, JsonValue]
    prev_hash: Hash
    hash: Hash

    def payload(self) -> Payload | None:
        cls = payload_type(self.type)
        return None if cls is None else cls.model_validate(self.data)

    def search_text(self) -> str:
        try:
            payload = self.payload()
        except ValidationError:
            payload = None
        return text_of(self.data, self.type) if payload is None else payload.search_text()


@dataclass(frozen=True, slots=True)
class Draft:
    """An event before the store assigns its sequence number, timestamp and hashes."""

    payload: Payload
    agent_id: str | None = None
