"""The profile lookup and add behind Settings > Models. The lookup reads the server's list of
models with no key and answers a profile as config file text; it saves nothing. The add saves
the profile and queues its check; once the check passes the server gives the profile its roles
and the reasoning the check saw."""

import contextlib
import tomllib
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from cp import CP, control_plane, start, stop, write_project
from test_listprice import BODY
from test_server import FakeRunner, ship

from aims.events import (
    Draft,
    LocalEventStore,
    ModelChecked,
    ReasoningKind,
    RunFinished,
    RunStarted,
    RunStatus,
)
from aims.events.store import LOG
from aims.models.add import ServerList, ServerLists, draft, fingerprint
from aims.models.check import Verdict
from aims.models.listprice import PriceLists
from aims.node import service
from aims.node.service import Node, NodeOptions
from aims.run.job import Job
from aims.server import edit_api
from aims.server.app import create_app
from aims.server.state import close_state, open_state
from aims.wire import OfferMessage, RunnerStatus, TracerHealth

pytestmark = pytest.mark.anyio
LOOKUP = "/api/models/lookup"
ADD = "/api/models/add"
BASE = "https://api.example.com/v1"
MODEL = "example/agent-model"
IDS = [MODEL, "example/agent-model-mini", "other/model-x"]
ASK = {"model": MODEL, "base_url": BASE, "key_env": "EXAMPLE_KEY"}
NAME = "agent-model"
TESTS = ("plain", "unstreamed", "tools", "structured")


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        prices(cp, tmp_path)
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


def prices(cp: CP, tmp_path: Path, status: int = 200) -> None:
    """The server reads LiteLLM's map from a test source that answers `status`."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=BODY if status == 200 else b"")

    cp.state.prices = PriceLists(
        tmp_path / "prices.json.gz", transport=httpx2.MockTransport(handle)
    )


def listed(cp: CP, body: Any = None, status: int = 200) -> list[httpx2.Request]:
    """The server reads `body` as each server's list of models. Returns the requests."""
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            status, json={"data": [{"id": i} for i in IDS]} if body is None else body
        )

    cp.state.server_lists = ServerLists(httpx2.MockTransport(handle))
    return seen


async def drafted(cp: CP, **params: str | None) -> dict[str, Any]:
    """The lookup of ASK with `params` over it; a None drops that parameter."""
    query = {k: v for k, v in {**ASK, **params}.items() if v is not None}
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params=query)
    assert r.status_code == 200, r.text
    return r.json()


# Lookup


async def test_an_operator_drafts_a_profile_and_nothing_is_saved(cp: CP) -> None:
    seen = listed(cp)
    before = cp.project.read_bytes()
    found = await drafted(cp)
    map_ = (await cp.state.prices.get()).prices
    want = draft(
        MODEL,
        base_url=BASE,
        key_env="EXAMPLE_KEY",
        listed=ServerList(f"{BASE}/models", tuple(IDS)),
        prices=map_,
    )
    assert found == {
        "name": NAME,
        "model": MODEL,
        "toml": want.toml(),
        "notes": list(want.notes),
        "exists": False,
        "fits": ["agent", "sweep", "judge"],
        "guessed": True,
        "candidates": list(want.candidates),
        "ids": [MODEL, "example/agent-model-mini"],
        "listed": 3,
    }
    assert found["toml"].startswith('[profiles.agent-model]\nbackend = "chat"\n')
    named = await drafted(cp, name="scripted")
    assert named["exists"] is True
    assert named["toml"].startswith("[profiles.scripted]\n")
    assert cp.project.read_bytes() == before
    # one read of the list serves both, and it sends no key
    assert [str(r.url) for r in seen] == [f"{BASE}/models"]
    assert "authorization" not in seen[0].headers


