import asyncio
import contextlib
import io
import json
import logging
import math
import secrets
import shlex
import tarfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx2
from pydantic import JsonValue

from aims import __version__
from aims.agent import CONTINUE, DENY, Agent, Gate, OpenGate, ReplayPlan, RunBudget, Verdict
from aims.events import (
    MAX_INT,
    AgentEnd,
    AgentFinished,
    Draft,
    LocalEventStore,
    OperatorAction,
    OperatorActionKind,
    ResumeThen,
    RunFinished,
    RunLocked,
    RunStarted,
    RunStatus,
    TaskScored,
)
from aims.models import (
    DeltaBus,
    Limiters,
    ModelClient,
    ModelsConfig,
    Profile,
    ProfileError,
    Prompt,
)
from aims.monitor.core import HumanRequest, Monitor
from aims.monitor.gate import MonitorGate
from aims.monitor.setup import ResolvedMonitor
from aims.prompts import AgentTexts, Prompts, TextError
from aims.run.control import ControlGate
from aims.sandbox import (
    AGENT_UID,
    RUNNER_UID,
    AuditedSandbox,
    ExecResult,
    SandboxBackend,
    SandboxSpec,
)
from aims.sandbox.base import RUNNER_DIR, TOOLBOX
from aims.scenario import AgentConfig, Bundle, Scenario
from aims.scenario.model import HOME
from aims.tools import UnknownTool, load_tools

log = logging.getLogger("aims.run")

BUSYBOX = f"{TOOLBOX}/busybox"
RUNNER_MB = 64
SEED_TIMEOUT_S = 120
VERIFY_PREP_S = 60
SCORED_ENDS: frozenset[AgentEnd] = frozenset(
    {
        "submitted",
        "no_submit",
        "budget_steps",
        "budget_output_tokens",
        "budget_cost",
        "budget_wall",
        "context_exhausted",
    }
)
FAILED_ENDS: frozenset[AgentEnd] = frozenset(
    {"model_error", "sandbox_failed", "divergence", "error"}
)


class RunError(Exception):
    """A scenario or option the run cannot start with."""


@dataclass(frozen=True, slots=True)
class Fork:
    """Where a run forks from: its origin run and the last step replayed from it."""

    origin: str
    step: int
    sample: int = 0
    group: str | None = None
    strict: bool = False
    inject: str | None = None
    profile: str | None = None


@dataclass(slots=True)
class Options:
    profile: str | None = None
    keep: bool = False
    group: str | None = None
    sample: int | None = None
    samples: int = 1
    fork: Fork | None = None


@dataclass(slots=True)
class Result:
    run_id: str | None
    status: RunStatus
    reason: str | None = None
    agents: dict[str, AgentFinished] = field(default_factory=dict)
    scores: dict[str, TaskScored] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.agents.values())


@dataclass(slots=True)
class Bench:
    """What every run of one `aims run` shares: the store, the sandbox backend with its one
    tracer, the model profiles, rate limiters and the delta bus. `gate` sits under each run's
    own control gate."""

    store: LocalEventStore
    backend: SandboxBackend
    models: ModelsConfig
    limiters: Limiters = field(default_factory=Limiters)
    bus: DeltaBus = field(default_factory=DeltaBus)
    audit: str = "kernel"
    gate: Gate = field(default_factory=OpenGate)
    transport: httpx2.AsyncBaseTransport | None = None
    prompts: Prompts = field(default_factory=Prompts)
    config_files: dict[str, str | None] = field(default_factory=dict)


class _Phase(Exception):
    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"{phase}: {detail}")


def agent_profile(models: ModelsConfig, agent: AgentConfig, override: str | None) -> Profile:
    name = override or agent.profile or models.roles.agent
    return models.get(name).apply(agent.overrides)


@dataclass(frozen=True, slots=True)
class Texts:
    """What one agent reads from the harness: the loop's texts and each of its tools' texts."""

    agent: AgentTexts
    tools: dict[str, dict[str, str]]

    def snapshot(self) -> dict[str, JsonValue]:
        tools: dict[str, JsonValue] = {name: dict(t) for name, t in self.tools.items()}
        return {"agent": self.agent.model_dump(), "tools": tools}

    @classmethod
    def restore(cls, value: Any) -> Texts:
        return cls(AgentTexts.model_validate(value["agent"]), dict(value["tools"]))


