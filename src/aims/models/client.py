import asyncio
import email.utils
import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
import rfc8785
from openai import AsyncOpenAI, AsyncStream

from aims.events import (
    BlobRef,
    Content,
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
from aims.models.replay import Pace, Recording, Replayer, ReplayError, ReplayWall
from aims.models.replay import chain as cause_chain
from aims.models.transport import Exchange, Recorder, current, encode_chunks
from aims.models.types import Prompt, Reply

log = logging.getLogger("aims.models")

REPLAY_URL = "http://replay.invalid/v1"
_RETRY_STATUS = frozenset({408, 409, 425, 429})


class ModelCallError(Exception):
    """Every attempt of a model call failed. `reply` describes the last one."""

    def __init__(self, reply: Reply) -> None:
        super().__init__(
            f"model call {reply.call_id} on {reply.profile} failed after {reply.attempts}"
            f" attempt(s): {reply.error}"
        )
        self.reply = reply


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


class ModelClient:
    """Calls models for one run and records every attempt in its event log.

    Each attempt writes `model.request` before the request is sent and `model.response` after,
    however it ends, then one `tool.call` per function call of a completed answer. With a
    `replay` recording, answers come from that run instead of the provider; a request it has no
    answer for fails the call.
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
    ) -> Reply:
        """One model call, retried per the profile. Raises ModelCallError when every attempt
        failed, and ReplayError when a replay has no answer for the request. `role` marks a call
        the monitor makes."""
        if isinstance(profile, str):
            profile = self.config.get(profile)
        profile = profile.apply(overrides)
        backend = BACKENDS[profile.backend]
        content = rfc8785.dumps(backend.body(profile, prompt))
        body = await self.store.put_blob(self.run_id, content)
        url = self._url(profile)
        client = self._client(profile, url)
        group = None if self.replay else self.limiters.group(profile.group, profile.limits)
        estimate = len(content) // 4 + (profile.max_output_tokens or 0)
        call_id = str(uuid.uuid7())
        request_params = params(profile, prompt)
        attempts = profile.retries.attempts
        for attempt in range(1, attempts + 1):
            lease = None if group is None else await group.acquire(estimate)
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
                    key=body.sha256,
                    queued_ms=0 if lease is None else lease.queued_ms,
                    role=role,
                )
                await self.store.append(self.run_id, [Draft(request, agent_id)])
                outcome = await self._attempt(
                    client, backend, profile, content, call_id, attempt, agent_id
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
            last = attempt == attempts
            retry = outcome.retryable and not last and outcome.reraise is None
            reply = await asyncio.shield(
                self._finish(profile, backend, call_id, attempt, agent_id, outcome, retry)
            )
            if outcome.reraise is not None:
                raise outcome.reraise
            if outcome.status in ("completed", "incomplete"):
                return reply
            if not retry:
                raise ModelCallError(reply)
            await self._backoff(profile, attempt, outcome.ratelimit)
        raise AssertionError("unreachable")

    def _url(self, profile: Profile) -> str:
        try:
            return profile.url(self.env)
        except ProfileError:
            if self.replay is None:
                raise
            return REPLAY_URL

    def _client(self, profile: Profile, url: str) -> AsyncOpenAI:
        t = profile.timeouts
        key = (url, profile.api_key_env, t)
        client = self._clients.get(key)
        if client is not None:
            return client
        if self.replay is not None:
            inner: httpx2.AsyncBaseTransport = Replayer(self.replay, self.pace)
            api_key = "replay"
        else:
            inner = self._transport or httpx2.AsyncHTTPTransport()
            api_key = profile.api_key(self.env) or "none"
        timeout = httpx2.Timeout(connect=t.connect, read=t.stall, write=t.stall, pool=t.wall)
        http = httpx2.AsyncClient(
            transport=Recorder(inner), timeout=timeout, follow_redirects=False
        )
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=url,
            max_retries=0,
            timeout=timeout,
            http_client=http,
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
        try:
            async with asyncio.timeout(profile.timeouts.wall):
                await self._send(client, backend, profile, content, acc, publish)
            assembled = acc.result()
            if assembled.status is None:
                error, kind, retryable = "the stream ended before the answer did", "protocol", True
            elif assembled.status == "error":
                error, kind, retryable = assembled.error or "provider error", "provider", True
            else:
                status = assembled.status
        except asyncio.CancelledError as e:
            status, error, reraise = "cancelled", "cancelled by the caller", e
        except TimeoutError:
            error, kind = f"no answer within the {profile.timeouts.wall:g} s wall timeout", "wall"
        except Exception as e:
            error, kind, retryable, reraise = _classify(e)
        finally:
            current.reset(token)
        assembled = acc.result()
        if status in ("completed", "incomplete") and assembled.reasoning_kind not in (
            "none",
            profile.reasoning,
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
                        publish(acc.feed(event))
        else:
            answer = await _within(
                stall, client.post(backend.path, cast_to=object, content=content)
            )
            if isinstance(answer, str):
                answer = json.loads(answer)
            if not isinstance(answer, dict):
                raise TypeError("the answer is not a JSON object")
            acc.finish(answer)
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
    ) -> Reply:
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
        # A replayed answer was paid for by the run it was recorded in.
        cost = 0.0 if self.replay is not None else _cost(profile.price, a.usage)
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
        return Reply(
            call_id=call_id,
            profile=profile.name,
            status=o.status,
            attempts=attempt,
            turn=a.turn(backend.name),
            reasoning_kind=a.reasoning_kind,
            finish_reason=a.finish_reason,
            usage=a.usage,
            cost_usd=cost,
            error=o.error,
        )

    async def _backoff(self, profile: Profile, attempt: int, limit: RateLimit) -> None:
        if self.replay is not None and self.pace == "instant":
            return
        if limit.retry_after_ms is not None:
            wait = limit.retry_after_ms / 1000 * random.uniform(1.0, 1.1)
        else:
            cap = min(profile.retries.max, profile.retries.backoff * 2 ** (attempt - 1))
            wait = cap / 2 + random.uniform(0, cap / 2)
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


def _classify(
    exc: Exception,
) -> tuple[str, ModelError, bool, BaseException | None]:
    for e in cause_chain(exc):
        if isinstance(e, ReplayWall):
            return str(e), "wall", False, None
        if isinstance(e, ReplayError):
            return str(e), "replay", False, e
    if isinstance(exc, _Stalled):
        return f"stalled: {exc}", "stall", True, None
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        kind: ModelError = "redirect" if 300 <= code < 400 else "http"
        retryable = code in _RETRY_STATUS or code >= 500
        return f"HTTP {code}: {exc.message}", kind, retryable, None
    for e in cause_chain(exc):
        match e:
            case httpx2.ReadTimeout():
                return f"no bytes for the stall timeout: {e}", "stall", True, None
            case httpx2.TimeoutException():
                return f"{type(e).__name__}: {e}", "timeout", True, None
            case httpx2.ConnectError():
                return f"cannot connect: {e}", "connect", True, None
            case httpx2.ProtocolError():
                return f"{type(e).__name__}: {e}", "protocol", True, None
            case httpx2.TransportError():
                return f"{type(e).__name__}: {e}", "read", True, None
    if isinstance(exc, openai.APIError) and not isinstance(exc, openai.APIConnectionError):
        return f"provider error: {exc.message}", "provider", True, None
    return f"{type(exc).__name__}: {exc}", "protocol", False, None


def _cost(price: Price | None, usage: ModelUsage) -> float | None:
    if price is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    cached = usage.cached_tokens or 0
    total = (
        (usage.input_tokens - cached) * price.input
        + cached * price.cached
        + usage.output_tokens * price.output
    )
    return round(total / 1_000_000, 10)


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
