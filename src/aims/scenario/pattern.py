"""Patterns for canary values: a bounded subset of regular expressions that can be filled with
random text.

Allowed: literal characters, `\\` escapes of punctuation, `\\d`, `\\w`, `.` (any printable
ASCII character but space), character classes such as `[A-Z0-9_]` and `[^aeiou]`, groups
`(...)` and `(?:...)` with `|` alternatives, and the bounded repeats `?`, `{n}` and `{n,m}`.
Open repeats (`*`, `+`, `{n,}`) and anchors inside a pattern are refused, so every value has a
known maximum length.
"""

import random
import string
from dataclasses import dataclass

MAX_REPEAT = 1024
MAX_LENGTH = 4096
PRINTABLE = "".join(chr(c) for c in range(0x21, 0x7F))
DIGITS = string.digits
WORD = string.ascii_letters + string.digits + "_"
_ESCAPES = frozenset("\\^$.*+?()[]{}|/-")


class PatternError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Chars:
    chars: str


@dataclass(frozen=True, slots=True)
class _Repeat:
    node: _Node
    low: int
    high: int


@dataclass(frozen=True, slots=True)
class _Seq:
    items: tuple[_Node, ...]


@dataclass(frozen=True, slots=True)
class _Alt:
    options: tuple[_Node, ...]


type _Node = _Chars | _Repeat | _Seq | _Alt


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.at = 0

    def parse(self) -> _Node:
        node = self.alternation()
        if self.at != len(self.pattern):
            raise PatternError(f"unexpected {self.pattern[self.at]!r} at {self.at}")
        if _max_length(node) > MAX_LENGTH:
            raise PatternError(f"a value can be longer than {MAX_LENGTH} characters")
        return node

    def alternation(self) -> _Node:
        options = [self.sequence()]
        while self._take("|"):
            options.append(self.sequence())
        return options[0] if len(options) == 1 else _Alt(tuple(options))

    def sequence(self) -> _Node:
        items: list[_Node] = []
        while self.at < len(self.pattern) and self.pattern[self.at] not in ")|":
            items.append(self.repeat())
        return items[0] if len(items) == 1 else _Seq(tuple(items))

    def repeat(self) -> _Node:
        node = self.atom()
        while self.at < len(self.pattern):
            ch = self.pattern[self.at]
            if ch == "?":
                self.at += 1
                node = _Repeat(node, 0, 1)
            elif ch == "{":
                low, high = self.bounds()
                node = _Repeat(node, low, high)
            elif ch in "*+":
                raise PatternError(f"open repeat {ch!r} at {self.at} is not bounded")
            else:
                break
        return node

    def bounds(self) -> tuple[int, int]:
        start = self.at
        self.at += 1
        low = self.integer()
        if low is None:
            raise PatternError(f"expected a repeat count at {start}")
        if self._take("}"):
            if low > MAX_REPEAT:
                raise PatternError(f"repeat count {low} is above {MAX_REPEAT}")
            return low, low
        if not self._take(","):
            raise PatternError(f"bad repeat at {start}")
        high = self.integer()
        if not self._take("}"):
            raise PatternError(f"unbounded repeat at {start}; use {{n}} or {{n,m}}")
        if high is None:
            raise PatternError(f"unbounded repeat at {start}; use {{n}} or {{n,m}}")
        if high < low:
            raise PatternError(f"repeat {{{low},{high}}} ends before it starts")
        if high > MAX_REPEAT:
            raise PatternError(f"repeat count {high} is above {MAX_REPEAT}")
        return low, high

    def integer(self) -> int | None:
        start = self.at
        while self.at < len(self.pattern) and self.pattern[self.at].isdigit():
            self.at += 1
        return int(self.pattern[start : self.at]) if self.at > start else None

    def atom(self) -> _Node:
        if self.at >= len(self.pattern):
            raise PatternError("pattern ends where a value is expected")
        ch = self.pattern[self.at]
        if ch in "^$":
            raise PatternError(f"anchor {ch!r} at {self.at} is not allowed")
        if ch in "*+":
            raise PatternError(f"open repeat {ch!r} at {self.at} is not bounded")
        if ch == "(":
            return self.group()
        if ch == "[":
            return self.character_class()
        if ch == ".":
            self.at += 1
            return _Chars(PRINTABLE)
        if ch == "\\":
            return self.escape()
        if ch in ")}|":
            raise PatternError(f"unexpected {ch!r} at {self.at}")
        self.at += 1
        return _Chars(ch)

    def group(self) -> _Node:
        self.at += 1
        if self.pattern.startswith("?:", self.at):
            self.at += 2
        node = self.alternation()
        if not self._take(")"):
            raise PatternError(f"unclosed group at {self.at}")
        return node

    def escape(self) -> _Node:
        start = self.at
        self.at += 1
        if self.at >= len(self.pattern):
            raise PatternError(f"dangling escape at {start}")
        ch = self.pattern[self.at]
        self.at += 1
        if ch == "d":
            return _Chars(DIGITS)
        if ch == "w":
            return _Chars(WORD)
        if ch in _ESCAPES or (ch in PRINTABLE and not ch.isalnum()):
            return _Chars(ch)
        raise PatternError(f"escape \\{ch} at {start} is not allowed")

    def character_class(self) -> _Node:
        start = self.at
        self.at += 1
        negate = self._take("^")
        chars: set[str] = set()
        first = True
        while self.at < len(self.pattern):
            ch = self.pattern[self.at]
            if ch == "]" and not first:
                self.at += 1
                if not chars:
                    raise PatternError(f"empty character class at {start}")
                if negate:
                    chars = set(PRINTABLE) - chars
                return _Chars("".join(sorted(chars)))
            first = False
            if ch == "\\":
                self.at += 1
                if self.at >= len(self.pattern):
                    raise PatternError(f"dangling escape at {self.at - 1}")
                esc = self.pattern[self.at]
                self.at += 1
                if esc == "d":
                    chars.update(DIGITS)
                elif esc == "w":
                    chars.update(WORD)
                elif esc in _ESCAPES or (esc in PRINTABLE and not esc.isalnum()):
                    chars.add(esc)
                else:
                    raise PatternError(f"escape \\{esc} in a class is not allowed")
                continue
            self.at += 1
            if self.at + 1 < len(self.pattern) and self.pattern[self.at] == "-":
                end = self.pattern[self.at + 1]
                if end == "]":
                    chars.add(ch)
                    chars.add("-")
                    continue
                if end == "\\":
                    raise PatternError(f"escape after a range in a class at {self.at}")
                if ord(end) < ord(ch):
                    raise PatternError(f"class range {ch}-{end} is backwards")
                chars.update(chr(c) for c in range(ord(ch), ord(end) + 1))
                self.at += 2
            else:
                chars.add(ch)
        raise PatternError(f"unclosed character class at {start}")

    def _take(self, text: str) -> bool:
        if self.pattern.startswith(text, self.at):
            self.at += len(text)
            return True
        return False


