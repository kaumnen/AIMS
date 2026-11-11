import asyncio
import gc
import json
import logging
from typing import Any, cast

import aiodocker
import pytest

from aims.events import MAX_INT
from aims.sandbox import execs as execs_module
from aims.sandbox import tracer as tracer_module
from aims.sandbox.base import (
    Cache,
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
    Sync,
)
from aims.sandbox.trace import (
    DEPTH,
    FORGET_US,
    LATE_US,
    STRLEN,
    SYSCALLS,
    UNGROUPED,
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
    namespaces,
    parse,
)
from aims.sandbox.trace import (
    PIPEFS_MAGIC as PIPEFS,
)
from aims.sandbox.tracer import (
    OWN_GAP,
    UNHANDLED,
    UNKNOWN,
    Tracer,
    parse_counts,
    parse_preemption,
    parse_probes,
    parse_skips,
)

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


CG = 512
AT = Stamp(5000, 0, 1, CG)
PARSED = Stamp(5, 0, 1, CG)


def line(text: str, at: Stamp = AT) -> str:
    tag, rest = text.split(" ", 1)
    head = f"{tag} {at.ts} {at.cpu} {at.seq}" + ("" if tag in UNGROUPED else f" {at.cgroup}")
    return json.dumps({"type": "printf", "data": f"{head} {rest}\n"})


class Feed:
    """Lines from one cpu with consecutive sequence numbers in their stream: cgroup `CG`, or the
    tracer's own for R and Q."""

    def __init__(self, tracer: Tracer, cpu: int = 0, cgroup: int = CG) -> None:
        self.tracer, self.cpu, self.cgroup, self.seq, self.own = tracer, cpu, cgroup, 0, 0

    def __call__(self, text: str) -> Stamp:
        if text.split(" ", 1)[0] in UNGROUPED:
            self.own += 1
            seq, cgroup, ts = self.own, 0, max(self.seq, 1)
        else:
            self.seq += 1
            seq, cgroup, ts = self.seq, self.cgroup, self.seq
        self.tracer._handle(line(text, Stamp(ts * 10_000, self.cpu, seq, cgroup)))
        return Stamp(ts * 10, self.cpu, seq, cgroup)


def claim(tracer: Tracer, pidns: int, cgroup: int = CG) -> None:
    """Tie `cgroup` to the sandbox of `pidns`, as the runtime's first exec in it does."""
    tracer._claim(cgroup, pidns)


def walk(*names: str) -> tuple[int, str]:
    """The depth and the `/`-joined names bpftrace prints for a dentry walk."""
    names = names[:DEPTH]
    return len(names), "/".join([*names, *["/"] * (DEPTH - len(names))])


def unlink(pid: int, dev: int, *names: str) -> str:
    depth, text = walk(*names)
    return f"U {pid} {dev} {depth} {text}"


