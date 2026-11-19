"""AIMS_KEYS: a key goes only to the URLs listed for it, and AIMS reads no env var it does not
list for a model call."""

import json
import os
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from proxy import Server, environ, serving
from typer.testing import CliRunner

from aims.cli import app
from aims.events import LocalEventStore, RunFinished, RunStarted
from aims.models import (
    KeyRefused,
    Limits,
    ModelClient,
    ModelsConfig,
    Profile,
    ProfileError,
    Prompt,
    Recording,
    Roles,
    load_models,
)
from aims.models.check import record_check
from aims.models.client import REPLAY_URL
from aims.models.keys import KEYS, Keys, KeysError, parse, place
from aims.models.types import Message
from aims.stored import STORED

CANARY = "CANARY-5c1e"
KEY = f"sk-{CANARY}"
URL = "https://api.example.com/v1"
PROMPT = Prompt(items=[Message(role="user", content="hi")])

pytestmark = pytest.mark.anyio


def told(e: BaseException) -> str:
    return "".join(traceback.format_exception(e))


def holding(root: Path, text: str) -> list[Path]:
    return [f for f in root.rglob("*") if f.is_file() and text.encode() in f.read_bytes()]


def profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "test",
        "backend": "chat",
        "base_url": URL,
        "model": "m",
        "weights": "w",
        "reasoning": "none",
        "stream": False,
        "limits": Limits(group=f"keys-{id(kw)}"),
    }
    return Profile.model_validate(base | kw)


def config(p: Profile) -> ModelsConfig:
    return ModelsConfig(profiles={p.name: p}, roles=Roles(agent=p.name))


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"{URL}/",
        f"{URL}/chat/completions",
        "https://API.Example.COM/v1",
        "https://api.example.com:443/v1",
        "https://api.example.com./v1",
        "https://api.example.com/v2/../v1/x",
    ],
)
def test_a_key_goes_to_its_url_and_the_paths_under_it(url: str) -> None:
    assert parse(f"K={URL}").lets("K", url, {})


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://api.example.com:8443/v1",
        "https://api.example.com.evil.test/v1",
        "https://api.example.com@evil.test/v1",
        "https://evil.test/api.example.com/v1",
        "https://sub.api.example.com/v1",
        "https://example.com/v1",
        "https://api.example.com/v10",
        "https://api.example.com/V1",
        "https://api.example.com/v",
        "https://api.example.com/",
        "https://api.example.com/v1/../v2",
        "https://api.example.com/v1/%2e%2e/admin",
        "https://api.example.com/v1%2Fx",
        "https://api.example.com/v1/x?y=1",
        "https://api.example.com/v1#x",
        "https://u:p@api.example.com/v1",
        "ftp://api.example.com/v1",
        "api.example.com/v1",
    ],
)
def test_a_key_goes_nowhere_else(url: str) -> None:
    assert not parse(f"K={URL}").lets("K", url, {})


def test_scheme_host_and_port_match_as_listed() -> None:
    keys = parse(
        "LOCAL=http://localhost:8000/v1,ROOT=https://root.example,SIX=http://[::1]:8080/v1,"
        "IDN=https://bücher.example/v1,PORT=https://a.example:443"
    )
    assert keys.lets("LOCAL", "http://localhost:8000/v1", {})
    assert not keys.lets("LOCAL", "https://localhost:8000/v1", {})
    assert not keys.lets("LOCAL", "http://localhost/v1", {})
    assert keys.lets("ROOT", "https://root.example/any/path", {})
    assert keys.lets("SIX", "http://[::1]:8080/v1/chat", {})
    assert keys.lets("IDN", "https://xn--bcher-kva.example/v1", {})
    assert keys.lets("PORT", "https://a.example/x", {})
    assert not keys.lets("ROOT", URL, {})
    assert not keys.lets("NONE", "https://root.example", {})


def test_a_name_may_repeat_and_spaces_and_empty_entries_are_ignored() -> None:
    keys = parse(f" K={URL} , ,K=https://b.example/v2, env:U ,")
    assert keys.lets("K", URL, {})
    assert keys.lets("K", "https://b.example/v2/x", {})
    assert keys.urls == {"U"}
    assert parse("") == Keys()


