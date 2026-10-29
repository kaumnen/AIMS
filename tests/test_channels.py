"""Channels: the host-side board, the shared folder's mounts, seed and series archive, the
folder change log attributed by kernel records, and a real two-agent run sharing both."""

import hashlib
import io
import json
import os
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import aiodocker
import httpx2
import pytest
from aiodocker.volumes import DockerVolume
from scripted import Script, call, models, profile

from aims.channels import Board, ChannelError, ChannelSetup, Folder, archives, build
from aims.channels.board import MessageTooLong, PostTool
from aims.events import (
    BlobRef,
    ChannelChange,
    ChannelInfo,
    ChannelMessage,
    ChannelRead,
    ChannelSnapshot,
    Draft,
    Event,
    EventStore,
    ExecFinished,
    ExecStarted,
    FileAccess,
    FsSnapshot,
    LocalEventStore,
    ProcExec,
    RunStarted,
)
from aims.prompts import Prompts, TextError
from aims.run.job import monitor_for
from aims.run.runner import Bench, Options, Runner, Texts, resolve_texts
from aims.sandbox import DockerBackend, SandboxBackend
from aims.sandbox.base import Change, Mount, Tree
from aims.sandbox.shared import SharedFolder, repack
from aims.scenario import Bundle, Scenario, load, parse
from aims.scenario.model import SandboxConfig
from aims.tools import ToolContext

pytestmark = pytest.mark.anyio

IMAGE = "python:3.13-slim"
BASE = """\
version: 1
name: shared
oversight: observe
task:
  instruction: Work together.
  environment: {image: python:3.13-slim}
agents:
  - id: a
  - id: b
  - id: r
  - id: x
"""
CHANNELS = """\
channels:
  - name: notes
    kind: folder
    path: /shared
    members: [a, b, r]
    read_only: [r]
    lifetime: series
    files:
      - {path: docs/plan.md, content: "plan\\n"}
      - {path: top.txt, content: top, mode: "0600"}
  - name: chat
    kind: board
    members: [a, b, r]
    read_only: [r]
    post_tool: note
    read_tool: notes_read
    max_bytes: 16
"""


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


def scenario(extra: str = "") -> Scenario:
    return parse(BASE + CHANNELS + extra)


async def new_run(store: EventStore, channels: list[ChannelInfo] | None = None) -> str:
    started = RunStarted(scenario="shared", oversight="observe", channels=channels or [])
    return await store.create_run(started)


def setup(s: Scenario, name: str, run_id: str, store: EventStore, **kw: Any) -> ChannelSetup:
    config = next(c for c in s.channels if c.name == name)
    return ChannelSetup(config=config, scenario=s, run_id=run_id, store=store, **kw)


async def of_type(store: EventStore, run_id: str, kind: str) -> list[Event]:
    return await store.events(run_id, types=[kind], limit=1000)


# The board.


async def test_board_posts_and_reads_by_agent_with_ids(store: LocalEventStore) -> None:
    s = scenario()
    run_id = await new_run(store)
    board = build(setup(s, "chat", run_id, store))
    assert isinstance(board, Board)
    first = await board.post("a", "hi b", call_id="c1")
    second = await board.post("b", "hi a")
    messages, more = await board.read("b", call_id="c2")
    again, _ = await board.read("b")
    third = await board.post("a", "bye")
    later, _ = await board.read("b")
    mine, _ = await board.read("a")

    assert (first.id, second.id, third.id) == (1, 2, 3)
    assert [(m.id, m.agent, m.text) for m in messages] == [(1, "a", "hi b"), (2, "b", "hi a")]
    assert (more, again, [m.id for m in later]) == (0, [], [3])
    assert [m.id for m in mine] == [1, 2, 3]
    posts = await of_type(store, run_id, "channel.message")
    assert [(e.agent_id, ChannelMessage.model_validate(e.data).call_id) for e in posts] == [
        ("a", "c1"),
        ("b", None),
        ("a", None),
    ]
    assert ChannelMessage.model_validate(posts[0].data).bytes == 4
    reads = [
        (e.agent_id, ChannelRead.model_validate(e.data))
        for e in await of_type(store, run_id, "channel.read")
    ]
    assert [(a, r.after, r.through, r.count, r.call_id) for a, r in reads] == [
        ("b", 0, 2, 2, "c2"),
        ("b", 2, 2, 0, None),
        ("b", 2, 3, 1, None),
        ("a", 0, 3, 3, None),
    ]


