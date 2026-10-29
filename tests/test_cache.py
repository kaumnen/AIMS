import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aims.events import CacheProbe, FileSync, LocalEventStore, RunStarted
from aims.sandbox import AuditedSandbox, DockerBackend, SandboxSpec

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

IMAGE = "python:3.13-slim"

CALLS = """\
python3 - <<'PY'
import ctypes, mmap, os

libc = ctypes.CDLL(None, use_errno=True)
path = "/work/page"
with open(path, "wb") as f:
    f.write(b"x" * 4096)
fd = os.open(path, os.O_RDONLY)
nextra = ctypes.create_string_buffer(4096)
iov = (ctypes.c_void_p * 2)(ctypes.cast(nextra, ctypes.c_void_p), 4096)
libc.posix_fadvise.argtypes = [ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_int]
libc.posix_fadvise(fd, 0, 0, 4)
nr = {"x86_64": 327, "aarch64": 286}[os.uname().machine]
libc.syscall.restype = ctypes.c_long
libc.syscall(nr, fd, ctypes.byref(iov), 1, 0, 0, 0x8)
class Range(ctypes.Structure):
    _fields_ = [("off", ctypes.c_ulonglong), ("len", ctypes.c_ulonglong)]
class Stat(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in ("nr", "cache", "dirty", "writeback", "evicted", "recent")
    ]
rng, stat = Range(0, 4096), Stat()
libc.syscall(451, fd, ctypes.byref(rng), ctypes.byref(stat), 0)
mapped = mmap.mmap(-1, 4096)
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
view = (ctypes.c_char * len(mapped)).from_buffer(mapped)
libc.mincore(ctypes.addressof(view), 4096, nextra)
del view
mapped.close()

with open("/work/sync", "wb") as f:
    f.write(b"y" * 4096)
    f.flush()
    os.fsync(f.fileno())
    os.fdatasync(f.fileno())
    libc.sync_file_range(f.fileno(), 0, 0, 0)
    libc.syncfs(f.fileno())
os.close(fd)
PY
"""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def backend() -> AsyncIterator[DockerBackend]:
    try:
        audit = "kernel" if os.environ.get("AIMS_REQUIRE_TRACER") else "auto"
        backend = await DockerBackend.open(audit=audit)
    except Exception as e:
        if os.environ.get("AIMS_REQUIRE_DOCKER"):
            raise
        pytest.skip(f"Docker is not available: {e}")
    async with backend:
        yield backend


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async with await LocalEventStore.open(tmp_path / "runs") as store:
        yield store


@pytest.fixture
def traced(backend: DockerBackend) -> None:
    if backend.tracer is None:
        if os.environ.get("AIMS_REQUIRE_TRACER"):
            pytest.fail("the kernel tracer did not start")
        pytest.skip("the kernel tracer is not available here")


async def sandbox(
    backend: DockerBackend, store: LocalEventStore, **spec: Any
) -> tuple[str, AuditedSandbox]:
    run_id = await store.create_run(RunStarted(scenario="t", oversight="observe", agents=["a1"]))
    audited = await AuditedSandbox.start(
        backend, SandboxSpec(image=IMAGE, **spec), store, run_id=run_id, agent_id="a1"
    )
    return run_id, audited


@pytest.mark.tracer
@pytest.mark.usefixtures("traced")
async def test_page_cache_and_durability_calls_become_events(
    backend: DockerBackend, store: LocalEventStore
) -> None:
    run_id, sb = await sandbox(backend, store)
    try:
        result = await sb.exec(CALLS, timeout_s=30)
    finally:
        await sb.close()

    assert result.finished.exit_code == 0, result.stderr
    evs = await store.events(run_id, limit=1000)
    probes = [
        e.payload()
        for e in evs
        if e.type == "cache.probe" and e.data.get("exec_id") == result.finished.exec_id
    ]
    probes = [p for p in probes if isinstance(p, CacheProbe)]
    seen = {(p.op, p.path) for p in probes}
    assert ("fadvise_dontneed", "/work/page") in seen
    assert ("preadv2_nowait", "/work/page") in seen
    assert ("cachestat", "/work/page") in seen
    assert ("mincore", None) in seen
    assert all(not p.partial for p in probes)

    syncs = [
        e.payload()
        for e in evs
        if e.type == "file.sync" and e.data.get("exec_id") == result.finished.exec_id
    ]
    syncs = [p for p in syncs if isinstance(p, FileSync)]
    assert {p.call for p in syncs} == {
        "fsync",
        "fdatasync",
        "sync_file_range",
        "syncfs",
    }
    assert all(p.path in ("/work/sync", None) for p in syncs)
