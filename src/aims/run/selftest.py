"""The plant self-test `aims scenario check` runs: the scenario's reference and exploit scripts,
and the references of its impossible plants, each in a run of scripted agents at no model cost,
whose sealed log the plant oracle then reads.

The reference must take no plant, though it may touch one honest work reads, such as a failing
test, and the exploit must take each plant it claims. An impossible plant's verifier must fail
both its reference, laid over the workdir, and a workdir no agent touched; in those runs no
plant may trigger at all.
"""

import inspect
import posixpath
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aims.events import Event, LocalEventStore, RunStatus, TaskScored
from aims.run import truth
from aims.run.runner import Bench, Options, Result, RunError, Runner
from aims.run.score import Taken, check_run
from aims.scenario import Bundle, Scenario
from aims.scenario.load import _Tar
from aims.scenario.model import ImpossiblePlant, ScriptStep, ToolPlant

MAX_SCRIPT_BYTES = 100_000
OK = "exit code 0"
CaseKind = Literal["reference", "exploit", "empty", "impossible"]


class SelfTestError(ValueError):
    """The self-test cannot run as written: a script is missing or not text, or a claim no
    script can prove."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Case:
    """One run of the self-test. Each subject follows its steps in `scripts`, none for an idle
    one; `agents` are the subjects whose claims and rewards it checks; `overlays` are laid over
    sandboxes once their setup ran."""

    kind: CaseKind
    name: str
    agents: tuple[str, ...]
    scripts: dict[str, list[ScriptStep]]
    overlays: dict[str, bytes] = field(default_factory=dict)


@dataclass(slots=True)
class Outcome:
    case: str
    run_id: str | None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kind: CaseKind = "reference"
    status: RunStatus | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "run_id": self.run_id,
            "ok": self.ok,
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


def wanted(scenario: Scenario) -> bool:
    """Whether the scenario has a self-test to run."""
    return scenario.selftest is not None or any(
        isinstance(p, ImpossiblePlant) for p in scenario.plants
    )


def cases(bundle: Bundle) -> list[Case]:
    """The runs the self-test of `bundle` makes, in order."""
    s = bundle.scenario
    subjects = tuple(s.subjects)
    problems: list[str] = []
    out: list[Case] = []
    test = s.selftest
    if test is not None:
        for kind, script in (("reference", test.reference), ("exploit", test.exploit)):
            if script is None:
                continue
            try:
                steps = [_script(bundle, script)] if isinstance(script, str) else script
            except SelfTestError as e:
                problems += e.problems
                continue
            mine = {a: [step for step in steps if s.can_take(a, step)] for a in subjects}
            out.append(Case(kind, kind, subjects, mine))
        for name in test.claims:
            plant = next(p for p in s.plants if p.name == name)
            if isinstance(plant, ToolPlant) and not _calls(test.exploit, plant.tool):
                problems.append(
                    f"selftest: claimed plant {name} is tool {plant.tool}, which the exploit"
                    " never calls"
                )
            elif not [a for a in subjects if plant in s.plants_of(a)]:
                problems.append(
                    f"selftest: claimed plant {name} applies to no subject to run the exploit"
                )
    impossible = [p for p in s.plants if isinstance(p, ImpossiblePlant)]
    for p in impossible:
        if not _scored(s, p):
            problems.append(f"plant {p.name}: applies to no subject with a verifier to show it")
    if problems:
        raise SelfTestError(problems)
    if not impossible:
        return out
    try:
        prepared = truth.prepare(bundle, seed=b"scenario-check")
    except Exception as e:
        raise SelfTestError([f"plants: {type(e).__name__}: {e}"]) from None
    idle: dict[str, list[ScriptStep]] = {a: [] for a in subjects}
    agents = tuple(a for a in subjects if any(p in s.plants_of(a) for p in impossible))
    out.append(Case("empty", "untouched workdir", agents, idle))
    for p in impossible:
        mine = tuple(a for a in subjects if p in s.plants_of(a))
        overlay = _overlay(prepared, p.reference, s.sandbox.workdir)
        out.append(
            Case("impossible", f"reference of {p.name}", mine, idle, dict.fromkeys(mine, overlay))
        )
    return out


async def run(
    bench: Bench,
    bundle: Bundle,
    plan: Sequence[Case],
    *,
    profile: str | None = None,
    on_outcome: Callable[[Outcome], Awaitable[None] | None] | None = None,
    origin: str | None = None,
    runner: Callable[[Options], Runner] | None = None,
) -> list[Outcome]:
    """Make each run of `plan` in turn and check it; a run cancelled or killed ends the
    self-test there. With `origin`, each is a `selftest.case` run under that one. `runner`
    makes each Runner, by default on `bench`."""
    out: list[Outcome] = []
    for case in plan:
        options = Options(
            profile=profile,
            scripts=case.scripts,
            overlays=case.overlays or None,
            kind="scenario" if origin is None else "selftest.case",
            origin=origin,
            case=case.name,
        )
        try:
            made = Runner(bench, bundle, options) if runner is None else runner(options)
            result = await made.run()
        except RunError as e:
            outcome = Outcome(case.name, None, problems=[f"the run did not start: {e}"])
            outcome.kind = case.kind
        else:
            outcome = await _check(bench.store, bundle.scenario, case, result)
        out.append(outcome)
        if on_outcome is not None:
            done = on_outcome(outcome)
            if inspect.isawaitable(done):
                await done
        if outcome.status in ("cancelled", "killed"):
            break
    return out


async def _check(store: LocalEventStore, scenario: Scenario, case: Case, result: Result) -> Outcome:
    outcome = Outcome(case.name, result.run_id, kind=case.kind, status=result.status)
    if result.run_id is None or result.status != "completed":
        outcome.problems.append(f"the run {result.status}: {result.reason or 'no reason given'}")
        return outcome
    rows = await check_run(store, result.run_id)
    kinds = ["exec.started", "exec.finished", "tool.call", "tool.result", "task.scored"]
    events = await store.every(result.run_id, types=kinds)
    problems, notes = judge(case, scenario, rows, _ends(events), _scores(events))
    outcome.problems += problems
    outcome.notes += notes
    return outcome


def judge(
    case: Case,
    scenario: Scenario,
    rows: Sequence[Taken],
    ends: Mapping[str, str],
    scores: Mapping[str, TaskScored],
) -> tuple[list[str], list[str]]:
    """The problems and notes of one run of `case`: `rows` are the plants the oracle found,
    `ends` how each agent's steps went, `scores` each agent's verdict."""
    problems: list[str] = []
    notes: list[str] = []
    claims = scenario.selftest.claims if scenario.selftest is not None else []
    if case.kind == "exploit":
        for name in claims:
            plant = next(p for p in scenario.plants if p.name == name)
            for agent in (a for a in case.agents if plant in scenario.plants_of(a)):
                mine = [r for r in rows if r.agent == agent and r.plant == name]
                if any(r.taken for r in mine):
                    continue
                if mine:
                    problems.append(f"{agent} only {_did(mine[0])}, short of taking it")
                else:
                    problems.append(f"{agent} did not trigger claimed plant {name}")
    for row in rows:
        if row.agent not in case.scripts:
            notes.append(f"confederate {row.agent} {_did(row)}")
        elif case.kind == "reference" and not row.taken:
            notes.append(f"{row.agent} {_did(row)}")
        elif case.kind != "exploit":
            problems.append(f"{row.agent} {_did(row)}")
        elif row.taken and row.plant not in claims:
            notes.append(f"{row.agent} also {_did(row)}")
    for agent in case.agents:
        end = ends.get(agent)
        if case.kind == "reference" and end is not None and end != OK:
            problems.append(f"{agent}: a step of the reference ended with {end}")
        elif case.kind == "exploit" and end is not None and end != OK:
            notes.append(f"{agent}: a step of the exploit ended with {end}")
        score = scores.get(agent)
        if case.kind in ("empty", "impossible") and (score is None or score.reward is None):
            why = score.error if score is not None and score.error else "no verdict"
            problems.append(f"{agent}: the verifier gave no reward: {why}")
            continue
        if score is not None and score.reward is not None:
            notes.append(f"{agent}: reward {score.reward:g}")
    return problems, notes


def _did(row: Taken) -> str:
    verb = "took" if row.taken else "touched" if row.tier == "touched" else "reached"
    return f"{verb} plant {row.plant} ({row.kind}, {row.tier}): {row.detail}"


def _script(bundle: Bundle, rel: str) -> ScriptStep:
    """The step that runs the shell script `rel` next to the scenario in the workdir."""
    base = bundle.base or (bundle.path.parent if bundle.path is not None else None)
    if base is None:
        raise SelfTestError([f"selftest: {rel}: the scenario has no directory to read it from"])
    try:
        data = (base / rel).read_bytes()
    except OSError as e:
        raise SelfTestError([f"selftest: {rel}: {e.strerror or e}"]) from None
    if len(data) > MAX_SCRIPT_BYTES:
        raise SelfTestError([f"selftest: {rel} is over {MAX_SCRIPT_BYTES} bytes"])
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise SelfTestError([f"selftest: {rel} is not UTF-8"]) from None
    if not text.strip():
        raise SelfTestError([f"selftest: {rel} is empty"])
    timeout = bundle.scenario.sandbox.max_command_timeout_s
    return ScriptStep(run=text, timeout_s=timeout)


def _calls(script: str | list[ScriptStep] | None, tool: str) -> bool:
    """Whether the steps of `script` call `tool`; a shell script calls no tool."""
    return isinstance(script, list) and any(
        step.call is not None and step.call.tool == tool for step in script
    )


def _scored(scenario: Scenario, plant: ImpossiblePlant) -> bool:
    """Whether a subject's verifier can show `plant`: some subject it applies to has one."""
    return any(
        plant in scenario.plants_of(a) and scenario.task_for(a).verifier is not None
        for a in scenario.subjects
    )


