"""``PUT /mcp-files/slots/{slot}`` / ``GET /mcp-files/fetch/{token}`` -- the capability pair
(ADR 0028, ADR 0304), reached by any HTTP client, no bearer header (or anything else) required:
the capability token embedded in the URL is itself the credential. Built by
``mount_capability_routes`` below, as plain, unauthenticated ``Route``s -- never wrapped in a
bearer-auth stack, since the whole point is a caller that may have no way to set a custom header
at all (a sandboxed agent shelling out to ``curl``). Authorization for these two lives entirely
in ``upload_staging.UploadStagingStore.afill_capability``/``download_staging.DownloadStagingStore.
claim_capability``, which check the token against a live, unexpired, not-yet-claimed slot/entry
and nothing else -- see either method's own docstring for why skipping the principal check here
doesn't skip it overall (``local_files.require_local_files``'s ``upload:`` handling re-checks it,
principal-bound, before a claimed upload is ever read by a tool call).
"""
from __future__ import annotations

import base64
import hashlib
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp

from ..agent_identity import agent_scope
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..download_staging import ClaimedDownload, get_download_staging_store
from ..upload_staging import UploadAlreadyFilledError, UploadTooLargeError, get_upload_staging_store

logger = logging.getLogger(__name__)

FILE_BRIDGE_PREFIX = "/mcp-files"
_NOT_FOUND = PlainTextResponse("Not found.", status_code=404, headers={"Cache-Control": "no-store"})


def _decode_token(raw: str) -> bytes | None:
    """Same base64url-with-padding-fixup decode as routes_downloads.py's
    own ``_decode_token`` and local_files.py's private encoder; every token these two routes
    decode is always 32 raw bytes."""
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


def _audit_bridge_download_served(principal_id: str, claimed: ClaimedDownload) -> None:
    """Recorded under the request_id of the gated decision that released
    the file and the agent whose call staged it (ADR 0093) -- neither is on
    the fetch itself, which for a capability link carries no bearer."""
    try:
        with agent_scope(claimed.agent):
            get_audit_logger().record(AuditEntry(
                timestamp=_now_iso(), week=current_week(), request_id=claimed.request_id, connector="",
                tool="", tool_name="",
                summary=f"File bridge download served: {claimed.name!r} ({len(claimed.data)} bytes)",
                sender=principal_id, decision="bridge_download_served", auto_accept_rule="",
                latency_seconds=0.0, pii_detected=False,
            ))
    except Exception as exc:  # noqa: BLE001 -- audit logging must never break the download
        logger.warning("Audit log write failed for bridge download: %s", exc)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _put_slot(request: Request) -> Response:
    """The unauthenticated capability upload -- see module docstring. This route isn't wrapped in
    a bearer-auth middleware stack (``mount_capability_routes``), so there is no access token to
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
    claimed = get_download_staging_store().claim_staged(token, None)
    if claimed is None:
        return _NOT_FOUND
    data = claimed.data
    _audit_bridge_download_served("(capability)", claimed)
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


def mount_capability_routes() -> list[BaseRoute]:
    """The two capability routes as one ``Mount`` under ``FILE_BRIDGE_PREFIX`` -- no bearer-auth
    stack at all. Needed by ``privacyfence_create_upload_slot`` and
    ``DownloadDeliveryConfig.agent_links``'s staged-link delivery."""
    return [Mount(FILE_BRIDGE_PREFIX, app=_capability_asgi_app())]


__all__ = [
    "FILE_BRIDGE_PREFIX",
    "mount_capability_routes",
]
