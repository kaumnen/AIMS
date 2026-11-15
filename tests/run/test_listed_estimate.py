"""Estimates of profiles that name a row of LiteLLM's price map and set no price: the list
price serves the estimate only, a cost cap it cannot hold leaves the ceiling, and a lookup that
fails leaves the profile unpriced with the reason."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cp import CP, control_plane
from scripted import profile
from test_estimate import (
    CONFIG,
    STEP,
    output_for,
    record,
    stepped,
    watcher,
    write,
)
from typer.testing import CliRunner

from aims.cli import app
from aims.events import AgentFinished, Draft, LocalEventStore, ModelUsage
from aims.models import listprice as lp
from aims.models.listprice import PriceLists
from aims.monitor.texts import resolve
from aims.prompts import Prompts
from aims.run import scoring
from aims.run.estimate import (
    History,
    ListedPrice,
    estimate_fork,
    estimate_review,
    estimate_runs,
    history,
    needs_confirm,
    reviews_confirm,
)
from aims.run.fork import Origin, fork_of
from aims.scenario import load as load_scenario

pytestmark = pytest.mark.anyio

KEY = "vendor-x/listed-model"
ROWS: dict[str, Any] = {
    KEY: {
        "mode": "chat",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
        "cache_read_input_token_cost": 1e-7,
    },
    "vendor-x/watch-model": {
        "mode": "chat",
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 4e-6,
        "cache_read_input_token_cost": 2e-7,
    },
    "vendor-x/tiered": {
        "mode": "chat",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
        "input_cost_per_token_above_200k_tokens": 2e-6,
    },
} | {f"filler/model-{i}": {"mode": "chat"} for i in range(lp.MIN_ROWS)}

LISTED = """
[profiles.listed]
backend = "chat"
base_url = "https://api.vendor-x.test/v1"
model = "listed-model"
weights = "listed-weights"
reasoning = "full"
effort = "medium"
context_tokens = 100000
max_output_tokens = 1000
price_key = "vendor-x/listed-model"

[profiles.missing]
extends = "listed"
model = "missing-model"
weights = "missing-weights"
price_key = "vendor-x/nothing"

[profiles.tiered]
extends = "listed"
model = "tiered-model"
weights = "tiered-weights"
price_key = "vendor-x/tiered"

[profiles.both]
extends = "agent"
model = "both-model"
weights = "both-weights"
price_key = "vendor-x/listed-model"

[profiles.listed-watch]
extends = "listed"
base_url = "https://api.vendor-y.test/v1"
model = "watch-listed"
weights = "watch-listed-weights"
price_key = "vendor-x/watch-model"
"""
CONFIG_LISTED = CONFIG + LISTED
OPEN = """
[profiles.open]
backend = "chat"
base_url = "https://api.vendor-x.test/v1"
model = "open-model"
weights = "open-weights"
reasoning = "full"
price_key = "vendor-x/listed-model"
"""
WATCHED = CONFIG_LISTED.replace(
    'sweep = "watcher"\njudge = "watcher"', 'sweep = "listed-watch"\njudge = "listed-watch"'
)
LINE = (
    "Profile listed: LiteLLM list price for vendor-x/listed-model, fetched {day}: $1.00 input,"
    " $0.10 cached, $2.00 output per million tokens."
)
CAP = " A cost cap holds on its calls only if the provider reports each call's cost."


class Source:
    """LiteLLM's map as a test serves it: `answers` in turn, the map after them."""

    def __init__(self, *answers: httpx2.Response) -> None:
        self.seen: list[httpx2.Request] = []
        self.answers = list(answers)

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            if self.answers:
                return self.answers.pop(0)
            return httpx2.Response(200, content=json.dumps(ROWS).encode(), headers={"etag": '"v1"'})

        return httpx2.MockTransport(handle)

    def lists(self, path: Path, clock: Any = None) -> PriceLists:
        kw = {} if clock is None else {"clock": clock}
        return PriceLists(path / "prices.json.gz", transport=self.transport(), **kw)


async def run(tmp_path: Path, lists: PriceLists | None, *, config: str = CONFIG_LISTED, **kw: Any):
    budget = kw.pop("budget_text", "{steps: 10}")
    extra = kw.pop("extra", "")
    preset = kw.pop("preset", "signals")
    settings, path = write(tmp_path, budget=budget, config=config, extra=extra, preset=preset)
    found = kw.pop("found", History())
    return settings, await estimate_runs(found, settings, [load_scenario(path)], lists=lists, **kw)


