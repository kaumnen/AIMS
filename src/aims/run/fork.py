import hashlib
import json
import logging
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aims.agent import ChannelVersions, RecordedOutput, ReplayPlan, Turnstile
from aims.events import (
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

PAGE = 1000


log = logging.getLogger("aims.run")


@dataclass(slots=True)
class Origin:
    """What a fork needs from the run it forks: the scenario as recorded, each agent's profiles
    and texts, its step count, the tool outputs it was given and the messages injected."""

    run_id: str
    bundle: Bundle
    schedules: dict[str, list[tuple[int | None, Profile]]]
    texts: dict[str, Texts]
    steps: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, dict[str, RecordedOutput]] = field(default_factory=dict)
    injects: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    full_bundle: Bundle | None = None
    truth: dict[str, Any] | None = None
    step_seqs: dict[str, list[int]] = field(default_factory=dict)
    step_calls: dict[str, list[list[str]]] = field(default_factory=dict)
    call_seqs: dict[str, dict[str, int]] = field(default_factory=dict)
    actions: dict[str, list[int]] = field(default_factory=dict)
    versions: dict[str, ChannelVersions] = field(default_factory=dict)
    restored: dict[str, bytes] = field(default_factory=dict)
    last_seq: int = -1

    @property
    def seed(self) -> bytes | None:
        """The canary seed of the origin, which its forks reuse."""
        return None if self.truth is None else bytes.fromhex(self.truth["seed"])

    @property
    def inherited(self) -> list[dict[str, Any]] | None:
        return None if self.truth is None else self.truth.get("inherited")


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
    return made.bundle.digest == origin.bundle.digest


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


async def load_origin(store: EventStore, run_id: str, *, full: bool = True) -> Origin:
    if await store.run(run_id) is None:
        raise RunError(f"unknown run {run_id}")
    first = await store.events(run_id, limit=1)
    started = first[0].payload() if first else None
    if not isinstance(started, RunStarted):
        raise RunError(f"run {run_id} has no run.started")
    config = started.config
    snapshot = config.get("scenario")
    agents = config.get("agents")
    if not isinstance(snapshot, dict) or not isinstance(agents, dict):
        raise RunError(f"run {run_id} was not started by `aims run`")
    blobs: dict[str, bytes] = {}
    for name in ("file", "files", "verifier", "context"):
        ref = snapshot.get(name)
        if isinstance(ref, dict):
            sha = str(ref["sha256"])
            blobs[sha] = await store.get_blob(run_id, sha)
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
            restored = truth.restore(snapshot, blobs, record).full_bundle
        except (KeyError, ValueError, OSError) as e:
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
    origin = Origin(run_id, bundle, schedules, texts, full_bundle=restored, truth=record)
    steps: dict[str, int] = defaultdict(int)
    outputs: dict[str, dict[str, RecordedOutput]] = defaultdict(dict)
    injects: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    types = ("agent.step", "tool.result", "agent.input")
    cursor: int | None = None
    while page := await store.events(run_id, after=cursor, limit=PAGE, types=types):
        for event in page:
            cursor = event.seq
            agent = event.agent_id or ""
            payload = event.payload()
            if event.type == "agent.step":
                steps[agent] += 1
            elif isinstance(payload, ToolResult):
                text = await _text(store, run_id, payload.output)
                outputs[agent][payload.call_id] = RecordedOutput(text, payload.error)
            elif isinstance(payload, AgentInput) and payload.kind == "inject":
                injects[agent][payload.step].append(await _text(store, run_id, payload.text))
    origin.steps = dict(steps)
    origin.outputs = dict(outputs)
    origin.injects = {a: dict(v) for a, v in injects.items()}
    await _order(store, run_id, origin)
    for c in started.channels:
        if c.restored is not None:
            origin.restored[c.name] = await store.get_blob(run_id, c.restored.sha256)
    return origin


