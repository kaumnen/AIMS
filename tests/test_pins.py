"""The weekly check of the pins Dependabot does not move (.github/pins.py)."""

import importlib.util
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

NOW = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    path = Path(__file__).parents[1] / ".github" / "pins.py"
    spec = importlib.util.spec_from_file_location("pins", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def days(n: float) -> datetime:
    return NOW - timedelta(days=n)


def test_newest_skips_young_and_pre_releases_and_compares_numbers(pins: ModuleType) -> None:
    Release = pins.Release
    releases = [
        Release("0.9.9", days(30)),
        Release("0.10.0", days(4)),
        Release("0.10.1", days(1)),
        Release("0.11.0rc1", days(10)),
    ]
    assert pins.newest(releases, NOW) == "0.10.0"
    assert pins.newest(releases[2:], NOW) is None


def test_a_step_pin_is_found_only_inside_that_step(pins: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text(
        "steps:\n"
        "  - uses: astral-sh/setup-uv@abc # v10\n"
        "    with:\n"
        '      version: "0.13.5"\n'
        "  - uses: other/tool@def # v1\n"
        "    with:\n"
        '      version: "9.9.9"\n'
    )
    pin = pins.Pin("uv", re.compile(r'^\s+version: "(?P<v>[\d.]+)"'), list, step="setup-uv@")
    assert [(f.line, f.version) for f in pins.find(pin, tmp_path)] == [(4, "0.13.5")]


def test_the_note_lists_only_late_pins_with_their_new_image(
    pins: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.yml").write_text("run: uvx zizmor@1.31.1\n")
    (tmp_path / "b.yml").write_text(
        "run: uvx zizmor@1.32.0\nBUILDKIT: moby/buildkit:v0.34.1@sha256:aa\n"
    )
    monkeypatch.setattr(pins, "digest", lambda image: f"sha256:{image.split(':')[1]}")
    Pin, Release = pins.Pin, pins.Release
    tools = [
        Pin("zizmor", re.compile(r"zizmor@(?P<v>[\d.]+)"), lambda: [Release("1.32.0", days(8))]),
        Pin(
            "BuildKit",
            re.compile(r"moby/buildkit:v(?P<v>[\d.]+)@"),
            lambda: [Release("0.34.2", days(7))],
            image="moby/buildkit:v{}",
        ),
    ]
    late = pins.behind(tools, tmp_path, NOW)
    assert [(b.pin.name, b.newest, [f.path.name for f in b.found]) for b in late] == [
        ("zizmor", "1.32.0", ["a.yml"]),
        ("BuildKit", "0.34.2", ["b.yml"]),
    ]
    note = pins.render(late, "owner/repo")
    assert "| zizmor | 1.31.1 | 1.32.0 | [a.yml:1](https://github.com/owner/repo/blob/main/" in note
    assert "- BuildKit: `moby/buildkit:v0.34.2@sha256:v0.34.2`" in note
    current = [Pin("zizmor", re.compile(r"zizmor@(?P<v>[\d.]+)"), list)]
    (tmp_path / "a.yml").write_text("run: uvx zizmor@1.32.0\n")
    with pytest.raises(SystemExit, match="no release found"):
        pins.behind(current, tmp_path, NOW)
    assert pins.render(pins.behind(tools[:1], tmp_path, NOW), None) == ""
