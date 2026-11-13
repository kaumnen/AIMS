"""Streams and replies from the Docker daemon that it cuts midway, against a fake daemon on a
local port."""

import asyncio
import contextlib
import errno
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

from aims.events import LocalEventStore, RunStarted
from aims.sandbox import (
    AuditedSandbox,
    DockerSandbox,
    SandboxInfo,
    SandboxSpec,
    SandboxStoppedError,
)
from aims.sandbox.base import TOOLBOX
from aims.sandbox.docker import _helper
from aims.sandbox.execs import DockerClient
from aims.sandbox.reader import stream_archive

pytestmark = pytest.mark.anyio


def frame(data: bytes) -> bytes:
    """`data` as one stdout frame of Docker's multiplexed stream."""
    return struct.pack(">BxxxL", 1, len(data)) + data


class Daemon:
    """A Docker daemon with the container, exec and archive calls a sandbox makes. It cuts each
    call in `cut` midway, as a daemon that dies or restarts does: `wait` after its header, `logs`
    and `archive` after their first chunk, the first exec's stream after its first output. A
    restarted daemon has no record of an exec it cut. With `lost` in `cut`, only the connection
    of the first exec's stream drops: that exec runs on until an `aims-exec -k` ends it."""

    def __init__(self, cut: set[str]) -> None:
        self.cut = cut
        self.calls: list[str] = []
        self.execs = 0
        self.forgot: set[str] = set()
        self.kills: list[list[str]] = []

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
        if path == f"exec/{name}/start":
            writer.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
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
        elif not whole:
            self.reply(writer, "409 Conflict", {"message": f"{path} is busy"}, whole)
        elif method in ("POST", "DELETE"):
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        else:
            self.reply(writer, "404 Not Found", {"message": f"no {path}"})
        return whole

    def reply(
        self, writer: asyncio.StreamWriter, status: str, body: dict[str, Any], whole: bool = True
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


@pytest.mark.parametrize("cut", ["open", "midway", "lost"])
async def test_an_exec_whose_stream_the_daemon_cuts_fails_and_stops_the_sandbox(
    cut: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut as it opens, the socket set-up fails: macOS refuses a socket option on a socket the
    daemon closed. Cut midway, the stream ends early and a daemon that restarted has no record of
    the exec. Lost midway, the stream ends early and the exec runs on, so it is ended. Each way
    the exec ends as failed, which stops the sandbox."""
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
        serving({"midway": {"exec"}, "lost": {"lost"}}.get(cut, set())) as (daemon, docker),
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
    if cut == "lost":
        # The exec is ended before the sandbox is stopped.
        assert result.finished.error == "the connection to Docker was lost while the exec ran"
        assert [k[:2] for k in daemon.kills] == [[f"{TOOLBOX}/aims-exec", "-k"]]
        assert daemon.calls.index("POST exec/e2/start") < daemon.calls.index(
            "POST containers/c1/kill"
        )
