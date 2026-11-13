"""The control plane: API, live streams, runner ingest and the UI, in one ASGI app. It never
touches Docker; runners do."""

import asyncio
import json
import mimetypes
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aims import __version__
from aims.aio import stopped
from aims.events.schema import payload_types
from aims.server import (
    api,
    audit_api,
    dispatch,
    edit_api,
    estimate_api,
    monitor_api,
    problems,
    report_api,
    runner_api,
    score_api,
    stream,
)
from aims.server.audit import AuditLog
from aims.server.auth import HEADERS, SAFE, SecurityHeaders
from aims.server.codings import allowed
from aims.server.files import Sources
from aims.server.limits import BodyLimit
from aims.server.problems import Problem
from aims.server.state import ServerState
from aims.wire import RunnerFeed, StreamMessage

LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")
REF = "#/components/schemas/{model}"
COPIES = (("br", ".br"), ("gzip", ".gz"))


def ui_root() -> Path | None:
    path = Path(__file__).parent.parent / "ui" / "dist"
    return path if (path / "index.html").is_file() else None


def operation_id(route: APIRoute) -> str:
    return route.name


class AimsAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        return openapi(self)


def create_app(
    state: ServerState,
    *,
    hosts: tuple[str, ...] = (),
    ui: Path | None = None,
    dispatcher: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        task = asyncio.create_task(dispatch.run(state)) if dispatcher else None
        try:
            yield
        finally:
            if task is not None:
                await stopped(task)

    app = AimsAPI(
        title="AIMS",
        version=__version__,
        generate_unique_id_function=operation_id,
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.server = state

    async def on_truth(run_id: str) -> None:
        await score_api.score_after(state, run_id)
        state.poke()

    state.on_truth = on_truth
    app.state.auth = state.auth
    app.state.sources = Sources(state.data, state.config)
    problems.install(app, HEADERS)
    app.include_router(api.router)
    app.include_router(edit_api.router)
    app.include_router(monitor_api.router)
    app.include_router(estimate_api.router)
    app.include_router(score_api.router)
    app.include_router(report_api.router)
    app.include_router(audit_api.router)
    app.include_router(stream.router)
    app.include_router(runner_api.router)
    app.add_api_route("/healthz", healthz, include_in_schema=False)
    app.add_middleware(AuditLog, db=state.db)
    app.add_middleware(BodyLimit)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK, *hosts])
    root = ui if ui is not None and (ui / "index.html").is_file() else ui_root()
    _spa(app, root)
    return app


async def healthz() -> JSONResponse:
    """For a container healthcheck. It needs no token and tells nothing else."""
    return JSONResponse({"ok": True})


def _stamp(root: Path | None) -> int | None:
    """When the assets folder last changed, which a build does; None without one."""
    try:
        return None if root is None else (root / "assets").stat().st_mtime_ns
    except OSError:
        return None


@dataclass(frozen=True)
class Asset:
    file: Path
    copies: dict[str, Path]
    """Its precompressed copies by content coding, in the order they are preferred."""

    def pick(self, accept: str | None) -> tuple[str | None, Path]:
        """The copy to send for an Accept-Encoding header, with its coding; the file itself
        when the header allows none of them."""
        for coding in allowed(accept, self.copies):
            return coding, self.copies[coding]
        return None, self.file


def _assets(root: Path | None) -> tuple[int | None, dict[str, Asset]]:
    stamp = _stamp(root)
    folder = None if root is None else root / "assets"
    if folder is None or not folder.is_dir():
        return stamp, {}
    files = {f.name: f for f in folder.iterdir() if f.is_file()}
    copy = tuple(ext for _, ext in COPIES)
    return stamp, {
        name: Asset(file, {c: files[name + ext] for c, ext in COPIES if name + ext in files})
        for name, file in files.items()
        if not name.endswith(copy)
    }


