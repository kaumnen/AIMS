"""Parse the kernel tracer's output.

Every line has a fixed field count. Strings that can hold any byte are hex, except a single
trailing path. A line that does not fit its shape is returned as `Garbled`, never guessed at.
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from aims.sandbox.base import Exit, File, Fork, Net, Oom

DEPTH = 12

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


@dataclass(frozen=True, slots=True)
class Dropped:
    count: int


@dataclass(frozen=True, slots=True)
class Bprm:
    pid: int
    path: str


@dataclass(frozen=True, slots=True)
class RawExec:
    pid: int
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
class Delete:
    pid: int
    walk: Walk


@dataclass(frozen=True, slots=True)
class Rename:
    pid: int
    old: Walk
    new: Walk


@dataclass(frozen=True, slots=True)
class RawDenied:
    pid: int
    nr: int
    errno: int


@dataclass(frozen=True, slots=True)
class Garbled:
    pid: int | None
    line: str


@dataclass(frozen=True, slots=True)
class Note:
    text: str


Item = (
    Ready | Dropped | Fork | Bprm | RawExec | Exit | Oom | File | Delete | Rename | Net | RawDenied
)


def parse(line: str) -> Item | Garbled | Note | None:
    """Turn one line of `bpftrace -f json` output into an item."""
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
    text = data.removesuffix("\n")
    try:
        return _parse(text)
    except ValueError, IndexError:
        return Garbled(_pid(text), text)


def _parse(text: str) -> Item | Garbled:
    tag = text[:2]
    match tag:
        case "R ":
            (base,) = _ints(text, 2)
            return Ready(base)
        case "F ":
            parent, child = _ints(text, 3)
            return Fork(parent, child)
        case "B ":
            _, pid, path = text.split(" ", 2)
            return Bprm(int(pid), path)
        case "E ":
            f = text.split(" ")
            _count(f, 9)
            pid, ppid, root, uid, pidns, ns_pid, size = (int(x) for x in f[1:8])
            argv = _hex(f[8]).split(b"\0")
            if argv and argv[-1] == b"":
                argv.pop()
            return RawExec(
                pid, ppid, root == 1, uid, pidns, ns_pid, size, tuple(_text(a) for a in argv)
            )
        case "X ":
            pid, code, sig = _ints(text, 4)
            return Exit(pid, None if sig else code, sig or None)
        case "O ":
            (pid,) = _ints(text, 2)
            return Oom(pid)
        case "W " | "A ":
            _, pid, path = text.split(" ", 2)
            return File(int(pid), "write" if tag == "W " else "read", path)
        case "U ":
            f = text.split(" ")
            _count(f, 3 + DEPTH)
            return Delete(int(f[1]), Walk(int(f[2]), tuple(_hex(x) for x in f[3:])))
        case "N ":
            f = text.split(" ")
            _count(f, 4 + 2 * DEPTH)
            old = Walk(int(f[2]), tuple(_hex(x) for x in f[4 : 4 + DEPTH]))
            new = Walk(int(f[3]), tuple(_hex(x) for x in f[4 + DEPTH :]))
            return Rename(int(f[1]), old, new)
        case "C ":
            f = text.split(" ")
            _count(f, 6)
            kind: Literal["connect", "sendto"] = "connect" if f[2] == "c" else "sendto"
            if f[3] == "1":
                raw = _hex(f[4])
                if raw.startswith(b"\0"):
                    path = "@" + _text(raw[1:].rstrip(b"\0"))
                else:
                    path = _text(raw.split(b"\0")[0])
                return Net(int(f[1]), kind, "unix", path, None)
            family: Literal["inet", "inet6"] = "inet" if f[3] == "4" else "inet6"
            return Net(int(f[1]), kind, family, f[4], int(f[5]))
        case "P ":
            _, pid, errno, path = text.split(" ", 3)
            relative = not path.startswith("/")
            return File(int(pid), "denied", path, errno=int(errno), partial=relative)
        case "D ":
            pid, nr, errno = _ints(text, 4)
            return RawDenied(pid, nr, errno)
    return Garbled(_pid(text), text)


def _ints(text: str, count: int) -> list[int]:
    f = text.split(" ")
    _count(f, count)
    return [int(x) for x in f[1:]]


def _count(fields: list[str], count: int) -> None:
    if len(fields) != count:
        raise ValueError(f"expected {count} fields, found {len(fields)}")


def _hex(value: str) -> bytes:
    if not _HEX.match(value):
        raise ValueError("not hex")
    return bytes.fromhex(value.replace("\\x", ""))


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="backslashreplace")


def _pid(text: str) -> int | None:
    parts = text.split(" ", 2)
    try:
        return int(parts[1])
    except IndexError, ValueError:
        return None


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
