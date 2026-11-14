"""`GET /api/stream`: one live connection per browser tab, carrying every subscription the page
holds. Events are read from the index by cursor, so a slow reader only lags; deltas are pushed
and dropped first when it falls behind. The SSE id is the cursor vector, so a reconnect with
Last-Event-ID resumes exactly."""

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from aims.aio import stopped
from aims.events import RunCorrupt
from aims.events.index import BIND_MAX
from aims.models import Delta
from aims.server.api import Cursor, State, Viewer, _kind, _visible, hidden, summaries, unseen
from aims.server.problems import ProblemException, responses
from aims.server.state import ServerState
from aims.wire import (
    DeltaMessage,
    DeltasDroppedMessage,
    EndMessage,
    EventsMessage,
    RunDeletedMessage,
    RunMessage,
    RunnerHealthMessage,
    StreamMessage,
    WireDelta,
)

router = APIRouter(prefix="/api", tags=["stream"], responses=responses(401, 403))
BATCH = 1000
COALESCE_S = 0.05
WAKE_S = 5.0
# how often a stream checks that its session or token still holds; ending one through this
# server wakes every stream at once
RECHECK_S = 30.0
MAX_RUNS = 50
DELTA_BATCH = 5000


def cursor_id(version: int | None, cursors: dict[str, int]) -> str:
    parts = [] if version is None else [f"@{version}"]
    parts += [f"{run}:{seq}" for run, seq in cursors.items()]
    return ",".join(parts)


def _number(text: str, low: int) -> int | None:
    """`text` as an int from `low` up to what the index holds; None when it does not read as
    one. Only ASCII digits count."""
    if not re.fullmatch(r"-?[0-9]{1,19}", text):
        return None
    n = int(text)
    return n if low <= n <= BIND_MAX else None


def parse_id(value: str) -> tuple[int | None, dict[str, int]]:
    """The version and cursors in a Last-Event-ID. A part that does not read is skipped."""
    version: int | None = None
    cursors: dict[str, int] = {}
    for part in value.split(","):
        part = part.strip()
        if part.startswith("@") and (found := _number(part[1:], 0)) is not None:
            version = found
            continue
        run, _, seq = part.rpartition(":")
        if run and (found := _number(seq, -1)) is not None:
            cursors[run] = found
    return version, cursors


def _delta(d: Delta) -> WireDelta:
    return WireDelta(
        run_id=d.run_id,
        agent_id=d.agent_id,
        call_id=d.call_id,
        attempt=d.attempt,
        kind=d.kind,
        text=d.text,
        index=d.index,
        ms=d.ms,
    )


async def _first(waits: list[Coroutine[Any, Any, Any]], within: float) -> None:
    tasks = [asyncio.ensure_future(w) for w in waits]
    try:
        await asyncio.wait(tasks, timeout=within, return_when=asyncio.FIRST_COMPLETED)
    finally:
        with contextlib.suppress(Exception):
            await stopped(*tasks)


Runs = Annotated[list[str] | None, Query(max_length=MAX_RUNS)]


async def _asked(
    principal: Viewer,
    state: State,
    run: Runs = None,
    after: Annotated[list[Cursor] | None, Query()] = None,
    deltas: Runs = None,
) -> None:
    """Refuse a subscription before the stream opens: a stream's errors after that come too
    late for a status. A run the index does not hold yet is waited for, not refused: a
    browser's event source gives up on any status but 200."""
    if len(after or []) > len(run or []):
        raise ProblemException(422, "Invalid request", "one `after` per `run`")
    for run_id in {*(run or []), *(deltas or [])}:
        await _visible(state, principal, run_id)


