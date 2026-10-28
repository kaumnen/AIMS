"""The signals of a run over one shared state, with deduplication.

A signal flags once per (signal, agent, key), then again when the key's count reaches 10, 100,
1000 and every later power of ten, at once when the importance rises or a new subcategory shows,
and at `close` with the exact count when it changed since the key's last flag. Verifier-phase
hits keep their own keys, so their flags always carry the verifier phase. Past a signal's key cap
an agent's new keys share one overflow key. No hit is dropped: each one counts toward a flag.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from pydantic import JsonValue, ValidationError

from aims.events import Draft, Event, EventStore, MonitorDegraded, MonitorFlag
from aims.monitor.config import MonitorConfig, SignalOptions, digest
from aims.monitor.signals.base import Hit, Signal, SignalError, SignalSetup, registry
from aims.monitor.signals.rules import RuleSignal, rule_signal, validate_rules
from aims.monitor.state import State
from aims.scenario import Scenario

log = logging.getLogger("aims.monitor")

KEY_CAP = 100
EVIDENCE = 32
OVERFLOW = "<overflow>"


def _classes(config: MonitorConfig) -> dict[str, type[Signal]]:
    """Every signal the config names or ships with, built-in and plugin ones and one per
    scenario rule. Raises SignalError for a rule id that takes a signal's name."""
    known = registry()
    out: dict[str, type[Signal]] = dict(known)
    for rule in config.rules:
        if rule.id in known:
            raise SignalError(f"rule {rule.id}: the id is a signal's name")
        out[rule.id] = rule_signal(rule)
    return out


def _enabled(config: MonitorConfig, classes: dict[str, type[Signal]]) -> dict[str, type[Signal]]:
    rules = {r.id: r for r in config.rules}
    out: dict[str, type[Signal]] = {}
    for name, cls in classes.items():
        opts = config.signals.get(name, SignalOptions())
        if opts.enabled and (name not in rules or rules[name].enabled):
            out[name] = cls
    return out


def signal_versions(config: MonitorConfig) -> dict[str, str]:
    """Each enabled signal's name and version, plus `rules` with the hash of the rules in use,
    for the monitor identity hash."""
    if not config.signals_on:
        return {}
    enabled = _enabled(config, _classes(config))
    out = {name: cls.version for name, cls in sorted(enabled.items())}
    rules: dict[str, JsonValue] = {
        name: [r.model_dump(mode="json") for r in cls.rules]
        for name, cls in sorted(enabled.items())
        if issubclass(cls, RuleSignal)
    }
    paths: JsonValue = config.paths.model_dump(mode="json")
    out["rules"] = digest({"signals": rules, "paths": paths})
    return out


def _build(config: MonitorConfig, scenario: Scenario) -> tuple[list[_Active], list[str]]:
    problems = validate_rules(config.rules)
    try:
        classes = _classes(config)
    except SignalError as e:
        return [], [*problems, str(e)]
    unknown = sorted(set(config.signals) - set(classes))
    if unknown:
        known = ", ".join(sorted(classes))
        problems.append(f"unknown signal(s) {', '.join(unknown)}; known: {known}")
    if problems:
        return [], problems
    out: list[_Active] = []
    for name, cls in _enabled(config, classes).items():
        opts = config.signals.get(name, SignalOptions())
        try:
            signal = cls(SignalSetup(config, scenario, opts.options))
        except SignalError as e:
            problems.append(str(e))
            continue
        cap = opts.key_cap or getattr(cls, "key_cap", None) or KEY_CAP
        out.append(_Active(signal, opts.importance, cap))
    return out, problems


def check_signals(config: MonitorConfig, scenario: Scenario) -> list[str]:
    """Every problem with the signals a config asks for: unknown signal names, options a
    signal does not take, and bad rules."""
    return _build(config, scenario)[1] if config.signals_on else validate_rules(config.rules)


@dataclass(slots=True)
class _Key:
    """What a key has seen and flagged."""

    lead: Hit
    count: int = 0
    flagged_count: int = 0
    flagged_importance: int = 0
    subcategories: set[str | None] = field(default_factory=set)
    first_flag: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    last: Hit | None = None
    keys: set[str] = field(default_factory=set)