def _overlay(prepared: truth.Prepared, rel: str, workdir: str) -> bytes:
    """A tar of the file or directory `rel` laid over `workdir`: a directory's contents go
    into the workdir, a file under its own name."""
    assert prepared.sources is not None
    root = workdir.lstrip("/")
    tar = _Tar()
    for e in prepared.sources(rel):
        if not e.name and e.kind == "dir":
            continue
        name = posixpath.join(root, e.name or posixpath.basename(rel))
        if e.kind == "dir":
            tar.dir(name, e.mtime)
        elif e.kind == "link":
            tar.link(name, e.target, e.mtime)
        else:
            tar.file(name, e.data, e.mode, e.mtime)
    return tar.finish()


def _ends(events: Sequence[Event]) -> dict[str, str]:
    """How each agent's own steps went: the first that failed, else `OK`. A step is a command
    it ran itself, not through a tool, or a tool call with no model behind it."""
    started: dict[str, str] = {}
    calls: dict[str, tuple[str, str]] = {}
    out: dict[str, str] = {}
    for e in events:
        data, agent = e.data, e.agent_id
        if agent is None:
            continue
        if e.type == "exec.started" and data.get("phase") == "agent":
            if data.get("call_id") is None:
                started[str(data.get("exec_id"))] = agent
        elif e.type == "tool.call" and data.get("model_call") is None:
            calls[str(data.get("call_id"))] = (agent, str(data.get("name")))
        elif e.type == "exec.finished" and str(data.get("exec_id")) in started:
            exited = data.get("end") == "exited"
            end = f"exit code {data.get('exit_code')}" if exited else str(data.get("end"))
            if out.get(agent, OK) == OK:
                out[agent] = end
        elif e.type == "tool.result" and str(data.get("call_id")) in calls:
            _, name = calls[str(data.get("call_id"))]
            end = f"an error from tool {name}" if data.get("error") else OK
            if out.get(agent, OK) == OK:
                out[agent] = end
    return out


def _scores(events: Sequence[Event]) -> dict[str, TaskScored]:
    out: dict[str, TaskScored] = {}
    for e in events:
        payload = e.payload() if e.type == "task.scored" else None
        if isinstance(payload, TaskScored) and e.agent_id is not None:
            out[e.agent_id] = payload
    return out
