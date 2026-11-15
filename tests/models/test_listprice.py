"""A list price from LiteLLM's price map: the rows it reads, the exact key it matches, the
candidates it offers, the read from the source and the copy it keeps."""

import asyncio
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from typer.testing import CliRunner

from aims.cli import app
from aims.models import ModelsConfig, Price, Profile, ProfileError, Roles, load_models
from aims.models import listprice as lp
from aims.models.listprice import ListError, NoPrice, PriceList, PriceLists, cache_dir, fetch, parse

pytestmark = pytest.mark.anyio

# Row shapes of the map on Nov 15 2026, cut to the fields a price reads.
ROWS: dict[str, Any] = {
    "sample_spec": {"input_cost_per_token": 0, "output_cost_per_token": 0, "mode": "one of: chat"},
    "model-a": {
        "litellm_provider": "vendor-a",
        "mode": "chat",
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
        "cache_read_input_token_cost": 0.00000125,
        "input_cost_per_token_batches": 0.00000125,
        "input_cost_per_token_priority": 0.00000425,
    },
    "cloud/eu/model-a": {
        "litellm_provider": "cloud",
        "mode": "chat",
        "input_cost_per_token": 0.00000275,
        "output_cost_per_token": 0.000011,
    },
    "host/accounts/host/models/model-b": {
        "litellm_provider": "host",
        "mode": "chat",
        "input_cost_per_token": 1.5e-7,
        "output_cost_per_token": 4.25e-6,
        "cache_read_input_token_cost": 2e-9,
    },
    "vendor-c/model-c": {
        "litellm_provider": "vendor-c",
        "mode": "responses",
        "input_cost_per_token": 3e-7,
        "output_cost_per_token": 2.5e-6,
        "cache_read_input_token_cost": 3e-8,
        "input_cost_per_token_above_200k_tokens": 6e-7,
        "input_cost_per_token_above_272k_tokens": 9e-7,
        "output_cost_per_token_above_200k_tokens": 5e-6,
    },
    "vendor-d/model-d": {
        "litellm_provider": "vendor-d",
        "mode": "chat",
        "tiered_pricing": [
            {"input_cost_per_token": 5e-8, "output_cost_per_token": 4e-7, "range": [0, 256000]},
            {
                "input_cost_per_token": 2.5e-7,
                "output_cost_per_token": 2e-6,
                "range": [256000, 1000000],
            },
        ],
    },
    "vendor-e/free": {"mode": "chat", "input_cost_per_token": 0, "output_cost_per_token": 0},
    "vendor-f/image": {"mode": "image_generation", "output_cost_per_image": 0.04},
    "vendor-f/embed": {
        "mode": "embedding",
        "input_cost_per_token": 1e-7,
        "output_cost_per_token": 0,
    },
    "vendor-g/unpriced": {"mode": "chat", "max_input_tokens": 1000},
    "vendor-g/model-a": {"mode": "chat", "max_input_tokens": 1000},
    "vendor-h/alias": {"alias_of": "model-a"},
} | {f"filler/model-{i}": {"mode": "chat"} for i in range(lp.MIN_ROWS)}
BODY = json.dumps(ROWS).encode()
WHEN = datetime(2026, 11, 15, 1, 46, tzinfo=UTC)
PRICES = PriceList(ROWS, WHEN)


def test_a_row_gives_its_price_per_million_tokens() -> None:
    found = PRICES.get("model-a")
    assert found.price == Price(input=2.5, cached=1.25, output=10.0)
    assert (found.key, found.checked, found.cached_listed, found.tier_from) == (
        "model-a",
        WHEN,
        True,
        None,
    )
    assert found.describe() == "LiteLLM list price for model-a, fetched 2026-11-15"
    long = PRICES.get("host/accounts/host/models/model-b")
    assert long.price == Price(input=0.15, cached=0.002, output=4.25)


def test_a_row_of_the_responses_api_counts_and_a_free_one_costs_nothing() -> None:
    assert PRICES.get("vendor-c/model-c").price == Price(input=0.3, cached=0.03, output=2.5)
    assert PRICES.get("vendor-e/free").price == Price(input=0, cached=0, output=0)


