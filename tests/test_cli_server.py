import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import stat
import sys
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import aiodocker
import httpx2
import pytest
from cp import CP, control_plane
from typer.testing import CliRunner

from aims.cli import EXIT_CANCELLED, app
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall
from aims.node.service import Node, NodeOptions
from aims.sandbox import DockerBackend, SandboxSpec
from aims.server.serve import (
    UI_RUNNER,
    ServeError,
    UiOptions,
    _redirect,
    _spawn_runner,
    check,
    connect_host,
    open_link,
    public_url,
    serve,
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


def test_a_public_url_is_a_bare_host_on_https_or_an_http_url_without_a_path() -> None:
    assert public_url("aims.example") == "https://aims.example"
    assert public_url("98.92.27.126") == "https://98.92.27.126"
    assert public_url("http://localhost:8484/") == "http://localhost:8484"
    assert public_url("https://[2001:db8::1]:8443") == "https://[2001:db8::1]:8443"
    for bad in ("ftp://aims.example", "aims.example/sub", "http://", "https://a.example:99999"):
        with pytest.raises(ServeError, match="--public-url"):
            public_url(bad)
    with pytest.raises(ServeError, match="--public-url"):
        check(UiOptions(public_url="https://a.example/?x=1"))


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


def test_runner_and_ui_refuse_an_unknown_audit_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def serve(o: UiOptions) -> int:
        return 0

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            pass

    monkeypatch.setattr("aims.server.serve.serve", serve)
    monkeypatch.setattr("aims.node.service.Node", Stub)
    runner = CliRunner()
    for args in (
        ["runner", "--server", "http://127.0.0.1:9", "--audit", "full"],
        ["ui", "--no-open", "--no-runner", "--data-dir", str(tmp_path), "--audit", "full"],
    ):
        result = runner.invoke(app, args, env={"AIMS_TOKEN": "x"})
        assert result.exit_code == 2, result.output
        assert "auto, kernel, snapshot" in result.output


def test_the_runner_gets_its_audit_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[NodeOptions] = []

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            seen.append(options)

        async def run(self, stop: asyncio.Event) -> None:
            pass

    monkeypatch.setattr("aims.node.service.Node", Stub)
    base = ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)]
    for args, mode in ((base, "auto"), ([*base, "--audit", "snapshot"], "snapshot")):
        result = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "x"})
        assert result.exit_code == 0, result.output
        assert seen.pop().audit == mode


# In `signalled_runner`: `aims ui` ends, so the pipe on the runner's stdin ends.
GONE = "gone"


def signalled_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signals: Sequence[signal.Signals | str],
    env: dict[str, str],
    hung: list[tuple[int, ...]] | None = None,
    said: list[str] | None = None,
) -> tuple[int, bool, bool, bool]:
    """Run `aims runner` on a stub node that sends itself `signals`, 10 ms apart; GONE ends
    the pipe on its stdin, which this test holds as `aims ui` does. Returns the exit code,
    whether the node was asked to stop, whether it ran to its end (a kill now cancels it) and
    whether the runner said it kills now. Each hangup that would point the streams at
    /dev/null goes to `hung` instead. The output goes to `said`."""
    seen: dict[str, bool] = {"done": False}
    read_end, write_end = os.pipe()
    held = [write_end]

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            try:
                for sig in signals:
                    if isinstance(sig, str):
                        os.close(held.pop())
                    else:
                        os.kill(os.getpid(), sig)
                    await asyncio.sleep(0.01)
                async with asyncio.timeout(5):
                    await stop.wait()
                # time for a late signal to cancel this
                await asyncio.sleep(0.2)
                seen["done"] = True
            finally:
                seen["stop"] = stop.is_set()

    monkeypatch.setattr("aims.node.service.Node", Stub)
    if hung is not None:
        monkeypatch.setattr("aims.terminal.hang_up", hung.append)
    previous = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    # a SIGHUP the runner does not handle would end this test process
    signal.signal(signal.SIGHUP, lambda *_: None)
    stdin = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        result = CliRunner().invoke(
            app,
            ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)],
            env={"AIMS_TOKEN": "x", **env},
        )
        # a runner `aims ui` starts ignores what comes after its stop
        assert (signal.getsignal(signal.SIGTERM) == signal.SIG_IGN) == bool(env)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.dup2(stdin, 0)
        os.close(stdin)
        for fd in held:
            os.close(fd)
    if said is not None:
        said.append(result.output)
    return result.exit_code, seen["stop"], seen["done"], "killing now" in result.output