def test_a_key_bound_to_a_url_var_goes_to_the_url_it_holds() -> None:
    keys = parse("K=env:ROUTER_URL")
    assert keys.urls == {"ROUTER_URL"}
    assert keys.lets("K", URL, {"ROUTER_URL": f"{URL}/"})
    assert keys.lets("K", f"{URL}/chat", {"ROUTER_URL": URL})
    assert not keys.lets("K", URL, {})
    assert not keys.lets("K", URL, {"ROUTER_URL": "https://other.example/v1"})
    assert not keys.lets("K", URL, {"ROUTER_URL": "https://u:p@api.example.com/v1"})
    assert not keys.lets("K", URL, {"ROUTER_URL": f"{URL}?x=1"})


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (CANARY, "AIMS_KEYS is not NAME=URL, NAME=env:VAR or env:VAR"),
        (f"K={CANARY}", "AIMS_KEYS: K= takes env:VAR or an http or https URL"),
        (f"K={KEY}", "AIMS_KEYS: K= takes env:VAR or an http or https URL"),
        (f"K=ftp://{CANARY}.test", "AIMS_KEYS: K= takes env:VAR or an http or https URL"),
        (f"K=https://u:{CANARY}@a.example", "AIMS_KEYS: K= takes env:VAR or an http or https"),
        (f"K=https://a.example/v1?key={CANARY}", "AIMS_KEYS: K= takes env:VAR or an http"),
        (f"K=https://a.example/v1#{CANARY}", "AIMS_KEYS: K= takes env:VAR or an http or https"),
        (f"K=https://a.example/{CANARY}%41", "AIMS_KEYS: K= takes env:VAR or an http or https"),
        (f"K=https://[{CANARY}]/v1", "AIMS_KEYS: K= takes env:VAR or an http or https URL"),
        (f"{KEY}=https://a.example", "AIMS_KEYS: a variable name takes letters, digits and _"),
        (f"K=env:{KEY}", "AIMS_KEYS: a variable name takes letters, digits and _"),
        (f"env:{CANARY}", "AIMS_KEYS: a variable name takes letters, digits and _"),
        (f"A=https://a.example,{CANARY}", "AIMS_KEYS entry 2 is not NAME=URL"),
        ("AIMS_TOKEN=https://a.example", "AIMS_KEYS: AIMS_TOKEN starts with AIMS_"),
        ("aims_token=https://a.example", "AIMS_KEYS: aims_token starts with AIMS_"),
        ("K=env:AIMS_TOKEN", "AIMS_KEYS: AIMS_TOKEN starts with AIMS_"),
        ("env:AIMS_KEYS", "AIMS_KEYS: AIMS_KEYS starts with AIMS_"),
        (f"K={URL},env:K", "AIMS_KEYS lists K as a key and as a URL var"),
        ("K=env:K", "AIMS_KEYS lists K as a key and as a URL var"),
        ("K=env:U,J=env:K", "AIMS_KEYS lists K as a key and as a URL var"),
    ],
)
def test_an_entry_aims_cannot_read_is_refused_without_what_follows_its_name(
    text: str, problem: str
) -> None:
    with pytest.raises(KeysError) as info:
        parse(text)
    assert str(info.value).startswith(problem)
    assert info.value.__context__ is None
    assert CANARY not in told(info.value)


def test_place_parses_as_the_http_client_does() -> None:
    assert place("https://API.example.com:443/v1/") == place("https://api.example.com/v1")
    assert place("https://a.example/v1/../v2") == place("https://a.example/v2")
    for bad in ["https://a.example/\udcff", "http://", "https://u@a.example", "https://a.example?"]:
        assert place(bad) is None


@pytest.mark.parametrize(
    ("fields", "entry"),
    [
        ({"api_key_env": "K"}, f"K={URL}"),
        ({"api_key_env": "K", "base_url": None, "base_url_env": "U"}, "K=env:U"),
        ({"base_url": None, "base_url_env": "U"}, "env:U"),
    ],
)
@pytest.mark.parametrize("held", [True, False])
def test_a_var_aims_keys_does_not_list_is_refused_the_same_whether_it_is_set_or_not(
    fields: dict[str, Any], entry: str, held: bool
) -> None:
    p = profile(**fields)
    env = {"K": KEY, "U": URL} if held else {}
    with pytest.raises(KeyRefused) as info:
        p.api_key(p.url(env), env)
    assert str(info.value) == (
        f"profile test: AIMS_KEYS lacks {entry}; add it there or pass --key {entry}"
    )
    assert info.value.entry == entry
    assert CANARY not in told(info.value)
    # the line it gives is the fix
    fixed = {**env, KEYS: entry}
    if held:
        assert p.api_key(p.url(fixed), fixed) == (KEY if p.api_key_env else None)