def late_tick(tracer: Tracer, ts_us: int) -> None:
    """A heartbeat from an idle cpu at `ts_us`, so held and missing records ripen."""
    tracer._handle(json.dumps({"type": "printf", "data": f"Q {ts_us * 1000} 7 1\n"}))


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
    wire = Stamp(9_007_199_254_740_993_123, 7, 123456789012, 2**64 - 1)
    parsed = Stamp(9_007_199_254_740_993, 7, 123456789012, 2**64 - 1)
    assert parse(line("F 10 11 2", wire)) == Fork(10, 11, 2, at=parsed)
    own = Stamp(5, 0, 1)
    ready = parse(line("R 0 1793054641268664792", AT))
    assert ready == Ready(0, 5, 1793054641268664, own)
    assert parse(json.dumps({"type": "printf", "data": "Q 5000 0 1\n"})) == Tick(own)


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
    assert parse(line("F 10 11 2")) == Fork(10, 11, 2, at=PARSED)
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
    raw = (
        r'{"type": "printf", "data": "W 5000 0 1 512 10 /a b\uffffffff caf\uffffffc3\uffffffa9\n"}'
    )
    assert parse(raw) == File(10, "write", "/a b\\xff café", at=PARSED)


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/work/\uffffff80.py", "/work/\uffffff80.py"),
        (r"/a\\uffffff80", r"/a\uffffff80"),
        (r"/a\\\uffffffc3\uffffffa9", r"/a\é"),
    ],
    ids=["real-uffff", "escaped-backslash", "backslash-then-byte"],
)
def test_only_the_x86_escape_is_restored(path: str, want: str) -> None:
    raw = f'{{"type": "printf", "data": "W 5000 0 1 512 10 {path}\\n"}}'
    assert parse(raw) == File(10, "write", want, at=PARSED)


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
    assert parse(line(f"I 10 s 1 {hx(path)}")) == Ipc(
        10, "sendto", "unix", "/var/run/x.sock", at=PARSED
    )
    abstract = b"\0hidden\0"
    assert parse(line(f"I 10 c 1 {hx(abstract)}")) == Ipc(
        10, "connect", "unix", "@hidden", at=PARSED
    )
    assert parse(line(f"I 10 s 1 {hx(abstract)}")) == Ipc(
        10, "sendto", "unix", "@hidden", at=PARSED
    )
    relative = b"../x.sock\0"
    assert parse(line(f"I 10 b 1 {hx(relative)}")) == Ipc(
        10, "bind", "unix", "../x.sock", partial=True, at=PARSED
    )
    assert parse(line(f"I 10 s 1 {hx(relative)}")) == Ipc(
        10, "sendto", "unix", "../x.sock", partial=True, at=PARSED
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
    assert parse(line("K 10 11 16 -1 1")) == Ptrace(
        10, 11, "attach", True, 1, ns_target=1, at=PARSED
    )
    assert parse(line("K 10 11 5 123 4")) == Ptrace(
        10, 11, "pokedata", False, None, ns_target=4, at=PARSED
    )
    assert parse(line("K 10 11 -1 24 4")) == Ptrace(
        10, 11, "process_vm_writev", False, None, ns_target=4, at=PARSED
    )
    assert parse(line("K 10 9 0 0 77")) == Ptrace(10, 9, "traceme", False, None, at=PARSED)
    # -2: the kernel found no process for the pid named; -1: its lookup was not seen.
    assert parse(line("K 10 -2 16 -3 99999")) == Ptrace(
        10, None, "attach", True, 3, ns_target=99999, at=PARSED
    )
    assert parse(line("K 10 -1 -1 -3 99999")) == Ptrace(
        10, None, "process_vm_writev", True, 3, ns_target=99999, at=PARSED
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
    mounts = Mounts.parse(MOUNTINFO)
    pipe = parse(line(f"H 10 14 0 2118 0 1000 0 0 0 0 0 {'/' * (DEPTH - 1)}"))
    assert isinstance(pipe, RawSetattr)
    assert pipe.walk == Walk(14, (b"",))
    assert mounts.resolve(pipe.walk) == (".../", True)
    eventfd = parse(line(f"H 10 15 0 65 384 0 0 0 0 0 0 {'/'.join(['[eventfd]'] * DEPTH)}"))
    assert isinstance(eventfd, RawSetattr)
    assert eventfd.walk == Walk(15, (b"[eventfd]",))
    assert mounts.resolve(eventfd.walk) == (".../[eventfd]", True)


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
    assert parse(line(f"K 10 {huge} 16 -{huge} {huge}")) == Ptrace(
        10, MAX_INT, "attach", True, MAX_INT, ns_target=MAX_INT, at=PARSED
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


def test_parse_page_cache_and_sync_lines() -> None:
    depth, text = walk("page", "_data", "aims-r-a1-work", "volumes", "docker")
    result = Walk(VDA1, (b"page", b"_data", b"aims-r-a1-work", b"volumes", b"docker", b"/"))
    for tag, op in (
        ("a", "fadvise_dontneed"),
        ("b", "preadv2_nowait"),
        ("c", "cachestat"),
    ):
        assert parse(line(f"{tag} 10 {VDA1} {depth} {text}")) == RawCache(10, op, result, at=PARSED)
    assert parse(line("a 10 0 -1")) == RawCache(10, "fadvise_dontneed", None, at=PARSED)
    assert parse(line("d 10")) == Cache(10, "mincore", None, at=PARSED)
    for tag, call in (
        ("e", "fsync"),
        ("f", "fdatasync"),
        ("g", "sync_file_range"),
        ("h", "syncfs"),
    ):
        assert parse(line(f"{tag} 10 {VDA1} {depth} {text}")) == RawSync(
            10, call, result, at=PARSED
        )
        assert parse(line(f"{tag} 10 0 -1")) == RawSync(10, call, None, at=PARSED)


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
    raw = b'{"type": "printf", "data": "W 5000 0 1 512 10 /work/scorer.py\xff\\n"}'
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
        f"H 10 15 0 65 384 0 0 0 0 0 0 {'/'.join(['[eventfd]'] * (DEPTH - 1))}",
        f"H 10 15 0 65 384 0 0 0 0 0 0 {'/'.join(['a'] * (DEPTH - 1) + ['b'])}",
        f"H 10 15 1 65 384 0 0 0 0 0 0 {'/'.join([''] + ['a'] * (DEPTH - 1))}",
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
    if tag in UNGROUPED:
        assert item == Garbled(10, f"{tag} 5000 0 1 {rest}", Stamp(5, 0, 1))
    else:
        assert item == Garbled(10, f"{tag} 5000 0 1 {CG} {rest}", PARSED)


@pytest.mark.parametrize("text", ["F 10 11", "F x 0 1 2 10 11", "F 10 0 1 x 10 11", "F", "Q 10"])
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


def test_capture_keeps_the_end_of_a_stream_past_its_head() -> None:
    capture = Capture(limit=4, tail=8)
    for chunk in (b"head", b"-lost\nlo", b"st\nkept\n"):
        capture.add(1, chunk)
    capture.add(2, b"short")
    assert capture.stdout_tail == b"st\nkept\n"
    assert capture.rest(1) == (b"kept\n", 11)
    assert capture.rest(2) == (b"t", 0)
    assert capture.stderr_tail == b"short"
    whole = Capture(limit=8, tail=8)
    whole.add(1, b"fits")
    assert whole.rest(1) is None
    assert whole.rest(2) is None


def test_the_end_of_a_capture_goes_on_from_its_head_when_nothing_is_left_out() -> None:
    capture = Capture(limit=4, tail=8)
    capture.add(1, b"0123456789")
    assert capture.rest(1) == (b"456789", 0)
    # The head stops inside a character, so the end starts at the next line.
    split = Capture(limit=4, tail=8)
    split.add(1, "abcé\nxyz".encode())
    assert split.rest(1) == (b"xyz", 2)
    # With no line break, it starts at the next character.
    binary = Capture(limit=4, tail=8)
    binary.add(1, "abcéxyz".encode())
    assert binary.rest(1) == (b"xyz", 1)


def test_the_end_of_a_capture_starts_at_a_line_or_a_character() -> None:
    capture = Capture(limit=1, tail=3)
    capture.add(1, ("a" + "é" * 5).encode())
    assert capture.rest(1) == ("é".encode(), 8)
    lines = Capture(limit=1, tail=6)
    lines.add(1, b"abcdefg\nhi\n")
    assert lines.rest(1) == (b"hi\n", 7)


def test_a_capture_keeps_its_tail_in_bounded_memory() -> None:
    capture = Capture(limit=0, tail=1024)
    data = bytes(range(256)) * 400
    for i in range(0, len(data), 7):
        capture.add(1, data[i : i + 7])
        assert len(capture._stdout_end) <= 2 * 1024 + 7
    assert capture.stdout_tail == data[-1024:]
    assert capture.stdout_bytes == len(data)


def test_syscall_tables_agree_on_names() -> None:
    arm, x86 = set(SYSCALLS["aarch64"].values()), set(SYSCALLS["x86_64"].values())
    assert {"unshare", "mount", "ptrace", "setns", "bpf"} <= arm & x86
    assert x86 - arm == {"mknod", "iopl", "ioperm"}


def drain(route: tracer_module.Route) -> list[object]:
    got = []
    while not route.queue.empty():
        got.append(route.queue.get_nowait())
    return got


def losses(route: tracer_module.Route) -> list[object]:
    return [r for r in drain(route) if isinstance(r, Lost)]


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
            "F 10 11 6",
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
            "E 11 broken",
        ]
    ]
    other = Feed(tracer, cpu=1, cgroup=CG + 1)
    other(f"E 50 1 1 1000 77 5 {len(argv)} {hx(argv)}")
    other("W 50 /other")
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 2}}))

    assert drain(route) == [
        Exec(10, 1, 5, 1000, "/bin/sh", ("sh", "-c", "ls"), len(argv), True, at=s[2]),
        Fork(10, 11, 6, at=s[3]),
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
        Lost(1, f"unparseable tracer line: E {s[-1].ts * 1000} 0 {s[-1].seq} {CG} 11 broken"),
    ]
    assert tracer.clock == (10, 0)
    assert tracer.garbled == 1
    assert (tracer.unrouted, tracer.foreign) == (0, 2)
    assert tracer.kernel_lost == 2
    assert await tracer.confirm(10, 99)
    assert await tracer.confirm(50, 77)
    assert not await tracer.confirm(10, 77)
    assert not await tracer.confirm(12345, 99)

    tracer.unregister(route)
    feed("W 10 /work/late")
    feed("W 11 /work/late")
    feed("W 12 /nobody")
    assert (tracer.unrouted, tracer.foreign) == (0, 4)
    late_tick(tracer, 5_000_000)
    assert route.queue.empty()
    # The sandbox's cgroup is no sandbox's now, so a new pid in it is foreign too.
    assert (tracer.unrouted, tracer.foreign) == (0, 5)
    await tracer.stop()


