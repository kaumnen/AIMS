"""The pins Dependabot does not move. Finds each in the workflows, looks up the newest release that
is at least COOLDOWN_DAYS old and writes a Markdown note of the pins behind it. The note is empty
when every pin is current. Standard library only, so the weekly job runs it with the runner's
Python."""

import argparse
import json
import os
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPResponse
from pathlib import Path

COOLDOWN_DAYS = 3
WORKFLOWS = Path(".github/workflows")
VERSION = re.compile(r"\d+(\.\d+)*")
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)


@dataclass(frozen=True)
class Release:
    version: str
    at: datetime


@dataclass(frozen=True)
class Found:
    path: Path
    line: int
    version: str


@dataclass(frozen=True)
class Pin:
    """`pattern` finds the pin with its version in group `v`. `step` limits it to lines inside a
    step that uses that action. `image` is the image whose digest goes with the version, as a
    format string over the version."""

    name: str
    pattern: re.Pattern[str]
    releases: Callable[[], list[Release]]
    step: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class Behind:
    pin: Pin
    newest: str
    found: list[Found]
    ref: str | None


def _get(url: str, headers: dict[str, str] | None = None, method: str = "GET") -> HTTPResponse:
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "pins-check"})
    for k, v in (headers or {}).items():
        request.add_header(k, v)
    return urllib.request.urlopen(request, timeout=30)


def _json(url: str, headers: dict[str, str] | None = None) -> object:
    with _get(url, headers) as r:
        return json.load(r)


def _when(text: str) -> datetime:
    at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def github(repo: str, prefix: str = "") -> Callable[[], list[Release]]:
    def releases() -> list[Release]:
        headers = {"Accept": "application/vnd.github+json"}
        if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        data = _json(f"https://api.github.com/repos/{repo}/releases?per_page=50", headers)
        assert isinstance(data, list)
        return [
            Release(r["tag_name"].removeprefix(prefix), _when(r["published_at"]))
            for r in data
            if not r["draft"] and not r["prerelease"] and r["tag_name"].startswith(prefix)
        ]

    return releases


def pypi(name: str) -> Callable[[], list[Release]]:
    def releases() -> list[Release]:
        data = _json(f"https://pypi.org/pypi/{name}/json")
        assert isinstance(data, dict)
        return [
            Release(version, min(_when(f["upload_time_iso_8601"]) for f in files))
            for version, files in data["releases"].items()
            if files and not any(f.get("yanked") for f in files)
        ]

    return releases


def node(major: int) -> Callable[[], list[Release]]:
    def releases() -> list[Release]:
        data = _json("https://nodejs.org/dist/index.json")
        assert isinstance(data, list)
        return [
            Release(r["version"].removeprefix("v"), _when(r["date"]))
            for r in data
            if r["version"].startswith(f"v{major}.")
        ]

    return releases


def digest(image: str) -> str:
    """The digest of a Docker Hub tag's image index, as `docker buildx imagetools` prints it."""
    name, tag = image.split(":")
    token = _json(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull"
    )
    assert isinstance(token, dict)
    headers = {"Authorization": f"Bearer {token['token']}", "Accept": ", ".join(INDEX_TYPES)}
    url = f"https://registry-1.docker.io/v2/{name}/manifests/{tag}"
    with _get(url, headers, method="HEAD") as r:
        return r.headers["Docker-Content-Digest"]


def key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def newest(releases: Iterable[Release], now: datetime) -> str | None:
    """The highest plain version released at least COOLDOWN_DAYS before `now`."""
    cutoff = now - timedelta(days=COOLDOWN_DAYS)
    ok = [r.version for r in releases if VERSION.fullmatch(r.version) and r.at <= cutoff]
    return max(ok, key=key, default=None)


def find(pin: Pin, root: Path) -> list[Found]:
    out = []
    for path in sorted(root.glob("*.yml")):
        step = None
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "uses:" in line:
                step = line
            if pin.step is not None and (step is None or pin.step not in step):
                continue
            if m := pin.pattern.search(line):
                out.append(Found(path, n, m["v"]))
    return out


