"""Outbound HTTP shared by the layers: to the control plane and to model APIs."""

import asyncio
import contextlib
import ipaddress
import os
import socket
import ssl
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Iterable,
    Sequence,
)
from typing import Any, cast
from urllib.request import getproxies_environment

import httpcore2
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
        if isinstance(verify, str):
            # httpx2 deprecates a path for `verify`; this loads it as httpx2 did
            if os.path.isdir(verify):
                verify = ssl.create_default_context(capath=verify)
            else:
                verify = ssl.create_default_context(cafile=verify)
        self._context = httpx2.create_ssl_context(verify=verify)
        self._direct: httpx2.AsyncBaseTransport = httpx2.AsyncHTTPTransport(verify=self._context)
        # built on first use: an ALL_PROXY no request needs, like a SOCKS one, must not fail
        self._proxies: dict[str, httpx2.AsyncHTTPTransport] = {}

    def _route(self, url: httpx2.URL) -> httpx2.AsyncBaseTransport:
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


# Cloud metadata services outside the link-local ranges: AWS over IPv6, Alibaba and Azure.
_METADATA = frozenset(
    ipaddress.ip_address(a) for a in ("fd00:ec2::254", "100.100.100.200", "168.63.129.16")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_NAT64 = ipaddress.ip_network("64:ff9b:1::/48")
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _inner(address: Address) -> list[ipaddress.IPv4Address]:
    """The IPv4 addresses an IPv6 address may carry: mapped, 6to4 or NAT64. A local NAT64
    prefix may be 48, 56, 64 or 96 bits long, so each place RFC 6052 puts the IPv4 address
    for those lengths counts."""
    if isinstance(address, ipaddress.IPv4Address):
        return []
    if address.ipv4_mapped is not None:
        return [address.ipv4_mapped]
    if address.sixtofour is not None:
        return [address.sixtofour]
    n = int(address)
    if address in _NAT64:
        return [ipaddress.IPv4Address(n & 0xFFFFFFFF)]
    if address in _LOCAL_NAT64:
        return [
            ipaddress.IPv4Address(v)
            for v in (
                ((n >> 64) & 0xFFFF) << 16 | (n >> 40) & 0xFFFF,
                ((n >> 64) & 0xFF) << 24 | (n >> 32) & 0xFFFFFF,
                (n >> 24) & 0xFFFFFFFF,
                n & 0xFFFFFFFF,
            )
        ]
    return []


def refused(address: Address) -> bool:
    """A link-local or cloud metadata address, also as an IPv6 address that carries one."""
    return any(a.is_link_local or a in _METADATA for a in (address, *_inner(address)))


class Refused(Exception):
    """A host that resolves to an address `refused` names."""


async def _resolve(host: str, port: int) -> list[Address]:
    """The addresses `host` resolves to, in the resolver's order, without repeats. Raises
    OSError when it resolves to none."""
    found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not found:
        raise socket.gaierror(socket.EAI_NONAME, f"{host} resolves to no address")
    # The scope of an IPv6 address follows a `%`.
    addresses = (ipaddress.ip_address(str(info[4][0]).partition("%")[0]) for info in found)
    return list(dict.fromkeys(addresses))


# RFC 8305's delay between connection attempts.
ATTEMPT_DELAY = 0.25


def interleave(addresses: Sequence[Address]) -> list[Address]:
    """`addresses` with IPv6 and IPv4 taking turns, from the family of the first, each in the
    resolver's order, as RFC 8305 orders them."""
    first = [a for a in addresses if a.version == addresses[0].version] if addresses else []
    other = [a for a in addresses if addresses and a.version != addresses[0].version]
    turns = [a for pair in zip(first, other, strict=False) for a in pair]
    return turns + first[len(other) :] + other[len(first) :]


async def race[T](
    addresses: Sequence[Address],
    connect: Callable[[Address], Coroutine[Any, Any, T]],
    close: Callable[[T], Awaitable[object]],
    delay: float = ATTEMPT_DELAY,
) -> T:
    """The first of `connect(address)` to succeed. Attempts start in order, each `delay` after
    the one before or as soon as every running one has failed, as RFC 8305 has it. Once one
    succeeds the others are cancelled, and one that succeeded too is closed. Raises the last
    failure when all fail."""
    queue = list(addresses)
    running: set[asyncio.Task[T]] = set()
    failure: BaseException | None = None
    try:
        while queue or running:
            if queue:
                running.add(asyncio.create_task(connect(queue.pop(0))))
            done, running = await asyncio.wait(
                running, timeout=delay if queue else None, return_when=asyncio.FIRST_COMPLETED
            )
            won: list[T] = []
            for task in done:
                if (error := task.exception()) is not None:
                    failure = error
                else:
                    won.append(task.result())
            for extra in won[1:]:
                await close(extra)
            if won:
                return won[0]
        if failure is None:
            raise ValueError("no address to connect to")
        raise failure
    finally:
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)
            for task in running:
                if not task.cancelled() and task.exception() is None:
                    await close(task.result())


