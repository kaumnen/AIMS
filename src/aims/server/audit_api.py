"""The audit log on `/api`, for operators: a page of rows newest first, or every row that
matches as JSON Lines. `aims audit` reads the same rows."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response, StreamingResponse

from aims.server import audit
from aims.server.api import MAX_PAGE, Operator, Out, State
from aims.server.db import AuditRow, Outcome
from aims.server.problems import responses

router = APIRouter(prefix="/api", responses=responses(401, 403, 422))
EXPORT_PAGE = 1000
JSONL = "application/x-ndjson"


class AuditEntry(Out):
    """A write to the control plane or a failed sign-in. `at` is UTC. `auth` is how the caller
    came: a `bearer` token, a session `cookie`, `open` when sign-in is off, a `token` or a
    launch `code` to sign in, or `cli` for `aims token` on the server's machine. `token`,
    `role` and `auth` are null for a failed sign-in with no known token and for the server's
    own housekeeping. `status` is the HTTP status, null outside a request. `detail` says a
    little more, or why a refused write was refused."""

    id: int
    at: str
    token: str | None
    role: str | None
    auth: str | None
    action: str
    target: str | None
    outcome: Outcome
    status: int | None
    ip: str | None
    detail: str | None


class AuditPage(Out):
    items: list[AuditEntry]
    next: int | None = None


def entry(row: AuditRow) -> AuditEntry:
    return AuditEntry(
        id=row.id or 0,
        at=datetime.fromtimestamp(row.at, UTC).isoformat(),
        token=row.token,
        role=row.role,
        auth=row.auth,
        action=row.action,
        target=row.target,
        outcome=row.outcome,
        status=row.status,
        ip=row.ip,
        detail=row.detail,
    )


def _seconds(at: datetime | None) -> float | None:
    if at is None:
        return None
    return (at if at.tzinfo is not None else at.replace(tzinfo=UTC)).timestamp()


@router.get(
    "/audit",
    tags=["audit"],
    response_model=AuditPage,
    responses={200: {"content": {JSONL: {"schema": {"type": "string"}}}}},
)
async def list_audit(
    principal: Operator,
    state: State,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    action: Annotated[str | None, Query(max_length=100)] = None,
    token: Annotated[str | None, Query(max_length=100)] = None,
    outcome: Outcome | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    format: Literal["json", "jsonl"] = "json",
) -> AuditPage | Response:
    """The audit log, newest first: each write sent to `/api`, refused ones too, with each
    failed sign-in. `action` keeps that action and the ones under it, so `run` keeps
    `run.kill`. `since` and `until` are times, UTC unless they say. `before` is the `next` of
    the page before. `format=jsonl` gives every row that matches as JSON Lines, to save."""
    filters: dict[str, Any] = {
        "action": action,
        "token": token,
        "outcome": outcome,
        "since": _seconds(since),
        "until": _seconds(until),
    }
    if format == "jsonl":

        async def lines() -> AsyncIterator[str]:
            async for row in audit.every(state.db, page=EXPORT_PAGE, **filters):
                yield entry(row).model_dump_json() + "\n"

        name = f"aims-audit-{datetime.now(UTC):%Y%m%d-%H%M%S}.jsonl"
        return StreamingResponse(
            lines(),
            media_type=JSONL,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    rows = await state.db.audit_rows(limit=limit, before=before, **filters)
    return AuditPage(
        items=[entry(r) for r in rows], next=rows[-1].id if len(rows) == limit else None
    )
