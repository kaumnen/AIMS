"""Edits to a scenario.yaml that keep its comments, order and layout: the agent prompt overrides
the UI sets in place. The result is text; the caller validates it before writing."""

import copy
import io
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString
from ruamel.yaml.util import load_yaml_guess_indent

from aims.prompts import Prompts
from aims.scenario.load import ScenarioError

TEXTS = frozenset(Prompts.model_fields) - {"tools", "monitor"}
# Mapping indent, sequence indent, dash offset: the layouts tried, in order, until one dumps the
# file back unchanged.
LAYOUTS = ((2, 4, 2), (2, 2, 0), (4, 4, 2), (4, 6, 4), (4, 4, 0), (4, 2, 0))
DEFAULT_AGENT = "agent"


@dataclass(frozen=True, slots=True)
class PromptChange:
    """Set `key` of agent `agent`'s prompts to `value`, or drop it when `value` is None. `key` is
    a text name, or ("tools", tool, text) for a tool's text."""

    agent: str
    key: tuple[str, ...]
    value: str | None


def check_key(key: tuple[str, ...]) -> None:
    if len(key) == 1 and key[0] in TEXTS:
        return
    if len(key) == 3 and key[0] == "tools" and all(key[1:]):
        return
    raise ValueError(f"not an agent text: {'.'.join(key) or '(empty)'}")


def set_prompts(text: str, changes: Iterable[PromptChange], *, source: str = "scenario") -> str:
    """`text` with `changes` applied to its agents' `prompts`."""
    yaml, doc = _load(text, source)
    for change in changes:
        check_key(change.key)
        agent = _agent(doc, change.agent, create=change.value is not None)
        if agent is None:
            continue
        before = _tail(agent)
        token = _take(before)
        if change.value is None:
            _unset(agent, change.key)
        else:
            _set(agent, change.key, change.value)
        _put(_tail(agent) or before, token)
    out = io.StringIO()
    yaml.dump(doc, out)
    return out.getvalue()


def _yaml(layout: tuple[int, int, int]) -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.width = 4096
    yaml.preserve_quotes = True
    mapping, sequence, offset = layout
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _load(text: str, source: str) -> tuple[YAML, CommentedMap]:
    """The document, with the YAML that dumps it back as it was written when one of `LAYOUTS`
    does; else the indent ruamel guesses."""
    try:
        _, indent, offset = load_yaml_guess_indent(text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f":{mark.line + 1}" if mark is not None else ""
        raise ScenarioError([f"{source}{line}: {getattr(e, 'problem', None) or e}"]) from None
    guess = (2, indent or 2, offset or 0)
    for layout in (*LAYOUTS, guess):
        yaml = _yaml(layout)
        doc = yaml.load(text)
        if not isinstance(doc, CommentedMap):
            raise ScenarioError([f"{source}: a scenario is a mapping"])
        out = io.StringIO()
        yaml.dump(doc, out)
        if out.getvalue() == text:
            return yaml, doc
    yaml = _yaml(guess)
    return yaml, yaml.load(text)


def _agent(doc: CommentedMap, agent_id: str, *, create: bool) -> CommentedMap | None:
    agents = doc.get("agents")
    if agents is None:
        if agent_id != DEFAULT_AGENT:
            raise ValueError(f"no agent {agent_id}")
        if not create:
            return None
        agents = doc["agents"] = CommentedSeq([CommentedMap(id=DEFAULT_AGENT)])
    if not isinstance(agents, list):
        raise ValueError("agents is not a list")
    for i, entry in enumerate(agents):
        if isinstance(entry, CommentedMap) and str(entry.get("id", DEFAULT_AGENT)) == agent_id:
            if create and entry.fa.flow_style() and len(entry):
                _block(agents, i, entry)
            return entry
    raise ValueError(f"no agent {agent_id}")


def _block(agents: CommentedSeq, i: int, entry: CommentedMap) -> None:
    """Write a flow `- {id: a}  # note` entry as a block, its comment after its first line."""
    entry.fa.set_block_style()
    token = _take((agents, i), whole=True)
    _put((entry, next(iter(entry))), token)


def _tail(node: Any) -> tuple[Any, Any] | None:
    """Where the comment after `node`'s last line sits: on its deepest last entry. ruamel keeps
    the blank lines and comments that end a block there."""
    last = None
    while isinstance(node, CommentedMap | CommentedSeq) and len(node):
        if last is not None and node.fa.flow_style():
            break
        key = next(reversed(node)) if isinstance(node, CommentedMap) else len(node) - 1
        last = (node, key)
        node = node[key]
    return last


def _slot(node: Any) -> int:
    return 2 if isinstance(node, CommentedMap) else 0


def _take(spot: tuple[Any, Any] | None, *, whole: bool = False) -> Any:
    """Remove the comment that follows `spot` and return it; an end-of-line comment stays, unless
    `whole`, and only the lines after it go."""
    if spot is None:
        return None
    node, key = spot
    entry = node.ca.items.get(key)
    token = entry[_slot(node)] if entry else None
    if token is None:
        return None
    head, newline, rest = token.value.partition("\n")
    if not whole and head.strip().startswith("#"):
        if not rest:
            return None
        moved = copy.copy(token)
        moved.value = newline + rest
        token.value = head + newline
        return moved
    entry[_slot(node)] = None
    return token


def _put(spot: tuple[Any, Any] | None, token: Any) -> None:
    if spot is None or token is None:
        return
    node, key = spot
    entry = node.ca.items.setdefault(key, [None] * (4 if isinstance(node, CommentedMap) else 2))
    kept = entry[_slot(node)]
    if kept is None:
        entry[_slot(node)] = token
    else:
        kept.value += token.value.removeprefix("\n")


def _set(agent: CommentedMap, key: tuple[str, ...], value: str) -> None:
    table = _child(agent, "prompts")
    for part in key[:-1]:
        table = _child(table, part)
    table[key[-1]] = LiteralScalarString(value) if "\n" in value else value


def _child(table: CommentedMap, name: str) -> CommentedMap:
    found = table.get(name)
    if not isinstance(found, CommentedMap):
        found = table[name] = CommentedMap()
    found.fa.set_block_style()
    return found


def _unset(agent: CommentedMap, key: tuple[str, ...]) -> None:
    path: list[tuple[CommentedMap, str]] = []
    table: Any = agent
    for part in ("prompts", *key[:-1]):
        nxt = table.get(part)
        if not isinstance(nxt, CommentedMap):
            return
        path.append((table, part))
        table = nxt
    table.pop(key[-1], None)
    for parent, name in reversed(path):
        if parent[name]:
            break
        del parent[name]
