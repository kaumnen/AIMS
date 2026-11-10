"""Outbound HTTP: proxies and CAs from the environment, loopback always direct. Shown here on
the runner's client; test_cli.py and test_models.py show it on the CLI and on model calls."""

from pathlib import Path

import httpx2
import pytest
from proxy import Server, certificate, environ, serving

from aims.net import Outbound
from aims.node.client import ControlClient

pytestmark = pytest.mark.anyio


async def truth(url: str, verify: bool | str = True) -> bytes:
    client = ControlClient(url, "t", runner="r", verify=verify)
    try:
        return await client.run_truth("r1")
    finally:
        await client.aclose()


async def test_a_runner_reaches_the_control_plane_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(Server(body=b"{}")) as proxy:
        environ(monkeypatch, HTTP_PROXY=proxy.url)
        assert await truth("http://cp.example") == b"{}"
    assert proxy.lines == ["GET http://cp.example/runner/v1/runs/r1/truth HTTP/1.1"]


async def test_loopback_goes_direct_whatever_the_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(Server(body=b"{}")) as server:
        environ(monkeypatch, HTTP_PROXY=server.url, HTTPS_PROXY=server.url, ALL_PROXY=server.url)
        for host in ("127.0.0.1", "localhost"):
            assert await truth(f"http://{host}:{server.port}") == b"{}"
    assert server.lines == ["GET /runner/v1/runs/r1/truth HTTP/1.1"] * 2


async def test_no_proxy_hosts_go_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    async with serving(Server(body=b"{}")) as proxy:
        environ(monkeypatch, http_proxy=proxy.url, no_proxy="example.org, .cp.invalid,192.0.2.0/24")
        with pytest.raises(httpx2.ConnectError):
            await truth("http://runner.cp.invalid")
        # TEST-NET-1 is unreachable: direct fails fast or times out
        async with httpx2.AsyncClient(transport=Outbound(), timeout=0.3) as client:
            with pytest.raises(httpx2.TransportError):
                await client.get("http://192.0.2.1:9/")
        assert await truth("http://cp.example") == b"{}"
    assert proxy.lines == ["GET http://cp.example/runner/v1/runs/r1/truth HTTP/1.1"]


async def test_the_tls_ca_wins_over_ssl_cert_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert, tls = certificate(tmp_path, "cp.example")
    other, _ = certificate(tmp_path, "other.example")
    async with (
        serving(Server(body=b"{}"), tls) as origin,
        serving(Server(tunnel=origin.port)) as proxy,
    ):
        environ(monkeypatch, HTTPS_PROXY=proxy.url, SSL_CERT_FILE=str(other))
        assert await truth("https://cp.example", verify=str(cert)) == b"{}"
        with pytest.raises(httpx2.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            await truth("https://cp.example")
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        assert await truth("https://cp.example") == b"{}"
    assert proxy.lines == ["CONNECT cp.example:443 HTTP/1.1"] * 3
    assert origin.lines == ["GET /runner/v1/runs/r1/truth HTTP/1.1"] * 2
