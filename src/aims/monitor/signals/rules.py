"""Sigma-style declarative rules over events.

A rule names its event types, a `match` map whose entries must all hold, `filter` maps any of
which excludes an event, a `scope` of phases and agents, and a `key` template. A field path is a
dotted path into the event's payload (`fds.1.ino`; a list without an index matches when any item
does) or a derived field from the monitor's state. A path may carry modifiers: one comparison
(glob, contains, startswith, endswith, re, cidr, gt, gte, lt, lte, exists) and one quantifier
over the rule's values (any, the default, or all). A list field matches when any of its items
does. Strings compare case-sensitively. A glob without a `/` also matches a path's base name;
`*` stays within one path part and `**` crosses them.
"""

import glob
import ipaddress
import re
import types
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, TypeAliasType, Union, get_args, get_origin

from pydantic import BaseModel, JsonValue

from aims.events import BlobRef, Event, KernelPayload
from aims.events.schema import payload_types
from aims.monitor.config import Rule, digest
from aims.monitor.signals.base import Base, Hit, SignalError, SignalSetup
from aims.monitor.state import ATTR_MODE, ATTR_MTIME_SET, FS_CHANGE, FsChangeRecord, State

COMPARE = frozenset(
    {"glob", "contains", "startswith", "endswith", "re", "cidr", "gt", "gte", "lt", "lte", "exists"}
)
QUANTIFY = frozenset({"any", "all"})
NUMERIC = frozenset({"gt", "gte", "lt", "lte"})
TEXT = frozenset({"glob", "contains", "startswith", "endswith", "re"})
SETID = 0o6000
_TEMPLATE = re.compile(r"\{([^{}]+)\}")

Values = list[Any]


def event_models() -> dict[str, type[BaseModel]]:
    """The event types a rule may name, with their payload models: every registered type but
    the monitor's own, and the `fs.change` virtual type."""
    out: dict[str, type[BaseModel]] = {
        name: cls for name, cls in payload_types().items() if not name.startswith("monitor.")
    }
    out[FS_CHANGE] = FsChangeRecord
    return out


def _fields(name: str) -> frozenset[str]:
    return frozenset(t for t, cls in event_models().items() if name in cls.model_fields)


def _kernel() -> frozenset[str]:
    return frozenset(
        t
        for t, cls in event_models().items()
        if isinstance(cls, type) and issubclass(cls, KernelPayload)
    )


@dataclass(frozen=True, slots=True)
class Derived:
    """A field computed from the monitor's state. `types` limits the events it applies to,
    None for every event."""

    doc: str
    get: Callable[[Event, Any, State], Any]
    types: frozenset[str] | None = None


def _exec_attr(attr: str) -> Callable[[Event, Any, State], Any]:
    def get(event: Event, payload: Any, state: State) -> Any:
        info = state.exec_of(event)
        return None if info is None else getattr(info, attr)

    return get


def _tool(event: Event, payload: Any, state: State) -> Any:
    call = state.call_of(event)
    return None if call is None else call.name


def _step(event: Event, payload: Any, state: State) -> Any:
    call = state.call_of(event)
    return None if call is None else call.step


def _authored(event: Event, payload: Any, state: State) -> Any:
    if event.type == "tool.call":
        return True
    info = state.exec_of(event)
    return None if info is None else info.authored


def _installs(event: Event, payload: Any, state: State) -> Any:
    node = state.actor(event)
    if node is not None:
        return node.installs
    info = state.exec_of(event)
    return info is not None and info.installs


def _pid_is_init(event: Event, payload: Any, state: State) -> Any:
    return state.harness(state.actor(event))


def _exe(event: Event, payload: Any, state: State) -> Any:
    node = state.actor(event)
    return None if node is None else node.path


def _program(event: Event, payload: Any, state: State) -> Any:
    node = state.actor(event)
    return None if node is None else node.program


def _target_in_tree(event: Event, payload: Any, state: State) -> Any:
    return state.in_tree(state.actor(event), state.target(event))


def _target_is_init(event: Event, payload: Any, state: State) -> Any:
    return state.target_harness(event)


def _target_agent(event: Event, payload: Any, state: State) -> Any:
    node = state.target(event)
    return None if node is None else node.agent_id


def _written(event: Event, payload: Any, state: State) -> Any:
    path = getattr(payload, "path", None)
    return isinstance(path, str) and state.agent_written(event.agent_id, path)


def _exec_wrote(event: Event, payload: Any, state: State) -> Any:
    path = getattr(payload, "path", None)
    exec_id = getattr(payload, "exec_id", None)
    return isinstance(path, str) and state.exec_wrote(event.agent_id, path, exec_id)


