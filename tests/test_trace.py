import asyncio
import json
from typing import Any, cast

import aiodocker
import pytest

from aims.events import MAX_INT
from aims.sandbox import execs as execs_module
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
    Ipc,
    Lost,
    Mem,
    Net,
    Ns,
    Oom,
    Output,
    Ptrace,
    Setattr,
    Signal,
    Stamp,
)
from aims.sandbox.trace import (
    DEPTH,
    STRLEN,
    SYSCALLS,
    Bprm,
    Context,
    Delete,
    Dropped,
    EnvChunk,
    Gap,
    Garbled,
    Identity,
    Link,
    Mkdir,
    Mounts,
    Note,
    RawClose,
    RawDenied,
    RawExec,
    RawFifo,
    RawMmap,
    RawSetattr,
    Ready,
    Rename,
    Sequence,
    Symlink,
    Tick,
    Walk,
    namespaces,
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


def release(pid: int, size: int, dev: int, *names: str) -> str:
    depth, text = walk(*names)
    return f"Z {pid} {size} {dev} {depth} {text}"


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
    assert parse(json.dumps({"type": "printf", "data": "Q 5000 0 1\n"})) == Tick(PARSED)


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
    assert parse(line("C 10 c 4 169.254.169.254 80")) == Net(
        10, "connect", "inet", "169.254.169.254", 80, at=PARSED
    )
    assert parse(line("C 10 s 6 ::1 53")) == Net(10, "sendto", "inet6", "::1", 53, at=PARSED)


def mkdir(pid: int, dev: int, *names: str) -> str:
    depth, text = walk(*names)
    return f"Y {pid} d {dev} {depth} {text}"


def symlink(pid: int, dev: int, target: bytes, *names: str, length: int | None = None) -> str:
    depth, text = walk(*names)
    size = len(target) if length is None else length
    return f"Y {pid} s {dev} {depth} {size} {hx(target)} {text}"


def link(pid: int, devs: tuple[int, int], old: tuple[str, ...], new: tuple[str, ...]) -> str:
    (old_depth, old_text), (new_depth, new_text) = walk(*old), walk(*new)
    return f"Y {pid} l {devs[0]} {devs[1]} {old_depth} {new_depth} {old_text}/{new_text}"


def test_paths_at_the_string_cap_are_partial() -> None:
    near, cut = "/work/" + "a" * (STRLEN - 8), "/work/" + "a" * (STRLEN - 7)
    assert parse(line(f"W 10 {near}")) == File(10, "write", near, at=PARSED)
    assert parse(line(f"W 10 {cut}")) == File(10, "write", cut, partial=True, at=PARSED)
    assert parse(line("W 10 ")) == File(10, "write", "", partial=True, at=PARSED)
    assert parse(line(f"A 10 {cut}")) == File(10, "read", cut, partial=True, at=PARSED)
    assert parse(line(f"P 10 13 {cut}")) == File(
        10, "denied", cut, errno=13, partial=True, at=PARSED
    )
    assert parse(line(f"M 10 x 42 0 {cut}")) == Mem(10, "exec", cut, 42, 0, partial=True, at=PARSED)
    assert Bprm(10, cut, at=PARSED).fields()["path_partial"] is True
    assert Bprm(10, near, at=PARSED).fields()["path_partial"] is False
    whole, full, kept = "a" * (STRLEN - 1), "a" * STRLEN, "a" * (STRLEN - 4)
    s = Walk(VDA1, (b"s", b"/"))
    assert parse(line(symlink(10, VDA1, whole.encode(), "s"))) == Symlink(10, s, whole, at=PARSED)
    assert parse(line(symlink(10, VDA1, full.encode(), "s"))) == Symlink(
        10, s, full, True, at=PARSED
    )
    for length in (STRLEN - 3, STRLEN):
        assert parse(line(symlink(10, VDA1, kept.encode(), "s", length=length))) == Symlink(
            10, s, kept, True, at=PARSED
        )


def test_bytes_bpftrace_escapes_on_x86_are_restored() -> None:
    raw = r'{"type": "printf", "data": "W 5000 0 1 10 /a b\uffffffff caf\uffffffc3\uffffffa9\n"}'
    assert parse(raw) == File(10, "write", "/a b\\xff café", at=PARSED)


def test_odd_bytes_never_hide_a_cut_path() -> None:
    odd = "/work/" + "\\xff" * (STRLEN // 4)
    item = parse(line(f"W 10 {odd}"))
    assert isinstance(item, File)
    assert item.partial


def test_parse_ipc_sockets() -> None:
    path = b"/var/run/x.sock\0"
    assert parse(line(f"I 10 b 1 {hx(path)}")) == Ipc(
        10, "bind", "unix", "/var/run/x.sock", at=PARSED
    )
    assert parse(line(f"I 10 c 1 {hx(path)}")) == Ipc(
        10, "connect", "unix", "/var/run/x.sock", at=PARSED
    )
    abstract = b"\0hidden\0"
    assert parse(line(f"I 10 c 1 {hx(abstract)}")) == Ipc(
        10, "connect", "unix", "@hidden", at=PARSED
    )
    relative = b"../x.sock\0"
    assert parse(line(f"I 10 b 1 {hx(relative)}")) == Ipc(
        10, "bind", "unix", "../x.sock", partial=True, at=PARSED
    )


def test_parse_ipc_fifo_and_shm() -> None:
    depth, text = walk("f", "_data", "aims-r-a1-work", "volumes", "docker")
    item = parse(line(f"I 10 f 0 {VDA1} {depth} {text}"))
    assert item == RawFifo(
        10,
        Walk(VDA1, (b"f", b"_data", b"aims-r-a1-work", b"volumes", b"docker", b"/")),
        at=PARSED,
    )
    assert parse(line("I 10 g 2 7 4096 384")) == Ipc(
        10, "shmget", "sysv", key=7, size=4096, flags=384, at=PARSED
    )
    assert parse(line("I 10 a 2 9 0")) == Ipc(10, "shmat", "sysv", id=9, flags=0, at=PARSED)


def test_parse_ptrace_and_signal() -> None:
    assert parse(line("K 10 11 16 -1")) == Ptrace(10, 11, "attach", True, 1, at=PARSED)
    assert parse(line("K 10 11 5 123")) == Ptrace(10, 11, "pokedata", False, None, at=PARSED)
    assert parse(line("K 10 11 -1 24")) == Ptrace(
        10, 11, "process_vm_writev", False, None, at=PARSED
    )
    assert parse(line("S 10 11 15")) == Signal(10, 11, 15, at=PARSED)


def test_parse_mem_exec_and_mmap() -> None:
    assert parse(line("M 10 x 42 0 /proc/self/fd/3")) == Mem(
        10, "exec", "/proc/self/fd/3", 42, 0, at=PARSED
    )
    depth, text = walk("m", "_data", "aims-r-a1-work", "volumes", "docker")
    item = parse(line(f"M 10 m 43 1 {VDA1} 5 {depth} {text}"))
    assert item == RawMmap(
        10,
        Walk(VDA1, (b"m", b"_data", b"aims-r-a1-work", b"volumes", b"docker", b"/")),
        43,
        1,
        5,
        at=PARSED,
    )


def test_parse_setattr() -> None:
    depth, text = walk("f", "_data", "aims-r-a1-work", "volumes", "docker")
    item = parse(
        line(f"H 10 {VDA1} {depth} 65 33152 0 0 1 978307200000000 978307200000000 0 {text}")
    )
    assert item == RawSetattr(
        10,
        Walk(VDA1, (b"f", b"_data", b"aims-r-a1-work", b"volumes", b"docker", b"/")),
        65,
        33152,
        0,
        0,
        1,
        978307200000000,
        978307200000000,
        0,
        at=PARSED,
    )


def test_parse_namespace_changes() -> None:
    assert parse(line("J 10 u 268435456")) == Ns(10, "unshare", 268435456, ("user",), at=PARSED)
    assert parse(line("J 10 n 0")) == Ns(10, "setns", 0, (), at=PARSED)
    flags = 0x20000 | 0x40000000
    assert parse(line(f"J 10 u {flags}")) == Ns(10, "unshare", flags, ("mount", "net"), at=PARSED)


def test_parse_exec_identity() -> None:
    assert parse(line("L 10 1 42 254 1 61267 100 978307200000000 0")) == Identity(
        10, 1, 42, 254, 1, 61267, 100, 978307200000000, 0, at=PARSED
    )


def test_large_numbers_are_clamped_to_the_canonical_range() -> None:
    huge = 2**63 + 7
    depth, text = walk("f", "_data", "aims-r-a1-work", "volumes", "docker")
    walk_names = (b"f", b"_data", b"aims-r-a1-work", b"volumes", b"docker", b"/")
    assert parse(line(f"I 10 g 2 {huge} {huge} {huge}")) == Ipc(
        10, "shmget", "sysv", key=MAX_INT, size=MAX_INT, flags=MAX_INT, at=PARSED
    )
    assert parse(line(f"I 10 a 2 {huge} {huge}")) == Ipc(
        10, "shmat", "sysv", id=MAX_INT, flags=MAX_INT, at=PARSED
    )
    assert parse(line(f"K 10 {huge} 16 -{huge}")) == Ptrace(
        10, MAX_INT, "attach", True, MAX_INT, at=PARSED
    )
    assert parse(line(f"S 10 {huge} {huge}")) == Signal(10, MAX_INT, MAX_INT, at=PARSED)
    assert parse(line(f"M 10 x {huge} {huge} /memfd:x")) == Mem(
        10, "exec", "/memfd:x", MAX_INT, MAX_INT, at=PARSED
    )
    assert parse(line(f"M 10 m {huge} {huge} {VDA1} {huge} {depth} {text}")) == RawMmap(
        10, Walk(VDA1, walk_names), MAX_INT, MAX_INT, MAX_INT, at=PARSED
    )
    assert parse(
        line(f"H 10 {VDA1} {depth} {huge} {huge} {huge} {huge} {huge} {huge} {huge} {huge} {text}")
    ) == RawSetattr(
        10,
        Walk(VDA1, walk_names),
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        at=PARSED,
    )
    assert parse(line(f"J 10 u {huge}")) == Ns(
        10, "unshare", MAX_INT, namespaces(MAX_INT), at=PARSED
    )
    assert parse(line(f"L 10 {huge} {huge} {huge} {huge} {huge} {huge} {huge} {huge}")) == Identity(
        10,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        MAX_INT,
        at=PARSED,
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
    item = parse(line(release(10, 42, VDA1, "a b", "_data")))
    assert item == RawClose(10, 42, Walk(VDA1, (b"a b", b"_data", b"/")), at=PARSED)
    item = parse(line(release(10, 2**62, TMP, "sparse")))
    assert item == RawClose(10, MAX_INT, Walk(TMP, (b"sparse", b"/")), at=PARSED)


def test_parse_creations() -> None:
    item = parse(line(mkdir(10, VDA1, "e f", "d", "_data")))
    assert item == Mkdir(10, Walk(VDA1, (b"e f", b"d", b"_data", b"/")), at=PARSED)
    item = parse(line(symlink(10, TMP, b"/etc/sha dow\n\xff", "s p", "x")))
    s = Walk(TMP, (b"s p", b"x", b"/"))
    assert item == Symlink(10, s, "/etc/sha dow\n\\xff", at=PARSED)
    item = parse(line(link(10, (VDA1, TMP), ("f", "_data"), ("h",))))
    old, new = Walk(VDA1, (b"f", b"_data", b"/")), Walk(TMP, (b"h", b"/"))
    assert item == Link(10, old, new, at=PARSED)


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
        "Q 10 1 2",
        "I 10",
        "I 10 z 0",
        f"I 10 b 2 {hx(b'/x')}",
        "I 10 b 1 ",
        "I 10 g 2 7 4096",
        "I 10 a 2 9",
        "I 10 g 1 7 4096 384",
        "I 10 a 3 9 0",
        "I 10 f 0 1 1 bad",
        "K 10 1",
        "K 10 11 16",
        "K 10 x 16 -1",
        "S 10 11",
        "S 10 x 15",
        "S 10 11 x",
        "M 10 x 42 0",
        "M 10 x x 0 /x",
        "M 10 q 1",
        "M 10 m 1 2 3 4 5",
        "M 10 m 42 1 1 5 2 a/b//",
        "H 10 1 2 3",
        "H 10 1 2 3 4 5 6 7 8 9 10",
        "H 10 1 2 x 4 5 6 7 8 9 10 " + walk("a")[1],
        f"H 10 {VDA1} 99 65 33152 0 0 1 0 0 0 {walk('a')[1]}",
        "J 10 x 0",
        "J 10 u",
        "J 10 u x",
        "J 10 u 0 0",
        "L 10 1 2 3",
        "L 10 1 2 3 4 5 6 x y",
        "Y 10",
        "Y 10 q 1 1 " + walk("a")[1],
        "Y 10 d 1 2 a/b//",
        "Y 10 d 1 x " + walk("a")[1],
        "Y 10 s 1 1 " + walk("a")[1],
        "Y 10 s 1 1 3 nothex " + walk("a")[1],
        f"Y 10 s 1 1 1 {hx(b'ab')} {walk('a')[1]}",
        f"Y 10 l 1 1 2 1 {walk('a')[1]}",
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
            release(11, 5, VDA1, "out", "_data", "aims-r-a1-work", "volumes", "docker"),
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
        File(11, "write", "/work/after-exit", at=s[13]),
        Close(11, 5, "/work/out", at=s[14]),
        Lost(1, f"unparseable tracer line: E {s[-1].ts * 1000} 0 {s[-1].seq} 11 broken"),
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
    feed("W 11 /work/late")
    assert route.queue.empty()
    await tracer.stop()


@pytest.mark.anyio
async def test_exited_pids_route_until_reused() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    feed("F 10 11")
    feed("F 10 12")
    feed("X 11 0 0")
    feed("X 12 0 0")
    drain(route)

    late = feed(release(11, 3, TMP, "c"))
    feed("F 7 11")
    feed("W 11 /work/reused")
    feed(f"E 12 1 1 1000 77 5 3 {hx(b'sh\0')}")
    feed("W 12 /work/reused")
    assert drain(route) == [Close(11, 3, "/tmp/c", at=late)]
    await tracer.stop()


@pytest.mark.anyio
async def test_tracer_resolves_new_records() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    drain(route)

    fifo_depth, fifo_text = walk("pipe", "_data", "aims-r-a1-work", "volumes", "docker")
    mmap_depth, mmap_text = walk("code.so", "_data", "aims-r-a1-work", "volumes", "docker")
    note_depth, note_text = walk("note", "_data", "aims-r-a1-work", "volumes", "docker")
    s = [
        feed("I 10 b 1 " + hx(b"/work/s.sock\0")),
        feed("K 10 1 16 -1"),
        feed("S 10 1 15"),
        feed("M 10 x 42 0 /memfd:x"),
        feed("J 10 u 268435456"),
        feed(f"I 10 f 0 {VDA1} {fifo_depth} {fifo_text}"),
        feed(f"M 10 m 43 1 {VDA1} 5 {mmap_depth} {mmap_text}"),
        feed(f"H 10 {VDA1} {note_depth} 65 33152 0 0 1 0 0 0 {note_text}"),
        feed(mkdir(10, VDA1, "d", "_data", "aims-r-a1-work", "volumes", "docker")),
        feed(
            symlink(10, VDA1, b"/etc/shadow", "s", "_data", "aims-r-a1-work", "volumes", "docker")
        ),
        feed(symlink(10, TMP, b"a" * (STRLEN - 4), "long", length=STRLEN)),
        feed(link(10, (VDA1, TMP), ("f", "_data", "aims-r-a1-work", "volumes", "docker"), ("h",))),
    ]

    assert drain(route) == [
        Ipc(10, "bind", "unix", "/work/s.sock", at=s[0]),
        Ptrace(10, 1, "attach", True, 1, at=s[1]),
        Signal(10, 1, 15, at=s[2]),
        Mem(10, "exec", "/memfd:x", 42, 0, at=s[3]),
        Ns(10, "unshare", 268435456, ("user",), at=s[4]),
        Ipc(10, "fifo", path="/work/pipe", at=s[5]),
        Mem(10, "mmap", "/work/code.so", 43, 1, 5, at=s[6]),
        Setattr(10, "/work/note", False, 65, 33152, 0, 0, 1, 0, 0, 0, at=s[7]),
        File(10, "mkdir", "/work/d", at=s[8]),
        File(10, "symlink", "/work/s", "/etc/shadow", at=s[9]),
        File(10, "symlink", "/tmp/long", "a" * (STRLEN - 4), partial=True, at=s[10]),
        File(10, "link", "/tmp/h", "/work/f", at=s[11]),
    ]
    await tracer.stop()


@pytest.mark.anyio
async def test_exec_path_at_the_cap_is_partial() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed("B 10 /work/" + "a" * STRLEN)
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    (record,) = drain(route)
    assert isinstance(record, Exec)
    assert record.path_partial
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
    monkeypatch.setattr(tracer_module, "PMU_S", 0)
    monkeypatch.setattr(execs_module, "GRACE_S", 0.05)
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    await tracer._probe_pmu(cast(Any, container))
    assert tracer.pmu is None
