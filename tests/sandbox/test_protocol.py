import re

from aims.events.schema import _registry
from aims.sandbox.base import TOOLBOX
from aims.sandbox.images import ASSETS
from aims.sandbox.trace import ARGV_CAP, ENV_CAP, LOOK_BUDGET, PARSERS, STRLEN, UNGROUPED
from aims.sandbox.tracer import LOOKS, TRACE

TAGS = set("RFBEXOWAUNCPD") | set("VGT") | set("Z") | set("Q") | set("IKSMHJL") | set("Y")
TAGS |= set("abcdefghi") | set("rt") | set("o") | set("jklm")
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
    "sandbox.freeze_failed",
    "sandbox.outside_pause",
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
    "file.look",
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

SEQ = "$g = cgroup; $s = @seq[$g, cpu] + 1; @seq[$g, cpu] = $s;"
# The tracer's own records number under cgroup 0; the oom record's header still names it.
OWN = {
    "R": "$s = @seq[(uint64)0, cpu] + 1; @seq[(uint64)0, cpu] = $s;",
    "Q": "$s = @seq[(uint64)0, cpu] + 1; @seq[(uint64)0, cpu] = $s;",
    "O": "$g = (uint64)0; $s = @seq[$g, cpu] + 1; @seq[$g, cpu] = $s;",
}
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
        assert before == OWN.get(tag, SEQ), tag
        if tag in UNGROUPED:
            assert fmt.startswith("%llu %llu %llu ") or fmt == "%llu %llu %llu\\n", tag
            assert args.startswith("nsecs(boot), cpu, $s,") or args == "nsecs(boot), cpu, $s", tag
        else:
            assert fmt.startswith("%llu %llu %llu %llu "), tag
            assert args.startswith("nsecs(boot), cpu, $s, $g,"), tag


def test_heartbeat_fits_on_the_stack() -> None:
    # The profile probe interrupts other probes; a printf over 32 bytes would share their buffer.
    (tick,) = [p for p in _printfs() if p[0] == "Q"]
    assert tick[1:3] == ("%llu %llu %llu\\n", "nsecs(boot), cpu, $s")


def test_env_cap_matches_the_probe() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    assert f"$er = $en > {ENV_CAP} ? (uint64){ENV_CAP} : $en;" in script
    chunks = re.search(r"unroll\((\d+)\) \{\s*if \(\$eo < \$er\)", script)
    size = re.search(r"\$eo \+= (\d+);", script)
    assert chunks is not None
    assert size is not None
    # Each chunk is one buf(), which keeps at most STRLEN - 4 bytes.
    n, step = int(chunks.group(1)), int(size.group(1))
    assert step == STRLEN - 4
    assert f"$er - $eo > {step} ? {step} : $er - $eo" in script
    assert (n - 1) * step < ENV_CAP <= n * step


def test_argv_cap_matches_the_probe() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    step = STRLEN - 4
    assert f"while ($i * {step} < {ARGV_CAP}) {{ @argv_chunk[$i] = $i * {step}; $i++; }}" in script
    assert f"$ar = $len > {ARGV_CAP} ? (uint64){ARGV_CAP} : $len;" in script
    assert f"if ($ar > {step}) {{" in script
    assert f"$ar - $ao > {step} ? {step} : $ar - $ao" in script


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
    probes = [
        line for line in script.splitlines() if re.match(r"(fentry|fexit|(raw)?tracepoint):", line)
    ]
    assert not [line for line in probes if re.search(r"\bcomm\b", line)]
    level = re.compile(
        r"curtask->thread_pid->level > @base|!\(curtask->thread_pid->level <= @base\)"
    )
    unfiltered = [
        line.split(" ", 1)[0]
        for line in probes
        if "@sandbox[cgroup]" in line and not level.search(line)
    ]
    assert unfiltered == [
        "rawtracepoint:task_newtask",
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
    assert len(hooks) == 12
    assert all("curtask->cred == curtask->real_cred" in line for line in hooks)


def test_the_proc_link_probes_go_together() -> None:
    # A kernel without a function they attach to drops them as one range of lines; any line of
    # the range's map left outside would fail to compile.
    script = (ASSETS / "tracer" / "trace.bt").read_text().splitlines()
    first = next(
        i for i, x in enumerate(script) if x.startswith("fentry:vmlinux:proc_pid_get_link ")
    )
    last = next(
        i
        for i, x in enumerate(script)
        if x.startswith("tracepoint:sched:sched_process_exit /@plink")
    )
    assert [i for i, x in enumerate(script) if "@plink" in x and not first <= i <= last] == []
    assert "'/^fentry:vmlinux:proc_pid_get_link /,/^tracepoint:sched:sched_process_exit" in TRACE
    opens = [x for x in script if ":vmlinux:do_filp_open " in x]
    assert len(opens) == 3
    assert r"s/^\(fentry\|fexit\):vmlinux:do_filp_open /" in TRACE


def test_the_lookup_probes_drop_cleanly() -> None:
    # A kernel without a call that marks a lookup drops that call's lines one by one, so each
    # must be a whole probe. Without filename_lookup each of its probes goes as a range of lines.
    script = (ASSETS / "tracer" / "trace.bt").read_text().splitlines()
    for call in ("vfs_statx", "do_faccessat", "do_readlinkat"):
        lines = [x for x in script if f":vmlinux:{call} " in x]
        assert len(lines) == 2, call
        assert all(re.match(rf"f(entry|exit):vmlinux:{call} /.*\}}$", x) for x in lines), call
        assert call in LOOKS
    lookups = [i for i, x in enumerate(script) if ":vmlinux:filename_lookup " in x]
    assert all(re.match(r"f(entry|exit):vmlinux:filename_lookup /", script[i]) for i in lookups)
    assert r"'/^f\(entry\|exit\):vmlinux:filename_lookup /,/^}/d'" in LOOKS
    assert LOOKS in TRACE


def test_the_lookup_budget_matches_the_script() -> None:
    script = (ASSETS / "tracer" / "trace.bt").read_text()
    # Each probe that logs a lookup by name checks the thread's budget the same way.
    assert script.count(f"else if ($w.1 < {LOOK_BUDGET}) {{") == 3
    assert f"A process logs at most {LOOK_BUDGET} lookups a second" in script
