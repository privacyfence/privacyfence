"""Local-mode bearer-token auth for ``/mcp``, plus the seam org mode's real
OAuth 2.1 verifier (web/oauth_provider.py's ``OrgOAuthProvider``) plugs
into.

``PerUserTokenVerifier`` below is a ``TokenVerifier`` (the official SDK's
protocol, ``mcp.server.auth.provider.TokenVerifier``) checking a bearer
token against a ``{sha256(token): principal_id}`` map rather than one fixed
shared secret. A separated install can have one principal per OS user
(ADR 0008, ``docs/adr/0008-one-principal-per-os-user.md``), and there a
single shared secret *is* the leak (any OS user who could read the file
could act as every other one), so each principal gets its own token, minted
over the control channel (``web/control_channel.py``'s ``MINT MCP``/``ROTATE
MCP``) rather than read from a shared file. Not real OAuth 2.1 -- that's
org mode's ``OrgOAuthProvider``, which satisfies the exact same
``TokenVerifier`` protocol via its own ``verify_token``. Using the SDK's own
``TokenVerifier``/``BearerAuthBackend``/``RequireAuthMiddleware`` here
means org mode only swaps this one class for a real verifier;
routes_mcp.py's own wiring is the same in both modes (see that module's
``build_mcp_asgi_app``, which takes a ``verifier: TokenVerifier`` --
either this module's or ``OrgOAuthProvider``'s).

This token is deliberately a **separate secret from the approval surface's
own session/CSRF cookie**: audience separation --
"the MCP access token must never be accepted on approval-decision
endpoints, and the browser session cookie must never be accepted on
/mcp" -- has to hold even if someone reuses one file's contents by hand, so
the two are generated independently and never compared against each other
anywhere in this codebase. See web/test_routes_mcp.py's audience-separation
test, which is the one required to fail loudly if that ever changes.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .. import paths, privilege_separation, secure_files
from ..principal import LOCAL_PRINCIPAL, Principal

MCP_TOKEN_FILE_NAME = "mcp_token"  # nosec B105  # a filename, not a credential value


def _mcp_token_path(principal: Principal) -> Path:
    """Where ``principal``'s own persisted MCP token lives. On an
    unseparated install this is ``handoff_dir()/mcp_token`` (``handoff_dir()``
    *is* ``data_dir()`` there) -- local mode has exactly one principal on
    such an install. On a privilege-separated install this is
    ``authority_dir(principal)/mcp_token`` instead -- unreadable to any OS
    user directly (``0700``, service-account-owned), reachable only by
    minting it fresh over the control channel (``MINT MCP``), which is what
    makes a shared, world/group-readable file unnecessary at all once every
    principal has its own."""
    if not privilege_separation.is_enabled():
        return paths.handoff_dir() / MCP_TOKEN_FILE_NAME
    return paths.authority_dir(principal) / MCP_TOKEN_FILE_NAME


def _read_token(path: Path) -> str | None:
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def load_or_create_mcp_token(principal: Principal = LOCAL_PRINCIPAL) -> str:
    """``principal``'s own token, reused across daemon restarts (same file)
    -- the direct successor of the pre-ADR-0008 shared ``mcp_token``, minus
    the sharing. ``MINT MCP`` (``web/control_channel.py``) is what actually
    calls this for a real install; a bare default of ``LOCAL_PRINCIPAL``
    keeps every pre-ADR-0008 caller (including this module's own tests)
    working unchanged."""
    path = _mcp_token_path(principal)
    token = _read_token(path)
    if token is not None:
        return token
    return _write_new_token(principal, path)


def rotate_mcp_token(principal: Principal = LOCAL_PRINCIPAL, *, verifier: "PerUserTokenVerifier | None" = None) -> str:
    """Discards whatever token ``principal`` had and mints a fresh one --
    ``ROTATE MCP``'s own backing. Unlike ``load_or_create_mcp_token()``,
    always writes a new value even if one already existed. When
    ``verifier`` is given, the superseded token (if any) is unregistered
    from it first, so it stops verifying immediately rather than staying
    valid in that process's memory until the next restart -- the caller is
    still responsible for registering the *new* token this returns."""
    path = _mcp_token_path(principal)
    if verifier is not None:
        old = _read_token(path)
        if old is not None:
            verifier.unregister(old)
    return _write_new_token(principal, path)


def _write_new_token(principal: Principal, path: Path) -> str:
    token = secrets.token_hex(32)
    if privilege_separation.is_enabled():
        # authority_dir(principal) already created this file's parent at
        # 0700, service-account-owned -- plain atomic_write_text's own
        # defaults are exactly that mode, unlike write_handoff_file()'s
        # group-shared one below, which this path must not use: nothing
        # but the daemon itself should ever read this file directly.
        secure_files.atomic_write_text(path, token)
    else:
        privilege_separation.write_handoff_file(path, token)
    return token


class PerUserTokenVerifier(TokenVerifier):
    """Verifies a bearer token against a ``{sha256(token): principal_id}``
    map instead of one fixed shared secret -- see module docstring.
    ``client_id`` stays the literal ``"local"`` for every token this
    verifier issues, exactly as ``StaticTokenVerifier`` always set it: it
    identifies *this install's own local-mode token scheme*, not which
    principal presented it -- that is what ``subject`` is for, and what
    ``principal_from_access_token()`` below reads back.

    ``register()`` looks up by the token's sha256 digest, not the token
    itself, so a lookup against an unknown or partially-guessed token never
    compares raw secret bytes one entry at a time against this process's
    own growing map -- the same "hash first, then a plain dict lookup"
    posture this codebase already uses wherever a secret is looked up by
    value rather than compared to one known value (see
    ``upload_staging.py``'s own claim-by-token lookup)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._principal_by_digest: dict[str, str] = {}

    def register(self, token: str, principal_id: str) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._principal_by_digest[digest] = principal_id

    def unregister(self, token: str) -> None:
        """Drops a superseded token (``ROTATE MCP``'s old value) so it stops
        verifying immediately, rather than staying valid until this process
        restarts."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._principal_by_digest.pop(digest, None)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            principal_id = self._principal_by_digest.get(digest)
        if principal_id is None:
            return None
        return AccessToken(token=token, client_id="local", scopes=[], subject=principal_id)


def single_token_verifier(token: str, principal: Principal = LOCAL_PRINCIPAL) -> PerUserTokenVerifier:
    """A ``PerUserTokenVerifier`` registered with exactly one token -- for a
    caller that has no multi-principal registration to grow (a direct,
    low-level call to ``build_mcp_asgi_app(token=...)``/
    ``build_file_bridge_asgi_app(token=...)``, or a test). Real local-mode
    traffic (``web/server.py``) builds and preloads its own long-lived
    ``PerUserTokenVerifier`` instead, so ``MINT MCP`` has something to
    register new principals into -- see ``preload_verifier()``."""
    verifier = PerUserTokenVerifier()
    verifier.register(token, principal.id)
    return verifier


def preload_verifier(verifier: PerUserTokenVerifier) -> None:
    """Populates ``verifier`` from every principal's own already-persisted
    token file at daemon startup -- ``MINT MCP``/``ROTATE MCP`` grow the map
    as they're called, but a token minted by a *previous* run has to verify
    again on this one without anyone calling ``MINT MCP`` first (a Claude
    Code config that pasted the token once and never asks again). Local
    principal plus every already-provisioned ``users/<id>/`` -- the same
    existence-only enumeration ``paths.all_uploads_dirs()``/
    ``all_downloads_dirs()`` already use, so a principal nobody has ever
    minted a token for (or one on an unseparated install, where there is
    only ever the local principal) contributes nothing rather than an
    empty file."""
    _preload_one(verifier, LOCAL_PRINCIPAL)
    if not privilege_separation.is_enabled():
        return
    users_root = paths.data_dir() / "users"
    if not users_root.is_dir():
        return
    for entry in sorted(users_root.iterdir()):
        if not entry.is_dir() or not paths.safe_principal_id(entry.name) == entry.name:
            continue
        _preload_one(verifier, Principal(id=entry.name))


def _preload_one(verifier: PerUserTokenVerifier, principal: Principal) -> None:
    token = _read_token(_mcp_token_path(principal))
    if token is not None:
        verifier.register(token, principal.id)


def principal_from_access_token(token: AccessToken | None) -> Principal:
    """The ``/mcp`` endpoint's principal_scope() entry point (entered
    once per HTTP request, in exactly one place per surface) -- routes_mcp.py calls this once per
    tool call, wrapping dispatch in ``principal_scope(...)`` around it.

    Local mode: ``PerUserTokenVerifier`` above sets ``client_id="local"``
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
    """
    if token is None:
        return LOCAL_PRINCIPAL
    if token.client_id == LOCAL_PRINCIPAL.id:
        principal_id = token.subject or LOCAL_PRINCIPAL.id
        return LOCAL_PRINCIPAL if principal_id == LOCAL_PRINCIPAL.id else Principal(id=principal_id)
    principal_id = token.subject or token.client_id
    claims = token.claims or {}
    return Principal(
        id=principal_id,
        email=str(claims.get("email", "") or ""),
        display_name=str(claims.get("display_name", "") or ""),
        is_admin=bool(claims.get("is_admin", False)),
    )