def behind(pins: Iterable[Pin], root: Path, now: datetime) -> list[Behind]:
    out = []
    for pin in pins:
        found = find(pin, root)
        if not found:
            raise SystemExit(f"{pin.name}: no pin found in {root}")
        top = newest(pin.releases(), now)
        if top is None:
            raise SystemExit(f"{pin.name}: no release found")
        late = [f for f in found if key(f.version) < key(top)]
        if late:
            ref = None if pin.image is None else _ref(pin.image.format(top))
            out.append(Behind(pin, top, late, ref))
    return out


def _ref(image: str) -> str:
    return f"{image}@{digest(image)}"


def _where(f: Found, repo: str | None) -> str:
    label = f"{f.path.name}:{f.line}"
    if repo is None:
        return f"`{label}`"
    return f"[{label}](https://github.com/{repo}/blob/main/{f.path.as_posix()}#L{f.line})"


def render(late: list[Behind], repo: str | None) -> str:
    if not late:
        return ""
    rows = [
        "Dependabot does not update these pins. Each has a newer release that is at least"
        f" {COOLDOWN_DAYS} days old. This issue updates every week and closes once all are"
        " current.",
        "",
        "| Pin | Now | Newest | Where |",
        "| --- | --- | --- | --- |",
    ]
    for b in late:
        now = ", ".join(sorted({f.version for f in b.found}, key=key))
        where = ", ".join(_where(f, repo) for f in b.found)
        rows.append(f"| {b.pin.name} | {now} | {b.newest} | {where} |")
    refs = [b for b in late if b.ref]
    if refs:
        rows += ["", "New image references, digest included:", ""]
        rows += [f"- {b.pin.name}: `{b.ref}`" for b in refs]
    return "\n".join(rows) + "\n"


def pins(node_major: int) -> list[Pin]:
    return [
        Pin(
            "uv in setup-uv",
            re.compile(r'^\s+version: "(?P<v>[\d.]+)"'),
            github("astral-sh/uv"),
            step="astral-sh/setup-uv@",
        ),
        Pin("Node", re.compile(r'node-version: "(?P<v>[\d.]+)"'), node(node_major)),
        Pin("Bun", re.compile(r'bun-version: "(?P<v>[\d.]+)"'), github("oven-sh/bun", "bun-v")),
        Pin("zizmor", re.compile(r"zizmor@(?P<v>[\d.]+)"), pypi("zizmor")),
        Pin("pip-audit", re.compile(r"pip-audit@(?P<v>[\d.]+)"), pypi("pip-audit")),
        Pin(
            "BuildKit",
            re.compile(r"moby/buildkit:v(?P<v>[\d.]+)@"),
            github("moby/buildkit", "v"),
            image="moby/buildkit:v{}",
        ),
        Pin(
            "SBOM scanner",
            re.compile(r"docker/buildkit-syft-scanner:(?P<v>[\d.]+)@"),
            github("docker/buildkit-syft-scanner", "v"),
            image="docker/buildkit-syft-scanner:{}",
        ),
    ]


def node_major(root: Path) -> int:
    probe = Pin("Node", re.compile(r'node-version: "(?P<v>\d+)\.'), list)
    found = find(probe, root)
    if not found:
        raise SystemExit(f"no node-version in {root}")
    return int(found[0].version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Where the note goes.")
    parser.add_argument("--workflows", type=Path, default=WORKFLOWS)
    args = parser.parse_args()
    late = behind(pins(node_major(args.workflows)), args.workflows, datetime.now(UTC))
    args.out.write_text(render(late, os.environ.get("GITHUB_REPOSITORY")))
    for b in late:
        print(f"{b.pin.name}: {', '.join(sorted({f.version for f in b.found}))} -> {b.newest}")
    if not late:
        print("every pin is current")


if __name__ == "__main__":
    main()
