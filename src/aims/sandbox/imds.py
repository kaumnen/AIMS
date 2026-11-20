"""The IMDS sidecar: a small HTTP server sharing one sandbox's network namespace."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass
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
        path, removed = cut(self.path, 1024)
        headers = {}
        for key, value in (self.headers or {}).items():
            key, a = cut(key, 128)
            value, b = cut(value, 512)
            headers[key] = value
            removed += a + b
        entry = {
            "time": stamp(now()),
            "method": self.command,
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


async def _follow(stream: Any, log_path: str, name: str) -> None:
    """Append the sidecar's stdout to `log_path`; report its stderr as warnings."""
    out = bytearray()
    err = bytearray()
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 2:
                    err += message.data
                    while (end := err.find(b"\n")) >= 0:
                        line = bytes(err[:end]).decode(errors="replace")
                        log.warning("IMDS sidecar %s: %s", name, line)
                        del err[: end + 1]
                else:
                    out += message.data
                    while (end := out.find(b"\n")) >= 0:
                        os.write(fd, out[: end + 1])
                        del out[: end + 1]
        finally:
            if out:
                if not out.endswith(b"\n"):
                    out += b"\n"
                os.write(fd, out)
            if err:
                line = bytes(err).decode(errors="replace").rstrip("\n")
                log.warning("IMDS sidecar %s: %s", name, line)
            os.fsync(fd)
            os.close(fd)
    finally:
        await stream.close()


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

    async def close(self) -> None:
        """Stop the server, read its log to the end and remove its container. A cancel waits
        for that and is raised then."""
        await settle(self._close())

    async def _close(self) -> None:
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
    try:
        attached = await open_stream(container.attach(stdout=True, stderr=True))
        follower = asyncio.create_task(_follow(attached, log_path, name))
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
    return ImdsServer(docker, container, log_path, follower)


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
