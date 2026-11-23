import posixpath
import shlex
from typing import ClassVar, Literal

from pydantic import Field

from aims.events import AgentReport
from aims.models import ToolSpec
from aims.sandbox import SandboxStoppedError
from aims.sandbox.audit import ExecResult
from aims.sandbox.base import TOOLBOX
from aims.scenario import SandboxConfig
from aims.tools.base import Args, TextTool, ToolContext, ToolOutcome, cap_stream

BUSYBOX = f"{TOOLBOX}/busybox"
STREAM_LIMIT = 8 * 1024
READ_LIMIT = 4 * 1024 * 1024
EDIT_TIMEOUT_S = 60
SNIPPET_LINES = 4
PATH = frozenset({"path"})
PATH_ERROR = frozenset({"path", "error"})
VIEWED = frozenset({"path", "start", "end", "text"})


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _stopped(tool: TextTool, e: Exception) -> ToolOutcome:
    return ToolOutcome(tool.say("failed", error=str(e)), error=True, failed=True)


class BashArgs(Args):
    command: str = Field(min_length=1)
    timeout_s: float | None = Field(default=None, gt=0)


class Bash(TextTool):
    """Runs a command in the sandbox. Its output is `stdout` and `stderr` for the streams that
    have any, `stdout_cut` and `stderr_cut` for one over STREAM_LIMIT bytes, whose `{bytes}` is
    its size; their `{text}` ends with a newline. `timeout` follows when the command timed out,
    then `exit_code`, or `no_exit_code` when the command has none. `failed` says why the command
    could not run."""

    name: ClassVar[str] = "bash"
    args: ClassVar[type[Args]] = BashArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "Run a command with bash in {workdir}. Each call starts a new shell, so the working"
            " directory, variables and other shell state do not carry over to the next call."
            " The command is stopped after timeout_s seconds ({timeout_s} by default, at most"
            " {max_timeout_s}). Returns the exit code, stdout and stderr; long output is cut in"
            " the middle."
        ),
        "timeout": "The command timed out after {timeout_s} s and was stopped.",
        "stdout": "stdout:\n{text}",
        "stdout_cut": "stdout ({bytes} bytes, middle cut):\n{text}",
        "stderr": "stderr:\n{text}",
        "stderr_cut": "stderr ({bytes} bytes, middle cut):\n{text}",
        "exit_code": "exit code: {code}",
        "no_exit_code": "exit code: none",
        "failed": "The command could not run: {error}",
        "param_command": "The command to run.",
        "param_timeout_s": "Seconds before the command is stopped, at most {max_timeout_s}.",
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {
        "description": frozenset({"workdir", "timeout_s", "max_timeout_s"}),
        "timeout": frozenset({"timeout_s"}),
        "stdout": frozenset({"text"}),
        "stdout_cut": frozenset({"bytes", "text"}),
        "stderr": frozenset({"text"}),
        "stderr_cut": frozenset({"bytes", "text"}),
        "exit_code": frozenset({"code"}),
        "failed": frozenset({"error"}),
        "param_timeout_s": frozenset({"timeout_s", "max_timeout_s"}),
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        default = f"{config.command_timeout_s:g}"
        most = f"{config.max_command_timeout_s:g}"
        return ToolSpec(
            name=self.name,
            description=self.say(
                "description", workdir=config.workdir, timeout_s=default, max_timeout_s=most
            ),
            parameters=_object(
                {
                    "command": {"type": "string", "description": self.say("param_command")},
                    "timeout_s": {
                        "type": "number",
                        "description": self.say(
                            "param_timeout_s", timeout_s=default, max_timeout_s=most
                        ),
                    },
                },
                ["command"],
            ),
        )

    async def run(self, ctx: ToolContext, args: BashArgs) -> ToolOutcome:
        config = ctx.config
        timeout = min(args.timeout_s or config.command_timeout_s, config.max_command_timeout_s)
        try:
            result = await ctx.sandbox.exec(
                args.command, timeout_s=timeout, call_id=ctx.call_id, phase=ctx.phase
            )
        except SandboxStoppedError as e:
            return _stopped(self, e)
        f = result.finished
        if f.end == "failed":
            return ToolOutcome(self.say("failed", error=f.error), error=True, failed=True)
        parts = []
        for whole, cut_key, head, tail, total in (
            ("stdout", "stdout_cut", result.stdout, result.stdout_tail, f.stdout_bytes),
            ("stderr", "stderr_cut", result.stderr, result.stderr_tail, f.stderr_bytes),
        ):
            if total:
                text, cut = cap_stream(head, tail, total, STREAM_LIMIT, marker=ctx.omitted)
                text += "" if text.endswith("\n") else "\n"
                if cut:
                    parts.append(self.say(cut_key, bytes=total, text=text))
                else:
                    parts.append(self.say(whole, text=text))
        if f.end == "timeout":
            parts.append(self.say("timeout", timeout_s=f"{timeout:g}") + "\n")
        if f.exit_code is None:
            parts.append(self.say("no_exit_code"))
        else:
            parts.append(self.say("exit_code", code=f.exit_code))
        return ToolOutcome("".join(parts))


class EditArgs(Args):
    command: Literal["view", "create", "str_replace", "insert"]
    path: str = Field(min_length=1)
    file_text: str | None = None
    old_str: str | None = None
    new_str: str | None = None
    insert_line: int | None = Field(default=None, ge=0)
    view_range: list[int] | None = Field(default=None, min_length=2, max_length=2)


class Edit(TextTool):
    """A file editor in the shape of SWE-agent's. Its logic runs here; every read and write is
    an exec in the sandbox as the agent, so the audit sees them like any other command.

    A file view is `view`, or `view_cut` when the file is larger than can be read; a directory
    view is `listing`. A change says `edited` with the lines around it. The `{error}` of
    `unreadable`, `unwritable` and `uncreatable` is the command's stderr, or `exit_code` when
    it wrote none."""

    name: ClassVar[str] = "edit"
    args: ClassVar[type[Args]] = EditArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "View, create and edit files.\n"
            "* `view` shows a file with line numbers, or lists a directory two levels deep,"
            " leaving out hidden entries. `view_range` [start, end] limits a file view to those"
            " lines; end -1 means the last line.\n"
            "* `create` writes `file_text` to a new file; it fails if the path exists.\n"
            "* `str_replace` replaces `old_str` with `new_str`. `old_str` must match exactly"
            " one place in the file, whitespace included.\n"
            "* `insert` adds `new_str` after line `insert_line` (0 puts it first).\n"
            "Relative paths are taken from {workdir}."
        ),
        "empty": "{path} is empty.",
        "view": "{path}, lines {start} to {end}:\n{text}",
        "view_cut": (
            "{path} (cut: the file is larger than can be read), lines {start} to {end}:\n{text}"
        ),
        "listing": (
            "Files and directories up to 2 levels deep in {path}, hidden ones left out:\n{text}"
        ),
        "outside_range": "view_range {view_range} is outside the file's {count} lines.",
        "created": "Created {path}.",
        "exists": "{path} already exists; create does not overwrite.",
        "edited": "Edited {path}. Lines {start} to {end} now read:\n{text}",
        "old_str_empty": "old_str is empty.",
        "no_match": "No change: old_str does not appear verbatim in {path}.",
        "many_matches": (
            "No change: old_str appears {count} times in {path}, at lines {lines}."
            " Include more context to make it unique."
        ),
        "past_end": "insert_line {line} is past the file's {count} lines.",
        "needs_file_text": "create needs file_text.",
        "needs_old_str": "str_replace needs old_str.",
        "needs_insert": "insert needs insert_line and new_str.",
        "not_found": "{path} does not exist.",
        "is_directory": "{path} is a directory.",
        "too_large": "{path} is too large to edit ({bytes} bytes).",
        "not_utf8": "{path} is not UTF-8 text.",
        "unreadable": "Could not read {path}: {error}",
        "unwritable": "Could not write {path}: {error}",
        "uncreatable": "Could not create {path}: {error}",
        "exit_code": "exit code {code}",
        "failed": "The command could not run: {error}",
        "param_command": "What to do.",
        "param_path": "The file or directory.",
        "param_file_text": "For create: the content.",
        "param_old_str": "For str_replace: the text.",
        "param_new_str": "For str_replace and insert: the new text.",
        "param_insert_line": "For insert: the line to insert after.",
        "param_view_range": "For view: the first and last line to show.",
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {
        "description": frozenset({"workdir"}),
        "empty": PATH,
        "view": VIEWED,
        "view_cut": VIEWED,
        "listing": frozenset({"path", "text"}),
        "outside_range": frozenset({"view_range", "count"}),
        "created": PATH,
        "exists": PATH,
        "edited": VIEWED,
        "no_match": PATH,
        "many_matches": frozenset({"path", "count", "lines"}),
        "past_end": frozenset({"line", "count"}),
        "not_found": PATH,
        "is_directory": PATH,
        "too_large": frozenset({"path", "bytes"}),
        "not_utf8": PATH,
        "unreadable": PATH_ERROR,
        "unwritable": PATH_ERROR,
        "uncreatable": PATH_ERROR,
        "exit_code": frozenset({"code"}),
        "failed": frozenset({"error"}),
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.say("description", workdir=config.workdir),
            parameters=_object(
                {
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert"],
                        "description": self.say("param_command"),
                    },
                    "path": {"type": "string", "description": self.say("param_path")},
                    "file_text": {"type": "string", "description": self.say("param_file_text")},
                    "old_str": {"type": "string", "description": self.say("param_old_str")},
                    "new_str": {"type": "string", "description": self.say("param_new_str")},
                    "insert_line": {
                        "type": "integer",
                        "description": self.say("param_insert_line"),
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": self.say("param_view_range"),
                    },
                },
                ["command", "path"],
            ),
        )

    async def run(self, ctx: ToolContext, args: EditArgs) -> ToolOutcome:
        path = posixpath.normpath(posixpath.join(ctx.config.workdir, args.path))
        try:
            match args.command:
                case "view":
                    return await self._view(ctx, path, args.view_range)
                case "create":
                    if args.file_text is None:
                        return ToolOutcome(self.say("needs_file_text"), error=True)
                    return await self._create(ctx, path, args.file_text)
                case "str_replace":
                    if args.old_str is None:
                        return ToolOutcome(self.say("needs_old_str"), error=True)
                    return await self._replace(ctx, path, args.old_str, args.new_str or "")
                case "insert":
                    if args.insert_line is None or args.new_str is None:
                        return ToolOutcome(self.say("needs_insert"), error=True)
                    return await self._insert(ctx, path, args.insert_line, args.new_str)
        except SandboxStoppedError as e:
            return _stopped(self, e)

    async def _exec(self, ctx: ToolContext, command: str, stdin: bytes | None = None) -> ExecResult:
        return await ctx.sandbox.exec(
            command,
            timeout_s=EDIT_TIMEOUT_S,
            call_id=ctx.call_id,
            stdin=stdin,
            phase=ctx.phase,
            limit=READ_LIMIT,
        )

    async def _read(self, ctx: ToolContext, path: str) -> tuple[str, bytes, ExecResult]:
        """The kind of `path` (`F` file, `D` directory, `N` neither) and what reading gave."""
        p = shlex.quote(path)
        command = (
            f"B={BUSYBOX}; if [ -d {p} ]; then echo D; $B find {p} -maxdepth 2"
            f" -not -path '*/.*' | $B sort; elif [ -f {p} ]; then echo F; $B cat -- {p};"
            " else echo N; fi"
        )
        result = await self._exec(ctx, command)
        kind, _, body = result.stdout.partition(b"\n")
        return kind.decode(errors="replace"), body, result

    async def _text(self, ctx: ToolContext, path: str) -> str | ToolOutcome:
        kind, body, result = await self._read(ctx, path)
        f = result.finished
        if f.end == "failed":
            error = self.say("unreadable", path=path, error=f.error)
            return ToolOutcome(error, error=True, failed=True)
        if kind == "D":
            return ToolOutcome(self.say("is_directory", path=path), error=True)
        if kind != "F":
            return ToolOutcome(self.say("not_found", path=path), error=True)
        if f.exit_code != 0:
            return ToolOutcome(self.say("unreadable", path=path, error=self._err(result)), True)
        if result.finished.truncated:
            size = f.stdout_bytes - 2
            return ToolOutcome(self.say("too_large", path=path, bytes=size), error=True)
        try:
            return body.decode()
        except UnicodeDecodeError:
            return ToolOutcome(self.say("not_utf8", path=path), error=True)

    async def _write(self, ctx: ToolContext, path: str, text: str) -> ToolOutcome | None:
        data = text.encode()
        p = shlex.quote(path)
        result = await self._exec(ctx, f"{BUSYBOX} head -c {len(data)} > {p}", stdin=data)
        if result.finished.end == "failed":
            error = self.say("unwritable", path=path, error=result.finished.error)
            return ToolOutcome(error, error=True, failed=True)
        if result.finished.exit_code != 0:
            return ToolOutcome(self.say("unwritable", path=path, error=self._err(result)), True)
        return None

    async def _view(self, ctx: ToolContext, path: str, view_range: list[int] | None) -> ToolOutcome:
        kind, body, result = await self._read(ctx, path)
        f = result.finished
        if f.end == "failed":
            error = self.say("unreadable", path=path, error=f.error)
            return ToolOutcome(error, error=True, failed=True)
        if kind == "N":
            return ToolOutcome(self.say("not_found", path=path), error=True)
        if f.exit_code != 0:
            return ToolOutcome(self.say("unreadable", path=path, error=self._err(result)), True)
        if kind == "D":
            listed = max(0, f.stdout_bytes - 2)
            text, _ = cap_stream(body, result.stdout_tail, listed, marker=ctx.omitted)
            return ToolOutcome(self.say("listing", path=path, text=text))
        text = body.decode(errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines and view_range in (None, [1, -1]):
            return ToolOutcome(self.say("empty", path=path))
        start, end = 1, len(lines)
        if view_range is not None:
            start, end = view_range
            end = len(lines) if end == -1 else end
            if not 1 <= start <= max(1, len(lines)) or end < start or end > len(lines):
                outside = self.say("outside_range", view_range=view_range, count=len(lines))
                return ToolOutcome(outside, error=True)
        numbered = _number(lines[start - 1 : end], start).encode()
        out, _ = cap_stream(numbered, numbered, len(numbered), marker=ctx.omitted)
        key = "view_cut" if f.truncated else "view"
        return ToolOutcome(self.say(key, path=path, start=start, end=end, text=out))

    async def _create(self, ctx: ToolContext, path: str, text: str) -> ToolOutcome:
        data = text.encode()
        p = shlex.quote(path)
        d = shlex.quote(posixpath.dirname(path))
        command = (
            f"B={BUSYBOX}; if [ -e {p} ] || [ -L {p} ]; then echo exists; exit 0; fi;"
            f" $B mkdir -p {d} && $B head -c {len(data)} > {p} && echo created"
        )
        result = await self._exec(ctx, command, stdin=data)
        f = result.finished
        if f.end == "failed":
            error = self.say("uncreatable", path=path, error=f.error)
            return ToolOutcome(error, error=True, failed=True)
        said = result.stdout.strip()
        if said == b"exists":
            return ToolOutcome(self.say("exists", path=path), error=True)
        if f.exit_code != 0 or said != b"created":
            return ToolOutcome(self.say("uncreatable", path=path, error=self._err(result)), True)
        return ToolOutcome(self.say("created", path=path))

    async def _replace(self, ctx: ToolContext, path: str, old: str, new: str) -> ToolOutcome:
        text = await self._text(ctx, path)
        if isinstance(text, ToolOutcome):
            return text
        if not old:
            return ToolOutcome(self.say("old_str_empty"), error=True)
        count = text.count(old)
        if count == 0:
            return ToolOutcome(self.say("no_match", path=path), error=True)
        if count > 1:
            lines, at = [], 0
            while (at := text.find(old, at)) != -1:
                lines.append(text.count("\n", 0, at) + 1)
                at += 1
            many = self.say("many_matches", path=path, count=count, lines=lines)
            return ToolOutcome(many, error=True)
        at = text.index(old)
        updated = text.replace(old, new, 1)
        if (failed := await self._write(ctx, path, updated)) is not None:
            return failed
        first = text.count("\n", 0, at) + 1
        return ToolOutcome(self._edited(path, updated, first, first + new.count("\n")))

    async def _insert(self, ctx: ToolContext, path: str, line: int, new: str) -> ToolOutcome:
        text = await self._text(ctx, path)
        if isinstance(text, ToolOutcome):
            return text
        lines = text.split("\n")
        trailing = lines[-1] == ""
        if trailing:
            lines.pop()
        if line > len(lines):
            return ToolOutcome(self.say("past_end", line=line, count=len(lines)), error=True)
        added = new.split("\n")
        if added and added[-1] == "" and new:
            added.pop()
        updated = "\n".join([*lines[:line], *added, *lines[line:]]) + ("\n" if trailing else "")
        if (failed := await self._write(ctx, path, updated)) is not None:
            return failed
        return ToolOutcome(self._edited(path, updated, line + 1, line + len(added)))

    def _err(self, result: ExecResult) -> str:
        text = result.stderr.decode(errors="replace").strip()
        return text or self.say("exit_code", code=result.finished.exit_code)

    def _edited(self, path: str, text: str, first: int, last: int) -> str:
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lo = max(1, first - SNIPPET_LINES)
        hi = min(len(lines), last + SNIPPET_LINES)
        body = _number(lines[lo - 1 : hi], lo)
        return self.say("edited", path=path, start=lo, end=hi, text=body)


def _number(lines: list[str], start: int) -> str:
    return "".join(f"{n:6}\t{line}\n" for n, line in enumerate(lines, start))


class SubmitArgs(Args):
    answer: str


class Submit(TextTool):
    name: ClassVar[str] = "submit"
    args: ClassVar[type[Args]] = SubmitArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "Submit your final answer and end the task. Call it once, when you are done."
        ),
        "reply": "Submitted.",
        "param_answer": "Your final answer.",
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.say("description"),
            parameters=_object(
                {"answer": {"type": "string", "description": self.say("param_answer")}},
                ["answer"],
            ),
        )

    async def run(self, ctx: ToolContext, args: SubmitArgs) -> ToolOutcome:
        return ToolOutcome(self.say("reply"), answer=args.answer)


class ReportArgs(Args):
    message: str = Field(min_length=1)


class Report(TextTool):
    name: ClassVar[str] = "report_to_operator"
    args: ClassVar[type[Args]] = ReportArgs
    texts: ClassVar[dict[str, str]] = {
        "description": (
            "Send a message to the operator responsible for this environment. It does not end"
            " your task."
        ),
        "reply": "The operator received your message.",
        "param_message": "What to tell the operator.",
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.say("description"),
            parameters=_object(
                {"message": {"type": "string", "description": self.say("param_message")}},
                ["message"],
            ),
        )

    async def run(self, ctx: ToolContext, args: ReportArgs) -> ToolOutcome:
        message = await ctx.store.content(ctx.run_id, args.message)
        await ctx.emit(AgentReport(call_id=ctx.call_id, step=ctx.step, message=message))
        return ToolOutcome(self.say("reply"))
