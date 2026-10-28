from collections import defaultdict
from dataclasses import dataclass, field

from aims.agent import RecordedOutput, ReplayPlan
from aims.events import AgentInput, BlobRef, Content, EventStore, RunStarted, ToolResult
from aims.models import ModelClient, ModelsConfig, Profile, ProfileError, Recording
from aims.run.runner import Bench, Fork, RunError, Texts
from aims.scenario import Bundle, ScenarioError, from_snapshot

PAGE = 1000


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


async def _text(store: EventStore, run_id: str, content: Content) -> str:
    if isinstance(content, BlobRef):
        return (await store.get_blob(run_id, content.sha256)).decode()
    return content


async def load_origin(store: EventStore, run_id: str) -> Origin:
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
    origin = Origin(run_id, bundle, schedules, texts)
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
    return origin


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
            recorded = _at(origin.schedules[agent.id], fork.step + 1)
            out[agent.id] = configured(bench.models, recorded)
        else:
            out[agent.id] = bench.models.get(fork.profile).apply(agent.overrides)
    return out


def fork_schedules(origin: Origin, fork: Fork) -> dict[str, list[tuple[int | None, Profile]]]:
    """Which profile made each step a fork replays, cut at the fork step."""
    out: dict[str, list[tuple[int | None, Profile]]] = {}
    for agent_id, schedule in origin.schedules.items():
        cut: list[tuple[int | None, Profile]] = []
        for until, profile in schedule:
            if until is not None and until < fork.step:
                cut.append((until, profile))
                continue
            if fork.step > 0:
                cut.append((fork.step, profile))
            break
        out[agent_id] = cut
    return out


def _at(schedule: list[tuple[int | None, Profile]], step: int) -> Profile:
    for until, profile in schedule:
        if until is None or step <= until:
            return profile
    return schedule[-1][1]


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
        out: dict[str, ReplayPlan] = {}
        for agent in origin.bundle.scenario.agents:
            injects = origin.injects.get(agent.id, {})
            out[agent.id] = ReplayPlan(
                client=client,
                fork_at=fork.step,
                schedule=origin.schedules[agent.id],
                outputs=origin.outputs.get(agent.id, {}),
                injects={s: list(t) for s, t in injects.items() if s < fork.step},
                strict=fork.strict,
                inject=fork.inject,
            )
        return out

    return plans
