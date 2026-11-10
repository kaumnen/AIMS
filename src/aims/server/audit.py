"""The audit log: a row for each write a caller sends to `/api`, refused ones too. Failed
sign-ins get one as well. A row says who sent it (token, role and how they signed in), what it
did (a dotted action and its target), how it went and from which address. It never holds a
secret or the contents of a file: handlers give it names, never bodies. Reads and runner
traffic are not recorded.

A handler is marked with `@audited("token.create")`. Its target is the route's path parameters
unless the handler names one. A `detail` it may set says a little more. Code outside a
request, such as a background task, calls `record` with no token.

Actions: session.start, session.end, session.code, token.create, token.rotate, token.revoke,
config.write, config.patch, scenario.create, scenario.duplicate, scenario.file.write,
scenario.file.delete, scenario.prompts, scenario.selftest, run.create, run.fork, run.pause,
run.resume, run.sweep, run.inject, run.kill, run.cancel, run.import, run.monitor, run.score,
run.label, run.delete, scenario.delete, models.check, run.prune and audit.prune."""

import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aims.config import ConfigError, Layers, audit_keep_days
from aims.server.auth import SAFE, Principal
from aims.server.db import AuditRow, Outcome, ServerDB
from aims.server.problems import Problem

log = logging.getLogger("aims.server")

KEY = "aims.audit"
TARGET_MAX = 300
DETAIL_MAX = 300
# recorded even when the caller is not known, as a failed sign-in is
UNKNOWN_CALLERS = frozenset({"session.start"})
ACTIONS: dict[Callable[..., Any], str] = {}


@dataclass(slots=True)
class Entry:
    """What a handler adds to the row of its request: names, never contents. The sign-in
    route, which has no principal, says who signed in and how."""

    target: str | None = None
    detail: str | None = None
    token: str | None = None
    role: str | None = None
    auth: str | None = None


def entry_of(scope: Scope) -> Entry:
    state = scope.setdefault("state", {})
    found = state.get(KEY)
    if not isinstance(found, Entry):
        found = state[KEY] = Entry()
    return found


def _entry(request: Request) -> Entry:
    return entry_of(request.scope)


Note = Annotated[Entry, Depends(_entry)]


def audited[F: Callable[..., Any]](action: str) -> Callable[[F], F]:
    """Mark a route's handler: each request to it leaves a row with `action`."""

    def mark(handler: F) -> F:
        ACTIONS[handler] = action
        return handler

    return mark


def runs(run_ids: Sequence[str]) -> str | None:
    """The detail of a write that starts runs."""
    if len(run_ids) <= 1:
        return run_ids[0] if run_ids else None
    return f"{len(run_ids)} runs, {run_ids[0]} to {run_ids[-1]}"


def _short(text: str | None, most: int) -> str | None:
    if text is None or len(text) <= most:
        return text
    return text[: most - 3] + "..."


async def record(
    db: ServerDB,
    action: str,
    *,
    outcome: Outcome = "ok",
    status: int | None = None,
    token: str | None = None,
    role: str | None = None,
    auth: str | None = None,
    target: str | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Add a row. A row that cannot be written is logged as a warning; the action stands."""
    row = AuditRow(
        at=time.time(),
        action=action,
        outcome=outcome,
        status=status,
        token=token,
        role=role,
        auth=auth,
        target=_short(target, TARGET_MAX),
        ip=ip,
        detail=_short(detail, DETAIL_MAX),
    )
    try:
        await db.add_audit(row)
    except Exception as e:
        log.warning("the audit row of %s on %s was not written: %s", action, target, e)


async def every(
    db: ServerDB, *, page: int, limit: int = 0, **filters: Any
) -> AsyncIterator[AuditRow]:
    """The rows that match `filters`, newest first, read `page` at a time; at most `limit`
    of them, or all when it is 0."""
    before = None
    left = limit or None
    while True:
        size = page if left is None else min(page, left)
        rows = await db.audit_rows(limit=size, before=before, **filters)
        for row in rows:
            yield row
        if left is not None:
            left -= len(rows)
        if len(rows) < size or left == 0:
            return
        before = rows[-1].id


async def prune(db: ServerDB, layers: Callable[[], Layers]) -> int:
    """Drop the rows older than `[audit] keep_days`; 0 keeps them all. A config that cannot
    say keeps every row."""
    try:
        days = audit_keep_days(layers())
    except ConfigError as e:
        log.warning("the audit log was not pruned: %s", e)
        return 0
    if days <= 0:
        return 0
    pruned = await db.prune_audit(time.time() - days * 86400)
    if pruned:
        detail = f"{pruned} row{'s' if pruned != 1 else ''} older than {days:g} days"
        log.info("audit log: dropped %s", detail)
        await record(db, "audit.prune", detail=detail)
    return pruned


def _outcome(status: int) -> Outcome:
    if status < 400:
        return "ok"
    return "refused" if status < 500 else "error"


class AuditLog:
    """Writes the row of each write to an audited route under `/api` once its reply starts,
    before the caller hears back. The caller is the principal `auth.require` found. A request
    it found no principal for leaves no row, unless it was a sign-in. The client address is
    the one the server took from the connection, or from a proxy it trusts."""

    def __init__(self, app: ASGIApp, db: ServerDB) -> None:
        self.app = app
        self.db = db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in SAFE
            or not scope["path"].startswith("/api/")
        ):
            await self.app(scope, receive, send)
            return
        written = False

        async def wrapped(message: Message) -> None:
            nonlocal written
            if message["type"] == "http.response.start" and not written:
                written = True
                await self._write(scope, message["status"])
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            if not written:
                written = True
                await self._write(scope, 500)
            raise

    async def _write(self, scope: Scope, status: int) -> None:
        action = ACTIONS.get(getattr(scope.get("route"), "endpoint", None))
        if action is None:
            return
        state = scope.get("state", {})
        entry = entry_of(scope)
        principal = state.get("principal")
        if isinstance(principal, Principal):
            token, role, auth = principal.name, principal.role, principal.via
        elif action in UNKNOWN_CALLERS:
            token, role, auth = entry.token, entry.role, entry.auth
        else:
            return
        target = entry.target
        if target is None:
            target = "/".join(str(v) for v in (scope.get("path_params") or {}).values()) or None
        detail = entry.detail
        if status >= 400:
            problem = state.get("problem")
            detail = (problem.code or problem.title) if isinstance(problem, Problem) else None
        client = scope.get("client")
        await record(
            self.db,
            action,
            outcome=_outcome(status),
            status=status,
            token=token,
            role=role,
            auth=auth,
            target=target,
            ip=client[0] if client else None,
            detail=detail,
        )
