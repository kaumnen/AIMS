"""Streams and replies from the Docker daemon that it cuts midway, against a fake daemon on a
local port."""

import asyncio
import contextlib
import errno
import hashlib
import json
import re
import socket
import struct
import urllib.parse
from asyncio import trsock
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiodocker
import aiohttp
import pytest
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec
from aiodocker.stream import Stream
from typer.testing import CliRunner

from aims.cli import app
from aims.events import LocalEventStore, RunStarted
from aims.sandbox import (
    AuditedSandbox,
    DockerSandbox,
    SandboxInfo,
    SandboxSpec,
    SandboxStoppedError,
    execs,
)
from aims.sandbox.base import TOOLBOX, Capture
from aims.sandbox.docker import DockerBackend, _call, _helper
from aims.sandbox.execs import DockerClient, exec_output, open_stream, refused
from aims.sandbox.reader import stream_archive

pytestmark = pytest.mark.anyio


def frame(data: bytes) -> bytes:
    """`data` as one stdout frame of Docker's multiplexed stream."""
    return struct.pack(">BxxxL", 1, len(data)) + data


# two scenario images no container uses, last tagged long ago
IMAGES = [
    {"Id": f"sha256:{n * 64}", "RepoTags": [f"aims-env:{n * 4}"], "Labels": {"aims.managed": "1"}}
    for n in "ab"
]


