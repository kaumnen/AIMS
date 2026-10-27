import asyncio
import json
from typing import Any, cast

import aiodocker
import pytest

from aims.sandbox import tracer as tracer_module
from aims.sandbox.base import (
    Capture,
    Close,
    Denied,
    Exec,
    Exit,
    Fd,
    File,
    Fork,
    Lost,
    Net,
    Oom,
    Output,
    Stamp,
)
from aims.sandbox.trace import (
    DEPTH,
    SYSCALLS,
    Bprm,
    Context,
    Delete,
    Dropped,
    EnvChunk,
    Gap,
    Garbled,
    Mounts,
    Note,
    RawDenied,
    RawExec,
    Ready,
    Rename,
    Sequence,
    Walk,
    parse,
)
from aims.sandbox.trace import (
    PIPEFS_MAGIC as PIPEFS,
)
from aims.sandbox.tracer import Tracer

MOUNTINFO = """\
161 93 0:64 / / ro,relatime - overlay overlay rw,lowerdir=/x
171 161 0:84 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw
172 161 254:1 /docker/volumes/aims-r-a1-work/_data /work rw,relatime - ext4 /dev/vda1 rw
173 161 254:1 /docker/volumes/aims-r-shared/_data /shared rw,relatime - ext4 /dev/vda1 rw
174 161 254:1 /docker/containers/abc/resolv.conf /etc/resolv.conf ro - ext4 /dev/vda1 rw
175 161 254:1 /docker/volumes/odd/_data /with\\040space rw - ext4 /dev/vda1 rw
"""
VDA1 = (254 << 20) | 1
TMP = 84


def hx(data: bytes) -> str:
    return "".join(f"\\x{b:02x}" for b in data)


AT = Stamp(5000, 0, 1)
PARSED = Stamp(5, 0, 1)


def line(text: str, at: Stamp = AT) -> str:
    tag, rest = text.split(" ", 1)
    return json.dumps({"type": "printf", "data": f"{tag} {at.ts} {at.cpu} {at.seq} {rest}\n"})


class Feed:
    """Lines from one cpu with consecutive sequence numbers."""

    def __init__(self, tracer: Tracer, cpu: int = 0) -> None:
        self.tracer, self.cpu, self.seq = tracer, cpu, 0

    def __call__(self, text: str) -> Stamp:
        self.seq += 1
        at = Stamp(self.seq * 10_000, self.cpu, self.seq)
        self.tracer._handle(line(text, at))
        return Stamp(self.seq * 10, self.cpu, self.seq)


def walk(*names: str) -> tuple[int, str]:
    """The depth and the `/`-joined names bpftrace prints for a dentry walk."""
    names = names[:DEPTH]
    return len(names), "/".join([*names, *["/"] * (DEPTH - len(names))])


def unlink(pid: int, dev: int, *names: str) -> str:
    depth, text = walk(*names)
    return f"U {pid} {dev} {depth} {text}"


def rename(pid: int, devs: tuple[int, int], old: tuple[str, ...], new: tuple[str, ...]) -> str:
    (old_depth, old_text), (new_depth, new_text) = walk(*old), walk(*new)
    return f"N {pid} {devs[0]} {devs[1]} {old_depth} {new_depth} {old_text}/{new_text}"


def test_parse_exec_splits_argv_exactly() -> None:
    argv = b"sh\0-c\0echo 'a b' \\x00 \"q\"\0"
    item = parse(line(f"E 10 1 1 1000 99 5 {len(argv)} {hx(argv)}"))
    args = ("sh", "-c", "echo 'a b' \\x00 \"q\"")
    assert item == RawExec(10, 1, True, 1000, 99, 5, len(argv), args, at=PARSED)


def test_parse_reads_the_header() -> None:
    wire = Stamp(9_007_199_254_740_993_123, 7, 123456789012)
    parsed = Stamp(9_007_199_254_740_993, 7, 123456789012)
    assert parse(line("F 10 11", wire)) == Fork(10, 11, at=parsed)
    ready = parse(line("R 0 1793054641268664792", AT))
    assert ready == Ready(0, 5, 1793054641268664, PARSED)