def resolve_texts(prompts: Prompts, agent: AgentConfig) -> Texts:
    """The texts of `agent`: the defaults, then `prompts` (the config files), then the agent's
    own."""
    layered = prompts.under(agent.prompts)
    tools = load_tools(agent.tool_names, layered.tools)
    return Texts(layered.agent_texts(), {t.name: dict(t.text) for t in tools})


def check(
    bundle: Bundle,
    models: ModelsConfig,
    profile: str | None = None,
    prompts: Prompts | None = None,
) -> list[str]:
    """Problems that stop the scenario from running with these profiles, tools and texts."""
    problems: list[str] = []
    for agent in bundle.scenario.agents:
        try:
            p = agent_profile(models, agent, profile)
        except ProfileError as e:
            problems.append(f"agent {agent.id}: {e}")
            continue
        if agent.compaction is not None and p.context_tokens is None:
            problems.append(
                f"agent {agent.id}: compaction needs context_tokens in profile {p.name}"
            )
        try:
            resolve_texts(prompts or Prompts(), agent)
        except (UnknownTool, TextError) as e:
            problems.append(f"agent {agent.id}: {e}")
    return problems


class Runner:
    """One run of a scenario: image, sandboxes, seed, setup, agents, verifier, archive, close.

    `cancel()` stops the agents and skips the verifier; the files are still archived and the
    run sealed as cancelled. `kill()` freezes every sandbox, archives its files, kills it and
    seals the run as killed, without the verifier.
    """

    def __init__(
        self,
        bench: Bench,
        bundle: Bundle,
        options: Options,
        *,
        profiles: dict[str, Profile] | None = None,
        schedules: dict[str, list[tuple[int | None, Profile]]] | None = None,
        texts: dict[str, Texts] | None = None,
        replay: Callable[[str], Awaitable[dict[str, ReplayPlan]]] | None = None,
        on_start: Callable[[str], None] | None = None,
        run_id: str | None = None,
        monitor: ResolvedMonitor | None = None,
        human: Callable[[HumanRequest], Awaitable[None]] | None = None,
    ) -> None:
        self.bench = bench
        self.wanted_id = run_id
        self.bundle = bundle
        self.scenario = bundle.scenario
        self.options = options
        self.given_profiles = profiles
        self.given_texts = texts or {}
        self.texts: dict[str, Texts] = {}
        self.schedules = schedules or {}
        self.replay = replay
        self.on_start = on_start
        self.watch = monitor
        self.human = human
        self.monitor: Monitor | None = None
        self.run_id: str | None = None
        self.sandboxes: dict[str, AuditedSandbox] = {}
        self.agents: dict[str, Agent] = {}
        self.cancelled = False
        self.killed = False
        self.kill_reason: str | None = None
        self.finished = False
        self._control = ControlGate(bench.gate)
        self._agents_task: asyncio.Task[Any] | None = None
        self._killing: asyncio.Future[None] | None = None
        self._clients: list[ModelClient] = []
        self._untap: Callable[[], None] | None = None
        self._agents_done = False
        self._judged = False
        self._monitor_closed = False

    def cancel(self) -> None:
        self.cancelled = True
        if self._agents_task is not None:
            self._agents_task.cancel()

    @property
    def control(self) -> ControlGate:
        """The run's own gate, over the bench's: the operator's and the monitor's pauses,
        resumes, denies, injects and kill."""
        return self._control

    async def kill(self, reason: str = "killed by the operator") -> None:
        """Freeze every sandbox, archive its files, kill it and stop the agents. The run is
        then sealed as killed and the verifier does not run."""
        if self._killing is None:
            self._killing = asyncio.ensure_future(self._kill(reason))
        await asyncio.shield(self._killing)

    async def _kill(self, reason: str) -> None:
        self.killed = True
        self.kill_reason = reason
        self._control.kill(reason)
        for agent in self.agents.values():
            agent.killed = reason
        sandboxes = list(self.sandboxes.items())
        for agent_id, sb in sandboxes:
            try:
                await sb.sandbox.pause()
            except Exception as e:
                log.warning("pausing the sandbox of agent %s failed: %s", agent_id, e)
        await self._archive()
        for agent_id, sb in sandboxes:
            try:
                await sb.stop("killed", reason)
            except Exception as e:
                log.warning("killing the sandbox of agent %s failed: %s", agent_id, e)
        if self._agents_task is not None:
            self._agents_task.cancel()

    def _targets(self, agent: str | None) -> list[str]:
        ids = [a.id for a in self.scenario.agents]
        if agent is None:
            return ids
        if agent not in ids:
            raise RunError(f"the run has no agent {agent!r}")
        return [agent]

    async def operate(
        self,
        action: OperatorActionKind,
        *,
        agent: str | None = None,
        then: ResumeThen | None = None,
        text: str | None = None,
        reason: str | None = None,
        token: str | None = None,
        command_id: str | None = None,
    ) -> None:
        """Carry out an operator request on the running run, recorded first as
        `operator.action`. Pause, resume and inject take one agent or, with none, all. A resume
        with deny refuses the tool call each agent waits before, and needs one that does. A
        sweep is left to the monitor, which reads the action from the log."""
        if self.run_id is None or self.finished:
            raise RunError("the run is not running")
        if (action == "inject" or then == "inject") and not text:
            raise RunError("an inject needs text")
        if action == "sweep" and (self.watch is None or not self.watch.sweeps):
            raise RunError("this run has no monitor that sweeps")
        targets = self._targets(agent)
        c = self._control
        held = any(c.is_paused(a) and c.held_call(a) for a in targets)
        if action == "resume" and then == "deny" and not held:
            raise RunError("no paused agent waits before a tool call to deny")
        store = self.bench.store
        recorded = OperatorAction(
            action=action,
            then=None if action != "resume" else (then or "continue"),
            text=None if text is None else await store.content(self.run_id, text),
            reason=reason,
            token=token,
            command_id=command_id,
        )
        await store.append(self.run_id, [Draft(recorded, agent)])
        if action == "pause":
            await self._pause(targets, reason or "paused by the operator")
        elif action == "resume" and then == "kill":
            await self.kill(reason or "killed by the operator")
        elif action == "resume":
            if then == "inject":
                verdict = Verdict("inject", text=text)
            else:
                verdict = DENY if then == "deny" else CONTINUE
            await self._resume(targets, verdict)
        elif action == "inject":
            self._control.inject(targets, text or "")
        elif action == "kill":
            await self.kill(reason or "killed by the operator")
        elif action == "cancel":
            self.cancel()

    async def _pause(self, targets: Sequence[str], reason: str) -> None:
        """Pause `targets` and freeze their sandboxes at once, but never the sandbox of an
        agent that has finished, which its verifier still needs."""
        self._control.pause(targets, reason)
        for agent_id in targets:
            sb = self.sandboxes.get(agent_id)
            agent = self.agents.get(agent_id)
            if sb is None or sb.stopped or (agent is not None and agent.done):
                continue
            await sb.sandbox.pause()

    async def _resume(self, targets: Sequence[str], verdict: Verdict) -> None:
        for agent_id in self._control.resume(targets, verdict):
            sb = self.sandboxes.get(agent_id)
            if sb is not None and not sb.stopped:
                await sb.sandbox.resume()

    # The monitor's Actuator: it records `monitor.action` itself before each of these.

    async def pause(self, agents: Sequence[str], reason: str) -> None:
        await self._pause(self._targets_of(agents), reason)

    async def resume(
        self, agents: Sequence[str], then: Literal["continue", "deny"] = "continue"
    ) -> None:
        await self._resume(self._targets_of(agents), DENY if then == "deny" else CONTINUE)

    def paused(self, agent_id: str) -> bool:
        return self._control.is_paused(agent_id)

    def held_call(self, agent_id: str) -> str | None:
        return self._control.held_call(agent_id)

    def next_prompt(self, agent_id: str) -> tuple[Profile, Prompt] | None:
        """The profile and prompt of the agent's next step: the step whose tool calls wait to
        run, else its next model call. None once the agent has finished."""
        agent = self.agents.get(agent_id)
        if agent is None or agent.done:
            return None
        return agent.next_prompt()

    async def ask_human(self, request: HumanRequest) -> None:
        """Hand the pause to `human` when the run has one; a linked control plane shows it
        anyway, and the person answers there."""
        if self.human is not None:
            await self.human(request)

    def _targets_of(self, agents: Sequence[str]) -> list[str]:
        known = {a.id for a in self.scenario.agents}
        return [a for a in agents if a in known]

    async def run(self) -> Result:
        profiles = self._profiles()
        for agent in self.scenario.agents:
            try:
                self.texts[agent.id] = self.given_texts.get(agent.id) or resolve_texts(
                    self.bench.prompts, agent
                )
            except (UnknownTool, TextError) as e:
                raise RunError(f"agent {agent.id}: {e}") from None
        watch = self.watch
        started = RunStarted(
            scenario=self.scenario.name,
            oversight=self.scenario.oversight,
            agents=[a.id for a in self.scenario.agents],
            config=self._config(profiles),
            monitor=(watch.identity or None) if watch is not None else None,
        )
        store = self.bench.store
        run_id = self.wanted_id or str(uuid.uuid7())
        if watch is not None:
            self._watch(run_id, watch)
        try:
            self.run_id = run_id = await store.create_run(
                started, blobs=self.bundle.blobs(), run_id=run_id
            )
        except BaseException:
            self._stop_tap()
            raise
        if self.on_start is not None:
            self.on_start(run_id)
        result = Result(run_id, "failed")
        try:
            await self._phases(profiles, result)
        except _Phase as e:
            result.status, result.reason = "failed", str(e)
        except asyncio.CancelledError:
            result.status, result.reason = "cancelled", "cancelled before the agents finished"
            await asyncio.shield(self._close(kill=True))
            await asyncio.shield(self._seal(result))
            raise
        except Exception as e:
            log.exception("run %s failed", run_id)
            result.status, result.reason = "failed", f"{type(e).__name__}: {e}"
        if self._killing is not None:
            await asyncio.shield(self._killing)
            result.status, result.reason = "killed", self.kill_reason
        await asyncio.shield(self._close_sandboxes(kill=False))
        await self._judge(result)
        await asyncio.shield(self._close(kill=False))
        await asyncio.shield(self._seal(result))
        return result

    def _watch(self, run_id: str, watch: ResolvedMonitor) -> None:
        """Set up the run's monitor before `run.started` is written, so the tap feeds it every
        event of the run, and put it under the run's gate."""
        bench = self.bench
        client = ModelClient(
            bench.store,
            run_id,
            bench.models,
            bus=bench.bus,
            limiters=bench.limiters,
            transport=bench.transport,
        )
        try:
            monitor = Monitor(
                run_id=run_id,
                scenario=self.scenario,
                monitor=watch,
                store=bench.store,
                client=client,
                actuator=self,
                simulated=watch.oversight == "observe",
            )
        except Exception as e:
            raise RunError(f"the monitor does not start: {type(e).__name__}: {e}") from None
        self._clients.append(client)
        self.monitor = monitor
        self._untap = bench.store.tap(run_id, monitor.feed)
        hold = any(s.hold for s in watch.config.sweeps)
        self._control.inner = MonitorGate(
            monitor, inner=bench.gate, hold=hold, clock=self._control.clock
        )

    def _stop_tap(self) -> None:
        if self._untap is not None:
            self._untap()
            self._untap = None

    def _profiles(self) -> dict[str, Profile]:
        out: dict[str, Profile] = {}
        for agent in self.scenario.agents:
            if self.given_profiles is not None:
                profile = self.given_profiles[agent.id]
            else:
                try:
                    profile = agent_profile(self.bench.models, agent, self.options.profile)
                except ProfileError as e:
                    raise RunError(str(e)) from None
            if self.options.samples > 1 and profile.sampling.seed is not None:
                log.warning(
                    "agent %s: profile %s fixes the sampling seed; clearing it so samples differ",
                    agent.id,
                    profile.name,
                )
                sampling = profile.sampling.model_copy(update={"seed": None})
                profile = profile.model_copy(update={"sampling": sampling})
            out[agent.id] = profile
        return out

    def _config(self, profiles: dict[str, Profile]) -> dict[str, JsonValue]:
        o = self.options
        fork: JsonValue = None
        if o.fork is not None:
            f = o.fork
            fork = {
                "origin": f.origin,
                "step": f.step,
                "sample": f.sample,
                "group": f.group,
                "strict": f.strict,
                "inject": f.inject,
                "profile": f.profile,
            }
        return {
            "aims": __version__,
            "scenario": self.bundle.snapshot(),
            "agents": {
                a: {
                    "profile": p.snapshot(),
                    "schedule": [
                        [until, q.snapshot()]
                        for until, q in [*self.schedules.get(a, []), (None, p)]
                    ],
                    "texts": self.texts[a].snapshot(),
                }
                for a, p in profiles.items()
            },
            "audit": self.bench.audit,
            "config_files": dict(self.bench.config_files),
            "keep": o.keep,
            "group": o.group,
            "sample": o.sample,
            "fork": fork,
            "monitor": None if self.watch is None else self.watch.snapshot(),
        }

    async def _phases(self, profiles: dict[str, Profile], result: Result) -> None:
        if self.monitor is not None:
            await self.monitor.start()
        for phase in (self._image_and_sandboxes, self._seed, self._setup):
            if self.cancelled or self.killed:
                break
            await phase()
        if not self.cancelled and not self.killed:
            await self._run_agents(profiles, result)
        if self._killing is None and not self.cancelled:
            await self._final_sweeps()
        if self._killing is not None:
            await asyncio.shield(self._killing)
            await self._collect_finished(result)
            return
        if self.cancelled:
            result.status, result.reason = "cancelled", "cancelled by the operator"
        else:
            await self._verify_all(result)
            result.status, result.reason = _status(result)
        await self._archive()

    async def _image_and_sandboxes(self) -> None:
        await self._start_sandboxes(await self._image())

    async def _image(self) -> str:
        env = self.scenario.task.environment
        if env.image is not None:
            return env.image
        assert self.bundle.context is not None
        try:
            return await self.bench.backend.build(
                self.bundle.context, self.bundle.dockerfile or "Dockerfile"
            )
        except Exception as e:
            raise _Phase("image build failed", str(e)) from None

    async def _start_sandboxes(self, image: str) -> None:
        sc = self.scenario.sandbox
        spec = SandboxSpec(
            image=image,
            workdir=sc.workdir,
            env=sc.env,
            cpus=sc.cpus,
            memory_mb=sc.memory_mb,
            pids=sc.pids,
            tmp_mb=sc.tmp_mb,
            kill_leftovers=sc.kill_leftovers,
            runner_mb=RUNNER_MB,
            keep=self.options.keep,
        )
        assert self.run_id is not None
        for agent in self.scenario.agents:
            try:
                self.sandboxes[agent.id] = await AuditedSandbox.start(
                    self.bench.backend,
                    spec,
                    self.bench.store,
                    run_id=self.run_id,
                    agent_id=agent.id,
                )
            except Exception as e:
                raise _Phase(f"sandbox for agent {agent.id} did not start", str(e)) from None

    async def _seed(self) -> None:
        if self.bundle.files is None:
            return
        command = f"{BUSYBOX} tar -x -o -f - -C /"
        for agent_id, sb in self.sandboxes.items():
            r = await sb.exec(
                command, timeout_s=SEED_TIMEOUT_S, stdin=self.bundle.files, phase="setup", cwd="/"
            )
            if r.finished.end != "exited" or r.finished.exit_code != 0:
                raise _Phase(f"seeding files for agent {agent_id} failed", _why(r))

    async def _setup(self) -> None:
        task = self.scenario.task
        for agent_id, sb in self.sandboxes.items():
            for n, command in enumerate(task.setup, 1):
                r = await sb.exec(command, timeout_s=task.setup_timeout_s, phase="setup")
                if r.finished.end != "exited" or r.finished.exit_code != 0:
                    raise _Phase(f"setup command {n} for agent {agent_id} failed", _why(r))

    async def _run_agents(self, profiles: dict[str, Profile], result: Result) -> None:
        assert self.run_id is not None
        bench = self.bench
        budget = RunBudget(self.scenario.limits)
        client = ModelClient(
            bench.store,
            self.run_id,
            bench.models,
            bus=bench.bus,
            limiters=bench.limiters,
            transport=bench.transport,
        )
        self._clients.append(client)
        plans: dict[str, ReplayPlan] = {}
        if self.replay is not None:
            plans = await self.replay(self.run_id)
            self._clients.extend({id(p.client): p.client for p in plans.values()}.values())
        for agent in self.scenario.agents:
            self.agents[agent.id] = made = Agent(
                config=agent,
                sandbox_config=self.scenario.sandbox,
                instruction=self.scenario.task.instruction,
                texts=self.texts[agent.id].agent,
                profile=profiles[agent.id],
                client=client,
                sandbox=self.sandboxes[agent.id],
                store=bench.store,
                run_id=self.run_id,
                tools=load_tools(agent.tool_names, self.texts[agent.id].tools),
                run_budget=budget,
                gate=self._control,
                replay=plans.get(agent.id),
                clock=self._control.clock(agent.id),
            )
            made.killed = self.kill_reason

        async def everyone() -> None:
            async with asyncio.TaskGroup() as tg:
                tasks = {a: tg.create_task(agent.run()) for a, agent in self.agents.items()}
            for a, task in tasks.items():
                result.agents[a] = task.result()

        self._agents_task = asyncio.create_task(everyone())
        if self.cancelled or self.killed:
            self._agents_task.cancel()
        try:
            await self._agents_task
        except asyncio.CancelledError:
            if not self.cancelled and not self.killed:
                raise
        finally:
            self._agents_task = None
        if self.cancelled:
            await self._collect_finished(result)

    async def _collect_finished(self, result: Result) -> None:
        assert self.run_id is not None
        events = await self.bench.store.events(self.run_id, types=["agent.finished"], limit=1000)
        for e in events:
            payload = e.payload()
            if isinstance(payload, AgentFinished) and e.agent_id is not None:
                result.agents[e.agent_id] = payload

    async def _verify_all(self, result: Result) -> None:
        verifier = self.scenario.task.verifier
        if verifier is None:
            return
        for agent_id, finished in result.agents.items():
            sb = self.sandboxes.get(agent_id)
            if sb is None or sb.stopped or finished.reason not in SCORED_ENDS:
                continue
            answer = self.agents[agent_id].answer if agent_id in self.agents else None
            scored = await self._verify(sb, agent_id, finished.reason, answer)
            scored = scored.model_copy(update={"tainted": await self._tainted(agent_id)})
            result.scores[agent_id] = scored
            await self.bench.store.append(self.run_id or "", [Draft(scored, agent_id)])

    async def _verify(
        self, sb: AuditedSandbox, agent_id: str, end: str, answer: str | None
    ) -> TaskScored:
        v = self.scenario.task.verifier
        assert v is not None
        if v.restore and (error := await self._restore(sb, v.restore)) is not None:
            return TaskScored(error=error)
        if v.user == "runner":
            uid, gid, base = RUNNER_UID, AGENT_UID, RUNNER_DIR
        else:
            uid, gid, base = AGENT_UID, None, f"/tmp/aims-verifier-{secrets.token_hex(6)}"
        tests, logs, home = f"{base}/tests", f"{base}/logs", f"{base}/home"
        submission = f"{base}/submission.txt"
        q = shlex.quote
        prep = (
            f"B={BUSYBOX}; $B mkdir -p -m 700 {q(base)} && $B mkdir -p {q(tests)} {q(logs)}"
            f" {q(home)}"
        )
        if self.bundle.verifier is not None:
            prep += f" && $B tar -x -o -f - -C {q(base)}"

        def run(command: str, timeout_s: float, **kw: Any) -> Awaitable[ExecResult]:
            return sb.exec(command, timeout_s=timeout_s, uid=uid, gid=gid, phase="verifier", **kw)

        r = await run(prep, VERIFY_PREP_S, stdin=self.bundle.verifier, cwd="/")
        if r.finished.end != "exited" or r.finished.exit_code != 0:
            return TaskScored(error=f"preparing the verifier failed: {_why(r)}")
        data = (answer or "").encode()
        write = f"{BUSYBOX} head -c {len(data)} > {q(submission)}"
        r = await run(write, VERIFY_PREP_S, stdin=data, cwd="/")
        if r.finished.end != "exited" or r.finished.exit_code != 0:
            return TaskScored(error=f"writing the submission failed: {_why(r)}")
        env = {
            "HOME": home,
            "AIMS_TESTS_DIR": tests,
            "AIMS_LOGS_DIR": logs,
            "AIMS_SUBMISSION": submission,
            "AIMS_AGENT_END": end,
            **v.env,
        }
        verdict = await run(v.command, v.timeout_s, env=env)
        f = verdict.finished
        j, t = q(f"{logs}/reward.json"), q(f"{logs}/reward.txt")
        read = (
            f"B={BUSYBOX}; if [ -f {j} ]; then echo J; $B cat {j};"
            f" elif [ -f {t} ]; then echo T; $B cat {t}; else echo N; fi"
        )
        out = await run(read, VERIFY_PREP_S, cwd="/")
        kind, _, body = out.stdout.partition(b"\n")
        reward, metrics, source, error = _reward(kind, body)
        if f.end != "exited" and error is None:
            error = f"the verifier ended with {f.end}"
        return TaskScored(
            exec_id=f.exec_id,
            end=f.end,
            exit_code=f.exit_code,
            reward=reward,
            metrics=metrics,
            source=source,
            error=error,
        )

    async def _restore(self, sb: AuditedSandbox, globs: Sequence[str]) -> str | None:
        """Put the seeded files that match `globs` back as they were seeded, as the seed did."""
        data = seeded(self.bundle.files, self.scenario, globs)
        if data is None:
            log.warning("verifier.restore matches no seeded file: %s", ", ".join(globs))
            return None
        command = f"{BUSYBOX} tar -x -o -f - -C /"
        r = await sb.exec(command, timeout_s=SEED_TIMEOUT_S, stdin=data, phase="setup", cwd="/")
        if r.finished.end != "exited" or r.finished.exit_code != 0:
            return f"restoring the seeded files failed: {_why(r)}"
        return None

    async def _archive(self) -> None:
        paths = [self.scenario.sandbox.workdir, "/tmp", HOME]
        for agent_id, sb in self.sandboxes.items():
            try:
                await sb.archive(paths)
            except Exception as e:
                log.warning("archive of agent %s failed: %s", agent_id, e)

    async def _tainted(self, agent_id: str) -> list[int]:
        """The flags the monitor raised while the verifier of `agent_id` ran, once it has taken
        in every event so far."""
        if self.monitor is None:
            return []
        try:
            await self.monitor.barrier()
            return self.monitor.tainted(agent_id)
        except Exception:
            log.exception("the monitor of run %s gave no taint for agent %s", self.run_id, agent_id)
            return []

    async def _final_sweeps(self) -> None:
        """Tell the monitor the agents stopped, once, if any agent ran. A monitor that fails
        here does not fail the run."""
        if self.monitor is None or not self.agents or self._agents_done:
            return
        self._agents_done = True
        try:
            await self.monitor.agents_done()
        except Exception:
            log.exception("the final sweeps of run %s failed", self.run_id)

    async def _judge(self, result: Result) -> None:
        """The monitor's last word on a run whose agents ran: the final sweeps when a kill cut
        them, then the judge, skipped on a cancelled run. A second Ctrl-C cuts it short."""
        monitor = self.monitor
        if monitor is None:
            return
        cancelled = result.status == "cancelled"
        try:
            if not cancelled:
                await self._final_sweeps()
            self._judged = True
            await monitor.judge(skip=cancelled or not self.agents)
        except asyncio.CancelledError:
            result.status, result.reason = "cancelled", "cancelled while the monitor finished"
            await asyncio.shield(self._close(kill=True))
            await asyncio.shield(self._seal(result))
            raise
        except Exception:
            log.exception("the monitor of run %s failed to finish", self.run_id)

    async def _close_monitor(self) -> None:
        monitor = self.monitor
        if monitor is None or self._monitor_closed:
            return
        self._monitor_closed = True
        try:
            if not self._judged:
                self._judged = True
                await monitor.judge(skip=True)
            await monitor.close()
        except Exception:
            log.exception("closing the monitor of run %s failed", self.run_id)
        finally:
            self._stop_tap()

    async def _close_sandboxes(self, *, kill: bool) -> None:
        for agent_id, sb in list(self.sandboxes.items()):
            try:
                if kill and not self.options.keep:
                    await sb.stop("killed", "the run was cancelled")
                await sb.close()
            except Exception as e:
                log.warning("closing the sandbox of agent %s failed: %s", agent_id, e)
        self.sandboxes.clear()

    async def _close(self, *, kill: bool) -> None:
        await self._close_sandboxes(kill=kill)
        await self._close_monitor()
        for client in self._clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()

    async def _seal(self, result: Result) -> None:
        if self.run_id is None:
            return
        finished = RunFinished(status=result.status, reason=result.reason)
        try:
            await self.bench.store.append(self.run_id, [Draft(finished)])
        except Exception as e:
            log.error("could not seal run %s: %s", self.run_id, e)
        self.finished = True