async def test_a_profile_with_a_key_and_no_price_counts_at_its_list_price(tmp_path: Path) -> None:
    source = Source()
    lists = source.lists(tmp_path)
    settings, e = await run(tmp_path, lists, profile="listed")
    assert len(source.seen) == 1
    assert e.unpriced == []
    assert e.parts[0].agents.priced
    # the step cap bounds the cost at the list price
    assert e.parts[0].agents.ceiling_usd == pytest.approx(10 * STEP)
    [entry] = e.listed
    day = f"{entry.checked:%Y-%m-%d}"
    assert entry.model_copy(update={"checked": None}) == ListedPrice.model_construct(
        profile="listed",
        source="LiteLLM",
        key=KEY,
        checked=None,
        input_usd=1.0,
        cached_usd=0.1,
        output_usd=2.0,
        cached_listed=True,
        tier_from=None,
        problem=None,
        text=f"LiteLLM list price for {KEY}, fetched {day}",
    )
    assert LINE.format(day=day) + CAP in e.lines
    # the run keeps the profile as it is
    assert settings.models.get("listed").price is None


async def test_past_runs_of_a_listed_profile_are_priced_at_the_list(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    bundle = load_scenario(path)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        await record(store, settings, bundle, 0.0, profile="listed", usage=None)
        found = await history(store)
    unpriced = await estimate_runs(found, settings, [bundle], profile="listed")
    assert not unpriced.parts[0].agents.priced
    assert unpriced.unpriced == ["listed"]
    async with await LocalEventStore.open(tmp_path / "runs2") as store:
        tokens = ModelUsage(input_tokens=0, cached_tokens=0, output_tokens=output_for(0.3))
        await record(store, settings, bundle, 0.0, profile="listed", usage=tokens)
        found = await history(store)
    e = await estimate_runs(
        found, settings, [bundle], profile="listed", lists=Source().lists(tmp_path)
    )
    assert e.parts[0].agents.typical_usd == pytest.approx(0.3)
    assert e.parts[0].agents.runs == 1


async def test_a_cost_cap_on_a_listed_profile_is_left_out_of_the_ceiling(tmp_path: Path) -> None:
    lists = Source().lists(tmp_path)
    _, own = await run(tmp_path, lists, budget_text="{steps: null, cost_usd: 5}", profile="agent")
    assert own.parts[0].agents.ceiling_usd == pytest.approx(5 + STEP)
    assert not own.confirm
    _, e = await run(tmp_path, lists, budget_text="{steps: null, cost_usd: 5}", profile="listed")
    assert e.parts[0].agents.ceiling_usd is None
    assert not e.parts[0].agents.cap_holds
    assert e.lines[1:3] == [
        "Agents: no past runs to go by. No ceiling: its cost cap holds only if the provider"
        " reports each call's cost.",
        "Monitor: signals only, no model calls.",
    ]
    assert "Total: no ceiling." in e.lines
    assert e.confirm
    assert needs_confirm(e) == "no cap bounds the cost and no past runs give an estimate"
    assert e.warnings == [
        "no cap bounds the cost of agent a: its profile listed has only a list price, so a cost"
        " cap holds only if the provider reports each call's cost; set a price in the profile,"
        " or a step cap"
    ]
    _, run_cap = await run(
        tmp_path,
        lists,
        budget_text="{steps: null, cost_usd: null}",
        extra="limits: {cost_usd: 3}\n",
        profile="listed",
    )
    assert run_cap.parts[0].agents.ceiling_usd is None
    _, stepped_cap = await run(
        tmp_path, lists, budget_text="{steps: 4, cost_usd: 5}", profile="listed"
    )
    assert stepped_cap.parts[0].agents.ceiling_usd == pytest.approx(4 * STEP)
    assert not stepped_cap.warnings


async def test_a_listed_monitor_has_no_ceiling_and_asks(tmp_path: Path) -> None:
    lists = Source().lists(tmp_path)
    _, plain = await run(tmp_path, None, config=WATCHED, preset="standard", profile="agent")
    assert plain.parts[0].monitor is not None
    assert not plain.parts[0].monitor.priced
    assert plain.unpriced == ["listed-watch"]
    _, e = await run(tmp_path, lists, config=WATCHED, preset="standard", profile="agent")
    monitor = e.parts[0].monitor
    assert monitor is not None
    assert monitor.priced
    assert monitor.ceiling_usd is None
    assert not monitor.cap_holds
    assert e.parts[0].agents.cap_holds
    assert e.lines[2] == (
        "Monitor: no past runs to go by. No ceiling: its cost cap holds only if the provider"
        " reports each call's cost."
    )
    # the cap in force is still the one the run has
    assert e.parts[0].caps.monitor_cost_usd == plain.parts[0].caps.monitor_cost_usd
    assert e.total.ceiling_usd is None
    assert e.confirm
    assert [x.profile for x in e.listed] == ["listed-watch"]
    assert not any("pays for sweeps" in w for w in e.warnings)


async def test_a_profile_with_its_own_price_keeps_it_and_nothing_is_read(tmp_path: Path) -> None:
    source = Source()
    _, e = await run(tmp_path, source.lists(tmp_path), profile="both")
    _, agent = await run(tmp_path, None, profile="agent")
    assert source.seen == []
    assert e.listed == []
    assert e.parts[0].agents == agent.parts[0].agents
    assert not (tmp_path / "prices.json.gz").exists()


async def test_a_key_the_map_lacks_leaves_the_profile_unpriced_and_says_why(
    tmp_path: Path,
) -> None:
    _, e = await run(tmp_path, Source().lists(tmp_path), profile="missing")
    assert e.unpriced == ["missing"]
    assert e.unlisted == {"missing": "LiteLLM lists no key vendor-x/nothing"}
    assert (
        "warning: profile missing has no price and LiteLLM lists no key vendor-x/nothing, so its"
        " calls are not counted"
    ) in e.lines


async def test_no_map_at_hand_leaves_the_profile_unpriced_and_says_why(tmp_path: Path) -> None:
    _, e = await run(tmp_path, Source(httpx2.Response(503)).lists(tmp_path), profile="listed")
    assert e.unpriced == ["listed"]
    why = f"no copy of the LiteLLM price map is at hand ({lp.URL} answered 503)"
    assert e.unlisted == {"listed": why}
    assert f"warning: profile listed has no price and {why}, so its calls are not counted" in (
        e.lines
    )


async def test_a_lookup_that_fails_in_any_way_never_fails_the_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: PriceLists) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(PriceLists, "get", broken)
    _, e = await run(tmp_path, PriceLists(tmp_path / "x"), profile="listed")
    assert e.unpriced == ["listed"]
    assert e.unlisted["listed"] == (
        "no copy of the LiteLLM price map is at hand (RuntimeError: boom)"
    )


