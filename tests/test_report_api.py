"""The per-run report through the control plane: JSON or Markdown, what each role sees, the
monitor source, and where evidence links point."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cp import CP, control_plane
from test_monitor_api import IDENTITY, flag, flagged, pushed
from test_score_api import scored
from test_scoring import took
from test_server import FakeRunner, ship

from aims.events import Draft, LocalEventStore, MonitorVerdict, RunFinished, RunStarted
from aims.report import build as build_module
from aims.report import texts
from aims.report.model import RunReport
from aims.server import serve as serve_module
from aims.server.serve import UiOptions, serve
from aims.server.state import close_state, open_state

pytestmark = pytest.mark.anyio


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


@pytest.fixture(autouse=True)
def plants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's one plant was taken at seq 2, whatever the scenario's rules say."""

    async def check(store: Any, run_id: str, added: Any = None, **_: Any) -> list[Any]:
        return [took(2, (2,))]

    monkeypatch.setattr(build_module, "check_run", check)


@pytest.fixture
async def run_id(cp: CP, local: LocalEventStore, fake: FakeRunner) -> str:
    return await pushed(cp, local, fake, flagged())


def url(run_id: str) -> str:
    return f"/api/runs/{run_id}/report"


async def review(local: LocalEventStore, fake: FakeRunner, origin: str) -> str:
    """A finished monitor run over `origin`, shipped."""
    started = RunStarted(
        scenario="control", oversight="observe", kind="monitor", origin=origin, monitor=IDENTITY
    )
    run_id = await local.create_run(started)
    drafts = [
        flag(layer="judge", source="judge", importance=5, evidence=[2]),
        Draft(MonitorVerdict(max_severity=5)),
        Draft(RunFinished(status="completed")),
    ]
    await local.append(run_id, drafts)
    await ship(local, fake.client, run_id)
    return run_id


async def test_the_report_is_json_by_default_and_markdown_on_request(cp: CP, run_id: str) -> None:
    async with cp.client(cp.operator) as c:
        r = await c.get(url(run_id))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/json"
        report = RunReport.model_validate(r.json())
        assert report.header.run_id == run_id
        assert report.header.pins.role == "operator"
        assert report.plants is not None
        assert [p.plant for p in report.plants.rows] == ["p1"]
        assert report.headline.importance == 4

        r = await c.get(url(run_id), params={"format": "md"})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "text/markdown; charset=utf-8"
        disposition = r.headers["content-disposition"]
        assert disposition == f'attachment; filename="aims-report-{run_id}.md"'
        lines = [line for line in r.text.splitlines() if line and not line.startswith("<!--")]
        assert lines[0] == "# " + texts.TITLE.format(scenario="control")
        assert texts.ACCESS["operator"] in r.text

        r = await c.get(url(run_id), params={"format": "html"})
        assert (r.status_code, r.json()["code"]) == (422, "invalid")


