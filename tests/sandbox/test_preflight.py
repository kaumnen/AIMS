"""The check that keeps the kernel tracer off a Docker host that cannot run it."""

import logging
import subprocess
from pathlib import Path
from typing import Any, cast

import aiodocker
import pytest

from aims.sandbox import docker as docker_module
from aims.sandbox.docker import DockerBackend, SandboxError
from aims.sandbox.tracer import BTF, TRACEFS, Tracer, TracerError, Unsupported, preflight

pytestmark = pytest.mark.anyio

# KernelVersion as `docker info` gave it on Ubuntu 24.04's GA kernel, Debian 13 and Docker
# Desktop on an Apple silicon Mac
UBUNTU_GA = "6.8.0-147-generic"
DEBIAN_13 = "6.12.117+deb13-amd64"
DOCKER_DESKTOP = "7.0.14-linuxkit"
TOO_OLD = f"needs Linux 6.11 or newer; the Docker host runs {UBUNTU_GA}"
NO_BTF = "needs a kernel built with BTF type data; the Docker host has no /sys/kernel/btf/vmlinux"
NO_TRACEFS = (
    "needs tracefs mounted at /sys/kernel/tracing; the Docker host has none there and the tracer"
    " cannot mount it"
)


def info(
    kernel: str | None = DEBIAN_13, cgroup: str | None = "2", arch: str = "x86_64"
) -> dict[str, Any]:
    found: dict[str, Any] = {"Architecture": arch}
    if kernel is not None:
        found["KernelVersion"] = kernel
    if cgroup is not None:
        found["CgroupVersion"] = cgroup
    return found


@pytest.mark.parametrize(
    "kernel",
    [DEBIAN_13, DOCKER_DESKTOP, "6.11.0-29-generic", "6.17.0-42-generic", "7.0.0-43-generic"],
)
def test_a_tested_kernel_passes_without_a_warning(kernel: str) -> None:
    assert preflight(info(kernel)) == (None, None)


@pytest.mark.parametrize(
    "kernel", [UBUNTU_GA, "6.9.12", "6.10.14", "6.1.0-28-amd64", "5.15.0-126-generic", "4.19.0"]
)
def test_a_kernel_older_than_6_11_cannot_run_the_tracer(kernel: str) -> None:
    """6.9 is older than 6.11 though 6.9 is the larger number as a decimal."""
    assert preflight(info(kernel)) == (
        f"needs Linux 6.11 or newer; the Docker host runs {kernel}",
        None,
    )


@pytest.mark.parametrize("kernel", ["7.1.2-arch1-1", "7.10.0", "8.0.0"])
def test_a_kernel_newer_than_any_tested_runs_with_a_warning(kernel: str) -> None:
    assert preflight(info(kernel)) == (
        None,
        f"the Docker host runs Linux {kernel}, newer than any kernel the tracer was tested on"
        " (7.0); kernel audit may fail or miss calls",
    )


def test_cgroup_v1_cannot_run_the_tracer() -> None:
    assert preflight(info(cgroup="1")) == ("needs cgroup v2; the Docker host uses cgroup v1", None)


@pytest.mark.parametrize("arch", ["armv7l", "s390x", "ppc64le", "riscv64"])
def test_an_architecture_the_tracer_was_not_built_for_cannot_run_it(arch: str) -> None:
    assert preflight(info(arch=arch)) == (
        f"needs an x86_64 or aarch64 Docker host; this one is {arch}",
        None,
    )


@pytest.mark.parametrize("arch", ["x86_64", "aarch64", "amd64", "arm64"])
def test_x86_64_and_aarch64_can_run_the_tracer(arch: str) -> None:
    assert preflight(info(arch=arch)) == (None, None)


def test_every_reason_is_given() -> None:
    assert preflight(info("5.4.0", "1", "armv7l")) == (
        "needs Linux 6.11 or newer; the Docker host runs 5.4.0; needs cgroup v2; the Docker"
        " host uses cgroup v1; needs an x86_64 or aarch64 Docker host; this one is armv7l",
        None,
    )


@pytest.mark.parametrize("kernel", [None, "", "unknown", "v6"])
@pytest.mark.parametrize("cgroup", [None, ""])
def test_fields_docker_leaves_out_or_that_do_not_parse_are_no_reason(
    kernel: str | None, cgroup: str | None
) -> None:
    """The tracer is tried as it was before the check."""
    assert preflight(info(kernel, cgroup)) == (None, None)


def btf(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """The tracer script's BTF check, looking for the type data at a path under `tmp_path`."""
    test = "[ -e /sys/kernel/btf/vmlinux ]"
    assert test in BTF
    script = BTF.replace(test, f"[ -e {tmp_path}/vmlinux ]") + "echo started"
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)


async def test_a_host_without_btf_type_data_is_unsupported(tmp_path: Path) -> None:
    ran = btf(tmp_path)
    assert ran.returncode == 1
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    for line in ran.stdout.splitlines():
        tracer._feed(line.encode())
    assert tracer._ready.done()
    error = tracer._ready.exception()
    assert isinstance(error, Unsupported)
    assert str(error) == NO_BTF


def test_a_host_with_btf_type_data_goes_on(tmp_path: Path) -> None:
    (tmp_path / "vmlinux").touch()
    ran = btf(tmp_path)
    assert (ran.returncode, ran.stdout) == (0, "started\n")


