"""``PUT /mcp-files/uploads/{slot}`` / ``GET /mcp-files/downloads/{token}`` --
the local file bridge's own HTTP endpoints (ADR 0007 SS1.3.3), reached by the
``.mcpb`` shim on behalf of the user (or, for a client with no shim, ``GET
.../downloads/{token}`` directly -- see local_files.py's SS1.4 fallback).

Mounted behind the *same* bearer-token auth as ``/mcp``: this module builds
its own copy of ``routes_mcp.build_mcp_asgi_app``'s three-layer middleware
stack (``RequireAuthMiddleware`` / ``AuthContextMiddleware`` /
``AuthenticationMiddleware(backend=BearerAuthBackend(verifier))``) around a
small nested Starlette app, exactly the same shape ``mount_mcp`` already
uses to keep ``/mcp`` off the main approval-surface app's own middleware
stack (audience separation: a browser session cookie must never be
accepted here any more than it is on ``/mcp``; see ADR 0061). The principal for both
routes always comes from that bearer token via ``principal_from_access_
token``, never from a cookie or a path parameter.

Also ``PUT /mcp-files/slots/{slot}`` / ``GET /mcp-files/fetch/{token}`` --
the capability pair (ADR 0007's "Clients without the bridge" section, ADR 0028),
reached by any HTTP client, no bearer header (or anything else) required:
the capability token embedded in the URL is itself the credential. Built by
``mount_capability_routes`` below, as plain, unauthenticated ``Route``s --
never wrapped in this module's own bearer-auth stack, since the whole point
is a caller that may have no way to set a custom header at all (a sandboxed
agent shelling out to ``curl``). Authorization for these two lives entirely
in ``upload_staging.UploadStagingStore.afill_capability``/``download_
staging.DownloadStagingStore.claim_capability``, which check the token
against a live, unexpired, not-yet-claimed slot/entry and nothing else --
see either method's own docstring for why skipping the principal check here
doesn't skip it overall (``local_files.require_local_files``'s ``upload:``
handling re-checks it, principal-bound, before a claimed upload is ever
read by a tool call).
"""
from __future__ import annotations

import base64
import hashlib
import logging

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, get_route_path
from starlette.types import ASGIApp

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..download_staging import get_download_staging_store
from ..upload_staging import UploadAlreadyFilledError, UploadTooLargeError, get_upload_staging_store
from .mcp_auth import principal_from_access_token, single_token_verifier as _single_token_verifier

logger = logging.getLogger(__name__)

FILE_BRIDGE_PREFIX = "/mcp-files"
_NOT_FOUND = PlainTextResponse("Not found.", status_code=404, headers={"Cache-Control": "no-store"})