@pytest.mark.anyio
async def test_processes_running_at_registration_are_adopted() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    claim(tracer, 99)
    tracer.adopt(route, [5, 6])
    s = feed("X 6 0 9")
    t = feed("X 7 0 9")
    assert drain(route) == [Exit(6, None, 9, at=s), Lost(1, UNKNOWN, (), t.ts, t.ts)]
    assert (tracer.unrouted, tracer.foreign) == (1, 0)
    await tracer.stop()


@pytest.mark.anyio
async def test_adoption_replaces_a_stale_route() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    old = tracer.register(77, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 5 1 1 1000 77 5 3 {hx(b'sh\0')}")
    feed(f"E 6 1 1 1000 77 6 3 {hx(b'sh\0')}")
    feed("X 6 0 0")
    drain(old)
    tracer.adopt(route, [5, 6])
    s = feed("W 5 /work/a")
    t = feed("W 6 /work/b")
    assert drain(route) == [File(5, "write", "/work/a", at=s), File(6, "write", "/work/b", at=t)]
    assert drain(old) == []
    await tracer.stop()


@pytest.mark.anyio
async def test_runtime_setup_in_a_sandbox_cgroup_is_foreign() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    init = b"runc\0init\0"
    feed("A 70 /runc")
    feed(f"E 70 60 1 0 0 0 {len(init)} {hx(init)}")
    feed("W 70 /proc/70/oom_score_adj")
    feed("F 70 71 2")
    s = feed(f"E 71 70 1 1000 99 5 3 {hx(b'sh\0')}")
    t = feed("W 71 /work/x")
    feed("X 70 0 0")

    assert drain(route) == [
        Exec(71, 70, 5, 1000, "sh", ("sh",), 3, True, at=s),
        File(71, "write", "/work/x", at=t),
    ]
    assert (tracer.unrouted, tracer.foreign) == (0, 5)
    feed("W 80 /a")
    feed("W 80 /b")
    feed("X 80 0 0")
    assert tracer.unrouted == 3
    await tracer.stop()


@pytest.mark.anyio
async def test_runtime_stage_before_a_root_exec_is_foreign() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed("A 70 /proc/filesystems")
    feed("S 70 71 33")
    feed("A 70 /.aims/aims-exec")
    s = feed(f"E 70 60 1 0 99 5 10 {hx(b'aims-exec\0')}")
    w = feed("W 80 /work/x")
    t = feed(f"E 80 70 0 1000 99 6 3 {hx(b'sh\0')}")

    assert drain(route) == [
        Exec(70, 60, 5, 0, "aims-exec", ("aims-exec",), 10, True, at=s),
        Lost(1, UNKNOWN, (), w.ts, w.ts),
        Exec(80, 70, 6, 1000, "sh", ("sh",), 3, False, at=t),
    ]
    assert (tracer.unrouted, tracer.foreign) == (1, 3)
    assert json.loads(tracer.unrouted_lines[-1])["data"].endswith(" 80 /work/x\n")
    await tracer.stop()


@pytest.mark.anyio
async def test_exited_pids_route_until_reused() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    feed("F 10 11 2")
    feed("F 10 12 3")
    feed("X 11 0 0")
    feed("X 12 0 0")
    drain(route)

    late = feed(release(11, 3, TMP, "c"))
    feed("F 7 11 5")
    feed("W 11 /work/reused")
    feed(f"E 12 1 1 1000 77 5 3 {hx(b'sh\0')}")
    feed("W 12 /work/reused")
    assert drain(route) == [Close(11, 3, "/tmp/c", at=late)]
    await tracer.stop()


@pytest.mark.anyio
async def test_a_process_exits_once_when_its_last_thread_does() -> None:
    """Two threads exiting together can both see the group die; the second exit is dropped."""
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    drain(route)
    s = feed("X 10 3 0")
    feed("X 10 3 0")
    t = feed(release(10, 4, TMP, "out"))

    assert drain(route) == [Exit(10, 3, None, at=s), Close(10, 4, "/tmp/out", at=t)]
    assert (tracer.unrouted, tracer.foreign) == (0, 0)
    await tracer.stop()


@pytest.mark.anyio
async def test_exited_pids_stop_routing_after_a_while() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    feed("X 10 0 0")
    drain(route)
    tick(tracer, tracer_module.GONE_US + 1_000, 7, 1)
    w = feed("W 10 /work/late")
    tick(tracer, tracer_module.GONE_US + 2_000_000, 7, 2)

    assert drain(route) == [Lost(1, UNKNOWN, (), w.ts, w.ts)]
    assert tracer.unrouted == 1
    await tracer.stop()


@pytest.mark.anyio
async def test_unrouted_records_are_lost_in_the_sandbox_of_their_cgroup() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    one = tracer.register(98, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    two = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    claim(tracer, 98, CG + 1)
    claim(tracer, 99)
    feed = Feed(tracer)
    feed("R 0 999")
    s = feed("W 80 /work/a")
    t = feed("W 80 /work/b")
    Feed(tracer, cpu=1, cgroup=CG + 2)("W 81 /work/c")
    assert two.queue.empty()
    late_tick(tracer, 2_000_000)

    assert (drain(one), drain(two)) == ([], [Lost(2, UNKNOWN, (), s.ts, t.ts)])
    assert (tracer.unrouted, tracer.foreign) == (2, 1)
    assert json.loads(tracer.unrouted_lines[-1])["data"].endswith(" 80 /work/b\n")
    await tracer.stop()


@pytest.mark.anyio
async def test_confirm_waits_past_an_older_process_with_the_pid() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 77 5 3 {hx(b'sh\0')}")
    confirm = asyncio.create_task(tracer.confirm(10, 99))
    await asyncio.sleep(0)
    assert not confirm.done()
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")

    assert await confirm
    await tracer.stop()


@pytest.mark.anyio
async def test_quiesce_waits_for_every_process_to_exit() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    tracer.alive = True
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    feed("F 10 11 2")
    quiet = asyncio.create_task(tracer.quiesce(route, 5))
    feed("X 11 0 0")
    await asyncio.sleep(0.01)
    assert not quiet.done()
    feed("X 10 0 9")
    async with asyncio.timeout(1):
        await quiet

    feed(f"E 20 1 1 1000 99 5 3 {hx(b'sh\0')}")
    async with asyncio.timeout(1):
        await tracer.quiesce(route, 0.1)
    tracer.alive = False
    await tracer.stop()


@pytest.mark.anyio
async def test_tracer_resolves_cache_and_sync_paths() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    drain(route)
    depth, text = walk("page", "_data", "aims-r-a1-work", "volumes", "docker")
    at = [
        feed(f"a 10 {VDA1} {depth} {text}"),
        feed(f"e 10 {VDA1} {depth} {text}"),
        feed("d 10"),
        feed("h 10 0 -1"),
    ]
    assert drain(route) == [
        Cache(10, "fadvise_dontneed", "/work/page", at=at[0]),
        Sync(10, "fsync", "/work/page", at=at[1]),
        Cache(10, "mincore", None, at=at[2]),
        Sync(10, "syncfs", None, at=at[3]),
    ]
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
        feed("K 10 1 16 -1 1"),
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
        Ptrace(10, 1, "attach", True, 1, ns_target=1, at=s[1]),
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
    claim(tracer, 99)
    tracer.adopt(route, [1])
    feed = Feed(tracer, cpu=3)
    feed("W 1 /a")
    feed.seq += 5
    feed("W 1 /b")
    for _ in range(70):
        feed("W 1 /c")
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 7}}))
    tracer.settle()
    assert losses(route) == [Lost(5, "kernel record gap", (3,), 10, 70)]
    late_tick(tracer, 5_000_000)

    unlocated = Lost(None, "the kernel dropped 2 records no sequence hole located", scope="tracer")
    assert losses(route) == [unlocated]
    assert (tracer.kernel_lost, tracer.located, tracer.unlocated) == (7, 5, 2)
    tracer.settle()
    assert route.queue.empty()
    await tracer.stop()


