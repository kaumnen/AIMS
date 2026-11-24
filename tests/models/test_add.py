"""The drafter behind `aims models add`: a profile for a model on any server, from a base URL
or a base profile, the server's list of ids, a LiteLLM row the person picks and `--set` pairs;
and the guarded read of the server's list."""

import asyncio
import contextlib
import ipaddress
import json
import socket
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpcore2
import httpx2
import pytest
from cp import control_plane
from proxy import environ
from typer.testing import CliRunner

from aims import net
from aims.cli import app
from aims.config import ConfigError, add_table, appended, layers, load, with_roles
from aims.models import Profile, ProfileError, load_models
from aims.models import add as adding
from aims.models import check as checking
from aims.models.add import (
    AddError,
    Draft,
    ServerList,
    ServerLists,
    default_name,
    draft,
    parse_set,
    read_list,
)
from aims.models.check import Check
from aims.models.listprice import Found, PriceList
from aims.models.profiles import Role
from aims.net import Guarded, Refused, refused

URL = "https://models.example.test/v1"
ROWS: dict[str, Any] = {
    "big-model": {
        "mode": "chat",
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
        "max_input_tokens": 128000,
        "max_output_tokens": 65536,
        "supports_function_calling": True,
        "supports_reasoning": True,
    },
    "host-a/small-model": {
        "mode": "chat",
        "input_cost_per_token": 1e-07,
        "output_cost_per_token": 2e-07,
        "max_input_tokens": 32768,
        "max_output_tokens": 4096,
        "supports_function_calling": False,
        "supports_response_schema": False,
    },
    "host-b/small-model": {
        "mode": "chat",
        "input_cost_per_token": 2e-07,
        "output_cost_per_token": 3e-07,
        "max_input_tokens": True,
        "max_output_tokens": "lots",
    },
    "embed-model": {"mode": "embedding", "input_cost_per_token": 1e-07},
}
PRICES = PriceList(ROWS, datetime(2026, 11, 16, tzinfo=UTC))


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's proxy variables stay out, in either case."""
    environ(monkeypatch)


def base_profile(**fields: Any) -> Profile:
    return Profile.model_validate(
        {
            "name": "router",
            "backend": "chat",
            "base_url_env": "ROUTER_URL",
            "model": "org/base-model",
            "weights": "org/base-model-v1",
            "api_key_env": "ROUTER_KEY",
            "reasoning": "full",
            "effort": "high",
            "max_output_tokens": 1000,
            "context_tokens": 2000,
            "cache_key": True,
            "stream": False,
            "headers": {"X-Title": "aims"},
            "extra": {"provider": {"sort": "price"}},
            "sampling": {"temperature": 0.5},
            "price": {"input": 1, "cached": 1, "output": 2},
            **fields,
        }
    )


def loaded(found: Draft, tmp_path: Path) -> Profile:
    """The profile the draft's TOML text gives when a config file holds it."""
    path = tmp_path / "aims.toml"
    path.write_text(found.toml(), encoding="utf-8")
    return load_models(path).get(found.name)


def has(found: Draft, text: str) -> bool:
    return any(text in note for note in found.notes)


def test_a_draft_from_a_base_url_takes_only_what_it_is_given(tmp_path: Path) -> None:
    found = draft("Org/Model-7B", base_url=f"{URL}/", key_env="MODELS_KEY")
    assert found.name == "Model-7B"
    assert found.table == {
        "backend": "chat",
        "base_url": URL,
        "model": "Org/Model-7B",
        "weights": "Org/Model-7B",
        "api_key_env": "MODELS_KEY",
        "reasoning": "none",
        "max_output_tokens": adding.OUTPUT_TOKENS,
    }
    assert loaded(found, tmp_path) == found.profile
    for text in ("weights is the model id", '"none" is a guess', "no price", "context size"):
        assert has(found, text), text
    assert found.candidates == ()


def test_a_server_without_a_key_takes_none() -> None:
    found = draft("local-model", base_url="http://127.0.0.1:8000/v1", backend="responses")
    assert "api_key_env" not in found.table
    assert found.table["backend"] == "responses"
    assert has(found, "sends no key")


@pytest.mark.parametrize(
    ("model", "name"),
    [
        ("org/Model-7B:free", "Model-7B-free"),
        ("llama3.1:8b", "llama3.1-8b"),
        ("@cf/meta/llama-3", "llama-3"),
        ("accounts/x/models/m_1", "m_1"),
        ("org/~model", "model"),
        ("org/", None),
        ("org/~~", None),
    ],
)
def test_the_default_name_is_the_ids_last_part(model: str, name: str | None) -> None:
    assert default_name(model) == name


def test_an_id_with_no_name_in_it_needs_one() -> None:
    with pytest.raises(AddError, match="give one") as e:
        draft("org/", base_url=URL)
    assert e.value.kind == "name"
    assert draft("org/", base_url=URL, name="mine").name == "mine"


@pytest.mark.parametrize("model", ["", "  ", "a b", "x" * 201, "modèle", "a\nb"])
def test_text_that_is_no_model_id_is_refused(model: str) -> None:
    with pytest.raises(AddError) as e:
        draft(model, base_url=URL)
    assert e.value.kind == "model"


@pytest.mark.parametrize("name", ["-x", "a b", "a/b", "x" * 101, ""])
def test_a_profile_name_holds_only_plain_characters(name: str) -> None:
    with pytest.raises(AddError) as e:
        draft("m", base_url=URL, name=name)
    assert e.value.kind == "name"


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ({}, "give a base URL"),
        ({"base_url": URL, "base": "base"}, "not both"),
        ({"key_env": "K", "base": "base"}, "not both"),
        ({"base_url": "ftp://h/v1"}, "not an http or https URL"),
        ({"base_url": "models.example.test/v1"}, "not an http or https URL"),
        ({"base_url": "https://"}, "not an http or https URL"),
        ({"base_url": "https://user:secret@h/v1"}, "user name or password"),
        ({"base_url": "https://h/v1?version=2"}, "no query or fragment"),
        ({"base_url": "https://h/v1#top"}, "no query or fragment"),
        ({"base_url": URL, "key_env": "1KEY"}, "not an env var name"),
        ({"base_url": URL, "key_env": "MY-KEY"}, "not an env var name"),
        ({"base_url": URL, "backend": "completions"}, "backend 'completions'"),
    ],
)
def test_a_draft_needs_one_way_to_the_server(given: dict[str, Any], problem: str) -> None:
    if given.get("base") == "base":
        given["base"] = base_profile()
    with pytest.raises(AddError, match=problem) as e:
        draft("m", **given)
    assert e.value.kind == "connection"


