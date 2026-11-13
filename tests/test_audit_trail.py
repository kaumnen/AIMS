"""The audit trail of operator actions: who changed what on the control plane, how it went,
and from where."""

import asyncio
import io
import json
import logging
import re
import sqlite3
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import uvicorn
from cp import CP, control_plane
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from typer.testing import CliRunner

from aims import __version__
from aims.cli import app as cli
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted
from aims.node.client import ControlClient
from aims.server import dispatch
from aims.server.app import create_app
from aims.server.state import close_state, open_state
from aims.wire import RunnerHello, TracerHealth

pytestmark = pytest.mark.anyio

# POST routes that only check what they are sent and change nothing
EXEMPT = {
    ("POST", "/api/scenarios/check"),
    ("POST", "/api/config/{layer}/check"),
}
OCTET = {"Content-Type": "application/octet-stream"}


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


async def rows(cp: CP, **query: Any) -> list[dict[str, Any]]:
    """The audit rows, newest first."""
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/audit", params={"limit": 1000, **query})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def trail(cp: CP) -> list[tuple[Any, ...]]:
    """Each row as (token, action, target, outcome, status, detail), oldest first."""
    return [
        (r["token"], r["action"], r["target"], r["outcome"], r["status"], r["detail"])
        for r in reversed(await rows(cp))
    ]


async def hello(cp: CP, name: str = "box") -> None:
    client = ControlClient(cp.url, cp.runner, runner=name)
    try:
        await client.hello(
            RunnerHello(
                name=name, version=__version__, arch="arm64", tracer=TracerHealth(alive=True)
            )
        )
    finally:
        await client.aclose()


async def sealed_tar(tmp_path: Path) -> tuple[str, bytes]:
    """A finished run as the tar an import takes."""
    async with await LocalEventStore.open(tmp_path / "local") as local:
        run_id = await local.create_run(
            RunStarted(scenario="pushed", oversight="observe", agents=["a1"])
        )
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        tar = io.BytesIO()
        with tarfile.open(fileobj=tar, mode="w") as t:
            t.add(local.root / run_id / "events.jsonl", arcname=f"{run_id}/events.jsonl")
    return run_id, tar.getvalue()


async def test_sign_ins_failed_ones_too_sign_outs_and_launch_codes_are_recorded(cp: CP) -> None:
    code = cp.state.codes.issue("op")
    async with cp.client() as c:
        assert (await c.post("/api/session", json={"token": "aims_wrong"})).status_code == 401
        assert (await c.post("/api/session", json={"token": cp.runner})).status_code == 401
        assert (await c.post("/api/session", json={"code": "nope"})).status_code == 401
        assert (await c.post("/api/session", json={})).status_code == 422
        assert (await c.post("/api/session", json={"code": code})).status_code == 204
        assert (await c.delete("/api/session", headers={"Origin": cp.url})).status_code == 204
        assert (await c.post("/api/session", json={"token": cp.viewer})).status_code == 204
    async with cp.client() as anon:
        r = await anon.post("/api/tokens", json={"name": "x", "role": "viewer"})
        assert r.status_code == 401
    async with cp.client(cp.operator) as c:
        assert (await c.post("/api/session/code")).status_code == 200
        assert (await c.get("/api/runs")).status_code == 200
    got = await rows(cp)
    assert [
        (r["token"], r["role"], r["auth"], r["action"], r["outcome"], r["status"])
        for r in reversed(got)
    ] == [
        (None, None, "token", "session.start", "refused", 401),
        ("r1", "runner", "token", "session.start", "refused", 401),
        (None, None, "code", "session.start", "refused", 401),
        (None, None, None, "session.start", "refused", 422),
        ("op", "operator", "code", "session.start", "ok", 204),
        ("op", "operator", "cookie", "session.end", "ok", 204),
        ("viewer", "viewer", "token", "session.start", "ok", 204),
        ("op", "operator", "bearer", "session.code", "ok", 200),
    ]
    assert {r["ip"] for r in got} == {"127.0.0.1"}
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*\+00:00", r["at"]) for r in got)