@pytest.mark.anyio
async def test_a_hole_is_lost_only_in_the_sandbox_whose_stream_it_is_in() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    one = tracer.register(98, Mounts([]), {})
    two = tracer.register(99, Mounts([]), {})
    claim(tracer, 98, CG + 1)
    claim(tracer, 99)
    tracer.adopt(one, [1])
    tracer.adopt(two, [2])
    busy = Feed(tracer, cpu=3)
    busy("W 2 /a")
    busy.seq += 4
    busy("W 2 /b")
    Feed(tracer, cpu=3, cgroup=CG + 1)("W 1 /c")
    stray = Feed(tracer, cpu=3, cgroup=CG + 2)
    stray("W 9 /d")
    stray.seq += 2
    stray("W 9 /e")
    tick(tracer, 1_000_000, 3, 1)
    tick(tracer, 1_000_000, 3, 4)
    late_tick(tracer, 3_000_000)

    own = Lost(2, OWN_GAP, (3,), 1_000_000, 1_000_000, "tracer")
    assert losses(one) == [own]
    assert losses(two) == [Lost(4, "kernel record gap", (3,), 10, 60), own]
    assert (tracer.located, tracer.foreign_lost, tracer.foreign) == (8, 2, 2)
    await tracer.stop()


@pytest.mark.anyio
async def test_a_reused_pid_namespace_drops_the_cgroup_of_the_gone_container() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts([]), {})
    helper = Feed(tracer, cgroup=CG + 1)
    helper(f"E 10 1 1 0 99 1 3 {hx(b'sh\0')}")
    helper("W 10 /a")
    helper.seq += 2
    helper("W 10 /b")
    feed = Feed(tracer)
    feed(f"E 20 1 1 0 99 5 3 {hx(b'sh\0')}")
    late_tick(tracer, 3_000_000)
    assert dict(tracer._cgroups) == {CG: 99}
    assert losses(route) == []
    assert tracer.foreign_lost == 2
    await tracer.stop()


