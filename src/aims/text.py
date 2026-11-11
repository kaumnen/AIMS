"""Text from outside the process, made fit for the log."""

import re
from typing import Annotated, Any, cast

from pydantic import BeforeValidator

_SURROGATE = re.compile("[\ud800-\udfff]")


def _lone(text: str) -> bool:
    return not text.isascii() and _SURROGATE.search(text) is not None


LONE = "holds a lone surrogate, an escape such as \\ud800 without its pair"


def encodable(text: str) -> bool:
    """Whether UTF-8 can encode `text`: it holds no lone surrogate."""
    return not _lone(text)


def lone_at(value: object) -> tuple[str | int, ...] | None:
    """The keys and indexes that lead to the first string or dict key in `value` that holds a
    lone surrogate, in document order, or None when none does. Takes a string, or a value
    json.loads or a YAML loader parsed. Walks without recursion, so any depth is fine, and in
    time linear in its size: each node links to its parent, and only the path found is built."""
    # a link is the index of the parent's link, -1 for the top, and the key or index under it
    links: list[tuple[int, str | int]] = []
    stack: list[tuple[object, int]] = [(value, -1)]
    while stack:
        node, link = stack.pop()
        if isinstance(node, str):
            if _lone(node):
                path: list[str | int] = []
                while link >= 0:
                    link, key = links[link]
                    path.append(key)
                return tuple(reversed(path))
        elif isinstance(node, dict):
            for key, item in reversed(node.items()):
                links.append((link, key))
                # the key comes off the stack before its value
                stack += [(item, len(links) - 1), (key, len(links) - 1)]
        elif isinstance(node, list):
            for i in reversed(range(len(node))):
                links.append((link, i))
                stack.append((node[i], len(links) - 1))
    return None


def _whole(value: object) -> object:
    lone = _lone(value) if isinstance(value, str) else lone_at(value) is not None
    if lone:
        raise ValueError(LONE)
    return value


# Annotates a field for text or JSON a caller sends that the server stores or passes on: it
# refuses a lone surrogate, which UTF-8 cannot encode, in a string or in a key or a string at
# any depth. Checked before the field's own type and bounds, for one clear error.
NoLoneSurrogate = BeforeValidator(_whole)
Utf8 = Annotated[str, NoLoneSurrogate]


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


def deeper(value: object, limit: int) -> bool:
    """Whether a value json.loads parsed nests more than `limit` levels. Code that walks such
    a value by recursion checks this first."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict | list):
            continue
        if depth > limit:
            return True
        items = node.values() if isinstance(node, dict) else node
        stack.extend((item, depth + 1) for item in items)
    return False
