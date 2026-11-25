"""Where a series goes on, for `aims run --series` and for a series queued on the control
plane alike."""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from aims.run import truth
from aims.scenario import Bundle

log = logging.getLogger("aims.run")


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


async def scenario_changed(store: Any, previous: str, bundle: Bundle) -> bool:
    """Whether `bundle` is another scenario than the one series run `previous` ran: the public
    copy it makes with that run's seed, variant and inherited canaries is not the one the run
    recorded. A public copy holds the run's canaries, so the digests of two runs say nothing
    by themselves. False when it cannot be told: the run's truth file is not in `store` or
    does not read, or the run recorded no digest. It only warns, so it never raises."""
    try:
        first = await store.events(previous, limit=1)
        config = first[0].data.get("config") if first else None
        scenario = config.get("scenario") if isinstance(config, dict) else None
        recorded = scenario.get("digest") if isinstance(scenario, dict) else None
        if not isinstance(recorded, str):
            return False
        record = truth.read(store.root / previous)
        seed = bytes.fromhex(str(record["seed"]))
        variant, inherited = record.get("variant"), record.get("inherited") or []
    except Exception:
        return False
    try:
        made = await asyncio.to_thread(truth.prepare, bundle, variant, seed, inherited)
    except ValueError, OSError:
        # it no longer prepares as that run did
        return True
    except Exception:
        log.exception("the scenario could not be compared with the one run %s ran", previous)
        return False
    return recorded not in truth.digests(made)
