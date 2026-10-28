import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

import rfc8785

from aims.events.schema import GENESIS, SCHEMA_VERSION

FIELDS = frozenset({"v", "run_id", "seq", "ts", "agent_id", "type", "data", "prev_hash", "hash"})
MAX_LINE = 256 * 1024


class ChainError(Exception):
    def __init__(self, reason: str, *, seq: int, offset: int) -> None:
        super().__init__(f"seq {seq} at byte {offset}: {reason}")
        self.reason = reason
        self.seq = seq
        self.offset = offset


@dataclass(frozen=True, slots=True)
class Head:
    """Position after the last verified event of a run log."""

    seq: int = -1
    hash: str = GENESIS
    offset: int = 0
    finished: bool = False


def digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


def seal(
    head: Head, *, run_id: str, ts: str, agent_id: str | None, type: str, data: dict[str, Any]
) -> tuple[dict[str, Any], bytes, Head]:
    """Chain one event onto `head`. Returns the event, its canonical line and the new head.
    The event is read back from the line, so it holds what any later reader of the log gets:
    1.0 is written as 1 and comes back an int."""
    if head.finished:
        raise ChainError("run is finished", **_at(head))
    if (head.seq == -1) != (type == "run.started"):
        raise ChainError("run.started must be the first event", **_at(head))
    body = {
        "v": SCHEMA_VERSION,
        "run_id": run_id,
        "seq": head.seq + 1,
        "ts": ts,
        "agent_id": agent_id,
        "type": type,
        "data": data,
        "prev_hash": head.hash,
    }
    event = {**body, "hash": digest(body)}
    line = rfc8785.dumps(event) + b"\n"
    if len(line) > MAX_LINE:
        raise ChainError(f"event is {len(line)} bytes, limit {MAX_LINE}", **_at(head))
    event = json.loads(line)
    return event, line, _advance(head, event, len(line))


def follow(head: Head, line: bytes, *, run_id: str) -> tuple[dict[str, Any], Head]:
    """Verify that `line` is the next event after `head`."""
    at = _at(head)
    if not line.endswith(b"\n"):
        raise ChainError("incomplete line", **at)
    try:
        event = json.loads(line)
    except ValueError as e:
        raise ChainError(f"invalid json: {e}", **at) from None
    if not isinstance(event, dict) or event.keys() != FIELDS:
        raise ChainError("wrong fields", **at)
    try:
        canonical = rfc8785.dumps(event) + b"\n"
    except rfc8785.CanonicalizationError as e:
        raise ChainError(f"not canonical: {e}", **at) from None
    if canonical != line:
        raise ChainError("not canonical", **at)
    checks = (
        (event["v"] == SCHEMA_VERSION, "unknown schema version"),
        (event["run_id"] == run_id, "event belongs to another run"),
        (event["seq"] == head.seq + 1, f"expected seq {head.seq + 1}, found {event['seq']}"),
        (event["prev_hash"] == head.hash, "prev_hash does not match the previous event"),
        (not head.finished, "event after run.finished"),
        ((head.seq == -1) == (event["type"] == "run.started"), "run.started must come first"),
        (digest({k: v for k, v in event.items() if k != "hash"}) == event["hash"], "bad hash"),
    )
    for ok, reason in checks:
        if not ok:
            raise ChainError(reason, **at)
    return event, _advance(head, event, len(line))


def _at(head: Head) -> dict[str, int]:
    return {"seq": head.seq + 1, "offset": head.offset}


def _advance(head: Head, event: dict[str, Any], size: int) -> Head:
    return replace(
        head,
        seq=event["seq"],
        hash=event["hash"],
        offset=head.offset + size,
        finished=event["type"] == "run.finished",
    )
