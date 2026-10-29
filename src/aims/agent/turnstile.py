import asyncio
from collections.abc import Iterable


class Turnstile:
    """Lets the replayed actions of a fork's agents run one at a time, in the order the origin
    ran them. Each action is named by its seq in the origin. An agent that ends before taking
    its actions gives them up with `drop`, so the others are not kept waiting."""

    def __init__(self, seqs: Iterable[int]) -> None:
        self._order = sorted(set(seqs))
        self._left = set(self._order)
        self._next = 0
        self._changed = asyncio.Condition()

    def holds(self, seq: int) -> bool:
        return seq in self._left

    async def wait(self, seq: int) -> None:
        """Wait until every action before `seq` was taken or given up; an action it was not
        given goes at once."""
        async with self._changed:
            await self._changed.wait_for(lambda: seq not in self._left or self._head() == seq)

    async def done(self, seq: int) -> None:
        await self.drop([seq])

    async def drop(self, seqs: Iterable[int]) -> None:
        async with self._changed:
            self._left.difference_update(seqs)
            self._changed.notify_all()

    def _head(self) -> int | None:
        while self._next < len(self._order) and self._order[self._next] not in self._left:
            self._next += 1
        return self._order[self._next] if self._next < len(self._order) else None
