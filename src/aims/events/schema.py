from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now() -> str:
    return format_ts(datetime.now(UTC))


Timestamp = Annotated[AwareDatetime, PlainSerializer(format_ts, return_type=str)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BlobRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: Hash
    size: int = Field(ge=0)


Content = str | BlobRef

_registry: dict[str, type[Payload]] = {}


class Payload(BaseModel):
    """Typed body of one event type. Subclasses register themselves by setting `type`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        return text_of(self.model_dump(mode="json"))


def payload_type(name: str) -> type[Payload] | None:
    return _registry.get(name)


def is_blob_ref(value: object) -> bool:
    return isinstance(value, Mapping) and value.keys() == {"sha256", "size"}


def blob_refs(value: Any) -> Iterator[str]:
    if is_blob_ref(value):
        yield value["sha256"]
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from blob_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from blob_refs(item)


def text_of(value: Any) -> str:
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if v:
                parts.append(v)
        elif isinstance(v, Mapping):
            if not is_blob_ref(v):
                for item in v.values():
                    walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    return "\n".join(parts)


class Event(BaseModel):
    """One line of `events.jsonl`. `hash` covers every other field, `prev_hash` links the chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        return text_of(self.data) if payload is None else payload.search_text()


@dataclass(frozen=True, slots=True)
class Draft:
    """An event before the store assigns its sequence number, timestamp and hashes."""

    payload: Payload
    agent_id: str | None = None
