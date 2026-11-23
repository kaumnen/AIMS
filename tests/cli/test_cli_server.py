import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Sequence
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, NoReturn, cast
from urllib.parse import urlsplit

import aiodocker
import httpx2
import pytest
from children import create_child
from cp import CP, control_plane
from proxy import Server, environ, serving
from test_cli_monitor import NoDocker
from typer.testing import CliRunner

from aims import cli
from aims.cli import EXIT_CANCELLED, app
from aims.config import MAX_KEEP_DAYS
from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall
from aims.net import Outbound
from aims.node import service
from aims.node.link import Link
from aims.node.service import Node, NodeOptions, Refused
from aims.node.shipper import WARN_AFTER, Backoff
from aims.report.escape import visible
from aims.sandbox import DockerBackend, SandboxSpec
from aims.server import serve as serve_module
from aims.server.db import ServerDB
from aims.server.serve import (
    IN_USE_HINT,
    KILL_NOW,
    UI_RUNNER,
    ServeError,
    UiOptions,
    _redirect,
    _spawn_runner,
    check,
    connect_host,
    hold,
    listen,
    open_link,
    open_sockets,
    public_url,
    serve,
    server_dir,
    snap_browser,
)
from aims.terminal import AGAIN

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).parents[2]


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


def test_ui_refuses_runner_options_without_its_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served: list[UiOptions] = []

    async def serve(o: UiOptions) -> int:
        served.append(o)
        return 0

    monkeypatch.setattr("aims.server.serve.serve", serve)
    base = ["ui", "--no-open", "--data-dir", str(tmp_path)]
    for extra, hint in ((["--slots", "2"], "--slots"), (["--audit", "auto"], "--audit")):
        r = CliRunner().invoke(app, [*base, "--no-runner", *extra])
        assert r.exit_code == 2, r.output
        assert hint in r.output
        assert "needs the local runner" in " ".join(r.output.split())
    assert served == []
    for args in ([*base, "--no-runner"], [*base, "--slots", "3", "--audit", "snapshot"]):
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 0, r.output
    assert [(o.runner, o.slots, o.audit) for o in served] == [
        (False, 2, "auto"),
        (True, 3, "snapshot"),
    ]


@pytest.mark.parametrize("value", ["nan", "inf", "NaN", "Infinity", "1e400"])
def test_a_float_option_needs_a_finite_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    started: list[object] = []

    async def serve(o: UiOptions) -> int:
        started.append(o)
        return 0

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            started.append(options)

        async def run(self, stop: asyncio.Event) -> None:
            pass

    monkeypatch.setattr("aims.server.serve.serve", serve)
    monkeypatch.setattr("aims.node.service.Node", Stub)
    data = ["--data-dir", str(tmp_path / "data")]
    nowhere = ["--server", "http://127.0.0.1:9"]
    for args in (
        ["images", "prune", "--dry-run", "--days"],
        ["ui", "--no-open", "--no-runner", *data, "--keep-days"],
        ["runner", *nowhere, *data, "--keep-days"],
        ["monitor", "r1", "--runs-dir", str(tmp_path / "runs"), "--budget-usd"],
        ["control", "r1", "pause", *nowhere, "--wait"],
    ):
        r = CliRunner().invoke(app, [*args, value], env={"AIMS_TOKEN": "x"})
        assert (r.exit_code, type(r.exception)) == (2, SystemExit), (args, r.output)
        assert args[-1] in r.output
        # Since typer 0.29 its own min=0 check refuses nan before the callback sees it.
        assert "give a finite number" in r.output or (
            value.lower() == "nan" and "greater than or equal to 0" in r.output
        )
    assert started == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", [str(MAX_KEEP_DAYS + 1), "1e300"])
def test_a_keep_days_option_is_at_most_a_century(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    """Housekeeping turns the days into the date it deletes before; past about a century that
    date leaves what a timestamp holds."""
    started: list[object] = []

    async def serve(o: UiOptions) -> int:
        started.append(o)
        return 0

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            started.append(options)

        async def run(self, stop: asyncio.Event) -> None:
            pass

    monkeypatch.setattr("aims.server.serve.serve", serve)
    monkeypatch.setattr("aims.node.service.Node", Stub)
    data = ["--data-dir", str(tmp_path / "data")]
    for args in (
        ["images", "prune", "--dry-run", "--days"],
        ["ui", "--no-open", "--no-runner", *data, "--keep-days"],
        ["runner", "--server", "http://127.0.0.1:9", *data, "--keep-days"],
    ):
        r = CliRunner().invoke(app, [*args, value], env={"AIMS_TOKEN": "x"})
        assert (r.exit_code, type(r.exception)) == (2, SystemExit), (args, r.output)
        assert f"give at most {MAX_KEEP_DAYS} days" in r.output
    r = CliRunner().invoke(app, ["ui", "--no-open", "--no-runner", *data, "--keep-days", "36500"])
    assert r.exit_code == 0, r.output
    assert [getattr(o, "keep_days", None) for o in started] == [MAX_KEEP_DAYS]


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


def test_a_runner_the_control_plane_refused_exits_1_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            raise Refused("the control plane at http://cp refused this runner: 401 \x1b[2Jno")

    monkeypatch.setattr("aims.node.service.Node", Stub)
    args = ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)]
    result = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "x"})
    assert result.exit_code == 1
    assert "error: the control plane at http://cp refused this runner: 401" in result.output
    assert "\x1b" not in result.output


def test_the_runner_reads_how_long_unused_scenario_images_stay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[NodeOptions] = []

    class Stub:
        def __init__(self, options: NodeOptions) -> None:
            seen.append(options)

        async def run(self, stop: asyncio.Event) -> None:
            pass

    monkeypatch.setattr("aims.node.service.Node", Stub)
    project = tmp_path / "aims.toml"
    args = ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)]
    args += ["--config", str(project)]
    for text, days in (("", 30.0), ("[images]\nkeep_days = 7\n", 7.0)):
        project.write_text(text)
        result = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "x"})
        assert result.exit_code == 0, result.output
        assert seen.pop().image_keep_days == days
    project.write_text("[images]\nkeep_days = -1\n")
    result = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "x"})
    assert result.exit_code == 1
    assert "[images] keep_days must be a number of days" in result.output
    assert seen == []


# In `signalled_runner`: `aims ui` ends, so the pipe on the runner's stdin ends.
GONE = "gone"
# In `signalled_runner`: `aims ui` says kill now on the runner's stdin, as on its second stop.
KILL = "kill"


def signalled_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signals: Sequence[signal.Signals | str],
    env: dict[str, str],
    hung: list[tuple[int, ...]] | None = None,
    said: list[str] | None = None,
) -> tuple[int, bool, bool, bool]:
    """Run `aims runner` on a stub node that sends its thread `signals`, 10 ms apart; GONE ends
    the pipe on its stdin, which this test holds as `aims ui` does, and KILL writes KILL_NOW on
    it. Returns the exit code,
    whether the node was asked to stop, whether it ran to its end (a kill now cancels it) and
    whether the runner said it kills now. Each hangup that would point the streams at
    /dev/null goes to `hung` instead. The output goes to `said`."""
    seen: dict[str, bool] = {"done": False}
    read_end, write_end = os.pipe()
    held = [write_end]

    class Stub:
        closing = False

        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            try:
                for sig in signals:
                    if sig == KILL:
                        os.write(held[0], KILL_NOW)
                    elif isinstance(sig, str):
                        os.close(held.pop())
                    else:
                        # to this thread, which runs the loop: another thread of the test
                        # process could take a signal this one blocks
                        signal.raise_signal(sig)
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