@pytest.mark.parametrize("held", [True, False])
def test_a_key_listed_for_another_url_is_refused_the_same_whether_it_is_set_or_not(
    held: bool,
) -> None:
    p = profile(api_key_env="K", base_url="https://evil.example/v1")
    env = {KEYS: f"K={URL}"} | ({"K": KEY} if held else {})
    with pytest.raises(KeyRefused) as info:
        p.api_key(p.url(env), env)
    assert str(info.value) == (
        "profile test: AIMS_KEYS lacks K=https://evil.example/v1; add it there or pass"
        " --key K=https://evil.example/v1"
    )


@pytest.mark.parametrize("held", [True, False])
@pytest.mark.parametrize("fields", [{"api_key_env": "AIMS_TOKEN"}, {"api_key_env": "aims_x"}])
def test_aims_never_reads_its_own_vars_for_a_model(fields: dict[str, Any], held: bool) -> None:
    p = profile(**fields)
    name = fields["api_key_env"]
    env = {KEYS: f"K={URL}"} | ({name: KEY} if held else {})
    with pytest.raises(ProfileError) as info:
        p.api_key(p.url(env), env)
    assert str(info.value) == (
        f"profile test: {name} starts with AIMS_, which AIMS keeps for its own variables;"
        " name another variable"
    )
    q = profile(base_url=None, base_url_env="AIMS_TOKEN")
    with pytest.raises(ProfileError, match="AIMS_TOKEN starts with AIMS_"):
        q.url({"AIMS_TOKEN": URL})


def test_a_profile_with_a_url_and_no_key_needs_no_list() -> None:
    p = profile()
    assert p.url({}) == URL
    assert p.api_key(URL, {}) is None


def test_a_var_listed_as_a_key_is_not_read_as_a_url_nor_the_other_way() -> None:
    env = {KEYS: f"K={URL},env:U", "K": URL, "U": KEY}
    with pytest.raises(KeyRefused):
        profile(base_url=None, base_url_env="K").url(env)
    with pytest.raises(KeyRefused):
        profile(api_key_env="U").api_key(URL, env)


def test_aims_keys_it_cannot_read_fails_every_profile_that_reads_an_env_var() -> None:
    env = {KEYS: f"K={KEY}", "K": KEY}
    with pytest.raises(ProfileError) as info:
        profile(api_key_env="K").api_key(URL, env)
    assert str(info.value).startswith("AIMS_KEYS: K= takes env:VAR or an http or https URL")
    assert CANARY not in told(info.value)
    assert profile().api_key(URL, env) is None


@pytest.mark.parametrize(
    "url", [f"https://api.example.com/v1?key={CANARY}", f"https://api.example.com/{CANARY}%2F"]
)
def test_no_key_goes_to_a_url_aims_cannot_match(url: str) -> None:
    p = profile(api_key_env="K", base_url=None, base_url_env="U")
    env = {KEYS: "K=env:U", "U": url, "K": KEY}
    with pytest.raises(ProfileError) as info:
        p.api_key(p.url(env), env)
    assert not isinstance(info.value, KeyRefused)
    assert str(info.value) == (
        "profile test: AIMS sends no key to a URL with a user name, password, query, fragment"
        " or % in its path"
    )
    assert CANARY not in told(info.value)


def test_a_url_with_a_user_name_or_password_is_refused_without_it() -> None:
    p = profile(base_url=None, base_url_env="U")
    env = {KEYS: "env:U", "U": f"https://user:{CANARY}@api.example.com/v1"}
    with pytest.raises(ProfileError) as info:
        p.url(env)
    assert str(info.value) == (
        "profile test: U holds a user name or password, which the run would record; put the"
        " key in api_key_env"
    )
    assert CANARY not in told(info.value)
    with pytest.raises(ValueError, match="the URL holds a user name or password") as refused:
        profile(base_url=f"https://user:{CANARY}@api.example.com/v1")
    assert CANARY not in told(refused.value)


def test_a_config_url_with_a_password_is_refused_without_it_and_a_stored_one_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aims.toml"
    url = f"https://user:{CANARY}@api.example.com/v1"
    path.write_text(
        f'[profiles.t]\nbackend = "chat"\nbase_url = "{url}"\nmodel = "m"\nweights = "w"\n'
        'reasoning = "none"\n'
    )
    with pytest.raises(ProfileError, match="the URL holds a user name or password") as info:
        load_models(path)
    assert CANARY not in told(info.value)
    stored = profile().model_dump(mode="json") | {"base_url": url}
    assert Profile.model_validate(stored, context=STORED).base_url == url


