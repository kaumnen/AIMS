import asyncio
import email.utils
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI, AsyncStream

from aims.aio import settle
from aims.events import (
    BlobRef,
    Content,
    CostSource,
    Draft,
    EventStore,
    ModelError,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStatus,
    ModelUsage,
    RateLimit,
    ToolCall,
)
from aims.models.backends import BACKENDS, Accumulator, Assembled, Backend, Piece, params
from aims.models.bus import Delta, DeltaBus
from aims.models.limiter import Limiters
from aims.models.profiles import ModelsConfig, Overrides, Price, Profile, ProfileError
from aims.models.replay import Pace, Recording, Replayer, ReplayError, ReplayWall, canonical_key
from aims.models.replay import chain as cause_chain
from aims.models.transport import Exchange, Recorder, current, encode_chunks
from aims.models.types import CHARS_PER_TOKEN, NO_ROOM, Prompt, Reply, SpendCap
from aims.net import Outbound
from aims.text import scrub

log = logging.getLogger("aims.models")

REPLAY_URL = "http://replay.invalid/v1"
CACHE_KEY = "prompt_cache_key"
_RETRY_STATUS = frozenset({408, 409, 425, 429})


class ModelCallError(Exception):
    """Every attempt of a model call failed. `reply` describes the last one, with the cost of
    them all. `no_room` says the call was not tried again since its spend cap had no room."""

    def __init__(self, reply: Reply, *, no_room: bool = False) -> None:
        super().__init__(
            f"model call {reply.call_id} on {reply.profile} failed after {reply.attempts}"
            f" attempt(s): {reply.error}"
        )
        self.reply = reply
        self.no_room = no_room


@dataclass(slots=True)
class _Outcome:
    exchange: Exchange
    assembled: Assembled
    status: ModelStatus
    latency_ms: int
    ttft_ms: int | None = None
    error: str | None = None
    error_kind: ModelError | None = None
    retryable: bool = False
    reraise: BaseException | None = None
    ratelimit: RateLimit = field(default_factory=RateLimit)
    code: int | None = None


