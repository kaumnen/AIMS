import asyncio
import hashlib
import json
import logging
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aims.agent import ChannelVersions, RecordedOutput, ReplayPlan, Turnstile
from aims.events import (
    MAX_INT,
    AgentInput,
    AgentStep,
    BlobRef,
    Content,
    EventStore,
    RunStarted,
    ToolCall,
    ToolResult,
)
from aims.models import ModelClient, ModelsConfig, Profile, ProfileError, Recording
from aims.run import truth
from aims.run.runner import Bench, Fork, RunError, Texts
from aims.scenario import Bundle, ScenarioError, from_snapshot
from aims.scenario import budget as budgets
from aims.scenario.budget import BudgetError, BudgetOverride

PAGE = 1000


log = logging.getLogger("aims.run")


@dataclass(slots=True)
class Origin:
    """What a fork needs from the run it forks: the scenario as recorded, each agent's profiles
    and texts, its step count, the tool outputs it was given and the messages injected. What
    the origin recorded of a tool call is keyed by its step and call id, since a provider may
    give calls of different steps the same id."""

    run_id: str
    bundle: Bundle
    schedules: dict[str, list[tuple[int | None, Profile]]]
    texts: dict[str, Texts]
    steps: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, dict[tuple[int, str], RecordedOutput]] = field(default_factory=dict)
    injects: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    full_bundle: Bundle | None = None
    truth: dict[str, Any] | None = None
    step_seqs: dict[str, list[int]] = field(default_factory=dict)
    step_calls: dict[str, list[dict[str, int]]] = field(default_factory=dict)
    """The origin seq of each tool call of each step, by call id."""
    actions: dict[str, list[int]] = field(default_factory=dict)
    versions: dict[str, dict[tuple[int, str], ChannelVersions]] = field(default_factory=dict)
    restored: dict[str, bytes] = field(default_factory=dict)
    last_seq: int = -1
    budget: BudgetOverride | None = None
    """The caps the origin ran under over its scenario's, which its forks keep."""
    replayable: dict[str, int] = field(default_factory=dict)
    """The last step of each agent a fork can replay, from `replayable_steps`."""
    running: bool = False
    """Whether the origin was still running when it was loaded."""
    ended: set[str] = field(default_factory=set)
    """The agents whose end is in the origin's log."""

    @property
    def seed(self) -> bytes | None:
        """The canary seed of the origin, which its forks reuse."""
        return None if self.truth is None else bytes.fromhex(self.truth["seed"])

    @property
    def inherited(self) -> list[dict[str, Any]] | None:
        return None if self.truth is None else self.truth.get("inherited")

    def check_budget(self, budget: BudgetOverride | None, steps: Mapping[str, int]) -> None:
        """Raises BudgetError when a step cap `budget` sets would end an agent, or the run,
        before the first live step after the cut: the cap must be above the steps replayed.
        `steps` is the steps each agent replays."""
        if budget is None or budget.empty:
            return
        merged = budgets.apply((self.full_bundle or self.bundle).scenario, self.fork_budget(budget))
        model = [a for a in merged.agents if not a.scripted]
        for a in model:
            n = steps.get(a.id, 0)
            cap = a.budget.steps
            if "steps" in budget.agent_caps(a.id) and cap is not None and cap <= n:
                raise BudgetError(
                    f"agent {a.id} replays {n} steps, so a cap of {cap} steps ends it before"
                    f" it goes live; give it more than {n}"
                )
        total = sum(steps.get(a.id, 0) for a in model)
        cap = merged.limits.steps
        if "steps" in budget.run_caps() and cap is not None and cap <= total:
            raise BudgetError(
                f"the agents replay {total} steps, so a run cap of {cap} steps ends the run"
                f" before it goes live; give it more than {total}"
            )

    def fork_budget(self, budget: BudgetOverride | None) -> BudgetOverride | None:
        """A fork's override: `budget` laid over the origin's, cap by cap. Raises BudgetError
        when `budget` names an agent the origin lacks."""
        ids = [a.id for a in self.bundle.scenario.agents]
        if budget is not None:
            budget.check(ids)
        return budgets.over(self.budget, budget, ids)