_Ident = tuple[str, str | None, str, bool]


@dataclass(slots=True)
class _Batch:
    """The drafts of one feed or close, with one flag per key."""

    drafts: list[Draft] = field(default_factory=list)
    slots: dict[_Ident, int] = field(default_factory=dict)
    about: dict[_Ident, Hit] = field(default_factory=dict)

    def put(self, ident: _Ident, draft: Draft, about: Hit) -> None:
        at = self.slots.get(ident)
        if at is None:
            self.slots[ident] = len(self.drafts)
            self.drafts.append(draft)
        else:
            self.drafts[at] = draft
        self.about[ident] = about


@dataclass(slots=True)
class _Active:
    signal: Signal
    importance: int | None
    key_cap: int
    failed: bool = False


def _power_of_ten(n: int) -> bool:
    if n < 10:
        return False
    while n % 10 == 0:
        n //= 10
    return n == 1


class SignalEngine:
    """Every enabled signal of a run over one shared derived state, with deduplication.

    `feed` takes each event of the run in seq order (the monitor's own events and model calls
    already skipped) and returns the `monitor.flag` drafts to write, deduplicated per (signal,
    agent, key). After writing them the caller passes the written events to `recorded`, before
    the next `feed`, so later flags of a key can name their first flag's seq. `close` returns
    the final exact counts. A signal that raises gives one `monitor.degraded` draft and keeps
    running. `run_id` names the run whose blobs hold the events' content: the origin, for a
    monitor run. Raises SignalError for a config the signals do not accept.
    """

    def __init__(
        self, config: MonitorConfig, scenario: Scenario, *, run_id: str, store: EventStore
    ) -> None:
        self.state = State(run_id=run_id, store=store)
        self.signals: list[_Active] = []
        if config.signals_on:
            self.signals, problems = _build(config, scenario)
            if problems:
                raise SignalError("; ".join(problems))
        self._any: list[_Active] = []
        self._by_type: dict[str, list[_Active]] = {}
        for active in self.signals:
            wanted = getattr(active.signal, "event_types", None)
            types = active.signal.types if wanted is None else wanted
            if not types and wanted is None:
                self._any.append(active)
            for t in types:
                self._by_type.setdefault(t, []).append(active)
        self._keys: dict[_Ident, _Key] = {}
        self._counts: dict[tuple[str, str | None], int] = {}

    @property
    def names(self) -> list[str]:
        return [a.signal.name for a in self.signals]

    async def feed(self, event: Event) -> list[Draft]:
        if not self.signals:
            return []
        records = await self.state.update(event)
        batch = _Batch()
        for record in records:
            for active in [*self._by_type.get(record.type, ()), *self._any]:
                try:
                    hits = await active.signal.feed(record, self.state)
                except Exception as e:
                    batch.drafts += self._failed(active, e, record)
                    continue
                for hit in hits:
                    self._add(active, hit, record, batch)
        return batch.drafts

    def recorded(self, events: Sequence[Event]) -> None:
        for e in events:
            if e.type != "monitor.flag":
                continue
            data = e.data
            if data.get("layer") != "signal" or data.get("first_flag") is not None:
                continue
            source = data.get("source")
            key = data.get("key") or ""
            if not isinstance(source, str) or not isinstance(key, str):
                continue
            entry = self._keys.get((source, e.agent_id, key, data.get("phase") == "verifier"))
            if entry is not None and entry.first_flag is None:
                entry.first_flag = e.seq

    async def close(self) -> list[Draft]:
        batch = _Batch()
        for active in self.signals:
            try:
                hits = await active.signal.close(self.state)
            except Exception as e:
                batch.drafts += self._failed(active, e, None)
                continue
            for hit in hits:
                self._add(active, hit, None, batch)
        for ident, entry in self._keys.items():
            if entry.count != entry.flagged_count and entry.last is not None:
                self._flag(ident, entry, entry.lead, entry.last, batch, final=True)
        return batch.drafts

    def _failed(self, active: _Active, error: Exception, event: Event | None) -> list[Draft]:
        name = active.signal.name
        at = f" at event {event.seq}" if event is not None else " at close"
        log.exception("signal %s failed%s", name, at, exc_info=error)
        if active.failed:
            return []
        active.failed = True
        return [
            Draft(
                MonitorDegraded(
                    part="signal",
                    reason=f"signal {name} failed{at}",
                    then="continue",
                    detail=f"{type(error).__name__}: {error}"[:2000],
                )
            )
        ]

    def _add(self, active: _Active, hit: Hit, event: Event | None, batch: _Batch) -> None:
        name = active.signal.name
        agent = hit.agent_id if hit.agent_id is not None else (event.agent_id if event else None)
        importance = min(5, max(1, active.importance or hit.importance))
        evidence = [s for s in hit.evidence if s >= 0] or ([event.seq] if event else [])
        if not evidence:
            log.warning("signal %s gave a hit with no evidence: %s", name, hit)
            evidence = [0]
        hit = replace(hit, importance=importance, evidence=evidence[:EVIDENCE], agent_id=agent)
        verifier = hit.phase == "verifier"
        ident: _Ident = (name, agent, hit.key, verifier)
        entry = self._keys.get(ident)
        if entry is None:
            used = self._counts.get((name, agent), 0)
            if used >= active.key_cap:
                ident = (name, agent, OVERFLOW, verifier)
                entry = self._keys.get(ident)
                if entry is None:
                    entry = self._keys[ident] = _Key(lead=hit)
                entry.keys.add(hit.key)
                hit = replace(
                    hit,
                    key=OVERFLOW,
                    justification=(
                        f"{len(entry.keys)} keys past the cap of {active.key_cap}; the latest,"
                        f" {hit.key}: {hit.justification}"
                    ),
                )
            else:
                self._counts[(name, agent)] = used + 1
                entry = self._keys[ident] = _Key(lead=hit)
        entry.count += 1
        lo, hi = min(hit.evidence), max(hit.evidence)
        entry.first_seq = lo if entry.first_seq is None else min(entry.first_seq, lo)
        entry.last_seq = hi if entry.last_seq is None else max(entry.last_seq, hi)
        entry.last = hit
        if hit.importance >= entry.lead.importance:
            entry.lead = hit
        rises = hit.importance > entry.flagged_importance
        new_sub = hit.subcategory not in entry.subcategories
        if entry.count == 1 or rises or new_sub:
            self._flag(ident, entry, hit, hit, batch, final=False)
        elif _power_of_ten(entry.count):
            self._flag(ident, entry, entry.lead, hit, batch, final=False)

    def _flag(
        self, ident: _Ident, entry: _Key, about: Hit, last: Hit, batch: _Batch, *, final: bool
    ) -> None:
        """Flag `ident`, described by the hit `about`, with `last` the latest hit. A key
        flagged again in the same batch keeps one draft, about its most important hit."""
        source, agent, key, _ = ident
        earlier = batch.about.get(ident)
        if earlier is not None and earlier.importance > about.importance:
            about = earlier
        evidence = list(dict.fromkeys([*about.evidence, *last.evidence]))[:EVIDENCE]
        try:
            flag = MonitorFlag(
                layer="signal",
                source=source,
                category=about.category,
                importance=about.importance,
                evidence=evidence,
                justification=about.justification,
                subcategory=about.subcategory,
                phase=last.phase,
                key=key or None,
                count=entry.count,
                first_flag=entry.first_flag,
                first_seq=entry.first_seq,
                last_seq=entry.last_seq,
                final=final,
            )
        except ValidationError as e:
            log.error("signal %s gave a hit that is not a valid flag: %s", source, e)
            batch.drafts.append(
                Draft(
                    MonitorDegraded(
                        part="signal",
                        reason=f"signal {source} gave a hit that is not a valid flag",
                        then="continue",
                        detail=str(e)[:2000],
                    ),
                    agent,
                )
            )
            return
        entry.flagged_count = entry.count
        entry.flagged_importance = max(entry.flagged_importance, about.importance)
        entry.subcategories.add(last.subcategory)
        batch.put(ident, Draft(flag, agent), about)
