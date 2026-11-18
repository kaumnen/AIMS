"""Parse the kernel tracer's output.

Every line has a fixed field count. Strings that can hold any byte are hex, except a single
trailing path or walk. Bytes that are not UTF-8 are kept as `\\xNN` escapes. A line that does
not fit its shape is returned as `Garbled`, never guessed at. Each tag is one `@record` parser
below and one probe block in `trace.bt`.
"""

import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from aims.events import MAX_INT, FdKind, LookOp
from aims.sandbox.base import (
    Cache,
    Exit,
    Fd,
    File,
    Fork,
    Ipc,
    Kernel,
    Listen,
    Look,
    LookSkip,
    Mem,
    Net,
    Ns,
    Oom,
    Output,
    Ptrace,
    Signal,
    Stamp,
)

PIPEFS_MAGIC = 0x50495045
_FD_KINDS: dict[int, FdKind] = {
    0o010000: "fifo",
    0o100000: "file",
    0o020000: "char",
    0o140000: "sock",
    0o040000: "dir",
}


def _fd(ino: int, mode: int, magic: int) -> Fd:
    kind = _FD_KINDS.get(mode & 0o170000, "other")
    if kind == "fifo" and magic == PIPEFS_MAGIC:
        kind = "pipe"
    return Fd(ino, kind)


DEPTH = 12
STRLEN = 384
ENV_CAP = 4096
# The tracer reads this much of a command line.
ARGV_CAP = 16384

PTRACE_REQUESTS: dict[int, str] = {
    -3: "pidfd_getfd",
    -2: "process_vm_readv",
    -1: "process_vm_writev",
    0: "traceme",
    1: "peektext",
    2: "peekdata",
    3: "peekuser",
    4: "poketext",
    5: "pokedata",
    6: "pokeuser",
    7: "cont",
    8: "kill",
    9: "singlestep",
    12: "getregs",
    13: "setregs",
    14: "getfpregs",
    15: "setfpregs",
    16: "attach",
    17: "detach",
    18: "getfpxregs",
    19: "setfpxregs",
    24: "syscall",
    0x4200: "setoptions",
    0x4201: "geteventmsg",
    0x4202: "getsiginfo",
    0x4203: "setsiginfo",
    0x4204: "getregset",
    0x4205: "setregset",
    0x4206: "seize",
    0x4207: "interrupt",
    0x4208: "listen",
    0x4209: "peeksiginfo",
    0x420A: "getsigmask",
    0x420B: "setsigmask",
    0x420C: "peekmtetags",
    0x420D: "pokemtetags",
    0x420E: "get_syscall_info",
}

UNIX_KINDS: dict[str, Literal["bind", "listen", "connect", "sendto"]] = {
    "b": "bind",
    "l": "listen",
    "c": "connect",
    "s": "sendto",
}

NS_FLAGS: tuple[tuple[int, str], ...] = (
    (0x00000080, "time"),
    (0x00020000, "mount"),
    (0x02000000, "cgroup"),
    (0x04000000, "uts"),
    (0x08000000, "ipc"),
    (0x10000000, "user"),
    (0x20000000, "pid"),
    (0x40000000, "net"),
)


def namespaces(flags: int) -> tuple[str, ...]:
    return tuple(name for bit, name in NS_FLAGS if flags & bit)


_HEX = re.compile(r"^(?:\\x[0-9a-f]{2})*$")
# bpftrace's JSON escape assumes an unsigned char, so on x86_64 a byte from 0x80 up arrives as
# `\uffff` followed by `ff` and the byte in hex. It is undone on the JSON text, where a real
# U+FFFF cannot pass for it, and an escaped backslash before it is left alone.
_SIGNED = re.compile(r"(?<!\\)((?:\\\\)*)\\uffffff([89a-f][0-9a-f])")

SYSCALLS: dict[str, dict[int, str]] = {
    "aarch64": {
        97: "unshare",
        268: "setns",
        40: "mount",
        39: "umount2",
        41: "pivot_root",
        51: "chroot",
        117: "ptrace",
        280: "bpf",
        241: "perf_event_open",
        105: "init_module",
        273: "finit_module",
        106: "delete_module",
        104: "kexec_load",
        294: "kexec_file_load",
        219: "keyctl",
        217: "add_key",
        218: "request_key",
        146: "setuid",
        144: "setgid",
        145: "setreuid",
        143: "setregid",
        147: "setresuid",
        149: "setresgid",
        151: "setfsuid",
        152: "setfsgid",
        159: "setgroups",
        91: "capset",
        220: "clone",
        435: "clone3",
        33: "mknodat",
        142: "reboot",
        224: "swapon",
        225: "swapoff",
        161: "sethostname",
        162: "setdomainname",
        170: "settimeofday",
        112: "clock_settime",
        171: "adjtimex",
        89: "acct",
        60: "quotactl",
        116: "syslog",
        270: "process_vm_readv",
        271: "process_vm_writev",
        272: "kcmp",
        438: "pidfd_getfd",
        440: "process_madvise",
        265: "open_by_handle_at",
        264: "name_to_handle_at",
        282: "userfaultfd",
        425: "io_uring_setup",
        426: "io_uring_enter",
        427: "io_uring_register",
        430: "fsopen",
        432: "fsmount",
        431: "fsconfig",
        429: "move_mount",
        428: "open_tree",
        442: "mount_setattr",
        92: "personality",
    },
    "x86_64": {
        272: "unshare",
        308: "setns",
        165: "mount",
        166: "umount2",
        155: "pivot_root",
        161: "chroot",
        101: "ptrace",
        321: "bpf",
        298: "perf_event_open",
        175: "init_module",
        313: "finit_module",
        176: "delete_module",
        246: "kexec_load",
        320: "kexec_file_load",
        250: "keyctl",
        248: "add_key",
        249: "request_key",
        105: "setuid",
        106: "setgid",
        113: "setreuid",
        114: "setregid",
        117: "setresuid",
        119: "setresgid",
        122: "setfsuid",
        123: "setfsgid",
        116: "setgroups",
        126: "capset",
        56: "clone",
        435: "clone3",
        259: "mknodat",
        133: "mknod",
        172: "iopl",
        173: "ioperm",
        169: "reboot",
        167: "swapon",
        168: "swapoff",
        170: "sethostname",
        171: "setdomainname",
        164: "settimeofday",
        227: "clock_settime",
        159: "adjtimex",
        163: "acct",
        179: "quotactl",
        103: "syslog",
        310: "process_vm_readv",
        311: "process_vm_writev",
        312: "kcmp",
        438: "pidfd_getfd",
        440: "process_madvise",
        304: "open_by_handle_at",
        303: "name_to_handle_at",
        323: "userfaultfd",
        425: "io_uring_setup",
        426: "io_uring_enter",
        427: "io_uring_register",
        430: "fsopen",
        432: "fsmount",
        431: "fsconfig",
        429: "move_mount",
        428: "open_tree",
        442: "mount_setattr",
        135: "personality",
    },
}
# On x86_64 a 32-bit call reaches the kernel through int 0x80 with ia32's numbers, which the
# tracer offsets by IA32, or through syscall with the kernel's X32 bit set. ia32 has 16-bit and
# 32-bit forms of the uid and gid calls, and x32 its own numbers for a few calls.
IA32 = 0x10000
X32 = 0x40000000
I386 = {
    310: "unshare",
    346: "setns",
    21: "mount",
    52: "umount2",
    217: "pivot_root",
    61: "chroot",
    26: "ptrace",
    357: "bpf",
    336: "perf_event_open",
    128: "init_module",
    350: "finit_module",
    129: "delete_module",
    283: "kexec_load",
    288: "keyctl",
    286: "add_key",
    287: "request_key",
    23: "setuid",
    213: "setuid",
    46: "setgid",
    214: "setgid",
    70: "setreuid",
    203: "setreuid",
    71: "setregid",
    204: "setregid",
    164: "setresuid",
    208: "setresuid",
    170: "setresgid",
    210: "setresgid",
    138: "setfsuid",
    215: "setfsuid",
    139: "setfsgid",
    216: "setfsgid",
    81: "setgroups",
    206: "setgroups",
    185: "capset",
    120: "clone",
    435: "clone3",
    297: "mknodat",
    14: "mknod",
    110: "iopl",
    101: "ioperm",
    88: "reboot",
    87: "swapon",
    115: "swapoff",
    74: "sethostname",
    121: "setdomainname",
    79: "settimeofday",
    264: "clock_settime",
    404: "clock_settime",
    124: "adjtimex",
    51: "acct",
    131: "quotactl",
    103: "syslog",
    347: "process_vm_readv",
    348: "process_vm_writev",
    349: "kcmp",
    438: "pidfd_getfd",
    440: "process_madvise",
    342: "open_by_handle_at",
    341: "name_to_handle_at",
    374: "userfaultfd",
    425: "io_uring_setup",
    426: "io_uring_enter",
    427: "io_uring_register",
    430: "fsopen",
    432: "fsmount",
    431: "fsconfig",
    429: "move_mount",
    428: "open_tree",
    442: "mount_setattr",
    136: "personality",
}
X32_OWN = {521: "ptrace", 528: "kexec_load", 539: "process_vm_readv", 540: "process_vm_writev"}
SYSCALLS["x86_64"] |= {IA32 | nr: name for nr, name in I386.items()} | {
    X32 | nr: name for nr, name in (SYSCALLS["x86_64"] | X32_OWN).items()
}
ARCHES = {"arm64": "aarch64", "amd64": "x86_64"}