def test_a_base_profile_gives_how_to_reach_the_server_and_nothing_of_its_model(
    tmp_path: Path,
) -> None:
    found = draft("org/other-model", base=base_profile())
    assert found.table == {
        "backend": "chat",
        "base_url_env": "ROUTER_URL",
        "model": "org/other-model",
        "weights": "org/other-model",
        "api_key_env": "ROUTER_KEY",
        "reasoning": "none",
        "max_output_tokens": adding.OUTPUT_TOKENS,
        "cache_key": True,
        "stream": False,
        "headers": {"X-Title": "aims"},
        "extra": {"provider": {"sort": "price"}},
    }
    assert has(
        found,
        "From router it takes backend, base_url_env, api_key_env, cache_key, stream, headers,"
        " extra.",
    )
    assert loaded(found, tmp_path) == found.profile
    # The draft's tables are its own.
    found.table["extra"]["provider"]["sort"] = "speed"
    assert base_profile().extra == {"provider": {"sort": "price"}}


def test_a_base_profile_leaves_out_what_it_does_not_set() -> None:
    base = base_profile(headers={}, extra={}, cache_key=None, base_url_env=None, base_url=URL)
    found = draft("m", base=base, backend="responses")
    assert found.table["backend"] == "responses"
    assert found.table["base_url"] == URL
    assert not {"headers", "extra", "cache_key", "base_url_env"} & set(found.table)


def test_the_servers_list_checks_the_id() -> None:
    listed = ServerList(URL, ("org/Model-7B", "org/model-8b", "other/thing"))
    assert not has(draft("org/Model-7B", base_url=URL, listed=listed), "list of models")
    missing = draft("org/model-7b", base_url=URL, listed=listed)
    assert has(missing, "has no org/model-7b. Close: org/Model-7B, org/model-8b.")
    far = draft("zzz", base_url=URL, listed=listed)
    assert has(far, "has no zzz.")
    assert not has(far, "Close")
    unread = draft("m", base_url=URL, listed=ServerList(URL, problem="it answered 401"))
    assert has(unread, "was not read: it answered 401.")


def test_a_picked_row_gives_the_price_key_context_and_output_cap(tmp_path: Path) -> None:
    found = draft("big-model", base_url=URL, prices=PRICES, row="big-model")
    assert found.table["price_key"] == "big-model"
    assert found.table["context_tokens"] == 128000
    assert found.table["max_output_tokens"] == adding.OUTPUT_TOKENS
    assert has(found, "row says it can reason")
    assert not has(found, "no price")
    assert not has(found, "context size")
    assert loaded(found, tmp_path) == found.profile

    small = draft("small-model", base_url=URL, prices=PRICES, row="host-a/small-model")
    assert small.table["max_output_tokens"] == 4096
    assert has(small, "row lists no tool calls")
    assert has(small, "row lists no structured output")
    # Capability flags are only notes: the check measures them.
    assert small.table.get("structured_output") is None


def test_a_row_with_odd_numbers_gives_only_its_price_key() -> None:
    found = draft("small-model", base_url=URL, prices=PRICES, row="host-b/small-model")
    assert found.table["price_key"] == "host-b/small-model"
    assert found.table["max_output_tokens"] == adding.OUTPUT_TOKENS
    assert "context_tokens" not in found.table
    assert has(found, "context size")


@pytest.mark.parametrize(
    ("row", "prices", "problem"),
    [
        ("nope", PRICES, "lists no key nope"),
        ("embed-model", PRICES, "no price per token for a chat model"),
        ("big-model", None, "no copy of the LiteLLM price map"),
    ],
)
def test_a_row_that_gives_no_price_is_refused(
    row: str, prices: PriceList | None, problem: str
) -> None:
    with pytest.raises(AddError, match=problem) as e:
        draft("big-model", base_url=URL, prices=prices, row=row)
    assert e.value.kind == "row"


def test_without_a_row_the_rows_that_name_the_model_are_offered() -> None:
    found = draft("org/small-model", base_url=URL, prices=PRICES)
    assert found.candidates == ("host-a/small-model", "host-b/small-model")
    assert "price_key" not in found.table
    assert has(found, "no price")


@pytest.mark.parametrize(
    ("pair", "path", "value"),
    [
        ("effort=low", ("effort",), "low"),
        ("effort = 'low'", ("effort",), "low"),
        ("max_output_tokens=4096", ("max_output_tokens",), 4096),
        ("stream=false", ("stream",), False),
        ("extra.provider.sort=price", ("extra", "provider", "sort"), "price"),
        ('headers."X-Title"=my bench', ("headers", "X-Title"), "my bench"),
        ("extra.order=['a', 'b']", ("extra", "order"), ["a", "b"]),
        ("extra.p={ a = 1, b = { c = true } }", ("extra", "p"), {"a": 1, "b": {"c": True}}),
        ("extra.note=x = 1", ("extra", "note"), "x = 1"),
        ("extra.note=1 # two", ("extra", "note"), 1),
    ],
)
def test_a_pair_sets_one_field_to_a_toml_value_or_text(
    pair: str, path: tuple[str, ...], value: Any
) -> None:
    assert parse_set(pair) == (path, value)


@pytest.mark.parametrize(
    ("pair", "problem"),
    [
        ("effort", "not key=value"),
        ("=low", "not a TOML key"),
        ("a..b=1", "not a TOML key"),
        ("a b=1", "not a TOML key"),
        ('headers."a=b"=1', "not a TOML key"),
        ("effort=", "no value"),
        ("effort=low\nmodel='x'", "one line"),
        ("name=x", "name cannot be set"),
        ("listed.checked=2026-11-16T00:00:00Z", "listed cannot be set"),
        ("extends=router", "extends cannot be set"),
    ],
)
def test_a_pair_that_is_no_one_field_is_refused(pair: str, problem: str) -> None:
    with pytest.raises(AddError, match=problem) as e:
        parse_set(pair)
    assert e.value.kind == "set"


def test_pairs_apply_last_and_add_up_in_a_table(tmp_path: Path) -> None:
    found = draft(
        "m",
        base=base_profile(),
        sets=[
            "extra.provider.only=['host-a']",
            "extra.reasoning.enabled=true",
            'headers."X-Run"=bench',
            "weights=shared/m",
            "reasoning=full",
            "context_tokens=8192",
            "price_key=big-model",
        ],
    )
    assert found.table["extra"] == {
        "provider": {"sort": "price", "only": ["host-a"]},
        "reasoning": {"enabled": True},
    }
    assert found.table["headers"] == {"X-Title": "aims", "X-Run": "bench"}
    assert found.table["weights"] == "shared/m"
    assert loaded(found, tmp_path) == found.profile
    for text in ("weights is the model id", "is a guess", "no price", "context size"):
        assert not has(found, text), text


def test_a_list_of_tables_and_a_key_that_needs_quotes_load_back(tmp_path: Path) -> None:
    found = draft(
        "m",
        base_url=URL,
        sets=["extra.tools=[{ type = 'x', 'a b' = 1 }]", 'extra."odd key".v="y"'],
    )
    assert loaded(found, tmp_path) == found.profile
    assert found.profile.extra == {"tools": [{"type": "x", "a b": 1}], "odd key": {"v": "y"}}


