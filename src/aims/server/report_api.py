"""The per-run report on `/api`: the same report `aims report` writes, as JSON or Markdown."""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response

from aims.report.build import ReportError, base_url_problem, build
from aims.report.markdown import render
from aims.report.model import Role, RunReport
from aims.server.api import State, Viewer, _run_id, _shown
from aims.server.problems import ProblemException, responses

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))
MARKDOWN = "text/markdown; charset=utf-8"


@router.get(
    "/runs/{run_id}/report",
    tags=["runs"],
    response_model=RunReport,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
        **responses(404, 503),
    },
)
async def get_report(
    run_id: str,
    request: Request,
    principal: Viewer,
    state: State,
    source: str | None = None,
    format: Literal["json", "md"] = "json",
) -> RunReport | Response:
    """The report of a run, built from its log each time. `source` picks the monitor: `live`
    or a monitor run over the run; by default the newest finished review, else live. Viewers
    get no plants and no score. `format=md` gives the Markdown file `aims report` writes.
    Evidence links start at the server's public URL, else at the address the request used."""
    run_id = _run_id(run_id)
    await _shown(state, principal, run_id)
    role: Role = "viewer" if principal.role == "viewer" else "operator"
    base = state.public_url or str(request.base_url).rstrip("/")
    try:
        report = await build(
            state.store,
            run_id,
            source=source,
            role=role,
            base_url=None if base_url_problem(base) else base,
        )
    except ReportError as e:
        raise ProblemException(404, "Not found", str(e)) from None
    if format == "json":
        return report
    return Response(
        render(report),
        media_type=MARKDOWN,
        headers={"Content-Disposition": f'attachment; filename="aims-report-{run_id}.md"'},
    )