async def test_token_writes_name_the_token_and_refusals_keep_their_status(cp: CP) -> None:
    async with cp.client(cp.operator) as op, cp.client(cp.viewer) as viewer:
        assert (
            await op.post("/api/tokens", json={"name": "ci", "role": "runner"})
        ).status_code == 201
        assert (
            await op.post("/api/tokens", json={"name": "ci", "role": "viewer"})
        ).status_code == 409
        assert (await op.delete("/api/tokens/ci")).status_code == 204
        assert (await op.delete("/api/tokens/ci")).status_code == 404
        r = await viewer.post("/api/tokens", json={"name": "sneak", "role": "operator"})
        assert r.status_code == 403
        r = await op.post("/api/tokens", json={"name": "bad name", "role": "viewer"})
        assert r.status_code == 422
    assert await trail(cp) == [
        ("op", "token.create", "ci", "ok", 201, "role runner"),
        ("op", "token.create", "ci", "refused", 409, "Conflict"),
        ("op", "token.revoke", "ci", "ok", 204, None),
        ("op", "token.revoke", "ci", "refused", 404, "Not found"),
        ("viewer", "token.create", None, "refused", 403, "role"),
        ("op", "token.create", None, "refused", 422, "invalid"),
    ]


async def test_config_writes_name_the_layer_and_the_keys(cp: CP) -> None:
    async with cp.client(cp.operator) as c:
        patch = {
            "set": [{"key": ["roles", "agent"], "value": "scripted"}],
            "unset": [["images", "keep_days"]],
        }
        r = await c.patch("/api/config/user", json=patch)
        assert r.status_code == 200, r.text
        tag = r.json()["etag"]
        text = {"text": '[roles]\nagent = "scripted"\n'}
        r = await c.put("/api/config/user", json=text, headers={"If-Match": f'"{tag}"'})
        assert r.status_code == 200, r.text
        r = await c.put("/api/config/user", json=text, headers={"If-Match": '"stale"'})
        assert r.status_code == 412
        assert (await c.post("/api/config/user/check", json=text)).status_code == 200
    assert await trail(cp) == [
        ("op", "config.patch", "user", "ok", 200, "set roles.agent; unset images.keep_days"),
        ("op", "config.write", "user", "ok", 200, None),
        ("op", "config.write", "user", "refused", 412, "changed"),
    ]


async def test_scenario_writes_name_the_scenario_and_the_file(cp: CP) -> None:
    base = "/api/scenarios/project"
    async with cp.client(cp.operator) as c:
        assert (await c.post(f"{base}/fresh")).status_code == 201
        assert (await c.post(f"{base}/fresh")).status_code == 409
        r = await c.post(f"{base}/control/duplicate", json={"source": "project", "name": "copy"})
        assert r.status_code == 201
        new = {**OCTET, "If-None-Match": "*"}
        r = await c.put(f"{base}/fresh/files/notes.txt", content=b"hello", headers=new)
        assert r.status_code == 200, r.text
        tag = {"If-Match": f'"{r.json()["etag"]}"'}
        r = await c.delete(f"{base}/fresh/files/notes.txt", headers=tag)
        assert r.status_code == 204
        prompts = {"set": [{"agent": "a", "key": ["system"], "value": "Be brief."}]}
        tag = {"If-Match": (await c.get(f"{base}/control/files/scenario.yaml")).headers["etag"]}
        r = await c.patch(f"{base}/control/prompts", json=prompts, headers=tag)
        assert r.status_code == 200, r.text
        assert (await c.post(f"{base}/control/selftest", json={})).status_code == 422
        assert (await c.post("/api/scenarios/check", json={"text": "x"})).status_code == 200
    assert await trail(cp) == [
        ("op", "scenario.create", "project/fresh", "ok", 201, None),
        ("op", "scenario.create", "project/fresh", "refused", 409, "Conflict"),
        ("op", "scenario.duplicate", "project/copy", "ok", 201, "from project/control"),
        ("op", "scenario.file.write", "project/fresh/notes.txt", "ok", 200, None),
        ("op", "scenario.file.delete", "project/fresh/notes.txt", "ok", 204, None),
        ("op", "scenario.prompts", "project/control", "ok", 200, "set a.system"),
        ("op", "scenario.selftest", "project/control", "refused", 422, "selftest"),
    ]