def _decode_token(raw: str) -> bytes | None:
    """Same base64url-with-padding-fixup decode as routes_downloads.py's
    own ``_decode_token`` and local_files.py's private encoder -- kept as
    its own small copy here rather than imported, matching how
    routes_downloads.py doesn't reach into download_staging.py's internals
    either; every token these two routes decode is always 32 raw bytes."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        token = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None
    return token if len(token) == 32 else None


def _audit_bridge_upload_received(principal_id: str, size_bytes: int) -> None:
    """Mirrors routes_downloads.py's own ``_audit_staged_download_served``:
    un-gated (the tool call that will consume this upload already went
    through gate.py's normal audit trail once it resumes), wrapped in
    try/except so a logging failure never blocks the response, never logs
    the slot token itself."""
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=_now_iso(), week=current_week(), request_id="", connector="", tool="",
            tool_name="", summary=f"File bridge upload received ({size_bytes} bytes)",
            sender=principal_id, decision="bridge_upload_received", auto_accept_rule="",
            latency_seconds=0.0, pii_detected=False,
        ))
    except Exception as exc:  # noqa: BLE001 -- audit logging must never break the upload
        logger.warning("Audit log write failed for bridge upload: %s", exc)


def _audit_bridge_download_served(principal_id: str, name: str, size_bytes: int) -> None:
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=_now_iso(), week=current_week(), request_id="", connector="", tool="",
            tool_name="", summary=f"File bridge download served: {name!r} ({size_bytes} bytes)",
            sender=principal_id, decision="bridge_download_served", auto_accept_rule="",
            latency_seconds=0.0, pii_detected=False,
        ))
    except Exception as exc:  # noqa: BLE001 -- audit logging must never break the download
        logger.warning("Audit log write failed for bridge download: %s", exc)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _put_upload(request: Request) -> Response:
    principal = principal_from_access_token(get_access_token())
    token = _decode_token(request.path_params["slot"])
    if token is None:
        return _NOT_FOUND
    store = get_upload_staging_store()
    try:
        size = await store.afill(token, principal.id, request.stream())
    except LookupError:
        return _NOT_FOUND
    except UploadAlreadyFilledError as exc:
        return PlainTextResponse(str(exc), status_code=409, headers={"Cache-Control": "no-store"})
    except UploadTooLargeError as exc:
        return PlainTextResponse(str(exc), status_code=413, headers={"Cache-Control": "no-store"})
    _audit_bridge_upload_received(principal.id, size)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


async def _get_download(request: Request) -> Response:
    principal = principal_from_access_token(get_access_token())
    token = _decode_token(request.path_params["token"])
    if token is None:
        return _NOT_FOUND
    result = get_download_staging_store().claim(token, principal.id)
    if result is None:
        return _NOT_FOUND
    data, name, _mime_type = result
    _audit_bridge_download_served(principal.id, name, len(data))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )


async def _put_slot(request: Request) -> Response:
    """The unauthenticated capability upload -- see module docstring. No
    ``principal_from_access_token(get_access_token())`` here at all: this
    route isn't wrapped in the bearer-auth middleware stack in the first
    place (``mount_capability_routes``), so there is no access token to
    read."""
    token = _decode_token(request.path_params["slot"])
    if token is None:
        return _NOT_FOUND
    store = get_upload_staging_store()
    try:
        size = await store.afill_capability(token, request.stream())
    except LookupError:
        return _NOT_FOUND
    except UploadAlreadyFilledError as exc:
        return PlainTextResponse(str(exc), status_code=409, headers={"Cache-Control": "no-store"})
    except UploadTooLargeError as exc:
        return PlainTextResponse(str(exc), status_code=413, headers={"Cache-Control": "no-store"})
    _audit_bridge_upload_received("(capability)", size)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


async def _get_fetch(request: Request) -> Response:
    """The unauthenticated capability download -- see module docstring, and
    ``_put_slot`` above for why there's no access token to read here
    either."""
    token = _decode_token(request.path_params["token"])
    if token is None:
        return _NOT_FOUND
    result = get_download_staging_store().claim_capability(token)
    if result is None:
        return _NOT_FOUND
    data, name, _mime_type = result
    _audit_bridge_download_served("(capability)", name, len(data))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )


def _capability_asgi_app() -> ASGIApp:
    """The two capability routes as one plain Starlette app, with
    *no* auth middleware around it -- see module docstring."""
    return Starlette(routes=[
        Route("/slots/{slot}", _put_slot, methods=["PUT"]),
        Route("/fetch/{token}", _get_fetch, methods=["GET"]),
    ])


class _FileBridgeRouter:
    """Dispatches by top-level path segment instead of nesting two
    ``Mount``s at the identical ``/mcp-files`` prefix. Starlette's own
    ``Mount.matches()`` only checks whether the request path starts with
    the mount's own prefix -- it never looks inside to see whether one of
    the mount's *own* routes actually matches -- so two ``Mount``s
    registered at the same prefix are not additive: whichever is tried
    first by the outer ``Router`` swallows every request under that
    prefix, auth stack and all, and the second ``Mount`` is unreachable
    dead code. (Caught by tests/unit/web/test_server.py's own
    audience-separation coverage: a capability route nested behind
    ``mount_file_bridge``'s bearer app came back 401, not 404, because the
    request never got anywhere near the unauthenticated app at all.)

    So both pairs of routes live under exactly one ``Mount``, and this
    class is that mount's own ASGI app: ``/slots`` and ``/fetch`` need no
    auth at all and go straight to the capability app; everything else
    (``/uploads``, ``/downloads``) goes through the bearer-authenticated
    app.
    """

    _NO_AUTH_SEGMENTS = frozenset({"slots", "fetch"})

    def __init__(self, authenticated: ASGIApp, capability: ASGIApp) -> None:
        self._authenticated = authenticated
        self._capability = capability

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            # Starlette's Mount does NOT rewrite scope["path"] to be
            # mount-relative for a plain ASGI ``app=`` callable like this
            # one -- it only extends scope["root_path"] (see Mount.matches
            # in starlette/routing.py) and expects the wrapped app to
            # compute its own route-relative path from root_path, exactly
            # as get_route_path() does (the same helper Starlette's own
            # Router/Route matching uses internally). Reading scope["path"]
            # directly here previously always started with "/mcp-files/...",
            # never matched "slots"/"fetch", and silently sent every
            # capability request into the bearer-authenticated app instead.
            segment = get_route_path(scope).lstrip("/").split("/", 1)[0]
            if segment in self._NO_AUTH_SEGMENTS:
                await self._capability(scope, receive, send)
                return
        await self._authenticated(scope, receive, send)


def build_file_bridge_asgi_app(
    *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> ASGIApp:
    """Same shape as ``routes_mcp.build_mcp_asgi_app``: exactly one of
    ``token``/``verifier`` should be given. ``token`` builds a one-off
    ``PerUserTokenVerifier`` registered to ``LOCAL_PRINCIPAL`` alone (a
    convenience for a caller with no multi-principal registration to grow
    -- web/server.py's real local-mode wiring passes a shared ``verifier``
    instead, the same instance ``MINT MCP`` registers new principals into);
    org mode passes its own ``OrgOAuthProvider`` as ``verifier``.

    This is the bearer-authenticated pair (``/uploads``, ``/downloads``)
    alone -- ``mount_file_bridge`` is what combines it with the
    capability pair under one ``Mount``; a caller that wants only the
    authenticated app itself (as a handful of existing tests do) still
    gets exactly that from this function, unchanged.
    """
    inner = Starlette(routes=[
        Route("/uploads/{slot}", _put_upload, methods=["PUT"]),
        Route("/downloads/{token}", _get_download, methods=["GET"]),
    ])
    if verifier is None:
        if token is None:
            raise ValueError("build_file_bridge_asgi_app needs either token or verifier")
        verifier = _single_token_verifier(token)
    protected = RequireAuthMiddleware(inner, required_scopes=[], resource_metadata_url=resource_metadata_url)
    authenticated = AuthContextMiddleware(protected)
    return AuthenticationMiddleware(authenticated, backend=BearerAuthBackend(verifier))


def mount_file_bridge(
    *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> list[BaseRoute]:
    """All four file-bridge routes -- the shim's bearer-authenticated
    ``/uploads``/``/downloads`` pair plus the unauthenticated
    ``/slots``/``/fetch`` capability pair (ADR 0007's "Clients without the
    bridge" section) -- as one ``Mount`` under ``FILE_BRIDGE_PREFIX``. Used
    by local mode's own ``build_app`` alongside ``mount_mcp``'s own route,
    whenever a dispatcher is present. Not mounted in org mode
    (``_build_org_app``): the bearer-authenticated half is Claude-Desktop-
    only (ADR 0007), and org-mode connectors never call into
    local_files.py's shim-facing side in the first place -- see that ADR's
    own "why is org mode untouched" section. Org mode gets the capability
    pair alone, via ``mount_capability_routes`` below."""
    authenticated = build_file_bridge_asgi_app(token=token, verifier=verifier, resource_metadata_url=resource_metadata_url)
    app = _FileBridgeRouter(authenticated, _capability_asgi_app())
    return [Mount(FILE_BRIDGE_PREFIX, app=app)]


def mount_capability_routes() -> list[BaseRoute]:
    """The two capability routes alone, as one ``Mount`` under
    ``FILE_BRIDGE_PREFIX`` -- no bearer-auth stack at all, unlike
    ``mount_file_bridge``. This is org mode's own mount
    (``_build_org_app``): org mode never mounts ``mount_file_bridge``'s
    bearer-authenticated pair (see that function's own docstring), but
    still needs ``privacyfence_create_upload_slot`` and
    ``DownloadDeliveryConfig.agent_links``'s staged-link delivery to work.
    Local mode gets the same two routes from ``mount_file_bridge`` instead
    -- combined into the *same* ``/mcp-files`` ``Mount`` alongside the
    bearer-authenticated pair, since two ``Mount``s can't share one prefix
    (see ``_FileBridgeRouter``'s own docstring) -- so local mode's
    ``build_app`` must never call this function too."""
    return [Mount(FILE_BRIDGE_PREFIX, app=_capability_asgi_app())]


__all__ = [
    "FILE_BRIDGE_PREFIX",
    "build_file_bridge_asgi_app",
    "mount_capability_routes",
    "mount_file_bridge",
]
