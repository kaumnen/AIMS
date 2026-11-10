"""Outbound HTTP shared by the layers: to the control plane and to model APIs."""

import contextlib
import ipaddress
import ssl
from urllib.request import getproxies_environment

import httpx2


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def no_proxy(host: str) -> dict[str, str]:
    """NO_PROXY and no_proxy for a child process: the list urllib reads here, plus `host`."""
    listed = getproxies_environment().get("no")
    value = f"{listed},{host}" if listed else host
    return {"NO_PROXY": value, "no_proxy": value}


def _bypass(host: str, no_proxy: str) -> bool:
    """Loopback, or a host NO_PROXY lists by `*`, by name or a domain it is in, or by an
    address or network it is in."""
    if loopback(host):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for name in filter(None, (e.strip().lstrip(".").lower() for e in no_proxy.split(","))):
        if name == "*" or host == name:
            return True
        if address is None and host.endswith(f".{name}"):
            return True
        with contextlib.suppress(ValueError):
            if address is not None and address in ipaddress.ip_network(name, strict=False):
                return True
    return False


class Outbound(httpx2.AsyncBaseTransport):
    """Sends each request through the proxy that HTTPS_PROXY, HTTP_PROXY or ALL_PROXY names for
    its scheme, or direct to the hosts NO_PROXY lists. Reads the variables in either case, as
    urllib does. Loopback always goes direct, like the runner `aims ui` starts on 127.0.0.1.
    Certificates come from `verify`, else SSL_CERT_FILE or SSL_CERT_DIR, else the system store."""

    def __init__(self, verify: ssl.SSLContext | str | bool = True) -> None:
        self._env = getproxies_environment()
        self._context = httpx2.create_ssl_context(verify=verify)
        self._direct = httpx2.AsyncHTTPTransport(verify=self._context)
        # built on first use: an ALL_PROXY no request needs, like a SOCKS one, must not fail
        self._proxies: dict[str, httpx2.AsyncHTTPTransport] = {}

    def _route(self, url: httpx2.URL) -> httpx2.AsyncHTTPTransport:
        proxy = self._env.get(url.scheme) or self._env.get("all")
        if not proxy or _bypass(url.host, self._env.get("no", "")):
            return self._direct
        if proxy not in self._proxies:
            self._proxies[proxy] = httpx2.AsyncHTTPTransport(
                verify=self._context, proxy=proxy if "://" in proxy else f"http://{proxy}"
            )
        return self._proxies[proxy]

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._route(request.url).handle_async_request(request)

    async def aclose(self) -> None:
        for transport in (self._direct, *self._proxies.values()):
            await transport.aclose()