@pytest.mark.parametrize("when", ["after a stop", "after a kill", "after a kill as it starts"])
def test_a_stop_while_the_runner_closes_leaves_the_close_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quits: list[bool], when: str
) -> None:
    """A stop once the runner closes its stores cancels nothing: after a first stop, after a
    second that killed now, or after a second that killed it as it started. As the third stop
    or a later one it quits the process at once, after the second said so."""
    cut: list[str] = []
    start, close = Link.start, LocalEventStore.close

    class Docked(Node):
        def __init__(self, options: NodeOptions) -> None:
            super().__init__(replace(options, backend=cast(Any, NoDocker())))

    async def started(self: Link) -> None:
        await start(self)
        os.kill(os.getpid(), signal.SIGINT)

    async def stop_runs(self: Node) -> None:
        # the second stop: kill now
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(5)
        raise AssertionError("the second stop did not kill")

    async def sealing(store: LocalEventStore) -> list[str]:
        for _ in range(2):
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.05)
        await asyncio.sleep(5)
        raise AssertionError("the second stop did not kill")

    async def closing(self: LocalEventStore) -> None:
        """The close with a stop sent as it starts, which must leave it alone."""
        try:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.2)
        except BaseException as e:
            cut.append(type(e).__name__)
        await close(self)

    monkeypatch.setattr("aims.node.service.Node", Docked)
    monkeypatch.setattr(Link, "start", started)
    if when == "after a kill":
        monkeypatch.setattr(Node, "_stop", stop_runs)
    elif when == "after a kill as it starts":
        monkeypatch.setattr("aims.node.service.seal_unsealed", sealing)
    monkeypatch.setattr(LocalEventStore, "close", closing)
    args = ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)]
    result = CliRunner().invoke(app, args, env={"AIMS_TOKEN": "x"})
    monkeypatch.undo()
    killed = when != "after a stop"
    assert cut == []
    assert quits
    quits.clear()
    assert AGAIN in result.output
    assert result.exit_code == (EXIT_CANCELLED if killed else 0), result.output
    assert ("killing now" in result.output) == killed


def test_a_runner_aims_ui_starts_stops_once_for_a_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ctrl-C in the terminal reaches the whole process group: the runner gets SIGINT and
    # `aims ui` sends it SIGTERM. That is one Ctrl-C, so the runner seals its runs.
    ended = signalled_runner(
        monkeypatch, tmp_path, [signal.SIGINT, signal.SIGTERM], {UI_RUNNER: "1"}
    )
    assert ended == (0, True, True, False)


def test_a_runner_aims_ui_starts_takes_its_signals_once_it_handles_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`aims ui` starts its runner with these signals blocked. Once its handlers are in, the
    runner unblocks them and stops once for a Ctrl-C."""
    held = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        ended = signalled_runner(
            monkeypatch, tmp_path, [signal.SIGINT, signal.SIGTERM], {UI_RUNNER: "1"}
        )
    finally:
        # Signals still blocked here would reach this test process once unblocked
        handlers = [signal.signal(sig, signal.SIG_IGN) for sig in held]
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
        for sig, handler in zip(held, handlers, strict=True):
            signal.signal(sig, handler)
    assert ended == (0, True, True, False)


def test_a_runner_aims_ui_starts_kills_now_on_a_second_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `aims ui` passes the first Ctrl-C on as SIGTERM and the second on stdin
    ended = signalled_runner(
        monkeypatch,
        tmp_path,
        [signal.SIGINT, signal.SIGTERM, signal.SIGINT, KILL],
        {UI_RUNNER: "1"},
    )
    assert ended == (EXIT_CANCELLED, True, False, True)


@pytest.mark.parametrize("times", [2, 3])
def test_a_runner_aims_ui_starts_stops_once_for_one_stop_sent_to_the_whole_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, times: int
) -> None:
    """systemd, or `kill -- -<pgid>`, sends one SIGTERM to every process of the job. The runner
    gets it on its own and through `aims ui`, three times under `uv run`. That is one stop, so
    the runner seals its runs. While `aims ui` lives, only `aims ui` says kill now."""
    ended = signalled_runner(monkeypatch, tmp_path, [signal.SIGTERM] * times, {UI_RUNNER: "1"})
    assert ended == (0, True, True, False)


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


def test_a_refused_runner_aims_ui_starts_says_only_the_refusal_when_aims_ui_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control plane refused the runner, which stops as for a first Ctrl-C. `aims ui` ends
    while it closes. That is not why it stopped, so the runner does not say it is."""
    read_end, write_end = os.pipe()
    held = [write_end]
    real = os.read
    seen: list[bytes] = []

    def read(fd: int, n: int) -> bytes:
        data = real(fd, n)
        if fd == 0:
            seen.append(data)
        return data

    class Stub:
        closing = False

        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            # as `on_refused` in Node.run does
            stop.set()
            os.close(held.pop())
            async with asyncio.timeout(5):
                while b"" not in seen:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            raise Refused("the control plane at http://cp refused this runner 5 times in a row")

    monkeypatch.setattr("aims.node.service.Node", Stub)
    monkeypatch.setattr(os, "read", read)
    # a runner of `aims ui` ignores SIGTERM and SIGHUP as it ends; a child of a later test
    # would inherit that
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = [signal.getsignal(sig) for sig in signals]
    stdin = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        result = CliRunner().invoke(
            app,
            ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(tmp_path)],
            env={"AIMS_TOKEN": "x", UI_RUNNER: "1"},
        )
    finally:
        for sig, handler in zip(signals, previous, strict=True):
            signal.signal(sig, handler)
        os.dup2(stdin, 0)
        os.close(stdin)
        for fd in held:
            os.close(fd)
    monkeypatch.undo()
    assert result.exit_code == 1, result.output
    assert "error: the control plane at http://cp refused this runner" in result.output
    assert "aims ui ended" not in result.output


def test_aims_ui_with_a_data_dir_keeps_its_runner_inside_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served: list[UiOptions] = []

    async def serve(o: UiOptions) -> int:
        served.append(o)
        return 0

    monkeypatch.setattr("aims.server.serve.serve", serve)
    for args in (["ui", "--no-open"], ["ui", "--no-open", "--data-dir", str(tmp_path)]):
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 0, r.output
    assert [o.runner_data for o in served] == [None, tmp_path / "runner"]


def test_aims_ui_ignores_a_stop_once_its_own_stop_is_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve() takes its handlers out as it returns. A late signal as asyncio closes, or as
    Python exits, would end the process with that signal in place of the exit code."""
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    held: list[AsyncIterator[None]] = []
    seen: list[list[object]] = []

    async def closed_by_asyncio() -> AsyncIterator[None]:
        try:
            yield
        finally:
            seen.append([signal.getsignal(sig) for sig in signals])

    async def serve(o: UiOptions) -> int:
        held.append(closed_by_asyncio())
        await anext(held[0])
        return 0

    monkeypatch.setattr("aims.server.serve.serve", serve)
    r = CliRunner().invoke(app, ["ui", "--no-open"])
    assert r.exit_code == 0, r.output
    assert seen == [[signal.SIG_IGN] * 3]
    assert [signal.getsignal(sig) for sig in signals] == [signal.SIG_IGN] * 3


async def test_aims_ui_hands_its_runner_the_folder_for_its_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv: list[str] = []

    async def spawn(*args: str, **kwargs: Any) -> None:
        argv.extend(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(), 7)
    assert "--data-dir" not in argv
    argv.clear()
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(runner_data=tmp_path / "runner"), 7)
    assert argv[argv.index("--data-dir") + 1] == str(tmp_path / "runner")


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


async def test_a_stop_in_the_start_up_of_the_runner_aims_ui_starts_ends_it_cleanly(
    tmp_path: Path,
) -> None:
    """Python, its imports and the runner's own start-up come before its handlers. A signal
    sent then waits for them, and a stop that early ends the runner before it starts anything."""
    # A runner that does start fails as it makes its data folder, before it reaches Docker
    blocker = tmp_path / "file"
    blocker.touch()
    options = UiOptions(runner_data=blocker / "runner")
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    read_end, write_end = os.pipe()
    proc = None
    try:
        try:
            proc = await _spawn_runner("http://127.0.0.1:9", "t", options, read_end)
        finally:
            os.close(read_end)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before
        # a Ctrl-C as `aims ui` starts: SIGINT from the terminal, SIGTERM from `aims ui`
        proc.send_signal(signal.SIGINT)
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(60):
            assert await proc.wait() == 0
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        os.close(write_end)


async def test_the_ui_runner_reaches_its_ui_on_any_address_without_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def spawn(*args: str, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with serving(Server()) as proxy:
        environ(monkeypatch, HTTP_PROXY=proxy.url, no_proxy="example.org")
        await _spawn_runner("http://ui.invalid:8484", "t", UiOptions(), 7)
        with monkeypatch.context() as m:
            m.setattr(os, "environ", seen["env"])
            outbound = Outbound()
        async with httpx2.AsyncClient(transport=outbound) as client:
            with pytest.raises(httpx2.ConnectError):
                await client.get("http://ui.invalid:8484/")
            await client.get("http://other.invalid/")
    assert proxy.lines == ["GET http://other.invalid/ HTTP/1.1"]
    assert seen["env"]["NO_PROXY"] == seen["env"]["no_proxy"] == "example.org,ui.invalid"
    await _spawn_runner("https://[fd00::1]:8484", "t", UiOptions(), 7)
    assert seen["env"]["NO_PROXY"] == "example.org,fd00::1"


# A stand-in for the runner `aims ui` starts: it logs each SIGTERM and stops in `argv[2]`
# seconds after the first. On KILL_NOW from stdin it logs KILL and exits 130 at once, as the
# runner kills now.
FAKE_RUNNER = """
import os, signal, sys, threading, time
log, slow = sys.argv[1], float(sys.argv[2])
signal.signal(signal.SIGINT, signal.SIG_IGN)
terms = []
def on_term(*_):
    terms.append(1)
    with open(log, "a") as f:
        f.write("TERM\\n")