@pytest.mark.anyio
async def test_quiesce_waits_for_the_records_a_reading_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    tracer.alive = True
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    feed("X 10 0 0")
    tracer._counted(f"C 0.5 {count(CG, 0, 3)}")
    quiet = asyncio.create_task(tracer.quiesce(route, 1))
    await asyncio.sleep(0.2)
    assert not quiet.done()
    feed("W 10 /work/late")
    tracer._fed.set()
    async with asyncio.timeout(1):
        await quiet

    # Records that never come hold the close only so long.
    monkeypatch.setattr(tracer_module, "SETTLE_S", 0.1)
    tracer._counted(f"C 0.6 {count(CG, 0, 5)}")
    async with asyncio.timeout(1):
        await tracer.quiesce(route, 1)
    tracer.alive = False
    await tracer.stop()


@pytest.mark.anyio
async def test_unreadable_counts_are_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        tracer._counted("C 1.0")
        tracer._counted("C 2.0")
        tracer._counted("C x")
    assert [r.getMessage() for r in caplog.records] == [
        "the kernel's record counts are unreadable; lost last records go unseen",
        "unreadable tracer stream counts: C x",
    ]
    await tracer.stop()


def tick(tracer: Tracer, ts_us: int, cpu: int, seq: int) -> None:
    tracer._handle(json.dumps({"type": "printf", "data": f"Q {ts_us * 1000} {cpu} {seq}\n"}))


