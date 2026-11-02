"""Which content codings a request's Accept-Encoding allows (RFC 9110 12.5.3)."""

from collections.abc import Iterable


def allowed(header: str | None, offered: Iterable[str]) -> list[str]:
    """The offered codings the header allows, in offered order: each named, or covered by *,
    with a weight above 0. A weight that does not parse counts as 0."""
    weights: dict[str, float] = {}
    for part in (header or "").split(","):
        name, *params = (p.strip() for p in part.split(";"))
        if not name:
            continue
        weight = 1.0
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        weights[name.lower()] = weight
    rest = weights.get("*", 0.0)
    return [c for c in offered if weights.get(c, rest) > 0]