@dataclass(frozen=True, slots=True)
class Ready:
    base: int
    boot_us: int
    tai_us: int
    at: Stamp


@dataclass(frozen=True, slots=True)
class Tick:
    at: Stamp


@dataclass(frozen=True, slots=True)
class Dropped:
    count: int


@dataclass(frozen=True, slots=True)
class Raw:
    """A kernel record the tracer must resolve before it can route it."""

    pid: int
    at: Stamp = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class ExecPart(Raw):
    """Fields of the next exec of `pid`, sent by the kernel before its `E` line."""

    def fields(self) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Bprm(ExecPart):
    path: str

    def fields(self) -> dict[str, object]:
        return {"path": self.path, "path_partial": _cut(self.path)}


@dataclass(frozen=True, slots=True)
class Identity(ExecPart):
    """The executed file's inode, device, link count and filesystem magic."""

    ppid: int
    inode: int
    dev: int
    nlink: int
    magic: int
    size: int
    mtime_us: int
    ctime_us: int

    def fields(self) -> dict[str, object]:
        return {
            "inode": self.inode,
            "dev": self.dev,
            "nlink": self.nlink,
            "magic": self.magic,
            "size": self.size,
            "mtime_us": self.mtime_us,
            "ctime_us": self.ctime_us,
        }


@dataclass(frozen=True, slots=True)
class Context(ExecPart):
    """The cwd walk and fd0/1/2 of the next exec. The cwd is resolved once the route is known."""

    fds: tuple[Fd, Fd, Fd]
    cwd: Walk

    def fields(self) -> dict[str, object]:
        return {"fds": self.fds, "cwd": self.cwd}


@dataclass(frozen=True, slots=True)
class EnvChunk(Raw):
    """A slice of the next exec's environment at `offset`; `env_bytes` is the full size."""

    env_bytes: int
    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ArgvChunk(Raw):
    """A slice of the next exec's command line at `offset`, past the part its `E` line holds."""

    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RawExec(Raw):
    """An exec with the start of its command line in `data`; `argv_bytes` is the full size."""

    ppid: int
    root: bool
    uid: int
    pidns: int
    ns_pid: int
    argv_bytes: int
    data: bytes


@dataclass(frozen=True, slots=True)
class Walk:
    """A path as the kernel holds it: names from the file up toward its filesystem root."""

    dev: int
    names: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class Delete(Raw):
    walk: Walk


@dataclass(frozen=True, slots=True)
class Rename(Raw):
    old: Walk
    new: Walk


@dataclass(frozen=True, slots=True)
class Mkdir(Raw):
    walk: Walk


@dataclass(frozen=True, slots=True)
class Symlink(Raw):
    walk: Walk
    target: str
    cut: bool = False


@dataclass(frozen=True, slots=True)
class Link(Raw):
    old: Walk
    new: Walk


@dataclass(frozen=True, slots=True)
class RawClose(Raw):
    size: int
    walk: Walk


@dataclass(frozen=True, slots=True)
class RawDenied(Raw):
    nr: int
    errno: int


@dataclass(frozen=True, slots=True)
class RawFifo(Raw):
    walk: Walk


@dataclass(frozen=True, slots=True)
class RawMmap(Raw):
    walk: Walk
    inode: int
    nlink: int
    prot: int


@dataclass(frozen=True, slots=True)
class RawSetattr(Raw):
    """A requested metadata change. `errno` is set when the kernel refused it before its hook;
    a time it would have taken from the clock is then None."""

    walk: Walk
    valid: int
    mode: int
    uid: int
    gid: int
    size: int
    atime_us: int | None
    mtime_us: int | None
    ctime_us: int | None
    errno: int | None = None


@dataclass(frozen=True, slots=True)
class Named:
    """A name as a process passed it, with the walk of the directory a relative name was looked
    up from. `base` is None for an absolute name or a directory the tracer could not read."""

    name: str
    base: Walk | None
    cut: bool = False


@dataclass(frozen=True, slots=True)
class Refused(Raw):
    """A call the kernel refused with EPERM, EACCES or EROFS, as `op` names it for file.access.
    A symlink's `target` is its content, which is never resolved."""

    op: Literal["denied", "delete", "mkdir", "rename", "link", "symlink"]
    errno: int
    path: Named
    target: Named | None = None


@dataclass(frozen=True, slots=True)
class RawLook(Raw):
    """A name looked up without opening it: the walk of what it reached, or the name as given
    with the lookup's errno when it failed. A lookup `again` beneath the directory of thread
    `tid`'s last one of its kind comes without that directory's walk: a found one as its own name
    only, a failed relative one with no base. `dir` names that directory."""

    op: LookOp
    found: Walk | None
    missed: Named | None = None
    errno: int | None = None
    tid: int | None = None
    again: bool = False
    # The directory's address and the kernel's count of directory renames when it was printed.
    dir: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class RefusedNode(Raw):
    """A mknod the kernel refused with EPERM, EACCES or EROFS, with the mode it asked for."""

    errno: int
    mode: int
    path: Named