async def _text(store: EventStore, run_id: str, content: Content) -> str:
    if isinstance(content, BlobRef):
        return (await store.get_blob(run_id, content.sha256)).decode()
    return content


def same_scenario(origin: Origin, bundle: Bundle) -> bool:
    """Whether `bundle` is the scenario `origin` ran: the public copy it makes with the
    origin's seed, variant and inherited canaries matches the one the origin recorded."""
    if origin.truth is None:
        return bundle.digest == origin.bundle.digest
    try:
        made = truth.prepare(bundle, origin.truth.get("variant"), origin.seed, origin.inherited)
    except ValueError, OSError:
        return False
    return origin.bundle.digest in truth.digests(made)


def _truth(store: EventStore, run_id: str, want: str) -> dict[str, Any] | None:
    """The run's truth record, None when this machine does not hold it. A fork without it
    starts from the public copy, which lacks the origin's plants, roles and scripts."""
    root = getattr(store, "root", None)
    file = truth.path(Path(root) / run_id) if root is not None else None
    if file is None or not file.is_file():
        log.warning("run %s: no truth file here; a fork of it starts from the public copy", run_id)
        return None
    try:
        data = file.read_bytes()
    except OSError as e:
        raise RunError(f"run {run_id}: cannot read its truth file: {e.strerror}") from None
    if hashlib.sha256(data).hexdigest() != want:
        raise RunError(f"run {run_id}: its truth file does not match run.started")
    return json.loads(data)


async def replayable_steps(store: EventStore, run_id: str, agents: Iterable[str]) -> dict[str, int]:
    """The last step of each of `agents` a fork of run `run_id` can replay. A replayed step
    gives each of its tool calls the output the run recorded, so an agent's last step counts
    only once the log holds a result for each of its tool calls; else the step before it is
    the last. A run that ended at a gate, mid-call or in a crash leaves its step without them,
    and so does a run still in that step. The steps before an agent's last always have them:
    its loop asks the model again only once every tool call of the step has its result."""
    out: dict[str, int] = {}
    for agent in agents:
        last = await store.events(
            run_id, before=MAX_INT, limit=1, types=["agent.step"], agent_id=agent
        )
        if not last:
            out[agent] = 0
            continue
        step = AgentStep.model_validate(last[0].data)
        waiting = set(step.tool_calls)
        cursor = last[0].seq
        while waiting and (
            page := await store.events(
                run_id, after=cursor, limit=PAGE, types=["tool.result"], agent_id=agent
            )
        ):
            waiting -= {str(e.data.get("call_id")) for e in page}
            cursor = page[-1].seq
        out[agent] = step.n - 1 if waiting else step.n
    return out