def test_a_runner_aims_ui_starts_stops_once_for_a_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ctrl-C in the terminal reaches the whole process group: the runner gets SIGINT and
    # `aims ui` sends it SIGTERM. That is one Ctrl-C, so the runner seals its runs.
    ended = signalled_runner(
        monkeypatch, tmp_path, [signal.SIGINT, signal.SIGTERM], {UI_RUNNER: "1"}
    )
    assert ended == (0, True, True, False)


def test_a_runner_aims_ui_starts_kills_now_on_a_second_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ended = signalled_runner(
        monkeypatch,
        tmp_path,
        [signal.SIGINT, signal.SIGTERM, signal.SIGINT, signal.SIGTERM],
        {UI_RUNNER: "1"},
    )
    assert ended == (EXIT_CANCELLED, True, False, True)


def test_a_runner_on_its_own_kills_now_on_a_second_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for signals in ([signal.SIGINT, signal.SIGINT], [signal.SIGINT, signal.SIGTERM]):
        assert signalled_runner(monkeypatch, tmp_path, signals, {}) == (
            EXIT_CANCELLED,
            True,
            False,
            True,
        )


def test_a_runner_on_its_own_stops_once_when_its_terminal_hangs_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # zsh passes a hangup on to its jobs twice; that is still one stop, so runs are sealed
    hung: list[tuple[int, ...]] = []
    ended = signalled_runner(monkeypatch, tmp_path, [signal.SIGHUP, signal.SIGHUP], {}, hung)
    assert ended == (0, True, True, False)
    assert len(hung) == 2


def test_a_runner_on_its_own_kills_now_on_a_signal_after_a_hangup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ended = signalled_runner(monkeypatch, tmp_path, [signal.SIGHUP, signal.SIGTERM], {}, [])
    assert ended == (EXIT_CANCELLED, True, False, True)


def test_a_runner_aims_ui_starts_leaves_a_hangup_to_aims_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # the hangup reaches both processes and `aims ui` passes it on as one SIGTERM
    hung: list[tuple[int, ...]] = []
    ended = signalled_runner(
        monkeypatch,
        tmp_path,
        [signal.SIGHUP, signal.SIGHUP, signal.SIGTERM],
        {UI_RUNNER: "1"},
        hung,
    )
    assert ended == (0, True, True, False)
    assert len(hung) == 2


@pytest.mark.parametrize(
    "signals",
    [
        [GONE],
        # `aims ui` was killed while it waited for the runner to stop: still one stop
        [signal.SIGINT, signal.SIGTERM, GONE],
    ],
)
def test_a_runner_aims_ui_starts_stops_once_when_aims_ui_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, signals: list[signal.Signals | str]
) -> None:
    said: list[str] = []
    ended = signalled_runner(monkeypatch, tmp_path, signals, {UI_RUNNER: "1"}, said=said)
    assert ended == (0, True, True, False)
    assert ("aims ui ended" in said[0]) == (signals == [GONE])


def test_a_runner_aims_ui_starts_kills_now_on_a_signal_after_aims_ui_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ended = signalled_runner(monkeypatch, tmp_path, [GONE, signal.SIGTERM], {UI_RUNNER: "1"})
    assert ended == (EXIT_CANCELLED, True, False, True)


