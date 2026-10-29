import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx2

from aims.events import Draft, EventStore, RunFinished, RunStarted, RunStatus
from aims.models.client import ModelCallError, ModelClient
from aims.models.profiles import ModelsConfig, Overrides, Profile
from aims.models.types import Message, OutputSchema, Prompt, Reply, ToolOutput, ToolSpec

Verdict = Literal["ok", "warn", "fail"]

WEATHER = ToolSpec(
    name="get_weather",
    description="Current weather for one city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)
VERDICT = OutputSchema(
    name="verdict",
    json_schema={
        "type": "object",
        "properties": {"answer": {"type": "integer"}, "sure": {"type": "boolean"}},
        "required": ["answer", "sure"],
        "additionalProperties": False,
    },
)
SMALL = Overrides(effort="low", max_output_tokens=2048)


@dataclass(frozen=True, slots=True)
class Check:
    profile: str
    name: str
    verdict: Verdict
    detail: str


def _prompt(text: str, **kw: object) -> Prompt:
    items = [Message(role="system", content="Answer briefly."), Message(role="user", content=text)]
    return Prompt.model_validate({"items": items, **kw})


async def check_profile(client: ModelClient, profile: Profile) -> list[Check]:
    """Smoke tests of one provider: plain, non-streamed, tool round trip, structured output."""
    small = SMALL if profile.effort is not None else Overrides(max_output_tokens=2048)
    out: list[Check] = []

    async def run(name: str, step: Callable[[], Awaitable[tuple[Verdict, str]]]) -> None:
        try:
            verdict, detail = await step()
        except ModelCallError as e:
            verdict, detail = "fail", str(e)
        out.append(Check(profile.name, name, verdict, detail))

    async def call(p: Profile, prompt: Prompt) -> Reply:
        return await client.call(p, prompt, overrides=small)

    async def plain() -> tuple[Verdict, str]:
        r = await call(profile, _prompt("What is 17 + 25? Reply with the number only."))
        if r.status != "completed" or "42" not in r.text:
            return "fail", f"status {r.status}, text {r.text[:80]!r}"
        u = r.usage
        missing = [k for k, v in u.model_dump().items() if v is None]
        if missing:
            return "warn", f"usage lacks {', '.join(missing)}"
        detail = f"usage {u.input_tokens}/{u.cached_tokens}/{u.output_tokens}/{u.reasoning_tokens}"
        return _reasoning(profile, r, detail)

    async def unstreamed() -> tuple[Verdict, str]:
        p = profile.model_copy(update={"stream": not profile.stream})
        r = await call(p, _prompt("What is 6 times 7? Reply with the number only."))
        ok = r.status == "completed" and "42" in r.text
        mode = "streamed" if p.stream else "not streamed"
        return ("ok", mode) if ok else ("fail", f"{mode}: status {r.status}, {r.text[:80]!r}")

    async def tools() -> tuple[Verdict, str]:
        prompt = _prompt("What is the weather in Paris and in Rome? Use the tool.", tools=[WEATHER])
        r = await call(profile, prompt)
        if r.status != "completed" or not r.tool_calls:
            return "fail", f"status {r.status}, no tool call, text {r.text[:80]!r}"
        first = r.tool_calls[0]
        try:
            json.loads(first.arguments)
        except ValueError:
            return "fail", f"arguments are not JSON: {first.arguments[:80]!r}"
        items = [*prompt.items, r.turn]
        items += [ToolOutput(call_id=c.id, output="18C and sunny") for c in r.tool_calls]
        r2 = await call(profile, prompt.model_copy(update={"items": items}))
        if r2.status != "completed":
            return "fail", f"round trip status {r2.status}"
        calls = len(r.tool_calls)
        if not profile.parallel_tool_calls and calls > 1:
            return "warn", f"{calls} calls in one turn with parallel_tool_calls off"
        return "ok", f"{calls} call(s), round trip {r2.status}"

    async def structured() -> tuple[Verdict, str]:
        if not profile.structured_output:
            return "warn", "not tried: the profile sets structured_output = false"
        prompt = _prompt("What is 12 + 30? Fill in the answer.", output_schema=VERDICT)
        r = await call(profile, prompt)
        try:
            value = json.loads(r.text)
        except ValueError:
            return "fail", f"not JSON: {r.text[:80]!r}"
        if not isinstance(value, dict) or set(value) != {"answer", "sure"}:
            return "fail", f"does not match the schema: {r.text[:80]!r}"
        return ("ok", r.text) if value["answer"] == 42 else ("warn", f"wrong answer {r.text}")

    await run("plain", plain)
    await run("unstreamed", unstreamed)
    await run("tools", tools)
    await run("structured", structured)
    return out


def _reasoning(profile: Profile, r: Reply, detail: str) -> tuple[Verdict, str]:
    got = r.reasoning_kind
    if got == profile.reasoning:
        return "ok", f"{detail}, reasoning {got}"
    if got == "none" and profile.reasoning == "summary":
        return "ok", f"{detail}, no summary for a short answer"
    return "warn", f"{detail}, reasoning {got}, profile says {profile.reasoning}"


async def record_check(
    store: EventStore,
    models: ModelsConfig,
    chosen: Sequence[Profile],
    *,
    run_id: str | None = None,
    on_check: Callable[[Check], None] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[str, list[Check]]:
    """Check each profile in `chosen`, recorded as a `models.check` run."""
    config = {"models": models.snapshot(p.name for p in chosen)}
    started = RunStarted(
        scenario="models.check", oversight="observe", config=config, kind="models.check"
    )
    run_id = await store.create_run(started, run_id=run_id)
    checks: list[Check] = []
    status: RunStatus = "failed"
    try:
        async with ModelClient(store, run_id, models, transport=transport) as client:
            for profile in chosen:
                for c in await check_profile(client, profile):
                    checks.append(c)
                    if on_check is not None:
                        on_check(c)
        status = "failed" if any(c.verdict == "fail" for c in checks) else "completed"
    finally:
        await store.append(run_id, [Draft(RunFinished(status=status))])
    return run_id, checks