async def test_no_key_leaves_without_a_list_and_one_does_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the real wire: with no AIMS_KEYS the check sends nothing and nothing it records
    holds the key. Listed, the request goes out."""
    environ(monkeypatch)
    monkeypatch.setenv("CANARY_KEY", KEY)
    root = tmp_path / "runs"
    async with serving(Server()) as server, await LocalEventStore.open(root) as store:
        p = profile(api_key_env="CANARY_KEY", base_url=f"{server.url}/v1")
        with pytest.raises(KeyRefused):
            await record_check(store, config(p), [p])
        (run_id,) = store.run_ids()
        finished = (await store.every(run_id))[-1].payload()
        assert server.lines == []
        assert isinstance(finished, RunFinished)
        assert finished.status == "failed"
        assert finished.reason is not None
        assert finished.reason.startswith("KeyRefused: profile test: AIMS_KEYS lacks CANARY_KEY=")
        assert holding(root, CANARY) == []

        monkeypatch.setenv(KEYS, f"CANARY_KEY={server.url}/v1")
        await record_check(store, config(p), [p])
    assert server.lines
    assert all(line.startswith("POST /v1/chat/completions ") for line in server.lines)


async def test_the_client_sends_the_key_only_where_aims_keys_lets_it(
    tmp_path: Path,
) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(500)

    p = profile(api_key_env="K", retries={"attempts": 1})
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(RunStarted(scenario="keys", oversight="observe"))
        for env in [{"K": KEY}, {"K": KEY, KEYS: "K=https://api.example.com/v2"}]:
            async with ModelClient(
                store, run_id, config(p), transport=httpx2.MockTransport(answer), env=env
            ) as mc:
                with pytest.raises(KeyRefused):
                    await mc.call("test", PROMPT)
        assert sent == []
        env = {"K": KEY, KEYS: f"K={URL}"}
        async with ModelClient(
            store, run_id, config(p), transport=httpx2.MockTransport(answer), env=env
        ) as mc:
            with pytest.raises(Exception, match="failed after 1 attempt"):
                await mc.call("test", PROMPT)
    assert [r.headers["authorization"] for r in sent] == [f"Bearer {KEY}"]
    assert str(sent[0].url) == f"{URL}/chat/completions"


class Once(httpx2.AsyncByteStream):
    """A body the recorder reads as it streams, as from a provider."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


async def test_a_replay_needs_no_list(tmp_path: Path) -> None:
    """A replay sends nothing, so it reads no key and no URL var AIMS_KEYS does not list."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        message = {"role": "assistant", "content": "ok"}
        choice = {"index": 0, "message": message, "finish_reason": "stop"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        body = {"id": "c", "object": "chat.completion", "model": "m", "choices": [choice]}
        data = json.dumps(body | {"usage": usage}).encode()
        return httpx2.Response(200, headers={"content-type": "application/json"}, stream=Once(data))

    p = profile(api_key_env="K", base_url=None, base_url_env="U")
    held = {"K": KEY, "U": URL}
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        source = await store.create_run(RunStarted(scenario="keys", oversight="observe"))
        env = held | {KEYS: "K=env:U"}
        async with ModelClient(
            store, source, config(p), transport=httpx2.MockTransport(answer), env=env
        ) as mc:
            first = await mc.call("test", PROMPT)
        recording = await Recording.load(store, source)
        run_id = await store.create_run(RunStarted(scenario="keys", oversight="observe"))
        for env in [held, {}]:
            async with ModelClient(store, run_id, config(p), replay=recording, env=env) as mc:
                assert (await mc.call("test", PROMPT)).turn == first.turn
            recording = await Recording.load(store, source)
        urls = [e.data["url"] for e in await store.events(run_id, types=["model.request"])]
    assert urls == [f"{REPLAY_URL}/chat/completions"] * 2


def cli_profile(tmp_path: Path) -> Path:
    path = tmp_path / "aims.toml"
    path.write_text(
        f"""
