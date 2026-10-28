"""Replay of recorded provider runs, and the live calls that record them.

The fixtures under tests/fixtures/models are real runs. Set AIMS_RECORD_FIXTURES=1 to record
them again and AIMS_LIVE_MODELS=1 to run the provider checks; both need the API keys.
"""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from aims.events import (
    Draft,
    EventStore,
    LocalEventStore,
    ModelResponse,
    RunFinished,
    RunStarted,
)
from aims.models import (
    DeltaBus,
    Message,
    ModelClient,
    Overrides,
    Prompt,
    Recording,
    ReplayError,
    Reply,
    ToolOutput,
    load_models,
)
from aims.models.check import VERDICT, WEATHER, check_profile

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "models"
LIVE = bool(os.environ.get("AIMS_LIVE_MODELS"))
RECORD = bool(os.environ.get("AIMS_RECORD_FIXTURES"))
SMALL = Overrides(effort="low", max_output_tokens=2048)
KINDS = {
    "responses": "meta-spark-1.3-contributor",
    "chat": "fireworks-spark-open",
    "retry": "fireworks-spark-open",
    "cancel": "meta-spark-1.3-contributor",
}


def system(text: str) -> list[Message]:
    return [Message(role="system", content="Answer briefly."), Message(role="user", content=text)]


async def script(client: ModelClient, name: str) -> list[Reply]:
    """A tool round trip, streamed, then a structured answer without streaming."""
    profile = client.config.get(name)
    prompt = Prompt(items=system("What is the weather in Paris? Use the tool."), tools=[WEATHER])
    first = await client.call(profile, prompt, overrides=SMALL)
    outputs = [ToolOutput(call_id=c.id, output="18C and sunny") for c in first.tool_calls]
    items = [*prompt.items, first.turn, *outputs]
    second = await client.call(profile, prompt.model_copy(update={"items": items}), overrides=SMALL)
    plain = profile.model_copy(update={"stream": False})
    structured = Prompt(items=system("What is 12 + 30? Fill in the answer."), output_schema=VERDICT)
    third = await client.call(plain, structured, overrides=SMALL)
    return [first, second, third]


async def plain_call(client: ModelClient, name: str) -> Reply:
    prompt = Prompt(items=system("What is 17 + 25? Reply with the number only."))
    return await client.call(name, prompt, overrides=SMALL)


async def cancelled_call(client: ModelClient, name: str, bus: DeltaBus) -> None:
    prompt = Prompt(items=system("Count from 1 to 200, one number per line."))
    with bus.subscribe() as sub:
        task = asyncio.create_task(client.call(name, prompt, overrides=SMALL))
        async for delta in sub:
            if delta.kind in ("text", "reasoning"):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class FirstRateLimited(httpx2.AsyncBaseTransport):
    """Answers the first request with a 429, then sends the rest to the provider."""

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.limited = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if not self.limited:
            self.limited = True
            body = json.dumps({"error": {"message": "rate limited", "type": "rate_limit"}})
            headers = {"content-type": "application/json", "retry-after": "0"}
            return httpx2.Response(429, headers=headers, content=body.encode())
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def open_fixture(kind: str, root: Path) -> tuple[LocalEventStore, str]:
    source = FIXTURES / kind
    runs = [p for p in source.iterdir() if (p / "events.jsonl").is_file()]
    assert len(runs) == 1, f"{source} holds {len(runs)} runs"
    shutil.copytree(runs[0], root / runs[0].name)
    return await LocalEventStore.open(root), runs[0].name


@pytest.fixture
async def replay_store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


async def replaying(kind: str, tmp_path: Path) -> AsyncIterator[tuple[ModelClient, str, str]]:
    store, source = await open_fixture(kind, tmp_path / "runs")
    try:
        run_id = await store.create_run(RunStarted(scenario="replay", oversight="observe"))
        recording = await Recording.load(store, source)
        async with ModelClient(store, run_id, load_models(), replay=recording, env={}) as client:
            yield client, source, run_id
        assert recording.remaining() == 0
    finally:
        await store.close()


async def recorded(store: EventStore, run_id: str) -> list[ModelResponse]:
    events = await store.events(run_id, types=["model.response"])
    return [r for e in events if isinstance(r := e.payload(), ModelResponse)]


@pytest.mark.parametrize("kind", ["responses", "chat"])
async def test_replay_of_a_recorded_run(kind: str, tmp_path: Path) -> None:
    async for client, source, run_id in replaying(kind, tmp_path):
        replies = await script(client, KINDS[kind])
        assert [r.status for r in replies] == ["completed"] * 3
        assert replies[0].tool_calls
        assert json.loads(replies[2].text)["answer"] == 42
        theirs = await recorded(client.store, source)
        ours = await recorded(client.store, run_id)
        assert [r.text for r in ours] == [r.text for r in theirs]
        assert [r.usage for r in ours] == [r.usage for r in theirs]
        assert [r.reasoning_kind for r in ours] == [r.reasoning_kind for r in theirs]
        assert {r.source for r in ours} == {"replay"}
        if kind == "chat":
            assert replies[0].reasoning_kind == "full"
            assert replies[0].turn.reasoning


async def test_replay_of_a_rate_limited_call(tmp_path: Path) -> None:
    async for client, _, run_id in replaying("retry", tmp_path):
        reply = await plain_call(client, KINDS["retry"])
        assert reply.attempts == 2
        first, second = await recorded(client.store, run_id)
        assert (first.http_status, first.retry, second.status) == (429, True, "completed")


async def test_replay_of_a_cancelled_call(tmp_path: Path) -> None:
    async for client, source, run_id in replaying("cancel", tmp_path):
        (theirs,) = await recorded(client.store, source)
        assert theirs.status == "cancelled"
        prompt = Prompt(items=system("Count from 1 to 200, one number per line."))
        with pytest.raises(ReplayError, match="cancelled by its caller"):
            await client.call(KINDS["cancel"], prompt, overrides=SMALL)
        (ours,) = await recorded(client.store, run_id)
        assert ours.error_kind == "replay"


@pytest.mark.skipif(not LIVE, reason="set AIMS_LIVE_MODELS=1 to call the providers")
@pytest.mark.parametrize("name", sorted(set(KINDS.values())))
async def test_live_provider_checks(name: str, replay_store: LocalEventStore) -> None:
    run_id = await replay_store.create_run(RunStarted(scenario="live", oversight="observe"))
    models = load_models()
    async with ModelClient(replay_store, run_id, models) as client:
        checks = await check_profile(client, models.get(name))
    failed = [c for c in checks if c.verdict == "fail"]
    assert not failed, failed


@pytest.mark.skipif(not RECORD, reason="set AIMS_RECORD_FIXTURES=1 to record the fixtures")
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_record_fixture(kind: str, tmp_path: Path) -> None:
    models = load_models()
    bus = DeltaBus()
    transport = FirstRateLimited() if kind == "retry" else None
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(RunStarted(scenario=f"fixture.{kind}", oversight="observe"))
        async with ModelClient(store, run_id, models, bus=bus, transport=transport) as client:
            match kind:
                case "responses" | "chat":
                    await script(client, KINDS[kind])
                case "retry":
                    await plain_call(client, KINDS[kind])
                case "cancel":
                    await cancelled_call(client, KINDS[kind], bus)
        await store.append(run_id, [Draft(RunFinished(status="completed"))])
        target = FIXTURES / kind
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        shutil.copytree(tmp_path / "runs" / run_id, target / run_id)