def test_parse_exec_context() -> None:
    char, pipe = 0o020666, 0o010600
    _, names = walk("sub", "_data", "aims-r-a1-work", "volumes", "docker")
    body = f"V 10 7 {char} 999 8 {pipe} {PIPEFS} 9 {pipe} {PIPEFS} {VDA1} 5 {names}"
    item = parse(line(body))
    assert isinstance(item, Context)
    assert item.fds == (Fd(7, "char"), Fd(8, "pipe"), Fd(9, "pipe"))
    assert item.cwd.names[:5] == (b"sub", b"_data", b"aims-r-a1-work", b"volumes", b"docker")
    fifo = parse(line(f"V 10 7 {char} 0 8 {pipe} 61267 9 {pipe} 61267 {VDA1} 5 {names}"))
    assert isinstance(fifo, Context)
    assert fifo.fds[1] == Fd(8, "fifo")


def test_parse_env_chunk() -> None:
    env = b"A=1\0LD_PRELOAD=/x\0"
    item = parse(line(f"G 10 300 512 {hx(env)}"))
    assert item == EnvChunk(10, 300, 512, env, at=PARSED)


def test_parse_output() -> None:
    first = parse(line(f"T 10 4242 {PIPEFS} 20 {hx(b'hello')}"))
    assert first == Output(10, 4242, 20, PIPEFS, b"hello", at=PARSED)
    more = parse(line(f"T 10 4242 {PIPEFS} 8 "))
    assert more == Output(10, 4242, 8, PIPEFS, None, at=PARSED)


def test_parse_simple_lines() -> None:
    assert parse(line("F 10 11")) == Fork(10, 11, at=PARSED)
    assert parse(line("B 10 /usr/bin/my tool")) == Bprm(10, "/usr/bin/my tool", at=PARSED)
    assert parse(line("X 10 3 0")) == Exit(10, 3, None, at=PARSED)
    assert parse(line("X 10 0 9")) == Exit(10, None, 9, at=PARSED)
    assert parse(line("O 10")) == Oom(10, at=PARSED)
    assert parse(line("W 10 /work/a b.txt")) == File(10, "write", "/work/a b.txt", at=PARSED)
    assert parse(line("A 10 /work/x")) == File(10, "read", "/work/x", at=PARSED)
    assert parse(line("P 10 13 /etc/shadow")) == File(
        10, "denied", "/etc/shadow", errno=13, at=PARSED
    )
    assert parse(line("P 10 1 rel")) == File(10, "denied", "rel", errno=1, partial=True, at=PARSED)
    assert parse(line("D 10 97 1")) == RawDenied(10, 97, 1, at=PARSED)
    assert parse(line("Z 10 42 /work/a b")) == Close(10, 42, "/work/a b", at=PARSED)
    assert parse(line(f"Z 10 {2**62} /work/sparse")) == Close(
        10, 2**53 - 1, "/work/sparse", at=PARSED
    )
    assert parse(line("C 10 c 4 169.254.169.254 80")) == Net(
        10, "connect", "inet", "169.254.169.254", 80, at=PARSED
    )
    assert parse(line("C 10 s 6 ::1 53")) == Net(10, "sendto", "inet6", "::1", 53, at=PARSED)


def test_parse_unix_sockets() -> None:
    path = b"/var/run/x.sock".ljust(108, b"\0")
    assert parse(line(f"C 10 c 1 {hx(path)} 0")) == Net(
        10, "connect", "unix", "/var/run/x.sock", None, at=PARSED
    )
    abstract = b"\0hidden".ljust(108, b"\0")
    assert parse(line(f"C 10 c 1 {hx(abstract)} 0")) == Net(
        10, "connect", "unix", "@hidden", None, at=PARSED
    )


def test_parse_walks() -> None:
    item = parse(line(unlink(10, VDA1, "c d.txt", "b", "_data")))
    assert item == Delete(10, Walk(VDA1, (b"c d.txt", b"b", b"_data", b"/")), at=PARSED)
    item = parse(line(rename(10, (VDA1, TMP), ("a b",), ("x", "y"))))
    assert item == Rename(10, Walk(VDA1, (b"a b", b"/")), Walk(TMP, (b"x", b"y", b"/")), at=PARSED)
    deep = parse(line(unlink(10, VDA1, *["d"] * DEPTH)))
    assert isinstance(deep, Delete)
    assert deep.walk.names == (b"d",) * DEPTH
    root = parse(line(rename(10, (1, 1), ("a",) * DEPTH, ())))
    assert isinstance(root, Rename)
    assert (root.old.names, root.new.names) == ((b"a",) * DEPTH, (b"/",))


def test_parse_keeps_bytes_that_are_not_utf8() -> None:
    raw = b'{"type": "printf", "data": "W 5000 0 1 10 /work/scorer.py\xff\\n"}'
    item = parse(raw.decode("utf-8", "surrogateescape"))
    assert item == File(10, "write", "/work/scorer.py\\xff", at=PARSED)


