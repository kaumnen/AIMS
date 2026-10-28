import asyncio
import hashlib
import itertools
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx2
import pytest
import rfc8785
from typer.testing import CliRunner

from aims.cli import app
from aims.events import (
    Event,
    LocalEventStore,
    ModelRequest,
    ModelResponse,
    RateLimit,
    RunStarted,
    ToolCall,
)
from aims.models import (
    DeltaBus,
    Limiters,
    Limits,
    Message,
    ModelCallError,
    ModelClient,
    ModelsConfig,
    OutputSchema,
    Overrides,
    Profile,
    ProfileError,
    Prompt,
    Recording,
    ReplayError,
    ReplayMiss,
    Retries,
    Roles,
    Sampling,
    Timeouts,
    ToolOutput,
    ToolRequest,
    ToolSpec,
    Turn,
    check_weights,
    first_difference,
    load_models,
    parse_ratelimit,
)
from aims.models.backends import Chat, Responses
from aims.models.client import _cost
from aims.models.transport import decode_chunks

pytestmark = pytest.mark.anyio

_groups = itertools.count()
WEATHER = ToolSpec(name="get_weather", parameters={"type": "object", "properties": {}})
PROMPT = Prompt(items=[Message(role="user", content="hi")])


def profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "test",
        "backend": "chat",
        "base_url": "http://model.invalid/v1",
        "model": "m",
        "weights": "w",
        "reasoning": "full",
        "limits": Limits(group=f"g{next(_groups)}"),
        "retries": Retries(attempts=3, backoff=0, max=0),
        "price": {"input": 1.0, "cached": 0.1, "output": 2.0},
    }
    return Profile.model_validate(base | kw)


def config(p: Profile) -> ModelsConfig:
    return ModelsConfig(profiles={p.name: p}, roles=Roles(agent=p.name, sweep=p.name, judge=p.name))


def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict[str, Any]:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "model": "m",
        "choices": [choice],
        **extra,
    }


