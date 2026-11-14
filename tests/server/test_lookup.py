"""The profile lookup behind Settings > Models: the server reads the provider's list of models
and answers a profile as config file text. It saves nothing."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cp import CP, control_plane
from test_openrouter import FLASH, MODELS

from aims.models.openrouter import Catalog, draft

pytestmark = pytest.mark.anyio
LOOKUP = "/api/models/lookup"


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


def listed(cp: CP, body: Any = None, status: int = 200) -> list[httpx2.Request]:
    """The server reads `body` in place of OpenRouter's list. Returns the requests it makes."""
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json={"data": MODELS} if body is None else body)

    cp.state.catalog = Catalog(httpx2.MockTransport(handle))
    return seen


async def test_an_operator_looks_a_model_up_and_nothing_is_saved(cp: CP) -> None:
    seen = listed(cp)
    before = cp.project.read_bytes()
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params={"slug": FLASH["id"]})
        assert r.status_code == 200, r.text
        found = draft(MODELS, FLASH["id"])
        assert r.json() == {
            "name": "openrouter-deepseek-v4.1-flash",
            "slug": FLASH["id"],
            "toml": found.toml(),
            "notes": list(found.notes),
            "exists": False,
        }
        named = await c.get(LOOKUP, params={"slug": FLASH["id"], "name": "scripted"})
        assert named.json()["exists"] is True
        assert named.json()["toml"].startswith("[profiles.scripted]\n")
    assert cp.project.read_bytes() == before
    # one read of the list serves both
    assert [str(r.url) for r in seen] == ["https://openrouter.ai/api/v1/models"]


async def test_the_lookup_is_for_operators(cp: CP) -> None:
    seen = listed(cp)
    async with cp.client(cp.viewer) as c:
        assert (await c.get(LOOKUP, params={"slug": FLASH["id"]})).status_code == 403
    async with cp.client(cp.runner) as c:
        assert (await c.get(LOOKUP, params={"slug": FLASH["id"]})).status_code in (401, 403)
    async with cp.client() as c:
        assert (await c.get(LOOKUP, params={"slug": FLASH["id"]})).status_code == 401
    assert seen == []


@pytest.mark.parametrize(
    ("params", "status", "code", "detail"),
    [
        ({"slug": "nope"}, 422, "slug", "is not an OpenRouter model id"),
        ({"slug": "a/b", "name": "a b"}, 422, "name", "profile name 'a b'"),
        ({"slug": "~anthropic/claude-haiku-latest"}, 422, "refused", "is an alias"),
        ({"slug": "deepseek/deepseek-v4.1-flash:batch"}, 422, "refused", "chat completions"),
        ({"slug": "deepseek/deepseek-v4.1-flsh"}, 404, "missing", "close: deepseek/"),
    ],
)
async def test_a_lookup_that_finds_no_profile_says_why(
    cp: CP, params: dict[str, str], status: int, code: str, detail: str
) -> None:
    listed(cp)
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params=params)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == code
    assert detail in r.json()["detail"]


async def test_an_alias_is_taken_when_asked_for(cp: CP) -> None:
    listed(cp)
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params={"slug": "~anthropic/claude-haiku-latest", "alias": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "openrouter-claude-haiku-latest"


async def test_a_provider_that_does_not_answer_is_a_bad_gateway(cp: CP) -> None:
    seen = listed(cp, {"error": "down"}, 503)
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params={"slug": FLASH["id"]})
        assert (r.status_code, r.json()["code"], r.json()["title"]) == (
            502,
            "unreachable",
            "Bad gateway",
        )
        assert "answered 503" in r.json()["detail"]
        # a failed read is not kept: the next lookup asks again
        assert (await c.get(LOOKUP, params={"slug": FLASH["id"]})).status_code == 502
    assert len(seen) == 2


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"slug": ""},
        {"slug": "a" * 201},
        {"slug": "a/b", "source": "other"},
        {"slug": "a/b", "name": ""},
    ],
)
async def test_a_malformed_lookup_reads_no_list(cp: CP, params: dict[str, str]) -> None:
    seen = listed(cp)
    async with cp.client(cp.operator) as c:
        assert (await c.get(LOOKUP, params=params)).status_code == 422
    assert seen == []