@dataclass(frozen=True, slots=True)
class RefusedChown(Raw):
    """A chown the kernel refused by name. -1 leaves an owner or group as it is."""

    errno: int
    uid: int
    gid: int
    path: Named


@dataclass(frozen=True, slots=True)
class RawXattr(Raw):
    """An extended attribute change at its security check. `cut` marks a name at the cap."""

    op: Literal["set", "remove"]
    size: int
    flags: int
    name: str
    cut: bool
    walk: Walk


@dataclass(frozen=True, slots=True)
class RefusedXattr(Raw):
    """An extended attribute call the kernel refused before its security check."""

    op: Literal["set", "remove"]
    errno: int
    size: int
    flags: int
    name: str
    cut: bool
    path: Named


@dataclass(frozen=True, slots=True)
class RawCache(Raw):
    """A page-cache call with the walk of the fd's file, or None for an invalid fd."""

    op: Literal["fadvise_dontneed", "preadv2_nowait", "cachestat", "mincore"]
    walk: Walk | None


@dataclass(frozen=True, slots=True)
class RawSync(Raw):
    """A durability call with the walk of the fd's file, or None for an invalid fd."""

    call: Literal["fsync", "fdatasync", "sync_file_range", "syncfs"]
    walk: Walk | None


@dataclass(frozen=True, slots=True)
class Deeper(Raw):
    """The next names of a walk the record after it cut at `DEPTH` names. `slot` is the walk's
    place among that record's walks. A record's continuations come right before it in its
    cgroup's stream on its cpu, so each one's sequence number is one less than the next one's."""

    slot: int
    walk: Walk


@dataclass(frozen=True, slots=True)
class Garbled:
    pid: int | None
    line: str
    at: Stamp | None = None


@dataclass(frozen=True, slots=True)
class Note:
    text: str


Item = Ready | Tick | Raw | Kernel
# The tracer's own records whose header has no cgroup: the heartbeat's printf must fit 32 bytes.
UNGROUPED = frozenset({"R", "Q"})
Parser = Callable[[str, Stamp], Item]

PARSERS: dict[str, Parser] = {}


def record(tag: str) -> Callable[[Parser], Parser]:
    """Register the parser of lines tagged `tag`. It gets the fields after the header."""

    def register(parser: Parser) -> Parser:
        if tag in PARSERS:
            raise ValueError(f"tracer tag {tag!r} is already registered")
        PARSERS[tag] = parser
        return parser

    return register


