import asyncio
import hashlib
import secrets
import threading
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from aims.config import ConfigError, Layers, Settings, layers, load
from aims.events import Event, LocalEventStore
from aims.models import Delta, DeltaBus, ProfileError
from aims.monitor.config import MonitorConfigError
from aims.prompts import TextError
from aims.run.estimate import History, history
from aims.scenario import Scenario
from aims.server import audit
from aims.server.auth import Auth
from aims.server.db import Job, ServerDB, hash_secret
from aims.server.problems import ProblemException
from aims.wire import (
    STALE_S,
    CommandMessage,
    OfferMessage,
    RunnerHello,
    RunnerInfo,
    RunnerStatus,
    WireDelta,
)

INFLIGHT_CAP = 1024 * 1024
INFLIGHT_CALLS = 1000
INFLIGHT_DONE = 10_000
CODE_TTL_S = 120.0
CODES = 32
GONE = 10_000
SPOOL = "tmp"
"""The data directory's folder for temporary files."""


class LaunchCodes:
    """One-shot sign-in codes for the browser `aims ui` and `aims open` open, so a token never
    reaches the browser. A code works once, for two minutes; only its hash is kept, and only
    the newest `CODES` of them."""

    def __init__(self, ttl: float = CODE_TTL_S) -> None:
        self.ttl = ttl
        self._codes: dict[str, tuple[str, float]] = {}

    def issue(self, token: str) -> str:
        now = time.time()
        live = [(k, v) for k, v in self._codes.items() if v[1] > now]
        self._codes = dict(live[-(CODES - 1) :])
        code = secrets.token_urlsafe(32)
        self._codes[hash_secret(code)] = (token, now + self.ttl)
        return code

    def redeem(self, code: str) -> str | None:
        """The token name behind `code`, which stops working whatever the outcome."""
        found = self._codes.pop(hash_secret(code), None)
        if found is None or found[1] <= time.time():
            return None
        return found[0]


class Changes:
    """A short history of which runs changed, so a stream that lists runs sends only those."""

    def __init__(self, size: int = 10_000) -> None:
        self.version = 0
        self._log: deque[tuple[int, str]] = deque(maxlen=size)
        self._event = asyncio.Event()

    def touch(self, run_id: str) -> None:
        self.version += 1
        self._log.append((self.version, run_id))
        event, self._event = self._event, asyncio.Event()
        event.set()

    def event(self) -> asyncio.Event:
        return self._event

    def since(self, version: int) -> tuple[int, list[str] | None]:
        """Runs changed after `version`, or None when the history no longer reaches back."""
        if version >= self.version:
            return self.version, []
        if not self._log or self._log[0][0] > version + 1:
            return self.version, None
        return self.version, list(dict.fromkeys(r for v, r in self._log if v > version))


class InFlight:
    """Deltas of model calls still streaming, so a viewer who opens a run mid-answer sees the
    answer so far. Each call keeps up to 1 MB; its deltas go once its response is logged, and
    deltas of that call that arrive later, such as its end, are not kept."""

    def __init__(self) -> None:
        self._calls: OrderedDict[tuple[str, str], tuple[list[WireDelta], int]] = OrderedDict()
        self._done: OrderedDict[tuple[str, str], None] = OrderedDict()

    def add(self, deltas: list[WireDelta]) -> None:
        for d in deltas:
            key = (d.run_id, d.call_id)
            if key in self._done:
                continue
            kept, size = self._calls.get(key, ([], 0))
            if size + len(d.text) > INFLIGHT_CAP:
                continue
            kept.append(d)
            self._calls[key] = (kept, size + len(d.text) + 64)
            self._calls.move_to_end(key)
        while len(self._calls) > INFLIGHT_CALLS:
            self._calls.popitem(last=False)

    def drop(self, run_id: str, call_id: str, *, final: bool = True) -> None:
        """Forget the call's deltas; `final` unless the response failed and is retried, whose
        next attempt streams under the same call."""
        self._calls.pop((run_id, call_id), None)
        if not final:
            return
        self._done[(run_id, call_id)] = None
        while len(self._done) > INFLIGHT_DONE:
            self._done.popitem(last=False)

    def of(self, run_ids: set[str]) -> list[WireDelta]:
        return [d for (run, _), (kept, _) in self._calls.items() if run in run_ids for d in kept]


