"""The losses a test of the kernel tracer can check as its own."""

from aims.events import LocalEventStore

# The tracer reports each probe run the kernel skipped to every sandbox open at the time, as it
# cannot tell whose records the skip hid. A BPF map call or BPF program running on a cpu, from
# any process on the host, makes that cpu's tracepoint probes skip.
SKIPPED = "kernel skipped tracer probe"


async def lost_records(store: LocalEventStore, run_id: str) -> list[str]:
    """The reasons of the run's losses, but for the kernel skipping a probe run."""
    return [
        reason
        for e in await store.events(run_id, limit=1000, types=["audit.lost"])
        if not (reason := str(e.data.get("reason"))).startswith(SKIPPED)
    ]