def parse(line: str) -> Item | Dropped | Garbled | Note | None:
    """Turn one line of `bpftrace -f json` output into an item.

    A record line is `TAG TS CPU SEQ CGROUP FIELDS...`: the kernel boot clock in ns, the cpu,
    the record's number in its cgroup's stream on that cpu, and the cgroup, then the fields of
    that tag, if it has any. The tracer's own R and Q leave out the cgroup. Times are kept in
    µs, which canonical JSON can hold.
    """
    try:
        doc = json.loads(_SIGNED.sub(r"\1\\udc\2", line))
    except ValueError:
        return Note(line) if line.strip() else None
    if not isinstance(doc, dict):
        return Note(line)
    kind, data = doc.get("type"), doc.get("data")
    if kind == "lost_events" and isinstance(data, dict):
        return Dropped(int(data.get("events", 0)))
    if kind != "printf" or not isinstance(data, str):
        return None
    text = (
        data.removesuffix("\n").encode("utf-8", "surrogateescape").decode(errors="backslashreplace")
    )
    tag, _, rest = text.partition(" ")
    width = 3 if tag in UNGROUPED else 4
    head = rest.split(" ", width)
    try:
        if len(head) < width:
            raise ValueError("short header")
        ts, cpu, seq = (int(x) for x in head[:3])
        at = Stamp(ts // 1000, cpu, seq, int(head[3]) if width == 4 else 0)
    except ValueError:
        return Garbled(None, text)
    body = head[width] if len(head) > width else ""
    parser = PARSERS.get(tag)
    if parser is None:
        return Garbled(_pid(body), text, at)
    try:
        return parser(body, at)
    except ValueError, IndexError:
        return Garbled(_pid(body), text, at)


@record("R")
def _ready(body: str, at: Stamp) -> Ready:
    base, tai = _ints(body, 2)
    return Ready(base, at.ts, tai // 1000, at)


@record("Q")
def _tick(body: str, at: Stamp) -> Tick:
    if body:
        raise ValueError("a tick has no fields")
    return Tick(at)


@record("F")
def _fork(body: str, at: Stamp) -> Fork:
    parent, child, ns_pid = _ints(body, 3)
    return Fork(parent, child, ns_pid, at=at)


@record("B")
def _bprm(body: str, at: Stamp) -> Bprm:
    pid, path = body.split(" ", 1)
    return Bprm(int(pid), path, at=at)


@record("E")
def _exec(body: str, at: Stamp) -> RawExec:
    f = _fields(body, 8)
    pid, ppid, root, uid, pidns, ns_pid, size = (int(x) for x in f[:7])
    return RawExec(pid, ppid, root == 1, uid, pidns, ns_pid, size, _hex(f[7]), at=at)


@record("i")
def _argv(body: str, at: Stamp) -> ArgvChunk:
    pid, offset, data = body.split(" ", 2)
    return ArgvChunk(int(pid), int(offset), _hex(data), at=at)


def split_argv(data: bytes) -> tuple[str, ...]:
    """The arguments of a command line as the kernel holds it: each one ends in a NUL."""
    argv = data.split(b"\0")
    if argv and argv[-1] == b"":
        argv.pop()
    return tuple(_text(a) for a in argv)


@record("V")
def _context(body: str, at: Stamp) -> Context:
    f = body.split(" ", 12)
    n = [int(x) for x in f[:12]]
    fds = (_fd(n[1], n[2], n[3]), _fd(n[4], n[5], n[6]), _fd(n[7], n[8], n[9]))
    cwd = _walk(n[10], n[11], f[12].split("/"))
    return Context(n[0], fds, cwd, at=at)


@record("G")
def _env(body: str, at: Stamp) -> EnvChunk:
    pid, total, offset, data = body.split(" ", 3)
    return EnvChunk(int(pid), int(total), int(offset), _hex(data), at=at)


@record("T")
def _output(body: str, at: Stamp) -> Output:
    pid, ino, magic, nbytes, prefix = body.split(" ", 4)
    data = _hex(prefix)
    return Output(int(pid), int(ino), int(nbytes), int(magic), data or None, at=at)


@record("X")
def _exit(body: str, at: Stamp) -> Exit:
    pid, code, sig = _ints(body, 3)
    return Exit(pid, None if sig else code, sig or None, at=at)


@record("O")
def _oom(body: str, at: Stamp) -> Oom:
    (pid,) = _ints(body, 1)
    return Oom(pid, at=at)


@record("W")
def _write(body: str, at: Stamp) -> File:
    pid, path = body.split(" ", 1)
    return File(int(pid), "write", path, partial=_cut(path), at=at)


@record("A")
def _read(body: str, at: Stamp) -> File:
    pid, path = body.split(" ", 1)
    return File(int(pid), "read", path, partial=_cut(path), at=at)


LOOK_OPS: dict[int, LookOp] = {1: "stat", 2: "access", 3: "readlink", 4: "chdir", 5: "list"}
# The depth of a lookup whose directory is the one the same thread's last lookup of its kind
# had: the tracer prints no walk for it, so the reader joins it to the one it kept.
AGAIN = -3


def _look_op(value: str) -> LookOp:
    op = LOOK_OPS.get(int(value))
    if op is None:
        raise ValueError("unknown lookup")
    return op


@record("j")
def _looked(body: str, at: Stamp) -> Look:
    """A found name with the path the kernel gives the file it reached."""
    f = body.split(" ", 2)
    if len(f) != 3:
        raise ValueError("short lookup line")
    return Look(int(f[0]), _look_op(f[1]), f[2], partial=_cut(f[2]), at=at)


# Lookups a process may log in a second; the tracer counts the rest.
LOOK_BUDGET = 2000


@record("l")
def _skipped(body: str, at: Stamp) -> LookSkip:
    pid, count = _ints(body, 2)
    return LookSkip(pid, _clamp(count), at=at)


@record("k")
def _found(body: str, at: Stamp) -> RawLook:
    f = body.split(" ", 7)
    if len(f) != 8:
        raise ValueError("short lookup line")
    pid, tid, dev, depth, ptr, gen = (int(x) for x in f[:2] + f[3:7])
    if depth == AGAIN:
        if not f[7] or "/" in f[7]:
            raise ValueError("a name beneath the last directory must be one name")
        walk = Walk(dev, (f[7].encode(),))
    else:
        walk = _walk(dev, depth, f[7].split("/"))
    op = _look_op(f[2])
    return RawLook(pid, op, walk, tid=tid, again=depth == AGAIN, dir=(ptr, gen), at=at)


@record("m")
def _missed(body: str, at: Stamp) -> RawLook:
    f = body.split(" ", 9)
    if len(f) != 10:
        raise ValueError("expected ten failed lookup fields")
    pid, tid, errno, dev, depth, ptr, gen = (int(x) for x in f[:2] + f[3:8])
    again = depth == AGAIN
    name = Named(f[9], None if again else _hex_walk(dev, depth, f[8]), _cut(f[9]))
    return RawLook(pid, _look_op(f[2]), None, name, errno, tid, again, (ptr, gen), at=at)


def _walked(body: str) -> tuple[int, Walk | None]:
    """`PID DEV DEPTH NAMES`, or `PID DEV -1` when the fd named no file."""
    f = body.split(" ", 3)
    pid = int(f[0])
    if len(f) >= 3 and int(f[2]) < 0:
        return pid, None
    if len(f) != 4:
        raise ValueError("short cache line")
    return pid, _walk(int(f[1]), int(f[2]), f[3].split("/"))


@record("a")
def _fadvise(body: str, at: Stamp) -> RawCache:
    pid, walk = _walked(body)
    return RawCache(pid, "fadvise_dontneed", walk, at=at)


@record("b")
def _preadv2(body: str, at: Stamp) -> RawCache:
    pid, walk = _walked(body)
    return RawCache(pid, "preadv2_nowait", walk, at=at)


@record("c")
def _cachestat(body: str, at: Stamp) -> RawCache:
    pid, walk = _walked(body)
    return RawCache(pid, "cachestat", walk, at=at)


@record("d")
def _mincore(body: str, at: Stamp) -> Cache:
    (pid,) = _ints(body, 1)
    return Cache(pid, "mincore", None, at=at)


@record("e")
def _fsync(body: str, at: Stamp) -> RawSync:
    pid, walk = _walked(body)
    return RawSync(pid, "fsync", walk, at=at)


@record("f")
def _fdatasync(body: str, at: Stamp) -> RawSync:
    pid, walk = _walked(body)
    return RawSync(pid, "fdatasync", walk, at=at)


@record("g")
def _sync_file_range(body: str, at: Stamp) -> RawSync:
    pid, walk = _walked(body)
    return RawSync(pid, "sync_file_range", walk, at=at)


@record("h")
def _syncfs(body: str, at: Stamp) -> RawSync:
    pid, walk = _walked(body)
    return RawSync(pid, "syncfs", walk, at=at)


@record("U")
def _delete(body: str, at: Stamp) -> Delete:
    pid, dev, depth, names = body.split(" ", 3)
    return Delete(int(pid), _walk(int(dev), int(depth), names.split("/")), at=at)


@record("N")
def _rename(body: str, at: Stamp) -> Rename:
    pid, old_dev, new_dev, old_depth, new_depth, names = body.split(" ", 5)
    parts = names.split("/")
    cut = 2 * DEPTH - int(old_depth)
    old = _walk(int(old_dev), int(old_depth), parts[:cut])
    return Rename(int(pid), old, _walk(int(new_dev), int(new_depth), parts[cut:]), at=at)


@record("Y")
def _create(body: str, at: Stamp) -> Mkdir | Symlink | Link:
    head = body.split(" ", 2)
    if len(head) < 3:
        raise ValueError("short create line")
    pid, kind = int(head[0]), head[1]
    if kind == "d":
        dev, depth, names = head[2].split(" ", 2)
        return Mkdir(pid, _walk(int(dev), int(depth), names.split("/")), at=at)
    if kind == "s":
        dev, depth, size, target, names = head[2].split(" ", 4)
        raw, length = _hex(target), int(size)
        if len(raw) > length:
            raise ValueError("symlink target is longer than its length")
        walk = _walk(int(dev), int(depth), names.split("/"))
        cut = length > len(raw) or length >= STRLEN
        return Symlink(pid, walk, _text(raw), cut, at=at)
    if kind == "l":
        old_dev, new_dev, old_depth, new_depth, names = head[2].split(" ", 4)
        parts = names.split("/")
        cut = 2 * DEPTH - int(old_depth)
        old = _walk(int(old_dev), int(old_depth), parts[:cut])
        return Link(pid, old, _walk(int(new_dev), int(new_depth), parts[cut:]), at=at)
    raise ValueError("unknown create kind")


@record("w")
def _deeper(body: str, at: Stamp) -> Deeper:
    f = body.split(" ", 3)
    if len(f) != 4:
        raise ValueError("expected four walk continuation fields")
    pid, slot, depth = int(f[0]), int(f[1]), int(f[2])
    if slot not in (0, 1):
        raise ValueError("unknown walk slot")
    return Deeper(pid, slot, _walk(0, depth, f[3].split("/")), at=at)


# The walks of each record the tracer continues past `DEPTH` names, in the order it prints them.
DEEP_WALKS: dict[type, tuple[str, ...]] = {}


def deepen(item: Raw, more: Mapping[int, list[Walk]]) -> Raw:
    """`item` with each walk the tracer cut at `DEPTH` names joined with the names after it."""
    fields = DEEP_WALKS.get(type(item), ())
    changes: dict[str, Walk] = {}
    for slot, walks in more.items():
        if slot >= len(fields):
            continue
        walk = getattr(item, fields[slot])
        if not isinstance(walk, Walk) or len(walk.names) != DEPTH or b"/" in walk.names:
            continue
        names = list(walk.names)
        for w in walks:
            names.extend(w.names)
        changes[fields[slot]] = Walk(walk.dev, tuple(names))
    return dataclasses.replace(item, **changes) if changes else item


@record("C")
def _net(body: str, at: Stamp) -> Net:
    f = _fields(body, 5)
    if f[2] not in ("4", "6"):
        raise ValueError("unknown address family")
    kind: Literal["connect", "sendto"] = "connect" if f[1] == "c" else "sendto"
    family: Literal["inet", "inet6"] = "inet" if f[2] == "4" else "inet6"
    return Net(int(f[0]), kind, family, f[3], int(f[4]), at=at)


@record("I")
def _ipc(body: str, at: Stamp) -> Ipc | RawFifo:
    head = body.split(" ", 3)
    if len(head) < 2:
        raise ValueError("short ipc line")
    pid, kind = int(head[0]), head[1]
    if kind in UNIX_KINDS:
        f = _fields(body, 4)
        if f[2] != "1":
            raise ValueError("unknown socket family")
        raw = _hex(f[3])
        if not raw:
            raise ValueError("empty socket path")
        if raw.startswith(b"\0"):
            path = "@" + _text(raw[1:].rstrip(b"\0"))
            partial = False
        else:
            # The kernel reads a path name up to its first NUL; what follows is caller memory.
            path = _text(raw.split(b"\0", 1)[0])
            partial = not path.startswith("/")
        return Ipc(
            pid,
            UNIX_KINDS[kind],
            "unix",
            path,
            partial=partial,
            at=at,
        )
    if kind == "f":
        f = _fields(body, 6)
        if f[2] != "0":
            raise ValueError("unknown fifo family")
        return RawFifo(pid, _walk(int(f[3]), int(f[4]), f[5].split("/")), at=at)
    if kind == "g":
        f = _fields(body, 6)
        if f[2] != "2":
            raise ValueError("unknown shm family")
        return Ipc(
            pid,
            "shmget",
            "sysv",
            key=_clamp(int(f[3])),
            size=min(int(f[4]), MAX_INT),
            flags=_clamp(int(f[5])),
            at=at,
        )
    if kind == "a":
        f = _fields(body, 5)
        if f[2] != "2":
            raise ValueError("unknown shm family")
        return Ipc(pid, "shmat", "sysv", id=_clamp(int(f[3])), flags=_clamp(int(f[4])), at=at)
    raise ValueError("unknown ipc kind")


@record("K")
def _ptrace(body: str, at: Stamp) -> Ptrace:
    pid, target, request, ret, named = _ints(body, 5)
    name = PTRACE_REQUESTS.get(request, f"request_{request:#x}")
    refused = ret < 0 and request not in (1, 2, 3)
    return Ptrace(
        pid,
        _clamp(target) if target > 0 else None,
        name,
        refused,
        _clamp(-ret) if refused else None,
        ns_target=None if request == 0 or (request == -3 and named == 0) else _clamp(named),
        at=at,
    )


PROC_LINKS: dict[int, str] = {-1: "proc_cwd", -2: "proc_root", -3: "proc_exe"}
FMODE_EXEC = 0x20
FMODE_PATH = 0x4000
# A lookup through a link prints its op from LOOK_OPS with this bit in place of the open's mode.
LOOKUP = 0x10000
LINK_LOOKS: dict[int, Literal["stat", "access", "chdir"]] = {1: "stat", 2: "access", 4: "chdir"}
ACCESS: dict[int, Literal["read", "write", "read_write"]] = {1: "read", 2: "write", 3: "read_write"}


@record("o")
def _proc_link(body: str, at: Stamp) -> Ptrace:
    f = body.split(" ", 8)
    if len(f) != 9:
        raise ValueError("short proc link line")
    pid, target, named, link, mode, ino, imode, magic = (int(x) for x in f[:8])
    request = PROC_LINKS.get(link, "proc_fd" if link >= 0 else "")
    if not request:
        raise ValueError("unknown proc link")
    # An O_PATH open or a lookup comes with the file's own name only, which a pipe does not have.
    path: str | None = f[8]
    if mode & LOOKUP:
        # A readlink reads the link itself and never follows it.
        access = LINK_LOOKS.get(mode & 0xFF)
        if access is None:
            raise ValueError("unknown lookup through a link")
        partial = bool(path)
        path = path or None
    elif mode & FMODE_PATH:
        access, partial = "path", bool(path)
        path = path or None
    else:
        access = "exec" if mode & FMODE_EXEC else ACCESS.get(mode & 3)
        partial = _cut(f[8])
    return Ptrace(
        pid,
        _clamp(target),
        request,
        ns_target=_clamp(named),
        fd=_clamp(link) if link >= 0 else None,
        access=access,
        kind=_fd(ino, imode, magic).kind,
        inode=_clamp(ino),
        path=path,
        path_partial=partial,
        at=at,
    )


@record("S")
def _signal(body: str, at: Stamp) -> Signal:
    pid, target, sig = _ints(body, 3)
    return Signal(pid, _clamp(target), _clamp(sig), at=at)


@record("M")
def _mem(body: str, at: Stamp) -> Mem | RawMmap:
    head = body.split(" ", 2)
    if len(head) < 3:
        raise ValueError("short mem line")
    pid, kind = int(head[0]), head[1]
    if kind == "x":
        f = body.split(" ", 4)
        if len(f) != 5:
            raise ValueError("expected five mem fields")
        path = f[4]
        partial = not path.startswith("/") or _cut(path)
        return Mem(
            pid,
            "exec",
            path,
            min(int(f[2]), MAX_INT),
            min(int(f[3]), MAX_INT),
            partial=partial,
            at=at,
        )
    if kind == "m":
        f = body.split(" ", 7)
        if len(f) != 8:
            raise ValueError("expected eight mem fields")
        walk = _walk(int(f[4]), int(f[6]), f[7].split("/"))
        return RawMmap(
            pid,
            walk,
            min(int(f[2]), MAX_INT),
            min(int(f[3]), MAX_INT),
            _clamp(int(f[5])),
            at=at,
        )
    raise ValueError("unknown mem kind")


@record("H")
def _setattr(body: str, at: Stamp) -> RawSetattr:
    f = body.split(" ", 11)
    if len(f) != 12:
        raise ValueError("expected twelve setattr fields")
    pid, dev, depth = int(f[0]), int(f[1]), int(f[2])
    valid, mode, uid, gid = (_clamp(int(x)) for x in f[3:7])
    size, atime_us, mtime_us, ctime_us = (int(x) for x in f[7:11])
    walk = _walk(dev, depth, f[11].split("/"))
    return RawSetattr(
        pid,
        walk,
        valid,
        mode,
        uid,
        gid,
        _clamp(size),
        _clamp(atime_us),
        _clamp(mtime_us),
        _clamp(ctime_us),
        at=at,
    )


@record("J")
def _ns(body: str, at: Stamp) -> Ns:
    pid, kind, flags = body.split(" ", 2)
    change: Literal["unshare", "setns"]
    if kind == "u":
        change = "unshare"
    elif kind == "n":
        change = "setns"
    else:
        raise ValueError("unknown namespace change")
    bits = _clamp(int(flags))
    return Ns(int(pid), change, bits, namespaces(bits), at=at)


@record("L")
def _identity(body: str, at: Stamp) -> Identity:
    pid, ppid, inode, dev, nlink, magic, size, mtime_us, ctime_us = _ints(body, 9)
    return Identity(
        pid,
        _clamp(ppid),
        _clamp(inode),
        _clamp(dev),
        _clamp(nlink),
        _clamp(magic),
        _clamp(size),
        _clamp(mtime_us),
        _clamp(ctime_us),
        at=at,
    )


@record("P")
def _refused_open(body: str, at: Stamp) -> Refused:
    f = body.split(" ", 5)
    if len(f) != 6:
        raise ValueError("expected six refused open fields")
    pid, errno, dev, depth = (int(x) for x in f[:4])
    name = Named(f[5], _hex_walk(dev, depth, f[4]), _cut(f[5]))
    return Refused(pid, "denied", errno, name, at=at)


REFUSED_OPS: dict[str, Literal["delete", "mkdir", "rename", "link", "symlink"]] = {
    "u": "delete",
    "d": "delete",
    "m": "mkdir",
    "n": "rename",
    "l": "link",
    "s": "symlink",
}


@record("r")
def _refused(body: str, at: Stamp) -> Refused:
    head = body.split(" ", 3)
    if len(head) != 4 or head[1] not in REFUSED_OPS:
        raise ValueError("unknown refused call")
    pid, op, errno = int(head[0]), REFUSED_OPS[head[1]], int(head[2])
    if head[1] in "udm":
        f = head[3].split(" ", 3)
        if len(f) != 4:
            raise ValueError("expected four refused call fields")
        dev, depth = int(f[0]), int(f[1])
        return Refused(pid, op, errno, Named(f[3], _hex_walk(dev, depth, f[2]), _cut(f[3])), at=at)
    f = head[3].split(" ", 7)
    if len(f) != 8:
        raise ValueError("expected eight refused call fields")
    raw = _hex(f[0])
    end = raw.find(b"\0")
    first = _text(raw if end < 0 else raw[:end])
    first_dev, first_depth, second_dev, second_depth = (int(x) for x in f[1:5])
    second = Named(f[7], _hex_walk(second_dev, second_depth, f[6]), _cut(f[7]))
    if op == "symlink":
        return Refused(pid, op, errno, second, Named(first, None, end < 0), at=at)
    first_named = Named(first, _hex_walk(first_dev, first_depth, f[5]), end < 0)
    if op == "link":
        return Refused(pid, op, errno, second, first_named, at=at)
    return Refused(pid, op, errno, first_named, second, at=at)


# Kernel iattr bits and the nanosecond values utimensat takes as now and as leave alone.
ATTR_MODE, ATTR_UID, ATTR_GID, ATTR_SIZE = 1, 2, 4, 8
ATTR_ATIME, ATTR_MTIME, ATTR_CTIME = 16, 32, 64
ATTR_ATIME_SET, ATTR_MTIME_SET, ATTR_TIMES_SET, ATTR_TOUCH = 128, 256, 1 << 16, 1 << 17
UTIME_NOW, UTIME_OMIT = (1 << 30) - 1, (1 << 30) - 2


@record("t")
def _refused_setattr(body: str, at: Stamp) -> RawSetattr:
    f = body.split(" ", 10)
    if len(f) != 11 or f[1] not in ("c", "o", "t", "u"):
        raise ValueError("unknown refused setattr")
    pid, kind, errno = int(f[0]), f[1], int(f[2])
    a, b, c, d, e = (int(x) for x in f[3:8])
    walk = _walk(int(f[8]), int(f[9]), f[10].split("/"))
    mode = size = 0
    atime: int | None = None
    mtime: int | None = None
    uid = gid = 0
    if kind == "c":
        valid, mode = ATTR_MODE | ATTR_CTIME, (a & 0o7777) | (b & 0o170000)
    elif kind == "o":
        valid, uid, gid = chown_mask(a, b)
    elif kind == "t":
        valid, size = ATTR_SIZE, _clamp(a)
    elif a == 0 or (c == UTIME_NOW and e == UTIME_NOW):
        valid = ATTR_ATIME | ATTR_MTIME | ATTR_CTIME | ATTR_TOUCH
    else:
        valid = ATTR_CTIME | ATTR_TIMES_SET
        if c != UTIME_OMIT:
            valid |= ATTR_ATIME
            if c != UTIME_NOW:
                valid, atime = valid | ATTR_ATIME_SET, _clamp(b * 1_000_000 + c // 1000)
        if e != UTIME_OMIT:
            valid |= ATTR_MTIME
            if e != UTIME_NOW:
                valid, mtime = valid | ATTR_MTIME_SET, _clamp(d * 1_000_000 + e // 1000)
    return RawSetattr(pid, walk, valid, mode, uid, gid, size, atime, mtime, None, errno, at=at)


def chown_mask(uid: int, gid: int) -> tuple[int, int, int]:
    """The iattr mask of a chown and the ids it sets; -1 leaves one as it is."""
    valid = ATTR_CTIME
    if uid != -1:
        valid |= ATTR_UID
    if gid != -1:
        valid |= ATTR_GID
    return valid, max(uid, 0), max(gid, 0)


@record("q")
def _refused_chown(body: str, at: Stamp) -> RefusedChown:
    f = body.split(" ", 6)
    if len(f) != 7:
        raise ValueError("expected seven refused chown fields")
    pid, errno, uid, gid, dev, depth = (int(x) for x in f[:6])
    walk, sep, name = f[6].partition(" ")
    if not walk or not sep:
        raise ValueError("refused chown without a walk and a name")
    named = Named(name, _hex_walk(dev, depth, walk), bool(name) and _cut(name))
    return RefusedChown(pid, errno, uid, gid, named, at=at)


@record("u")
def _refused_node(body: str, at: Stamp) -> RefusedNode:
    f = body.split(" ", 5)
    if len(f) != 6:
        raise ValueError("expected six refused mknod fields")
    pid, errno, mode, dev, depth = (int(x) for x in f[:5])
    walk, sep, name = f[5].partition(" ")
    if not walk or not sep:
        raise ValueError("refused mknod without a walk and a name")
    named = Named(name, _hex_walk(dev, depth, walk), _cut(name))
    return RefusedNode(pid, errno, _clamp(mode), named, at=at)


XATTR_OPS: dict[str, Literal["set", "remove"]] = {
    "s": "set",
    "r": "remove",
    "1": "set",
    "2": "remove",
}


def _xattr_name(field: str) -> tuple[str, bool]:
    """An attribute name printed as 256 bytes of hex, cut at its NUL. No NUL means it is cut."""
    raw = _hex(field)
    end = raw.find(b"\0")
    return _text(raw if end < 0 else raw[:end]), end < 0


@record("x")
def _xattr(body: str, at: Stamp) -> RawXattr:
    f = body.split(" ", 6)
    if len(f) != 7 or f[1] not in ("s", "r"):
        raise ValueError("unknown xattr call")
    pid, op = int(f[0]), XATTR_OPS[f[1]]
    size, flags = min(int(f[2]), MAX_INT), _clamp(int(f[3]))
    name, cut = _xattr_name(f[4])
    rest = f[6].split(" ", 1)
    if len(rest) != 2:
        raise ValueError("expected an xattr walk")
    walk = _walk(int(f[5]), int(rest[0]), rest[1].split("/"))
    return RawXattr(pid, op, size, flags, name, cut, walk, at=at)


@record("y")
def _refused_xattr(body: str, at: Stamp) -> RefusedXattr:
    f = body.split(" ", 8)
    if len(f) != 9 or f[1] not in ("1", "2"):
        raise ValueError("unknown refused xattr call")
    pid, op, errno = int(f[0]), XATTR_OPS[f[1]], int(f[2])
    size, flags = min(int(f[3]), MAX_INT), _clamp(int(f[4]))
    name, cut = _xattr_name(f[5])
    dev, depth = int(f[6]), int(f[7])
    walk, sep, path = f[8].partition(" ")
    if not walk or not sep:
        raise ValueError("refused xattr call without a walk and a path")
    named = Named(path, _hex_walk(dev, depth, walk), bool(path) and _cut(path))
    return RefusedXattr(pid, op, errno, size, flags, name, cut, named, at=at)


PROTOCOLS: dict[int, Literal["tcp", "udp"]] = {6: "tcp", 17: "udp"}


@record("n")
def _listen(body: str, at: Stamp) -> Listen:
    f = _fields(body, 6)
    if f[1] not in ("b", "l") or f[2] not in ("4", "6"):
        raise ValueError("unknown bind or listen")
    kind: Literal["bind", "listen"] = "bind" if f[1] == "b" else "listen"
    family: Literal["inet", "inet6"] = "inet" if f[2] == "4" else "inet6"
    port = int(f[5])
    # A listen on a socket never bound holds port 0 until the kernel binds it.
    none = kind == "listen" and port == 0
    return Listen(
        int(f[0]), kind, family, PROTOCOLS.get(int(f[3])), f[4], None if none else port, at=at
    )


@record("Z")
def _close(body: str, at: Stamp) -> RawClose:
    pid, size, dev, depth, names = body.split(" ", 4)
    walk = _walk(int(dev), int(depth), names.split("/"))
    return RawClose(int(pid), min(int(size), MAX_INT), walk, at=at)


@record("D")
def _denied(body: str, at: Stamp) -> RawDenied:
    pid, nr, errno = _ints(body, 3)
    return RawDenied(pid, nr, errno, at=at)


def _cut(path: str) -> bool:
    """Whether a path the kernel printed may be cut at the tracer's string cap, or is missing.

    Bytes that are not UTF-8 arrive as four-character escapes, so this never undercounts.
    """
    return not path or len(path.encode()) >= STRLEN - 1


def _walk(dev: int, depth: int, parts: list[str]) -> Walk:
    """A walk printed as `DEPTH` names joined by `/`, where `depth` names precede the root.

    Names cannot hold `/`, and a filesystem root's name is `/`, so each root level adds two empty
    parts. A pseudo filesystem's file, such as a pipe or an eventfd, is its own root and repeats
    its own name, so its walk resolves as a partial path.
    """
    names, rest = parts[:depth], parts[depth:]
    if not 0 <= depth <= DEPTH or not all(names):
        raise ValueError("walk does not match its depth")
    if len(parts) == 2 * DEPTH - depth and not any(rest):
        root = (b"/",) if depth < DEPTH else ()
        return Walk(dev, tuple(n.encode() for n in names) + root)
    if len(parts) == DEPTH and depth < DEPTH and len(set(rest)) == 1:
        return Walk(dev, tuple(n.encode() for n in [*names, rest[0]]))
    raise ValueError("walk does not match its depth")


def _hex_walk(dev: int, depth: int, field: str) -> Walk | None:
    """A walk printed as DEPTH hex names joined by `/`, or None when `depth` is -1.

    Names cannot hold `/`, so a filesystem root's name `/` marks the root. A pseudo filesystem's
    file is its own root and repeats its own name, so its walk resolves as a partial path.
    """
    if depth == -1:
        return None
    parts = [_hex(x) for x in field.split("/")]
    names, rest = parts[:depth], parts[depth:]
    if len(parts) != DEPTH or not 0 <= depth <= DEPTH or not all(names):
        raise ValueError("walk does not match its depth")
    if any(b"/" in n for n in names):
        raise ValueError("walk does not match its depth")
    if not rest or all(r == b"/" for r in rest):
        return Walk(dev, tuple(names) + ((b"/",) if rest else ()))
    if len(set(rest)) == 1:
        return Walk(dev, (*names, rest[0]))
    raise ValueError("walk does not match its depth")


def _fields(body: str, count: int) -> list[str]:
    f = body.split(" ")
    if len(f) != count:
        raise ValueError(f"expected {count} fields, found {len(f)}")
    return f


def _ints(body: str, count: int) -> list[int]:
    return [int(x) for x in _fields(body, count)]


def _hex(value: str) -> bytes:
    if not _HEX.match(value):
        raise ValueError("not hex")
    return bytes.fromhex(value.replace("\\x", ""))


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="backslashreplace")


def _clamp(value: int) -> int:
    return max(-MAX_INT, min(value, MAX_INT))


def _pid(body: str) -> int | None:
    try:
        return int(body.split(" ", 1)[0])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Gap:
    """Records missing from one cgroup's stream on one cpu, between the kernel times of their
    neighbours."""

    cpu: int
    first: int
    count: int
    ts_from: int | None
    ts_to: int
    cgroup: int = 0


@dataclass(slots=True)
class _Hole:
    start: int
    end: int
    ts_from: int | None
    ts_to: int


# How far a cpu's stream must run past a counter reading before the reading is checked: a
# record numbered before it may still be on its way, as late as a hole is waited for.
LATE_US = 1_000_000
# A stream not counted and not heard from this long is forgotten: its cgroup is gone.
FORGET_US = 10_000_000

Key = tuple[int, int]


class Sequence:
    """Finds lost records from the sequence numbers in record headers, one stream per cgroup
    and cpu.

    One probe can interrupt another on the same cpu, so a record may arrive after a later one.
    A hole is reported once its stream has moved `window` records past it, or by `expire`. The
    last records of a stream leave no hole; `counted` takes the kernel's own count of each
    stream, which shows them once their cpu's records are read `LATE_US` past the reading.
    """

    def __init__(self, window: int = 64) -> None:
        self.window = window
        self.latest = 0
        self._next: dict[Key, int] = {}
        self._ts: dict[Key, int] = {}
        self._holes: dict[Key, list[_Hole]] = {}
        # Each cpu's latest record time, and the counter readings that wait for it to pass.
        self._read: dict[int, int] = {}
        self._marks: dict[int, dict[int, tuple[int, int]]] = {}

    def observe(self, at: Stamp) -> list[Gap]:
        self.latest = max(self.latest, at.ts)
        read = self._read[at.cpu] = max(self._read.get(at.cpu, 0), at.ts)
        key = (at.cgroup, at.cpu)
        expected = self._next.get(key, 1)
        if at.seq >= expected:
            if at.seq > expected:
                hole = _Hole(expected, at.seq, self._ts.get(key), at.ts)
                self._holes.setdefault(key, []).append(hole)
            self._next[key] = at.seq + 1
            self._ts[key] = at.ts
        else:
            self._fill(key, at.seq)
        limit = self._next.get(key, 1) - self.window
        return self._take(key, lambda h: h.end <= limit) + self._check(at.cpu, read - LATE_US)

    def expire(self, before_ts: int) -> list[Gap]:
        """Report holes whose later neighbour came before `before_ts`."""
        return [
            g for key in list(self._holes) for g in self._take(key, lambda h: h.ts_to < before_ts)
        ]

    def counted(self, ts: int, counts: dict[Key, int]) -> None:
        """The kernel had numbered `counts[key]` records of each stream by `ts`.

        A reading replaces an earlier one of the same stream only once that one's records have
        all come, so records that never come are always reported. Streams the kernel no longer
        counts are forgotten once quiet: their cgroup was removed.
        """
        for (cgroup, cpu), seq in counts.items():
            expected = self._next.get((cgroup, cpu), 1)
            if seq < expected:
                continue
            marks = self._marks.setdefault(cpu, {})
            mark = marks.get(cgroup)
            if mark is None or mark[0] < expected:
                marks[cgroup] = (seq, ts)
        for key in [k for k, t in self._ts.items() if k not in counts and t < ts - FORGET_US]:
            if key not in self._holes and key[0] not in self._marks.get(key[1], {}):
                del self._next[key], self._ts[key]

    def pending(self, cgroup: int) -> bool:
        """Whether a hole or a reading ahead of the records of `cgroup` is still open."""
        if any(key[0] == cgroup for key in self._holes):
            return True
        for cpu, marks in self._marks.items():
            mark = marks.get(cgroup)
            if mark is not None and mark[0] >= self._next.get((cgroup, cpu), 1):
                return True
        return False

    def _check(self, cpu: int, before_ts: int) -> list[Gap]:
        """Report the records each ripe reading on `cpu` counted that never came."""
        marks = self._marks.get(cpu)
        if not marks:
            return []
        gaps = []
        for cgroup, (seq, ts) in list(marks.items()):
            if ts >= before_ts:
                continue
            del marks[cgroup]
            key = (cgroup, cpu)
            expected = self._next.get(key, 1)
            if seq >= expected:
                gaps.append(Gap(cpu, expected, seq + 1 - expected, self._ts.get(key), ts, cgroup))
                self._next[key], self._ts[key] = seq + 1, ts
        return gaps

    def _fill(self, key: Key, seq: int) -> None:
        holes = self._holes.get(key, [])
        for i, h in enumerate(holes):
            if h.start <= seq < h.end:
                rest = [
                    _Hole(h.start, seq, h.ts_from, h.ts_to),
                    _Hole(seq + 1, h.end, h.ts_from, h.ts_to),
                ]
                holes[i : i + 1] = [r for r in rest if r.end > r.start]
                if not holes:
                    del self._holes[key]
                return

    def _take(self, key: Key, ripe: Callable[[_Hole], bool]) -> list[Gap]:
        holes = self._holes.get(key)
        if not holes:
            return []
        gone = [h for h in holes if ripe(h)]
        if not gone:
            return []
        rest = [h for h in holes if not ripe(h)]
        if rest:
            self._holes[key] = rest
        else:
            del self._holes[key]
        cgroup, cpu = key
        return [Gap(cpu, h.start, h.end - h.start, h.ts_from, h.ts_to, cgroup) for h in gone]


@dataclass(frozen=True, slots=True)
class MountPoint:
    dev: tuple[int, int]
    root: str
    target: str


class Mounts:
    """Maps a filesystem-relative path to where the sandbox sees it, from `/proc/self/mountinfo`."""

    def __init__(self, points: list[MountPoint]) -> None:
        self.points = points

    @classmethod
    def parse(cls, mountinfo: str) -> Mounts:
        points: list[MountPoint] = []
        for line in mountinfo.splitlines():
            f = line.split(" ")
            if len(f) < 5 or ":" not in f[2]:
                continue
            major, minor = f[2].split(":", 1)
            points.append(MountPoint((int(major), int(minor)), _unescape(f[3]), _unescape(f[4])))
        return cls(points)

    def resolve(self, walk: Walk) -> tuple[str, bool]:
        """The sandbox path of `walk`, and whether its leading part is unknown."""
        names: list[str] = []
        whole = False
        for name in walk.names:
            if name == b"/":
                whole = True
                break
            names.append(_text(name))
        inner = "/".join(reversed(names))
        if not whole:
            beneath = self._beneath(walk.dev, list(reversed(names)))
            return (f".../{inner}", True) if beneath is None else (beneath, False)
        path = f"/{inner}"
        dev = (walk.dev >> 20, walk.dev & 0xFFFFF)
        best: MountPoint | None = None
        for point in self.points:
            if point.dev != dev or not _under(path, point.root):
                continue
            if best is None or len(point.root) > len(best.root):
                best = point
        if best is None:
            return path, True
        rest = path if best.root == "/" else path[len(best.root) :]
        return (best.target.rstrip("/") + rest) or "/", False

    def _beneath(self, dev: int, parts: list[str]) -> str | None:
        """Where a walk cut short of its filesystem root sits in the sandbox, when its top names
        end the root of one mount of its filesystem: at least that mount's last two names, so a
        folder of the same name inside it cannot pass for it."""
        major_minor = (dev >> 20, dev & 0xFFFFF)
        found: list[tuple[int, str]] = []
        for point in self.points:
            if point.dev != major_minor or point.root == "/":
                continue
            root = point.root.strip("/").split("/")
            for k in range(min(len(root), len(parts)), 1, -1):
                if parts[:k] == root[-k:]:
                    rest = "/".join(parts[k:])
                    found.append((k, point.target.rstrip("/") + (f"/{rest}" if rest else "")))
                    break
        if not found:
            return None
        best = max(k for k, _ in found)
        paths = {path for k, path in found if k == best}
        return paths.pop() if len(paths) == 1 else None


def _under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(f"{root}/")


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


DEEP_WALKS.update(
    {
        RawClose: ("walk",),
        Delete: ("walk",),
        Rename: ("old", "new"),
        Mkdir: ("walk",),
        Symlink: ("walk",),
        Link: ("old", "new"),
        RawFifo: ("walk",),
        RawSetattr: ("walk",),
        RawXattr: ("walk",),
        RawCache: ("walk",),
        RawSync: ("walk",),
    }
)
