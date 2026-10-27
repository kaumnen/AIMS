import re

from aims.events.schema import _registry
from aims.sandbox.base import TOOLBOX
from aims.sandbox.images import ASSETS
from aims.sandbox.trace import ENV_CAP, PARSERS

TAGS = set("RFBEXOWAUNCPD") | set("VGT") | set("Z") | set("Q")
TYPES = {
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
    "proc.output",
    "file.access",
    "file.write",
    "fs.snapshot",
    "net.connect",
    "sys.denied",
    "audit.lost",
    "audit.spill",
    "resource.sample",
    "monitor.flag",
}
RESERVED_TAGS = set("IKSMHJL")
RESERVED_TYPES = {
    "ipc.event",
    "ptrace.event",
    "proc.signal",
    "mem.exec",
    "file.setattr",
    "ns.event",
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
    assert not TAGS & RESERVED_TAGS
    assert not TYPES & RESERVED_TYPES


def test_built_tags_and_types_match() -> None:
    assert set(PARSERS) == TAGS
    assert set(_registry) == TYPES


def test_every_tracer_line_has_a_parser_and_a_header() -> None:
    printfs = _printfs()
    assert {tag for tag, *_ in printfs} == TAGS
    for tag, fmt, args, before in printfs:
        assert fmt.startswith("%llu %llu %llu "), tag
        assert args.startswith("nsecs(boot), cpu, $s,"), tag
        assert before == SEQ, tag


def test_env_cap_matches_the_probe() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    chunks = re.search(r"unroll\((\d+)\) \{\s*if \(\$eo < \$en\)", script)
    size = re.search(r"\$eo \+= (\d+);", script)
    assert chunks is not None
    assert size is not None
    assert int(chunks.group(1)) * int(size.group(1)) == ENV_CAP


def test_sandbox_mark_matches_the_toolbox_mount() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    assert f'strncmp($f, "{TOOLBOX}/", {len(TOOLBOX) + 1}) == 0' in script
