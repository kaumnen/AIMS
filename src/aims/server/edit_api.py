"""Editing config layers and scenario files from the UI."""

import asyncio
import contextlib
import os
import secrets
import shutil
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

import tomlkit
import tomlkit.items
from fastapi import APIRouter, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, JsonValue

from aims.config import STARTER, ConfigError, Layers, load_layers
from aims.models import ProfileError
from aims.monitor.config import MonitorConfigError
from aims.prompts import TextError
from aims.scenario import ScenarioError, parse
from aims.server.api import (
    Operator,
    Out,
    SourcesDep,
    State,
    Uploader,
    Viewer,
    scenario_problems,
)
from aims.server.files import (
    MAX_FILE,
    SCENARIO,
    STARTER_SCENARIO,
    check_match,
    confined,
    etag,
    is_scenario_file,
    listing,
    quoted,
    read_or_none,
    write_atomic,
)
from aims.server.problems import ProblemError, ProblemException, responses
from aims.server.state import ServerState

router = APIRouter(prefix="/api", responses=responses(401, 403, 404, 412, 422, 428))
Layer = Literal["user", "project"]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]


class LayerInfo(Out):
    layer: Layer
    path: str
    exists: bool
    etag: str | None


class ConfigInfo(Out):
    layers: list[LayerInfo]
    error: str | None = None


class ConfigFile(Out):
    layer: Layer
    path: str
    exists: bool
    text: str
    etag: str | None


class KeySet(BaseModel):
    key: list[str] = Field(min_length=1)
    value: JsonValue


class ConfigPatch(BaseModel):
    """Changes by key path, applied with comments and order kept. `set` makes the tables it
    needs; `unset` drops a key if it is there."""

    set: list[KeySet] = Field(default_factory=list)
    unset: list[list[str]] = Field(default_factory=list)


class ConfigText(BaseModel):
    text: str = Field(max_length=1024 * 1024)


def _layers(state: ServerState) -> Layers:
    return state.config.layers()


def _path(state: ServerState, layer: Layer) -> Path:
    found = _layers(state)
    if layer == "user":
        return found.user
    if found.project is None:
        raise ProblemException(
            404, "Not found", "no project file: start `aims ui` in a project or with --config"
        )
    return found.project


@router.get("/config", tags=["config"])
async def config_layers(principal: Viewer, state: State) -> ConfigInfo:
    """The config files read, lowest layer first, and whether they load together."""
    found = _layers(state)
    out = []
    pairs: list[tuple[Layer, Path | None]] = [("user", found.user), ("project", found.project)]
    for layer, path in pairs:
        if path is None:
            continue
        data = read_or_none(path)
        out.append(
            LayerInfo(
                layer=layer,
                path=str(path),
                exists=data is not None,
                etag=None if data is None else etag(data),
            )
        )
    error = None
    try:
        state.config.settings()
    except ProblemException as e:
        error = e.problem.detail
    return ConfigInfo(layers=out, error=error)


def _file(layer: Layer, path: Path, data: bytes | None) -> ConfigFile:
    return ConfigFile(
        layer=layer,
        path=str(path),
        exists=data is not None,
        text=STARTER if data is None else data.decode(errors="replace"),
        etag=None if data is None else etag(data),
    )


@router.get("/config/{layer}", tags=["config"])
async def read_config(layer: Layer, principal: Viewer, state: State) -> ConfigFile:
    """A layer's text; a file that does not exist yet comes back as the starter text."""
    path = _path(state, layer)
    return _file(layer, path, read_or_none(path))