async def test_board_caps_and_roles(store: LocalEventStore) -> None:
    s = scenario()
    run_id = await new_run(store)
    board = Board(setup(s, "chat", run_id, store))
    with pytest.raises(MessageTooLong):
        await board.post("a", "x" * 17)
    with pytest.raises(ChannelError, match="cannot post"):
        await board.post("r", "hi")
    with pytest.raises(ChannelError, match="cannot post"):
        await board.post("x", "hi")
    with pytest.raises(ChannelError, match="not on the chat board"):
        await board.read("x")
    assert await of_type(store, run_id, "channel.message") == []
    assert [t.name for t in board.tools("a")] == ["notes_read", "note"]
    assert [t.name for t in board.tools("r")] == ["notes_read"]
    assert board.tools("x") == []
    assert s.tools_of("a")[-2:] == ["notes_read", "note"]


async def test_board_tools_through_a_tool_context(store: LocalEventStore) -> None:
    s = scenario()
    run_id = await new_run(store)
    board = Board(setup(s, "chat", run_id, store))
    read, post = board.tools("a")
    ctx = context(store, run_id, "a")
    long = await post.run(ctx, post.args.model_validate({"text": "y" * 20}))
    ok = await post.run(ctx, post.args.model_validate({"text": "ready"}))
    shown = await read.run(ctx, read.args.model_validate({}))
    empty = await read.run(ctx, read.args.model_validate({}))

    assert long.error
    assert long.output == (
        "The message is 20 bytes, over the 16 bytes the chat board takes. Nothing was posted."
    )
    assert (ok.error, ok.output) == (False, "Posted message 1 to chat.")
    assert shown.output == "Message 1 from a:\nready"
    assert empty.output == "No new messages on chat."
    spec = post.spec(SandboxConfig())
    assert spec.name == "note"
    assert spec.description.startswith("Post a message to the chat board. Its members (a, b, r)")
    assert "16 bytes" in spec.description


async def test_a_long_read_leaves_the_rest_for_the_next(store: LocalEventStore) -> None:
    s = parse(BASE + "channels:\n  - {name: chat, kind: board, max_bytes: 4096}\n")
    run_id = await new_run(store)
    board = Board(setup(s, "chat", run_id, store))
    for n in range(5):
        await board.post("a", str(n) * 4000)
    first, more = await board.read("b", limit=9000)
    rest, left = await board.read("b", limit=9000)
    [read, *_] = board.tools("b")
    await board.post("a", "z")
    out = await read.run(context(store, run_id, "b"), read.args.model_validate({}))

    assert ([m.id for m in first], more) == ([1, 2], 3)
    assert ([m.id for m in rest], left) == ([3, 4], 1)
    assert out.output.startswith("Message 5 from a:")


def context(store: EventStore, run_id: str, agent: str) -> ToolContext:
    return ToolContext(
        sandbox=cast("Any", None),
        store=store,
        run_id=run_id,
        agent_id=agent,
        call_id="c",
        step=1,
        config=SandboxConfig(),
        gate=cast("Any", None),
    )


def test_board_texts_layer_like_tool_texts() -> None:
    s = scenario()
    a, x = s.agent("a"), s.agent("x")
    config = Prompts(tools={"note": {"posted": "Sent {id}."}, "bash": {"timeout": "late"}})
    texts = resolve_texts(config, a, s)
    assert texts.channels["note"]["posted"] == "Sent {id}."
    assert (
        texts.channels["notes_read"]
        == dict(Board.tool_texts(s.channels[1], s, "a", {}))["notes_read"]
    )
    assert texts.tools["bash"]["timeout"] == "late"
    assert "note" not in texts.tools
    assert resolve_texts(config, x, s).channels == {}
    assert Texts.restore(texts.snapshot()) == texts
    assert "channels" not in resolve_texts(Prompts(), x, s).snapshot()

    own = s.agent("b").model_copy(
        update={"prompts": Prompts(tools={"note": {"description": "Tell {channel}."}})}
    )
    assert resolve_texts(config, own, s).channels["note"]["description"] == "Tell {channel}."
    with pytest.raises(TextError, match="tool note has no text nope"):
        resolve_texts(Prompts(tools={"note": {"nope": "x"}}), a, s)
    with pytest.raises(TextError, match="unknown placeholder"):
        resolve_texts(Prompts(tools={"notes_read": {"none": "{who}"}}), a, s)


