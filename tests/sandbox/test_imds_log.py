import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from aiodocker.stream import Message

import aims.sandbox.imds as imds

pytestmark = pytest.mark.anyio

STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class Stream:
    """An attach stream that hands out `chunks`, then ends, or waits forever with `hang`."""

    def __init__(self, chunks: list[tuple[int, bytes]], *, hang: bool = False) -> None:
        self.chunks = list(chunks)
        self.hang = hang
        self.closed = False

    async def read_out(self) -> Message | None:
        if self.chunks:
            stream, data = self.chunks.pop(0)
            return Message(stream, data)
        if self.hang:
            await asyncio.Event().wait()
        return None

    async def close(self) -> None:
        self.closed = True


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
    tmp_path: Path,
    chunks: list[tuple[int, bytes]],
    *,
    closing: bool = True,
    container: Container | None = None,
) -> list[dict[str, Any]]:
    log = tmp_path / "imds-agent.jsonl"
    done = asyncio.Event()
    if closing:
        done.set()
    stream = Stream(chunks)
    await imds._follow(stream, str(log), "sidecar", container or Container(), done)
    assert stream.closed
    return [json.loads(line) for line in log.read_text().splitlines()]


def out(*lines: bytes) -> list[tuple[int, bytes]]:
    return [(1, line) for line in lines]


async def test_no_summary_when_nothing_was_counted(tmp_path: Path) -> None:
    lines = await follow(tmp_path, out(record("/a"), record("/b")))
    assert [line["path"] for line in lines] == ["/a", "/b"]


async def test_the_first_records_are_kept_and_the_rest_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imds, "RECORDS", 3)
    monkeypatch.setattr(imds, "KEYS", 2)
    monkeypatch.setattr(imds, "KEY_PATH", 8)
    t = [f"2026-11-20T10:00:0{i}Z" for i in range(8)]
    lines = await follow(
        tmp_path,
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
    assert [line["path"] for line in lines[:3]] == ["/k1", "/k2", "/k3"]
    (summary,) = [line["summary"] for line in lines[3:]]
    assert STAMP.fullmatch(lines[3]["time"])
    assert summary["kept"] == 3
    assert summary["dropped"] == 5
    # Two paths that share their first 8 characters count as one.
    assert summary["paths"] == [
        {
            "method": "GET",
            "path": "/a/long/",
            "status": 200,
            "count": 2,
            "first": t[1],
            "last": t[2],
        },
        {"method": "GET", "path": "/a", "status": 404, "count": 1, "first": t[3], "last": t[3]},
    ]
    assert summary["other"] == {"count": 2, "first": t[4], "last": t[5]}
    assert "refused" not in summary


async def test_refused_connections_are_counted_not_written(tmp_path: Path) -> None:
    lines = await follow(
        tmp_path,
        out(
            record("/a"),
            refused("2026-11-20T10:00:01Z"),
            refused("2026-11-20T10:00:02Z"),
            refused("2026-11-20T10:00:03Z"),
        ),
    )
    assert lines[0]["path"] == "/a"
    assert lines[1]["summary"] == {
        "kept": 1,
        "dropped": 0,
        "refused": {"count": 3, "first": "2026-11-20T10:00:01Z", "last": "2026-11-20T10:00:03Z"},
    }
    assert len(lines) == 2


async def test_a_line_past_the_cap_is_counted_at_the_followers_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imds, "LINE", 128)
    big = record("/" + "x" * 300)
    lines = await follow(
        tmp_path,
        [
            # One line in three parts, then one whole in a single part.
            (1, big[:100]),
            (1, big[100:200]),
            (1, big[200:] + record("/kept")),
            (1, big + record("/also")),
        ],
    )
    assert [line.get("path") for line in lines[:2]] == ["/kept", "/also"]
    summary = lines[2]["summary"]
    assert summary["oversize"]["count"] == 2
    assert STAMP.fullmatch(summary["oversize"]["first"])
    assert STAMP.fullmatch(summary["oversize"]["last"])
    assert summary["kept"] == 2


async def test_lines_that_are_not_records_are_counted(tmp_path: Path) -> None:
    lines = await follow(
        tmp_path, out(b"not json\n", b"[1]\n", b'{"time":"2026-11-20T10:00:00Z"}\n', record("/a"))
    )
    assert lines[0]["path"] == "/a"
    assert lines[1]["summary"]["invalid"] == 3


async def test_a_last_line_without_a_newline_is_kept(tmp_path: Path) -> None:
    lines = await follow(tmp_path, out(record("/a"), record("/b").rstrip(b"\n")))
    assert [line["path"] for line in lines] == ["/a", "/b"]


async def test_stderr_past_twenty_lines_is_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    err = b"".join(b"Problem %d\n" % i for i in range(25))
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        lines = await follow(tmp_path, [(2, err[:7]), (2, err[7:]), (2, b"x" * 200_000)])
    messages = [r.getMessage() for r in caplog.records]
    assert messages[:20] == [f"IMDS sidecar sidecar: Problem {i}" for i in range(20)]
    assert messages[20:] == ["IMDS sidecar sidecar: 6 more stderr lines not shown"]
    assert lines == [{"time": lines[0]["time"], "summary": {"kept": 0, "dropped": 0, "stderr": 26}}]


async def test_a_long_stderr_line_is_cut(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        await follow(tmp_path, [(2, b"y" * 200_000), (2, b"z\nshort\n")])
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "IMDS sidecar sidecar: a line over 64 KB was left out",
        "IMDS sidecar sidecar: short",
    ]


async def test_a_sidecar_that_dies_leaves_a_stopped_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": True})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        lines = await follow(tmp_path, out(record("/a")), closing=False, container=container)
    assert lines[0]["path"] == "/a"
    assert STAMP.fullmatch(lines[1]["time"])
    assert lines[1]["stopped"] == {"exit": 137, "oom": True}
    assert len(lines) == 2
    (message,) = [r.getMessage() for r in caplog.records]
    assert "stopped" in message
    assert "137" in message
    assert "out of memory" in message


async def test_a_stream_that_ends_while_the_sidecar_runs_is_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    container = Container({"Running": True, "ExitCode": 0, "OOMKilled": False})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        lines = await follow(tmp_path, out(record("/a")), closing=False, container=container)
    assert lines[1] == {"time": lines[1]["time"], "lost": True}
    (message,) = [r.getMessage() for r in caplog.records]
    assert "not recorded" in message


async def test_a_close_writes_no_stopped_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": False})
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        lines = await follow(tmp_path, out(record("/a")), closing=True, container=container)
    assert [line["path"] for line in lines] == ["/a"]
    assert container.asked == 0
    assert caplog.records == []


async def test_a_cancelled_follower_does_not_inspect(tmp_path: Path) -> None:
    log = tmp_path / "imds-agent.jsonl"
    container = Container({"Running": False, "ExitCode": 137, "OOMKilled": False})
    stream = Stream(out(record("/a"), refused("2026-11-20T10:00:01Z")), hang=True)
    task = asyncio.create_task(
        imds._follow(stream, str(log), "sidecar", container, asyncio.Event())
    )
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
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert lines[0]["path"] == "/a"
    assert lines[1]["summary"]["refused"]["count"] == 1