async def test_runs_started_steered_forked_and_imported_name_the_run(
    cp: CP, tmp_path: Path
) -> None:
    await hello(cp)
    ghost = str(uuid.uuid7())
    imported, tar = await sealed_tar(tmp_path)
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs",
            json={"scenario": {"source": "project", "name": "control"}, "monitor": "signals"},
        )
        assert r.status_code == 202, r.text
        (run_id,) = r.json()["run_ids"]
        assert (await c.post(f"/api/runs/{run_id}/pause", json={})).status_code == 409
        assert (await c.post(f"/api/runs/{run_id}/cancel", json={})).status_code == 202
        for action in ("resume", "sweep", "kill"):
            assert (await c.post(f"/api/runs/{ghost}/{action}", json={})).status_code == 404
        r = await c.post(f"/api/runs/{ghost}/inject", json={"agent": "a", "text": "go on"})
        assert r.status_code == 404
        assert (await c.post("/api/runs/nope/forks", json={})).status_code == 404
        tarred = {"Content-Type": "application/x-tar"}
        assert (await c.post("/api/runs/import", content=tar, headers=tarred)).status_code == 201
        r = await c.post("/api/runs/import", content=b"not a tar", headers=tarred)
        assert r.status_code == 422
    assert await trail(cp) == [
        ("op", "run.create", "project/control", "ok", 202, run_id),
        ("op", "run.pause", run_id, "refused", 409, "not_started"),
        ("op", "run.cancel", run_id, "ok", 202, None),
        ("op", "run.resume", ghost, "refused", 404, "Not found"),
        ("op", "run.sweep", ghost, "refused", 404, "Not found"),
        ("op", "run.kill", ghost, "refused", 404, "Not found"),
        ("op", "run.inject", ghost, "refused", 404, "Not found"),
        ("op", "run.fork", "nope", "refused", 404, "Not found"),
        ("op", "run.import", imported, "ok", 201, None),
        ("op", "run.import", None, "refused", 422, "Invalid request"),
    ]


async def test_reviews_scores_labels_and_model_checks_name_what_they_start(
    cp: CP, tmp_path: Path
) -> None:
    await hello(cp)
    imported, tar = await sealed_tar(tmp_path)
    async with cp.client(cp.operator) as c:
        r = await c.post(
            "/api/runs/import", content=tar, headers={"Content-Type": "application/x-tar"}
        )
        assert r.status_code == 201
        monitor = await c.post(f"/api/runs/{imported}/monitor", json={"preset": "signals"})
        score = await c.post(f"/api/runs/{imported}/score", json={})
        assert score.status_code == 409
        label = await c.post(f"/api/runs/{imported}/labels", json={"flag": 1, "label": "real"})
        assert label.status_code == 404
        r = await c.post("/api/models/check", json={"profiles": ["scripted"]})
        assert r.status_code == 202, r.text
        (check,) = r.json()["run_ids"]
    got = (await trail(cp))[1:]
    assert [g[:5] for g in got] == [
        (
            "op",
            "run.monitor",
            imported,
            "ok" if monitor.status_code < 400 else "refused",
            monitor.status_code,
        ),
        ("op", "run.score", imported, "refused", 409),
        ("op", "run.label", imported, "refused", 404),
        ("op", "models.check", "scripted", "ok", 202),
    ]
    assert got[1][5] == "truth"
    assert got[3][5] == check