def _dump(body: Mapping[str, Any]) -> bytes:
    """Keys go out in the order the body was built: a model writes a structured answer in its
    schema's order, and that order is part of the prompt."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


class ModelClient:
    """Calls models for one run and records every attempt in its event log.

    Each attempt writes `model.request` before the request is sent and `model.response` after,
    however it ends, then one `tool.call` per function call of a completed answer. With a
    `replay` recording, answers come from that run instead of the provider; a request it has no
    answer for fails the call. It logs a warning the first time a profile's answer carries
    another reasoning kind than the profile says, unless `warn_reasoning` is false.
    """

    def __init__(
        self,
        store: EventStore,
        run_id: str,
        config: ModelsConfig,
        *,
        bus: DeltaBus | None = None,
        limiters: Limiters | None = None,
        replay: Recording | None = None,
        pace: Pace = "instant",
        env: Mapping[str, str] | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        warn_reasoning: bool = True,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.config = config
        self.bus = bus
        self.limiters = limiters or Limiters()
        self.replay = replay
        self.pace = pace
        self.env = os.environ if env is None else env
        self._transport = transport
        self._clients: dict[tuple[Any, ...], AsyncOpenAI] = {}
        self._warned: set[str] = set()
        self._warn_reasoning = warn_reasoning

    async def __aenter__(self) -> ModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.close()

    async def call(
        self,
        profile: str | Profile,
        prompt: Prompt,
        *,
        agent_id: str | None = None,
        overrides: Overrides | None = None,
        role: ModelRole | None = None,
        recorded: Callable[[str], None] | None = None,
        cap: SpendCap | None = None,
        cut: Callable[[Reply], None] | None = None,
    ) -> Reply:
        """One model call, retried per the profile. The reply's cost sums every attempt, since a
        failed one may have been billed. Raises ModelCallError when every attempt failed, and
        ReplayError when a replay has no answer for the request. `role` marks a call the
        monitor makes. `recorded` gets the call's id once its first `model.request` is in the
        log, so a caller knows the id of a call that is then cancelled or raises. A live call
        under a `cap` is tried again only when the cap says another attempt fits. A replay
        stops where the recorded call did. `cut` gets a reply when the call is cancelled, or
        raises other than ModelCallError, after an attempt was logged: what its attempts cost,
        the cut one as unseen under a `cap`. The caller charges it as it charges a reply."""
        logged: list[Reply] = []
        try:
            return await self._tries(
                profile,
                prompt,
                agent_id=agent_id,
                overrides=overrides,
                role=role,
                recorded=recorded,
                cap=cap,
                logged=logged,
            )
        except ModelCallError:
            raise
        except BaseException:
            if cut is not None and logged:
                cut(logged[-1])
            raise

    async def _tries(
        self,
        profile: str | Profile,
        prompt: Prompt,
        *,
        agent_id: str | None,
        overrides: Overrides | None,
        role: ModelRole | None,
        recorded: Callable[[str], None] | None,
        cap: SpendCap | None,
        logged: list[Reply],
    ) -> Reply:
        """The attempts of `call`. Each attempt's reply goes on `logged` once it is logged."""
        if isinstance(profile, str):
            profile = self.config.get(profile)
        if prompt.output_schema is not None and not profile.structured_output:
            raise ProfileError(f"profile {profile.name} cannot answer to a schema")
        profile = profile.apply(overrides)
        backend = BACKENDS[profile.backend]
        made = backend.body(profile, prompt)
        content = _dump(made)
        body = await self.store.put_blob(self.run_id, content)
        key = canonical_key(content)
        cache_key = self._cache_key(profile, prompt, agent_id, role)
        wire = content if cache_key is None else _dump(made | {CACHE_KEY: cache_key})
        url = self._url(profile)
        client = self._client(profile, url)
        group = None if self.replay else self.limiters.group(profile.group, profile.limits)
        account = None if group is None else self.limiters.account(url, profile.api_key_env)
        estimate = len(content) // 4 + (profile.max_output_tokens or 0)
        call_id = str(uuid.uuid7())
        request_params = params(profile, prompt)
        retries = profile.retries
        spent: float | None = None
        billed = ModelUsage()
        unseen = 0.0
        attempt = counted = 0
        limited_since: float | None = None
        while True:
            attempt += 1
            lease = None if group is None else await group.acquire(estimate, account)
            outcome: _Outcome | None = None
            try:
                request = ModelRequest(
                    call_id=call_id,
                    attempt=attempt,
                    profile=profile.name,
                    backend=profile.backend,
                    model=profile.model,
                    url=url + backend.path,
                    params=request_params,
                    body=body,
                    key=key,
                    queued_ms=0 if lease is None else lease.queued_ms,
                    role=role,
                    cache_key=cache_key,
                )
                await self.store.append(self.run_id, [Draft(request, agent_id)])
                if attempt == 1 and recorded is not None:
                    recorded(call_id)
                outcome = await self._attempt(
                    client, backend, profile, wire, call_id, attempt, agent_id
                )
            finally:
                if group is not None and lease is not None:
                    used = None
                    if outcome is not None:
                        u = outcome.assembled.usage
                        if u.input_tokens is not None or u.output_tokens is not None:
                            used = (u.input_tokens or 0) + (u.output_tokens or 0)
                        group.observe(outcome.ratelimit)
                    lease.release(used)
            assert outcome is not None
            wait = self._wait(profile, attempt, outcome.ratelimit)
            limited = outcome.code == 429
            if limited and limited_since is None:
                limited_since = time.monotonic()
            # A rate limit lifts when its window ends, so 429 retries are bounded by time. One
            # with no wait counts toward `attempts`, so a profile with no backoff cannot spin.
            free = (
                limited
                and limited_since is not None
                and wait > 0
                and time.monotonic() - limited_since + wait <= retries.rate_limited
            )
            counted += not free
            retry = outcome.retryable and counted < retries.attempts and outcome.reraise is None
            if outcome.exchange.retry is not None:
                # A replay retries where the recorded call did, whatever the rule says now.
                retry = outcome.exchange.retry and outcome.reraise is None
            cost = self._priced(profile, outcome.assembled)
            if cap is not None and self._unseen(outcome, cost[0]):
                unseen = round(unseen + cap.attempt_usd, 10)
            no_room = outcome.exchange.no_room
            if retry and cap is not None and self.replay is None:
                no_room = not cap.fits(round((_plus(spent, cost[0]) or 0.0) + unseen, 10))
                retry = retry and not no_room
            if no_room and not (outcome.error or "").endswith(NO_ROOM):
                outcome.error = f"{outcome.error}; {NO_ROOM}"
            sized = 0.0
            if outcome.status in ("completed", "incomplete"):
                sized = self._sized(profile, content, outcome.assembled, cost[0])
            reply = await settle(
                self._finish(
                    profile,
                    backend,
                    call_id,
                    attempt,
                    agent_id,
                    outcome,
                    retry,
                    spent,
                    billed,
                    cost,
                    unseen,
                    sized,
                    logged,
                )
            )
            spent, billed = reply.cost_usd, reply.billed
            asked = limited or outcome.ratelimit.retry_after_ms is not None
            if account is not None and outcome.status == "error" and asked:
                account.pause(wait)
            if outcome.reraise is not None:
                raise outcome.reraise
            if outcome.status in ("completed", "incomplete"):
                return reply
            if not retry:
                raise ModelCallError(reply, no_room=no_room)
            await self._sleep(wait)

    def _cache_key(
        self, profile: Profile, prompt: Prompt, agent_id: str | None, role: ModelRole | None
    ) -> str | None:
        """The `prompt_cache_key` of a call, None when it carries none. Calls of one run that
        share a prefix share a key: the same caller, the same model and the same answer
        schema. Without one, a provider with many backends answers a repeated prefix from
        whichever backend it lands on, mostly one that never saw it. A replay sends nothing."""
        on = profile.backend == "responses" if profile.cache_key is None else profile.cache_key
        if not on or self.replay is not None or CACHE_KEY in profile.extra:
            return None
        schema = "" if prompt.output_schema is None else prompt.output_schema.name
        who = "\0".join([self.run_id, role or "agent", agent_id or "", profile.model, schema])
        return "aims-" + hashlib.sha256(who.encode()).hexdigest()[:32]

    def _url(self, profile: Profile) -> str:
        try:
            return profile.url(self.env)
        except ProfileError:
            if self.replay is None:
                raise
            return REPLAY_URL

    def _client(self, profile: Profile, url: str) -> AsyncOpenAI:
        t = profile.timeouts
        key = (url, profile.api_key_env, t, tuple(profile.headers.items()))
        client = self._clients.get(key)
        if client is not None:
            return client
        if self.replay is not None:
            inner: httpx2.AsyncBaseTransport = Replayer(self.replay, self.pace)
            api_key = "replay"
        else:
            inner = self._transport or Outbound()
            api_key = profile.api_key(url, self.env) or "none"
        timeout = httpx2.Timeout(connect=t.connect, read=t.stall, write=t.stall, pool=t.wall)
        recorder = Recorder(inner, key=api_key, headers=profile.headers)
        http = httpx2.AsyncClient(transport=recorder, timeout=timeout, follow_redirects=False)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=url,
            max_retries=0,
            timeout=timeout,
            http_client=http,
            default_headers=profile.headers,
        )
        self._clients[key] = client
        return client

    async def _attempt(
        self,
        client: AsyncOpenAI,
        backend: Backend,
        profile: Profile,
        content: bytes,
        call_id: str,
        attempt: int,
        agent_id: str | None,
    ) -> _Outcome:
        exchange = Exchange()
        acc = backend.accumulator()
        token = current.set(exchange)
        start = time.monotonic()
        ttft: list[int] = []

        def publish(pieces: list[Piece]) -> None:
            for piece in pieces:
                if not ttft:
                    ttft.append(exchange.ms())
                if self.bus is not None:
                    self.bus.publish(
                        Delta(
                            self.run_id,
                            agent_id,
                            call_id,
                            attempt,
                            piece.kind,
                            piece.text,
                            piece.index,
                            exchange.ms(),
                        )
                    )

        self._delta(call_id, attempt, agent_id, "start", "")
        status: ModelStatus = "error"
        error: str | None = None
        kind: ModelError | None = None
        retryable = False
        reraise: BaseException | None = None
        code: int | None = None
        try:
            async with asyncio.timeout(profile.timeouts.wall):
                await self._send(client, backend, profile, content, acc, publish)
            assembled = acc.result()
            if assembled.status is None:
                error, kind, retryable = "the stream ended before the answer did", "protocol", True
            elif assembled.status == "error":
                code = assembled.error_code
                error = assembled.error or "provider error"
                if code is not None:
                    error = f"provider error {code}: {error}"
                kind, retryable = "provider", _retry_code(code)
            else:
                status = assembled.status
        except asyncio.CancelledError as e:
            status, error, reraise = "cancelled", "cancelled by the caller", e
        except TimeoutError:
            error, kind = f"no answer within the {profile.timeouts.wall:g} s wall timeout", "wall"
        except Exception as e:
            error, kind, retryable, reraise, code = _classify(e)
        finally:
            current.reset(token)
        assembled = acc.result()
        if (
            self._warn_reasoning
            and status in ("completed", "incomplete")
            and assembled.reasoning_kind not in ("none", profile.reasoning)
        ):
            self._warn(profile, assembled.reasoning_kind)
        outcome = _Outcome(
            exchange=exchange,
            assembled=assembled,
            status=status,
            latency_ms=int((time.monotonic() - start) * 1000),
            ttft_ms=ttft[0] if ttft and profile.stream else None,
            error=error,
            error_kind=kind,
            retryable=retryable,
            reraise=reraise,
            ratelimit=parse_ratelimit(exchange.headers),
            code=code,
        )
        self._delta(call_id, attempt, agent_id, "end", status)
        return outcome

    async def _send(
        self,
        client: AsyncOpenAI,
        backend: Backend,
        profile: Profile,
        content: bytes,
        acc: Accumulator,
        publish: Any,
    ) -> None:
        stall = profile.timeouts.stall
        if profile.stream:
            stream = await _within(
                stall,
                client.post(
                    backend.path,
                    cast_to=object,
                    content=content,
                    stream=True,
                    stream_cls=AsyncStream[object],
                ),
            )
            async with stream:
                events = aiter(stream)
                while True:
                    try:
                        event = await _within(stall, anext(events))
                    except StopAsyncIteration:
                        break
                    if isinstance(event, dict):
                        publish(acc.feed(scrub(event)))
        else:
            answer = await _within(
                stall, client.post(backend.path, cast_to=object, content=content)
            )
            if isinstance(answer, str):
                answer = json.loads(answer)
            if not isinstance(answer, dict):
                raise TypeError("the answer is not a JSON object")
            acc.finish(scrub(answer))
            result = acc.result()
            publish([Piece("reasoning", result.reasoning)] if result.reasoning else [])
            publish([Piece("text", result.text)] if result.text else [])

    async def _finish(
        self,
        profile: Profile,
        backend: Backend,
        call_id: str,
        attempt: int,
        agent_id: str | None,
        o: _Outcome,
        retry: bool,
        spent: float | None,
        billed: ModelUsage,
        priced: tuple[float | None, CostSource | None],
        unseen: float,
        sized: float,
        logged: list[Reply],
    ) -> Reply:
        """Log the attempt with its own `priced` cost; the reply adds `spent` and `billed`, what
        the attempts before it cost and the tokens they counted, and carries `unseen` and
        `sized`. The reply goes on `logged` once the attempt is in the log, here rather than in
        the caller, so a cancel that lands meanwhile does not drop what the attempt cost."""
        a = o.assembled
        ex = o.exchange
        datas: list[bytes] = []
        stream_ref: BlobRef | None = None
        body_ref: BlobRef | None = None
        if ex.status is not None:
            if profile.stream:
                datas.append(encode_chunks(ex.chunks))
            if profile.stream and ex.status < 300:
                if a.final is not None:
                    datas.append(json.dumps(a.final, ensure_ascii=False).encode())
            elif ex.chunks:
                datas.append(ex.body())
        refs = await self.store.put_blobs(self.run_id, datas) if datas else []
        if profile.stream and ex.status is not None:
            stream_ref, refs = refs[0], refs[1:]
        if refs:
            body_ref = refs[0]
        text = await self.store.content(self.run_id, a.text)
        reasoning: Content | None = None
        if a.reasoning:
            reasoning = await self.store.content(self.run_id, a.reasoning)
        cost, cost_source = priced
        response = ModelResponse(
            call_id=call_id,
            attempt=attempt,
            status=o.status,
            http_status=ex.status,
            provider_id=a.provider_id,
            finish_reason=a.finish_reason,
            latency_ms=o.latency_ms,
            ttft_ms=o.ttft_ms,
            usage=a.usage,
            cost_usd=cost,
            cost_source=cost_source,
            provider=a.provider,
            model=a.model,
            service_tier=a.service_tier,
            ratelimit=o.ratelimit,
            text=text,
            reasoning=reasoning,
            reasoning_kind=a.reasoning_kind,
            tool_calls=len(a.tool_calls) if o.status == "completed" else 0,
            body=body_ref,
            stream=stream_ref,
            headers=ex.headers,
            error=o.error,
            error_kind=o.error_kind,
            retry=retry,
            source="live" if self.replay is None else "replay",
            origin_run=None if ex.origin is None else ex.origin[0],
            origin_seq=None if ex.origin is None else ex.origin[1],
        )
        drafts = [Draft(response, agent_id)]
        if o.status == "completed":
            for c in a.tool_calls:
                arguments = await self.store.content(self.run_id, c.arguments)
                call = ToolCall(call_id=c.id, name=c.name, arguments=arguments, model_call=call_id)
                drafts.append(Draft(call, agent_id))
        await self.store.append(self.run_id, drafts)
        reply = Reply(
            call_id=call_id,
            profile=profile.name,
            status=o.status,
            attempts=attempt,
            turn=a.turn(backend.name),
            reasoning_kind=a.reasoning_kind,
            finish_reason=a.finish_reason,
            usage=a.usage,
            billed=_summed(billed, a.usage),
            cost_usd=_plus(spent, cost),
            unseen_usd=unseen,
            sized_usd=sized,
            error=o.error,
        )
        logged.append(reply)
        return reply

    def _priced(self, profile: Profile, a: Assembled) -> tuple[float | None, CostSource | None]:
        """What one attempt cost and where the figure came from. A replayed answer was paid for
        by the run it was recorded in. A live one costs what the provider reports, which a
        router knows per call and a price table does not."""
        if self.replay is not None:
            return 0.0, None
        if a.cost_usd is not None:
            return a.cost_usd, "provider"
        cost = _cost(profile.price, a.usage)
        if cost is None:
            return None, None
        return cost, "list" if profile.listed is not None else "price"

    def _sized(self, profile: Profile, content: bytes, a: Assembled, cost: float | None) -> float:
        """What a live answer of a priced profile that came with no cost and without token
        counts is taken to cost by its size, every input token uncached: the request it answered
        for a missing input count; for a missing output count its text, reasoning and tool
        calls, or its provider items when longer, as encrypted reasoning makes them, at most the
        output cap sent. 0 for an answer with a cost or with no price."""
        price, usage = profile.price, a.usage
        if cost is not None or price is None or self.replay is not None:
            return 0.0
        given, made = usage.input_tokens, usage.output_tokens
        if given is None or given < 0:
            given = len(content) // CHARS_PER_TOKEN
        if made is None or made < 0:
            size = len(a.text) + len(a.reasoning)
            size += sum(len(c.name) + len(c.arguments) for c in a.tool_calls)
            if a.native:
                size = max(
                    size, len(json.dumps(a.native, ensure_ascii=False, separators=(",", ":")))
                )
            made = -(-size // CHARS_PER_TOKEN)
            if profile.max_output_tokens is not None:
                made = min(made, profile.max_output_tokens)
        return round((given * price.input + made * price.output) / 1_000_000, 10)

    def _unseen(self, o: _Outcome, cost: float | None) -> bool:
        """Whether a failed or cancelled live attempt may have been billed with no cost reported.
        A provider may bill an answer it began, or the whole of it, when the answer does not
        arrive. One that could not connect or answered with an error status did not take the
        request."""
        if o.status not in ("error", "cancelled") or cost is not None or self.replay is not None:
            return False
        if o.error_kind == "connect":
            return False
        return o.exchange.status is None or o.exchange.status < 300

    def _wait(self, profile: Profile, attempt: int, limit: RateLimit) -> float:
        """Seconds before the attempt after `attempt`: the wait the provider asks for, else the
        backoff, never above `retries.max`."""
        r = profile.retries
        if limit.retry_after_ms is not None:
            return min(limit.retry_after_ms / 1000 * random.uniform(1.0, 1.1), r.max)
        cap = min(r.max, r.backoff * 2 ** min(attempt - 1, 32))
        return cap / 2 + random.uniform(0, cap / 2)

    async def _sleep(self, wait: float) -> None:
        if self.replay is not None and self.pace == "instant":
            return
        await asyncio.sleep(wait)

    def _delta(
        self, call_id: str, attempt: int, agent_id: str | None, kind: Any, text: str
    ) -> None:
        if self.bus is not None:
            self.bus.publish(Delta(self.run_id, agent_id, call_id, attempt, kind, text))

    def _warn(self, profile: Profile, got: str) -> None:
        if profile.name not in self._warned:
            self._warned.add(profile.name)
            log.warning(
                "profile %s says reasoning %s but the provider returned %s",
                profile.name,
                profile.reasoning,
                got,
            )


class _Stalled(Exception):
    pass


async def _within[T](seconds: float, step: Awaitable[T]) -> T:
    """Await `step`, raising _Stalled when it takes longer than `seconds`."""
    try:
        async with asyncio.timeout(seconds):
            return await step
    except TimeoutError:
        raise _Stalled(f"nothing arrived for {seconds:g} s") from None


# h11 puts a header it refuses in its error, and the Authorization header holds the key.
_REFUSED_HEADER = re.compile(r"Illegal header (?:value|name)")


def _told(e: BaseException) -> str:
    """The text of `e`, cut before a header the HTTP client refused."""
    text = str(e)
    if m := _REFUSED_HEADER.search(text):
        return f"{text[: m.end()]} (not shown)"
    return text


def _classify(
    exc: Exception,
) -> tuple[str, ModelError, bool, BaseException | None, int | None]:
    """The error text, kind, whether a retry may pass, what to raise and the HTTP status or
    the provider's error code."""
    for e in cause_chain(exc):
        if isinstance(e, ReplayWall):
            return str(e), "wall", False, None, None
        if isinstance(e, ReplayError):
            return str(e), "replay", False, e, None
    if isinstance(exc, _Stalled):
        return f"stalled: {exc}", "stall", True, None, None
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        kind: ModelError = "redirect" if 300 <= code < 400 else "http"
        return f"HTTP {code}: {scrub(exc.message)}", kind, _retry_code(code), None, code
    for e in cause_chain(exc):
        match e:
            case httpx2.ReadTimeout():
                return f"no bytes for the stall timeout: {_told(e)}", "stall", True, None, None
            case httpx2.TimeoutException():
                return f"{type(e).__name__}: {_told(e)}", "timeout", True, None, None
            case httpx2.ConnectError():
                return f"cannot connect: {_told(e)}", "connect", True, None, None
            case httpx2.ProtocolError():
                return f"{type(e).__name__}: {_told(e)}", "protocol", True, None, None
            case httpx2.TransportError():
                return f"{type(e).__name__}: {_told(e)}", "read", True, None, None
    if isinstance(exc, openai.APIError) and not isinstance(exc, openai.APIConnectionError):
        # An error chunk in a stream that began with HTTP 200; its body is the error object.
        body = exc.body
        code = body.get("code") if isinstance(body, dict) else None
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        label = "provider error" if code is None else f"provider error {code}"
        return f"{label}: {scrub(exc.message)}", "provider", _retry_code(code), None, code
    return f"{type(exc).__name__}: {_told(exc)}", "protocol", False, None, None


def _retry_code(code: int | None) -> bool:
    """Whether a failure with this HTTP status, or this code in an error a provider sent after
    HTTP 200, may pass on a retry. A 4xx says the request, the key or the account is at fault
    (400, 401, 402, 403, 404), except the few that ask to wait. No code says nothing, so it
    is retried."""
    return code is None or code in _RETRY_STATUS or not 300 <= code < 500


def _cost(price: Price | None, usage: ModelUsage) -> float | None:
    """What one attempt cost at `price`, never below 0. None without a price or token counts,
    or with a count below 0, which counts nothing. Cached tokens are at most the input."""
    given, made, cached = usage.input_tokens, usage.output_tokens, usage.cached_tokens or 0
    if price is None or given is None or made is None or min(given, made, cached) < 0:
        return None
    cached = min(cached, given)
    total = (given - cached) * price.input + cached * price.cached + made * price.output
    return round(total / 1_000_000, 10)


def _summed(a: ModelUsage, b: ModelUsage) -> ModelUsage:
    """The token counts of `a` and `b` together. A count neither of them has stays None."""

    def add(x: int | None, y: int | None) -> int | None:
        return x if y is None else y if x is None else x + y

    return ModelUsage(
        input_tokens=add(a.input_tokens, b.input_tokens),
        cached_tokens=add(a.cached_tokens, b.cached_tokens),
        output_tokens=add(a.output_tokens, b.output_tokens),
        reasoning_tokens=add(a.reasoning_tokens, b.reasoning_tokens),
    )


def _plus(spent: float | None, cost: float | None) -> float | None:
    """The cost of the attempts so far: the sum of the priced ones, None while none is."""
    if spent is None or cost is None:
        return cost if spent is None else spent
    return round(spent + cost, 10)


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def _duration_ms(value: str | None) -> int | None:
    """Parse `6m0s`, `1.5s`, `20ms` or plain seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return int(float(value) * 1000)
    except ValueError:
        pass
    parts = _DURATION.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    scale = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}
    return int(sum(float(n) * scale[u] for n, u in parts))


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    if (ms := headers.get("retry-after-ms")) is not None:
        try:
            return int(float(ms))
        except ValueError:
            pass
    value = headers.get("retry-after")
    if value is None:
        return None
    if (seconds := _duration_ms(value)) is not None:
        return seconds
    try:
        when = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    return max(0, int((when.timestamp() - time.time()) * 1000))


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except KeyError, ValueError:
        return None


def parse_ratelimit(headers: Mapping[str, str]) -> RateLimit:
    over = headers.get("x-ratelimit-over-limit")
    return RateLimit(
        limit_requests=_header_int(headers, "x-ratelimit-limit-requests"),
        remaining_requests=_header_int(headers, "x-ratelimit-remaining-requests"),
        limit_tokens=_header_int(headers, "x-ratelimit-limit-tokens"),
        remaining_tokens=_header_int(headers, "x-ratelimit-remaining-tokens"),
        reset_requests_ms=_duration_ms(headers.get("x-ratelimit-reset-requests")),
        reset_tokens_ms=_duration_ms(headers.get("x-ratelimit-reset-tokens")),
        retry_after_ms=_retry_after_ms(headers),
        over_limit=None if over is None else over.lower() in ("yes", "true", "1"),
    )
