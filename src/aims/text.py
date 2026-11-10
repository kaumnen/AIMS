"""Text from outside the process, made fit for the log."""

import re
from typing import Any, cast

_SURROGATE = re.compile("[\ud800-\udfff]")


def _lone(text: str) -> bool:
    return not text.isascii() and _SURROGATE.search(text) is not None


def _escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match[0]):04x}"


def _text(text: str) -> str:
    return _SURROGATE.sub(_escape, text) if _lone(text) else text


def scrub[T](value: T) -> T:
    """`value` with each lone surrogate written as the six characters `\\ud800`, the form
    Python's backslashreplace gives, so it stays visible as text. Takes a string, or a value
    json.loads parsed, whose strings and dict keys it changes in place. UTF-8 and canonical
    JSON refuse a lone surrogate, which only a JSON escape like `\\ud800` makes; json.loads
    joins a valid pair into one character, which is left as it is. Walks without recursion,
    so any depth json.loads parses is fine."""
    if isinstance(value, str):
        return cast(T, _text(value))
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(isinstance(k, str) and _lone(k) for k in node):
                pairs = [(_text(k) if isinstance(k, str) else k, v) for k, v in node.items()]
                node.clear()
                node.update(pairs)
            slots: Any = list(node)
        elif isinstance(node, list):
            slots = range(len(node))
        else:
            continue
        for slot in slots:
            item = node[slot]
            if isinstance(item, str):
                node[slot] = _text(item)
            elif isinstance(item, dict | list):
                stack.append(item)
    return value
