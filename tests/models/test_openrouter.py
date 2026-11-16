"""A profile filled in from OpenRouter's list of models, as the app's Add from OpenRouter
dialog drafts it."""

import copy
import tomllib
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aims.models import Message, Prompt, load_models
from aims.models import openrouter as router
from aims.models.backends import BACKENDS
from aims.models.openrouter import Catalog, CatalogError, default_name, draft, fetch

CHAT = ["/api/v1/chat/completions", "/api/v1/responses"]
# The shapes of OpenRouter's list on Nov 14 2026, cut to the fields a profile reads.
FLASH: dict[str, Any] = {
    "id": "deepseek/deepseek-v4.1-flash",
    "canonical_slug": "deepseek/deepseek-v4.1-flash-20260910",
    "name": "DeepSeek: DeepSeek V4.1 Flash",
    "context_length": 1048576,
    "pricing": {
        "prompt": "0.0000000507",
        "completion": "0.0000002028",
        "input_cache_read": "0.000000001014",
    },
    "top_provider": {"context_length": 1048576, "max_completion_tokens": 393216},
    "supported_parameters": ["max_tokens", "reasoning", "structured_outputs", "tools"],
    "expiration_date": None,
    "endpoints": CHAT,
    "reasoning": {
        "mandatory": False,
        "default_enabled": True,
        "supported_efforts": ["max", "high", "low", "none"],
        "default_effort": "high",
    },
}
HAIKU: dict[str, Any] = {
    "id": "anthropic/claude-haiku-5.5",
    "canonical_slug": "anthropic/claude-haiku-5.5-20261007",
    "context_length": 200000,
    "pricing": {"prompt": "0.000001", "completion": "0.000005"},
    "top_provider": {"max_completion_tokens": 8000},
    "supported_parameters": ["reasoning", "tools"],
    "expiration_date": "2026-12-11",
    "endpoints": CHAT,
    "reasoning": {"mandatory": False},
}
LATEST: dict[str, Any] = {
    **HAIKU,
    "id": "~anthropic/claude-haiku-latest",
    "canonical_slug": "~anthropic/claude-haiku-latest",
    "alias_target": {"name": "Claude Haiku 5.5", "slug": "anthropic/claude-haiku-5.5"},
}
AUTO: dict[str, Any] = {
    "id": "openrouter/auto",
    "canonical_slug": "openrouter/auto",
    "context_length": None,
    "pricing": {"prompt": "-1", "completion": "-1"},
    "top_provider": {"context_length": None, "max_completion_tokens": None},
    "supported_parameters": ["reasoning", "structured_outputs", "tools"],
    "endpoints": CHAT,
}
BATCH: dict[str, Any] = {**FLASH, "id": "deepseek/deepseek-v4.1-flash:batch", "endpoints": []}
BATCH["endpoints"] = ["/api/v1/batches"]
MODELS = [FLASH, HAIKU, LATEST, AUTO, BATCH]


def changed(model: dict[str, Any], **fields: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(model) | fields]


def test_a_profile_carries_the_openrouter_defaults_and_what_the_list_says() -> None:
    found = draft(MODELS, "deepseek/deepseek-v4.1-flash")
    assert found.name == "openrouter-deepseek-v4.1-flash"
    assert found.table == {
        "backend": "chat",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-v4.1-flash",
        "weights": "deepseek/deepseek-v4.1-flash-20260910",
        "api_key_env": "OPENROUTER_API_KEY",
        "reasoning": "full",
        "effort": "high",
        "max_output_tokens": 16384,
        "context_tokens": 1048576,
        "cache_key": True,
        "extra": {"provider": {"sort": "price", "require_parameters": True}},
        "price": {"input": 0.0507, "cached": 0.001014, "output": 0.2028},
    }
    profile = found.profile
    # unset, so `require_parameters` does not cut the route to the endpoints that take it
    assert profile.parallel_tool_calls is None
    assert profile.headers == {}
    assert [n.split(".")[0] for n in found.notes] == [
        'reasoning = "full" is a guess',
        "The price is the lowest among its providers",
    ]