@pytest.mark.anyio
async def test_loss_waits_for_its_hole_to_ripen() -> None:
    """Records lost at the end of a burst leave a hole that the cpu's next tick shows. Every cpu
    ticks in phase, a little under a second apart, so the hole ripens two ticks later.
    """
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts([]), {})
    tick(tracer, 1_000_000, 0, 1)
    tick(tracer, 1_000_000, 1, 1)
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 3}}))
    tick(tracer, 1_100_000, 1, 5)
    tick(tracer, 2_099_000, 0, 2)
    tracer.settle()
    assert route.queue.empty()
    for n in range(3, 7):
        tick(tracer, 1_100_000 + (n - 1) * 999_000, 0, n)

    assert drain(route) == [Lost(3, OWN_GAP, (1,), 1_000_000, 1_100_000, "tracer")]
    assert (tracer.kernel_lost, tracer.located, tracer.unlocated) == (3, 3, 0)
    tracer._handle(json.dumps({"type": "lost_events", "data": {"events": 2}}))
    tick(tracer, 11_000_000, 0, 7)
    reason = "the kernel dropped 2 records no sequence hole located"
    assert drain(route) == [Lost(None, reason, scope="tracer")]
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


def test_each_cgroup_numbers_its_own_stream() -> None:
    seq = Sequence()
    assert seq.observe(Stamp(10, 0, 1, 5)) == []
    assert seq.observe(Stamp(20, 0, 1, 6)) == []
    assert seq.observe(Stamp(30, 0, 2, 5)) == []
    assert seq.observe(Stamp(40, 0, 4, 6)) == []
    assert seq.expire(1_000) == [Gap(0, 2, 2, 20, 40, 6)]
    assert not seq.pending(5)


def count(cgroup: int, cpu: int, seq: int) -> str:
    """One stream's count as the tracer prints it: the raw key and value bytes in hex."""
    key = cgroup.to_bytes(8, "little") + cpu.to_bytes(8, "little")
    return f"{key.hex()}:{seq.to_bytes(8, 'little').hex()}"


def test_parse_counts_reads_the_raw_keys() -> None:
    # As bpftool dumped them on linuxkit 7.0.14: the tracer's own stream and cgroup 805.
    line = (
        "C 439.52 00000000000000000000000000000000:0700000000000000 "
        "25030000000000000600000000000000:0100000000000000"
    )
    assert parse_counts(line) == (439_520_000, {(0, 0): 7, (805, 6): 1})
    assert parse_counts(f"C 1.5 {count(2**64 - 1, 3, 9)}") == (1_500_000, {(2**64 - 1, 3): 9})
    assert parse_counts("C 439.52") == (439_520_000, {})
    assert parse_counts("C 439.52 2503:01") is None
    assert parse_counts("C 439.52 zz") is None
    assert parse_counts("C x") is None


def test_a_reading_shows_the_last_records_once_their_cpu_has_passed_it() -> None:
    late = LATE_US
    seq = Sequence()
    assert seq.observe(Stamp(10, 0, 1, 5)) == []
    seq.counted(100, {(5, 0): 3, (0, 0): 0})
    assert seq.pending(5)
    assert seq.observe(Stamp(50, 0, 2, 5)) == []
    assert seq.pending(5)
    assert seq.observe(Stamp(100 + late, 0, 1)) == []
    assert seq.observe(Stamp(101 + late, 1, 1)) == []
    assert seq.observe(Stamp(101 + late, 0, 2)) == [Gap(0, 3, 1, 50, 100, 5)]
    assert not seq.pending(5)
    assert seq.observe(Stamp(60, 0, 3, 5)) == []

    # Counted records that arrive in time leave no hole.
    seq.counted(200 + late, {(5, 0): 4})
    assert seq.observe(Stamp(150 + late, 0, 4, 5)) == []
    assert not seq.pending(5)
    assert seq.observe(Stamp(201 + 2 * late, 0, 3)) == []


