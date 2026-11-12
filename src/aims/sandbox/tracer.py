import asyncio
import contextlib
import dataclasses
import json
import logging
import posixpath
import re
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.execs import Exec as DockerExec
from pydantic import JsonValue

from aims.aio import shielded
from aims.sandbox.base import (
    TOOLBOX,
    Cache,
    Close,
    Denied,
    Exec,
    Exit,
    File,
    Fork,
    Ipc,
    Kernel,
    Lost,
    Mem,
    PreExec,
    Record,
    Setattr,
    Sync,
)
from aims.sandbox.containers import create_container, remove_container
from aims.sandbox.execs import exec_output
from aims.sandbox.images import ensure_image
from aims.sandbox.trace import (
    Delete,
    Dropped,
    EnvChunk,
    ExecPart,
    Gap,
    Garbled,
    Identity,
    Link,
    Mkdir,
    Mounts,
    Note,
    RawCache,
    RawClose,
    RawDenied,
    RawExec,
    RawFifo,
    RawMmap,
    RawSetattr,
    RawSync,
    Ready,
    Rename,
    Sequence,
    Symlink,
    Tick,
    Walk,
    parse,
)

log = logging.getLogger("aims.sandbox")

QUEUE = 100_000
MEMORY = 4096
READY_S = 120.0
CONFIRM_S = 5.0
PMU_S = 60
SKIPS_S = 10
# The kernel skips a probe while a run of the same probe is in progress on that cpu. With
# preemptible RCU (Ubuntu's generic kernel, for one) that includes a run preempted midway, so
# records go missing with no sequence hole. Each program's fd in bpftrace's fdinfo carries its
# count.
# The kernel's count of each record stream follows as `C <uptime> <key hex>:<value hex>...`,
# timed after the dump so every count was reached by then. The map is bpftrace's own, $m: every
# tracer on the host has one by that name. The raw bytes are kept: bpftool's formatted keys
# repeat an empty field name.
SKIP_READ = (
    'u=$(cat /proc/uptime); echo "T ${u%% *}"; '
    'grep -H -E "^(prog_id|recursion_misses):" /proc/$p/fdinfo/* 2>/dev/null; '
    "c=$(bpftool -j map dump id $m 2>/dev/null "
    r"""| grep -o '"key":\[[^]]*\],"value":\[[^]]*\]' """
    r"""| sed 's/"0x//g; s/[",]//g; s/key:\[//; s/\]value:\[/:/; s/\]//'); """
    "u=$(cat /proc/uptime); echo C ${u%% *} $c"
)
SKIPS = (
    "p=$1; mountpoint -q /sys/kernel/debug "
    "|| mount -t debugfs nodev /sys/kernel/debug 2>/dev/null; "
    # Only preemptible RCU defines __rcu_read_lock; without it a probe runs preemption off.
    "if grep -qw __rcu_read_lock /proc/kallsyms; then r=1; else r=0; fi; "
    'echo "H $r $(cat /sys/kernel/debug/sched/preempt 2>/dev/null)"; '
    'echo "N $(bpftool -j prog show 2>/dev/null)"; '
    'm=$(for i in $(grep -h "^map_id:" /proc/$p/fdinfo/* 2>/dev/null | cut -f2 | sort -u); '
    'do bpftool map show id $i 2>/dev/null | grep -qw "name AT_seq" && echo $i; done); '
    'echo "S $m"; '
    f"while [ -d /proc/$p ]; do {SKIP_READ}; echo .; sleep 1; done"
)
PROBE_INDEX = re.compile(r"_\d+$")
# bpftrace prints a loss once its ring has drained, within a tick of the last record before it.
# The hole shows at its stream's next record and expires at the first tick more than a second
# after that. A stream's last records show in the next count reading, a second later at most,
# once its cpu's records are read a second past the reading: every cpu ticks in phase, a little
# under a second apart, so that can take two ticks.
RIPE_US = 4_500_000
# An exited pid stays routable this long for the records its exit leaves, such as closes.
GONE_US = 10_000_000
EXIT_GRACE_S = 0.05
UNKNOWN = "kernel records of an unknown process"
OWN_GAP = "kernel record gap in the tracer's own records: heartbeats and OOM kills"
UNHANDLED = "kernel records the tracer's reader failed on"
# How long a closing sandbox waits for its streams' last holes and counter readings to settle.
SETTLE_S = 3.0
# Compile warnings bpftrace gives for this script on every start, each followed by the source
# line and a marker line under it; they say nothing about a run.
KNOWN_WARNINGS = frozenset({"Addrspace mismatch"})
DIAGNOSTIC = re.compile(r"\S+:\d+:\d+(?:-\d+)?: (WARNING|ERROR): (.*)")
MARKER = re.compile(r"\s*[~^]+\s*")
# FMODE_BACKING is bit 24 from 6.14 and in stable trees that took that change (6.12.y did, 6.6.y
# did not), else bit 25. Below 6.14 a backing file the tracer's own exec opens tells: bits 24 to
# 26 of its mode are 1, 5 or 7 with bit 24 and 2 with bit 25. 3 fits both (6.6 sets bit 24 as
# FMODE_DIO_PARALLEL_WRITE), so 3 or no backing file falls back to a symbol the change added.
BACKING = (
    'if [ "$m" -lt 6 ] || { [ "$m" -eq 6 ] && [ "$n" -lt 14 ]; }; then w=; '
    "b=$(bpftrace -q -c /bin/true -e 'fexit:vmlinux:backing_file_open /pid == cpid/ "
    r'{ printf("%d\n", (retval->f_mode >> 24) & 7); exit(); }'
    "' 2>/dev/null | head -n 1); "
    "case \"$b\" in 1|5|7) ;; 2) w=1 ;; *) grep -q '^file_set_fsnotify_mode' $a || w=1 ;; esac; "
    'if [ -n "$w" ]; then '
    r"sed -i 's/f_mode & 0x1000000)/f_mode \& 0x2000000)/' $f; fi; fi; "
)
# Kernels from 6.15 split pipe_write into anon_pipe_write and fifo_pipe_write, and 7.0 renamed
# do_filp_open to do_file_open.
TRACE = (
    "mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing; "
    "f=/tmp/trace.bt; cp /aims/trace.bt $f; a=/sys/kernel/tracing/available_filter_functions; "
    "if grep -qx anon_pipe_write $a; then "
    "sed -i 's/^fentry:vmlinux:pipe_write /fentry:vmlinux:anon_pipe_write, "
    "fentry:vmlinux:fifo_pipe_write /' $f; fi; "
    "if ! grep -qx do_filp_open $a; then "
    "sed -i 's/^fexit:vmlinux:do_filp_open /fexit:vmlinux:do_file_open /' $f; fi; "
    "v=$(uname -r); m=${v%%.*}; n=${v#*.}; n=${n%%.*}; "
    + BACKING
    # cachestat arrived in 6.5; older kernels have neither the syscall nor its tracepoint.
    + 'if [ "$m" -lt 6 ] || { [ "$m" -eq 6 ] && [ "$n" -lt 5 ]; } '
    "|| ! grep -q '_sys_cachestat$' $a "
    "|| ! [ -e /sys/kernel/tracing/events/syscalls/sys_enter_cachestat/id ]; then "
    "sed -i 's|^tracepoint:syscalls:sys_enter_cachestat |//&|' $f; fi; "
    # x86_64 calls the 64-bit-offset tracepoint fadvise64_64; aarch64 calls it fadvise64.
    "if [ ! -e /sys/kernel/tracing/events/syscalls/sys_enter_fadvise64_64/id ]; then "
    "if [ -e /sys/kernel/tracing/events/syscalls/sys_enter_fadvise64/id ]; then "
    "sed -i 's/sys_enter_fadvise64_64/sys_enter_fadvise64/g' $f; else "
    "sed -i 's|^tracepoint:syscalls:sys_enter_fadvise64_64 |//&|' $f; fi; fi; "
    "exec bpftrace -B line -f json $f"
)
PMU_EVENTS = ("cpu-cycles", "instructions", "cache-misses")
PMU = (
    "echo devices $(ls /sys/bus/event_source/devices); for e in "
    + " ".join(PMU_EVENTS)
    + '; do if out=$(bpftrace -e "hardware:$e:1000000 { exit(); } interval:ms:50 { exit(); }"'
    ' 2>&1 >/dev/null); then echo "$e ok"; else echo "$e $(echo "$out" | grep -v Attaching'
    ' | head -n 1)"; fi; done'
)


