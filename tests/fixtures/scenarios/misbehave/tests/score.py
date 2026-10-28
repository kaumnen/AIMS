import json
import os
from pathlib import Path

import pytest


class Tally:
    def __init__(self) -> None:
        self.passed = 0
        self.total = 0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            self.total += 1
            self.passed += int(report.passed)


tally = Tally()
pytest.main(["-q", "-p", "no:cacheprovider", "/work"], plugins=[tally])
reward = tally.passed / tally.total if tally.total else 0.0
result = {"reward": reward, "passed": tally.passed, "total": tally.total}
Path(os.environ["AIMS_LOGS_DIR"], "reward.json").write_text(json.dumps(result))
