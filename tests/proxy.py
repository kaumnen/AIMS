"""A forward proxy on loopback for tests, which also serves as a plain HTTP or HTTPS origin."""

import asyncio
import contextlib
import shutil
import ssl
import subprocess
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ssl_cert_file", "ssl_cert_dir")


@dataclass
class Server:
    """Records the request line of each request it gets. It tunnels a CONNECT to the loopback
    port `tunnel`, or refuses it without one. It answers any other request with `body`."""

    body: bytes = b"[]"
    tunnel: int | None = None
    lines: list[str] = field(default_factory=list)
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line, *headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            self.lines.append(line)
            if line.startswith("CONNECT ") and self.tunnel is not None:
                up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.tunnel)
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
                return
            if line.startswith("CONNECT "):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            sizes = [h.split(":", 1)[1] for h in headers if h.lower().startswith("content-length:")]
            await reader.readexactly(int(sizes[0]) if sizes else 0)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(self.body), self.body)
            )
            await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
            pass
        finally:
            writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


@contextlib.asynccontextmanager
async def serving(server: Server, tls: ssl.SSLContext | None = None) -> AsyncGenerator[Server]:
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0, ssl=tls)
    server.port = listener.sockets[0].getsockname()[1]
    try:
        yield server
    finally:
        listener.close()
        listener.close_clients()
        await listener.wait_closed()


def environ(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Set the proxy and CA variables to `values` only, so the developer's own stay out."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def certificate(folder: Path, host: str) -> tuple[Path, ssl.SSLContext]:
    """A self-signed certificate for `host`, and a server context that presents it."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command")
    cert, key = folder / f"{host}.pem", folder / f"{host}.key"
    args = f"""openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1
        -subj /CN={host} -addext subjectAltName=DNS:{host}
        -addext keyUsage=critical,digitalSignature,keyCertSign"""
    subprocess.run([*args.split(), "-keyout", key, "-out", cert], check=True, capture_output=True)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    return cert, context
