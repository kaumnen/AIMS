"""What `aims run` and `aims monitor` show: the live view, the end summary, JSON lines and the
prompt a person answers when the monitor pauses an agent under human oversight."""

import asyncio
import contextlib
import json
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aims.events import BlobRef, Event, EventStore, ResumeThen
from aims.models import Delta, DeltaBus
from aims.monitor.core import HumanRequest
from aims.run.estimate import Progress, project_run, usd
from aims.run.runner import Result, RunError, Runner

TAIL = 240
ARGS = 120
SHOWN_FLAGS = 5
SHOWN_NOTES = 3
MONITOR_TYPES = ("monitor.flag", "monitor.action", "monitor.sweep", "monitor.verdict")
LINE_TYPES = {
    "monitor.flag": "flag",
    "monitor.action": "action",
    "monitor.sweep": "sweep",
    "monitor.verdict": "verdict",
    "monitor.degraded": "degraded",
    "monitor.cleared": "cleared",
}


def _short(value: object, n: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass(slots=True)
class _Agent:
    step: int = 0
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning: str = ""
    text: str = ""
    call: str = ""
    result: str = ""
    result_error: bool = False
    reports: int = 0
    last_report: str = ""
    ended: str = ""
    reward: str = ""
    divergences: int = 0
    paused: str = ""
    steps_cap: int | None = None
    cost_cap: float | None = None


@dataclass(slots=True)
class _Run:
    run_id: str
    scenario: str = ""
    status: str = "running"
    phase: str = "starting"
    agents: dict[str, _Agent] = field(default_factory=dict)
    flags: int = 0
    max_importance: int = 0
    shown: list[str] = field(default_factory=list)
    sweeps: int = 0
    sweeping: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    verdict: str = ""
    cost_cap: float | None = None
    monitor_cap: float | None = None
    monitor_calls: set[str] = field(default_factory=set)
    monitor_cost: float = 0.0


def flag_line(data: dict[str, Any], agent: str | None) -> str:
    """One flag as category, importance, layer and source, with its justification."""
    where = f"{data.get('layer')}/{data.get('source')}"
    count = data.get("count") or 1
    seen = f" x{count}" if count > 1 else ""
    who = f" {agent}" if agent else ""
    why = _short(data.get("justification", ""), ARGS)
    return f"{data.get('category')} {data.get('importance')} {where}{who}{seen}: {why}"


def action_line(e: Event) -> str:
    """A pause, resume, deny or kill and its cause: the monitor's, simulated or not, or an
    operator's."""
    data = e.data
    who = ", ".join(_agents(data.get("agents"), e.agent_id)) or "all agents"
    if e.type == "operator.action":
        action = str(data.get("action"))
        then = data.get("then")
        if action == "resume" and then not in (None, "continue"):
            action = f"resume then {then}"
        cause = f"operator {data.get('token') or ''}".strip()
    else:
        action = str(data.get("action"))
        cause = "monitor (simulated)" if data.get("simulated") else "monitor"
    reason = data.get("reason")
    return f"{action} {who} by {cause}" + (f": {_short(reason, ARGS)}" if reason else "")


class LiveView:
    """What each agent of each run is doing, from the run logs and the delta bus, and what the
    run is on track to cost. `typical_steps` are the steps each agent took in past runs of the
    scenario."""

    def __init__(
        self, store: EventStore, bus: DeltaBus, typical_steps: Mapping[str, float] | None = None
    ) -> None:
        self.store = store
        self.bus = bus
        self.typical_steps = dict(typical_steps or {})
        self.runs: dict[str, _Run] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._deltas()))

    def add(self, run_id: str) -> None:
        self.runs[run_id] = _Run(run_id)
        self._tasks.append(asyncio.create_task(self._watch(run_id)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _watch(self, run_id: str) -> None:
        async for event in self.store.stream(run_id):
            self._on_event(self.runs[run_id], event)

    async def _deltas(self) -> None:
        with self.bus.subscribe() as sub:
            async for delta in sub:
                self._on_delta(delta)

    def _agent(self, run: _Run, agent_id: str | None) -> _Agent:
        return run.agents.setdefault(agent_id or "-", _Agent())

    def _on_delta(self, d: Delta) -> None:
        run = self.runs.get(d.run_id)
        if run is None or d.agent_id is None:
            return
        a = self._agent(run, d.agent_id)
        if d.kind == "start":
            a.text = a.reasoning = ""
        elif d.kind == "text":
            a.text = (a.text + d.text)[-TAIL:]
        elif d.kind == "reasoning":
            a.reasoning = (a.reasoning + d.text)[-TAIL:]

    def _on_event(self, run: _Run, e: Event) -> None:
        data = e.data
        match e.type:
            case "model.request" if data.get("role") is not None:
                run.monitor_calls.add(str(data.get("call_id")))
                if data.get("role") == "sweep":
                    run.sweeping.add(str(data.get("call_id")))
            case "model.response" if data.get("call_id") in run.monitor_calls:
                run.sweeping.discard(str(data.get("call_id")))
                run.monitor_cost += _num(data.get("cost_usd"))
            case "monitor.flag":
                importance = int(_num(data.get("importance")))
                run.max_importance = max(run.max_importance, importance)
                if data.get("first_flag") is None:
                    run.flags += 1
                run.shown = [*run.shown, flag_line(data, e.agent_id)][-SHOWN_FLAGS:]
            case "monitor.sweep":
                run.sweeps += 1
            case "monitor.verdict":
                run.verdict = f"severity {data.get('max_severity')}: {_text(data.get('summary'))}"
            case "monitor.degraded":
                run.notes = [*run.notes, f"monitor {data.get('part')}: {data.get('reason')}"]
            case "monitor.action" | "operator.action":
                self._acted(run, e)
            case "run.started":
                run.scenario = str(data.get("scenario", ""))
                config = data.get("config")
                run.cost_cap = _cap(_at(config, "scenario", "resolved", "limits", "cost_usd"))
                run.monitor_cap = _cap(_at(config, "monitor", "budget_usd"))
            case "sandbox.started":
                run.phase = "sandbox"
            case "exec.started" if data.get("phase") in ("setup", "verifier"):
                run.phase = str(data["phase"])
            case "agent.started":
                run.phase = "agents"
                a = self._agent(run, e.agent_id)
                steps = _cap(_at(data, "budget", "steps"))
                a.steps_cap = None if steps is None else int(steps)
                a.cost_cap = _cap(_at(data, "budget", "cost_usd"))
            case "agent.step":
                a = self._agent(run, e.agent_id)
                a.step = int(_num(data.get("n")))
                a.cost = _num(data.get("total_cost_usd"))
                total = data.get("total")
                if isinstance(total, dict):
                    a.tokens_in = int(_num(total.get("input_tokens")))
                    a.tokens_out = int(_num(total.get("output_tokens")))
            case "tool.call":
                a = self._agent(run, e.agent_id)
                args = data.get("arguments")
                a.call = f"{data.get('name')} {_args(args)}"
                a.result = ""
            case "tool.result":
                a = self._agent(run, e.agent_id)
                out = data.get("output")
                a.result = "(large output)" if isinstance(out, dict) else _short(out, ARGS)
                a.result_error = bool(data.get("error"))
            case "agent.report":
                a = self._agent(run, e.agent_id)
                a.reports += 1
                message = data.get("message")
                long = isinstance(message, dict)
                a.last_report = "(long message)" if long else _short(message, ARGS)
            case "replay.divergence":
                self._agent(run, e.agent_id).divergences += 1
            case "agent.finished":
                a = self._agent(run, e.agent_id)
                a.ended = str(data.get("reason"))
                a.cost = _num(data.get("cost_usd"))
            case "task.scored":
                a = self._agent(run, e.agent_id)
                reward = data.get("reward")
                a.reward = "none" if reward is None else f"{_num(reward):g}"
            case "sandbox.archived":
                run.phase = "archive"
            case "run.finished":
                run.status = str(data.get("status"))
                run.phase = "done"

    def _acted(self, run: _Run, e: Event) -> None:
        data = e.data
        action = data.get("action")
        if action not in ("pause", "resume", "deny", "kill"):
            return
        run.notes = [*run.notes, action_line(e)][-SHOWN_NOTES:]
        if data.get("simulated"):
            return
        agents = _agents(data.get("agents"), e.agent_id) or list(run.agents)
        for agent_id in agents:
            a = self._agent(run, agent_id)
            if action == "pause":
                why = data.get("reason") or "no reason given"
                by = "operator" if e.type == "operator.action" else "monitor"
                a.paused = f"paused by the {by}: {_short(why, ARGS)}"
            elif action in ("resume", "deny"):
                a.paused = ""

    def __rich__(self) -> RenderableType:
        panels = []
        for run in self.runs.values():
            rows: list[RenderableType] = []
            for agent_id, a in run.agents.items():
                head = Text.assemble(
                    (agent_id, "bold"),
                    f"  step {a.step}  ${a.cost:.4f}  in {a.tokens_in:,} out {a.tokens_out:,}",
                )
                if a.reports:
                    head.append(f"  reports {a.reports}", style="yellow")
                if a.divergences:
                    head.append(f"  diverged {a.divergences}", style="magenta")
                if a.ended:
                    head.append(f"  {a.ended}", style="green" if a.ended == "submitted" else "red")
                if a.reward:
                    head.append(f"  reward {a.reward}", style="cyan")
                rows.append(head)
                if a.paused and not a.ended:
                    rows.append(Text(f"  {a.paused}", style="bold yellow"))
                if a.reasoning and not a.ended:
                    rows.append(Text(f"  {_short(a.reasoning, TAIL)}", style="dim"))
                if a.text and not a.ended:
                    rows.append(Text(f"  {_short(a.text, TAIL)}"))
                if a.call:
                    rows.append(Text(f"  > {_short(a.call, ARGS)}", style="blue"))
                if a.result:
                    rows.append(Text(f"  < {a.result}", style="red" if a.result_error else ""))
                if a.last_report:
                    rows.append(Text(f"  report: {a.last_report}", style="yellow"))
            cost = self._cost_row(run)
            if cost is not None:
                rows.append(cost)
            rows += self._monitor_rows(run)
            title = Text(f"{run.scenario} {run.run_id}  {run.phase}")
            panels.append(Panel(Group(*rows) if rows else Text("starting"), title=title))
        return Group(*panels)

    def _cost_row(self, run: _Run) -> Text | None:
        """What the run has cost so far and is on track to cost, once an agent is a few steps
        in, bounded by its caps."""
        agents = [
            Progress(
                cost=a.cost,
                steps=a.step,
                steps_cap=a.steps_cap,
                cost_cap=a.cost_cap,
                typical_steps=self.typical_steps.get(agent_id),
                done=bool(a.ended),
            )
            for agent_id, a in run.agents.items()
        ]
        found = project_run(
            agents,
            cost_cap=run.cost_cap,
            monitor_cost=run.monitor_cost,
            monitor_cap=run.monitor_cap,
        )
        if found is None:
            return None
        ahead, monitor = found
        spent = sum(a.cost for a in agents) + run.monitor_cost
        line = f"cost {usd(spent)} so far, on track for about {usd(ahead + monitor)}"
        if run.monitor_cost:
            line += f" (agents {usd(ahead)}, monitor {usd(monitor)})"
        return Text(line, style="dim")

    def _monitor_rows(self, run: _Run) -> list[RenderableType]:
        if not (run.flags or run.sweeps or run.sweeping or run.notes or run.verdict):
            return []
        head = Text("monitor", style="bold magenta")
        head.append(f"  flags {run.flags}")
        if run.max_importance:
            head.append(f"  max {run.max_importance}", style=_severity(run.max_importance))
        head.append(f"  sweeps {run.sweeps}")
        if run.sweeping:
            head.append("  sweeping", style="magenta")
        rows: list[RenderableType] = [head]
        rows += [Text(f"  ! {line}", style="magenta") for line in run.shown]
        rows += [Text(f"  {note}", style="yellow") for note in run.notes]
        if run.verdict:
            rows.append(Text(f"  verdict {_short(run.verdict, TAIL)}", style="cyan"))
        return rows


def _agents(value: object, agent_id: str | None) -> list[str]:
    if isinstance(value, list) and value:
        return [str(a) for a in value]
    return [agent_id] if agent_id else []


def _severity(importance: int) -> str:
    return "red" if importance >= 4 else "yellow" if importance >= 3 else ""


def _text(value: object) -> str:
    return "(long text)" if isinstance(value, dict) else _short(value or "", TAIL)


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _cap(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _at(value: object, *keys: str) -> object:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _args(args: object) -> str:
    if isinstance(args, dict) and set(args) == {"sha256", "size"}:
        return "(large arguments)"
    try:
        value = json.loads(str(args))
    except ValueError:
        return _short(args, ARGS)
    if isinstance(value, dict):
        if "command" in value and isinstance(value["command"], str) and len(value) <= 2:
            return _short(value["command"], ARGS)
        return _short(", ".join(f"{k}={v}" for k, v in value.items()), ARGS)
    return _short(value, ARGS)


def runs_table(results: list[Result]) -> Table:
    table = Table(title="runs", show_lines=False)
    table.add_column("run", overflow="fold")
    for column in ("status", "agent", "end", "steps", "cost", "reward", "reason"):
        table.add_column(column, overflow="fold")
    for r in results:
        if not r.agents:
            table.add_row(r.run_id or "-", r.status, "", "", "", "", "", r.reason or "")
        for agent_id, f in r.agents.items():
            score = r.scores.get(agent_id)
            reward = ""
            if score is not None:
                reward = "none" if score.reward is None else f"{score.reward:g}"
            table.add_row(
                r.run_id or "-",
                r.status,
                agent_id,
                f.reason,
                str(f.steps),
                f"${f.cost_usd:.4f}" + ("" if f.cost_complete else "+"),
                reward,
                r.reason or (f.detail or ""),
            )
    return table


def result_json(r: Result) -> dict[str, object]:
    def content(value: object) -> object:
        return value.model_dump() if isinstance(value, BlobRef) else value

    return {
        "run_id": r.run_id,
        "status": r.status,
        "reason": r.reason,
        "cost_usd": round(r.cost_usd, 10),
        "agents": {
            a: {
                "end": f.reason,
                "detail": f.detail,
                "steps": f.steps,
                "cost_usd": f.cost_usd,
                "cost_complete": f.cost_complete,
                "answer": content(f.answer),
                "usage": f.usage.model_dump(),
                "wall_ms": f.wall_ms,
            }
            for a, f in r.agents.items()
        },
        "scores": {
            a: s.model_dump(mode="json", exclude={"metrics"}) | {"metrics": s.metrics}
            for a, s in r.scores.items()
        },
    }


@dataclass(slots=True)
class Digest:
    """What the monitor wrote in one run, for the end summary: each logical flag with the
    count its re-flags reached, the actions, the sweep count and the verdict."""

    run_id: str
    flags: list[Event] = field(default_factory=list)
    counts: dict[int, int] = field(default_factory=dict)
    actions: list[Event] = field(default_factory=list)
    sweeps: int = 0
    verdict: Event | None = None
    degraded: list[Event] = field(default_factory=list)

    def json(self) -> dict[str, object]:
        def flag(e: Event) -> dict[str, object]:
            return {"seq": e.seq, "agent_id": e.agent_id, **e.data, "count": self.counts[e.seq]}

        return {
            "flags": [flag(e) for e in self.flags],
            "actions": [{"seq": e.seq, "agent_id": e.agent_id, **e.data} for e in self.actions],
            "sweeps": self.sweeps,
            "verdict": None if self.verdict is None else self.verdict.data,
            "degraded": [e.data for e in self.degraded],
        }


async def digest(store: EventStore, run_id: str) -> Digest:
    """The monitor's findings in `run_id`, read back from its log."""
    out = Digest(run_id)
    types = [*MONITOR_TYPES, "monitor.degraded", "operator.action"]
    cursor: int | None = None
    while page := await store.events(run_id, after=cursor, limit=1000, types=types):
        cursor = page[-1].seq
        for e in page:
            data = e.data
            match e.type:
                case "monitor.flag":
                    first = data.get("first_flag")
                    count = int(_num(data.get("count")) or 1)
                    if not isinstance(first, int) or first not in out.counts:
                        out.flags.append(e)
                        out.counts[e.seq] = count
                    else:
                        out.counts[first] = max(out.counts[first], count)
                case "monitor.action":
                    out.actions.append(e)
                case "operator.action" if data.get("action") in ("pause", "resume", "kill"):
                    out.actions.append(e)
                case "monitor.sweep":
                    out.sweeps += 1
                case "monitor.verdict":
                    out.verdict = e
                case "monitor.degraded":
                    out.degraded.append(e)
    return out


def monitor_table(digests: Sequence[Digest]) -> Table | None:
    """Every run's flags, actions and verdict, or None when no monitor wrote any."""
    table = Table(title="monitor", show_lines=False)
    for column in ("run", "what", "agent", "detail"):
        table.add_column(column, overflow="fold")
    rows = 0
    for d in digests:
        for e in d.flags:
            data = {**e.data, "count": d.counts[e.seq]}
            style = _severity(int(_num(data.get("importance"))))
            table.add_row(d.run_id, "flag", e.agent_id or "", flag_line(data, None), style=style)
            rows += 1
        for e in d.actions:
            table.add_row(d.run_id, "action", "", action_line(e))
            rows += 1
        for e in d.degraded:
            table.add_row(d.run_id, "degraded", "", f"{e.data.get('part')}: {e.data.get('reason')}")
            rows += 1
        if d.verdict is not None:
            v = d.verdict.data
            detail = f"severity {v.get('max_severity')}: {_text(v.get('summary'))}"
            table.add_row(d.run_id, "verdict", "", detail)
            rows += 1
    return table if rows else None


def summary(results: list[Result], digests: Sequence[Digest] = ()) -> RenderableType:
    """The runs, then what their monitors found."""
    found = monitor_table(digests)
    return runs_table(results) if found is None else Group(runs_table(results), found)


class JsonLines:
    """The monitor's events of each run added, one JSON line each as they land: flags,
    actions, sweeps, verdicts, degradations and clears."""

    def __init__(self, store: EventStore, emit: Callable[[str], None]) -> None:
        self.store = store
        self.emit = emit
        self._tasks: list[asyncio.Task[None]] = []

    def add(self, run_id: str) -> None:
        self._tasks.append(asyncio.create_task(self._watch(run_id)))

    async def _watch(self, run_id: str) -> None:
        async for e in self.store.stream(run_id):
            kind = LINE_TYPES.get(e.type)
            if kind is not None:
                self.emit(json.dumps(event_line(kind, run_id, e), ensure_ascii=False))

    async def stop(self, within: float = 5.0) -> None:
        """Let each watch reach its run's seal, for `within` seconds, then stop it."""
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=within)
            for task in pending:
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def event_line(kind: str, run_id: str, e: Event) -> dict[str, object]:
    return {
        "type": kind,
        "run_id": run_id,
        "seq": e.seq,
        "agent_id": e.agent_id,
        "ts": e.ts.isoformat(),
        "data": e.data,
    }


class HumanPrompt:
    """Asks the person at the terminal what to do with agents the monitor paused under human
    oversight: resume, deny the tool call they are held before, inject a message, or kill the
    run. The answer goes through `Runner.operate`, as an operator's would. Prompts come one at
    a time; one whose pause ended elsewhere, in the UI or by its timeout, closes."""

    def __init__(self, console: Console, read: Callable[[], str] | None = None) -> None:
        self.console = console
        self.read = read or sys.stdin.readline
        self.live: Live | None = None
        self._lock = asyncio.Lock()
        self._reading: asyncio.Future[str] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def hook(self, runner: Runner) -> Callable[[HumanRequest], Any]:
        """The `human` callback of `runner`. It returns at once: the monitor's pause task must
        not wait on a person, since the answer ends that task."""

        async def ask(request: HumanRequest) -> None:
            task = asyncio.create_task(self._prompt(runner, request))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return ask

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _waiting(self, runner: Runner, request: HumanRequest) -> list[str]:
        if runner.finished or runner.killed:
            return []
        return [a for a in request.agents if runner.paused(a)]

    async def _prompt(self, runner: Runner, request: HumanRequest) -> None:
        async with self._lock:
            if not self._waiting(runner, request):
                return
            live = self.live
            if live is not None:
                live.stop()
            try:
                await self._ask(runner, request)
            finally:
                if live is not None and self.live is live:
                    live.start()

    async def _ask(self, runner: Runner, request: HumanRequest) -> None:
        c = self.console
        agents = ", ".join(request.agents)
        c.print(f"[bold yellow]the monitor paused {escape(agents)} in run {request.run_id}[/]")
        for e in request.flags:
            c.print(f"  [#{e.seq}] {escape(flag_line(e.data, e.agent_id))}")
        if request.advice:
            c.print(f"  advice: {escape(request.advice)}")
        for agent_id, call_id in request.held_call.items():
            c.print(f"  {escape(agent_id)} waits before tool call {escape(call_id)}")
        if request.timeout_s is not None:
            c.print(f"  with no answer in {request.timeout_s:g} s the run is killed")
        choices = ["resume", *(["deny"] if request.held_call else []), "inject", "kill"]
        while self._waiting(runner, request):
            c.print(f"{', '.join(choices)}? [resume] ", end="")
            line = await self._line(runner, request)
            if line is None:
                c.print("\nthe pause ended without this answer")
                return
            answer = line.strip().lower() or "resume"
            choice = next((x for x in choices if x.startswith(answer)), None)
            if choice is None:
                c.print(f"answer {', '.join(choices)}")
                continue
            text = None
            if choice == "inject":
                c.print("message for the agent: ", end="")
                text = await self._line(runner, request)
                if text is None:
                    return
                if not text.strip():
                    c.print("an inject needs text")
                    continue
            try:
                await self._answer(runner, request, choice, text)
            except RunError as e:
                c.print(f"[red]error:[/] {escape(str(e))}")
                continue
            return

    async def _answer(
        self, runner: Runner, request: HumanRequest, choice: str, text: str | None
    ) -> None:
        if choice == "kill":
            await runner.operate("kill", reason="killed by the person at the terminal")
            return
        for agent_id in self._waiting(runner, request):
            then: ResumeThen = "continue"
            if choice == "inject":
                then = "inject"
            elif choice == "deny" and agent_id in request.held_call:
                then = "deny"
            reason = "answered at the terminal"
            await runner.operate(
                "resume",
                agent=agent_id,
                then=then,
                text=(text or "").strip() or None,
                reason=reason,
            )

    async def _line(self, runner: Runner, request: HumanRequest) -> str | None:
        """The next line typed, or None once the pause ended or stdin closed. A line asked for
        but not read yet is kept for the next prompt."""
        if self._reading is None:
            self._reading = self._start_read()
        reading = self._reading
        while not reading.done():
            if not self._waiting(runner, request):
                return None
            await asyncio.wait([reading], timeout=0.25)
        self._reading = None
        line = reading.result()
        return line or None

    def _start_read(self) -> asyncio.Future[str]:
        """Read a line in a daemon thread, so a prompt nobody answers never holds the exit."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def done(line: str) -> None:
            if not future.done():
                future.set_result(line)

        def run() -> None:
            try:
                line = self.read()
            except Exception:
                line = ""
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(done, line)

        threading.Thread(target=run, name="aims-prompt", daemon=True).start()
        return future
