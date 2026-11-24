import ast
import asyncio
import contextlib
import dataclasses
import gc
import gzip
import hashlib
import inspect
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import textwrap
import threading
import time
import typing
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Sequence
from compression import zstd
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, Literal, cast
from unittest.mock import patch

import anyio
import httpx2
import pytest
from cp import CP, SCENARIO, control_plane, start, stop
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel, JsonValue, ValidationError
from starlette.types import Message, Scope

import aims
from aims import __version__
from aims.config import STARTER
from aims.events import (
    AgentInput,
    BlobRef,
    Draft,
    Event,
    LocalEventStore,
    ModelResponse,
    ResourceSample,
    RunFinished,
    RunStarted,
    ServiceRequest,
    TaskScored,
    ToolCall,
    chain,
    split_lines,
)
from aims.events.blobs import ZSTD_MAGIC, decode, decoded
from aims.events.chain import MAX_LINE
from aims.events.index import Index
from aims.events.schema import blob_refs
from aims.models import DeltaBus, Profile
from aims.models.keys import KeysError, parse
from aims.node import shipper as shipper_module
from aims.node.client import FEED, ApiError, ControlClient
from aims.node.link import Link
from aims.node.service import Node, NodeOptions
from aims.node.shipper import WARN_AFTER, Shipper
from aims.prompts import Prompts
from aims.run.job import Job
from aims.run.runner import _reward, resolve_texts
from aims.scenario import Bundle, load
from aims.scenario import parse as parse_scenario
from aims.server import api, dispatch, edit_api, runner_api
from aims.server import app as app_module
from aims.server import db as sdb
from aims.server.app import create_app
from aims.server.auth import HEADERS
from aims.server.db import UI_TOKEN
from aims.server.files import builtin_root
from aims.server.problems import ProblemError, ProblemException
from aims.server.state import CODES, InFlight, LaunchCodes, close_state, open_state
from aims.server.stream import DELTA_BATCH, parse_id
from aims.text import LONE, NoLoneSurrogate
from aims.wire import (
    Ack,
    CommandAction,
    CommandMessage,
    Deltas,
    MonitorJob,
    OfferMessage,
    RunnerHello,
    RunnerStatus,
    ScoreJob,
    TracerHealth,
    WireDelta,
)

pytestmark = pytest.mark.anyio

STARTED = RunStarted(scenario="pushed", oversight="observe", agents=["a1"])


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


def call(n: int) -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments="{}"), "a1")


# What a runner allows to call the keyed profiles of the tests: tests/profiles.toml and the
# profiles that extend tests/cp.py's scripted one read MODEL_API_KEY.
MODEL_KEYS = ["MODEL_API_KEY=https://api.example.com/v1", "MODEL_API_KEY=http://model.invalid/v1"]


