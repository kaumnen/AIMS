"""Where a series goes on, for `aims run --series` and for a series queued on the control
plane alike."""

from collections.abc import Iterable
from typing import Any


async def last_of_series(
    store: Any, name: str, indexes: Iterable[int] = ()
) -> tuple[Any | None, int] | None:
    """Where series `name` goes on: its completed run with the highest index, whose folders
    and canaries the next run inherits (None when none completed), and the index after every
    run of it and every one of `indexes` (the runs of it not started yet); None when it has no
    run at all."""
    return series_end(await store.runs(series=name, kind="scenario", limit=1000), indexes)


async def series_start(store: Any, name: str, restart: bool = False) -> tuple[Any | None, int]:
    """Where `aims run --series` starts: the completed run it goes on after (None for a new
    series or a `restart`, which starts fresh) and the index of its first run, the next free
    one even on a restart, so indexes are never taken twice."""
    found = await last_of_series(store, name)
    last, start = found if found is not None else (None, 0)
    return None if restart else last, start


def series_end(infos: Iterable[Any], indexes: Iterable[int] = ()) -> tuple[Any | None, int] | None:
    """`last_of_series` over the runs `infos` of the series, already read."""
    runs = [i for i in infos if i.series_index is not None]
    taken = [*(i.series_index for i in runs), *indexes]
    if not taken:
        return None
    done = [i for i in runs if i.status == "completed"]
    last = max(done, key=lambda i: (i.series_index, i.run_id)) if done else None
    return last, max(taken) + 1