async def load_origin(store: EventStore, run_id: str, *, full: bool = True) -> Origin:
    info = await store.run(run_id)
    if info is None:
        raise RunError(f"unknown run {run_id}")
    first = await store.events(run_id, limit=1)
    started = first[0].payload() if first else None
    if not isinstance(started, RunStarted):
        raise RunError(f"run {run_id} has no run.started")
    config = started.config
    snapshot = config.get("scenario")
    agents = config.get("agents")
    if not isinstance(snapshot, dict) or not isinstance(agents, dict):
        raise RunError(f"run {run_id} is not a scenario run (no scenario or agents in run.started)")
    blobs: dict[str, bytes] = {}
    for name in ("file", "files", "verifier", "context"):
        ref = snapshot.get(name)
        if isinstance(ref, dict):
            sha = str(ref["sha256"])
            try:
                blobs[sha] = await store.get_blob(run_id, sha)
            except KeyError:
                # a delete moves the run's folder away before it forgets the run's rows
                raise RunError(f"unknown run {run_id}") from None
    try:
        bundle = from_snapshot(snapshot, blobs)
    except ScenarioError as e:
        raise RunError(f"run {run_id}: {e}") from None
    restored: Bundle | None = None
    record: dict[str, Any] | None = None
    if full and started.truth is not None:
        record = _truth(store, run_id, started.truth)
    if record is not None:
        try:
            made = await asyncio.to_thread(truth.restore, snapshot, blobs, record)
            restored = made.full_bundle
        except (KeyError, ValueError, OSError, ScenarioError) as e:
            raise RunError(f"run {run_id}: cannot restore its truth: {e}") from None
    schedules: dict[str, list[tuple[int | None, Profile]]] = {}
    texts: dict[str, Texts] = {}
    for agent_id, entry in agents.items():
        assert isinstance(entry, dict)
        schedules[agent_id] = _schedule(entry.get("schedule") or [[None, entry["profile"]]])
        try:
            texts[agent_id] = Texts.restore(entry["texts"])
        except (KeyError, TypeError, ValueError) as e:
            raise RunError(
                f"run {run_id}: agent {agent_id} recorded no usable texts: {e}"
            ) from None
    origin = Origin(
        run_id,
        bundle,
        schedules,
        texts,
        full_bundle=restored,
        truth=record,
        budget=budgets.load(config.get("budget")),
    )
    steps: dict[str, int] = defaultdict(int)
    outputs: dict[str, dict[tuple[int, str], RecordedOutput]] = defaultdict(dict)
    injects: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    types = ("agent.step", "tool.result", "agent.input", "agent.finished")
    cursor: int | None = None
    while page := await store.events(run_id, after=cursor, limit=PAGE, types=types):
        for event in page:
            cursor = event.seq
            agent = event.agent_id or ""
            if event.type == "agent.finished":
                origin.ended.add(agent)
                continue
            payload = event.payload()
            if event.type == "agent.step":
                steps[agent] += 1
            elif isinstance(payload, ToolResult):
                text = await _text(store, run_id, payload.output)
                # a step's tool results come after its agent.step and before the next
                outputs[agent][steps[agent], payload.call_id] = RecordedOutput(text, payload.error)
            elif isinstance(payload, AgentInput) and payload.kind == "inject":
                injects[agent][payload.step].append(await _text(store, run_id, payload.text))
    origin.steps = dict(steps)
    origin.outputs = dict(outputs)
    origin.injects = {a: dict(v) for a, v in injects.items()}
    ids = [a.id for a in bundle.scenario.agents]
    origin.replayable = await replayable_steps(store, run_id, ids)
    origin.running = info.status == "running"
    await _order(store, run_id, origin)
    for c in started.channels:
        if c.restored is not None:
            origin.restored[c.name] = await store.get_blob(run_id, c.restored.sha256)
    return origin


async def _order(store: EventStore, run_id: str, origin: Origin) -> None:
    """When each agent took each step and tool call in the origin, a scripted agent's steps,
    and what each tool call met of the channels. A model's tool calls come before the step
    that made them; the execs and reads of a call come after it."""
    step_seqs: dict[str, list[int]] = defaultdict(list)
    step_calls: dict[str, list[dict[str, int]]] = defaultdict(list)
    replies: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    actions: dict[str, list[int]] = defaultdict(list)
    # a tool call by its agent, its step and its call id
    execs: dict[str, tuple[str, int, str]] = {}
    reads: dict[tuple[str, int, str], dict[str, int]] = defaultdict(dict)
    writes: dict[tuple[str, int, str], dict[str, str | None]] = defaultdict(dict)
    where: dict[tuple[str, int, str], dict[str, str]] = defaultdict(dict)
    types = (
        "agent.step",
        "tool.call",
        "exec.started",
        "channel.message",
        "channel.read",
        "channel.snapshot",
        "run.finished",
    )
    cursor: int | None = None
    while page := await store.events(run_id, after=cursor, limit=PAGE, types=types):
        for e in page:
            cursor = e.seq
            origin.last_seq = max(origin.last_seq, e.seq)
            agent, d = e.agent_id or "", e.data
            match e.type:
                case "agent.step":
                    step = AgentStep.model_validate(d)
                    step_seqs[agent].append(e.seq)
                    seqs = replies.pop((agent, step.call_id), {})
                    step_calls[agent].append({c: seqs[c] for c in step.tool_calls if c in seqs})
                case "tool.call":
                    made = ToolCall.model_validate(d)
                    # A call no model made is a scripted agent's step.
                    if made.model_call is None:
                        actions[agent].append(e.seq)
                    else:
                        replies[agent, made.model_call][made.call_id] = e.seq
                case "exec.started":
                    call = d.get("call_id")
                    if isinstance(call, str):
                        execs[str(d.get("exec_id"))] = (agent, len(step_seqs[agent]), call)
                    elif d.get("phase") == "agent":
                        actions[agent].append(e.seq)
                case "channel.message" if d.get("call_id") is None:
                    actions[agent].append(e.seq)
                case "channel.read" if isinstance(d.get("call_id"), str):
                    through = d.get("through")
                    if isinstance(through, int):
                        key = (agent, len(step_seqs[agent]), str(d["call_id"]))
                        reads[key][str(d.get("channel"))] = through
                case "channel.snapshot" if str(d.get("exec_id")) in execs:
                    key = execs[str(d.get("exec_id"))]
                    changes = d.get("changes")
                    for c in changes if isinstance(changes, list) else []:
                        path = c.get("path") if isinstance(c, dict) else None
                        own = isinstance(c, dict) and c.get("agent") == key[0]
                        if own and isinstance(c, dict) and isinstance(path, str):
                            sha = c.get("sha256")
                            writes[key][path] = sha if isinstance(sha, str) else None
                            where[key][path] = str(d.get("channel"))
    origin.step_seqs = dict(step_seqs)
    origin.step_calls = dict(step_calls)
    origin.actions = dict(actions)
    for key in set(reads) | set(writes):
        agent, n, call = key
        origin.versions.setdefault(agent, {})[n, call] = ChannelVersions(
            seq=step_calls[agent][n - 1].get(call, 0) if n else 0,
            reads=dict(reads.get(key, {})),
            writes=dict(writes.get(key, {})),
            channels=dict(where.get(key, {})),
        )


