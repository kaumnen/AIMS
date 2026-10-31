"""The control plane: API, live streams, runner ingest and the UI, in one ASGI app. It never
touches Docker; runners do."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aims import __version__
from aims.events.schema import payload_types
from aims.server import (
    api,
    dispatch,
    edit_api,
    estimate_api,
    monitor_api,
    problems,
    runner_api,
    score_api,
    stream,
)
from aims.server.auth import SecurityHeaders
from aims.server.files import Sources
from aims.server.problems import Problem
from aims.server.state import ServerState
from aims.wire import RunnerFeed, StreamMessage

LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")
REF = "#/components/schemas/{model}"


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
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
    problems.install(app)
    app.include_router(api.router)
    app.include_router(edit_api.router)
    app.include_router(monitor_api.router)
    app.include_router(estimate_api.router)
    app.include_router(score_api.router)
    app.include_router(stream.router)
    app.include_router(runner_api.router)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK, *hosts])
    root = ui if ui is not None and (ui / "index.html").is_file() else ui_root()
    _spa(app, root)
    return app


def _spa(app: FastAPI, root: Path | None) -> None:
    """The UI shell and its assets, served without auth; the API behind them needs it."""
    assets: dict[str, Path] = {}
    if root is not None and (root / "assets").is_dir():
        assets = {f.name: f for f in (root / "assets").iterdir() if f.is_file()}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request) -> Response:
        if path.startswith(("api/", "runner/")) or path in ("api", "runner"):
            raise problems.ProblemException(404, "Not found", f"no route /{path}")
        if root is None:
            return Response(
                "The UI is not built: run `bun run build` in web/.", media_type="text/plain"
            )
        if path.startswith("assets/"):
            file = assets.get(path.removeprefix("assets/"))
            if file is None:
                raise problems.ProblemException(404, "Not found", f"no asset /{path}")
            return FileResponse(
                file, headers={"Cache-Control": "public, max-age=31536000, immutable"}
            )
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
    app.openapi_schema = schema
    return schema


def _problems_for_validation(schema: dict[str, Any]) -> None:
    """Validation errors are answered as problem details, not FastAPI's default body."""
    problem = {
        "description": "Invalid request",
        "content": {problems.MEDIA: {"schema": {"$ref": REF.format(model="Problem")}}},
    }
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            replies = operation.get("responses", {})
            if "422" in replies or operation.get("parameters") or operation.get("requestBody"):
                replies["422"] = problem
    components = schema["components"]["schemas"]
    text = json.dumps(schema["paths"])
    for name in ("HTTPValidationError", "ValidationError"):
        if REF.format(model=name) not in text:
            components.pop(name, None)