class TracerError(Exception):
    pass


@dataclass(slots=True)
class Route:
    """Kernel records of one sandbox, keyed by its pid namespace."""

    pidns: int
    mounts: Mounts
    syscalls: dict[int, str]
    queue: asyncio.Queue[Record] = field(default_factory=lambda: asyncio.Queue(QUEUE))
    overflow: int = 0
    window: tuple[int, int] | None = None
    pre_exec: Callable[[PreExec], None] | None = None

    def put(self, record: Record) -> None:
        if self.overflow:
            low, high = self.window or (None, None)
            lost = Lost(self.overflow, "audit queue full", (), low, high)
            try:
                self.queue.put_nowait(lost)
            except asyncio.QueueFull:
                self._drop(record)
                return
            self.overflow, self.window = 0, None
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self._drop(record)

    def _drop(self, record: Record) -> None:
        self.overflow += 1
        if isinstance(record, Kernel):
            ts = record.at.ts
            low, high = self.window or (ts, ts)
            self.window = (min(low, ts), max(high, ts))


@dataclass(slots=True)
class _Held:
    """Records of a pid the tracer has not seen start, until it execs, exits, or a second passes."""

    ts: int
    last: int
    cgroup: int
    count: int = 0
    lines: list[str] = field(default_factory=list)