@dataclass(slots=True)
class Connected:
    """A runner the control plane has heard from."""

    name: str
    token: str
    hello: RunnerHello
    status: RunnerStatus | None = None
    seen: float = field(default_factory=time.time)
    streams: int = 0
    feed: asyncio.Queue[CommandMessage | OfferMessage | None] = field(
        default_factory=lambda: asyncio.Queue(10_000)
    )
    offered: set[str] = field(default_factory=set)
    local: bool = False

    @property
    def stale(self) -> bool:
        return time.time() - self.seen > STALE_S

    @property
    def tracer_alive(self) -> bool:
        tracer = self.status.tracer if self.status is not None else self.hello.tracer
        return tracer.alive

    @property
    def envs(self) -> list[str]:
        return self.status.envs if self.status is not None else self.hello.envs

    def free(self) -> int:
        if self.status is not None:
            free = self.status.free
        else:
            free = max(0, self.hello.slots - len(self.hello.jobs))
        return max(0, free - len(self.offered))

    def info(self) -> RunnerInfo:
        status = self.status
        return RunnerInfo(
            name=self.name,
            version=self.hello.version,
            arch=self.hello.arch,
            tracer=status.tracer if status is not None else self.hello.tracer,
            envs=self.envs,
            slots=self.hello.slots,
            free=self.free(),
            running=status.running if status is not None else [],
            connected=self.streams > 0,
            stale=self.stale,
            seen_s=round(time.time() - self.seen, 1),
            local=self.local,
            kinds=list(self.hello.kinds),
        )


def stamp(path: Path) -> tuple[int, int, int] | None:
    """The file's inode, mtime and size, which a write changes; None when it cannot be read."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


class FileCache[T]:
    """What a read made of each file, kept while the file's stamp stays the same. Worker
    threads may share one."""

    def __init__(self) -> None:
        self._held: dict[Path, tuple[tuple[int, int, int], T]] = {}
        self._lock = threading.Lock()

    def __contains__(self, path: object) -> bool:
        return path in self._held

    def get(self, path: Path, read: Callable[[Path], T]) -> T:
        """`read(path)`, or what it returned before when the file has not changed since."""
        now = stamp(path)
        with self._lock:
            held = self._held.get(path)
        if now is not None and held is not None and held[0] == now:
            return held[1]
        value = read(path)
        if now is not None:
            with self._lock:
                self._held[path] = (now, value)
        return value

    def keep(self, paths: Collection[Path]) -> None:
        """Forget every file not in `paths`."""
        with self._lock:
            self._held = {p: v for p, v in self._held.items() if p in paths}


class ConfigCache:
    """The merged config layers, read again whenever a file's stamp changes."""

    def __init__(self, project: Path | None) -> None:
        self.project = project
        self._stamp: tuple[object, ...] | None = None
        self._settings: Settings | None = None
        self._error: str | None = None

    def layers(self) -> Layers:
        return layers(self.project)

    def _key(self) -> tuple[object, ...]:
        found = self.layers()
        return tuple(None if p is None else (str(p), stamp(p)) for p in (found.user, found.project))

    def settings(self) -> Settings:
        key = self._key()
        if key != self._stamp:
            self._stamp = key
            try:
                self._settings, self._error = load(self.project), None
            except (ConfigError, ProfileError, TextError, MonitorConfigError) as e:
                self._settings, self._error = None, str(e)
        if self._settings is None:
            raise ProblemException(422, "Config does not load", self._error, code="config")
        return self._settings


