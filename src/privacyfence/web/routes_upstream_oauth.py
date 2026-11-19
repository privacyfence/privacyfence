"""The Connect flow of hosted, OAuth-authenticated upstream MCP servers (ADR 0329).

``GET /upstream-mcp/<id>/connect`` discovers the upstream's authorization server, registers or looks up
the client, and sends the signed-in person to the provider with a single-use ``state`` and an S256 PKCE
challenge. ``GET /upstream-mcp/<id>/callback`` must be finished in the same signed-in session that started
it (ADR 0178); it checks the RFC 9207 ``iss`` parameter, exchanges the code and saves the token file.

The ``state``, the authorization ``code``, every token and every response body stay out of the logs: a
failure is logged as the upstream id and an error kind only. A person who is not connected is sent back to
the Connect page with ``?upstream_error=<id>``; nothing from the provider is echoed.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from html import escape as _esc

import httpx2
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import org_identity, upstream_mcp_runtime
from ..connector_registry import ConnectorRegistry
from ..principal import Principal
from ..upstream_oauth_discovery import discover
from ..upstream_oauth_tokens import (
    OAuthClient,
    UpstreamOAuthError,
    build_authorize_url,
    exchange_code,
    load_token,
    save_token,
    token_path,
)
from . import org_session
from .org_session import OrgSessionStore

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600
PENDING_MAX_ENTRIES = 1000

_NO_STORE = {"Cache-Control": "no-store"}
_PROVIDER_ERROR_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_PROVIDER_ERROR_MAX = 40
_CONNECTED_BADGE = '<span class="badge badge-success">Connected</span>'


@dataclass(frozen=True)
class PendingUpstreamAuth:
    principal_id: str
    upstream_id: str
    code_verifier: str
    redirect_uri: str
    issuer: str
    client: OAuthClient
    token_endpoint: str
    resource: str | None
    iss_required: bool


class PendingUpstreamAuthStore:
    """State -> the sign-in attempt it started: single use, good for ``ttl_seconds``, and bounded by
    ``max_entries`` (the oldest attempt is dropped first)."""

    def __init__(
        self, ttl_seconds: float = PENDING_TTL_SECONDS, max_entries: int = PENDING_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[float, PendingUpstreamAuth]] = {}

    def put(self, state: str, pending: PendingUpstreamAuth) -> None:
        with self._lock:
            self._pending.pop(state, None)
            self._pending[state] = (self._clock(), pending)
            while len(self._pending) > self._max:
                del self._pending[next(iter(self._pending))]

    def pop(self, state: str) -> PendingUpstreamAuth | None:
        with self._lock:
            found = self._pending.pop(state, None)
        if found is None:
            return None
        created_at, pending = found
        return None if self._clock() - created_at > self._ttl else pending


def _error_redirect(upstream_id: str) -> Response:
    return RedirectResponse(f"/connect?upstream_error={upstream_id}", status_code=302, headers=_NO_STORE)


def _safe_provider_error(value: str) -> str:
    return _PROVIDER_ERROR_CHARS.sub("", value)[:_PROVIDER_ERROR_MAX]


def build_routes(
    *, sessions: OrgSessionStore, connector_registry: ConnectorRegistry, issuer_url: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> list[Route]:
    pending_store = PendingUpstreamAuthStore()
    base_url = issuer_url.rstrip("/")

    async def connect(request: Request) -> Response:
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers=_NO_STORE)
        upstream_id = request.path_params["upstream_id"]
        upstream = upstream_mcp_runtime.oauth_upstream(upstream_id)
        if upstream is None:
            return PlainTextResponse("Not found", status_code=404)
        redirect_uri = f"{base_url}/upstream-mcp/{upstream_id}/callback"
        try:
            endpoints = await discover(upstream, transport=transport)
            client = await upstream_mcp_runtime.registrations_for(upstream_id).ensure_client(
                upstream, endpoints, redirect_uri,
            )
        except UpstreamOAuthError as exc:
            logger.warning("Upstream %s connect failed: %s", upstream_id, exc.kind)
            return _error_redirect(upstream_id)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        state = secrets.token_urlsafe(32)
        pending_store.put(state, PendingUpstreamAuth(
            principal_id=principal.id, upstream_id=upstream_id, code_verifier=code_verifier,
            redirect_uri=redirect_uri, issuer=endpoints.issuer, client=client,
            token_endpoint=endpoints.token_endpoint, resource=endpoints.resource,
            iss_required=endpoints.iss_supported,
        ))
        authorize_url = build_authorize_url(
            endpoints.authorization_endpoint, client.client_id, redirect_uri, state, code_challenge,
            endpoints.scope, endpoints.resource,
        )
        return RedirectResponse(authorize_url, status_code=302, headers=_NO_STORE)

    async def callback(request: Request) -> Response:
        upstream_id = request.path_params["upstream_id"]
        state = request.query_params.get("state", "")
        pending = pending_store.pop(state) if state else None
        if pending is None or pending.upstream_id != upstream_id:
            return PlainTextResponse("Invalid or expired sign-in attempt", status_code=400)
        # Finished in the session that started it, so a consent link cannot be handed to a colleague (ADR 0178).
        principal = org_session.authenticated(request, sessions)
        if principal is None or principal.id != pending.principal_id:
            logger.info("Upstream %s callback ignored: not finished by the person who started it", upstream_id)
            return _error_redirect(upstream_id)
        provider_error = request.query_params.get("error")
        if provider_error:
            logger.info("Upstream %s sign-in declined: %s", upstream_id, _safe_provider_error(provider_error))
            return _error_redirect(upstream_id)
        code = request.query_params.get("code", "")
        if not code:
            logger.info("Upstream %s callback had no code", upstream_id)
            return _error_redirect(upstream_id)
        iss = request.query_params.get("iss")
        if (iss is not None and iss != pending.issuer) or (iss is None and pending.iss_required):
            logger.warning("Upstream %s callback refused: issuer check failed", upstream_id)
            return _error_redirect(upstream_id)
        try:
            record = await exchange_code(
                token_endpoint=pending.token_endpoint, client=pending.client, code=code,
                redirect_uri=pending.redirect_uri, code_verifier=pending.code_verifier,
                resource=pending.resource, upstream_id=upstream_id, issuer=pending.issuer, transport=transport,
            )
        except UpstreamOAuthError as exc:
            logger.warning("Upstream %s code exchange failed: %s", upstream_id, exc.kind)
            return _error_redirect(upstream_id)
        save_token(token_path(principal, upstream_id), record)
        connector_registry.evict(principal.id)
        return RedirectResponse(f"/connect?upstream_connected={upstream_id}", status_code=302, headers=_NO_STORE)

    return [
        Route("/upstream-mcp/{upstream_id}/connect", connect, methods=["GET"]),
        Route("/upstream-mcp/{upstream_id}/callback", callback, methods=["GET"]),
    ]


def _label_of(upstream_id: str) -> str | None:
    upstream = upstream_mcp_runtime.oauth_upstream(upstream_id)
    return upstream.label if upstream is not None else None


def connect_section_html(principal: Principal, flash_connected_id: str, flash_error_id: str) -> tuple[str, str]:
    """``(flash HTML, row HTML)`` for the Connect page; both empty when no upstream is configured."""
    flash = ""
    connected_label = _label_of(flash_connected_id) if flash_connected_id else None
    if connected_label is not None:
        flash += f'<div class="card card-success" role="status">{_esc(connected_label)} connected.</div>'
    error_label = _label_of(flash_error_id) if flash_error_id else None
    if error_label is not None:
        flash += (
            f'<div class="card card-danger" role="alert">Could not connect {_esc(error_label)} -- '
            "sign-in was declined, or the server could not be reached.</div>"
        )
    rows = []
    for upstream_id in sorted(upstream_mcp_runtime.upstream_ids()):
        label = _label_of(upstream_id) or upstream_id
        if load_token(token_path(principal, upstream_id)) is not None:
            badge = _CONNECTED_BADGE
            action_text, action_class = "Reconnect", "secondary"
        else:
            badge = '<span class="badge">Not connected</span>'
            action_text, action_class = "Connect", "primary"
        rows.append(
            f'<li class="service card cluster"><span class="pf-service-head cluster">'
            f'<span class="pf-service-chip">{_esc(label)}</span>{badge}</span>'
            f'<a class="button {action_class}" href="/upstream-mcp/{_esc(upstream_id)}/connect">{action_text}</a></li>'
        )
    return flash, "".join(rows)


__all__ = [
    "PendingUpstreamAuth",
    "PendingUpstreamAuthStore",
    "build_routes",
    "connect_section_html",
]