@dataclass(frozen=True, slots=True)
class CutAt:
    """A cut of a run of several agents: after the origin's event `seq`, by when each agent
    had taken `steps[agent]` steps; `agent` is the one the cut names."""

    seq: int
    steps: dict[str, int]
    agent: str | None = None


def cut_at(origin: Origin, at: str) -> CutAt | int:
    """Where `--fork-at` cuts `origin`: `agent:N` after that agent's step N, `@seq` after an
    event of the origin, or a plain step N, the same for every agent, which only a run of one
    agent takes. Returns the step for a plain N. Raises RunError, also for `agent:N` past the
    last step that agent can replay. An agent the cut does not name that was in a step it
    cannot replay goes live from that step."""
    agents = [a.id for a in origin.bundle.scenario.agents]
    text = at.strip()
    if text.isdigit():
        if len(agents) > 1:
            raise RunError(
                f"run {origin.run_id} has {len(agents)} agents; fork at agent:N (after that"
                " agent's step N) or @seq (after an event of the run)"
            )
        return int(text)
    if text.startswith("@"):
        if not text[1:].isdigit():
            raise RunError(f"bad cut {at!r}: @ takes an event seq")
        seq = int(text[1:])
        if seq > origin.last_seq:
            raise RunError(f"run {origin.run_id} ends at event {origin.last_seq}")
        return CutAt(seq, _taken(origin, seq))
    name, sep, n = text.rpartition(":")
    if not sep or not n.isdigit() or name not in agents:
        known = ", ".join(agents)
        raise RunError(f"bad cut {at!r}: give agent:N with one of {known}, or @seq")
    step = int(n)
    seqs = origin.step_seqs.get(name, [])
    if step > len(seqs):
        raise RunError(f"agent {name} of run {origin.run_id} took {len(seqs)} steps")
    _check_whole(origin, name, step, f"{name}:")
    first = seqs[0] - 1 if seqs else origin.last_seq
    seq = first if step == 0 else seqs[step - 1]
    steps = _taken(origin, seq)
    steps[name] = step
    return CutAt(seq, steps, name)


def _agents(origin: Origin) -> list[Any]:
    """The origin's agents with their roles and scripts, which the public copy leaves out."""
    return list((origin.full_bundle or origin.bundle).scenario.agents)


def _taken(origin: Origin, seq: int) -> dict[str, int]:
    """The steps each agent replays of a cut after event `seq`: those it had taken by then, up
    to the last it can replay."""
    out: dict[str, int] = {}
    for agent in _agents(origin):
        if agent.script is not None:
            out[agent.id] = bisect_right(origin.actions.get(agent.id, []), seq)
        else:
            taken = bisect_right(origin.step_seqs.get(agent.id, []), seq)
            out[agent.id] = min(taken, origin.replayable.get(agent.id, 0))
    return out