def _validate(state: ServerState, layer: Layer, text: str) -> None:
    """Load every layer with `text` in place of `layer`, with the real loaders."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProblemException(422, "Not TOML", str(e), code="toml") from None
    found = _layers(state)
    path = _path(state, layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.check.{secrets.token_hex(4)}"
    try:
        tmp.write_text(text)
        trial = Layers(
            user=tmp if layer == "user" else found.user,
            project=tmp if layer == "project" else found.project,
        )
        load_layers(trial)
    except (ConfigError, ProfileError, TextError, MonitorConfigError) as e:
        raise ProblemException(
            422, "Config does not load", str(e).replace(str(tmp), str(path)), code="config"
        ) from None
    finally:
        tmp.unlink(missing_ok=True)


_LOCKS: dict[Path, asyncio.Lock] = {}


def _lock(path: Path) -> asyncio.Lock:
    return _LOCKS.setdefault(path, asyncio.Lock())


async def _save(
    state: ServerState, layer: Layer, text: str, if_match: str | None, if_none_match: str | None
) -> Response:
    path = _path(state, layer)
    data = text.encode()
    async with _lock(path):
        current = read_or_none(path)
        check_match(current, if_match, if_none_match)
        _validate(state, layer, text)
        await asyncio.to_thread(write_atomic, path, data)
    body = _file(layer, path, data)
    return Response(
        body.model_dump_json(), media_type="application/json", headers={"ETag": quoted(etag(data))}
    )


@router.put("/config/{layer}", tags=["config"], response_model=ConfigFile)
async def write_config(
    layer: Layer,
    body: ConfigText,
    principal: Operator,
    state: State,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """Replace a layer's text. The merged layers must load first."""
    return await _save(state, layer, body.text, if_match, if_none_match)


def _value(value: JsonValue) -> object:
    if isinstance(value, str) and "\n" in value:
        return tomlkit.string(value, multiline=True)
    return value


@router.patch("/config/{layer}", tags=["config"], response_model=ConfigFile)
async def patch_config(
    layer: Layer,
    body: ConfigPatch,
    principal: Operator,
    state: State,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """Set or unset keys in a layer, keeping its comments and order."""
    path = _path(state, layer)
    current = read_or_none(path)
    doc = tomlkit.parse(STARTER if current is None else current.decode())
    for change in body.set:
        table = _table(doc, change.key[:-1], create=True)
        if table is None:
            raise ProblemException(
                422, "Invalid request", f"{'.'.join(change.key)}: a part of it is not a table"
            )
        table[change.key[-1]] = _value(change.value)
    for key in body.unset:
        table = _table(doc, key[:-1], create=False)
        if table is not None and key and key[-1] in table:
            del table[key[-1]]
    return await _save(state, layer, tomlkit.dumps(doc), if_match, if_none_match)


def _table(doc: tomlkit.TOMLDocument, parts: list[str], *, create: bool) -> Any:
    """The table at `parts`, made when `create` and missing; None if a part is not a table."""
    table: Any = doc
    for part in parts:
        nxt = table.get(part)
        if nxt is None and create:
            nxt = tomlkit.table()
            table[part] = nxt
        if not isinstance(nxt, tomlkit.items.Table | tomlkit.items.InlineTable):
            return None
        table = nxt
    return table


# Scenario files


class FileInfo(Out):
    path: str
    size: int


class Saved(Out):
    path: str
    etag: str
    warnings: list[ProblemError] = Field(default_factory=list)


class Duplicate(BaseModel):
    source: str
    name: str


@router.get("/scenarios/{source}/{name}/files", tags=["scenarios"])
async def list_files(
    source: str, name: str, principal: Viewer, sources: SourcesDep
) -> list[FileInfo]:
    _, path = sources.scenario(source, name)
    return [FileInfo.model_validate(f) for f in await asyncio.to_thread(listing, path)]


@router.get(
    "/scenarios/{source}/{name}/files/{file:path}",
    tags=["scenarios"],
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def read_file(
    source: str, name: str, file: str, principal: Viewer, sources: SourcesDep
) -> Response:
    """A scenario file's bytes, with its ETag for the write that replaces it."""
    _, base = sources.scenario(source, name)
    path = confined(base, file)
    data = await asyncio.to_thread(read_or_none, path)
    if data is None or not path.is_file():
        raise ProblemException(404, "Not found", f"no file {file}")
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"ETag": quoted(etag(data)), "Cache-Control": "no-cache"},
    )