async def test_the_lookup_is_for_operators(cp: CP) -> None:
    seen = listed(cp)
    async with cp.client(cp.viewer) as c:
        assert (await c.get(LOOKUP, params=ASK)).status_code == 403
    async with cp.client(cp.runner) as c:
        assert (await c.get(LOOKUP, params=ASK)).status_code in (401, 403)
    async with cp.client() as c:
        assert (await c.get(LOOKUP, params=ASK)).status_code == 401
    assert seen == []


async def test_a_lookup_from_a_profile_reaches_its_server(cp: CP) -> None:
    seen = listed(cp)
    found = await drafted(cp, model="m2", base_url=None, key_env=None, **{"from": "scripted"})
    assert [str(r.url) for r in seen] == ["http://model.invalid/v1/models"]
    assert found["name"] == "m2"
    assert any(n.startswith("From scripted it takes") for n in found["notes"])
    assert 'base_url = "http://model.invalid/v1"' in found["toml"]


async def test_a_base_profile_whose_url_is_in_an_unset_env_var_gives_a_note(cp: CP) -> None:
    seen = listed(cp)
    cp.project.write_text(
        cp.project.read_text()
        + '\n[profiles.far]\nbackend = "chat"\nbase_url_env = "AIMS_TEST_URL_NOT_SET"\n'
        'model = "f"\nweights = "fw"\nreasoning = "none"\n'
    )
    found = await drafted(cp, base_url=None, key_env=None, **{"from": "far"})
    assert seen == []
    assert found["listed"] is None
    assert any(
        n.startswith("The server's list of models was not read:") and "AIMS_TEST_URL_NOT_SET" in n
        for n in found["notes"]
    ), found["notes"]
    assert 'base_url_env = "AIMS_TEST_URL_NOT_SET"' in found["toml"]


async def test_a_list_that_is_not_read_is_a_note_and_is_read_again(cp: CP) -> None:
    seen = listed(cp, {"error": "no"}, 401)
    for _ in range(2):
        found = await drafted(cp)
        assert (found["listed"], found["ids"]) == (None, [])
        assert "The server's list of models was not read: it needs a key (401)." in found["notes"]
    assert len(seen) == 2


async def test_a_list_that_lacks_the_model_names_the_close_ones(cp: CP) -> None:
    listed(cp)
    found = await drafted(cp, model="example/agent-modl")
    assert any("has no example/agent-modl. Close: example/agent-model" in n for n in found["notes"])
    assert found["ids"][0] == MODEL


@pytest.mark.parametrize("host", ["169.254.169.254", "[fd00:ec2::254]"])
async def test_the_lookup_never_reads_a_metadata_address(cp: CP, host: str) -> None:
    # the real guarded transport: no list is faked
    found = await drafted(cp, base_url=f"http://{host}/v1")
    assert found["listed"] is None
    assert any("link-local or cloud metadata" in n for n in found["notes"]), found["notes"]


async def test_a_picked_row_prices_the_draft(cp: CP) -> None:
    listed(cp)
    found = await drafted(cp, model="model-a", litellm="cloud/eu/model-a")
    assert 'price_key = "cloud/eu/model-a"' in found["toml"]
    assert found["candidates"] == []
    unpicked = await drafted(cp, model="model-a")
    assert unpicked["candidates"][:2] == ["model-a", "cloud/eu/model-a"]


@pytest.mark.parametrize(
    ("params", "status", "code", "detail"),
    [
        ({"model": "a b"}, 422, "model", "is not a model id"),
        ({"name": "a b"}, 422, "name", "profile name 'a b'"),
        ({"base_url": "ftp://x"}, 422, "connection", "is not an http or https URL"),
        ({"base_url": "https://u:p@x"}, 422, "connection", "user name or password"),
        ({"key_env": "1X"}, 422, "connection", "is not an env var name"),
        ({"from": "nope", "base_url": None, "key_env": None}, 422, "connection", "nope"),
        ({"from": "scripted"}, 422, "connection", "not both"),
        ({"base_url": None, "key_env": None}, 422, "connection", "give a base URL"),
        ({"litellm": "nope/row"}, 422, "row", "nope/row"),
    ],
)
async def test_a_lookup_that_drafts_no_profile_says_why(
    cp: CP, params: dict[str, str | None], status: int, code: str, detail: str
) -> None:
    listed(cp)
    query = {k: v for k, v in {**ASK, **params}.items() if v is not None}
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params=query)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == code
    assert detail in r.json()["detail"]