async def _order(store: EventStore, run_id: str, origin: Origin) -> None:
    """When each agent took each step and tool call in the origin, a scripted agent's steps,
    and what each tool call met of the channels."""
    step_seqs: dict[str, list[int]] = defaultdict(list)
    step_calls: dict[str, list[list[str]]] = defaultdict(list)
    call_seqs: dict[str, dict[str, int]] = defaultdict(dict)
    actions: dict[str, list[int]] = defaultdict(list)
    execs: dict[str, tuple[str, str]] = {}
    reads: dict[str, dict[str, int]] = defaultdict(dict)
    writes: dict[str, dict[str, str | None]] = defaultdict(dict)
    where: dict[str, dict[str, str]] = defaultdict(dict)
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
                    step_calls[agent].append(list(step.tool_calls))
                case "tool.call":
                    made = ToolCall.model_validate(d)
                    call_seqs[agent][made.call_id] = e.seq
                    # A call no model made is a scripted agent's step.
                    if made.model_call is None:
                        actions[agent].append(e.seq)
                case "exec.started":
                    call = d.get("call_id")
                    if isinstance(call, str):
                        execs[str(d.get("exec_id"))] = (call, agent)
                    elif d.get("phase") == "agent":
                        actions[agent].append(e.seq)
                case "channel.message" if d.get("call_id") is None:
                    actions[agent].append(e.seq)
                case "channel.read" if isinstance(d.get("call_id"), str):
                    through = d.get("through")
                    if isinstance(through, int):
                        reads[str(d["call_id"])][str(d.get("channel"))] = through
                case "channel.snapshot" if str(d.get("exec_id")) in execs:
                    call, writer = execs[str(d.get("exec_id"))]
                    changes = d.get("changes")
                    for c in changes if isinstance(changes, list) else []:
                        path = c.get("path") if isinstance(c, dict) else None
                        own = isinstance(c, dict) and c.get("agent") == writer
                        if own and isinstance(c, dict) and isinstance(path, str):
                            sha = c.get("sha256")
                            writes[call][path] = sha if isinstance(sha, str) else None
                            where[call][path] = str(d.get("channel"))
    origin.step_seqs = dict(step_seqs)
    origin.step_calls = dict(step_calls)
    origin.call_seqs = dict(call_seqs)
    origin.actions = dict(actions)
    seqs = {c: s for calls in call_seqs.values() for c, s in calls.items()}
    for call in set(reads) | set(writes):
        origin.versions[call] = ChannelVersions(
            seq=seqs.get(call, 0),
            reads=dict(reads.get(call, {})),
            writes=dict(writes.get(call, {})),
            channels=dict(where.get(call, {})),
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
    agent takes. Returns the step for a plain N. Raises RunError."""
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
    first = seqs[0] - 1 if seqs else origin.last_seq
    seq = first if step == 0 else seqs[step - 1]
    steps = _taken(origin, seq)
    steps[name] = step
    return CutAt(seq, steps, name)


def _agents(origin: Origin) -> list[Any]:
    """The origin's agents with their roles and scripts, which the public copy leaves out."""
    return list((origin.full_bundle or origin.bundle).scenario.agents)


def _taken(origin: Origin, seq: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for agent in _agents(origin):
        if agent.script is not None:
            out[agent.id] = bisect_right(origin.actions.get(agent.id, []), seq)
        else:
            out[agent.id] = bisect_right(origin.step_seqs.get(agent.id, []), seq)
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


def live_profiles(models: ModelsConfig, origin: Origin, step: int) -> dict[str, Profile]:
    """The profiles the origin's agents used right after `step`, checked against `models`."""
    out: dict[str, Profile] = {}
    for agent in origin.bundle.scenario.agents:
        p = configured(models, _at(origin.schedules[agent.id], step + 1))
        out[p.name] = p
    return out


def fork_profiles(bench: Bench, origin: Origin, fork: Fork) -> dict[str, Profile]:
    """The profile each agent uses once it is live: the one the origin used next, checked
    against the bench's config, or `fork.profile`."""
    out: dict[str, Profile] = {}
    for agent in origin.bundle.scenario.agents:
        if fork.profile is None:
            recorded = _at(origin.schedules[agent.id], fork.at(agent.id) + 1)
            out[agent.id] = configured(bench.models, recorded)
        else:
            out[agent.id] = bench.models.get(fork.profile).apply(agent.overrides)
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


def _replayed(origin: Origin, agent_id: str, step: int) -> dict[str, int]:
    """The origin seq of each tool call of the agent's first `step` steps."""
    seqs = origin.call_seqs.get(agent_id, {})
    calls = origin.step_calls.get(agent_id, [])[:step]
    return {c: seqs[c] for cs in calls for c in cs if c in seqs}


def check_fork(origin: Origin, step: int) -> None:
    for agent in origin.bundle.scenario.agents:
        taken = origin.steps.get(agent.id, 0)
        if not 0 <= step <= taken:
            raise RunError(
                f"agent {agent.id} of run {origin.run_id} took {taken} steps; fork at 0 to {taken}"
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
        out: dict[str, ReplayPlan] = {}
        for agent in origin.bundle.scenario.agents:
            step = fork.at(agent.id)
            injects = origin.injects.get(agent.id, {})
            order = orders[agent.id]
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
                versions={c: v for c, v in origin.versions.items() if c in order},
                keep=fork.keep_others and agent.id != fork.agent,
                actions=actions.get(agent.id, []),
            )
        return out

    return plans
