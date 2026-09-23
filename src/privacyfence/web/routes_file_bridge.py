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
stack (SEC-06's audience separation: a browser session cookie must never be
accepted here any more than it is on ``/mcp``). The principal for both
routes always comes from that bearer token via ``principal_from_access_
token``, never from a cookie or a path parameter.
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
from starlette.routing import BaseRoute, Mount, Route
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
    org mode passes its own ``OrgOAuthProvider`` as ``verifier``."""
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
    """The two file-bridge routes, as one ``Mount`` under
    ``FILE_BRIDGE_PREFIX``. ``web/server.py``'s local-mode ``build_app``
    extends its own ``extra_routes`` with this, alongside ``mount_mcp``'s
    own route, whenever a dispatcher is present. Not mounted in org mode
    (``_build_org_app``): the file bridge is Claude-Desktop-only (ADR
    0007), and org-mode connectors never call into local_files.py in the
    first place -- see that ADR's own "why is org mode untouched" section."""
    app = build_file_bridge_asgi_app(token=token, verifier=verifier, resource_metadata_url=resource_metadata_url)
    return [Mount(FILE_BRIDGE_PREFIX, app=app)]


__all__ = ["FILE_BRIDGE_PREFIX", "build_file_bridge_asgi_app", "mount_file_bridge"]