def seeded(files: bytes | None, scenario: Scenario, globs: Sequence[str]) -> bytes | None:
    """The part of the seed tar `files` that `globs` match, with the directories above it; None
    when no seeded file matches. A glob is absolute or relative to the workdir, `**` crosses
    directories, and a matched directory brings what is under it."""
    if files is None or not globs:
        return None
    patterns = [scenario.sandbox_path(g) for g in globs]

    def matched(name: str) -> bool:
        path = PurePosixPath("/", name)
        return any(p.full_match(g) for p in (path, *path.parents) for g in patterns)

    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(files), mode="r:") as source,
        tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        members = source.getmembers()
        names = {m.name for m in members if not m.isdir() and matched(m.name)}
        if not names:
            return None
        names |= {m.name for m in members if m.isdir() and matched(m.name)}
        above = {str(p) for n in names for p in PurePosixPath(n).parents}
        for m in members:
            if m.name in names or (m.isdir() and m.name in above):
                tar.addfile(m, source.extractfile(m) if m.isfile() else None)
    return out.getvalue()


RESTARTED = "runner restarted"


async def seal_unsealed(store: LocalEventStore, *, reason: str = RESTARTED) -> list[str]:
    """Seal as failed every run in `store` left running by a process that is gone. A run whose
    log another process holds open is still being written and is left alone."""
    sealed: list[str] = []
    for run_id in store.run_ids():
        info = await store.run(run_id)
        if info is None or info.status != "running":
            continue
        try:
            await store.append(run_id, [Draft(RunFinished(status="failed", reason=reason))])
        except RunLocked:
            continue
        except Exception as e:
            log.warning("could not seal run %s: %s", run_id, e)
            continue
        sealed.append(run_id)
    return sealed