@pytest.mark.parametrize(
    ("sets", "problem"),
    [
        (["effort=extreme"], "effort: Input should be"),
        (["unknown_field=1"], "unknown_field: Extra inputs are not permitted"),
        (["base_url_env=URL_ENV"], "set exactly one of base_url and base_url_env"),
        (["summary=auto"], "summary is only sent by the responses backend"),
    ],
)
def test_pairs_that_make_no_profile_are_refused(sets: list[str], problem: str) -> None:
    with pytest.raises(AddError, match=problem) as e:
        draft("m", base_url=URL, sets=sets)
    assert e.value.kind == "profile"


def test_a_pair_inside_a_value_that_is_no_table_is_refused() -> None:
    with pytest.raises(AddError, match="model is not a table") as e:
        draft("m", base_url=URL, sets=["model.x=1"])
    assert e.value.kind == "set"


def test_unset_removes_a_field_before_the_pairs_apply(tmp_path: Path) -> None:
    found = draft(
        "m",
        base=base_profile(),
        unset=["headers.X-Title", "extra.provider", "max_output_tokens", "cache_key"],
        sets=['headers."X-Run"=bench', "extra.provider.sort=latency"],
    )
    assert found.table["headers"] == {"X-Run": "bench"}
    assert found.table["extra"] == {"provider": {"sort": "latency"}}
    assert "max_output_tokens" not in found.table
    assert "cache_key" not in found.table
    assert loaded(found, tmp_path) == found.profile
    # A note that a removed field made no longer holds is left out.
    priced = draft("big-model", base_url=URL, prices=PRICES, row="big-model", unset=["price_key"])
    assert has(priced, "no price")
    named = draft("m", base_url=URL, unset=["weights"], sets=["weights=shared/m"])
    assert not has(named, "weights is the model id")


@pytest.mark.parametrize(
    ("key", "kind", "problem"),
    [
        ("headers.X-Other", "set", "headers.X-Other is not set"),
        ("price_key", "set", "price_key is not set"),
        ("model.x", "set", "model.x is not set"),
        ("a..b", "set", "not a TOML key"),
        ("name", "set", "name cannot be set"),
        ("model", "profile", "model: Field required"),
    ],
)
def test_unset_names_a_field_that_is_set(key: str, kind: str, problem: str) -> None:
    with pytest.raises(AddError, match=problem) as e:
        draft("m", base=base_profile(), unset=[key])
    assert e.value.kind == kind


def test_a_draft_says_which_roles_it_fits() -> None:
    found = draft("m", base_url=URL)
    assert found.fits == ("agent", "sweep", "judge")
    assert found.unfit(["agent", "judge"]) is None
    plain = draft("m", base_url=URL, sets=["structured_output=false"])
    assert plain.fits == ("agent",)
    assert plain.unfit(["agent", "judge"]) == (
        "m cannot take the judge role: structured_output is false"
    )


# The server's list of models


def served(
    body: object, status: int = 200, seen: list[httpx2.Request] | None = None, **headers: str
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body, headers=headers)
        return httpx2.Response(status, json=body, headers=headers)

    return httpx2.MockTransport(handle)


pytestmark = pytest.mark.anyio


async def test_the_list_is_read_without_a_key_and_gives_its_ids() -> None:
    seen: list[httpx2.Request] = []
    body = {"data": [{"id": "a/one"}, "junk", {"id": "b two"}, {"id": 3}, {"id": "a/one"}, {}]}
    found = await read_list(f"{URL}/", served(body, seen=seen))
    assert found == ServerList(f"{URL}/models", ("a/one",))
    (request,) = seen
    assert str(request.url) == f"{URL}/models"
    assert "authorization" not in request.headers


async def test_a_bare_array_is_a_list_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adding, "MAX_IDS", 2)
    found = await read_list(URL, served([{"id": "x"}, {"id": "y"}, {"id": "z"}]))
    assert found.ids == ("x", "y")


@pytest.mark.parametrize(
    ("body", "status", "problem"),
    [
        ({"error": "no key"}, 401, "it needs a key (401)"),
        ({"error": "no"}, 403, "it needs a key (403)"),
        ({"error": "none"}, 404, "it answered 404"),
        (b"<html>secret page</html>", 200, "it sent no list of models"),
        ({"data": {"a": 1}}, 200, "it sent no list of models"),
        ({"models": []}, 200, "it sent no list of models"),
    ],
)
async def test_an_answer_that_is_no_list_says_why(body: object, status: int, problem: str) -> None:
    found = await read_list(URL, served(body, status))
    assert found.ids == ()
    assert found.problem == problem


@pytest.mark.parametrize(
    ("base_url", "problem"),
    [
        ("https://user:secret@models.example.test/v1", "user name or password"),
        ("https://models.example.test/v1?key=1", "no query or fragment"),
        ("http://[::1", "not an http or https URL"),
        ("ftp://models.example.test/v1", "not an http or https URL"),
    ],
)
async def test_a_base_url_the_draft_refuses_is_not_read(base_url: str, problem: str) -> None:
    seen: list[httpx2.Request] = []
    with pytest.raises(AddError, match=problem):
        await read_list(base_url, served({"data": []}, seen=seen))
    with pytest.raises(AddError, match=problem):
        await ServerLists(served({"data": []}, seen=seen)).get(base_url)
    assert seen == []


async def test_a_list_nested_too_deep_is_no_list() -> None:
    found = await read_list(URL, served(b"[" * 200_000))
    assert found.problem == "it sent no list of models"


async def test_a_redirect_is_not_followed() -> None:
    seen: list[httpx2.Request] = []
    found = await read_list(URL, served({}, 302, seen=seen, location="http://169.254.169.254/"))
    assert found.problem == "it answered 302"
    assert len(seen) == 1


async def test_a_list_too_large_slow_or_unreachable_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adding, "MAX_BYTES", 64)
    found = await read_list(URL, served({"data": [{"id": "x" * 100}]}))
    assert found.problem == "it sent more than 64 bytes"

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to 10.1.2.3", request=request)

    found = await read_list(URL, httpx2.MockTransport(refuse))
    assert found.problem == "ConnectError"

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1)
        return httpx2.Response(200, json=[])

    monkeypatch.setattr(adding, "FETCH_SECONDS", 0.05)
    found = await read_list(URL, httpx2.MockTransport(slow))
    assert found.problem == "no answer in 0.05 s"


async def test_lists_are_kept_a_while_by_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    now = [1000.0]
    lists = ServerLists(served({"data": [{"id": "x"}]}, seen=seen), clock=lambda: now[0])
    first = await lists.get(URL)
    now[0] += adding.FRESH_SECONDS
    assert await lists.get(f"{URL}/") is first
    assert len(seen) == 1
    now[0] += 1
    assert await lists.get(URL) == first
    assert len(seen) == 2

    monkeypatch.setattr(adding, "MAX_LISTS", 2)
    for n in range(3):
        await lists.get(f"https://h{n}.test/v1")
    assert len(seen) == 5
    await lists.get("https://h2.test/v1")
    assert len(seen) == 5
    await lists.get(URL)
    assert len(seen) == 6


