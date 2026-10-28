import asyncio
import contextlib
import io
import json
import os
import tarfile
import uuid
from collections.abc import AsyncIterator
from compression import zstd
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cp import CP, control_plane

from aims import __version__
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
from aims.node.shipper import Shipper
from aims.prompts import Prompts
from aims.run.job import Job
from aims.run.runner import resolve_texts
from aims.scenario import load
from aims.server import dispatch
from aims.wire import (
    Ack,
    CommandMessage,
    Deltas,
    MonitorJob,
    OfferMessage,
    RunnerHello,
    RunnerStatus,
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
) -> Job | MonitorJob:
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
        assert [(x["action"], x["outcome"]) for x in listed] == [("pause", "done")]
        await fake.disconnect()
        await fake.connect()
        with pytest.raises(TimeoutError):
            await fake.next(CommandMessage, within=0.5)
        await c.post(f"/api/runs/{run_id}/inject", json={"text": "hi"})
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(10):
            await task
        assert (await cp.state.db.pending_commands("fake")) == []
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


async def test_scenario_files_are_validated_and_confined(cp: CP) -> None:
    base = "/api/scenarios/project/control/files"
    async with cp.client(cp.operator) as c:
        listed = (await c.get("/api/scenarios")).json()
        assert [(s["source"], s["name"], s["writable"]) for s in listed] == [
            ("project", "control", True)
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
    }
    problem = spec["paths"]["/api/runs"]["post"]["responses"]["422"]["content"]
    assert list(problem) == ["application/problem+json"]
    assert spec["paths"]["/api/runs/{run_id}/events"]["get"]["operationId"] == "list_events"


# Fixes from the security review


async def push_forged(cp: CP, local: LocalEventStore, base_url: str) -> str:
    """A run a runner token pushed, whose recorded profile it chose."""
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
        config={"scenario": bundle.snapshot(), "agents": {"a": entry}},
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
        await reader.client.run_log(run_id)
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