def test_a_busy_stream_cannot_keep_replacing_an_unmet_reading() -> None:
    late = LATE_US
    seq = Sequence()
    assert seq.observe(Stamp(10, 0, 1, 5)) == []
    seq.counted(100, {(5, 0): 3})
    seq.counted(200, {(5, 0): 5})
    assert seq.observe(Stamp(150, 0, 2, 5)) == []
    assert seq.observe(Stamp(100 + late + 1, 0, 1)) == [Gap(0, 3, 1, 150, 100, 5)]
    # A reading whose records all came gives way to the next one.
    seq.counted(300 + late, {(5, 0): 5})
    assert seq.observe(Stamp(280 + late, 0, 4, 5)) == []
    assert seq.observe(Stamp(290 + late, 0, 5, 5)) == []
    seq.counted(400 + late, {(5, 0): 7})
    assert seq.observe(Stamp(390 + late, 0, 6, 5)) == []
    gap = Gap(0, 7, 1, 390 + late, 400 + late, 5)
    assert seq.observe(Stamp(400 + 2 * late + 1, 0, 2)) == [gap]


def test_streams_the_kernel_stopped_counting_are_forgotten() -> None:
    seq = Sequence()
    assert seq.observe(Stamp(10, 0, 1, 5)) == []
    assert seq.observe(Stamp(10, 0, 1, 6)) == []
    seq.counted(10 + FORGET_US, {(6, 0): 1})
    assert (5, 0) in seq._next
    seq.counted(11 + FORGET_US, {(6, 0): 1})
    assert set(seq._next) == {(6, 0)}


@pytest.mark.anyio
async def test_route_reports_overflow() -> None:
    route = tracer_module.Route(1, Mounts([]), {}, asyncio.Queue(2))
    for pid in range(5):
        route.put(Fork(0, pid, pid, at=Stamp(100 + pid, 0, pid + 1)))
    assert route.overflow == 3
    route.queue.get_nowait()
    route.queue.get_nowait()
    late = Fork(0, 9, 9, at=Stamp(200, 0, 9))
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


# One reading of bpftrace's fdinfo on 7.0.14-linuxkit with a probe made to recurse: fds 12 and
# 13 hold the programs, 15 and 16 their links.
SKIP_READING = """\
T 1687.99
/proc/6331/fdinfo/12:prog_id:\t3183
/proc/6331/fdinfo/12:recursion_misses:\t1002
/proc/6331/fdinfo/13:prog_id:\t3185
/proc/6331/fdinfo/13:recursion_misses:\t0
/proc/6331/fdinfo/15:prog_id:\t3185
/proc/6331/fdinfo/16:prog_id:\t3183
"""
SKIP_REASON = "kernel skipped tracer probe {}: a run of it was in progress on that cpu"


def test_parse_skips_reads_program_fds_only() -> None:
    assert parse_skips(SKIP_READING.splitlines()) == (1_687_990_000, {3183: 1002, 3185: 0})
    assert parse_skips(["T x", "/proc/1/fdinfo/3:prog_id:\tnope", "junk"]) == (None, {})


def test_parse_probes_drops_the_probe_index() -> None:
    text = json.dumps(
        [
            {"id": 3136, "type": "tracing", "name": "fentry_vmlinux_security_file_open_9"},
            {"id": 3104, "type": "cgroup_device"},
            {"id": 3173, "type": "perf_event", "name": "interval_s_5_45"},
        ]
    )
    assert parse_probes(text) == {3136: "fentry_vmlinux_security_file_open", 3173: "interval_s_5"}
    assert parse_probes("") == {}
    assert parse_probes("{}") == {}


def test_parse_preemption() -> None:
    plain = {"preemptible_rcu": False, "mode": None, "preemptible": False}
    assert parse_preemption("H 0 ") == plain
    assert parse_preemption("H") == plain
    assert parse_preemption("H 0 none voluntary (full) lazy")["preemptible"] is False
    assert parse_preemption("H 1 ") == {"preemptible_rcu": True, "mode": None, "preemptible": True}
    lazy = parse_preemption("H 1 none voluntary full (lazy)")
    assert lazy == {"preemptible_rcu": True, "mode": "lazy", "preemptible": True}
    assert parse_preemption("H 1 none (voluntary) full lazy")["preemptible"] is False


