"""Costs at a list price name their row of LiteLLM's map and the day of the copy: the events
decide which calls count, the run's start names where the price came from."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from scripted import profile
from test_report import agent_events, verdict
from test_scoring import sealed

from aims.events import (
    BlobRef,
    CostSource,
    Draft,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    RunFinished,
    RunStarted,
)
from aims.models.profiles import Listed
from aims.report import build as build_module
from aims.report.build import build
from aims.report.listed import ListedCost, RecordedPrice, listed_costs, recorded_prices
from aims.report.markdown import render

pytestmark = pytest.mark.anyio

AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
DAY = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
EARLIER = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
KEY = "vendor-x/listed-model"
REF = BlobRef(sha256="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", size=2)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as s:
        yield s


@pytest.fixture(autouse=True)
def no_plants(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
        return []

    monkeypatch.setattr(build_module, "check_run", check)


def snap(name: str, key: str = KEY, checked: datetime = DAY) -> dict[str, Any]:
    """A profile as a run's start records it, with a list price laid on it."""
    laid = profile(name=name, price_key=key, listed=Listed(checked=checked))
    return laid.snapshot()


def attempt(
    call: str,
    profile_name: str,
    cost: float | None,
    priced: CostSource | None,
    n: int = 1,
    **kw: Any,
) -> list[Draft]:
    asked = ModelRequest(
        call_id=call,
        attempt=n,
        profile=profile_name,
        backend="chat",
        model="m",
        url="http://model",
        params=ModelParams(stream=False),
        body=REF,
        key=REF.sha256,
    )
    answer = ModelResponse(
        call_id=call,
        attempt=n,
        status="completed",
        latency_ms=1,
        cost_usd=cost,
        cost_source=priced,
        **kw,
    )
    return [Draft(asked, "a"), Draft(answer, "a")]


def test_a_run_s_start_names_the_list_price_of_each_place_it_keeps_a_profile() -> None:
    config = {
        "agents": {
            "a": {
                "profile": snap("agent"),
                "schedule": [[3, snap("early", "vendor-x/early")], [None, snap("agent")]],
            },
            "b": "not an agent",
        },
        "monitor": {
            "sweeps": {"window": snap("sweep")},
            "judge": snap("judge"),
            "review": None,
            "extract": snap("extract"),
            "channels": snap("channels"),
        },
        "classifier": snap("classifier"),
    }
    found = recorded_prices(config)
    assert sorted(found) == [
        "agent",
        "channels",
        "classifier",
        "early",
        "extract",
        "judge",
        "sweep",
    ]
    # the same row and copy is named once
    assert found["agent"] == [RecordedPrice(key=KEY, checked=DAY)]
    assert found["early"] == [RecordedPrice(key="vendor-x/early", checked=DAY)]


def test_a_name_laid_from_two_copies_keeps_both() -> None:
    # a fork keeps its origin's price and looks up its monitor's fresh, under one name
    config = {
        "agents": {"a": {"profile": snap("p", checked=EARLIER)}},
        "monitor": {"judge": snap("p")},
    }
    assert recorded_prices(config)["p"] == [
        RecordedPrice(key=KEY, checked=EARLIER),
        RecordedPrice(key=KEY, checked=DAY),
    ]


def test_a_start_from_another_version_reads_what_it_can() -> None:
    good = snap("good")
    config = {
        "agents": [good],
        "monitor": {
            "sweeps": [good],
            "judge": good | {"listed": {"checked": "2026-11-15T09:30:00"}},
            "review": good | {"listed": {"checked": "yesterday"}},
            "extract": good | {"listed": "laid"},
            "channels": good | {"price_key": 7},
        },
        "classifier": good,
    }
    assert recorded_prices(config) == {"good": [RecordedPrice(key=KEY, checked=DAY)]}
    assert recorded_prices(None) == {}
    assert recorded_prices({"agents": {"a": {"profile": None, "schedule": [[1]]}}}) == {}


async def test_the_index_counts_only_live_calls_at_a_list_price(store: LocalEventStore) -> None:
    run_id = await sealed(
        store,
        [
            # a retry at the list price after one the provider priced: both cost, the list part
            # is the second attempt's
            *attempt("c1", "listed", 0.002, "provider"),
            *attempt("c1", "listed", 0.003, "list", n=2),
            *attempt("c2", "listed", 0.004, "list"),
            *attempt("c3", "own", 0.005, "price"),
            *attempt("c4", "other", 0.001, "list"),
            *attempt("c5", "listed", None, None, source="replay"),
            *attempt("c6", "listed", 0.5, "list", source="replay"),
        ],
    )
    calls = await store.listed_calls(run_id)
    assert [(c.profile, c.calls, c.cost_usd) for c in calls] == [
        ("listed", 2, pytest.approx(0.007)),
        ("other", 1, pytest.approx(0.001)),
    ]
    info = await store.run(run_id)
    assert info is not None
    assert info.cost_usd == pytest.approx(0.015)
    assert info.listed_usd == pytest.approx(0.008)


