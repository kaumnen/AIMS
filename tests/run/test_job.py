import hashlib
import tracemalloc
from compression import zstd
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue
from scripted import profile

from aims.models import ProfileError, load_models
from aims.node.client import ControlClient
from aims.node.service import job_inputs
from aims.run.job import Job, JobFork

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

    async def job_blob(self, run_id: str, sha256: str) -> bytes:
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
