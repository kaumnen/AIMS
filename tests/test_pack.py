"""Packing a folder of runs to share: `organism/pack.py`."""

import subprocess
import sys
import tarfile
from pathlib import Path

from aims.events.store import GRAVE

ROOT = Path(__file__).resolve().parents[1]
RUN = "019a6f00-0000-7000-8000-000000000001"


def test_a_pack_leaves_out_a_deleted_runs_grave(tmp_path: Path) -> None:
    folder = tmp_path / "aims-organism-runs"
    for run_dir in (folder / "runs" / RUN, folder / "runs" / f"{GRAVE}{RUN}.0a1b2c3d"):
        (run_dir / "blobs").mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("{}\n")
    (folder / "README.md").write_text("Runs.\n")
    subprocess.run(
        [sys.executable, "-m", "organism.pack", str(folder)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    with tarfile.open(tmp_path / "aims-organism-runs.tar.gz") as tar:
        names = tar.getnames()
    assert f"aims-organism-runs/runs/{RUN}/events.jsonl" in names
    assert "aims-organism-runs/README.md" in names
    assert not [n for n in names if GRAVE in n]
