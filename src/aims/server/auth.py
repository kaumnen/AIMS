"""Who may call the control plane. Tokens are named and carry a role: viewers read, operators
also act, runners only use `/runner/v1`. A browser trades a token for a session cookie once;
cookie requests that change state must come from the same origin."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aims.server.db import Role, ServerDB, Token
from aims.server.problems import ProblemException

COOKIE = "aims_session"
SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
RANK: dict[Role, int] = {"viewer": 1, "operator": 2, "runner": 0}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self';"
    " worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none';"
    " form-action 'self'"
)
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    role: Role
    via: Literal["bearer", "cookie", "open"]


class Auth:
    def __init__(
        self, db: ServerDB, *, enabled: bool = True, tls: bool = False, scope: str = ""
    ) -> None:
        """`scope` tells this control plane's cookie from another's on the same host: cookies
        ignore the port, so two of them would sign each other out."""
        self.db = db
        self.enabled = enabled
        self.tls = tls
        self.scope = scope
        self.ephemeral: dict[str, Token] = {}
        # set and replaced when a session ends or a token is revoked, so open streams check
        # that their caller may still read
        self.revoked = asyncio.Event()

    @property
    def cookie(self) -> str:
        name = f"{COOKIE}_{self.scope}" if self.scope else COOKIE
        return f"__Host-{name}" if self.tls else name

    async def token(self, secret: str) -> Token | None:
        token = self.ephemeral.get(secret)
        return token if token is not None else await self.db.token_for(secret)

    def revoke(self) -> None:
        event, self.revoked = self.revoked, asyncio.Event()
        event.set()

    async def still(self, request: Request, principal: Principal) -> bool:
        """Whether the credentials of `request` still admit `principal`."""
        now = await self.principal(request)
        return now is not None and (now.name, now.role) == (principal.name, principal.role)

    async def principal(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization", "")
        scheme, _, secret = header.partition(" ")
        if scheme.lower() == "bearer" and secret:
            token = await self.token(secret.strip())
            return None if token is None else Principal(token.name, token.role, "bearer")
        session = request.cookies.get(self.cookie)
        if session:
            token = await self.db.session(session)
            if token is not None:
                return Principal(token.name, token.role, "cookie")
        if not self.enabled:
            return Principal("anonymous", "operator", "open")
        return None


def same_origin(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    return (
        origin is not None and host is not None and origin in (f"http://{host}", f"https://{host}")
    )


def cross_site(request: Request) -> bool:
    """Whether a browser says the request comes from another site; a client that sends no
    such header is not a browser."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    return origin is not None and not same_origin(request)


JSON_ONLY = frozenset({"application/json"})


def require(
    role: Role, *, bodies: frozenset[str] = JSON_ONLY
) -> Callable[[Request], Awaitable[Principal]]:
    """A dependency that admits `role` or a higher one. `bodies` are the media types the
    route takes."""

    async def check(request: Request) -> Principal:
        auth: Auth = request.app.state.auth
        principal = await auth.principal(request)
        if principal is None:
            raise ProblemException(401, "Not signed in", "send a token", code="unauthenticated")
        if role == "runner":
            if principal.role != "runner":
                raise ProblemException(403, "Forbidden", "this needs a runner token", code="role")
            return principal
        if principal.role == "runner" or RANK[principal.role] < RANK[role]:
            raise ProblemException(403, "Forbidden", f"this needs the {role} role", code="role")
        if request.method not in SAFE:
            refused = principal.via == "cookie" and not same_origin(request)
            if principal.via == "open" and cross_site(request):
                refused = True
            if refused:
                raise ProblemException(
                    403, "Forbidden", "cross-origin request refused", code="origin"
                )
            kind = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            length = request.headers.get("content-length")
            has_body = kind or (length not in (None, "0"))
            if has_body and kind not in bodies:
                raise ProblemException(
                    415,
                    "Unsupported media type",
                    f"send {' or '.join(sorted(bodies))}",
                    code="media_type",
                )
        return principal

    return check


class SecurityHeaders:
    """Adds the content security policy and the other hardening headers to every reply."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in HEADERS if k not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, wrapped)
