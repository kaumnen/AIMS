import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from typing import get_args

import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.stream import Stream

from aims.events import MAX_INT, FsChangeType, FsKind
from aims.sandbox.base import Change, Tree

log = logging.getLogger("aims.sandbox")

FILE_CAP = 1024 * 1024
BLOB_CAP = 16 * 1024 * 1024
ENTRY_CAP = 100_000
HASH_CAP = 64 * 1024 * 1024
TREE_S = 60.0
MOUNT = "/r"
HELPER = "/reader/aims-snap"

_CHANGES: dict[str, FsChangeType] = {
    "A": "added",
    "M": "modified",
    "T": "meta",
    "D": "removed",
    "K": "kind",
}
_KINDS: dict[str, FsKind] = dict(zip("fdlpso", get_args(FsKind), strict=True))


class ReaderError(Exception):
    pass


class Reader:
    """An unprivileged sidecar that hashes a sandbox's volumes, mounted read-only.

    It runs as root with only CAP_DAC_READ_SEARCH, so it reads files the agent locked away, and
    keeps the last manifest in memory so each diff hashes only what moved.
    """

    def __init__(self, container: DockerContainer, roots: tuple[str, ...]) -> None:
        self._container = container
        self.roots = roots
        self._stream: Stream | None = None
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self.broken: str | None = None

    @classmethod
    async def start(
        cls,
        docker: aiodocker.Docker,
        image: str,
        name: str,
        volumes: Sequence[tuple[str, str]],
        labels: dict[str, str],
    ) -> Reader:
        container = await docker.containers.create(
            config={
                "Image": image,
                "Entrypoint": ["/bin/busybox"],
                "Cmd": ["sleep", "infinity"],
                "User": "0:0",
                "Labels": {**labels, "aims.role": "reader"},
                "NetworkDisabled": True,
                "HostConfig": {
                    "NetworkMode": "none",
                    "CapDrop": ["ALL"],
                    "CapAdd": ["DAC_READ_SEARCH"],
                    "SecurityOpt": ["no-new-privileges"],
                    "ReadonlyRootfs": True,
                    "PidsLimit": 16,
                    "Memory": 512 * 1024 * 1024,
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Source": volume,
                            "Target": f"{MOUNT}{target}",
                            "ReadOnly": True,
                            "VolumeOptions": {"NoCopy": True},
                        }
                        for volume, target in volumes
                    ],
                    "LogConfig": {"Type": "none"},
                },
            },
            name=name,
        )
        reader = cls(container, tuple(target for _, target in volumes))
        try:
            await container.start()
            args = [f"{target}={MOUNT}{target}" for _, target in volumes]
            exe = await container.exec([HELPER, *args], stdin=True, stdout=True, stderr=True)
            reader._stream = await exe.start(detach=False).__aenter__()
        except BaseException:
            await reader.close()
            raise
        return reader

    async def tree(self) -> Tree:
        async with self._lock:
            if self.broken is not None:
                raise ReaderError(self.broken)
            start = time.monotonic()
            try:
                async with asyncio.timeout(TREE_S):
                    return await self._tree(start)
            except asyncio.CancelledError:
                self.broken = "file reader was cancelled mid-reply"
                await self._end()
                raise
            except Exception as e:
                self.broken = f"file reader failed: {type(e).__name__}: {e}"
                await self._end()
                raise ReaderError(self.broken) from e

    async def close(self) -> None:
        await self._end()
        with contextlib.suppress(aiodocker.DockerError):
            await self._container.delete(force=True)

    async def _end(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.close()
            self._stream = None

    async def _tree(self, start: float) -> Tree:
        stream = self._stream
        if stream is None:
            raise ReaderError("the file reader is closed")
        await stream.write_in(f"S {FILE_CAP} {BLOB_CAP} {ENTRY_CAP} {HASH_CAP}\n".encode())
        changes: list[Change] = []
        while True:
            line = (await self._line()).decode()
            f = line.split(" ")
            if f[0] == "E" and len(f) == 9:
                entries, files, size, hashed, unhashed, skipped, truncated = map(int, f[1:8])
                return Tree(
                    roots=self.roots,
                    digest=f[8],
                    entries=entries,
                    files=files,
                    bytes=_int(size),
                    hashed_bytes=hashed,
                    unhashed=unhashed,
                    content_skipped=skipped,
                    truncated=truncated == 1,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    changes=tuple(changes),
                )
            if f[0] != "C" or len(f) != 11:
                raise ReaderError(f"unexpected line from the file reader: {line[:200]!r}")
            change, kind, size, mode, mtime_us, sha = f[1:7]
            content_len, timestomp, path_len, target_len = map(int, f[7:11])
            path = _text(await self._take(path_len))
            target = _text(await self._take(target_len)) if target_len else None
            data = await self._take(content_len) if content_len else None
            changes.append(
                Change(
                    path=path,
                    change=_CHANGES[change],
                    kind=_KINDS[kind],
                    size=_int(int(size)),
                    mode=int(mode),
                    mtime_us=_int(int(mtime_us)),
                    sha256=None if sha == "-" else sha,
                    data=data,
                    target=target,
                    timestomp=timestomp == 1,
                )
            )

    async def _fill(self) -> None:
        stream = self._stream
        if stream is None:
            raise ReaderError("the file reader is closed")
        message = await stream.read_out()
        if message is None:
            raise ReaderError("the file reader exited")
        if message.stream == 2:
            log.warning("file reader: %s", message.data.decode(errors="replace").strip())
        else:
            self._buf += message.data

    async def _line(self) -> bytes:
        while (end := self._buf.find(b"\n")) < 0:
            await self._fill()
        line = bytes(self._buf[:end])
        del self._buf[: end + 1]
        return line

    async def _take(self, size: int) -> bytes:
        while len(self._buf) < size:
            await self._fill()
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data


def _int(value: int) -> int:
    return max(-MAX_INT, min(value, MAX_INT))


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="backslashreplace")
