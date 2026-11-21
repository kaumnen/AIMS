import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from aiodocker.stream import Message

import aims.sandbox.imds as imds
from aims.events import Payload, ServiceEnded, ServicePath, ServiceRequest, ServiceSpan, ServiceStop

pytestmark = pytest.mark.anyio

STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")

type Chunk = tuple[int, bytes] | Callable[[], None]


class Stream:
    """An attach stream that hands out `chunks`, then ends, or waits forever with `hang`. A
    callable among them runs when its turn comes. With `pause` each read lets other tasks run
    first."""

    def __init__(self, chunks: list[Chunk], *, hang: bool = False, pause: bool = False) -> None:
        self.chunks = list(chunks)
        self.hang = hang
        self.pause = pause
        self.closed = False

    async def read_out(self) -> Message | None:
        while self.chunks:
            if self.pause:
                await asyncio.sleep(0)
            chunk = self.chunks.pop(0)
            if callable(chunk):
                chunk()
                continue
            stream, data = chunk
            return Message(stream, data)
        if self.hang:
            await asyncio.Event().wait()
        return None

    async def close(self) -> None:
        self.closed = True


class Sink:
    """The audited sandbox as the follower sees it: the exec running now and the run log.
    `refuse` says which batches the log refuses, `fail` which ones fail otherwise."""

    def __init__(
        self,
        refuse: Callable[[Sequence[Payload]], bool] = lambda batch: False,
        fail: Callable[[Sequence[Payload]], bool] = lambda batch: False,
    ) -> None:
        self.exec_id: str | None = None
        self.batches: list[list[Payload]] = []
        self.refuse = refuse
        self.fail = fail
        self.gate: asyncio.Event | None = None

    async def append(self, payloads: Sequence[Payload]) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse(payloads):
            raise ValueError("the log refused it")
        if self.fail(payloads):
            raise OSError("the disk is full")
        self.batches.append(list(payloads))

    @property
    def events(self) -> list[Payload]:
        return [p for batch in self.batches for p in batch]


class Container:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {"Running": False, "ExitCode": 0, "OOMKilled": False}
        self.asked = 0

    async def wait(self, **kwargs: Any) -> dict[str, Any]:
        self.asked += 1
        return {"StatusCode": self.state.get("ExitCode")}

    async def show(self) -> dict[str, Any]:
        self.asked += 1
        return {"State": self.state}


def record(path: str, time: str = "2026-11-20T10:00:00Z", status: int = 200) -> bytes:
    entry = {"time": time, "method": "GET", "path": path, "headers": {}, "status": status}
    return json.dumps(entry, separators=(",", ":")).encode() + b"\n"


def refused(time: str) -> bytes:
    return json.dumps({"time": time, "status": 503, "refused": True}).encode() + b"\n"


async def follow(
    chunks: list[Chunk],
    *,
    closing: bool = True,
    container: Container | None = None,
    sink: Sink | None = None,
    pause: bool = False,
) -> tuple[list[ServiceRequest], ServiceEnded]:
    """The request events the follower logged in order, with the event that closed them."""
    sink = sink or Sink()
    done = asyncio.Event()
    if closing:
        done.set()
    stream = Stream(chunks, pause=pause)
    await imds._follow(stream, sink, "sidecar", container or Container(), done)
    assert stream.closed
    return split(sink.events)


def split(events: list[Payload]) -> tuple[list[ServiceRequest], ServiceEnded]:
    *head, ended = events
    assert isinstance(ended, ServiceEnded)
    requests = [r for r in head if isinstance(r, ServiceRequest)]
    assert requests == head
    return requests, ended


def out(*lines: bytes) -> list[Chunk]:
    return [(1, line) for line in lines]