def _target_written(event: Event, payload: Any, state: State) -> Any:
    target = getattr(payload, "target", None)
    return isinstance(target, str) and state.agent_written(event.agent_id, target)


def _proc_in_tree(event: Event, payload: Any, state: State) -> Any:
    path = getattr(payload, "path", None)
    if not isinstance(path, str):
        return None
    named = state.proc_path(event, path)
    if isinstance(named, bool):
        return named or None
    return state.in_tree(state.actor(event), named)


def _setid(event: Event, payload: Any, state: State) -> Any:
    mode = getattr(payload, "mode", None)
    return bool(payload.valid & ATTR_MODE and mode is not None and mode & SETID)


def _mtime_set(event: Event, payload: Any, state: State) -> Any:
    return bool(payload.valid & ATTR_MTIME_SET)


def derived_fields() -> dict[str, Derived]:
    exec_types = _fields("exec_id")
    kernel = _kernel()
    paths = _fields("path") & (kernel | {FS_CHANGE})
    targets = frozenset({"proc.signal", "ptrace.event"})
    return {
        "agent": Derived("the event's agent", lambda e, p, s: e.agent_id),
        "agent_done": Derived(
            "the event's agent had finished when the event came",
            lambda e, p, s: None if e.agent_id is None else e.agent_id in s.finished,
        ),
        "phase": Derived("the phase of the event's exec", lambda e, p, s: s.phase_of(e)),
        "tool": Derived("the tool of the event's call", _tool),
        "step": Derived("the agent step of the event's call", _step),
        "authored": Derived("the exec runs a command the agent wrote itself", _authored),
        "exec_user": Derived("the user the exec ran as", _exec_attr("user"), exec_types),
        "installs": Derived(
            "the process, or for a snapshot change the exec, installs packages",
            _installs,
            exec_types,
        ),
        "pid_is_init": Derived("the process is the sandbox's own", _pid_is_init, kernel),
        "exe": Derived("the program file the process runs", _exe, kernel),
        "program": Derived("the program name the process runs", _program, kernel),
        "target_in_tree": Derived("the target is in the caller's tree", _target_in_tree, targets),
        "target_is_init": Derived("the target is the sandbox's own", _target_is_init, targets),
        "target_agent": Derived("the agent the target belongs to", _target_agent, targets),
        "agent_written": Derived("the agent wrote the path last", _written, paths),
        "exec_wrote": Derived("the event's exec wrote the path before", _exec_wrote, paths),
        "target_agent_written": Derived(
            "the agent wrote the target last", _target_written, frozenset({"file.access"})
        ),
        "proc_in_tree": Derived(
            "a /proc/<pid> path names the caller's tree; null for other paths",
            _proc_in_tree,
            paths,
        ),
        "setid": Derived("sets a setuid or setgid bit", _setid, frozenset({"file.setattr"})),
        "mtime_set": Derived(
            "sets an explicit modification time", _mtime_set, frozenset({"file.setattr"})
        ),
    }


def _strip(ann: Any) -> Any:
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    return ann


def _leaf(ann: Any) -> bool:
    return isinstance(ann, type) and issubclass(ann, BlobRef)


def _path_ok(ann: Any, segs: Sequence[str]) -> bool:
    """Whether `segs` walks into the type `ann`."""
    if not segs:
        return True
    ann = _strip(ann)
    if ann is Any or ann is object or isinstance(ann, TypeAliasType) or ann is JsonValue:
        return True
    origin = get_origin(ann)
    args = [a for a in get_args(ann) if a is not types.NoneType]
    if origin in (Union, types.UnionType):
        if any(_leaf(a) for a in args):
            return False
        return any(_path_ok(a, segs) for a in args)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        f = ann.model_fields.get(segs[0])
        return f is not None and _path_ok(f.annotation, segs[1:])
    if origin in (list, set, frozenset, Sequence):
        return _path_ok(args[0], segs[1:] if segs[0].isdigit() else segs)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return _path_ok(args[0], segs[1:] if segs[0].isdigit() else segs)
        if segs[0].isdigit():
            i = int(segs[0])
            return i < len(args) and _path_ok(args[i], segs[1:])
        return any(_path_ok(a, segs) for a in args)
    if origin in (dict, Mapping):
        return _path_ok(args[-1], segs[1:])
    return False


