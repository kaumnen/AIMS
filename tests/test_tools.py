import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aims.agent import OpenGate
from aims.events import ExecStarted, LocalEventStore, RunStarted
from aims.sandbox import AuditedSandbox, DockerBackend, SandboxSpec
from aims.scenario import SandboxConfig
from aims.tools import ToolContext, load_tools
from aims.tools.builtin import BashArgs, EditArgs

pytestmark = [pytest.mark.anyio, pytest.mark.docker]


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


@pytest.fixture
async def ctx(backend: DockerBackend, tmp_path: Path) -> AsyncIterator[ToolContext]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        run_id = await store.create_run(RunStarted(scenario="t", oversight="observe"))
        sb = await AuditedSandbox.start(
            backend, SandboxSpec(image="python:3.13-slim"), store, run_id=run_id, agent_id="a"
        )
        try:
            yield ToolContext(
                sandbox=sb,
                store=store,
                run_id=run_id,
                agent_id="a",
                call_id="c1",
                step=1,
                config=SandboxConfig(command_timeout_s=5, max_command_timeout_s=10),
                gate=OpenGate(),
            )
        finally:
            await sb.close()


async def test_edit_views_creates_and_replaces(ctx: ToolContext) -> None:
    (edit,) = load_tools(["edit"])

    async def do(**kw: object) -> tuple[str, bool]:
        out = await edit.run(ctx, EditArgs.model_validate(kw))
        return out.output, out.error

    assert await do(command="create", path="pkg/a.py", file_text="one\ntwo\ntwo\n") == (
        "Created /work/pkg/a.py.",
        False,
    )
    out, error = await do(command="create", path="/work/pkg/a.py", file_text="x")
    assert error
    assert "already exists" in out
    out, _ = await do(command="view", path="pkg/a.py")
    assert out == "/work/pkg/a.py, lines 1 to 3:\n     1\tone\n     2\ttwo\n     3\ttwo\n"
    out, error = await do(command="str_replace", path="pkg/a.py", old_str="two", new_str="2")
    assert error
    assert "appears 2 times" in out
    assert "[2, 3]" in out
    out, error = await do(command="str_replace", path="pkg/a.py", old_str="three", new_str="3")
    assert error
    assert "does not appear verbatim" in out
    out, error = await do(command="str_replace", path="pkg/a.py", old_str="one\n", new_str="1\n")
    assert not error
    assert "     1\t1\n" in out
    out, error = await do(command="insert", path="pkg/a.py", insert_line=0, new_str="zero")
    assert not error
    out, _ = await do(command="view", path="pkg/a.py", view_range=[1, 2])
    assert out.endswith("     1\tzero\n     2\t1\n")
    out, _ = await do(command="view", path="/work")
    assert "/work/pkg/a.py" in out
    out, error = await do(command="view", path="nope.py")
    assert (out, error) == ("/work/nope.py does not exist.", True)
    data = await ctx.sandbox.exec("cat /work/pkg/a.py", timeout_s=5)
    assert data.stdout == b"zero\n1\ntwo\ntwo\n"

    big = await ctx.sandbox.exec("head -c 5000000 /dev/zero | tr '\\0' a > /work/big", timeout_s=10)
    assert big.finished.exit_code == 0
    out, error = await do(command="str_replace", path="big", old_str="a", new_str="b")
    assert error
    assert "too large to edit" in out

    started = [
        p
        for p in (e.payload() for e in await ctx.store.events(ctx.run_id, types=["exec.started"]))
        if isinstance(p, ExecStarted)
    ]
    mine = [s for s in started if s.call_id == "c1"]
    assert mine
    assert all(s.user == "1000" and s.phase == "agent" for s in mine)
    writes = [s for s in mine if s.stdin is not None]
    assert writes[0].stdin == "one\ntwo\ntwo\n"


async def test_bash_reports_streams_exit_and_timeout(ctx: ToolContext) -> None:
    (bash,) = load_tools(["bash"])

    async def do(**kw: object) -> str:
        return (await bash.run(ctx, BashArgs.model_validate(kw))).output

    assert await do(command="echo out; echo err >&2; exit 3") == (
        "stdout:\nout\nstderr:\nerr\nexit code: 3"
    )
    assert await do(command="cd /tmp && pwd") == "stdout:\n/tmp\nexit code: 0"
    assert await do(command="pwd") == "stdout:\n/work\nexit code: 0"
    out = await do(command="sleep 30", timeout_s=60)
    assert "The command timed out after 10 s and was stopped." in out
    out = await do(command="seq 1 100000")
    assert out.startswith("stdout (588895 bytes, middle cut):\n1\n2\n")
    assert out.endswith("99999\n100000\nexit code: 0")
    assert "bytes omitted" in out
    out = await do(command="[[ 1 == 1 ]] && echo bash")
    assert out == "stdout:\nbash\nexit code: 0"