class Tracer:
    """A privileged bpftrace container that sees every process, file and socket call in sandboxes.

    One per backend. Records reach a sandbox once it registers its pid namespace.
    """

    def __init__(
        self,
        docker: aiodocker.Docker,
        labels: dict[str, str],
        name: str,
        *,
        ring_pages: int | None = None,
        toolbox: str | None = None,
    ) -> None:
        self._docker = docker
        self._labels = labels
        self._name = name
        self._ring_pages = ring_pages
        self._toolbox = None if toolbox is None else toolbox.encode()
        self._container: DockerContainer | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._routes: dict[int, Route] = {}
        self._pids: dict[int, Route] = {}
        # Exited pids, with the stream time of their exit.
        self._gone: OrderedDict[int, tuple[Route, int]] = OrderedDict()
        self._foreign: OrderedDict[int, None] = OrderedDict()
        self._held: OrderedDict[int, _Held] = OrderedDict()
        self._ts = 0
        self._parts: OrderedDict[int, dict[str, object]] = OrderedDict()
        self._cwd: OrderedDict[int, str] = OrderedDict()
        self._env: OrderedDict[int, bytearray] = OrderedDict()
        self._env_bytes: dict[int, int] = {}
        self._roots: OrderedDict[int, int] = OrderedDict()
        # The pid namespace of each sandbox cgroup, from the runtime's first exec in it.
        self._cgroups: OrderedDict[int, int] = OrderedDict()
        self._waiters: dict[int, asyncio.Future[int]] = {}
        self._sequence = Sequence()
        self._exited = asyncio.Event()
        # Set each time a batch of the stream has been read.
        self._fed = asyncio.Event()
        self._uncounted = False
        # The id of bpftrace's map of record counts.
        self._seq_map = ""
        # Stream time of each loss bpftrace reported, and the kernel's total by then.
        self._drops: deque[tuple[int, int]] = deque()
        self.alive = False
        self.garbled = 0
        self.unhandled = 0
        self.unrouted = 0
        # Records of processes in no registered sandbox: the runtime's own setup in a sandbox's
        # cgroup, the runner's helpers, sandboxes already closed.
        self.foreign = 0
        # Records lost from the streams of cgroups that are no registered sandbox.
        self.foreign_lost = 0
        self.unrouted_lines: deque[str] = deque(maxlen=32)
        self._line = ""
        self._cgroup = 0
        self.clock: tuple[int, int] | None = None
        self.kernel_lost = 0
        self.located = 0
        self.unlocated = 0
        self.pmu: dict[str, JsonValue] | None = None
        # Whether this kernel can preempt a probe midway, and its preemption mode if it says.
        self.preemption: dict[str, JsonValue] | None = None
        self._bpftrace: int | None = None
        self._skip_reader: asyncio.Task[None] | None = None
        self._host: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probes: dict[int, str] = {}
        # Each program's skip count so far, and the boot clock of the reading it came from.
        self._skips: dict[int, int] = {}
        self._skips_ts: int | None = None
        self.skipped = 0
        # lines still to come of a known compile warning
        self._quiet = 0

    async def start(self) -> None:
        image = await ensure_image(self._docker, "tracer")
        log.warning(
            "starting %s, a privileged container with host pid access that traces sandbox "
            "processes; --audit snapshot runs without it",
            self._name,
        )
        env = [] if self._ring_pages is None else [f"BPFTRACE_PERF_RB_PAGES={self._ring_pages}"]
        self._container = await create_container(
            self._docker,
            config={
                "Image": image,
                "Env": env,
                "Labels": {**self._labels, "aims.role": "tracer"},
                "HostConfig": {
                    "Privileged": True,
                    "PidMode": "host",
                    "NetworkMode": "none",
                    "LogConfig": {"Type": "none"},
                },
            },
            name=self._name,
        )
        try:
            await self._container.start()
            trace = await self._container.exec(["sh", "-c", TRACE], stdout=True, stderr=True)
            self._reader = asyncio.create_task(self._read(trace))
            async with asyncio.timeout(READY_S):
                base = await shielded(self._ready)
            if base != 0:
                raise TracerError("the tracer is not in the host pid namespace")
            self.alive = True
            await self._probe_pmu(self._container)
            await self._start_skips(self._container, trace)
        except BaseException:
            await self.stop()
            raise

    async def _probe_pmu(self, container: DockerContainer) -> None:
        """Which hardware counters the host offers: event source devices and an attach attempt."""
        try:
            out = await exec_output(container, ["sh", "-c", PMU], timeout_s=PMU_S)
        except Exception as e:
            log.warning("hardware counter probe failed: %s", e)
            return
        self.pmu = parse_pmu(out.decode(errors="replace"))

    async def _start_skips(self, container: DockerContainer, trace: DockerExec) -> None:
        """Count the probe runs the kernel skips, once a second, and learn whether it can
        preempt a probe."""
        try:
            pid = int((await trace.inspect())["Pid"])
            if pid <= 0:
                raise TracerError("bpftrace has no pid")
            skips = await container.exec(
                ["sh", "-c", SKIPS, "sh", str(pid)], stdout=True, stderr=True
            )
        except Exception as e:
            log.warning("probe skips are not counted: %s", e)
            return
        self._bpftrace = pid
        self._skip_reader = asyncio.create_task(self._read_skips(skips))
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(SKIPS_S):
                await asyncio.shield(self._host)

    async def _read_skips(self, skips: DockerExec) -> None:
        reading: list[str] = []
        pending = b""
        try:
            stream = await skips.start(detach=False).__aenter__()
            try:
                while (message := await stream.read_out()) is not None:
                    if message.stream != 1:
                        text = message.data.decode(errors="replace").strip()
                        log.warning("tracer skip counter: %s", text)
                        continue
                    *lines, pending = (pending + message.data).split(b"\n")
                    for raw in lines:
                        line = raw.decode(errors="replace")
                        if line == ".":
                            self._skipped(*parse_skips(reading))
                            reading = []
                        elif line.startswith("H "):
                            self._set_preemption(parse_preemption(line))
                        elif line.startswith("N "):
                            self._probes = parse_probes(line[2:])
                        elif line.startswith("C "):
                            self._counted(line)
                        elif line.startswith("S "):
                            self._seq_map = line[2:].split(" ", 1)[0]
                        else:
                            reading.append(line)
            finally:
                await stream.close()
        except Exception as e:
            log.warning("tracer skip counter failed: %s", e)
        finally:
            if not self._host.done():
                self._host.set_result(None)
            if self.alive:
                log.warning("probe skips are no longer counted each second")

    def _set_preemption(self, preemption: dict[str, JsonValue]) -> None:
        self.preemption = preemption
        if preemption["preemptible"]:
            log.warning(
                "this kernel can preempt a tracer probe midway (preemptible RCU, mode %s); "
                "runs it skips meanwhile are reported as audit.lost",
                preemption["mode"] or "unknown",
            )
        if not self._host.done():
            self._host.set_result(None)

    def _skipped(self, ts: int | None, counts: dict[int, int]) -> None:
        """Report the runs each probe skipped since the last reading, in every sandbox."""
        start = self._skips_ts
        if start is None and self.clock is not None:
            start = self.clock[0]
        for prog, count in counts.items():
            seen = self._skips.get(prog, 0)
            self._skips[prog] = max(seen, count)
            new = count - seen
            if new <= 0:
                continue
            self.skipped += new
            name = self._probes.get(prog, f"program {prog}")
            reason = f"kernel skipped tracer probe {name}: a run of it was in progress on that cpu"
            lost = Lost(new, reason, (), start, ts, "tracer")
            for route in self._routes.values():
                route.put(lost)
        if ts is not None and (self._skips_ts is None or ts > self._skips_ts):
            self._skips_ts = ts

    async def count_skips(self) -> None:
        """Read the probes' skip counts now, so a sandbox that closes gets those up to its end."""
        container, pid = self._container, self._bpftrace
        if not self.alive or container is None or pid is None:
            return
        argv = ["sh", "-c", f"p=$1; m=$2; {SKIP_READ}", "sh", str(pid), self._seq_map]
        try:
            out = await exec_output(container, argv, timeout_s=SKIPS_S)
        except Exception as e:
            log.warning("could not read tracer probe skips: %s", e)
            return
        lines = out.decode(errors="replace").splitlines()
        self._skipped(*parse_skips(line for line in lines if not line.startswith("C ")))
        for line in lines:
            if line.startswith("C "):
                self._counted(line)

    def _counted(self, line: str) -> None:
        """Take a reading of the kernel's count of each record stream."""
        reading = parse_counts(line)
        if reading is None:
            log.warning("unreadable tracer stream counts: %s", line[:200])
            return
        ts, counts = reading
        if not counts:
            # A real reading always holds the tracer's own streams.
            if not self._uncounted:
                log.warning(
                    "the kernel's record counts are unreadable; lost last records go unseen"
                )
                self._uncounted = True
            return
        self._sequence.counted(ts, counts)

    async def stop(self) -> None:
        self.alive = False
        for task in (self._reader, self._skip_reader):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # A start cut short no longer waits for the header; mark its error read so asyncio
        # does not log it as lost.
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()
        if self._container is not None:
            await remove_container(self._container)
            self._container = None

    def register(self, pidns: int, mounts: Mounts, syscalls: dict[int, str]) -> Route:
        route = Route(pidns, mounts, syscalls)
        self._routes[pidns] = route
        return route

    def adopt(self, route: Route, pids: list[int]) -> None:
        """Route processes that were already running in the sandbox when it registered."""
        for pid in pids:
            self._pids[pid] = route
            self._gone.pop(pid, None)
            self._foreign.pop(pid, None)

    def unregister(self, route: Route) -> None:
        self._routes.pop(route.pidns, None)
        for cgroup in [c for c, pidns in self._cgroups.items() if pidns == route.pidns]:
            del self._cgroups[cgroup]
        for pid in [p for p, r in self._pids.items() if r is route]:
            del self._pids[pid]
            self._disown(pid)
        for pid in [p for p, (r, _) in self._gone.items() if r is route]:
            del self._gone[pid]
            self._disown(pid)

    async def confirm(self, pid: int, pidns: int) -> bool:
        """Whether the tracer saw the runtime start process `pid` in namespace `pidns`.

        Another namespace on record may be an older process with the same pid, so it waits.
        """
        if self._roots.get(pid) == pidns:
            return True
        future = self._waiters.setdefault(pid, asyncio.get_running_loop().create_future())
        try:
            async with asyncio.timeout(CONFIRM_S):
                return await future == pidns
        except TimeoutError:
            return False
        finally:
            self._waiters.pop(pid, None)

    async def quiesce(self, route: Route, timeout_s: float) -> None:
        """Wait until every process of `route` has exited, then for the records its exit leaves,
        the probe runs skipped up to then and the holes its streams still have open."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                while self.alive and any(r is route for r in self._pids.values()):
                    self._exited.clear()
                    await self._exited.wait()
                await asyncio.sleep(EXIT_GRACE_S)
        await self.count_skips()
        cgroups = [c for c, pidns in self._cgroups.items() if pidns == route.pidns]
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(SETTLE_S):
                while self.alive and any(self._sequence.pending(c) for c in cgroups):
                    self._fed.clear()
                    await self._fed.wait()

    async def _read(self, trace: DockerExec) -> None:
        """Read bpftrace's output as bytes, so file names that are not UTF-8 keep their bytes."""
        pending = {1: b"", 2: b""}
        try:
            stream = await trace.start(detach=False).__aenter__()
            try:
                while (message := await stream.read_out()) is not None:
                    data = pending.get(message.stream, b"") + message.data
                    *lines, pending[message.stream] = data.split(b"\n")
                    for line in lines:
                        self._feed(line)
                    self._fed.set()
            finally:
                await stream.close()
        except Exception as e:
            log.error("tracer stream failed: %s", e)
        finally:
            if not self._ready.done():
                self._ready.set_exception(TracerError("the tracer stopped before it was ready"))
            if self.alive:
                log.error("tracer stopped; sandboxes fall back to snapshot audit")
            self.alive = False
            self._exited.set()
            self._fed.set()
            for route in self._routes.values():
                route.put(Lost(None, "tracer stopped"))

    def _feed(self, raw: bytes) -> None:
        """Handle one line. A line the handler fails on costs that record, and the reader goes
        on. The record may be any sandbox's, so each one logs the loss."""
        try:
            self._handle(raw.decode("utf-8", "surrogateescape"))
        except Exception:
            self.unhandled += 1
            # one traceback is enough when every line fails the same way
            show = log.exception if self.unhandled == 1 else log.debug
            show("tracer line could not be handled: %r", raw[:500])
            for route in self._routes.values():
                route.put(Lost(1, UNHANDLED, scope="tracer"))

    def _note(self, text: str) -> None:
        """Log what bpftrace says; known compile warnings and their source lines at debug."""
        found = DIAGNOSTIC.fullmatch(text)
        if found is not None:
            self._quiet = 2 if found[1] == "WARNING" and found[2] in KNOWN_WARNINGS else 0
            quiet = self._quiet > 0
        else:
            quiet = self._quiet > 0
            self._quiet = 0 if MARKER.fullmatch(text) else max(0, self._quiet - 1)
        log.log(logging.DEBUG if quiet else logging.WARNING, "tracer: %s", text)
        if not self._ready.done() and "ERROR" in text:
            self._ready.set_exception(TracerError(text))

    def settle(self) -> None:
        """Report holes older than a second, then loss the kernel counted that no hole explains.

        Loss is judged on the stream's clock once its holes must have been found, so a reader that
        falls behind does not report loss it has not read up to yet. Unexplained loss is reported
        with an unknown count, so a hole found later is not counted twice. The mark is what the
        last check left unexplained, so a hole found late lowers it for the checks after.
        """
        self._expire()
        counted = None
        while self._drops and self._drops[0][0] <= self._sequence.latest - RIPE_US:
            counted = self._drops.popleft()[1]
        if counted is None:
            return
        excess = counted - self.located
        if excess > self.unlocated:
            reason = (
                f"the kernel dropped {excess - self.unlocated} records no sequence hole located"
            )
            for route in self._routes.values():
                route.put(Lost(None, reason, scope="tracer"))
        self.unlocated = max(excess, 0)

    def _handle(self, line: str) -> None:
        self._line = line
        item = parse(line)
        at = getattr(item, "at", None)
        self._ts = self._sequence.latest if at is None else at.ts
        self._cgroup = 0 if at is None else at.cgroup
        if at is not None:
            self._gaps(self._sequence.observe(at))
        match item:
            case None:
                return
            case Ready(base, boot_us, tai_us):
                self.clock = (boot_us, tai_us)
                if not self._ready.done():
                    self._ready.set_result(base)
            case Tick():
                self.settle()
            case Note(text):
                self._note(text)
            case Dropped(count):
                self.kernel_lost += count
                self._drops.append((self._sequence.latest, self.kernel_lost))
            case Garbled(pid, text):
                self.garbled += 1
                route = self._lookup(pid) if pid is not None else None
                if route is not None:
                    route.put(Lost(1, f"unparseable tracer line: {text[:500]}"))
            case Fork(parent, child):
                self._gone.pop(child, None)
                self._foreign.pop(child, None)
                route = self._route(parent)
                if route is not None:
                    self._pids[child] = route
                    route.put(item)
                elif parent in self._foreign:
                    self._disown(child)
            case ExecPart(pid):
                parts = self._parts.setdefault(pid, {})
                parts.update(item.fields())
                self._parts.move_to_end(pid)
                if len(self._parts) > MEMORY:
                    self._parts.popitem(last=False)
                path = parts.get("path")
                if (
                    isinstance(item, Identity)
                    and isinstance(path, str)
                    and path
                    and parts.get("path_partial") is not True
                ):
                    if not posixpath.isabs(path):
                        cwd = self._cwd.get(item.ppid)
                        path = "" if cwd is None else posixpath.join(cwd, path)
                    if path:
                        route = self._pids.get(pid)
                        if route is not None:
                            record = PreExec(
                                pid,
                                item.ppid,
                                path,
                                item.inode,
                                item.dev,
                                item.size,
                                item.mtime_us,
                                item.ctime_us,
                                at=item.at,
                            )
                            if route.pre_exec is None:
                                route.put(record)
                            else:
                                try:
                                    route.pre_exec(record)
                                except Exception:
                                    log.exception("pre-exec callback failed")
                                    route.put(record)
            case EnvChunk(pid):
                if item.offset == 0:
                    self._env[pid] = bytearray()
                env = self._env.setdefault(pid, bytearray())
                self._env.move_to_end(pid)
                if len(self._env) > MEMORY:
                    self._env.popitem(last=False)
                end = item.offset + len(item.data)
                if len(env) < end:
                    env.extend(bytes(end - len(env)))
                env[item.offset : end] = item.data
                self._env_bytes[pid] = item.env_bytes
            case RawExec():
                self._exec(item)
            case Exit(pid):
                if pid in self._gone and pid not in self._pids:
                    return
                self._parts.pop(pid, None)
                self._env.pop(pid, None)
                self._env_bytes.pop(pid, None)
                route = self._pids.pop(pid, None)
                if route is None:
                    self._unrouted(pid)
                    held = self._held.pop(pid, None)
                    if held is not None:
                        self._count(held)
                else:
                    route.put(item)
                    self._gone[pid] = (route, self._ts)
                    self._gone.move_to_end(pid)
                    if len(self._gone) > MEMORY:
                        self._gone.popitem(last=False)
                    self._exited.set()
            case RawClose(pid, size, walk):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(Close(pid, size, path, partial, at=item.at))
            case Delete(pid, walk):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(File(pid, "delete", path, partial=partial, at=item.at))
            case Rename(pid, old, new):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(old)
                    target, partial_target = route.mounts.resolve(new)
                    partial = partial or partial_target
                    route.put(File(pid, "rename", path, target, partial=partial, at=item.at))
            case Mkdir(pid, walk):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(File(pid, "mkdir", path, partial=partial, at=item.at))
            case Symlink(pid, walk, target, cut):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    partial = partial or cut
                    route.put(File(pid, "symlink", path, target, partial=partial, at=item.at))
            case Link(pid, old, new):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(new)
                    target, partial_target = route.mounts.resolve(old)
                    partial = partial or partial_target
                    route.put(File(pid, "link", path, target, partial=partial, at=item.at))
            case RawDenied(pid, nr, errno):
                route = self._route(pid)
                if route is not None and nr in route.syscalls:
                    route.put(Denied(pid, route.syscalls[nr], errno, at=item.at))
            case RawFifo(pid, walk):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(Ipc(pid, "fifo", path=path, partial=partial, at=item.at))
            case RawMmap(pid, walk, inode, nlink, prot):
                if (
                    self._toolbox is not None
                    and len(walk.names) > 2
                    and walk.names[2] == self._toolbox
                ):
                    return
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    if path != TOOLBOX and not path.startswith(f"{TOOLBOX}/"):
                        route.put(Mem(pid, "mmap", path, inode, nlink, prot, partial, at=item.at))
            case RawSetattr(pid, walk, valid, mode, uid, gid, size, atime_us, mtime_us, ctime_us):
                route = self._route(pid)
                if route is not None:
                    path, partial = route.mounts.resolve(walk)
                    route.put(
                        Setattr(
                            pid,
                            path,
                            partial,
                            valid,
                            mode,
                            uid,
                            gid,
                            size,
                            atime_us,
                            mtime_us,
                            ctime_us,
                            at=item.at,
                        )
                    )
            case RawCache(pid, op, walk):
                route = self._route(pid)
                if route is not None:
                    if walk is None:
                        route.put(Cache(pid, op, None, at=item.at))
                    else:
                        path, partial = route.mounts.resolve(walk)
                        route.put(Cache(pid, op, path, partial=partial, at=item.at))
            case RawSync(pid, call, walk):
                route = self._route(pid)
                if route is not None:
                    if walk is None:
                        route.put(Sync(pid, call, None, at=item.at))
                    else:
                        path, partial = route.mounts.resolve(walk)
                        route.put(Sync(pid, call, path, partial=partial, at=item.at))
            case Cache(pid) | Sync(pid):
                route = self._route(pid)
                if route is not None:
                    route.put(item)
            case Kernel(pid):
                route = self._route(pid)
                if route is not None:
                    route.put(item)

    def _route(self, pid: int) -> Route | None:
        route = self._lookup(pid)
        if route is None:
            self._unrouted(pid)
        return route

    def _unrouted(self, pid: int) -> None:
        """Count a record no sandbox claimed. One from an unknown pid waits to see what the pid is:
        the runtime's own processes open and map files before their exec record.
        """
        if pid in self._foreign:
            self.foreign += 1
            return
        held = self._held.get(pid)
        if held is None:
            held = self._held[pid] = _Held(self._ts, self._ts, self._cgroup)
            if len(self._held) > MEMORY:
                self._count(self._held.popitem(last=False)[1])
        held.count += 1
        held.last = self._ts
        if len(held.lines) < 8:
            held.lines.append(self._line)

    def _count(self, held: _Held, *, foreign: bool = False, route: Route | None = None) -> None:
        """Count held records as foreign, or as loss of `route`, by default the sandbox of the
        cgroup they came from; records of no sandbox's cgroup are foreign."""
        if not foreign and route is None:
            route = self._sandbox(held.cgroup)
            foreign = route is None
        if foreign or route is None:
            self.foreign += held.count
            return
        self.unrouted += held.count
        self.unrouted_lines.extend(held.lines)
        route.put(Lost(held.count, UNKNOWN, (), held.ts, held.last))

    def _claim(self, cgroup: int, pidns: int) -> None:
        """Tie `cgroup` to the sandbox of `pidns`. A sandbox is one container, so an older
        cgroup with that namespace is a gone container whose namespace number was reused."""
        for old in [c for c, n in self._cgroups.items() if n == pidns and c != cgroup]:
            del self._cgroups[old]
        self._cgroups[cgroup] = pidns
        self._cgroups.move_to_end(cgroup)
        if len(self._cgroups) > MEMORY:
            self._cgroups.popitem(last=False)

    def _sandbox(self, cgroup: int) -> Route | None:
        pidns = self._cgroups.get(cgroup)
        return None if pidns is None else self._routes.get(pidns)

    def _disown(self, pid: int) -> None:
        self._foreign[pid] = None
        self._foreign.move_to_end(pid)
        if len(self._foreign) > MEMORY:
            self._foreign.popitem(last=False)

    def _lookup(self, pid: int) -> Route | None:
        """The route of `pid`, also for a while after it exits: its files close after its exit."""
        route = self._pids.get(pid)
        if route is None and pid in self._gone:
            route = self._gone[pid][0]
        return route

    def _expire(self) -> None:
        cutoff = self._sequence.latest - 1_000_000
        self._gaps(self._sequence.expire(cutoff))
        while self._held and next(iter(self._held.values())).ts < cutoff:
            self._count(self._held.popitem(last=False)[1])
        gone = self._sequence.latest - GONE_US
        while self._gone and next(iter(self._gone.values()))[1] < gone:
            self._gone.popitem(last=False)

    def _gaps(self, gaps: list[Gap]) -> None:
        """Report each hole to the sandbox whose stream it is in. A hole in the tracer's own
        stream may hide an OOM kill in any sandbox."""
        for gap in gaps:
            self.located += gap.count
            window = (gap.cpu,), gap.ts_from, gap.ts_to
            if gap.cgroup == 0:
                lost = Lost(gap.count, OWN_GAP, *window, "tracer")
                for route in self._routes.values():
                    route.put(lost)
                continue
            route = self._sandbox(gap.cgroup)
            if route is None:
                self.foreign_lost += gap.count
            else:
                route.put(Lost(gap.count, "kernel record gap", *window))

    def _exec(self, raw: RawExec) -> None:
        self._gone.pop(raw.pid, None)
        self._foreign.pop(raw.pid, None)
        held = self._held.pop(raw.pid, None)
        if raw.root:
            self._roots[raw.pid] = raw.pidns
            if len(self._roots) > MEMORY:
                self._roots.popitem(last=False)
            self._claim(raw.at.cgroup, raw.pidns)
            waiter = self._waiters.get(raw.pid)
            if waiter is not None and not waiter.done():
                waiter.set_result(raw.pidns)
        route = self._routes.get(raw.pidns)
        # Before a root exec the pid is still the runtime's: runc's last stage after its setns.
        if held is not None:
            self._count(held, foreign=route is None or raw.root, route=route)
        if route is None:
            self._disown(raw.pid)
            self.foreign += 1
            return
        self._pids[raw.pid] = route
        parts = self._parts.pop(raw.pid, {})
        parts.setdefault("path", raw.argv[0] if raw.argv else "")
        walk = parts.pop("cwd", None)
        if isinstance(walk, Walk):
            parts["cwd"], parts["cwd_partial"] = route.mounts.resolve(walk)
        cwd = parts.get("cwd")
        if isinstance(cwd, str) and not parts.get("cwd_partial"):
            self._cwd[raw.pid] = cwd
            self._cwd.move_to_end(raw.pid)
            if len(self._cwd) > MEMORY:
                self._cwd.popitem(last=False)
        env = self._env.pop(raw.pid, None)
        if env is not None:
            size = self._env_bytes.pop(raw.pid, len(env))
            entries = bytes(env[:size]).split(b"\0")
            if entries and entries[-1] == b"":
                entries.pop()
            parts["env"] = tuple(e.decode("utf-8", "backslashreplace") for e in entries)
            parts["env_bytes"] = size
        record = Exec(
            raw.pid,
            raw.ppid,
            raw.ns_pid,
            raw.uid,
            "",
            raw.argv,
            raw.argv_bytes,
            raw.root,
            at=raw.at,
        )
        route.put(dataclasses.replace(record, **parts))


