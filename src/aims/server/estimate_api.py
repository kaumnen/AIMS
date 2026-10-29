"""Cost on `/api`: the estimate the run launcher shows before a run, and the spend per provider.
Both read the runs this control plane holds, through the same code as `aims estimate` and
`aims spend`."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from aims.models import ProfileError
from aims.monitor.config import MonitorConfigError, Preset
from aims.prompts import TextError
from aims.run.estimate import Estimate, History, ProviderCredit, estimate_runs, history, spend
from aims.run.estimate import spend_lines as describe_spend
from aims.scenario import ScenarioError, load
from aims.server.api import Out, SourcesDep, State, Viewer, scenario_problems
from aims.server.problems import ProblemException, responses
from aims.server.state import ServerState

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))
_cache: dict[int, tuple[float, int, History]] = {}


async def _history(state: ServerState) -> History:
    """The runs' history, read again once a run changed."""
    stamp = (state.started, state.changes.version)
    kept = _cache.get(id(state))
    if kept is not None and kept[:2] == stamp:
        return kept[2]
    found = await history(state.store)
    _cache[id(state)] = (*stamp, found)
    return found


@router.get("/scenarios/{source}/{name}/estimate", tags=["scenarios"], responses=responses(404))
async def estimate_scenario(
    source: str,
    name: str,
    principal: Viewer,
    state: State,
    sources: SourcesDep,
    variant: str | None = None,
    variants: Literal["all"] | None = None,
    samples: Annotated[int, Query(ge=1, le=100)] = 1,
    profile: str | None = None,
    monitor: Preset | None = None,
) -> Estimate:
    """What `samples` runs of the scenario cost, of one variant or with `variants=all` of each:
    what the caps allow, and the typical and high cost of past runs."""
    if variant is not None and variants is not None:
        raise ProblemException(422, "Invalid request", "give variant or variants, not both")
    _, path = sources.scenario(source, name)
    file = path / "scenario.yaml"
    settings = state.config.settings()
    try:
        names: list[str | None] = [variant]
        if variants is not None:
            names = list(load(file).scenario.variants)
            if not names:
                raise ProblemException(422, "Invalid request", "the scenario has no variants")
        bundles = [load(file, n) for n in names]
    except ScenarioError as e:
        raise ProblemException(
            422, "Scenario does not load", code="scenario", errors=scenario_problems(e)
        ) from None
    try:
        return estimate_runs(
            await _history(state),
            settings,
            bundles,
            profile=profile,
            preset=monitor,
            samples=samples,
        )
    except (ProfileError, MonitorConfigError, TextError) as e:
        raise ProblemException(422, "Cannot estimate", str(e), code="estimate") from None


class Spend(Out):
    """What each provider was paid in the runs on record, against the `[credits]` of the
    config files; `lines` says it in words."""

    providers: list[ProviderCredit]
    lines: list[str]


@router.get("/spend", tags=["runs"])
async def get_spend(principal: Viewer, state: State) -> Spend:
    credits = spend(await _history(state), state.config.settings().credits)
    return Spend(providers=credits, lines=describe_spend(credits))