async def test_a_picked_row_with_no_price_map_at_hand_is_a_bad_gateway(
    cp: CP, tmp_path: Path
) -> None:
    listed(cp)
    prices(cp, tmp_path, 503)
    async with cp.client(cp.operator) as c:
        r = await c.get(LOOKUP, params={**ASK, "litellm": "model-a"})
        assert (r.status_code, r.json()["code"]) == (502, "unreachable"), r.text
        # with no row picked the map's absence is a note
        r = await c.get(LOOKUP, params=ASK)
    assert r.status_code == 200, r.text
    assert r.json()["candidates"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"base_url": BASE},
        {**ASK, "model": ""},
        {**ASK, "model": "a" * 201},
        {**ASK, "backend": "other"},
        {**ASK, "name": ""},
    ],
)
async def test_a_malformed_lookup_reads_no_list(cp: CP, params: dict[str, str]) -> None:
    seen = listed(cp)
    async with cp.client(cp.operator) as c:
        assert (await c.get(LOOKUP, params=params)).status_code == 422
    assert seen == []


# Add


async def toml(cp: CP, **params: str) -> str:
    listed(cp)
    return (await drafted(cp, **params))["toml"]


async def audit(cp: CP) -> list[tuple[Any, ...]]:
    async with cp.client(cp.operator) as c:
        rows = (await c.get("/api/audit", params={"action": "models.add"})).json()["items"]
    return [(r["target"], r["outcome"], r["status"], r["detail"]) for r in reversed(rows)]


async def profiles(cp: CP) -> dict[str, Any]:
    async with cp.client(cp.viewer) as c:
        return (await c.get("/api/models")).json()


async def test_an_unchecked_add_saves_the_profile_with_its_roles(cp: CP) -> None:
    text = await toml(cp)
    before = cp.project.read_text()
    async with cp.client(cp.operator) as c:
        r = await c.post(ADD, json={"toml": text, "roles": ["judge", "agent"], "check": False})
        assert r.status_code == 200, r.text
        assert r.json() == {"name": NAME, "path": str(cp.project), "run_id": None, "notes": []}
        again = await c.post(ADD, json={"toml": text, "check": False})
        assert (again.status_code, again.json()["code"]) == (409, "exists")
    # it goes above the [roles] that ends the file
    assert cp.project.read_text() == before.replace(
        '[roles]\nagent = "scripted"\n', f'{text}\n[roles]\nagent = "{NAME}"\njudge = "{NAME}"\n'
    )
    roles = (await profiles(cp))["roles"]
    assert (roles["agent"], roles["judge"]) == (NAME, NAME)
    assert await cp.state.db.jobs() == []
    assert await audit(cp) == [
        (NAME, "ok", 200, "roles agent, judge; unchecked"),
        (NAME, "refused", 409, "exists"),
    ]


async def test_a_profile_given_as_root_keys_still_lands_in_its_own_table(cp: CP) -> None:
    # as written, root keys would join the table above them
    text = "".join(
        f"profiles.loose.{k} = {v}\n"
        for k, v in (
            ("backend", '"chat"'),
            ("base_url", '"http://m.invalid/v1"'),
            ("model", '"m"'),
            ("weights", '"lw"'),
            ("reasoning", '"none"'),
        )
    )
    async with cp.client(cp.operator) as c:
        r = await c.post(ADD, json={"toml": text, "check": False})
    assert r.status_code == 200, r.text
    assert cp.project.read_text().endswith(
        '\n[profiles.loose]\nbackend = "chat"\nbase_url = "http://m.invalid/v1"\nmodel = "m"\n'
        'weights = "lw"\nreasoning = "none"\n\n[roles]\nagent = "scripted"\n'
    )
    assert cp.state.config.settings().models.get("loose").weights == "lw"


