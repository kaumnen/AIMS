"""A chat model that answers from a script, for agent and run tests."""

import itertools
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2

from aims.models import Limits, ModelsConfig, Profile, Retries, Roles

_groups = itertools.count()

Turn = dict[str, Any]


def profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "scripted",
        "backend": "chat",
        "base_url": "http://model.invalid/v1",
        "model": "m",
        "weights": "w",
        "reasoning": "full",
        "stream": False,
        "limits": Limits(group=f"scripted{next(_groups)}"),
        "retries": Retries(attempts=1, backoff=0, max=0),
        "price": {"input": 1.0, "cached": 0.1, "output": 2.0},
    }
    return Profile.model_validate(base | kw)


def models(*profiles: Profile) -> ModelsConfig:
    first = profiles[0].name
    return ModelsConfig(
        profiles={p.name: p for p in profiles}, roles=Roles(agent=first, sweep=first, judge=first)
    )


def call(name: str, **args: Any) -> Turn:
    return {"tool": name, "args": args}


def say(text: str) -> Turn:
    return {"text": text}


def raw_call(name: str, arguments: str) -> Turn:
    return {"tool": name, "raw": arguments}


class _Body(httpx2.AsyncByteStream):
    """A body the recorder sees arrive, unlike `content=`, which httpx reads up front."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def respond(status: int, value: Any) -> httpx2.Response:
    headers = {"content-type": "application/json"}
    return httpx2.Response(status, headers=headers, stream=_Body(json.dumps(value).encode()))


class Script:
    """Answers each request with the next turn. `fail` turns answer with an HTTP error."""

    def __init__(self, *turns: Turn | Callable[[dict[str, Any]], Turn]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.calls = itertools.count(1)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.turns:
            return respond(500, {"error": {"message": "script ran out"}})
        item = self.turns.pop(0)
        turn: Turn = item if isinstance(item, dict) else item(body)
        if "status" in turn:
            return respond(turn["status"], {"error": {"message": turn["error"]}})
        message: dict[str, Any] = {"role": "assistant", "content": turn.get("text", "")}
        finish = "stop"
        if "tool" in turn:
            n = next(self.calls)
            arguments = turn.get("raw", json.dumps(turn.get("args", {})))
            message["tool_calls"] = [
                {
                    "id": f"call_{n}",
                    "type": "function",
                    "function": {"name": turn["tool"], "arguments": arguments},
                }
            ]
            finish = "tool_calls"
        usage = turn.get("usage", {"prompt_tokens": 100, "completion_tokens": 10})
        return respond(
            200,
            {
                "id": f"r{len(self.requests)}",
                "object": "chat.completion",
                "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            },
        )
