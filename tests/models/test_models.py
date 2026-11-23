import asyncio
import itertools
import json
import logging
import os
import signal
import time
import traceback
import tracemalloc
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from proxy import Server, environ, serving
from pydantic import ValidationError
from typer.testing import CliRunner

from aims.cli import EXIT_CANCELLED, app
from aims.events import (
    BlobRef,
    Draft,
    Event,
    LocalEventStore,
    ModelParams,
    ModelRequest,
    ModelResponse,
    RateLimit,
    RunFinished,
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
    Reply,
    Retries,
    Roles,
    Sampling,
    Timeouts,
    ToolOutput,
    ToolRequest,
    ToolSpec,
    Turn,
    check,
    check_weights,
    first_difference,
    load_models,
    parse_ratelimit,
)
from aims.models.backends import BACKENDS, Chat, Responses, _reported_cost, params
from aims.models.check import check_profile, record_check
from aims.models.client import _classify, _cost
from aims.models.replay import canonical_key
from aims.models.transport import decode_chunks
from aims.models.types import NO_ROOM, SpendCap
from aims.run.fork import _schedule
from aims.stored import STORED

pytestmark = pytest.mark.anyio

_groups = itertools.count()
WEATHER = ToolSpec(name="get_weather", parameters={"type": "object", "properties": {}})
PROMPT = Prompt(items=[Message(role="user", content="hi")])
PROFILES = Path(__file__).parents[1] / "profiles.toml"


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


@pytest.mark.parametrize("warn", [True, False])
async def test_another_reasoning_kind_is_warned_of_once_unless_turned_off(
    store: LocalEventStore, caplog: pytest.LogCaptureFixture, warn: bool
) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(lambda: stream(sse(CHAT_EVENTS)), lambda: stream(sse(CHAT_EVENTS)))
    with caplog.at_level(logging.WARNING, logger="aims.models"):
        async with client(
            store, run_id, profile(reasoning="none"), handler, warn_reasoning=warn
        ) as mc:
            for _ in range(2):
                assert (await mc.call("test", PROMPT)).reasoning_kind == "full"
    said = [r.getMessage() for r in caplog.records if "reasoning" in r.getMessage()]
    assert said == (["profile test says reasoning none but the provider returned full"] * warn)


@pytest.mark.parametrize("measure", [False, True])
async def test_a_check_that_measures_the_reasoning_does_not_warn_of_it(
    store: LocalEventStore, caplog: pytest.LogCaptureFixture, measure: bool
) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return stream(sse(CHAT_EVENTS))

    p = profile(reasoning="none")
    with caplog.at_level(logging.WARNING, logger="aims.models"):
        await record_check(
            store, config(p), [p], transport=httpx2.MockTransport(answer), measure=measure
        )
    said = [r.getMessage() for r in caplog.records if "provider returned" in r.getMessage()]
    assert len(said) == (not measure)


async def model_events(store: LocalEventStore, run_id: str) -> list[Event]:
    return await store.events(run_id, types=["model.request", "model.response", "tool.call"])


def payloads(events: list[Event]) -> list[Any]:
    return [e.payload() for e in events]