async def test_adding_is_for_operators_and_a_refused_add_leaves_the_file(cp: CP) -> None:
    text = await toml(cp)
    unstructured = text + "structured_output = false\n"
    before = cp.project.read_bytes()
    async with cp.client(cp.viewer) as c:
        assert (await c.post(ADD, json={"toml": text})).status_code == 403
    async with cp.client() as c:
        assert (await c.post(ADD, json={"toml": text})).status_code == 401
    async with cp.client(cp.operator) as c:
        for body, status, code, detail in (
            ({"toml": "nope ="}, 422, "profile", "not TOML"),
            ({"toml": text + '\n[profiles.two]\nmodel = "m"\n'}, 422, "profile", "one"),
            ({"toml": text + '\n[roles]\nagent = "agent-model"\n'}, 422, "profile", "one"),
            ({"toml": text.replace("agent-model]", '"a b"]', 1)}, 422, "name", "a b"),
            ({"toml": text + 'extends = "scripted"\n'}, 422, "profile", "extends"),
            ({"toml": text + "nope = 1\n"}, 422, "profile", "nope"),
            ({"toml": text.replace("agent-model]", "scripted]", 1)}, 409, "exists", "scripted"),
            ({"toml": unstructured, "roles": ["judge"]}, 422, "role", "cannot take the judge"),
            ({"toml": text}, 409, "no_runner", "no runner has said hello"),
            ({"toml": text, "roles": ["nope"]}, 422, "invalid", None),
            ({"toml": text, "roles": ["agent"] * 4}, 422, "invalid", None),
            ({"toml": ""}, 422, "invalid", None),
            ({}, 422, "invalid", None),
        ):
            r = await c.post(ADD, json=body)
            assert (r.status_code, r.json().get("code")) == (status, code), r.text
            assert detail is None or detail in r.json()["detail"], r.text
    assert cp.project.read_bytes() == before
    assert await cp.state.db.jobs() == []


async def test_a_project_file_that_cannot_take_the_table_stays_as_it_was(cp: CP) -> None:
    text = await toml(cp)
    cp.project.write_text(cp.project.read_text() + "\n[nope]\n")
    before = cp.project.read_bytes()
    async with cp.client(cp.operator) as c:
        r = await c.post(ADD, json={"toml": text, "check": False})
    assert (r.status_code, r.json()["code"]) == (422, "config")
    assert cp.project.read_bytes() == before
    assert sorted(p.name for p in cp.project.parent.iterdir()) == ["aims.toml", "scenarios"]


# Add and check


async def runner(cp: CP, name: str = "fake") -> FakeRunner:
    fake = FakeRunner(cp, name, envs=["EXAMPLE_KEY"])
    await fake.connect()
    return fake


async def add(cp: CP, text: str, **body: Any) -> str:
    async with cp.client(cp.operator) as c:
        r = await c.post(ADD, json={"toml": text, **body})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == []
    run_id = r.json()["run_id"]
    assert isinstance(run_id, str)
    return run_id


