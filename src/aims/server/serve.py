"""`aims ui`: the control plane on this machine, with a runner of its own unless told not to."""

import asyncio
import contextlib
import html
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from aims.net import loopback, no_proxy
from aims.report.escape import visible
from aims.server.app import create_app
from aims.server.db import UI_TOKEN, Token, new_token
from aims.server.state import CODE_TTL_S, close_state, open_state
from aims.terminal import on_hangup

log = logging.getLogger("aims.server")

PORT = 8484
SERVER_JSON = "server.json"
LOCAL_RUNNER = "local"
UI_RUNNER = "AIMS_UI_RUNNER"


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
    audit: str = "auto"
    tls_cert: Path | None = None
    tls_key: Path | None = None
    behind_proxy: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    public_url: str | None = None
    auth: bool = True
    open_browser: bool = True
    keep_days: float | None = None


def public_url(value: str) -> str:
    """`value` as the URL people open; a bare host means https, as in a Caddy site address."""
    url = value if "://" in value else f"https://{value}"
    try:
        parts = urlsplit(url)
        bad = parts.scheme not in ("http", "https") or not parts.hostname or parts.port == 0
        bad = bad or bool(parts.path.strip("/") or parts.query or parts.fragment)
    except ValueError:
        bad = True
    if bad:
        raise ServeError(
            f"--public-url {value!r}: give a host, or an http or https URL without a path"
        )
    return url.rstrip("/")


def check(o: UiOptions) -> None:
    if o.public_url is not None:
        public_url(o.public_url)
    exposed = not loopback(o.host)
    if (o.tls_cert is None) != (o.tls_key is None):
        raise ServeError("--tls-cert and --tls-key go together")
    if o.data.exists() and not o.data.is_dir():
        raise ServeError(f"{o.data} is not a directory")
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


