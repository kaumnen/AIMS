"""Runs, forks, reviews, scores and their estimates of profiles that name a row of LiteLLM's
price map and set no price: the list price goes on the profiles the work gets, so its caps hold
and the estimate counts them; a lookup that fails leaves the profile unpriced with the reason."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from cp import CP, control_plane
from scripted import models, profile
from test_estimate import (
    CONFIG,
    STEP,
    output_for,
    record,
    stepped,
    watcher,
    write,
)
from test_server import FakeRunner
from typer.testing import CliRunner

from aims.cli import app
from aims.config import Settings
from aims.events import AgentFinished, Draft, LocalEventStore, ModelUsage
from aims.models import listprice as lp
from aims.models.listprice import PriceLists
from aims.models.profiles import Price, Profile
from aims.monitor.offline import monitor_offline, monitored, offline_monitor, origin_agents
from aims.monitor.setup import ResolvedMonitor
from aims.monitor.texts import resolve
from aims.prompts import Prompts
from aims.report.listed import RecordedPrice, listed_costs, recorded_prices
from aims.run import scoring
from aims.run.estimate import (
    History,
    ListedPrice,
    estimate_fork,
    estimate_review,
    estimate_runs,
    history,
    priced_fork,
    priced_review,
    priced_runs,
    reviews_confirm,
)
from aims.run.fork import Origin, fork_of, fork_profiles
from aims.run.job import Job, fork_jobs, runner_for
from aims.run.runner import Bench
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
EXTRACT = """
[profiles.extract-only]
backend = "chat"
base_url = "https://api.vendor-x.test/v1"
model = "extract-model"
weights = "extract-weights"
reasoning = "full"
price_key = "vendor-x/tiered"
"""
WATCHED = CONFIG_LISTED.replace(
    'sweep = "watcher"\njudge = "watcher"', 'sweep = "listed-watch"\njudge = "listed-watch"'
)
LINE = (
    "Profile listed: LiteLLM list price for vendor-x/listed-model, fetched {day}: $1.00 input,"
    " $0.10 cached, $2.00 output per million tokens."
)
PRICE = Price(input=1.0, cached=0.1, output=2.0)
WATCH_PRICE = Price(input=2.0, cached=0.2, output=4.0)


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
    bundles = [load_scenario(path)]
    laid, listing = await priced_runs(settings, bundles, profile=kw.get("profile"), lists=lists)
    return laid, await estimate_runs(found, laid, bundles, listing=listing, **kw)


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
    assert LINE.format(day=day) in e.lines
    # the run gets the list price, marked as one
    laid = settings.models.get("listed")
    assert laid.price == PRICE
    assert laid.listed is not None
    assert (laid.listed.checked, laid.listed.cached_listed) == (entry.checked, True)


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
    laid, listing = await priced_runs(
        settings, [bundle], profile="listed", lists=Source().lists(tmp_path)
    )
    e = await estimate_runs(found, laid, [bundle], profile="listed", listing=listing)
    assert e.parts[0].agents.typical_usd == pytest.approx(0.3)
    assert e.parts[0].agents.runs == 1


async def test_a_cost_cap_on_a_listed_profile_holds_as_on_its_own_price(tmp_path: Path) -> None:
    lists = Source().lists(tmp_path)
    _, own = await run(tmp_path, lists, budget_text="{steps: null, cost_usd: 5}", profile="agent")
    assert own.parts[0].agents.ceiling_usd == pytest.approx(5 + STEP)
    _, e = await run(tmp_path, lists, budget_text="{steps: null, cost_usd: 5}", profile="listed")
    # the run counts each call at the list price when the provider reports no cost
    assert e.parts[0].agents == own.parts[0].agents
    assert e.total.ceiling_usd == pytest.approx(5 + STEP)
    assert not e.confirm
    assert e.warnings == []
    _, run_cap = await run(
        tmp_path,
        lists,
        budget_text="{steps: null, cost_usd: null}",
        extra="limits: {cost_usd: 3}\n",
        profile="listed",
    )
    assert run_cap.parts[0].agents.ceiling_usd == pytest.approx(3 + STEP)


async def test_a_listed_monitor_is_sized_and_capped_at_its_list_price(tmp_path: Path) -> None:
    lists = Source().lists(tmp_path)
    kw: dict[str, Any] = {"config": WATCHED, "preset": "standard", "profile": "agent"}
    _, plain = await run(tmp_path, None, budget_text="{steps: 400}", **kw)
    assert plain.parts[0].monitor is not None
    assert not plain.parts[0].monitor.priced
    assert plain.unpriced == ["listed-watch"]
    settings, e = await run(tmp_path, lists, budget_text="{steps: 400}", **kw)
    monitor = e.parts[0].monitor
    assert monitor is not None
    assert monitor.priced
    cap = e.parts[0].caps.monitor_cost_usd
    assert cap is not None
    assert monitor.ceiling_usd == cap
    # its budget pays for the sweeps of 400 steps at the list price, past the preset's amount
    assert plain.parts[0].caps.monitor_cost_usd is not None
    assert cap > plain.parts[0].caps.monitor_cost_usd
    assert e.total.ceiling_usd is not None
    assert [x.profile for x in e.listed] == ["listed-watch"]
    assert settings.models.get("listed-watch").listed is not None


async def test_a_key_only_the_extractor_names_is_laid(tmp_path: Path) -> None:
    config = CONFIG + EXTRACT + '\n[monitor]\nextract = ["extract-only"]\n'
    source = Source()
    settings, e = await run(
        tmp_path, source.lists(tmp_path), config=config, preset="standard", profile="agent"
    )
    assert len(source.seen) == 1
    found = settings.models.get("extract-only")
    assert found.listed is not None
    assert found.price == Price(input=1.0, cached=1.0, output=2.0)
    assert e.unpriced == []


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
        " fetched 2025-11-15."
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


def origin_of(settings: Any, path: Path, recorded: Profile) -> Origin:
    return Origin(
        run_id="origin",
        bundle=load_scenario(path),
        schedules={"a": [(None, recorded)]},
        texts={},
        steps={"a": 10},
    )


async def test_a_fork_counts_its_live_steps_at_the_list_price(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    origin = origin_of(settings, path, settings.models.get("listed"))
    fork = fork_of(origin, 5)
    plain = await estimate_fork(History(), settings, origin, fork)
    assert plain.unpriced == ["listed"]
    laid, listing = await priced_fork(settings, origin, fork, lists=Source().lists(tmp_path))
    e = await estimate_fork(History(), laid, origin, fork, listing=listing)
    assert [x.profile for x in e.listed] == ["listed"]
    # the five steps left at the list price
    assert e.parts[0].agents.ceiling_usd == pytest.approx(5 * STEP)
    assert fork_profiles(laid.models, origin, fork)["a"].price == PRICE


async def test_a_fork_of_a_run_that_recorded_no_price_takes_today_s(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    # as a run from before list prices recorded it: no price and no key
    bare = settings.models.get("listed").model_copy(update={"price_key": None})
    origin = origin_of(settings, path, bare)
    fork = fork_of(origin, 5)
    laid, listing = await priced_fork(settings, origin, fork, lists=Source().lists(tmp_path))
    live = fork_profiles(laid.models, origin, fork)["a"]
    assert (live.price, live.price_key) == (PRICE, KEY)
    assert live.listed is not None
    e = await estimate_fork(History(), laid, origin, fork, listing=listing)
    assert e.parts[0].agents.ceiling_usd == pytest.approx(5 * STEP)
    assert [x.profile for x in e.listed] == ["listed"]


async def test_a_fork_takes_no_price_from_a_profile_that_now_names_another_model(
    tmp_path: Path,
) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    bare = settings.models.get("listed").model_copy(update={"price_key": None, "model": "old"})
    origin = origin_of(settings, path, bare)
    fork = fork_of(origin, 5)
    source = Source()
    laid, _ = await priced_fork(settings, origin, fork, lists=source.lists(tmp_path))
    live = fork_profiles(laid.models, origin, fork)["a"]
    assert (live.model, live.price, live.price_key, live.listed) == ("old", None, None, None)
    assert source.seen == []


async def test_a_fork_keeps_the_price_its_origin_recorded(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    dear = Price(input=3.0, cached=0.3, output=6.0)
    recorded = settings.models.get("listed").model_copy(update={"price": dear})
    origin = origin_of(settings, path, recorded)
    fork = fork_of(origin, 5)
    source = Source()
    laid, listing = await priced_fork(settings, origin, fork, lists=source.lists(tmp_path))
    assert source.seen == []
    assert listing.prices == {}
    live = fork_profiles(laid.models, origin, fork)["a"]
    assert (live.price, live.listed) == (dear, None)


async def test_a_fork_job_carries_the_list_price_to_its_runner(tmp_path: Path) -> None:
    settings, path = write(tmp_path, config=CONFIG_LISTED)
    bare = settings.models.get("listed").model_copy(update={"price_key": None})
    origin = origin_of(settings, path, bare)
    fork = fork_of(origin, 5)
    laid, _ = await priced_fork(settings, origin, fork, lists=Source().lists(tmp_path))
    (job,) = fork_jobs(origin, laid.models, step=5)
    job = Job.model_validate_json(job.model_dump_json())
    # the runner has only the job, and rebuilds the live profile from it as the plane did
    runner = runner_for(
        Bench(cast(Any, None), cast(Any, None), settings.models), job, origin.bundle, origin=origin
    )
    assert runner.given_profiles is not None
    live = runner.given_profiles["a"]
    assert (live.price, live.price_key) == (PRICE, KEY)
    assert live.listed is not None
    # the fork's start records it as a run's does, so its costs name the row and the copy
    config = {"agents": {"a": {"profile": live.snapshot()}}}
    assert recorded_prices(json.loads(json.dumps(config))) == {
        "listed": [RecordedPrice(key=KEY, checked=live.listed.checked)]
    }


async def test_a_review_by_a_listed_monitor_is_sized_and_capped_at_its_list_price(
    tmp_path: Path,
) -> None:
    settings, path = write(tmp_path, preset="standard", config=WATCHED)
    bundle = load_scenario(path)
    m = watcher(settings, bundle, preset="standard", steps=400)
    source = Source()
    laid, listing = await priced_review(settings, m, source.lists(tmp_path))
    assert len(source.seen) == 1
    live = watcher(laid, bundle, preset="standard", steps=400)
    assert live.judge is not None
    for q in [*live.sweeps.values(), live.judge]:
        assert q.price == WATCH_PRICE
        assert q.listed is not None
    # its budget pays for the sweeps of 400 steps at the list price, past the preset's amount
    assert live.budget_usd > m.budget_usd
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        origin = await stepped(store, "s", 10)
        plain = await estimate_review(store, origin, m, "full")
        e = await estimate_review(store, origin, live, "full", listing=listing)
    assert not plain.priced
    assert plain.lines == [
        "Review: standard monitor; its models have no price, so its cost is not counted."
    ]
    assert (e.priced, e.ceiling_usd, e.covers) == (True, live.budget_usd, None)
    assert e.confirm == (live.budget_usd > e.confirm_above_usd)
    assert e.lines[0] == (
        "Review: standard monitor, over 10 agent steps. No past reviews to go by. Stops at"
        f" ${live.budget_usd:,.2f}, its spend cap."
    )
    assert [x.profile for x in e.listed] == ["listed-watch"]
    assert e.lines[1].startswith(
        "Profile listed-watch: LiteLLM list price for vendor-x/watch-model"
    )


async def test_a_review_under_the_threshold_starts_without_a_yes(tmp_path: Path) -> None:
    settings, path = write(tmp_path, preset="standard", config=WATCHED)
    bundle = load_scenario(path)
    laid, listing = await priced_review(
        settings, watcher(settings, bundle, preset="standard"), Source().lists(tmp_path)
    )
    live = watcher(laid, bundle, preset="standard", budget_usd=1.0)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        origin = await stepped(store, "s", 10)
        e = await estimate_review(store, origin, live, "full", listing=listing)
    # the cap holds on the list price, so a small one needs no yes
    assert (e.priced, e.ceiling_usd, e.confirm) == (True, 1.0, False)
    assert reviews_confirm([e]) is None


async def test_a_review_counts_its_calls_at_the_list_price(tmp_path: Path) -> None:
    from test_offline import FIXTURE, WATCHER, monitor_model

    (origin_dir,) = FIXTURE.iterdir()
    root = tmp_path / "runs"
    shutil.copytree(origin_dir, root / origin_dir.name)
    bare = WATCHER.model_copy(update={"price": None, "price_key": "vendor-x/watch-model"})
    settings = Settings(models=models(bare), prompts=Prompts(), layers=cast(Any, None))
    async with await LocalEventStore.open(root) as store:
        origin, _ = await monitored(store, origin_dir.name)
        agents = origin_agents(origin)
        scenario = origin.bundle.scenario
        unlaid = offline_monitor(settings.models, settings.prompts, scenario, agents)
        laid, _ = await priced_review(settings, unlaid, Source().lists(tmp_path))
        resolved = offline_monitor(laid.models, laid.prompts, scenario, agents)
        # the job carries the monitor to the runner as JSON
        resolved = ResolvedMonitor.restore(json.loads(json.dumps(resolved.snapshot())))
        result = await monitor_offline(
            store, origin, resolved, models=settings.models, transport=monitor_model().transport()
        )
        assert result.status == "completed", result.reason
        replies = await store.events(result.run_id, types=["model.response"], limit=1000)
        (began,) = await store.events(result.run_id, types=["run.started"], limit=1)
        labels = await listed_costs(store, result.run_id, began.data["config"])
    assert replies
    assert {e.data["cost_source"] for e in replies} == {"list"}
    assert result.cost_usd == pytest.approx(sum(cast(float, e.data["cost_usd"]) for e in replies))
    assert result.cost_usd > 0
    recorded = cast(dict[str, Any], began.data["config"])["monitor"]["sweeps"]
    assert {(q["price_key"], q["listed"] is not None) for q in recorded.values()} == {
        ("vendor-x/watch-model", True)
    }
    assert sum(x.cost_usd for x in labels) == pytest.approx(result.cost_usd)
    assert {p.key for x in labels for p in x.prices} == {"vendor-x/watch-model"}


async def test_a_score_counts_its_ceiling_at_the_classifier_s_list_price(tmp_path: Path) -> None:
    from test_scoring import ANSWER_TEXT, FACTS, sealed

    texts = resolve(Prompts())
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        drafts = [Draft(AgentFinished(reason="submitted", answer=ANSWER_TEXT), "a")]
        run_id = await sealed(store, drafts, facts=FACTS)
        priced = profile(name="cheap", max_output_tokens=1000)
        listed = profile(name="cheap", max_output_tokens=1000, price=None, price_key=KEY)
        found = await lp.listing([listed], Source().lists(tmp_path))
        own = await scoring.estimate_score(store, run_id, priced, texts)
        bare = await scoring.estimate_score(store, run_id, listed, texts)
        e = await scoring.estimate_score(store, run_id, found.lay(listed), texts, listing=found)
    assert bare.ceiling_usd is None
    assert e.ceiling_usd == pytest.approx(own.ceiling_usd)
    assert [x.key for x in e.listed] == [KEY]
    assert e.lines[1].startswith(f"Profile cheap: LiteLLM list price for {KEY}")
    assert own.listed == []


async def test_a_score_counts_its_calls_at_the_classifier_s_list_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripted import Script, say
    from test_scoring import TEXTS, oracle, sealed, words

    listed = profile(name="cheap", model="cheap", price=None, price_key=KEY)
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await sealed(store, await words(store, 2))
        oracle(monkeypatch, [])
        found = await lp.listing([listed], Source().lists(tmp_path))
        laid = found.lay_models(models(listed))
        no = say(json.dumps({"finding": "no", "quote": "", "frames": []}))
        build = scoring.classifier_for(
            store, laid, TEXTS, ["cheap"], [], transport=Script(no).transport(), env={}
        )
        assert build is not None
        result = await scoring.score_run(store, run_id, classifier=build)
        replies = await store.events(result.run_id, types=["model.response"], limit=10)
        (began,) = await store.events(result.run_id, types=["run.started"], limit=1)
        (label,) = await listed_costs(store, result.run_id, began.data["config"])
    assert [e.data["cost_source"] for e in replies] == ["list"]
    # the score's start records the classifier, so its cost names the row and the copy
    recorded = cast(dict[str, Any], began.data["config"])["classifier"]
    assert (recorded["name"], recorded["price_key"]) == ("cheap", KEY)
    assert (label.profile, [p.key for p in label.prices]) == ("cheap", [KEY])
    # the scripted call reads 100 tokens and writes 10, at $1 and $2 per million
    assert result.cost_usd == pytest.approx(120 / 1_000_000)


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


def test_aims_run_lays_the_list_price_before_it_estimates(tmp_path: Path) -> None:
    lists = PriceLists(transport=Source().transport())
    asyncio.run(lists.get())
    _, path = write(tmp_path, config=CONFIG_LISTED + "\n[estimate]\nconfirm_above_usd = 0.01\n")
    config = ["--config", str(tmp_path / "aims.toml"), "--runs-dir", str(tmp_path / "runs")]
    out = CliRunner().invoke(app, ["run", str(path), *config, "--profile", "listed", "--json"])
    # the caps bound the run at the list price, past the threshold, so it asks and stops
    assert out.exit_code != 0
    assert "the caps allow $1.02, above confirm_above_usd" in out.output
    [line] = [json.loads(x) for x in out.stdout.splitlines() if '"estimate"' in x]
    assert [x["key"] for x in line["listed"]] == [KEY]
    assert line["total"]["ceiling_usd"] == pytest.approx(10 * STEP)


async def test_the_api_estimate_carries_the_list_price(tmp_path: Path) -> None:
    async with control_plane(tmp_path) as cp:
        await _api(cp, tmp_path)


async def _api(cp: CP, tmp_path: Path) -> None:
    cp.project.write_text(
        cp.project.read_text() + LISTED.replace('extends = "agent"', 'extends = "listed"')
    )
    cp.project.write_text(cp.project.read_text() + OPEN)
    cp.state.prices = Source().lists(tmp_path)
    runner = FakeRunner(cp)
    await runner.connect()
    try:
        async with cp.client(cp.operator) as c:
            # only the agent's cost cap bounds a profile with no context size, and it holds on
            # the list price, so the run starts without a yes
            scenario = {"source": "project", "name": "control"}
            start = {"scenario": scenario, "monitor": "signals", "profile": "open"}
            ids = []
            # a run started with a yes skips the estimate, and gets the list price all the same
            for body in (start, {**start, "confirm": True}):
                r = await c.post("/api/runs", json=body)
                assert r.status_code == 202, r.text
                ids += r.json()["run_ids"]
        for run_id in ids:
            job = await runner.client.claim(run_id)
            assert isinstance(job, Job)
            [live] = job.profiles.values()
            assert (live.name, live.price, live.price_key) == ("open", PRICE, KEY)
            assert live.listed is not None
    finally:
        await runner.close()
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/scenarios/project/control/estimate", params={"profile": "listed"})
        assert r.status_code == 200, r.text
        data = r.json()
    assert [x["key"] for x in data["listed"]] == [KEY]
    assert data["unlisted"] == {}


WATCH = """
[profiles.listed-watch]
backend = "chat"
base_url = "https://api.vendor-y.test/v1"
model = "watch-listed"
weights = "watch-listed-weights"
reasoning = "full"
max_output_tokens = 1000
price_key = "vendor-x/watch-model"
"""


async def test_review_and_score_jobs_carry_the_list_price_to_their_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_monitor_api import MonitorRunner, flagged, pushed
    from test_score_api import ScoreRunner, planted

    from aims.wire import MonitorJob, ScoreJob

    async with (
        control_plane(tmp_path) as cp,
        await LocalEventStore.open(tmp_path / "local") as local,
    ):
        # the score's classifier is the first extract profile the run's agents do not run
        extract = '\n[monitor]\nextract = ["listed-watch"]\n'
        cp.project.write_text(cp.project.read_text() + WATCH + extract)
        cp.state.prices = Source().lists(tmp_path)
        fake, mon, scorer = FakeRunner(cp), MonitorRunner(cp, "mon"), ScoreRunner(cp, "scorer")
        for runner in (fake, mon, scorer):
            await runner.connect()
        try:
            origin = await pushed(cp, local, fake, flagged())
            scored = await planted(cp, local, fake)
            review = {"preset": "standard", "profile": "listed-watch"}
            async with cp.client(cp.operator) as c:
                r = await c.get(f"/api/runs/{origin}/monitor/estimate", params=review)
                assert r.status_code == 200, r.text
                e = r.json()
                r = await c.post(f"/api/runs/{origin}/monitor", json={**review, "confirm": True})
                assert r.status_code == 202, r.text
                (watched,) = r.json()["run_ids"]
                r = await c.get(f"/api/runs/{scored}/score/estimate")
                assert r.status_code == 200, r.text
                score = r.json()
                r = await c.post(f"/api/runs/{scored}/score", json={})
                assert r.status_code == 202, r.text
                (scoring_run,) = r.json()["run_ids"]
            mjob = await mon.client.claim(watched)
            sjob = await scorer.client.claim(scoring_run)
        finally:
            for runner in (fake, mon, scorer):
                await runner.close()
    # the estimate counts the cap the run keeps to
    assert [x["profile"] for x in e["listed"]] == ["listed-watch"]
    assert e["priced"]
    assert e["ceiling_usd"] is not None
    assert isinstance(mjob, MonitorJob)
    m = mjob.resolved()
    assert e["ceiling_usd"] == m.budget_usd
    for q in [*m.sweeps.values(), m.review, m.judge]:
        assert q is not None
        assert (q.price, q.price_key) == (WATCH_PRICE, "vendor-x/watch-model")
        assert q.listed is not None
    assert isinstance(sjob, ScoreJob)
    classifier = sjob.profile()
    assert classifier is not None
    assert (classifier.name, classifier.price) == ("listed-watch", WATCH_PRICE)
    assert classifier.listed is not None
    assert [x["key"] for x in score["listed"]] == ["vendor-x/watch-model"]
    assert score["ceiling_usd"] is not None