async def play_check(
    fake: FakeRunner,
    local: LocalEventStore,
    *,
    verdicts: dict[str, Verdict] | None = None,
    reasoning: ReasoningKind | None = "full",
    status: RunStatus = "completed",
) -> Job:
    """Take the offered check job as a runner would, record its run and ship it."""
    offer = await fake.next(OfferMessage)
    job = await fake.client.claim(offer.run_id)
    assert isinstance(job, Job)
    (name,) = job.check
    started = RunStarted(
        scenario="models.check",
        oversight="observe",
        kind="models.check",
        config={"models": {n: p.model_dump(mode="json") for n, p in job.models.items()}},
    )
    await local.create_run(started, run_id=job.run_id)
    tests = [
        ModelChecked(
            profile=name,
            name=t,
            verdict=(verdicts or {}).get(t, "ok"),
            detail="seen",
            reasoning=reasoning if t == "plain" else None,
        )
        for t in TESTS
    ]
    ended = Draft(RunFinished(status=status))
    await local.append(job.run_id, [*(Draft(t) for t in tests), ended])
    await ship(local, fake.client, job.run_id)
    return job


async def outcome(cp: CP, name: str = NAME) -> dict[str, Any] | None:
    """What `GET /models` says of the newest add of profile `name`."""
    found = {p["name"]: p for p in (await profiles(cp))["profiles"]}
    return found[name]["add"]


async def test_a_checked_add_sets_the_roles_and_reasoning_once_the_check_passes(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        text = await toml(cp)
        before = cp.project.read_text()
        run_id = await add(cp, text, roles=["agent"])
        # the profile is saved at once, without its role
        assert cp.project.read_text() == before.replace("[roles]\n", f"{text}\n[roles]\n")
        assert (await profiles(cp))["roles"]["agent"] == "scripted"
        job = await play_check(fake, local)
        assert job.run_id == run_id
        assert job.add is not None
        assert (job.add.roles, job.add.measure, job.add.outcome) == (["agent"], True, None)
        assert job.add.table == fingerprint(tomllib.loads(text)["profiles"][NAME])
        assert job.add.was == {"agent": "scripted"}
    finally:
        await fake.close()
    full = text.replace('reasoning = "none"', 'reasoning = "full"')
    assert cp.project.read_text() == before.replace(
        '[roles]\nagent = "scripted"\n', f'{full}\n[roles]\nagent = "{NAME}"\n'
    )
    assert cp.state.config.settings().models.get(NAME).reasoning == "full"
    assert await outcome(cp) == {
        "run_id": run_id,
        "roles": ["agent"],
        "measure": True,
        "waiting": None,
        "outcome": "done",
        "note": "the check passed; set reasoning full and the role agent",
    }
    lines = (await profiles(cp))["profiles"]
    plain = next(p for p in lines if p["name"] == NAME)["check"]["checks"][0]
    assert (plain["name"], plain["reasoning"]) == ("plain", "full")
    assert await audit(cp) == [
        (NAME, "ok", 200, f"roles agent; checked; check run {run_id}"),
        (
            NAME,
            "ok",
            None,
            f"after check run {run_id}: the check passed; set reasoning full and the role agent",
        ),
    ]


async def test_the_write_after_a_check_runs_once(cp: CP, local: LocalEventStore) -> None:
    fake = await runner(cp)
    try:
        run_id = await add(cp, await toml(cp), roles=["agent"])
        await play_check(fake, local)
        after = cp.project.read_bytes()
        # a runner sends the whole log again, as an overlap after a lost answer would
        lines = (local.root / run_id / LOG).read_bytes().splitlines(keepends=True)
        head = await fake.client.post_events(run_id, lines)
        assert head.seq == len(lines) - 1
    finally:
        await fake.close()
    assert cp.project.read_bytes() == after
    assert [row[3].split(":")[0] for row in await audit(cp)] == [
        f"roles agent; checked; check run {run_id}",
        f"after check run {run_id}",
    ]


async def test_a_check_that_fails_leaves_the_profile_without_roles(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        run_id = await add(cp, await toml(cp), roles=["agent"])
        added = cp.project.read_bytes()
        await play_check(fake, local, verdicts={"tools": "fail"}, status="failed")
    finally:
        await fake.close()
    assert cp.project.read_bytes() == added
    assert await outcome(cp) == {
        "run_id": run_id,
        "roles": ["agent"],
        "measure": True,
        "waiting": None,
        "outcome": "failed",
        "note": "the check did not pass: it failed tools. The profile has no role",
    }
    assert (await audit(cp))[-1][:2] == (NAME, "refused")
    assert run_id in (await audit(cp))[-1][3]


async def test_a_profile_that_changed_during_the_check_is_left_as_it_is(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp), roles=["agent"])
        # the profile is right above [roles], so this key lands in its table
        text = cp.project.read_text()
        cp.project.write_text(text.replace("\n[roles]\n", "context_tokens = 1000\n\n[roles]\n"))
        edited = cp.project.read_bytes()
        await play_check(fake, local)
    finally:
        await fake.close()
    assert cp.project.read_bytes() == edited
    found = await outcome(cp)
    assert found is not None
    assert (found["outcome"], found["note"]) == (
        "changed",
        f"profile {NAME} changed or went while the check ran, so its roles and reasoning are"
        " not set",
    )


async def test_edits_elsewhere_and_a_role_set_meanwhile_are_kept(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp), roles=["agent", "judge"])
        text = cp.project.read_text()
        # someone gives the judge role to another profile and adds a comment
        cp.project.write_text(
            "# an edit\n"
            + text.replace('agent = "scripted"', 'agent = "scripted"\njudge = "keyed"')
        )
        await play_check(fake, local)
    finally:
        await fake.close()
    roles = cp.state.config.settings().models.roles
    assert (roles.agent, roles.judge) == (NAME, "keyed")
    assert cp.project.read_text().startswith("# an edit\n")
    found = await outcome(cp)
    assert found is not None
    assert (found["outcome"], found["note"]) == (
        "done",
        "the check passed; set reasoning full and the role agent; left the role judge, which"
        " changed while the check ran",
    )


