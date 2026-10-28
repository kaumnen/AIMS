import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Self

DeltaKind = Literal["start", "text", "reasoning", "tool_call", "tool_args", "end"]


@dataclass(frozen=True, slots=True)
class Delta:
    """A piece of a model answer as it streams. `end` carries the attempt's status as text.

    Deltas are not events: the log keeps the raw stream and the assembled answer.
    """

    run_id: str
    agent_id: str | None
    call_id: str
    attempt: int
    kind: DeltaKind
    text: str = ""
    index: int | None = None
    ms: int = 0


@dataclass(eq=False, slots=True)
class Subscription:
    bus: DeltaBus
    run_id: str | None
    queue: asyncio.Queue[Delta]
    dropped: int = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop receiving. Deltas already queued are still read."""
        self.bus.subscribers.discard(self)
        self.queue.shutdown()

    async def __aiter__(self) -> AsyncIterator[Delta]:
        while True:
            try:
                yield await self.queue.get()
            except asyncio.QueueShutDown:
                return


class DeltaBus:
    """In-process fan-out of deltas. A subscriber whose queue is full loses the delta, counted
    in `dropped`, so a slow reader never holds up a model call."""

    def __init__(self, maxsize: int = 4096) -> None:
        self.maxsize = maxsize
        self.subscribers: set[Subscription] = set()

    def subscribe(self, run_id: str | None = None) -> Subscription:
        sub = Subscription(self, run_id, asyncio.Queue(self.maxsize))
        self.subscribers.add(sub)
        return sub

    def publish(self, delta: Delta) -> None:
        for sub in tuple(self.subscribers):
            if sub.run_id is not None and sub.run_id != delta.run_id:
                continue
            try:
                sub.queue.put_nowait(delta)
            except asyncio.QueueFull:
                sub.dropped += 1
            except asyncio.QueueShutDown:
                pass
