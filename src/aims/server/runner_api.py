"""`/runner/v1`: what runners call. A runner says hello under a name, holds a command stream,
claims jobs it is offered, and ships its runs: blobs first, then exact log lines."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from compression import zstd
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import TypeAdapter, ValidationError

from aims import __version__
from aims.events import Diverged, Gap, Invalid, MissingBlobs, RunCorrupt, split_lines
from aims.events.blobs import BlobError
from aims.events.chain import MAX_LINE
from aims.events.store import TruthRefused, is_run_id
from aims.server.auth import Principal, require
from aims.server.problems import ProblemException, responses
from aims.server.state import Connected, ServerState
from aims.wire import (
    RUNNER_API,
    Ack,
    AnyJob,
    Claimed,
    CommandAction,
    CommandMessage,
    Deltas,
    Head,
    Heads,
    HeadsReply,
    HelloReply,
    Labels,
    OfferMessage,
    Release,
    RunnerHello,
    RunnerStatus,
    Shas,
)

MAX_EVENTS = 1000
MAX_BATCH = 4 * 1024 * 1024 + MAX_LINE
MAX_WIRE = 16 * 1024 * 1024
BLOB_LIMIT = 2 * 1024**3
RELEASE_TRIES = 3
JOBS: TypeAdapter[AnyJob] = TypeAdapter(AnyJob)

router = APIRouter(prefix="/runner/v1", tags=["runner"], responses=responses(401, 403, 409))
RunnerToken = Annotated[Principal, Depends(require("runner"))]


def state_of(request: Request) -> ServerState:
    return request.app.state.server


State = Annotated[ServerState, Depends(state_of)]


def connected(state: ServerState, principal: Principal, name: str | None) -> Connected:
    if not name:
        raise ProblemException(400, "Bad request", "send X-Aims-Runner", code="runner_name")
    conn = state.runners.get(name)
    if conn is None:
        raise ProblemException(409, "Conflict", "say hello first", code="hello")
    if conn.token != principal.name:
        raise ProblemException(403, "Forbidden", f"runner {name} uses another token", code="name")
    conn.seen = time.time()
    return conn


RunnerName = Annotated[str | None, Header(alias="X-Aims-Runner")]


async def writer(state: ServerState, run_id: str, conn: Connected) -> None:
    """Refuse unless `conn` writes this run: its job was claimed by it, or it shipped the run
    first, or the runner that did uses the same token (an `aims push` finishing what an
    `aims run` began). A run nobody claimed yet becomes this runner's."""
    if not is_run_id(run_id):
        raise ProblemException(404, "Not found", f"not a run id: {run_id}")
    job = await state.db.job(run_id)
    if job is not None:
        if job.runner != conn.name or job.state not in ("claimed", "running", "done"):
            raise ProblemException(403, "Forbidden", f"run {run_id} is not yours", code="owner")
        await state.db.attribute(run_id, conn.name, "job")
        return
    meta = await state.db.run_meta(run_id)
    if meta is None or meta.runner is None:
        meta = await state.db.attribute(run_id, conn.name, "push")
    if meta.source == "import" or not await _same_owner(state, meta.runner, conn):
        raise ProblemException(403, "Forbidden", f"run {run_id} is not yours", code="owner")


async def _same_owner(state: ServerState, owner: str | None, conn: Connected) -> bool:
    if owner == conn.name:
        return True
    if owner is None:
        return False
    known = await state.db.runner(owner)
    return known is not None and known.token == conn.token


async def reader(state: ServerState, run_id: str, name: str) -> None:
    """Refuse unless `name` owns the run or holds a fork or monitor job of it."""
    meta = await state.db.run_meta(run_id)
    if meta is not None and meta.runner == name and meta.source != "import":
        return
    for job in await state.db.jobs(("claimed", "running")):
        if job.runner != name:
            continue
        fork = job.payload.get("fork")
        if isinstance(fork, dict) and fork.get("origin") == run_id:
            return
        if job.payload.get("kind") == "monitor" and job.payload.get("origin") == run_id:
            return
        if job.payload.get("kind") == "score" and run_id in (
            job.payload.get("origin"),
            job.payload.get("monitor"),
        ):
            return
    raise ProblemException(403, "Forbidden", f"run {run_id} is not yours to read", code="owner")