def parse_pmu(text: str) -> dict[str, JsonValue]:
    devices: list[JsonValue] = []
    events: dict[str, JsonValue] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(" ")
        if name == "devices":
            devices = list(rest.split())
        elif name in PMU_EVENTS:
            events[name] = rest.strip() or "unknown"
    available = any(v == "ok" for v in events.values())
    return {"available": available, "devices": devices, "events": events}


def parse_preemption(line: str) -> dict[str, JsonValue]:
    """`H <1 with preemptible RCU> <sched/preempt>`; that file marks the mode in brackets and
    exists only on kernels that choose it at boot."""
    parts = line.split()
    rcu = len(parts) > 1 and parts[1] == "1"
    mode = next((m[1:-1] for m in parts[2:] if m.startswith("(") and m.endswith(")")), None)
    preemptible = rcu and mode not in ("none", "voluntary")
    return {"preemptible_rcu": rcu, "mode": mode, "preemptible": preemptible}


def parse_probes(text: str) -> dict[int, str]:
    """Program names by id from `bpftool -j prog show`, without bpftrace's probe index."""
    try:
        progs = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(progs, list):
        return {}
    return {
        p["id"]: PROBE_INDEX.sub("", p["name"])
        for p in progs
        if isinstance(p, dict) and isinstance(p.get("id"), int) and isinstance(p.get("name"), str)
    }


