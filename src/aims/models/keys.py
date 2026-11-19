"""AIMS_KEYS: which env vars AIMS reads for a model call and where it may send each key.

Entries are separated by commas. `NAME=URL` lets the key in env var NAME go to URL and the
paths under it. `NAME=env:VAR` lets it go to the URL that env var VAR holds. `env:VAR` lets a
profile with no key read its URL from VAR. A name may repeat for more URLs. AIMS reads no
other env var for a model's URL or key, so a profile cannot name one to learn whether it is
set.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache

import httpx2

KEYS = "AIMS_KEYS"
VIA = "env:"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PORTS = {"http": 80, "https": 443}


class KeysError(ValueError):
    """An entry of AIMS_KEYS or of --key that AIMS cannot read. The text names the entry by
    its place and tells nothing after its name: a key pasted there by mistake would show."""


def own(name: str) -> bool:
    """Whether `name` is one of AIMS's own variables, which it never sends or reads as a URL."""
    return name.upper().startswith("AIMS_")


@dataclass(frozen=True, slots=True)
class Place:
    """Where a URL leads, as AIMS compares it: the scheme, the host in ASCII without a trailing
    dot, the port and the segments of the path."""

    scheme: str
    host: str
    port: int
    path: tuple[str, ...]

    def holds(self, other: Place) -> bool:
        """Whether `other` is this place or a path under it."""
        return (self.scheme, self.host, self.port) == (
            other.scheme,
            other.host,
            other.port,
        ) and other.path[: len(self.path)] == self.path


def place(url: str) -> Place | None:
    """Where `url` leads, or None when AIMS sends no key there: it is not an http or https
    URL, or it has a user name or password, a query, a fragment or a % in its path. A server
    may decode an escaped dot or slash in the path after the match."""
    if not url.startswith(("https://", "http://")) or "?" in url or "#" in url:
        return None
    try:
        u = httpx2.URL(url)
        host = u.raw_host.decode("ascii").lower().removesuffix(".")
    # UnicodeEncodeError, a ValueError, comes of a lone surrogate in the URL
    except httpx2.InvalidURL, ValueError:
        return None
    if not host or u.userinfo or b"%" in u.raw_path:
        return None
    segments = u.path.split("/")[1:]
    if segments and not segments[-1]:
        segments.pop()
    return Place(u.scheme, host, u.port or _PORTS[u.scheme], tuple(segments))


@dataclass(frozen=True, slots=True)
class Keys:
    """A read AIMS_KEYS. `places` holds the URLs each key may go to, `via` the URL vars whose
    URL it may go to, `urls` every URL var AIMS may read."""

    places: Mapping[str, tuple[Place, ...]] = field(default_factory=dict)
    via: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    urls: frozenset[str] = frozenset()

    def lets(self, name: str, url: str, env: Mapping[str, str]) -> bool:
        """Whether the key in `name` may go to `url`. The URL vars it may go to are read from
        `env`."""
        to = place(url)
        if to is None:
            return False
        if any(p.holds(to) for p in self.places.get(name, ())):
            return True
        for var in self.via.get(name, ()):
            at = place(env.get(var, ""))
            if at is not None and at.holds(to):
                return True
        return False


def _name(text: str, at: str) -> str:
    if not _NAME.fullmatch(text):
        raise KeysError(
            f"{at}: a variable name takes letters, digits and _ and does not start with a digit"
        )
    if own(text):
        raise KeysError(f"{at}: {text} starts with AIMS_, which AIMS keeps for its own variables")
    return text


@lru_cache(maxsize=32)
def parse(text: str, what: str = KEYS) -> Keys:
    """The entries of `text`, the value of AIMS_KEYS or of a --key flag as `what` says. Raises
    KeysError for an entry it cannot read."""
    places: dict[str, list[Place]] = {}
    via: dict[str, list[str]] = {}
    urls: set[str] = set()
    parts = text.split(",")
    for i, part in enumerate(parts, 1):
        entry = part.strip()
        if not entry:
            continue
        at = f"{what} entry {i}" if len(parts) > 1 else what
        if entry.startswith(VIA):
            urls.add(_name(entry.removeprefix(VIA), at))
            continue
        name, eq, target = entry.partition("=")
        if not eq:
            raise KeysError(f"{at} is not NAME=URL, NAME=env:VAR or env:VAR")
        name = _name(name.strip(), at)
        target = target.strip()
        if target.startswith(VIA):
            var = _name(target.removeprefix(VIA), at)
            via.setdefault(name, []).append(var)
            urls.add(var)
            continue
        to = place(target)
        if to is None:
            raise KeysError(
                f"{at}: {name}= takes env:VAR or an http or https URL with no user name,"
                " password, query, fragment or % in it"
            )
        places.setdefault(name, []).append(to)
    both = sorted((places.keys() | via.keys()) & urls)
    if both:
        them = "them" if len(both) > 1 else "it"
        raise KeysError(
            f"{what} lists {', '.join(both)} as a key and as a URL var; a variable holds"
            f" one or the other, so list {them} once"
        )
    return Keys(
        {k: tuple(v) for k, v in places.items()},
        {k: tuple(v) for k, v in via.items()},
        frozenset(urls),
    )


def keys(env: Mapping[str, str]) -> Keys:
    """The AIMS_KEYS of `env`. Raises KeysError when it cannot be read."""
    return parse(env.get(KEYS, ""))
