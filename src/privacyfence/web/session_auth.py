"""CSRF and origin checks for the ``pf_session`` cookie.

``check_csrf`` is a double-submit check: the session id itself doubles as the token (the same
reasoning ``web/org_session.py``'s ``check_csrf`` gives). ``check_origin`` is defense in depth on
top of it.
"""
from __future__ import annotations

import hmac

from starlette.requests import Request

SESSION_COOKIE = "pf_session"


def check_csrf(request: Request, csrf: str | None) -> bool:
    """Double-submit check: the session cookie (HttpOnly, so page JS never
    reads it -- it can only have been set by this server's own
    set_session_cookie) must equal the csrf value the page's own bridge
    shim baked in at render time -- the session id itself doubles as the
    CSRF token, the same reasoning web/org_session.py's own check_csrf
    gives for why no separate per-session value needs to be minted and
    tracked. Constant-time compare, so the check leaks no timing."""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or not csrf:
        return False
    return hmac.compare_digest(cookie, csrf)


def check_origin(request: Request) -> bool:
    """Defense in depth on top of the double-submit token above -- a
    same-site page couldn't forge the cookie value into its own request
    body, but this also stops a same-origin-cookie-jar edge case from ever
    mattering. ``None`` (no Origin header at all, e.g. a same-origin
    navigation in some browsers) is accepted -- only a *mismatched* Origin
    is rejected."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin == f"{request.url.scheme}://{request.url.netloc}"


__all__ = [
    "SESSION_COOKIE",
    "check_csrf",
    "check_origin",
]
