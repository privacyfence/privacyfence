"""Local-mode bearer-token auth for ``/mcp``, plus the seam org mode's real
OAuth 2.1 verifier (web/oauth_provider.py's ``OrgOAuthProvider``) plugs
into.

``StaticTokenVerifier`` below is a ``TokenVerifier`` (the official SDK's
protocol, ``mcp.server.auth.provider.TokenVerifier``) checking a single
shared secret -- the same "possession of this file is the authority"
posture ``~/.privacyfence/ipc_token`` had for the bridge, before P5 retired
it. Not real OAuth 2.1 -- that's org mode
(landed at P7 as ``OrgOAuthProvider``, which satisfies the exact same ``TokenVerifier``
protocol via its own ``verify_token``). Using the SDK's own
``TokenVerifier``/``BearerAuthBackend``/``RequireAuthMiddleware`` here
meant P7 only had to swap this one class for a real verifier;
routes_mcp.py's own wiring didn't change (see that module's
``build_mcp_asgi_app``, which takes a ``verifier: TokenVerifier`` --
either this module's or ``OrgOAuthProvider``'s).

This token is deliberately a **separate secret from the approval surface's
own session/CSRF cookie**: §10.3's audience separation --
"the MCP access token must never be accepted on approval-decision
endpoints, and the browser session cookie must never be accepted on
/mcp" -- has to hold even if someone reuses one file's contents by hand, so
the two are generated independently and never compared against each other
anywhere in this codebase. See web/test_routes_mcp.py's audience-separation
test, which is the one required to fail loudly if that ever changes.
"""
from __future__ import annotations

import hmac
import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .. import paths, privilege_separation
from ..principal import LOCAL_PRINCIPAL, Principal

MCP_TOKEN_FILE_NAME = "mcp_token"  # nosec B105  # a filename, not a credential value


def load_or_create_mcp_token() -> str:
    """Reused across daemon restarts (same file) -- the agent's own
    long-lived credential, unlike the approval surface's session cookie."""
    # handoff_dir(), not data_dir(): this is the one credential #428 keeps
    # deliberately reachable by the agent ("mcp_token stays reachable by the
    # agent. It is the agent's own credential and the product doesn't work
    # without it"), so on a privilege-separated install it lives in the
    # directory the user's own session can still read rather than in the
    # service-account-owned root. Identical path on every other install --
    # handoff_dir() *is* data_dir() there.
    path = paths.handoff_dir() / MCP_TOKEN_FILE_NAME
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            # The one handoff file that isn't rewritten on every start, so
            # the only one whose mode has to be re-asserted on the way past:
            # a token carried over from a pre-#428-Phase-4 install arrives
            # still 0600 and owned by the service account, which would leave
            # the agent unable to read its own credential. No-op everywhere
            # else.
            privilege_separation.ensure_handoff_file_mode(path)
            return token
    token = secrets.token_hex(32)
    privilege_separation.write_handoff_file(path, token)
    return token


class StaticTokenVerifier(TokenVerifier):
    """Verifies a bearer token against one fixed shared secret -- see module
    docstring. ``client_id`` is always ``"local"``: there is exactly one
    principal in local mode (§9.2), so there's nothing else it could be."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(token=token, client_id="local", scopes=[])


def principal_from_access_token(token: AccessToken | None) -> Principal:
    """The ``/mcp`` endpoint's principal_scope() entry point (P6: "entered
    once per HTTP request, in exactly one place per surface") -- routes_mcp.py calls this once per
    tool call, wrapping dispatch in ``principal_scope(...)`` around it.

    Local mode: ``StaticTokenVerifier`` above only ever mints
    ``client_id="local"`` and no ``subject``, so this resolves to
    ``LOCAL_PRINCIPAL``.

    Org mode (P7): the token comes from ``web/oauth_provider.py``'s
    ``OrgOAuthProvider`` instead, whose ``AccessToken.subject`` is the
    resolved human's principal id (an OAuth client_id identifies *which
    Claude installation* registered via DCR, not *which human* is using
    it -- ``client_id`` is deliberately never used as a principal id here)
    and whose ``AccessToken.claims`` carries the email/display_name/
    is_admin ``OrgOAuthProvider._mint_tokens`` stashed there. Falls back to
    ``client_id`` only if a verifier somehow returns a token with no
    ``subject`` at all -- better than crashing, though nothing in this
    codebase does that today outside ``StaticTokenVerifier``'s own
    local-mode case, which is handled above already.
    """
    if token is None or token.client_id == LOCAL_PRINCIPAL.id:
        return LOCAL_PRINCIPAL
    principal_id = token.subject or token.client_id
    claims = token.claims or {}
    return Principal(
        id=principal_id,
        email=str(claims.get("email", "") or ""),
        display_name=str(claims.get("display_name", "") or ""),
        is_admin=bool(claims.get("is_admin", False)),
    )