async def test_every_write_route_under_api_is_audited_or_exempt(cp: CP) -> None:
    app = cp.server.config.app
    assert isinstance(app, FastAPI)
    routes = [
        (method, rc.path or "")
        for rc in iter_route_contexts(app.routes)
        if (rc.path or "").startswith("/api/")
        for method in sorted((getattr(rc, "methods", None) or set()) - {"GET", "HEAD"})
    ]
    assert set(routes) >= EXEMPT, "an exempt route is gone; drop it from EXEMPT"
    seen = len(await rows(cp))
    missing = []
    async with cp.client(cp.viewer) as c:
        for method, path in routes:
            r = await c.request(method, re.sub(r"\{[^}]+\}", "x", path))
            now = await rows(cp)
            added, seen = now[: len(now) - seen], len(now)
            if (method, path) in EXEMPT:
                assert added == [], f"{method} {path} is exempt but wrote a row"
                continue
            ok = len(added) == 1 and added[0]["status"] == r.status_code
            if not ok or not re.fullmatch(r"[a-z]+(\.[a-z]+)+", added[0]["action"]):
                missing.append(f"{method} {path}")
    assert missing == [], "these write routes record nothing: audit them or add them to EXEMPT"


async def test_no_secret_or_file_content_reaches_the_audit_log(cp: CP) -> None:
    code = cp.state.codes.issue("op")
    async with cp.client() as c:
        await c.post("/api/session", json={"token": cp.viewer})
        await c.post("/api/session", json={"code": code})
        await c.post("/api/session", json={"token": "aims_GUESSED-TOKEN"})
    async with cp.client(cp.operator) as c:
        made = (await c.post("/api/tokens", json={"name": "ci", "role": "runner"})).json()["token"]
        launch = (await c.post("/api/session/code")).json()["code"]
        patch = {"set": [{"key": ["prompts", "system"], "value": "CONFIG-VALUE"}]}
        tag = (await c.patch("/api/config/user", json=patch)).json()["etag"]
        bad = {"text": "[nonsense]\nx = 'CONFIG-TEXT'\n"}
        assert (
            await c.put("/api/config/user", json=bad, headers={"If-Match": f'"{tag}"'})
        ).status_code == 422
        new = {**OCTET, "If-None-Match": "*"}
        await c.put(
            "/api/scenarios/project/control/files/notes.txt", content=b"FILE-BYTES", headers=new
        )
        prompts = {"set": [{"agent": "a", "key": ["system"], "value": "PROMPT-TEXT"}]}
        await c.patch("/api/scenarios/project/control/prompts", json=prompts)
        await c.post(f"/api/runs/{uuid.uuid7()}/inject", json={"text": "INJECTED-TEXT"})
    raw = sqlite3.connect(cp.state.data / "server.sqlite")
    stored = json.dumps(raw.execute("select * from audit").fetchall())
    raw.close()
    assert stored.count("session.start") == 3
    for secret in (
        cp.operator,
        cp.viewer,
        made,
        code,
        launch,
        "GUESSED-TOKEN",
        "CONFIG-VALUE",
        "CONFIG-TEXT",
        "FILE-BYTES",
        "PROMPT-TEXT",
        "INJECTED-TEXT",
    ):
        assert secret not in stored


async def test_the_client_address_comes_from_a_trusted_proxy_only(tmp_path: Path) -> None:
    state = await open_state(tmp_path / "server")
    try:
        operator = await state.db.create_token("op", "operator")
        # as `aims ui --behind-proxy 10.0.0.2` sets it up
        config = uvicorn.Config(
            create_app(state, dispatcher=False),
            proxy_headers=True,
            forwarded_allow_ips="10.0.0.2",
        )
        config.load()
        app = cast(Any, config.loaded_app)
        headers = {"Authorization": f"Bearer {operator}", "X-Forwarded-For": "203.0.113.9"}
        body = {"name": "x", "role": "viewer"}
        for peer in ("10.0.0.2", "198.51.100.7"):
            transport = httpx2.ASGITransport(app=app, client=(peer, 4000))
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1", headers=headers
            ) as c:
                await c.post("/api/tokens", json=body)
                r = await c.get("/api/audit")
            assert r.status_code == 200, r.text
        assert [row["ip"] for row in reversed(r.json()["items"])] == [
            "203.0.113.9",
            "198.51.100.7",
        ]
    finally:
        await close_state(state)