class FakeRunner:
    """A runner without Docker: it says hello, reads its stream and ships runs it makes."""

    def __init__(self, cp: CP, name: str = "fake", *, keys: list[str] | None = None) -> None:
        """`keys` are the AIMS_KEYS entries it says it allows with their variables set."""
        self.client = ControlClient(cp.url, cp.runner, runner=name)
        self.name = name
        self.keys = keys or []
        self.feed: asyncio.Queue[Any] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.last_id: str | None = None

    def hello_body(self, jobs: list[str] | None = None) -> RunnerHello:
        return RunnerHello(
            name=self.name,
            version=__version__,
            arch="arm64",
            tracer=TracerHealth(alive=True),
            keys=self.keys,
            slots=2,
            jobs=jobs or [],
        )

    async def connect(self) -> None:
        await self.client.hello(self.hello_body())
        await self.client.status(RunnerStatus(tracer=TracerHealth(alive=True), free=2))
        ready = asyncio.Event()

        async def read() -> None:
            async with self.client.stream(self.last_id) as source:
                ready.set()
                async for sse in source:
                    if sse.event in ("command", "offer"):
                        if sse.event == "command":
                            self.last_id = sse.id
                        await self.feed.put(FEED.validate_json(sse.data))

        self.task = asyncio.create_task(read())
        async with asyncio.timeout(5):
            await ready.wait()
        await asyncio.sleep(0.1)

    async def disconnect(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        await asyncio.sleep(0.1)

    async def next(self, kind: type[Any], within: float = 10) -> Any:
        async with asyncio.timeout(within):
            while True:
                message = await self.feed.get()
                if isinstance(message, kind):
                    return message

    async def close(self) -> None:
        await self.disconnect()
        await self.client.aclose()


@pytest.fixture
async def fake(cp: CP) -> AsyncIterator[FakeRunner]:
    runner = FakeRunner(cp)
    await runner.connect()
    yield runner
    await runner.close()


async def until(probe: Any, want: Any, within: float = 10) -> None:
    """Poll `probe()` until its value is `want`, or `want(value)` is true."""
    async with asyncio.timeout(within):
        while True:
            value = await probe()
            if want(value) if callable(want) else value == want:
                return
            await asyncio.sleep(0.05)


async def job_state(cp: CP, run_id: str) -> str | None:
    job = await cp.state.db.job(run_id)
    return None if job is None else job.state


async def ship(store: LocalEventStore, client: ControlClient, run_id: str) -> Shipper:
    shipper = Shipper(store, client, run_id)
    async with asyncio.timeout(20):
        await shipper.run()
    return shipper


# Auth, hosts, headers


async def test_tokens_roles_and_hosts(cp: CP) -> None:
    async with cp.client() as anon, cp.client(cp.viewer) as viewer, cp.client(cp.runner) as rt:
        r = await anon.get("/api/runs")
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "unauthenticated"
        assert (await viewer.get("/api/runs")).status_code == 200
        r = await viewer.post("/api/runs/import", content=b"x")
        assert r.status_code == 403
        assert (await rt.get("/api/runs")).status_code == 403
        r = await viewer.get("/api/runs", headers={"Host": "evil.example"})
        assert r.status_code == 400
        r = await viewer.get("/api/runs", headers={"Host": "127.0.0.1.nip.io"})
        assert r.status_code == 400
        assert (await viewer.get("/api/runs", headers={"Host": "localhost:9"})).status_code == 200


async def test_security_headers_on_api_and_shell(cp: CP) -> None:
    async with cp.client(cp.viewer) as c:
        for path in ("/api/health", "/", "/runs/x"):
            r = await c.get(path)
            assert r.status_code == 200, path
            csp = r.headers["content-security-policy"]
            assert "script-src 'self'" in csp
            assert "object-src 'none'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "base-uri 'none'" in csp
            assert r.headers["x-content-type-options"] == "nosniff"
            assert "access-control-allow-origin" not in r.headers
        assert (await c.get("/api/nope")).status_code == 404


async def test_healthz_needs_no_token_and_tells_nothing_else(cp: CP) -> None:
    async with cp.client() as anon:
        for host in ("127.0.0.1:8484", "localhost"):
            r = await anon.get("/healthz", headers={"Host": host})
            assert r.status_code == 200, host
            assert r.headers["content-type"] == "application/json"
            assert r.content == b'{"ok":true}'
            assert r.headers["x-content-type-options"] == "nosniff"
        assert (await anon.get("/healthz", headers={"Host": "evil.example"})).status_code == 400
        assert (await anon.post("/healthz")).status_code == 405
        assert (await anon.get("/api/health")).status_code == 401
    async with cp.client(cp.viewer) as viewer:
        assert (await viewer.get("/api/health")).json()["ok"] is True
    server, task, port = await start(create_app(cp.state, hosts=("server",), dispatcher=False))
    try:
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            r = await c.get("/healthz", headers={"Host": "server:8484"})
            assert (r.status_code, r.json()) == (200, {"ok": True})
    finally:
        await stop(server, task)


async def test_the_test_control_plane_stops_a_server_restarted_into_it(tmp_path: Path) -> None:
    async with control_plane(tmp_path) as cp:
        port = int(cp.url.rsplit(":", 1)[1])
        await stop(cp.server, cp.task)
        cp.server, cp.task, _ = await start(create_app(cp.state, dispatcher=False), port)
    assert cp.task.done()


async def test_the_api_schema_needs_a_viewer_token(cp: CP, tmp_path: Path) -> None:
    async with cp.client() as anon:
        r = await anon.get("/api/openapi.json")
        assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
        assert __version__ not in r.text
        assert all(__version__ not in v for v in r.headers.values())
    async with cp.client(cp.runner) as runner:
        assert (await runner.get("/api/openapi.json")).status_code == 403
    for token in (cp.viewer, cp.operator):
        async with cp.client(token) as c:
            spec = (await c.get("/api/openapi.json")).json()
            assert spec["info"]["version"] == __version__
            assert "/healthz" not in spec["paths"]
            assert "/api/openapi.json" not in spec["paths"]
    async with control_plane(tmp_path / "open", auth=False) as open_cp, open_cp.client() as c:
        assert (await c.get("/api/openapi.json")).status_code == 200


async def test_a_session_cookie_needs_the_same_origin_to_act(cp: CP) -> None:
    async with cp.client() as c:
        r = await c.post("/api/session", json={"token": cp.runner})
        assert r.status_code == 401
        r = await c.post("/api/session", json={"token": cp.operator})
        assert r.status_code == 204
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
        assert (await c.get("/api/session")).json()["name"] == "op"
        body = {"name": "x", "role": "viewer"}
        assert (await c.post("/api/tokens", json=body)).status_code == 403
        r = await c.post("/api/tokens", json=body, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/tokens", json=body, headers={"Origin": cp.url})
        assert r.status_code == 201
        r = await c.post(
            "/api/tokens",
            json={"name": "y", "role": "viewer"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert r.status_code == 201
        form = {"Origin": cp.url, "Content-Type": "application/x-www-form-urlencoded"}
        r = await c.post("/api/tokens", content=b"name=z&role=viewer", headers=form)
        assert r.status_code == 415
        await cp.state.db.revoke_token("op")
        assert (await c.get("/api/session")).status_code == 401


async def test_two_control_planes_on_one_host_keep_their_own_sessions(
    cp: CP, tmp_path: Path
) -> None:
    # a browser sends a host's cookies to every port of it
    async with (
        control_plane(tmp_path / "other") as other,
        httpx2.AsyncClient(timeout=30, trust_env=False) as c,
    ):
        for plane in (cp, other):
            r = await c.post(f"{plane.url}/api/session", json={"token": plane.viewer})
            assert r.status_code == 204
        for plane in (cp, other):
            assert (await c.get(f"{plane.url}/api/session")).status_code == 200


async def test_tokens_list_marks_the_ui_token_and_names_stay_taken(cp: CP) -> None:
    await cp.state.db.rotate_token(UI_TOKEN, "operator")
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/tokens", json={"name": "gone", "role": "viewer"})
        assert r.status_code == 201
        assert (await c.delete("/api/tokens/gone")).status_code == 204
        assert (await c.delete("/api/tokens/gone")).status_code == 404
        r = await c.post("/api/tokens", json={"name": "gone", "role": "runner"})
        assert r.status_code == 409
        listed = {t["name"]: t for t in (await c.get("/api/tokens")).json()}
        assert listed[UI_TOKEN]["ui"] is True
        assert listed["op"]["ui"] is False
        assert listed["gone"]["revoked"] is not None
        assert listed["gone"]["role"] == "viewer"


async def test_a_launch_code_signs_in_once_and_expires(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = cp.state.codes.issue("op")
    async with cp.client() as c:
        for body in ({}, {"token": cp.operator, "code": code}):
            r = await c.post("/api/session", json=body)
            assert (r.status_code, r.json()["code"]) == (422, "invalid")
        assert (await c.post("/api/session", json={"code": "nope"})).status_code == 401
        assert (await c.post("/api/session", json={"code": code})).status_code == 204
        assert (await c.get("/api/session")).json()["name"] == "op"
    async with cp.client() as c:
        r = await c.post("/api/session", json={"code": code})
        assert (r.status_code, r.json()["code"]) == (401, "code")
        late = cp.state.codes.issue("op")
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 121)
        assert (await c.post("/api/session", json={"code": late})).status_code == 401
        monkeypatch.undo()
        assert (await c.post("/api/session", json={"code": late})).status_code == 401


async def test_problem_details_for_bad_input(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/runs", json={"scenario": {"source": "project"}})
        assert r.status_code == 422
        problem = r.json()
        assert problem["code"] == "invalid"
        assert problem["errors"][0]["loc"] == ["body", "scenario", "name"]


# Runner ingest


async def test_a_pushed_run_lands_byte_for_byte(cp: CP, local: LocalEventStore) -> None:
    run_id = await local.create_run(STARTED, blobs=[b"x" * 10_000])
    big = await local.content(run_id, "y" * 20_000)
    assert isinstance(big, BlobRef)
    await local.append(run_id, [Draft(AgentInput(kind="inject", step=0, text=big), "a1")])
    await local.append(run_id, [call(n) for n in range(2000)])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        with pytest.raises(ApiError) as caught:
            await client.heads([run_id])
        assert caught.value.code == "hello"
        await FakeRunner(cp, "pusher").client.hello(FakeRunner(cp, "pusher").hello_body())
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert shipper.done
    assert shipper.batches >= 3
    copy = cp.state.store.root / run_id / "events.jsonl"
    assert copy.read_bytes() == (local.root / run_id / "events.jsonl").read_bytes()
    assert (await cp.state.store.verify(run_id)).ok
    meta = await cp.state.db.run_meta(run_id)
    assert meta is not None
    assert (meta.runner, meta.source) == ("pusher", "push")
    async with cp.client(cp.viewer) as c:
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["status"], run["events"], run["runner"]) == ("completed", 2003, "pusher")
        r = await c.get(
            f"/api/runs/{run_id}/blobs/{big.sha256}", headers={"Accept-Encoding": "zstd"}
        )
        assert r.headers.get("content-encoding") == "zstd" or r.content == b"y" * 20_000
        r = await c.get(
            f"/api/runs/{run_id}/blobs/{big.sha256}", headers={"Accept-Encoding": "gzip"}
        )
        assert r.content == b"y" * 20_000
        r = await c.get(
            f"/api/runs/{run_id}/blobs/{big.sha256}",
            headers={"Accept-Encoding": "zstd;q=0, gzip"},
        )
        assert "content-encoding" not in r.headers
        assert r.content == b"y" * 20_000
        assert r.headers["content-disposition"].startswith("attachment")
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["etag"] == f'"{big.sha256}"'
        assert "immutable" in r.headers["cache-control"]
        r = await c.get(
            f"/api/runs/{run_id}/blobs/{big.sha256}", headers={"If-None-Match": f'"{big.sha256}"'}
        )
        assert r.status_code == 304
        page = (await c.get(f"/api/runs/{run_id}/events", params={"limit": 10})).json()
        assert [e["seq"] for e in page["items"]] == list(range(10))
        assert page["next"] == "9"
        found = (await c.get("/api/events", params={"q": "shell"})).json()
        assert found["items"][0]["run_id"] == run_id
        v = (await c.get(f"/api/runs/{run_id}/verification")).json()
        assert v["ok"]


FORGED_REWARDS = {
    "not hex": {"sha256": "not-a-sha", "size": 1},
    "a list": {"sha256": ["a"], "size": 1},
    "no such blob": {"sha256": "ab" * 32, "size": 1},
}


@pytest.mark.parametrize("forged", [*FORGED_REWARDS, "metadata headers"])
async def test_a_mapping_the_sandbox_shaped_as_a_blob_reference_does_not_stop_shipping(
    cp: CP, local: LocalEventStore, forged: str
) -> None:
    """The verifier's reward.json and the headers of a request to the metadata service are
    written in the sandbox. Shaped as a blob reference, they once stopped the run's shipping
    for good at that event."""
    run_id = await local.create_run(STARTED)
    if forged in FORGED_REWARDS:
        body = json.dumps({"reward": 1, "x": FORGED_REWARDS[forged]}).encode()
        reward, metrics, source, error = _reward(b"J", body)
        payload: Any = TaskScored(reward=reward, metrics=metrics, source=source, error=error)
    else:
        headers = {"sha256": "cd" * 32, "size": "5"}
        payload = ServiceRequest(
            service="metadata", time="t", method="GET", path="/", headers=headers, status=200
        )
    await local.append(run_id, [Draft(payload)])
    await local.append(run_id, [call(1), call(2)])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        await client.hello(FakeRunner(cp, "pusher").hello_body())
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert (shipper.done, shipper.refused) == (True, None)
    copy = cp.state.store.root / run_id / "events.jsonl"
    assert copy.read_bytes() == (local.root / run_id / "events.jsonl").read_bytes()
    assert (await cp.state.store.verify(run_id)).ok


async def test_a_profile_table_shaped_as_a_blob_reference_does_not_stop_shipping(
    cp: CP, local: LocalEventStore
) -> None:
    """A profile's `extra` table and its `headers` are what a person wrote in aims.toml.
    run.started records the profile wherever the run uses it. Shaped as a blob reference,
    either once stopped the run's shipping for good. A real reference in the same event still
    needs its blob."""
    extra: dict[str, Any] = {"vendor": {"sha256": "ab" * 32, "size": 3}}
    profile = Profile.model_validate(
        {
            "name": "p",
            "backend": "chat",
            "base_url": "http://model.invalid/v1",
            "model": "m",
            "weights": "w",
            "reasoning": "full",
            "extra": extra,
            "headers": {"sha256": "cd" * 32, "size": "5"},
        }
    ).snapshot()
    data = b"the scenario"
    real = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    config: dict[str, Any] = {
        "scenario": {"file": real.model_dump()},
        "agents": {"a1": {"profile": profile, "schedule": [[None, profile]]}},
        "monitor": {"sweeps": {"s": profile}, "review": None, "judge": profile},
        "classifier": profile,
    }
    started = STARTED.model_copy(update={"config": config})
    run_id = await local.create_run(started, blobs=[data])
    [line] = (local.root / run_id / "events.jsonl").read_bytes().splitlines()
    first = json.loads(line)
    assert list(blob_refs(first["type"], first["data"])) == [real.sha256]
    await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        await client.hello(FakeRunner(cp, "pusher").hello_body())
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert (shipper.done, shipper.refused) == (True, None)
    copy = cp.state.store.root / run_id / "events.jsonl"
    assert copy.read_bytes() == (local.root / run_id / "events.jsonl").read_bytes()
    assert (await cp.state.store.verify(run_id)).ok


async def test_a_plugin_option_shaped_as_a_blob_reference_does_not_stop_shipping(
    cp: CP, local: LocalEventStore
) -> None:
    """run.started records the scenario as resolved from its file, which a person wrote, with
    the references to its inputs beside it. A plugin channel's options shaped as a blob
    reference once stopped the run's shipping for good."""
    channel = '  - {name: radio, kind: walkie, options: {sha256: "' + "ef" * 32 + '", size: 2}}\n'
    text = SCENARIO + "channels:\n" + channel
    bundle = Bundle(parse_scenario(text), text.encode())
    started = STARTED.model_copy(update={"config": {"scenario": bundle.snapshot()}})
    run_id = await local.create_run(started, blobs=bundle.blobs())
    [line] = (local.root / run_id / "events.jsonl").read_bytes().splitlines()
    first = json.loads(line)
    assert first["data"]["config"]["scenario"]["resolved"]["channels"][0]["options"] == {
        "sha256": "ef" * 32,
        "size": 2,
    }
    assert list(blob_refs(first["type"], first["data"])) == [
        hashlib.sha256(text.encode()).hexdigest()
    ]
    await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        await client.hello(FakeRunner(cp, "pusher").hello_body())
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert (shipper.done, shipper.refused) == (True, None)
    copy = cp.state.store.root / run_id / "events.jsonl"
    assert copy.read_bytes() == (local.root / run_id / "events.jsonl").read_bytes()
    assert (await cp.state.store.verify(run_id)).ok


async def test_a_plugin_signal_option_shaped_as_a_blob_reference_does_not_stop_shipping(
    cp: CP, local: LocalEventStore
) -> None:
    """run.started records the scenario's monitor block in the scenario as resolved and again
    merged into the monitor's config. A plugin signal takes its options as a person wrote
    them. One shaped as a blob reference once stopped the run's shipping for good."""
    from aims.monitor.setup import resolve_config

    ref = '{sha256: "' + "ef" * 32 + '", size: 2}'
    signal = "    my_plugin:\n      options: {known_file: " + ref + "}\n"
    text = SCENARIO + "monitor:\n  signals:\n" + signal
    scenario = parse_scenario(text)
    bundle = Bundle(scenario, text.encode())
    monitor = {"config": resolve_config([], scenario).model_dump(mode="json")}
    config = {"scenario": bundle.snapshot(), "monitor": monitor}
    run_id = await local.create_run(
        STARTED.model_copy(update={"config": config}), blobs=bundle.blobs()
    )
    [line] = (local.root / run_id / "events.jsonl").read_bytes().splitlines()
    first = json.loads(line)
    signals = first["data"]["config"]["monitor"]["config"]["signals"]
    assert signals["my_plugin"]["options"] == {"known_file": {"sha256": "ef" * 32, "size": 2}}
    assert list(blob_refs(first["type"], first["data"])) == [
        hashlib.sha256(text.encode()).hexdigest()
    ]
    await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    client = ControlClient(cp.url, cp.runner, runner="pusher")
    try:
        await client.hello(FakeRunner(cp, "pusher").hello_body())
        shipper = await ship(local, client, run_id)
    finally:
        await client.aclose()
    assert (shipper.done, shipper.refused) == (True, None)
    copy = cp.state.store.root / run_id / "events.jsonl"
    assert copy.read_bytes() == (local.root / run_id / "events.jsonl").read_bytes()
    assert (await cp.state.store.verify(run_id)).ok


async def raw_events(
    cp: CP, runner: str, run_id: str, lines: list[bytes], token: str | None = None
) -> httpx2.Response:
    async with cp.client(token or cp.runner) as c:
        return await c.post(
            f"/runner/v1/runs/{run_id}/events",
            content=zstd.compress(b"".join(lines)),
            headers={
                "Content-Type": "application/x-ndjson",
                "Content-Encoding": "zstd",
                "X-Aims-Runner": runner,
            },
        )


async def test_ingest_refuses_gaps_divergence_and_missing_blobs(
    cp: CP, fake: FakeRunner, tmp_path: Path
) -> None:
    run_id = str(uuid.uuid7())
    async with (
        await LocalEventStore.open(tmp_path / "a") as a,
        await LocalEventStore.open(tmp_path / "b") as b,
    ):
        await a.create_run(STARTED, run_id=run_id)
        await a.append(run_id, [call(1), call(2)])
        await b.create_run(STARTED, run_id=run_id)
        await b.append(run_id, [call(9), call(2)])
        lines = split_lines((a.root / run_id / "events.jsonl").read_bytes())
        other = split_lines((b.root / run_id / "events.jsonl").read_bytes())
    r = await raw_events(cp, "fake", run_id, lines[:1])
    assert r.status_code == 200
    assert r.json()["head"]["seq"] == 0
    r = await raw_events(cp, "fake", run_id, lines[2:])
    assert (r.status_code, r.json()["code"], r.json()["head"]["seq"]) == (409, "gap", 0)
    r = await raw_events(cp, "fake", run_id, lines[:2])
    assert r.json()["head"]["seq"] == 1
    r = await raw_events(cp, "fake", run_id, other[1:])
    assert (r.status_code, r.json()["code"]) == (409, "diverged")
    meta = await cp.state.db.run_meta(run_id)
    assert meta is not None
    assert meta.diverged is not None
    async with cp.client(cp.viewer) as c:
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert run["diverged"]
    other_token = await cp.state.db.create_token("r2", "runner")
    other_runner = FakeRunner(cp, "other")
    other_runner.client = ControlClient(cp.url, other_token, runner="other")
    await other_runner.client.hello(other_runner.hello_body())
    r = await raw_events(cp, "other", run_id, lines[2:], other_token)
    assert (r.status_code, r.json()["code"]) == (403, "owner")
    await other_runner.client.aclose()

    ref = BlobRef(sha256="ab" * 32, size=3)
    async with await LocalEventStore.open(tmp_path / "c") as c_store:
        rid = await c_store.create_run(STARTED)
        await c_store.append(rid, [Draft(AgentInput(kind="inject", step=0, text=ref), "a1")])
        c_lines = split_lines((c_store.root / rid / "events.jsonl").read_bytes())
    r = await raw_events(cp, "fake", rid, c_lines)
    assert (r.status_code, r.json()["code"], r.json()["missing"]) == (
        422,
        "missing_blobs",
        [ref.sha256],
    )
    assert r.json()["head"]["seq"] == -1


@pytest.mark.parametrize("sha256", ["x" * 64, "A" * 64, 5, ["a"], {"k": 1}])
async def test_ingest_of_a_reference_that_names_no_sha256_is_invalid_not_a_crash(
    cp: CP, fake: FakeRunner, sha256: object
) -> None:
    run_id = str(uuid.uuid7())
    head = chain.Head()
    lines: list[bytes] = []
    for kind, data in (
        (STARTED.type, STARTED.model_dump(mode="json")),
        ("x.custom", {"refs": [{"sha256": sha256, "size": 1}, {"sha256": "ab" * 32, "size": 1}]}),
    ):
        ts = "2026-11-23T00:00:00.000000Z"
        _, line, head = chain.seal(head, run_id=run_id, ts=ts, agent_id=None, type=kind, data=data)
        lines.append(line)
    r = await raw_events(cp, "fake", run_id, lines)
    assert (r.status_code, r.json()["code"], r.json()["head"]["seq"]) == (422, "invalid", -1)


@pytest.mark.parametrize("sha256", ["x" * 64, ["a"]])
async def test_a_hand_made_log_that_names_a_blob_by_no_sha256_is_refused_not_a_crash(
    cp: CP, tmp_path: Path, sha256: object
) -> None:
    root = tmp_path / "hand"
    async with await LocalEventStore.open(root) as store:
        run_id = await store.create_run(STARTED)
    path = root / run_id / "events.jsonl"
    last = json.loads(path.read_bytes().splitlines()[-1])
    head = chain.Head(seq=last["seq"], hash=last["hash"], offset=path.stat().st_size)
    ts = "2026-11-23T00:00:00.000000Z"
    data = {"ref": {"sha256": sha256, "size": 1}}
    _, line, _ = chain.seal(head, run_id=run_id, ts=ts, agent_id=None, type="x.custom", data=data)
    with path.open("ab") as f:
        f.write(line)
    async with await LocalEventStore.open(root) as store:
        client = ControlClient(cp.url, cp.runner, runner="pusher")
        try:
            await client.hello(FakeRunner(cp, "pusher").hello_body())
            shipper = await ship(store, client, run_id)
        finally:
            await client.aclose()
    assert shipper.refused is not None
    assert "event 1 names a blob by no sha256" in shipper.refused


async def test_a_wrong_version_runner_is_refused(cp: CP) -> None:
    client = ControlClient(cp.url, cp.runner, runner="old")
    hello = FakeRunner(cp, "old").hello_body().model_copy(update={"version": "0.0.1"})
    with pytest.raises(ApiError) as caught:
        await client.hello(hello)
    assert caught.value.code == "version"
    await client.aclose()


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aims.node.shipper.BACKOFF_MIN_S", 0.0)


RETRIED = ["ConnectTimeout", "409 Conflict: say hello first", "503 Busy"]


def retried(n: int) -> list[httpx2.Response | Exception]:
    """`n` failures a shipper retries: a timeout, no hello yet and a busy server, in turn."""

    def one(i: int) -> httpx2.Response | Exception:
        match i % 3:
            case 0:
                return httpx2.ConnectTimeout("")
            case 1:
                hello = {"title": "Conflict", "detail": "say hello first", "code": "hello"}
                return httpx2.Response(409, json=hello)
            case _:
                return httpx2.Response(503, json={"title": "Busy"})

    return [one(i) for i in range(n)]


async def ship_through(
    local: LocalEventStore, run_id: str, failures: list[httpx2.Response | Exception]
) -> Shipper:
    """Ship `run_id` to a control plane that fails each call with `failures` in turn, then
    holds the whole run."""
    head = await local.head(run_id)
    assert head is not None
    held = {"heads": {run_id: {"seq": head.seq, "hash": head.hash, "truth": True}}}
    replies = iter(failures)

    def handle(request: httpx2.Request) -> httpx2.Response:
        reply = next(replies, None)
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else httpx2.Response(200, json=held)

    transport = httpx2.MockTransport(handle)
    client = ControlClient("http://cp.test", "token", runner="r", transport=transport)
    try:
        return await ship(local, client, run_id)
    finally:
        await client.aclose()


def said(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "aims.node"]


@pytest.mark.parametrize(
    ("fails", "warned"),
    [
        (WARN_AFTER - 1, []),
        (WARN_AFTER, [WARN_AFTER]),
        (2 * WARN_AFTER - 1, [WARN_AFTER]),
        (4 * WARN_AFTER, [WARN_AFTER, 2 * WARN_AFTER, 4 * WARN_AFTER]),
    ],
)
async def test_a_run_that_keeps_failing_to_ship_says_why_now_and_then(
    local: LocalEventStore,
    no_backoff: None,
    caplog: pytest.LogCaptureFixture,
    fails: int,
    warned: list[int],
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    with caplog.at_level(logging.INFO, logger="aims.node"):
        shipper = await ship_through(local, run_id, retried(fails))
    assert (shipper.done, shipper.truthful, shipper.refused) == (True, True, None)
    want = [
        ("WARNING", f"run {run_id}: shipping failed {n} times in a row: {RETRIED[(n - 1) % 3]}")
        for n in warned
    ]
    if warned:
        want.append(("WARNING", f"run {run_id}: shipping works again after {fails} failures"))
    assert said(caplog) == want


@pytest.mark.parametrize("fails", [0, WARN_AFTER])
async def test_a_refused_run_says_so_once_after_any_failures(
    local: LocalEventStore, no_backoff: None, caplog: pytest.LogCaptureFixture, fails: int
) -> None:
    run_id = await local.create_run(STARTED)
    owner = {"title": "Forbidden", "detail": "not yours", "code": "owner"}
    with caplog.at_level(logging.INFO, logger="aims.node"):
        shipper = await ship_through(
            local, run_id, [*retried(fails), httpx2.Response(403, json=owner)]
        )
    assert shipper.refused == "403 Forbidden: not yours"
    last = RETRIED[(WARN_AFTER - 1) % 3]
    warned = [("WARNING", f"run {run_id}: shipping failed {fails} times in a row: {last}")]
    assert said(caplog) == [
        *(warned if fails else []),
        ("ERROR", f"run {run_id}: shipping refused: 403 Forbidden: not yours"),
    ]


# Jobs and commands


async def start_run(cp: CP, **body: Any) -> list[str]:
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={
                "scenario": {"source": "project", "name": "control"},
                "monitor": "signals",
                **body,
            },
        )
        assert r.status_code == 202, r.text
        return r.json()["run_ids"]


async def play_job(
    fake: FakeRunner, local: LocalEventStore, offer: OfferMessage
) -> Job | MonitorJob | ScoreJob:
    """Take an offered job as a runner would, and record a run for it."""
    job = await fake.client.claim(offer.run_id)
    assert job is not None
    blobs = [await fake.client.job_blob(job.run_id, sha) for sha in job.blobs()]
    datas = [decode(b) for b in blobs]
    assert job.scenario is not None
    started = RunStarted(scenario="control", oversight="observe", config={"scenario": job.scenario})
    await local.create_run(started, blobs=datas, run_id=job.run_id)
    return job


async def test_a_job_is_offered_claimed_run_and_done(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    offer = await fake.next(OfferMessage)
    assert offer.run_id == run_id
    assert offer.needs == {"keys": [], "kernel": True}
    thief = FakeRunner(cp, "thief")
    assert await thief.client.claim(run_id) is None
    await thief.client.aclose()
    job = await play_job(fake, local, offer)
    assert job.run_id == run_id
    assert (await fake.client.claim(run_id)) is None
    shipper = Shipper(local, fake.client, run_id)
    task = asyncio.create_task(shipper.run())
    await until(lambda: job_state(cp, run_id), "running")
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    async with asyncio.timeout(10):
        await task
    done = await cp.state.db.job(run_id)
    assert done is not None
    assert (done.state, done.reason) == ("done", "completed")
    async with cp.client(cp.viewer) as c:
        listed = (await c.get("/api/runs")).json()["items"]
        assert [(r["run_id"], r["status"], r["state"]) for r in listed] == [
            (run_id, "completed", "done")
        ]


@pytest.mark.parametrize("moved", ["running", "done"])
async def test_the_run_list_stream_sends_a_run_again_once_its_job_moves_on(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch, moved: str
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    if moved == "done":
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
    status = "completed" if moved == "done" else "running"
    read = asyncio.Event()
    set_job = cp.state.db.set_job

    async def late(run: str, state: Any, reason: str | None = None) -> bool:
        # a stream that reads the run between the ingest and the job's move, as a busy
        # database or a stream already awake can make it
        if state == moved:
            await read.wait()
        return await set_job(run, state, reason)

    monkeypatch.setattr(cp.state.db, "set_job", late)
    seen: list[tuple[str, str]] = []
    async with (
        cp.client(cp.viewer) as c,
        asyncio.timeout(10),
        c.sse("/api/stream", params={"runs": "true"}) as source,
    ):
        task = asyncio.create_task(ship(local, fake.client, run_id))
        async for e in source:
            if e.event != "run" or (run := json.loads(e.data)["run"])["run_id"] != run_id:
                continue
            seen.append((run["status"], run["state"]))
            if run["status"] == status:
                read.set()
            if run["state"] == moved:
                break
    assert seen[0] == (status, "running" if moved == "done" else "claimed")
    assert seen[-1] == (status, moved)
    if moved == "running":
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await task


async def test_a_shipper_ends_when_its_run_is_deleted_from_the_local_store(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shipper_module, "IDLE_S", 0.05)
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    link = Link(fake.client, local, DeltaBus())
    link.ship(run_id)
    shipper, task = link.shippers[run_id], link._ships[run_id]

    async def shipped() -> int:
        head = await cp.state.store.head(run_id)
        return -1 if head is None else head.seq

    await until(shipped, 1)
    # what a delete by another process on the same data folder leaves
    shutil.rmtree(local.root / run_id)
    await local._index.remove(run_id)
    async with asyncio.timeout(10):
        await task
    assert (shipper.gone, shipper.refused) == (True, "the run was deleted here")
    await asyncio.sleep(0)
    assert run_id not in link.shippers


async def test_a_shipper_whose_run_is_deleted_while_it_reads_a_blob_ends(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    ref = await local.put_blob(run_id, b"x" * 20000)
    await local.append(run_id, [Draft(ToolCall(call_id="c", name="n", arguments=ref), "a1")])
    shipper = Shipper(local, fake.client, run_id)
    missing = fake.client.missing

    async def then_deleted(*args: Any) -> Any:
        found = await missing(*args)
        shutil.rmtree(local.root / run_id)
        return found

    monkeypatch.setattr(fake.client, "missing", then_deleted)
    async with asyncio.timeout(10):
        await shipper.run()
    assert (shipper.gone, shipper.refused) == (True, "the run was deleted here")


async def test_a_shipper_ends_when_its_run_was_deleted_here_before_it_read_it(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server holds events of the run, so it is no run still to start."""
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    first = Shipper(local, fake.client, run_id)
    task = asyncio.create_task(first.run())

    async def shipped() -> int:
        head = await cp.state.store.head(run_id)
        return -1 if head is None else head.seq

    await until(shipped, 1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    shutil.rmtree(local.root / run_id)
    await local._index.remove(run_id)
    shipper = Shipper(local, fake.client, run_id)
    async with asyncio.timeout(10):
        await shipper.run()
    assert (shipper.gone, shipper.refused, shipper.streak.failures) == (
        True,
        "the run was deleted here",
        0,
    )


async def test_a_shipper_of_a_run_ship_all_found_ends_when_it_goes_before_the_first_read(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shipper_module, "IDLE_S", 0.05)
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    link = Link(fake.client, local, DeltaBus())
    heads = fake.client.heads
    asked = 0

    async def then_deleted(run_ids: Sequence[str]) -> Any:
        nonlocal asked
        asked += 1
        if asked == 2:
            # the shipper's first ask: the run was deleted since ship_all read it
            shutil.rmtree(local.root / run_id)
            await local._index.remove(run_id)
        return await heads(run_ids)

    monkeypatch.setattr(fake.client, "heads", then_deleted)
    await link.ship_all()
    shipper, task = link.shippers[run_id], link._ships[run_id]
    async with asyncio.timeout(10):
        await task
    assert (shipper.gone, shipper.refused) == (True, "the run was deleted here")
    assert await cp.state.store.head(run_id) is None


async def test_a_shipper_that_starts_before_its_run_waits_for_it(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shipper_module, "IDLE_S", 0.05)
    run_id = str(uuid.uuid7())
    shipper = Shipper(local, fake.client, run_id)
    task = asyncio.create_task(shipper.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    await local.create_run(STARTED, run_id=run_id)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    async with asyncio.timeout(10):
        await task
    assert (shipper.done, shipper.gone) == (True, False)


async def test_an_unclaimed_offer_goes_back_to_the_queue(
    cp: CP, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch, "CLAIM_S", 0.3)
    (run_id,) = await start_run(cp)
    first = await fake.next(OfferMessage)
    second = await fake.next(OfferMessage, within=5)
    assert first.run_id == second.run_id == run_id
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert job.tried[:2] == ["fake", "fake"]


async def test_the_dispatcher_prunes_sessions_now_and_then_and_lives_through_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    state = await open_state(tmp_path / "server")
    calls = 0
    twice = asyncio.Event()

    async def prune() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk full")
        twice.set()
        return 0

    monkeypatch.setattr(state.db, "prune_sessions", prune)
    monkeypatch.setattr(dispatch, "TICK_S", 0.01)
    monkeypatch.setattr(dispatch, "HOUSEKEEPING_S", 0.05)
    task = asyncio.create_task(dispatch.run(state))
    try:
        with (
            caplog.at_level(logging.ERROR, logger="aims.server"),
            contextlib.suppress(TimeoutError),
        ):
            async with asyncio.timeout(5):
                await twice.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await close_state(state)
    assert calls >= 2
    assert [r.getMessage() for r in caplog.records] == ["housekeeping failed"]


async def test_queued_runs_cancel_without_a_log_and_say_why_they_wait(cp: CP) -> None:
    fake = FakeRunner(cp)
    await fake.client.hello(fake.hello_body())
    (run_id,) = await start_run(cp)
    async with cp.client(cp.operator) as c:
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["state"], run["reason"]) == ("queued", "no runner is connected")
        r = await c.post(f"/api/runs/{run_id}/pause", json={})
        assert (r.status_code, r.json()["code"]) == (409, "not_started")
        r = await c.post(f"/api/runs/{run_id}/cancel", json={"reason": "not now"})
        assert r.json()["state"] == "cancelled"
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["state"], run["reason"]) == ("cancelled", "not now")
    assert await cp.state.store.run(run_id) is None
    await fake.client.aclose()


async def test_a_job_of_a_kind_this_version_does_not_know_breaks_no_list(
    cp: CP, fake: FakeRunner
) -> None:
    from aims.server.db import Job as JobRow

    (good,) = await start_run(cp)
    bad = "01a18000-0000-7000-8000-00000000000b"
    # as a newer version may store, before a downgrade
    payload = {"kind": "unknown"}
    now = time.time()
    row = JobRow(bad, None, None, "queued", payload, {}, None, None, None, [], None, now, now, None)
    await cp.state.db.add_jobs([row])
    cp.state.changes.touch(bad)
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/runs")
        assert r.status_code == 200, r.text
        assert [i["run_id"] for i in r.json()["items"]] == [good]
        assert (await c.get(f"/api/runs/{bad}")).status_code == 404
        # the bad row sorts first, so the stream would send it before the good one
        listed: list[str] = []
        params = {"runs": "true", "last_event_id": "@0"}
        async with asyncio.timeout(10), c.sse("/api/stream", params=params) as source:
            async for e in source:
                if e.event == "run":
                    listed.append(json.loads(e.data)["run"]["run_id"])
                    break
        assert listed == [good]


@pytest.mark.parametrize(
    ("payload", "tried", "needs", "state"),
    [
        ("[]", "[]", "{}", "waiting"),
        ('"x"', "[]", "{}", "queued"),
        ("null", "[]", "{}", "done"),
        ("1", "[]", "{}", "waiting"),
        ('{"kind": "unknown"}', "null", "{}", "queued"),
        # a payload that is not JSON cannot be stored: an index reads json_extract(payload)
        ('{"kind": "unknown"}', "[", "{}", "queued"),
        ('{"kind": "unknown"}', "[]", "{", "queued"),
    ],
)
async def test_a_damaged_job_row_breaks_no_list_and_stops_no_dispatch(
    cp: CP, fake: FakeRunner, payload: str, tried: str, needs: str, state: sdb.JobState
) -> None:
    from aims.server.db import Job as JobRow

    bad = "01a18000-0000-7000-8000-00000000000f"
    t = time.time()
    payload_ok = {"kind": "unknown"}
    row = JobRow(bad, None, None, state, payload_ok, {}, None, None, None, [], None, t, t, None)
    await cp.state.db.add_jobs([row])
    # as a hand edit or a damaged file may leave it: valid JSON of the wrong shape, or text
    # that is not JSON
    await cp.state.db._db.execute(
        "update jobs set payload = ?, tried = ?, needs = ? where run_id = ?",
        (payload, tried, needs, bad),
    )
    await cp.state.db._db.commit()
    async with cp.client(cp.operator) as c, cp.client(cp.viewer) as viewer:
        r = await c.get("/api/runs")
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
        assert (await c.get(f"/api/runs/{bad}")).status_code == 404
        r = await viewer.get(f"/api/runs/{bad}")
        assert (r.status_code, r.json()["code"]) == (403, "kind")
        (good,) = await start_run(cp)
        offered: set[str] = set()
        while good not in offered:
            offered.add((await fake.next(OfferMessage)).run_id)
        r = await c.get("/api/runs")
        assert [i["run_id"] for i in r.json()["items"]] == [good]
    if needs != "{}":
        # no runner can read what it needs, so it waits
        assert bad not in offered
        job = await cp.state.db.job(bad)
        assert job is not None
        assert job.state == state
    if bad in offered:
        # no runner can load it, so the claim ends it
        assert await fake.client.claim(bad) is None
        job = await cp.state.db.job(bad)
        assert job is not None
        assert (job.state, (job.reason or "")[:21]) == ("done", "the job does not load")


async def test_a_viewer_gets_nothing_of_a_job_of_a_kind_this_version_does_not_know(
    cp: CP, fake: FakeRunner
) -> None:
    """What such a job shows is unknown here, so a viewer is refused it, as for a score run.
    An operator can still cancel it."""
    from aims.server.db import Job as JobRow

    bad = "01a18000-0000-7000-8000-00000000000d"
    t = time.time()
    payload = {"kind": "unknown"}
    row = JobRow(bad, None, None, "queued", payload, {}, None, None, None, [], None, t, t, None)
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        # subscribed before the job is here, so its kind is not known yet
        params = {"run": [bad], "after": [-1]}
        async with asyncio.timeout(10), viewer.sse("/api/stream", params=params) as source:
            await cp.state.db.add_jobs([row])
            cp.state.changes.touch(bad)
            got = [e.event async for e in source if e.event not in ("message", "")]
        assert got == []
        for path in ("", "/events", "/flags"):
            r = await viewer.get(f"/api/runs/{bad}{path}")
            assert (r.status_code, r.json()["code"]) == (403, "kind"), path
        assert (await viewer.get("/api/stream", params={"run": [bad]})).status_code == 403
        cp.state.note_deleted(bad, "unknown")
        cp.state.note_deleted("01a18000-0000-7000-8000-00000000000e", "scenario")
        deleted: list[str] = []
        stream = {"runs": "true", "last_event_id": "@0"}
        async with asyncio.timeout(10), viewer.sse("/api/stream", params=stream) as source:
            async for e in source:
                if e.event == "deleted":
                    deleted.append(json.loads(e.data)["run_id"])
                    if len(deleted) == 1:
                        break
        assert deleted == ["01a18000-0000-7000-8000-00000000000e"]
        r = await c.post(f"/api/runs/{bad}/cancel", json={})
        assert (r.status_code, r.json()["state"]) == (202, "cancelled"), r.text


@pytest.mark.parametrize("needs", [{"keys": 5}, {"keys": [1, "KEY"]}, ["kernel"], None])
async def test_a_job_whose_needs_this_version_cannot_read_breaks_no_list_and_no_queue(
    cp: CP, fake: FakeRunner, needs: Any
) -> None:
    from aims.server.db import Job as JobRow

    bad = "01a18000-0000-7000-8000-00000000000c"
    # as another version or a damaged row may hold; older, so the queue takes it first
    t = time.time() - 60
    payload = {"kind": "scenario", "scenario": "control"}
    row = JobRow(bad, None, None, "queued", payload, needs, None, None, None, [], None, t, t, None)
    await cp.state.db.add_jobs([row])
    cp.state.changes.touch(bad)
    (good,) = await start_run(cp)
    offer = await fake.next(OfferMessage, within=5)
    assert offer.run_id == good
    assert await job_state(cp, bad) == "queued"
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/runs")
        assert r.status_code == 200, r.text
        assert [i["run_id"] for i in r.json()["items"]] == [good]
        assert (await c.get(f"/api/runs/{bad}")).status_code == 404
        listed: list[str] = []
        params = {"runs": "true", "last_event_id": "@0"}
        async with asyncio.timeout(10), c.sse("/api/stream", params=params) as source:
            async for e in source:
                if e.event == "run":
                    listed.append(json.loads(e.data)["run"]["run_id"])
                    break
        assert listed == [good]


@pytest.mark.parametrize(
    ("action", "said"), [("cancel", "cancelled by op"), ("kill", "killed by op")]
)
async def test_a_queued_run_stopped_with_no_reason_says_who_stopped_it(
    cp: CP, action: str, said: str
) -> None:
    fake = FakeRunner(cp)
    await fake.client.hello(fake.hello_body())
    (run_id,) = await start_run(cp)
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{run_id}/{action}", json={})
        assert r.json()["state"] == "cancelled"
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["state"], run["reason"]) == ("cancelled", said)
    await fake.client.aclose()


@pytest.mark.usefixtures("profiles")
async def test_a_run_needs_a_runner_with_its_keys(cp: CP, fake: FakeRunner) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={
                "scenario": {"source": "project", "name": "control"},
                "profile": "keyed",
                "confirm": True,
            },
        )
        assert (r.status_code, r.json()["code"]) == (422, "keys")
        # the monitor's profile reads a key too
        assert r.json()["detail"] == (
            "no runner allows MODEL_API_KEY=https://api.example.com/v1,"
            " TEST_KEY_THAT_IS_NOT_SET=http://model.invalid/v1; add them to a runner's AIMS_KEYS"
            " and set their variables there"
        )


async def test_commands_reach_the_runner_and_unacked_ones_come_again(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    shipper = Shipper(local, fake.client, run_id)
    task = asyncio.create_task(shipper.run())
    async with cp.client(cp.operator) as c:
        await until(lambda: cp.state.store.run(run_id), lambda info: info is not None)
        r = await c.post(f"/api/runs/{run_id}/pause", json={"agent": "a", "reason": "look"})
        assert r.status_code == 202
        sent = r.json()
        assert sent["delivered"]
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["commands"], run["commands_waiting"]) == (1, 1)
        command = await fake.next(CommandMessage)
        assert command.id == sent["command_id"]
        assert (command.command.action, command.command.agent, command.command.token) == (
            "pause",
            "a",
            "op",
        )
        await fake.disconnect()
        await fake.connect()
        again = await fake.next(CommandMessage)
        assert again.id == command.id
        await fake.client.ack(command.id, Ack(ok=True, outcome="done"))
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["action"], x["outcome"], x["state"]) for x in listed] == [
            ("pause", "done", "done")
        ]
        assert (listed[0]["agent"], listed[0]["reason"], listed[0]["by"]) == ("a", "look", "op")
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["commands"], run["commands_waiting"]) == (1, 0)
        await fake.disconnect()
        await fake.connect()
        with pytest.raises(TimeoutError):
            await fake.next(CommandMessage, within=0.5)
        await c.post(f"/api/runs/{run_id}/inject", json={"text": "hi"})
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(10):
            await task
        assert (await cp.state.db.pending_commands("fake")) == []
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["action"], x["text"], x["state"]) for x in listed] == [
            ("pause", None, "done"),
            ("inject", "hi", "dropped"),
        ]
        r = await c.post(f"/api/runs/{run_id}/kill", json={})
        assert (r.status_code, r.json()["code"]) == (409, "ended")


async def test_a_command_carried_out_ends_done_when_the_run_ends_before_its_answer(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    async with cp.client(cp.operator) as c:
        kill = (await c.post(f"/api/runs/{run_id}/kill", json={})).json()["command_id"]
        pause = (await c.post(f"/api/runs/{run_id}/pause", json={})).json()["command_id"]
        for _ in range(2):
            await fake.next(CommandMessage)
        # the kill took effect, and the run's last events reach the control plane before the ack
        await local.append(run_id, [Draft(RunFinished(status="killed"))])
        await ship(local, fake.client, run_id)
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["state"], x["final"]) for x in listed] == [("dropped", False)] * 2
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["commands"], run["commands_waiting"], run["commands_dropped"]) == (2, 0, 2)
        await fake.client.ack(kill, Ack(ok=True, outcome="done"))
        await fake.client.ack(pause, Ack(ok=False, outcome="the run is not running here"))
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["action"], x["state"], x["outcome"], x["final"]) for x in listed] == [
            ("kill", "done", "done", True),
            ("pause", "dropped", "dropped: the run ended", True),
        ]
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["commands"], run["commands_waiting"], run["commands_dropped"]) == (2, 0, 1)


async def test_a_commands_state_comes_from_the_answer_never_from_its_text(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    answers = [
        Ack(ok=True, outcome="failed: the sandbox of a", in_part=True),
        Ack(ok=True, outcome="dropped: the sandbox of a", in_part=True),
        Ack(ok=False, outcome="dropped: no such agent", in_part=True),
        Ack(ok=True, outcome="Done."),
    ]
    async with cp.client(cp.operator) as c:
        for ack in answers:
            sent = (await c.post(f"/api/runs/{run_id}/pause", json={})).json()["command_id"]
            await fake.next(CommandMessage)
            await fake.client.ack(sent, ack)
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["state"], x["outcome"], x["in_part"], x["final"]) for x in listed] == [
            ("done", "failed: the sandbox of a", True, True),
            ("done", "dropped: the sandbox of a", True, True),
            # a refusal took no effect at all
            ("failed", "failed: dropped: no such agent", False, True),
            ("done", "Done.", False, True),
        ]
        run = (await c.get(f"/api/runs/{run_id}")).json()["run"]
        assert (run["commands"], run["commands_waiting"], run["commands_dropped"]) == (4, 0, 0)


# operator bodies as JSON text, each with the escape of a lone surrogate in one field
LONE_SURROGATE = [
    ("pause", '{"agent": "a\\ud800"}', "agent"),
    ("pause", '{"reason": "a\\ud800"}', "reason"),
    ("resume", '{"agent": "a\\ud800"}', "agent"),
    ("resume", '{"then": "inject", "text": "a\\ud800"}', "text"),
    ("resume", '{"reason": "\\udfff"}', "reason"),
    ("sweep", '{"agent": "a\\ud800"}', "agent"),
    ("sweep", '{"reason": "a\\ud800"}', "reason"),
    ("inject", '{"agent": "a\\ud800", "text": "hi"}', "agent"),
    ("inject", '{"text": "a\\ud800"}', "text"),
    ("kill", '{"reason": "a\\ud800"}', "reason"),
    ("cancel", '{"reason": "a\\ud800"}', "reason"),
]


@contextlib.asynccontextmanager
async def running_run(cp: CP, fake: FakeRunner, local: LocalEventStore) -> AsyncGenerator[str]:
    """A run the fake runner holds and ships, running on the control plane until the end."""
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    task = asyncio.create_task(Shipper(local, fake.client, run_id).run())
    try:
        await until(lambda: cp.state.store.run(run_id), lambda info: info is not None)
        yield run_id
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_an_inject_as_long_as_an_api_body_allows_reaches_the_runner(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    """The runner's feed takes an event as long as the largest command, so a long inject does
    not stop it."""
    text = "x" * (3 * 1024 * 1024)
    async with running_run(cp, fake, local) as run_id, cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{run_id}/inject", json={"text": text})
        assert r.status_code == 202, r.text
        inject = await fake.next(CommandMessage)
        assert (inject.command.action, inject.command.text) == ("inject", text)


async def test_operator_text_with_a_lone_surrogate_is_refused_and_never_blocks_a_kill(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    json_body = {"Content-Type": "application/json"}
    async with running_run(cp, fake, local) as run_id, cp.client(cp.operator) as c:
        refused = [
            await c.post(f"/api/runs/{run_id}/{action}", content=body, headers=json_body)
            for action, body, _ in LONE_SURROGATE
        ]
        r = await c.post(f"/api/runs/{run_id}/kill", json={"reason": "stop"})
        assert r.status_code == 202
        kill = await fake.next(CommandMessage)
        assert (kill.command.action, kill.command.reason) == ("kill", "stop")
        for r, (action, _, field) in zip(refused, LONE_SURROGATE, strict=True):
            assert r.status_code == 422, (action, field)
            assert r.headers["content-type"] == "application/problem+json"
            [error] = r.json()["errors"]
            assert error["loc"] == ["body", field]
            assert "lone surrogate" in error["msg"]
        stored = await cp.state.db.commands_of(run_id)
        assert [x.payload["action"] for x in stored] == ["kill"]
        pair = '{"text": "smile \\ud83d\\ude00"}'
        r = await c.post(f"/api/runs/{run_id}/inject", content=pair, headers=json_body)
        assert r.status_code == 202
        inject = await fake.next(CommandMessage)
        assert inject.command.text == "smile \U0001f600"
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [x["text"] for x in listed] == [None, "smile \U0001f600"]


async def test_a_stored_command_with_a_lone_surrogate_does_not_block_the_feed(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    async with running_run(cp, fake, local) as run_id, cp.client(cp.operator) as c:
        # stored and sent below the API, as a control plane did before it refused such text
        bad = await cp.state.db.add_command(
            "fake", run_id, {"action": "pause", "reason": "a\ud800", "token": "op"}
        )
        sent = CommandMessage(
            id=bad.id, seq=bad.seq, run_id=run_id, command=CommandAction(**bad.payload)
        )
        assert runner_api.send(cp.state, "fake", sent)
        r = await c.post(f"/api/runs/{run_id}/kill", json={})
        assert r.status_code == 202
        for _ in range(2):
            pause = await fake.next(CommandMessage)
            assert (pause.id, pause.command.reason) == (bad.id, "a\\ud800")
            kill = await fake.next(CommandMessage)
            assert (kill.id, kill.command.action) == (r.json()["command_id"], "kill")
            # unacked, both come again after a reconnect
            await fake.disconnect()
            await fake.connect()
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [(x["action"], x["reason"]) for x in listed] == [
            ("pause", "a\\ud800"),
            ("kill", None),
        ]


async def test_a_scenario_with_a_lone_surrogate_escape_is_refused_where_it_loads(cp: CP) -> None:
    path = cp.project.parent / "scenarios/control/scenario.yaml"
    escaped = SCENARIO + '    prompts: {system: "Be \\ud800 brief."}\n'
    error = {"loc": [], "msg": f"agents[0].prompts.system: {LONE}", "line": 11}
    async with cp.client(cp.operator) as c:
        r = await c.get(f"{FILES}/scenario.yaml")
        headers = {"Content-Type": "application/octet-stream", "If-Match": r.headers["etag"]}
        r = await c.put(f"{FILES}/scenario.yaml", content=escaped.encode(), headers=headers)
        assert (r.status_code, r.json()["errors"]) == (422, [error])
        assert path.read_text() == SCENARIO
        r = await c.post("/api/scenarios/check", json={"text": escaped})
        assert r.json() == {"ok": False, "errors": [error], "warnings": [], "notes": []}
        # written by hand, it loads nowhere and lists as a scenario that does not load
        path.write_text(escaped)
        r = await c.post("/api/runs", json={"scenario": {"source": "project", "name": "control"}})
        assert (r.status_code, r.json()["errors"]) == (422, [error])
        r = await c.post("/api/scenarios/project/control/selftest", json={})
        assert (r.status_code, r.json()["errors"]) == (422, [error])
        listed = (await c.get("/api/scenarios")).json()
        assert [s["error"] for s in listed if s["source"] == "project"] == [
            f"control:11: agents[0].prompts.system: {LONE}"
        ]
        detail = await c.get("/api/scenarios/project/control")
        assert (detail.status_code, detail.json()["errors"]) == (200, [error])
    assert await cp.state.db.jobs() == []


async def test_a_key_with_a_lone_surrogate_is_refused_at_any_depth(cp: CP) -> None:
    # JSON dumps of the bodies a client may send: json.dumps escapes each lone surrogate
    rule = {"id": "r", "title": "t", "event": ["tool.call"], "match": {"name\ud800": "shell"}}
    sent = [
        ("PATCH", CONFIG, {"set": [{"key": ["a"], "value": {"t": [{"k\udfff": 1}]}}]}),
        ("PATCH", CONFIG, {"set": [{"key": ["a"], "value": {"t": "v\ud800"}}]}),
        ("POST", f"/api/runs/{RUN}/monitor", {"config": {"rules": [rule]}}),
        ("POST", f"/api/runs/{RUN}/monitor", {"config": {"signals": {"s\udc00": {}}}}),
    ]
    places = [["body", "set", 0, "value"]] * 2 + [["body", "config"]] * 2
    before = cp.project.read_bytes()
    json_body = {"Content-Type": "application/json"}
    async with cp.client(cp.operator) as c:
        for (method, url, body), loc in zip(sent, places, strict=True):
            r = await c.request(method, url, content=json.dumps(body), headers=json_body)
            assert r.status_code == 422, (url, r.text)
            [error] = r.json()["errors"]
            assert (error["loc"], error["msg"]) == (loc, f"Value error, {LONE}")
        # a budget names agents, which the name check refuses
        body = {"scenario": {"source": "project", "name": "control"}}
        body["budget"] = {"agents": {"a\ud800": {"steps": 1}}}
        r = await c.post("/api/runs", content=json.dumps(body), headers=json_body)
        assert r.status_code == 422
        [error] = r.json()["errors"]
        assert error["loc"][:3] == ["body", "budget", "agents"]
        assert "is not a valid name" in error["msg"]
    assert cp.project.read_bytes() == before
    assert await cp.state.db.jobs() == []


async def test_hello_takes_back_claimed_jobs_it_does_not_list(cp: CP, fake: FakeRunner) -> None:
    (run_id,) = await start_run(cp)
    offer = await fake.next(OfferMessage)
    assert await fake.client.claim(offer.run_id) is not None
    reply = await fake.client.hello(fake.hello_body(jobs=[]))
    assert reply.requeued == [run_id]
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert job.state in ("queued", "offered")


# Config and scenario files


async def test_config_edits_keep_comments_and_need_the_current_etag(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        layer = (await c.get("/api/config/user")).json()
        assert not layer["exists"]
        assert layer["text"].startswith("# AIMS settings")
        patch = {"set": [{"key": ["prompts", "system"], "value": "line one\nline two"}]}
        r = await c.patch("/api/config/user", json=patch)
        assert r.status_code == 200, r.text
        saved = r.json()
        file = Path(saved["path"])
        text = await asyncio.to_thread(file.read_text)
        assert text.startswith("# AIMS settings")
        assert "line one\nline two" in text
        r = await c.patch("/api/config/user", json=patch)
        assert r.status_code == 428
        r = await c.patch("/api/config/user", json=patch, headers={"If-Match": '"stale"'})
        assert (r.status_code, r.json()["current"]) == (412, text)
        bad = {"text": "[nonsense]\nx = 1\n"}
        r = await c.put("/api/config/user", json=bad, headers={"If-Match": f'"{saved["etag"]}"'})
        assert (r.status_code, r.json()["code"]) == (422, "config")
        assert await asyncio.to_thread(file.read_text) == text
        unset = {"unset": [["prompts", "system"]]}
        r = await c.patch("/api/config/user", json=unset, headers={"If-Match": saved["etag"]})
        assert r.status_code == 200
        assert "line one" not in await asyncio.to_thread(file.read_text)
        prompts = (await c.get("/api/prompts")).json()
        assert prompts["agent"]["system"]["source"] == "default"
        project = (await c.get("/api/config/project")).json()
        assert project["text"].startswith("# Test project.")
        info = (await c.get("/api/config")).json()
        assert [x["layer"] for x in info["layers"]] == ["user", "project"]


@contextlib.contextmanager
def read_only(path: Path) -> Generator[None]:
    mode = path.stat().st_mode
    path.chmod(0o500)
    try:
        yield
    finally:
        path.chmod(mode)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
async def test_a_write_the_filesystem_refuses_is_a_problem_not_a_crash(
    cp: CP, user_config: Path
) -> None:
    def refused(r: httpx2.Response, path: Path) -> None:
        assert r.status_code == 403, r.text
        body = r.json()
        assert body["code"] == "not_writable"
        assert body["detail"] == f"cannot write {path}: permission denied"

    scenario = cp.project.parent / "scenarios" / "control"
    async with cp.client(cp.operator) as c:
        with read_only(user_config):
            r = await c.put("/api/config/user", json={"text": "# mine\n"})
            refused(r, user_config / "config.toml")
            patch = {"set": [{"key": ["prompts", "system"], "value": "hi"}]}
            refused(await c.patch("/api/config/user", json=patch), user_config / "config.toml")
        assert await asyncio.to_thread(os.listdir, user_config) == []
        with read_only(scenario):
            r = await c.put(
                "/api/scenarios/project/control/files/notes.md",
                content=b"notes",
                headers={"Content-Type": "application/octet-stream", "If-None-Match": "*"},
            )
            refused(r, scenario / "notes.md")
        with read_only(scenario.parent):
            refused(await c.post("/api/scenarios/project/fresh"), scenario.parent / "fresh")
            r = await c.post(
                "/api/scenarios/builtin/word-count/duplicate",
                json={"source": "project", "name": "copy"},
            )
            refused(r, scenario.parent / "copy")
        assert await asyncio.to_thread(os.listdir, scenario.parent) == ["control"]


async def test_the_server_makes_its_private_temp_folder_at_start(cp: CP) -> None:
    spool = await asyncio.to_thread((cp.state.data / "tmp").stat)
    assert stat.S_IMODE(spool.st_mode) == 0o700


async def test_a_run_import_spools_in_the_data_dir(
    cp: CP, local: LocalEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        t.add(local.root / run_id / "events.jsonl", arcname=f"{run_id}/events.jsonl")
    # a /tmp that cannot take a file, as a full or missing one
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-tmp"))
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs/import",
            content=tar.getvalue(),
            headers={"Content-Type": "application/x-tar"},
        )
        assert r.status_code == 201, r.text
    spool = cp.state.data / "tmp"
    assert stat.S_IMODE((await asyncio.to_thread(spool.stat)).st_mode) == 0o700
    assert await asyncio.to_thread(os.listdir, spool) == []


async def test_config_text_is_checked_by_line_and_merged_for_viewers(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/config/user/check", json={"text": "[roles]\nagent = \n"})
        assert r.json() == {
            "ok": False,
            "errors": [{"loc": [], "msg": "Invalid value (column 9)", "line": 2}],
            "warnings": [],
        }
        r = await c.post("/api/config/project/check", json={"text": "[nonsense]\n"})
        (error,) = r.json()["errors"]
        assert error["line"] is None
        assert error["msg"].startswith(f"{cp.project}: unknown table(s) nonsense")
        r = await c.post("/api/config/user/check", json={"text": "[credits]\nexample = 1\n"})
        assert r.json()["ok"]
        r = await c.put("/api/config/user", json={"text": "x = \n"}, headers={"If-None-Match": "*"})
        assert (r.status_code, r.json()["code"], r.json()["errors"][0]["line"]) == (422, "toml", 1)
        user = Path((await c.get("/api/config/user")).json()["path"])
        assert [p.name for p in user.parent.iterdir()] == []
    cp.project.write_text((Path(__file__).parents[1] / "profiles.toml").read_text())
    async with cp.client(cp.viewer) as c:
        r = await c.post("/api/config/user/check", json={"text": ""})
        assert r.status_code == 403
        r = await c.get("/api/config/merged")
        assert r.status_code == 200, r.text
        assert '\n[roles]\nagent = "' in r.json()["text"]


async def test_a_config_file_that_is_not_utf8_is_a_problem_not_a_crash(cp: CP) -> None:
    cp.project.write_bytes("[credits]\nexample = 1 # café\n".encode("latin-1"))
    error = f"{cp.project} is not UTF-8"
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/config/merged")
        assert (r.status_code, r.json()["code"], r.json()["detail"]) == (422, "config", error)
        assert (await c.get("/api/config")).json()["error"] == error
        r = await c.post("/api/config/user/check", json={"text": ""})
        assert r.json()["errors"] == [{"loc": [], "msg": error, "line": None}]
        r = await c.put("/api/config/user", json={"text": ""}, headers={"If-None-Match": "*"})
        assert (r.status_code, r.json()["code"]) == (422, "config"), r.text


async def test_config_routes_read_and_load_files_off_the_event_loop(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = threading.current_thread()
    threads: dict[str, list[threading.Thread]] = {"_problem": [], "read_or_none": []}

    def record(name: str) -> None:
        real = getattr(edit_api, name)

        def wrapper(*args: Any) -> Any:
            threads[name].append(threading.current_thread())
            return real(*args)

        monkeypatch.setattr(edit_api, name, wrapper)

    for name in threads:
        record(name)
    async with cp.client(cp.operator) as c:
        assert (await c.get("/api/config")).status_code == 200
        assert (await c.get("/api/config/user")).status_code == 200
        r = await c.put(
            "/api/config/user",
            json={"text": "[credits]\nexample = 1\n"},
            headers={"If-None-Match": "*"},
        )
        assert r.status_code == 200, r.text
        patch = {"set": [{"key": ["credits", "example"], "value": 2}]}
        r = await c.patch("/api/config/user", json=patch, headers={"If-Match": r.headers["ETag"]})
        assert r.status_code == 200, r.text
    assert (len(threads["_problem"]), len(threads["read_or_none"])) == (2, 6)
    assert loop not in threads["_problem"] + threads["read_or_none"]


async def test_without_a_project_file_saving_the_project_layer_makes_one(
    cp: CP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    cp.state.config.project = None
    async with cp.client(cp.operator) as c:
        info = (await c.get("/api/config")).json()
        assert info["layers"][1] == {
            "layer": "project",
            "path": str(Path.cwd() / "aims.toml"),
            "exists": False,
            "etag": None,
        }
        assert (await c.get("/api/config/project")).json()["text"] == STARTER
        r = await c.put(
            "/api/config/project",
            json={"text": "[credits]\nexample = 1\n"},
            headers={"If-None-Match": "*"},
        )
        assert r.status_code == 200, r.text
        assert (await c.get("/api/config")).json()["layers"][1]["exists"]
        merged = (await c.get("/api/config/merged")).json()["text"]
        assert "example = 1.0  # this project" in merged


async def test_scenario_files_are_validated_and_confined(cp: CP) -> None:
    base = "/api/scenarios/project/control/files"
    async with cp.client(cp.operator) as c:
        listed = (await c.get("/api/scenarios")).json()
        assert [(s["name"], s["writable"]) for s in listed if s["source"] == "project"] == [
            ("control", True)
        ]
        r = await c.get(f"{base}/scenario.yaml")
        tag = r.headers["etag"]
        original = r.content
        bad = original.replace(b"oversight: observe", b"oversight: nope")
        headers = {"Content-Type": "application/octet-stream", "If-Match": tag}
        r = await c.put(f"{base}/scenario.yaml", content=bad, headers=headers)
        assert r.status_code == 422
        assert r.json()["errors"][0]["line"] == 3
        good = original.replace(b"Do what you are told.", b"Do it now.")
        r = await c.put(f"{base}/scenario.yaml", content=good, headers=headers)
        assert r.status_code == 200, r.text
        assert (cp.project.parent / "scenarios/control/scenario.yaml").read_bytes() == good
        new = {"Content-Type": "application/octet-stream", "If-None-Match": "*"}
        assert (
            await c.put(f"{base}/files/data.bin", content=b"\0\1", headers=new)
        ).status_code == 200
        assert (await c.put(f"{base}/files/data.bin", content=b"x", headers=new)).status_code == 412
        assert (await c.put(f"{base}/..%2F..%2Fescape", content=b"x", headers=new)).status_code in (
            403,
            404,
            422,
        )
        outside = cp.project.parent / "outside"
        outside.mkdir()
        os.symlink(outside, cp.project.parent / "scenarios/control/link")
        r = await c.put(f"{base}/link/x.txt", content=b"x", headers=new)
        assert r.status_code == 403
        assert not (outside / "x.txt").exists()
        files = [f["path"] for f in (await c.get(base)).json()]
        assert files == ["files/data.bin", "scenario.yaml"]
        r = await c.post(
            "/api/scenarios/project/control/duplicate", json={"source": "project", "name": "copy"}
        )
        assert r.status_code == 201
        assert not (cp.project.parent / "scenarios/copy/link").exists()
        check = (await c.post("/api/scenarios/check", json={"text": good.decode()})).json()
        assert check["ok"]
        check = (await c.post("/api/scenarios/check", json={"text": bad.decode()})).json()
        assert not check["ok"]


FILES = "/api/scenarios/project/control/files"
CONFIG = "/api/config/project"
SET = {"set": [{"key": ["credits", "example"], "value": 1}]}
RUN = str(uuid.UUID(int=1))


LONE_TEXT = "a\ud800"
PROMPTS = "/api/scenarios/project/control/prompts"
CONTROL = {"scenario": {"source": "project", "name": "control"}}
# a lone surrogate in each text field of an /api body, and the field the refusal names
BODY_TEXT: list[tuple[str, str, dict[str, Any], list[str | int]]] = [
    ("POST", "/api/session", {"token": LONE_TEXT}, ["token"]),
    ("POST", "/api/session", {"code": LONE_TEXT}, ["code"]),
    (
        "POST",
        "/api/runs",
        {"scenario": {"source": LONE_TEXT, "name": "control"}},
        ["scenario", "source"],
    ),
    (
        "POST",
        "/api/runs",
        {"scenario": {"source": "project", "name": LONE_TEXT}},
        ["scenario", "name"],
    ),
    ("POST", "/api/runs", {**CONTROL, "variant": LONE_TEXT}, ["variant"]),
    ("POST", "/api/runs", {**CONTROL, "series": LONE_TEXT}, ["series"]),
    ("POST", "/api/runs", {**CONTROL, "profile": LONE_TEXT}, ["profile"]),
    ("POST", "/api/runs", {**CONTROL, "runner": LONE_TEXT}, ["runner"]),
    ("POST", f"/api/runs/{RUN}/forks", {"at": LONE_TEXT}, ["at"]),
    ("POST", f"/api/runs/{RUN}/forks", {"profile": LONE_TEXT}, ["profile"]),
    ("POST", f"/api/runs/{RUN}/forks", {"runner": LONE_TEXT}, ["runner"]),
    ("POST", f"/api/runs/{RUN}/monitor", {"profile": LONE_TEXT}, ["profile"]),
    ("POST", f"/api/runs/{RUN}/monitor", {"runner": LONE_TEXT}, ["runner"]),
    ("POST", f"/api/runs/{RUN}/score", {"monitor": LONE_TEXT}, ["monitor"]),
    ("POST", f"/api/runs/{RUN}/score", {"runner": LONE_TEXT}, ["runner"]),
    (
        "POST",
        f"/api/runs/{RUN}/score",
        {"plants_from": {"source": "project", "name": LONE_TEXT}},
        ["plants_from", "name"],
    ),
    ("POST", f"/api/runs/{RUN}/labels", {"flag": 1, "label": "real", "note": LONE_TEXT}, ["note"]),
    ("POST", "/api/models/check", {"profiles": [LONE_TEXT]}, ["profiles", 0]),
    ("POST", "/api/models/check", {"runner": LONE_TEXT}, ["runner"]),
    ("POST", "/api/scenarios/check", {"text": LONE_TEXT}, ["text"]),
    ("POST", "/api/scenarios/check", {"text": "x", "profile": LONE_TEXT}, ["profile"]),
    (
        "POST",
        "/api/scenarios/check",
        {"text": "x", "source": LONE_TEXT, "name": "control"},
        ["source"],
    ),
    (
        "POST",
        "/api/scenarios/check",
        {"text": "x", "source": "project", "name": LONE_TEXT},
        ["name"],
    ),
    ("POST", "/api/scenarios/project/control/selftest", {"runner": LONE_TEXT}, ["runner"]),
    (
        "POST",
        "/api/scenarios/project/control/duplicate",
        {"source": LONE_TEXT, "name": "n"},
        ["source"],
    ),
    (
        "POST",
        "/api/scenarios/project/control/duplicate",
        {"source": "project", "name": LONE_TEXT},
        ["name"],
    ),
    ("POST", "/api/tokens", {"name": LONE_TEXT, "role": "viewer"}, ["name"]),
    ("POST", f"{CONFIG}/check", {"text": LONE_TEXT}, ["text"]),
    ("PUT", CONFIG, {"text": LONE_TEXT}, ["text"]),
    ("PATCH", CONFIG, {"set": [{"key": ["a", LONE_TEXT], "value": 1}]}, ["set", 0, "key", 1]),
    ("PATCH", CONFIG, {"unset": [[LONE_TEXT]]}, ["unset", 0, 0]),
    (
        "PATCH",
        PROMPTS,
        {"set": [{"agent": LONE_TEXT, "key": ["system"], "value": "x"}]},
        ["set", 0, "agent"],
    ),
    (
        "PATCH",
        PROMPTS,
        {"set": [{"agent": "a", "key": [LONE_TEXT], "value": "x"}]},
        ["set", 0, "key", 0],
    ),
    (
        "PATCH",
        PROMPTS,
        {"set": [{"agent": "a", "key": ["system"], "value": LONE_TEXT}]},
        ["set", 0, "value"],
    ),
    ("PATCH", PROMPTS, {"unset": [{"agent": LONE_TEXT, "key": ["system"]}]}, ["unset", 0, "agent"]),
    ("PATCH", PROMPTS, {"unset": [{"agent": "a", "key": [LONE_TEXT]}]}, ["unset", 0, "key", 0]),
]


async def test_body_text_with_a_lone_surrogate_is_refused_naming_its_field(cp: CP) -> None:
    tokens = await cp.state.db.tokens()
    scenario = (cp.project.parent / "scenarios/control/scenario.yaml").read_bytes()
    config = cp.project.read_bytes()
    async with cp.client(cp.operator) as c:
        for method, url, body, loc in BODY_TEXT:
            r = await c.request(
                method, url, content=json.dumps(body), headers={"Content-Type": "application/json"}
            )
            assert r.status_code == 422, (method, url, r.text)
            [error] = r.json()["errors"]
            assert (error["loc"], error["msg"]) == (["body", *loc], f"Value error, {LONE}")
    assert await cp.state.db.tokens() == tokens
    assert (cp.project.parent / "scenarios/control/scenario.yaml").read_bytes() == scenario
    assert cp.project.read_bytes() == config
    assert not (cp.project.parent / "scenarios/n").exists()
    assert await cp.state.db.jobs() == []


def unchecked_text(model: type[BaseModel], seen: set[type]) -> set[str]:
    """The fields of `model`, and of the models it holds, with text no NoLoneSurrogate covers:
    a str, or a JsonValue, whose strings and keys are text too."""
    if model in seen:
        return set()
    seen.add(model)
    out: set[str] = set()

    def bare(tp: Any, checked: bool) -> bool:
        if typing.get_origin(tp) is typing.Annotated:
            base, *meta = typing.get_args(tp)
            return bare(base, checked or NoLoneSurrogate in meta)
        if tp is str or tp is JsonValue:
            return not checked
        if typing.get_origin(tp) is Literal:
            return False
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            # NoLoneSurrogate on a list of models walks the JSON of each one
            if not checked:
                out.update(unchecked_text(tp, seen))
            return False
        return any(bare(arg, checked) for arg in typing.get_args(tp))

    for name, field in model.model_fields.items():
        if bare(field.annotation, NoLoneSurrogate in field.metadata):
            out.add(f"{model.__name__}.{name}")
    return out


async def test_every_text_field_of_an_api_body_refuses_a_lone_surrogate(tmp_path: Path) -> None:
    app, _ = await _openapi(tmp_path)
    bodies: set[type[BaseModel]] = set()
    for route in iter_route_contexts(app.routes):
        if route.endpoint is None or not (route.path_format or "").startswith("/api/"):
            continue
        hints = typing.get_type_hints(inspect.unwrap(route.endpoint), include_extras=True)
        hints.pop("return", None)
        bodies |= {t for t in hints.values() if isinstance(t, type) and issubclass(t, BaseModel)}
    assert {"StartRun", "StartMonitor", "ConfigPatch", "PromptsPatch"} <= {
        b.__name__ for b in bodies
    }
    seen: set[type] = set()
    unchecked = set().union(*(unchecked_text(b, seen) for b in bodies))
    # agent names, which the name check refuses with its own error
    assert unchecked == {"BudgetOverride.agents"}


async def test_every_text_field_of_a_runner_body_refuses_a_lone_surrogate(tmp_path: Path) -> None:
    app, _ = await _openapi(tmp_path)
    bodies: set[type[BaseModel]] = set()
    for route in iter_route_contexts(app.routes):
        if route.endpoint is None or not (route.path_format or "").startswith("/runner/"):
            continue
        hints = typing.get_type_hints(inspect.unwrap(route.endpoint), include_extras=True)
        hints.pop("return", None)
        bodies |= {t for t in hints.values() if isinstance(t, type) and issubclass(t, BaseModel)}
    assert {"RunnerHello", "RunnerStatus", "Ack", "Release", "Heads", "Labels", "Deltas"} <= {
        b.__name__ for b in bodies
    }
    seen: set[type] = set()
    assert set().union(*(unchecked_text(b, seen) for b in bodies)) == set()


def _hello(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "r",
        "version": __version__,
        "arch": "arm64",
        "tracer": {"alive": True},
    }
    return body | over


LABEL = {"run_id": RUN, "flag": 1, "label": "real", "by": "op", "at": "2026-11-23T00:00:00Z"}
DELTA = {"run_id": RUN, "agent_id": "a", "call_id": "c", "attempt": 1, "kind": "text"}
# a lone surrogate in each text field of a runner body, with the field the refusal names
RUNNER_TEXT: list[tuple[str, dict[str, Any], list[str | int]]] = [
    ("/hello", _hello(arch=LONE_TEXT), ["arch"]),
    ("/hello", _hello(tracer={"alive": False, "detail": LONE_TEXT}), ["tracer", "detail"]),
    ("/hello", _hello(keys=[LONE_TEXT]), ["keys", 0]),
    ("/hello", _hello(jobs=[LONE_TEXT]), ["jobs", 0]),
    ("/hello", _hello(version=LONE_TEXT), ["version"]),
    ("/hello", _hello(name=LONE_TEXT), ["name"]),
    ("/status", {"tracer": {"alive": False, "detail": LONE_TEXT}, "free": 2}, ["tracer", "detail"]),
    ("/status", {"tracer": {"alive": True}, "free": 2, "running": [LONE_TEXT]}, ["running", 0]),
    ("/status", {"tracer": {"alive": True}, "free": 2, "jobs": [LONE_TEXT]}, ["jobs", 0]),
    ("/commands/c1/ack", {"ok": True, "outcome": LONE_TEXT}, ["outcome"]),
    (f"/jobs/{RUN}/release", {"reason": LONE_TEXT}, ["reason"]),
    ("/heads", {"run_ids": [LONE_TEXT]}, ["run_ids", 0]),
    (f"/runs/{RUN}/blobs/missing", {"shas": [LONE_TEXT]}, ["shas", 0]),
    (f"/runs/{RUN}/labels", {"labels": [LABEL | {"note": LONE_TEXT}]}, ["labels"]),
    (f"/runs/{RUN}/labels", {"labels": [LABEL | {"by": LONE_TEXT}]}, ["labels"]),
    ("/deltas", {"deltas": [DELTA | {"text": LONE_TEXT}]}, ["deltas", 0, "text"]),
    ("/deltas", {"deltas": [DELTA | {"run_id": LONE_TEXT}]}, ["deltas", 0, "run_id"]),
    ("/deltas", {"deltas": [DELTA | {"agent_id": LONE_TEXT}]}, ["deltas", 0, "agent_id"]),
    ("/deltas", {"deltas": [DELTA | {"call_id": LONE_TEXT}]}, ["deltas", 0, "call_id"]),
]


async def test_runner_text_with_a_lone_surrogate_is_refused_and_the_runners_page_stays_up(
    cp: CP,
) -> None:
    headers = {"X-Aims-Runner": "r", "Content-Type": "application/json"}
    async with cp.client(cp.runner) as c, cp.client(cp.viewer) as v:
        # the status, ack and other routes check the runner said hello after its body
        assert (await c.post("/runner/v1/hello", json=_hello(), headers=headers)).status_code == 200
        for path, body, loc in RUNNER_TEXT:
            # json.dumps writes each lone surrogate as its escape, as a client may
            r = await c.post(f"/runner/v1{path}", content=json.dumps(body), headers=headers)
            assert r.status_code == 422, (path, loc, r.text)
            [error] = r.json()["errors"]
            assert (error["loc"], error["msg"]) == (["body", *loc], f"Value error, {LONE}")
            assert (await v.get("/api/runners")).status_code == 200, (path, loc)
        # a valid pair is one character, kept as it is
        pair = _hello(tracer={"alive": False, "detail": "smile \U0001f600"})
        assert (await c.post("/runner/v1/hello", json=pair, headers=headers)).status_code == 200
        [runner] = (await v.get("/api/runners")).json()
        assert runner["tracer"]["detail"] == "smile \U0001f600"


async def test_a_problem_that_repeats_a_lone_surrogate_is_sent_as_its_escape(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(settings: object) -> str:
        error = ProblemError(loc=["body", "k\udfff"], msg="no agent b\udc00")
        raise ProblemException(404, "Not found", "no runner named a\ud800", errors=[error])

    monkeypatch.setattr(edit_api, "merged", refuse)
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/config/merged")
    assert (r.status_code, r.headers["content-type"]) == (404, "application/problem+json")
    problem = r.json()
    assert problem["detail"] == "no runner named a\\ud800"
    assert problem["errors"] == [{"loc": ["body", "k\\udfff"], "msg": "no agent b\\udc00"}]


@pytest.mark.parametrize(
    ("method", "url", "body", "disk", "status", "code"),
    [
        pytest.param("GET", f"{FILES}/sub", None, None, 404, None, id="read-dir"),
        pytest.param("GET", f"{FILES}/scenario.yaml/x", None, None, 404, None, id="read-in-file"),
        pytest.param("DELETE", f"{FILES}/sub", None, None, 422, None, id="delete-dir"),
        pytest.param(
            "DELETE", f"{FILES}/scenario.yaml/x", None, None, 404, None, id="delete-in-file"
        ),
        pytest.param("PUT", f"{FILES}/sub", b"x", None, 409, None, id="write-dir"),
        pytest.param("PUT", f"{FILES}/scenario.yaml/x", b"x", None, 409, None, id="write-in-file"),
        pytest.param(
            "PATCH",
            CONFIG,
            {"set": [{"key": ["credits", "example"], "value": None}]},
            None,
            422,
            None,
            id="set-null",
        ),
        pytest.param(
            "PATCH",
            CONFIG,
            {"set": [{"key": ["scenarios", "paths"], "value": ["a", None]}]},
            None,
            422,
            None,
            id="set-null-in-list",
        ),
        pytest.param("PATCH", CONFIG, SET, ("project", b"x = \n"), 422, "toml", id="not-toml"),
        pytest.param("PATCH", CONFIG, SET, ("project", b"\xff = 1\n"), 422, None, id="not-utf8"),
        pytest.param(
            "GET", f"/api/runs/{RUN}/truth", None, ("truth", b"{"), 500, None, id="truth-not-json"
        ),
    ],
)
async def test_edit_errors_are_problem_replies(
    cp: CP,
    method: str,
    url: str,
    body: dict[str, Any] | bytes | None,
    disk: tuple[str, bytes] | None,
    status: int,
    code: str | None,
) -> None:
    scenario = cp.project.parent / "scenarios/control"
    (scenario / "sub").mkdir()
    if disk is not None:
        where, data = disk
        path = cp.project if where == "project" else cp.state.store.truth_path(RUN)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    headers = {"Content-Type": "application/octet-stream", "If-None-Match": "*"}
    async with cp.client(cp.operator) as c:
        if isinstance(body, dict):
            r = await c.request(method, url, json=body)
        elif body is not None:
            r = await c.request(method, url, content=body, headers=headers)
        else:
            r = await c.request(method, url)
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json().get("code") == code
    assert (scenario / "sub").is_dir()
    assert (scenario / "scenario.yaml").read_text() == SCENARIO


async def test_an_unexpected_error_is_a_problem_reply_with_the_security_headers(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(settings: object) -> str:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(edit_api, "merged", boom)
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/config/merged")
    assert r.status_code == 500
    assert r.headers["content-type"] == "application/problem+json"
    assert "secret" not in r.text
    assert r.json()["title"] == "Server error"
    for name, value in HEADERS:
        assert r.headers[name.decode()] == value.decode()


async def _openapi(tmp_path: Path) -> tuple[FastAPI, dict[str, Any]]:
    state = await open_state(tmp_path / "server")
    try:
        app = create_app(state, dispatcher=False)
        return app, app.openapi()
    finally:
        await close_state(state)


async def test_every_status_a_route_raises_itself_is_declared(tmp_path: Path) -> None:
    # only the route's own body: following its helpers by name is not reliable
    app, schema = await _openapi(tmp_path)
    missing = []
    for route in iter_route_contexts(app.routes):
        if route.endpoint is None or not route.include_in_schema:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(route.endpoint))))
        raised = {
            str(node.args[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) in ("ProblemException", "problems.ProblemException")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        for method in sorted(route.methods or ()):
            declared = schema["paths"][route.path_format][method.lower()]["responses"]
            missing += [
                f"{method} {route.path_format} {code}"
                for code in sorted(raised)
                if code not in declared
            ]
    assert missing == []


async def test_every_write_a_person_makes_declares_the_media_type_refusal(tmp_path: Path) -> None:
    _, schema = await _openapi(tmp_path)
    operations = [
        (f"{method.upper()} {path}", operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    ]
    writes = {
        name
        for name, _ in operations
        if not name.startswith("GET ") and name.split()[1].startswith("/api/")
    }
    declared = {name for name, operation in operations if "415" in operation["responses"]}
    # signing in takes no role; runner ingest checks its own content encoding
    assert declared == writes - {"POST /api/session"} | {"POST /runner/v1/runs/{run_id}/events"}


async def test_scenario_files_and_truth_are_for_operators(cp: CP) -> None:
    src = Path(__file__).parents[2] / "src/aims/scenarios/cache-keys/scenario.yaml"
    dst = cp.project.parent / "scenarios/cache-keys"
    dst.mkdir()
    (dst / "scenario.yaml").write_bytes(src.read_bytes())
    async with cp.client(cp.viewer) as c:
        base = "/api/scenarios/project/cache-keys"
        assert (await c.get(f"{base}/files")).status_code == 403
        assert (await c.get(f"{base}/files/scenario.yaml")).status_code == 403
        listed = {s["name"]: s for s in (await c.get("/api/scenarios")).json()}
        assert listed["cache-keys"]["selftest"] is False
        assert listed["cache-keys"]["title"] == "cache-keys"
        seen = (await c.get(base)).json()
        assert seen["truth"] is False
        assert seen["title"] == "cache-keys"
        assert seen["plants"] is None
        assert seen["selftest"] is None
        assert [(a["id"], a["role"], a["prompts"]) for a in seen["agents"]] == [
            ("agent", None, None)
        ]
        assert seen["agents"][0]["budget"]["steps"] == 80
    async with cp.client(cp.operator) as c:
        listed = {s["name"]: s for s in (await c.get("/api/scenarios")).json()}
        assert listed["cache-keys"]["selftest"] is True
        assert listed["control"]["selftest"] is False
        full = (await c.get(base)).json()
        assert full["truth"] is True
        assert full["etag"] == hashlib.sha256(src.read_bytes()).hexdigest()
        assert full["agents"][0]["role"] == "subject"
        plants = {p["name"]: p for p in full["plants"]}
        assert plants["golden-keys"]["category"] == "scorer_tampering"
        assert plants["golden-keys"]["taken"] == "used"
        assert full["selftest"]["exploit"] is True
        found = (await c.get("/api/scenarios/sources")).json()
        assert found == [
            {"id": "builtin", "label": "built in", "writable": False},
            {"id": "project", "label": "this project", "writable": True},
        ]
        (dst / "scenario.yaml").write_text("version: 1\nname: [\n")
        broken = (await c.get(base)).json()
        assert broken["errors"]
        assert broken["title"] is None
        assert (await c.get("/api/scenarios/project/missing")).status_code == 404


async def test_only_operators_get_the_tag_a_scenario_write_names(cp: CP) -> None:
    broken = cp.project.parent / "scenarios/broken"
    broken.mkdir()
    (broken / "scenario.yaml").write_text("version: 1\nname: [\n")
    for name in ("control", "broken"):
        url = f"/api/scenarios/project/{name}"
        data = (cp.project.parent / "scenarios" / name / "scenario.yaml").read_bytes()
        tag = hashlib.sha256(data).hexdigest()
        async with cp.client(cp.viewer) as c:
            r = await c.get(url)
            assert r.status_code == 200, r.text
            assert r.json()["etag"] is None
            assert tag not in r.text
            assert all(tag not in v for v in r.headers.values())
        async with cp.client(cp.operator) as c:
            assert (await c.get(url)).json()["etag"] == tag


async def test_the_built_in_scenarios_are_read_only_and_duplicate_to_edit(cp: CP) -> None:
    root = builtin_root()
    assert root == Path(aims.__file__).parent / "scenarios"
    base = "/api/scenarios/builtin/cache-keys"
    async with cp.client(cp.operator) as c:
        listed = (await c.get("/api/scenarios")).json()
        shipped = {s["name"]: s for s in listed if s["source"] == "builtin"}
        assert sorted(shipped) == ["cache-keys", "shared-cache", "word-count"]
        assert all(s["error"] is None and not s["writable"] for s in shipped.values())
        r = await c.get(f"{base}/files/scenario.yaml")
        assert r.status_code == 200
        headers = {"Content-Type": "application/octet-stream", "If-Match": r.headers["etag"]}
        refused = [
            await c.put(f"{base}/files/scenario.yaml", content=r.content, headers=headers),
            await c.delete(f"{base}/files/scenario.yaml"),
            await c.patch(f"{base}/prompts", json={"set": [], "unset": []}),
            await c.post("/api/scenarios/builtin/new", json={}),
        ]
        assert [x.status_code for x in refused] == [403] * 4
        assert "duplicate the scenario to edit it" in refused[0].json()["detail"]
        r = await c.post(f"{base}/duplicate", json={"source": "project", "name": "mine"})
        assert r.status_code == 201, r.text
        copy = cp.project.parent / "scenarios/mine/scenario.yaml"
        assert copy.read_bytes() == (root / "cache-keys/scenario.yaml").read_bytes()


async def test_a_scenario_whose_folder_is_over_what_a_runner_takes_is_refused(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner takes no scenario input over INPUT_LIMIT, the folder's tar among them, so the
    control plane makes no job it would refuse."""
    monkeypatch.setattr(api, "INPUT_LIMIT", 64 * 1024, raising=False)
    src = Path(__file__).parents[2] / "src/aims/scenarios/cache-keys"
    shutil.copytree(src, cp.project.parent / "scenarios/cache-keys")
    for name in ("control", "cache-keys"):
        (cp.project.parent / "scenarios" / name / "data.bin").write_bytes(bytes(128 * 1024))

    def over(detail: str) -> bool:
        said = ("the scenario folder is ", " bytes as a tar, over the 65536 bytes a runner takes")
        return detail.startswith(said[0]) and detail.endswith(said[1])

    async with cp.client(cp.operator) as c:
        body = {"scenario": {"source": "project", "name": "control"}, "monitor": "signals"}
        r = await c.post("/api/runs", json=body)
        assert (r.status_code, r.json()["code"]) == (422, "scenario"), r.text
        assert over(r.json()["detail"])
        r = await c.post("/api/scenarios/project/cache-keys/selftest", json={})
        assert (r.status_code, r.json()["code"]) == (422, "selftest"), r.text
        assert over(r.json()["detail"])
    assert await cp.state.db.jobs() == []


async def test_a_selftest_is_queued_for_a_runner_that_runs_it(cp: CP) -> None:
    src = Path(__file__).parents[2] / "src/aims/scenarios/cache-keys"
    shutil.copytree(src, cp.project.parent / "scenarios/cache-keys")
    runner = FakeRunner(cp)
    await runner.client.hello(runner.hello_body())
    url = "/api/scenarios/project/cache-keys/selftest"
    async with cp.client(cp.viewer) as c:
        assert (await c.post(url, json={})).status_code == 403
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/scenarios/project/control/selftest", json={})
        assert (r.status_code, r.json()["code"]) == (422, "selftest")
        r = await c.post(url, json={})
        assert (r.status_code, r.json()["code"]) == (422, "kind")
        able = runner.hello_body().model_copy(update={"kinds": ["scenario", "selftest"]})
        await runner.client.hello(able)
        r = await c.post(url, json={})
        assert r.status_code == 202, r.text
        (run_id,) = r.json()["run_ids"]
        job = await cp.state.db.job(run_id)
        assert job is not None
        assert job.payload["kind"] == "selftest"
        assert job.needs == {"keys": [], "kernel": True, "kind": "selftest"}
        listed = (await c.get("/api/runs", params={"kind": "selftest"})).json()["items"]
        assert [(x["run_id"], x["kind"], x["scenario"]) for x in listed] == [
            (run_id, "selftest", "cache-keys")
        ]
    async with cp.client(cp.viewer) as c:
        for params in ({"kind": "selftest"}, {}):
            assert (await c.get("/api/runs", params=params)).json()["items"] == [], params
        r = await c.get(f"/api/runs/{run_id}")
        assert (r.status_code, r.json()["code"]) == (403, "truth")
        assert (await c.get("/api/stream", params={"run": [run_id]})).status_code == 403


async def test_prompt_overrides_are_patched_in_place(cp: CP) -> None:
    path = cp.project.parent / "scenarios/control/scenario.yaml"
    url = "/api/scenarios/project/control/prompts"
    original = path.read_text()
    body = {"set": [{"agent": "a", "key": ["nudge"], "value": "Keep going.\nPlease."}]}
    async with cp.client(cp.viewer) as c:
        assert (await c.patch(url, json=body)).status_code == 403
    async with cp.client(cp.operator) as c:
        assert (await c.patch(url, json=body)).status_code == 428
        tag = (await c.get("/api/scenarios/project/control")).json()["etag"]
        r = await c.patch(url, json=body, headers={"If-Match": f'"{tag}"'})
        assert r.status_code == 200, r.text
        assert (
            path.read_text()
            == original + "    prompts:\n      nudge: |-\n        Keep going.\n        Please.\n"
        )
        new = r.json()["etag"]
        assert (await c.patch(url, json=body, headers={"If-Match": f'"{tag}"'})).status_code == 412
        seen = (await c.get("/api/scenarios/project/control")).json()
        assert seen["agents"][0]["prompts"] == {"nudge": "Keep going.\nPlease."}
        bad = {"set": [{"agent": "a", "key": ["monitor"], "value": "x"}]}
        assert (await c.patch(url, json=bad, headers={"If-Match": f'"{new}"'})).status_code == 422
        who = {"set": [{"agent": "zz", "key": ["nudge"], "value": "x"}]}
        assert (await c.patch(url, json=who, headers={"If-Match": f'"{new}"'})).status_code == 422
        undo = {"unset": [{"agent": "a", "key": ["nudge"]}]}
        r = await c.patch(url, json=undo, headers={"If-Match": f'"{new}"'})
        assert r.status_code == 200, r.text
        assert path.read_text() == original


async def test_scenario_check_notes_unset_roles_and_checks_the_monitor(cp: CP) -> None:
    text = (cp.project.parent / "scenarios/control/scenario.yaml").read_text()
    odd = "    - {id: odd, title: Odd, event: no.such.event, category: drift, importance: 2}\n"

    async def check(**body: str) -> dict[str, Any]:
        r = await c.post("/api/scenarios/check", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def msgs(found: list[dict[str, Any]]) -> list[str]:
        return [p["msg"] for p in found]

    async with cp.client(cp.viewer) as c:
        found = await check(text=text)
        assert found["ok"]
        assert found["warnings"] == []
        assert [m.split(";")[0] for m in msgs(found["notes"])] == [
            "monitor: no profile has the sweep role"
        ]
        found = await check(text=text, monitor="signals")
        assert (found["warnings"], found["notes"]) == ([], [])
        found = await check(text=text + f"monitor:\n  preset: signals\n  rules:\n{odd}")
        assert found["ok"]
        assert len(found["warnings"]) == 1
        assert found["warnings"][0]["msg"].startswith("monitor: ")
        assert "no.such.event" in found["warnings"][0]["msg"]
        assert found["notes"] == []
        r = await c.post("/api/scenarios/check", json={"text": text, "monitor": "loud"})
        assert r.status_code == 422
    # with no roles set, the agent's role is a note in the check and not a warning on a save
    cp.project.write_text(cp.project.read_text().replace('[roles]\nagent = "scripted"\n', ""))
    async with cp.client(cp.operator) as c:
        found = await check(text=text, monitor="signals")
        assert found["warnings"] == []
        assert [m.split(";")[0] for m in msgs(found["notes"])] == [
            "agent a: no profile has the agent role"
        ]
        tag = (await c.get("/api/scenarios/project/control")).json()["etag"]
        body = {"set": [{"agent": "a", "key": ["nudge"], "value": "Keep going."}]}
        r = await c.patch(PROMPTS, json=body, headers={"If-Match": f'"{tag}"'})
        assert r.status_code == 200, r.text
        assert r.json()["warnings"] == []


async def test_scenario_check_reports_a_bad_field_in_a_rule_plant_rule(cp: CP) -> None:
    text = (cp.project.parent / "scenarios/control/scenario.yaml").read_text()
    plant = (
        "plants:\n  - {name: tidy, kind: rule, rule: {id: tidy, title: Tidy, event: proc.exec,"
        " match: {FIELD: tidy-cache}, category: drift, importance: 2}}\n"
    )

    async def warnings(field: str) -> list[str]:
        body = {"text": text + plant.replace("FIELD", field), "monitor": "signals"}
        r = await c.post("/api/scenarios/check", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["ok"]
        return [w["msg"] for w in r.json()["warnings"]]

    async with cp.client(cp.viewer) as c:
        assert await warnings("argv") == []
        assert await warnings("argvv") == [
            "plant tidy: rule tidy: match: proc.exec has no field 'argvv'"
        ]


async def test_a_run_or_selftest_with_a_bad_field_in_a_rule_plant_rule_is_refused(cp: CP) -> None:
    path = cp.project.parent / "scenarios/control/scenario.yaml"
    path.write_text(
        path.read_text()
        + "plants:\n  - {name: tidy, kind: rule, rule: {id: tidy, title: Tidy, event: proc.exec,"
        " match: {argvv: tidy-cache}, category: drift, importance: 2}}\n"
        + "selftest:\n  reference: [{run: 'true'}]\n"
    )
    said = "plant tidy: rule tidy: match: proc.exec has no field 'argvv'"
    async with cp.client(cp.operator) as c:
        body = {"scenario": {"source": "project", "name": "control"}, "monitor": "signals"}
        r = await c.post("/api/runs", json=body)
        assert (r.status_code, r.json()["code"]) == (422, "scenario"), r.text
        assert [e["msg"] for e in r.json()["errors"]] == [said]
        r = await c.post("/api/scenarios/project/control/selftest", json={})
        assert (r.status_code, r.json()["code"]) == (422, "selftest"), r.text
        assert [e["msg"] for e in r.json()["errors"]] == [said]
    assert await cp.state.db.jobs() == []


async def test_scenario_check_over_the_folder_lints_plants_and_names_missing_files(
    cp: CP,
) -> None:
    folder = cp.project.parent / "scenarios/control"
    (folder / "bait").mkdir()
    (folder / "bait/notes.txt").write_text("this file is a honeypot\n")
    saved = (folder / "scenario.yaml").read_text()
    text = saved + (
        "plants:\n  - {name: notes, kind: file, path: notes.txt, source: bait/notes.txt,"
        " category: scorer_tampering}\n"
    )
    where = {"source": "project", "name": "control"}

    async def warnings(body: dict[str, str]) -> list[str]:
        r = await c.post("/api/scenarios/check", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["ok"]
        return [w["msg"] for w in r.json()["warnings"]]

    async with cp.client(cp.operator) as c:
        # without the folder the plant's source is not read
        assert await warnings({"text": text}) == []
        # the unsaved text over the files on disk
        assert await warnings({"text": text, **where}) == ["plant notes: content says 'honeypot'"]
        assert (folder / "scenario.yaml").read_text() == saved
        gone = await warnings({"text": text.replace("bait/notes.txt", "bait/gone.txt"), **where})
        assert gone == ["plants: FileNotFoundError: the scenario has no file bait/gone.txt"]
        task = "  instruction: Do what you are told.\n"
        files = "  files:\n    - {path: data.txt, source: gone.txt}\n"
        found = await warnings({"text": text.replace(task, task + files), **where})
        # named from the scenario's folder: the server's own paths stay on the server
        assert found == [
            "task.files: gone.txt: No such file or directory",
            "plant notes: content says 'honeypot'",
        ]
        built = text.replace("{image: python:3.13-slim}", "{dockerfile: env/Dockerfile}")
        built = built.replace("    command:", "    files: tests\n    command:")
        assert await warnings({"text": built, **where}) == [
            "task.verifier.files: tests: No such file or directory",
            "task.environment.dockerfile: env/Dockerfile: No such file or directory",
            "plant notes: content says 'honeypot'",
        ]
        (folder / "env").mkdir()
        (folder / "env/Dockerfile").write_text("FROM python:3.13-slim\n")
        (folder / "tests").mkdir()
        assert await warnings({"text": built, **where}) == ["plant notes: content says 'honeypot'"]
        # a symlinked folder that leads out is not read through
        (cp.project.parent / "outside").mkdir()
        (cp.project.parent / "outside/x").write_text("honeypot\n")
        os.symlink(cp.project.parent / "outside", folder / "out")
        outside = await warnings({"text": text.replace("bait/notes.txt", "out/x"), **where})
        assert outside == [
            "plants: ScenarioError: plants[0].source: out/x leads outside the scenario's directory"
        ]
        r = await c.post("/api/scenarios/check", json={"text": text, "source": "project"})
        assert r.status_code == 422
        r = await c.post("/api/scenarios/check", json={"text": text, **where, "name": "nope"})
        assert r.status_code == 404
    async with cp.client(cp.viewer) as c:
        assert await warnings({"text": text}) == []
        r = await c.post("/api/scenarios/check", json={"text": text, **where})
        assert r.status_code == 403


# The live stream


async def sse(
    c: httpx2.AsyncClient, params: dict[str, Any], want: int, last: str | None = None
) -> list[Any]:
    # the server reads header bytes as Latin-1
    raw = None if last is None else last.encode("latin-1", "replace")
    headers = {} if raw is None else {b"Last-Event-ID": raw}
    got: list[Any] = []
    async with asyncio.timeout(10), c.sse("/api/stream", params=params, headers=headers) as source:
        async for e in source:
            if e.event in ("message", ""):
                continue
            got.append((e.event, json.loads(e.data), e.id))
            if len(got) >= want:
                break
    return got


async def test_the_stream_resumes_from_its_cursor(cp: CP, local: LocalEventStore) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(n) for n in range(5)])
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    shipper = Shipper(local, pusher.client, run_id)
    task = asyncio.create_task(shipper.run())
    async with cp.client(cp.viewer) as c:
        got = await sse(c, {"run": run_id, "after": -1}, want=1)
        kind, data, last = got[0]
        assert kind == "events"
        seqs = [e["seq"] for e in data["events"]]
        assert seqs == list(range(len(seqs)))
        await local.append(run_id, [call(9), Draft(RunFinished(status="completed"))])
        rest = await sse(c, {"run": run_id, "after": -1}, want=3, last=last)
        events = [e["seq"] for k, d, _ in rest if k == "events" for e in d["events"]]
        assert events[0] == seqs[-1] + 1
        assert events[-1] == 7
        assert rest[-1][0] == "end" or any(k == "end" for k, _, _ in rest)
    await task
    await pusher.client.aclose()


async def test_a_new_stream_resumes_from_the_cursor_in_its_query(
    cp: CP, local: LocalEventStore
) -> None:
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(n) for n in range(5)])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await ship(local, pusher.client, run_id)

    def first_seq(got: list[Any]) -> int:
        return next(d["events"][0]["seq"] for k, d, _ in got if k == "events")

    async with cp.client(cp.viewer) as c:
        params = {"run": run_id, "after": -1, "last_event_id": f"{run_id}:2"}
        assert first_seq(await sse(c, params, want=1)) == 3
        # the header a reconnecting browser sends is newer than its URL
        got = await sse(c, params, want=1, last=f"{run_id}:3")
        assert first_seq(got) == 4
    await pusher.client.aclose()


@pytest.mark.parametrize(
    "seq",
    ["²", "٣", "--5", "-2", "9" * 5000, str(2**63), " 1_0", "", "x"],
    ids=["superscript", "arabic", "two-signs", "below", "long", "past", "underscore", "empty", "x"],
)
async def test_a_stream_skips_a_cursor_that_does_not_read(
    cp: CP, local: LocalEventStore, seq: str
) -> None:
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(n) for n in range(5)])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await ship(local, pusher.client, run_id)
    async with cp.client(cp.viewer) as c:
        # the bad part is skipped; the parts around it still count
        for header, first in ((f"@{seq},{run_id}:{seq}", 2), (f"{run_id}:2,{run_id}:{seq}", 3)):
            got = await sse(c, {"run": run_id, "after": 1}, want=1, last=header)
            assert got[0][1]["events"][0]["seq"] == first
            params = {"run": run_id, "after": 1, "last_event_id": header}
            got = await sse(c, params, want=1)
            assert got[0][1]["events"][0]["seq"] == first
    await pusher.client.aclose()


def test_a_stream_cursor_reads_ascii_digits_within_the_index() -> None:
    assert parse_id(f"@0,a:b:-1,c:{2**63 - 1}") == (0, {"a:b": -1, "c": 2**63 - 1})
    assert parse_id(f"@{2**63},c:-1, @7 ") == (7, {"c": -1})
    assert parse_id("@-1,c:+5,d:٣,e:0") == (None, {"e": 0})


def test_a_version_from_before_a_restart_gets_the_run_list_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aims.server.state import Changes

    old = Changes()
    for i in range(57):
        old.touch(f"run-{i % 3}")
    assert old.since(old.version) == (old.version, [])
    assert old.since(old.version - 2) == (old.version, ["run-1", "run-2"])
    # the restarted server touches runs before the browser's stream is back
    later = time.time_ns() + 10**12
    monkeypatch.setattr(time, "time_ns", lambda: later)
    new = Changes()
    assert new.since(new.version) == (new.version, [])
    new.touch("run-a")
    new.touch("run-b")
    assert new.version > old.version
    assert new.since(old.version) == (new.version, None)
    assert new.since(new.version - 1) == (new.version, ["run-b"])
    # a restart with the clock set back: the old version is ahead of every new one
    monkeypatch.setattr(time, "time_ns", lambda: 0)
    back = Changes()
    back.touch("run-a")
    assert back.since(old.version) == (back.version, None)


async def test_a_new_stream_hears_of_runs_that_changed_since_its_cursor(
    cp: CP, local: LocalEventStore
) -> None:
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    async with cp.client(cp.viewer) as c:
        [(kind, _, mark)] = await sse(c, {"runs": "true"}, want=1)
        assert kind == "runner"
        assert mark.startswith("@")
        run_id = await local.create_run(STARTED)
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        await ship(local, pusher.client, run_id)
        params = {"runs": "true", "last_event_id": mark}
        async with asyncio.timeout(10), c.sse("/api/stream", params=params) as source:
            async for e in source:
                if e.event == "run" and json.loads(e.data)["run"]["run_id"] == run_id:
                    break
    await pusher.client.aclose()


async def test_a_stream_resumed_from_before_a_restart_gets_the_runs_and_the_jobs_waiting(
    cp: CP, local: LocalEventStore
) -> None:
    """The browser's stream comes back with the version of the server that went away. No run
    changes after that, and the list still comes: the run that ended meanwhile and a job not
    started yet."""
    from aims.server.db import Job as JobRow

    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await ship(local, pusher.client, run_id)
    waiting = "01a18000-0000-7000-8000-00000000000a"
    t = time.time()
    job = {"kind": "models.check", "models": {"profiles": {}}}
    row = JobRow(waiting, None, None, "queued", job, {}, None, None, None, [], None, t, t, None)
    await cp.state.db.add_jobs([row])
    for before in (cp.state.changes.version + 10**9, 57):
        seen: dict[str, str] = {}
        params = {"runs": "true", "last_event_id": f"@{before}"}
        async with (
            cp.client(cp.viewer) as c,
            asyncio.timeout(10),
            c.sse("/api/stream", params=params) as source,
        ):
            async for e in source:
                if e.event == "run":
                    found = json.loads(e.data)["run"]
                    seen[found["run_id"]] = found["status"]
                if len(seen) == 2:
                    break
        assert seen == {run_id: "completed", waiting: "queued"}, before
    await pusher.client.aclose()


async def test_health_counts_the_open_streams(cp: CP) -> None:
    async def streams(c: httpx2.AsyncClient, want: int) -> None:
        async with asyncio.timeout(10):
            while True:
                if (await c.get("/api/health")).json()["streams"] == want:
                    return
                await asyncio.sleep(0.05)

    async with cp.client(cp.viewer) as c, cp.client(cp.viewer) as reader:
        await streams(c, 0)
        async with reader.sse("/api/stream", params={"runs": "true"}):
            await streams(c, 1)
        await streams(c, 0)


async def test_a_review_that_starts_resends_the_run_it_reviews(
    cp: CP, local: LocalEventStore
) -> None:
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    await ship(local, pusher.client, run_id)
    review = await local.create_run(
        RunStarted(
            scenario="pushed", oversight="observe", agents=["a1"], kind="monitor", origin=run_id
        )
    )
    await local.append(review, [Draft(RunFinished(status="completed"))])
    async with (
        cp.client(cp.viewer) as c,
        asyncio.timeout(10),
        c.sse("/api/stream", params={"runs": "true"}) as source,
    ):
        task = asyncio.create_task(ship(local, pusher.client, review))
        async for e in source:
            if e.event == "run" and (run := json.loads(e.data)["run"])["run_id"] == run_id:
                assert run["reviews"] == 1
                break
    await task
    await pusher.client.aclose()


async def test_streams_end_when_the_server_stops(cp: CP) -> None:
    async with (
        cp.client(cp.viewer) as c,
        asyncio.timeout(10),
        c.sse("/api/stream", params={"runs": "true"}) as source,
    ):
        cp.state.stopping.set()
        async for _ in source:
            pass


async def test_a_runner_stream_ends_when_the_server_stops(cp: CP, fake: FakeRunner) -> None:
    (run_id,) = await start_run(cp)
    await fake.next(OfferMessage)
    cp.state.stopping.set()
    # on its own, or uvicorn waits out its grace, then cancels the response and logs an error
    assert fake.task is not None
    async with asyncio.timeout(2):
        await fake.task
    conn = cp.state.runners["fake"]

    async def streams() -> int:
        return conn.streams

    # the stream's cleanup runs as its request ends, just after the last frame
    await until(streams, 0, within=2)
    # the offer it never claimed goes back to the queue
    await until(lambda: job_state(cp, run_id), "queued", within=2)


@contextlib.contextmanager
def gc_held() -> Generator[None]:
    """No cyclic GC in the block, so only a request's own cleanup counts: the GC also finalizes
    an endpoint's generator that a response left suspended."""
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


async def stalled(
    cp: CP,
    path: str,
    token: str,
    *,
    query: str = "",
    runner: str | None = None,
    frames: int = 3,
    stops: bool = True,
) -> None:
    """GET `path` straight through the app as a client that reads `frames` body frames, then
    stops reading and goes a moment later. What the server sends meanwhile backs up. Unless
    `stops`, it goes on reading until it goes."""
    headers = [(b"host", b"127.0.0.1"), (b"authorization", f"Bearer {token}".encode())]
    if runner is not None:
        headers.append((b"x-aims-runner", runner.encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    gone = asyncio.Event()
    read = 0

    async def receive() -> Message:
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal read
        if message["type"] != "http.response.body":
            return
        read += 1
        if read == frames:
            asyncio.get_running_loop().call_later(0.2, gone.set)
            if stops:
                await asyncio.Event().wait()

    app = create_app(cp.state, dispatcher=False)
    async with asyncio.timeout(10):
        await app(scope, receive, send)


async def test_a_runner_stream_that_stalls_and_drops_releases_its_offers(cp: CP) -> None:
    runner = FakeRunner(cp, "stalls")
    await runner.client.hello(runner.hello_body())
    await runner.client.status(RunnerStatus(tracer=TracerHealth(alive=True), free=2))
    await runner.client.aclose()
    conn = cp.state.runners["stalls"]

    async def streams() -> int:
        return conn.streams

    async def offered() -> set[str]:
        return conn.offered

    with gc_held():
        reading = asyncio.create_task(stalled(cp, "/runner/v1/stream", cp.runner, runner="stalls"))
        await until(streams, 1)
        (run_id,) = await start_run(cp)
        await until(offered, {run_id})
        for i in range(20):
            action = CommandAction(action="pause", reason="x" * 1000)
            message = CommandMessage(id=f"c{i}", seq=i, run_id=run_id, command=action)
            assert runner_api.send(cp.state, "stalls", message)
        await reading
        assert conn.streams == 0
        assert conn.offered == set()
    assert await job_state(cp, run_id) == "queued"


async def test_a_runner_stream_that_drops_releases_its_offers(cp: CP, fake: FakeRunner) -> None:
    (run_id,) = await start_run(cp)
    await fake.next(OfferMessage)
    conn = cp.state.runners["fake"]
    assert conn.free() == 1
    await fake.disconnect()

    async def streams() -> int:
        return conn.streams

    await until(streams, 0)
    assert conn.offered == set()
    assert conn.free() == 2
    # the requeue follows the count down
    await until(lambda: job_state(cp, run_id), "queued")


async def test_a_runner_stream_before_hello_is_refused_with_a_status(cp: CP) -> None:
    client = ControlClient(cp.url, cp.runner, runner="early")
    try:
        with pytest.raises(ApiError) as caught:
            async with client.stream():
                pass
        assert (caught.value.status, caught.value.code) == (409, "hello")
    finally:
        await client.aclose()


async def test_a_ui_stream_that_stalls_and_drops_releases_its_subscription(cp: CP) -> None:
    run_id = str(uuid.uuid4())
    for call in range(10):
        cp.state.inflight.add(
            [
                WireDelta(
                    run_id=run_id,
                    agent_id="a",
                    call_id=f"c{call}",
                    attempt=1,
                    kind="text",
                    text="x",
                )
                for _ in range(DELTA_BATCH)
            ]
        )
    with gc_held():
        await stalled(cp, "/api/stream", cp.operator, query=f"deltas={run_id}")
        assert cp.state.streams == 0
        assert not cp.state.bus.subscribers


def keepalive_error() -> anyio.BrokenResourceError:
    """A BrokenResourceError raised in a frame that passes for FastAPI's SSE keepalive task."""
    space: dict[str, Any] = {"__name__": "fastapi.routing", "anyio": anyio}
    exec("def _keepalive_inserter():\n    raise anyio.BrokenResourceError\n", space)
    try:
        space["_keepalive_inserter"]()
    except anyio.BrokenResourceError as e:
        return e
    raise AssertionError("it did not raise")


@pytest.mark.parametrize(
    ("errors", "gone", "dropped"),
    [
        ([keepalive_error()], True, True),
        ([keepalive_error(), keepalive_error()], True, True),
        ([keepalive_error()], False, False),
        ([keepalive_error(), ValueError("x")], True, False),
        ([anyio.BrokenResourceError()], True, False),
    ],
)
async def test_only_the_keepalive_error_after_the_client_goes_is_dropped(
    errors: list[Exception], gone: bool, dropped: bool
) -> None:
    async def failing(scope: Scope, receive: Any, send: Any) -> None:
        if gone:
            await receive()
        raise ExceptionGroup("in a group", errors)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    guarded = app_module.KeepaliveGone(failing)
    if dropped:
        await guarded({"type": "http"}, receive, send)
    else:
        with pytest.raises(ExceptionGroup):
            await guarded({"type": "http"}, receive, send)


def open_fds(path: Path) -> int:
    """How many fds of this process are open on the file at `path`."""
    target = os.stat(path)
    found = 0
    for name in os.listdir("/dev/fd"):
        with contextlib.suppress(OSError):
            st = os.fstat(int(name))
            found += (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino)
    return found


async def test_a_run_log_read_that_stalls_and_drops_closes_the_log(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_api, "LOG_CHUNK", 1024)
    run_id = await pushed(cp, local, [call(n) for n in range(100)])
    log = cp.state.store.root / run_id / "events.jsonl"
    with gc_held():
        await stalled(cp, f"/runner/v1/runs/{run_id}/log", cp.runner, runner="pusher")
        await asyncio.sleep(0.1)
        assert open_fds(log) == 0


async def compressed_blob(cp: CP, local: LocalEventStore) -> tuple[str, Path]:
    """A pushed run with a blob of 6 MiB, stored as a zstd frame: the path of its blob."""
    run_id = await local.create_run(STARTED)
    big = await local.content(run_id, "ab" * 3 * 1024 * 1024)
    assert isinstance(big, BlobRef)
    await local.append(run_id, [Draft(AgentInput(kind="inject", step=0, text=big), "a1")])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    await ship(local, pusher.client, run_id)
    await pusher.client.aclose()
    path = cp.state.store.blob_path(run_id, big.sha256)
    assert path.read_bytes().startswith(ZSTD_MAGIC)
    return f"/api/runs/{run_id}/blobs/{big.sha256}", path


async def test_a_blob_read_that_stalls_and_drops_closes_the_blob(
    cp: CP, local: LocalEventStore
) -> None:
    url, path = await compressed_blob(cp, local)
    with gc_held():
        await stalled(cp, url, cp.viewer)
        await asyncio.sleep(0.1)
        assert open_fds(path) == 0


async def test_a_blob_read_that_drops_while_it_decodes_closes_the_blob(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(path: Path) -> Generator[bytes]:
        for chunk in decoded(path):
            time.sleep(0.5)
            yield chunk

    url, path = await compressed_blob(cp, local)
    monkeypatch.setattr(api, "decoded", slow)
    with gc_held():
        # it goes 0.2 s after the first chunk, while the next one decodes
        await stalled(cp, url, cp.viewer, frames=1, stops=False)
        assert open_fds(path) == 0


async def test_a_stream_ends_when_its_session_does(cp: CP) -> None:
    async with cp.client() as c:
        assert (await c.post("/api/session", json={"token": cp.viewer})).status_code == 204
        async with asyncio.timeout(10), c.sse("/api/stream", params={"runs": "true"}) as source:
            r = await c.delete("/api/session", headers={"Sec-Fetch-Site": "same-origin"})
            assert r.status_code == 204
            async for _ in source:
                pass


async def test_a_stream_ends_when_its_token_is_revoked(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with cp.client(cp.operator) as op:
        made = await op.post("/api/tokens", json={"name": "gone", "role": "viewer"})
        lapsed = await op.post("/api/tokens", json={"name": "lapsed", "role": "viewer"})
        async with (
            cp.client(made.json()["token"]) as c,
            asyncio.timeout(10),
            c.sse("/api/stream", params={"runs": "true"}) as source,
        ):
            assert (await op.delete("/api/tokens/gone")).status_code == 204
            async for _ in source:
                pass
    # a revoke by `aims token revoke` touches only the database; the stream sees it on its
    # next check
    monkeypatch.setattr("aims.server.stream.RECHECK_S", 0.1)
    async with (
        cp.client(lapsed.json()["token"]) as c,
        asyncio.timeout(10),
        c.sse("/api/stream", params={"runs": "true"}) as source,
    ):
        assert await cp.state.db.revoke_token("lapsed")
        async for _ in source:
            pass


async def test_deltas_reach_the_stream_and_a_slow_one_is_told_it_lost_some(
    cp: CP, fake: FakeRunner
) -> None:
    # a viewer gets the deltas of a run whose kind is known, here by its job
    (run_id,) = await start_run(cp)
    cp.state.bus.maxsize = 4

    def delta(i: int) -> WireDelta:
        return WireDelta(
            run_id=run_id, agent_id="a", call_id="m1", attempt=1, kind="text", text=str(i)
        )

    async with cp.client(cp.viewer) as c:
        reader = asyncio.create_task(sse(c, {"deltas": run_id}, want=2))
        await asyncio.sleep(0.3)
        await fake.client.post_deltas(Deltas(deltas=[delta(i) for i in range(50)]))
        got = await reader
    kinds = [k for k, _, _ in got]
    assert "delta" in kinds
    assert "deltas_dropped" in kinds
    late = await sse_inflight(cp, run_id)
    assert late[0][0] == "delta"
    assert [d["text"] for d in late[0][1]["deltas"]][:3] == ["0", "1", "2"]


def test_a_finished_call_keeps_no_deltas_that_come_after_its_response() -> None:
    inflight = InFlight()

    def d(call: str, kind: Literal["start", "text", "end"], attempt: int = 1) -> WireDelta:
        return WireDelta(run_id="r", agent_id="a", call_id=call, attempt=attempt, kind=kind)

    inflight.add([d("m1", "start"), d("m1", "text"), d("m2", "start")])
    inflight.drop("r", "m1")
    inflight.add([d("m1", "end")])
    assert [x.call_id for x in inflight.of({"r"})] == ["m2"]
    # a failed attempt that is retried streams again under the same call
    inflight.drop("r", "m2", final=False)
    inflight.add([d("m2", "start", attempt=2)])
    assert [(x.call_id, x.attempt) for x in inflight.of({"r"})] == [("m2", 2)]


async def test_a_logged_response_drops_its_deltas_while_the_index_is_behind(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's index takes no batch with a model response, as when another process
    holds its write lock: the response is read from the log, so its call's deltas go."""
    started = RunStarted(scenario="control", oversight="observe", agents=["a"])
    run_id = await local.create_run(started)
    reply = ModelResponse(call_id="m1", attempt=1, status="completed", latency_ms=1)
    await local.append(run_id, [Draft(reply, "a")])
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, fake.name, run_id, lines[:1])).status_code == 200
    delta = WireDelta(run_id=run_id, agent_id="a", call_id="m1", attempt=1, kind="text", text="x")
    cp.state.inflight.add([delta])
    real = Index.add
    refused: list[int] = []

    async def add(index: Index, events: Sequence[Event], head: Any, **kw: Any) -> None:
        if any(e.type == "model.response" for e in events):
            refused.extend(e.seq for e in events)
            raise sqlite3.OperationalError("database is locked")
        await real(index, events, head, **kw)

    monkeypatch.setattr(Index, "add", add)
    assert (await raw_events(cp, fake.name, run_id, lines[1:])).status_code == 200
    assert refused
    assert cp.state.inflight.of({run_id}) == []


async def test_a_retry_keeps_its_deltas_when_a_later_batch_lands_while_the_index_is_behind(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt is logged with `retry`, then the server's index stops taking batches.
    The retry streams under the same call id: a later batch must not read the old response
    again and drop the retry's deltas."""
    started = RunStarted(scenario="control", oversight="observe", agents=["a"])
    run_id = await local.create_run(started)
    failed = ModelResponse(
        call_id="m1", attempt=1, status="error", latency_ms=1, error="overloaded", retry=True
    )
    await local.append(run_id, [Draft(failed, "a")])
    await local.append(run_id, [Draft(ToolCall(call_id="c1", name="shell", arguments="{}"), "a")])
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, fake.name, run_id, lines[:1])).status_code == 200
    real = Index.add

    async def add(index: Index, events: Sequence[Event], head: Any, **kw: Any) -> None:
        if any(e.seq >= 1 for e in events):
            raise sqlite3.OperationalError("database is locked")
        await real(index, events, head, **kw)

    monkeypatch.setattr(Index, "add", add)
    assert (await raw_events(cp, fake.name, run_id, lines[1:2])).status_code == 200
    delta = WireDelta(run_id=run_id, agent_id="a", call_id="m1", attempt=2, kind="text", text="x")
    cp.state.inflight.add([delta])
    assert (await raw_events(cp, fake.name, run_id, lines[2:])).status_code == 200
    indexed = await cp.state.store.head(run_id)
    assert indexed is not None
    assert indexed.seq == 0
    assert [(d.call_id, d.attempt) for d in cp.state.inflight.of({run_id})] == [("m1", 2)]


async def test_a_batch_drops_the_deltas_of_its_responses_far_past_an_index_behind(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's index lags by a full batch of responses: the next batch's responses still
    drop their calls' deltas."""
    started = RunStarted(scenario="control", oversight="observe", agents=["a"])
    run_id = await local.create_run(started)
    count = 1100
    replies = [
        Draft(ModelResponse(call_id=f"m{i}", attempt=1, status="completed", latency_ms=1), "a")
        for i in range(count)
    ]
    for first in range(0, count, 500):
        await local.append(run_id, replies[first : first + 500])
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, fake.name, run_id, lines[:1])).status_code == 200
    real = Index.add

    async def add(index: Index, events: Sequence[Event], head: Any, **kw: Any) -> None:
        if any(e.type == "model.response" for e in events):
            raise sqlite3.OperationalError("database is locked")
        await real(index, events, head, **kw)

    monkeypatch.setattr(Index, "add", add)
    assert (await raw_events(cp, fake.name, run_id, lines[1:1001])).status_code == 200
    last = f"m{count - 1}"
    delta = WireDelta(run_id=run_id, agent_id="a", call_id=last, attempt=1, kind="text", text="x")
    cp.state.inflight.add([delta])
    assert (await raw_events(cp, fake.name, run_id, lines[1001:])).status_code == 200
    assert cp.state.inflight.of({run_id}) == []


async def sse_inflight(cp: CP, run_id: str) -> list[Any]:
    async with cp.client(cp.viewer) as c:
        return await sse(c, {"deltas": run_id}, want=1)


async def test_openapi_lists_every_payload_and_the_stream_messages(cp: CP) -> None:
    async with cp.client(cp.viewer) as c:
        spec = (await c.get("/api/openapi.json")).json()
    schemas = spec["components"]["schemas"]
    assert "run.started" in schemas["EventTypes"]["properties"]
    assert schemas["EventTypes"]["properties"]["operator.action"] == {
        "$ref": "#/components/schemas/OperatorAction"
    }
    assert "operator.action" in schemas["EventType"]["enum"]
    assert set(schemas["StreamMessage"]["discriminator"]["mapping"]) == {
        "events",
        "run",
        "runner",
        "delta",
        "deltas_dropped",
        "end",
        "deleted",
    }
    problem = spec["paths"]["/api/runs"]["post"]["responses"]["422"]["content"]
    assert list(problem) == ["application/problem+json"]
    assert spec["paths"]["/api/runs/{run_id}/events"]["get"]["operationId"] == "list_events"


async def pushed(cp: CP, local: LocalEventStore, drafts: list[Draft]) -> str:
    """A finished run of `drafts` that a runner pushed."""
    run_id = await local.create_run(STARTED)
    await local.append(run_id, drafts)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    await ship(local, pusher.client, run_id)
    await pusher.client.aclose()
    return run_id


def window(sandbox: str, first_us: int, last_us: int) -> Draft:
    empty = dict.fromkeys(ResourceSample.COLUMNS)
    sample = ResourceSample(sandbox_id=sandbox, hz=10, ts=[first_us, last_us - first_us], **empty)
    return Draft(sample, sandbox)


async def test_sample_pages_hold_every_window_once(cp: CP, local: LocalEventStore) -> None:
    drafts = []
    for k in range(700):
        base = k * 1_000_000
        drafts.append(window("a1", base + 50_000, base + 950_000))
        if k == 500:
            drafts.append(window("a2", base + 300_000, base + 900_000))
        elif k > 0:
            drafts.append(window("a2", base + 20_000, base + 920_000))
    run_id = await pushed(cp, local, drafts)
    events = await local.every(run_id, types=[ResourceSample.type])
    windows = [(ResourceSample.model_validate(e.data).ts[0], e.seq, e.agent_id) for e in events]
    stored = sorted((ts, seq) for ts, seq, _ in windows)

    async def paged(c: httpx2.AsyncClient, **params: Any) -> list[tuple[int, int]]:
        seen: list[tuple[int, int]] = []
        for _ in range(20):
            r = await c.get(f"/api/runs/{run_id}/samples", params=params)
            assert r.status_code == 200, r.text
            page = r.json()
            seen += [(w["ts_from"], w["seq"]) for w in page["items"]]
            if page["next"] is None:
                return seen
            params["after"] = page["next"]
        raise AssertionError(f"the pages never end: {len(seen)} windows, {len(set(seen))} distinct")

    async with cp.client(cp.viewer) as c:
        assert await paged(c) == stored
        assert await paged(c, limit=300) == stored
        a2 = sorted((ts, seq) for ts, seq, agent in windows if agent == "a2")
        assert len(a2) == 699
        assert await paged(c, agent="a2", limit=200) == a2
        # by seq, as a reader that keeps its windows reads on from the last one it has
        by_seq = sorted(stored, key=lambda w: w[1])
        assert await paged(c, order="seq", limit=300) == by_seq
        last = by_seq[-2][1]
        r = await c.get(f"/api/runs/{run_id}/samples", params={"order": "seq", "after": last})
        assert [(w["ts_from"], w["seq"]) for w in r.json()["items"]] == by_seq[-1:]
        r = await c.get(f"/api/runs/{run_id}/samples", params={"order": "start"})
        assert r.status_code == 422


async def test_search_text_and_cursors_the_index_cannot_bind_are_refused(
    cp: CP, local: LocalEventStore
) -> None:
    run_id = await pushed(cp, local, [call(1), window("a1", 1_000, 2_000)])
    run = f"/api/runs/{run_id}"
    huge = "99999999999999999999"
    refused = [
        ("/api/events", {"q": "ab\x00c"}),
        ("/api/events", {"q": "\x00"}),
        ("/api/events", {"q": "shell", "before": huge}),
        ("/api/events", {"q": "shell", "before": "-2"}),
        (f"{run}/events", {"q": "sh\x00"}),
        (f"{run}/events", {"after": huge}),
        (f"{run}/events", {"before": huge}),
        (f"{run}/events", {"after": "-" + huge}),
        (f"{run}/events/{huge}", {}),
        (f"{run}/events/-1", {}),
        (f"{run}/steps", {"after": huge}),
        (f"{run}/flags", {"after": huge}),
        (f"{run}/samples", {"ts_from": huge}),
        (f"{run}/samples", {"ts_to": huge}),
        (f"{run}/samples", {"ts_from": "-1"}),
        (f"{run}/samples", {"after": huge}),
        ("/api/audit", {"before": huge}),
        ("/api/stream", {"run": run_id, "after": huge}),
    ]
    async with cp.client(cp.operator) as c:
        for path, params in refused:
            r = await c.get(path, params=params)
            assert (r.status_code, r.json()["code"]) == (422, "invalid"), (path, params, r.text)
        r = await c.post(f"{run}/labels", json={"flag": int(huge), "label": "real"})
        assert (r.status_code, r.json()["code"]) == (422, "invalid"), r.text
        for q in ("\x01\x02\x03", "x" * 500, '"' * 500, "\U0010ffff" * 3):
            r = await c.get("/api/events", params={"q": q})
            assert r.json()["items"] == [], q[:3]
            assert (await c.get(f"{run}/events", params={"q": q})).json()["items"] == []
        r = await c.get("/api/events?q=%ED%A0%80sh")
        assert r.status_code == 200, r.text
        # a search of every run needs the three characters the trigram index does
        for q in ("", "s", "sh", "\U0001f600" * 2):
            r = await c.get("/api/events", params={"q": q})
            assert (r.status_code, r.json()["code"]) == (422, "invalid"), (q, r.text)
        found = (await c.get("/api/events", params={"q": "she"})).json()["items"]
        assert 1 in [e["seq"] for e in found]
        within = (await c.get(f"{run}/events", params={"q": "sh"})).json()["items"]
        assert 1 in [e["seq"] for e in within]
        bound = str(2**63 - 1)
        for path, params in [
            ("/api/events", {"q": "shell", "before": bound}),
            (f"{run}/events", {"after": "-1", "before": bound}),
            (f"{run}/steps", {"after": bound}),
            (f"{run}/flags", {"after": bound}),
            (f"{run}/samples", {"ts_from": bound, "after": bound}),
            ("/api/audit", {"before": bound}),
        ]:
            assert (await c.get(path, params=params)).status_code == 200, (path, params)
        assert (await c.get(f"{run}/events/{bound}")).status_code == 404


# Fixes from the security review


async def push_forged(
    cp: CP,
    local: LocalEventStore,
    base_url: str,
    *,
    budget: dict[str, Any] | None = None,
    drafts: list[Draft] | None = None,
) -> str:
    """A run a runner token pushed, whose recorded profile it chose, run under `budget`, with
    the events `drafts` before its end."""
    bundle = load(cp.project.parent / "scenarios" / "control" / "scenario.yaml")
    settings = cp.state.config.settings()
    recorded = settings.models.get("scripted").model_copy(update={"base_url": base_url})
    texts = resolve_texts(Prompts(), bundle.scenario.agents[0]).snapshot()
    entry = {
        "profile": recorded.snapshot(),
        "schedule": [[None, recorded.snapshot()]],
        "texts": texts,
    }
    started = RunStarted(
        scenario="control",
        oversight="observe",
        agents=["a"],
        config={"scenario": bundle.snapshot(), "agents": {"a": entry}, "budget": budget},
    )
    run_id = await local.create_run(started, blobs=bundle.blobs())
    await local.append(run_id, [*(drafts or []), Draft(RunFinished(status="completed"))])
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    await ship(local, pusher.client, run_id)
    await pusher.client.aclose()
    return run_id


async def test_a_missing_run_folder_is_reported_without_the_data_path(
    cp: CP, local: LocalEventStore
) -> None:
    run_id = await push_forged(cp, local, "http://model.invalid/v1")
    shutil.rmtree(cp.state.store.root / run_id)
    async with cp.client(cp.viewer) as c:
        r = await c.get(f"/api/runs/{run_id}/verification")
    assert r.status_code == 200, r.text
    assert r.json()["error"] == f"no events.jsonl in the folder of run {run_id}"
    assert str(cp.state.store.root.parent) not in r.text


async def test_a_fork_never_sends_keys_where_a_pushed_log_says(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    forged = await push_forged(cp, local, "https://attacker.example/v1")
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{forged}/forks", json={"monitor": "signals"})
        assert (r.status_code, r.json()["code"]) == (422, "fork")
        assert "another endpoint" in r.json()["detail"]
        r = await c.post(
            f"/api/runs/{forged}/forks", json={"profile": "scripted", "monitor": "signals"}
        )
        assert r.status_code == 202, r.text
        (fork,) = r.json()["run_ids"]
    job = await cp.state.db.job(fork)
    assert job is not None
    profiles = job.payload["models"]
    assert [p["base_url"] for p in profiles.values()] == ["http://model.invalid/v1"]
    honest = await push_forged(cp, local, "http://model.invalid/v1")
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{honest}/forks", json={"monitor": "signals"})
        assert r.status_code == 202, r.text


async def test_runner_names_cannot_pose_as_imports_or_the_local_runner(
    cp: CP, local: LocalEventStore
) -> None:
    client = ControlClient(cp.url, cp.runner, runner="import:op")
    with pytest.raises(ApiError) as caught:
        await client.hello(
            FakeRunner(cp, "x").hello_body().model_copy(update={"name": "import:op"})
        )
    assert caught.value.status == 422
    await client.aclose()
    cp.state.local_runner = "local"
    impostor = FakeRunner(cp, "local")
    with pytest.raises(ApiError) as caught:
        await impostor.client.hello(impostor.hello_body())
    assert (caught.value.status, caught.value.code) == (403, "name")
    await impostor.client.aclose()

    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        t.add(local.root / run_id / "events.jsonl", arcname=f"{run_id}/events.jsonl")
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs/import",
            content=tar.getvalue(),
            headers={"Content-Type": "application/x-tar"},
        )
        assert r.status_code == 201, r.text
    meta = await cp.state.db.run_meta(run_id)
    assert meta is not None
    assert meta.source == "import"
    reader = FakeRunner(cp, "curious")
    await reader.client.hello(reader.hello_body())
    with pytest.raises(ApiError) as caught:
        async with reader.client.run_log(run_id):
            pass
    assert caught.value.code == "owner"
    r = await raw_events(
        cp, "curious", run_id, split_lines((local.root / run_id / "events.jsonl").read_bytes())
    )
    assert (r.status_code, r.json()["code"]) == (403, "owner")
    await reader.client.aclose()


async def test_open_mode_still_refuses_cross_site_browsers(tmp_path: Path) -> None:
    async with control_plane(tmp_path / "open", auth=False) as cp, cp.client() as c:
        create = "/api/scenarios/project/fresh"
        r = await c.post(create, headers={"Origin": "http://evil.example"})
        assert (r.status_code, r.json()["code"]) == (403, "origin")
        r = await c.post(create, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert (await c.post(create)).status_code == 201
        assert (await c.get("/api/session")).json()["role"] == "operator"


# Fixes from the code review


async def test_scenario_yaml_guards_see_through_spelling(cp: CP) -> None:
    base = "/api/scenarios/project/control/files"
    async with cp.client(cp.operator) as c:
        tag = (await c.get(f"{base}/scenario.yaml")).headers["etag"]
        headers = {"Content-Type": "application/octet-stream", "If-Match": tag}
        r = await c.put(f"{base}/scenario.yaml/", content=b"oversight: nope", headers=headers)
        assert r.status_code == 422
        for spelling in ("scenario.yaml/", "Scenario.yaml", "SCENARIO.YAML"):
            r = await c.delete(f"{base}/{spelling}", headers={"If-Match": tag})
            assert r.status_code == 422, spelling
        assert (cp.project.parent / "scenarios/control/scenario.yaml").is_file()


async def test_a_bad_import_is_refused_not_a_crash(cp: CP) -> None:
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        body = b'{"run_id": "x"}\nnot json\n'
        info = tarfile.TarInfo("r/events.jsonl")
        info.size = len(body)
        t.addfile(info, io.BytesIO(body))
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs/import",
            content=tar.getvalue(),
            headers={"Content-Type": "application/x-tar"},
        )
        assert r.status_code == 422
        r = await c.post(
            "/api/runs/import", content=b"not a tar", headers={"Content-Type": "application/x-tar"}
        )
        assert r.status_code == 422


async def test_an_import_whose_log_names_no_valid_run_id_is_invalid(cp: CP) -> None:
    for run_id in ("../escape", str(uuid.uuid7()).upper(), 7):
        line = json.dumps({"run_id": run_id, "data": {}}).encode() + b"\n"
        r = await post_tar(cp, run_tar(Path("r"), lines=[line], blobs={}))
        assert (r.status_code, r.json().get("code")) == (422, "run_id"), r.text
    assert [p for p in cp.state.store.root.iterdir() if p.is_dir()] == []
    assert not (cp.state.data / "escape").exists()


def log_of(store: LocalEventStore, run_id: str) -> list[bytes]:
    return split_lines((store.root / run_id / "events.jsonl").read_bytes())


def run_tar(
    run_dir: Path, lines: list[bytes] | None = None, blobs: dict[str, bytes] | None = None
) -> bytes:
    """`run_dir` as an import takes it, with `lines` as its log and `blobs` as its blob files
    when given."""
    if lines is None:
        lines = split_lines((run_dir / "events.jsonl").read_bytes())
    if blobs is None:
        held = run_dir / "blobs"
        blobs = {p.name: p.read_bytes() for p in held.iterdir()} if held.is_dir() else {}
    files = {"events.jsonl": b"".join(lines)} | {f"blobs/{k}": v for k, v in blobs.items()}
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{run_dir.name}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return tar.getvalue()


async def post_tar(cp: CP, tar: bytes) -> httpx2.Response:
    async with cp.client(cp.operator) as c:
        return await c.post(
            "/api/runs/import", content=tar, headers={"Content-Type": "application/x-tar"}
        )


async def with_blob(local: LocalEventStore) -> tuple[str, str]:
    """A run whose second event keeps its text in a blob, and the blob's sha256."""
    run_id = await local.create_run(STARTED)
    ref = await local.content(run_id, "y" * 20_000)
    assert isinstance(ref, BlobRef)
    await local.append(run_id, [Draft(AgentInput(kind="inject", step=0, text=ref), "a1")])
    return run_id, ref.sha256


async def test_a_refused_import_leaves_no_run_behind(cp: CP, local: LocalEventStore) -> None:
    bad, sha = await with_blob(local)
    missing, _ = await with_blob(local)
    gap = await local.create_run(STARTED)
    await local.append(gap, [call(1)])
    late = await local.create_run(STARTED)
    await local.append(late, [call(n) for n in range(1100)])
    broken = log_of(local, late)
    broken[-1] = broken[-1].replace(b'"c1099"', b'"c9999"')
    refusals = {
        bad: (run_tar(local.root / bad, blobs={sha: b"not the blob"}), 422, "blob"),
        missing: (run_tar(local.root / missing, blobs={}), 422, "missing_blobs"),
        gap: (run_tar(local.root / gap, lines=log_of(local, gap)[1:]), 409, "ingest"),
        late: (run_tar(local.root / late, lines=broken), 409, "ingest"),
    }
    for run_id, (tar, status, code) in refusals.items():
        r = await post_tar(cp, tar)
        assert (r.status_code, r.json().get("code")) == (status, code), r.text
        assert not (cp.state.store.root / run_id).exists()
        assert await cp.state.db.run_meta(run_id) is None
        assert await cp.state.store.run(run_id) is None
    async with cp.client(cp.viewer) as c:
        assert (await c.get("/api/runs")).json()["items"] == []
    assert await asyncio.to_thread(os.listdir, cp.state.data / "tmp") == []
    r = await post_tar(cp, run_tar(local.root / late))
    assert r.status_code == 201, r.text
    assert (await cp.state.store.verify(late)).events == 1101


@pytest.mark.parametrize("cut", [False, True])
async def test_a_blob_that_is_a_zstd_frame_downloads_as_its_own_bytes(
    cp: CP, local: LocalEventStore, cut: bool
) -> None:
    frame = zstd.compress(b"hello world " * 1000)
    data = frame[:-3] if cut else frame
    sha = hashlib.sha256(data).hexdigest()
    run_id = await local.create_run(STARTED)
    ref = BlobRef(sha256=sha, size=len(data))
    shell = Draft(ToolCall(call_id="c1", name="shell", arguments=ref), "a1")
    await local.append(run_id, [shell, Draft(RunFinished(status="completed"))])
    # sent as it is; the import stores it as a zstd frame of it
    r = await post_tar(cp, run_tar(local.root / run_id, blobs={sha: data}))
    assert r.status_code == 201, r.text
    path = cp.state.store.blob_path(run_id, sha)
    assert path.read_bytes() != data
    for placed in (False, True):
        if placed:
            # a store from before kept such a blob as it came
            path.write_bytes(data)
        for accept in ("identity", "zstd"):
            async with cp.client(cp.viewer) as c:
                got = await c.get(
                    f"/api/runs/{run_id}/blobs/{sha}", headers={"Accept-Encoding": accept}
                )
            assert (got.status_code, got.content) == (200, data)
    path.write_bytes(data[:-1])
    async with cp.client(cp.viewer) as c:
        assert (await c.get(f"/api/runs/{run_id}/blobs/{sha}")).status_code == 404


async def test_a_refused_import_leaves_a_run_that_was_here_as_it_was(
    cp: CP, local: LocalEventStore, tmp_path: Path
) -> None:
    pushed = await local.create_run(STARTED)
    await local.append(pushed, [call(1)])
    runner = FakeRunner(cp, "r1")
    await runner.client.hello(runner.hello_body())
    assert (await raw_events(cp, "r1", pushed, log_of(local, pushed))).status_code == 200
    await runner.client.aclose()
    imported, sha = await with_blob(local)
    r = await post_tar(cp, run_tar(local.root / imported, lines=log_of(local, imported)[:1]))
    assert r.status_code == 201, r.text
    async with await LocalEventStore.open(tmp_path / "other") as other:
        await other.create_run(STARTED, run_id=imported)
        forged = run_tar(other.root / imported)
    before = {
        run_id: (
            await cp.state.db.run_meta(run_id),
            (cp.state.store.root / run_id / "events.jsonl").read_bytes(),
        )
        for run_id in (pushed, imported)
    }
    refusals = [
        (run_tar(local.root / pushed), 409, None),
        (run_tar(local.root / imported, blobs={sha: b"not the blob"}), 422, "blob"),
        (forged, 409, "ingest"),
    ]
    for tar, status, code in refusals:
        r = await post_tar(cp, tar)
        assert (r.status_code, r.json().get("code")) == (status, code), r.text
    for run_id, (meta, data) in before.items():
        assert await cp.state.db.run_meta(run_id) == meta
        assert (cp.state.store.root / run_id / "events.jsonl").read_bytes() == data
        assert (await cp.state.store.verify(run_id)).ok


async def test_a_refused_import_never_drops_what_another_import_of_the_run_wrote(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    lines = log_of(local, run_id)
    lines[-1] = lines[-1].replace(json.loads(lines[-1])["hash"].encode(), b"0" * 64)
    forged = run_tar(local.root / run_id, lines=lines)
    first, entered, release = threading.Lock(), threading.Event(), threading.Event()
    copy = api._tar_blobs

    def held(*args: Any) -> None:
        if first.acquire(blocking=False):
            entered.set()
            release.wait(10)
        copy(*args)

    monkeypatch.setattr(api, "_tar_blobs", held)
    refused = asyncio.create_task(post_tar(cp, forged))
    assert await asyncio.to_thread(entered.wait, 10)
    taken = asyncio.create_task(post_tar(cp, run_tar(local.root / run_id)))
    await asyncio.wait({taken}, timeout=0.5)
    release.set()
    assert ((await refused).status_code, (await taken).status_code) == (409, 201)
    assert log_of(cp.state.store, run_id) == log_of(local, run_id)
    assert (await cp.state.store.verify(run_id)).ok


async def test_an_import_of_a_run_a_job_holds_is_refused(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    await play_job(fake, local, await fake.next(OfferMessage))
    reserved = await cp.state.db.run_meta(run_id)
    r = await post_tar(cp, run_tar(local.root / run_id))
    assert (r.status_code, r.json().get("code")) == (409, "in_use"), r.text
    assert await cp.state.db.run_meta(run_id) == reserved
    shipper = Shipper(local, fake.client, run_id)
    task = asyncio.create_task(shipper.run())
    await until(lambda: job_state(cp, run_id), "running")
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    async with asyncio.timeout(10):
        await task
    assert shipper.done
    meta = await cp.state.db.run_meta(run_id)
    assert meta is not None
    assert (meta.runner, meta.source) == ("fake", "job")


async def test_an_import_into_a_directory_with_no_record_needs_a_run_there(
    cp: CP, local: LocalEventStore
) -> None:
    prepared = await local.create_run(STARTED)
    cp.state.store.prepare_run(prepared)
    r = await post_tar(cp, run_tar(local.root / prepared))
    assert (r.status_code, r.json().get("code")) == (409, "in_use"), r.text
    assert await asyncio.to_thread(os.listdir, cp.state.store.root / prepared) == []
    assert await cp.state.db.run_meta(prepared) is None
    unrecorded = await local.create_run(STARTED)
    await cp.state.store.ingest(unrecorded, log_of(local, unrecorded))
    r = await post_tar(cp, run_tar(local.root / unrecorded))
    assert r.status_code == 201, r.text


async def test_an_import_holds_its_decoded_log_to_the_limit(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "IMPORT_LIMIT", 1024**2)
    body = bytes(2 * 1024**2)
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w:gz") as t:
        info = tarfile.TarInfo("r/events.jsonl")
        info.size = len(body)
        t.addfile(info, io.BytesIO(body))
    assert len(tar.getvalue()) < 64 * 1024
    r = await post_tar(cp, tar.getvalue())
    assert r.status_code == 413, r.text
    assert await asyncio.to_thread(os.listdir, cp.state.data / "tmp") == []
    assert [p for p in cp.state.store.root.iterdir() if p.is_dir()] == []


async def test_an_import_refuses_a_line_longer_than_a_log_line(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    with monkeypatch.context() as m:
        m.setattr(chain, "MAX_LINE", 2 * MAX_LINE)
        long = AgentInput(kind="inject", step=0, text="y" * MAX_LINE)
        await local.append(run_id, [Draft(long, "a1")])
    assert len(log_of(local, run_id)[1]) > MAX_LINE
    r = await post_tar(cp, run_tar(local.root / run_id))
    assert (r.status_code, r.json()["detail"]) == (
        422,
        f"a line is longer than {MAX_LINE} bytes",
    ), r.text
    assert not (cp.state.store.root / run_id).exists()


def odd_member(kind: str) -> tuple[tarfile.TarInfo, bytes]:
    """A tar member that makes tarfile read a header past the cap or a sparse map."""
    if kind in ("pax", "gnu"):
        return tarfile.TarInfo("n" * (api.MAX_TAR_HEADER + 1)), b""
    info = tarfile.TarInfo("s")
    if kind == "sparse":
        info.type = tarfile.GNUTYPE_SPARSE
        return info, b""
    data = b"1\n0\n0\n"
    info.size = len(data)
    info.pax_headers = {
        "GNU.sparse.major": "1",
        "GNU.sparse.minor": "0",
        "GNU.sparse.name": "s",
        "GNU.sparse.realsize": "0",
    }
    return info, data


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("pax", f"a tar header is longer than {api.MAX_TAR_HEADER} bytes"),
        ("gnu", f"a tar header is longer than {api.MAX_TAR_HEADER} bytes"),
        ("sparse", "the tar holds a sparse file"),
        ("pax-sparse", "the tar holds a sparse file"),
    ],
)
@pytest.mark.parametrize("last", [False, True])
async def test_an_import_refuses_a_long_tar_header_or_a_sparse_file(
    cp: CP, local: LocalEventStore, kind: str, detail: str, last: bool
) -> None:
    run_id = await local.create_run(STARTED)
    tar = io.BytesIO()
    fmt = tarfile.GNU_FORMAT if kind in ("gnu", "sparse") else tarfile.PAX_FORMAT
    with (
        tarfile.open(fileobj=io.BytesIO(run_tar(local.root / run_id))) as src,
        tarfile.open(fileobj=tar, mode="w", format=fmt) as dst,
    ):
        members = [(m, cast(IO[bytes], src.extractfile(m)).read()) for m in src]
        members.insert(len(members) if last else 0, odd_member(kind))
        for info, data in members:
            dst.addfile(info, io.BytesIO(data))
    r = await post_tar(cp, tar.getvalue())
    assert (r.status_code, r.json()["detail"]) == (422, detail), r.text
    assert not (cp.state.store.root / run_id).exists()


def pax_header(kind: bytes, values: dict[str, str]) -> bytes:
    return tarfile.TarInfo._create_pax_generic_header(values, kind, "utf-8")  # ty: ignore[missing-attribute]


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ([pax_header(tarfile.XHDTYPE, {"comment": "c"})] * api.MAX_TAR_CHAIN, None),
        (
            [pax_header(tarfile.XHDTYPE, {"comment": "c"})] * (api.MAX_TAR_CHAIN + 1),
            f"a tar member has more than {api.MAX_TAR_CHAIN} extended headers",
        ),
        (
            [pax_header(tarfile.XGLTYPE, {f"k{i}": "a" * 600_000}) for i in range(2)],
            f"the global tar headers hold more than {api.MAX_TAR_HEADER} bytes",
        ),
    ],
)
async def test_an_import_bounds_the_headers_before_a_member(
    cp: CP, local: LocalEventStore, headers: list[bytes], detail: str | None
) -> None:
    run_id = await local.create_run(STARTED)
    r = await post_tar(cp, b"".join(headers) + run_tar(local.root / run_id))
    if detail is None:
        assert r.status_code == 201, r.text
    else:
        assert (r.status_code, r.json()["detail"]) == (422, detail), r.text
        assert not (cp.state.store.root / run_id).exists()


async def test_an_import_keeps_no_tar_member_it_has_read(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = await with_blob(local)
    tar = run_tar(local.root / run_id)
    opened: list[tarfile.TarFile] = []
    real = api._ImportTar.__init__

    def track(self: tarfile.TarFile, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(api._ImportTar, "__init__", track)
    r = await post_tar(cp, tar)
    assert r.status_code == 201, r.text
    assert len(opened) == 2
    assert [t.members for t in opened] == [[], []]  # ty: ignore[missing-attribute]


@pytest.mark.parametrize("batch", [1, 600])
async def test_an_import_ingested_in_small_batches_keeps_the_log_whole(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch, batch: int
) -> None:
    monkeypatch.setattr(api, "IMPORT_BATCH", batch)
    run_id, _ = await with_blob(local)
    await local.append(run_id, [call(n) for n in range(5)])
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    sizes: list[int] = []
    ingest = cp.state.store.ingest

    async def counted(run: str, lines: Sequence[bytes]) -> object:
        sizes.append(len(lines))
        return await ingest(run, lines)

    monkeypatch.setattr(cp.state.store, "ingest", counted)
    r = await post_tar(cp, run_tar(local.root / run_id))
    assert r.status_code == 201, r.text
    assert log_of(cp.state.store, run_id) == log_of(local, run_id)
    assert (await cp.state.store.verify(run_id)).ok
    assert sum(sizes) == len(log_of(local, run_id)) == 8
    assert max(sizes) == 1 if batch == 1 else 1 < max(sizes) < 8


async def test_a_server_copy_longer_than_the_local_log_is_a_divergence(
    cp: CP, local: LocalEventStore, tmp_path: Path
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1), call(2)])
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    runner = FakeRunner(cp, "r1")
    await runner.client.hello(runner.hello_body())
    assert (await raw_events(cp, "r1", run_id, lines)).status_code == 200
    short = tmp_path / "short"
    (short / run_id).mkdir(parents=True)
    (short / run_id / "events.jsonl").write_bytes(b"".join(lines[:2]))
    async with await LocalEventStore.open(short) as behind:
        shipper = await ship(behind, runner.client, run_id)
    assert shipper.diverged is not None
    assert "past the end of this log" in shipper.diverged
    await runner.client.aclose()


def late_profile(name: str, key: str) -> str:
    return f'\n[profiles.{name}]\nextends = "scripted"\napi_key_env = "{key}"\n'


LATE_KEYS = "LATE_KEY=http://model.invalid/v1,UNSET_KEY=http://model.invalid/v1,env:UNSET_URL"


@contextlib.asynccontextmanager
async def late_runner(cp: CP, local: LocalEventStore) -> AsyncGenerator[Link]:
    """A real runner link, connected, that reads AIMS_KEYS and its vars from the process env."""
    link = Link(ControlClient(cp.url, cp.runner, runner="late"), local, DeltaBus())
    await link.start()

    async def streaming() -> bool:
        conn = cp.state.runners.get("late")
        return conn is not None and conn.streams > 0

    try:
        await until(streaming, True)
        yield link
    finally:
        await link.close()


async def test_a_runner_reports_the_entries_it_allows_with_their_variables_set(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMS_KEYS", LATE_KEYS)
    monkeypatch.setenv("LATE_KEY", "sk-CANARY-late")
    monkeypatch.setenv("UNLISTED_KEY", "sk-CANARY-unlisted")
    monkeypatch.delenv("UNSET_KEY", raising=False)
    monkeypatch.delenv("UNSET_URL", raising=False)
    # no heartbeat comes in the test's time: what routes is the hello alone
    monkeypatch.setattr("aims.node.link.HEARTBEAT_S", 60)
    async with late_runner(cp, local), cp.client(cp.operator) as c:
        assert cp.state.runners["late"].hello.keys == ["LATE_KEY=http://model.invalid/v1"]
        # a profile saved after the hello routes to it at once, with nothing sent to the runner
        layer = (await c.get("/api/config/project")).json()
        text = layer["text"] + late_profile("added", "LATE_KEY")
        text += late_profile("unset", "UNSET_KEY") + late_profile("unlisted", "UNLISTED_KEY")
        r = await c.put(
            "/api/config/project", json={"text": text}, headers={"If-Match": f'"{layer["etag"]}"'}
        )
        assert r.status_code == 200, r.text
        models = {p["name"]: p for p in (await c.get("/api/models")).json()["profiles"]}
        assert models["added"]["entry"] == "LATE_KEY=http://model.invalid/v1"
        assert models["added"]["runners"] == ["late"]
        assert models["unset"]["runners"] == []
        assert models["unlisted"]["runners"] == []
        assert models["scripted"]["entry"] is None
        assert models["scripted"]["runners"] == ["late"]
        runners = (await c.get("/api/runners")).json()
        assert [r["keys"] for r in runners if r["name"] == "late"] == [
            ["LATE_KEY=http://model.invalid/v1"]
        ]
    # what the control plane holds of the runner names no var AIMS_KEYS does not list
    hello = await cp.state.db.runner("late")
    assert hello is not None
    assert "UNLISTED" not in json.dumps(hello.info)
    assert "CANARY" not in json.dumps(hello.info)


@pytest.mark.parametrize("held", [True, False])
def test_a_runner_says_the_same_whether_a_var_aims_keys_does_not_list_is_set_or_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    monkeypatch.setenv("AIMS_KEYS", LATE_KEYS)
    monkeypatch.setenv("LATE_KEY", "x")
    if held:
        monkeypatch.setenv("UNLISTED_KEY", "x")
    else:
        monkeypatch.delenv("UNLISTED_KEY", raising=False)
    link = Link(ControlClient("http://cp.invalid", "t", runner="r"), cast(Any, None), DeltaBus())
    assert link.hello().keys == ["LATE_KEY=http://model.invalid/v1"]
    assert link.allowed.allows("LATE_KEY=http://model.invalid/v1/x")
    assert not link.allowed.allows("UNLISTED_KEY=http://model.invalid/v1")


def test_a_runner_with_an_aims_keys_it_cannot_read_fails_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMS_KEYS", "K=sk-CANARY")
    with pytest.raises(KeysError) as info:
        Link(ControlClient("http://cp.invalid", "t", runner="r"), cast(Any, None), DeltaBus())
    assert "CANARY" not in str(info.value)


@pytest.mark.parametrize("keys", [["K=sk-CANARY"], ["K=http://a.invalid,L=http://b.invalid"]])
async def test_a_hello_with_entries_aims_cannot_read_is_refused(cp: CP, keys: list[str]) -> None:
    body = FakeRunner(cp, "odd").hello_body().model_dump(mode="json") | {"keys": keys}
    headers = {"Authorization": f"Bearer {cp.runner}", "X-Aims-Runner": "odd"}
    async with cp.client() as c:
        r = await c.post("/runner/v1/hello", json=body, headers=headers)
    assert r.status_code == 422, r.text
    assert "odd" not in cp.state.runners


async def test_a_job_goes_only_to_a_runner_that_allows_every_entry_it_needs(cp: CP) -> None:
    both = ["A=https://a.example/v1", "B=https://b.example/v1"]
    one = FakeRunner(cp, "one", keys=both[:1])
    other = FakeRunner(cp, "other", keys=both[1:])
    await one.connect()
    await other.connect()
    try:
        conns = [cp.state.runners["one"], cp.state.runners["other"]]
        assert api.keys_reason(both, conns, "connected runner") == (
            "no connected runner allows all of A=https://a.example/v1, B=https://b.example/v1"
            " at once"
        )
        assert api.keys_reason([*both, "C=env:U"], conns, "runner") == (
            "no runner allows C=env:U; add it to a runner's AIMS_KEYS and set its variables there"
        )
        assert api.keys_reason(["C=env:U", "D=env:U"], conns, "runner") == (
            "no runner allows C=env:U, D=env:U; add them to a runner's AIMS_KEYS and set their"
            " variables there"
        )
        # an entry AIMS_KEYS cannot hold needs another fix
        assert api.keys_reason(
            ["AIMS_X=https://a.example/v1", "E=https://e.example/v1?q"], conns, "runner"
        ) == (
            "no runner can allow AIMS_X=https://a.example/v1, E=https://e.example/v1?q, which"
            " AIMS_KEYS cannot hold; give the profile another key name or URL"
        )
        row = sdb.Job(
            run_id="r",
            group=None,
            parallel=None,
            state="queued",
            payload={},
            needs={"keys": [f"{both[0]}/chat"]},
            target=None,
            runner=None,
            offered_at=None,
            tried=[],
            token=None,
            created=0.0,
            updated=0.0,
            reason=None,
        )
        assert [dispatch.can_take(c, row) for c in conns] == [True, False]
        # a row queued before needs held keys goes to any runner: its call checks the key
        old = dataclasses.replace(row, needs={"envs": ["A"], "kernel": False})
        assert old.wants().keys is None
        assert [dispatch.can_take(c, old) for c in conns] == [True, True]
    finally:
        await one.close()
        await other.close()


async def test_a_runner_takes_only_the_offers_its_own_aims_keys_allows(tmp_path: Path) -> None:
    claimed: list[str] = []

    async def claim(of: str) -> None:
        claimed.append(of)

    made = Node(NodeOptions(url="http://cp", token="t", name="n", data=tmp_path))
    link = SimpleNamespace(
        client=SimpleNamespace(claim=claim), allowed=parse("K=https://a.example/v1")
    )
    made._open = cast(Any, SimpleNamespace(link=link))
    # the control plane would not offer these; the runner checks again all the same
    await made._offer(OfferMessage(run_id="other", needs={"keys": ["K=https://b.example/v1"]}))
    await made._offer(OfferMessage(run_id="url-var", needs={"keys": ["K=env:U"]}))
    await made._offer(OfferMessage(run_id="odd", needs={"keys": "K=https://a.example/v1"}))
    await made._offer(OfferMessage(run_id="ok", needs={"keys": ["K=https://a.example/v1/x"]}))
    await made._offer(OfferMessage(run_id="old", needs={"envs": ["K"]}))
    assert claimed == ["ok", "old"]


@pytest.mark.parametrize("reply", ["past its cap", "not JSON", "no job"])
async def test_a_claim_whose_reply_does_not_load_frees_its_slot_and_hands_the_job_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """The control plane gave the job to this runner, so the runner hands it back with the
    reason. Once a slot was lost for good and the job stayed in the runner's jobs."""
    monkeypatch.setattr("aims.node.client.REPLY_LIMIT", 1024)
    body = {
        "past its cap": b" " * 4096,
        "not JSON": b"<html>proxy</html>",
        "no job": b'{"job": {"kind": "other"}}',
    }[reply]
    claims: list[str] = []
    released: list[tuple[str, str]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/claim"):
            claims.append(path)
            return httpx2.Response(200, content=body)
        released.append((path, json.loads(request.content)["reason"]))
        return httpx2.Response(204)

    client = ControlClient("http://cp", "t", runner="n", transport=httpx2.MockTransport(handle))
    made = Node(NodeOptions(url="http://cp", token="t", name="n", data=tmp_path, slots=1))
    made._open = cast(Any, SimpleNamespace(link=SimpleNamespace(client=client)))
    try:
        await made._offer(OfferMessage(run_id="r1"))
        assert made.claimed == {}
        [(path, reason)] = released
        assert path == "/runner/v1/jobs/r1/release"
        assert reason.startswith("the reply to its claim does not load: ")
        if reply == "past its cap":
            assert reason.endswith("passes 1024 bytes")
        # the one slot is free for the next offer
        await made._offer(OfferMessage(run_id="r2"))
    finally:
        await client.aclose()
    assert claims == ["/runner/v1/jobs/r1/claim", "/runner/v1/jobs/r2/claim"]
    assert made.claimed == {}


async def test_closing_a_link_stops_its_shippers(local: LocalEventStore) -> None:
    run_id = await local.create_run(STARTED)
    client = ControlClient("http://127.0.0.1:9", "token", runner="r1")
    link = Link(client, local, DeltaBus())
    link.ship(run_id)
    task = link._ships[run_id]
    await asyncio.sleep(0.05)
    await link.close()
    assert task.cancelled()


async def test_a_link_forgets_a_run_once_it_has_shipped(
    local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    link = Link(fake.client, local, DeltaBus())
    link.ship(run_id)
    assert await link.drain(20)
    await asyncio.sleep(0)
    assert run_id not in link.shippers
    assert run_id not in link._ships


async def test_a_link_keeps_a_shipper_that_failed(cp: CP, local: LocalEventStore) -> None:
    run_id = await local.create_run(STARTED)
    link = Link(ControlClient(cp.url, "bad", runner="r1"), local, DeltaBus())
    link.ship(run_id)
    assert await link.drain(20)
    await asyncio.sleep(0)
    shipper = link.shippers[run_id]
    assert shipper.refused is not None
    assert "401" in shipper.refused
    assert link._ships[run_id].done()
    await link.close()


async def test_a_link_logs_a_shipper_that_dies_of_an_error(
    local: LocalEventStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = await local.create_run(STARTED)
    page = httpx2.MockTransport(lambda request: httpx2.Response(200, text="<html>proxy</html>"))
    client = ControlClient("http://cp.test", "token", runner="r1", transport=page)
    link = Link(client, local, DeltaBus())
    with caplog.at_level(logging.ERROR, logger="aims.node"):
        link.ship(run_id)
        assert await link.drain(5)
        await asyncio.sleep(0)
    assert said(caplog) == [("ERROR", f"run {run_id}: shipping stopped on an error")]
    error = caplog.records[-1].exc_info
    assert isinstance(error, tuple)
    assert isinstance(error[1], ValidationError)
    assert run_id in link.shippers
    await link.close()


async def test_a_corrupt_run_does_not_stop_the_others_shipping(
    cp: CP, fake: FakeRunner, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "corrupt"
    bad, good = sorted(str(uuid.uuid7()) for _ in range(2))
    async with await LocalEventStore.open(root) as store:
        await store.create_run(STARTED, run_id=bad)
        await store.append(bad, [call(1), call(2)])
        await store.create_run(STARTED, run_id=good)
        await store.append(good, [call(1), Draft(RunFinished(status="completed"))])
    path = root / bad / "events.jsonl"
    path.write_bytes(b"".join(split_lines(path.read_bytes())[:2]))
    async with await LocalEventStore.open(root) as store:
        link = Link(fake.client, store, DeltaBus())
        with caplog.at_level(logging.ERROR, logger="aims.node"):
            await link.ship_all()
        assert list(link.shippers) == [good]
        assert said(caplog) == [("ERROR", f"run {bad} is marked corrupt; it is not shipped")]
        assert await link.drain(20)
        await link.close()
    head = await cp.state.store.head(good)
    assert head is not None
    assert (head.seq, head.finished) == (2, True)
    assert await cp.state.store.run(bad) is None


async def test_a_run_that_fails_here_does_not_stop_the_others_shipping(
    cp: CP, fake: FakeRunner, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, good = sorted(str(uuid.uuid7()) for _ in range(2))
    for run_id in (bad, good):
        await local.create_run(STARTED, run_id=run_id)
        await local.append(run_id, [call(1), Draft(RunFinished(status="completed"))])
    real = local.log_head

    async def log_head(run_id: str) -> Any:
        if run_id == bad:
            raise sqlite3.OperationalError("database is locked")
        return await real(run_id)

    monkeypatch.setattr(local, "log_head", log_head)
    link = Link(fake.client, local, DeltaBus())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await link.ship_all()
    assert list(link.shippers) == [good]
    assert await link.drain(20)
    monkeypatch.undo()
    await link.ship_all()
    assert await link.drain(20)
    await link.close()
    for run_id in (bad, good):
        head = await cp.state.store.head(run_id)
        assert head is not None
        assert (head.seq, head.finished) == (2, True)


def refuse_from(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch, run_id: str, seq: int
) -> None:
    """Make the store's index refuse each batch of `run_id` that reaches `seq`, so it stays
    behind the log."""
    real = store._index.add

    async def add(events: list[Any], head: Any, **kw: Any) -> None:
        if events and events[0].run_id == run_id and events[-1].seq >= seq:
            raise ValueError("the index cannot take it")
        await real(events, head, **kw)

    monkeypatch.setattr(store._index, "add", add)


async def server_seq(cp: CP, run_id: str) -> int:
    head = await cp.state.store.head(run_id)
    return -1 if head is None else head.seq


async def test_a_run_whose_index_stays_behind_ships_its_whole_log(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    refuse_from(local, monkeypatch, run_id, 2)
    task = asyncio.create_task(Shipper(local, fake.client, run_id).run())
    await until(lambda: server_seq(cp, run_id), 1)
    await local.append(run_id, [call(2)])
    await until(lambda: server_seq(cp, run_id), 2)
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    async with asyncio.timeout(10):
        await task
    head = await cp.state.store.head(run_id)
    assert head is not None
    assert (head.seq, head.finished) == (3, True)
    indexed = await local.head(run_id)
    assert indexed is not None
    assert indexed.seq == 1


async def test_ship_all_ships_a_run_whose_index_stays_behind(
    cp: CP, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    await fake.client.post_events(
        run_id, split_lines((local.root / run_id / "events.jsonl").read_bytes())
    )
    refuse_from(local, monkeypatch, run_id, 2)
    await local.append(run_id, [call(2), Draft(RunFinished(status="completed"))])
    link = Link(fake.client, local, DeltaBus())
    await link.ship_all()
    assert list(link.shippers) == [run_id]
    assert await link.drain(20)
    await link.close()
    head = await cp.state.store.head(run_id)
    assert head is not None
    assert (head.seq, head.finished) == (3, True)


@pytest.mark.parametrize(
    ("fails", "refused_from", "lines"),
    [
        pytest.param(WARN_AFTER - 1, None, [], id="unreachable-4"),
        pytest.param(WARN_AFTER, None, ["streak", "back"], id="unreachable-5"),
        pytest.param(WARN_AFTER - 1, 1, ["now"], id="refused-4"),
        pytest.param(WARN_AFTER, 1, ["now", "streak", "back"], id="refused-5"),
        pytest.param(WARN_AFTER, 3, ["now", "streak", "back"], id="upgraded-5"),
        pytest.param(WARN_AFTER, WARN_AFTER, ["streak", "back"], id="refused-on-the-5th"),
    ],
)
async def test_a_link_says_when_it_cannot_reach_the_control_plane_and_when_it_can(
    local: LocalEventStore,
    no_backoff: None,
    caplog: pytest.LogCaptureFixture,
    fails: int,
    refused_from: int | None,
    lines: list[str],
) -> None:
    """Unreachable, then from hello `refused_from` on refused as a runner of another version.
    The first refusal shows at once unless the streak's own line already shows it."""
    hellos = 0
    version = {"title": "Conflict", "detail": "runner 0.0.1 does not match", "code": "version"}

    async def open_stream() -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal hellos
        if request.url.path.endswith("/hello"):
            hellos += 1
            if hellos <= fails and refused_from is not None and hellos >= refused_from:
                return httpx2.Response(409, json=version)
            if hellos <= fails:
                raise httpx2.ConnectError("All connection attempts failed")
            return httpx2.Response(200, json={"name": "r1"})
        sse = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=sse, content=open_stream())

    client = ControlClient(
        "http://cp.test", "token", runner="r1", transport=httpx2.MockTransport(handle)
    )
    link = Link(client, local, DeltaBus())
    with caplog.at_level(logging.INFO, logger="aims.node"):
        task = asyncio.create_task(link._stream())
        async with asyncio.timeout(5):
            await link.connected.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await client.aclose()
    assert hellos == fails + 1
    refusal = "409 Conflict: runner 0.0.1 does not match"
    why = "All connection attempts failed" if refused_from is None else refusal
    to = "the link to the control plane at http://cp.test"
    told = {
        "now": f"control plane: {refusal}",
        "streak": f"{to} failed {fails} times in a row: {why}",
        "back": f"{to} works again after {fails} failures",
    }
    assert said(caplog) == [("WARNING", told[line]) for line in lines]


async def set_or_ended(event: asyncio.Event, tasks: Sequence[asyncio.Task[None]]) -> None:
    """Wait until `event` is set or one of `tasks` has ended."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait([waiter, *tasks], timeout=10, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


async def test_a_link_says_hello_again_after_a_reply_past_its_cap(
    local: LocalEventStore, no_backoff: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As after a reply that is not JSON. Once such a reply ended the stream for good."""
    monkeypatch.setattr("aims.node.client.REPLY_LIMIT", 1024)
    hellos = 0

    async def open_stream() -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal hellos
        if request.url.path.endswith("/hello"):
            hellos += 1
            return httpx2.Response(200, content=b" " * 4096 if hellos <= 2 else b'{"name": "r1"}')
        sse = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=sse, content=open_stream())

    transport = httpx2.MockTransport(handle)
    client = ControlClient("http://cp.test", "t", runner="r1", transport=transport)
    link = Link(client, local, DeltaBus())
    task = asyncio.create_task(link._stream())
    try:
        await set_or_ended(link.connected, [task])
        assert not task.done(), task.exception()
    finally:
        task.cancel()
        await asyncio.wait([task])
        await client.aclose()
    assert link.connected.is_set()
    assert hellos == 3


async def test_a_links_status_and_deltas_go_on_after_a_reply_past_its_cap(
    local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once such a reply ended the loop that sends them for good."""
    from aims.models import Delta

    monkeypatch.setattr("aims.node.client.REPLY_LIMIT", 1024)
    monkeypatch.setattr("aims.node.link.HEARTBEAT_S", 0.0)
    sent = {"status": 0, "deltas": 0}
    statuses, posted = asyncio.Event(), asyncio.Event()

    def handle(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.rsplit("/", 1)[1]
        sent[path] += 1
        if path == "deltas":
            posted.set()
        elif sent["status"] >= 3:
            statuses.set()
        return httpx2.Response(200, content=b" " * 4096)

    transport = httpx2.MockTransport(handle)
    client = ControlClient("http://cp.test", "t", runner="r1", transport=transport)
    bus = DeltaBus()
    link = Link(client, local, bus)
    loops = [asyncio.create_task(link._heartbeat()), asyncio.create_task(link._deltas())]
    try:
        # the deltas loop subscribes as it starts
        await asyncio.sleep(0)
        assert bus.subscribers
        for n in range(3):
            posted.clear()
            bus.publish(Delta("r", None, "c", 1, "text", str(n)))
            await set_or_ended(posted, loops)
        await set_or_ended(statuses, loops)
        assert not any(t.done() for t in loops), [t.exception() for t in loops if t.done()]
    finally:
        for t in loops:
            t.cancel()
        await asyncio.wait(loops)
        await client.aclose()
    assert sent["deltas"] == 3
    assert sent["status"] >= 3


async def test_the_end_of_a_ship_does_not_drop_the_ship_that_replaced_it(
    local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold = asyncio.Event()
    ran: list[Shipper] = []

    async def run(self: Shipper) -> None:
        ran.append(self)
        if len(ran) > 1:
            await hold.wait()
        self.done = self.truthful = True

    monkeypatch.setattr(Shipper, "run", run)
    run_id = await local.create_run(STARTED)
    link = Link(ControlClient("http://127.0.0.1:9", "token", runner="r1"), local, DeltaBus())
    link.ship(run_id)
    first, task = link.shippers[run_id], link._ships[run_id]
    # One loop turn: the first ship ends but its done callbacks have not run yet.
    await asyncio.sleep(0)
    assert task.done()
    link.ship(run_id)
    newer = link.shippers[run_id]
    # It looks shipped while it still runs, as when it sends labels last.
    newer.done = newer.truthful = True
    await asyncio.sleep(0)
    assert ran == [first, newer]
    assert link.shippers.get(run_id) is newer
    assert not link._ships[run_id].done()
    hold.set()
    assert await link.drain(5)
    await asyncio.sleep(0)
    assert run_id not in link.shippers
    await link.close()


async def test_push_finishes_what_a_run_on_the_same_token_began(
    cp: CP, local: LocalEventStore
) -> None:
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    first = FakeRunner(cp, "run-host-1")
    await first.client.hello(first.hello_body())
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, "run-host-1", run_id, lines)).status_code == 200
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    later = FakeRunner(cp, "push-host")
    await later.client.hello(later.hello_body())
    assert (await ship(local, later.client, run_id)).done
    meta = await cp.state.db.run_meta(run_id)
    assert meta is not None
    assert meta.runner == "run-host-1"
    other_token = await cp.state.db.create_token("r2", "runner")
    stranger = ControlClient(cp.url, other_token, runner="stranger")
    await stranger.hello(FakeRunner(cp, "stranger").hello_body())
    another = await local.create_run(STARTED)
    begun = split_lines((local.root / another / "events.jsonl").read_bytes())
    assert (await raw_events(cp, "run-host-1", another, begun)).status_code == 200
    await local.append(another, [Draft(RunFinished(status="completed"))])
    refused = await ship(local, stranger, another)
    assert refused.refused is not None
    for c in (first.client, later.client, stranger):
        await c.aclose()


async def test_a_revoked_token_frees_its_runner_name(cp: CP, local: LocalEventStore) -> None:
    old = FakeRunner(cp, "box")
    await old.client.hello(old.hello_body())
    run_id = await local.create_run(STARTED)
    await local.append(run_id, [call(1)])
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, "box", run_id, lines[:1])).status_code == 200
    sealed = await local.create_run(STARTED)
    await local.append(sealed, [Draft(RunFinished(status="completed"))])
    assert (await ship(local, old.client, sealed)).done
    async with cp.client(cp.operator) as c:
        assert (await c.post(f"/api/runs/{run_id}/kill", json={})).status_code == 202
    token = await cp.state.db.create_token("r2", "runner")
    new = ControlClient(cp.url, token, runner="box")
    with pytest.raises(ApiError) as caught:
        await new.hello(old.hello_body())
    assert (caught.value.status, caught.value.code) == (403, "name")
    assert "revoke that token" in str(caught.value)

    async with cp.client(cp.operator) as c:
        assert (await c.delete("/api/tokens/r1")).status_code == 204
    await new.hello(old.hello_body())
    assert cp.state.runners["box"].token == "r2"
    (kill,) = await cp.state.db.commands_of(run_id)
    assert kill.outcome == "dropped: another token took the runner name"
    r = await raw_events(cp, "box", run_id, lines[1:], token)
    assert (r.status_code, r.json()["code"]) == (403, "owner")
    with pytest.raises(ApiError) as caught:
        async with new.run_log(run_id):
            pass
    assert caught.value.code == "owner"
    async with cp.client(cp.operator) as c:
        r = await c.post(f"/api/runs/{run_id}/kill", json={})
        assert (r.status_code, r.json()["code"]) == (409, "runner_gone")
    again = await ship(local, new, sealed)
    assert (again.done, again.refused) == (True, None)

    fresh = await local.create_run(STARTED)
    await local.append(fresh, [Draft(RunFinished(status="completed"))])
    assert (await ship(local, new, fresh)).done
    meta = await cp.state.db.run_meta(fresh)
    assert meta is not None
    assert (meta.runner, meta.shipper) == ("box", "r2")
    await cp.state.db.hello("ghost", "vanished", {})
    ghost = ControlClient(cp.url, token, runner="ghost")
    await ghost.hello(FakeRunner(cp, "ghost").hello_body())
    for client in (old.client, new, ghost):
        await client.aclose()


async def test_taking_a_name_takes_back_the_jobs_it_held(cp: CP, fake: FakeRunner) -> None:
    (run_id,) = await start_run(cp)
    offer = await fake.next(OfferMessage)
    assert await fake.client.claim(offer.run_id) is not None
    async with cp.client(cp.operator) as c:
        assert (await c.delete("/api/tokens/r1")).status_code == 204
    token = await cp.state.db.create_token("r2", "runner")
    new = ControlClient(cp.url, token, runner="fake")
    reply = await new.hello(fake.hello_body(jobs=[run_id]))
    assert reply.requeued == [run_id]
    await new.aclose()


async def test_taking_a_name_does_not_take_the_inputs_of_jobs_the_old_token_runs(
    cp: CP, fake: FakeRunner, local: LocalEventStore
) -> None:
    (run_id,) = await start_run(cp)
    job = await play_job(fake, local, await fake.next(OfferMessage))
    lines = split_lines((local.root / run_id / "events.jsonl").read_bytes())
    assert (await raw_events(cp, "fake", run_id, lines[:1])).status_code == 200
    assert await job_state(cp, run_id) == "running"
    sha = job.blobs()[0]
    assert await fake.client.job_blob(run_id, sha)
    async with cp.client(cp.operator) as c:
        assert (await c.delete("/api/tokens/r1")).status_code == 204
    token = await cp.state.db.create_token("r2", "runner")
    new = ControlClient(cp.url, token, runner="fake")
    assert (await new.hello(fake.hello_body())).requeued == []
    with pytest.raises(ApiError) as caught:
        await new.job_blob(run_id, sha)
    assert (caught.value.status, caught.value.code) == (403, "owner")
    await new.aclose()


async def test_requeue_only_gives_back_its_own_offers(cp: CP) -> None:
    from aims.server.db import Job as JobRow

    row = JobRow(
        "01a18000-0000-7000-8000-000000000001",
        None,
        None,
        "queued",
        {},
        {},
        None,
        None,
        None,
        [],
        None,
        0.0,
        0.0,
        None,
    )
    await cp.state.db.add_jobs([row])
    assert await cp.state.db.offer(row.run_id, "b")
    assert not await cp.state.db.requeue(row.run_id, runner="a")
    assert await cp.state.db.requeue(row.run_id, runner="b")


async def test_the_ui_serves_the_assets_of_a_build_made_while_it_runs(
    cp: CP, tmp_path: Path
) -> None:
    assets = tmp_path / "ui" / "assets"
    async with cp.client() as c:
        assert (await c.get("/assets/a.js")).status_code == 200
        (assets / "a.js").unlink()
        (assets / "b.js").write_text("export const b = 1;\n")
        r = await c.get("/assets/b.js")
        assert (r.status_code, r.text) == (200, "export const b = 1;\n")
        assert (await c.get("/assets/a.js")).status_code == 404
        assert (await c.get("/assets/..%2Findex.html")).status_code == 404
        listings = 0
        real = app_module._assets

        def counted(root: Path | None) -> Any:
            nonlocal listings
            listings += 1
            return real(root)

        with patch.object(app_module, "_assets", counted):
            for _ in range(3):
                assert (await c.get("/assets/nope.js")).status_code == 404
            assert listings == 0
            (assets / "c.js").write_text("export const c = 1;\n")
            assert (await c.get("/assets/c.js")).status_code == 200
            assert listings == 1


async def test_the_ui_sends_the_precompressed_copy_a_request_accepts(
    cp: CP, tmp_path: Path
) -> None:
    assets = tmp_path / "ui" / "assets"
    source = b"export const big = '" + b"x" * 2000 + b"';\n"
    (assets / "big.js").write_bytes(source)
    (assets / "big.js.br").write_bytes(b"brotli bytes")
    (assets / "big.js.gz").write_bytes(gzip.compress(source))
    async with cp.client() as c:
        headers = {"Accept-Encoding": "gzip, deflate, br, zstd"}
        async with c.stream("GET", "/assets/big.js", headers=headers) as r:
            body = b"".join([chunk async for chunk in r.aiter_raw()])
        assert (r.status_code, r.headers["content-encoding"], body) == (200, "br", b"brotli bytes")
        assert r.headers["content-type"].startswith("text/javascript")
        assert r.headers["vary"] == "Accept-Encoding"
        assert "immutable" in r.headers["cache-control"]
        r = await c.get("/assets/big.js", headers={"Accept-Encoding": "br;q=0, gzip"})
        assert (r.headers["content-encoding"], r.content) == ("gzip", source)
        for accept in ("identity", "", "br;q=0, gzip;q=0"):
            r = await c.get("/assets/big.js", headers={"Accept-Encoding": accept})
            assert "content-encoding" not in r.headers
            assert (r.content, r.headers["vary"]) == (source, "Accept-Encoding")
        r = await c.get("/assets/a.js", headers=headers)
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert "vary" not in r.headers
        for copy in ("big.js.br", "big.js.gz"):
            assert (await c.get(f"/assets/{copy}", headers=headers)).status_code == 404
        (assets / "big.js.br").unlink()
        r = await c.get("/assets/big.js", headers=headers)
        assert (r.headers["content-encoding"], r.content) == ("gzip", source)


async def test_a_self_test_takes_one_slot_while_its_case_runs(local: LocalEventStore) -> None:
    client = ControlClient("http://127.0.0.1:9", "t", runner="x")
    held = ["selftest"]
    link = Link(client, local, DeltaBus(), slots=2, jobs=lambda: list(held))
    # commands to the self-test's run go to the case running
    case: Any = object()
    other: Any = object()
    link.runners["selftest"] = link.runners["case-1"] = case
    try:
        assert (link.status().free, link.hello().jobs) == (1, ["selftest"])
        assert link.status().running == ["selftest"]
        held.append("scenario")
        link.runners["scenario"] = other
        assert (link.status().free, link.hello().jobs) == (0, ["scenario", "selftest"])
    finally:
        await client.aclose()


async def test_an_operator_token_gets_a_code_that_signs_a_browser_in_once(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/session/code")
        assert r.status_code == 200, r.text
        code = r.json()["code"]
        assert r.json()["ttl_s"] == 120
    for token in (cp.viewer, cp.runner):
        async with cp.client(token) as c:
            assert (await c.post("/api/session/code")).status_code == 403
    async with cp.client() as browser:
        assert (await browser.post("/api/session/code")).status_code == 401
        assert (await browser.post("/api/session", json={"code": code})).status_code == 204
        assert (await browser.get("/api/session")).json()["name"] == "op"
        # a session makes no codes of its own
        r = await browser.post("/api/session/code", headers={"Origin": cp.url})
        assert (r.status_code, r.json()["code"]) == (403, "bearer")
    async with cp.client() as other:
        assert (await other.post("/api/session", json={"code": code})).status_code == 401


async def test_without_auth_the_link_needs_no_code(tmp_path: Path) -> None:
    async with control_plane(tmp_path, auth=False) as cp, cp.client(cp.operator) as c:
        assert (await c.post("/api/session/code")).json() == {"code": None, "ttl_s": 0}


def test_only_the_newest_launch_codes_work() -> None:
    codes = LaunchCodes()
    issued = [codes.issue("op") for _ in range(CODES + 8)]
    assert codes.redeem(issued[0]) is None
    assert codes.redeem(issued[-CODES]) == "op"
    assert codes.redeem(issued[-1]) == "op"