def test_the_toml_text_loads_as_the_same_profile(tmp_path: Path) -> None:
    found = draft(MODELS, "deepseek/deepseek-v4.1-flash")
    text = found.toml()
    assert text.startswith('[profiles."openrouter-deepseek-v4.1-flash"]\n')
    assert "# OpenRouter lists: max, high, low, none\n" in text
    assert tomllib.loads(text) == {"profiles": {found.name: found.table}}
    path = tmp_path / "aims.toml"
    path.write_text(text)
    assert load_models(path).get(found.name) == found.profile


def test_the_id_is_trimmed_and_lowercased_and_the_name_can_be_chosen() -> None:
    found = draft(MODELS, "  DeepSeek/DeepSeek-V4.1-Flash\n", name="agent_1.x")
    assert (found.name, found.slug) == ("agent_1.x", "deepseek/deepseek-v4.1-flash")
    assert default_name("author/model:free") == "openrouter-model-free"


@pytest.mark.parametrize("slug", ["", "flash", "a/b/c", "a b/c", "../models", "a/b?x=1", "a/b:"])
def test_text_that_is_no_model_id_is_refused(slug: str) -> None:
    with pytest.raises(CatalogError, match="is not an OpenRouter model id") as e:
        draft(MODELS, slug)
    assert e.value.kind == "slug"


@pytest.mark.parametrize("name", ["", "a b", "-a", 'a"]\nx = 1', "a" * 101])
def test_a_profile_name_holds_only_plain_characters(name: str) -> None:
    with pytest.raises(CatalogError, match="profile name") as e:
        draft(MODELS, FLASH["id"], name=name)
    assert e.value.kind == "name"


def test_a_model_the_list_lacks_names_the_close_ones() -> None:
    with pytest.raises(CatalogError) as e:
        draft(MODELS, "deepseek/deepseek-v4.1-flsh")
    assert e.value.kind == "missing"
    assert str(e.value).startswith(
        "OpenRouter lists no model deepseek/deepseek-v4.1-flsh; close: deepseek/deepseek-v4.1-flash"
    )
    with pytest.raises(CatalogError, match=r"lists no model zzz/qqq$"):
        draft(MODELS, "zzz/qqq")


def test_an_alias_is_refused_unless_asked_for() -> None:
    with pytest.raises(CatalogError, match=r"now points at anthropic/claude-haiku-5\.5") as e:
        draft(MODELS, "~anthropic/claude-haiku-latest")
    assert e.value.kind == "refused"
    found = draft(MODELS, "~anthropic/claude-haiku-latest", alias=True)
    assert found.name == "openrouter-claude-haiku-latest"
    assert found.table["model"] == "~anthropic/claude-haiku-latest"
    # the model it points at today, so a monitor on that model is still refused
    assert found.table["weights"] == "anthropic/claude-haiku-5.5-20261007"
    assert found.notes[0].startswith("~anthropic/claude-haiku-latest is an alias, which now")


def test_a_model_with_a_cache_write_price_asks_for_its_prompt_cache(tmp_path: Path) -> None:
    pricing = FLASH["pricing"] | {"input_cache_write": "0.000000063"}
    found = draft(changed(FLASH, pricing=pricing), FLASH["id"])
    assert found.table["extra"] == {
        "cache_control": {"type": "ephemeral"},
        "provider": {"sort": "price", "require_parameters": True},
    }
    assert any(n.startswith("OpenRouter lists a price for a cache write") for n in found.notes)
    text = found.toml()
    assert (
        'extra = {cache_control = {type = "ephemeral"}, provider = {sort = "price",'
        " require_parameters = true}}\n"
    ) in text
    path = tmp_path / "aims.toml"
    path.write_text(text)
    profile = load_models(path).get(found.name)
    assert profile == found.profile
    body = BACKENDS["chat"].body(profile, Prompt(items=[Message(role="user", content="hi")]))
    assert body["cache_control"] == {"type": "ephemeral"}
    # a write price that is no amount says nothing
    for price in (None, "free", "-1", 0.5):
        listed = changed(FLASH, pricing=FLASH["pricing"] | {"input_cache_write": price})
        assert "cache_control" not in draft(listed, FLASH["id"]).table["extra"]
    # the model's author plays no part
    assert "cache_control" not in draft(MODELS, "anthropic/claude-haiku-5.5").table["extra"]