def wanted_envs(state: ServerState) -> list[str]:
    try:
        settings = state.config.settings()
    except ProblemException:
        return []
    names: set[str] = set()
    for p in settings.models.profiles.values():
        names.update(e for e in (p.api_key_env, p.base_url_env) if e)
    return sorted(names)


@router.post("/hello")
async def runner_hello(
    body: RunnerHello, principal: RunnerToken, state: State, name: RunnerName = None
) -> HelloReply:
    if body.api != RUNNER_API or body.version != __version__:
        raise ProblemException(
            409,
            "Conflict",
            f"runner {body.version} (api {body.api}) does not match control plane {__version__}"
            f" (api {RUNNER_API})",
            code="version",
        )
    if name != body.name:
        raise ProblemException(400, "Bad request", "X-Aims-Runner must match the name")
    local = {t.name for t in state.auth.ephemeral.values()}
    if body.name == state.local_runner and principal.name not in local:
        raise ProblemException(
            403, "Forbidden", f"{body.name} is the name of the local runner", code="name"
        )
    known = await state.db.runner(body.name)
    if known is not None and known.token != principal.name:
        raise ProblemException(
            403, "Forbidden", f"runner {body.name} uses another token", code="name"
        )
    await state.db.hello(body.name, principal.name, body.model_dump(mode="json"))
    conn = state.runners.get(body.name)
    if conn is None:
        conn = Connected(body.name, principal.name, body, local=body.name == state.local_runner)
        state.runners[body.name] = conn
    else:
        conn.hello, conn.status, conn.seen = body, None, time.time()
    requeued: list[str] = []
    for job in await state.db.jobs(("offered", "claimed")):
        if job.runner != body.name or job.run_id in body.jobs:
            continue
        if await state.store.run(job.run_id) is not None:
            continue
        if await state.db.requeue(job.run_id, states=("offered", "claimed")):
            requeued.append(job.run_id)
            conn.offered.discard(job.run_id)
            state.changes.touch(job.run_id)
    state.runner_changed()
    state.poke()
    return HelloReply(name=body.name, requeued=requeued, envs=wanted_envs(state))


@router.post("/status", status_code=204)
async def runner_status(
    body: RunnerStatus, principal: RunnerToken, state: State, name: RunnerName = None
) -> None:
    conn = connected(state, principal, name)
    conn.status = body
    await state.db.seen(conn.name)
    state.runner_changed()
    state.poke()


def _feed_event(message: CommandMessage | OfferMessage) -> ServerSentEvent:
    if isinstance(message, CommandMessage):
        return ServerSentEvent(data=message, event="command", id=str(message.seq))
    return ServerSentEvent(data=message, event="offer")


@router.get("/stream", response_class=EventSourceResponse)
async def runner_stream(
    principal: RunnerToken, state: State, name: RunnerName = None
) -> AsyncIterator[ServerSentEvent]:
    """Commands for this runner's runs, unacknowledged ones first, and job offers."""
    conn = connected(state, principal, name)
    old = conn.feed
    conn.feed = feed = asyncio.Queue(10_000)
    with contextlib.suppress(asyncio.QueueFull):
        old.put_nowait(None)
    conn.streams += 1
    state.runner_changed()
    state.poke()
    try:
        yield ServerSentEvent(comment="connected", retry=2000)
        sent: set[str] = set()
        for c in await state.db.pending_commands(conn.name):
            sent.add(c.id)
            yield _feed_event(
                CommandMessage(
                    id=c.id, seq=c.seq, run_id=c.run_id, command=CommandAction(**c.payload)
                )
            )
        while True:
            message = await feed.get()
            if message is None:
                return
            if isinstance(message, CommandMessage) and message.id in sent:
                continue
            yield _feed_event(message)
    finally:
        conn.streams -= 1
        if conn.feed is feed:
            for run_id in list(conn.offered):
                if await state.db.requeue(run_id, runner=conn.name):
                    state.changes.touch(run_id)
            conn.offered.clear()
        state.runner_changed()
        state.poke()