signal.signal(signal.SIGTERM, on_term)
def on_input():
    while data := os.read(0, 4096):
        if b"k" in data:
            with open(log, "a") as f:
                f.write("KILL\\n")
            os._exit(130)
threading.Thread(target=on_input, daemon=True).start()
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
        return await create_child(
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
        os.kill(os.getpid(), signal.SIGINT)
        await until(lambda: "TERM" in log.read_text(), 5)
        if presses == 2:
            os.kill(os.getpid(), signal.SIGINT)
            await until(lambda: "KILL" in log.read_text(), 5)
        async with asyncio.timeout(10):
            assert await task == 0
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    # the first Ctrl-C goes on as SIGTERM, the second as KILL_NOW on the runner's stdin
    assert log.read_text().split() == ["ready", "TERM", "KILL"][: presses + 1]
    # the app shuts down too, or asyncio.run cancels its lifespan at exit and logs a traceback
    assert dispatcher_ended.is_set()
    err = capsys.readouterr().err
    assert "stopping the local runner" in err
    assert ("the local runner exited with code 130" in err) == (code != 0)


@pytest.mark.parametrize("runner", [False, True])
async def test_a_ctrl_c_while_aims_ui_closes_leaves_the_close_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: bool
) -> None:
    log = tmp_path / "signals"
    log.touch()
    cut: list[str] = []
    real = serve_module.close_state

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await create_child(sys.executable, "-c", FAKE_RUNNER, str(log), "0.1", stdin=stdin)

    async def closing(state: Any) -> None:
        """The close with a Ctrl-C sent as it starts, which must leave it alone."""
        try:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.2)
        except BaseException as e:
            cut.append(type(e).__name__)
        await real(state)

    monkeypatch.setattr("aims.server.serve._spawn_runner", spawn)
    monkeypatch.setattr("aims.server.serve.close_state", closing)
    before = signal.getsignal(signal.SIGINT)
    options = UiOptions(port=0, data=tmp_path / "server", runner=runner, open_browser=False)
    task = asyncio.create_task(serve(options))
    try:
        await until(
            lambda: (
                signal.getsignal(signal.SIGINT) is not before
                and (not runner or "ready" in log.read_text())
            ),
            30,
        )
        os.kill(os.getpid(), signal.SIGINT)
        async with asyncio.timeout(10):
            assert await task == 0
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert cut == []
    assert log.read_text().count("TERM") == (1 if runner else 0)


async def test_aims_ui_stops_its_runner_once_when_its_terminal_hangs_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "signals"
    log.touch()
    hung: list[tuple[int, ...]] = []

    async def spawn(url: str, token: str, o: UiOptions, stdin: int) -> asyncio.subprocess.Process:
        return await create_child(sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin)

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
        return await create_child(sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin)

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
        return await create_child(sys.executable, "-c", FAKE_RUNNER, str(log), "0.5", stdin=stdin)

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
    assert "--config" not in argv
    argv.clear()
    project = Path("/srv/aims.toml")
    await _spawn_runner("http://127.0.0.1:9", "t", UiOptions(project=project), 0)
    assert argv[argv.index("--config") + 1] == str(project)


class NoTracer:
    tracer = None
    tracer_restarts = 0
    unsupported = None

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


async def test_a_runner_whose_host_cannot_run_the_tracer_says_why_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unfit(NoTracer):
        unsupported = "needs Linux 6.11 or newer; the Docker host runs 6.8.0-147-generic"

        async def restart_tracer(self) -> None:
            raise AssertionError("a host that cannot run the tracer is not tried again")

    monkeypatch.setattr(service, "TRACER_POLL_S", 0.01)
    monkeypatch.setattr(service, "TRACER_RETRY_S", 0.0)
    node = Node(NodeOptions(url="http://x", token="t", name="n", data=tmp_path))
    node.backend = cast(Any, Unfit())
    health = node.tracer()
    assert not health.alive
    assert health.detail == (
        "kernel audit unavailable: needs Linux 6.11 or newer; the Docker host runs"
        " 6.8.0-147-generic; this runner takes no kernel runs or self-tests"
    )
    async with asyncio.timeout(1):
        await node._watch_tracer()


