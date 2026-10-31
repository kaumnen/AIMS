import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cp import CP, control_plane
from typer.testing import CliRunner

from aims.cli import app
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall
from aims.sandbox import DockerBackend, SandboxSpec
from aims.server.serve import (
    ServeError,
    UiOptions,
    _redirect,
    check,
    connect_host,
    open_link,
    server_dir,
    snap_browser,
)

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).parent.parent


def test_exposed_binds_need_tls_or_a_proxy() -> None:
    check(UiOptions(host="127.0.0.1"))
    check(UiOptions(host="[::1]"))
    with pytest.raises(ServeError, match="exposes"):
        check(UiOptions(host="0.0.0.0"))
    check(UiOptions(host="0.0.0.0", behind_proxy=["10.0.0.2"]))
    with pytest.raises(ServeError, match="loopback only"):
        check(UiOptions(host="0.0.0.0", behind_proxy=["10.0.0.2"], auth=False))
    with pytest.raises(ServeError, match="go together"):
        check(UiOptions(tls_cert=Path("c.pem")))


def test_the_browser_opens_a_private_redirect_not_the_code(tmp_path: Path) -> None:
    url = "http://127.0.0.1:8484/#code=abc&x=<b>"
    path = _redirect(tmp_path, url)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "abc" not in path.as_uri()
    text = path.read_text()
    assert "#code=abc&amp;x=&lt;b&gt;" in text
    assert "<b>" not in text


def test_a_link_opens_through_a_redirect_and_old_ones_go(tmp_path: Path) -> None:
    stale = tmp_path / "open-1.html"
    stale.write_text("old")
    os.utime(stale, (0, 0))
    kept = tmp_path / "other.html"
    kept.write_text("x")
    os.utime(kept, (0, 0))
    opened: list[str] = []
    path = open_link(
        tmp_path, "http://127.0.0.1:1/#code=abc", snap=lambda: False, browser=opened.append
    )
    assert path is not None
    assert opened == [path.as_uri()]
    assert "#code=abc" in path.read_text()
    assert not stale.exists()
    assert kept.exists()
    assert open_link(tmp_path, "http://x/#code=d", snap=lambda: True, browser=opened.append) is None
    assert opened[-1] == "http://x/#code=d"


def _desktop(base: Path, name: str, exec_line: str) -> None:
    apps = base / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = f"[Desktop Entry]\nName=B\nExec={exec_line}\n[Desktop Action new]\nExec=x\n"
    (apps / name).write_text(entry)