@router.post("/commands/{command_id}/ack", status_code=204, responses=responses(404))
async def ack_command(
    command_id: str, body: Ack, principal: RunnerToken, state: State, name: RunnerName = None
) -> None:
    conn = connected(state, principal, name)
    command = await state.db.command(command_id)
    if command is None or command.runner != conn.name:
        raise ProblemException(404, "Not found", f"no command {command_id}")
    outcome = body.outcome if body.ok else f"failed: {body.outcome}"
    await state.db.ack(command_id, conn.name, outcome)
    state.changes.touch(command.run_id)


@router.post("/jobs/{run_id}/claim")
async def claim_job(
    run_id: str, principal: RunnerToken, state: State, name: RunnerName = None
) -> Claimed:
    conn = connected(state, principal, name)
    conn.offered.discard(run_id)
    if not await state.db.claim(run_id, conn.name):
        raise ProblemException(409, "Conflict", "the job is not offered to you", code="claim")
    job = await state.db.job(run_id)
    assert job is not None
    state.changes.touch(run_id)
    try:
        return Claimed(job=JOBS.validate_python(job.payload))
    except ValidationError as e:
        await state.db.set_job(run_id, "done", f"the job does not load: {e}")
        state.poke()
        raise ProblemException(409, "Conflict", "the job does not load", code="claim") from None


@router.post("/jobs/{run_id}/release", status_code=204)
async def release_job(
    run_id: str, body: Release, principal: RunnerToken, state: State, name: RunnerName = None
) -> None:
    conn = connected(state, principal, name)
    conn.offered.discard(run_id)
    job = await state.db.job(run_id)
    if job is None or job.runner != conn.name or job.state not in ("offered", "claimed"):
        raise ProblemException(409, "Conflict", "the job is not yours to release", code="claim")
    if await state.db.released(run_id) >= RELEASE_TRIES:
        await state.db.set_job(run_id, "done", f"no runner could start it: {body.reason}")
    else:
        await state.db.requeue(run_id, states=("offered", "claimed"))
    state.changes.touch(run_id)
    state.poke()


@router.get(
    "/jobs/{run_id}/blobs/{sha256}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def job_blob(
    run_id: str, sha256: str, principal: RunnerToken, state: State, name: RunnerName = None
) -> FileResponse:
    conn = connected(state, principal, name)
    job = await state.db.job(run_id)
    if job is None or job.runner != conn.name:
        raise ProblemException(403, "Forbidden", "the job is not yours", code="owner")
    if sha256 not in JOBS.validate_python(job.payload).blobs():
        raise ProblemException(404, "Not found", f"the job has no blob {sha256}")
    return _stored(state, run_id, sha256)


def _stored(state: ServerState, run_id: str, sha256: str) -> FileResponse:
    try:
        path = state.store.blob_path(run_id, sha256)
    except KeyError, BlobError:
        raise ProblemException(404, "Not found", f"no blob {sha256}") from None
    return FileResponse(path, media_type="application/octet-stream")


@router.post("/heads")
async def run_heads(
    body: Heads, principal: RunnerToken, state: State, name: RunnerName = None
) -> HeadsReply:
    connected(state, principal, name)
    out: dict[str, Head] = {}
    for run_id in body.run_ids:
        try:
            head = await state.store.head(run_id) if is_run_id(run_id) else None
        except RunCorrupt:
            head = None
        if head is None:
            out[run_id] = Head()
            continue
        truth = await asyncio.to_thread(state.store.truth_path(run_id).is_file)
        out[run_id] = Head(seq=head.seq, hash=head.hash, truth=truth)
    return HeadsReply(heads=out)


@router.post("/runs/{run_id}/blobs/missing")
async def missing_blobs(
    run_id: str, body: Shas, principal: RunnerToken, state: State, name: RunnerName = None
) -> dict[str, list[str]]:
    conn = connected(state, principal, name)
    await writer(state, run_id, conn)
    state.store.prepare_run(run_id)
    try:
        return {"missing": await state.store.missing_blobs(run_id, body.shas)}
    except BlobError as e:
        raise ProblemException(422, "Invalid request", str(e), code="invalid") from None