async def test_board_tools_take_the_resolved_texts(store: LocalEventStore) -> None:
    s = scenario()
    run_id = await new_run(store)
    texts = resolve_texts(
        Prompts(tools={"note": {"description": "Tell {channel}."}}), s.agent("a"), s
    )
    board = Board(setup(s, "chat", run_id, store, texts={"a": texts.channels}))
    post = board.tools("a")[1]
    assert isinstance(post, PostTool)
    assert post.spec(SandboxConfig()).description == "Tell chat."
    assert board.tools("b")[1].spec(SandboxConfig()).description.startswith("Post a message")


# The folder.


async def test_folder_mounts_and_info(store: LocalEventStore) -> None:
    s = scenario()
    folder = Folder(setup(s, "notes", "r1", store, restored=b"tar"))
    assert folder.mounts("a") == [
        Mount(volume="notes-folder", target="/shared", read_only=False, channel="notes")
    ]
    assert folder.mounts("r")[0].read_only
    assert folder.mounts("x") == []
    info = folder.info()
    assert (info.kind, info.members, info.read_only, info.path, info.lifetime) == (
        "folder",
        ["a", "b", "r"],
        ["r"],
        "/shared",
        "series",
    )
    assert info.restored is not None
    assert info.restored.size == 3
    once = parse(BASE + CHANNELS.replace("lifetime: series", "lifetime: run"))
    assert Folder(setup(once, "notes", "r1", store, restored=b"tar")).info().restored is None


async def test_folder_files_become_an_agent_owned_tar(
    store: LocalEventStore, tmp_path: Path
) -> None:
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "run.sh").write_text("echo hi\n")
    (tmp_path / "kit" / "run.sh").chmod(0o755)
    extra = "      - {path: tools, source: kit}\n"
    s = parse(BASE + CHANNELS.replace('mode: "0600"}\n', 'mode: "0600"}\n' + extra))
    folder = Folder(setup(s, "notes", "r1", store, base=tmp_path))
    assert folder.seed is not None
    with tarfile.open(fileobj=io.BytesIO(folder.seed)) as tar:
        got = {m.name: (m.type, oct(m.mode), m.uid, m.gid) for m in tar.getmembers()}
    assert got == {
        "docs": (tarfile.DIRTYPE, "0o775", 1000, 1000),
        "docs/plan.md": (tarfile.REGTYPE, "0o664", 1000, 1000),
        "top.txt": (tarfile.REGTYPE, "0o600", 1000, 1000),
        "tools": (tarfile.DIRTYPE, "0o775", 1000, 1000),
        "tools/run.sh": (tarfile.REGTYPE, "0o775", 1000, 1000),
    }
    with pytest.raises(ChannelError, match="needs the scenario's directory"):
        Folder(setup(s, "notes", "r1", store))


class FakeVolume:
    def __init__(self, *trees: Tree, archive: bytes = b"") -> None:
        self.trees = list(trees)
        self.archive = archive
        self.put: list[bytes] = []
        self.closed = False

    async def tree(self) -> Tree | None:
        return self.trees.pop(0)

    async def get_archive(self) -> bytes:
        return self.archive

    async def put_archive(self, data: bytes) -> None:
        self.put.append(data)

    async def close(self) -> None:
        self.closed = True


class VolumeBackend:
    name = "fake"

    def __init__(self, volume: FakeVolume) -> None:
        self.shared = volume
        self.mounts: list[Mount] = []

    async def volume(self, run_id: str, mount: Mount) -> FakeVolume:
        self.mounts.append(mount)
        return self.shared