def _program(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_a_snap_browser_gets_the_url_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    snapd = tmp_path / "var" / "lib" / "snapd" / "desktop"
    share = tmp_path / "share"
    bin_dir = tmp_path / "bin"
    env = {"XDG_DATA_HOME": str(tmp_path / "home"), "XDG_DATA_DIRS": f"{share}:{snapd}"}
    env["PATH"] = str(bin_dir)
    _desktop(snapd, "firefox_firefox.desktop", "env BAMF=/x /snap/bin/firefox %u")
    _desktop(share, "firefox.desktop", "firefox %u")
    _desktop(share, "chromium.desktop", "env A=1 chromium --flag=1 %U")
    _program(bin_dir, "firefox", '#!/bin/sh\nexec /snap/bin/firefox "$@"\n')
    _program(bin_dir, "chromium", "#!/bin/sh\nexec /usr/lib/chromium/chromium\n")
    snap = _program(tmp_path / "usr", "snap", "")
    (bin_dir / "linked").symlink_to(snap)

    def answers(default: str | None, html: str | None = None) -> Any:
        return lambda q: default if q[0] == "xdg-settings" else html

    assert snap_browser(env, answers("firefox_firefox.desktop"))
    assert snap_browser(env, answers(None, "firefox_firefox.desktop"))
    assert snap_browser(env, answers("firefox.desktop"))
    assert not snap_browser(env, answers("chromium.desktop", "chromium.desktop"))
    assert not snap_browser(env, answers(None))
    assert not snap_browser(env, answers("../x.desktop", "missing.desktop"))
    assert snap_browser({**env, "BROWSER": "linked %s"}, answers(None))
    assert not snap_browser({**env, "BROWSER": "chromium"}, answers(None))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not snap_browser(env, answers("firefox_firefox.desktop"))


def test_local_processes_reach_the_address_bound() -> None:
    assert connect_host("127.0.0.1") == "127.0.0.1"
    assert connect_host("0.0.0.0") == "127.0.0.1"
    assert connect_host("::") == "[::1]"
    assert connect_host("[::1]") == "[::1]"
    assert connect_host("10.0.0.5") == "10.0.0.5"
    assert connect_host("aims.example") == "aims.example"
    assert server_dir({"AIMS_SERVER_DIR": "/srv/aims"}) == Path("/srv/aims")


def test_tokens_from_the_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    made = runner.invoke(
        app, ["token", "create", "ci", "--role", "runner", "--data-dir", str(tmp_path)]
    )
    assert made.exit_code == 0, made.output
    assert made.output.strip().startswith("aims_")
    again = runner.invoke(app, ["token", "create", "ci", "--data-dir", str(tmp_path)])
    assert again.exit_code == 1
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert re.search(r"^ci  runner  .*  live$", listed.output, re.M)
    gone = runner.invoke(app, ["token", "revoke", "ci", "--data-dir", str(tmp_path)])
    assert gone.exit_code == 0
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert "revoked" in listed.output
    assert made.output.strip() not in (tmp_path / "server.sqlite").read_text(errors="ignore")


def test_the_openapi_export_is_stable_and_committed(tmp_path: Path) -> None:
    runner = CliRunner()
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert runner.invoke(app, ["openapi", "--out", str(a)]).exit_code == 0
    assert runner.invoke(app, ["openapi", "--out", str(b)]).exit_code == 0
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() == (ROOT / "web" / "openapi.json").read_bytes(), (
        "web/openapi.json is stale: run `uv run aims openapi --out web/openapi.json`"
    )


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


async def test_push_ships_runs_recorded_offline(cp: CP, tmp_path: Path) -> None:
    started = RunStarted(scenario="offline", oversight="observe", agents=["a"])
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        sealed = await store.create_run(started)
        await store.append(
            sealed,
            [
                Draft(ToolCall(call_id="c", name="x", arguments="{}"), "a"),
                Draft(RunFinished(status="completed")),
            ],
        )
        open_run = await store.create_run(started)
    env = {"AIMS_TOKEN": cp.runner}
    result = await asyncio.to_thread(
        CliRunner().invoke, app, ["push", str(tmp_path / "runs"), "--server", cp.url], env=env
    )
    assert result.exit_code == 0, result.output
    assert f"{sealed} completed 3 events" in result.output
    assert f"skip {open_run}" in result.output
    copy = cp.state.store.root / sealed / "events.jsonl"
    assert copy.read_bytes() == (tmp_path / "runs" / sealed / "events.jsonl").read_bytes()
    meta = await cp.state.db.run_meta(sealed)
    assert meta is not None
    assert meta.runner is not None
    assert meta.runner.startswith("push@")


async def read_url(proc: asyncio.subprocess.Process) -> str:
    assert proc.stderr is not None
    async with asyncio.timeout(60):
        while line := await proc.stderr.readline():
            text = line.decode()
            if text.startswith("open "):
                return text.removeprefix("open ").strip()
    raise AssertionError("aims ui printed no URL")


@contextlib.asynccontextmanager
async def ui_process(
    tmp_path: Path, *args: str
) -> AsyncGenerator[tuple[asyncio.subprocess.Process, str]]:
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "xdg")}
    env.pop("AIMS_TOKEN", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aims",
        "ui",
        "--port",
        "0",
        "--no-open",
        "--data-dir",
        str(tmp_path / "server"),
        *args,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    try:
        yield proc, await read_url(proc)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_aims_ui_hands_the_browser_a_code_not_the_token(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, url):
        base, _, code = url.partition("/#code=")
        assert base.startswith("http://127.0.0.1:")
        info = tmp_path / "server" / "server.json"
        assert stat.S_IMODE(info.stat().st_mode) == 0o600
        local = json.loads(info.read_text())
        assert local["url"] == base
        assert code
        assert code != local["operator"]
        async with httpx2.AsyncClient(base_url=base, trust_env=False) as c:
            assert (await c.get("/api/health")).status_code == 401
            shell = await c.get("/")
            assert shell.status_code == 200
            r = await c.post("/api/session", json={"code": code})
            assert r.status_code == 204
            assert (await c.get("/api/session")).json() == {
                "name": "ui",
                "role": "operator",
                "auth": True,
            }
            r = await c.post("/api/session", json={"token": local["token"]})
            assert r.status_code == 401
        async with httpx2.AsyncClient(base_url=base, trust_env=False) as c:
            assert (await c.post("/api/session", json={"code": code})).status_code == 401
            r = await c.post("/api/session", json={"token": local["operator"]})
            assert r.status_code == 204
        async with httpx2.AsyncClient(
            base_url=base, headers={"Authorization": f"Bearer {local['token']}"}, trust_env=False
        ) as rc:
            r = await rc.post("/runner/v1/heads", json={"run_ids": []})
            assert (r.status_code, r.json()["code"]) == (400, "runner_name")
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0
        assert not info.exists()
    async with ui_process(tmp_path, "--no-runner") as (proc, second):
        assert second.partition("/#code=")[2] != code
        proc.send_signal(signal.SIGTERM)
        await proc.wait()


async def test_aims_open_gives_a_new_link_while_aims_ui_runs(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, first):
        base = first.partition("/#code=")[0]
        env = {**os.environ, "AIMS_SERVER_DIR": str(tmp_path / "server")}
        env.pop("AIMS_TOKEN", None)
        done = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "aims",
            "open",
            "--no-open",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await done.communicate()
        assert done.returncode == 0, err
        link = out.decode().strip()
        assert link.startswith(f"{base}/#code=")
        assert link != first
        assert "within 120 s" in err.decode()
        async with httpx2.AsyncClient(base_url=base, trust_env=False) as c:
            code = link.partition("/#code=")[2]
            assert (await c.post("/api/session", json={"code": code})).status_code == 204
            assert (await c.get("/api/session")).json()["name"] == "ui"
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0
    assert list((tmp_path / "server").glob("open-*.html")) == []


async def test_aims_ui_answers_on_the_address_it_binds(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner", "--host", "::1") as (proc, _):
        local = json.loads((tmp_path / "server" / "server.json").read_text())
        assert local["url"].startswith("http://[::1]:")
        async with httpx2.AsyncClient(base_url=local["url"], trust_env=False) as c:
            assert (await c.get("/api/health")).status_code == 401
        proc.send_signal(signal.SIGTERM)
        await proc.wait()


@pytest.mark.docker
async def test_aims_ui_brings_a_runner_and_stops_it(tmp_path: Path) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    async with ui_process(tmp_path) as (proc, url):
        base = url.partition("/#code=")[0]
        token = json.loads((tmp_path / "server" / "server.json").read_text())["operator"]
        async with httpx2.AsyncClient(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, trust_env=False
        ) as c:
            async with asyncio.timeout(120):
                while True:
                    runners = (await c.get("/api/runners")).json()
                    if runners and runners[0]["connected"]:
                        break
                    await asyncio.sleep(0.2)
            assert (runners[0]["name"], runners[0]["local"]) == ("local", True)
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(120):
            assert await proc.wait() == 0
    assert (tmp_path / "xdg" / "aims" / "runner" / "runs").is_dir()


@pytest.mark.docker
@pytest.mark.tracer
async def test_the_tracer_comes_back_after_it_stops(tmp_path: Path) -> None:
    try:
        backend = await DockerBackend.open(audit="kernel")
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            raise
        pytest.skip(f"no kernel tracer: {e}")
    async with backend:
        tracer = backend.tracer
        assert tracer is not None
        await tracer.stop()
        assert not tracer.alive
        await backend.restart_tracer()
        assert backend.tracer is not None
        assert backend.tracer is not tracer
        assert backend.tracer.alive
        assert backend.tracer_restarts == 1
        sandbox = await backend.create(
            SandboxSpec(image="python:3.13-slim"), run_id="r", agent_id="a"
        )
        try:
            assert sandbox.audit == "kernel"
        finally:
            await sandbox.close()