def tracefs(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """The tracer script's tracefs check with `tmp_path` for /sys/kernel/tracing and a mount
    that fails, as AppArmor makes it."""
    mount = "mount -t tracefs nodev /sys/kernel/tracing"
    assert mount in TRACEFS
    script = TRACEFS.replace(mount, "false").replace("/sys/kernel/tracing/", f"{tmp_path}/")
    return subprocess.run(
        ["sh", "-c", script + "echo started"], capture_output=True, text=True, check=False
    )


async def test_a_host_without_tracefs_the_tracer_cannot_mount_is_unsupported(
    tmp_path: Path,
) -> None:
    ran = tracefs(tmp_path)
    assert ran.returncode == 1
    tracer = Tracer(cast(aiodocker.Docker, None), {}, "t")
    for line in ran.stdout.splitlines():
        tracer._feed(line.encode())
    error = tracer._ready.exception()
    assert isinstance(error, Unsupported)
    assert str(error) == NO_TRACEFS


def test_a_host_with_tracefs_goes_on(tmp_path: Path) -> None:
    (tmp_path / "available_filter_functions").touch()
    ran = tracefs(tmp_path)
    assert (ran.returncode, ran.stdout) == (0, "started\n")


class Daemon:
    """The Docker daemon `DockerBackend.open` connects to: `docker info` only."""

    def __init__(self, found: dict[str, Any]) -> None:
        self.system = self
        self.found = found

    async def info(self) -> dict[str, Any]:
        return self.found

    async def close(self) -> None:
        pass


class Started:
    """What `open` and `restart_tracer` did, with the error the next tracer start raises."""

    def __init__(self) -> None:
        self.tracers = 0
        self.toolbox = 0
        self.error: BaseException | None = None


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Started:
    """`DockerBackend.open` with its helpers and the tracer stubbed; the daemon is `use`d."""
    done = Started()

    class FakeTracer:
        def __init__(self, *_: Any, **__: Any) -> None:
            self.alive = False

        async def start(self) -> None:
            done.tracers += 1
            if done.error is not None:
                raise done.error
            self.alive = True

        async def stop(self) -> None:
            self.alive = False

    async def toolbox(*_: Any) -> tuple[str, str]:
        done.toolbox += 1
        return "aims-toolbox:x", "v"

    async def nothing(*_: Any) -> None:
        return None

    monkeypatch.setattr(docker_module, "Tracer", FakeTracer)
    monkeypatch.setattr(docker_module, "_own_container", nothing)
    monkeypatch.setattr(docker_module, "_sweep", nothing)
    monkeypatch.setattr(docker_module, "_toolbox", toolbox)
    monkeypatch.setattr(docker_module, "_remove_old", nothing)
    monkeypatch.setattr(docker_module, "_sampler", nothing)
    return done


def use(monkeypatch: pytest.MonkeyPatch, found: dict[str, Any]) -> None:
    monkeypatch.setattr(docker_module, "docker_client", lambda url=None: Daemon(found))


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def test_an_auto_backend_on_an_old_kernel_starts_no_tracer_and_says_why(
    started: Started, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, info(UBUNTU_GA))
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        backend = await DockerBackend.open(audit="auto")
    async with backend:
        assert backend.tracer is None
        assert backend.unsupported == TOO_OLD
        with pytest.raises(SandboxError) as caught:
            await backend.restart_tracer()
        assert str(caught.value) == f"kernel audit unavailable: {TOO_OLD}"
    assert started.tracers == 0
    assert warnings(caplog) == [f"kernel audit unavailable, using snapshot audit: {TOO_OLD}"]


async def test_a_kernel_backend_on_an_old_kernel_refuses_before_it_makes_anything(
    started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, info(UBUNTU_GA))
    with pytest.raises(SandboxError) as caught:
        await DockerBackend.open(audit="kernel")
    assert str(caught.value) == f"kernel audit unavailable: {TOO_OLD}"
    assert (started.tracers, started.toolbox) == (0, 0)


async def test_a_snapshot_backend_does_not_check_the_kernel(
    started: Started, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, info(UBUNTU_GA, "1", "armv7l"))
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        backend = await DockerBackend.open(audit="snapshot")
    async with backend:
        assert backend.unsupported is None
    assert started.tracers == 0
    assert warnings(caplog) == []


async def test_a_kernel_newer_than_any_tested_gets_the_tracer_and_one_warning(
    started: Started, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, info("7.1.2-arch1-1"))
    with caplog.at_level(logging.WARNING, "aims.sandbox"):
        backend = await DockerBackend.open(audit="kernel")
    async with backend:
        assert backend.tracer is not None
        assert backend.unsupported is None
    assert started.tracers == 1
    assert warnings(caplog) == [preflight(info("7.1.2-arch1-1"))[1]]


@pytest.mark.parametrize("audit", ["auto", "kernel"])
async def test_a_host_without_btf_type_data_is_never_tried_again(
    audit: str, started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, info())
    started.error = Unsupported(NO_BTF)
    if audit == "kernel":
        with pytest.raises(SandboxError) as caught:
            await DockerBackend.open(audit="kernel")
        assert str(caught.value) == f"kernel audit unavailable: {NO_BTF}"
        return
    backend = await DockerBackend.open(audit="auto")
    async with backend:
        assert backend.tracer is None
        assert backend.unsupported == NO_BTF
        with pytest.raises(SandboxError, match=NO_BTF):
            await backend.restart_tracer()
    assert started.tracers == 1


async def test_a_tracer_that_fails_another_way_is_tried_again(
    started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, info())
    started.error = TracerError("the tracer stopped before it was ready")
    backend = await DockerBackend.open(audit="auto")
    async with backend:
        assert backend.unsupported is None
        with pytest.raises(SandboxError, match="stopped before it was ready"):
            await backend.restart_tracer()
        assert backend.unsupported is None
        started.error = None
        await backend.restart_tracer()
        assert backend.tracer is not None
        assert backend.tracer_restarts == 1
    assert started.tracers == 3