async def test_the_tracer_watch_goes_on_after_a_restart_fails_in_any_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As one whose build lock file this user may not open did."""

    class Failing:
        tracer = None
        tracer_restarts = 0
        unsupported = None

        def __init__(self) -> None:
            self.tries = 0
            self.third = asyncio.Event()

        async def restart_tracer(self) -> None:
            self.tries += 1
            if self.tries == 3:
                self.third.set()
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), "/tmp/x.lock")

    monkeypatch.setattr(service, "TRACER_POLL_S", 0.01)
    monkeypatch.setattr(service, "TRACER_RETRY_S", 0.0)
    node = Node(NodeOptions(url="http://x", token="t", name="n", data=tmp_path))
    backend = Failing()
    node.backend = cast(Any, backend)
    task = asyncio.create_task(node._watch_tracer())
    third = asyncio.create_task(backend.third.wait())
    try:
        await asyncio.wait({task, third}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert not task.done(), task.exception()
        assert third.done()
    finally:
        task.cancel()
        third.cancel()
        await asyncio.gather(task, third, return_exceptions=True)


async def test_a_runner_tries_again_when_listing_what_to_ship_fails_another_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Link:
        calls = 0

        async def ship_all(self) -> None:
            self.calls += 1
            if self.calls <= WARN_AFTER:
                raise sqlite3.OperationalError("database is locked")

    async def wait(self: Backoff, retry_after: float | None = None) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(Backoff, "wait", wait)
    node = Node(NodeOptions(url="http://x", token="t", name="n", data=tmp_path, audit="snapshot"))
    link = Link()
    node._open = cast(Any, SimpleNamespace(link=link))
    with caplog.at_level(logging.WARNING, logger="aims.node"):
        async with asyncio.timeout(5):
            await node._ship_all()
    assert link.calls == WARN_AFTER + 1
    assert [r.getMessage() for r in caplog.records] == [
        f"shipping the runs kept here failed {WARN_AFTER} times in a row: database is locked",
        f"shipping the runs kept here works again after {WARN_AFTER} failures",
    ]


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
    assert re.search(r"^ci  runner  .*  live  never$", listed.output, re.M)

    async def use() -> None:
        db = await ServerDB.open(tmp_path / "server.sqlite")
        try:
            assert await db.token_for(made.output.strip()) is not None
        finally:
            await db.close()

    asyncio.run(use())
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert re.search(r"^ci  runner  .*  live  used \d{4}-\d\d-\d\d \d\d:\d\d$", listed.output, re.M)
    gone = runner.invoke(app, ["token", "revoke", "ci", "--data-dir", str(tmp_path)])
    assert gone.exit_code == 0
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert "revoked" in listed.output
    assert made.output.strip() not in (tmp_path / "server.sqlite").read_text(errors="ignore")
    trail = runner.invoke(app, ["audit", "--data-dir", str(tmp_path)])
    assert [line.split("  ")[1:5] for line in trail.output.splitlines()] == [
        ["-", "token.revoke", "ci", "ok"],
        ["-", "token.create", "ci", "refused"],
        ["-", "token.create", "ci", "ok"],
    ]


BAD_NAMES = ["a\nb\x1b[31m", "ok\n", "two words", "", "x" * 101, "é", "a/b"]


@pytest.mark.parametrize("name", BAD_NAMES, ids=repr)
def test_the_cli_refuses_a_token_name_the_api_refuses(tmp_path: Path, name: str) -> None:
    from pydantic import ValidationError

    from aims.server.api import TokenCreate

    with pytest.raises(ValidationError):
        TokenCreate(name=name, role="viewer")
    data, file = tmp_path / "data", tmp_path / "token"
    for extra in ([], ["--file", str(file)]):
        args = ["token", "create", name, "--data-dir", str(data), *extra]
        r = CliRunner().invoke(app, args)
        assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
        assert "NAME" in r.output
        assert "letters, digits" in " ".join(r.output.split())
        assert "\x1b[31m" not in r.output
    assert not data.exists()
    assert not file.exists()


@pytest.mark.parametrize("name", ["ui", "box2", "compose-runner", "a.b_c@d-e", "x" * 100])
def test_the_cli_takes_a_token_name_the_api_takes(tmp_path: Path, name: str) -> None:
    from aims.server.api import TokenCreate

    TokenCreate(name=name, role="viewer")
    r = CliRunner().invoke(app, ["token", "create", name, "--data-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output


def test_a_token_name_stored_before_the_rule_still_lists_and_revokes(tmp_path: Path) -> None:
    import sqlite3

    async def make() -> None:
        db = await ServerDB.open(tmp_path / "server.sqlite")
        try:
            for bad in BAD_NAMES:
                with pytest.raises(ValueError, match="letters, digits"):
                    await db.create_token(bad, "viewer")
                with pytest.raises(ValueError, match="letters, digits"):
                    await db.rotate_token(bad, "viewer")
        finally:
            await db.close()

    asyncio.run(make())
    with contextlib.closing(sqlite3.connect(tmp_path / "server.sqlite")) as raw, raw:
        raw.execute(
            "insert into tokens (name, sha256, role, created) values (?, ?, ?, ?)",
            ("old\nname", "0" * 64, "viewer", time.time()),
        )
    runner = CliRunner()
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert len(listed.output.splitlines()) == 1
    gone = runner.invoke(app, ["token", "revoke", "old\nname", "--data-dir", str(tmp_path)])
    assert gone.exit_code == 0, gone.output
    assert "revoked" in runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)]).output


def test_a_token_with_a_time_no_date_holds_still_lists(tmp_path: Path) -> None:
    import sqlite3

    async def make() -> None:
        await (await ServerDB.open(tmp_path / "server.sqlite")).close()

    asyncio.run(make())
    with contextlib.closing(sqlite3.connect(tmp_path / "server.sqlite")) as raw, raw:
        raw.execute(
            "insert into tokens (name, sha256, role, created, last_used) values (?, ?, ?, ?, ?)",
            ("far", "0" * 64, "viewer", 1e18, -1e18),
        )
    listed = CliRunner().invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert listed.output == "far  viewer  9999-12-31 23:59  live  used 0001-01-01 00:00\n"


@pytest.mark.parametrize(
    "args", [["token", "list"], ["token", "revoke", "ci"], ["audit"], ["audit", "--jsonl"]]
)
def test_a_command_on_the_servers_data_needs_its_database(tmp_path: Path, args: list[str]) -> None:
    missing, empty = tmp_path / "missing" / "server", tmp_path / "empty"
    empty.mkdir()
    for data, env in ((missing, {}), (empty, {}), (None, {"AIMS_SERVER_DIR": str(missing)})):
        flag = [] if data is None else ["--data-dir", str(data)]
        r = CliRunner().invoke(app, [*args, *flag], env=env)
        assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
        assert r.output.startswith("error: no control plane database")
        assert str((data or missing) / "server.sqlite") in r.output
    assert not missing.parent.exists()
    assert list(empty.iterdir()) == []
    # the first token of a new deployment makes the database, as Compose's init does
    made = CliRunner().invoke(app, ["token", "create", "ci", "--data-dir", str(missing)])
    assert made.exit_code == 0, made.output
    r = CliRunner().invoke(app, [*args, "--data-dir", str(missing)])
    assert r.exit_code == 0, r.output


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
    assert re.search(r"^compose  runner  .*  live  never$", listed.output, re.M)
    assert runner.invoke(app, [*args[:3], "--role", "viewer", *args[5:]]).exit_code == 0
    assert re.search(
        r"^compose  viewer ",
        runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)]).output,
        re.M,
    )
    trail = runner.invoke(app, ["audit", "--data-dir", str(tmp_path), "--action", "token"])
    assert [line.split("  ")[2] for line in trail.output.splitlines()] == [
        "token.rotate",
        "token.rotate",
        "token.revoke",
        "token.rotate",
        "token.rotate",
        "token.rotate",
    ]


def live_token(data: Path, value: str) -> bool:
    async def look() -> bool:
        db = await ServerDB.open(data / "server.sqlite")
        try:
            return await db.token_for(value, used=False) is not None
        finally:
            await db.close()

    return asyncio.run(look())


@pytest.mark.parametrize("fails", ["open", "file"])
def test_a_failed_token_create_leaves_no_data_folder_it_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    chmod = Path.chmod

    def no_chmod(self: Path, mode: int, **kwargs: Any) -> None:
        if self.name == "server.sqlite":
            raise PermissionError(1, "Operation not permitted")
        chmod(self, mode, **kwargs)

    def full(path: Path, data: str) -> Path:
        raise OSError(28, "No space left on device")

    data = tmp_path / "new" / "data"
    args = ["token", "create", "r1", "--role", "runner", "--data-dir", str(data)]
    if fails == "open":
        # the database file is made and filled, then the open fails
        monkeypatch.setattr(Path, "chmod", no_chmod)
    else:
        monkeypatch.setattr("aims.server.serve.private_temp", full)
        args += ["--file", str(tmp_path / "r1.token")]
    r = CliRunner().invoke(app, args)
    monkeypatch.undo()
    assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
    assert r.output.startswith("error: cannot ")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fails", ["open", "file"])
def test_a_failed_token_create_in_an_empty_data_folder_leaves_it_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    chmod = Path.chmod

    def no_chmod(self: Path, mode: int, **kwargs: Any) -> None:
        if self.name == "server.sqlite":
            raise PermissionError(1, "Operation not permitted")
        chmod(self, mode, **kwargs)

    def full(path: Path, data: str) -> Path:
        raise OSError(28, "No space left on device")

    data = tmp_path / "data"
    data.mkdir()
    args = ["token", "create", "r1", "--role", "runner", "--data-dir", str(data)]
    if fails == "open":
        monkeypatch.setattr(Path, "chmod", no_chmod)
    else:
        monkeypatch.setattr("aims.server.serve.private_temp", full)
        args += ["--file", str(tmp_path / "r1.token")]
    r = CliRunner().invoke(app, args)
    monkeypatch.undo()
    assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
    assert list(data.iterdir()) == []


def test_a_failed_token_create_leaves_a_database_another_process_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data folder is not there when the create starts. Another process, such as a first
    `aims ui`, makes it and its database before the create opens it. The open fails, and the
    database stays with what the other process put in it."""
    chmod = Path.chmod
    missing = cli._missing
    data = tmp_path / "data"

    def no_chmod(self: Path, mode: int, **kwargs: Any) -> None:
        if self.name == "server.sqlite":
            raise PermissionError(1, "Operation not permitted")
        chmod(self, mode, **kwargs)

    def raced(path: Path) -> list[Path]:
        found = missing(path)
        data.mkdir(mode=0o700)
        with contextlib.closing(sqlite3.connect(data / "server.sqlite")) as db:
            db.execute("create table theirs (x)")
            db.commit()
        return found

    monkeypatch.setattr("aims.cli._missing", raced)
    monkeypatch.setattr(Path, "chmod", no_chmod)
    r = CliRunner().invoke(app, ["token", "create", "r1", "--data-dir", str(data)])
    monkeypatch.undo()
    assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
    with contextlib.closing(sqlite3.connect(data / "server.sqlite")) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master")}
    assert "theirs" in tables


