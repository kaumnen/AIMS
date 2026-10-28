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


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _stopped(e: Exception) -> ToolOutcome:
    return ToolOutcome(f"The command could not run: {e}", error=True, failed=True)


class BashArgs(Args):
    command: str = Field(min_length=1)
    timeout_s: float | None = Field(default=None, gt=0)


class Bash(TextTool):
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
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {
        "description": frozenset({"workdir", "timeout_s", "max_timeout_s"}),
        "timeout": frozenset({"timeout_s"}),
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        default = f"{config.command_timeout_s:g}"
        most = f"{config.max_command_timeout_s:g}"
        return ToolSpec(
            name=self.name,
            description=self.text["description"].format(
                workdir=config.workdir, timeout_s=default, max_timeout_s=most
            ),
            parameters=_object(
                {
                    "command": {"type": "string", "description": "The command to run."},
                    "timeout_s": {
                        "type": "number",
                        "description": f"Seconds before the command is stopped, at most {most}.",
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
            return _stopped(e)
        f = result.finished
        if f.end == "failed":
            return ToolOutcome(f"The command could not run: {f.error}", error=True, failed=True)
        parts = []
        for label, head, tail, total in (
            ("stdout", result.stdout, result.stdout_tail, f.stdout_bytes),
            ("stderr", result.stderr, result.stderr_tail, f.stderr_bytes),
        ):
            if total:
                text, cut = cap_stream(head, tail, total, STREAM_LIMIT)
                size = f" ({total} bytes, middle cut)" if cut else ""
                parts.append(f"{label}{size}:\n{text}{'' if text.endswith(chr(10)) else chr(10)}")
        if f.end == "timeout":
            parts.append(self.text["timeout"].format(timeout_s=f"{timeout:g}") + "\n")
        code = "none" if f.exit_code is None else str(f.exit_code)
        parts.append(f"exit code: {code}")
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
    an exec in the sandbox as the agent, so the audit sees them like any other command."""

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
    }
    placeholders: ClassVar[dict[str, frozenset[str]]] = {"description": frozenset({"workdir"})}

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.text["description"].format(workdir=config.workdir),
            parameters=_object(
                {
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert"],
                        "description": "What to do.",
                    },
                    "path": {"type": "string", "description": "The file or directory."},
                    "file_text": {"type": "string", "description": "For create: the content."},
                    "old_str": {"type": "string", "description": "For str_replace: the text."},
                    "new_str": {
                        "type": "string",
                        "description": "For str_replace and insert: the new text.",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "For insert: the line to insert after.",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "For view: the first and last line to show.",
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
                        return ToolOutcome("create needs file_text.", error=True)
                    return await self._create(ctx, path, args.file_text)
                case "str_replace":
                    if args.old_str is None:
                        return ToolOutcome("str_replace needs old_str.", error=True)
                    return await self._replace(ctx, path, args.old_str, args.new_str or "")
                case "insert":
                    if args.insert_line is None or args.new_str is None:
                        return ToolOutcome("insert needs insert_line and new_str.", error=True)
                    return await self._insert(ctx, path, args.insert_line, args.new_str)
        except SandboxStoppedError as e:
            return _stopped(e)

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
            return ToolOutcome(f"Could not read {path}: {f.error}", error=True, failed=True)
        if kind != "F":
            what = "is a directory" if kind == "D" else "does not exist"
            return ToolOutcome(f"{path} {what}.", error=True)
        if f.exit_code != 0:
            return ToolOutcome(f"Could not read {path}: {_err(result)}", error=True)
        if result.finished.truncated:
            size = f.stdout_bytes - 2
            return ToolOutcome(f"{path} is too large to edit ({size} bytes).", error=True)
        try:
            return body.decode()
        except UnicodeDecodeError:
            return ToolOutcome(f"{path} is not UTF-8 text.", error=True)

    async def _write(self, ctx: ToolContext, path: str, text: str) -> ToolOutcome | None:
        data = text.encode()
        p = shlex.quote(path)
        result = await self._exec(ctx, f"{BUSYBOX} head -c {len(data)} > {p}", stdin=data)
        if result.finished.end == "failed":
            error = result.finished.error
            return ToolOutcome(f"Could not write {path}: {error}", error=True, failed=True)
        if result.finished.exit_code != 0:
            return ToolOutcome(f"Could not write {path}: {_err(result)}", error=True)
        return None

    async def _view(self, ctx: ToolContext, path: str, view_range: list[int] | None) -> ToolOutcome:
        kind, body, result = await self._read(ctx, path)
        f = result.finished
        if f.end == "failed":
            return ToolOutcome(f"Could not read {path}: {f.error}", error=True, failed=True)
        if kind == "N":
            return ToolOutcome(f"{path} does not exist.", error=True)
        if f.exit_code != 0:
            return ToolOutcome(f"Could not read {path}: {_err(result)}", error=True)
        if kind == "D":
            text, _ = cap_stream(body, result.stdout_tail, max(0, f.stdout_bytes - 2))
            return ToolOutcome(
                f"Files and directories up to 2 levels deep in {path}, hidden ones left out:\n"
                f"{text}"
            )
        text = body.decode(errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        start, end = 1, len(lines)
        if view_range is not None:
            start, end = view_range
            end = len(lines) if end == -1 else end
            if not 1 <= start <= max(1, len(lines)) or end < start or end > len(lines):
                return ToolOutcome(
                    f"view_range {view_range} is outside the file's {len(lines)} lines.",
                    error=True,
                )
        numbered = _number(lines[start - 1 : end], start)
        out, _ = cap_stream(numbered.encode(), numbered.encode(), len(numbered.encode()))
        note = " (cut: the file is larger than can be read)" if f.truncated else ""
        return ToolOutcome(f"{path}{note}, lines {start} to {end}:\n{out}")

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
            return ToolOutcome(f"Could not create {path}: {f.error}", error=True, failed=True)
        said = result.stdout.strip()
        if said == b"exists":
            return ToolOutcome(f"{path} already exists; create does not overwrite.", error=True)
        if f.exit_code != 0 or said != b"created":
            return ToolOutcome(f"Could not create {path}: {_err(result)}", error=True)
        return ToolOutcome(f"Created {path}.")

    async def _replace(self, ctx: ToolContext, path: str, old: str, new: str) -> ToolOutcome:
        text = await self._text(ctx, path)
        if isinstance(text, ToolOutcome):
            return text
        if not old:
            return ToolOutcome("old_str is empty.", error=True)
        count = text.count(old)
        if count == 0:
            return ToolOutcome(
                f"No change: old_str does not appear verbatim in {path}.", error=True
            )
        if count > 1:
            lines, at = [], 0
            while (at := text.find(old, at)) != -1:
                lines.append(text.count("\n", 0, at) + 1)
                at += 1
            return ToolOutcome(
                f"No change: old_str appears {count} times in {path}, at lines {lines}."
                " Include more context to make it unique.",
                error=True,
            )
        at = text.index(old)
        updated = text.replace(old, new, 1)
        if (failed := await self._write(ctx, path, updated)) is not None:
            return failed
        first = text.count("\n", 0, at) + 1
        return ToolOutcome(_snippet(path, updated, first, first + new.count("\n")))

    async def _insert(self, ctx: ToolContext, path: str, line: int, new: str) -> ToolOutcome:
        text = await self._text(ctx, path)
        if isinstance(text, ToolOutcome):
            return text
        lines = text.split("\n")
        trailing = lines[-1] == ""
        if trailing:
            lines.pop()
        if line > len(lines):
            return ToolOutcome(
                f"insert_line {line} is past the file's {len(lines)} lines.", error=True
            )
        added = new.split("\n")
        if added and added[-1] == "" and new:
            added.pop()
        updated = "\n".join([*lines[:line], *added, *lines[line:]]) + ("\n" if trailing else "")
        if (failed := await self._write(ctx, path, updated)) is not None:
            return failed
        return ToolOutcome(_snippet(path, updated, line + 1, line + len(added)))


def _err(result: ExecResult) -> str:
    text = result.stderr.decode(errors="replace").strip()
    return text or f"exit code {result.finished.exit_code}"


def _number(lines: list[str], start: int) -> str:
    return "".join(f"{n:6}\t{line}\n" for n, line in enumerate(lines, start))


def _snippet(path: str, text: str, first: int, last: int) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lo = max(1, first - SNIPPET_LINES)
    hi = min(len(lines), last + SNIPPET_LINES)
    body = _number(lines[lo - 1 : hi], lo)
    return f"Edited {path}. Lines {lo} to {hi} now read:\n{body}"


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
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.text["description"],
            parameters=_object(
                {"answer": {"type": "string", "description": "Your final answer."}}, ["answer"]
            ),
        )

    async def run(self, ctx: ToolContext, args: SubmitArgs) -> ToolOutcome:
        return ToolOutcome(self.text["reply"], answer=args.answer)


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
    }

    def spec(self, config: SandboxConfig) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.text["description"],
            parameters=_object(
                {"message": {"type": "string", "description": "What to tell the operator."}},
                ["message"],
            ),
        )

    async def run(self, ctx: ToolContext, args: ReportArgs) -> ToolOutcome:
        message = await ctx.store.content(ctx.run_id, args.message)
        await ctx.emit(AgentReport(call_id=ctx.call_id, step=ctx.step, message=message))
        return ToolOutcome(self.text["reply"])
