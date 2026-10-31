"""Editing config layers and scenario files from the UI, and the scenario overview it shows."""

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

from aims.config import PROJECT, STARTER, ConfigError, Layers, load_layers
from aims.merged import merged
from aims.models import ProfileError
from aims.monitor.config import MonitorConfigError
from aims.plants import PlantError, category_of, registry, taken_tier
from aims.prompts import Prompts, TextError
from aims.run import selftest
from aims.run.runner import check
from aims.scenario import Bundle, Scenario, ScenarioError, parse
from aims.scenario.edit import PromptChange, check_key, set_prompts
from aims.scenario.model import ImpossiblePlant, Role
from aims.server.api import (
    CheckResult,
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


class MergedConfig(Out):
    text: str


def _layers(state: ServerState) -> Layers:
    return state.config.layers()


def _path(state: ServerState, layer: Layer) -> Path:
    """The layer's file. Without a project file it is aims.toml in the working directory, where
    `aims init` writes one; saving there makes it the project's."""
    found = _layers(state)
    if layer == "user":
        return found.user
    return found.project if found.project is not None else Path.cwd() / PROJECT


@router.get("/config", tags=["config"])
async def config_layers(principal: Viewer, state: State) -> ConfigInfo:
    """The config files read, lowest layer first, and whether they load together. A layer
    without a file names where saving it would create one."""
    out = []
    layers: list[Layer] = ["user", "project"]
    for layer in layers:
        path = _path(state, layer)
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


@router.get("/config/merged", tags=["config"])
async def merged_config(principal: Viewer, state: State) -> MergedConfig:
    """The settings in use, every layer merged, as TOML naming the source of each value. Texts
    are left out; /prompts lists them."""
    return MergedConfig(text=merged(state.config.settings()))


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


def _problem(state: ServerState, layer: Layer, text: str) -> tuple[str, ProblemError] | None:
    """Load every layer with `text` in place of `layer`, with the real loaders: None when they
    load, else the error code and what is wrong. TOML errors carry their line. The trial file
    sits beside the real one, as `[scenarios] paths` are relative to it."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return "toml", ProblemError(msg=f"{e.msg} (column {e.colno})", line=e.lineno)
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
        return "config", ProblemError(msg=str(e).replace(str(tmp), str(path)))
    finally:
        tmp.unlink(missing_ok=True)
    return None


def _validate(state: ServerState, layer: Layer, text: str) -> None:
    found = _problem(state, layer, text)
    if found is None:
        return
    code, error = found
    title = "Not TOML" if code == "toml" else "Config does not load"
    detail = error.msg if error.line is None else f"line {error.line}: {error.msg}"
    raise ProblemException(422, title, detail, code=code, errors=[error])


@router.post("/config/{layer}/check", tags=["config"])
async def check_config(
    layer: Layer, body: ConfigText, principal: Operator, state: State
) -> CheckResult:
    """Whether the layer would load with `text` in place of its file, as a save checks it."""
    found = await asyncio.to_thread(_problem, state, layer, body.text)
    return CheckResult(ok=found is None, errors=[] if found is None else [found[1]], warnings=[])


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
    source: str, name: str, principal: Operator, sources: SourcesDep
) -> list[FileInfo]:
    """A scenario's files: operators only, as they hold its plants and self-test."""
    _, path = sources.scenario(source, name)
    return [FileInfo.model_validate(f) for f in await asyncio.to_thread(listing, path)]


@router.get(
    "/scenarios/{source}/{name}/files/{file:path}",
    tags=["scenarios"],
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def read_file(
    source: str, name: str, file: str, principal: Operator, sources: SourcesDep
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


# The scenario overview


class SourceInfo(Out):
    """A directory of scenarios: `label` says where it is, `writable` whether new scenarios and
    edits can go there."""

    id: str
    label: str
    writable: bool


@router.get("/scenarios/sources", tags=["scenarios"])
async def scenario_sources(principal: Viewer, sources: SourcesDep) -> list[SourceInfo]:
    return [SourceInfo(id=s.id, label=s.label, writable=s.writable) for s in sources.all()]


class AgentOverview(Out):
    """`profile` is the one it runs unless a run picks another. `role`, `scripted` and `prompts`
    (its own text overrides) are sent to operators only."""

    id: str
    profile: str
    tools: list[str]
    budget: dict[str, JsonValue]
    role: Role | None = None
    scripted: bool | None = None
    prompts: dict[str, JsonValue] | None = None


class ChannelOverview(Out):
    name: str
    kind: str
    members: list[str] | None
    read_only: list[str]


class PlantOverview(Out):
    """`category` and `taken` are the ones scoring uses, None when the plant kind is unknown."""

    name: str
    kind: str
    category: str | None
    taken: str | None
    agents: list[str] | None
    canary: bool


class SelfTestOverview(Out):
    """`impossible` names the impossible plants whose references the self-test also runs."""

    reference: bool
    exploit: bool
    claims: list[str]
    impossible: list[str]


class ScenarioDetail(Out):
    """A scenario as the scenario page shows it. `errors` say why it does not load, and then
    only the names are set. Viewers see what a run of it shows (`truth` False): no plants,
    roles, scripts, prompts or self-test."""

    source: str
    name: str
    writable: bool
    etag: str
    errors: list[ProblemError]
    truth: bool
    title: str | None
    oversight: str | None
    instruction: str | None
    environment: str | None
    limits: dict[str, JsonValue]
    agents: list[AgentOverview]
    channels: list[ChannelOverview]
    variants: list[str]
    plants: list[PlantOverview] | None
    selftest: SelfTestOverview | None


@router.get("/scenarios/{source}/{name}", tags=["scenarios"])
async def scenario_detail(
    source: str, name: str, principal: Viewer, state: State, sources: SourcesDep
) -> ScenarioDetail:
    s, base = sources.scenario(source, name)
    data = await asyncio.to_thread((base / SCENARIO).read_bytes)
    truth = principal.role != "viewer"
    out = ScenarioDetail(
        source=s.id,
        name=name,
        writable=s.writable,
        etag=etag(data),
        errors=[],
        truth=truth,
        title=None,
        oversight=None,
        instruction=None,
        environment=None,
        limits={},
        agents=[],
        channels=[],
        variants=[],
        plants=None,
        selftest=None,
    )
    try:
        scenario = parse(data.decode(), source=SCENARIO)
    except UnicodeDecodeError:
        out.errors = [ProblemError(msg="scenario.yaml is not UTF-8")]
        return out
    except ScenarioError as e:
        out.errors = scenario_problems(e)
        return out
    return _overview(out, scenario, state.config.settings().models.roles.agent)


def _overview(out: ScenarioDetail, sc: Scenario, default: str) -> ScenarioDetail:
    truth = out.truth
    env = sc.task.environment
    out.title = sc.name
    out.oversight = sc.oversight
    out.instruction = sc.task.instruction
    out.environment = env.image or (f"built from {env.dockerfile}" if env.dockerfile else None)
    out.limits = sc.limits.model_dump(mode="json", exclude_none=True)
    out.variants = list(sc.variants)
    for a in sc.agents:
        agent = AgentOverview(
            id=a.id,
            profile=a.profile or default,
            tools=a.tool_names,
            budget=a.budget.model_dump(mode="json", exclude_none=True),
        )
        if truth:
            agent.role = a.role
            agent.scripted = a.scripted
            agent.prompts = _prompts(a.prompts)
        out.agents.append(agent)
    for c in sc.channels:
        kind = getattr(c, "kind", "plugin")
        out.channels.append(
            ChannelOverview(name=c.name, kind=kind, members=c.members, read_only=c.read_only)
        )
    if not truth:
        return out
    kinds = registry()
    out.plants = []
    for p in sc.plants:
        cls = kinds.get(p.kind)
        category = taken = None
        if cls is not None:
            with contextlib.suppress(PlantError):
                category = category_of(p, cls)
            with contextlib.suppress(PlantError):
                taken = taken_tier(p, cls)
        out.plants.append(
            PlantOverview(
                name=p.name,
                kind=p.kind,
                category=category,
                taken=taken,
                agents=p.agents,
                canary=p.canary is not None,
            )
        )
    if selftest.wanted(sc):
        test = sc.selftest
        out.selftest = SelfTestOverview(
            reference=test is not None and test.reference is not None,
            exploit=test is not None and test.exploit is not None,
            claims=list(test.claims) if test is not None else [],
            impossible=[p.name for p in sc.plants if isinstance(p, ImpossiblePlant)],
        )
    return out


def _prompts(prompts: Prompts | None) -> dict[str, JsonValue]:
    if prompts is None:
        return {}
    dumped = prompts.model_dump(mode="json", exclude_none=True)
    return {k: v for k, v in dumped.items() if v or k not in ("tools", "monitor")}


class PromptKeySet(BaseModel):
    agent: str = Field(min_length=1)
    key: list[str] = Field(min_length=1, max_length=3)
    value: str = Field(max_length=256 * 1024)


class PromptKeyUnset(BaseModel):
    agent: str = Field(min_length=1)
    key: list[str] = Field(min_length=1, max_length=3)


class PromptsPatch(BaseModel):
    """Agent text overrides to set or drop in scenario.yaml, by agent id and key: a text name,
    or ["tools", tool, text]."""

    set: list[PromptKeySet] = Field(default_factory=list)
    unset: list[PromptKeyUnset] = Field(default_factory=list)


@router.patch("/scenarios/{source}/{name}/prompts", tags=["scenarios"])
async def patch_prompts(
    source: str,
    name: str,
    body: PromptsPatch,
    principal: Operator,
    state: State,
    sources: SourcesDep,
    if_match: IfMatch = None,
) -> Saved:
    """Set or drop agents' text overrides in scenario.yaml, keeping its comments and layout.
    The file must still load; texts the run would refuse come back as warnings."""
    base = sources.writable(source, name)
    path = base / SCENARIO
    changes = [PromptChange(c.agent, tuple(c.key), c.value) for c in body.set]
    changes += [PromptChange(c.agent, tuple(c.key), None) for c in body.unset]
    for change in changes:
        try:
            check_key(change.key)
        except ValueError as e:
            raise ProblemException(422, "Invalid request", str(e)) from None
    async with _lock(path):
        current = await asyncio.to_thread(read_or_none, path)
        check_match(current, if_match, None)
        if current is None:
            raise ProblemException(404, "Not found", f"no {SCENARIO}")
        try:
            text = set_prompts(current.decode(), changes, source=SCENARIO)
            scenario = parse(text, source=SCENARIO)
        except UnicodeDecodeError:
            raise ProblemException(422, "Invalid request", "scenario.yaml is not UTF-8") from None
        except ScenarioError as e:
            raise ProblemException(
                422, "Scenario does not load", code="scenario", errors=scenario_problems(e)
            ) from None
        except ValueError as e:
            raise ProblemException(422, "Invalid request", str(e)) from None
        data = text.encode()
        await asyncio.to_thread(write_atomic, path, data)
    settings = state.config.settings()
    bundle = Bundle(scenario, data)
    warnings = [ProblemError(msg=w) for w in check(bundle, settings.models, None, settings.prompts)]
    return Saved(path=SCENARIO, etag=etag(data), warnings=warnings)