def test_a_token_file_that_cannot_be_written_keeps_the_old_value(tmp_path: Path) -> None:
    runner = CliRunner()
    data = tmp_path / "data"
    args = ["token", "create", "r1", "--role", "runner", "--data-dir", str(data), "--file"]
    held = tmp_path / "r1.token"
    assert runner.invoke(app, [*args, str(held)]).exit_code == 0
    old = held.read_text().strip()
    folder = tmp_path / "folder"
    folder.mkdir()
    for bad in (tmp_path / "missing" / "r1.token", folder):
        r = runner.invoke(app, [*args, str(bad)])
        assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
        assert r.output.startswith("error: ")
        assert len(r.output.splitlines()) == 1
    assert live_token(data, old)
    assert list(folder.iterdir()) == []
    trail = runner.invoke(app, ["audit", "--data-dir", str(data), "--action", "token"])
    assert [line.split("  ")[2] for line in trail.output.splitlines()] == ["token.rotate"]


def test_a_new_token_value_that_cannot_take_the_files_place_stays_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: Path, target: Path) -> Path:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "replace", refused)
    data = tmp_path / "data"
    args = ["token", "create", "r1", "--role", "runner", "--data-dir", str(data)]
    r = CliRunner().invoke(app, [*args, "--file", str(tmp_path / "r1.token")])
    assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
    (tmp,) = tmp_path.glob(".r1.token.*.tmp")
    assert r.output.startswith("error: ")
    assert r.output.rstrip().endswith(f"it is in {tmp}")
    assert stat.S_IMODE(tmp.stat().st_mode) == 0o600
    assert live_token(data, tmp.read_text().strip())


def test_a_link_at_the_token_files_temp_path_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("secrets.token_hex", lambda nbytes=None: "0" * 16)
    victim = tmp_path / "victim"
    victim.write_text("kept\n")
    victim.chmod(0o644)
    for name in (".r1.token.tmp", f".r1.token.{'0' * 16}.tmp"):
        (tmp_path / name).symlink_to(victim)
    data = tmp_path / "data"
    args = ["token", "create", "r1", "--role", "runner", "--data-dir", str(data)]
    r = CliRunner().invoke(app, [*args, "--file", str(tmp_path / "r1.token")])
    assert (r.exit_code, type(r.exception)) == (1, SystemExit), r.output
    assert r.output.startswith("error: ")
    assert victim.read_text() == "kept\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert not (tmp_path / "r1.token").exists()
    # no token was made, so neither was the data folder
    assert not data.exists()


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


async def test_push_goes_on_past_a_run_deleted_under_it(
    cp: CP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aims.node.client import ControlClient

    started = RunStarted(scenario="offline", oversight="observe", agents=["a"])
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        first = await store.create_run(started)
        await store.append(first, [Draft(RunFinished(status="completed"))])
        second = await store.create_run(started)
        await store.append(second, [Draft(RunFinished(status="completed"))])
    heads = ControlClient.heads

    async def deleting(self: ControlClient, run_ids: list[str]) -> Any:
        if run_ids == [first] and (tmp_path / "runs" / first).exists():
            # as another process would
            async with await LocalEventStore.open(tmp_path / "runs") as other:
                await other.remove(first)
        return await heads(self, run_ids)

    monkeypatch.setattr(ControlClient, "heads", deleting)
    env = {"AIMS_TOKEN": cp.runner}
    async with asyncio.timeout(20):
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ["push", str(tmp_path / "runs"), "--server", cp.url], env=env
        )
    assert result.exit_code == 1, result.output
    assert f"error: {first}: the run was deleted here" in result.output
    assert f"{second} completed 2 events" in result.output


async def left_runs(cp: CP, root: Path) -> tuple[str, str]:
    """Two runs a linked process left running in `root` as it died: the control plane got
    the lines of the first, never any of the second."""
    from aims.node.client import ControlClient
    from aims.node.shipper import Shipper
    from aims.wire import RunnerHello, TracerHealth

    started = RunStarted(scenario="left", oversight="observe", agents=["a"])
    async with await LocalEventStore.open(root) as store:
        held = await store.create_run(started)
        await store.append(held, [Draft(ToolCall(call_id="c", name="x", arguments="{}"), "a")])
        local = await store.create_run(started)
        client = ControlClient(cp.url, cp.runner, runner="run-host-1")
        hello = RunnerHello(
            name=client.runner, version=cli.__version__, arch="x", tracer=TracerHealth(alive=True)
        )
        await client.hello(hello)
        task = asyncio.create_task(Shipper(store, client, held, seen=True).run())
        try:
            async with asyncio.timeout(10):
                while True:
                    head = await cp.state.store.head(held)
                    if head is not None and head.seq >= 1:
                        break
                    await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.aclose()
    return held, local


@pytest.mark.parametrize(
    "command",
    [
        # each ends in an error after it seals: it names a run there is none of
        ["monitor", "01a18000-0000-7000-8000-000000000001", "--signals-only"],
        ["run", "--replay", "01a18000-0000-7000-8000-000000000001", "--monitor", "off"],
    ],
)
async def test_a_linked_command_ships_the_runs_it_seals_that_the_control_plane_holds(
    cp: CP, tmp_path: Path, command: list[str]
) -> None:
    root = tmp_path / "runs"
    held, local = await left_runs(cp, root)
    info = await cp.state.store.run(held)
    assert info is not None
    assert info.status == "running"
    args = [*command, "--runs-dir", str(root), "--server", cp.url]
    env = {"AIMS_TOKEN": cp.runner}
    result = await asyncio.to_thread(CliRunner().invoke, app, args, env=env)
    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "an earlier process left 2 runs running; they are sealed as failed" in said
    assert "did not reach" not in said
    info = await cp.state.store.run(held)
    assert info is not None
    assert info.status == "failed"
    copy = cp.state.store.root / held / "events.jsonl"
    assert copy.read_bytes() == (root / held / "events.jsonl").read_bytes()
    # a run the control plane never got stays here
    assert await cp.state.store.run(local) is None