@pytest.mark.parametrize("measure", [True, False, None], ids=["measure", "keep", "no add"])
async def test_a_runner_checks_with_measure_only_for_an_add_that_measures(
    cp: CP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measure: bool | None
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp), roles=["agent"], measure=bool(measure))
        offer = await fake.next(OfferMessage)
        job = await fake.client.claim(offer.run_id)
    finally:
        await fake.close()
    assert isinstance(job, Job)
    if measure is None:
        job = job.model_copy(update={"add": None})
    seen: list[object] = []

    async def record(*args: object, **kw: object) -> None:
        seen.append(kw.get("measure"))

    monkeypatch.setattr(service, "record_check", record)
    node = Node(NodeOptions(url="http://cp", token="t", name="n", data=tmp_path))
    node._open = cast(Any, SimpleNamespace(store=None, link=SimpleNamespace(ship=lambda _: None)))
    await node._execute(job)
    assert seen == [measure is True]


async def test_measure_off_keeps_the_reasoning_the_profile_sets(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp), roles=["agent"], measure=False)
        await play_check(fake, local, reasoning="full")
    finally:
        await fake.close()
    assert cp.state.config.settings().models.get(NAME).reasoning == "none"
    assert cp.state.config.settings().models.roles.agent == NAME
    found = await outcome(cp)
    assert found is not None
    assert (found["outcome"], found["note"]) == ("done", "the check passed; set the role agent")


async def test_a_passing_check_with_nothing_to_set_writes_nothing(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp))
        added = cp.project.read_bytes()
        await play_check(fake, local, reasoning="none")
    finally:
        await fake.close()
    assert cp.project.read_bytes() == added
    found = await outcome(cp)
    assert found is not None
    assert (found["outcome"], found["note"]) == ("done", "the check passed; nothing to set")