async def test_a_failed_audit_write_is_logged_and_the_action_still_happens(
    cp: CP, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cp.state.db, "add_audit", broken)
    with caplog.at_level(logging.WARNING, logger="aims.server"):
        async with cp.client(cp.operator) as c:
            r = await c.post("/api/tokens", json={"name": "ci", "role": "viewer"})
    assert r.status_code == 201
    assert "ci" in {t.name for t in await cp.state.db.tokens()}
    assert any("token.create" in m and "disk I/O error" in m for m in caplog.messages)


async def test_the_audit_log_is_for_operators(cp: CP) -> None:
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/audit")
        assert (r.status_code, r.json()["code"]) == (403, "role")
        r = await c.get("/api/audit", params={"format": "jsonl"})
        assert r.status_code == 403
    async with cp.client() as c:
        assert (await c.get("/api/audit")).status_code == 401


async def test_audit_pages_newest_first_and_filters(cp: CP) -> None:
    async with cp.client(cp.operator) as op, cp.client(cp.viewer) as viewer:
        for name in ("a", "b", "c"):
            await op.post("/api/tokens", json={"name": name, "role": "viewer"})
        await op.delete("/api/tokens/a")
        await viewer.post("/api/tokens", json={"name": "d", "role": "viewer"})
        await op.patch(
            "/api/config/user", json={"set": [{"key": ["roles", "agent"], "value": "scripted"}]}
        )
    every = await rows(cp)
    assert [r["action"] for r in every] == [
        "config.patch",
        "token.create",
        "token.revoke",
        "token.create",
        "token.create",
        "token.create",
    ]
    assert [r["id"] for r in every] == sorted((r["id"] for r in every), reverse=True)

    async with cp.client(cp.operator) as c:
        first = (await c.get("/api/audit", params={"limit": 4})).json()
        assert [r["id"] for r in first["items"]] == [r["id"] for r in every[:4]]
        assert first["next"] == every[3]["id"]
        rest = (await c.get("/api/audit", params={"limit": 4, "before": first["next"]})).json()
        assert [r["id"] for r in rest["items"]] == [r["id"] for r in every[4:]]
        assert rest["next"] is None

    async def actions(**query: Any) -> list[str]:
        return [f"{r['action']} {r['target']}" for r in await rows(cp, **query)]

    assert await actions(action="token") == [
        "token.create None",
        "token.revoke a",
        "token.create c",
        "token.create b",
        "token.create a",
    ]
    assert await actions(action="token.revoke") == ["token.revoke a"]
    assert await actions(action="tok") == []
    assert await actions(token="viewer") == ["token.create None"]
    assert await actions(outcome="refused") == ["token.create None"]
    assert await actions(action="config", outcome="ok") == ["config.patch user"]

    middle = every[2]["at"]
    assert await actions(since=middle, until=middle) == ["token.revoke a"]
    assert len(await actions(since=middle)) == 3
    assert len(await actions(until=middle)) == 4
    async with cp.client(cp.operator) as c:
        r = await c.get("/api/audit", params={"outcome": "maybe"})
        assert r.status_code == 422


