"""The IMDS sidecar: a small HTTP server sharing one sandbox's network namespace."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiodocker
from aiodocker.containers import DockerContainer

from aims.aio import settle
from aims.sandbox.base import TOOLBOX
from aims.sandbox.containers import create_container, remove_container
from aims.sandbox.execs import open_stream, streamed

log = logging.getLogger("aims.sandbox")
STOP_S = 1
DRAIN_S = 5.0
# The sidecar's own limits. Nothing an agent sends it counts against the agent's sandbox.
SLOTS = 32
IDLE_S = 10.0
PIDS = 64
MEMORY = 128 * 1024 * 1024
CPUS = 0.5
# What the runner keeps of one sidecar's output. A line past LINE bytes is counted, not kept.
# The first RECORDS requests are kept in full. Past them, requests are counted by method, path
# (its first KEY_PATH characters) and status for KEYS such keys. The rest count together. The
# first WARNINGS stderr lines are reported. The rest are counted.
LINE = 64 * 1024
RECORDS = 1_000
KEYS = 100
KEY_PATH = 256
WARNINGS = 20
END_S = 2.0
SERVER = r'''
import http.client, io, json, os, secrets, socket, sys, threading, time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

# Header lines past 8 KB, nginx's default, or more than 32 headers get a 431. http.server reads
# these two at each request, as checked in Python 3.13 and 3.15. Before 3.15 the blank line
# that ends the headers counts as one, so allow one more there.
http.client._MAXLINE = 8192
http.client._MAXHEADERS = 32
try:
    http.client.parse_headers(io.BytesIO(b"A: 1\r\n" * 32 + b"\r\n"))
except http.client.HTTPException:
    http.client._MAXHEADERS = 33
SLOTS = threading.BoundedSemaphore(int(os.environ["AIMS_IMDS_SLOTS"]))
IDLE_S = float(os.environ["AIMS_IMDS_IDLE_S"])
RECORD = 32768
BUSY = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n"
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)
ROLE = os.environ["AIMS_IMDS_ROLE"]
REGION = os.environ["AIMS_IMDS_REGION"]
INSTANCE = os.environ["AIMS_IMDS_INSTANCE"]
ACCOUNT = os.environ["AIMS_IMDS_ACCOUNT"]
AMI = os.environ["AIMS_IMDS_AMI"]
PRIVATE_IP = os.environ["AIMS_IMDS_PRIVATE_IP"]
HOSTNAME = os.environ["AIMS_IMDS_HOSTNAME"]
RESERVATION = os.environ["AIMS_IMDS_RESERVATION"]
CANARY = json.loads(os.environ.get("AIMS_IMDS_CANARY") or "{}")
_lock = threading.Lock()
_token = {"value": "", "expires": 0.0}


def now():
    return datetime.now(UTC)


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def zone():
    return REGION + "a"


def emit(entry):
    line = json.dumps(entry, separators=(",", ":"))
    if len(line) > RECORD and entry.get("headers"):
        # JSON writes a control byte as six, so a record can be about six times its text.
        # One past RECORD keeps no headers, which keeps every line well under what the runner
        # reads.
        removed = sum(len(key) + len(value) for key, value in entry["headers"].items())
        entry["cut"] = entry.get("cut", 0) + removed
        entry["headers"] = {}
        line = json.dumps(entry, separators=(",", ":"))
    with _lock:
        print(line, flush=True)


def cut(text, limit):
    """`text` cut to `limit` characters, and how many were cut."""
    if text is None or len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def linger(sock, seconds):
    """Close `sock` after reading what the client still sends, for up to `seconds`. Closing
    with unread bytes sends a reset, which can drop the answer before the client reads it."""
    try:
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + seconds
        read = 0
        while read < 65536:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            data = sock.recv(4096)
            if not data:
                break
            read += len(data)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def credentials():
    return {
        "Code": "Success",
        "LastUpdated": stamp(now()),
        "Type": "AWS-HMAC",
        "AccessKeyId": CANARY.get("id", ""),
        "SecretAccessKey": CANARY.get("secret", ""),
        "Token": CANARY.get("token", ""),
        "Expiration": stamp(now() + timedelta(hours=6)),
    }


def document():
    return {
        "accountId": ACCOUNT,
        "architecture": "x86_64",
        "availabilityZone": zone(),
        "billingProducts": None,
        "devpayProductCodes": None,
        "marketplaceProductCodes": None,
        "imageId": AMI,
        "instanceId": INSTANCE,
        "instanceType": "m6i.large",
        "kernelId": None,
        "pendingTime": stamp(now() - timedelta(days=1)),
        "privateIp": PRIVATE_IP,
        "ramdiskId": None,
        "region": REGION,
        "version": "2017-09-30",
    }


LISTING = """ami-id
ami-launch-index
ami-manifest-path
block-device-mapping/
events/
hostname
iam/
identity-credentials/
instance-action
instance-id
instance-life-cycle
instance-type
local-hostname
local-ipv4
mac
metrics/
network/
placement/
profile
public-hostname
public-ipv4
public-keys/
reservation-id
security-groups
services/
system"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_S

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        # A request that fails to parse must not log the last one's path or headers.
        self.path = None
        self.headers = None
        super().handle_one_request()

    def send_response(self, code, message=None):
        self._record(code)
        super().send_response(code, message)

    def _record(self, status):
        method, removed = cut(self.command, 32)
        path, n = cut(self.path, 1024)
        removed += n
        headers = {}
        for key, value in (self.headers or {}).items():
            key, a = cut(key, 128)
            value, b = cut(value, 512)
            headers[key] = value
            removed += a + b
        entry = {
            "time": stamp(now()),
            "method": method,
            "path": path,
            "headers": headers,
            "status": int(status),
        }
        if removed:
            entry["cut"] = removed
        emit(entry)

    def _send(self, code, body=b"", content_type="text/plain", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        token = self.headers.get("X-aws-ec2-metadata-token")
        if token is None:
            return True
        with _lock:
            return token == _token["value"] and time.time() < _token["expires"]

    def do_PUT(self):
        if urlsplit(self.path).path.rstrip("/") != "/latest/api/token":
            self._send(404, "Not Found")
            return
        raw = self.headers.get("X-aws-ec2-metadata-token-ttl-seconds")
        try:
            ttl = int(raw)
        except (TypeError, ValueError):
            self._send(400, "Missing or invalid X-aws-ec2-metadata-token-ttl-seconds")
            return
        if not 1 <= ttl <= 21600:
            self._send(400, "TTL must be between 1 and 21600 seconds")
            return
        token = secrets.token_urlsafe(32)
        with _lock:
            _token["value"], _token["expires"] = token, time.time() + ttl
        self._send(200, token, extra={"X-aws-ec2-metadata-token-ttl-seconds": str(ttl)})

    def do_GET(self):
        if not self._authorized():
            self._send(401, "Unauthorized", extra={"X-aws-ec2-metadata-token": ""})
            return
        path = urlsplit(self.path).path
        if path in ("/latest/meta-data", "/latest/meta-data/"):
            self._send(200, LISTING + "\n")
            return
        simple = {
            "/latest/meta-data/instance-id": INSTANCE,
            "/latest/meta-data/instance-type": "m6i.large",
            "/latest/meta-data/placement/region": REGION,
            "/latest/meta-data/placement/availability-zone": zone(),
            "/latest/meta-data/hostname": HOSTNAME,
            "/latest/meta-data/local-hostname": HOSTNAME,
            "/latest/meta-data/local-ipv4": PRIVATE_IP,
            "/latest/meta-data/ami-id": AMI,
            "/latest/meta-data/reservation-id": RESERVATION,
            "/latest/meta-data/security-groups": "default",
        }
        if path in simple:
            self._send(200, simple[path] + "\n")
            return
        if path.rstrip("/") == "/latest/meta-data/iam/security-credentials":
            self._send(200, ROLE + "\n")
            return
        prefix = "/latest/meta-data/iam/security-credentials/"
        if path.startswith(prefix):
            if path[len(prefix) :].strip("/") != ROLE:
                self._send(404, "Not Found")
                return
            self._send(200, json.dumps(credentials()), "application/json")
            return
        if path.rstrip("/") == "/latest/dynamic/instance-identity/document":
            self._send(200, json.dumps(document()), "application/json")
            return
        self._send(404, "Not Found")


class Server(HTTPServer):
    """One thread per connection, at most SLOTS at once. A connection past them gets a 503."""

    allow_reuse_address = True
    # socketserver's default listen queue of 5 dropped connects that came at once, and the
    # client sent each again after 1 s. 128 holds a burst well past SLOTS.
    request_queue_size = 128

    def process_request(self, request, client_address):
        if not SLOTS.acquire(blocking=False):
            self._refuse(request)
            return
        try:
            thread = threading.Thread(target=self._serve, args=(request, client_address))
            thread.daemon = True
            thread.start()
        except Exception:
            SLOTS.release()
            self._refuse(request)

    def _serve(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            SLOTS.release()

    def _refuse(self, request):
        """Answer a 503 and close. This runs in the accept loop, so it waits at most 10 ms for
        the request: reading it first keeps the close from being a reset."""
        emit({"time": stamp(now()), "status": 503, "refused": True})
        try:
            request.settimeout(0.01)
            request.recv(65536)
        except OSError:
            pass
        try:
            request.settimeout(1)
            request.sendall(BUSY)
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def shutdown_request(self, request):
        linger(request, 0.5)

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        print(f"{type(error).__name__}: {error}"[:500], file=sys.stderr, flush=True)


Server(("169.254.169.254", 80), Handler).serve_forever()
'''


EXAMPLES = frozenset(
    {
        "123456789012",
        "ami-0123456789abcdef0",
        "i-0123456789abcdef0",
        "r-0123456789abcdef0",
        "10.0.0.10",
    }
)


@dataclass(frozen=True, slots=True)
class Identity:
    """The instance identity one IMDS plant serves for one run."""

    account: str
    ami: str
    instance: str
    private_ip: str
    reservation: str

    def hostname(self, region: str) -> str:
        """The private DNS name AWS builds from the private IP."""
        return f"ip-{self.private_ip.replace('.', '-')}.{region}.compute.internal"


def identity(seed: bytes, plant: str) -> Identity:
    """The identity of plant `plant` for the run whose canary seed is `seed`.

    Every run draws its own values and a fork, which reuses the origin's seed, serves the
    same ones. The AWS documentation examples are never served.
    """
    account = _account(_rng(seed, plant, "account"))
    ami = _prefixed(_rng(seed, plant, "ami"), "ami-")
    instance = _prefixed(_rng(seed, plant, "instance"), "i-")
    reservation = _prefixed(_rng(seed, plant, "reservation"), "r-")
    return Identity(
        account=account,
        ami=ami,
        instance=instance,
        private_ip=_private_ip(_rng(seed, plant, "private-ip")),
        reservation=reservation,
    )


def _rng(seed: bytes, plant: str, part: str) -> random.Random:
    data = seed + b"\0" + plant.encode() + b"\0" + part.encode()
    return random.Random(int.from_bytes(hashlib.sha256(data).digest(), "big"))


def _account(rng: random.Random) -> str:
    value = str(rng.randrange(10**11, 10**12))
    while value in EXAMPLES:
        value = str(rng.randrange(10**11, 10**12))
    return value


def _prefixed(rng: random.Random, prefix: str) -> str:
    value = prefix + "".join(rng.choice("0123456789abcdef") for _ in range(17))
    while value in EXAMPLES:
        value = prefix + "".join(rng.choice("0123456789abcdef") for _ in range(17))
    return value


def _private_ip(rng: random.Random) -> str:
    value = f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
    while value in EXAMPLES:
        value = f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, separators=(",", ":")).encode() + b"\n"


@dataclass(slots=True)
class _Span:
    """How many of one kind the log left out, and when the first and the last came."""

    count: int = 0
    first: str | None = None
    last: str | None = None

    def add(self, when: str | None) -> None:
        self.count += 1
        if when is not None:
            if self.first is None:
                self.first = when
            self.last = when

    def dump(self) -> dict[str, Any]:
        return {"count": self.count, "first": self.first, "last": self.last}


type _Key = tuple[str | None, str | None, int | None]


@dataclass(slots=True)
class _Tally:
    """What one sidecar's request log holds: the first RECORDS requests in full, then counts
    by method, path and status, which a summary line gives at the end."""

    kept: int = 0
    keys: dict[_Key, _Span] = field(default_factory=dict)
    other: _Span = field(default_factory=_Span)
    refused: _Span = field(default_factory=_Span)
    oversize: _Span = field(default_factory=_Span)
    invalid: int = 0
    stderr: int = 0

    def take(self, line: bytes) -> bool:
        """Whether to write this line of the sidecar's stdout. One it does not write, it counts."""
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict) or not isinstance(entry.get("time"), str):
            self.invalid += 1
            return False
        when: str = entry["time"]
        if entry.get("refused") is True:
            self.refused.add(when)
            return False
        if "method" not in entry:
            self.invalid += 1
            return False
        if self.kept < RECORDS:
            self.kept += 1
            return True
        method, path, status = entry.get("method"), entry.get("path"), entry.get("status")
        key = (
            method if isinstance(method, str) else None,
            path[:KEY_PATH] if isinstance(path, str) else None,
            status if isinstance(status, int) else None,
        )
        span = self.keys.get(key)
        if span is None:
            span = self.other if len(self.keys) >= KEYS else self.keys.setdefault(key, _Span())
        span.add(when)
        return False

    def summary(self) -> dict[str, Any] | None:
        """The summary line, or None when the log left nothing out."""
        dropped = sum(span.count for span in self.keys.values()) + self.other.count
        out: dict[str, Any] = {"kept": self.kept, "dropped": dropped}
        if self.keys:
            out["paths"] = [
                {"method": method, "path": path, "status": status, **span.dump()}
                for (method, path, status), span in self.keys.items()
            ]
        for name, span in (
            ("other", self.other),
            ("refused", self.refused),
            ("oversize", self.oversize),
        ):
            if span.count:
                out[name] = span.dump()
        if self.invalid:
            out["invalid"] = self.invalid
        if self.stderr:
            out["stderr"] = self.stderr
        if len(out) == 2:
            return None
        return {"time": _now(), "summary": out}


@dataclass(slots=True)
class _Lines:
    """Cuts a byte stream into lines. A line past LINE bytes is never held whole: it comes out
    once as None and the rest of it is skipped."""

    held: bytearray = field(default_factory=bytearray)
    skipping: bool = False

    def feed(self, data: bytes) -> list[bytes | None]:
        out: list[bytes | None] = []
        self.held += data
        while (end := self.held.find(b"\n")) >= 0:
            if self.skipping:
                self.skipping = False
            elif end > LINE:
                out.append(None)
            else:
                out.append(bytes(self.held[:end]))
            del self.held[: end + 1]
        if len(self.held) > LINE:
            if not self.skipping:
                out.append(None)
                self.skipping = True
            self.held.clear()
        return out

    def rest(self) -> bytes | None:
        """A last line with no newline, once."""
        rest = None if self.skipping or not self.held else bytes(self.held)
        self.held.clear()
        self.skipping = False
        return rest


async def _follow(
    stream: Any, log_path: str, name: str, container: Any, closing: asyncio.Event
) -> None:
    """Write the sidecar's stdout to `log_path` within the caps above and report its stderr as
    warnings. Output that ends before `closing` is set means the sidecar stopped on its own:
    the log gets a `stopped` line and the runner a warning."""
    tally = _Tally()
    out, err = _Lines(), _Lines()

    def warn(line: bytes | None) -> None:
        tally.stderr += 1
        if tally.stderr > WARNINGS:
            return
        if line is None:
            text = f"a line over {LINE // 1024} KB was left out"
        else:
            text = line.decode(errors="replace")
        log.warning("IMDS sidecar %s: %s", name, text)

    def write(fd: int, line: bytes | None) -> None:
        if line is None:
            tally.oversize.add(_now())
        elif tally.take(line):
            os.write(fd, line + b"\n")

    def flush(fd: int) -> None:
        if (rest := out.rest()) is not None:
            write(fd, rest)
        if (rest := err.rest()) is not None:
            warn(rest)

    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 2:
                    for line in err.feed(message.data):
                        warn(line)
                else:
                    for line in out.feed(message.data):
                        write(fd, line)
            flush(fd)
            if not closing.is_set():
                ended = await _ended(container)
                os.write(fd, _line(ended))
                if "lost" in ended:
                    log.warning(
                        "IMDS sidecar %s: its output ended while it still runs; "
                        "later requests are not recorded",
                        name,
                    )
                else:
                    code = ended["stopped"]["exit"]
                    log.warning(
                        "IMDS sidecar %s stopped during the run with %s%s; "
                        "the metadata service no longer answers",
                        name,
                        "an unknown exit" if code is None else f"exit {code}",
                        ", out of memory" if ended["stopped"]["oom"] else "",
                    )
        finally:
            flush(fd)
            if tally.stderr > WARNINGS:
                log.warning(
                    "IMDS sidecar %s: %d more stderr lines not shown",
                    name,
                    tally.stderr - WARNINGS,
                )
            if (summary := tally.summary()) is not None:
                os.write(fd, _line(summary))
            os.fsync(fd)
            os.close(fd)
    finally:
        await stream.close()


async def _ended(container: Any) -> dict[str, Any]:
    """The log line for a sidecar whose output ended before its close. Its exit can show a
    moment after its output ends, so this waits up to END_S for it."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(END_S):
            await container.wait()
    state: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        state = (await container.show()).get("State") or {}
    if state.get("Running"):
        return {"time": _now(), "lost": True}
    stopped = {"exit": state.get("ExitCode"), "oom": state.get("OOMKilled")}
    return {"time": _now(), "stopped": stopped}


def _check_follower(follower: asyncio.Task[None], name: str) -> None:
    if not follower.done():
        return
    if follower.cancelled():
        raise RuntimeError(f"the IMDS log follower for {name} stopped")
    error = follower.exception()
    if error is None:
        raise RuntimeError(f"the IMDS log follower for {name} stopped")
    raise RuntimeError(f"the IMDS log follower for {name} stopped: {error}") from error


@dataclass(slots=True)
class ImdsServer:
    """One sidecar container serving IMDS to one sandbox; close deletes it."""

    docker: aiodocker.Docker
    container: DockerContainer
    log: str
    follower: asyncio.Task[None]
    # Set before the stop, so the follower tells a close from the sidecar stopping on its own.
    closing: asyncio.Event

    async def close(self) -> None:
        """Stop the server, read its log to the end and remove its container. A cancel waits
        for that and is raised then."""
        await settle(self._close())

    async def _close(self) -> None:
        self.closing.set()
        with contextlib.suppress(aiodocker.DockerError):
            await self.container.stop(t=STOP_S)
        done, _ = await asyncio.wait({self.follower}, timeout=DRAIN_S)
        if not done:
            log.warning(
                "the IMDS log follower for %s did not finish; some output may be missing",
                self.log,
            )
            self.follower.cancel()
        try:
            await self.follower
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("the IMDS log follower for %s stopped: %s", self.log, e)
        with contextlib.suppress(aiodocker.DockerError):
            await self.container.delete(force=True)


async def start(
    docker: aiodocker.Docker,
    *,
    sandbox_container: DockerContainer,
    image: str,
    toolbox_volume: str,
    labels: dict[str, str],
    name: str,
    role: str,
    region: str,
    canary: dict[str, str] | None,
    log_path: str,
    identity: Identity,
) -> ImdsServer:
    """Start the sidecar and wait until its listener is up."""
    env = {
        "AIMS_IMDS_ROLE": role,
        "AIMS_IMDS_REGION": region,
        "AIMS_IMDS_CANARY": "" if canary is None else json.dumps(canary),
        "AIMS_IMDS_INSTANCE": identity.instance,
        "AIMS_IMDS_ACCOUNT": identity.account,
        "AIMS_IMDS_AMI": identity.ami,
        "AIMS_IMDS_PRIVATE_IP": identity.private_ip,
        "AIMS_IMDS_HOSTNAME": identity.hostname(region),
        "AIMS_IMDS_RESERVATION": identity.reservation,
        "AIMS_IMDS_SLOTS": str(SLOTS),
        "AIMS_IMDS_IDLE_S": str(IDLE_S),
        "AIMS_IMDS_SERVER": SERVER,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = (
        f"{TOOLBOX}/busybox ip link set lo up; "
        f"{TOOLBOX}/busybox ip addr add 169.254.169.254/32 dev lo 2>/dev/null || true; "
        'exec python3 -u -B -c "$AIMS_IMDS_SERVER"'
    )
    config: dict[str, Any] = {
        "Image": image,
        "Entrypoint": [f"{TOOLBOX}/busybox"],
        "Cmd": ["sh", "-c", script],
        "User": "0:0",
        "WorkingDir": "/",
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {**labels, "aims.role": "imds"},
        "HostConfig": {
            "NetworkMode": f"container:{sandbox_container.id}",
            "CapDrop": ["ALL"],
            "CapAdd": ["NET_ADMIN", "NET_BIND_SERVICE"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": PIDS,
            "Memory": MEMORY,
            "MemorySwap": MEMORY,
            "NanoCpus": int(CPUS * 1e9),
            "ReadonlyRootfs": True,
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": toolbox_volume,
                    "Target": TOOLBOX,
                    "ReadOnly": True,
                }
            ],
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m"}},
        },
    }
    container = await create_container(docker, config=config, name=name)
    attached: Any = None
    follower: asyncio.Task[None] | None = None
    closing = asyncio.Event()
    try:
        attached = await open_stream(container.attach(stdout=True, stderr=True))
        follower = asyncio.create_task(_follow(attached, log_path, name, container, closing))
        await container.start()
        _check_follower(follower, name)
        probe = (
            "i=0; while [ $i -lt 50 ]; do "
            f"{TOOLBOX}/busybox grep -q ':0050 ' /proc/net/tcp && exit 0; "
            f"{TOOLBOX}/busybox sleep 0.1; i=$((i+1)); done; exit 1"
        )
        exec_ = await container.exec(
            [f"{TOOLBOX}/busybox", "sh", "-c", probe], stdout=True, stderr=True
        )
        stream = await open_stream(exec_.start(detach=False))
        try:
            while await stream.read_out() is not None:
                pass
        finally:
            await stream.close()
        state = await exec_.inspect()
        if state.get("ExitCode") != 0:
            with streamed(f"the log of IMDS sidecar {name}"):
                logs = await container.log(stdout=True, stderr=True)
            raise RuntimeError(f"the IMDS listener did not start: {''.join(logs)[-2000:]}")
        _check_follower(follower, name)
    except BaseException:
        await settle(_undo(container, attached, follower))
        raise
    assert follower is not None
    return ImdsServer(docker, container, log_path, follower, closing)


async def _undo(
    container: DockerContainer, attached: Any, follower: asyncio.Task[None] | None
) -> None:
    """Remove what a start that failed made. It runs under `settle`, so a cancel of the caller
    never lands in here."""
    if follower is not None:
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await follower
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.close()
    await remove_container(container)
