"""Parse the kernel tracer's output.

Every line has a fixed field count. Strings that can hold any byte are hex, except a single
trailing path or walk. Bytes that are not UTF-8 are kept as `\\xNN` escapes. A line that does
not fit its shape is returned as `Garbled`, never guessed at. Each tag is one `@record` parser
below and one probe block in `trace.bt`.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from aims.events import MAX_INT, FdKind
from aims.sandbox.base import (
    Close,
    Exit,
    Fd,
    File,
    Fork,
    Ipc,
    Kernel,
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

PTRACE_REQUESTS: dict[int, str] = {
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
        265: "open_by_handle_at",
        264: "name_to_handle_at",
        282: "userfaultfd",
        425: "io_uring_setup",
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
        304: "open_by_handle_at",
        303: "name_to_handle_at",
        323: "userfaultfd",
        425: "io_uring_setup",
        430: "fsopen",
        432: "fsmount",
        431: "fsconfig",
        429: "move_mount",
        428: "open_tree",
        442: "mount_setattr",
        135: "personality",
    },
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
class RawExec(Raw):
    ppid: int
    root: bool
    uid: int
    pidns: int
    ns_pid: int
    argv_bytes: int
    argv: tuple[str, ...]


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
    walk: Walk
    valid: int
    mode: int
    uid: int
    gid: int
    size: int
    atime_us: int
    mtime_us: int
    ctime_us: int


@dataclass(frozen=True, slots=True)
class Garbled:
    pid: int | None
    line: str
    at: Stamp | None = None


@dataclass(frozen=True, slots=True)
class Note:
    text: str


Item = Ready | Tick | Raw | Kernel
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

    A record line is `TAG TS CPU SEQ FIELDS...`: the kernel boot clock in ns, the cpu, and the
    cpu's sequence number, then the fields of that tag, if it has any. Times are kept in µs,
    which canonical JSON can hold.
    """
    try:
        doc = json.loads(line)
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
    try:
        tag, ts, cpu, seq, *rest = text.split(" ", 4)
        at = Stamp(int(ts) // 1000, int(cpu), int(seq))
        body = rest[0] if rest else ""
    except ValueError:
        return Garbled(None, text)
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
    parent, child = _ints(body, 2)
    return Fork(parent, child, at=at)


@record("B")
def _bprm(body: str, at: Stamp) -> Bprm:
    pid, path = body.split(" ", 1)
    return Bprm(int(pid), path, at=at)


@record("E")
def _exec(body: str, at: Stamp) -> RawExec:
    f = _fields(body, 8)
    pid, ppid, root, uid, pidns, ns_pid, size = (int(x) for x in f[:7])
    argv = _hex(f[7]).split(b"\0")
    if argv and argv[-1] == b"":
        argv.pop()
    args = tuple(_text(a) for a in argv)
    return RawExec(pid, ppid, root == 1, uid, pidns, ns_pid, size, args, at=at)


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
    if kind in ("b", "c"):
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
            path = _text(raw.rstrip(b"\0"))
            partial = not path.startswith("/")
        return Ipc(
            pid,
            "bind" if kind == "b" else "connect",
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
    pid, target, request, ret = _ints(body, 4)
    name = PTRACE_REQUESTS.get(request, f"request_{request:#x}")
    refused = ret < 0 and request not in (1, 2, 3)
    return Ptrace(
        pid,
        _clamp(target),
        name,
        refused,
        _clamp(-ret) if refused else None,
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
def _refused(body: str, at: Stamp) -> File:
    pid, errno, path = body.split(" ", 2)
    partial = not path.startswith("/") or _cut(path)
    return File(int(pid), "denied", path, errno=int(errno), partial=partial, at=at)


@record("Z")
def _close(body: str, at: Stamp) -> Close:
    pid, size, path = body.split(" ", 2)
    return Close(int(pid), min(int(size), MAX_INT), path, partial=_cut(path), at=at)


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

    Names cannot hold `/`, and the root's name is `/`, so each root level adds two empty parts.
    """
    if not 0 <= depth <= DEPTH or len(parts) != 2 * DEPTH - depth:
        raise ValueError("walk does not match its depth")
    names, rest = parts[:depth], parts[depth:]
    if not all(names) or any(rest):
        raise ValueError("walk does not match its depth")
    root = (b"/",) if depth < DEPTH else ()
    return Walk(dev, tuple(n.encode() for n in names) + root)


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
    """Records missing from one cpu's stream, between the kernel times of their neighbours."""

    cpu: int
    first: int
    count: int
    ts_from: int | None
    ts_to: int


@dataclass(slots=True)
class _Hole:
    start: int
    end: int
    ts_from: int | None
    ts_to: int


class Sequence:
    """Finds lost records from the per-cpu sequence numbers in record headers.

    One probe can interrupt another on the same cpu, so a record may arrive after a later one.
    A hole is reported once its cpu has moved `window` records past it, or by `expire`.
    """

    def __init__(self, window: int = 64) -> None:
        self.window = window
        self.latest = 0
        self._next: dict[int, int] = {}
        self._ts: dict[int, int] = {}
        self._holes: dict[int, list[_Hole]] = {}

    def observe(self, at: Stamp) -> list[Gap]:
        self.latest = max(self.latest, at.ts)
        expected = self._next.get(at.cpu, 1)
        holes = self._holes.setdefault(at.cpu, [])
        if at.seq >= expected:
            if at.seq > expected:
                holes.append(_Hole(expected, at.seq, self._ts.get(at.cpu), at.ts))
            self._next[at.cpu] = at.seq + 1
            self._ts[at.cpu] = at.ts
        else:
            self._fill(holes, at.seq)
        return self._take(at.cpu, lambda h: h.end <= self._next[at.cpu] - self.window)

    def expire(self, before_ts: int) -> list[Gap]:
        """Report holes whose later neighbour came before `before_ts`."""
        return [
            g for cpu in list(self._holes) for g in self._take(cpu, lambda h: h.ts_to < before_ts)
        ]

    @staticmethod
    def _fill(holes: list[_Hole], seq: int) -> None:
        for i, h in enumerate(holes):
            if h.start <= seq < h.end:
                rest = [
                    _Hole(h.start, seq, h.ts_from, h.ts_to),
                    _Hole(seq + 1, h.end, h.ts_from, h.ts_to),
                ]
                holes[i : i + 1] = [r for r in rest if r.end > r.start]
                return

    def _take(self, cpu: int, ripe: Callable[[_Hole], bool]) -> list[Gap]:
        holes = self._holes.get(cpu)
        if not holes:
            return []
        gone = [h for h in holes if ripe(h)]
        if not gone:
            return []
        self._holes[cpu] = [h for h in holes if not ripe(h)]
        return [Gap(cpu, h.start, h.end - h.start, h.ts_from, h.ts_to) for h in gone]


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
            return f".../{inner}", True
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


def _under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(f"{root}/")


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