@router.put("/runs/{run_id}/blobs/{sha256}", status_code=204, responses=responses(413, 422))
async def put_blob(
    run_id: str,
    sha256: str,
    request: Request,
    principal: RunnerToken,
    state: State,
    name: RunnerName = None,
) -> None:
    conn = connected(state, principal, name)
    await writer(state, run_id, conn)
    state.store.prepare_run(run_id)
    try:
        await state.store.put_blob_stream(run_id, sha256, request.stream(), limit=BLOB_LIMIT)
    except BlobError as e:
        raise ProblemException(422, "Invalid request", str(e), code="blob") from None


TRUTH_LIMIT = 16 * 1024 * 1024


@router.put(
    "/runs/{run_id}/truth",
    status_code=204,
    openapi_extra={"requestBody": {"content": {"application/json": {}}, "required": True}},
    responses=responses(404, 413, 422),
)
async def put_truth(
    run_id: str,
    request: Request,
    principal: RunnerToken,
    state: State,
    name: RunnerName = None,
) -> None:
    """The truth file of a sealed run, sent by the runner that wrote it once the control plane
    holds the run sealed. Its bytes must hash to the truth hash in run.started; sending the
    same file again does nothing."""
    conn = connected(state, principal, name)
    await writer(state, run_id, conn)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > TRUTH_LIMIT:
            raise ProblemException(413, "Too large", f"a truth file is at most {TRUTH_LIMIT} bytes")
        chunks.append(chunk)
    try:
        kept = await state.store.put_truth(run_id, b"".join(chunks))
    except TruthRefused as e:
        raise ProblemException(e.status, "Truth refused", str(e), code="truth") from None
    if kept and state.on_truth is not None:
        await state.on_truth(run_id)


@router.get(
    "/runs/{run_id}/truth",
    response_class=Response,
    responses={200: {"content": {"application/json": {}}}, **responses(404)},
)
async def runner_truth(
    run_id: str, principal: RunnerToken, state: State, name: RunnerName = None
) -> Response:
    """The truth file of a sealed run, for the runner that holds a score job of it."""
    conn = connected(state, principal, name)
    await reader(state, run_id, conn.name)
    try:
        data = await asyncio.to_thread(state.store.truth_path(run_id).read_bytes)
    except OSError:
        raise ProblemException(404, "Not found", f"run {run_id} has no truth file here") from None
    return Response(data, media_type="application/json")


@router.post("/runs/{run_id}/labels", status_code=204, responses=responses(404, 422))
async def post_labels(
    run_id: str, body: Labels, principal: RunnerToken, state: State, name: RunnerName = None
) -> None:
    """Labels the runner's people gave the flags of a run it wrote; those the control plane
    holds already are left alone."""
    conn = connected(state, principal, name)
    await writer(state, run_id, conn)
    if await state.store.run(run_id) is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    if any(x.run_id != run_id for x in body.labels):
        raise ProblemException(422, "Invalid request", "every label must name this run")
    await state.store.merge_labels(run_id, body.labels)


async def _body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WIRE:
            raise ProblemException(413, "Too large", f"a batch is at most {MAX_WIRE} bytes")
        chunks.append(chunk)
    data = b"".join(chunks)
    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding == "identity":
        if len(data) > MAX_BATCH:
            raise ProblemException(413, "Too large", "a batch is at most 4 MiB of lines")
        return data
    if encoding != "zstd":
        raise ProblemException(415, "Unsupported media type", f"content-encoding {encoding}")
    try:
        frame = zstd.ZstdDecompressor()
        out = frame.decompress(data, max_length=MAX_BATCH + 1)
    except zstd.ZstdError as e:
        raise ProblemException(400, "Bad request", f"not zstd: {e}") from None
    if len(out) > MAX_BATCH:
        raise ProblemException(413, "Too large", "a batch is at most 4 MiB of lines")
    return out


