"""The purse's holds: the room the monitor's calls take in its budget while they are under way."""

import asyncio

import pytest
from scripted import profile

from aims.models import Message, Prompt, Reply, Turn
from aims.models.purse import PLANNED_OUTPUT, Hold, Purse

pytestmark = pytest.mark.anyio

PROMPT = Prompt(items=[Message(role="user", content="x")])
# Input is free, so a call is planned at its output cap alone: 100 tokens at $1 per million.
CAPPED = profile(max_output_tokens=100, price={"input": 0.0, "cached": 0.0, "output": 1.0})


def reply(cost_usd: float, unseen_usd: float = 0.0) -> Reply:
    return Reply(
        call_id="c",
        profile="p",
        status="completed",
        attempts=1,
        turn=Turn(),
        cost_usd=cost_usd,
        unseen_usd=unseen_usd,
    )


async def held(purse: Purse) -> Hold:
    hold = await purse.hold(CAPPED, PROMPT)
    assert hold is not None
    return hold


async def test_a_call_waits_for_calls_under_way_when_it_fits_only_without_them() -> None:
    purse = Purse(0.0005, keep=0.0002)
    first, second, third = [await held(purse) for _ in range(3)]
    assert (first.usd, purse.held) == (0.0001, 0.0003)
    fourth = asyncio.ensure_future(purse.hold(CAPPED, PROMPT))
    await asyncio.sleep(0.01)
    assert not fourth.done()
    first.settle(reply(cost_usd=0.00005, unseen_usd=0.00001))
    await asyncio.sleep(0.01)
    # $0.00004 is free beside the $0.0002 still held: too little, but enough once one settles
    assert not fourth.done()
    second.settle()
    hold = await asyncio.wait_for(fourth, 1)
    assert hold is not None
    assert (purse.spent, purse.unseen, purse.held) == (0.00005, 0.00001, 0.0002)
    third.settle(reply(cost_usd=0.0001))
    third.settle(reply(cost_usd=0.0001))
    hold.settle(reply(cost_usd=0.0001))
    assert (purse.spent, purse.held) == (0.00025, 0.0)
    # $0.00004 is left beside the reserve: a call that does not fit alone is never sent
    assert await purse.hold(CAPPED, PROMPT) is None


async def test_a_held_call_is_tried_again_only_while_another_attempt_fits() -> None:
    purse = Purse(0.001, keep=0.0005)
    hold = await held(purse)
    await held(purse)
    assert hold.cap is not None
    assert hold.cap.attempt_usd == 0.0001
    # $0.0004 is left beside the other call: what this call may have cost and one attempt more
    assert hold.cap.fits(0.0003)
    assert not hold.cap.fits(0.00031)
    purse.add_cost(0.0005)
    # the room let the call start, so a retry after attempts that cost nothing always fits
    assert hold.cap.fits(0.0)
    assert not hold.cap.fits(0.00001)


async def test_an_unpriced_call_is_held_only_by_what_was_counted() -> None:
    unpriced = profile(price=None)
    purse = Purse(0.001, keep=0.0005)
    hold = await purse.hold(unpriced, PROMPT)
    assert hold is not None
    assert (hold.usd, hold.cap, purse.held) == (0.0, None, 0.0)
    purse.add_cost(0.0005)
    assert await purse.hold(unpriced, PROMPT) is None


async def test_a_call_with_no_output_cap_is_planned_at_the_planned_output() -> None:
    uncapped = profile(price={"input": 0.0, "cached": 0.0, "output": 1.0})
    planned = PLANNED_OUTPUT / 1_000_000
    assert await Purse(planned - 1e-9).hold(uncapped, PROMPT) is None
    hold = await Purse(planned).hold(uncapped, PROMPT)
    assert hold is not None
    assert hold.usd == planned


async def test_no_budget_holds_nothing() -> None:
    purse = Purse(None)
    hold = await held(purse)
    assert (hold.usd, hold.cap, purse.held) == (0.0, None, 0.0)
    hold.settle(reply(cost_usd=0.5))
    assert purse.spent == 0.5