async def test_a_run_with_no_call_at_a_list_price_has_none(store: LocalEventStore) -> None:
    run_id = await sealed(store, attempt("c1", "own", 0.005, "price"))
    info = await store.run(run_id)
    assert info is not None
    assert (info.cost_usd, info.listed_usd) == (pytest.approx(0.005), None)
    assert await listed_costs(store, run_id, {}) == []


async def test_the_report_names_the_row_and_copy_of_each_list_price(
    store: LocalEventStore,
) -> None:
    started = RunStarted(
        scenario="s",
        oversight="observe",
        agents=["a"],
        config={
            "agents": {"a": {"profile": snap("listed")}},
            "monitor": {
                "sweeps": {"w": snap("watch", "vendor-x/watch", EARLIER)},
                "judge": snap("watch", "vendor-x/watch`", DAY),
            },
        },
    )
    run_id = await store.create_run(started)
    await store.append(
        run_id,
        [
            *agent_events(),
            *attempt("c1", "listed", 0.004, "list"),
            *attempt("c2", "listed", 0.002, "list"),
            *attempt("c3", "watch", 0.03, "list"),
            *attempt("c4", "unknown", 0.001, "list"),
            Draft(verdict()),
            Draft(RunFinished(status="completed")),
        ],
    )
    report = await build(store, run_id, now=AT)
    assert report.facts.listed == [
        ListedCost(
            profile="listed", calls=2, cost_usd=0.006, prices=[RecordedPrice(key=KEY, checked=DAY)]
        ),
        ListedCost(
            profile="watch",
            calls=1,
            cost_usd=0.03,
            prices=[
                RecordedPrice(key="vendor-x/watch", checked=EARLIER),
                RecordedPrice(key="vendor-x/watch`", checked=DAY),
            ],
        ),
        ListedCost(profile="unknown", calls=1, cost_usd=0.001),
    ]
    assert report.facts.review_listed == []
    md = render(report)
    assert (
        "List prices: 2 calls of profile listed (<$0.01) count at row `vendor-x/listed-model` of"
        " LiteLLM's price map, copy checked 2026-11-15."
        " 1 call of profile watch ($0.03) counts at row `vendor-x/watch` (copy checked"
        " 2026-11-02) or row `` vendor-x/watch` `` (copy checked 2026-11-15) of LiteLLM's price"
        " map. 1 call of profile unknown (<$0.01) counts at a list price from LiteLLM's price"
        " map that the run did not record." in md
    )


async def test_a_review_s_list_prices_come_from_its_own_start(store: LocalEventStore) -> None:
    run_id = await sealed(store, [*agent_events(), Draft(verdict())])
    started = RunStarted(
        scenario="s",
        oversight="observe",
        kind="monitor",
        origin=run_id,
        monitor="b" * 64,
        config={"monitor": {"judge": snap("judge")}},
    )
    review = await store.create_run(started)
    await store.append(
        review,
        [
            *attempt("j1", "judge", 0.02, "list"),
            Draft(verdict(summary="The review.")),
            Draft(RunFinished(status="completed")),
        ],
    )
    report = await build(store, run_id, now=AT)
    assert report.header.pins.source == review
    assert report.facts.listed == []
    md = render(report)
    assert (
        "List prices in this review: 1 call of profile judge ($0.02) counts at row"
        " `vendor-x/listed-model` of LiteLLM's price map, copy checked 2026-11-15." in md
    )
    assert "List prices:" not in md


async def test_a_call_at_a_list_price_of_zero_still_counts(store: LocalEventStore) -> None:
    # a row that lists the model as free: the run says its cost came from the list
    run_id = await sealed(store, attempt("c1", "free", 0.0, "list"))
    info = await store.run(run_id)
    assert info is not None
    assert (info.cost_usd, info.listed_usd) == (0.0, 0.0)
    calls = await store.listed_calls(run_id)
    assert [(c.profile, c.calls, c.cost_usd) for c in calls] == [("free", 1, 0.0)]