def _get(obj: Any, segs: Sequence[str]) -> list[Any]:
    if not segs:
        return [obj]
    if obj is None or isinstance(obj, BlobRef):
        return []
    seg = segs[0]
    if isinstance(obj, BaseModel):
        if seg in type(obj).model_fields:
            return _get(getattr(obj, seg), segs[1:])
        return []
    if isinstance(obj, Mapping):
        return _get(obj[seg], segs[1:]) if seg in obj else []
    if isinstance(obj, (list, tuple)):
        if seg.isdigit():
            i = int(seg)
            return _get(obj[i], segs[1:]) if i < len(obj) else []
        return [x for item in obj for x in _get(item, segs)]
    return []


def compile_glob(pattern: str) -> Callable[[str], bool]:
    """A matcher for `pattern`: `*` within a path part, `**` across parts; a pattern without a
    `/` also matches the base name."""
    full = re.compile(glob.translate(pattern, recursive=True, include_hidden=True, seps="/"))
    if "/" in pattern:
        return lambda s: full.match(s) is not None
    return lambda s: full.match(s) is not None or full.match(s.rsplit("/", 1)[-1]) is not None


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _ip(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(value, str):
        return None
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _matcher(mod: str | None, value: Any) -> Callable[[Any], bool]:
    """A test of one candidate against one rule value. Raises ValueError for a bad value."""
    if mod is None:
        if not _scalar(value):
            raise ValueError(f"{value!r} is not a scalar")
        return lambda c: _same(c, value)
    if mod in TEXT and not isinstance(value, str):
        raise ValueError(f"{mod} takes text, not {value!r}")
    if mod in NUMERIC and not _number(value):
        raise ValueError(f"{mod} takes a number, not {value!r}")
    match mod:
        case "contains":
            return lambda c: isinstance(c, str) and value in c
        case "startswith":
            return lambda c: isinstance(c, str) and c.startswith(value)
        case "endswith":
            return lambda c: isinstance(c, str) and c.endswith(value)
        case "glob":
            test = compile_glob(value)
            return lambda c: isinstance(c, str) and test(c)
        case "re":
            try:
                pattern = re.compile(value)
            except re.error as e:
                raise ValueError(f"bad regex {value!r}: {e}") from None
            return lambda c: isinstance(c, str) and pattern.search(c) is not None
        case "cidr":
            if not isinstance(value, str):
                raise ValueError(f"cidr takes a network, not {value!r}")
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                raise ValueError(f"{value!r} is not a network") from None

            def in_net(c: Any) -> bool:
                ip = _ip(c)
                return ip is not None and ip.version == net.version and ip in net

            return in_net
        case "gt":
            return lambda c: _number(c) and c > value
        case "gte":
            return lambda c: _number(c) and c >= value
        case "lt":
            return lambda c: _number(c) and c < value
        case "lte":
            return lambda c: _number(c) and c <= value
        case _:
            raise ValueError(f"unknown modifier {mod}")


@dataclass(frozen=True, slots=True)
class Condition:
    """One entry of a match or filter map, compiled."""

    path: tuple[str, ...]
    derived: Derived | None
    mod: str | None
    every: bool
    tests: tuple[Callable[[Any], bool], ...]
    exists: bool | None = None

    async def holds(self, event: Event, payload: Any, state: State) -> bool:
        if self.derived is not None:
            raw = [self.derived.get(event, payload, state)]
        else:
            raw = _get(payload, self.path)
        values: Values = []
        for v in raw:
            if isinstance(v, BlobRef):
                values.append(await state.text(v))
            elif isinstance(v, (list, tuple)):
                values.extend(v)
            else:
                values.append(v)
        if self.exists is not None:
            return any(v is not None for v in values) == self.exists
        if self.every:
            return all(any(t(v) for v in values) for t in self.tests)
        return any(t(v) for t in self.tests for v in values)


@dataclass(frozen=True, slots=True)
class Compiled:
    rule: Rule
    types: frozenset[str]
    match: tuple[Condition, ...]
    filters: tuple[tuple[Condition, ...], ...]
    key: tuple[tuple[str, Condition | None], ...]
    distinct: Condition | None = None
    by_key: bool = False

    async def matches(self, event: Event, payload: Any, state: State) -> bool:
        scope = self.rule.scope
        if scope.agent is not None and event.agent_id not in scope.agent:
            return False
        if scope.phase is not None and state.phase_of(event) not in scope.phase:
            return False
        for c in self.match:
            if not await c.holds(event, payload, state):
                return False
        for group in self.filters:
            if all([await c.holds(event, payload, state) for c in group]):
                return False
        return True

    async def value(self, event: Event, payload: Any, state: State) -> Any:
        """The field a rate rule counts as distinct, or its key, or its payload."""
        if self.distinct is not None:
            if self.distinct.derived is not None:
                return self.distinct.derived.get(event, payload, state)
            found = _get(payload, self.distinct.path)
            return found[0] if found else None
        if self.by_key:
            rendered = await self.render_key(event, payload, state)
            return rendered if rendered else payload.model_dump_json()
        return None

    async def render_key(self, event: Event, payload: Any, state: State) -> str:
        parts: list[str] = []
        for text, field in self.key:
            if field is None:
                parts.append(text)
                continue
            if field.derived is not None:
                value = field.derived.get(event, payload, state)
            else:
                found = _get(payload, field.path)
                value = found[0] if found else None
            if isinstance(value, BlobRef):
                value = await state.text(value, 256)
            if isinstance(value, (list, tuple)):
                value = " ".join(str(v) for v in value)
            parts.append("" if value is None else str(value))
        return "".join(parts)


def _condition(
    spec: str, value: JsonValue, rule_types: frozenset[str], derived: Mapping[str, Derived]
) -> Condition:
    """One match entry. Raises ValueError."""
    path, *mods = spec.split("|")
    if not path:
        raise ValueError(f"{spec!r} names no field")
    unknown = [m for m in mods if m not in COMPARE | QUANTIFY]
    if unknown:
        raise ValueError(f"{spec!r}: unknown modifier {', '.join(unknown)}")
    compare = [m for m in mods if m in COMPARE]
    quant = [m for m in mods if m in QUANTIFY]
    if len(compare) > 1 or len(quant) > 1:
        raise ValueError(f"{spec!r}: at most one comparison and one of any or all")
    mod = compare[0] if compare else None
    segs = tuple(path.split("."))
    field = _field(path, segs, rule_types, derived)
    if mod == "exists":
        if not isinstance(value, bool) or quant:
            raise ValueError(f"{spec!r}: exists takes true or false")
        return Condition(segs, field, mod, False, (), exists=value)
    items = value if isinstance(value, list) else [value]
    if not items:
        raise ValueError(f"{spec!r}: an empty list matches nothing")
    tests = tuple(_matcher(mod, v) for v in items)
    return Condition(segs, field, mod, quant == ["all"], tests)


def _field(
    path: str, segs: Sequence[str], rule_types: frozenset[str], derived: Mapping[str, Derived]
) -> Derived | None:
    """The derived field `path` names, or None for a payload path. Raises ValueError."""
    models = event_models()
    d = derived.get(segs[0])
    if d is not None:
        if len(segs) > 1:
            raise ValueError(f"{path!r}: derived field {segs[0]} has no parts")
        outside = sorted(rule_types - d.types) if d.types is not None else []
        if outside:
            raise ValueError(f"{path!r}: derived field {segs[0]} does not apply to {outside[0]}")
        return d
    for t in sorted(rule_types):
        if not _path_ok(models[t], segs):
            raise ValueError(f"{t} has no field {path!r}")
    return None


def compile_rule(rule: Rule) -> Compiled:
    """`rule` ready to match. Raises SignalError naming every problem."""
    problems = rule_problems(rule)
    if problems:
        raise SignalError("; ".join(problems))
    return _compile(rule)


def _compile(rule: Rule) -> Compiled:
    derived = derived_fields()
    rule_types = frozenset(rule.event)
    match = tuple(_condition(k, v, rule_types, derived) for k, v in rule.match.items())
    filters = tuple(
        tuple(_condition(k, v, rule_types, derived) for k, v in group.items())
        for group in rule.filter
    )
    key: list[tuple[str, Condition | None]] = []
    if rule.key:
        at = 0
        for m in _TEMPLATE.finditer(rule.key):
            if m.start() > at:
                key.append((rule.key[at : m.start()], None))
            name = m.group(1)
            segs = tuple(name.split("."))
            field = _field(name, segs, rule_types, derived)
            key.append(("", Condition(segs, field, None, False, ())))
            at = m.end()
        if at < len(rule.key):
            key.append((rule.key[at:], None))
    distinct = None
    by_key = rule.distinct is True
    if isinstance(rule.distinct, str):
        segs = tuple(rule.distinct.split("."))
        field = _field(rule.distinct, segs, rule_types, derived)
        distinct = Condition(segs, field, None, False, ())
    return Compiled(rule, rule_types, match, filters, tuple(key), distinct, by_key)


def rule_problems(rule: Rule) -> list[str]:
    """What is wrong with `rule`, as messages naming it."""
    models = event_models()
    where = f"rule {rule.id}"
    unknown = [t for t in rule.event if t not in models]
    if unknown:
        return [f"{where}: unknown event type {', '.join(unknown)}"]
    derived = derived_fields()
    rule_types = frozenset(rule.event)
    problems: list[str] = []
    entries = [("match", k, v) for k, v in rule.match.items()]
    entries += [(f"filter {i + 1}", k, v) for i, g in enumerate(rule.filter) for k, v in g.items()]
    for part, k, v in entries:
        try:
            _condition(k, v, rule_types, derived)
        except ValueError as e:
            problems.append(f"{where}: {part}: {e}")
    if rule.key:
        if "{" in _TEMPLATE.sub("", rule.key) or "}" in _TEMPLATE.sub("", rule.key):
            problems.append(f"{where}: key: unbalanced braces in {rule.key!r}")
        for m in _TEMPLATE.finditer(rule.key):
            name = m.group(1)
            try:
                _field(name, name.split("."), rule_types, derived)
            except ValueError as e:
                problems.append(f"{where}: key: {e}")
    if isinstance(rule.distinct, str):
        try:
            _field(rule.distinct, rule.distinct.split("."), rule_types, derived)
        except ValueError as e:
            problems.append(f"{where}: distinct: {e}")
    return problems


def validate_rules(rules: Sequence[Rule]) -> list[str]:
    """Every problem with `rules`: unknown event types, field paths missing from a payload,
    unknown modifiers, values a modifier does not take and bad regexes."""
    problems: list[str] = []
    for rule in rules:
        problems += rule_problems(rule)
    return problems


def rules_digest(rules: Sequence[Rule]) -> str:
    return digest([r.model_dump(mode="json") for r in rules])


class RuleSignal(Base):
    """A signal made of rules. An event that matches several of them gives one hit, from the
    rule of the highest importance. Each hit's justification is the rule's title and key."""

    rules: ClassVar[tuple[Rule, ...]] = ()

    def __init__(self, setup: SignalSetup, rules: Sequence[Rule] | None = None) -> None:
        super().__init__(setup)
        source = self.build(setup) if rules is None else rules
        self.compiled = [compile_rule(r) for r in source if r.enabled]
        self.by_type: dict[str, list[Compiled]] = {}
        for c in self.compiled:
            for t in c.types:
                self.by_type.setdefault(t, []).append(c)
        self.event_types = frozenset(self.by_type)
        self.windows: dict[tuple[str, str | None], deque[tuple[float, int, Any]]] = {}

    @classmethod
    def build(cls, setup: SignalSetup) -> Sequence[Rule]:
        """The rules of this signal for a run; the class's own by default."""
        return cls.rules

    async def _window(
        self, c: Compiled, event: Event, payload: Any, state: State
    ) -> list[int] | None:
        """The evidence when this match completes the rule's count, else None."""
        rule = c.rule
        if rule.count <= 1 and rule.window_s is None and not rule.distinct:
            return [event.seq]
        now = event.ts.timestamp()
        key = (rule.id, event.agent_id)
        seen = self.windows.setdefault(key, deque())
        if rule.window_s is not None:
            cutoff = now - rule.window_s
            while seen and seen[0][0] < cutoff:
                seen.popleft()
        elif len(seen) >= rule.count:
            seen.popleft()
        seen.append((now, event.seq, await c.value(event, payload, state)))
        if rule.distinct:
            distinct = {value for _, _, value in seen if value is not None}
            reached = len(distinct) >= rule.count
        else:
            reached = len(seen) >= rule.count
        if not reached:
            return None
        evidence = [seq for _, seq, _ in seen]
        seen.clear()
        return evidence

    async def feed(self, event: Event, state: State) -> list[Hit]:
        candidates = self.by_type.get(event.type)
        if not candidates:
            return []
        payload = state.payload(event)
        if payload is None:
            return []
        best: Compiled | None = None
        evidence: list[int] = []
        for c in candidates:
            if not await c.matches(event, payload, state):
                continue
            found = await self._window(c, event, payload, state)
            if found is None:
                continue
            if best is None or c.rule.importance > best.rule.importance:
                best, evidence = c, found
        if best is None:
            return []
        rule = best.rule
        key = await best.render_key(event, payload, state)
        text = f"{rule.title}: {key}" if key else rule.title
        return [
            Hit(
                category=rule.category,
                importance=rule.importance,
                evidence=evidence,
                justification=text,
                key=key,
                subcategory=rule.subcategory,
                phase=state.phase_of(event),
            )
        ]


def rule_signal(rule: Rule) -> type[RuleSignal]:
    """A signal class for one scenario rule, named by its id."""
    return type(
        f"Rule_{rule.id}",
        (RuleSignal,),
        {"name": rule.id, "version": rules_digest([rule])[:12], "rules": (rule,)},
    )