class Daemon:
    """A Docker daemon with the container, exec and archive calls a sandbox makes. It cuts each
    call in `cut` midway, as a daemon that dies or restarts does: `wait` after its header, `logs`
    and `archive` after their first chunk, the first exec's stream after its first output. A
    restarted daemon has no record of an exec it cut. With `lost` in `cut`, only the connection
    of the first exec's stream drops: that exec runs on until an `aims-exec -k` ends it. With
    `kill` too, the first kill's connection drops as well."""

    def __init__(self, cut: set[str]) -> None:
        self.cut = cut
        self.calls: list[str] = []
        self.execs = 0
        self.forgot: set[str] = set()
        self.kills: list[list[str]] = []
        # the execs of the kills, by name
        self.killers: list[str] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                method, target, _ = head.split(" ", 2)
                body = b""
                if size := re.search(r"(?im)^content-length: *(\d+)", head):
                    body = await reader.readexactly(int(size[1]))
                url = urllib.parse.urlsplit(target)
                path = urllib.parse.unquote(url.path).removeprefix("/v1.45/")
                self.calls.append(f"{method} {path}")
                if path == "containers/c1/exec" and "-k" in (cmd := json.loads(body)["Cmd"]):
                    self.kills.append(cmd)
                    self.killers.append(f"e{self.execs + 1}")
                if not await self.answer(method, path, writer):
                    break
        writer.close()

    async def answer(self, method: str, path: str, writer: asyncio.StreamWriter) -> bool:
        """Answer one call; False once the connection is done. A call in `cut` by its method
        and path gets its reply cut after the header and half the body."""
        name = path.split("/")[1] if path.startswith("exec/") else ""
        whole = f"{method} {path}" not in self.cut
        if path in ("containers/c1/wait", "containers/c1/logs", "containers/c1/archive"):
            kind = path.rsplit("/", 1)[1]
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            if kind != "wait":
                data = frame(b"denied\n") if kind == "logs" else b"x" * 512
                writer.write(b"%x\r\n%s\r\n" % (len(data), data))
            elif kind not in self.cut:
                data = json.dumps({"StatusCode": 1}).encode()
                writer.write(b"%x\r\n%s\r\n" % (len(data), data))
            await writer.drain()
            if kind in self.cut:
                return False
            writer.write(b"0\r\n\r\n")
            return True
        if path == f"exec/{name}/start" and "refuse" in self.cut:
            self.reply(writer, "409 Conflict", {"message": "Container c1 is paused"})
            return True
        if path == f"exec/{name}/start":
            writer.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            if "upgrade" in self.cut:
                return False
            writer.write(frame(b"hi\n"))
            await writer.drain()
            # Docker ends the stream when the exec ends; a client set up by then reads all of it.
            await asyncio.sleep(0.05)
            if "exec" in self.cut and name == "e1":
                self.forgot.add(name)
            return False
        if path == "containers/c1/exec":
            self.execs += 1
            self.reply(writer, "201 Created", {"Id": f"e{self.execs}"})
        elif path == f"exec/{name}/json":
            if name in self.forgot:
                self.reply(writer, "404 Not Found", {"message": f"No such exec instance: {name}"})
            elif "kill" in self.cut and self.killers[:1] == [name]:
                state = {"Running": True, "ExitCode": None, "Pid": 43}
                self.reply(writer, "200 OK", {**state, "ProcessConfig": {"tty": False}})
            elif "lost" in self.cut and name == "e1":
                running = not self.kills
                state = {"Running": running, "ExitCode": None if running else 137, "Pid": 42}
                self.reply(writer, "200 OK", {**state, "ProcessConfig": {"tty": False}})
            else:
                state = {"Running": False, "ExitCode": 0, "Pid": 0, "ProcessConfig": {"tty": False}}
                self.reply(writer, "200 OK", state)
        elif path == "containers/create":
            self.reply(writer, "201 Created", {"Id": "c1"})
        elif path == "containers/c1/json":
            self.reply(writer, "200 OK", {"Id": "c1", "Config": {"Tty": False}}, whole)
        elif path == "containers/c1/top":
            self.reply(writer, "200 OK", {"Titles": ["PID", "PPID", "UID", "CMD"], "Processes": []})
        elif path == "/version":
            self.reply(writer, "200 OK", {"ApiVersion": "1.45"})
        elif path == "info":
            self.reply(writer, "500 Internal Server Error", {"message": "the fake daemon"})
        elif path == "containers/json":
            self.reply(writer, "200 OK", [])
        elif path == "images/json":
            self.reply(writer, "200 OK", IMAGES)
        elif method == "GET" and path.startswith("images/"):
            self.reply(writer, "200 OK", {"Metadata": {"LastTagTime": "2020-01-01T00:00:00Z"}})
        elif method == "DELETE" and path.startswith("images/"):
            self.reply(writer, "200 OK", [{"Untagged": path.removeprefix("images/")}], whole)
        elif not whole:
            self.reply(writer, "409 Conflict", {"message": f"{path} is busy"}, whole)
        elif method in ("POST", "DELETE"):
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        else:
            self.reply(writer, "404 Not Found", {"message": f"no {path}"})
        return whole

    def reply(
        self, writer: asyncio.StreamWriter, status: str, body: Any, whole: bool = True
    ) -> None:
        data = json.dumps(body).encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n\r\n".encode()
            + (data if whole else data[: len(data) // 2])
        )


@contextlib.asynccontextmanager
async def serving(cut: set[str]) -> AsyncGenerator[tuple[Daemon, aiodocker.Docker]]:
    daemon = Daemon(cut)
    server = await asyncio.start_server(daemon.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    docker = DockerClient(url=f"tcp://127.0.0.1:{port}", api_version="v1.45")
    try:
        yield daemon, docker
    finally:
        await docker.close()
        server.close()
        server.close_clients()
        await server.wait_closed()


async def test_an_archive_the_daemon_cuts_raises_a_docker_error() -> None:
    """aiohttp raises its own error for a stream cut midway, which callers do not catch."""
    chunks: list[bytes] = []
    async with serving({"archive"}) as (_, docker):

        async def read() -> None:
            async for chunk in stream_archive(DockerContainer(docker, id="c1"), "/w"):
                chunks.append(chunk)

        with pytest.raises(aiodocker.DockerError, match="the archive of /w was cut") as caught:
            await read()
    assert isinstance(caught.value.__cause__, aiohttp.ClientPayloadError)
    assert chunks == [b"x" * 512]


@pytest.mark.parametrize("call", ["wait", "logs"])
async def test_a_helper_whose_wait_or_log_the_daemon_cuts_raises_a_docker_error(
    call: str,
) -> None:
    """Docker sends a wait's header at once and its body once the container ends."""
    async with serving({call}) as (daemon, docker):
        with pytest.raises(aiodocker.DockerError, match="was cut") as caught:
            await _helper(docker, "img", ["chown"], [], labels={}, caps=[])
        assert isinstance(caught.value.__cause__, aiohttp.ClientPayloadError)
        assert daemon.calls[-1] == "DELETE containers/c1"


async def test_a_reply_the_daemon_cuts_raises_a_docker_error() -> None:
    """aiodocker raises aiohttp's own error for a reply whose body is cut after its header, which
    callers do not catch. Docker sends a small reply in one write, so this takes a daemon that
    dies just then."""
    async with serving({"GET containers/c1/json"}) as (_, docker):
        with pytest.raises(aiodocker.DockerError, match="GET containers/c1/json was cut") as caught:
            await DockerContainer(docker, id="c1").show()
    assert isinstance(caught.value.__cause__, aiohttp.ClientPayloadError)


async def test_images_prune_goes_on_past_an_image_whose_delete_reply_the_daemon_cuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image whose reply is cut is skipped; the prune still lists those it removed."""
    async with serving({"DELETE images/aims-env:bbbb"}) as (daemon, docker):
        monkeypatch.setenv("DOCKER_CONTEXT", "default")
        monkeypatch.setenv("DOCKER_HOST", docker.docker_host)
        args = ["images", "prune", "--days", "30"]
        out = await asyncio.to_thread(CliRunner().invoke, app, args)
    assert (out.exit_code, out.stdout) == (0, "removed aims-env:aaaa\n"), out.output
    deleted = [c for c in daemon.calls if c.startswith("DELETE")]
    assert deleted == ["DELETE images/aims-env:aaaa", "DELETE images/aims-env:bbbb"]


@pytest.mark.parametrize("call", ["output", "exit code"])
async def test_a_short_exec_whose_stream_is_lost_raises_a_docker_error(call: str) -> None:
    """Docker ends an exec's stream once the exec ended. A stream that ends while the exec runs
    on lost its connection, so what came is not all of the exec's output."""

    def run(docker: aiodocker.Docker) -> Any:
        container = DockerContainer(docker, id="c1")
        if call == "output":
            return exec_output(container, ["x"], timeout_s=5)
        return _call(container, ["x"], "root")

    async with serving({"lost"}) as (_, docker):
        with pytest.raises(aiodocker.DockerStreamError, match="connection to Docker was lost"):
            await run(docker)
    async with serving(set()) as (_, docker):
        assert await run(docker) == (b"hi\n" if call == "output" else 0)


def docker_config(home: Path, current: str, hosts: dict[str, str]) -> None:
    """A docker CLI config in `home` whose current context is `current`, with a context of each
    name in `hosts` and the daemon it names."""
    config = home / ".docker"
    for name, host in hosts.items():
        meta = config / "contexts" / "meta" / hashlib.sha256(name.encode()).hexdigest()
        meta.mkdir(parents=True)
        endpoint = {"Host": host, "SkipTLSVerify": False}
        (meta / "meta.json").write_text(
            json.dumps({"Name": name, "Endpoints": {"docker": endpoint}})
        )
    (config / "config.json").write_text(json.dumps({"currentContext": current}))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with a docker CLI config whose current context names a daemon that is not there,
    as Docker Desktop's does on a host where DOCKER_HOST names another."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    gone = f"unix://{tmp_path}/gone.sock"
    docker_config(tmp_path, "desktop", {"desktop": gone, "other": "tcp://10.0.0.8:2375"})
    return tmp_path


@pytest.mark.parametrize(
    ("host", "context", "target"),
    [
        (None, None, "unix://localhost"),
        (None, "other", "tcp://10.0.0.8:2375"),
        ("tcp://127.0.0.1:1", None, "tcp://127.0.0.1:1"),
        ("tcp://127.0.0.1:1", "other", "tcp://127.0.0.1:1"),
        ("", None, "unix://localhost"),
    ],
)
async def test_aims_picks_the_docker_daemon_as_the_docker_cli_does(
    home: Path, monkeypatch: pytest.MonkeyPatch, host: str | None, context: str | None, target: str
) -> None:
    """DOCKER_HOST, then DOCKER_CONTEXT, then the current context. The docker CLI takes an empty
    variable as unset."""
    for name, value in (("DOCKER_HOST", host), ("DOCKER_CONTEXT", context)):
        if value is not None:
            monkeypatch.setenv(name, value)
    docker = execs.docker_client()
    try:
        assert docker.docker_host == target
    finally:
        await docker.close()
    docker = execs.docker_client("tcp://127.0.0.1:2")
    try:
        assert docker.docker_host == "tcp://127.0.0.1:2"
    finally:
        await docker.close()


async def test_aims_reaches_the_daemon_docker_host_names_over_the_current_context(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serving(set()) as (_, docker):
        monkeypatch.setenv("DOCKER_HOST", docker.docker_host)
        args = ["images", "prune", "--days", "30", "--dry-run"]
        out = await asyncio.to_thread(CliRunner().invoke, app, args)
        with pytest.raises(aiodocker.DockerError, match="the fake daemon"):
            await DockerBackend.open(audit="snapshot")
    removed = "would remove aims-env:aaaa\nwould remove aims-env:bbbb\n"
    assert (out.exit_code, out.stdout) == (0, removed), out.output


async def test_a_close_whose_container_delete_the_daemon_cuts_still_removes_the_volumes() -> None:
    """aiodocker reads the body of an error reply before it raises, so that read is cut too."""
    async with serving({"DELETE containers/c1"}) as (daemon, docker):
        backend = SimpleNamespace(tracer=None, docker=docker)
        info = SandboxInfo("c1", "docker", "img", "sha256:img", {})
        container = DockerContainer(docker, id="c1")
        spec = SandboxSpec(image="img")
        sandbox = DockerSandbox(
            cast(Any, backend), container, spec, info, "w", [], None, scratch=("s",)
        )
        await sandbox.close()
    assert daemon.calls == ["DELETE containers/c1", "DELETE volumes/s", "DELETE volumes/w"]


class Backend:
    def __init__(self, sandbox: DockerSandbox) -> None:
        self.sandbox = sandbox
        self.name = "docker"

    async def build(self, context: bytes, dockerfile: str = "Dockerfile") -> str:
        return "img"

    async def create(self, spec: SandboxSpec, *, run_id: str, agent_id: str) -> DockerSandbox:
        return self.sandbox

    async def close(self) -> None:
        pass


CUTS = {"midway": {"exec"}, "lost": {"lost"}, "lost kill": {"lost", "kill"}}


@pytest.mark.parametrize("cut", ["open", "midway", "lost", "lost kill"])
async def test_an_exec_whose_stream_the_daemon_cuts_fails_and_stops_the_sandbox(
    cut: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut as it opens, the socket set-up fails: macOS refuses a socket option on a socket the
    daemon closed. Cut midway, the stream ends early and a daemon that restarted has no record of
    the exec. Lost midway, the stream ends early and the exec runs on, so it is ended. Each way
    the exec ends as failed, which stops the sandbox. A kill whose own stream is lost may still
    have ended it, so the exec is looked at again."""
    real = trsock.TransportSocket.setsockopt
    refused: list[int] = []

    def setsockopt(self: Any, *args: Any) -> None:
        if cut == "open" and args[1] == socket.SO_KEEPALIVE and not refused:
            refused.append(1)
            raise OSError(errno.EINVAL, "Invalid argument")
        real(self, *args)

    monkeypatch.setattr(trsock.TransportSocket, "setsockopt", setsockopt)
    spec = SandboxSpec(image="img")
    async with (
        serving(CUTS.get(cut, set())) as (daemon, docker),
        await LocalEventStore.open(tmp_path / "runs") as store,
    ):
        backend = SimpleNamespace(tracer=None, docker=docker)
        info = SandboxInfo("c1", "docker", "img", "sha256:img", {})
        container = DockerContainer(docker, id="c1")
        inner = DockerSandbox(cast(Any, backend), container, spec, info, "w", ["sh", "-c"], None)
        run_id = await store.create_run(
            RunStarted(scenario="t", oversight="observe", agents=["a1"])
        )
        sb = await AuditedSandbox.start(
            cast(Any, Backend(inner)), spec, store, run_id=run_id, agent_id="a1"
        )
        try:
            result = await sb.exec("echo hi", timeout_s=10)
            with pytest.raises(SandboxStoppedError):
                await sb.exec("echo hi", timeout_s=10)
        finally:
            await sb.close()
        ends = await store.events(run_id, limit=100, types=("exec.finished", "sandbox.stopped"))

    why = {"open": "was cut", "midway": "No such exec instance"}.get(cut, "connection to Docker")
    assert result.finished.end == "failed"
    assert why in (result.finished.error or "")
    assert [(e.type, e.data.get("end"), e.data.get("reason")) for e in ends] == [
        ("exec.finished", "failed", None),
        ("sandbox.stopped", None, "exec_failed"),
    ]
    assert "POST containers/c1/kill" in daemon.calls
    if cut.startswith("lost"):
        # The exec is ended before the sandbox is stopped.
        assert result.finished.error == "the connection to Docker was lost while the exec ran"
        assert [k[:2] for k in daemon.kills] == [[f"{TOOLBOX}/aims-exec", "-k"]]
        assert daemon.calls.index("POST exec/e2/start") < daemon.calls.index(
            "POST containers/c1/kill"
        )


@pytest.mark.parametrize("cut", ["refuse", "open"])
async def test_only_an_error_answer_counts_as_a_refused_exec_start(
    cut: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start the daemon refuses did nothing. One cut after its request went out, here as the
    socket is set up, may have started the exec."""
    real = trsock.TransportSocket.setsockopt

    def setsockopt(self: Any, *args: Any) -> None:
        if cut == "open" and args[1] == socket.SO_KEEPALIVE:
            raise OSError(errno.EINVAL, "Invalid argument")
        real(self, *args)

    monkeypatch.setattr(trsock.TransportSocket, "setsockopt", setsockopt)
    sent: list[bool] = []
    async with serving({cut}) as (_, docker):
        stream = Exec(docker, "e1", False).start(detach=False)
        with pytest.raises(aiodocker.DockerError) as caught:
            await open_stream(stream, on_send=lambda: sent.append(True))
    assert sent == [True]
    assert refused(stream, caught.value) == (cut == "refuse")


async def test_an_exec_stream_whose_socket_closes_as_it_opens_raises_a_docker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiodocker asserts the stream still has its socket once it has read the daemon's answer.
    A daemon that closes the socket just after it answers takes it away by then."""
    real = aiohttp.ClientResponse.read

    async def read(self: aiohttp.ClientResponse) -> bytes:
        body = await real(self)
        protocol = None if self.connection is None else self.connection.protocol
        if self.status == 101 and protocol is not None and protocol.closed is not None:
            await protocol.closed
        return body

    monkeypatch.setattr(aiohttp.ClientResponse, "read", read)
    async with serving({"upgrade"}) as (_, docker), asyncio.timeout(5):
        stream = Exec(docker, "e1", False).start(detach=False)
        with pytest.raises(aiodocker.DockerError, match="was cut") as caught:
            await open_stream(stream)
    assert isinstance(caught.value.__cause__, AssertionError)
    assert stream._closed


async def test_an_exec_whose_socket_closes_before_its_stdin_goes_out_is_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiodocker asserts the stream still has its socket when it writes stdin. A connection that
    drops first takes it away and leaves the exec running."""
    real = Stream.write_in

    async def write_in(self: Stream, data: bytes) -> None:
        assert self._resp is not None
        assert self._resp.connection is not None
        transport = self._resp.connection.transport
        assert transport is not None
        transport.close()
        await asyncio.sleep(0)
        await real(self, data)

    monkeypatch.setattr(Stream, "write_in", write_in)
    async with serving({"lost"}) as (daemon, docker):
        backend = SimpleNamespace(tracer=None, docker=docker)
        info = SandboxInfo("c1", "docker", "img", "sha256:img", {})
        container = DockerContainer(docker, id="c1")
        sandbox = DockerSandbox(
            cast(Any, backend), container, SandboxSpec(image="img"), info, "w", [], None
        )
        outcome = await sandbox.run(
            ["cat"], uid=1000, cwd="/", timeout_ms=10_000, capture=Capture(1024), stdin=b"in"
        )
    assert outcome.end == "failed"
    assert outcome.error == "the connection to Docker was lost while the exec ran"
    assert daemon.kills == [[f"{TOOLBOX}/aims-exec", "-k", "cat"]]
