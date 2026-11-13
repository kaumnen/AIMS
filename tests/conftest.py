import os
import signal
import threading
from collections.abc import Generator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

# Property tests run under pytest-xdist load, so neither an example nor its data is timed. The
# examples of a test share its fixtures, which suits the autouse config fixture below.
# Hypothesis loads its "ci" profile, which also fixes the examples, when it sees a CI environment.
for _profile in ("default", "ci"):
    settings.register_profile(
        _profile,
        settings.get_profile(_profile),
        deadline=None,
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )

# Typer colours and boxes its usage errors when it sees GITHUB_ACTIONS, which splits the text
# the tests look for. It reads this once at import, so it is set before any test imports typer;
# child processes get it too. The errors then read on CI as they do on a developer's machine.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

# Scenario projects under fixtures hold their own tests, which are the agent's to fix.
collect_ignore = ["fixtures"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def user_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the developer's own AIMS config, and any `aims ui` they have running, out of every
    test."""
    path = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("AIMS_CONFIG_DIR", str(path))
    monkeypatch.setenv("AIMS_SERVER_DIR", str(tmp_path_factory.mktemp("server")))
    return path


SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.fixture(autouse=True)
def signal_handlers() -> Generator[None]:
    """Put back the handlers a test leaves changed. A command run in process can leave a signal
    ignored, as `aims runner` under `aims ui` does with SIGTERM and SIGHUP, and the child of a
    later test would inherit that."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    before = {sig: signal.getsignal(sig) for sig in SIGNALS}
    yield
    for sig, handler in before.items():
        # None is a handler that Python did not set, which it cannot set back either
        if handler is not None and signal.getsignal(sig) is not handler:
            signal.signal(sig, handler)