async def test_each_request_is_an_event_and_an_end_event_follows() -> None:
    requests, ended = await follow(out(record("/a"), record("/b", status=404)))
    assert [(r.method, r.path, r.status) for r in requests] == [
        ("GET", "/a", 200),
        ("GET", "/b", 404),
    ]
    assert all(r.service == "metadata" for r in requests)
    assert requests[0].time == "2026-11-20T10:00:00Z"
    assert ended == ServiceEnded(service="metadata", kept=2, dropped=0)


async def test_an_end_event_follows_no_requests() -> None:
    requests, ended = await follow([])
    assert requests == []
    assert ended == ServiceEnded(service="metadata", kept=0, dropped=0)


async def test_each_request_names_the_exec_running_as_it_came(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing is logged before the end, when no exec runs.
    monkeypatch.setattr(imds, "FLUSH_S", 60.0)
    sink = Sink()

    def running(exec_id: str | None) -> Callable[[], None]:
        def set_it() -> None:
            sink.exec_id = exec_id

        return set_it

    chunks: list[Chunk] = [
        (1, record("/before")),
        running("e1"),
        (1, record("/first")),
        running("e2"),
        (1, record("/second")),
        running(None),
    ]
    requests, _ = await follow(chunks, sink=sink)
    assert [(r.path, r.exec_id) for r in requests] == [
        ("/before", None),
        ("/first", "e1"),
        ("/second", "e2"),
    ]
    assert len(sink.batches) == 2


async def test_requests_are_logged_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imds, "FLUSH_N", 2)
    sink = Sink()
    requests, _ = await follow(out(*(record(f"/{i}") for i in range(5))), sink=sink, pause=True)
    assert [r.path for r in requests] == [f"/{i}" for i in range(5)]
    *batches, last = sink.batches
    assert len(batches) >= 3
    assert all(1 <= len(batch) <= 2 for batch in batches)
    assert len(last) == 1


async def test_a_slow_log_does_not_hold_up_the_reads() -> None:
    sink = Sink()
    sink.gate = asyncio.Event()
    stream = Stream(out(*(record(f"/{i}") for i in range(50))), pause=True)
    done = asyncio.Event()
    done.set()
    task = asyncio.create_task(imds._follow(stream, sink, "sidecar", Container(), done))
    for _ in range(200):
        if not stream.chunks:
            break
        await asyncio.sleep(0.01)
    # Every line was read while the first write still waits.
    assert stream.chunks == []
    assert sink.batches == []
    sink.gate.set()
    await task
    requests, ended = split(sink.events)
    assert len(requests) == 50
    assert ended.kept == 50


async def test_a_refused_batch_goes_in_one_at_a_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad(batch: Sequence[Payload]) -> bool:
        return any(isinstance(p, ServiceRequest) and p.path == "/bad" for p in batch)

    sink = Sink(refuse=bad)
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        requests, ended = await follow(out(record("/a"), record("/bad"), record("/b")), sink=sink)
    assert [r.path for r in requests] == ["/a", "/b"]
    assert (ended.kept, ended.unwritten) == (3, 1)
    (message,) = [r.getMessage() for r in caplog.records]
    assert "did not take 1 records" in message


async def test_a_batch_that_fails_counts_whole_and_the_follower_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imds, "FLUSH_N", 2)
    failed: list[int] = []

    def first(batch: Sequence[Payload]) -> bool:
        if not failed:
            failed.append(len(batch))
            return True
        return False

    sink = Sink(fail=first)
    requests, ended = await follow(out(*(record(f"/{i}") for i in range(5))), sink=sink, pause=True)
    assert ended.unwritten == failed[0]
    assert len(requests) == 5 - failed[0]
    assert ended.kept == 5


async def test_an_end_the_log_refuses_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    sink = Sink(refuse=lambda batch: any(isinstance(p, ServiceEnded) for p in batch))
    done = asyncio.Event()
    done.set()
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        await imds._follow(Stream(out(record("/a"))), sink, "sidecar", Container(), done)
    assert [type(p) for p in sink.events] == [ServiceRequest]
    (message,) = [r.getMessage() for r in caplog.records]
    assert "did not take the end" in message