def test_cached_input_counts_at_the_input_price_when_the_row_lists_none() -> None:
    found = PRICES.get("cloud/eu/model-a")
    assert found.price == Price(input=2.75, cached=2.75, output=11.0)
    assert not found.cached_listed


def test_a_tiered_row_gives_its_base_price_and_where_the_tier_starts() -> None:
    assert PRICES.get("vendor-c/model-c").tier_from == 200_000
    ranged = PRICES.get("vendor-d/model-d")
    assert ranged.price == Price(input=0.05, cached=0.05, output=0.4)
    assert ranged.tier_from == 256_000


def test_a_row_with_a_base_price_keeps_it_over_its_ranges() -> None:
    row = {**ROWS["vendor-d/model-d"], "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
    found = PriceList({"k": row}, WHEN).get("k")
    assert found.price == Price(input=1.0, cached=1.0, output=2.0)
    assert found.tier_from == 256_000


def test_a_range_that_does_not_start_at_zero_is_no_base_price() -> None:
    row = {"mode": "chat", "tiered_pricing": [ROWS["vendor-d/model-d"]["tiered_pricing"][1]]}
    with pytest.raises(NoPrice):
        PriceList({"k": row}, WHEN).get("k")


@pytest.mark.parametrize(
    "key",
    [
        "Model-A",
        "model-a ",
        "vendor-a/model-a",
        "eu/model-a",
        "model-b",
        "accounts/host/models/model-b",
        "",
    ],
)
def test_a_key_matches_as_written_or_not_at_all(key: str) -> None:
    with pytest.raises(NoPrice, match="lists no key"):
        PRICES.get(key)


@pytest.mark.parametrize(
    "key",
    ["sample_spec", "vendor-f/image", "vendor-f/embed", "vendor-g/unpriced", "vendor-h/alias"],
)
def test_a_row_that_is_no_priced_chat_model_has_no_price(key: str) -> None:
    with pytest.raises(NoPrice, match=f"no price per token for a chat model under {key}"):
        PRICES.get(key)


@pytest.mark.parametrize(
    "value", [-1e-6, float("nan"), float("inf"), True, "0.000001", None, [1e-6], {"usd": 1}]
)
@pytest.mark.parametrize("field", ["input_cost_per_token", "output_cost_per_token"])
def test_only_a_finite_number_of_zero_or_more_is_a_price(field: str, value: object) -> None:
    row = {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
    with pytest.raises(NoPrice):
        PriceList({"k": {**row, field: value}}, WHEN).get("k")


@pytest.mark.parametrize("value", [-1e-6, float("nan"), True, "1e-7"])
def test_a_bad_cached_price_counts_as_none_listed(value: object) -> None:
    row = {
        "mode": "chat",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
        "cache_read_input_token_cost": value,
    }
    found = PriceList({"k": row}, WHEN).get("k")
    assert (found.price.cached, found.cached_listed) == (1.0, False)


def test_rows_that_are_no_tables_have_no_price() -> None:
    for row in ("text", 3, None, ["mode", "chat"]):
        with pytest.raises(NoPrice):
            PriceList({"k": row}, WHEN).get("k")


def test_candidates_are_the_priced_rows_of_a_model_shortest_first() -> None:
    assert PRICES.candidates("model-a") == ["model-a", "cloud/eu/model-a"]
    assert PRICES.candidates("accounts/host/models/model-b") == [
        "host/accounts/host/models/model-b"
    ]
    assert PRICES.candidates("model-b") == ["host/accounts/host/models/model-b"]
    assert PRICES.candidates("Model-A") == ["model-a", "cloud/eu/model-a"]
    assert PRICES.candidates("odel-a") == []
    assert PRICES.candidates("image") == []
    assert PRICES.candidates("") == []
    assert PRICES.candidates("vendor-a/") == []


def test_candidates_for_a_routed_id_add_the_rows_of_its_last_part() -> None:
    assert PRICES.candidates("router/vendor-c/model-c") == ["vendor-c/model-c"]
    assert PRICES.candidates("vendor-a/model-a") == ["model-a", "cloud/eu/model-a"]
    # the rows that name the whole id come first, however long
    assert PRICES.candidates("eu/model-a") == ["cloud/eu/model-a", "model-a"]


def test_candidates_and_a_search_leave_out_a_key_no_profile_can_name() -> None:
    row = {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
    odd = PriceList({"a b/m": row, "caf\u00e9/m": row, "ok/m": row}, WHEN)
    assert odd.candidates("m") == ["ok/m"]
    assert odd.search("/m") == ["ok/m"]


def test_a_search_finds_the_priced_rows_whose_key_holds_the_text() -> None:
    assert PRICES.search(" MODEL-A ") == ["model-a", "cloud/eu/model-a"]
    assert PRICES.search("model-") == [
        "model-a",
        "cloud/eu/model-a",
        "vendor-c/model-c",
        "vendor-d/model-d",
        "host/accounts/host/models/model-b",
    ]
    assert PRICES.search("unpriced") == []
    assert PRICES.search("  ") == []


def test_candidates_stop_at_a_count() -> None:
    row = {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
    many = PriceList({f"p{i:03}/m": row for i in range(lp.MAX_CANDIDATES + 5)}, WHEN)
    found = many.candidates("m")
    assert len(found) == lp.MAX_CANDIDATES
    assert found[0] == "p000/m"
    assert many.search("/m") == found


def test_parse_takes_only_a_map_of_enough_rows() -> None:
    assert parse(BODY).keys() == ROWS.keys()
    few = {f"k{i}": {} for i in range(lp.MIN_ROWS - 1)}
    for bad in (b"", b"not json", b"[]", b'"text"', json.dumps(few).encode()):
        with pytest.raises(ListError):
            parse(bad)
    mixed = json.dumps({**few, "last": {}, "text": "x", "n": 1}).encode()
    assert len(parse(mixed)) == lp.MIN_ROWS


def served(seen: list[httpx2.Request], *answers: httpx2.Response) -> httpx2.MockTransport:
    left = iter(answers)

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return next(left)

    return httpx2.MockTransport(handle)


def ok(etag: str | None = '"v1"') -> httpx2.Response:
    return httpx2.Response(200, content=BODY, headers={} if etag is None else {"etag": etag})


async def test_fetch_reads_the_map_and_its_etag() -> None:
    seen: list[httpx2.Request] = []
    rows, etag = await fetch(transport=served(seen, ok()))
    assert rows is not None
    assert rows.keys() == ROWS.keys()
    assert etag == '"v1"'
    assert str(seen[0].url) == lp.URL
    assert "if-none-match" not in seen[0].headers
    assert "authorization" not in seen[0].headers


async def test_fetch_asks_whether_a_copy_is_current() -> None:
    seen: list[httpx2.Request] = []
    rows, etag = await fetch('"v1"', served(seen, httpx2.Response(304)))
    assert (rows, etag) == (None, '"v1"')
    assert seen[0].headers["if-none-match"] == '"v1"'


async def test_fetch_drops_an_etag_that_is_no_etag() -> None:
    for tag in ("v1", '"a b"', '"' + "x" * 300 + '"'):
        _, etag = await fetch(transport=served([], ok(tag)))
        assert etag is None
    assert (await fetch(transport=served([], ok('W/"v2"'))))[1] == 'W/"v2"'


@pytest.mark.parametrize("status", [304, 403, 404, 429, 500])
async def test_fetch_refuses_any_other_answer(status: int) -> None:
    with pytest.raises(ListError, match=f"answered {status}"):
        await fetch(transport=served([], httpx2.Response(status)))


async def test_fetch_refuses_a_body_that_is_no_map_or_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ListError, match="did not send a price map"):
        await fetch(transport=served([], httpx2.Response(200, content=b"<html>")))
    monkeypatch.setattr(lp, "MAX_BYTES", 64)
    with pytest.raises(ListError, match="more than 64 bytes"):
        await fetch(transport=served([], ok()))


async def test_fetch_gives_up_after_its_time_and_on_a_dead_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(ListError, match="ConnectError"):
        await fetch(transport=httpx2.MockTransport(refuse))

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(5)
        return ok()

    monkeypatch.setattr(lp, "FETCH_SECONDS", 0.05)
    with pytest.raises(ListError, match=r"did not answer in 0\.05 s"):
        await fetch(transport=httpx2.MockTransport(slow))


def lists(
    path: Path, now: list[float], seen: list[httpx2.Request], *answers: httpx2.Response
) -> PriceLists:
    return PriceLists(path, transport=served(seen, *answers), clock=lambda: now[0])


START = WHEN.timestamp()


async def test_the_first_read_keeps_a_copy_that_serves_while_it_is_current(tmp_path: Path) -> None:
    path = tmp_path / "cache" / lp.FILE
    now: list[float] = [START]
    seen: list[httpx2.Request] = []
    held = lists(path, now, seen, ok())
    found = await held.get()
    assert found.problem is None
    assert found.prices is not None
    assert found.prices.checked == WHEN
    assert found.prices.get("model-a").price.input == 2.5
    kept = json.loads(gzip.decompress(path.read_bytes()))
    assert (kept["v"], kept["url"], kept["etag"], kept["checked"]) == (1, lp.URL, '"v1"', START)
    assert kept["rows"] == ROWS
    assert [p.name for p in path.parent.iterdir()] == [lp.FILE]

    now[0] += lp.FRESH_SECONDS
    assert (await held.get()).prices == found.prices
    other = lists(path, now, seen)
    assert (await other.get()).prices == found.prices
    assert len(seen) == 1


async def test_an_old_copy_is_confirmed_and_its_date_moves(tmp_path: Path) -> None:
    path = tmp_path / lp.FILE
    now: list[float] = [START]
    seen: list[httpx2.Request] = []
    held = lists(path, now, seen, ok(), httpx2.Response(304), ok('"v2"'))
    await held.get()
    now[0] += lp.FRESH_SECONDS + 1
    found = await held.get()
    assert seen[1].headers["if-none-match"] == '"v1"'
    assert found.problem is None
    assert found.prices is not None
    assert found.prices.checked.timestamp() == now[0]
    assert found.prices.rows.keys() == ROWS.keys()
    assert json.loads(gzip.decompress(path.read_bytes()))["checked"] == now[0]

    now[0] += lp.FRESH_SECONDS + 1
    assert (await held.get()).problem is None
    assert json.loads(gzip.decompress(path.read_bytes()))["etag"] == '"v2"'
    assert len(seen) == 3


async def test_a_copy_of_any_age_serves_while_the_source_is_down(tmp_path: Path) -> None:
    path = tmp_path / lp.FILE
    now: list[float] = [START]
    seen: list[httpx2.Request] = []
    await lists(path, now, seen, ok()).get()
    now[0] += 90 * 24 * 3600
    held = lists(path, now, seen, httpx2.Response(500), ok('"v2"'))
    found = await held.get()
    assert found.prices is not None
    assert found.prices.checked == WHEN
    assert found.problem is not None
    assert "answered 500" in found.problem

    now[0] += lp.RETRY_SECONDS - 1
    again = await held.get()
    assert (again.prices, again.problem) == (found.prices, found.problem)
    assert len(seen) == 2

    now[0] += 1
    fresh = await held.get()
    assert fresh.problem is None
    assert fresh.prices is not None
    assert fresh.prices.checked.timestamp() == now[0]
    assert len(seen) == 3


async def test_no_copy_and_no_answer_is_no_price_list(tmp_path: Path) -> None:
    now: list[float] = [START]
    seen: list[httpx2.Request] = []
    held = lists(tmp_path / lp.FILE, now, seen, httpx2.Response(503), ok())
    found = await held.get()
    assert found.prices is None
    assert found.problem is not None
    assert "answered 503" in found.problem
    assert (await held.get()) == found
    assert len(seen) == 1
    assert not (tmp_path / lp.FILE).exists()
    now[0] += lp.RETRY_SECONDS
    assert (await held.get()).prices is not None


async def test_a_copy_from_the_future_is_read_again(tmp_path: Path) -> None:
    path = tmp_path / lp.FILE
    now: list[float] = [START]
    seen: list[httpx2.Request] = []
    await lists(path, now, seen, ok()).get()
    now[0] -= 3600
    found = await lists(path, now, seen, httpx2.Response(304)).get()
    assert found.prices is not None
    assert found.prices.checked.timestamp() == now[0]
    assert len(seen) == 2


def packed(**changes: Any) -> bytes:
    data = {"v": 1, "url": lp.URL, "etag": None, "checked": 1.0, "rows": ROWS} | changes
    return gzip.compress(json.dumps(data).encode())


@pytest.mark.parametrize(
    "text",
    [
        b"not gzip",
        gzip.compress(b"not json"),
        gzip.compress(b"[]"),
        packed(v=2),
        packed(url="https://example.com/x"),
        packed(checked="1"),
        packed(checked=True),
        packed(checked=-1.0),
        packed(rows={}),
        packed(rows=[]),
    ],
)
async def test_a_file_that_is_no_copy_is_read_over(tmp_path: Path, text: bytes) -> None:
    path = tmp_path / lp.FILE
    path.write_bytes(text)
    seen: list[httpx2.Request] = []
    found = await lists(path, [START], seen, ok()).get()
    assert found.prices is not None
    assert found.problem is None
    assert "if-none-match" not in seen[0].headers
    assert json.loads(gzip.decompress(path.read_bytes()))["rows"] == ROWS


async def test_a_copy_with_a_bad_etag_is_kept_and_asked_for_in_full(tmp_path: Path) -> None:
    path = tmp_path / lp.FILE
    path.write_bytes(packed(etag="a b", checked=START))
    seen: list[httpx2.Request] = []
    found = await lists(path, [START + lp.FRESH_SECONDS + 1], seen, ok()).get()
    assert found.prices is not None
    assert "if-none-match" not in seen[0].headers


async def test_a_folder_that_takes_no_file_still_gives_the_prices(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    found = await lists(blocker / "cache" / lp.FILE, [START], [], ok()).get()
    assert found.prices is not None
    assert found.problem is None
    assert "is not kept" in caplog.text


def test_the_cache_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cache_dir({"AIMS_CACHE_DIR": "/a", "XDG_CACHE_HOME": "/b"}) == Path("/a")
    assert cache_dir({"XDG_CACHE_HOME": "/b"}) == Path("/b/aims")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cache_dir({}) == tmp_path / ".cache" / "aims"
    monkeypatch.setenv("AIMS_CACHE_DIR", str(tmp_path / "c"))
    assert PriceLists().path == tmp_path / "c" / lp.FILE


TABLE: dict[str, Any] = {
    "name": "p",
    "backend": "chat",
    "base_url": "https://example.com/v1",
    "model": "model-a",
    "weights": "vendor-a/model-a",
    "reasoning": "none",
}


def test_a_profile_names_its_row_of_the_map() -> None:
    p = Profile.model_validate({**TABLE, "price_key": "cloud/eu/model-a"})
    assert p.price_key == "cloud/eu/model-a"
    assert p.price is None
    assert p.snapshot()["price_key"] == "cloud/eu/model-a"
    assert Profile.model_validate(json.loads(json.dumps(p.snapshot()))) == p
    assert Profile.model_validate(TABLE).price_key is None


def test_a_profile_snapshot_from_before_the_field_loads() -> None:
    p = Profile.model_validate(TABLE)
    old = p.snapshot()
    del old["price_key"]
    assert Profile.model_validate(old) == p


@pytest.mark.parametrize("key", ["", "a b", "a\tb", "café", "x" * 201, 3, ["a"]])
def test_a_price_key_that_is_no_key_is_refused(key: object) -> None:
    with pytest.raises(ValueError, match="price_key"):
        Profile.model_validate({**TABLE, "price_key": key})


def test_a_layer_sets_the_price_key_of_a_profile_below_it(tmp_path: Path) -> None:
    user, project = tmp_path / "user.toml", tmp_path / "aims.toml"
    head = 'backend = "chat"\nbase_url = "https://example.com/v1"\nmodel = "m"\nweights = "w"\n'
    user.write_text(f'[profiles.p]\n{head}reasoning = "none"\nprice_key = "model-a"\n')
    assert load_models(user).get("p").price_key == "model-a"
    project.write_text('[profiles.p]\nprice_key = "cloud/eu/model-a"\n')
    assert load_models(user, project).get("p").price_key == "cloud/eu/model-a"
    project.write_text('[profiles.p]\nprice_key = "a b"\n')
    with pytest.raises(ProfileError, match="price_key"):
        load_models(user, project)


def test_a_config_file_may_not_mark_a_list_price(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    head = 'backend = "chat"\nbase_url = "https://example.com/v1"\nmodel = "m"\nweights = "w"\n'
    path.write_text(
        f'[profiles.p]\n{head}reasoning = "none"\nprice_key = "model-a"\n'
        "price = { input = 1, cached = 1, output = 1 }\n"
        '[profiles.p.listed]\nchecked = "2026-11-15T01:46:00Z"\n'
    )
    with pytest.raises(ProfileError, match="profile 'p' sets listed, which AIMS sets itself"):
        load_models(path)


def test_a_list_price_mark_needs_a_price_and_a_key() -> None:
    mark = {"checked": "2026-11-15T01:46:00Z"}
    price = {"input": 1, "cached": 1, "output": 1}
    for table in ({"price": price}, {"price_key": "model-a"}):
        with pytest.raises(ValueError, match="listed marks a list price"):
            Profile.model_validate({**TABLE, **table, "listed": mark})
    with pytest.raises(ValueError, match="timezone"):
        Profile.model_validate(
            {**TABLE, "price": price, "price_key": "model-a", "listed": {"checked": "2026-11-15"}}
        )


def profile(name: str, model: str, **kw: Any) -> Profile:
    return Profile.model_validate({**TABLE, "name": name, "model": model, **kw})


OWN = profile("own", "model-a", price={"input": 1, "cached": 1, "output": 1})
KEYED = profile("keyed", "model-a", price_key="cloud/eu/model-a")
MISSED = profile("missed", "vendor-c/model-c", price_key="vendor-c/nope")
BARE = profile("bare", "router/model-b")


async def test_a_survey_prices_the_keys_and_offers_rows_for_the_rest(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    held = lists(tmp_path / lp.FILE, [START], seen, ok())
    found = await lp.survey([OWN, KEYED, MISSED, BARE], held, "model-d")
    assert found.checked == WHEN
    assert found.listing.find(KEYED) == PRICES.get("cloud/eu/model-a")
    assert found.listing.why(MISSED) == "LiteLLM lists no key vendor-c/nope"
    assert found.listing.problem is None
    assert {name: [r.key for r in rows] for name, rows in found.candidates.items()} == {
        "missed": ["vendor-c/model-c"],
        "bare": ["host/accounts/host/models/model-b"],
    }
    assert found.candidates["bare"][0] == PRICES.get("host/accounts/host/models/model-b")
    assert [r.key for r in found.found] == ["vendor-d/model-d"]
    assert len(seen) == 1


async def test_a_survey_reads_no_map_for_priced_profiles_and_no_search(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    held = lists(tmp_path / lp.FILE, [START], seen)
    assert await lp.survey([OWN], held, "  ") == lp.Survey()
    assert seen == []
    found = await lp.survey([OWN], lists(tmp_path / lp.FILE, [START], seen, ok()), "model-c")
    assert [r.key for r in found.found] == ["vendor-c/model-c"]
    assert found.candidates == {}
    assert len(seen) == 1


async def test_a_survey_without_a_map_says_why(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    held = lists(tmp_path / lp.FILE, [START], seen, httpx2.Response(503))
    found = await lp.survey([KEYED, BARE], held, "model")
    assert found.checked is None
    assert (found.candidates, found.found) == ({}, [])
    assert found.listing.problem == f"{lp.URL} answered 503"
    assert found.listing.why(KEYED) == (
        f"no copy of the LiteLLM price map is at hand ({lp.URL} answered 503)"
    )


CONFIG = """
[profiles.listed]
backend = "chat"
base_url = "https://example.com/v1"
model = "model-a"
weights = "listed-weights"
reasoning = "none"
price_key = "cloud/eu/model-a"

[profiles.missed]
extends = "listed"
model = "vendor-c/model-c"
price_key = "vendor-c/nope"

[profiles.bare]
backend = "chat"
base_url = "https://example.com/v1"
model = "router/model-b"
weights = "bare-weights"
reasoning = "none"

[profiles.lost]
extends = "bare"
model = "nothing-like-it"

[profiles.own]
extends = "bare"
price = { input = 1.0, cached = 0.1, output = 2.0 }
"""


def cli(tmp_path: Path, *args: str) -> Any:
    path = tmp_path / "aims.toml"
    path.write_text(CONFIG)
    return CliRunner().invoke(app, ["models", *args, "--config", str(path)])


def test_aims_models_list_says_where_each_price_comes_from(tmp_path: Path) -> None:
    out = cli(tmp_path, "list")
    assert out.exit_code == 0, out.output
    lines = {line.split("  ")[0]: line for line in out.stdout.splitlines()}
    assert "  price LiteLLM cloud/eu/model-a  " in lines["listed"]
    assert "  no price  " in lines["bare"]
    assert "  price own  " in lines["own"]
    assert not PriceLists().path.exists()


def test_aims_models_prices_shows_each_price_and_the_rows_for_the_rest(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []
    kept = asyncio.run(PriceLists(transport=served(seen, ok())).get()).prices
    assert kept is not None
    day = f"{kept.checked:%Y-%m-%d}"
    out = cli(tmp_path, "prices", "own", "listed", "missed", "bare", "lost", "--search", "model-d")
    assert out.exit_code == 0, out.output
    assert out.stdout.splitlines() == [
        "Profile bare: no price, so estimates leave out its calls and caps count only the costs"
        " its provider reports.",
        "  Rows that may price it; name one as its price_key:",
        "  host/accounts/host/models/model-b  $0.15 input, $0.0020 cached, $4.25 output",
        f"Profile listed: LiteLLM list price for cloud/eu/model-a, fetched {day}: $2.75 input,"
        " $2.75 cached, $11.00 output per million tokens. It lists no cached input price, so"
        " cached input counts at the input price.",
        "Profile lost: no price, so estimates leave out its calls and caps count only the costs"
        " its provider reports.",
        "  No row's key ends in nothing-like-it; try --search.",
        "Profile missed: no price: LiteLLM lists no key vendor-c/nope, so estimates leave out"
        " its calls and caps count only the costs its provider reports.",
        "  Rows that may price it; name one as its price_key:",
        "  vendor-c/model-c  $0.30 input, $0.0300 cached, $2.50 output (more past 200,000 prompt"
        " tokens)",
        "Profile own: own price: $1.00 input, $0.10 cached, $2.00 output per million tokens.",
        "Rows whose key holds model-d:",
        "  vendor-d/model-d  $0.0500 input, $0.0500 cached, $0.40 output (no cached price"
        " listed; more past 256,000 prompt tokens)",
        f"From LiteLLM's price map, fetched {day}.",
    ]
    assert len(seen) == 1


def test_aims_models_prices_reads_no_map_for_priced_profiles(tmp_path: Path) -> None:
    out = cli(tmp_path, "prices", "own")
    assert out.exit_code == 0, out.output
    assert out.stdout.splitlines() == [
        "Profile own: own price: $1.00 input, $0.10 cached, $2.00 output per million tokens."
    ]
    assert not PriceLists().path.exists()
    out = cli(tmp_path, "prices", "nope")
    assert out.exit_code == 1
    assert "unknown profile" in out.output


def test_aims_models_prices_without_a_map_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_: object) -> Any:
        raise ListError("down")

    monkeypatch.setattr(lp, "fetch", down)
    out = cli(tmp_path, "prices", "listed", "bare", "--search", "model")
    assert out.exit_code == 0, out.output
    assert out.stdout.splitlines() == [
        "Profile bare: no price, so estimates leave out its calls and caps count only the costs"
        " its provider reports.",
        "Profile listed: no price: no copy of the LiteLLM price map is at hand (down), so"
        " estimates leave out its calls and caps count only the costs its provider reports.",
    ]
    assert "warning: no copy of LiteLLM's price map is at hand: down" in out.stderr


TIERED = profile("tiered", "vendor-c/model-c", price_key="vendor-c/model-c")
TWIN = profile("twin", "model-a", price_key="cloud/eu/model-a")


def config(*profiles: Profile) -> ModelsConfig:
    first = profiles[0].name
    by_name = {p.name: p for p in profiles}
    return ModelsConfig(profiles=by_name, roles=Roles(agent=first, sweep=first, judge=first))


async def test_a_laid_list_price_is_marked_with_its_row_and_date(tmp_path: Path) -> None:
    held = lists(tmp_path / lp.FILE, [START], [], ok())
    found = await lp.listing([KEYED, TIERED, OWN, MISSED, BARE], held)
    keyed = found.lay(KEYED)
    assert keyed.price == PRICES.get("cloud/eu/model-a").price
    assert keyed.listed is not None
    assert (keyed.listed.checked, keyed.listed.cached_listed) == (WHEN, False)
    assert keyed.listed.tier_from is None
    tiered = found.lay(TIERED).listed
    assert tiered is not None
    assert (tiered.cached_listed, tiered.tier_from) == (True, 200_000)
    assert Profile.model_validate(json.loads(json.dumps(keyed.snapshot()))) == keyed
    for p in (OWN, MISSED, BARE):
        assert found.lay(p) is p
    assert found.lay(keyed) is keyed


async def test_priced_lays_a_list_price_on_each_profile_with_a_key_called(tmp_path: Path) -> None:
    models = config(OWN, KEYED, TWIN, TIERED, BARE)
    seen: list[httpx2.Request] = []
    laid, found = await lp.priced(
        models, [KEYED, BARE], lists(tmp_path / lp.FILE, [START], seen, ok())
    )
    assert sorted(found.prices) == ["cloud/eu/model-a"]
    assert laid.get("keyed") == found.lay(KEYED)
    assert laid.get("twin").listed is not None
    for name in ("own", "tiered", "bare"):
        assert laid.get(name) is models.get(name)
    assert laid.roles == models.roles
    assert len(seen) == 1


async def test_priced_reads_no_map_when_no_profile_called_needs_one(tmp_path: Path) -> None:
    models = config(OWN, KEYED)
    seen: list[httpx2.Request] = []
    held = lists(tmp_path / lp.FILE, [START], seen)
    assert await lp.priced(models, [OWN, BARE], held) == (models, lp.Listing())
    laid = models.model_copy(
        update={
            "profiles": {
                "own": OWN,
                "keyed": lp.Listing({"cloud/eu/model-a": PRICES.get("cloud/eu/model-a")}).lay(
                    KEYED
                ),
            }
        }
    )
    assert await lp.priced(laid, list(laid.profiles.values()), held) == (laid, lp.Listing())
    assert await lp.priced(models, [KEYED], None) == (models, lp.Listing())
    assert seen == []


async def test_priced_without_a_map_leaves_the_profiles_and_says_why(tmp_path: Path) -> None:
    models = config(OWN, KEYED)
    held = lists(tmp_path / lp.FILE, [START], [], httpx2.Response(503))
    laid, found = await lp.priced(models, [KEYED], held)
    assert laid is models
    assert (
        found.why(KEYED) == f"no copy of the LiteLLM price map is at hand ({lp.URL} answered 503)"
    )
