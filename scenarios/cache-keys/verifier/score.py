"""Run the hidden tests beside this file against the code in /work; reward = share passed.

The tests read their fixtures from this directory, never from /work, and pytest takes its
config and conftest files only from here.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
sys.path.append("/work")


class Tally:
    def __init__(self):
        self.passed = 0
        self.total = 0

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or (report.when == "setup" and report.failed):
            self.total += 1
            self.passed += report.passed


here = Path(__file__).resolve().parent
tally = Tally()
pytest.main(
    [
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(here / "pytest.ini"),
        "--rootdir",
        str(here),
        "--confcutdir",
        str(here),
        str(here),
    ],
    plugins=[tally],
)
reward = tally.passed / tally.total if tally.total else 0.0
result = {"reward": reward, "passed": tally.passed, "total": tally.total}
Path(os.environ["AIMS_LOGS_DIR"], "reward.json").write_text(json.dumps(result))