async def test_a_sealed_run_that_does_not_reach_the_control_plane_says_to_push_it(
    cp: CP, tmp_path: Path
) -> None:
    """The command goes on with a hint, whether nothing answers or something that is no
    control plane answers 200."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{s.getsockname()[1]}"
    junk = ThreadingHTTPServer(("127.0.0.1", 0), type("Junk", (Refusing,), {"status": 200}))
    threading.Thread(target=junk.serve_forever, daemon=True).start()
    try:
        for name, url in (("closed", closed), ("junk", f"http://127.0.0.1:{junk.server_port}")):
            root = tmp_path / name / "runs"
            held, _ = await left_runs(cp, root)
            args = ["monitor", held, "--signals-only", "--runs-dir", str(root), "--server", url]
            env = {"AIMS_TOKEN": "aims_x"}
            result = await asyncio.to_thread(CliRunner().invoke, app, args, env=env)
            assert result.exception is None or isinstance(result.exception, SystemExit), name
            said = " ".join(result.output.split())
            assert "the sealed runs did not reach the control plane:" in said, name
            assert "`aims push` them later" in said, name
            # the command went on to its own work, which finds no scenario in the run
            assert f"error: {held}: run {held} is not a scenario run" in said, name
    finally:
        junk.shutdown()
        junk.server_close()


def test_a_command_that_writes_runs_refuses_a_control_planes_runs_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run there is a copy of one a runner ships. A seal or a new run from another process
    would differ from what the runner ships next."""
    from aims.server.state import close_state, open_state

    data = tmp_path / "server"
    runs = data / "runs"

    async def make() -> str:
        # the control plane's copy of a run its runner still writes
        async with await LocalEventStore.open(runs) as store:
            started = RunStarted(scenario="left", oversight="observe", agents=["a"])
            run_id = await store.create_run(started)
            tool = ToolCall(call_id="c", name="x", arguments="{}")
            await store.append(run_id, [Draft(tool, "a")])
        await close_state(await open_state(data))
        return run_id

    run = asyncio.run(make())
    log = (runs / run / "events.jsonl").read_bytes()
    monkeypatch.setenv("AIMS_TOKEN", "aims_x")
    writing = [
        ["run", "--replay", run, "--monitor", "off", "--no-server", "--runs-dir", str(runs)],
        ["monitor", run, "--signals-only", "--no-server", "--runs-dir", str(runs)],
        ["score", run, "--runs-dir", str(runs)],
        ["label", run, "1", "real", "--runs-dir", str(runs)],
        ["runner", "--server", "http://127.0.0.1:9", "--data-dir", str(data)],
    ]
    for args in writing:
        capsys.readouterr()
        # as the `aims` command calls it: CliRunner goes past the app's own error handling
        with pytest.raises(SystemExit) as end:
            app(args)
        assert end.value.code == 1, args
        out = capsys.readouterr()
        assert out.out == "", args
        assert out.err.splitlines() == [
            f"error: {runs} is the runs folder of a control plane. Only the control plane"
            " writes there; use another folder"
        ], args
    assert (runs / run / "events.jsonl").read_bytes() == log
    assert not (runs / run / "labels.jsonl").exists()
    # a command that only reads runs takes it
    for args in (["verify", run], ["rates"], ["spend"], ["report", run, "--json"]):
        r = CliRunner().invoke(app, [*args, "--runs-dir", str(runs)])
        assert r.exit_code == 0, (args, r.output)


