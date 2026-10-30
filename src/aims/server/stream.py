"""`GET /api/stream`: one live connection per browser tab, carrying every subscription the page
holds. Events are read from the index by cursor, so a slow reader only lags; deltas are pushed
and dropped first when it falls behind. The SSE id is the cursor vector, so a reconnect with
Last-Event-ID resumes exactly."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from aims.events import RunCorrupt
from aims.models import Delta
from aims.server.api import State, Viewer, summaries
from aims.server.problems import ProblemException
from aims.wire import (
    DeltaMessage,
    DeltasDroppedMessage,
    EndMessage,
    EventsMessage,
    RunMessage,
    RunnerHealthMessage,
    StreamMessage,
    WireDelta,
)

router = APIRouter(prefix="/api", tags=["stream"])
BATCH = 1000
COALESCE_S = 0.05
WAKE_S = 5.0
MAX_RUNS = 50
DELTA_BATCH = 5000


def cursor_id(version: int | None, cursors: dict[str, int]) -> str:
    parts = [] if version is None else [f"@{version}"]
    parts += [f"{run}:{seq}" for run, seq in cursors.items()]
    return ",".join(parts)


def parse_id(value: str) -> tuple[int | None, dict[str, int]]:
    version: int | None = None
    cursors: dict[str, int] = {}
    for part in value.split(","):
        part = part.strip()
        if part.startswith("@") and part[1:].isdigit():
            version = int(part[1:])
            continue
        run, _, seq = part.rpartition(":")
        if run and seq.lstrip("-").isdigit():
            cursors[run] = int(seq)
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
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


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
    principal: Viewer,
    state: State,
    runs: bool = False,
    run: Annotated[list[str] | None, Query(max_length=MAX_RUNS)] = None,
    after: Annotated[list[int] | None, Query()] = None,
    types: Annotated[list[str] | None, Query()] = None,
    deltas: Annotated[list[str] | None, Query(max_length=MAX_RUNS)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Subscribe with `runs` (run list and runner changes), `run` with `after` (that run's
    events past a seq, `types` to filter) and `deltas` (live model output of runs). Each
    message's `event` is its `kind`."""
    run = run or []
    after = after or []
    if len(after) > len(run):
        raise ProblemException(422, "Invalid request", "one `after` per `run`")
    cursors = {r: (after[i] if i < len(after) else -1) for i, r in enumerate(run)}
    version: int | None = state.changes.version if runs or run else None
    if last_event_id:
        resumed_version, resumed = parse_id(last_event_id)
        cursors.update({r: s for r, s in resumed.items() if r in cursors})
        if resumed_version is not None and version is not None:
            version = resumed_version
    wanted = set(deltas or [])
    sub = state.bus.subscribe() if wanted else None
    ended: set[str] = set()
    runners_seen: dict[str, dict[str, object]] = {}
    dropped = 0
    pending: list[Delta] = []

    def message(m: StreamMessage, with_id: bool = False) -> ServerSentEvent:
        return ServerSentEvent(
            data=m, event=m.kind, id=cursor_id(version, cursors) if with_id else None
        )

    try:
        yield ServerSentEvent(comment="connected", retry=2000)
        if wanted:
            early = state.inflight.of(wanted)
            for i in range(0, len(early), DELTA_BATCH):
                yield message(DeltaMessage(deltas=early[i : i + DELTA_BATCH]))
        if runs:
            for info in sorted((c.info() for c in state.runners.values()), key=lambda i: i.name):
                runners_seen[info.name] = info.model_dump(exclude={"seen_s"})
                yield message(RunnerHealthMessage(runner=info))
        while not state.stopping.is_set():
            store_changed = state.store.changed()
            runs_changed = state.changes.event()
            runners_changed = state.runners_changed
            more = False
            for run_id in list(cursors):
                if run_id in ended:
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
                        listed = await state.store.runs(limit=MAX_RUNS)
                        pick = [i.run_id for i in listed] + list(cursors)
                    else:
                        pick = sorted(ids if runs else ids & set(cursors))
                    for summary in await summaries(state, pick):
                        yield message(RunMessage(run=summary), with_id=True)
            if runs:
                for conn in state.runners.values():
                    info = conn.info()
                    key = info.model_dump(exclude={"seen_s"})
                    if runners_seen.get(info.name) != key:
                        runners_seen[info.name] = key
                        yield message(RunnerHealthMessage(runner=info))
            if sub is not None:
                while len(pending) < DELTA_BATCH:
                    try:
                        d = sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if d.run_id in wanted:
                        pending.append(d)
                batch = [_delta(d) for d in pending if d.run_id in wanted]
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
            ]
            if version is not None:
                waits.append(runs_changed.wait())
            if sub is not None:
                waits.append(_peek(sub.queue, pending))
            await _first(waits, WAKE_S)
            await asyncio.sleep(COALESCE_S)
    finally:
        if sub is not None:
            sub.close()


async def _peek(queue: asyncio.Queue[Delta], into: list[Delta]) -> None:
    into.append(await queue.get())