def _max_length(node: _Node) -> int:
    match node:
        case _Chars(chars):
            return 1 if chars else 0
        case _Repeat(child, _low, high):
            return high * _max_length(child)
        case _Seq(items):
            return sum(_max_length(item) for item in items)
        case _Alt(options):
            return max((_max_length(option) for option in options), default=0)
    raise AssertionError(f"unknown node {node!r}")


def parse(pattern: str) -> _Node:
    """The syntax tree of `pattern`. Raises PatternError when it is outside the subset."""
    if not isinstance(pattern, str):
        raise PatternError(f"{pattern!r} is not a pattern")
    return _Parser(pattern).parse()


def check(pattern: str) -> None:
    """Raise PatternError when `pattern` is outside the subset this module can fill."""
    parse(pattern)


def fill(pattern: str | _Node, rng: random.Random) -> str:
    """`pattern` filled with random text drawn from `rng`."""
    return _fill(parse(pattern) if isinstance(pattern, str) else pattern, rng)


def random_chars(pattern: str) -> int:
    """The fewest characters of any value of `pattern` drawn from a choice of more than one."""
    return _random(parse(pattern))


def _random(node: _Node) -> int:
    match node:
        case _Chars(chars):
            return 1 if len(chars) > 1 else 0
        case _Repeat(child, low, _high):
            return low * _random(child)
        case _Seq(items):
            return sum(_random(item) for item in items)
        case _Alt(options):
            return min((_random(option) for option in options), default=0)
    raise AssertionError(f"unknown node {node!r}")


def _fill(node: _Node, rng: random.Random) -> str:
    match node:
        case _Chars(chars):
            return rng.choice(chars) if chars else ""
        case _Repeat(child, low, high):
            return "".join(_fill(child, rng) for _ in range(rng.randint(low, high)))
        case _Seq(items):
            return "".join(_fill(item, rng) for item in items)
        case _Alt(options):
            return _fill(rng.choice(options), rng)
    raise AssertionError(f"unknown node {node!r}")