async def test_chat_stream_records_the_attempt(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(lambda: stream(sse(CHAT_EVENTS), x_ratelimit_over_limit="no"))
    bus = DeltaBus()
    sub = bus.subscribe(run_id)
    p = profile(parallel_tool_calls=False)
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
    assert request.key == canonical_key(body)
    built = Chat().body(p, Prompt(items=PROMPT.items, tools=[WEATHER]))
    assert body == json.dumps(built, ensure_ascii=False, separators=(",", ":")).encode()
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
    """A server that sends the running usage on every chunk: the last one is the total."""

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


async def test_a_responses_call_carries_a_cache_key_the_recorded_body_leaves_out(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    other = await new_run(store)
    answers = [lambda: stream(sse(RESPONSES_EVENTS))] * 6
    handler, sent = scripted(*answers)
    p = profile(backend="responses", reasoning="summary")
    async with client(store, run_id, p, handler) as mc:
        await mc.call("test", PROMPT, agent_id="a")
        await mc.call("test", PROMPT, agent_id="a")
        await mc.call("test", PROMPT, agent_id="b")
        await mc.call("test", PROMPT, role="sweep")
    async with client(store, other, p, handler) as mc:
        await mc.call("test", PROMPT, agent_id="a")
    keys = [json.loads(body).get("prompt_cache_key") for body in sent]
    assert all(isinstance(k, str) and k.startswith("aims-") and len(k) == 37 for k in keys)
    assert keys[0] == keys[1]
    assert len(set(keys)) == 4
    requests = [r for r in payloads(await model_events(store, run_id)) if r.type == "model.request"]
    assert [r.cache_key for r in requests] == keys[:4]
    recorded = json.loads(await store.get_blob(run_id, requests[0].body.sha256))
    assert "prompt_cache_key" not in recorded
    assert recorded | {"prompt_cache_key": keys[0]} == json.loads(sent[0])
    assert len({r.key for r in requests}) == 1

    async with client(store, run_id, p.model_copy(update={"cache_key": False}), handler) as mc:
        await mc.call("test", PROMPT)
    assert "prompt_cache_key" not in json.loads(sent[-1])


async def test_a_chat_call_carries_a_cache_key_only_when_its_profile_asks(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(*[lambda: stream(sse(CHAT_EVENTS))] * 3)
    async with client(store, run_id, profile(), handler) as mc:
        await mc.call("test", PROMPT)
    assert "prompt_cache_key" not in json.loads(sent[0])
    async with client(store, run_id, profile(cache_key=True), handler) as mc:
        await mc.call("test", PROMPT)
    assert json.loads(sent[1])["prompt_cache_key"].startswith("aims-")
    own = profile(cache_key=True, extra={"prompt_cache_key": "mine"})
    async with client(store, run_id, own, handler) as mc:
        await mc.call("test", PROMPT)
    assert json.loads(sent[2])["prompt_cache_key"] == "mine"
    requests = [r for r in payloads(await model_events(store, run_id)) if r.type == "model.request"]
    assert [r.cache_key is None for r in requests] == [True, False, True]


async def test_rate_limit_is_retried_after_the_wait(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()
    handler, sent = scripted(
        lambda: stream([error], 429, retry_after="0.05", content_type="application/json"),
        lambda: stream(sse(CHAT_EVENTS)),
    )
    p = profile(retries=Retries(attempts=3, backoff=0, max=1))
    async with client(store, run_id, p, handler) as mc:
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


FAILED = {
    "id": "resp_2",
    "status": "failed",
    "error": {"message": "overloaded"},
    "output": [],
    "usage": {"input_tokens": 20, "output_tokens": 7},
}


async def test_the_reply_costs_every_attempt_that_used_tokens(store: LocalEventStore) -> None:
    failed = [{"type": "response.failed", "response": FAILED}]
    stalled = [*sse(RESPONSES_EVENTS, done=False), 1.0]
    p = profile(backend="responses", reasoning="summary", timeouts=Timeouts(stall=0.2))
    run_id = await new_run(store)
    handler, _ = scripted(
        lambda: stream(sse(failed)),
        lambda: stream(stalled),
        lambda: stream(sse(RESPONSES_EVENTS)),
    )
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    events = payloads(await model_events(store, run_id))
    responses = [e for e in events if isinstance(e, ModelResponse)]
    assert [(r.status, r.error_kind) for r in responses] == [
        ("error", "provider"),
        ("error", "stall"),
        ("completed", None),
    ]
    costs = [r.cost_usd for r in responses]
    assert costs == pytest.approx([34e-6, 38e-6, 38e-6])
    assert reply.attempts == 3
    assert reply.cost_usd == pytest.approx(sum(costs))

    run_id = await new_run(store)
    handler, _ = scripted(*[lambda: stream(sse(failed))] * 3)
    async with client(store, run_id, p, handler) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert info.value.reply.cost_usd == pytest.approx(3 * 34e-6)

    run_id = await new_run(store)
    handler, _ = scripted(
        lambda: stream([b'{"error": {"message": "busy"}}'], 503),
        lambda: stream(sse(RESPONSES_EVENTS)),
    )
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.cost_usd == pytest.approx(38e-6)
    unpriced = profile(price=None)
    handler, _ = scripted(
        lambda: stream([b'{"error": {"message": "busy"}}'], 503),
        lambda: stream(sse(CHAT_EVENTS)),
    )
    async with client(store, run_id, unpriced, handler) as mc:
        assert (await mc.call(unpriced, PROMPT)).cost_usd is None


async def test_a_profile_without_structured_output_refuses_a_schema(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    handler, sent = scripted(lambda: stream(sse(CHAT_EVENTS)))
    p = profile(structured_output=False)
    schema = OutputSchema(name="v", json_schema={"type": "object"})
    async with client(store, run_id, profile(), handler) as mc:
        with pytest.raises(ProfileError, match="cannot answer to a schema"):
            await mc.call(p, Prompt(items=PROMPT.items, output_schema=schema))
    assert sent == []
    assert await model_events(store, run_id) == []


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


def failing(code: object, **more: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": "no credits", "metadata": {}}, **more}


@pytest.mark.parametrize("backend", ["chat", "responses"])
@pytest.mark.parametrize(
    ("code", "attempts"),
    [(400, 1), (401, 1), (402, 1), (403, 1), (404, 1), (429, 3), (502, 3), ("overloaded", 3)],
)
async def test_an_error_body_behind_http_200_is_a_provider_error(
    store: LocalEventStore, backend: str, code: object, attempts: int
) -> None:
    run_id = await new_run(store)
    raw = json.dumps(failing(code)).encode()
    handler, sent = scripted(*[lambda: stream([raw], content_type="application/json")] * attempts)
    p = profile(backend=backend, stream=False)
    async with client(store, run_id, p, handler) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert len(sent) == attempts
    label = f"provider error {code}: " if isinstance(code, int) else ""
    assert info.value.reply.error == label + "no credits"
    responses = [
        e for e in payloads(await model_events(store, run_id)) if e.type != "model.request"
    ]
    assert [(r.status, r.http_status, r.error_kind) for r in responses] == [
        ("error", 200, "provider")
    ] * attempts
    assert [r.retry for r in responses] == [True] * (attempts - 1) + [False]
    assert await store.get_blob(run_id, responses[0].body.sha256) == raw


@pytest.mark.parametrize(("code", "attempts"), [(402, 1), (403, 1), (502, 2), (None, 2)])
async def test_an_error_chunk_in_a_stream_is_retried_by_its_code(
    store: LocalEventStore, code: int | None, attempts: int
) -> None:
    run_id = await new_run(store)
    error = {"message": "cut off"} if code is None else {"code": code, "message": "cut off"}
    cut = [*CHAT_EVENTS[:4], chunk({"content": ""}, "error", error=error)]
    handler, sent = scripted(lambda: stream(sse(cut)), lambda: stream(sse(CHAT_EVENTS)))
    async with client(store, run_id, profile(), handler) as mc:
        if attempts == 1:
            with pytest.raises(ModelCallError):
                await mc.call("test", PROMPT)
        else:
            assert (await mc.call("test", PROMPT)).attempts == 2
    assert len(sent) == attempts
    first = payloads(await model_events(store, run_id))[1]
    label = "provider error" if code is None else f"provider error {code}"
    assert (first.status, first.error_kind, first.error) == (
        "error",
        "provider",
        f"{label}: cut off",
    )
    assert first.retry is (attempts == 2)
    assert (first.text, first.reasoning, first.tool_calls) == ("Hel", "think more", 0)


async def test_an_answer_that_finished_with_an_error_is_not_an_answer(
    store: LocalEventStore,
) -> None:
    """Without the top-level error the SDK lets the chunk through; the finish reason alone
    says the answer failed."""
    bodies = [
        {
            "id": "c1",
            "choices": [{"index": 0, "message": {"content": "Hel"}, "finish_reason": "error"}],
        },
        {
            "id": "c1",
            "choices": [
                {
                    "index": 0,
                    "message": {"content": "Hel"},
                    "finish_reason": "error",
                    "error": {"code": 403, "message": "flagged"},
                }
            ],
        },
    ]
    for body, error, attempts in zip(
        bodies,
        ["the answer ended with an error", "provider error 403: flagged"],
        [3, 1],
        strict=True,
    ):
        run_id = await new_run(store)
        raw = json.dumps(body).encode()
        handler, sent = scripted(
            *[lambda raw=raw: stream([raw], content_type="application/json")] * 3
        )
        async with client(store, run_id, profile(stream=False), handler) as mc:
            with pytest.raises(ModelCallError) as info:
                await mc.call("test", PROMPT)
        assert (info.value.reply.error, len(sent)) == (error, attempts)
    run_id = await new_run(store)
    cut = [*CHAT_EVENTS[:4], chunk({}, "error")]
    handler, sent = scripted(lambda: stream(sse(cut)), lambda: stream(sse(CHAT_EVENTS)))
    async with client(store, run_id, profile(), handler) as mc:
        assert (await mc.call("test", PROMPT)).attempts == 2
    first = payloads(await model_events(store, run_id))[1]
    assert (first.status, first.finish_reason, first.error_kind, first.retry) == (
        "error",
        "error",
        "provider",
        True,
    )


async def test_a_lone_surrogate_in_a_provider_error_is_logged_as_text(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(lambda: stream([b'{"error": {"message": "bad \\ud800"}}'], 400))
    async with client(store, run_id, profile(), handler) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert info.value.reply.error == "HTTP 400: Error code: 400 - bad \\ud800"
    response = payloads(await model_events(store, run_id))[1]
    assert response.error == info.value.reply.error


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


@pytest.mark.parametrize("fits", [True, False])
async def test_a_retry_under_a_spend_cap_runs_only_while_it_fits(
    store: LocalEventStore, fits: bool
) -> None:
    """A cut answer with no cost may have been billed: it counts at what the caller planned an
    attempt to cost, and the cap decides the retry."""
    run_id = await new_run(store)
    cut = [*CHAT_EVENTS[:4], chunk({"content": ""}, "error", error={"message": "cut off"})]
    handler, sent = scripted(lambda: stream(sse(cut)), lambda: stream(sse(CHAT_EVENTS)))
    owed: list[float] = []

    def check(value: float) -> bool:
        owed.append(value)
        return fits

    cap = SpendCap(attempt_usd=0.001, fits=check)
    async with client(store, run_id, profile(), handler) as mc:
        if fits:
            reply = await mc.call("test", PROMPT, cap=cap)
            assert reply.attempts == 2
        else:
            with pytest.raises(ModelCallError) as info:
                await mc.call("test", PROMPT, cap=cap)
            assert info.value.no_room
            reply = info.value.reply
            assert reply.error == f"provider error: cut off; {NO_ROOM}"
    assert (owed, reply.unseen_usd, len(sent)) == ([0.001], 0.001, 2 if fits else 1)
    first = payloads(await model_events(store, run_id))[1]
    assert (first.cost_usd, first.retry) == (None, fits)


@pytest.mark.parametrize(
    ("usage", "limit", "price", "sized"),
    [
        (False, None, True, True),
        (False, 5, True, True),
        (True, None, True, False),
        (False, None, False, False),
    ],
)
async def test_an_answer_with_no_token_counts_is_sized_against_caps_only(
    store: LocalEventStore, usage: bool, limit: int | None, price: bool, sized: bool
) -> None:
    """With no counts and no cost, a priced answer counts at its size: the request it
    answered, every token uncached, and its provider items, longer than its 42 characters of
    text, reasoning and tool call, at most the output cap. Its recorded cost stays empty."""
    run_id = await new_run(store)
    events = CHAT_EVENTS if usage else CHAT_EVENTS[:-1]
    handler, sent = scripted(lambda: stream(sse(events)))
    p = profile(max_output_tokens=limit) if price else profile(max_output_tokens=limit, price=None)
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    if not sized:
        assert reply.sized_usd == 0
        return
    assert reply.cost_usd is None
    [body] = sent
    items = json.dumps(reply.turn.native, ensure_ascii=False, separators=(",", ":"))
    assert len(items) > 42
    made = min(-(-len(items) // 3), limit or len(items))
    assert reply.sized_usd == pytest.approx((len(body) // 3 * 1.0 + made * 2.0) / 1e6)
    response = payloads(await model_events(store, run_id))[1]
    assert (response.status, response.cost_usd) == ("completed", None)


async def test_a_replayed_answer_with_no_token_counts_is_not_sized(
    store: LocalEventStore,
) -> None:
    source = await new_run(store)
    handler, _ = scripted(lambda: stream(sse(CHAT_EVENTS[:-1])))
    async with client(store, source, profile(), handler) as mc:
        assert (await mc.call("test", PROMPT)).sized_usd > 0
    recording = await Recording.load(store, source)
    run_id = await new_run(store)
    async with client(store, run_id, profile(), handler, replay=recording) as mc:
        assert (await mc.call("test", PROMPT)).sized_usd == 0


@pytest.mark.parametrize("failure", ["connect", "status"])
async def test_an_attempt_the_provider_did_not_take_owes_nothing(
    store: LocalEventStore, failure: str
) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "busy"}}).encode()
    first: Callable[[], httpx2.Response] | Exception = httpx2.ConnectError("refused")
    if failure == "status":
        first = lambda: stream([error], 503, content_type="application/json")  # noqa: E731
    handler, _ = scripted(first, lambda: stream(sse(CHAT_EVENTS)))
    owed: list[float] = []

    def check(value: float) -> bool:
        owed.append(value)
        return True

    async with client(store, run_id, profile(), handler) as mc:
        reply = await mc.call("test", PROMPT, cap=SpendCap(attempt_usd=0.001, fits=check))
    assert (reply.attempts, reply.unseen_usd, owed) == (2, 0.0, [0.0])


async def test_a_replay_stops_where_the_spend_cap_stopped_the_call(
    store: LocalEventStore,
) -> None:
    source = await new_run(store)
    cut = [*CHAT_EVENTS[:4], chunk({"content": ""}, "error", error={"message": "cut off"})]
    handler, _ = scripted(lambda: stream(sse(cut)))
    cap = SpendCap(attempt_usd=0.001, fits=lambda _: False)
    async with client(store, source, profile(), handler) as mc:
        with pytest.raises(ModelCallError):
            await mc.call("test", PROMPT, cap=cap)
    recording = await Recording.load(store, source)
    run_id = await new_run(store)
    async with client(store, run_id, profile(), handler, replay=recording) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert info.value.no_room
    assert info.value.reply.attempts == 1
    assert info.value.reply.error == f"provider error: cut off; {NO_ROOM}"


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


async def test_a_cancel_while_the_answer_is_logged_waits_for_the_log(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(lambda: stream(sse(CHAT_EVENTS)))
    entered, hold = asyncio.Event(), asyncio.Event()
    append = store.append

    async def slow(run: str, drafts: Any) -> Any:
        if any(isinstance(d.payload, ModelResponse) for d in drafts):
            entered.set()
            await hold.wait()
        return await append(run, drafts)

    monkeypatch.setattr(store, "append", slow)
    async with client(store, run_id, profile(), handler) as mc:
        task = asyncio.create_task(mc.call("test", PROMPT))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        hold.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [type(p).__name__ for p in payloads(await model_events(store, run_id))][:2] == [
        "ModelRequest",
        "ModelResponse",
    ]


async def test_lone_surrogates_in_an_answer_are_kept_as_text(store: LocalEventStore) -> None:
    events = [
        chunk({"role": "assistant"}),
        chunk({"reasoning_content": "think \ud800"}),
        chunk({"content": "Hel\udfff"}),
        chunk({"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "get\ud83d"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"\udc00"}'}}]}),
        chunk({}, "tool_calls"),
        USAGE,
    ]
    run_id = await new_run(store)
    message = {"role": "assistant", "content": "ok \ud800"}
    answer = {"id": "c2", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
    handler, _ = scripted(
        lambda: stream(sse(events)),
        lambda: stream([json.dumps(answer).encode()], content_type="application/json"),
    )
    async with client(store, run_id, profile(), handler) as mc:
        reply = await mc.call("test", Prompt(items=PROMPT.items, tools=[WEATHER]))
        unstreamed = await mc.call(profile(stream=False), PROMPT)
    assert (reply.text, reply.turn.reasoning) == ("Hel\\udfff", "think \\ud800")
    assert reply.tool_calls == [
        ToolRequest(id="t1", name="get\\ud83d", arguments='{"city":"\\udc00"}')
    ]
    assert unstreamed.text == "ok \\ud800"
    _, response, call, _, second = payloads(await model_events(store, run_id))
    assert (response.text, response.reasoning, call.name) == (
        "Hel\\udfff",
        "think \\ud800",
        "get\\ud83d",
    )
    assert second.text == "ok \\ud800"


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


async def test_model_calls_go_through_the_proxy_and_are_recorded(
    store: LocalEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await new_run(store)
    answer = {
        "id": "c3",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": USAGE["usage"],
    }
    raw = json.dumps(answer).encode()
    p = profile(stream=False, base_url="http://models.example/v1")
    async with serving(Server(body=raw)) as proxy:
        environ(monkeypatch, HTTP_PROXY=proxy.url)
        async with ModelClient(store, run_id, config(p), env={}) as mc:
            reply = await mc.call("test", PROMPT)
    assert (reply.status, reply.text) == ("completed", "ok")
    assert proxy.lines == ["POST http://models.example/v1/chat/completions HTTP/1.1"]
    response = payloads(await model_events(store, run_id))[1]
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


async def test_a_replay_miss_reads_a_recorded_body_no_further_than_it_states(
    store: LocalEventStore,
) -> None:
    source = await new_run(store)
    big = await store.put_blob(source, b'{"m":"' + b"a" * (64 * 1024 * 1024) + b'"}')
    request = ModelRequest(
        call_id="c",
        attempt=1,
        profile="p",
        backend="chat",
        model="m",
        url="u",
        params=ModelParams(stream=False),
        body=BlobRef(sha256=big.sha256, size=10),
        key="0" * 64,
    )
    small = await store.put_blob(source, b"{}")
    response = ModelResponse(
        call_id="c", attempt=1, status="completed", http_status=200, latency_ms=1, body=small
    )
    await store.append(source, [Draft(request, "a"), Draft(response, "a")])
    recording = await Recording.load(store, source)
    tracemalloc.start()
    try:
        with pytest.raises(ReplayMiss) as info:
            await recording.take(b'{"x":1}')
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert info.value.nearest is None
    assert peak < 8 * 1024 * 1024


@pytest.mark.timing
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


def test_a_cost_is_never_below_0() -> None:
    """A provider that says more tokens were cached than read, or a count below 0."""
    p = profile()
    from aims.events import ModelUsage

    assert _cost(p.price, ModelUsage(input_tokens=1000, cached_tokens=5000, output_tokens=0)) == (
        pytest.approx(1000 * 0.1 / 1e6)
    )
    for given, cached, made in ((-1000, 0, 10), (1000, -5, 10), (1000, 0, -10)):
        usage = ModelUsage(input_tokens=given, cached_tokens=cached, output_tokens=made)
        assert _cost(p.price, usage) is None


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
    limiters = Limiters()
    account = limiters.account("http://model.invalid/v1", None)
    account.pause(0.1)
    free = limiters.group("free", Limits(concurrency=1))
    start = time.monotonic()
    (await free.acquire(0)).release()
    assert time.monotonic() - start < 0.05
    (await free.acquire(0, account)).release()
    assert time.monotonic() - start >= 0.09


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_rpm_holds_any_60_s_to_its_count() -> None:
    from aims.models.limiter import _Window

    clock = Clock()
    window = _Window(3, clock)
    for at in (0, 10, 20):
        clock.t = 1000 + at
        assert window.wait() == 0
        window.take()
    clock.t = 1030
    assert window.wait() == pytest.approx(30)
    clock.t = 1059.9
    assert window.wait() == pytest.approx(0.1)
    clock.t = 1060
    assert window.wait() == 0
    window.take()
    assert window.wait() == pytest.approx(10)


def test_a_window_holds_what_the_provider_says_is_gone() -> None:
    from aims.models.limiter import _Window

    clock = Clock()
    window = _Window(5, clock)
    window.take()
    window.cap(0, 2000)
    assert window.wait() == pytest.approx(2)
    clock.t += 2
    assert window.wait() == 0
    later = _Window(5, Clock())
    later.cap(3, None)
    assert later.wait() == 0
    for _ in range(3):
        later.take()
    assert later.wait() == pytest.approx(12)
    late = _Window(5, Clock())
    late.cap(-1, None)
    assert late.wait() == pytest.approx(12)
    far = _Window(5, Clock())
    far.cap(0, 3_600_000)
    assert far.wait() == pytest.approx(60)


async def test_a_rate_limit_pauses_every_profile_on_the_account(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()
    arrived: list[float] = []

    def at(answer: Callable[[], httpx2.Response]) -> Callable[[], httpx2.Response]:
        def timed() -> httpx2.Response:
            arrived.append(time.monotonic())
            return answer()

        return timed

    handler, sent = scripted(
        at(lambda: stream([error], 429, retry_after="0.5", content_type="application/json")),
        at(lambda: stream(sse(CHAT_EVENTS))),
        at(lambda: stream(sse(CHAT_EVENTS))),
    )
    once = Retries(attempts=1, backoff=0, max=1, rate_limited=0)
    first = profile(name="first", api_key_env="KEY_A", retries=once)
    other = profile(name="other", api_key_env="KEY_B")
    same = profile(name="same", api_key_env="KEY_A")
    models = ModelsConfig(
        profiles={p.name: p for p in (first, other, same)},
        roles=Roles(agent="first", sweep="first", judge="first"),
    )
    keys = "KEY_A=http://model.invalid/v1,KEY_B=http://model.invalid/v1"
    env = {"KEY_A": "a", "KEY_B": "b", "AIMS_KEYS": keys}
    transport = httpx2.MockTransport(handler)
    limiters = Limiters()
    agent = ModelClient(store, run_id, models, transport=transport, env=env, limiters=limiters)
    monitor = ModelClient(store, run_id, models, transport=transport, env=env, limiters=limiters)
    async with agent, monitor:
        with pytest.raises(ModelCallError):
            await agent.call("first", PROMPT)
        await monitor.call("other", PROMPT)
        await monitor.call("same", PROMPT)
    requests = payloads(await store.events(run_id, types=["model.request"]))
    queued = {r.profile: r.queued_ms for r in requests}
    assert queued["first"] < 250
    assert queued["other"] < 250
    assert len(sent) == 3
    # The pause starts after the 429 arrives and lasts its Retry-After, so this holds on a slow
    # host too. What `same` waits in the queue is only what the pause has left.
    limited, _, same_sent = arrived
    assert same_sent - limited >= 0.5


async def test_rate_limit_retries_do_not_count_toward_attempts(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()
    limited = lambda: stream([error], 429, content_type="application/json")  # noqa: E731
    handler, sent = scripted(limited, limited, limited, lambda: stream(sse(CHAT_EVENTS)))
    p = profile(retries=Retries(attempts=1, backoff=0.01, max=0.02, rate_limited=10))
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert (reply.status, reply.attempts, len(sent)) == ("completed", 4, 4)
    responses = payloads(await store.events(run_id, types=["model.response"]))
    assert [r.retry for r in responses] == [True, True, True, False]


async def test_rate_limit_retries_count_once_their_time_is_spent(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()

    def limited() -> httpx2.Response:
        return stream([error], 429, retry_after="0.05", content_type="application/json")

    handler, sent = scripted(limited, limited, limited, limited)
    p = profile(retries=Retries(attempts=2, backoff=0, max=1, rate_limited=0.08))
    async with client(store, run_id, p, handler) as mc:
        with pytest.raises(ModelCallError) as info:
            await mc.call("test", PROMPT)
    assert info.value.reply.attempts == 3
    assert len(sent) == 3
    responses = payloads(await store.events(run_id, types=["model.response"]))
    assert [r.retry for r in responses] == [True, True, False]


@pytest.mark.parametrize("where", ["body", "stream"])
async def test_a_429_code_behind_http_200_is_a_rate_limit(
    store: LocalEventStore, where: str
) -> None:
    def answer(code: int) -> Callable[[], httpx2.Response]:
        if where == "body":
            raw = json.dumps(failing(code)).encode()
            return lambda: stream([raw], content_type="application/json")
        cut = [*CHAT_EVENTS[:4], chunk({"content": ""}, "error", error={"code": code})]
        return lambda: stream(sse(cut))

    run_id = await new_run(store)
    handler, sent = scripted(answer(429), answer(502))
    retries = Retries(attempts=1, backoff=0.01, max=0.02, rate_limited=10)
    p = profile(stream=where == "stream", retries=retries)
    async with client(store, run_id, p, handler) as mc:
        with pytest.raises(ModelCallError):
            await mc.call("test", PROMPT)
    assert len(sent) == 2
    responses = payloads(await store.events(run_id, types=["model.response"]))
    assert [r.retry for r in responses] == [True, False]


async def test_a_provider_wait_is_capped_at_retries_max(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    error = json.dumps({"error": {"message": "busy"}}).encode()
    handler, _ = scripted(
        lambda: stream([error], 503, retry_after="5", content_type="application/json"),
        lambda: stream(sse(CHAT_EVENTS)),
    )
    p = profile(retries=Retries(attempts=2, backoff=0, max=0.05))
    async with client(store, run_id, p, handler) as mc:
        start = time.monotonic()
        reply = await mc.call("test", PROMPT)
    assert time.monotonic() - start < 1
    assert reply.attempts == 2


async def test_replay_retries_where_the_recorded_call_did(store: LocalEventStore) -> None:
    source = await new_run(store)
    error = json.dumps({"error": {"message": "slow down"}}).encode()
    limited = lambda: stream([error], 429, content_type="application/json")  # noqa: E731
    handler, _ = scripted(limited, limited, lambda: stream(sse(CHAT_EVENTS)))
    recorded = profile(retries=Retries(attempts=3, backoff=0, max=0, rate_limited=0))
    async with client(store, source, recorded, handler) as mc:
        first = await mc.call("test", PROMPT)
    assert first.attempts == 3
    recording = await Recording.load(store, source)
    run_id = await new_run(store)
    fewer = recorded.model_copy(update={"retries": Retries(attempts=2, backoff=0, max=0)})
    async with client(store, run_id, fewer, handler, replay=recording) as mc:
        # a replay retries where the recording did, whatever the caller's check says
        again = await mc.call("test", PROMPT, cap=SpendCap(attempt_usd=0, fits=lambda _: False))
    assert (again.status, again.attempts, again.turn) == ("completed", 3, first.turn)
    assert recording.remaining() == 0


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


def test_no_profiles_ship() -> None:
    models = load_models()
    assert models.profiles == {}
    assert models.roles.unset() == ["agent", "sweep", "judge"]
    with pytest.raises(ProfileError, match="unknown profile"):
        models.get("nope")


def test_config_files_layer_in_turn(tmp_path: Path) -> None:
    user = tmp_path / "config.toml"
    user.write_text(
        PROFILES.read_text()
        + """
[profiles.served]
backend = "chat"
base_url_env = "SERVED_URL"
model = "served-model"
weights = "example/served-model"
reasoning = "full"
"""
    )
    path = tmp_path / "aims.toml"
    path.write_text(
        """
[profiles.agent-model.limits]
rpm = 100

[profiles.local]
extends = "served"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_KEY"

[roles]
agent = "local"
"""
    )
    models = load_models(user, path)
    agent = models.get("agent-model")
    assert agent.limits.rpm == 100
    assert agent.limits.tpm == 3000000
    local = models.get("local")
    assert (local.backend, local.model, local.api_key_env) == ("chat", "served-model", "LOCAL_KEY")
    assert local.base_url_env is None
    served = models.get("served")
    listed = {"AIMS_KEYS": "env:SERVED_URL"}
    with pytest.raises(ProfileError, match="SERVED_URL is not set"):
        served.url(listed)
    assert served.url(listed | {"SERVED_URL": "https://x.example/v1/"}) == "https://x.example/v1"
    assert models.role("agent").name == "local"
    assert models.role("judge").name == "monitor-model"
    assert models.snapshot(["local"]) == {"local": local.model_dump(mode="json")}

    for bad, match in [
        ('[profiles.x]\nextends = "agent-model"\ncolour = "red"\n', "colour"),
        ('[profiles.x]\nextends = "missing"\n', "unknown profile"),
        ('[profiles.x]\nextends = "x"\n', "extends itself"),
        ('[roles]\njudge = "missing"\n', "unknown profile"),
        ("[profiles.agent-model.sampling]\ntemperature = 9\n", "temperature"),
        ('[profiles.c]\nextends = "served"\nsummary = "auto"\n', "summary"),
        ("[profiles\n", "aims.toml"),
    ]:
        path.write_text(bad)
        with pytest.raises(ProfileError, match=match):
            load_models(user, path)


CANARY = "CANARY-7f3a"
KEY = f"sk-{CANARY}"
LISTED = {"AIMS_KEYS": "TEST_KEY=http://model.invalid/v1"}


def told(e: BaseException) -> str:
    """All an error tells when it is shown: its text, its chain and its traceback."""
    return "".join(traceback.format_exception(e))


def holding(root: Path, text: str) -> list[Path]:
    """The files under `root` that hold `text`."""
    return [f for f in root.rglob("*") if f.is_file() and text.encode() in f.read_bytes()]


@pytest.mark.parametrize(
    "value",
    [
        CANARY,
        f"ftp://{CANARY}.test/v1",
        f"http://[{CANARY}]/v1",
        f"http://[::1]{CANARY}/v1",
        f"http://{CANARY}\u00ad.test/v1",
        f"http:///{CANARY}",
        f"http://{CANARY}.test/\udcff",
        f"http://{CANARY}.test/v1?\udcff",
        "http://",
    ],
)
def test_an_env_url_that_is_not_one_is_refused_without_its_value(value: str) -> None:
    p = profile(base_url=None, base_url_env="SERVED_URL")
    with pytest.raises(ProfileError) as info:
        p.url({"SERVED_URL": value, "AIMS_KEYS": "env:SERVED_URL"})
    assert str(info.value) == "profile test: SERVED_URL does not hold an http or https URL"
    assert info.value.__context__ is None
    assert CANARY not in told(info.value)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (f"{KEY}\r", "a carriage return at character 15"),
        (f"{KEY}\n", "a line feed at character 15"),
        (f"sk-CA\r\nX-Evil: {CANARY}", "a carriage return at character 6"),
        (f"{KEY}\u00c9", "a non-ASCII character at character 15"),
        (f"{KEY}\u2028", "a non-ASCII character at character 15"),
        (f"{KEY}\x00", "a control character at character 15"),
        (f"{KEY}\x7f", "a control character at character 15"),
        (f"\x01{KEY}", "a control character at character 1"),
        (f"{KEY} ", "a space or tab at its start or end"),
        (f"\t{KEY}", "a space or tab at its start or end"),
    ],
)
def test_a_key_an_http_header_cannot_carry_is_refused_without_its_value(
    value: str, problem: str
) -> None:
    p = profile(api_key_env="TEST_KEY")
    with pytest.raises(ProfileError) as info:
        p.api_key(p.url(), {"TEST_KEY": value} | LISTED)
    assert str(info.value).startswith(
        f"profile test: TEST_KEY holds {problem}, which an HTTP header cannot carry"
    )
    assert info.value.__context__ is None
    assert CANARY not in told(info.value)


def test_a_key_an_http_header_can_carry_is_used_as_it_is() -> None:
    p = profile(api_key_env="TEST_KEY")
    for value in [KEY, "sk-a b\tc", "sk-'\"=+/~"]:
        assert p.api_key(p.url(), {"TEST_KEY": value} | LISTED) == value
    with pytest.raises(ProfileError) as info:
        p.api_key(p.url(), {"TEST_KEY": f"{KEY}\r"} | LISTED)
    assert str(info.value) == (
        "profile test: TEST_KEY holds a carriage return at character 15, which an HTTP header"
        " cannot carry (a file with Windows line ends adds one to each value)"
    )


@pytest.mark.parametrize(
    ("fields", "env"),
    [
        ({"base_url": None, "base_url_env": "CANARY_URL"}, {"CANARY_URL": CANARY}),
        ({"base_url": None, "base_url_env": "CANARY_URL"}, {"CANARY_URL": f"http://[{CANARY}]"}),
        ({"api_key_env": "CANARY_KEY"}, {"CANARY_KEY": f"{KEY}\r"}),
        ({"api_key_env": "CANARY_KEY"}, {"CANARY_KEY": f"{KEY} "}),
        ({"api_key_env": "CANARY_KEY"}, {"CANARY_KEY": f"{KEY}\u00c9"}),
    ],
)
async def test_a_value_aims_cannot_send_stays_out_of_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any], env: dict[str, str]
) -> None:
    """A URL or key env var whose value cannot go on the wire fails the check before anything
    is sent. Neither the error nor any file of the run tells the value."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AIMS_KEYS", "env:CANARY_URL,CANARY_KEY=http://models.example/v1")
    p = profile(**{"stream": False, "base_url": "http://models.example/v1"} | fields)
    root = tmp_path / "runs"
    async with serving(Server()) as proxy:
        environ(monkeypatch, HTTP_PROXY=proxy.url)
        async with await LocalEventStore.open(root) as store:
            with pytest.raises(ProfileError) as info:
                await record_check(store, config(p), [p])
            (run_id,) = store.run_ids()
            finished = (await store.every(run_id))[-1].payload()
    assert proxy.lines == []
    assert isinstance(finished, RunFinished)
    assert (finished.status, finished.reason) == ("failed", f"ProfileError: {info.value}")
    assert CANARY not in told(info.value)
    assert holding(root, CANARY) == []
    # the scan reads the run's files: they name the variable
    assert holding(root, next(iter(env)))


@pytest.mark.parametrize(
    ("kw", "refused"),
    [
        ({"api_key": f"{KEY}\r"}, "value"),
        ({"api_key": f"sk-'{CANARY}\"\r\nX-Evil: 1"}, "value"),
        ({"api_key": f"{KEY} "}, "value"),
        ({"api_key": "k", "default_headers": {f"X-{CANARY}\r": "1"}}, "name"),
    ],
)
async def test_a_header_h11_refuses_is_not_told(kw: dict[str, Any], refused: str) -> None:
    """h11 puts a header it refuses in its error, and the Authorization header holds the key.
    Keys and headers are checked before use; the error text hides the header all the same."""
    async with (
        serving(Server()) as server,
        openai.AsyncOpenAI(
            base_url=f"{server.url}/v1",
            max_retries=0,
            http_client=httpx2.AsyncClient(trust_env=False),
            **kw,
        ) as sdk,
    ):
        with pytest.raises(openai.APIConnectionError) as info:
            await sdk.models.list()
    assert server.lines == []
    # h11 lowers a header name
    assert CANARY.lower() in told(info.value).lower()
    text, kind, *_ = _classify(info.value)
    assert (text, kind) == (f"LocalProtocolError: Illegal header {refused} (not shown)", "protocol")
    assert CANARY.lower() not in text.lower()


def test_aims_models_check_refuses_a_value_it_cannot_send_without_telling_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "aims.toml"
    path.write_text(
        """
[profiles.served]
backend = "chat"
base_url_env = "CANARY_URL"
api_key_env = "CANARY_KEY"
model = "m"
weights = "w"
reasoning = "none"
"""
    )
    root = tmp_path / "runs"
    monkeypatch.setenv("AIMS_KEYS", "CANARY_KEY=env:CANARY_URL")
    for env, problem in [
        (
            {"CANARY_URL": f"http://[{CANARY}]/v1", "CANARY_KEY": "k"},
            "CANARY_URL does not hold an http or https URL",
        ),
        (
            {"CANARY_URL": "http://models.example/v1", "CANARY_KEY": f"{CANARY}\r"},
            "CANARY_KEY holds a carriage return at character 12",
        ),
    ]:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        result = CliRunner().invoke(
            app, ["models", "check", "served", "--config", str(path), "--runs-dir", str(root)]
        )
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit), result.exception
        assert f"error: profile served: {problem}" in result.output
        assert CANARY not in result.output
    assert not root.exists() or holding(root, CANARY) == []


def test_weights_check() -> None:
    models = load_models(PROFILES)
    agent = models.get("agent-model")
    paid = agent.model_copy(update={"name": "agent-model-paid"})
    with pytest.raises(ProfileError, match=r"example/agent-model-1"):
        check_weights([agent], [paid])
    check_weights([agent], [paid], allow_same_model=True)
    check_weights([agent], [models.get("monitor-model")])


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

    kwarg = Chat().body(profile(effort="xhigh", effort_kwarg="reasoning_strength"), PROMPT)
    assert kwarg["chat_template_kwargs"] == {"reasoning_strength": "xhigh"}
    assert "reasoning_effort" not in kwarg
    with pytest.raises(ValidationError, match="effort_kwarg is only sent by the chat backend"):
        profile(backend="responses", effort_kwarg="reasoning_strength")

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


def detail(index: int, kind: str = "text", **fields: Any) -> dict[str, Any]:
    return {"type": f"reasoning.{kind}", **fields, "format": "f", "index": index}


def thought(*parts: Any, reasoning: str = "") -> dict[str, Any]:
    return chunk({"reasoning": reasoning, "reasoning_details": list(parts)})


def assembled(events: Iterable[dict[str, Any]]) -> Any:
    acc = Chat().accumulator()
    for event in events:
        acc.feed(event)
    return acc.result()


SIGNED = [
    thought(detail(0, text="17*23 "), reasoning="17*23 "),
    thought(detail(0, text="is 391."), reasoning="is 391."),
    thought(detail(0, signature="c2lnbmVk")),
    chunk({"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "f", "arguments": "{}"}}]}),
    chunk({}, "tool_calls"),
]
SEALED = [
    thought(detail(0, "summary", summary="Checking "), reasoning="Checking "),
    thought(detail(0, "summary", summary="Paris."), reasoning="Paris."),
    thought(detail(1, "encrypted", data="gAAA", id="rs_1")),
    chunk({"content": "ok"}, "stop"),
]


def test_streamed_reasoning_details_merge_into_their_blocks() -> None:
    signed = assembled(SIGNED)
    block = {
        "type": "reasoning.text",
        "text": "17*23 is 391.",
        "format": "f",
        "index": 0,
        "signature": "c2lnbmVk",
    }
    assert signed.native == [
        {
            "role": "assistant",
            "content": None,
            "reasoning": "17*23 is 391.",
            "reasoning_details": [block],
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
    ]
    assert (signed.reasoning, signed.reasoning_kind) == ("17*23 is 391.", "full")
    assert signed.final["choices"][0]["message"]["reasoning_details"] == [block]

    sealed = assembled(SEALED)
    assert sealed.native[0]["reasoning_details"] == [
        {"type": "reasoning.summary", "summary": "Checking Paris.", "format": "f", "index": 0},
        {"type": "reasoning.encrypted", "data": "gAAA", "id": "rs_1", "format": "f", "index": 1},
    ]
    assert (sealed.reasoning, sealed.reasoning_kind) == ("Checking Paris.", "summary")


def test_reasoning_details_go_back_with_the_turn() -> None:
    turn = assembled(SIGNED).turn("chat")
    items = [Message(role="user", content="u"), turn, ToolOutput(call_id="t1", output="o")]
    sent = Chat().render(Prompt(items=items))
    assert sent[1] == turn.native[0]
    assert "c2lnbmVk" in json.dumps(sent[1])
    assert "reasoning_details" not in json.dumps(Responses().render(Prompt(items=items)))


def test_reasoning_details_of_one_index_keep_their_types_apart() -> None:
    out = assembled(
        [
            thought(detail(0, text="a"), detail(0, "encrypted", data="x")),
            thought(detail(0, text="b"), detail(0, "encrypted", data="y"), "odd"),
            chunk({"reasoning_details": "odd"}),
            chunk({"reasoning_details": None}, "stop"),
        ]
    )
    assert [
        (d["type"], d.get("text"), d.get("data")) for d in out.native[0]["reasoning_details"]
    ] == [
        ("reasoning.text", "ab", None),
        ("reasoning.encrypted", None, "xy"),
    ]


def test_a_later_null_does_not_clear_a_reasoning_detail() -> None:
    out = assembled(
        [
            thought(detail(0, text="a", signature=None, id="r1")),
            thought(detail(0, text="b", signature="s", id=None)),
            thought(detail(0, text=None, signature=None)),
            chunk({}, "stop"),
        ]
    )
    assert out.native[0]["reasoning_details"] == [
        {
            "type": "reasoning.text",
            "text": "ab",
            "signature": "s",
            "id": "r1",
            "format": "f",
            "index": 0,
        }
    ]


def test_an_answer_with_no_reasoning_details_sends_none_back() -> None:
    out = assembled(CHAT_EVENTS)
    assert "reasoning_details" not in out.native[0]
    assert out.reasoning_kind == "full"


async def test_unstreamed_reasoning_details_stay_as_they_came(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    details = [
        detail(0, "summary", summary="Checking Paris."),
        detail(1, "encrypted", data="gAAA", id="rs_1"),
    ]
    message = {
        "role": "assistant",
        "content": "ok",
        "reasoning": "Checking Paris.",
        "reasoning_details": details,
    }
    answer = {"id": "c2", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
    raw = json.dumps(answer).encode()
    handler, _ = scripted(lambda: stream([raw], content_type="application/json"))
    async with client(store, run_id, profile(stream=False, reasoning="summary"), handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.turn.native == [message]
    assert reply.reasoning_kind == "summary"


def test_cli_models_list(tmp_path: Path) -> None:
    path = tmp_path / "aims.toml"
    path.write_text(PROFILES.read_text())
    result = CliRunner().invoke(app, ["models", "list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "agent-model  responses  agent-model-1" in result.output
    assert "[agent]" in result.output
    result = CliRunner().invoke(app, ["models", "check", "nope", "--config", str(path)])
    assert result.exit_code == 1
    assert "unknown profile" in result.output


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_a_second_signal_while_aims_models_check_seals_leaves_the_seal_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sig: signal.Signals
) -> None:
    """A Ctrl-C or SIGTERM during `aims models check` seals its run as cancelled and exits 130.
    Another one while it seals does not cut the seal."""
    root = tmp_path / "runs"
    append = LocalEventStore.append

    async def hang(client: object, p: Profile, *, measure: bool = False) -> list[Any]:
        os.kill(os.getpid(), sig)
        await asyncio.sleep(5)
        raise AssertionError("the signal did not stop the check")

    async def slow(self: LocalEventStore, run: str, drafts: list[Any]) -> Any:
        if any(isinstance(d.payload, RunFinished) for d in drafts):
            os.kill(os.getpid(), sig)
            await asyncio.sleep(0.2)
        return await append(self, run, drafts)

    settings = SimpleNamespace(models=config(profile()), monitor=())
    monkeypatch.setattr("aims.cli._settings", lambda config: settings)
    monkeypatch.setattr(check, "check_profile", hang)
    monkeypatch.setattr(LocalEventStore, "append", slow)
    # a SIGTERM the command does not handle would end this test process
    before = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        result = CliRunner().invoke(app, ["models", "check", "--runs-dir", str(root)])
    finally:
        signal.signal(signal.SIGTERM, before)
    monkeypatch.undo()
    assert result.exit_code == EXIT_CANCELLED, result.output

    async def read() -> list[tuple[str, str, Any]]:
        async with await LocalEventStore.open(root) as store:
            found = [await store.run(r) for r in store.run_ids()]
            return [
                (i.run_id, i.status, (await store.every(i.run_id))[-1].data) for i in found if i
            ]

    ((run_id, status, last),) = asyncio.run(read())
    assert status == "cancelled"
    assert last["reason"] == "cancelled before the checks finished"
    assert f"stopped; run {run_id} is sealed as cancelled" in result.output


def test_a_ctrl_c_while_aims_models_check_makes_its_run_is_told_as_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Ctrl-C lands between the run's folder and its first event. The run is made anyway
    and sealed as cancelled, and the stop line says so: no folder without events is left."""
    root = tmp_path / "runs"
    writer = LocalEventStore._writer
    sent: list[str] = []

    async def opening(self: LocalEventStore, run_id: str) -> Any:
        if not sent:
            sent.append(run_id)
            os.kill(os.getpid(), signal.SIGINT)
            async with asyncio.timeout(5):
                while not any(t.cancelling() for t in asyncio.all_tasks()):  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        return await writer(self, run_id)

    async def never(client: object, p: Profile, *, measure: bool = False) -> list[Any]:
        raise AssertionError("the check began after the Ctrl-C")

    settings = SimpleNamespace(models=config(profile()), monitor=())
    monkeypatch.setattr("aims.cli._settings", lambda config: settings)
    monkeypatch.setattr(check, "check_profile", never)
    monkeypatch.setattr(LocalEventStore, "_writer", opening)
    result = CliRunner().invoke(app, ["models", "check", "--runs-dir", str(root)])
    monkeypatch.undo()
    assert result.exit_code == EXIT_CANCELLED, result.output
    (run_id,) = sent
    assert [p.name for p in root.iterdir() if p.is_dir()] == [run_id]

    async def read() -> tuple[str | None, list[str]]:
        async with await LocalEventStore.open(root) as store:
            info = await store.run(run_id)
            return None if info is None else info.status, [
                e.type for e in await store.every(run_id)
            ]

    assert asyncio.run(read()) == ("cancelled", ["run.started", "run.finished"])
    assert f"stopped; run {run_id} is sealed as cancelled" in result.output


def test_aims_models_check_seals_the_runs_an_earlier_process_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"

    async def left() -> str:
        async with await LocalEventStore.open(root) as store:
            return await store.create_run(RunStarted(scenario="left", oversight="observe"))

    async def nothing(client: object, p: Profile, *, measure: bool = False) -> list[Any]:
        return []

    run_id = asyncio.run(left())
    settings = SimpleNamespace(models=config(profile()), monitor=())
    monkeypatch.setattr("aims.cli._settings", lambda config: settings)
    monkeypatch.setattr(check, "check_profile", nothing)
    result = CliRunner().invoke(app, ["models", "check", "--runs-dir", str(root)])
    monkeypatch.undo()
    assert result.exit_code == 0, result.output
    said = " ".join(result.stderr.split())
    assert "an earlier process left 1 run running; it is sealed as failed" in said

    async def read() -> tuple[str | None, object]:
        async with await LocalEventStore.open(root) as store:
            info = await store.run(run_id)
            (end,) = await store.events(run_id, types=["run.finished"])
            return None if info is None else info.status, end.data["reason"]

    assert asyncio.run(read()) == ("failed", "the process that ran it ended before it was sealed")


@pytest.mark.timing
async def test_replay_can_keep_the_recorded_pace(store: LocalEventStore) -> None:
    source = await new_run(store)
    parts = [part for e in CHAT_EVENTS for part in (*sse([e], done=False), 0.1)]
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
        # The recorded pace never beats the recording; a loaded host only slows the instant one.
        assert (took < recorded.latency_ms * 0.9) is fast, took


@pytest.mark.timing
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


BILL = {
    "cost": 0.00042,
    "is_byok": False,
    "cost_details": {"upstream_inference_cost": 0.00042},
}
PRICED = (6 * 1.0 + 4 * 0.1 + 5 * 2.0) / 1e6


def _usage(answer: Mapping[str, Any]) -> dict[str, Any]:
    return dict(answer["usage"])


def billed(backend: str, streamed: bool, bill: dict[str, Any]) -> httpx2.Response:
    """An answer whose usage carries `bill`, as a router's does."""
    events: list[dict[str, Any]]
    body: dict[str, Any]
    if backend == "chat":
        usage = _usage(USAGE) | bill
        events = [*CHAT_EVENTS[:-1], {**USAGE, "usage": usage}]
        message = {"role": "assistant", "content": "Hello"}
        body = {
            "id": "c1",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": usage,
        }
    else:
        body = RESPONSE | {"usage": _usage(RESPONSE) | bill}
        events = [*RESPONSES_EVENTS[:-1], {"type": "response.completed", "response": body}]
    if streamed:
        return stream(sse(events))
    return stream([json.dumps(body).encode()], content_type="application/json")


@pytest.mark.parametrize("backend", ["chat", "responses"])
@pytest.mark.parametrize("streamed", [True, False])
async def test_a_cost_the_provider_reports_wins_over_the_price(
    store: LocalEventStore, backend: str, streamed: bool
) -> None:
    run_id = await new_run(store)
    p = profile(backend=backend, stream=streamed, reasoning="none")
    handler, _ = scripted(lambda: billed(backend, streamed, BILL))
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    (response,) = [
        e for e in payloads(await model_events(store, run_id)) if e.type == "model.response"
    ]
    assert reply.cost_usd == response.cost_usd == pytest.approx(0.00042)
    assert response.cost_source == "provider"
    assert response.body is not None
    body = json.loads(await store.get_blob(run_id, response.body.sha256))
    assert body["usage"]["cost"] == 0.00042
    assert body["usage"]["cost_details"] == BILL["cost_details"]


async def test_a_cost_comes_from_the_price_when_the_provider_reports_none(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(*[lambda: stream(sse(CHAT_EVENTS))] * 2)
    unpriced = profile(name="unpriced", price=None)
    async with client(store, run_id, profile(), handler) as mc:
        await mc.call("test", PROMPT)
        await mc.call(unpriced, PROMPT)
    priced, free = await store.events(run_id, types=["model.response"])
    assert priced.data["cost_usd"] == pytest.approx(PRICED)
    assert priced.data["cost_source"] == "price"
    assert (free.data["cost_usd"], free.data["cost_source"]) == (None, None)
    kept = ModelResponse.model_validate(priced.data).body
    assert kept is not None
    body = json.loads(await store.get_blob(run_id, kept.sha256))
    assert "cost" not in body["usage"]


async def test_a_cost_at_a_list_price_says_so(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(*[lambda: stream(sse(CHAT_EVENTS))] * 2)
    listed = profile(
        name="listed", price_key="vendor/m", listed={"checked": "2026-11-15T01:46:00Z"}
    )
    async with client(store, run_id, listed, handler) as mc:
        await mc.call("listed", PROMPT)
    (response,) = await store.events(run_id, types=["model.response"])
    assert response.data["cost_usd"] == pytest.approx(PRICED)
    assert response.data["cost_source"] == "list"
    handler, _ = scripted(lambda: billed("chat", True, BILL))
    async with client(store, run_id, listed, handler) as mc:
        await mc.call("listed", PROMPT)
    _, reported = await store.events(run_id, types=["model.response"])
    assert (reported.data["cost_usd"], reported.data["cost_source"]) == (0.00042, "provider")


async def test_a_reported_cost_prices_a_profile_with_no_price(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(lambda: billed("chat", True, BILL))
    async with client(store, run_id, profile(price=None), handler) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.cost_usd == pytest.approx(0.00042)


@pytest.mark.parametrize(
    ("bill", "cost"),
    [
        ({"cost": 0}, 0.0),
        ({"cost": 3}, 3.0),
        # The caller's own provider key: `cost` is the router's fee, the provider bills the rest.
        (
            {"cost": 0.001, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.02}},
            0.021,
        ),
        (
            {"cost": 0.001, "is_byok": True, "cost_details": {"upstream_inference_cost": None}},
            0.001,
        ),
        ({"cost": 0.001, "is_byok": True}, 0.001),
        ({"cost": 0.001, "is_byok": True, "cost_details": {"upstream_inference_cost": -1}}, 0.001),
        (
            {"cost": 0.001, "is_byok": "yes", "cost_details": {"upstream_inference_cost": 0.02}},
            0.001,
        ),
        ({"cost": 0.001, "cost_details": {"upstream_inference_cost": 0.02}}, 0.001),
        ({}, None),
        ({"cost": None}, None),
        ({"cost": True}, None),
        ({"cost": "0.001"}, None),
        ({"cost": -0.001}, None),
        ({"cost": float("nan")}, None),
        ({"cost": float("inf")}, None),
        ({"cost": {"usd": 1}}, None),
        ({"is_byok": True, "cost_details": {"upstream_inference_cost": 0.02}}, None),
    ],
)
def test_reported_cost(bill: dict[str, Any], cost: float | None) -> None:
    assert _reported_cost(bill) == (None if cost is None else pytest.approx(cost))


async def test_a_cost_the_provider_reports_badly_falls_back_to_the_price(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    handler, _ = scripted(lambda: billed("chat", True, {"cost": -1}))
    async with client(store, run_id, profile(), handler) as mc:
        reply = await mc.call("test", PROMPT)
    (response,) = await store.events(run_id, types=["model.response"])
    assert reply.cost_usd == pytest.approx(PRICED)
    assert response.data["cost_source"] == "price"


async def test_the_reply_sums_reported_and_priced_attempts(store: LocalEventStore) -> None:
    run_id = await new_run(store)
    failed = FAILED | {"usage": _usage(FAILED) | {"cost": 0.01}}
    handler, _ = scripted(
        lambda: stream(sse([{"type": "response.failed", "response": failed}])),
        lambda: stream(sse(RESPONSES_EVENTS)),
    )
    p = profile(backend="responses", reasoning="summary")
    async with client(store, run_id, p, handler) as mc:
        reply = await mc.call("test", PROMPT)
    first, second = await store.events(run_id, types=["model.response"])
    assert (first.data["cost_usd"], first.data["cost_source"]) == (0.01, "provider")
    assert second.data["cost_source"] == "price"
    assert reply.cost_usd == pytest.approx(0.01 + 38e-6)


async def test_a_replayed_reported_cost_costs_nothing(store: LocalEventStore) -> None:
    source = await new_run(store)
    handler, _ = scripted(lambda: billed("chat", True, BILL))
    p = profile()
    async with client(store, source, p, handler) as mc:
        await mc.call("test", PROMPT)
    run_id = await new_run(store)
    recording = await Recording.load(store, source)
    async with client(store, run_id, p, lambda r: None, replay=recording) as mc:
        reply = await mc.call("test", PROMPT)
    (response,) = await store.events(run_id, types=["model.response"])
    assert reply.cost_usd == 0.0
    assert (response.data["cost_usd"], response.data["cost_source"]) == (0.0, None)


def test_a_model_response_recorded_before_cost_source_loads() -> None:
    old = ModelResponse(call_id="c", attempt=1, status="completed", latency_ms=1, cost_usd=0.5)
    data = old.model_dump(mode="json")
    del data["cost_source"]
    assert ModelResponse.model_validate(data).cost_source is None


ROUTED = {"provider": "Wafer", "model": "vendor/m-0910", "service_tier": "flex"}


def routed(backend: str, streamed: bool, said: dict[str, Any]) -> httpx2.Response:
    """An answer that says `said` about itself at the top, as a router's does."""
    events: list[dict[str, Any]]
    body: dict[str, Any]
    if backend == "chat":
        events = [{**e, **said} for e in CHAT_EVENTS]
        message = {"role": "assistant", "content": "Hello"}
        choices = [{"index": 0, "message": message, "finish_reason": "stop"}]
        body = {"id": "c1", "choices": choices, "usage": _usage(USAGE), **said}
    else:
        body = RESPONSE | said
        created = {"id": "resp_1", "status": "in_progress", **said}
        events = [
            {"type": "response.created", "response": created},
            *RESPONSES_EVENTS[1:-1],
            {"type": "response.completed", "response": body},
        ]
    if streamed:
        return stream(sse(events))
    return stream([json.dumps(body).encode()], content_type="application/json")


async def _answered(store: LocalEventStore, backend: str, streamed: bool, said: Any) -> Any:
    run_id = await new_run(store)
    p = profile(backend=backend, stream=streamed, reasoning="none")
    handler, _ = scripted(lambda: routed(backend, streamed, said))
    async with client(store, run_id, p, handler) as mc:
        await mc.call("test", PROMPT)
    (response,) = [
        e for e in payloads(await model_events(store, run_id)) if e.type == "model.response"
    ]
    assert response.body is not None
    return response, json.loads(await store.get_blob(run_id, response.body.sha256))


@pytest.mark.parametrize("backend", ["chat", "responses"])
@pytest.mark.parametrize("streamed", [True, False])
async def test_the_response_records_who_served_it(
    store: LocalEventStore, backend: str, streamed: bool
) -> None:
    response, body = await _answered(store, backend, streamed, ROUTED)
    assert (response.provider, response.model, response.service_tier) == (
        "Wafer",
        "vendor/m-0910",
        "flex",
    )
    assert {k: body[k] for k in ROUTED} == ROUTED


@pytest.mark.parametrize(
    "said",
    [
        {},
        {"provider": None, "service_tier": None},
        {"provider": 7, "service_tier": ["x"]},
    ],
)
async def test_an_answer_that_names_no_provider_records_none(
    store: LocalEventStore, said: dict[str, Any]
) -> None:
    response, _ = await _answered(store, "chat", True, said)
    assert (response.provider, response.service_tier) == (None, None)
    assert response.model == "m"


def test_a_model_response_recorded_before_the_provider_fields_loads() -> None:
    old = ModelResponse(call_id="c", attempt=1, status="completed", latency_ms=1)
    data = old.model_dump(mode="json")
    for key in ("provider", "model", "service_tier"):
        del data[key]
    loaded = ModelResponse.model_validate(data)
    assert (loaded.provider, loaded.model, loaded.service_tier) == (None, None, None)


TOOLS = Prompt(items=PROMPT.items, tools=[WEATHER])
# What a profile with tools and an extra field sends, byte for byte.
PLAIN_BODY = {
    "chat": '{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true,'
    '"stream_options":{"include_usage":true},"tools":[{"type":"function","function":'
    '{"name":"get_weather","description":"","parameters":{"type":"object","properties":{}}}}],'
    '"parallel_tool_calls":false,"x":1}',
    "responses": '{"model":"m","input":[{"role":"user","content":"hi"}],"store":false,'
    '"stream":true,"include":["reasoning.encrypted_content"],"tools":[{"type":"function",'
    '"name":"get_weather","description":"","parameters":{"type":"object","properties":{}}}],'
    '"parallel_tool_calls":false,"x":1}',
}


@pytest.mark.parametrize("backend", ["chat", "responses"])
def test_a_profile_sends_the_body_it_sent_before(backend: str) -> None:
    p = profile(backend=backend, parallel_tool_calls=False, extra={"x": 1})
    body = BACKENDS[p.backend].body(p, TOOLS)
    assert json.dumps(body, separators=(",", ":")) == PLAIN_BODY[backend]


def test_a_router_object_in_extra_goes_out_as_written() -> None:
    provider = {"sort": {"by": "latency", "partition": "none"}, "preferred_min_throughput": 50}
    own = profile(extra={"provider": provider})
    assert json.dumps(Chat().body(own, PROMPT)["provider"]) == json.dumps(provider)


def test_a_routing_table_in_a_config_is_refused() -> None:
    with pytest.raises(ValidationError) as info:
        profile(routing={"sort": "price"})
    assert [e["loc"] for e in info.value.errors()] == [("routing",)]


# The `routing` table of a profile snapshot recorded while profiles had one, and the bodies
# such a profile with `extra = {x = 1}` sent then.
STORED_ROUTING = {
    "order": None,
    "only": None,
    "ignore": None,
    "allow_fallbacks": None,
    "require_parameters": None,
    "data_collection": None,
    "zdr": None,
    "enforce_distillable_text": None,
    "quantizations": None,
    "sort": {"by": "latency", "partition": "none"},
    "preferred_min_throughput": 50,
    "preferred_max_latency": None,
    "max_price": {"prompt": 1, "completion": 2.5, "request": None, "image": None},
}
SENT_WITH_ROUTING = {
    "chat": '{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true,'
    '"stream_options":{"include_usage":true},"provider":{"sort":{"by":"latency",'
    '"partition":"none"},"preferred_min_throughput":50,"max_price":{"prompt":1,'
    '"completion":2.5}},"x":1}',
    "responses": '{"model":"m","input":[{"role":"user","content":"hi"}],"store":false,'
    '"stream":true,"include":["reasoning.encrypted_content"],"provider":{"sort":'
    '{"by":"latency","partition":"none"},"preferred_min_throughput":50,"max_price":'
    '{"prompt":1,"completion":2.5}},"x":1}',
}


@pytest.mark.parametrize("backend", ["chat", "responses"])
def test_a_stored_profile_with_routing_sends_the_body_it_recorded(backend: str) -> None:
    snap = profile(backend=backend, extra={"x": 1}).snapshot() | {"routing": STORED_ROUTING}
    loaded = Profile.model_validate(snap, context=STORED)
    assert loaded.extra["x"] == 1
    body = json.dumps(BACKENDS[loaded.backend].body(loaded, PROMPT)).encode()
    # A replay keys a request by its canonical body.
    assert canonical_key(body) == canonical_key(SENT_WITH_ROUTING[backend].encode())
    assert _schedule([[None, snap]]) == [(None, loaded)]


def test_a_stored_profile_with_routing_and_no_extra_keeps_the_provider_object() -> None:
    snap = profile().snapshot() | {"routing": STORED_ROUTING}
    del snap["extra"]
    loaded = Profile.model_validate(snap, context=STORED)
    assert loaded.extra == {
        "provider": {
            "sort": {"by": "latency", "partition": "none"},
            "preferred_min_throughput": 50,
            "max_price": {"prompt": 1, "completion": 2.5},
        }
    }


def test_a_stored_profile_with_an_empty_routing_table_loads_as_it_was() -> None:
    p = profile(extra={"x": 1})
    snap = p.snapshot() | {"routing": dict.fromkeys(STORED_ROUTING)}
    assert Profile.model_validate(snap, context=STORED) == p


@pytest.mark.parametrize(
    ("headers", "match"),
    [
        ({"X Title": "a"}, "not an HTTP token"),
        ({"": "a"}, "not an HTTP token"),
        ({"X-Tïtle": "a"}, "not an HTTP token"),
        ({"X-Title:": "a"}, "not an HTTP token"),
        ({"X-Title": ""}, "printable ASCII"),
        ({"X-Title": "a\r\nX-Injected: 1"}, "printable ASCII"),
        ({"X-Title": "a\nb"}, "printable ASCII"),
        ({"X-Title": "a\rb"}, "printable ASCII"),
        ({"X-Title": "a\x00b"}, "printable ASCII"),
        ({"X-Title": " a"}, "printable ASCII"),
        ({"X-Title": "a\t"}, "printable ASCII"),
        ({"X-Title": "café"}, "printable ASCII"),
        ({"X-Title": "a", "x-title": "b"}, "headers X-Title and x-title differ only by case"),
    ],
)
def test_bad_headers_are_refused(headers: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        profile(headers=headers)


@pytest.mark.parametrize(
    "name",
    [
        "Accept",
        "accept-encoding",
        "Authorization",
        "AUTHORIZATION",
        "Connection",
        "Content-Length",
        "Content-Type",
        "Cookie",
        "Host",
        "Keep-Alive",
        "OpenAI-Organization",
        "OpenAI-Project",
        "Proxy-Authorization",
        "Proxy-Connection",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "X-Stainless-Retry-Count",
        "x-stainless-lang",
    ],
)
def test_headers_that_aims_sets_or_that_carry_a_secret_are_refused(name: str) -> None:
    with pytest.raises(ValidationError, match=f"header {name} is set by AIMS or carries a secret"):
        profile(headers={name: "x"})


def test_headers_keep_their_names_and_values_in_the_snapshot() -> None:
    headers = {
        "HTTP-Referer": "https://example.com/aims",
        "X-Title": "AIMS\tbench run",
        "User-Agent": "aims-test/1",
    }
    p = profile(headers=headers)
    assert p.headers == headers
    assert p.snapshot()["headers"] == headers


def _heard(seen: list[httpx2.Request], answer: Callable[[], httpx2.Response]) -> Handler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answer()

    return handle


@pytest.mark.parametrize("backend", ["chat", "responses"])
async def test_profile_headers_go_out_beside_the_sdks_own(
    store: LocalEventStore, backend: str
) -> None:
    run_id = await new_run(store)
    events = CHAT_EVENTS if backend == "chat" else RESPONSES_EVENTS
    seen: list[httpx2.Request] = []
    handler = _heard(seen, lambda: stream(sse(events)))
    headers = {"X-Title": "AIMS bench", "HTTP-Referer": "https://example.com"}
    reasoning = "full" if backend == "chat" else "summary"
    p = profile(backend=backend, reasoning=reasoning, api_key_env="TEST_KEY", headers=headers)
    async with ModelClient(
        store,
        run_id,
        config(p),
        transport=httpx2.MockTransport(handler),
        env={"TEST_KEY": "sk-test"} | LISTED,
    ) as mc:
        reply = await mc.call("test", PROMPT)
    assert reply.status == "completed"
    (request,) = seen
    assert request.headers["x-title"] == "AIMS bench"
    assert request.headers["http-referer"] == "https://example.com"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("AsyncOpenAI/Python")
    assert request.headers["x-stainless-retry-count"] == "0"
    assert request.headers["accept-encoding"] == "identity"

    # A replay sends no request, so the headers change nothing there.
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("replay must not reach the provider")

    for sent in (headers, {"X-Title": "elsewhere"}):
        replayed = await new_run(store)
        recording = await Recording.load(store, run_id)
        again = p.model_copy(update={"headers": sent})
        async with client(store, replayed, again, refuse, replay=recording) as mc:
            assert (await mc.call("test", PROMPT)).turn == reply.turn


async def test_profiles_that_differ_only_in_headers_get_their_own_client(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    seen: list[httpx2.Request] = []
    handler = _heard(seen, lambda: stream(sse(CHAT_EVENTS)))
    a = profile(name="a", headers={"X-Title": "a"})
    b = profile(name="b", headers={"X-Title": "b"})
    same = profile(name="same", headers={"X-Title": "a"})
    bare = profile(name="bare")
    async with client(store, run_id, a, handler) as mc:
        for p in (a, b, same, bare, a):
            await mc.call(p, PROMPT)
        assert len(mc._clients) == 3
    assert [r.headers.get("x-title") for r in seen] == ["a", "b", "a", None, "a"]


@pytest.mark.parametrize("backend", ["chat", "responses"])
@pytest.mark.parametrize("parallel", [None, True, False])
def test_parallel_tool_calls_goes_out_only_when_set_and_with_tools(
    backend: str, parallel: bool | None
) -> None:
    p = profile(backend=backend, parallel_tool_calls=parallel)
    backend_ = BACKENDS[p.backend]
    sent = backend_.body(p, TOOLS).get("parallel_tool_calls", "unsent")
    assert sent == ("unsent" if parallel is None else parallel)
    assert "parallel_tool_calls" not in backend_.body(p, PROMPT)
    assert params(p, TOOLS).parallel_tool_calls is parallel
    assert params(p, PROMPT).parallel_tool_calls is None


def test_a_profile_sends_no_parallel_tool_calls_unless_it_says() -> None:
    assert profile().parallel_tool_calls is None
    assert "parallel_tool_calls" not in Chat().body(profile(), TOOLS)


@pytest.mark.parametrize(("parallel", "verdict"), [(False, "warn"), (None, "ok"), (True, "ok")])
async def test_the_tools_check_warns_of_parallel_calls_only_when_the_profile_turns_them_off(
    parallel: bool | None, verdict: str
) -> None:
    async def answer(p: Profile, prompt: Prompt, *, overrides: Overrides | None = None) -> Reply:
        if prompt.tools and isinstance(prompt.items[-1], Message):
            calls = [
                ToolRequest(id=f"t{i}", name="get_weather", arguments='{"city":"Paris"}')
                for i in (1, 2)
            ]
            turn = Turn(tool_calls=calls, backend=p.backend)
        elif prompt.output_schema is not None:
            turn = Turn(text='{"answer":42,"sure":true}', backend=p.backend)
        else:
            turn = Turn(text="42", backend=p.backend)
        return Reply(call_id="c", profile=p.name, status="completed", attempts=1, turn=turn)

    fake: Any = SimpleNamespace(call=answer)
    checks = {c.name: c for c in await check_profile(fake, profile(parallel_tool_calls=parallel))}
    tools = checks["tools"]
    assert tools.verdict == verdict
    if verdict == "warn":
        assert tools.detail == "2 calls in one turn with parallel_tool_calls off"


def _answers(kind: str = "none", usage: bool = True) -> tuple[Any, list[Overrides | None]]:
    """A client that answers each check right, with reasoning `kind` on each answer, and the
    overrides each call got."""
    seen: list[Overrides | None] = []

    async def answer(p: Profile, prompt: Prompt, *, overrides: Overrides | None = None) -> Reply:
        seen.append(overrides)
        if prompt.tools and isinstance(prompt.items[-1], Message):
            call = ToolRequest(id="t1", name="get_weather", arguments='{"city":"Paris"}')
            turn = Turn(tool_calls=[call], backend=p.backend)
        elif prompt.output_schema is not None:
            turn = Turn(text='{"answer":42,"sure":true}', backend=p.backend)
        else:
            turn = Turn(text="42", backend=p.backend)
        counts = {"input_tokens": 9, "cached_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0}
        return Reply.model_validate(
            {
                "call_id": "c",
                "profile": p.name,
                "status": "completed",
                "attempts": 1,
                "turn": turn,
                "reasoning_kind": kind,
                "usage": counts if usage else {},
            }
        )

    return SimpleNamespace(call=answer), seen


@pytest.mark.parametrize(
    ("effort", "sent"), [(None, None), ("none", None), ("minimal", None), ("high", "low")]
)
async def test_the_check_never_raises_the_effort_it_measures_at(
    effort: str | None, sent: str | None
) -> None:
    fake, seen = _answers()
    await check_profile(fake, profile(effort=effort))
    assert {o.effort if o else None for o in seen} == {sent}
    assert {o.max_output_tokens if o else None for o in seen} == {2048}


@pytest.mark.parametrize(
    ("kind", "measure", "verdict", "end"),
    [
        ("full", False, "warn", "reasoning full, profile says none"),
        ("full", True, "ok", "reasoning full, set once the check passes"),
        ("none", True, "ok", "reasoning none"),
        ("none", False, "ok", "reasoning none"),
    ],
)
async def test_the_plain_check_warns_of_another_reasoning_kind_unless_it_measures(
    kind: str, measure: bool, verdict: str, end: str
) -> None:
    fake, _ = _answers(kind)
    plain = (await check_profile(fake, profile(reasoning="none"), measure=measure))[0]
    assert (plain.name, plain.verdict, plain.reasoning) == ("plain", verdict, kind)
    assert plain.detail == f"usage 9/0/1/0, {end}"


async def test_a_measuring_check_still_warns_of_missing_usage() -> None:
    fake, _ = _answers("full", usage=False)
    plain = (await check_profile(fake, profile(reasoning="none"), measure=True))[0]
    assert (plain.verdict, plain.reasoning) == ("warn", "full")
    assert plain.detail.startswith("usage lacks ")


@pytest.mark.parametrize("usage", [True, False])
async def test_the_plain_check_keeps_the_reasoning_kind_it_saw(usage: bool) -> None:
    fake, _ = _answers("full", usage=usage)
    checks = await check_profile(fake, profile(reasoning="none"))
    assert [(c.name, c.reasoning) for c in checks] == [
        ("plain", "full"),
        ("unstreamed", None),
        ("tools", None),
        ("structured", None),
    ]
    assert checks[0].verdict == "warn"

    async def broken(p: Profile, prompt: Prompt, *, overrides: Overrides | None = None) -> Reply:
        raise ModelCallError(
            Reply(
                call_id="c",
                profile=p.name,
                status="error",
                attempts=1,
                turn=Turn(backend=p.backend),
                error="down",
            )
        )

    down: Any = SimpleNamespace(call=broken)
    failed = await check_profile(down, profile())
    assert [c.reasoning for c in failed] == [None] * 4


def test_headers_layer_key_by_key_and_extra_as_a_whole(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    user.write_text(
        """
[profiles.router]
backend = "chat"
model = "router-model"
weights = "example/router-model"
reasoning = "full"
parallel_tool_calls = false
base_url = "https://router.example/api/v1"
headers = { "X-Title" = "AIMS", "HTTP-Referer" = "https://example.com" }

[profiles.router.extra]
provider = { order = ["a", "b"], sort = "price" }
cache_control = { type = "ephemeral" }
"""
    )
    project = tmp_path / "aims.toml"
    project.write_text(
        """
[profiles.router.headers]
"X-Extra" = "1"

[profiles.child]
extends = "router"
headers = { "x-title" = "Child" }
extra = { provider = { order = ["c"] } }

[profiles.plain]
backend = "chat"
base_url = "http://localhost:8000/v1"
model = "m"
weights = "w"
reasoning = "none"
"""
    )
    models = load_models(user, project)
    router = models.get("router")
    assert router.headers == {
        "X-Title": "AIMS",
        "HTTP-Referer": "https://example.com",
        "X-Extra": "1",
    }
    assert Chat().body(router, PROMPT)["provider"] == {"order": ["a", "b"], "sort": "price"}
    child = models.get("child")
    # A header's name ignores case, so the child's replaces the parent's.
    assert child.headers == {
        "HTTP-Referer": "https://example.com",
        "X-Extra": "1",
        "x-title": "Child",
    }
    # A layer's extra replaces the one below as a whole.
    assert child.extra == {"provider": {"order": ["c"]}}
    assert Chat().body(child, PROMPT)["provider"] == {"order": ["c"]}
    assert child.parallel_tool_calls is False
    assert models.get("plain").parallel_tool_calls is None

    for bad, match in [
        ('[profiles.router.headers]\nAuthorization = "Bearer k"\n', "set by AIMS"),
        ('[profiles.x]\nextends = "router"\nheaders = { "X-A" = 1 }\n', "X-A"),
        ('[profiles.x]\nextends = "router"\nrouting = { sort = "price" }\n', "routing"),
    ]:
        project.write_text(bad)
        with pytest.raises(ProfileError, match=match):
            load_models(user, project)


def test_a_profile_snapshot_from_before_headers_loads() -> None:
    p = profile(parallel_tool_calls=False)
    old = p.snapshot()
    del old["headers"]
    loaded = Profile.model_validate(old)
    assert (loaded.headers, loaded.parallel_tool_calls) == ({}, False)
    assert loaded == p
    assert Chat().body(loaded, TOOLS) == Chat().body(p, TOOLS)


def test_a_profile_snapshot_keeps_headers_and_extra() -> None:
    p = profile(
        extra={
            "provider": {"order": ["a"], "preferred_min_throughput": 50, "sort": {"by": "price"}}
        },
        headers={"X-Title": "AIMS"},
    )
    loaded = Profile.model_validate(json.loads(json.dumps(p.snapshot())))
    assert loaded == p
    assert json.dumps(Chat().body(loaded, PROMPT)) == json.dumps(Chat().body(p, PROMPT))