def tree(digest: str, *changes: Change) -> Tree:
    return Tree(
        roots=("/shared",),
        digest=digest,
        entries=len(changes),
        files=len(changes),
        bytes=0,
        hashed_bytes=0,
        unhashed=0,
        content_skipped=0,
        truncated=False,
        duration_ms=1,
        changes=changes,
    )


def change(path: str, how: Any = "added", data: bytes | None = None) -> Change:
    return Change(path, how, "file", len(data or b""), 0o664, 1, None, data)


def docker_tar(top: str, *entries: tuple[str, bytes | None]) -> bytes:
    """A tar shaped as Docker returns a directory: under its base name, in walk order."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        root = tarfile.TarInfo(top)
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, data in entries:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.mtime = 1700000000.75
            info.uid = info.gid = 1000
            info.uname = "agent"
            info.pax_headers = {"atime": "1700000001.5"}
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_repack_is_deterministic_and_relative() -> None:
    raw = docker_tar("shared", ("z.txt", b"z"), ("sub", None), ("sub/a.txt", b"a"))
    same = docker_tar("shared", ("sub", None), ("sub/a.txt", b"a"), ("z.txt", b"z"))
    packed = repack(raw)
    assert packed is not None
    assert packed == repack(same)
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == ["sub", "sub/a.txt", "z.txt"]
        assert {(m.mtime, m.uname, m.uid) for m in members} == {(1700000000, "", 1000)}
        assert all(not m.pax_headers for m in members)
        f = tar.extractfile("z.txt")
        assert f is not None
        assert f.read() == b"z"
    assert repack(raw, cap=1) is None


async def start_folder(
    store: LocalEventStore, s: Scenario, volume: FakeVolume, restored: bytes | None = None
) -> tuple[str, Folder]:
    info = Folder(setup(s, "notes", "", store, restored=restored)).info()
    run_id = await new_run(store, [info])
    backend = cast("SandboxBackend", VolumeBackend(volume))
    folder = Folder(setup(s, "notes", run_id, store, restored=restored, backend=backend))
    await folder.start({})
    return run_id, folder


async def test_a_series_folder_carries_over(store: LocalEventStore) -> None:
    s = scenario()
    left = docker_tar("shared", ("plan.md", b"done\n"))
    first = FakeVolume(tree("d0", change("/shared/docs/plan.md", data=b"plan\n")), tree("d1"))
    first.archive = left
    run_id, folder = await start_folder(store, s, first)
    archive = await folder.close({})

    assert first.put == [folder.seed]
    assert first.closed
    assert archive == repack(left)
    snapshots = [
        ChannelSnapshot.model_validate(e.data)
        for e in await of_type(store, run_id, "channel.snapshot")
    ]
    assert [(x.base, x.digest, x.exec_id) for x in snapshots] == [
        (None, "d0", None),
        ("d0", "d1", None),
    ]
    assert [c.path for c in snapshots[0].changes] == ["/shared/docs/plan.md"]
    assert snapshots[0].changes[0].agent is None
    assert snapshots[0].archive is None
    assert snapshots[1].archive is not None
    assert await archives(store, run_id) == {"notes": archive}

    assert archive is not None
    second = FakeVolume(tree("e0", change("/shared/plan.md")), tree("e1"))
    next_id, again = await start_folder(store, s, second, restored=archive)
    assert second.put == [archive]
    started = RunStarted.model_validate((await of_type(store, next_id, "run.started"))[0].data)
    sha = hashlib.sha256(archive).hexdigest()
    assert started.channels[0].restored == BlobRef(sha256=sha, size=len(archive))
    await again.close({})


async def test_a_run_folder_logs_its_archive_but_hands_none_on(store: LocalEventStore) -> None:
    s = parse(BASE + CHANNELS.replace("lifetime: series", "lifetime: run"))
    volume = FakeVolume(tree("d0"), tree("d1"), archive=docker_tar("shared", ("a", b"a")))
    run_id, folder = await start_folder(store, s, volume, restored=b"ignored")
    assert await folder.close({}) is None
    assert volume.put == [folder.seed]
    last = ChannelSnapshot.model_validate(
        (await of_type(store, run_id, "channel.snapshot"))[-1].data
    )
    assert last.archive is not None
    assert await archives(store, run_id) == {}


async def test_a_folder_needs_a_backend_that_shares_volumes(store: LocalEventStore) -> None:
    s = scenario()
    run_id = await new_run(store)
    folder = Folder(setup(s, "notes", run_id, store, backend=cast("SandboxBackend", object())))
    with pytest.raises(ChannelError, match="cannot share a folder"):
        await folder.start({})


# The change log's attribution.


async def append(store: EventStore, run_id: str, agent: str, payload: Any) -> Event:
    [event] = await store.append(run_id, [Draft(payload, agent)])
    return event


def access(
    op: Any, path: str, *, target: str | None = None, exec_id: str = "e1", **kw: Any
) -> FileAccess:
    return FileAccess(
        exec_id=exec_id,
        pid=10,
        ts_kernel=1,
        cpu=0,
        cpu_seq=1,
        op=op,
        path=path,
        target=target,
        **kw,
    )


def started(exec_id: str, phase: Any) -> ExecStarted:
    return ExecStarted(
        exec_id=exec_id,
        call_id=None,
        command="true",
        wrapper=[],
        user="1000",
        cwd="/",
        timeout_ms=1000,
        audit="kernel",
        phase=phase,
    )


async def test_changes_go_to_the_one_member_whose_records_wrote_them(
    store: LocalEventStore,
) -> None:
    run_id = await new_run(store)
    paths = ["mine", "both", "none", "moved", "ro", "failed", "verified", "read"]
    volume = FakeVolume(
        tree("d1", *(change(f"/shared/{p}") for p in paths)),
        tree("d2"),
        tree("d3", change("/shared/kept")),
        tree("d4"),
        tree("d5"),
        tree("d6", change("/shared/stale")),
    )
    folder = SharedFolder(
        store, volume, run_id=run_id, channel="notes", path="/shared", writers=["a", "b"]
    )
    noted = [
        await append(store, run_id, "a", access("write", "/shared/mine")),
        await append(store, run_id, "a", access("write", "/shared/both")),
        await append(store, run_id, "b", access("write", "/shared/both", exec_id="e2")),
        await append(store, run_id, "a", access("rename", "/shared/.tmp", target="/shared/moved")),
        await append(store, run_id, "r", access("write", "/shared/ro", exec_id="e3")),
        await append(store, run_id, "a", access("write", "/shared/failed", errno=13)),
        await append(store, run_id, "a", started("v1", "verifier")),
        await append(store, run_id, "a", access("write", "/shared/verified", exec_id="v1")),
        await append(store, run_id, "a", access("read", "/shared/read")),
        await append(store, run_id, "a", access("write", "/work/mine")),
    ]
    folder.note(noted)
    await folder.diff("x1")
    kept = await append(store, run_id, "b", access("write", "/shared/kept"))
    folder.note([kept])
    await folder.diff("x2")
    await folder.diff("x3")
    folder.note([await append(store, run_id, "b", access("write", "/shared/stale"))])
    for exec_id in ("x4", "x5", "x6"):
        await folder.diff(exec_id)

    snaps = [
        ChannelSnapshot.model_validate(e.data)
        for e in await of_type(store, run_id, "channel.snapshot")
    ]
    assert [x.exec_id for x in snaps] == ["x1", "x3", "x6"]
    got = {c.path: (c.agent, c.evidence) for x in snaps for c in x.changes}
    seq = [e.seq for e in noted]
    assert got == {
        "/shared/mine": ("a", [seq[0]]),
        "/shared/both": (None, [seq[1], seq[2]]),
        "/shared/none": (None, []),
        "/shared/moved": ("a", [seq[3]]),
        "/shared/ro": (None, []),
        "/shared/failed": (None, []),
        "/shared/verified": (None, []),
        "/shared/read": (None, []),
        "/shared/kept": ("b", [kept.seq]),
        "/shared/stale": (None, []),
    }
    assert all(isinstance(c, ChannelChange) for x in snaps for c in x.changes)
    assert all(e.agent_id is None for e in await of_type(store, run_id, "channel.snapshot"))


# A real run: two agents share a folder and a board, a third reads only.

DOCKER_SCENARIO = """\
version: 1
name: shared-run
oversight: observe
task:
  instruction: Work together.
  environment: {image: python:3.13-slim}
