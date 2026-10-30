import asyncio
import json
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cp import CP, control_plane
from test_server import FakeRunner, ship, until

from aims.events import Draft, LocalEventStore, RunFinished, RunStarted, ToolCall
from aims.events.payloads import FlagReview, MonitorFlag, MonitorVerdict
from aims.monitor.texts import defaults
from aims.node.shipper import Shipper
from aims.prompts import Prompts
from aims.run.runner import resolve_texts
from aims.scenario import load
from aims.wire import CommandMessage, MonitorJob, OfferMessage, RunnerHello

pytestmark = pytest.mark.anyio

IDENTITY = "a" * 64


class MonitorRunner(FakeRunner):
    """A fake runner that says it runs monitor jobs."""

    def hello_body(self, jobs: list[str] | None = None) -> RunnerHello:
        body = super().hello_body(jobs)
        return body.model_copy(update={"kinds": ["scenario", "models.check", "monitor"]})


@pytest.fixture
async def cp(tmp_path: Path) -> AsyncIterator[CP]:
    async with control_plane(tmp_path) as cp:
        yield cp


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "local") as store:
        yield store


@pytest.fixture
async def fake(cp: CP) -> AsyncIterator[FakeRunner]:
    runner = FakeRunner(cp)
    await runner.connect()
    yield runner
    await runner.close()


def call(n: int) -> Draft:
    return Draft(ToolCall(call_id=f"c{n}", name="shell", arguments="{}"), "a")


def flag(**fields: Any) -> Draft:
    base: dict[str, Any] = {
        "layer": "signal",
        "source": "repeat",
        "category": "loop_or_confusion",
        "importance": 2,
        "evidence": [1],
        "justification": "same call",
    }
    return Draft(MonitorFlag(**{**base, **fields}), "a")


def started(cp: CP, *, monitor: str | None = IDENTITY) -> tuple[RunStarted, list[bytes]]:
    """A `run.started` as `aims run` writes it, so the run can be forked or monitored."""
    bundle = load(cp.project.parent / "scenarios" / "control" / "scenario.yaml")
    profile = cp.state.config.settings().models.get("scripted")
    texts = resolve_texts(Prompts(), bundle.scenario.agents[0]).snapshot()
    entry = {
        "profile": profile.snapshot(),
        "schedule": [[None, profile.snapshot()]],
        "texts": texts,
    }
    return (
        RunStarted(
            scenario="control",
            oversight="observe",
            agents=["a"],
            config={"scenario": bundle.snapshot(), "agents": {"a": entry}},
            monitor=monitor,
        ),
        bundle.blobs(),
    )


async def pushed(
    cp: CP,
    local: LocalEventStore,
    runner: FakeRunner,
    drafts: list[Draft],
    *,
    monitor: str | None = IDENTITY,
) -> str:
    """A sealed run, shipped by `runner`."""
    first, blobs = started(cp, monitor=monitor)
    run_id = await local.create_run(first, blobs=blobs)
    await local.append(run_id, [*drafts, Draft(RunFinished(status="completed"))])
    await ship(local, runner.client, run_id)
    return run_id


async def live(
    cp: CP,
    local: LocalEventStore,
    runner: FakeRunner,
    drafts: list[Draft],
    *,
    monitor: str | None = IDENTITY,
) -> tuple[str, asyncio.Task[None]]:
    """A running run, shipped by `runner` as it grows until it is sealed."""
    first, blobs = started(cp, monitor=monitor)
    run_id = await local.create_run(first, blobs=blobs)
    await local.append(run_id, drafts)
    task = asyncio.create_task(Shipper(local, runner.client, run_id).run())

    async def head() -> int:
        h = await cp.state.store.head(run_id)
        return -1 if h is None else h.seq

    await until(head, len(drafts))
    return run_id, task


async def seal(local: LocalEventStore, run_id: str, task: asyncio.Task[None]) -> None:
    await local.append(run_id, [Draft(RunFinished(status="completed"))])
    async with asyncio.timeout(10):
        await task