async def test_the_first_records_are_kept_and_the_rest_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imds, "RECORDS", 3)
    monkeypatch.setattr(imds, "KEYS", 2)
    monkeypatch.setattr(imds, "KEY_PATH", 8)
    t = [f"2026-11-20T10:00:0{i}Z" for i in range(8)]
    requests, ended = await follow(
        out(
            record("/k1", t[0]),
            record("/k2", t[0]),
            record("/k3", t[0]),
            record("/a/long/path/1", t[1]),
            record("/a/long/path/2", t[2]),
            record("/a", t[3], status=404),
            record("/b", t[4]),
            record("/c", t[5]),
        ),
    )
    assert [r.path for r in requests] == ["/k1", "/k2", "/k3"]
    assert (ended.kept, ended.dropped) == (3, 5)
    # Two paths that share their first 8 characters count as one.
    assert ended.paths == [
        ServicePath(method="GET", path="/a/long/", status=200, count=2, first=t[1], last=t[2]),
        ServicePath(method="GET", path="/a", status=404, count=1, first=t[3], last=t[3]),
    ]
    assert ended.other == ServiceSpan(count=2, first=t[4], last=t[5])
    assert ended.refused is None


async def test_the_first_refused_connection_in_each_exec_is_an_event() -> None:
    sink = Sink()

    def running(exec_id: str | None) -> Callable[[], None]:
        def set_it() -> None:
            sink.exec_id = exec_id

        return set_it

    t = [f"2026-11-20T10:00:0{i}Z" for i in range(7)]
    chunks: list[Chunk] = [
        (1, record("/a")),
        (1, refused(t[1])),
        (1, refused(t[2])),
        running("e1"),
        (1, refused(t[3])),
        (1, refused(t[4])),
        running("e2"),
        (1, refused(t[5])),
        running(None),
        (1, refused(t[6])),
    ]
    requests, ended = await follow(chunks, sink=sink)
    assert [(r.path, r.refused, r.exec_id, r.time) for r in requests] == [
        ("/a", False, None, "2026-11-20T10:00:00Z"),
        (None, True, None, t[1]),
        (None, True, "e1", t[3]),
        (None, True, "e2", t[5]),
    ]
    assert all((r.method, r.status) == (None, 503) for r in requests[1:])
    # Every refused connection is counted. Only requests count as kept.
    assert ended.refused == ServiceSpan(count=6, first=t[1], last=t[6])
    assert (ended.kept, ended.dropped) == (1, 0)


async def test_a_flood_of_refused_connections_adds_one_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imds, "RECORDS", 2)
    flood = [refused("2026-11-20T10:00:01Z")] * 5000
    requests, ended = await follow(out(record("/a"), record("/b"), record("/c"), *flood))
    assert [(r.path, r.refused) for r in requests] == [("/a", False), ("/b", False), (None, True)]
    assert ended.refused is not None
    assert ended.refused.count == 5000
    assert (ended.kept, ended.dropped) == (2, 1)


async def test_a_line_past_the_cap_is_counted_at_the_followers_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imds, "LINE", 128)
    big = record("/" + "x" * 300)
    requests, ended = await follow(
        [
            # One line in three parts, then one whole in a single part.
            (1, big[:100]),
            (1, big[100:200]),
            (1, big[200:] + record("/kept")),
            (1, big + record("/also")),
        ],
    )
    assert [r.path for r in requests] == ["/kept", "/also"]
    assert ended.oversize is not None
    assert ended.oversize.count == 2
    assert STAMP.fullmatch(ended.oversize.first or "")
    assert STAMP.fullmatch(ended.oversize.last or "")
    assert ended.kept == 2


async def test_lines_that_are_not_records_are_counted() -> None:
    requests, ended = await follow(
        out(
            b"not json\n",
            b"[1]\n",
            b'{"time":"2026-11-20T10:00:00Z"}\n',
            # A method that is no string fits no event.
            b'{"time":"2026-11-20T10:00:00Z","method":7,"path":"/","headers":{},"status":200}\n',
            record("/a"),
        )
    )
    assert [r.path for r in requests] == ["/a"]
    assert (ended.kept, ended.invalid) == (1, 4)


