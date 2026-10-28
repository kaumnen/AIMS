"""A control plane on loopback for tests: real uvicorn, a project with a scripted profile and a
scenario, and tokens of each role."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import uvicorn
from fastapi import FastAPI

from aims.server.app import create_app
from aims.server.serve import _port, _Server
from aims.server.state import ServerState, close_state, open_state

CONFIG = """\
# Test project.
[profiles.scripted]
backend = "chat"
base_url = "http://model.invalid/v1"
model = "m"
weights = "w"
reasoning = "full"
stream = false

[profiles.scripted.retries]
attempts = 1
backoff = 0
max = 0

[profiles.scripted.price]
input = 1.0
cached = 0.1
output = 2.0

[profiles.keyed]
extends = "scripted"
api_key_env = "AIMS_TEST_KEY_THAT_IS_NOT_SET"

[roles]
agent = "scripted"
"""

SCENARIO = """\
version: 1
name: control
oversight: observe
task:
  instruction: Do what you are told.
  environment: {image: python:3.13-slim}
  verifier:
    command: echo 1 > "$AIMS_LOGS_DIR/reward.txt"
agents:
  - id: a
"""


@dataclass
class CP:
    url: str
    state: ServerState
    project: Path
    operator: str
    viewer: str
    runner: str
    server: uvicorn.Server
    task: asyncio.Task[None]

    def client(self, token: str | None = None) -> httpx2.AsyncClient:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return httpx2.AsyncClient(base_url=self.url, headers=headers, timeout=30, trust_env=False)


def write_project(root: Path) -> Path:
    project = root / "aims.toml"
    root.mkdir(parents=True, exist_ok=True)
    project.write_text(CONFIG)
    scenario = root / "scenarios" / "control"
    scenario.mkdir(parents=True)
    (scenario / "scenario.yaml").write_text(SCENARIO)
    return project


async def start(app: FastAPI, port: int = 0) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=1,
    )
    server = _Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
    return server, task, _port(server, port)


async def stop(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.timeout(10):
            await task


def write_ui(root: Path) -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><script type="module" src="/assets/a.js">')
    (root / "assets" / "a.js").write_text("export {};\n")
    return root


@contextlib.asynccontextmanager
async def control_plane(tmp: Path, *, auth: bool = True) -> AsyncGenerator[CP]:
    project = write_project(tmp / "project")
    ui = write_ui(tmp / "ui")
    state = await open_state(tmp / "server", project=project, auth=auth)
    try:
        operator = await state.db.create_token("op", "operator")
        viewer = await state.db.create_token("viewer", "viewer")
        runner = await state.db.create_token("r1", "runner")
        server, task, port = await start(create_app(state, ui=ui))
        try:
            yield CP(
                f"http://127.0.0.1:{port}",
                state,
                project,
                operator,
                viewer,
                runner,
                server,
                task,
            )
        finally:
            await stop(server, task)
    finally:
        await close_state(state)