[profiles.served]
backend = "chat"
base_url = "{URL}"
api_key_env = "CANARY_KEY"
model = "m"
weights = "w"
reasoning = "none"
"""
    )
    return path


def test_aims_models_check_says_the_line_to_add_and_takes_it_from_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cli_profile(tmp_path)
    monkeypatch.setenv("CANARY_KEY", KEY)
    monkeypatch.setenv(KEYS, "env:OTHER_URL")
    seen: list[str | None] = []

    async def check(*args: Any, **kw: Any) -> list[Any]:
        seen.append(os.environ.get(KEYS))
        return []

    monkeypatch.setattr("aims.cli._check", check)
    args = ["models", "check", "served", "--config", str(path), "--runs-dir", str(tmp_path / "r")]
    refused = CliRunner().invoke(app, args)
    assert refused.exit_code == 1
    assert refused.output == (
        f"error: profile served: AIMS_KEYS lacks CANARY_KEY={URL}; add it there or pass"
        f" --key CANARY_KEY={URL}\n"
    )
    assert seen == []
    taken = CliRunner().invoke(app, [*args, "--key", f"CANARY_KEY={URL}"])
    assert taken.exit_code == 0, taken.output
    assert seen == [f"env:OTHER_URL,CANARY_KEY={URL}"]
    assert CANARY not in refused.output + taken.output


@pytest.mark.parametrize(
    ("env", "given", "problem"),
    [
        (None, f"CANARY_KEY={KEY}", "error: --key: CANARY_KEY= takes env:VAR or an http"),
        (None, f"{KEY}", "error: --key is not NAME=URL, NAME=env:VAR or env:VAR"),
        (None, "AIMS_TOKEN=https://a.example", "error: --key: AIMS_TOKEN starts with AIMS_"),
        (f"A={URL},K={KEY}", None, "error: AIMS_KEYS entry 2: K= takes env:VAR or an http"),
        ("env:K", f"K={URL}", "error: AIMS_KEYS lists K as a key and as a URL var"),
    ],
)
def test_a_key_list_aims_cannot_read_stops_the_command_before_it_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    given: str | None,
    problem: str,
) -> None:
    path = cli_profile(tmp_path)
    if env is not None:
        monkeypatch.setenv(KEYS, env)

    async def check(*args: Any, **kw: Any) -> list[Any]:
        raise AssertionError("the command ran")

    monkeypatch.setattr("aims.cli._check", check)
    args = ["models", "check", "served", "--config", str(path)]
    result = CliRunner().invoke(app, args + (["--key", given] if given else []))
    assert result.exit_code == 1
    assert result.output.startswith(problem)
    assert CANARY not in result.output
    assert os.environ.get(KEYS) == env


@pytest.mark.parametrize(
    "command",
    [
        ["run"],
        ["models", "check"],
        ["models", "add"],
        ["monitor"],
        ["score"],
        ["runner"],
        ["ui"],
    ],
)
def test_every_command_that_calls_a_model_or_starts_a_runner_takes_key(
    command: list[str],
) -> None:
    result = CliRunner().invoke(app, [*command, "--help"], terminal_width=200)
    assert result.exit_code == 0
    assert "--key" in result.output


def test_aims_models_list_says_what_aims_keys_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cli_profile(tmp_path)
    monkeypatch.setenv("CANARY_KEY", KEY)
    listed = CliRunner().invoke(app, ["models", "list", "--config", str(path)])
    assert listed.exit_code == 0, listed.output
    assert listed.output.split("\n")[0].endswith(f"needs CANARY_KEY={URL} in AIMS_KEYS")
    monkeypatch.setenv(KEYS, f"CANARY_KEY={URL}")
    listed = CliRunner().invoke(app, ["models", "list", "--config", str(path)])
    assert listed.output.split("\n")[0].endswith("  ready")
    monkeypatch.delenv("CANARY_KEY")
    listed = CliRunner().invoke(app, ["models", "list", "--config", str(path)])
    assert listed.output.split("\n")[0].endswith("  CANARY_KEY is not set")


@pytest.mark.parametrize("keyed", [True, False])
async def test_no_header_the_sdk_takes_from_the_env_goes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyed: bool
) -> None:
    """The OpenAI SDK adds headers from OPENAI_CUSTOM_HEADERS, an Authorization header among
    them, and from OPENAI_ORG_ID and OPENAI_PROJECT_ID, to a request to any URL."""
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", f"Authorization: Bearer {KEY}\nX-Env: {CANARY}")
    monkeypatch.setenv("OPENAI_ORG_ID", f"org-{CANARY}")
    monkeypatch.setenv("OPENAI_PROJECT_ID", f"proj-{CANARY}")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(500)

    fields: dict[str, Any] = {"api_key_env": "K"} if keyed else {}
    p = profile(**fields, headers={"X-Title": "AIMS"}, retries={"attempts": 1})
    env = {"K": "sk-listed", KEYS: f"K={URL}"}
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(RunStarted(scenario="keys", oversight="observe"))
        async with ModelClient(
            store, run_id, config(p), transport=httpx2.MockTransport(answer), env=env
        ) as mc:
            with pytest.raises(Exception, match="failed after 1 attempt"):
                await mc.call("test", PROMPT)
    (request,) = sent
    assert request.headers["authorization"] == ("Bearer sk-listed" if keyed else "Bearer none")
    assert request.headers["x-title"] == "AIMS"
    assert request.headers["x-stainless-retry-count"] == "0"
    assert CANARY not in str(request.headers.raw)
    assert KEY not in str(request.headers.raw)