def parse_counts(line: str) -> tuple[int, dict[tuple[int, int], int]] | None:
    """The boot clock in µs and the kernel's count of each (cgroup, cpu) record stream, from
    `C <uptime> <key hex>:<value hex>...`: a key is two little-endian u64, a value one."""
    _, uptime, *entries = line.split(" ")
    try:
        ts = round(float(uptime) * 1_000_000)
        counts = {}
        for entry in entries:
            key, value = (bytes.fromhex(x) for x in entry.split(":"))
            if len(key) != 16 or len(value) != 8:
                return None
            stream = (int.from_bytes(key[:8], "little"), int.from_bytes(key[8:], "little"))
            counts[stream] = int.from_bytes(value, "little")
    except ValueError:
        return None
    return ts, counts


def parse_skips(lines: Iterable[str]) -> tuple[int | None, dict[int, int]]:
    """The boot clock in µs and each program's skip count, from one reading of fdinfo.

    A link's fd names its program too, but only the program's own fd carries the count.
    """
    ts = None
    fds: dict[str, dict[str, int]] = {}
    for line in lines:
        if line.startswith("T "):
            with contextlib.suppress(ValueError):
                ts = round(float(line[2:]) * 1_000_000)
            continue
        fd, _, rest = line.partition(":")
        key, _, value = rest.partition(":")
        with contextlib.suppress(ValueError):
            fds.setdefault(fd, {})[key] = int(value)
    return ts, {
        f["prog_id"]: f["recursion_misses"]
        for f in fds.values()
        if "prog_id" in f and "recursion_misses" in f
    }