class _Checked(httpcore2.AsyncNetworkBackend):
    """Connects to a host only when no address it resolves to is `refused`, and then only to
    those addresses, so a second lookup cannot swap one in. httpcore2's own backend opens each
    connection."""

    def __init__(self) -> None:
        # anyio comes with httpx2, so the backend is the real one, not httpcore2's stand-in.
        self._inner = cast(httpcore2.AsyncNetworkBackend, httpcore2.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109  httpcore2's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        connect = self._inner.connect_tcp

        async def attempt(address: Address) -> httpcore2.AsyncNetworkStream:
            return await connect(str(address), port, None, local_address, socket_options)

        async def close(stream: httpcore2.AsyncNetworkStream) -> None:
            await stream.aclose()

        try:
            async with asyncio.timeout(timeout):
                try:
                    addresses = await _resolve(host, port)
                except OSError as e:
                    raise httpcore2.ConnectError(f"{host} does not resolve: {e}") from None
                if any(refused(a) for a in addresses):
                    raise Refused(f"{host} resolves to a link-local or cloud metadata address")
                return await race(interleave(addresses), attempt, close)
        except TimeoutError:
            raise httpcore2.ConnectTimeout(f"{host} did not answer in {timeout:g} s") from None

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109  httpcore2's interface
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise httpcore2.ConnectError("a typed URL does not reach a Unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


# httpcore2's errors as httpx2's, each before the ones it is a kind of.
_ERRORS: tuple[tuple[type[Exception], type[httpx2.HTTPError]], ...] = (
    (httpcore2.ConnectTimeout, httpx2.ConnectTimeout),
    (httpcore2.ReadTimeout, httpx2.ReadTimeout),
    (httpcore2.WriteTimeout, httpx2.WriteTimeout),
    (httpcore2.PoolTimeout, httpx2.PoolTimeout),
    (httpcore2.TimeoutException, httpx2.TimeoutException),
    (httpcore2.ConnectError, httpx2.ConnectError),
    (httpcore2.ReadError, httpx2.ReadError),
    (httpcore2.WriteError, httpx2.WriteError),
    (httpcore2.NetworkError, httpx2.NetworkError),
    (httpcore2.ProxyError, httpx2.ProxyError),
    (httpcore2.UnsupportedProtocol, httpx2.UnsupportedProtocol),
    (httpcore2.LocalProtocolError, httpx2.LocalProtocolError),
    (httpcore2.RemoteProtocolError, httpx2.RemoteProtocolError),
    (httpcore2.ProtocolError, httpx2.ProtocolError),
)


@contextlib.contextmanager
def _as_httpx2() -> Generator[None]:
    """Raise an httpcore2 error as the httpx2 error of the same kind."""
    try:
        yield
    except Exception as e:
        for theirs, ours in _ERRORS:
            if isinstance(e, theirs):
                raise ours(str(e)) from e
        raise


class _Body(httpx2.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _as_httpx2():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        # httpcore2's body releases its connection on aclose.
        if (close := getattr(self._stream, "aclose", None)) is not None:
            await close()


class _CheckedTransport(httpx2.AsyncBaseTransport):
    """An httpx2 transport over an httpcore2 pool that opens connections through _Checked,
    with httpx2's default limits. httpx2's own transport takes no network backend."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=context,
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=_Checked(),
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if not isinstance(request.stream, httpx2.AsyncByteStream):
            raise TypeError("an async transport takes an async request body")
        url = request.url
        sent = httpcore2.Request(
            method=request.method,
            url=httpcore2.URL(
                scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _as_httpx2():
            answer = await self._pool.handle_async_request(sent)
        if not isinstance(answer.stream, AsyncIterable):
            raise TypeError("an async pool sent a body that is not async")
        return httpx2.Response(
            status_code=answer.status,
            headers=answer.headers,
            stream=_Body(answer.stream),
            extensions=answer.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class Guarded(Outbound):
    """Outbound for a URL someone typed: refuses a host when any address it resolves to is
    link-local or a cloud metadata service, such as 169.254.169.254. The check runs as each
    connection opens, which then goes only to the addresses it checked, so a second lookup
    cannot swap one in. Through a proxy the proxy resolves the host again; the check still
    runs first, unless this host cannot resolve the name, as on a host that reaches out only
    through its proxy. Loopback and private addresses pass, for model servers on the same
    host or network."""

    def __init__(self, verify: ssl.SSLContext | str | bool = True) -> None:
        super().__init__(verify)
        self._direct = _CheckedTransport(self._context)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        route = self._route(request.url)
        if route is not self._direct:
            url = request.url
            host = url.raw_host.decode("ascii")
            try:
                addresses = await _resolve(host, url.port or (443 if url.scheme == "https" else 80))
            except OSError:
                return await route.handle_async_request(request)
            if any(refused(a) for a in addresses):
                raise Refused(f"{host} resolves to a link-local or cloud metadata address")
        return await route.handle_async_request(request)