async def test_a_write_that_breaks_is_kept_and_the_runs_end_still_lands(
    cp: CP, local: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(text: str, roles: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(edit_api, "with_roles", broken)
    fake = await runner(cp)
    try:
        run_id = await add(cp, await toml(cp), roles=["agent"])
        added = cp.project.read_bytes()
        await play_check(fake, local)
    finally:
        await fake.close()
    job = await cp.state.db.job(run_id)
    assert job is not None
    assert (job.state, job.reason) == ("done", "completed")
    assert cp.project.read_bytes() == added
    found = await outcome(cp)
    assert found is not None
    assert (found["outcome"], found["note"]) == ("refused", "the write failed: RuntimeError")


async def test_a_check_job_that_never_ran_reads_as_failed(cp: CP) -> None:
    fake = await runner(cp)
    try:
        run_id = await add(cp, await toml(cp), roles=["agent"])
        assert await cp.state.db.set_job(run_id, "done", "no runner could start it: gone")
    finally:
        await fake.close()
    found = await outcome(cp)
    assert found is not None
    assert (found["run_id"], found["waiting"], found["outcome"], found["note"]) == (
        run_id,
        None,
        "failed",
        "the check did not pass: no runner could start it: gone",
    )
    assert cp.state.config.settings().models.roles.agent == "scripted"


async def test_a_check_waits_for_a_runner_with_the_new_key_then_sets_the_roles(
    cp: CP, local: LocalEventStore
) -> None:
    # the runner is up before the profile exists, so it has not said it holds the key
    fake = FakeRunner(cp)
    await fake.connect()
    try:
        run_id = await add(cp, await toml(cp), roles=["agent"])
        found = await outcome(cp)
        assert found is not None
        assert (found["run_id"], found["outcome"]) == (run_id, None)
        assert found["waiting"] == "no connected runner has EXAMPLE_KEY set"
        assert "EXAMPLE_KEY" in cp.state.runners["fake"].told
        fake.envs = ["EXAMPLE_KEY"]
        await fake.client.status(
            RunnerStatus(tracer=TracerHealth(alive=True), free=2, envs=fake.envs)
        )
        job = await play_check(fake, local)
    finally:
        await fake.close()
    assert job.run_id == run_id
    assert cp.state.config.settings().models.roles.agent == NAME


@pytest.mark.parametrize(
    ("roles", "unset"),
    [
        (["agent"], "The role agent is not set."),
        (["agent", "judge"], "The roles agent, judge are not set."),
    ],
)
async def test_a_check_that_was_not_queued_answers_with_a_note(
    cp: CP, monkeypatch: pytest.MonkeyPatch, roles: list[str], unset: str
) -> None:
    async def refused(*args: object, **kw: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(edit_api, "_queue", refused)
    fake = await runner(cp)
    try:
        text = await toml(cp)
        async with cp.client(cp.operator) as c:
            r = await c.post(ADD, json={"toml": text, "roles": roles})
    finally:
        await fake.close()
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["run_id"]) == (NAME, None)
    assert r.json()["notes"] == [
        f"Its check was not queued: RuntimeError. {unset} Check it under Settings > Models."
    ]
    assert NAME in cp.state.config.settings().models.profiles


async def test_an_add_names_a_runner_that_must_exist(cp: CP) -> None:
    fake = await runner(cp)
    try:
        text = await toml(cp)
        before = cp.project.read_bytes()
        async with cp.client(cp.operator) as c:
            r = await c.post(ADD, json={"toml": text, "runner": "nope"})
            assert r.status_code == 422, r.text
            assert cp.project.read_bytes() == before
            r = await c.post(ADD, json={"toml": text, "runner": "fake"})
            assert r.status_code == 200, r.text
        job = await cp.state.db.job(r.json()["run_id"])
        assert job is not None
        assert job.target == "fake"
    finally:
        await fake.close()


async def test_the_roles_still_follow_the_check_after_a_restart(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project")
    data = tmp_path / "server"

    @contextlib.asynccontextmanager
    async def serving() -> AsyncGenerator[CP]:
        state = await open_state(data, project=project)
        server, task, port = await start(create_app(state))
        try:
            yield CP(f"http://127.0.0.1:{port}", state, project, op, "", runner_token, server, task)
        finally:
            await stop(server, task)
            await close_state(state)

    state = await open_state(data, project=project)
    op = await state.db.create_token("op", "operator")
    runner_token = await state.db.create_token("r1", "runner")
    await close_state(state)
    async with serving() as first:
        listed(first)
        prices(first, tmp_path)
        fake = await runner(first)
        try:
            run_id = await add(first, (await drafted(first))["toml"], roles=["agent"])
        finally:
            await fake.close()
    async with serving() as second, await LocalEventStore.open(tmp_path / "local") as local:
        fake = await runner(second)
        try:
            job = await play_check(fake, local)
        finally:
            await fake.close()
        assert job.run_id == run_id
        assert second.state.config.settings().models.roles.agent == NAME
        assert second.state.config.settings().models.get(NAME).reasoning == "full"


async def test_runners_learn_the_key_of_an_added_profile(cp: CP) -> None:
    fake = FakeRunner(cp)
    await fake.connect()
    try:
        assert "EXAMPLE_KEY" not in cp.state.runners["fake"].told
        text = await toml(cp)
        async with cp.client(cp.operator) as c:
            r = await c.post(ADD, json={"toml": text, "check": False})
            assert r.status_code == 200, r.text
        assert "EXAMPLE_KEY" in cp.state.runners["fake"].told
    finally:
        await fake.close()


async def test_a_profile_added_after_the_roles_can_be_patched(cp: CP) -> None:
    # the add leaves [profiles.x] after [roles], so the parts of [profiles] are apart
    text = await toml(cp)
    async with cp.client(cp.operator) as c:
        assert (await c.post(ADD, json={"toml": text, "check": False})).status_code == 200
        tag = (await c.get("/api/config/project")).json()["etag"]
        body = {"set": [{"key": ["profiles", NAME, "reasoning"], "value": "full"}]}
        r = await c.patch("/api/config/project", json=body, headers={"If-Match": f'"{tag}"'})
        assert r.status_code == 200, r.text
        assert cp.state.config.settings().models.get(NAME).reasoning == "full"
        body = {"unset": [["profiles", NAME, "max_output_tokens"]]}
        tag = r.json()["etag"]
        r = await c.patch("/api/config/project", json=body, headers={"If-Match": f'"{tag}"'})
    assert r.status_code == 200, r.text
    assert "max_output_tokens" not in cp.project.read_text().split(f"[profiles.{NAME}]")[1]


async def test_an_added_profile_can_be_removed_with_a_patch(cp: CP) -> None:
    # Settings > Models removes a profile whose add failed this way
    before = cp.project.read_text()
    text = await toml(cp)
    async with cp.client(cp.operator) as c:
        assert (await c.post(ADD, json={"toml": text, "check": False})).status_code == 200
        tag = (await c.get("/api/config/project")).json()["etag"]
        body = {"unset": [["profiles", NAME]]}
        r = await c.patch("/api/config/project", json=body, headers={"If-Match": f'"{tag}"'})
        assert r.status_code == 200, r.text
        names = [p["name"] for p in (await c.get("/api/models")).json()["profiles"]]
    assert NAME not in names
    assert NAME not in cp.state.config.settings().models.profiles
    assert tomllib.loads(cp.project.read_text()) == tomllib.loads(before)


async def test_two_checked_adds_in_a_row_both_get_their_roles(
    cp: CP, local: LocalEventStore
) -> None:
    fake = await runner(cp)
    try:
        await add(cp, await toml(cp), roles=["agent"])
        await add(cp, await toml(cp, model="example/judge-model"), roles=["sweep", "judge"])
        await play_check(fake, local)
        await play_check(fake, local)
    finally:
        await fake.close()
    roles = cp.state.config.settings().models.roles
    assert (roles.agent, roles.sweep, roles.judge) == (NAME, "judge-model", "judge-model")