@router.post(
    "/runs/{run_id}/events",
    openapi_extra={"requestBody": {"content": {"application/x-ndjson": {}}, "required": True}},
    responses=responses(409, 413, 415, 422),
)
async def ingest_events(
    run_id: str, request: Request, principal: RunnerToken, state: State, name: RunnerName = None
) -> dict[str, Head]:
    """Exact log lines of one run, the next after the copy here; overlap is checked and
    skipped. Blobs the lines name must be stored first."""
    conn = connected(state, principal, name)
    await writer(state, run_id, conn)
    lines = split_lines(await _body(request))
    if len(lines) > MAX_EVENTS:
        raise ProblemException(413, "Too large", f"a batch is at most {MAX_EVENTS} events")
    store = state.store
    try:
        before = await store.head(run_id)
        head = await store.ingest(run_id, lines)
    except MissingBlobs as e:
        raise ProblemException(
            422,
            "Blobs missing",
            str(e),
            code="missing_blobs",
            missing=e.missing,
            head=_head(e.head),
        ) from None
    except Gap as e:
        raise ProblemException(409, "Conflict", str(e), code="gap", head=_head(e.head)) from None
    except Diverged as e:
        await state.db.mark_diverged(run_id, str(e))
        state.changes.touch(run_id)
        raise ProblemException(
            409, "Conflict", str(e), code="diverged", head=_head(e.head)
        ) from None
    except Invalid as e:
        raise ProblemException(
            422, "Invalid request", str(e), code="invalid", head=_head(e.head)
        ) from None
    except RunCorrupt as e:
        raise ProblemException(409, "Conflict", str(e), code="corrupt") from None
    job = await state.db.job(run_id)
    if job is not None and job.state == "claimed":
        await state.db.set_job(run_id, "running")
    after = -1 if before is None else before.seq
    for event in await store.events(run_id, after=after, types=["model.response"], limit=1000):
        call_id = event.data.get("call_id")
        if isinstance(call_id, str):
            state.inflight.drop(run_id, call_id)
    if head.finished:
        info = await store.run(run_id)
        if job is not None:
            await state.db.set_job(run_id, "done", None if info is None else info.status)
        await state.db.drop_commands(run_id)
        state.poke()
    return {"head": _head(head)}


def _head(head: object) -> Head:
    seq = getattr(head, "seq", -1)
    return Head(seq=seq, hash=getattr(head, "hash", None) if seq >= 0 else None)


@router.post("/deltas", status_code=204)
async def post_deltas(
    body: Deltas, principal: RunnerToken, state: State, name: RunnerName = None
) -> None:
    conn = connected(state, principal, name)
    mine: dict[str, bool] = {}
    keep = []
    for d in body.deltas:
        if d.run_id not in mine:
            meta = await state.db.run_meta(d.run_id)
            job = await state.db.job(d.run_id)
            owner = (job.runner if job is not None else None) or (meta.runner if meta else None)
            imported = meta is not None and meta.source == "import"
            mine[d.run_id] = not imported and owner in (None, conn.name)
        if mine[d.run_id]:
            keep.append(d)
    state.publish(keep)


@router.get(
    "/runs/{run_id}/log",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def run_log(
    run_id: str, principal: RunnerToken, state: State, name: RunnerName = None
) -> Response:
    """The exact log of a run, to fork it on another runner."""
    conn = connected(state, principal, name)
    await reader(state, run_id, conn.name)
    head = await state.store.head(run_id)
    if head is None:
        raise ProblemException(404, "Not found", f"no run {run_id}")
    path = state.store.root / run_id / "events.jsonl"
    data = await asyncio.to_thread(_read_upto, path, head.offset)
    return Response(data, media_type="application/x-ndjson")


def _read_upto(path: Path, size: int) -> bytes:
    with path.open("rb") as f:
        return f.read(size)


@router.get(
    "/runs/{run_id}/blobs/{sha256}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def run_blob(
    run_id: str, sha256: str, principal: RunnerToken, state: State, name: RunnerName = None
) -> FileResponse:
    conn = connected(state, principal, name)
    await reader(state, run_id, conn.name)
    return _stored(state, run_id, sha256)


def send(state: ServerState, runner: str, message: CommandMessage | OfferMessage) -> bool:
    """Put a message on a runner's stream. False if it has none open."""
    conn = state.runners.get(runner)
    if conn is None or conn.streams <= 0:
        return False
    try:
        conn.feed.put_nowait(message)
    except asyncio.QueueFull:
        return False
    return True
