import re

from aims.events.schema import _registry
from aims.sandbox.base import TOOLBOX
from aims.sandbox.images import ASSETS
from aims.sandbox.trace import ENV_CAP, PARSERS, STRLEN
from aims.sandbox.tracer import TRACE

TAGS = set("RFBEXOWAUNCPD") | set("VGT") | set("Z") | set("Q") | set("IKSMHJL") | set("Y")
TAGS |= set("abcdefgh")
TYPES = {
    "run.started",
    "run.finished",
    "log.truncated",
    "tool.call",
    "tool.result",
    "model.request",
    "model.response",
    "model.checked",
    "sandbox.started",
    "sandbox.stopped",
    "exec.started",
    "exec.finished",
    "proc.exec",
    "proc.fork",
    "proc.exit",
    "proc.output",
    "file.access",
    "file.write",
    "file.sync",
    "file.setattr",
    "fs.snapshot",
    "net.connect",
    "sys.denied",
    "ipc.event",
    "ptrace.event",
    "proc.signal",
    "mem.exec",
    "ns.event",
    "audit.lost",
    "audit.spill",
    "resource.sample",
    "monitor.flag",
    "monitor.sweep",
    "monitor.cleared",
    "monitor.verdict",
    "monitor.action",
    "monitor.degraded",
    "agent.started",
    "agent.input",
    "agent.step",
    "agent.finished",
    "agent.held",
    "agent.report",
    "agent.compacted",
    "task.scored",
    "replay.divergence",
    "sandbox.archived",
    "operator.action",
    "channel.message",
    "channel.read",
    "channel.snapshot",
    "cache.probe",
    "plant.triggered",
    "score.unit",
    "score.unexplained",
    "score.answer",
    "score.awareness",
    "selftest.case",
}
RESERVED_TAGS: set[str] = set()
RESERVED_TYPES: set[str] = set()

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
        assert fmt.startswith("%llu %llu %llu ") or fmt == "%llu %llu %llu\\n", tag
        assert args.startswith("nsecs(boot), cpu, $s,") or args == "nsecs(boot), cpu, $s", tag
        assert before == SEQ, tag


def test_heartbeat_fits_on_the_stack() -> None:
    # The profile probe interrupts other probes; a printf over 32 bytes would share their buffer.
    (tick,) = [p for p in _printfs() if p[0] == "Q"]
    assert tick[1:3] == ("%llu %llu %llu\\n", "nsecs(boot), cpu, $s")


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


def test_string_cap_matches_the_script() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    assert f"max_strlen = {STRLEN}" in script
    assert f"while ($n < {STRLEN})" in script


def test_sandbox_probes_filter_by_pid_namespace() -> None:
    # A sandbox process can rename itself but cannot leave its pid namespace. bpftrace tags the
    # level as kernel memory and syscall exit maps as user memory, and warns when && joins them;
    # `!` drops the tag. On 7.0 a tracepoint probe that reads curtask->thread_pid after args makes
    # bpftrace 0.23 lose struct filename's anonymous head, so raw_syscalls:sys_exit goes without
    # the level test; the runtime records it lets through are already foreign.
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    probes = [line for line in script.splitlines() if re.match(r"(fentry|fexit|tracepoint):", line)]
    assert not [line for line in probes if "comm" in line]
    level = re.compile(
        r"curtask->thread_pid->level > @base|!\(curtask->thread_pid->level <= @base\)"
    )
    unfiltered = [
        line.split(" ", 1)[0]
        for line in probes
        if "@sandbox[cgroup]" in line and not level.search(line)
    ]
    assert unfiltered == [
        "tracepoint:task:task_newtask",
        "tracepoint:sched:sched_process_exec",
        "tracepoint:sched:sched_process_exit",
        "tracepoint:raw_syscalls:sys_exit",
    ]


def test_overlay_filters_match_the_script() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    assert "!(args.file->f_mode & 0x1000000)" in script
    assert "s/f_mode & 0x1000000)/" in TRACE
    hooks = [
        line
        for line in script.splitlines()
        if line.startswith(("fentry:vmlinux:security_inode_", "fentry:vmlinux:security_file_open"))
    ]
    assert len(hooks) == 8
    assert all("curtask->cred == curtask->real_cred" in line for line in hooks)