def preview(origin: Origin, fork: Fork) -> list[str]:
    """What a fork's cut keeps of each agent, in words."""
    if fork.steps is None:
        return [f"each agent replays its first {fork.step} steps of {origin.run_id}"]
    head = f"cut of {origin.run_id} after event {fork.cut}"
    lines = [head + (f", at step {fork.at(fork.agent)} of {fork.agent}" if fork.agent else "")]
    for agent_id, n in fork.steps.items():
        taken = len(origin.step_seqs.get(agent_id, origin.actions.get(agent_id, [])))
        kept = fork.keep_others and agent_id != fork.agent
        after = "keeps its recorded answers after" if kept else "goes live after"
        lines.append(f"  {agent_id}: replays {n} of {taken} steps, {after}")
    return lines


def _schedule(value: object) -> list[tuple[int | None, Profile]]:
    if not isinstance(value, list):
        raise RunError("the run recorded no profile schedule")
    out: list[tuple[int | None, Profile]] = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise RunError(f"bad profile schedule entry {pair!r}")
        until, profile = pair
        out.append((until if isinstance(until, int) else None, Profile.model_validate(profile)))
    return out


CONNECTION = ("backend", "base_url", "base_url_env", "api_key_env")


def configured(models: ModelsConfig, recorded: Profile) -> Profile:
    """`recorded`, once this config has a profile of the same name that reaches the same
    endpoint with the same key variable. A run's log never decides where a key is sent."""
    try:
        mine = models.get(recorded.name)
    except ProfileError:
        raise RunError(
            f"the run used profile {recorded.name}, which this config does not have;"
            " fork with a profile"
        ) from None
    if any(getattr(mine, f) != getattr(recorded, f) for f in CONNECTION):
        raise RunError(
            f"profile {recorded.name} reaches another endpoint or key here than in the run;"
            " fork with a profile"
        )
    return recorded


def fork_of(
    origin: Origin,
    step: int,
    cut: CutAt | None = None,
    *,
    profile: str | None = None,
    keep_others: bool = False,
) -> Fork:
    """The fork of `origin` after `step`, or at `cut` in a run of several agents."""
    return Fork(
        origin=origin.run_id,
        step=step,
        profile=profile,
        steps=None if cut is None else dict(cut.steps),
        cut=None if cut is None else cut.seq,
        agent=None if cut is None else cut.agent,
        keep_others=keep_others,
    )


def fork_profiles(models: ModelsConfig, origin: Origin, fork: Fork) -> dict[str, Profile]:
    """The profile each agent uses once it is live, by agent id: the one the origin used
    after the agent's last replayed step, checked against `models`, or `fork.profile` with the
    agent's overrides."""
    out: dict[str, Profile] = {}
    for agent in origin.bundle.scenario.agents:
        if fork.profile is None:
            recorded = _at(origin.schedules[agent.id], fork.at(agent.id) + 1)
            out[agent.id] = configured(models, recorded)
        else:
            out[agent.id] = models.get(fork.profile).apply(agent.overrides)
    return out


def fork_agents(models: ModelsConfig, origin: Origin, fork: Fork) -> list[Profile]:
    """The profiles the agents of `fork` run, which its monitor is resolved with: those of the
    steps each agent replays, as recorded, then the one it goes live with. An agent that keeps
    its recorded answers may answer with any profile the origin used. Raises RunError or
    ProfileError."""
    replayed = fork_schedules(origin, fork)
    live = fork_profiles(models, origin, fork)
    out: list[Profile] = []
    for agent in origin.bundle.scenario.agents:
        kept = fork.keep_others and agent.id != fork.agent
        schedule = origin.schedules[agent.id] if kept else replayed[agent.id]
        out += [p for _, p in schedule]
        out.append(live[agent.id])
    return out


def fork_schedules(origin: Origin, fork: Fork) -> dict[str, list[tuple[int | None, Profile]]]:
    """Which profile made each step a fork replays, cut at the fork step."""
    out: dict[str, list[tuple[int | None, Profile]]] = {}
    for agent_id, schedule in origin.schedules.items():
        step = fork.at(agent_id)
        cut: list[tuple[int | None, Profile]] = []
        for until, profile in schedule:
            if until is not None and until < step:
                cut.append((until, profile))
                continue
            if step > 0:
                cut.append((step, profile))
            break
        out[agent_id] = cut
    return out


