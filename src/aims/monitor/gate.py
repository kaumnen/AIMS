from collections.abc import Callable
from typing import TYPE_CHECKING

from aims.agent import Gate, GatePoint, OpenGate, PauseClock, Verdict

if TYPE_CHECKING:
    from aims.monitor.core import Monitor


class MonitorGate:
    """The monitor's place in the gate chain. Every gate point waits at the monitor's barrier,
    so a pause the signals decide lands before the agent moves on; with `hold`, an agent also
    waits before each tool call until the sweep due has scored it, on its pause `clock` so the
    wait does not count against its wall limit. The inner gate decides the rest. The monitor acts
    through the runner, never through a verdict of its own."""

    def __init__(
        self,
        monitor: Monitor,
        inner: Gate | None = None,
        *,
        hold: bool = False,
        clock: Callable[[str], PauseClock] | None = None,
    ) -> None:
        self.monitor = monitor
        self.inner = inner or OpenGate()
        self.hold = hold
        self.clock = clock

    async def check(self, point: GatePoint) -> Verdict:
        await self.monitor.barrier()
        if self.hold and point.kind == "before_tool":
            clock = None if self.clock is None else self.clock(point.agent_id)
            if clock is not None:
                clock.hold("monitor-hold")
            try:
                await self.monitor.hold(point)
            finally:
                if clock is not None:
                    clock.release("monitor-hold")
        return await self.inner.check(point)

    async def held(self, point: GatePoint) -> Verdict:
        return await self.inner.held(point)