def test_a_runner_on_its_own_does_not_stop_when_its_stdin_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `true | aims runner` gives it a stdin pipe that ends at once
    said: list[str] = []
    ended = signalled_runner(monkeypatch, tmp_path, [GONE, signal.SIGTERM], {}, said=said)
    assert ended == (0, True, True, False)
    assert "aims ui ended" not in said[0]


async def test_aims_ui_tells_its_runner_to_leave_ctrl_c_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def spawn(*args: str, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(), 7)
    assert seen["env"][UI_RUNNER] == "1"
    assert seen["stdin"] == 7
    assert seen["env"]["AIMS_TOKEN"] == "t"


# A stand-in for the runner `aims ui` starts: it logs each SIGTERM, stops in `argv[2]` seconds
# after the first and exits 130 at once on the second, as the runner kills now.
FAKE_RUNNER = """
import os, signal, sys, time
log, slow = sys.argv[1], float(sys.argv[2])
signal.signal(signal.SIGINT, signal.SIG_IGN)
terms = []
def on_term(*_):
    terms.append(1)
    with open(log, "a") as f:
        f.write("TERM\\n")
    if len(terms) > 1:
        os._exit(130)
signal.signal(signal.SIGTERM, on_term)
with open(log, "a") as f:
    f.write("ready\\n")
while not terms:
    time.sleep(0.01)
end = time.monotonic() + slow
while time.monotonic() < end:
    time.sleep(0.01)
"""


async def until(test: Callable[[], bool], within: float) -> None:
    async with asyncio.timeout(within):
        while True:
            if test():
                return
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(("presses", "code"), [(1, 0), (2, EXIT_CANCELLED)])
async def test_aims_ui_passes_each_ctrl_c_on_to_its_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    presses: int,
    code: int,
) -> None:
    log = tmp_path / "signals"
    log.touch()
    slow = 0.5 if presses == 1 else 30.0
    dispatcher_ended = asyncio.Event()

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", FAKE_RUNNER, str(log), str(slow), stdin=stdin
        )

    async def dispatcher(state: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            dispatcher_ended.set()

    monkeypatch.setattr("aims.server.serve._spawn_runner", spawn)
    monkeypatch.setattr("aims.server.dispatch.run", dispatcher)
    before = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(
        serve(UiOptions(port=0, data=tmp_path / "server", open_browser=False))
    )
    try:
        # the server takes Ctrl-C once its handler replaces the one before
        await until(
            lambda: signal.getsignal(signal.SIGINT) is not before and "ready" in log.read_text(),
            30,
        )
        for press in range(1, presses + 1):
            os.kill(os.getpid(), signal.SIGINT)
            await until(lambda n=press: log.read_text().count("TERM") >= n, 5)
        async with asyncio.timeout(10):
            assert await task == 0
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert log.read_text().count("TERM") == presses
    # the app shuts down too, or asyncio.run cancels its lifespan at exit and logs a traceback
    assert dispatcher_ended.is_set()
    err = capsys.readouterr().err
    assert "stopping the local runner" in err
    assert ("the local runner exited with code 130" in err) == (code != 0)


async def test_aims_ui_stops_its_runner_once_when_its_terminal_hangs_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "signals"
    log.touch()
    hung: list[tuple[int, ...]] = []

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin
        )

    monkeypatch.setattr("aims.server.serve._spawn_runner", spawn)
    monkeypatch.setattr("aims.terminal.hang_up", hung.append)

    def guard(*_: object) -> None:
        """A SIGHUP the server does not handle would end this test process."""

    before = signal.signal(signal.SIGHUP, guard)
    task = asyncio.create_task(
        serve(UiOptions(port=0, data=tmp_path / "server", open_browser=False))
    )
    try:
        await until(
            lambda: signal.getsignal(signal.SIGHUP) is not guard and "ready" in log.read_text(),
            30,
        )
        # zsh passes a hangup on to its jobs twice
        for _ in range(2):
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.01)
        async with asyncio.timeout(10):
            assert await task == 0
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        signal.signal(signal.SIGHUP, before)
    assert log.read_text().count("TERM") == 1
    assert len(hung) == 2
    err = capsys.readouterr().err
    assert "stopping the local runner" in err
    assert "the local runner exited" not in err