def _spa(app: FastAPI, root: Path | None) -> None:
    """The UI shell and its assets, served without auth; the API behind them needs it. Assets
    are found by name in a listing of their folder, listed again only when the folder changed
    since the last listing, so a name that is not there costs a stat, not a listing. The build
    writes brotli and gzip copies next to each text asset; the one the request accepts is sent
    as it is, and the copies are never served by their own names."""
    stamp, assets = _assets(root)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request) -> Response:
        nonlocal stamp, assets
        if path.startswith(("api/", "runner/")) or path in ("api", "runner"):
            raise problems.ProblemException(404, "Not found", f"no route /{path}")
        if root is None:
            return Response(
                "The UI is not built: run `bun run build` in web/.", media_type="text/plain"
            )
        if path.startswith("assets/"):
            name = path.removeprefix("assets/")
            accept = request.headers.get("accept-encoding")
            asset = assets.get(name)
            there = asset is not None and asset.pick(accept)[1].is_file()
            if not there and _stamp(root) != stamp:
                stamp, assets = await asyncio.to_thread(_assets, root)
                asset = assets.get(name)
                there = asset is not None
            if asset is None or not there:
                raise problems.ProblemException(404, "Not found", f"no asset /{path}")
            coding, file = asset.pick(accept)
            headers = {"Cache-Control": "public, max-age=31536000, immutable"}
            if asset.copies:
                headers["Vary"] = "Accept-Encoding"
            if coding is not None:
                headers["Content-Encoding"] = coding
            return FileResponse(file, media_type=mimetypes.guess_type(name)[0], headers=headers)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


def openapi(app: FastAPI) -> dict[str, Any]:
    """The API schema, plus every event payload, a map from event type to payload, the
    event type enum and the stream message unions, so TypeScript gets all of them."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, routes=app.routes, description=__doc__
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    payloads = sorted(payload_types().items())
    _, defs = models_json_schema(
        [(cls, "serialization") for _, cls in payloads] + [(Problem, "serialization")],
        ref_template=REF,
    )
    for name, value in defs.get("$defs", {}).items():
        known = components.get(name)
        if known is not None and set(known.get("properties", {})) != set(
            value.get("properties", {})
        ):
            raise RuntimeError(f"two different schemas are named {name}")
        components.setdefault(name, value)
    components["EventTypes"] = {
        "type": "object",
        "description": "Each event type and the schema of its `data`.",
        "properties": {t: {"$ref": REF.format(model=cls.__name__)} for t, cls in payloads},
        "required": [t for t, _ in payloads],
    }
    components["EventType"] = {"type": "string", "enum": [t for t, _ in payloads]}
    for name, union in (("StreamMessage", StreamMessage), ("RunnerFeed", RunnerFeed)):
        adapter: TypeAdapter[Any] = TypeAdapter(union)
        value = adapter.json_schema(ref_template=REF, mode="serialization")
        for sub, body in value.pop("$defs", {}).items():
            components.setdefault(sub, body)
        components[name] = value
    _problems_for_validation(schema)
    _media_type_refusals(app, schema)
    app.openapi_schema = schema
    return schema


def _media_type_refusals(app: FastAPI, schema: dict[str, Any]) -> None:
    """A person's role check answers 415 to a write whose body has another media type
    (`auth.require`). A runner's role check does not."""
    people = {get_args(role)[1].dependency for role in (api.Viewer, api.Operator, api.Uploader)}
    content = {problems.MEDIA: {"schema": {"$ref": REF.format(model="Problem")}}}
    refused = {"description": problems.TITLES[415], "content": content}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if not any(d.call in people for d in route.dependant.dependencies):
            continue
        for method in (route.methods or set()) - SAFE:
            operation = schema["paths"][context.path_format][method.lower()]
            operation["responses"].setdefault("415", refused)


def _problems_for_validation(schema: dict[str, Any]) -> None:
    """Validation errors are answered as problem details, not FastAPI's default body. Every
    route that takes a body can also answer 413 (`limits.BodyLimit`)."""
    content = {problems.MEDIA: {"schema": {"$ref": REF.format(model="Problem")}}}
    problem = {"description": "Invalid request", "content": content}
    too_large = {"description": problems.TITLES[413], "content": content}
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            replies = operation.get("responses", {})
            if "422" in replies or operation.get("parameters") or operation.get("requestBody"):
                replies["422"] = problem
            if operation.get("requestBody"):
                replies.setdefault("413", too_large)
    components = schema["components"]["schemas"]
    text = json.dumps(schema["paths"])
    for name in ("HTTPValidationError", "ValidationError"):
        if REF.format(model=name) not in text:
            components.pop(name, None)
