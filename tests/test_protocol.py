import re

from aims.events.schema import _registry
from aims.sandbox.images import ASSETS
from aims.sandbox.trace import PARSERS

CORE_TAGS = set("RFBEXOWAUNCPD")
CORE_TYPES = {
    "run.started",
    "run.finished",
    "log.truncated",
    "tool.call",
    "tool.result",
    "sandbox.started",
    "sandbox.stopped",
    "exec.started",
    "exec.finished",
    "proc.exec",
    "proc.exit",
    "file.access",
    "net.connect",
    "sys.denied",
    "audit.lost",
    "monitor.flag",
}
RESERVED_TAGS = {
    "exec context and output": set("VGT"),
    "ipc, ptrace, signals, fileless, setattr, namespaces": set("IKSMHJL"),
    "file content": set("ZY"),
    "resources": set("Q"),
}
RESERVED_TYPES = {
    "exec context and output": {"proc.output"},
    "ipc, ptrace, signals, fileless, setattr, namespaces": {
        "ipc.event",
        "ptrace.event",
        "proc.signal",
        "mem.exec",
        "file.setattr",
        "ns.event",
    },
    "file content": {"file.write", "fs.snapshot", "audit.spill"},
    "resources": {"resource.sample"},
}

SEQ = "$s = @seq[cpu] + 1; @seq[cpu] = $s;"
_PRINTF = re.compile(r'printf\("(\S+) ([^"]*)",\s*([^;]*)\);', re.S)


def _printfs() -> list[tuple[str, str, str, str]]:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    out = []
    for match in _PRINTF.finditer(script):
        before = script[: match.start()].rstrip().rsplit("\n", 1)[-1].strip()
        out.append((match.group(1), match.group(2), match.group(3), before))
    return out


def test_reservations_do_not_overlap() -> None:
    tags = [CORE_TAGS, *RESERVED_TAGS.values()]
    assert sum(len(t) for t in tags) == len(set().union(*tags))
    types = [CORE_TYPES, *RESERVED_TYPES.values()]
    assert sum(len(t) for t in types) == len(set().union(*types))


def test_tags_and_types_are_reserved() -> None:
    assert set(PARSERS) <= CORE_TAGS.union(*RESERVED_TAGS.values())
    assert set(_registry) <= CORE_TYPES.union(*RESERVED_TYPES.values())


def test_every_tracer_line_has_a_parser_and_a_header() -> None:
    printfs = _printfs()
    assert {tag for tag, *_ in printfs} == set(PARSERS)
    for tag, fmt, args, before in printfs:
        assert fmt.startswith("%llu %llu %llu "), tag
        assert args.startswith("nsecs(boot), cpu, $s,"), tag
        assert before == SEQ, tag