async def test_a_ctrl_c_while_the_browser_opens_stops_the_runner_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "signals"
    log.touch()
    opening = threading.Event()
    opened = threading.Event()
    dispatcher_ended = asyncio.Event()

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin
        )

    def slow_browser(folder: Path, url: str) -> None:
        opening.set()
        opened.wait(30)

    async def dispatcher(state: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            dispatcher_ended.set()

    monkeypatch.setattr("aims.server.serve._spawn_runner", spawn)
    monkeypatch.setattr("aims.server.serve.open_link", slow_browser)
    monkeypatch.setattr("aims.server.dispatch.run", dispatcher)
    task = asyncio.create_task(serve(UiOptions(port=0, data=tmp_path / "server")))
    loop = asyncio.get_running_loop()

    def interrupted(*_: object) -> None:
        """What asyncio.run does with a Ctrl-C that no handler of the server takes."""
        loop.call_soon_threadsafe(task.cancel)

    before = signal.signal(signal.SIGINT, interrupted)
    try:
        await until(lambda: opening.is_set() and "ready" in log.read_text(), 30)
        os.kill(os.getpid(), signal.SIGINT)
        await until(lambda: "TERM" in log.read_text() or task.done(), 5)
        opened.set()
        await asyncio.wait([task], timeout=10)
    finally:
        opened.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        signal.signal(signal.SIGINT, before)
    err = capsys.readouterr().err
    assert "killed by SIGKILL" not in err
    assert log.read_text().count("TERM") == 1
    assert "stopping the local runner" in err
    assert dispatcher_ended.is_set()
    assert not task.cancelled()
    assert task.result() == 0


@pytest.mark.parametrize("fails", ["server", "browser"])
async def test_aims_ui_stops_its_runner_gracefully_when_it_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fails: str,
) -> None:
    log = tmp_path / "signals"
    log.touch()
    dispatcher_ended = asyncio.Event()

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin
        )

    async def broken_server(self: Any, sockets: object = None) -> None:
        self.servers = []
        self.started = True
        await until(lambda: "ready" in log.read_text(), 30)
        raise RuntimeError("the server broke")

    def broken_browser(folder: Path, url: str) -> None:
        end = time.monotonic() + 30
        while "ready" not in log.read_text() and time.monotonic() < end:
            time.sleep(0.02)
        raise OSError(28, "No space left on device")

    async def dispatcher(state: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            dispatcher_ended.set()

    monkeypatch.setattr("aims.server.serve._spawn_runner", spawn)
    monkeypatch.setattr("aims.server.dispatch.run", dispatcher)
    if fails == "server":
        monkeypatch.setattr("aims.server.serve._Server.serve", broken_server)
    else:
        monkeypatch.setattr("aims.server.serve.open_link", broken_browser)
    task = asyncio.create_task(
        serve(UiOptions(port=0, data=tmp_path / "server", open_browser=fails == "browser"))
    )
    try:
        await asyncio.wait([task], timeout=40)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert isinstance(task.exception(), RuntimeError if fails == "server" else OSError)
    err = capsys.readouterr().err
    assert "killed by SIGKILL" not in err
    assert log.read_text().count("TERM") == 1
    assert "stopping the local runner" in err
    # a server that failed has nothing left to shut down; one that runs must end its app
    assert dispatcher_ended.is_set() == (fails == "browser")


async def test_aims_ui_hands_its_audit_mode_to_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv: list[str] = []

    async def spawn(*args: str, **kwargs: Any) -> None:
        argv.extend(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(audit="snapshot"), 0)
    assert argv[argv.index("--audit") + 1] == "snapshot"


class NoTracer:
    tracer = None
    tracer_restarts = 0

    async def restart_tracer(self) -> None:
        raise AssertionError("a snapshot runner never starts the tracer")


async def test_a_snapshot_runner_never_starts_the_tracer(tmp_path: Path) -> None:
    node = Node(NodeOptions(url="http://x", token="t", name="n", data=tmp_path, audit="snapshot"))
    node.backend = cast(Any, NoTracer())
    health = node.tracer()
    assert not health.alive
    assert health.detail is not None
    assert "--audit snapshot" in health.detail
    async with asyncio.timeout(1):
        await node._watch_tracer()


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


def test_a_token_file_is_kept_while_it_holds_the_live_token(tmp_path: Path) -> None:
    runner = CliRunner()
    file = tmp_path / "token" / "runner"
    file.parent.mkdir()
    args = ["token", "create", "compose", "--role", "runner", "--data-dir", str(tmp_path)]
    args += ["--file", str(file)]

    def made() -> str:
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
        assert "aims_" not in r.output
        return file.read_text().strip()

    first = made()
    assert first.startswith("aims_")
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert made() == first
    assert list(file.parent.iterdir()) == [file]
    file.unlink()
    second = made()
    assert second != first
    file.write_text("aims_not-a-token\n")
    third = made()
    assert third != second
    assert (
        runner.invoke(app, ["token", "revoke", "compose", "--data-dir", str(tmp_path)]).exit_code
        == 0
    )
    assert made() != third
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert re.search(r"^compose  runner  .*  live$", listed.output, re.M)
    assert runner.invoke(app, [*args[:3], "--role", "viewer", *args[5:]]).exit_code == 0
    assert re.search(
        r"^compose  viewer ",
        runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)]).output,
        re.M,
    )


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
    tmp_path: Path, *args: str, session: bool = False
) -> AsyncGenerator[tuple[asyncio.subprocess.Process, str]]:
    """`aims ui` with `args`; `session` puts it in a session of its own, as a terminal job."""
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
        start_new_session=session,
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