@pytest.mark.parametrize(
    "text",
    [
        "E 10 1 1 1000",
        "E 10 1 1 1000 99 5 3 notahex",
        "U 10 1 2 a/b//",
        "U 10 1 1 a/b/" + "/" * 20,
        "U 10 1 x " + walk("a")[1],
        "X 10 zero 0",
        "unknown 10",
        f"N 10 1 2 1 1 {walk('a')[1]}",
    ],
)
def test_parse_rejects_misshapen_lines(text: str) -> None:
    item = parse(line(text))
    tag, rest = text.split(" ", 1)
    assert item == Garbled(10, f"{tag} 5000 0 1 {rest}", PARSED)


@pytest.mark.parametrize("text", ["F 10 11", "F x 0 1 10 11", "F"])
def test_parse_rejects_bad_headers(text: str) -> None:
    item = parse(json.dumps({"type": "printf", "data": f"{text}\n"}))
    assert item == Garbled(None, text)


def test_parse_other_output() -> None:
    assert parse(json.dumps({"type": "lost_events", "data": {"events": 7}})) == Dropped(7)
    assert parse(json.dumps({"type": "attached_probes", "data": {"probes": 3}})) is None
    assert parse("ERROR: something broke") == Note("ERROR: something broke")
    assert parse("") is None


def test_mounts_resolve() -> None:
    mounts = Mounts.parse(MOUNTINFO)

    def resolve(dev: int, *names: str) -> tuple[str, bool]:
        return mounts.resolve(Walk(dev, tuple(n.encode() for n in names)))

    volume = ("_data", "aims-r-a1-work", "volumes", "docker", "/")
    assert resolve(VDA1, "s.py", "d", *volume) == ("/work/d/s.py", False)
    assert resolve(VDA1, *volume) == ("/work", False)
    shared = ("_data", "aims-r-shared", "volumes", "docker", "/")
    assert resolve(VDA1, "m", *shared) == ("/shared/m", False)
    assert resolve(VDA1, "f", "_data", "odd", "volumes", "docker", "/") == ("/with space/f", False)
    assert resolve(TMP, "x", "/") == ("/tmp/x", False)
    assert resolve(64, "passwd", "etc", "/") == ("/etc/passwd", False)
    assert resolve(VDA1, "f", "elsewhere", "/") == ("/elsewhere/f", True)
    assert resolve(VDA1, *["d"] * DEPTH) == (".../" + "/".join(["d"] * DEPTH), True)


def test_capture_keeps_head_and_counts_all() -> None:
    capture = Capture(limit=4)
    capture.add(1, b"abc")
    capture.add(1, b"defg")
    capture.add(2, b"xy")
    assert (bytes(capture.stdout), capture.stdout_bytes) == (b"abcd", 7)
    assert (bytes(capture.stderr), capture.stderr_bytes) == (b"xy", 2)
    assert capture.truncated


def test_syscall_tables_agree_on_names() -> None:
    arm, x86 = set(SYSCALLS["aarch64"].values()), set(SYSCALLS["x86_64"].values())
    assert {"unshare", "mount", "ptrace", "setns", "bpf"} <= arm & x86
    assert x86 - arm == {"mknod", "iopl", "ioperm"}


def drain(route: tracer_module.Route) -> list[object]:
    got = []
    while not route.queue.empty():
        got.append(route.queue.get_nowait())
    return got


@pytest.mark.anyio
async def test_tracer_routes_by_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracer_module, "CONFIRM_S", 0.05)
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    argv = b"sh\0-c\0ls\0"
    s = [
        feed(text)
        for text in [
            "R 0 999",
            "B 10 /bin/sh",
            f"E 10 1 1 1000 99 5 {len(argv)} {hx(argv)}",
            "F 10 11",
            f"E 11 10 0 1000 99 6 3 {hx(b'ls\0')}",
            "W 11 /work/out",
            unlink(11, VDA1, "out", "_data", "aims-r-a1-work", "volumes", "docker"),
            rename(11, (TMP, TMP), ("a",), ("b",)),
            "D 11 97 1",
            "D 11 21 1",
            "C 11 c 4 1.1.1.1 443",
            "O 11",
            "X 11 0 0",
            "W 11 /work/after-exit",
            f"E 50 1 1 1000 77 5 {len(argv)} {hx(argv)}",
            "W 50 /other",
            "E 11 broken",
        ]
    ]
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 2}}))

    assert drain(route) == [
        Exec(10, 1, 5, 1000, "/bin/sh", ("sh", "-c", "ls"), len(argv), True, at=s[2]),
        Fork(10, 11, at=s[3]),
        Exec(11, 10, 6, 1000, "ls", ("ls",), 3, False, at=s[4]),
        File(11, "write", "/work/out", at=s[5]),
        File(11, "delete", "/work/out", at=s[6]),
        File(11, "rename", "/tmp/a", "/tmp/b", at=s[7]),
        Denied(11, "unshare", 1, at=s[8]),
        Net(11, "connect", "inet", "1.1.1.1", 443, at=s[10]),
        Oom(11, at=s[11]),
        Exit(11, 0, None, at=s[12]),
    ]
    assert tracer.clock == (10, 0)
    assert tracer.garbled == 1
    assert tracer.kernel_lost == 2
    assert await tracer.confirm(10, 99)
    assert await tracer.confirm(50, 77)
    assert not await tracer.confirm(10, 77)
    assert not await tracer.confirm(12345, 99)

    tracer.unregister(route)
    feed("W 10 /work/late")
    assert route.queue.empty()
    await tracer.stop()