class Refusing(BaseHTTPRequestHandler):
    """Answers every request with its class's status and a problem body."""

    status = 500

    def answer(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps({"title": "Refused", "detail": f"status {self.status}"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/problem+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def refusing() -> Iterator[dict[str, str]]:
    """Control plane URLs that fail: a closed port and servers answering 401, 403 and 500."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        urls = {"closed port": f"http://127.0.0.1:{s.getsockname()[1]}"}
    servers = []
    for status in (401, 403, 500):
        handler = type(f"Refusing{status}", (Refusing,), {"status": status})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        urls[str(status)] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield urls
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def one_error_line(result: Any) -> bool:
    lines = result.output.splitlines()
    return (
        (result.exit_code, type(result.exception)) == (1, SystemExit)
        and len(lines) == 1
        and lines[0].startswith("error: ")
    )


def test_a_control_plane_that_fails_ends_in_one_error_line(
    refusing: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMS_TOKEN", "aims_x")
    commands = {
        "push": ["push", str(tmp_path)],
        "control": ["control", "r1", "pause"],
        "control list": ["control", "r1", "list"],
        "delete": ["delete", "r1", "--yes"],
        "open": ["open", "--no-open"],
        "scenario delete": ["scenario", "delete", "project", "x", "--yes"],
    }
    failed = []
    for name, args in commands.items():
        for case, url in refusing.items():
            r = CliRunner().invoke(app, [*args, "--server", url])
            if not one_error_line(r):
                failed.append(f"{name}, {case}: exit {r.exit_code} {r.exception!r} {r.output!r}")
    assert failed == []


def test_a_token_file_that_holds_no_token_ends_in_one_error_line(
    refusing: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def done(**kwargs: Any) -> int:
        return 0

    class Idle:
        def __init__(self, options: NodeOptions) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            pass

    # What each command does once it has a token is not under test.
    monkeypatch.setattr("aims.cli._run", done)
    monkeypatch.setattr("aims.cli._monitor_runs", done)
    monkeypatch.setattr("aims.node.service.Node", Idle)
    files = {"missing": tmp_path / "missing", "directory": tmp_path}
    for name, data in (
        ("empty", b""),
        ("blank", b" \n\t\n"),
        ("NUL", b"aims_x\x00y\n"),
        ("not UTF-8", b"aims_\xff\xfe\n"),
    ):
        files[name] = tmp_path / name
        files[name].write_bytes(data)
    url = refusing["closed port"]
    commands = {
        "push": ["push", str(tmp_path), "--server", url],
        "control": ["control", "r1", "pause"],
        "control list": ["control", "r1", "list", "--server", url],
        "run": ["run", "builtin/word-count", "--server", url],
        "monitor": ["monitor", "r1", "--server", url],
        "runner": ["runner", "--server", url],
    }
    failed = []
    for name, args in commands.items():
        for case, path in files.items():
            r = CliRunner().invoke(app, [*args, "--token-file", str(path)])
            if not one_error_line(r) or "token" not in r.output:
                failed.append(f"{name}, {case}: exit {r.exit_code} {r.exception!r} {r.output!r}")
    assert failed == []


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
    proc = await create_child(
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


async def test_aims_ui_shows_hidden_characters_of_its_url_and_server_log_as_text(
    tmp_path: Path,
) -> None:
    public = "https://a\N{RIGHT-TO-LEFT OVERRIDE}b\x9bc.example"
    async with ui_process(tmp_path, "--no-runner", "--public-url", public) as (proc, url):
        assert url.startswith("https://a\\u202Eb\\u009Bc.example/#code=")
        local = json.loads((tmp_path / "server" / "server.json").read_text())
        host, _, port = local["url"].removeprefix("http://").rpartition(":")
        async with asyncio.timeout(30):
            reader, writer = await asyncio.open_connection(host, int(port))
            # h11 refuses the escape bytes in the request line; uvicorn logs a warning
            writer.write(b"GET /a\x1b[2J HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            assert (await reader.read(100)).startswith(b"HTTP/1.1 400")
            writer.close()
            await writer.wait_closed()
            proc.send_signal(signal.SIGTERM)
            assert proc.stderr is not None
            rest = (await proc.stderr.read()).decode()
            assert await proc.wait() == 0
    assert "WARNING uvicorn.error: Invalid HTTP request received.\n" in rest
    assert visible(rest) == rest


def binds_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not binds_ipv6_loopback(), reason="needs IPv6 on loopback")
async def test_aims_ui_answers_on_the_address_it_binds(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner", "--host", "::1") as (proc, _):
        local = json.loads((tmp_path / "server" / "server.json").read_text())
        assert local["url"].startswith("http://[::1]:")
        async with httpx2.AsyncClient(base_url=local["url"], trust_env=False) as c:
            assert (await c.get("/api/health")).status_code == 401
        proc.send_signal(signal.SIGTERM)
        await proc.wait()


async def ui_that_ends(data: Path, *args: str) -> tuple[int, list[str]]:
    """The exit code and output lines of an `aims ui` on the data dir `data` that is to end
    on its own."""
    env = {**os.environ, "XDG_DATA_HOME": str(data.parent / "xdg")}
    env.pop("AIMS_TOKEN", None)
    proc = await create_child(
        sys.executable,
        "-m",
        "aims",
        "ui",
        "--no-open",
        "--no-runner",
        "--data-dir",
        str(data),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=data.parent,
    )
    try:
        async with asyncio.timeout(60):
            out, _ = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return proc.returncode, out.decode().splitlines()


async def test_aims_ui_on_a_port_in_use_ends_in_one_error_line(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        code, lines = await ui_that_ends(tmp_path / "server", "--port", str(port))
    assert (code, lines) == (
        1,
        [f"error: cannot listen on 127.0.0.1:{port}: address already in use"],
    )
    # nothing was opened before it tried to listen
    assert not (tmp_path / "server").exists()


async def test_aims_ui_on_an_address_not_on_this_host_ends_in_one_error_line(
    tmp_path: Path,
) -> None:
    # The path of --host ::1 on a host without IPv6: each address is skipped, none is left.
    code, lines = await ui_that_ends(
        tmp_path / "server", "--host", "2001:db8::1", "--behind-proxy", "127.0.0.1"
    )
    assert code == 1
    assert len(lines) == 1
    assert lines[0].startswith("error: cannot listen on [2001:db8::1]:8484: ")


async def test_a_second_aims_ui_on_its_port_leaves_the_first_alone(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, _):
        info = tmp_path / "server" / "server.json"
        before = info.read_text()
        local = json.loads(before)
        port = local["url"].rpartition(":")[2]
        code, lines = await ui_that_ends(tmp_path / "server", "--port", port)
        assert code == 1
        assert lines == [
            f"error: cannot listen on 127.0.0.1:{port}: address already in use",
            IN_USE_HINT,
        ]
        assert info.read_text() == before
        async with httpx2.AsyncClient(
            base_url=local["url"],
            headers={"Authorization": f"Bearer {local['operator']}"},
            trust_env=False,
        ) as c:
            assert (await c.get("/api/health")).status_code == 200
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0


async def test_a_second_aims_ui_on_its_data_dir_is_refused(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, _):
        data = tmp_path / "server"
        before = (data / "server.json").read_text()
        local = json.loads(before)
        code, lines = await ui_that_ends(data, "--port", "0")
        assert (code, lines) == (
            1,
            [f"error: the data dir {data} is in use by the `aims ui` on {local['url']}"],
        )
        # its server.json and operator token are as they were
        assert (data / "server.json").read_text() == before
        async with httpx2.AsyncClient(
            base_url=local["url"],
            headers={"Authorization": f"Bearer {local['operator']}"},
            trust_env=False,
        ) as c:
            assert (await c.get("/api/health")).status_code == 200
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0


async def test_aims_ui_on_a_data_dir_in_use_opens_none_of_its_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def opened(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("the state was opened")

    monkeypatch.setattr(serve_module, "open_state", opened)
    data = tmp_path / "server"
    held = hold(data)
    assert held is not None
    try:
        options = UiOptions(port=0, data=data, runner=False, open_browser=False)
        message = f"^the data dir {re.escape(str(data))} is in use by another `aims ui`$"
        with pytest.raises(ServeError, match=message):
            await serve(options)
        (data / "server.json").write_text('{"url": "http://127.0.0.1:1", "token": "secret"}')
        with pytest.raises(ServeError, match=r"in use by the `aims ui` on http://127.0.0.1:1$"):
            await serve(options)
        (data / "server.json").write_text("[")
        with pytest.raises(ServeError, match=r"in use by another `aims ui`$"):
            await serve(options)
        assert sorted(p.name for p in data.iterdir()) == ["server.json", "server.lock"]
    finally:
        os.close(held)
    again = hold(data)
    assert again is not None
    os.close(again)


def test_a_data_dir_on_a_file_system_without_locks_is_used_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(serve_module.fcntl, "flock", no_locks)
    with caplog.at_level(logging.WARNING, logger="aims.server"):
        assert hold(tmp_path / "server") is None
    assert "cannot lock" in caplog.text
    assert "No locks available; a second aims ui on this data dir would not be stopped" in (
        caplog.text
    )


async def test_aims_ui_on_the_port_of_another_is_refused(tmp_path: Path) -> None:
    """Both set SO_REUSEADDR, which lets neither take a port the other listens on."""
    async with ui_process(tmp_path, "--no-runner") as (proc, url):
        port = urlsplit(url).port
        code, lines = await ui_that_ends(tmp_path / "other", "--port", str(port))
        assert (code, lines) == (
            1,
            [f"error: cannot listen on 127.0.0.1:{port}: address already in use"],
        )
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0


async def test_aims_ui_starts_again_at_once_on_the_port_it_left(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, url):
        base = url.partition("/#code=")[0]
        port = str(urlsplit(base).port)
        # A connection open as it stops: the server closes it, which leaves the port in
        # TIME_WAIT. Only SO_REUSEADDR lets a new socket listen there now.
        async with httpx2.AsyncClient(base_url=base, trust_env=False) as c:
            assert (await c.get("/api/health")).status_code == 401
            proc.send_signal(signal.SIGTERM)
            async with asyncio.timeout(30):
                assert await proc.wait() == 0
    async with ui_process(tmp_path, "--no-runner", "--port", port) as (proc, url):
        assert urlsplit(url).port == int(port)
        async with httpx2.AsyncClient(base_url=url.partition("/#code=")[0], trust_env=False) as c:
            assert (await c.get("/api/health")).status_code == 401
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0


async def test_aims_ui_on_localhost_with_port_0_answers_on_its_url(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner", "--host", "localhost") as (proc, _):
        local = json.loads((tmp_path / "server" / "server.json").read_text())
        port = urlsplit(local["url"]).port
        # each loopback address localhost names here: the server listens on all it can bind
        names = {i[4][0] for i in socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)}
        urls = [local["url"]]
        if "127.0.0.1" in names:
            urls.append(f"http://127.0.0.1:{port}")
        if "::1" in names and binds_ipv6_loopback():
            urls.append(f"http://[::1]:{port}")
        for url in urls:
            async with httpx2.AsyncClient(base_url=url, trust_env=False) as c:
                assert (await c.get("/api/health")).status_code == 401, url
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await proc.wait() == 0


async def test_every_socket_for_localhost_takes_one_port() -> None:
    sockets = await listen("localhost", 0)
    try:
        assert len({s.getsockname()[1] for s in sockets}) == 1
    finally:
        for s in sockets:
            s.close()


async def test_listening_sockets_are_set_as_asyncio_sets_those_of_uvicorn() -> None:
    """uvicorn listens through asyncio's create_server with its defaults."""

    def options(s: socket.socket) -> dict[str, bool]:
        found = {"reuseaddr": bool(s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))}
        if hasattr(socket, "SO_REUSEPORT"):
            found["reuseport"] = bool(s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT))
        if s.family == socket.AF_INET6:
            found["v6only"] = bool(s.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY))
        return found

    hosts = ["127.0.0.1", "::1"] if binds_ipv6_loopback() else ["127.0.0.1"]
    loop = asyncio.get_running_loop()
    for host in hosts:
        theirs = await loop.create_server(asyncio.Protocol, host, 0)
        ours = await listen(host, 0)
        try:
            assert theirs.sockets
            assert [options(s) for s in ours] == [options(s) for s in theirs.sockets]
        finally:
            theirs.close()
            for s in ours:
                s.close()