async def test_aims_ui_links_to_its_public_url_and_answers_its_host(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner", "--public-url", "aims.example:8443") as (
        proc,
        first,
    ):
        assert first.startswith("https://aims.example:8443/#code=")
        local = json.loads((tmp_path / "server" / "server.json").read_text())
        assert local["url"].startswith("http://127.0.0.1:")
        assert local["public"] == "https://aims.example:8443"
        async with httpx2.AsyncClient(base_url=local["url"], trust_env=False) as c:
            assert (await c.get("/", headers={"Host": "aims.example:8443"})).status_code == 200
            assert (await c.get("/", headers={"Host": "other.example"})).status_code == 400
            site = {"Host": "aims.example:8443", "Origin": "https://aims.example:8443"}
            r = await c.post("/api/session", json={"token": local["operator"]}, headers=site)
            assert r.status_code == 204
            cookie = r.headers["set-cookie"]
            assert cookie.startswith("__Host-")
            assert "secure" in cookie.lower()
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
        assert out.decode().startswith("https://aims.example:8443/#code=")
        proc.send_signal(signal.SIGTERM)
        await proc.wait()


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
async def test_a_ctrl_c_in_the_terminal_stops_the_local_runner_once(tmp_path: Path) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    async with ui_process(tmp_path, "--audit", "snapshot", session=True) as (proc, url):
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
        # what a terminal does on Ctrl-C: SIGINT to every process of the job
        os.killpg(proc.pid, signal.SIGINT)
        assert proc.stderr is not None
        async with asyncio.timeout(120):
            err = (await proc.stderr.read()).decode()
            assert await proc.wait() == 0
    assert err.count("stopping: runs are sealed as cancelled; again to kill now") == 1
    assert "killing now" not in err
    assert "the local runner exited" not in err
    assert "the local runner was killed" not in err


async def helpers_of(pid: int) -> list[str]:
    """The containers AIMS process `pid` made on this host."""
    docker = aiodocker.Docker()
    try:
        found = await docker.containers.list(
            all=True,
            filters={"label": [f"aims.pid={pid}", f"aims.host={socket.gethostname()}"]},
        )
        return [c["Names"][0].lstrip("/") for c in found]
    finally:
        await docker.close()