async def test_a_copy_that_could_not_be_read_again_serves_with_its_date(tmp_path: Path) -> None:
    now = [1_763_171_160.0]
    lists = Source(
        httpx2.Response(200, content=json.dumps(ROWS).encode()), httpx2.Response(503)
    ).lists(tmp_path, clock=lambda: now[0])
    await lists.get()
    now[0] += lp.FRESH_SECONDS + 1
    _, e = await run(tmp_path, lists, profile="listed")
    [entry] = e.listed
    assert entry.problem == f"{lp.URL} answered 503"
    assert f"{entry.checked:%Y-%m-%d}" == "2025-11-15"
    assert (
        LINE.format(day="2025-11-15")
        + f" The map could not be read again ({lp.URL} answered 503), so this is its copy"
        " fetched 2025-11-15." + CAP
    ) in e.lines


async def test_a_row_without_a_cached_price_or_with_tiers_says_so(tmp_path: Path) -> None:
    _, e = await run(tmp_path, Source().lists(tmp_path), profile="tiered")
    [entry] = e.listed
    assert (entry.cached_listed, entry.tier_from) == (False, 200_000)
    assert entry.cached_usd == entry.input_usd == 1.0
    line = next(x for x in e.lines if x.startswith("Profile tiered:"))
    assert "It lists no cached input price, so cached input counts at the input price." in line
    assert "It lists a higher price past 200,000 prompt tokens, which the estimate leaves out." in (
        line
    )


