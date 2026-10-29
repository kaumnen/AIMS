import asyncio
import json
import logging
import random
from collections.abc import Sequence

import httpx2

from aims.events import LocalEventStore
from aims.events.chain import Head as LogHead
from aims.events.schema import blob_refs
from aims.node.client import ApiError, ControlClient
from aims.wire import Head

log = logging.getLogger("aims.node")

COALESCE_S = 0.05
MAX_EVENTS = 1000
MAX_BYTES = 4 * 1024 * 1024
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 30.0
IDLE_S = 5.0


class Backoff:
    """Waits that double from 0.5 s to 30 s, with jitter; a server's Retry-After wins."""

    def __init__(self) -> None:
        self.next = BACKOFF_MIN_S

    def reset(self) -> None:
        self.next = BACKOFF_MIN_S

    async def wait(self, retry_after: float | None = None) -> None:
        if retry_after is not None:
            delay = min(retry_after, 300.0)
        else:
            delay = self.next * random.uniform(0.5, 1.0)
            self.next = min(self.next * 2, BACKOFF_MAX_S)
        await asyncio.sleep(delay)


class Diverged(Exception):
    pass


class Shipper:
    """Copies one run's log and blobs to the control plane as the log grows: blobs first, then
    exact lines from where the server's copy ends. Once the server holds the sealed run, sends
    the run's truth file and its labels, each by its own path, and ends. Local writes never
    wait for it."""

    def __init__(self, store: LocalEventStore, client: ControlClient, run_id: str) -> None:
        self.store = store
        self.client = client
        self.run_id = run_id
        self.server = Head()
        self.local = LogHead()
        self.sent: set[str] = set()
        self.done = False
        self.diverged: str | None = None
        self.refused: str | None = None
        self.batches = 0
        self.lines = 0
        self.truthful = False
        self.truth_refused: str | None = None

    async def run(self) -> None:
        backoff = Backoff()
        while not self.done or not self.truthful:
            try:
                await self._resume()
                while not self.done:
                    changed = self.store.changed()
                    if await self._ship():
                        backoff.reset()
                        continue
                    if self.done:
                        break
                    try:
                        async with asyncio.timeout(IDLE_S):
                            await changed.wait()
                    except TimeoutError:
                        continue
                    await asyncio.sleep(COALESCE_S)
                await self._truth()
                await self._labels()
            except Diverged as e:
                self.diverged = str(e)
                log.error("run %s: the control plane's copy differs: %s", self.run_id, e)
                return
            except ApiError as e:
                if not e.retryable and e.status != 409:
                    self.refused = str(e)
                    log.error("run %s: shipping refused: %s", self.run_id, e)
                    return
                await backoff.wait(e.retry_after)
            except (httpx2.TransportError, OSError) as e:
                log.debug("run %s: control plane unreachable: %s", self.run_id, e)
                await backoff.wait()

    async def _resume(self) -> None:
        """Find where the server's copy ends and the same place in the local log."""
        heads = await self.client.heads([self.run_id])
        server = heads.get(self.run_id, Head())
        local = await self._seek(server.seq) if server.seq >= 0 else LogHead()
        if server.seq >= 0 and local.hash != server.hash:
            raise Diverged(f"event {server.seq} has another hash there")
        self.server, self.local = server, local
        self.done = local.finished
        self.truthful = server.truth

    async def _labels(self) -> None:
        """Send the labels people gave the run's flags; the server keeps those it lacks."""
        if not self.store.labels_path(self.run_id).is_file():
            return
        labels = await self.store.label_history(self.run_id)
        try:
            await self.client.post_labels(self.run_id, labels)
        except ApiError as e:
            if e.retryable:
                raise
            log.warning("run %s: labels not sent: %s", self.run_id, e)

    async def _truth(self) -> None:
        """Send the truth file once the server holds the sealed run; a run without one is
        done."""
        if self.truthful:
            return
        path = self.store.truth_path(self.run_id)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            self.truthful = True
            return
        try:
            await self.client.put_truth(self.run_id, data)
        except ApiError as e:
            if e.retryable:
                raise
            self.truth_refused = str(e)
            log.error("run %s: the control plane refused its truth file: %s", self.run_id, e)
        self.truthful = True

    async def _seek(self, seq: int) -> LogHead:
        """The local head after event `seq`; the server holding events this log lacks is a
        divergence."""
        try:
            return await self.store.seek(self.run_id, seq)
        except KeyError:
            raise Diverged(f"the control plane has event {seq}, past the end of this log") from None

    async def _ship(self) -> bool:
        """Send one batch. False when there was nothing new."""
        head = await self.store.head(self.run_id)
        if head is None or head.seq <= self.local.seq:
            self.done = self.local.finished
            return False
        lines, after = await self.store.lines(
            self.run_id, self.local, upto=head.seq, max_events=MAX_EVENTS, max_bytes=MAX_BYTES
        )
        if not lines:
            return False
        await self._blobs(lines)
        try:
            server = await self.client.post_events(self.run_id, lines)
        except ApiError as e:
            if e.code == "missing_blobs":
                self.sent.difference_update(e.missing)
                await self._upload(e.missing)
                return True
            if e.code == "diverged":
                raise Diverged(e.problem.get("detail") or "diverged") from None
            if e.code == "gap" and e.head is not None:
                self.server = e.head
                self.local = await self._seek(e.head.seq)
                return True
            raise
        self.batches += 1
        self.lines += len(lines)
        self.server = server
        if server.seq == after.seq:
            self.local = after
        else:
            self.local = await self._seek(server.seq)
        self.done = self.local.finished
        return True

    async def _blobs(self, lines: Sequence[bytes]) -> None:
        def refs() -> list[str]:
            out: dict[str, None] = {}
            for line in lines:
                for sha in blob_refs(json.loads(line)["data"]):
                    out[sha] = None
            return list(out)

        wanted = [sha for sha in await asyncio.to_thread(refs) if sha not in self.sent]
        if not wanted:
            return
        missing = await self.client.missing(self.run_id, wanted)
        await self._upload(missing)
        self.sent.update(wanted)

    async def _upload(self, shas: Sequence[str]) -> None:
        for sha in shas:
            await self.client.put_blob(self.run_id, sha, self.store.blob_path(self.run_id, sha))
            self.sent.add(sha)