def _at(schedule: list[tuple[int | None, Profile]], step: int) -> Profile:
    for until, profile in schedule:
        if until is None or step <= until:
            return profile
    return schedule[-1][1]


def _replayed(origin: Origin, agent_id: str, step: int) -> dict[tuple[int, str], int]:
    """The origin seq of each tool call of the agent's first `step` steps, by step and call
    id."""
    calls = origin.step_calls.get(agent_id, [])[:step]
    return {(n, c): seq for n, seqs in enumerate(calls, 1) for c, seq in seqs.items()}


def check_fork(origin: Origin, step: int) -> None:
    """Raises RunError unless every agent of `origin` took `step` steps and can replay them."""
    for agent in origin.bundle.scenario.agents:
        taken = origin.steps.get(agent.id, 0)
        if not 0 <= step <= taken:
            raise RunError(
                f"agent {agent.id} of run {origin.run_id} took {taken} steps; fork at 0 to {taken}"
            )
        _check_whole(origin, agent.id, step, "step ")


def fork_step(origin: Origin) -> int:
    """Where a fork of a run of one agent cuts when it names no step: after the last step a
    fork can replay."""
    return min(origin.replayable.values(), default=0)


def _check_whole(origin: Origin, agent_id: str, step: int, name: str) -> None:
    """Raises RunError when a fork that replays `step` steps of the agent cannot replay the
    last of them. `name` goes before a step in the error: `step ` or `agent:`."""
    last = origin.replayable.get(agent_id, 0)
    if step <= last:
        return
    if origin.running and agent_id not in origin.ended:
        why = "cannot be replayed yet: its tool calls have not returned"
    else:
        who = "the agent" if origin.running else "the run"
        why = f"cannot be replayed: {who} ended before its tool calls returned"
    where = f"{name}{last} or before" if last else f"{name}0 to make every step again live"
    raise RunError(
        f"step {step} of agent {agent_id} {why}, so they have no recorded output; fork at {where}"
    )


def planner(bench: Bench, origin: Origin, fork: Fork, *, source: EventStore | None = None):
    """Builds each agent's replay plan once the fork's run exists. `source` holds the origin
    when it is not in the bench's store."""

    async def plans(run_id: str) -> dict[str, ReplayPlan]:
        recording = await Recording.load(source or bench.store, origin.run_id)
        client = ModelClient(bench.store, run_id, bench.models, bus=bench.bus, replay=recording)
        orders = {a.id: _replayed(origin, a.id, fork.at(a.id)) for a in _agents(origin)}
        actions = {
            a.id: origin.actions.get(a.id, [])[: fork.at(a.id)]
            for a in _agents(origin)
            if a.script is not None
        }
        turnstile = None
        if fork.cut is not None:
            seqs = [s for order in orders.values() for s in order.values()]
            turnstile = Turnstile([*seqs, *(s for a in actions.values() for s in a)])
        ran = budgets.apply(origin.bundle.scenario, origin.budget)
        caps = {a.id: a.budget for a in ran.agents}
        out: dict[str, ReplayPlan] = {}
        for agent in origin.bundle.scenario.agents:
            step = fork.at(agent.id)
            injects = origin.injects.get(agent.id, {})
            order = orders[agent.id]
            versions = origin.versions.get(agent.id, {})
            out[agent.id] = ReplayPlan(
                client=client,
                fork_at=step,
                schedule=origin.schedules[agent.id],
                outputs=origin.outputs.get(agent.id, {}),
                injects={s: list(t) for s, t in injects.items() if s < step},
                strict=fork.strict,
                inject=fork.inject if fork.agent in (None, agent.id) else None,
                turnstile=turnstile,
                order=order,
                versions={k: v for k, v in versions.items() if k in order},
                keep=fork.keep_others and agent.id != fork.agent,
                actions=actions.get(agent.id, []),
                budget=caps[agent.id],
                limits=ran.limits,
            )
        return out

    return plans