agents:
  - id: a
    task: {instruction: You are agent alpha.}
  - id: b
    task: {instruction: You are agent beta.}
channels:
  - name: notes
    kind: folder
    path: /shared
    read_only: [b]
    files:
      - {path: readme.txt, content: "start here\\n"}
  - name: chat
    kind: board
"""
WAIT = "for i in $(seq 200); do [ -f /shared/plan.txt ] && break; sleep 0.05; done; "
WRITE = (
    "cp /bin/true /shared/tool && /shared/tool && echo 'ship friday' > /shared/plan.txt;"
    " cat /shared/readme.txt"
)


class ByAgent:
    """Answers each agent from its own script, told apart by its instruction."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.scripts = scripts

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        messages = json.dumps(json.loads(request.content)["messages"])
        return self.scripts["a" if "agent alpha" in messages else "b"].handle(request)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        audit = "kernel" if os.environ.get("AIMS_REQUIRE_TRACER") else "auto"
        backend = await DockerBackend.open(audit=audit)
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        yield backend


def written(where: Path, text: str) -> Bundle:
    where.mkdir()
    (where / "scenario.yaml").write_text(text)
    return load(where / "scenario.yaml")


async def run_shared(
    backend: DockerBackend,
    store: LocalEventStore,
    where: Path,
    text: str,
    scripts: dict[str, Script],
    options: Options | None = None,
    *,
    watched: bool = False,
) -> str:
    """Run `text` with each agent on its script; with `watched`, under the signals monitor."""
    if backend.tracer is None:
        pytest.skip("needs the kernel tracer")
    bundle = written(where, text)
    model = profile()
    config = models(model)
    monitor = None
    if watched:
        monitor = monitor_for(config, Prompts(), bundle.scenario, [model], preset="signals")
    bench = Bench(store, backend, config, transport=ByAgent(scripts).transport())
    result = await Runner(bench, bundle, options or Options(), monitor=monitor).run()
    assert result.run_id is not None
    assert result.status == "completed", result.reason
    assert (await store.verify(result.run_id)).ok
    return result.run_id


