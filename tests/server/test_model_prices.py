"""Prices behind Settings > Models: the models list says where each profile's price comes from
without reading LiteLLM's price map, and the prices endpoint reads it for the list prices and
the rows that may price a profile with none."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from cp import CP, control_plane
from test_listprice import BODY

from aims.models import listprice as lp
from aims.models.listprice import PriceLists

pytestmark = pytest.mark.anyio
PRICES = "/api/models/prices"

PROFILES = """
[profiles.listed]
backend = "chat"
base_url = "http://model.invalid/v1"
model = "model-a"
weights = "listed-weights"
reasoning = "full"
price_key = "cloud/eu/model-a"

[profiles.missed]
extends = "listed"
model = "vendor-c/model-c"
price_key = "vendor-c/nope"

[profiles.bare]
backend = "chat"
base_url = "http://model.invalid/v1"
model = "router/model-b"
weights = "bare-weights"
reasoning = "full"
"""


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        cp.project.write_text(cp.project.read_text() + PROFILES)
        yield cp


def source(cp: CP, tmp_path: Path, status: int = 200) -> list[httpx2.Request]:
    """The server reads the map from a test source that answers `status`. Returns the
    requests it makes."""
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, content=BODY if status == 200 else b"")

    cp.state.prices = PriceLists(
        tmp_path / "prices.json.gz", transport=httpx2.MockTransport(handle)
    )
    return seen


async def test_the_models_list_says_where_a_price_comes_from_without_the_map(
    cp: CP, tmp_path: Path
) -> None:
    seen = source(cp, tmp_path)
    async with cp.client(cp.viewer) as c:
        r = await c.get("/api/models")
        assert r.status_code == 200, r.text
    profiles = {p["name"]: p for p in r.json()["profiles"]}
    assert profiles["scripted"]["price"] == {"input": 1.0, "cached": 0.1, "output": 2.0}
    assert profiles["scripted"]["price_key"] is None
    assert (profiles["listed"]["price"], profiles["listed"]["price_key"]) == (
        None,
        "cloud/eu/model-a",
    )
    assert (profiles["bare"]["price"], profiles["bare"]["price_key"]) == (None, None)
    assert seen == []


async def test_a_viewer_reads_the_list_prices_and_the_rows_for_the_rest(
    cp: CP, tmp_path: Path
) -> None:
    seen = source(cp, tmp_path)
    async with cp.client(cp.viewer) as c:
        r = await c.get(PRICES, params={"search": "Model-D"})
        assert r.status_code == 200, r.text
        again = await c.get(PRICES)
    data = r.json()
    assert (data["source"], data["problem"]) == ("LiteLLM", None)
    profiles = {p["name"]: p for p in data["profiles"]}
    assert "scripted" not in profiles
    listed = profiles["listed"]
    assert listed["listed"]["key"] == "cloud/eu/model-a"
    day = data["checked"][:10]
    assert listed["listed"]["text"] == f"LiteLLM list price for cloud/eu/model-a, fetched {day}"
    assert (listed["listed"]["input_usd"], listed["listed"]["cached_listed"]) == (2.75, False)
    assert (listed["unlisted"], listed["candidates"]) == (None, [])
    missed = profiles["missed"]
    assert missed["listed"] is None
    assert missed["unlisted"] == "LiteLLM lists no key vendor-c/nope"
    assert [x["key"] for x in missed["candidates"]] == ["vendor-c/model-c"]
    assert missed["candidates"][0] == {
        "key": "vendor-c/model-c",
        "input_usd": 0.3,
        "cached_usd": 0.03,
        "output_usd": 2.5,
        "cached_listed": True,
        "tier_from": 200_000,
    }
    assert [x["key"] for x in profiles["bare"]["candidates"]] == [
        "host/accounts/host/models/model-b"
    ]
    assert [x["key"] for x in data["found"]] == ["vendor-d/model-d"]
    assert again.json()["found"] == []
    assert again.json()["checked"] == data["checked"]
    assert len(seen) == 1


async def test_a_map_that_cannot_be_read_leaves_the_profiles_unpriced_with_why(
    cp: CP, tmp_path: Path
) -> None:
    source(cp, tmp_path, status=503)
    async with cp.client(cp.viewer) as c:
        r = await c.get(PRICES, params={"search": "model"})
        assert r.status_code == 200, r.text
    data = r.json()
    assert (data["checked"], data["found"], data["problem"]) == (
        None,
        [],
        f"{lp.URL} answered 503",
    )
    listed = next(p for p in data["profiles"] if p["name"] == "listed")
    assert listed["listed"] is None
    assert (
        listed["unlisted"] == f"no copy of the LiteLLM price map is at hand ({lp.URL} answered 503)"
    )
    assert all(p["candidates"] == [] for p in data["profiles"])


async def test_a_search_is_at_most_200_characters(cp: CP, tmp_path: Path) -> None:
    source(cp, tmp_path)
    async with cp.client(cp.viewer) as c:
        assert (await c.get(PRICES, params={"search": "x" * 201})).status_code == 422
        assert (await c.get(PRICES, params={"search": "x" * 200})).status_code == 200
    async with cp.client(None) as c:
        assert (await c.get(PRICES)).status_code == 401