async def test_the_jsonl_export_streams_every_matching_row(
    cp: CP, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aims.server import audit_api

    monkeypatch.setattr(audit_api, "EXPORT_PAGE", 2)
    async with cp.client(cp.operator) as c:
        for i in range(5):
            await c.post("/api/tokens", json={"name": f"t{i}", "role": "viewer"})
        await c.delete("/api/tokens/t0")
        r = await c.get("/api/audit", params={"format": "jsonl", "action": "token.create"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert 'attachment; filename="aims-audit' in r.headers["content-disposition"]
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [x["target"] for x in lines] == ["t4", "t3", "t2", "t1", "t0"]
    assert set(lines[0]) == {
        "id",
        "at",
        "token",
        "role",
        "auth",
        "action",
        "target",
        "outcome",
        "status",
        "ip",
        "detail",
    }


async def test_rows_older_than_keep_days_go_at_start_and_each_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aims.server.db import AuditRow

    project = tmp_path / "aims.toml"
    project.write_text("[audit]\nkeep_days = 10\n")
    now = time.time()
    state = await open_state(tmp_path / "server", project=project)
    for days in (30, 11, 9, 1):
        row = AuditRow(at=now - days * 86400, action="token.create", outcome="ok", target=f"{days}")
        await state.db.add_audit(row)
    await close_state(state)

    async def kept() -> list[str | None]:
        found = await state.db.audit_rows(limit=100)
        return [r.target for r in found if r.action == "token.create"]

    state = await open_state(tmp_path / "server", project=project)
    try:
        assert await kept() == ["1", "9"]
        (pruned,) = [r for r in await state.db.audit_rows(limit=100) if r.action == "audit.prune"]
        assert (pruned.token, pruned.outcome, pruned.detail) == (
            None,
            "ok",
            "2 rows older than 10 days",
        )
        monkeypatch.setattr(time, "time", lambda: now + 2 * 86400)
        await dispatch.housekeeping(state)
        assert await kept() == ["1"]
        project.write_text("[audit]\nkeep_days = 0\n")
        monkeypatch.setattr(time, "time", lambda: now + 1000 * 86400)
        await dispatch.housekeeping(state)
        assert await kept() == ["1"]
    finally:
        await close_state(state)


def test_aims_audit_reads_the_local_trail(tmp_path: Path) -> None:
    async def fill() -> None:
        async with (
            control_plane(tmp_path) as cp,
            cp.client(cp.operator) as op,
            cp.client(cp.viewer) as viewer,
        ):
            await op.post("/api/tokens", json={"name": "ci", "role": "runner"})
            await op.delete("/api/tokens/ci")
            await viewer.post("/api/tokens", json={"name": "x", "role": "viewer"})

    asyncio.run(fill())
    data = ["--data-dir", str(tmp_path / "server")]
    runner = CliRunner()
    out = runner.invoke(cli, ["audit", *data])
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert len(lines) == 3
    when = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d"
    assert re.fullmatch(
        rf"{when}  viewer  token\.create  -  refused 403  127\.0\.0\.1  role", lines[0]
    )
    assert "  op  token.create  ci  ok 201  127.0.0.1  role runner" in lines[2]
    out = runner.invoke(cli, ["audit", *data, "--action", "token.revoke"])
    assert [line.split("  ")[2:4] for line in out.output.splitlines()] == [["token.revoke", "ci"]]
    out = runner.invoke(cli, ["audit", *data, "--outcome", "refused", "--token", "viewer"])
    assert len(out.output.splitlines()) == 1
    out = runner.invoke(cli, ["audit", *data, "--limit", "1", "--jsonl"])
    (only,) = [json.loads(line) for line in out.output.splitlines()]
    assert (only["token"], only["action"], only["status"]) == ("viewer", "token.create", 403)
    out = runner.invoke(cli, ["audit", *data, "--since", "2999-01-01"])
    assert (out.exit_code, out.output) == (0, "")
    out = runner.invoke(cli, ["audit", *data, "--outcome", "maybe"])
    assert out.exit_code == 2


@pytest.mark.parametrize("jsonl", [False, True])
def test_aims_audit_prints_no_rows_when_one_fails(tmp_path: Path, jsonl: bool) -> None:
    data = tmp_path / "server"
    runner = CliRunner()
    made = runner.invoke(cli, ["token", "create", "a", "--data-dir", str(data)])
    assert made.exit_code == 0, made.output
    # a row whose time no date holds, older than the next one
    db = sqlite3.connect(data / "server.sqlite")
    try:
        with db:
            db.execute("insert into audit (at_us, action, outcome) values (?, 'x', 'ok')", (2**62,))
    finally:
        db.close()
    made = runner.invoke(cli, ["token", "create", "b", "--data-dir", str(data)])
    assert made.exit_code == 0, made.output
    out = runner.invoke(cli, ["audit", "--data-dir", str(data), *(["--jsonl"] if jsonl else [])])
    assert out.exit_code == 1
    assert out.stdout == ""