def test_a_model_not_served_on_chat_completions_is_refused() -> None:
    with pytest.raises(CatalogError, match=r"does not serve .*:batch on chat completions") as e:
        draft(MODELS, "deepseek/deepseek-v4.1-flash:batch")
    assert e.value.kind == "refused"


def test_reasoning_the_list_does_not_turn_on_stays_off() -> None:
    found = draft(MODELS, "anthropic/claude-haiku-5.5")
    assert found.table["reasoning"] == "none"
    assert "effort" not in found.table
    assert found.efforts == ()
    assert any(n.startswith("OpenRouter does not say that it reasons") for n in found.notes)
    assert "OpenRouter lists:" not in found.toml()


@pytest.mark.parametrize(
    ("reasoning", "kind", "effort"),
    [
        (None, "none", None),
        ({"mandatory": True}, "full", None),
        ({"mandatory": False, "default_enabled": True}, "full", None),
        ({"mandatory": False, "default_enabled": False}, "none", None),
        ({"supported_efforts": ["low", "none"], "default_effort": "none"}, "none", "none"),
        ({"supported_efforts": ["low", "high"], "default_effort": "low"}, "full", "low"),
        # an effort AIMS does not know, or one the model does not list, is left out
        ({"supported_efforts": ["turbo", "low"], "default_effort": "turbo"}, "none", None),
        ({"supported_efforts": ["low"], "default_effort": "high"}, "none", None),
        ({"supported_efforts": "low", "default_effort": 3, "mandatory": "yes"}, "none", None),
    ],
)
def test_reasoning_and_effort_follow_the_list(
    reasoning: dict[str, Any] | None, kind: str, effort: str | None
) -> None:
    found = draft(changed(FLASH, reasoning=reasoning), FLASH["id"])
    assert (found.table["reasoning"], found.table.get("effort")) == (kind, effort)


def test_the_output_cap_is_the_models_when_it_is_lower() -> None:
    assert draft(MODELS, HAIKU["id"]).table["max_output_tokens"] == 8000
    assert draft(MODELS, FLASH["id"]).table["max_output_tokens"] == 16384


def test_missing_abilities_are_said_and_set() -> None:
    found = draft(MODELS, HAIKU["id"])
    assert found.table["structured_output"] is False
    assert any("cannot be the sweep or judge model" in n for n in found.notes)
    bare = draft(changed(HAIKU, supported_parameters=[]), HAIKU["id"])
    assert any("it cannot run an agent" in n for n in bare.notes)
    assert "structured_output" not in draft(MODELS, FLASH["id"]).table


def test_prices_are_per_million_and_a_model_without_one_has_none() -> None:
    haiku = draft(MODELS, HAIKU["id"])
    assert haiku.table["price"] == {"input": 1.0, "cached": 1.0, "output": 5.0}
    assert any("no price for cached input" in n for n in haiku.notes)
    assert "OpenRouter retires it on 2026-12-11." in haiku.notes
    auto = draft(MODELS, "openrouter/auto")
    assert "price" not in auto.table
    assert "context_tokens" not in auto.table
    assert any(n.startswith("OpenRouter lists no fixed price") for n in auto.notes)
    assert any(n.startswith("OpenRouter lists no context size") for n in auto.notes)
    free = draft(changed(FLASH, pricing={"prompt": "0", "completion": "0"}), FLASH["id"])
    assert free.table["price"] == {"input": 0.0, "cached": 0.0, "output": 0.0}
    assert not any("cached input" in n for n in free.notes)
    tiers = {
        **FLASH["pricing"],
        "overrides": [{"min_prompt_tokens": 272000}, {"min_prompt_tokens": 200000}, {}, 7],
    }
    tiered = draft(changed(FLASH, pricing=tiers), FLASH["id"])
    assert "From 200000 prompt tokens on the price is higher." in tiered.notes


@pytest.mark.parametrize("price", [None, 5, "", "abc", "nan", "inf", "-0.1", ["1"]])
def test_a_price_that_is_no_amount_leaves_the_profile_without_one(price: object) -> None:
    pricing = {"prompt": price, "completion": "0.000001"}
    assert "price" not in draft(changed(FLASH, pricing=pricing), FLASH["id"]).table