USAGE = {
    "id": "c1",
    "choices": [],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 2},
    },
}
CHAT_EVENTS = [
    chunk({"role": "assistant"}),
    chunk({"reasoning_content": "think "}),
    chunk({"reasoning_content": "more"}),
    chunk({"content": "Hel"}),
    chunk({"content": "lo"}),
    chunk({"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "get_weather"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
    chunk({}, "tool_calls"),
    USAGE,
]


def sse(events: Iterable[dict[str, Any]], done: bool = True) -> list[bytes]:
    out = [f"data: {json.dumps(e)}\n\n".encode() for e in events]
    return [*out, b"data: [DONE]\n\n"] if done else out


class Body(httpx2.AsyncByteStream):
    """Body parts with pauses: a float is a sleep in seconds."""

    def __init__(self, parts: Iterable[bytes | float]) -> None:
        self.parts = iter(list(parts))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            if isinstance(part, bytes):
                yield part
            else:
                await asyncio.sleep(part)


def stream(parts: Iterable[bytes | float], status: int = 200, **headers: str) -> httpx2.Response:
    h = {
        "content-type": "text/event-stream",
        **{k.replace("_", "-"): v for k, v in headers.items()},
    }
    return httpx2.Response(status, headers=h, stream=Body(parts))


Handler = Callable[[httpx2.Request], Any]


def scripted(*answers: Callable[[], httpx2.Response] | Exception) -> tuple[Handler, list[bytes]]:
    sent: list[bytes] = []
    queue = list(answers)

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer()

    return handle, sent


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def new_run(store: LocalEventStore) -> str:
    return await store.create_run(RunStarted(scenario="models", oversight="observe"))


def client(store: LocalEventStore, run_id: str, p: Profile, handler: Handler, **kw: Any):
    return ModelClient(
        store, run_id, config(p), transport=httpx2.MockTransport(handler), env={}, **kw
    )


async def model_events(store: LocalEventStore, run_id: str) -> list[Event]:
    return await store.events(run_id, types=["model.request", "model.response", "tool.call"])


def payloads(events: list[Event]) -> list[Any]:
    return [e.payload() for e in events]


async def test_chat_stream_records_the_attempt(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(lambda: stream(sse(CHAT_EVENTS), x_ratelimit_over_limit="no"))
    bus = DeltaBus()
    sub = bus.subscribe(run_id)
    p = profile()
    async with client(store, run_id, p, handler, bus=bus) as mc:
        reply = await mc.call("test", Prompt(items=PROMPT.items, tools=[WEATHER]), agent_id="a1")
    assert reply.status == "completed"
    assert reply.text == "Hello"
    assert reply.turn.reasoning == "think more"
    assert reply.reasoning_kind == "full"
    assert reply.tool_calls == [
        ToolRequest(id="t1", name="get_weather", arguments='{"city":"Paris"}')
    ]
    assert reply.usage.cached_tokens == 4
    assert reply.cost_usd == pytest.approx((6 * 1.0 + 4 * 0.1 + 5 * 2.0) / 1e6)

    request, response, call = payloads(await model_events(store, run_id))
    assert isinstance(request, ModelRequest)
    assert isinstance(response, ModelResponse)
    assert isinstance(call, ToolCall)
    body = await store.get_blob(run_id, request.body.sha256)
    assert sent == [body]
    assert body == rfc8785.dumps(json.loads(body))
    assert request.key == hashlib.sha256(body).hexdigest()
    assert request.params.tools == ["get_weather"]
    assert request.params.parallel_tool_calls is False
    assert json.loads(body)["stream_options"] == {"include_usage": True}

    assert response.status == "completed"
    assert response.http_status == 200
    assert response.ratelimit.over_limit is False
    assert response.ttft_ms is not None
    assert response.tool_calls == 1
    assert response.stream is not None
    assert response.body is not None
    raw = decode_chunks(await store.get_blob(run_id, response.stream.sha256))
    assert b"".join(c for _, c in raw) == b"".join(sse(CHAT_EVENTS))
    final = json.loads(await store.get_blob(run_id, response.body.sha256))
    assert final["choices"][0]["message"]["reasoning_content"] == "think more"
    assert call.model_call == request.call_id
    assert call.call_id == "t1"

    sub.close()
    kinds = [d.kind async for d in sub]
    assert kinds[0] == "start"
    assert kinds[-1] == "end"
    assert {"text", "reasoning", "tool_call", "tool_args"} <= set(kinds)


async def test_chat_usage_on_every_chunk_keeps_the_last(store: LocalEventStore) -> None:
    """Baseten sends the running usage on every chunk, so the last one is the total."""

    def used(out: int, thought: int) -> dict[str, Any]:
        return {
            "prompt_tokens": 10,
            "completion_tokens": out,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": thought},
        }

    events = [
        chunk({"role": "assistant"}, usage=used(1, 1)),
        chunk({"reasoning_content": "sum"}, usage=used(3, 3)),
        chunk({"content": "42"}, usage=used(5, 3)),
        chunk({}, "stop", usage=used(6, 3)),
    ]
    run_id = await new_run(store)
    handler, _ = scripted(lambda: stream(sse(events)))
    async with client(store, run_id, profile(), handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.text == "42"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (10, 6)
    assert reply.usage.reasoning_tokens == 3


RESPONSE = {
    "id": "resp_1",
    "status": "completed",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "plan"}],
            "encrypted_content": "enc",
        },
        {
            "type": "message",
            "id": "m1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking."}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
        },
    ],
    "usage": {
        "input_tokens": 20,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 9,
        "output_tokens_details": {"reasoning_tokens": 3},
    },
}
RESPONSES_EVENTS = [
    {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
    {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
    {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "plan"},
    {"type": "response.output_item.done", "item": RESPONSE["output"][0]},
    {"type": "response.output_text.delta", "item_id": "m1", "delta": "Checking."},
    {"type": "response.output_item.done", "item": RESPONSE["output"][1]},
    {"type": "response.output_item.added", "item": {**RESPONSE["output"][2], "arguments": ""}},
    {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{}"},
    {"type": "response.output_item.done", "item": RESPONSE["output"][2]},
    {"type": "response.completed", "response": RESPONSE},
]


async def test_responses_stream_keeps_native_items(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(lambda: stream(sse(RESPONSES_EVENTS)))
    p = profile(backend="responses", reasoning="summary", effort="low", summary="auto")
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", Prompt(items=PROMPT.items, tools=[WEATHER]))
    assert reply.text == "Checking."
    assert reply.turn.reasoning == "plan"
    assert reply.reasoning_kind == "summary"
    assert reply.turn.native == RESPONSE["output"]
    assert reply.turn.backend == "responses"
    assert reply.finish_reason == "tool_calls"
    assert reply.usage.reasoning_tokens == 3
    body = json.loads(sent[0])
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert body["tools"][0]["type"] == "function"
    response = payloads(await model_events(store, run_id))[1]
    assert response.provider_id == "resp_1"
    assert json.loads(await store.get_blob(run_id, response.body.sha256)) == RESPONSE


async def test_rate_limit_is_retried_after_the_wait(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()
    handler, sent = scripted(
        lambda: stream([error], 429, retry_after="0.05", content_type="application/json"),
        lambda: stream(sse(CHAT_EVENTS)),
    )
    async with client(store, run_id, profile(), handler) as mc:
        start = time.monotonic()
        reply = await mc.call("test", PROMPT)
    assert time.monotonic() - start >= 0.05
    assert reply.attempts == 2
    assert sent[0] == sent[1]
    events = payloads(await model_events(store, run_id))
    assert [type(e).__name__ for e in events] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
        "ToolCall",
    ]
    first = events[1]
    assert (first.status, first.http_status, first.error_kind, first.retry) == (
        "error",
        429,
        "http",
        True,
    )
    assert first.ratelimit.retry_after_ms == 50
    assert await store.get_blob(run_id, first.body.sha256) == error
    assert events[2].attempt == 2
    assert events[3].status == "completed"


async def test_client_errors_are_not_retried(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(lambda: stream([b'{"error": {"message": "bad"}}'], 400))
    async with client(store, run_id, profile(), handler) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert info.value.reply.attempts == 1
    assert len(sent) == 1
    response = payloads(await model_events(store, run_id))[1]
    assert (response.status, response.http_status, response.retry) == ("error", 400, False)


async def test_connect_errors_are_retried(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(httpx2.ConnectError("refused"), lambda: stream(sse(CHAT_EVENTS)))
    async with client(store, run_id, profile(), handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.attempts == 2
    first = payloads(await model_events(store, run_id))[1]
    assert (first.http_status, first.error_kind, first.retry) == (None, "connect", True)
    assert first.stream is None


async def test_a_stalled_stream_keeps_its_partial_answer(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    head = sse(CHAT_EVENTS[:4], done=False)
    handler, _ = scripted(lambda: stream([*head, 1.0]), lambda: stream(sse(CHAT_EVENTS)))
    p = profile(timeouts=Timeouts(stall=0.2))
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.attempts == 2
    stalled = payloads(await model_events(store, run_id))[1]
    assert (stalled.status, stalled.error_kind, stalled.retry) == ("error", "stall", True)
    assert stalled.text == "Hel"
    assert stalled.reasoning == "think more"
    assert stalled.body is None
    raw = decode_chunks(await store.get_blob(run_id, stalled.stream.sha256))
    assert b"".join(c for _, c in raw) == b"".join(head)


async def test_the_wall_timeout_is_not_retried(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    slow = [part for e in CHAT_EVENTS[:5] for part in (*sse([e], done=False), 0.1)]
    handler, sent = scripted(lambda: stream(slow * 20))
    p = profile(timeouts=Timeouts(wall=0.3))
    async with client(store, run_id, p, handler) as mc:
        with pytest.raises(ModelCallError):
            await mc.call("test", PROMPT)
    assert len(sent) == 1
    response = payloads(await model_events(store, run_id))[1]
    assert (response.status, response.error_kind) == ("error", "wall")


async def test_a_cancelled_call_records_what_arrived(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    head = sse(CHAT_EVENTS[:4], done=False)
    handler, _ = scripted(lambda: stream([*head, 10.0]))
    bus = DeltaBus()
    sub = bus.subscribe(run_id)
    async with client(store, run_id, profile(), handler, bus=bus) as mc:
        task = asyncio.create_task(mc.call("test", PROMPT))
        async for delta in sub:
            if delta.kind == "text":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    response = payloads(await model_events(store, run_id))[1]
    assert (response.status, response.text, response.error_kind) == ("cancelled", "Hel", None)
    assert response.stream is not None


async def test_unstreamed_answers(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    answer = {
        "id": "c2",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok", "reasoning_content": "r"},
                "finish_reason": "length",
            }
        ],
        "usage": USAGE["usage"],
    }
    raw = json.dumps(answer).encode()
    handler, sent = scripted(lambda: stream([raw], content_type="application/json"))
    async with client(store, run_id, profile(stream=False), handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert (reply.status, reply.text, reply.finish_reason) == ("incomplete", "ok", "length")
    assert "stream_options" not in json.loads(sent[0])
    response = payloads(await model_events(store, run_id))[1]
    assert response.stream is None
    assert await store.get_blob(run_id, response.body.sha256) == raw


async def test_replay_serves_recorded_answers_in_order(store: LocalEventStore) -> None:
    source = await new_run(store)
    error = b'{"error": {"message": "busy"}}'
    handler, _ = scripted(
        lambda: stream([error], 503),
        lambda: stream(sse(CHAT_EVENTS)),
        lambda: stream(sse(CHAT_EVENTS[3:])),
    )
    p = profile()
    tools = Prompt(items=PROMPT.items, tools=[WEATHER])
    async with client(store, source, p, handler) as mc:
        first = await mc.call("test", tools)
        second = await mc.call("test", PROMPT)

    recording = await Recording.load(store, source)
    assert recording.remaining() == 3
    run_id = await new_run(store)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("replay must not reach the provider")

    async with client(store, run_id, p, refuse, replay=recording) as mc:
        again = await mc.call("test", tools)
        assert again.turn == first.turn
        assert again.attempts == 2
        assert (await mc.call("test", PROMPT)).turn == second.turn
        with pytest.raises(ReplayMiss, match="recorded 1 times"):
            await mc.call("test", PROMPT)
        changed = Prompt(items=[Message(role="user", content="other")])
        with pytest.raises(ReplayMiss) as info:
            await mc.call("test", changed)
    assert info.value.path == "$.messages[0].content"
    responses = await store.events(run_id, types=["model.response"])
    sources = [(e.data["source"], e.data["status"], e.data["error_kind"]) for e in responses]
    assert sources == [
        ("replay", "error", "http"),
        ("replay", "completed", None),
        ("replay", "completed", None),
        ("replay", "error", "replay"),
        ("replay", "error", "replay"),
    ]
    origin = await store.events(source, types=["model.response"])
    assert [e.data["origin_seq"] for e in responses[:3]] == [e.seq for e in origin]


async def test_replay_repeats_a_stall(store: LocalEventStore) -> None:
    source = await new_run(store)
    head = sse(CHAT_EVENTS[:4], done=False)
    handler, _ = scripted(lambda: stream([*head, 1.0]), lambda: stream(sse(CHAT_EVENTS)))
    p = profile(timeouts=Timeouts(stall=0.2))
    async with client(store, source, p, handler) as mc:
        await mc.call("test", PROMPT)
    run_id = await new_run(store)
    recording = await Recording.load(store, source)
    async with client(store, run_id, p, handler, replay=recording) as mc:
        start = time.monotonic()
        reply = await mc.call("test", PROMPT)
    assert time.monotonic() - start < 0.2
    assert reply.attempts == 2
    first = payloads(await store.events(run_id, types=["model.response"]))[0]
    assert (first.error_kind, first.text) == ("stall", "Hel")


async def test_replay_refuses_a_cancelled_attempt(store: LocalEventStore) -> None:
    source = await new_run(store)
    handler, _ = scripted(lambda: stream([*sse(CHAT_EVENTS[:4], done=False), 10.0]))
    bus = DeltaBus()
    sub = bus.subscribe(source)
    async with client(store, source, profile(), handler, bus=bus) as mc:
        task = asyncio.create_task(mc.call("test", PROMPT))
        async for delta in sub:
            if delta.kind == "text":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run_id = await new_run(store)
    recording = await Recording.load(store, source)
    async with client(store, run_id, profile(), handler, replay=recording) as mc:
        with pytest.raises(ReplayError, match="cancelled by its caller"):
            await mc.call("test", PROMPT)


def test_first_difference() -> None:
    assert first_difference({"a": [1, 2]}, {"a": [1, 2]}) is None
    assert first_difference({"a": [1, 2]}, {"a": [1, 3]}) == "$.a[1]"
    assert first_difference({"a": [1]}, {"a": [1, 2]}) == "$.a[1]"
    assert first_difference({"a": 1}, {"b": 1}) == "$.a"
    assert first_difference({"a": 1}, {"a": 1.0}) == "$.a"


def test_rate_limit_headers() -> None:
    limit = parse_ratelimit(
        {
            "x-ratelimit-limit-requests": "150",
            "x-ratelimit-remaining-requests": "149",
            "x-ratelimit-remaining-tokens": "2999000",
            "x-ratelimit-reset-requests": "6m0s",
            "x-ratelimit-reset-tokens": "20ms",
            "retry-after": "2",
            "x-ratelimit-over-limit": "yes",
        }
    )
    assert limit == RateLimit(
        limit_requests=150,
        remaining_requests=149,
        remaining_tokens=2999000,
        reset_requests_ms=360000,
        reset_tokens_ms=20,
        retry_after_ms=2000,
        over_limit=True,
    )
    assert parse_ratelimit({"retry-after-ms": "150", "retry-after": "9"}).retry_after_ms == 150
    assert parse_ratelimit({"x-ratelimit-reset-requests": "soon"}) == RateLimit()
    later = parse_ratelimit({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert later.retry_after_ms is not None
    assert later.retry_after_ms > 0


def test_cost() -> None:
    p = profile()
    from aims.events import ModelUsage

    assert _cost(p.price, ModelUsage(input_tokens=1000, cached_tokens=400, output_tokens=10)) == (
        pytest.approx((600 * 1.0 + 400 * 0.1 + 10 * 2.0) / 1e6)
    )
    assert _cost(None, ModelUsage(input_tokens=1, output_tokens=1)) is None
    assert _cost(p.price, ModelUsage()) is None


async def test_limiter_waits_for_the_headers_and_slots() -> None:
    group = Limiters().group("g", Limits(rpm=600, concurrency=1))
    lease = await group.acquire(0)
    group.observe(RateLimit(remaining_requests=0))
    waiter = asyncio.create_task(group.acquire(0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    lease.release()
    start = time.monotonic()
    second = await waiter
    assert time.monotonic() - start >= 0.04
    assert second.queued_ms >= 50
    second.release()
    group.observe(RateLimit(retry_after_ms=100))
    start = time.monotonic()
    (await group.acquire(0)).release()
    assert time.monotonic() - start >= 0.09


async def test_the_bus_drops_for_slow_readers() -> None:
    from aims.models import Delta

    bus = DeltaBus(maxsize=2)
    mine = bus.subscribe("r1")
    other = bus.subscribe("r2")
    for i in range(3):
        bus.publish(Delta("r1", None, "c", 1, "text", str(i)))
    assert mine.dropped == 1
    assert other.queue.empty()
    mine.close()
    assert [d.text async for d in mine] == ["0", "1"]
    bus.publish(Delta("r1", None, "c", 1, "text", "late"))


def test_builtin_profiles() -> None:
    models = load_models()
    agent = models.role("agent")
    assert agent.name == "meta-spark-1.3-contributor"
    assert agent.group == "meta-contributor"
    assert models.role("judge").weights != agent.weights
    for p in models.profiles.values():
        assert p.effort is not None or p.backend == "chat"
    glimmer = models.get("modal-glimmer")
    with pytest.raises(ProfileError, match="AIMS_GLIMMER_URL"):
        glimmer.url({})
    assert glimmer.url({"AIMS_GLIMMER_URL": "https://x.modal.run/v1/"}) == "https://x.modal.run/v1"
    with pytest.raises(ProfileError, match="unknown profile"):
        models.get("nope")


def test_aims_toml_layers_on_the_builtins(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text(
        """
[profiles."meta-spark-1.3-contributor".limits]
rpm = 100

[profiles.local]
extends = "fireworks-spark-open"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_KEY"

[profiles.glimmer]
extends = "modal-glimmer"
base_url = "http://gpu:8000/v1"

[roles]
agent = "local"
"""
    )
    models = load_models(path)
    contributor = models.get("meta-spark-1.3-contributor")
    assert contributor.limits.rpm == 100
    assert contributor.limits.tpm == 3000000
    local = models.get("local")
    assert (local.backend, local.model, local.api_key_env) == (
        "chat",
        "accounts/fireworks/models/muse-spark-open",
        "LOCAL_KEY",
    )
    assert models.get("glimmer").base_url_env is None
    assert models.role("agent").name == "local"
    assert models.snapshot(["local"]) == {"local": local.model_dump(mode="json")}

    for bad, match in [
        ('[profiles.x]\nextends = "meta-spark-1.3"\ncolour = "red"\n', "colour"),
        ('[profiles.x]\nextends = "missing"\n', "unknown profile"),
        ('[profiles.x]\nextends = "x"\n', "extends itself"),
        ('[roles]\njudge = "missing"\n', "unknown profile"),
        ('[profiles."meta-spark-1.3".sampling]\ntemperature = 9\n', "temperature"),
        ('[profiles.c]\nextends = "fireworks-spark-open"\nsummary = "auto"\n', "summary"),
        ("[profiles\n", "aims.toml"),
    ]:
        path.write_text(bad)
        with pytest.raises(ProfileError, match=match):
            load_models(path)


def test_weights_check() -> None:
    models = load_models()
    contributor = models.get("meta-spark-1.3-contributor")
    standard = models.get("meta-spark-1.3")
    with pytest.raises(ProfileError, match=r"meta/muse-spark-1\.3"):
        check_weights([contributor], [standard])
    check_weights([contributor], [standard], allow_same_model=True)
    check_weights([contributor], [models.get("meta-wonder-1.0")])


def test_overrides() -> None:
    p = profile(sampling=Sampling(temperature=1.0, top_k=64))
    o = p.apply(Overrides(effort="high", sampling=Sampling(top_p=0.9), max_output_tokens=10))
    assert (o.effort, o.max_output_tokens) == ("high", 10)
    assert o.sampling == Sampling(temperature=1.0, top_p=0.9, top_k=64)
    assert p.apply(None) is p


def test_request_bodies() -> None:
    p = profile(effort="low", max_output_tokens=50, sampling=Sampling(top_k=5), extra={"x": 1})
    schema = OutputSchema(name="v", json_schema={"type": "object"})
    chat = Chat().body(p, Prompt(items=PROMPT.items, output_schema=schema))
    assert chat["reasoning_effort"] == "low"
    assert chat["max_tokens"] == 50
    assert chat["top_k"] == 5
    assert chat["x"] == 1
    assert "tools" not in chat
    assert "parallel_tool_calls" not in chat
    assert chat["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "v", "schema": {"type": "object"}, "strict": True},
    }

    r = profile(backend="responses", reasoning="summary", effort="low", stream=False)
    body = Responses().body(r, Prompt(items=PROMPT.items, output_schema=schema))
    assert body["text"] == {
        "format": {"type": "json_schema", "name": "v", "schema": {"type": "object"}, "strict": True}
    }
    assert body["stream"] is False
    assert "max_output_tokens" not in body


def test_turns_render_per_backend() -> None:
    native = Turn(
        text="a",
        tool_calls=[ToolRequest(id="t", name="f", arguments="{}")],
        backend="responses",
        native=[{"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "e"}],
    )
    items = [
        Message(role="system", content="s"),
        Message(role="user", content="u"),
        native,
        Message(role="user", content="next"),
        ToolOutput(call_id="t", output="o"),
    ]
    responses = Responses().render(Prompt(items=items))
    assert responses[2] == native.native[0]
    assert responses[3] == {"role": "assistant", "content": "."}
    assert responses[-1] == {"type": "function_call_output", "call_id": "t", "output": "o"}
    chat = Chat().render(Prompt(items=items))
    assert chat[2] == {
        "role": "assistant",
        "content": "a",
        "tool_calls": [
            {"id": "t", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ],
    }
    assert chat[-1] == {"role": "tool", "tool_call_id": "t", "content": "o"}


def test_cli_models_list(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text('[roles]\nagent = "meta-spark-1.3"\n')
    result = CliRunner().invoke(app, ["models", "list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "meta-spark-1.3  responses  muse-spark-1.3" in result.output
    assert "[agent]" in result.output
    result = CliRunner().invoke(app, ["models", "check", "nope", "--config", str(path)])
    assert result.exit_code == 1
    assert "unknown profile" in result.output


async def test_replay_can_keep_the_recorded_pace(store: LocalEventStore) -> None:
    source = await new_run(store)
    parts = [part for e in CHAT_EVENTS for part in (*sse([e], done=False), 0.02)]
    handler, _ = scripted(lambda: stream([*parts, b"data: [DONE]\n\n"]))
    p = profile()
    async with client(store, source, p, handler) as mc:
        await mc.call("test", PROMPT)
    (recorded,) = payloads(await store.events(source, types=["model.response"]))
    for pace, fast in [("instant", True), ("recorded", False)]:
        run_id = await new_run(store)
        recording = await Recording.load(store, source)
        async with client(store, run_id, p, handler, replay=recording, pace=pace) as mc:
            start = time.monotonic()
            await mc.call("test", PROMPT)
        took = (time.monotonic() - start) * 1000
        assert (took < recorded.latency_ms / 2) is fast


async def test_replay_skips_the_rate_limiter(store: LocalEventStore) -> None:
    source = await new_run(store)
    answers = [lambda: stream(sse(CHAT_EVENTS), x_ratelimit_remaining_requests="0")] * 3
    handler, _ = scripted(*answers)
    async with client(store, source, profile(), handler) as mc:
        for _ in range(3):
            await mc.call("test", PROMPT)
    run_id = await new_run(store)
    recording = await Recording.load(store, source)
    slow = profile(limits=Limits(rpm=1, concurrency=1, group="replayed"))
    async with client(store, run_id, slow, handler, replay=recording) as mc:
        start = time.monotonic()
        for _ in range(3):
            await mc.call("test", PROMPT)
    assert time.monotonic() - start < 1
    requests = payloads(await store.events(run_id, types=["model.request"]))
    assert [r.queued_ms for r in requests] == [0, 0, 0]
