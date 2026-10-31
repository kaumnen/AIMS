"""`aims ui`: the control plane on this machine, with a runner of its own unless told not to."""

import asyncio
import contextlib
import html
import ipaddress
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

from aims.server.app import create_app
from aims.server.db import UI_TOKEN, Token, new_token
from aims.server.state import close_state, open_state

log = logging.getLogger("aims.server")

PORT = 8484
SERVER_JSON = "server.json"
LOCAL_RUNNER = "local"


def server_dir(env: dict[str, str] | None = None) -> Path:
    """Where `aims ui` keeps its state: `$AIMS_SERVER_DIR`, else `%LOCALAPPDATA%\\aims\\server`
    on Windows, else `$XDG_DATA_HOME/aims/server` or `~/.local/share/aims/server`."""
    env = dict(os.environ) if env is None else env
    if value := env.get("AIMS_SERVER_DIR"):
        return Path(value)
    if os.name == "nt" and (local := env.get("LOCALAPPDATA")):
        return Path(local) / "aims" / "server"
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aims" / "server"


def connect_host(host: str) -> str:
    """How a process on this machine reaches a server bound to `host`."""
    bare = host.strip("[]")
    if bare in ("0.0.0.0", "", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    if bare == "::":
        return "[::1]"
    return f"[{bare}]" if ":" in bare else bare


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class ServeError(Exception):
    pass


@dataclass(slots=True)
class UiOptions:
    host: str = "127.0.0.1"
    port: int = PORT
    data: Path = field(default_factory=server_dir)
    project: Path | None = None
    runner: bool = True
    slots: int = 2
    tls_cert: Path | None = None
    tls_key: Path | None = None
    behind_proxy: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    auth: bool = True
    open_browser: bool = True


def check(o: UiOptions) -> None:
    exposed = not loopback(o.host)
    if (o.tls_cert is None) != (o.tls_key is None):
        raise ServeError("--tls-cert and --tls-key go together")
    if exposed and o.tls_cert is None and not o.behind_proxy:
        raise ServeError(
            f"binding {o.host} exposes the API to the network: give --tls-cert and --tls-key,"
            " or --behind-proxy with the proxy's address"
        )
    if not o.auth and exposed:
        raise ServeError("--no-auth works on loopback only")


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers; `aims ui` stops its runner first."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    path.chmod(0o600)


async def serve(o: UiOptions) -> int:
    check(o)
    if not o.data.exists():
        o.data.mkdir(mode=0o700, parents=True)
    tls = o.tls_cert is not None
    state = await open_state(o.data, project=o.project, auth=o.auth, tls=tls)
    child: asyncio.subprocess.Process | None = None
    info = o.data / SERVER_JSON
    launch: list[Path] = []
    try:
        runner_token = new_token()
        state.auth.ephemeral[runner_token] = Token(LOCAL_RUNNER, "runner", 0.0)
        ui_token = await state.db.rotate_token(UI_TOKEN, "operator")
        if o.runner:
            state.local_runner = LOCAL_RUNNER
        app = create_app(state, hosts=(connect_host(o.host), *o.hosts))
        config = uvicorn.Config(
            app,
            host=o.host.strip("[]"),
            port=o.port,
            log_level="warning",
            lifespan="on",
            proxy_headers=bool(o.behind_proxy),
            forwarded_allow_ips=",".join(o.behind_proxy) or None,
            ssl_certfile=None if o.tls_cert is None else str(o.tls_cert),
            ssl_keyfile=None if o.tls_key is None else str(o.tls_key),
            timeout_graceful_shutdown=5,
        )
        server = _Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()
                raise ServeError("the server did not start")
            await asyncio.sleep(0.05)
        port = _port(server, o.port)
        scheme = "https" if tls else "http"
        local = f"{scheme}://{connect_host(o.host)}:{port}"
        write_private(info, json.dumps({"url": local, "token": runner_token, "operator": ui_token}))
        if o.runner:
            child = await _spawn_runner(local, runner_token, o)
        url = f"{local}/#code={state.codes.issue(UI_TOKEN)}" if o.auth else local
        print(f"AIMS control plane on {local}", file=sys.stderr)
        if not o.auth:
            print(
                "WARNING: --no-auth: anyone who can reach this port on this machine can start"
                " containers and read every run",
                file=sys.stderr,
            )
        print(f"open {url}", file=sys.stderr)
        if o.auth:
            print(
                "the link signs in once, within 2 minutes; after that sign in with a token from"
                " `aims token create <name> --role operator`",
                file=sys.stderr,
            )
        if o.open_browser:
            target = url
            if not await asyncio.to_thread(snap_browser):
                launch.append(_redirect(o.data, url))
                target = launch[0].as_uri()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(webbrowser.open, target)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        stops = 0

        def on_signal() -> None:
            nonlocal stops
            stops += 1
            stop.set()
            if stops > 1:
                server.force_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
        try:
            done, _ = await asyncio.wait(
                [task, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                task.result()
            if child is not None and child.returncode is None:
                print(
                    "stopping the local runner; its runs are sealed as cancelled", file=sys.stderr
                )
                child.send_signal(signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(180):
                        await child.wait()
            state.stopping.set()
            server.should_exit = True
            await task
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        info.unlink(missing_ok=True)
        for path in launch:
            path.unlink(missing_ok=True)
        await close_state(state)
    return 0


def _redirect(data: Path, url: str) -> Path:
    """A file only this user can read that sends the browser on to `url`, so even the one-shot
    code never sits in a browser's argv where other users of the machine can read it."""
    path = data / f"open-{os.getpid()}.html"
    target = html.escape(url, quote=True)
    write_private(
        path,
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}">'
        f'<title>AIMS</title><a href="{target}">Open AIMS</a>\n',
    )
    return path


def _ask(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout.strip() or None


def snap_browser(
    env: Mapping[str, str] | None = None, ask: Callable[[list[str]], str | None] = _ask
) -> bool:
    """Whether the browser that would open here is a snap. Snaps cannot read files under a
    hidden directory of $HOME, so the redirect file would not load; they get the URL itself."""
    if sys.platform != "linux":
        return False
    env = os.environ if env is None else env
    commands: list[str] = []
    if browser := env.get("BROWSER", "").split(os.pathsep)[0].strip():
        commands.append(browser.split()[0])
    for query in (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "text/html"],
    ):
        desktop = ask(query)
        file = _desktop_file(desktop, env) if desktop else None
        if file is None:
            continue
        if "snapd" in file.parts:
            return True
        if command := _exec(file):
            commands.append(command)
    return any(_snapped(c, env) for c in commands)


def _desktop_file(desktop: str, env: Mapping[str, str]) -> Path | None:
    if "/" in desktop or not desktop.endswith(".desktop"):
        return None
    home = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for base in [home, *dirs.split(":")]:
        path = Path(base) / "applications" / desktop
        if base and path.is_file():
            return path
    return None


def _exec(file: Path) -> str | None:
    """The program a desktop entry starts, past any `env VAR=value` prefix."""
    try:
        lines = file.read_text(errors="replace").splitlines()
    except OSError:
        return None
    section = ""
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Desktop Entry]" and line.startswith("Exec="):
            try:
                words = shlex.split(line.removeprefix("Exec="))
            except ValueError:
                return None
            if words and words[0] == "env":
                words = words[1:]
            words = [w for w in words if "=" not in w.partition("/")[0]]
            return words[0] if words else None
    return None


def _snapped(command: str, env: Mapping[str, str]) -> bool:
    found = shutil.which(command, path=env.get("PATH"))
    if found is None:
        return False
    path = Path(found)
    if path.parts[1:2] == ("snap",) or path.resolve().name == "snap":
        return True
    try:
        with path.open("rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/bin/" in head


def _port(server: uvicorn.Server, wanted: int) -> int:
    for s in server.servers:
        for sock in s.sockets:
            name = sock.getsockname()
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return int(name[1])
    return wanted


async def _spawn_runner(url: str, token: str, o: UiOptions) -> asyncio.subprocess.Process:
    """The runner `aims ui` brings: its token goes in the environment, never on argv."""
    env = {**os.environ, "AIMS_TOKEN": token}
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-P",
        "-m",
        "aims",
        "runner",
        "--server",
        url,
        "--name",
        LOCAL_RUNNER,
        "--slots",
        str(o.slots),
        *(["--tls-ca", str(o.tls_cert)] if o.tls_cert is not None else []),
        env=env,
    )