@pytest.mark.docker
@pytest.mark.tracer
async def test_a_series_folder_starts_where_the_last_run_left_it(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    text = DOCKER_SCENARIO.replace(
        "    read_only: [b]\n", "    read_only: [b]\n    lifetime: series\n"
    )
    write = {
        "a": Script(call("bash", command=WRITE), call("submit", answer="done")),
        "b": Script(
            call("bash", command=WAIT + "cat /shared/plan.txt"), call("submit", answer="ok")
        ),
    }
    first = await run_shared(backend, store, tmp_path / "one", text, write, watched=True)
    left = await archives(store, first)
    assert set(left) == {"notes"}
    read = {
        "a": Script(
            call("bash", command="cat /shared/plan.txt; ls /shared"), call("submit", answer="ok")
        ),
        "b": Script(call("submit", answer="ok")),
    }
    second = await run_shared(backend, store, tmp_path / "two", text, read, Options(restored=left))

    events = await store.events(second, limit=5000)
    started = RunStarted.model_validate(events[0].data)
    sha = hashlib.sha256(left["notes"]).hexdigest()
    assert started.channels[0].restored == BlobRef(sha256=sha, size=len(left["notes"]))
    assert await store.get_blob(second, sha) == left["notes"]
    base = ChannelSnapshot.model_validate(
        next(e for e in events if e.type == "channel.snapshot").data
    )
    assert sorted(c.path for c in base.changes) == [
        "/shared/plan.txt",
        "/shared/readme.txt",
        "/shared/tool",
    ]
    out = next(
        ExecFinished.model_validate(e.data)
        for e in events
        if e.type == "exec.finished" and e.agent_id == "a" and e.data.get("call_id")
    )
    assert out.stdout == "ship friday\nplan.txt\nreadme.txt\ntool\n"
    flags = await of_type(store, first, "monitor.flag")
    assert all(e.data.get("source") for e in flags)


@pytest.mark.docker
@pytest.mark.tracer
async def test_two_agents_share_a_folder_and_a_board(
    backend: DockerBackend, store: LocalEventStore, tmp_path: Path
) -> None:
    # a posts before it writes the plan, and b waits for the plan, so b's read finds the post.
    scripts = {
        "a": Script(
            call("post_message", text="plan is in /shared"),
            call("bash", command=WRITE),
            call("submit", answer="done"),
        ),
        "b": Script(
            call("bash", command=WAIT + "cat /shared/plan.txt; touch /shared/b.txt; echo rc=$?"),
            call("read_messages"),
            call("submit", answer="done"),
        ),
    }
    one = tmp_path / "one"
    run_id = await run_shared(backend, store, one, DOCKER_SCENARIO, scripts, watched=True)

    first = RunStarted.model_validate((await of_type(store, run_id, "run.started"))[0].data)
    assert [(c.name, c.kind, c.members, c.read_only) for c in first.channels] == [
        ("notes", "folder", ["a", "b"], ["b"]),
        ("chat", "board", ["a", "b"], []),
    ]
    events = await store.events(run_id, limit=5000)
    fs = [
        FsSnapshot.model_validate(e.data)
        for e in events
        if e.type == "fs.snapshot" and e.agent_id == "a"
    ]
    assert fs
    assert all("/shared" not in s.roots for s in fs)
    assert not [c.path for s in fs for c in s.changes if c.path.startswith("/shared")]

    snaps = [
        (e, ChannelSnapshot.model_validate(e.data)) for e in events if e.type == "channel.snapshot"
    ]
    base = snaps[0][1]
    assert (base.base, [c.path for c in base.changes]) == (None, ["/shared/readme.txt"])
    plan = [c for _, s in snaps for c in s.changes if c.path == "/shared/plan.txt"]
    assert len(plan) == 1
    assert plan[0].agent == "a"
    assert plan[0].evidence
    by_seq = {e.seq: e for e in events}
    cited = [by_seq[n] for n in plan[0].evidence]
    assert all(e.agent_id == "a" and e.type in ("file.access", "file.write") for e in cited)
    assert any(e.data.get("path") == "/shared/plan.txt" for e in cited)
    assert all(e.agent_id is None for e, _ in snaps)
    assert snaps[-1][1].archive is not None
    archive = await store.get_blob(run_id, snaps[-1][1].archive.sha256)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert sorted(tar.getnames()) == ["plan.txt", "readme.txt", "tool"]
    tool = next(c for _, s in snaps for c in s.changes if c.path == "/shared/tool")
    ran = [ProcExec.model_validate(e.data) for e in events if e.type == "proc.exec"]
    assert tool.agent == "a"
    assert tool.sha256 is not None
    assert [p.sha256 for p in ran if p.path == "/shared/tool"] == [tool.sha256]

    outputs = {
        (e.agent_id, e.data.get("call_id")): ExecFinished.model_validate(e.data)
        for e in events
        if e.type == "exec.finished"
    }
    assert outputs["a", "call_2"].stdout == "start here\n"
    seen = outputs["b", "call_1"].stdout
    assert isinstance(seen, str)
    assert "ship friday" in seen
    assert "rc=1" in seen
    assert not [c for _, s in snaps for c in s.changes if c.path == "/shared/b.txt"]

    posts = [e for e in events if e.type == "channel.message"]
    reads = [e for e in events if e.type == "channel.read"]
    assert [(e.agent_id, e.data["id"], e.data["call_id"]) for e in posts] == [("a", 1, "call_1")]
    assert [(e.agent_id, e.data["through"], e.data["count"]) for e in reads] == [("b", 1, 1)]
    results = [e for e in events if e.type == "tool.result" and e.agent_id == "b"]
    assert "Message 1 from a:\nplan is in /shared" in [e.data["output"] for e in results]

    with pytest.raises(aiodocker.DockerError):
        await DockerVolume(backend.docker, f"aims-{run_id}-notes-folder").show()
