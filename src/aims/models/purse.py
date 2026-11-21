"""What the calls under one spend cap may still cost: the checks of a score, and the sweeps
and reviews of a monitor."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aims.models.profiles import Price, Profile
from aims.models.types import CHARS_PER_TOKEN, Prompt, Reply, SpendCap

MILLION = 1_000_000
# The least output a check call is worth: its answer is a short JSON object.
MIN_OUTPUT = 512
# The output a call of a profile with no output cap is planned at under a spend cap. The score
# sends it as the call's cap. The monitor never changes what it sends, so for its calls the
# figure is a plan, not a bound. Models with the smallest output limits in use still take it.
PLANNED_OUTPUT = 4096


def input_usd(price: Price, profile: Profile, prompt: Prompt) -> float:
    """What `prompt` costs as input at `price`, every token uncached, counted by its size and
    at most a full context of `profile`."""
    sent: list[Any] = [*prompt.items, *prompt.tools, *filter(None, [prompt.output_schema])]
    given = sum(len(x.model_dump_json()) for x in sent) // CHARS_PER_TOKEN
    if profile.context_tokens is not None:
        given = min(given, profile.context_tokens)
    return given * price.input / MILLION


@dataclass(slots=True)
class Purse:
    """What calls under one spend cap may spend: `budget` USD, None for no cap, of which
    `spent` went. `unseen` is what failed calls that may have been billed with no cost reported
    count at, `sized` what answers that came with no token counts count at by their size, both
    against the budget only. `stopped` says why the score's checks stopped, None while they go
    on. `keep` is what calls let through by `hold` leave for a later part, and `held` what the
    calls under way may cost."""

    budget: float | None
    spent: float = 0.0
    unseen: float = 0.0
    sized: float = 0.0
    stopped: str | None = None
    keep: float = 0.0
    held: float = 0.0
    _freed: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    @property
    def counted(self) -> float:
        """What the calls count against the budget: what they spent, what failed calls with
        no cost reported and answers with no token counts count at."""
        return self.spent + self.unseen + self.sized

    def room(self, profile: Profile, prompt: Prompt) -> int | None:
        """The output cap the next check of `profile` with `prompt` gets, so the checks stay
        within the budget with its input priced uncached; None to keep the profile's. A profile
        with no output cap gets at most PLANNED_OUTPUT. Stops the checks when the call does not
        fit. An unpriced call is held only by the costs its provider reports."""
        if self.stopped is not None or self.budget is None:
            return None
        left = self.budget - self.counted
        price = profile.price
        if price is None:
            if left <= 0:
                self._stop()
            return None
        left -= input_usd(price, profile, prompt)
        if price.output <= 0:
            if left < 0:
                self._stop()
            return None
        out = int(left / price.output * MILLION)
        cap = profile.max_output_tokens
        limit = cap or PLANNED_OUTPUT
        if out < min(MIN_OUTPUT, limit):
            self._stop()
            return None
        if out < limit:
            return out
        return None if cap else limit

    def cap(self, profile: Profile, prompt: Prompt) -> SpendCap | None:
        """How a check of `profile`, as sent with its output cap, and `prompt` is tried again:
        only while another attempt at that cap fits beside what its attempts so far may have
        cost. None with no budget or no price."""
        price, budget = profile.price, self.budget
        if budget is None or price is None:
            return None
        given, planned = input_usd(price, profile, prompt), profile.max_output_tokens

        def fits(owed: float) -> bool:
            left = budget - self.counted - owed
            left -= given
            if price.output <= 0 or planned is None:
                return left >= 0
            return int(left / price.output * MILLION) >= planned

        attempt = round(given + (planned or 0) * price.output / MILLION, 10)
        return SpendCap(attempt_usd=attempt, fits=fits)

    def charge(self, reply: Reply, *, no_room: bool = False) -> None:
        """Add what `reply` cost and what its unseen attempts and an answer with no token
        counts count at. The checks stop once a call had no room to be tried again, or past the
        budget, which an unpriced call, a provider's own figure or an answer's size can bring."""
        self.add(reply)
        over = self.budget is not None and self.counted > self.budget
        if self.stopped is None and (no_room or over):
            self._stop()

    def add(self, reply: Reply) -> None:
        """Add what `reply` cost and what its unseen attempts and an answer with no token
        counts count at."""
        self.spent = round(self.spent + (reply.cost_usd or 0.0), 10)
        self.unseen = round(self.unseen + reply.unseen_usd, 10)
        self.sized = round(self.sized + reply.sized_usd, 10)

    def add_cost(self, usd: float) -> None:
        """Add `usd` that a part with its own cap spent, such as the monitor's judge."""
        self.spent = round(self.spent + usd, 10)

    async def hold(self, profile: Profile, prompt: Prompt) -> Hold | None:
        """Room for one call of `profile` with `prompt` as it is sent, at its output cap or at
        PLANNED_OUTPUT with none, its input priced uncached. The room is held until the call
        settles, so calls under way at once cannot pass the budget together. A call that fits
        only once calls under way settle waits for them. None when it does not fit beside what
        was counted and `keep`: the call is then not sent. An unpriced call is held only by the
        costs its provider reports. With no budget nothing is held."""
        budget, price = self.budget, profile.price
        if budget is None:
            return Hold(self, 0.0, None)
        attempt = 0.0
        if price is not None:
            made = (profile.max_output_tokens or PLANNED_OUTPUT) * price.output / MILLION
            attempt = round(input_usd(price, profile, prompt) + made, 10)
        while True:
            left = budget - self.keep - self.counted
            free = left - self.held
            if free > 0 if price is None else attempt <= free:
                self.held = round(self.held + attempt, 10)
                return Hold(self, attempt, None if price is None else self._retry(attempt))
            alone = left > 0 if price is None else attempt <= left
            if not alone or self.held <= 0:
                return None
            await self._freed.wait()

    def _retry(self, attempt: float) -> SpendCap:
        """How a held call planned at `attempt` USD is tried again: only while another attempt
        fits beside what its attempts so far may have cost and what other calls hold. The room
        let the call start, so a retry after attempts that cost nothing always fits."""

        def fits(owed: float) -> bool:
            assert self.budget is not None
            left = self.budget - self.keep - self.counted - (self.held - attempt)
            return owed + attempt <= max(left, attempt)

        return SpendCap(attempt_usd=attempt, fits=fits)

    def _free(self, usd: float) -> None:
        self.held = max(0.0, round(self.held - usd, 10))
        self._freed.set()
        self._freed = asyncio.Event()

    def _stop(self) -> None:
        self.stopped = f"the score spent ${self.spent:.4f} of its ${self.budget:g}"
        if self.unseen:
            self.stopped += f"; failed calls with no cost reported may add ${self.unseen:.4f}"
        if self.sized:
            self.stopped += f"; answers with no token counts count at ${self.sized:.4f}"


@dataclass(slots=True)
class Hold:
    """The room one call holds in `purse`, `usd` at its planned cost, until it settles. `cap`
    is how the call is tried again, None for an unpriced one."""

    purse: Purse
    usd: float
    cap: SpendCap | None
    settled: bool = False

    def settle(self, reply: Reply | None = None) -> None:
        """Free the room and add what `reply` cost; only the first time counts. With no reply,
        a call that raised or was cancelled, only the room is freed."""
        if self.settled:
            return
        self.settled = True
        if reply is not None:
            self.purse.add(reply)
        self.purse._free(self.usd)
