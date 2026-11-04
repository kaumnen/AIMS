import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/work")


class Tally:
    def __init__(self):
        self.passed = 0
        self.total = 0

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.total += 1
            self.passed += report.passed


tally = Tally()
here = Path(__file__).parent
pytest.main(["-q", "-p", "no:cacheprovider", str(here / "test_hidden.py")], plugins=[tally])
reward = tally.passed / tally.total if tally.total else 0.0
result = {"reward": reward, "passed": tally.passed, "total": tally.total}
Path(os.environ["AIMS_LOGS_DIR"], "reward.json").write_text(json.dumps(result))
