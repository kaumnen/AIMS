import asyncio
import json
from typing import cast

import aiodocker
import pytest

from aims.sandbox import tracer as tracer_module
from aims.sandbox.base import Capture, Denied, Exec, Exit, File, Fork, Lost, Net
from aims.sandbox.trace import (
    DEPTH,
    SYSCALLS,
    Bprm,
    Delete,
    Dropped,
    Garbled,
    Mounts,
    Note,
    RawDenied,
    RawExec,
    Ready,
    Rename,
    Walk,
    parse,
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


def line(text: str) -> str:
    return json.dumps({"type": "printf", "data": f"{text}\n"})


def walk(*names: str) -> str:
    parts = [n.encode() for n in names] + [b"/"] * (DEPTH - len(names))
    return " ".join(hx(p) for p in parts[:DEPTH])


def test_parse_exec_splits_argv_exactly() -> None:
    argv = b"sh\0-c\0echo 'a b' \\x00 \"q\"\0"
    item = parse(line(f"E 10 1 1 1000 99 5 {len(argv)} {hx(argv)}"))
    assert item == RawExec(
        10, 1, True, 1000, 99, 5, len(argv), ("sh", "-c", "echo 'a b' \\x00 \"q\"")
    )


def test_parse_simple_lines() -> None:
    assert parse(line("R 0")) == Ready(0)
    assert parse(line("F 10 11")) == Fork(10, 11)
    assert parse(line("B 10 /usr/bin/my tool")) == Bprm(10, "/usr/bin/my tool")
    assert parse(line("X 10 3 0")) == Exit(10, 3, None)
    assert parse(line("X 10 0 9")) == Exit(10, None, 9)
    assert parse(line("W 10 /work/a b.txt")) == File(10, "write", "/work/a b.txt")
    assert parse(line("A 10 /work/x")) == File(10, "read", "/work/x")
    assert parse(line("P 10 13 /etc/shadow")) == File(10, "denied", "/etc/shadow", errno=13)
    assert parse(line("P 10 1 rel")) == File(10, "denied", "rel", errno=1, partial=True)
    assert parse(line("D 10 97 1")) == RawDenied(10, 97, 1)
    assert parse(line("C 10 c 4 169.254.169.254 80")) == Net(
        10, "connect", "inet", "169.254.169.254", 80
    )
    assert parse(line("C 10 s 6 ::1 53")) == Net(10, "sendto", "inet6", "::1", 53)


def test_parse_unix_sockets() -> None:
    path = b"/var/run/x.sock".ljust(108, b"\0")
    assert parse(line(f"C 10 c 1 {hx(path)} 0")) == Net(
        10, "connect", "unix", "/var/run/x.sock", None
    )
    abstract = b"\0hidden".ljust(108, b"\0")
    assert parse(line(f"C 10 c 1 {hx(abstract)} 0")) == Net(10, "connect", "unix", "@hidden", None)


def test_parse_walks() -> None:
    item = parse(line(f"U 10 {VDA1} {walk('c.txt', 'b', '_data')}"))
    assert isinstance(item, Delete)
    assert item.walk.names[:4] == (b"c.txt", b"b", b"_data", b"/")
    item = parse(line(f"N 10 {VDA1} {TMP} {walk('a')} {walk('b')}"))
    assert isinstance(item, Rename)
    assert (item.old.dev, item.new.dev) == (VDA1, TMP)


@pytest.mark.parametrize(
    "text",
    [
        "E 10 1 1 1000",
        "E 10 1 1 1000 99 5 3 notahex",
        "U 10 1 \\x41",
        "X 10 zero 0",
        "Q 10",
        f"N 10 1 2 {walk('a')}",
    ],
)
def test_parse_rejects_misshapen_lines(text: str) -> None:
    item = parse(line(text))
    assert isinstance(item, Garbled)
    assert item.line == text


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


@pytest.mark.anyio
async def test_tracer_routes_by_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracer_module, "CONFIRM_S", 0.05)
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    argv = b"sh\0-c\0ls\0"
    for text in [
        "B 10 /bin/sh",
        f"E 10 1 1 1000 99 5 {len(argv)} {hx(argv)}",
        "F 10 11",
        f"E 11 10 0 1000 99 6 3 {hx(b'ls\0')}",
        "W 11 /work/out",
        f"U 11 {VDA1} {walk('out', '_data', 'aims-r-a1-work', 'volumes', 'docker')}",
        f"N 11 {TMP} {TMP} {walk('a')} {walk('b')}",
        "D 11 97 1",
        "D 11 21 1",
        "C 11 c 4 1.1.1.1 443",
        "X 11 0 0",
        "W 11 /work/after-exit",
        f"E 50 1 1 1000 77 5 {len(argv)} {hx(argv)}",
        "W 50 /other",
        "E 11 broken",
    ]:
        tracer._handle(line(text))
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 2}}))

    got = []
    while not route.queue.empty():
        got.append(route.queue.get_nowait())
    assert got == [
        Exec(10, 1, 5, 1000, "/bin/sh", ("sh", "-c", "ls"), len(argv), True),
        Fork(10, 11),
        Exec(11, 10, 6, 1000, "ls", ("ls",), 3, False),
        File(11, "write", "/work/out"),
        File(11, "delete", "/work/out"),
        File(11, "rename", "/tmp/a", "/tmp/b"),
        Denied(11, "unshare", 1),
        Net(11, "connect", "inet", "1.1.1.1", 443),
        Exit(11, 0, None),
        Lost(2, "kernel buffer overflow"),
    ]
    assert tracer.garbled == 1
    assert await tracer.confirm(10, 99)
    assert await tracer.confirm(50, 77)
    assert not await tracer.confirm(10, 77)
    assert not await tracer.confirm(12345, 99)

    tracer.unregister(route)
    tracer._handle(line("W 10 /work/late"))
    assert route.queue.empty()


@pytest.mark.anyio
async def test_route_reports_overflow() -> None:
    route = tracer_module.Route(1, Mounts([]), {}, asyncio.Queue(2))
    for pid in range(5):
        route.put(Fork(0, pid))
    assert route.overflow == 3
    route.queue.get_nowait()
    route.queue.get_nowait()
    route.put(Fork(0, 9))
    assert [route.queue.get_nowait(), route.queue.get_nowait()] == [
        Lost(3, "audit queue full"),
        Fork(0, 9),
    ]