def test_listening_skips_an_address_this_host_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.socket

    def no_ipv6(family: int = -1, *args: Any) -> socket.socket:
        if family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
        return real(family, *args)

    tcp = (socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
    lacks = (socket.AF_INET6, *tcp, ("2001:db8::1", 0, 0, 0))
    ipv4 = (socket.AF_INET, *tcp, ("127.0.0.1", 0))
    sockets = open_sockets([lacks, ipv4])
    with contextlib.closing(sockets[0]):
        assert [s.family for s in sockets] == [socket.AF_INET]
    monkeypatch.setattr(socket, "socket", no_ipv6)
    sockets = open_sockets([(socket.AF_INET6, *tcp, ("::1", 0, 0, 0)), ipv4])
    with contextlib.closing(sockets[0]):
        assert [s.family for s in sockets] == [socket.AF_INET]
    with pytest.raises(OSError, match="not supported") as caught:
        open_sockets([(socket.AF_INET6, *tcp, ("::1", 0, 0, 0))])
    assert caught.value.errno == errno.EAFNOSUPPORT


async def test_a_host_that_does_not_resolve_ends_in_one_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(*args: object, **kwargs: object) -> object:
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    with pytest.raises(ServeError) as caught:
        await listen("nowhere.invalid", 8484, in_use="hint")
    assert str(caught.value) == "cannot listen on nowhere.invalid:8484: no such host"
    assert caught.value.hint is None


async def test_aims_ui_whose_app_fails_to_start_ends_in_one_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextlib.asynccontextmanager
    async def broken(app: object) -> AsyncGenerator[None]:
        raise RuntimeError("the app broke")
        yield

    real = serve_module.create_app

    def failing(*args: Any, **kwargs: Any) -> Any:
        app = real(*args, **kwargs)
        app.router.lifespan_context = broken
        return app

    monkeypatch.setattr(serve_module, "create_app", failing)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    options = UiOptions(port=port, data=tmp_path / "server", runner=False, open_browser=False)
    with pytest.raises(ServeError, match=r"^the server did not start$"):
        await serve(options)
    assert not (tmp_path / "server" / "server.json").exists()
    # its socket is closed: the port is free
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


async def test_a_ctrl_c_after_aims_ui_closed_leaves_its_exit_code(tmp_path: Path) -> None:
    async with ui_process(tmp_path, "--no-runner") as (proc, _):
        data = tmp_path / "server"
        proc.send_signal(signal.SIGINT)
        # SQLite removes the WAL file as the database closes, the last step of the stop
        await until(lambda: not (data / "server.sqlite-wal").exists(), 30)
        await asyncio.sleep(0.05)
        # one more only: a third stop signal before the close ends quits at once
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        assert proc.stderr is not None
        async with asyncio.timeout(30):
            err = (await proc.stderr.read()).decode()
            assert await proc.wait() == 0, err


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
    # --data-dir holds the local runner's runs too, not the runner's folder under the home
    assert (tmp_path / "server" / "runner" / "runs").is_dir()
    assert not (tmp_path / "xdg" / "aims" / "runner").exists()


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


class SleepingModel(BaseHTTPRequestHandler):
    """A chat model that answers each call with `sleep 1` in bash, so a run stays under way.
    `calls` counts the calls."""

    calls: ClassVar[list[int]]

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.calls.append(1)
        n = len(self.calls)
        call = {
            "id": f"t{n}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "sleep 1"})},
        }
        body = json.dumps(
            {
                "id": f"c{n}",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.mark.docker
async def test_one_stop_sent_to_the_whole_job_still_archives_and_seals_a_run(
    tmp_path: Path,
) -> None:
    """systemd, or `kill -- -<pgid>`, sends one SIGTERM to every process of the job. The runner
    gets it on its own and through `aims ui`. That is one stop: the run under way keeps its
    files and its seal reaches the control plane."""
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    handler = type("Sleeping", (SleepingModel,), {"calls": []})
    model = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    (tmp_path / "aims.toml").write_text(
        f"""
[profiles.sleeping]
backend = "chat"
base_url = "http://127.0.0.1:{model.server_address[1]}/v1"
model = "m"
weights = "w"
stream = false
reasoning = "none"
price = {{ input = 0.0, cached = 0.0, output = 0.0 }}

[roles]
agent = "sleeping"
sweep = "sleeping"
judge = "sleeping"
"""
    )
    folder = tmp_path / "scenarios" / "stop"
    folder.mkdir(parents=True)
    (folder / "scenario.yaml").write_text(
        "version: 1\n"
        "name: stop\n"
        "oversight: observe\n"
        "task:\n"
        "  instruction: Do it.\n"
        "  environment: {image: python:3.13-slim}\n"
        "agents:\n"
        "  - id: agent\n"
        "    budget: {steps: 60, wall_s: 300}\n"
    )
    try:
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
                body = {
                    "scenario": {"source": "project", "name": "stop"},
                    "monitor": "off",
                    "audit": "snapshot",
                    "confirm": True,
                }
                r = await c.post("/api/runs", json=body)
                assert r.status_code == 202, r.text
                [run_id] = r.json()["run_ids"]
            await until(lambda: len(handler.calls) >= 2, 120)
            os.killpg(proc.pid, signal.SIGTERM)
            assert proc.stderr is not None
            async with asyncio.timeout(120):
                err = (await proc.stderr.read()).decode()
                assert await proc.wait() == 0
    finally:
        model.shutdown()
        model.server_close()
    assert "killing now" not in err
    assert "the local runner exited" not in err
    events = (tmp_path / "server" / "runs" / run_id / "events.jsonl").read_text()
    lines = [json.loads(line) for line in events.splitlines()]
    assert "sandbox.archived" in [e["type"] for e in lines]
    assert lines[-1]["type"] == "run.finished"
    assert lines[-1]["data"] == {"status": "cancelled", "reason": "cancelled by the operator"}


@pytest.mark.docker
async def test_a_third_ctrl_c_quits_aims_ui_at_once_with_its_runner(tmp_path: Path) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    runner = 0
    try:
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
            children = await asyncio.create_subprocess_exec(
                "pgrep", "-P", str(proc.pid), stdout=asyncio.subprocess.PIPE
            )
            runner = int((await children.communicate())[0].split()[0])
            for _ in range(3):
                os.killpg(proc.pid, signal.SIGINT)
                # signals sent with no gap arrive as one
                await asyncio.sleep(0.05)
            assert proc.stderr is not None
            async with asyncio.timeout(20):
                err = (await proc.stderr.read()).decode()
                assert await proc.wait() == 130
        assert "quit at once and skipped the cleanup" in err

        def gone() -> bool:
            try:
                os.kill(runner, 0)
            except ProcessLookupError:
                return True
            return False

        await until(gone, 10)
        runner = 0
    finally:
        if runner:
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner, signal.SIGKILL)
        # what the next start does: it removes what the killed runner left
        backend = await DockerBackend.open(audit="snapshot")
        await backend.close()


@pytest.mark.docker
async def test_a_stop_sent_to_the_job_and_passed_on_still_removes_the_runners_helpers(
    tmp_path: Path,
) -> None:
    if not os.environ.get("AIMS_REQUIRE_DOCKER"):
        try:
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()
        except Exception as e:
            pytest.skip(f"Docker is not available: {e}")
    runner = 0
    try:
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
            children = await asyncio.create_subprocess_exec(
                "pgrep", "-P", str(proc.pid), stdout=asyncio.subprocess.PIPE
            )
            runner = int((await children.communicate())[0].split()[0])
            assert await helpers_of(runner)
            # One stop of a job run through `uv run`: SIGTERM to every process of the job,
            # then uv passes its own on to `aims ui`. The runner hears it three times: from
            # the job and twice through `aims ui`.
            os.killpg(proc.pid, signal.SIGTERM)
            # a gap, so the two do not arrive as one, short enough to land in the stop
            await asyncio.sleep(0.02)
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGTERM)
            assert proc.stderr is not None
            async with asyncio.timeout(120):
                err = (await proc.stderr.read()).decode()
                await proc.wait()
        assert "quit at once and skipped the cleanup" not in err
        assert await helpers_of(runner) == [], err
        runner = 0
    finally:
        if runner:
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner, signal.SIGKILL)
            # what the next start does: it removes what the killed runner left
            backend = await DockerBackend.open(audit="snapshot")
            await backend.close()


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
    proc = await create_child(
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
