from pathlib import Path

import pytest

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