async def test_a_last_line_without_a_newline_is_kept() -> None:
    requests, _ = await follow(out(record("/a"), record("/b").rstrip(b"\n")))
    assert [r.path for r in requests] == ["/a", "/b"]


async def test_stderr_past_twenty_lines_is_counted(caplog: pytest.LogCaptureFixture) -> None:
    err = b"".join(b"Problem %d\n" % i for i in range(25))
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        requests, ended = await follow([(2, err[:7]), (2, err[7:]), (2, b"x" * 200_000)])
    messages = [r.getMessage() for r in caplog.records]
    assert messages[:20] == [f"IMDS sidecar sidecar: Problem {i}" for i in range(20)]
    assert messages[20:] == ["IMDS sidecar sidecar: 6 more stderr lines not shown"]
    assert requests == []
    assert ended == ServiceEnded(service="metadata", kept=0, dropped=0, stderr=26)


async def test_a_long_stderr_line_is_cut(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        await follow([(2, b"y" * 200_000), (2, b"z\nshort\n")])
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "IMDS sidecar sidecar: a line over 64 KB was left out",
        "IMDS sidecar sidecar: short",
    ]


async def test_a_sidecar_that_dies_says_so_at_the_end(caplog: pytest.LogCaptureFixture) -> None:
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": True})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        requests, ended = await follow(out(record("/a")), closing=False, container=container)
    assert [r.path for r in requests] == ["/a"]
    assert ended.stopped == ServiceStop(exit=137, oom=True)
    assert (ended.lost, ended.incomplete) == (False, False)
    (message,) = [r.getMessage() for r in caplog.records]
    assert "stopped" in message
    assert "137" in message
    assert "out of memory" in message


async def test_a_stream_that_ends_while_the_sidecar_runs_is_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = Container({"Running": True, "ExitCode": 0, "OOMKilled": False})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        _, ended = await follow(out(record("/a")), closing=False, container=container)
    assert (ended.lost, ended.stopped) == (True, None)
    (message,) = [r.getMessage() for r in caplog.records]
    assert "not recorded" in message


async def test_a_close_says_nothing_of_a_stop(caplog: pytest.LogCaptureFixture) -> None:
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": False})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        requests, ended = await follow(out(record("/a")), closing=True, container=container)
    assert [r.path for r in requests] == ["/a"]
    assert (ended.stopped, ended.lost, ended.incomplete) == (None, False, False)
    assert container.asked == 0
    assert caplog.records == []


async def test_a_cancelled_follower_ends_its_record_as_incomplete() -> None:
    sink = Sink()
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": False})
    stream = Stream(out(record("/a"), refused("2026-11-20T10:00:01Z")), hang=True)
    task = asyncio.create_task(imds._follow(stream, sink, "sidecar", container, asyncio.Event()))
    for _ in range(100):
        if not stream.chunks:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert container.asked == 0
    assert stream.closed
    requests, ended = split(sink.events)
    assert [(r.path, r.refused) for r in requests] == [("/a", False), (None, True)]
    assert ended.refused is not None
    assert ended.refused.count == 1
    assert ended.incomplete


async def test_a_second_cancel_still_ends_the_record() -> None:
    sink = Sink()
    sink.gate = asyncio.Event()
    stream = Stream(out(record("/a")), hang=True)
    task = asyncio.create_task(imds._follow(stream, sink, "sidecar", Container(), asyncio.Event()))
    for _ in range(100):
        if not stream.chunks:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    # The end waits on the log; a cancel now must not split it.
    task.cancel()
    await asyncio.sleep(0.05)
    sink.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    requests, ended = split(sink.events)
    assert [r.path for r in requests] == ["/a"]
    assert ended.incomplete