async def test_a_fork_counts_its_live_steps_at_the_list_price(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    bundle = load_scenario(path)
    listed = settings.models.get("listed")
    origin = Origin(
        run_id="origin",
        bundle=bundle,
        schedules={"a": [(None, listed)]},
        texts={},
        steps={"a": 10},
    )
    plain = await estimate_fork(History(), settings, origin, fork_of(origin, 5))
    assert plain.unpriced == ["listed"]
    e = await estimate_fork(
        History(), settings, origin, fork_of(origin, 5), lists=Source().lists(tmp_path)
    )
    assert [x.profile for x in e.listed] == ["listed"]
    # the five steps left at the list price
    assert e.parts[0].agents.ceiling_usd == pytest.approx(5 * STEP)


async def test_a_review_by_a_listed_monitor_has_no_cap_and_asks(tmp_path: Path) -> None:
    settings, path = write(tmp_path, preset="standard", config=WATCHED)
    bundle = load_scenario(path)
    m = watcher(settings, bundle, preset="standard")
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        origin = await stepped(store, "s", 10)
        plain = await estimate_review(store, origin, m, "full")
        e = await estimate_review(store, origin, m, "full", lists=Source().lists(tmp_path))
    assert not plain.priced
    assert plain.lines == [
        "Review: standard monitor; its models have no price, so its cost is not counted."
    ]
    assert (e.priced, e.ceiling_usd, e.covers) == (True, None, None)
    assert e.confirm
    assert reviews_confirm([e]) == "no cap bounds the cost and no past reviews give an estimate"
    assert e.lines[0] == (
        "Review: standard monitor, over 10 agent steps. No past reviews to go by. Its spend cap"
        " holds only if the provider reports each call's cost."
    )
    assert [x.profile for x in e.listed] == ["listed-watch"]
    assert e.lines[1].startswith(
        "Profile listed-watch: LiteLLM list price for vendor-x/watch-model"
    )


async def test_a_score_counts_its_ceiling_at_the_classifier_s_list_price(tmp_path: Path) -> None:
    from test_scoring import ANSWER_TEXT, FACTS, sealed

    texts = resolve(Prompts())
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        drafts = [Draft(AgentFinished(reason="submitted", answer=ANSWER_TEXT), "a")]
        run_id = await sealed(store, drafts, facts=FACTS)
        priced = profile(name="cheap", max_output_tokens=1000)
        listed = profile(name="cheap", max_output_tokens=1000, price=None, price_key=KEY)
        own = await scoring.estimate_score(store, run_id, priced, texts)
        bare = await scoring.estimate_score(store, run_id, listed, texts)
        e = await scoring.estimate_score(
            store, run_id, listed, texts, lists=Source().lists(tmp_path)
        )
    assert bare.ceiling_usd is None
    assert e.ceiling_usd == pytest.approx(own.ceiling_usd)
    assert [x.key for x in e.listed] == [KEY]
    assert e.lines[1].startswith(f"Profile cheap: LiteLLM list price for {KEY}")
    assert own.listed == []
    assert listed.price is None


def test_aims_estimate_prints_the_list_price_from_the_kept_copy(tmp_path: Path) -> None:
    lists = PriceLists(transport=Source().transport())
    asyncio.run(lists.get())
    assert lists.path.is_file()
    _, path = write(tmp_path, config=CONFIG_LISTED)
    config = ["--config", str(tmp_path / "aims.toml"), "--runs-dir", str(tmp_path / "runs")]
    out = CliRunner().invoke(app, ["estimate", str(path), *config, "--profile", "listed"])
    assert out.exit_code == 0, out.output
    assert f"Profile listed: LiteLLM list price for {KEY}, fetched" in out.stdout
    assert "has no price" not in out.stdout


async def test_the_api_estimate_carries_the_list_price(tmp_path: Path) -> None:
    async with control_plane(tmp_path) as cp:
        await _api(cp, tmp_path)


async def _api(cp: CP, tmp_path: Path) -> None:
    cp.project.write_text(
        cp.project.read_text() + LISTED.replace('extends = "agent"', 'extends = "listed"')
    )
    cp.project.write_text(cp.project.read_text() + OPEN)
    cp.state.prices = Source().lists(tmp_path)
    async with cp.client(cp.operator) as c:
        # only the agent's cost cap bounds a profile with no context size, and it cannot hold
        scenario = {"source": "project", "name": "control"}
        start = {"scenario": scenario, "monitor": "signals", "profile": "open"}
        r = await c.post("/api/runs", json=start)
        assert (r.status_code, r.json()["code"]) == (409, "confirm"), r.text
        assert r.json()["detail"] == (
            "no cap bounds the cost and no past runs give an estimate; set confirm to start anyway"
        )
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/scenarios/project/control/estimate", params={"profile": "listed"})
        assert r.status_code == 200, r.text
        data = r.json()
    assert [x["key"] for x in data["listed"]] == [KEY]
    assert data["unlisted"] == {}