async def _hides(state: ServerState, unsure: set[str], skip: Sequence[str]) -> bool:
    """Whether a run of `unsure` turned out to be of a kind `unseen` keeps from a principal who
    does not see `skip`. Each run whose kind is known now leaves `unsure`."""
    for run_id in list(unsure):
        kind = await _kind(state, run_id)
        if kind is None:
            continue
        if unseen(kind, skip):
            return True
        unsure.discard(run_id)
    return False


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "itemSchema": {
                        "type": "object",
                        "required": ["data"],
                        "properties": {
                            "event": {"type": "string"},
                            "id": {"type": "string"},
                            "data": {
                                "type": "string",
                                "contentMediaType": "application/json",
                                "contentSchema": {"$ref": "#/components/schemas/StreamMessage"},
                            },
                        },
                    }
                }
            }
        }
    },
)
async def stream(
    request: Request,
    principal: Viewer,
    state: State,
    _: Annotated[None, Depends(_asked)],
    runs: bool = False,
    run: Runs = None,
    after: Annotated[list[Cursor] | None, Query()] = None,
    types: Annotated[list[str] | None, Query()] = None,
    deltas: Runs = None,
    last_event_id: str | None = None,
    header_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Subscribe with `runs` (the run list, runner changes and deleted runs), `run` with
    `after` (that run's events past a seq, `types` to filter) and `deltas` (live model output
    of runs). Each message's `event` is its `kind`. `last_event_id` resumes like the
    Last-Event-ID header,
    for a client that opens a new stream with other subscriptions; the header wins, since a
    browser that reconnects sends a newer one than its URL holds. A run whose kind is not known
    yet, as while the index is rebuilt, sends a viewer nothing until it is; when it turns out
    to be one the viewer may not read, the stream ends and a new one is refused with 403."""
    run = run or []
    after = after or []
    cursors = {r: (after[i] if i < len(after) else -1) for i, r in enumerate(run)}
    version: int | None = state.changes.version if runs or run else None
    resume = header_id or last_event_id
    if resume:
        resumed_version, resumed = parse_id(resume)
        cursors.update({r: s for r, s in resumed.items() if r in cursors})
        if resumed_version is not None and version is not None:
            version = resumed_version
    wanted = set(deltas or [])
    skip = hidden(principal)
    # runs subscribed to before their kind was known, as while the index is rebuilt: nothing
    # of theirs goes out until it is
    unsure = {*cursors, *wanted} if skip else set()
    sub = state.bus.subscribe() if wanted else None
    ended: set[str] = set()
    runners_seen: dict[str, dict[str, object]] = {}
    dropped = 0
    pending: list[Delta] = []

    def message(m: StreamMessage, with_id: bool = False) -> ServerSentEvent:
        return ServerSentEvent(
            data=m, event=m.kind, id=cursor_id(version, cursors) if with_id else None
        )

    revoked = state.auth.revoked
    checked = time.monotonic()
    state.streams += 1
    try:
        yield ServerSentEvent(comment="connected", retry=2000)
        if await _hides(state, unsure, skip):
            return
        if wanted:
            early = state.inflight.of(wanted - unsure)
            for i in range(0, len(early), DELTA_BATCH):
                yield message(DeltaMessage(deltas=early[i : i + DELTA_BATCH]))
        if runs:
            for info in sorted((c.info() for c in state.runners.values()), key=lambda i: i.name):
                runners_seen[info.name] = info.model_dump(exclude={"seen_s"})
                yield message(RunnerHealthMessage(runner=info), with_id=True)
        while not state.stopping.is_set():
            if revoked.is_set() or time.monotonic() - checked > RECHECK_S:
                revoked, checked = state.auth.revoked, time.monotonic()
                if not await state.auth.still(request, principal):
                    return
            if await _hides(state, unsure, skip):
                # `_asked` refuses this subscribe now: the reconnect gets its 403
                return
            live = wanted - unsure
            store_changed = state.store.changed()
            runs_changed = state.changes.event()
            runners_changed = state.runners_changed
            more = False
            for run_id in list(cursors):
                if run_id in ended or run_id in unsure:
                    continue
                try:
                    head = await state.store.head(run_id)
                except RunCorrupt, KeyError:
                    head = None
                if head is None:
                    continue
                cursor = cursors[run_id]
                if head.seq > cursor:
                    events = await state.store.events(
                        run_id, after=cursor, before=head.seq + 1, limit=BATCH, types=types or ()
                    )
                    full = len(events) == BATCH
                    cursors[run_id] = events[-1].seq if full else head.seq
                    more |= full
                    if events:
                        yield message(EventsMessage(run_id=run_id, events=events), with_id=True)
                if head.finished and cursors[run_id] >= head.seq:
                    ended.add(run_id)
                    yield message(EndMessage(run_id=run_id), with_id=True)
            if version is not None:
                version, changed = state.changes.since(version)
                ids = set(changed) if changed is not None else None
                if runs or ids:
                    if ids is None:
                        # as the first page of the run list: with the jobs not started yet
                        listed = await state.store.runs(limit=MAX_RUNS, skip_kinds=skip)
                        jobs = await state.db.jobs_page(
                            before=None, limit=MAX_RUNS, skip_kinds=skip
                        )
                        newest = {i.run_id for i in listed} | {j.run_id for j in jobs}
                        pick = sorted(newest, reverse=True)[:MAX_RUNS] + list(cursors)
                        # and the runs deleted lately, which a list held from before may show
                        lately = list(state.gone)[-MAX_RUNS:]
                        pick += [gone for gone in lately if gone not in newest]
                    else:
                        pick = sorted(ids if runs else ids & set(cursors))
                    found = await summaries(state, pick, skip)
                    for summary in found:
                        yield message(RunMessage(run=summary), with_id=True)
                    shown = {s.run_id for s in found}
                    for run_id in pick:
                        kind = state.gone.get(run_id)
                        if run_id not in shown and kind is not None and not unseen(kind, skip):
                            yield message(RunDeletedMessage(run_id=run_id), with_id=True)
            if runs:
                for conn in sorted(state.runners.values(), key=lambda c: c.name):
                    info = conn.info()
                    key = info.model_dump(exclude={"seen_s"})
                    if runners_seen.get(info.name) != key:
                        runners_seen[info.name] = key
                        yield message(RunnerHealthMessage(runner=info), with_id=True)
            if sub is not None:
                while len(pending) < DELTA_BATCH:
                    try:
                        d = sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if d.run_id in live:
                        pending.append(d)
                batch = [_delta(d) for d in pending if d.run_id in live]
                pending.clear()
                if batch:
                    yield message(DeltaMessage(deltas=batch))
                if sub.dropped > dropped:
                    yield message(DeltasDroppedMessage(count=sub.dropped - dropped))
                    dropped = sub.dropped
            if more:
                continue
            waits: list[Coroutine[Any, Any, Any]] = [
                store_changed.wait(),
                runners_changed.wait(),
                state.stopping.wait(),
                revoked.wait(),
            ]
            if version is not None:
                waits.append(runs_changed.wait())
            if sub is not None:
                waits.append(_peek(sub.queue, pending))
            await _first(waits, WAKE_S)
            await asyncio.sleep(COALESCE_S)
    finally:
        state.streams -= 1
        if sub is not None:
            sub.close()


async def _peek(queue: asyncio.Queue[Delta], into: list[Delta]) -> None:
    into.append(await queue.get())
