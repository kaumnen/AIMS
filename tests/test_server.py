import ast
import asyncio
import contextlib
import gzip
import hashlib
import inspect
import io
import json
import logging
import os
import shutil
import stat
import tarfile
import tempfile
import textwrap
import threading
import time
import uuid
from collections.abc import AsyncIterator, Generator
from compression import zstd
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx2
import pytest
from cp import CP, SCENARIO, control_plane, start, stop
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from pydantic import ValidationError

import aims
from aims import __version__
from aims.config import STARTER
from aims.events import (
    AgentInput,
    BlobRef,
    Draft,
    LocalEventStore,
    RunFinished,
    RunStarted,
    ToolCall,
    split_lines,
)
from aims.events.blobs import decode
from aims.models import DeltaBus
from aims.node.client import FEED, ApiError, ControlClient
from aims.node.link import Link
from aims.node.shipper import WARN_AFTER, Shipper
from aims.prompts import Prompts
from aims.run.job import Job
from aims.run.runner import resolve_texts
from aims.scenario import load
from aims.server import api, dispatch, edit_api
from aims.server import app as app_module
from aims.server.app import create_app
from aims.server.auth import HEADERS
from aims.server.db import UI_TOKEN
from aims.server.files import builtin_root
from aims.server.state import CODES, InFlight, LaunchCodes, close_state, open_state
from aims.wire import (
    Ack,
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


class FakeRunner:
    """A runner without Docker: it says hello, reads its stream and ships runs it makes."""

    def __init__(self, cp: CP, name: str = "fake", *, envs: list[str] | None = None) -> None:
        self.client = ControlClient(cp.url, cp.runner, runner=name)
        self.name = name
        self.envs = envs or []
        self.feed: asyncio.Queue[Any] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.last_id: str | None = None

    def hello_body(self, jobs: list[str] | None = None) -> RunnerHello:
        return RunnerHello(
            name=self.name,
            version=__version__,
            arch="arm64",
            tracer=TracerHealth(alive=True),
            envs=self.envs,
            slots=2,
            jobs=jobs or [],
        )

    async def connect(self) -> None:
        await self.client.hello(self.hello_body())
        await self.client.status(
            RunnerStatus(tracer=TracerHealth(alive=True), free=2, envs=self.envs)
        )
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
        spec = (await anon.get("/api/openapi.json")).json()
        assert "/healthz" not in spec["paths"]
    async with cp.client(cp.viewer) as viewer:
        assert (await viewer.get("/api/health")).json()["ok"] is True
    server, task, port = await start(create_app(cp.state, hosts=("server",), dispatcher=False))
    try:
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            r = await c.get("/healthz", headers={"Host": "server:8484"})
            assert (r.status_code, r.json()) == (200, {"ok": True})
    finally:
        await stop(server, task)


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
    assert offer.needs == {"envs": [], "kernel": True}
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


async def test_a_run_needs_a_runner_with_its_keys(cp: CP, fake: FakeRunner) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={"scenario": {"source": "project", "name": "control"}, "profile": "keyed"},
        )
        assert (r.status_code, r.json()["code"]) == (422, "envs")
        assert "AIMS_TEST_KEY_THAT_IS_NOT_SET" in r.json()["detail"]


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
        r = await c.post("/api/config/user/check", json={"text": "[credits]\nmeta = 1\n"})
        assert r.json()["ok"]
        r = await c.put("/api/config/user", json={"text": "x = \n"}, headers={"If-None-Match": "*"})
        assert (r.status_code, r.json()["code"], r.json()["errors"][0]["line"]) == (422, "toml", 1)
        user = Path((await c.get("/api/config/user")).json()["path"])
        assert [p.name for p in user.parent.iterdir()] == []
    async with cp.client(cp.viewer) as c:
        r = await c.post("/api/config/user/check", json={"text": ""})
        assert r.status_code == 403
        r = await c.get("/api/config/merged")
        assert r.status_code == 200, r.text
        assert '\n[roles]\nagent = "' in r.json()["text"]


async def test_a_config_file_that_is_not_utf8_is_a_problem_not_a_crash(cp: CP) -> None:
    cp.project.write_bytes("[credits]\nmeta = 1 # café\n".encode("latin-1"))
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
            json={"text": "[credits]\nmeta = 1\n"},
            headers={"If-None-Match": "*"},
        )
        assert r.status_code == 200, r.text
        patch = {"set": [{"key": ["credits", "meta"], "value": 2}]}
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
            json={"text": "[credits]\nmeta = 1\n"},
            headers={"If-None-Match": "*"},
        )
        assert r.status_code == 200, r.text
        assert (await c.get("/api/config")).json()["layers"][1]["exists"]
        merged = (await c.get("/api/config/merged")).json()["text"]
        assert "meta = 1.0  # this project" in merged


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
SET = {"set": [{"key": ["credits", "meta"], "value": 1}]}
RUN = str(uuid.UUID(int=1))


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
            {"set": [{"key": ["credits", "meta"], "value": None}]},
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
    src = Path(__file__).parent.parent / "src/aims/scenarios/cache-keys/scenario.yaml"
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


async def test_a_selftest_is_queued_for_a_runner_that_runs_it(cp: CP) -> None:
    src = Path(__file__).parent.parent / "src/aims/scenarios/cache-keys"
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
        assert job.needs == {"envs": [], "kernel": True, "kind": "selftest"}
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


# The live stream


async def sse(
    c: httpx2.AsyncClient, params: dict[str, Any], want: int, last: str | None = None
) -> list[Any]:
    headers = {} if last is None else {"Last-Event-ID": last}
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
    assert cp.state.runners["fake"].streams == 0
    # the offer it never claimed goes back to the queue
    assert await job_state(cp, run_id) == "queued"


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
    run_id = str(uuid.uuid7())
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


async def sse_inflight(cp: CP, run_id: str) -> list[Any]:
    async with cp.client(cp.viewer) as c:
        return await sse(c, {"deltas": run_id}, want=1)


async def test_openapi_lists_every_payload_and_the_stream_messages(cp: CP) -> None:
    async with cp.client() as c:
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


# Fixes from the security review


async def push_forged(
    cp: CP, local: LocalEventStore, base_url: str, *, budget: dict[str, Any] | None = None
) -> str:
    """A run a runner token pushed, whose recorded profile it chose, run under `budget`."""
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
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    pusher = FakeRunner(cp, "pusher")
    await pusher.client.hello(pusher.hello_body())
    await ship(local, pusher.client, run_id)
    await pusher.client.aclose()
    return run_id


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
