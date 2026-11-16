import asyncio
import bisect
import time
from collections.abc import Callable
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


class _Window:
    """At most `per_minute` requests in any 60 s: each request holds its place for a minute.
    A bucket that refills over the minute lets close to twice that through in the first one,
    which a provider's per-minute limit refuses."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = per_minute
        self.clock = clock
        self.starts: list[float] = []

    def _expire(self, now: float) -> None:
        del self.starts[: bisect.bisect_right(self.starts, now - 60)]

    def wait(self) -> float:
        now = self.clock()
        self._expire(now)
        if len(self.starts) < self.capacity:
            return 0.0
        return self.starts[len(self.starts) - self.capacity] + 60 - now

    def take(self) -> None:
        now = self.clock()
        self._expire(now)
        bisect.insort(self.starts, now)

    def cap(self, remaining: int, reset_ms: int | None) -> None:
        """Hold the places the provider says are gone until its `reset_ms`, at most a minute.
        Without one they free one by one over the minute, as the bucket refilled."""
        now = self.clock()
        self._expire(now)
        for i in range(1, self.capacity - len(self.starts) - max(remaining, 0) + 1):
            free = min(reset_ms / 1000, 60) if reset_ms is not None else i * 60 / self.capacity
            bisect.insort(self.starts, now + free - 60)


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


class Account:
    """The profiles that reach one base URL with one key env. A provider's limit often
    covers all of an account's models, so a rate-limit answer to one pauses them all."""

    def __init__(self) -> None:
        self.paused_until = 0.0

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, time.monotonic() + seconds)


class Group:
    """The limits shared by the profiles of one group. Waiters are served in arrival order."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.slots = asyncio.Semaphore(limits.concurrency)
        self.requests = _Window(limits.rpm) if limits.rpm else None
        self.tokens = _Bucket(limits.tpm) if limits.tpm else None
        self.turn = asyncio.Lock()

    async def acquire(self, tokens: int, account: Account | None = None) -> Lease:
        """A slot once the caps have room and `account` is not paused."""
        start = time.monotonic()
        await self.slots.acquire()
        try:
            async with self.turn:
                while True:
                    wait = max(
                        account.paused_until - time.monotonic() if account else 0.0,
                        self.requests.wait() if self.requests else 0.0,
                        self.tokens.wait(tokens) if self.tokens else 0.0,
                    )
                    if wait <= 0:
                        break
                    await asyncio.sleep(wait)
                if self.requests:
                    self.requests.take()
                if self.tokens:
                    self.tokens.take(tokens)
        except BaseException:
            self.slots.release()
            raise
        return Lease(self, tokens, int((time.monotonic() - start) * 1000))

    def observe(self, limit: RateLimit) -> None:
        """Lower the caps to what the provider says is left."""
        if self.requests and limit.remaining_requests is not None:
            self.requests.cap(limit.remaining_requests, limit.reset_requests_ms)
        if self.tokens and limit.remaining_tokens is not None:
            self.tokens.cap(limit.remaining_tokens)


class Limiters:
    """Groups by name and accounts by base URL and key env. Share one between clients that
    use the same provider accounts."""

    def __init__(self) -> None:
        self.groups: dict[str, Group] = {}
        self.accounts: dict[tuple[str, str | None], Account] = {}

    def group(self, name: str, limits: Limits) -> Group:
        group = self.groups.get(name)
        if group is None:
            group = self.groups[name] = Group(limits)
        return group

    def account(self, url: str, key_env: str | None) -> Account:
        account = self.accounts.get((url, key_env))
        if account is None:
            account = self.accounts[url, key_env] = Account()
        return account