@dataclass(slots=True)
class ServerState:
    data: Path
    store: LocalEventStore
    db: ServerDB
    auth: Auth
    config: ConfigCache
    # the address people open, from `--public-url`; None when the server was given none
    public_url: str | None = None
    bus: DeltaBus = field(default_factory=lambda: DeltaBus(maxsize=8192))
    changes: Changes = field(default_factory=Changes)
    inflight: InFlight = field(default_factory=InFlight)
    runners: dict[str, Connected] = field(default_factory=dict)
    local_runner: str | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    runners_changed: asyncio.Event = field(default_factory=asyncio.Event)
    started: float = field(default_factory=time.time)
    # live UI streams open now
    streams: int = 0
    codes: LaunchCodes = field(default_factory=LaunchCodes)
    # set as the server shuts down, so live streams end instead of holding it open
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _history: tuple[int, History] | None = None
    # called with a run's id once its truth file is kept here
    on_truth: Callable[[str], Awaitable[None]] | None = None
    # held from placing a series' runs to queueing them, so two starts never take one index
    series_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # held from checking that no job uses a run to keeping it as deleted; a queue holds it
    # from checking that no run its jobs read was deleted to adding them
    delete_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # one per run an import writes, held from its checks to its end, so a refused import that
    # undoes a new run never undoes what another import of that run wrote
    import_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = field(
        default_factory=weakref.WeakValueDictionary
    )
    # runs that ended more days ago than this go at start and once a day; None keeps them all
    keep_days: float | None = None
    # the newest runs deleted while this server runs, with their kinds, for live streams
    gone: OrderedDict[str, str] = field(default_factory=OrderedDict)
    # each scenario.yaml the scenario list read: its scenario, or why it does not load
    scenarios: FileCache[Scenario | str] = field(default_factory=FileCache)
    # each model check run the models list read, by run id: the head it was read at, the
    # profiles it checks and its model.checked events
    check_runs: dict[str, tuple[str, list[str], list[Event]]] = field(default_factory=dict)

    def spool(self) -> Path:
        """A private folder in the data directory for request bodies too large for memory, made
        when the server starts. A container's /tmp may be held in memory, so Compose also points
        TMPDIR and SQLITE_TMPDIR here."""
        path = self.data / SPOOL
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    async def history(self) -> History:
        """The runs' history the estimates read, read again once a run changed."""
        version = self.changes.version
        if self._history is None or self._history[0] != version:
            self._history = (version, await history(self.store))
        return self._history[1]

    def note_deleted(self, run_id: str, kind: str) -> None:
        """Tell live streams that a run of `kind` was deleted."""
        self.gone[run_id] = kind
        while len(self.gone) > GONE:
            self.gone.popitem(last=False)
        self.changes.touch(run_id)

    def touch_job(self, job: Job) -> None:
        """Note that a job changed, and for a monitor job the run it watches, whose `reviews`
        count counts it."""
        self.changes.touch(job.run_id)
        origin = job.payload.get("origin")
        if job.payload.get("kind") == "monitor" and isinstance(origin, str):
            self.changes.touch(origin)

    def poke(self) -> None:
        """Ask the dispatcher to look at the queue now."""
        self.wake.set()

    def runner_changed(self) -> None:
        event, self.runners_changed = self.runners_changed, asyncio.Event()
        event.set()

    def publish(self, deltas: list[WireDelta]) -> None:
        self.inflight.add(deltas)
        for d in deltas:
            self.bus.publish(
                Delta(d.run_id, d.agent_id, d.call_id, d.attempt, d.kind, d.text, d.index, d.ms)
            )


async def open_state(
    data: Path,
    *,
    project: Path | None = None,
    auth: bool = True,
    tls: bool = False,
    public_url: str | None = None,
    keep_days: float | None = None,
) -> ServerState:
    """The control plane's stores under `data`: `runs/` (ingest only) and `server.sqlite`,
    with expired sessions and old audit rows dropped. `public_url` is the address people open,
    for links the server writes. Housekeeping deletes runs that ended more than `keep_days`
    ago."""
    db = await ServerDB.open(data / "server.sqlite")
    config = ConfigCache(project)
    try:
        await db.prune_sessions()
        await audit.prune(db, config.layers)
        store = await LocalEventStore.open(data / "runs", ingest_only=True)
    except BaseException:
        await db.close()
        raise
    state = ServerState(
        data=data,
        store=store,
        db=db,
        auth=Auth(db, enabled=auth, tls=tls, scope=cookie_scope(data)),
        config=config,
        public_url=public_url,
        keep_days=keep_days,
    )
    store.listeners.append(state.changes.touch)
    state.spool()
    return state


def cookie_scope(data: Path) -> str:
    return hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:8]


async def close_state(state: ServerState) -> None:
    await state.store.close()
    await state.db.close()
