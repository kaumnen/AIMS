import hashlib
import tracemalloc
from collections.abc import AsyncIterator
from compression import zstd
from pathlib import Path
from typing import cast

import httpx2
import pytest
from pydantic import JsonValue
from scripted import profile

from aims import __version__
from aims.models import ProfileError, load_models
from aims.node import client as client_module
from aims.node import service
from aims.node.client import ControlClient
from aims.node.service import job_inputs
from aims.run.job import Job, JobFork
from aims.wire import RunnerHello, RunnerStatus, TracerHealth

PLAIN = profile(name="plain", structured_output=False)
JUDGE = profile(name="judge")


@pytest.mark.parametrize(
    "job",
    [
        Job(run_id="r", profiles={"a": PLAIN}),
        Job(run_id="r", profiles={"a": PLAIN}, models={"judge": JUDGE}),
        Job(run_id="r", models={"judge": JUDGE, "plain": PLAIN}, fork=JobFork(origin="o", step=1)),
        Job(run_id="r", models={"plain": PLAIN}, fork=JobFork(origin="o", step=1, profile="plain")),
        Job(run_id="r", kind="models.check", models={"plain": PLAIN}, check=["plain"]),
    ],
    ids=["run", "run with a monitor", "fork with a monitor", "fork", "check"],
)
def test_a_job_may_name_a_profile_without_structured_output(job: Job) -> None:
    assert job.config().get("plain") == PLAIN


@pytest.mark.parametrize("role", ["sweep", "judge"])
def test_a_config_file_role_of_the_monitor_needs_structured_output(
    tmp_path: Path, role: str
) -> None:
    path = tmp_path / "aims.toml"
    path.write_text(
        '[profiles.plain]\nbackend = "chat"\nbase_url = "https://example.com/v1"\nmodel = "m"\n'
        'weights = "w"\nreasoning = "none"\nstructured_output = false\n\n'
        f'[roles]\n{role} = "plain"\n'
    )
    with pytest.raises(ProfileError, match=f"^role {role} needs structured output, which plain"):
        load_models(path)
    path.write_text(path.read_text().replace(f'{role} = "plain"', 'agent = "plain"'))
    assert load_models(path).role("agent").name == "plain"


def test_a_job_holds_only_the_inputs_its_scenario_names_by_blob_reference() -> None:
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    scenario: dict[str, JsonValue] = {
        "file": {"sha256": a, "size": 3},
        "files": {"sha256": b},
        "verifier": {"sha256": c, "size": 1, "more": True},
        "context": None,
    }
    job = Job(run_id="r", scenario=scenario, folder="d" * 64)
    assert job.blobs() == [a, "d" * 64]
    assert job.sizes() == {a: 3, "d" * 64: None}


class Blobs:
    """Serves blob files by sha256, as the control plane holds them."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def job_blob(self, run_id: str, sha256: str, limit: int | None = None) -> bytes:
        return self.files[sha256]


@pytest.mark.anyio
async def test_a_runner_decodes_a_job_input_no_further_than_its_reference_states() -> None:
    text, big = b"version: 1", b"a" * (64 * 1024 * 1024)
    frame = zstd.compress(b"hello")
    shas = [hashlib.sha256(d).hexdigest() for d in (text, big, frame)]
    files = {shas[0]: zstd.compress(text), shas[1]: zstd.compress(big), shas[2]: frame}
    scenario: dict[str, JsonValue] = {
        "file": {"sha256": shas[0], "size": len(text)},
        "context": {"sha256": shas[1], "size": 10},
    }
    client = cast("ControlClient", Blobs(files))
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match=f"blob {shas[1]} came back with other bytes"):
            await job_inputs(client, Job(run_id="r", scenario=scenario))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    # a zstd frame stored as it came, named by its own hash, is that frame
    scenario["context"] = {"sha256": shas[2], "size": len(frame)}
    got = await job_inputs(client, Job(run_id="r", scenario=scenario, folder=shas[0]))
    assert got == {shas[0]: text, shas[2]: frame}


@pytest.mark.anyio
async def test_a_runner_decodes_a_scenario_folder_no_further_than_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reference states the size of the folder the control plane tars, and a reply of a few
    KiB may decode to many GiB."""
    monkeypatch.setattr(service, "INPUT_LIMIT", 1024 * 1024, raising=False)
    folder = bytes(64 * 1024 * 1024)
    sha = hashlib.sha256(folder).hexdigest()
    streamed = zstd.ZstdCompressor()
    client = cast("ControlClient", Blobs({sha: streamed.compress(folder) + streamed.flush()}))
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match=f"folder {sha} holds more than 1048576 bytes"):
            await job_inputs(client, Job(run_id="r", folder=sha))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    # one that fits is read; one with other bytes is told apart
    small = b"x" * 1024
    client = cast("ControlClient", Blobs({sha: zstd.compress(small)}))
    with pytest.raises(RuntimeError, match=f"blob {sha} came back with other bytes"):
        await job_inputs(client, Job(run_id="r", folder=sha))
    sha = hashlib.sha256(small).hexdigest()
    client = cast("ControlClient", Blobs({sha: zstd.compress(small)}))
    assert await job_inputs(client, Job(run_id="r", folder=sha)) == {sha: small}


