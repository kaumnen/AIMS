"""Ship the seeded run that is still going and stay connected as its runner, so the control plane
shows it live: `uv run python scripts/live.py <seed dir> <run id>`. Uses the `aims ui` that
server.json names. Prints `live` once the control plane holds the whole log, then keeps the
runner stream open and reports in until stopped. The run never moves on.
"""

import argparse
import asyncio
import json
from pathlib import Path

from aims import __version__
from aims.events import LocalEventStore
from aims.node.client import ControlClient
from aims.node.link import arch
from aims.node.shipper import Shipper
from aims.server.serve import SERVER_JSON, server_dir
from aims.wire import RunnerHello, RunnerStatus, TracerHealth

NAME = "stand-in"
TRACER = TracerHealth(alive=False, detail="stand-in for the seeded live run")


async def main(root: Path, run_id: str) -> None:
    info = json.loads((server_dir() / SERVER_JSON).read_text())
    client = ControlClient(str(info["url"]), str(info["token"]), runner=NAME)
    try:
        await client.hello(
            RunnerHello(name=NAME, version=__version__, arch=arch(), tracer=TRACER, slots=0)
        )
        async with await LocalEventStore.open(root) as store:
            head = await store.head(run_id)
            if head is None:
                raise SystemExit(f"no run {run_id} in {root}")
            shipping = asyncio.create_task(Shipper(store, client, run_id).run())
            while True:
                there = (await client.heads([run_id])).get(run_id)
                if there is not None and there.seq >= head.seq:
                    break
                if shipping.done():
                    shipping.result()
                    raise SystemExit("shipping stopped before the log arrived")
                await asyncio.sleep(0.1)
            # An open stream shows the runner connected; commands it brings wait unanswered.
            listening = asyncio.create_task(listen(client))
            print("live", flush=True)
            # A runner not heard from in a minute shows as offline.
            while not listening.done():
                await client.status(RunnerStatus(tracer=TRACER, free=0, running=[run_id]))
                await asyncio.sleep(10)
            listening.result()
    finally:
        await client.aclose()


async def listen(client: ControlClient) -> None:
    async with client.stream() as source:
        async for _ in source:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="The directory seed.py wrote.")
    parser.add_argument("run", help="The id of the run still going.")
    args = parser.parse_args()
    asyncio.run(main(args.root, args.run))