@pytest.mark.docker
async def test_closing_the_terminal_stops_aims_ui_and_its_runner_cleanly(
    tmp_path: Path,
) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "xdg")}
    env.pop("AIMS_TOKEN", None)
    main, tty = os.openpty()
    os.set_blocking(main, False)
    out = bytearray()
    loop = asyncio.get_running_loop()

    def drain() -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            out.extend(os.read(main, 65536))

    loop.add_reader(main, drain)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aims",
        "ui",
        "--port",
        "0",
        "--no-open",
        "--audit",
        "snapshot",
        "--data-dir",
        str(tmp_path / "server"),
        stdin=tty,
        stdout=tty,
        stderr=tty,
        env=env,
        cwd=tmp_path,
        start_new_session=True,
    )
    os.close(tty)
    runner = 0
    try:
        info = tmp_path / "server" / "server.json"
        await until(info.exists, 60)
        server = json.loads(info.read_text())
        async with httpx2.AsyncClient(
            base_url=server["url"],
            headers={"Authorization": f"Bearer {server['operator']}"},
            trust_env=False,
        ) as c:
            async with asyncio.timeout(120):
                while True:
                    runners = (await c.get("/api/runners")).json()
                    if runners and runners[0]["connected"]:
                        break
                    await asyncio.sleep(0.2)
        children = await asyncio.create_subprocess_exec(
            "pgrep", "-P", str(proc.pid), stdout=asyncio.subprocess.PIPE
        )
        runner = int((await children.communicate())[0].split()[0])
        helpers = await helpers_of(runner)
        assert helpers, out.decode(errors="replace")
        # what closing the window does: the terminal hangs up, so writes to it fail. The shell
        # passes SIGHUP on to the job, zsh twice.
        loop.remove_reader(main)
        os.close(main)
        main = -1
        os.killpg(proc.pid, signal.SIGHUP)
        await asyncio.sleep(0.001)
        # macOS says EPERM for a group whose processes all died
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGHUP)
        async with asyncio.timeout(120):
            assert await proc.wait() == 0
        assert await helpers_of(runner) == []
    finally:
        if main >= 0:
            loop.remove_reader(main)
            os.close(main)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if runner:
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner, signal.SIGKILL)
            for name in await helpers_of(runner):
                rm = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", "-v", name, stdout=asyncio.subprocess.DEVNULL
                )
                await rm.wait()


@pytest.mark.docker
async def test_a_killed_aims_ui_leaves_no_runner_behind(tmp_path: Path) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    runner = 0
    try:
        async with ui_process(tmp_path, "--audit", "snapshot") as (proc, url):
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
            children = await asyncio.create_subprocess_exec(
                "pgrep", "-P", str(proc.pid), stdout=asyncio.subprocess.PIPE
            )
            runner = int((await children.communicate())[0].split()[0])
            assert await helpers_of(runner)
            proc.kill()
            await proc.wait()
            # init or launchd takes the runner over and reaps it once it ends
            await until(lambda: not alive(runner), 60)
            assert await helpers_of(runner) == []
            assert proc.stderr is not None
            async with asyncio.timeout(10):
                err = (await proc.stderr.read()).decode()
        assert "aims ui ended: stopping, runs are sealed as cancelled" in err
    finally:
        if runner:
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner, signal.SIGKILL)
            for name in await helpers_of(runner):
                rm = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", "-v", name, stdout=asyncio.subprocess.DEVNULL
                )
                await rm.wait()


@pytest.mark.docker
async def test_aims_ui_with_snapshot_audit_brings_a_runner_without_the_tracer(
    tmp_path: Path,
) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    async with ui_process(tmp_path, "--audit", "snapshot") as (proc, url):
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
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(120):
            assert await proc.wait() == 0
    tracer = runners[0]["tracer"]
    assert not tracer["alive"]
    assert "--audit snapshot" in tracer["detail"]


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