@pytest.mark.anyio
async def test_tracer_reports_sequence_gaps_once() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts([]), {})
    feed = Feed(tracer, cpu=3)
    feed("W 1 /a")
    feed.seq += 5
    feed("W 1 /b")
    for _ in range(70):
        feed("W 1 /c")
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 7}}))
    tracer.settle()

    assert drain(route) == [
        Lost(5, "kernel record gap", (3,), 10, 70),
        Lost(None, "the kernel dropped 2 records no sequence hole located"),
    ]
    assert (tracer.kernel_lost, tracer.located, tracer.unlocated) == (7, 5, 2)
    tracer.settle()
    assert route.queue.empty()
    await tracer.stop()


def test_sequence_waits_for_late_records() -> None:
    seq = Sequence(window=4)
    assert seq.observe(Stamp(10, 0, 1)) == []
    assert seq.observe(Stamp(40, 0, 4)) == []
    assert seq.observe(Stamp(20, 0, 2)) == []
    assert seq.observe(Stamp(50, 0, 5)) == []
    assert seq.observe(Stamp(60, 0, 6)) == []
    assert seq.observe(Stamp(70, 0, 7)) == [Gap(0, 3, 1, 10, 40)]
    assert seq.observe(Stamp(30, 0, 3)) == []
    assert seq.observe(Stamp(80, 0, 8)) == []


def test_sequence_counts_a_lost_first_record_and_expires_idle_cpus() -> None:
    seq = Sequence()
    assert seq.observe(Stamp(100, 1, 3)) == []
    assert seq.observe(Stamp(5_000, 2, 1)) == []
    assert seq.expire(101) == [Gap(1, 1, 2, None, 100)]
    assert seq.expire(10_000) == []
    assert seq.latest == 5_000


@pytest.mark.anyio
async def test_route_reports_overflow() -> None:
    route = tracer_module.Route(1, Mounts([]), {}, asyncio.Queue(2))
    for pid in range(5):
        route.put(Fork(0, pid, at=Stamp(100 + pid, 0, pid + 1)))
    assert route.overflow == 3
    route.queue.get_nowait()
    route.queue.get_nowait()
    late = Fork(0, 9, at=Stamp(200, 0, 9))
    route.put(late)
    assert [route.queue.get_nowait(), route.queue.get_nowait()] == [
        Lost(3, "audit queue full", (), 102, 104),
        late,
    ]


@pytest.mark.anyio
async def test_env_of_an_exec_never_keeps_an_earlier_exec_tail() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts([]), {})
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    long, short = b"A=1\0OLD=stale\0", b"B=2\0"
    feed(f"G 10 {len(long)} 0 {hx(long)}")
    feed(f"G 10 {len(short)} 0 {hx(short)}")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'ls\0')}")

    execs = [r for r in drain(route) if isinstance(r, Exec)]
    assert [(e.env, e.env_bytes) for e in execs] == [((), 0), (("B=2",), len(short))]
    await tracer.stop()


class StuckContainer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def exec(self, *args: object, **kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        await asyncio.Event().wait()
        raise AssertionError


@pytest.mark.anyio
@pytest.mark.parametrize("container", [StuckContainer(), StuckContainer(OSError("gone"))])
async def test_pmu_probe_failure_leaves_the_tracer_running(
    monkeypatch: pytest.MonkeyPatch, container: StuckContainer
) -> None:
    monkeypatch.setattr(tracer_module, "PMU_S", 0.05)
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    await tracer._probe_pmu(cast(Any, container))
    assert tracer.pmu is None