async def test_a_list_that_was_not_read_is_not_kept() -> None:
    answers = iter([httpx2.Response(500), httpx2.Response(200, json={"data": [{"id": "x"}]})])
    lists = ServerLists(httpx2.MockTransport(lambda request: next(answers)))
    assert (await lists.get(URL)).problem == "it answered 500"
    assert (await lists.get(URL)).ids == ("x",)


# The address guard


@pytest.mark.parametrize(
    ("address", "out"),
    [
        ("169.254.169.254", True),
        ("169.254.1.1", True),
        ("fe80::1", True),
        ("::ffff:169.254.169.254", True),
        ("2002:a9fe:a9fe::1", True),
        ("64:ff9b::a9fe:a9fe", True),
        # 169.254.169.254 behind a local NAT64 prefix of 96, 64, 56 and 48 bits (RFC 6052).
        ("64:ff9b:1::a9fe:a9fe", True),
        ("64:ff9b:1:0:a9:fea9:fe00:0", True),
        ("64:ff9b:1:a9:fe:a9fe::", True),
        ("64:ff9b:1:a9fe:a9:fe00::", True),
        ("fd00:ec2::254", True),
        ("100.100.100.200", True),
        ("168.63.129.16", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.0.0.1", False),
        ("192.168.1.10", False),
        ("100.64.0.1", False),
        ("8.8.8.8", False),
        ("2001:db8::1", False),
        ("64:ff9b::808:808", False),
        ("64:ff9b:1::808:808", False),
    ],
)
def test_link_local_and_metadata_addresses_are_refused(address: str, out: bool) -> None:
    assert refused(ipaddress.ip_address(address)) is out


@dataclass
class Resolver:
    """Names resolve to `names`, and after their first lookup to `then` when it has them.
    An address resolves to itself. `asked` lists the names looked up."""

    names: dict[str, list[str]] = field(default_factory=dict)
    then: dict[str, list[str]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    def lookup(self, host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        try:
            answer = [str(ipaddress.ip_address(host))]
        except ValueError:
            seen = host in self.asked
            self.asked.append(host)
            answer = self.then[host] if seen and host in self.then else self.names.get(host)
            if answer is None:
                raise socket.gaierror(socket.EAI_NONAME, "unknown") from None
        family = {4: socket.AF_INET, 6: socket.AF_INET6}
        return [
            (family[6 if ":" in a else 4], socket.SOCK_STREAM, 6, "", (a, port)) for a in answer
        ]


# httpcore2's backend, which it exports behind a stand-in for when anyio is missing.
BACKEND: Any = httpcore2.AnyIOBackend


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Resolver:
    found = Resolver()
    monkeypatch.setattr(socket, "getaddrinfo", found.lookup)
    return found


@contextlib.asynccontextmanager
async def serving(ids: list[str]) -> AsyncGenerator[tuple[int, list[bytes]]]:
    """A list server on 127.0.0.1 only: its port and the request heads it heard."""
    heard: list[bytes] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heard.append(await reader.readuntil(b"\r\n\r\n"))
        body = json.dumps({"data": [{"id": i} for i in ids]}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], heard


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The hosts httpcore2's backend opens a connection to, which still opens it."""
    hosts: list[str] = []
    real = BACKEND.connect_tcp

    async def connect(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        hosts.append(host)
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(BACKEND, "connect_tcp", connect)
    return hosts


async def test_a_name_that_resolves_to_a_refused_address_is_refused(
    resolver: Resolver, connects: list[str]
) -> None:
    resolver.names["sneaky.test"] = ["127.0.0.1", "169.254.169.254"]
    resolver.names["mapped.test"] = ["::ffff:a9fe:a9fe"]
    resolver.names["2852039166"] = ["169.254.169.254"]
    for url in (
        "http://sneaky.test/v1",
        "https://mapped.test/v1",
        "http://2852039166/v1",
        "http://169.254.169.254/latest",
        "http://[fe80::1]:8080/v1",
    ):
        with pytest.raises(Refused, match="link-local or cloud metadata"):
            await read_list(url)
    assert connects == []


async def test_a_server_on_loopback_is_read_by_name(resolver: Resolver) -> None:
    """`localhost` gives ::1 first. The server listens on 127.0.0.1 only."""
    async with serving(["local-model"]) as (port, heard):
        resolver.names["localhost"] = ["::1", "127.0.0.1"]
        found = await read_list(f"http://localhost:{port}/v1")
    assert found.ids == ("local-model",)
    assert heard[-1].startswith(b"GET /v1/models HTTP/1.1\r\n")
    assert f"host: localhost:{port}\r\n".encode() in heard[-1].lower()


async def test_the_connection_goes_to_the_address_that_was_checked(
    resolver: Resolver, connects: list[str]
) -> None:
    """A name that answers a second lookup with the metadata address still reaches the
    address of the first, which was checked."""
    resolver.names["rebind.test"] = ["127.0.0.1"]
    resolver.then["rebind.test"] = ["169.254.169.254"]
    async with serving(["m"]) as (port, heard):
        found = await read_list(f"http://rebind.test:{port}/v1")
    assert found.ids == ("m",)
    assert resolver.asked == ["rebind.test"]
    assert connects == ["127.0.0.1"]
    assert f"host: rebind.test:{port}\r\n".encode() in heard[-1].lower()


ERROR_KINDS = (
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutException",
    "ConnectError",
    "ReadError",
    "WriteError",
    "NetworkError",
    "ProxyError",
    "UnsupportedProtocol",
    "LocalProtocolError",
    "RemoteProtocolError",
    "ProtocolError",
)


@pytest.mark.parametrize(
    ("theirs", "ours"),
    [(getattr(httpcore2, k), getattr(httpx2, k)) for k in ERROR_KINDS],
    ids=ERROR_KINDS,
)
def test_each_httpcore2_error_reads_as_the_httpx2_error_of_its_kind(
    theirs: type[Exception], ours: type[Exception]
) -> None:
    with pytest.raises(ours, match="boom") as e, net._as_httpx2():
        raise theirs("boom")
    assert type(e.value) is ours
    with pytest.raises(KeyError), net._as_httpx2():
        raise KeyError("other")


async def test_a_body_cut_short_says_why(resolver: Resolver) -> None:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"data": [')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        found = await read_list(f"http://127.0.0.1:{port}/v1")
    assert found.problem == "RemoteProtocolError"


async def test_a_connection_serves_the_next_request(connects: list[str]) -> None:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(2):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    async with (
        server,
        httpx2.AsyncClient(transport=Guarded()) as client,
    ):
        port = server.sockets[0].getsockname()[1]
        for _ in range(2):
            response = await client.get(f"http://127.0.0.1:{port}/")
            assert response.text == "ok"
    assert connects == ["127.0.0.1"]


async def test_a_name_that_does_not_resolve_is_unreachable(resolver: Resolver) -> None:
    found = await read_list("https://unknown.test/v1")
    assert found.problem == "ConnectError"


async def test_a_connection_that_does_not_open_in_time_is_a_connect_timeout(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(self: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(BACKEND, "connect_tcp", hang)
    resolver.names["slow.test"] = ["203.0.113.7", "203.0.113.8"]
    with pytest.raises(httpcore2.ConnectTimeout, match=r"slow\.test did not answer in 0\.05 s"):
        await net._Checked().connect_tcp("slow.test", 443, timeout=0.05)


async def test_through_a_proxy_the_check_runs_and_the_proxy_gets_the_name(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    resolver.names["models.example.test"] = ["203.0.113.7"]
    resolver.names["bad.test"] = ["169.254.169.254"]
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": [{"id": "x"}]})

    transport = Guarded()
    transport._proxies["http://proxy.test:3128"] = httpx2.MockTransport(handle)  # ty: ignore[invalid-assignment]
    assert (await read_list(URL, transport)).ids == ("x",)
    (request,) = seen
    assert str(request.url) == f"{URL}/models"
    with pytest.raises(Refused):
        await read_list("https://bad.test/v1", transport)
    assert len(seen) == 1
    # A host that reaches out only through its proxy may not resolve the name itself.
    assert (await read_list("https://far.test/v1", transport)).ids == ("x",)
    assert str(seen[-1].url) == "https://far.test/v1/models"


# Racing the addresses (RFC 8305)


@pytest.mark.parametrize(
    ("given", "order"),
    [
        (
            ["::1", "::2", "10.0.0.1", "10.0.0.2", "10.0.0.3"],
            ["::1", "10.0.0.1", "::2", "10.0.0.2", "10.0.0.3"],
        ),
        (["10.0.0.1", "10.0.0.2", "::1"], ["10.0.0.1", "::1", "10.0.0.2"]),
        (["10.0.0.1", "10.0.0.2"], ["10.0.0.1", "10.0.0.2"]),
        ([], []),
    ],
)
def test_the_families_take_turns_from_the_first(given: list[str], order: list[str]) -> None:
    addresses = [ipaddress.ip_address(a) for a in given]
    assert [str(a) for a in net.interleave(addresses)] == order


A, B, C = (ipaddress.ip_address(a) for a in ("2001:db8::1", "2001:db8::2", "192.0.2.3"))


class Attempts:
    """Fake connections: each address's outcome, when each attempt started and what ended."""

    def __init__(self, **outcomes: str) -> None:
        self.outcomes = {ipaddress.ip_address(k.replace("_", ":")): v for k, v in outcomes.items()}
        self.started: dict[str, float] = {}
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.gate = asyncio.Event()
        self.moved = asyncio.Event()

    async def until_started(self, count: int) -> None:
        while len(self.started) < count:
            self.moved.clear()
            await self.moved.wait()

    async def connect(self, address: Any) -> str:
        name = str(address)
        self.started[name] = time.monotonic()
        self.moved.set()
        how = self.outcomes.get(address, "hang")
        try:
            if how == "fail":
                raise httpcore2.ConnectError(f"{name} refused")
            if how == "gate":
                await self.gate.wait()
            elif how == "stubborn":
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.Event().wait()
            elif how == "hang":
                await asyncio.Event().wait()
            return name
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise

    async def close(self, stream: str) -> None:
        self.closed.append(stream)


async def test_the_next_address_starts_after_the_delay_while_the_first_hangs() -> None:
    attempts = Attempts(**{"2001:db8::2": "ok"})
    begun = time.monotonic()
    assert await net.race([A, B], attempts.connect, attempts.close, delay=0.05) == str(B)
    assert attempts.started[str(B)] - begun >= 0.05
    assert attempts.cancelled == [str(A)]
    assert attempts.closed == []


async def test_the_next_address_starts_at_once_when_the_first_fails() -> None:
    attempts = Attempts(**{"2001:db8::1": "fail", "2001:db8::2": "ok"})
    assert await net.race([A, B], attempts.connect, attempts.close, delay=30) == str(B)
    assert attempts.started[str(B)] - attempts.started[str(A)] < 15


async def test_a_second_connection_that_opened_too_is_closed() -> None:
    attempts = Attempts(**{"2001:db8::1": "gate", "2001:db8::2": "gate"})

    async def open_gate() -> None:
        await attempts.until_started(2)
        attempts.gate.set()

    opener = asyncio.create_task(open_gate())
    won = await net.race([A, B], attempts.connect, attempts.close, delay=0)
    await opener
    assert {won, *attempts.closed} == {str(A), str(B)}
    assert len(attempts.closed) == 1

    # One that opens as its cancel arrives is closed too.
    stubborn = Attempts(**{"2001:db8::1": "stubborn", "2001:db8::2": "ok"})
    assert await net.race([A, B], stubborn.connect, stubborn.close, delay=0) == str(B)
    assert stubborn.closed == [str(A)]


async def test_when_every_address_fails_the_last_failure_is_raised() -> None:
    attempts = Attempts(**{"2001:db8::1": "fail", "2001:db8::2": "fail", "192.0.2.3": "fail"})
    with pytest.raises(httpcore2.ConnectError, match=r"192\.0\.2\.3 refused"):
        await net.race([A, B, C], attempts.connect, attempts.close, delay=30)
    with pytest.raises(ValueError, match="no address"):
        await net.race([], attempts.connect, attempts.close)


async def test_a_cancelled_race_cancels_every_attempt() -> None:
    attempts = Attempts()
    task = asyncio.create_task(net.race([A, B, C], attempts.connect, attempts.close, delay=0))
    await attempts.until_started(3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(attempts.cancelled) == sorted(str(a) for a in (A, B, C))


# The command


@dataclass
class Fakes:
    """What `aims models add` read and called in a test: the server lists it read by URL,
    and the profiles the check got with whether it measured. `checks` gives each test's
    verdict and the plain test's reasoning kind. `prices` is the map at hand, None for none."""

    reads: list[str] = field(default_factory=list)
    ids: tuple[str, ...] = ("org/big-model", "org/small-model")
    list_problem: Exception | None = None
    prices: PriceList | None = PRICES
    checked: list[Profile] = field(default_factory=list)
    measured: list[bool] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)
    reasoning: str | None = "full"


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Fakes:
    """The command's server list, price map and check, faked, in an empty project."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODELS_KEY", "secret")
    monkeypatch.setenv("AIMS_KEYS", f"MODELS_KEY={URL},MODELS_KEY=env:ROUTER_URL")
    found = Fakes()

    async def read(base_url: str, transport: object = None) -> ServerList:
        found.reads.append(base_url)
        if found.list_problem is not None:
            raise found.list_problem
        return ServerList(f"{base_url}/models", found.ids)

    async def at_hand(lists: object) -> Found:
        return Found(found.prices, None if found.prices is not None else "down")

    async def check(client: object, p: Profile, *, measure: bool = False) -> list[Check]:
        found.checked.append(p)
        found.measured.append(measure)
        out = []
        for name in ("plain", "unstreamed", "tools", "structured"):
            verdict: Any = found.verdicts.get(name, "ok")
            kind: Any = found.reasoning if name == "plain" else None
            out.append(Check(p.name, name, verdict, "seen", kind))
        return out

    monkeypatch.setattr(adding, "read_list", read)
    monkeypatch.setattr(adding, "at_hand", at_hand)
    monkeypatch.setattr(checking, "check_profile", check)
    return found


def add(*args: str) -> Any:
    return CliRunner().invoke(app, ["models", "add", *args])


def flat(text: str) -> str:
    return " ".join(text.split())


BIG = ["org/big-model", "--base-url", URL, "--key-env", "MODELS_KEY"]


def test_add_checks_the_draft_and_prints_it_with_the_reasoning_it_measured(
    fakes: Fakes, tmp_path: Path
) -> None:
    out = add(*BIG, "--litellm", "big-model")
    assert out.exit_code == 0, out.output
    drafted = draft(BIG[0], base_url=URL, key_env="MODELS_KEY", prices=PRICES, row="big-model")
    assert out.stdout == drafted.measured("full").toml()
    assert 'reasoning = "full"' in out.stdout
    assert [p.reasoning for p in fakes.checked] == ["none"]
    assert fakes.measured == [True]
    assert fakes.reads == [URL]
    lines = out.stderr.splitlines()
    assert lines[0] == "checking big-model with a few small requests"
    assert lines[1] == "ok    big-model  plain: seen"
    assert lines[5].startswith("run ")
    assert 'note: reasoning = "full", as the check measured.' in lines
    assert "guess" not in out.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == [".git", "runs"]


def test_reasoning_that_is_given_stays_and_none_is_said_softly(fakes: Fakes) -> None:
    given = add(*BIG, "--set", "reasoning=summary")
    assert given.exit_code == 0, given.output
    assert fakes.measured == [False]
    assert 'reasoning = "summary"' in given.stdout
    assert "as the check" not in given.stderr
    fakes.reasoning = "none"
    seen = add(*BIG)
    assert 'reasoning = "none"' in seen.stdout
    assert "as the check saw on a short answer" in flat(seen.stderr)
    fakes.reasoning = None
    failed = add(*BIG)
    assert 'note: reasoning = "none" is a guess.' in flat(failed.stderr)


def test_without_a_row_the_notes_name_the_rows_that_may_fit(fakes: Fakes) -> None:
    out = add("org/small-model", "--base-url", URL)
    assert out.exit_code == 0, out.output
    assert (
        "note: LiteLLM rows that may price it: host-a/small-model, host-b/small-model. Pick one"
        " with --litellm" in flat(out.stderr)
    )
    assert "note: It sends no key" in out.stderr
    none = add("org/other-model", "--base-url", URL)
    assert "No LiteLLM row names it. aims models prices --search" in flat(none.stderr)
    fakes.prices = None
    down = add(*BIG)
    assert down.exit_code == 0, down.output
    assert "note: No copy of LiteLLM's price map is at hand: down." in flat(down.stderr)
    row = add(*BIG, "--litellm", "big-model")
    assert (row.exit_code, row.stdout) == (1, "")
    assert "error: no copy of LiteLLM's price map is at hand to read big-model: down" in flat(
        row.stderr
    )


def test_a_list_that_cannot_be_read_is_a_note(fakes: Fakes) -> None:
    fakes.list_problem = Refused("x resolves to a link-local or cloud metadata address")
    out = add(*BIG)
    assert out.exit_code == 0, out.output
    assert "The server's list of models was not read: x resolves to a link-local" in flat(
        out.stderr
    )
    fakes.list_problem = None
    missing = add("org/big-modl", "--base-url", URL)
    assert "The server's list of models has no org/big-modl. Close: org/big-model," in flat(
        missing.stderr
    )


def test_write_adds_the_checked_profile_with_its_roles(fakes: Fakes, tmp_path: Path) -> None:
    out = add(*BIG, "--write", "--role", "agent", "--role", "judge")
    assert out.exit_code == 0, out.output
    path = tmp_path / "aims.toml"
    assert out.stdout == ""
    assert f"added big-model to {path}" in flat(out.stderr)
    assert "role agent: big-model\nrole judge: big-model" in out.stderr
    models = load(path).models
    assert models.get("big-model").reasoning == "full"
    assert (models.roles.agent, models.roles.judge) == ("big-model", "big-model")
    again = add(*BIG, "--write")
    assert again.exit_code == 1
    assert "a profile named big-model exists; pass --name for another" in again.stderr
    assert len(fakes.checked) == 1
    second = add(*BIG, "--write", "--name", "second", "--role", "agent")
    assert second.exit_code == 0, second.output
    assert "role agent: second (was big-model)" in second.stderr


def test_a_failed_check_prints_the_profile_and_writes_nothing(fakes: Fakes, tmp_path: Path) -> None:
    fakes.verdicts = {"structured": "fail"}
    out = add(*BIG, "--write", "--role", "agent")
    assert out.exit_code == 1
    assert out.stdout == draft(BIG[0], base_url=URL, key_env="MODELS_KEY").measured("full").toml()
    err = flat(out.stderr)
    assert "fail  big-model  structured: seen" in out.stderr
    assert (
        "error: big-model failed the check: structured, so it is not added. Change it with"
        " --set, or pass --no-check to skip the check" in err
    )
    assert "note: --set structured_output=false adds it as a model without structured" in err
    assert not (tmp_path / "aims.toml").exists()
    judge = add(*BIG, "--write", "--role", "judge")
    assert "structured_output=false" not in judge.stderr
    fakes.verdicts = {"tools": "fail", "structured": "fail"}
    printed = add(*BIG)
    assert printed.exit_code == 1
    assert "failed the check: tools, structured. Change it" in flat(printed.stderr)
    assert "structured_output=false" not in printed.stderr
    fixed = add(*BIG, "--write", "--no-check", "--set", "structured_output=false")
    assert fixed.exit_code == 0, fixed.output
    assert load(tmp_path / "aims.toml").models.get("big-model").structured_output is False


def test_no_check_adds_the_draft_unchecked(fakes: Fakes, tmp_path: Path) -> None:
    monkeyless = add(*BIG, "--no-check", "--write")
    assert monkeyless.exit_code == 0, monkeyless.output
    assert fakes.checked == []
    err = flat(monkeyless.stderr)
    assert 'note: reasoning = "none" is a guess.' in err
    assert "note: It is not checked. aims models check big-model checks it." in err
    assert load(tmp_path / "aims.toml").models.get("big-model").reasoning == "none"
    assert not (tmp_path / "runs").exists()


def run_of(out: Any) -> str:
    [run_id] = [line[4:] for line in out.output.splitlines() if line.startswith("run ")]
    return run_id


def local_ui(at: Path, url: str, token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server.json as a local `aims ui` writes it."""
    at.mkdir()
    (at / "server.json").write_text(json.dumps({"url": url, "token": token}))
    monkeypatch.setenv("AIMS_SERVER_DIR", str(at))


async def test_a_check_ships_to_the_local_ui_so_the_app_shows_it(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed check ships too. Once the check stayed in the runs dir. The app then said the
    model was never checked."""
    async with control_plane(tmp_path / "app") as cp:
        local_ui(tmp_path / "local-ui", cp.url, cp.runner, monkeypatch)
        monkeypatch.chdir(cp.project.parent)
        added = await asyncio.to_thread(add, *BIG, "--write")
        assert added.exit_code == 0, added.output
        async with cp.client(cp.viewer) as c:
            r = await c.get("/api/models")
        [shown] = [p["check"] for p in r.json()["profiles"] if p["name"] == "big-model"]
        assert shown is not None
        assert shown["run_id"] == run_of(added)
        assert [c["verdict"] for c in shown["checks"]] == ["ok"] * 4
        fakes.verdicts = {"tools": "fail"}
        failed = await asyncio.to_thread(add, *BIG, "--name", "failed", "--write")
        assert failed.exit_code == 1
        fakes.verdicts = {}
        kept = await asyncio.to_thread(add, *BIG, "--name", "kept", "--write", "--no-server")
        assert kept.exit_code == 0, kept.output
        held = {run.run_id for run in await cp.state.store.runs(kind="models.check")}
    assert "warning" not in added.stderr + failed.stderr
    assert held == {run_of(added), run_of(failed)}


async def test_models_check_ships_to_the_local_ui_too(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with control_plane(tmp_path / "app") as cp:
        local_ui(tmp_path / "local-ui", cp.url, cp.runner, monkeypatch)
        monkeypatch.chdir(cp.project.parent)
        out = await asyncio.to_thread(CliRunner().invoke, app, ["models", "check", "scripted"])
        assert out.exit_code == 0, out.output
        async with cp.client(cp.viewer) as c:
            r = await c.get("/api/models")
    [shown] = [p["check"] for p in r.json()["profiles"] if p["name"] == "scripted"]
    assert shown is not None
    assert shown["run_id"] == run_of(out)


def test_a_check_that_does_not_reach_the_control_plane_only_warns(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_ui(tmp_path / "local-ui", "http://127.0.0.1:1", "t", monkeypatch)
    out = add(*BIG, "--write")
    assert out.exit_code == 0, out.output
    assert "warning: the check did not reach the control plane: " in out.stderr
    assert load(tmp_path / "aims.toml").models.get("big-model").reasoning == "full"


def test_what_is_refused_for_free_is_refused_before_the_check(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "aims.toml"
    inline = (
        "profiles = { a = { backend = 'chat', base_url = 'https://a.example/v1', model = 'a',"
        " weights = 'a', reasoning = 'none' } }\n"
    )
    path.write_text(inline)
    for args, problem in (
        # an inline `profiles` table cannot take another table
        ([*BIG, "--write"], f"error: {path}: "),
        ([*BIG, "--name", "a", "--write"], "a profile named a exists"),
        (
            [*BIG, "--set", "structured_output=false", "--write", "--role", "sweep"],
            "big-model cannot take the sweep role: structured_output is false",
        ),
        ([*BIG, "--set", "max_output_tokens=0"], "error: max_output_tokens: "),
        (
            ["org/big-model", "--base-url", URL, "--key-env", "NOPE"],
            f"AIMS_KEYS lacks NOPE={URL}; add it there or pass --key NOPE={URL}, or pass"
            " --no-check to skip the check",
        ),
    ):
        out = add(*args)
        assert (out.exit_code, out.stdout) == (1, ""), (args, out.output)
        assert problem in flat(out.stderr), (args, out.stderr)
    assert fakes.checked == []
    assert path.read_text() == inline
    assert sorted(p.name for p in tmp_path.iterdir()) == [".git", "aims.toml"]
    monkeypatch.delenv("MODELS_KEY")
    keyless = add(*BIG)
    assert "error: profile big-model: MODELS_KEY is not set; set it, or pass --no-check" in flat(
        keyless.stderr
    )


def test_the_flags_are_checked_before_anything_is_read(fakes: Fakes, tmp_path: Path) -> None:
    (tmp_path / "aims.toml").write_text("[nope]\n")
    for args, problem in (
        ([*BIG, "--role", "agent"], "needs --write"),
        ([*BIG, "--write", "--role", "boss"], "use agent, sweep or judge"),
        (["m", "--from", "a", "--base-url", URL], "give --from or --base-url, not both"),
        (["m", "--from", "a", "--key-env", "K"], "give --from or --base-url, not both"),
        (["m"], "give the server's --base-url, or --from"),
        (["m", "--base-url", URL, "--backend", "rest"], "use responses or chat"),
    ):
        out = add(*args)
        assert out.exit_code == 2, (args, out.output)
        assert problem in flat(out.stderr), (args, out.stderr)
    broken = add(*BIG, "--write")
    assert broken.exit_code == 1
    assert "unknown table(s) nope" in broken.stderr
    assert (fakes.reads, fakes.checked) == ([], [])


def test_from_reaches_the_server_as_a_profile_does(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "aims.toml").write_text(
        """
[profiles.router]
backend = "responses"
base_url_env = "ROUTER_URL"
model = "org/base-model"
weights = "org/base-model"
api_key_env = "MODELS_KEY"
reasoning = "full"
headers = { "X-Title" = "AIMS" }
"""
    )
    unset = add("org/big-model", "--from", "router")
    assert (unset.exit_code, unset.stdout) == (1, "")
    assert "error: profile big-model: ROUTER_URL is not set; set it" in flat(unset.stderr)
    unchecked = add("org/big-model", "--from", "router", "--no-check")
    assert unchecked.exit_code == 0, unchecked.output
    assert "The server's list of models was not read: profile router: ROUTER_URL is not set" in (
        flat(unchecked.stderr)
    )
    monkeypatch.setenv("ROUTER_URL", f"{URL}/")
    out = add("org/big-model", "--from", "router", "--write", "--role", "agent")
    assert out.exit_code == 0, out.output
    assert fakes.reads == [URL]
    made = load(tmp_path / "aims.toml").models.get("big-model")
    assert (made.backend, made.base_url_env, made.headers) == (
        "responses",
        "ROUTER_URL",
        {"X-Title": "AIMS"},
    )
    assert "From router it takes backend, base_url_env, api_key_env, headers." in flat(out.stderr)
    unknown = add("m", "--from", "nope")
    assert unknown.exit_code == 1
    assert "unknown profile 'nope'" in unknown.stderr


def test_add_shows_hidden_characters_of_outside_text_as_text(fakes: Fakes) -> None:
    out = add("a/b\x1b[31m", "--base-url", URL)
    assert out.exit_code == 1
    assert "\x1b" not in out.stderr


# The file it writes


def test_a_trial_add_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text("# mine\n")
    table = draft("m", base_url=URL).toml()
    assert add_table(layers(path), table, {"agent": "m"}, trial=True) == path
    assert path.read_text() == "# mine\n"
    assert [p.name for p in tmp_path.iterdir()] == ["aims.toml"]


def test_write_keeps_the_files_text_and_mode_and_the_role_tables_place(
    fakes: Fakes, tmp_path: Path
) -> None:
    path = tmp_path / "aims.toml"
    before = '# mine\n[roles]\nagent = "old"  # kept\n'
    path.write_text(
        before + '\n[profiles.old]\nbackend = "chat"\nbase_url = "https://o.example/v1"\n'
        'model = "o"\nweights = "o"\nreasoning = "none"\n'
    )
    path.chmod(0o600)
    text = path.read_text()
    out = add(*BIG, "--write", "--role", "agent")
    assert out.exit_code == 0, out.output
    table = draft(BIG[0], base_url=URL, key_env="MODELS_KEY").measured("full").toml()
    assert path.read_text() == text.replace('"old"  # kept', '"big-model"  # kept') + ("\n" + table)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "role agent: big-model (was old)" in out.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == [".git", "aims.toml", "runs"]


def test_write_goes_to_the_file_config_names(fakes: Fakes, tmp_path: Path) -> None:
    other = tmp_path / "other.toml"
    other.write_text("")
    out = add(*BIG, "--write", "--config", str(other))
    assert out.exit_code == 0, out.output
    assert other.read_text() == (
        draft(BIG[0], base_url=URL, key_env="MODELS_KEY").measured("full").toml()
    )
    assert not (tmp_path / "aims.toml").exists()


def test_a_table_the_layers_do_not_load_with_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text("profiles = { a = { backend = 'chat' } }\n")
    found = layers(path)
    before = path.read_bytes()
    # an inline `profiles` table cannot take another table
    with pytest.raises(ConfigError, match=rf"^{path}: ") as e:
        add_table(found, draft("m", base_url=URL).toml())
    assert ".aims.toml." not in str(e.value)
    assert path.read_bytes() == before
    path.write_text("")
    with pytest.raises(ProfileError, match="Field required"):
        add_table(layers(path), '[profiles.half]\nbackend = "chat"\n')
    assert path.read_bytes() == b""
    assert [p.name for p in tmp_path.iterdir()] == ["aims.toml"]


def test_a_table_is_added_after_a_last_line_with_no_newline(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_bytes(b"[credits]\r\nexample = 1")
    table = draft("m", base_url=URL).toml()
    assert add_table(layers(path), table) == path
    assert path.read_bytes() == b"[credits]\r\nexample = 1\n\n" + table.encode()
    path.write_bytes(b"[credits]\nexample = 1\n")
    add_table(layers(path), draft("second", base_url=URL).toml())
    assert path.read_bytes().startswith(b"[credits]\nexample = 1\n\n[profiles.")
    assert json.dumps(sorted(load(path).credits.items())) == '[["example", 1.0]]'


NEW_TABLE = '[profiles.new]\nmodel = "m"\n'
OLD_TABLE = '[profiles.a]\nmodel = "a"\n'


@pytest.mark.parametrize(
    ("old", "want"),
    [
        ("", NEW_TABLE),
        (OLD_TABLE, OLD_TABLE + "\n" + NEW_TABLE),
        (
            OLD_TABLE + '\n[roles]\nagent = "a"\n',
            OLD_TABLE + "\n" + NEW_TABLE + '\n[roles]\nagent = "a"\n',
        ),
        (
            OLD_TABLE + '[roles]  # who runs what\nagent = "a"',
            OLD_TABLE + "\n" + NEW_TABLE + '\n[roles]  # who runs what\nagent = "a"',
        ),
        # the comment right over [roles] stays with it; one a blank line apart stays put
        (
            OLD_TABLE + "\n# about a\n\n# the roles\n[roles]\n",
            OLD_TABLE + "\n# about a\n\n" + NEW_TABLE + "\n# the roles\n[roles]\n",
        ),
        (
            '[roles]\nagent = "a"\n\n' + OLD_TABLE,
            '[roles]\nagent = "a"\n\n' + OLD_TABLE + "\n" + NEW_TABLE,
        ),
        ('[roles]\nagent = "a"\n', NEW_TABLE + '\n[roles]\nagent = "a"\n'),
        # [roles] ends the file only inside a string, so the values would change
        (
            '[prompts]\nsystem = """\n[roles]\n"""\n',
            '[prompts]\nsystem = """\n[roles]\n"""\n\n' + NEW_TABLE,
        ),
        ("[roles]\n[[runs]]\nx = 1\n", "[roles]\n[[runs]]\nx = 1\n\n" + NEW_TABLE),
        ("[roles\n", "[roles\n\n" + NEW_TABLE),
    ],
    ids=[
        "empty",
        "no roles",
        "roles last",
        "roles with a comment, no newline",
        "comments",
        "roles first",
        "only roles",
        "roles in a string",
        "array of tables after",
        "not TOML",
    ],
)
def test_a_new_profile_goes_above_roles_when_they_end_the_file(old: str, want: str) -> None:
    assert appended(old.encode(), NEW_TABLE).decode() == want


def test_roles_are_set_in_the_table_the_file_has() -> None:
    roles: dict[Role, str] = {"sweep": "b", "agent": "b"}
    for text, want in (
        ("", '[roles]\nagent = "b"\nsweep = "b"\n'),
        ('roles.agent = "a"\n', 'roles.agent = "b"\nroles.sweep = "b"\n'),
        (
            'roles = { judge = "a" }  # c\n',
            'roles = {judge = "a", agent = "b", sweep = "b"}  # c\n',
        ),
    ):
        assert with_roles(text, roles) == want
    for text, problem in (("roles = 1\n", "roles is not a table"), ("[a", "not TOML")):
        with pytest.raises(ConfigError, match=problem):
            with_roles(text, roles)


def test_a_role_table_that_cannot_take_a_role_leaves_the_file(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text("roles = 1\n")
    table = draft("m", base_url=URL).toml()
    with pytest.raises(ConfigError, match=rf"^{path}: roles is not a table"):
        add_table(layers(path), table, {"agent": "m"})
    assert path.read_text() == "roles = 1\n"
    assert [p.name for p in tmp_path.iterdir()] == ["aims.toml"]
