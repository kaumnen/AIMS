import asyncio
import time
from dataclasses import dataclass

from aims.events import RateLimit
from aims.models.profiles import Limits


class _Bucket:
    """A token bucket that refills `per_minute` tokens a minute, up to that many."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60
        self.level = self.capacity
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now

    def wait(self, n: float) -> float:
        self._refill()
        n = min(n, self.capacity)
        return 0.0 if self.level >= n else (n - self.level) / self.rate

    def take(self, n: float) -> None:
        self._refill()
        self.level -= n

    def cap(self, remaining: int) -> None:
        self._refill()
        self.level = min(self.level, float(remaining))


@dataclass(slots=True)
class Lease:
    group: Group
    tokens: int
    queued_ms: int
    released: bool = False

    def release(self, used_tokens: int | None = None) -> None:
        if self.released:
            return
        self.released = True
        if used_tokens is not None and self.group.tokens is not None:
            self.group.tokens.take(used_tokens - self.tokens)
        self.group.slots.release()


class Group:
    """The limits shared by the profiles of one group. Waiters are served in arrival order."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.slots = asyncio.Semaphore(limits.concurrency)
        self.requests = _Bucket(limits.rpm) if limits.rpm else None
        self.tokens = _Bucket(limits.tpm) if limits.tpm else None
        self.paused_until = 0.0
        self.turn = asyncio.Lock()

    async def acquire(self, tokens: int) -> Lease:
        start = time.monotonic()
        await self.slots.acquire()
        try:
            async with self.turn:
                while True:
                    wait = max(
                        self.paused_until - time.monotonic(),
                        self.requests.wait(1) if self.requests else 0.0,
                        self.tokens.wait(tokens) if self.tokens else 0.0,
                    )
                    if wait <= 0:
                        break
                    await asyncio.sleep(wait)
                if self.requests:
                    self.requests.take(1)
                if self.tokens:
                    self.tokens.take(tokens)
        except BaseException:
            self.slots.release()
            raise
        return Lease(self, tokens, int((time.monotonic() - start) * 1000))

    def observe(self, limit: RateLimit) -> None:
        """Lower the buckets to what the provider says is left, and pause on a retry-after."""
        if self.requests and limit.remaining_requests is not None:
            self.requests.cap(limit.remaining_requests)
        if self.tokens and limit.remaining_tokens is not None:
            self.tokens.cap(limit.remaining_tokens)
        if limit.retry_after_ms:
            until = time.monotonic() + limit.retry_after_ms / 1000
            self.paused_until = max(self.paused_until, until)


class Limiters:
    """Groups by name. Share one between clients that use the same provider accounts."""

    def __init__(self) -> None:
        self.groups: dict[str, Group] = {}

    def group(self, name: str, limits: Limits) -> Group:
        group = self.groups.get(name)
        if group is None:
            group = self.groups[name] = Group(limits)
        return group
