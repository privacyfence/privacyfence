"""Bearer-token verifiers for ``/mcp``: the scoped-token verifier (ADR 0164), the composite that
routes a token to it or to the organization OAuth 2.1 provider
(web/oauth_provider.py's ``OrgOAuthProvider``, which satisfies the SDK's ``TokenVerifier``
protocol, ``mcp.server.auth.provider.TokenVerifier``), and the helper that turns a verified
token into a principal.

The MCP access token is deliberately a **separate secret from the approval surface's own
session/CSRF cookie**: audience separation -- "the MCP access token must never be accepted on
approval-decision endpoints, and the browser session cookie must never be accepted on /mcp" --
holds because the two are generated independently and never compared against each other anywhere
in this codebase (ADR 0061).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Iterator

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .. import paths, scoped_tokens
from ..principal import LOCAL_PRINCIPAL, Principal
from ..scoped_tokens import ScopedToken

MCP_TOKEN_FILE_NAME = "mcp_token"  # nosec B105  # a filename, not a credential value

# The ``client_id`` prefix of an ``AccessToken`` that ``ScopedTokenVerifier``
# issued (ADR 0164): ``scoped:<token id>``, so the SDK binds such a token's
# stateful sessions apart from the person's other clients.
SCOPED_CLIENT_PREFIX = "scoped:"


class _SingleTokenVerifier(TokenVerifier):
    """Verifies one fixed bearer token, compared in constant time."""

    def __init__(self, token: str, principal_id: str) -> None:
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()
        self._principal_id = principal_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).digest(), self._digest):
            return None
        return AccessToken(token=token, client_id="local", scopes=[], subject=self._principal_id)


def single_token_verifier(token: str, principal: Principal = LOCAL_PRINCIPAL) -> TokenVerifier:
    """A verifier that accepts exactly ``token`` -- for a direct, low-level call to
    ``build_mcp_asgi_app(token=...)`` or ``build_file_bridge_asgi_app(token=...)``, or a test."""
    return _SingleTokenVerifier(token, principal.id)


def scoped_token_principals() -> Iterator[Principal]:
    """Every principal whose ``scoped_tokens.json`` the store loads at
    startup: the local principal plus every ``users/<id>/`` directory.
    Unlike ``preload_verifier()`` this does not stop on an unseparated
    install: an organization server is not privilege-separated, and its
    people's scoped tokens must still verify after a restart (ADR 0165)."""
    yield LOCAL_PRINCIPAL
    users_root = paths.data_dir() / "users"
    if not users_root.is_dir():
        return
    for entry in sorted(users_root.iterdir()):
        if entry.is_dir() and paths.safe_principal_id(entry.name) == entry.name:
            yield Principal(id=entry.name)


class ScopedTokenVerifier(TokenVerifier):
    """Verifies a scoped token (ADR 0164) against ``scoped_tokens.get_store()``.
    The ``AccessToken`` carries ``client_id="scoped:<id>"``, the owner as
    ``subject``, the token's expiry (which the SDK's ``BearerAuthBackend``
    enforces again) and the owner's claims, never ``is_admin``."""

    async def verify_token(self, token: str) -> AccessToken | None:
        found = scoped_tokens.get_store().find(token)
        if found is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"{SCOPED_CLIENT_PREFIX}{found.id}",
            scopes=["scoped"],
            expires_at=int(found.expires_at),
            subject=found.principal_id,
            claims={
                "email": found.principal_email,
                "display_name": found.principal_display_name,
                "is_admin": False,
                "email_verified": found.principal_email_verified,
                "scoped_token_id": found.id,
            },
        )


class CompositeTokenVerifier(TokenVerifier):
    """Routes a bearer token to exactly one of two verifiers by its prefix: a
    ``scoped_tokens.TOKEN_PREFIX`` token only to ``scoped``, any other only to
    ``inner`` (the local token map or the org OAuth provider). Neither store
    is ever probed with the other's tokens (ADR 0164). Mounted on ``/mcp``
    only; the bearer file bridge keeps the plain local verifier, so a scoped
    token is refused there."""

    def __init__(self, scoped: TokenVerifier, inner: TokenVerifier) -> None:
        self._scoped = scoped
        self._inner = inner

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(scoped_tokens.TOKEN_PREFIX):
            return await self._scoped.verify_token(token)
        return await self._inner.verify_token(token)


def scope_from_access_token(token: AccessToken | None) -> ScopedToken | None:
    """The scope of a call made with ``token``: ``None`` only for a token
    that is not a scoped one. For a scoped token the record is read fresh
    from the store on every call; when it is gone (revoked since the token
    was verified, or a file that no longer loads) the answer is
    ``scoped_tokens.REVOKED_SCOPE``, which allows nothing -- a missing
    record never falls back to the person's full access (ADR 0164)."""
    if token is None or not token.client_id.startswith(SCOPED_CLIENT_PREFIX):
        return None
    token_id = str((token.claims or {}).get("scoped_token_id", "") or "")
    found = scoped_tokens.get_store().get(token.subject or "", token_id) if token_id else None
    return found if found is not None else scoped_tokens.REVOKED_SCOPE


def principal_from_access_token(token: AccessToken | None) -> Principal:
    """The ``/mcp`` endpoint's principal_scope() entry point (entered
    once per HTTP request, in exactly one place per surface) -- routes_mcp.py calls this once per
    tool call, wrapping dispatch in ``principal_scope(...)`` around it.

    A fixed-token verifier (``single_token_verifier``) sets ``client_id="local"``
    and ``subject=<the presenting principal's id>`` (ADR 0008) -- a
    ``subject`` of ``LOCAL_PRINCIPAL.id`` (the install's owner) resolves to
    ``LOCAL_PRINCIPAL`` itself rather than a freshly-built ``Principal``
    with no email/display_name, so every existing per-principal registry
    keeps seeing the exact object it always has for the owner.

    Org mode: the token comes from ``web/oauth_provider.py``'s
    ``OrgOAuthProvider`` instead, whose ``AccessToken.subject`` is the
    resolved human's principal id (an OAuth client_id identifies *which
    Claude installation* registered via DCR, not *which human* is using
    it -- ``client_id`` is deliberately never used as a principal id here)
    and whose ``AccessToken.claims`` carries the email/display_name/
    is_admin ``OrgOAuthProvider._mint_tokens`` stashed there. Falls back to
    ``client_id`` only if a verifier somehow returns a token with no
    ``subject`` at all -- better than crashing, though nothing in this
    codebase does that today outside local mode's own case, which is
    handled above already.

    A scoped token (ADR 0164, ``client_id="scoped:<id>"``) owned by the
    local principal resolves to ``LOCAL_PRINCIPAL`` itself; any other owner
    goes through the org branch, built from ``subject`` and the claims
    ``ScopedTokenVerifier`` copied from the token's record.
    """
    if token is None:
        return LOCAL_PRINCIPAL
    if token.client_id == LOCAL_PRINCIPAL.id:
        principal_id = token.subject or LOCAL_PRINCIPAL.id
        return LOCAL_PRINCIPAL if principal_id == LOCAL_PRINCIPAL.id else Principal(id=principal_id)
    if token.client_id.startswith(SCOPED_CLIENT_PREFIX) and token.subject == LOCAL_PRINCIPAL.id:
        return LOCAL_PRINCIPAL
    principal_id = token.subject or token.client_id
    claims = token.claims or {}
    return Principal(
        id=principal_id,
        email=str(claims.get("email", "") or ""),
        display_name=str(claims.get("display_name", "") or ""),
        is_admin=bool(claims.get("is_admin", False)),
        email_verified=claims.get("email_verified") is True,
    )