async def test_a_viewer_gets_no_plants_and_no_score(
    cp: CP, run_id: str, local: LocalEventStore, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with cp.client(cp.viewer) as c:
        r = await c.get(url(run_id))
        assert r.status_code == 200, r.text
        report = RunReport.model_validate(r.json())
        assert r.json()["plants"] is None
        assert (report.header.pins.role, report.header.pins.score) == ("viewer", None)
        md = (await c.get(url(run_id), params={"format": "md"})).text
        assert texts.ACCESS["viewer"] in md

    origin, score = await scored(cp, local, fake, monkeypatch)
    async with cp.client(cp.viewer) as viewer, cp.client(cp.operator) as c:
        r = await viewer.get(url(score))
        assert (r.status_code, r.json()["code"]) == (403, "truth")
        r = await c.get(url(score))
        assert r.status_code == 404
        assert "score run" in r.json()["detail"]
        report = RunReport.model_validate((await c.get(url(origin))).json())
        assert report.header.pins.score == score
        report = RunReport.model_validate((await viewer.get(url(origin))).json())
        assert (report.header.pins.score, report.plants) == (None, None)


async def test_unknown_runs_and_sources_are_not_found(cp: CP, run_id: str) -> None:
    async with cp.client() as anon, cp.client(cp.viewer) as c:
        r = await anon.get(url(run_id))
        assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
        for missing in (str(uuid.uuid7()), "nope"):
            r = await c.get(url(missing))
            assert r.status_code == 404, missing
            assert r.headers["content-type"] == "application/problem+json"
        r = await c.get(url(run_id), params={"source": "nope"})
        assert r.status_code == 404
        assert r.json()["detail"] == f"nope is not a monitor run over {run_id}"
        r = await c.get(url(run_id), params={"source": str(uuid.uuid7()), "format": "md"})
        assert r.status_code == 404


async def test_the_source_picks_the_monitor(
    cp: CP, run_id: str, local: LocalEventStore, fake: FakeRunner
) -> None:
    held = await review(local, fake, run_id)
    async with cp.client(cp.viewer) as c:
        default = RunReport.model_validate((await c.get(url(run_id))).json())
        assert (default.header.pins.source, default.headline.importance) == (held, 5)
        r = await c.get(url(run_id), params={"source": "live"})
        live = RunReport.model_validate(r.json())
        assert (live.header.pins.source, live.headline.importance) == ("live", 4)
        r = await c.get(url(run_id), params={"source": held})
        assert RunReport.model_validate(r.json()).header.pins.source == held


async def test_links_start_at_the_public_url_else_at_the_request(cp: CP, run_id: str) -> None:
    link = f"/runs/{run_id}?seq=4)"
    async with cp.client(cp.viewer) as c:
        report = RunReport.model_validate((await c.get(url(run_id))).json())
        assert report.header.pins.base_url == cp.url
        md = (await c.get(url(run_id), params={"format": "md"})).text
        assert f"[seq 4]({cp.url}{link}" in md

        cp.state.public_url = "https://aims.example"
        report = RunReport.model_validate((await c.get(url(run_id))).json())
        assert report.header.pins.base_url == "https://aims.example"
        md = (await c.get(url(run_id), params={"format": "md"})).text
        assert f"[seq 4](https://aims.example{link}" in md
        assert cp.url not in md


async def test_a_base_unfit_for_links_gives_plain_seqs(cp: CP, run_id: str) -> None:
    port = cp.url.rsplit(":", 1)[1]
    async with cp.client(cp.viewer) as c:
        # A literal IPv6 host is a base `base_url_problem` refuses: it holds brackets.
        r = await c.get(url(run_id), headers={"Host": f"[::1]:{port}"})
        assert r.status_code == 200, r.text
        assert RunReport.model_validate(r.json()).header.pins.base_url is None
        cp.state.public_url = "https://aims.example/a b"
        r = await c.get(url(run_id), params={"format": "md"})
        assert "](" not in r.text
        assert "| seq 4 | a | sweep |" in r.text
        assert texts.NO_LINKS in r.text


async def test_the_schema_types_both_formats(cp: CP) -> None:
    async with cp.client() as c:
        spec = (await c.get("/api/openapi.json")).json()
    op = spec["paths"]["/api/runs/{run_id}/report"]["get"]
    content = op["responses"]["200"]["content"]
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/RunReport"}
    assert content["text/markdown"]["schema"] == {"type": "string"}
    params = {p["name"]: p for p in op["parameters"]}
    assert params["format"]["schema"]["enum"] == ["json", "md"]
    assert set(op["responses"]) >= {"200", "401", "403", "404", "422"}


async def test_aims_ui_hands_its_public_url_to_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await open_state(tmp_path / "a", public_url="https://aims.example")
    try:
        assert state.public_url == "https://aims.example"
    finally:
        await close_state(state)
    seen: dict[str, Any] = {}

    async def opened(data: Path, **kw: Any) -> Any:
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(serve_module, "open_state", opened)
    options = UiOptions(data=tmp_path / "b", public_url="aims.example:8443", runner=False)
    with pytest.raises(RuntimeError, match="stop here"):
        await serve(options)
    assert seen["public_url"] == "https://aims.example:8443"
    with pytest.raises(RuntimeError, match="stop here"):
        await serve(UiOptions(data=tmp_path / "c", runner=False))
    assert seen["public_url"] is None