def private_temp(path: Path, data: str) -> Path:
    """A new file beside `path` that holds `data`, synced, readable by its owner only. Its name
    is random and must be free, so nothing put there before is followed or written."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def write_private(path: Path, data: str) -> None:
    """Replace `path` with `data`, readable by its owner only, whole or not at all."""
    tmp = private_temp(path, data)
    try:
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def serve(o: UiOptions) -> int:
    check(o)
    if not o.data.exists():
        o.data.mkdir(mode=0o700, parents=True)
    tls = o.tls_cert is not None
    shown = None if o.public_url is None else public_url(o.public_url)
    # Behind a TLS proxy the browser is on https though this server is not.
    https = tls or (shown is not None and shown.startswith("https://"))
    state = await open_state(
        o.data,
        project=o.project,
        auth=o.auth,
        tls=https,
        public_url=shown,
        keep_days=o.keep_days,
    )
    child: asyncio.subprocess.Process | None = None
    server: _Server | None = None
    task: asyncio.Task[None] | None = None
    info = o.data / SERVER_JSON
    lifeline = -1
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    handled: list[signal.Signals] = []
    stops = 0
    told = 0

    def tell_runner() -> None:
        """The runner leaves Ctrl-C to this server, which sends it one SIGTERM per stop:
        the first seals its runs as cancelled, the next kills it now."""
        nonlocal told
        if child is not None and child.returncode is None:
            told += 1
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGTERM)

    def on_signal() -> None:
        """No uvicorn force_exit on a second stop. It skips the app's shutdown, so the
        lifespan is cancelled at exit and logs a traceback. It also never cuts the wait for
        open connections, which timeout_graceful_shutdown caps at 5 s."""
        nonlocal stops
        stops += 1
        stop.set()
        tell_runner()

    try:
        runner_token = new_token()
        state.auth.ephemeral[runner_token] = Token(LOCAL_RUNNER, "runner", 0.0)
        ui_token = await state.db.rotate_token(UI_TOKEN, "operator")
        if o.runner:
            state.local_runner = LOCAL_RUNNER
        public = () if shown is None else (urlsplit(shown).hostname or "",)
        app = create_app(state, hosts=(connect_host(o.host), *o.hosts, *public))
        config = uvicorn.Config(
            app,
            host=o.host.strip("[]"),
            port=o.port,
            # No handlers of uvicorn's own: its lines reach the handler `aims ui` set, which
            # writes them through visible() as it does every other log line
            log_config=None,
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
                try:
                    task.result()
                except OSError as e:
                    raise ServeError(f"the server did not start: {e}") from None
                raise ServeError("the server did not start")
            await asyncio.sleep(0.05)
        port = _port(server, o.port)
        scheme = "https" if tls else "http"
        local = f"{scheme}://{connect_host(o.host)}:{port}"
        shown = shown or local
        write_private(
            info,
            json.dumps(
                {"url": local, "public": shown, "token": runner_token, "operator": ui_token}
            ),
        )
        # Handlers go in before the runner starts. A Ctrl-C with no handler cancels this task.
        # The runner would then get no SIGTERM, only the SIGKILL below.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
            handled.append(sig)
        # A hangup starts a stop and never adds to one: nobody is left to press Ctrl-C.
        if on_hangup(loop, lambda: None if stops else on_signal()):
            handled.append(signal.SIGHUP)
        if o.runner:
            # The runner reads a pipe that only this process holds open. The pipe ends when
            # this process does, even by SIGKILL, and the runner then stops with its helpers.
            runner_stdin, lifeline = os.pipe()
            try:
                child = await _spawn_runner(local, runner_token, o, runner_stdin)
            finally:
                os.close(runner_stdin)
        url = f"{shown}/#code={state.codes.issue(UI_TOKEN)}" if o.auth else shown
        # --public-url is outside text
        print(visible(f"AIMS control plane on {shown}"), file=sys.stderr)
        if not o.auth:
            print(
                "WARNING: --no-auth: anyone who can reach this port on this machine can start"
                " containers and read every run",
                file=sys.stderr,
            )
        print(visible(f"open {url}"), file=sys.stderr)
        if o.auth:
            print(
                "the link signs in once, within 2 minutes; `aims open` gives a new one, or sign"
                " in with a token from `aims token create <name> --role operator`",
                file=sys.stderr,
            )
        if o.open_browser:
            await asyncio.to_thread(open_link, o.data, url)
        done, _ = await asyncio.wait(
            [task, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            task.result()
    finally:
        # The same stop after an error as after Ctrl-C: the runner seals its runs, then the
        # server shuts its app down.
        try:
            if child is not None and child.returncode is None:
                print(
                    "stopping the local runner; its runs are sealed as cancelled", file=sys.stderr
                )
                if not told:
                    tell_runner()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(180):
                        await child.wait()
            state.stopping.set()
            if server is not None and task is not None and not task.done():
                server.should_exit = True
                await task
        finally:
            for sig in handled:
                loop.remove_signal_handler(sig)
            if child is not None:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
                if child.returncode:
                    print(runner_ended(child.returncode), file=sys.stderr)
            if lifeline >= 0:
                os.close(lifeline)
            info.unlink(missing_ok=True)
            # the codes in them die with this server
            _prune(o.data, None)
            await close_state(state)
    return 0


def runner_ended(code: int) -> str:
    """How the local runner ended, from its exit code; asyncio gives -N for signal N."""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"the local runner was killed by {name}"
    return f"the local runner exited with code {code}"


def open_link(
    folder: Path,
    url: str,
    *,
    snap: Callable[[], bool] | None = None,
    browser: Callable[[str], object] = webbrowser.open,
) -> Path | None:
    """Open `url` in a browser through a redirect file in `folder`, or as it is for a snap
    browser, which cannot read the file. Returns the file, if one was written; files of
    codes that expired are removed."""
    _prune(folder, time.time() - CODE_TTL_S)
    path = None if (snap or snap_browser)() else _redirect(folder, url)
    with contextlib.suppress(Exception):
        browser(url if path is None else path.as_uri())
    return path


def _prune(folder: Path, before: float | None) -> None:
    """Remove the redirect files in `folder` made before `before`, or all of them."""
    for path in folder.glob("open-*.html"):
        with contextlib.suppress(OSError):
            if before is None or path.stat().st_mtime < before:
                path.unlink()


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


async def _spawn_runner(
    url: str, token: str, o: UiOptions, stdin: int
) -> asyncio.subprocess.Process:
    """The runner `aims ui` brings: its token goes in the environment, never on argv.
    UI_RUNNER tells it to ignore the terminal's Ctrl-C and wait for this server's SIGTERM, and
    to stop once `stdin` ends. NO_PROXY lists this server, on any address."""
    host = urlsplit(url).hostname or ""
    env = {**os.environ, **no_proxy(host), "AIMS_TOKEN": token, UI_RUNNER: "1"}
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
        "--audit",
        o.audit,
        *(["--config", str(o.project)] if o.project is not None else []),
        *(["--tls-ca", str(o.tls_cert)] if o.tls_cert is not None else []),
        *(["--keep-days", f"{o.keep_days:g}"] if o.keep_days is not None else []),
        stdin=stdin,
        env=env,
    )