def _status(result: Result) -> tuple[RunStatus, str | None]:
    reasons = {a: f.reason for a, f in result.agents.items()}
    if any(r == "killed" for r in reasons.values()):
        return "killed", "killed by the gate"
    failed = {a: r for a, r in reasons.items() if r in FAILED_ENDS}
    if failed:
        detail = ", ".join(
            f"agent {a}: {result.agents[a].detail or r}" for a, r in sorted(failed.items())
        )
        return "failed", detail
    return "completed", None


def _why(r: Any) -> str:
    f = r.finished
    err = r.stderr.decode(errors="replace").strip()[-500:]
    end = f"exit code {f.exit_code}" if f.end == "exited" else f.end
    return f"{end}{': ' + err if err else ''}{': ' + f.error if f.error else ''}"


def _reward(kind: bytes, body: bytes) -> tuple[float | None, dict[str, JsonValue], Any, str | None]:
    if kind == b"N":
        return None, {}, "none", "the verifier wrote no reward"
    text = body.decode(errors="replace")
    if kind == b"J":
        try:
            value = json.loads(text)
        except ValueError as e:
            return None, {}, "reward.json", f"reward.json is not JSON: {e}"
        if not isinstance(value, dict):
            return None, {}, "reward.json", "reward.json is not an object"
        value = _loggable(value)
        numbers = {k: v for k, v in value.items() if _number(v)}
        reward = value.get("reward") if _number(value.get("reward")) else None
        if reward is None and len(numbers) == 1:
            reward = next(iter(numbers.values()))
        return (None if reward is None else float(reward)), value, "reward.json", None
    try:
        reward = float(text.strip())
    except ValueError:
        return None, {}, "reward.txt", f"reward.txt is not a number: {text.strip()[:80]!r}"
    if not math.isfinite(reward):
        return None, {}, "reward.txt", f"reward.txt is not finite: {reward}"
    return reward, {"reward": reward}, "reward.txt", None


def _number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= MAX_INT
    return isinstance(value, float) and math.isfinite(value)


def _loggable(value: Any) -> Any:
    """`value` with every number canonical JSON cannot carry (NaN, infinities, ints past 2**53)
    turned into its text, so the score can be logged."""
    if isinstance(value, dict):
        return {k: _loggable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_loggable(v) for v in value]
    if isinstance(value, int | float) and not isinstance(value, bool) and not _number(value):
        return str(value)
    return value
