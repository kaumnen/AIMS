"""AIMS_KEYS: which env vars AIMS reads for a model call and where it may send each key.

Entries are separated by commas. `NAME=URL` lets the key in env var NAME go to a profile whose
base_url is URL or a path under it. `NAME=env:VAR` lets it go to a profile that reads its URL
from env var VAR. `env:VAR` lets a profile with no key read its URL from VAR. A name may repeat
for more URLs. AIMS reads no other env var for a model's URL or key, so a profile cannot name
one to learn whether it is set.

A runner tells the control plane the entries whose variables it has set, never their values,
and the control plane offers it only jobs whose profiles those entries allow.
"""

import re
from collections.abc import Iterable, Mapping
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
    dot, the port and the segments of the path. `text` is the URL as written."""

    scheme: str
    host: str
    port: int
    path: tuple[str, ...]
    text: str = field(default="", compare=False)

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
    return Place(u.scheme, host, u.port or _PORTS[u.scheme], tuple(segments), url)


@dataclass(frozen=True, slots=True)
class Keys:
    """A read AIMS_KEYS. `places` holds the URLs each key may go to, `via` the URL vars whose
    profiles it may go to, `urls` every URL var AIMS may read."""

    places: Mapping[str, tuple[Place, ...]] = field(default_factory=dict)
    via: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    urls: frozenset[str] = frozenset()

    def lets(self, name: str, *, url: str | None = None, var: str | None = None) -> bool:
        """Whether the key in `name` may go to a profile that reads its URL from `var`, or else
        whose base_url is `url`."""
        if var is not None:
            return var in self.via.get(name, ())
        to = place(url or "")
        return to is not None and any(p.holds(to) for p in self.places.get(name, ()))

    def covers(self, other: Keys) -> bool:
        """Whether this list allows each entry of `other`."""
        return (
            other.urls <= self.urls
            and all(self.lets(k, var=v) for k, vs in other.via.items() for v in vs)
            and all(
                any(q.holds(p) for q in self.places.get(k, ()))
                for k, ps in other.places.items()
                for p in ps
            )
        )

    def allows(self, entry: str) -> bool:
        """Whether this list allows `entry`, the one entry a profile needs."""
        return listable(entry) and self.covers(parse(entry))

    def report(self, env: Mapping[str, str]) -> list[str]:
        """The entries whose variables `env` has set, as a runner reports them: names and
        URLs, never values."""
        out: set[str] = {f"{VIA}{v}" for v in self.urls if env.get(v)}
        for name, ps in self.places.items():
            if env.get(name):
                out.update(f"{name}={p.text}" for p in ps)
        for name, vs in self.via.items():
            if env.get(name):
                out.update(f"{name}={VIA}{v}" for v in vs if env.get(v))
        return sorted(out)


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


def listable(entry: str) -> bool:
    """Whether AIMS_KEYS can hold `entry`, the one entry a profile needs. It cannot hold one
    with a comma, an AIMS_ name or a URL AIMS sends no key to."""
    if "," in entry:
        return False
    try:
        parse(entry)
    except KeysError:
        return False
    return True


def reported(entries: Iterable[str]) -> Keys:
    """The entries a runner reported, read as one list. Raises KeysError for one AIMS cannot
    read."""
    entries = list(entries)
    for e in entries:
        if "," in e:
            raise KeysError("a reported entry holds a comma")
    return parse(",".join(entries), "the reported entries")