@pytest.mark.anyio
async def test_skipped_probe_runs_are_lost_in_every_sandbox() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    one = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    two = tracer.register(77, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    tracer.clock = (1_000_000, 0)
    tracer._probes = {3183: "fentry_vmlinux_strncpy_from_kernel_nofault"}
    tracer._skipped(*parse_skips(SKIP_READING.splitlines()))
    reason = SKIP_REASON.format("fentry_vmlinux_strncpy_from_kernel_nofault")
    first = [Lost(1002, reason, (), 1_000_000, 1_687_990_000, "tracer")]
    assert drain(one) == first
    assert drain(two) == first

    # A reading that arrives after a newer one reports nothing; the next, the runs since.
    tracer._skipped(1_600_000_000, {3183: 900})
    tracer._skipped(1_688_990_000, {3183: 1010, 3185: 2})
    window = (1_687_990_000, 1_688_990_000)
    assert drain(one) == [
        Lost(8, reason, (), *window, "tracer"),
        Lost(2, SKIP_REASON.format("program 3185"), (), *window, "tracer"),
    ]
    assert tracer.skipped == 1012
    assert tracer._skips == {3183: 1010, 3185: 2}
    await tracer.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("container", [StuckContainer(), StuckContainer(OSError("gone"))])
async def test_a_failed_skip_reading_leaves_the_close_alone(
    monkeypatch: pytest.MonkeyPatch, container: StuckContainer
) -> None:
    monkeypatch.setattr(tracer_module, "SKIPS_S", 0)
    monkeypatch.setattr(execs_module, "GRACE_S", 0.05)
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    tracer.alive = True
    tracer._bpftrace = 5
    tracer._container = cast(Any, container)
    async with asyncio.timeout(2):
        await tracer.quiesce(route, 1)
    assert route.queue.empty()
    tracer.alive = False
    tracer._container = None
    await tracer.stop()


ADDRSPACE = [
    "/tmp/trace.bt:519:100-102: WARNING: Addrspace mismatch",
    "tracepoint:syscalls:sys_enter_fadvise64_64 /@sandbox[cgroup] && (int64)args.advice == 4/ {",
    "                                                                ~~",
    "/tmp/trace.bt:521:46-47: WARNING: Addrspace mismatch",
    "  if ((int64)args.fd >= 0 && (uint64)args.fd < (uint64)$fdt->max_fds) {",
    "                                              ~",
]


@pytest.mark.anyio
async def test_known_compile_warnings_stay_out_of_the_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    with caplog.at_level(logging.DEBUG, logger="aims.sandbox"):
        for text in [*ADDRSPACE, "/tmp/trace.bt:9:1-2: WARNING: something new", "x", "~", "else"]:
            tracer._handle(text)
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    quiet = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert loud == [
        "tracer: /tmp/trace.bt:9:1-2: WARNING: something new",
        "tracer: x",
        "tracer: ~",
        "tracer: else",
    ]
    assert quiet == [f"tracer: {t}" for t in ADDRSPACE]


@pytest.mark.anyio
async def test_a_known_warning_does_not_hide_an_error_after_it() -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    tracer._handle(ADDRSPACE[0])
    tracer._handle("/tmp/trace.bt:7:1-2: ERROR: unknown function")
    with pytest.raises(tracer_module.TracerError, match="unknown function"):
        await tracer._ready


class SilentTrace:
    """A trace exec whose bpftrace never prints its header."""

    def start(self, detach: bool = False) -> object:
        class Opening:
            async def __aenter__(self) -> object:
                return SilentTrace()

        return Opening()

    async def read_out(self) -> object:
        await asyncio.Event().wait()
        raise AssertionError

    async def close(self) -> None:
        pass


class TracerContainer:
    async def start(self) -> None:
        pass

    async def exec(self, *args: object, **kwargs: object) -> SilentTrace:
        return SilentTrace()

    async def delete(self, force: bool = False) -> None:
        pass


class TracerContainers:
    async def create(self, **kwargs: object) -> TracerContainer:
        return TracerContainer()


class TracerDocker:
    containers = TracerContainers()


@pytest.mark.anyio
@pytest.mark.parametrize("cut", ["cancel", "timeout"])
async def test_a_start_cut_short_leaves_no_unread_error(
    monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    async def image(*args: object) -> str:
        return "tracer"

    monkeypatch.setattr(tracer_module, "ensure_image", image)
    monkeypatch.setattr(tracer_module, "READY_S", 0.05 if cut == "timeout" else 60)
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    errors: list[str] = []
    loop.set_exception_handler(lambda _, context: errors.append(context["message"]))
    try:
        tracer = Tracer(cast(aiodocker.Docker, TracerDocker()), {}, "t")
        start = asyncio.create_task(tracer.start())
        await asyncio.sleep(0.01)
        if cut == "cancel":
            start.cancel()
        with pytest.raises(asyncio.CancelledError if cut == "cancel" else TimeoutError):
            await start
        assert tracer._container is None
        del tracer, start
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(handler)
    assert errors == []


@pytest.mark.anyio
async def test_a_line_the_handler_fails_on_costs_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    route = tracer.register(99, Mounts.parse(MOUNTINFO), SYSCALLS["aarch64"])
    feed = Feed(tracer)
    feed("R 0 999")
    feed(f"E 10 1 1 1000 99 5 3 {hx(b'sh\0')}")
    drain(route)
    real = tracer_module.parse

    def parse_or_fail(text: str) -> object:
        if "/work/bad" in text:
            raise RuntimeError("boom")
        return real(text)

    monkeypatch.setattr(tracer_module, "parse", parse_or_fail)
    tracer._feed(line("W 10 /work/bad", Stamp(30_000, 0, 3, CG)).encode())
    tracer._feed(line("W 10 /work/good", Stamp(40_000, 0, 4, CG)).encode())

    assert drain(route) == [
        Lost(1, UNHANDLED, scope="tracer"),
        File(10, "write", "/work/good", at=Stamp(40, 0, 4, CG)),
    ]
    assert tracer.unhandled == 1
    await tracer.stop()