@pytest.mark.anyio
async def test_a_runner_decodes_no_job_input_past_its_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size a scenario's reference states comes from the control plane too."""
    monkeypatch.setattr(service, "INPUT_LIMIT", 1024 * 1024, raising=False)
    big = bytes(64 * 1024 * 1024)
    sha = hashlib.sha256(big).hexdigest()
    streamed = zstd.ZstdCompressor()
    client = cast("ControlClient", Blobs({sha: streamed.compress(big) + streamed.flush()}))
    scenario: dict[str, JsonValue] = {"context": {"sha256": sha, "size": len(big)}}
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match=f"input {sha} holds more than 1048576 bytes"):
            await job_inputs(client, Job(run_id="r", scenario=scenario))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


@pytest.mark.anyio
async def test_a_runner_reads_no_more_of_a_job_input_reply_than_a_file_under_its_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control plane may send a blob as is, any number of bytes, or a reply that never ends."""
    monkeypatch.setattr(service, "INPUT_LIMIT", 1024 * 1024, raising=False)
    chunk = bytes(1024 * 1024)
    sha = hashlib.sha256(chunk * 64).hexdigest()
    sent: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for n in range(64):
            sent.append(n)
            yield chunk

    async def reply(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == f"/runner/v1/jobs/r/blobs/{sha}"
        return httpx2.Response(200, content=body())

    client = ControlClient("http://cp", "token", runner="r1", transport=httpx2.MockTransport(reply))
    try:
        with pytest.raises(RuntimeError, match=f"folder {sha} holds more than 1048576 bytes"):
            await job_inputs(client, Job(run_id="r", folder=sha))
    finally:
        await client.aclose()
    assert len(sent) <= 3


REPLIES = {
    "hello": lambda c: c.hello(
        RunnerHello(name="r1", version=__version__, arch="arm64", tracer=TracerHealth(alive=True))
    ),
    "claim": lambda c: c.claim("r"),
    "heads": lambda c: c.heads(["r"]),
    "missing": lambda c: c.missing("r", ["a" * 64]),
    "events": lambda c: c.post_events("r", [b"{}\n"]),
    "truth": lambda c: c.run_truth("r"),
    "error": lambda c: c.status(RunnerStatus(tracer=TracerHealth(alive=True), free=1)),
}


@pytest.mark.anyio
@pytest.mark.parametrize("call", list(REPLIES))
async def test_a_runner_reads_no_more_of_a_control_plane_reply_than_its_cap(
    call: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each kind of reply has its cap, an error's body too, so a reply that never ends costs a
    runner no more memory than that."""
    mib = 1024 * 1024
    for name in ("REPLY_LIMIT", "TRUTH_LIMIT"):
        monkeypatch.setattr(client_module, name, mib, raising=False)
    sent: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for n in range(64):
            sent.append(n)
            yield b" " * mib

    status = 500 if call == "error" else 200
    transport = httpx2.MockTransport(lambda request: httpx2.Response(status, content=body()))
    client = ControlClient("http://cp", "token", runner="r1", transport=transport)
    raised: Exception | None = None
    try:
        await REPLIES[call](client)
    except Exception as e:
        raised = e
    finally:
        await client.aclose()
    assert len(sent) <= 3
    assert type(raised).__name__ == ("ApiError" if call == "error" else "TooLarge")
    assert f"passes {mib} bytes" in str(raised)