def test_only_checked_values_of_the_list_reach_the_profile() -> None:
    """The list is outside text: a field of another shape is left out, never copied."""
    bad = "x\n[roles]\nagent = 'y'"
    odd = changed(
        FLASH,
        canonical_slug=bad,
        name=bad,
        context_length="many",
        top_provider=[1],
        supported_parameters="tools",
        expiration_date=bad,
        pricing=[bad],
        reasoning={"supported_efforts": [bad, "low", 3], "default_effort": bad},
        alias_target=bad,
    )
    found = draft(odd, FLASH["id"])
    assert found.table["weights"] == FLASH["id"]
    assert found.efforts == ("low",)
    text = found.toml() + "".join(found.notes)
    assert "roles" not in text
    assert set(tomllib.loads(found.toml())) == {"profiles"}
    # entries of another shape do not stop the lookup of a good one
    mixed = [{"id": 4}, {}, *MODELS]
    assert draft(mixed, FLASH["id"]).table == draft(MODELS, FLASH["id"]).table


def served(body: object, status: int = 200, seen: list[httpx2.Request] | None = None) -> Any:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body)
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(handle)


pytestmark = pytest.mark.anyio


async def test_the_list_is_read_without_a_key() -> None:
    seen: list[httpx2.Request] = []
    models = await fetch(served({"data": [FLASH, "junk", HAIKU]}, seen=seen))
    assert [m["id"] for m in models] == [FLASH["id"], HAIKU["id"]]
    (request,) = seen
    assert str(request.url) == "https://openrouter.ai/api/v1/models"
    assert "authorization" not in request.headers


@pytest.mark.parametrize(
    ("body", "status", "problem"),
    [
        ({"error": {"message": "down"}}, 503, "answered 503"),
        (b"<html>", 200, "did not send a list of models"),
        ({"data": {"a": 1}}, 200, "did not send a list of models"),
        ([1], 200, "did not send a list of models"),
    ],
)
async def test_an_answer_that_is_no_list_is_an_error(
    body: object, status: int, problem: str
) -> None:
    with pytest.raises(CatalogError, match=problem) as e:
        await fetch(served(body, status))
    assert e.value.kind == "unreachable"


async def test_a_list_too_large_or_a_host_that_does_not_answer_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "MAX_BYTES", 64)
    with pytest.raises(CatalogError, match="sent more than 64 bytes"):
        await fetch(served({"data": [FLASH]}))

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(CatalogError, match="ConnectError: no route") as e:
        await fetch(httpx2.MockTransport(refuse))
    assert e.value.kind == "unreachable"


async def test_the_catalog_reads_the_list_again_only_once_it_is_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx2.Request] = []
    now = [1000.0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    catalog = Catalog(served({"data": [FLASH]}, seen=seen))
    first = await catalog.models()
    now[0] += router.FRESH_SECONDS
    assert await catalog.models() is first
    assert len(seen) == 1
    now[0] += 1
    assert await catalog.models() == first
    assert len(seen) == 2


async def test_a_failed_read_is_not_kept() -> None:
    answers = iter([httpx2.Response(500), httpx2.Response(200, json={"data": [FLASH]})])
    catalog = Catalog(httpx2.MockTransport(lambda request: next(answers)))
    with pytest.raises(CatalogError):
        await catalog.models()
    assert [m["id"] for m in await catalog.models()] == [FLASH["id"]]


# Roles


def test_a_draft_says_which_roles_it_fits() -> None:
    assert draft(MODELS, FLASH["id"]).fits == ("agent", "sweep", "judge")
    assert draft(MODELS, FLASH["id"]).unfit(["agent", "sweep", "judge"]) is None
    haiku = draft(MODELS, HAIKU["id"])
    assert haiku.fits == ("agent",)
    assert haiku.unfit(["agent", "sweep"]) == (
        "openrouter-claude-haiku-5.5 cannot take the sweep role: OpenRouter lists no structured"
        " output"
    )
    tool_less = draft(changed(FLASH, supported_parameters=["structured_outputs"]), FLASH["id"])
    assert tool_less.fits == ("sweep", "judge")
    assert tool_less.unfit(["judge", "agent"]) == (
        "openrouter-deepseek-v4.1-flash cannot take the agent role: OpenRouter lists no tool calls"
    )