@router.put(
    "/scenarios/{source}/{name}/files/{file:path}",
    tags=["scenarios"],
    openapi_extra={"requestBody": {"content": {"application/octet-stream": {}}, "required": True}},
)
async def write_file(
    source: str,
    name: str,
    file: str,
    request: Request,
    principal: Uploader,
    state: State,
    sources: SourcesDep,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> Saved:
    """Write a scenario file as sent. `scenario.yaml` must validate; it is stored verbatim."""
    base = sources.writable(source, name)
    path = confined(base, file)
    data = await _read(request)
    warnings: list[ProblemError] = []
    if is_scenario_file(base, path):
        try:
            parse(data.decode(), source=SCENARIO)
        except UnicodeDecodeError:
            raise ProblemException(422, "Invalid request", "scenario.yaml is not UTF-8") from None
        except ScenarioError as e:
            raise ProblemException(
                422, "Scenario does not load", code="scenario", errors=scenario_problems(e)
            ) from None
    async with _lock(path):
        current = await asyncio.to_thread(read_or_none, path)
        check_match(current, if_match, if_none_match)
        await asyncio.to_thread(write_atomic, path, data)
    return Saved(path=file, etag=etag(data), warnings=warnings)


async def _read(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FILE:
            raise ProblemException(413, "Too large", f"a file is at most {MAX_FILE} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.delete("/scenarios/{source}/{name}/files/{file:path}", status_code=204, tags=["scenarios"])
async def delete_file(
    source: str,
    name: str,
    file: str,
    principal: Operator,
    sources: SourcesDep,
    if_match: IfMatch = None,
) -> None:
    base = sources.writable(source, name)
    path = confined(base, file)
    if is_scenario_file(base, path):
        raise ProblemException(422, "Invalid request", "a scenario keeps its scenario.yaml")
    async with _lock(path):
        current = await asyncio.to_thread(read_or_none, path)
        if current is None:
            raise ProblemException(404, "Not found", f"no file {file}")
        check_match(current, if_match, None)
        await asyncio.to_thread(path.unlink)


@router.post("/scenarios/{source}/{name}", status_code=201, tags=["scenarios"])
async def create_scenario(
    source: str, name: str, principal: Operator, sources: SourcesDep
) -> Saved:
    """A new scenario directory with a starter scenario.yaml."""
    path = sources.writable(source, name, exists=False)
    if path.exists():
        raise ProblemException(409, "Conflict", f"{name} exists")
    data = STARTER_SCENARIO.format(name=name).encode()
    await asyncio.to_thread(write_atomic, path / SCENARIO, data)
    return Saved(path=SCENARIO, etag=etag(data))


@router.post("/scenarios/{source}/{name}/duplicate", status_code=201, tags=["scenarios"])
async def duplicate_scenario(
    source: str, name: str, body: Duplicate, principal: Operator, sources: SourcesDep
) -> Saved:
    """Copy a scenario, built-in ones included, to a writable source to edit it."""
    _, src = sources.scenario(source, name)
    dst = sources.writable(body.source, body.name, exists=False)
    if dst.exists():
        raise ProblemException(409, "Conflict", f"{body.name} exists")

    def skip(where: str, names: list[str]) -> list[str]:
        return [
            n
            for n in names
            if n.startswith(".") or n == "__pycache__" or os.path.islink(os.path.join(where, n))
        ]

    def copy() -> None:
        shutil.copytree(src, dst, ignore=skip)

    try:
        await asyncio.to_thread(copy)
    except OSError as e:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(shutil.rmtree, dst)
        raise ProblemException(422, "Invalid request", f"copy failed: {e}") from None
    data = (dst / SCENARIO).read_bytes()
    return Saved(path=SCENARIO, etag=etag(data))