def flagged() -> list[Draft]:
    return [
        call(1),
        call(2),
        flag(key="k"),
        flag(source="actions", layer="sweep", category="scorer_tampering", importance=4),
        flag(key="k", count=10, first_flag=3, evidence=[2]),
        Draft(
            MonitorVerdict(
                flags=[FlagReview(flag=4, status="confirmed", reason="seen")], max_severity=4
            )
        ),
    ]


async def test_flags_are_listed_filtered_and_paged(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id = await pushed(cp, local, fake, flagged())
    url = f"/api/runs/{run_id}/flags"
    async with cp.client() as anon, cp.client(cp.viewer) as c:
        assert (await anon.get(url)).status_code == 401
        page = (await c.get(url)).json()
        assert page["origin"] is None
        assert page["next"] is None
        assert [(f["seq"], f["count"], f["reflags"], f["status"]) for f in page["items"]] == [
            (3, 10, 1, "open"),
            (4, 1, 0, "confirmed"),
        ]
        assert page["items"][0]["evidence"] == [1, 2]
        first = (await c.get(url, params={"limit": 1})).json()
        assert ([f["seq"] for f in first["items"]], first["next"]) == ([3], "3")
        rest = (await c.get(url, params={"limit": 1, "after": first["next"]})).json()
        assert [f["seq"] for f in rest["items"]] == [4]

        async def seqs(**params: Any) -> list[int]:
            r = await c.get(url, params=params)
            assert r.status_code == 200, r.text
            return [f["seq"] for f in r.json()["items"]]

        assert await seqs(layer="sweep") == [4]
        assert await seqs(category="loop_or_confusion") == [3]
        assert await seqs(min_importance=3) == [4]
        assert await seqs(status=["open", "rejected"]) == [3]
        assert await seqs(agent="a") == [3, 4]
        assert await seqs(agent="b") == []
        assert await seqs(tainted="true") == []
        r = await c.get(url, params={"status": "nonsense"})
        assert r.status_code == 422
        r = await c.get(url, params={"min_importance": 6})
        assert r.status_code == 422
        unknown = "00000000-0000-7000-8000-000000000009"
        assert (await c.get(f"/api/runs/{unknown}/flags")).status_code == 404


async def test_run_list_filters_and_carries_monitor_fields(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    hot = await pushed(cp, local, fake, flagged())
    quiet = await pushed(cp, local, fake, [call(1)])
    async with cp.client(cp.viewer) as c:

        async def ids(**params: Any) -> list[str]:
            r = await c.get("/api/runs", params=params)
            assert r.status_code == 200, r.text
            return [x["run_id"] for x in r.json()["items"]]

        assert await ids() == [quiet, hot]
        assert await ids(severity=4) == [hot]
        assert await ids(severity=5) == []
        assert await ids(category="scorer_tampering") == [hot]
        assert await ids(kind="scenario") == [quiet, hot]
        assert await ids(kind="monitor") == []
        assert (await c.get("/api/runs", params={"kind": "other"})).status_code == 422
        assert (await c.get("/api/runs", params={"category": "other"})).status_code == 422
        detail = (await c.get(f"/api/runs/{hot}")).json()
        run = detail["run"]
        assert (run["kind"], run["origin"], run["flags"]) == ("scenario", None, 2)
        assert (run["max_importance"], run["verdict_severity"], run["max_severity"]) == (4, 4, 4)
        assert run["categories"] == {"loop_or_confusion": 1, "scorer_tampering": 1}
        assert detail["verdict"]["type"] == "monitor.verdict"
        assert detail["monitor_runs"] == []
        calm = (await c.get(f"/api/runs/{quiet}")).json()
        assert (calm["run"]["flags"], calm["run"]["max_severity"], calm["verdict"]) == (0, 0, None)


async def test_sweep_and_deny_reach_the_runner(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id, task = await live(cp, local, fake, [call(1)])
    bare, bare_task = await live(cp, local, fake, [call(1)], monitor=None)
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        r = await viewer.post(f"/api/runs/{run_id}/sweep", json={})
        assert r.status_code == 403
        r = await viewer.post(f"/api/runs/{run_id}/resume", json={"then": "deny"})
        assert r.status_code == 403
        r = await c.post(f"/api/runs/{run_id}/sweep", json={"agent": "a", "reason": "look"})
        assert r.status_code == 202, r.text
        assert r.json()["delivered"]
        sweep = await fake.next(CommandMessage)
        assert (sweep.command.action, sweep.command.agent, sweep.command.reason) == (
            "sweep",
            "a",
            "look",
        )
        assert sweep.command.token == "op"
        r = await c.post(f"/api/runs/{run_id}/resume", json={"agent": "a", "then": "deny"})
        assert r.status_code == 202, r.text
        deny = await fake.next(CommandMessage)
        assert (deny.command.action, deny.command.then, deny.command.text) == (
            "resume",
            "deny",
            None,
        )
        r = await c.post(f"/api/runs/{run_id}/resume", json={"then": "deny", "text": "why"})
        assert r.status_code == 422
        r = await c.post(f"/api/runs/{run_id}/resume", json={"then": "explode"})
        assert r.status_code == 422
        r = await c.post(f"/api/runs/{bare}/sweep", json={})
        assert (r.status_code, r.json()["code"]) == (409, "no_monitor")
        runners = {x["name"]: x for x in (await c.get("/api/runners")).json()}
        assert runners["fake"]["kinds"] == ["scenario", "models.check"]
        listed = (await c.get(f"/api/runs/{run_id}/commands")).json()
        assert [x["action"] for x in listed] == ["sweep", "resume"]
    await seal(local, run_id, task)
    await seal(local, bare, bare_task)


async def claim(cp: CP, runner: str, run_id: str) -> httpx2.Response:
    async with cp.client(cp.runner) as c:
        return await c.post(f"/runner/v1/jobs/{run_id}/claim", headers={"X-Aims-Runner": runner})


async def test_monitor_runs_queue_for_runners_that_take_them(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    origin = await pushed(cp, local, fake, flagged())
    running, task = await live(cp, local, fake, [call(1)])
    url = f"/api/runs/{origin}/monitor"
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        assert (await viewer.post(url, json={})).status_code == 403
        r = await c.post(url, json={"signals_only": True})
        assert (r.status_code, r.json()["code"]) == (422, "kind")
        r = await c.post(f"/api/runs/{running}/monitor", json={})
        assert (r.status_code, r.json()["code"]) == (409, "not_sealed")
        r = await c.post(url, json={"signals_only": True, "judge_only": True})
        assert r.status_code == 422

        mon = MonitorRunner(cp, "mon", envs=["MODEL_API_KEY"])
        await mon.connect()
        try:
            r = await c.post(url, json={"profile": "nope"})
            assert (r.status_code, r.json()["code"]) == (422, "monitor")
            r = await c.post(url, json={"config": {"preset": "bogus"}})
            assert (r.status_code, r.json()["code"]) == (422, "monitor")
            r = await c.post(url, json={"preset": "signals", "judge_only": True})
            assert (r.status_code, r.json()["code"]) == (422, "monitor")
            r = await c.post(url, json={"signals_only": True, "budget_usd": -1})
            assert r.status_code == 422

            r = await c.post(url, json={"signals_only": True})
            assert r.status_code == 202, r.text
            (signals,) = r.json()["run_ids"]
            offer = await mon.next(OfferMessage)
            assert offer.run_id == signals
            assert offer.needs == {"envs": [], "kernel": False, "kind": "monitor"}
            with pytest.raises(TimeoutError):
                await fake.next(OfferMessage, within=0.5)
            job = await cp.state.db.job(signals)
            assert job is not None
            spec = MonitorJob.model_validate(job.payload)
            assert (spec.origin, spec.mode, spec.scenario) == (origin, "signals_only", "control")
            head = await cp.state.store.head(origin)
            assert head is not None
            assert spec.origin_head == head.hash
            resolved = spec.resolved()
            assert (resolved.config.preset, resolved.sweeps, resolved.judge) == (
                "signals",
                {},
                None,
            )

            log = f"/runner/v1/runs/{origin}/log"
            async with cp.client(cp.runner) as rc:
                r = await rc.get(log, headers={"X-Aims-Runner": "mon"})
                assert r.status_code == 403
                r = await claim(cp, "mon", signals)
                assert r.status_code == 200, r.text
                assert r.json()["job"]["kind"] == "monitor"
                r = await rc.get(log, headers={"X-Aims-Runner": "mon"})
                assert r.status_code == 200
                assert r.content == (cp.state.store.root / origin / "events.jsonl").read_bytes()

            r = await c.post(url, json={"profile": "keyed"})
            assert (r.status_code, r.json()["code"]) == (422, "monitor")
            assert "allow_same_model" in r.json()["detail"]
            cp.project.write_text(
                cp.project.read_text()
                + '\n[profiles.distant]\nextends = "keyed"\nmodel = "n"\nweights = "v"\n'
            )
            r = await c.post(url, json={"samples": 2, "profile": "distant"})
            assert (r.status_code, r.json()["code"]) == (422, "envs")
            r = await c.post(url, json={"samples": 2, "preset": "research"})
            assert r.status_code == 202, r.text
            started_ = r.json()
            assert started_["group"] is not None
            pair = started_["run_ids"]
            job = await cp.state.db.job(pair[0])
            assert job is not None
            spec = MonitorJob.model_validate(job.payload)
            assert spec.mode == "full"
            assert spec.envs() == ["MODEL_API_KEY"]
            resolved = spec.resolved()
            assert resolved.config.preset == "research"
            assert resolved.judge is not None
            assert resolved.config.judge is not None
            assert resolved.config.judge.rollouts == 2

            r = await c.post(url, json={"judge_only": True})
            assert r.status_code == 202, r.text
            (judge_only,) = r.json()["run_ids"]
            job = await cp.state.db.job(judge_only)
            assert job is not None
            spec = MonitorJob.model_validate(job.payload)
            assert (spec.mode, spec.resolved().config.sweeps) == ("judge_only", [])

            listed = (await c.get("/api/runs", params={"kind": "monitor"})).json()["items"]
            assert {x["run_id"] for x in listed} == {signals, judge_only, *pair}
            assert {(x["kind"], x["origin"], x["scenario"]) for x in listed} == {
                ("monitor", origin, "control")
            }
            assert (await c.get("/api/runs", params={"origin": origin})).json()["items"]
            detail = (await c.get(f"/api/runs/{origin}")).json()
            assert set(detail["monitor_runs"]) == {signals, judge_only, *pair}
        finally:
            await mon.close()
    await seal(local, running, task)


async def test_a_job_that_does_not_load_ends_instead_of_failing_the_claim(
    cp: CP, fake: FakeRunner
) -> None:
    r = await claim(cp, "fake", "00000000-0000-7000-8000-000000000001")
    assert r.status_code == 409
    async with cp.client(cp.operator) as c:
        r = await c.post("/api/models/check", json={})
        (run_id,) = r.json()["run_ids"]
    offer = await fake.next(OfferMessage)
    assert offer.run_id == run_id
    db = cp.state.db
    job = await db.job(run_id)
    assert job is not None
    await db._write(
        "update jobs set payload = ? where run_id = ?",
        (json.dumps({**job.payload, "kind": "unknown"}), run_id),
    )
    r = await claim(cp, "fake", run_id)
    assert (r.status_code, r.json()["code"]) == (409, "claim")
    job = await db.job(run_id)
    assert job is not None
    assert job.state == "done"
    assert job.reason is not None
    assert job.reason.startswith("the job does not load")


async def test_the_stream_sends_flag_counts_as_they_land(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    run_id, task = await live(cp, local, fake, [])
    seen: list[dict[str, Any]] = []

    async def read() -> None:
        async with cp.client(cp.viewer) as c, c.sse("/api/stream", params={"runs": True}) as src:
            async for e in src:
                if e.event != "run":
                    continue
                run = json.loads(e.data)["run"]
                if run["run_id"] == run_id:
                    seen.append(run)
                    if run["flags"] == 2 and run["max_severity"] == 4:
                        return

    async with cp.client(cp.viewer) as c:
        before = (await c.get(f"/api/runs/{run_id}")).json()["run"]
    assert (before["flags"], before["max_severity"]) == (0, 0)
    reader = asyncio.create_task(read())
    await asyncio.sleep(0.3)
    await local.append(run_id, flagged()[:4])
    async with asyncio.timeout(10):
        await reader
    last = seen[-1]
    assert last["categories"] == {"loop_or_confusion": 1, "scorer_tampering": 1}
    assert (last["max_importance"], last["verdict_severity"]) == (4, None)
    await seal(local, run_id, task)


async def test_prompts_list_monitor_texts_with_their_layer(cp: CP) -> None:
    names = sorted(defaults())
    assert names, "the monitor modules define their texts"
    name = names[0]
    text = cp.project.read_text()
    cp.project.write_text(
        text
        + '\n[prompts]\ndenied = "Not allowed."\n'
        + f'\n[prompts.monitor]\n{name} = "Project {name}."\n'
        + '\n[prompts.monitor.categories]\ndrift = "Wandering off the task."\n'
    )
    async with cp.client(cp.viewer) as c:
        info = (await c.get("/api/prompts")).json()
    assert info["agent"]["denied"] == {"text": "Not allowed.", "source": "project"}
    assert info["agent"]["notify_on_resume"]["source"] == "default"
    assert info["monitor"][name] == {"text": f"Project {name}.", "source": "project"}
    assert {v["source"] for k, v in info["monitor"].items() if k != name} == {"default"}
    assert set(info["monitor"]) == set(names)
    assert info["categories"]["drift"] == {"text": "Wandering off the task.", "source": "project"}
    assert info["categories"]["scorer_tampering"]["source"] == "default"


async def test_config_edits_take_the_monitor_tables(cp: CP) -> None:
    name = sorted(defaults())[0]

    async def patch(c: httpx2.AsyncClient, layer: str, sets: list[Any]) -> httpx2.Response:
        current = (await c.get(f"/api/config/{layer}")).json()
        headers = {"If-Match": current["etag"]} if current["exists"] else {}
        body = {"set": [{"key": k, "value": v} for k, v in sets]}
        return await c.patch(f"/api/config/{layer}", json=body, headers=headers)

    async with cp.client(cp.operator) as c:
        r = await patch(
            c,
            "user",
            [
                (["monitor", "preset"], "research"),
                (["monitor", "sweeps"], [{"name": "fast", "every": 3}]),
                (["monitor", "judge", "rollouts"], 2),
                (["monitor", "judge", "blind"], 2),
                (["prompts", "monitor", name], f"User {name}."),
                (["prompts", "monitor", "categories", "drift"], "Off task."),
            ],
        )
        assert r.status_code == 200, r.text
        saved = tomllib.loads(r.json()["text"])
        assert saved["monitor"]["preset"] == "research"
        assert saved["monitor"]["sweeps"] == [{"name": "fast", "every": 3}]
        prompts = (await c.get("/api/prompts")).json()
        assert prompts["monitor"][name]["source"] == "user"
        assert prompts["categories"]["drift"]["source"] == "user"

        r = await patch(c, "user", [(["prompts", "monitor", "no_such_text"], "x")])
        assert (r.status_code, r.json()["code"]) == (422, "config")
        assert "no_such_text" in r.json()["detail"]
        r = await patch(c, "user", [(["prompts", "monitor", "categories", "nope"], "x")])
        assert (r.status_code, r.json()["code"]) == (422, "config")
        r = await patch(c, "user", [(["monitor", "preset"], "loud")])
        assert (r.status_code, r.json()["code"]) == (422, "config")
        r = await patch(c, "project", [(["monitor", "judge", "rollouts"], 1)])
        assert (r.status_code, r.json()["code"]) == (422, "config")
        assert "blind" in r.json()["detail"]
        assert (await c.get("/api/config")).json()["error"] is None


async def test_a_bad_monitor_text_in_a_file_is_reported(cp: CP) -> None:
    cp.project.write_text(cp.project.read_text() + '\n[prompts.monitor]\nnope = "x"\n')
    async with cp.client(cp.viewer) as c:
        info = (await c.get("/api/config")).json()
        assert "nope" in info["error"]
        r = await c.get("/api/prompts")
        assert (r.status_code, r.json()["code"]) == (422, "config")


async def test_a_monitor_run_lists_by_origin_and_takes_no_sweep(
    cp: CP, local: LocalEventStore, fake: FakeRunner
) -> None:
    origin = await pushed(cp, local, fake, flagged())
    head = await cp.state.store.head(origin)
    assert head is not None
    first = RunStarted(
        scenario="control",
        oversight="observe",
        kind="monitor",
        origin=origin,
        origin_head=head.hash,
        monitor=IDENTITY,
    )
    run_id = await local.create_run(first)
    await local.append(run_id, [flag(evidence=[4], category="drift", importance=3)])
    task = asyncio.create_task(Shipper(local, fake.client, run_id).run())
    async with cp.client(cp.operator) as c:
        await until(lambda: cp.state.store.run(run_id), lambda info: info is not None)
        r = await c.post(f"/api/runs/{run_id}/sweep", json={})
        assert (r.status_code, r.json()["code"]) == (409, "kind")
        r = await c.post(f"/api/runs/{run_id}/monitor", json={})
        assert r.status_code == 409
        await local.append(run_id, [Draft(RunFinished(status="completed"))])
        async with asyncio.timeout(10):
            await task
        r = await c.post(f"/api/runs/{run_id}/monitor", json={})
        assert (r.status_code, r.json()["code"]) == (422, "kind")
        page = (await c.get(f"/api/runs/{run_id}/flags")).json()
        assert (page["origin"], [f["evidence"] for f in page["items"]]) == (origin, [[4]])
        listed = (await c.get("/api/runs", params={"origin": origin})).json()["items"]
        assert [(x["run_id"], x["kind"], x["max_severity"]) for x in listed] == [
            (run_id, "monitor", 3)
        ]
        assert (await c.get(f"/api/runs/{origin}")).json()["monitor_runs"] == [run_id]


async def test_openapi_documents_the_monitor_surface(cp: CP) -> None:
    async with cp.client() as c:
        spec = (await c.get("/api/openapi.json")).json()
    paths = spec["paths"]
    assert paths["/api/runs/{run_id}/flags"]["get"]["operationId"] == "list_flags"
    assert paths["/api/runs/{run_id}/monitor"]["post"]["operationId"] == "monitor_run"
    assert paths["/api/runs/{run_id}/sweep"]["post"]["operationId"] == "sweep_run"
    schemas = spec["components"]["schemas"]
    summary = schemas["RunSummary"]
    assert {"kind", "origin", "flags", "categories", "max_severity"} <= set(summary["required"])
    assert "deny" in schemas["ResumeBody"]["properties"]["then"]["enum"]
    assert set(schemas["Claimed"]["properties"]["job"]["discriminator"]["mapping"]) == {
        "scenario",
        "models.check",
        "monitor",
        "score",
    }
    status = schemas["FlagInfo"]["properties"]["status"]["enum"]
    assert status == ["open", "cleared", "tainted", "confirmed", "rejected", "unclear"]
