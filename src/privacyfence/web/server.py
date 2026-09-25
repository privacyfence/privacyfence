"""Embedded HTTP(S) server lifecycle: bind policy, security headers, and
starting/stopping the ASGI app (uvicorn) on its own thread -- see daemon_
main.py's own module docstring's "Threading model" section (its "Web
thread") for how this fits alongside the daemon's other threads.

**Local mode**: loopback HTTP (ADR 0010), bound to ``localhost`` (not a
bare ``127.0.0.1``/``0.0.0.0``) so ``http://localhost`` stays a secure
context for whatever this surface needs (WebAuthn step-up) without anyone
having to move the bind address. Auth is deliberately the simplest thing
that's still a real control, not sessions/OIDC -- but it is not
"possession of one never-expiring, URL-carried token is the authority":
this surface is reachable from a browser rather than only a local process,
and needs more. What a browser actually presents is a short-lived,
single-use bootstrap code (``?bootstrap=<code>``, see
``_BootstrapMiddleware``), exchanged exactly once for an independent,
random session id (web/session_auth.py's ``LocalSessionStore``) carrying
its own idle and absolute expiry -- web/routes_approvals.py's own docstring
covers the CSRF double-submit that session id also backs. Minting a fresh
code on demand -- once a previous one has expired, without restarting the
daemon -- does not go through this loopback port at all:
``web/control_channel.py``'s ``ControlChannelServer`` is a Unix domain
socket (macOS/Linux) or ACL'd named pipe (Windows) a browser's own loopback
connection cannot speak, rather than a persistent secret presented over
HTTP (ADR 0002 decision 2). See that module's own docstring for the full
reasoning.

**Org mode**: a
configurable bind host/port, optional TLS termination, optional
``X-Forwarded-*`` trust for a small explicit set of reverse-proxy
addresses (never by default), and ``/mcp`` authenticated by
``web/oauth_provider.py``'s real OAuth 2.1 authorization server (ADR 0011)
instead of one shared secret. Passed to ``build_app``/``WebServer`` as one
``OrgAuth`` bundle (see that class) rather than four separate parameters,
so a caller either opts into the whole org-mode picture or none of it.

**`/approvals` in org mode** (web/routes_approvals.py's ``build_routes()``)
is *not* ``routes_approvals.create_app``'s local-mode surface -- that one
authenticates with one shared secret and lists *every* pending approval
with no principal filtering, so exposing it as-is under org mode, where
many principals share one daemon, would leak every principal's pending
approvals to whoever holds any valid token. ``/approvals``/``/security``
here are a separate, principal-aware route set (ADR 0033): every read and
write is authorized against ``current_principal()`` via ``org_session``,
and a *write* decision additionally demands a fresh WebAuthn step-up when
``org_config.json``'s ``step_up.enabled`` is set --
web/routes_approvals.py's own module docstring covers both.

**`/settings` in org mode** is, likewise, *not*
``routes_settings.py``'s ~30-action local-mode dispatcher opened up
wholesale -- ``routes_settings.build_org_routes()`` mounts a
capability-filtered subset instead,
restricted to ``org_settings_scope.ACTION_SCOPES``'s own ``ORG_MODE``
members: ``GET /settings``, every signed-in principal's own auto-accept
rules, read-only except for adding or removing one; and ``GET
/settings/privacy``, an admin-only (``Principal.is_admin``) view of the
install-wide PII/privacy policy, editable through web/org_install_policy.py.
That subset renders through the exact same settings_window_html.build_html()
local mode's own settings page does (capability-filtered per
mode/``is_admin``) and dispatches every write through the same generic
``POST /api/settings/{action}`` local mode's own dispatcher answers (ADR
0032), restricted to
``routes_settings._ORG_ALLOWED_ACTIONS``. The rest of routes_settings.py's
~30 actions (connector management, the update banner, Telegram's
interactive auth -- see web/org_settings_scope.py's own ``ACTION_SCOPES``
for the ones that only ever meant something on a desktop install) remain
unmounted, as do the two admin-only actions that are install-wide but
aren't privacy policy (``set_log_level``, ``toggle_calendar_free_busy``,
``toggle_gmail_signature``) --
``ACTION_SCOPES`` is the one place that split is declared now, consulted
by both ``build_routes``/``build_org_routes`` rather than filtered
separately by each.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .. import __version__, paths, privilege_separation, web_shell, webauthn_stepup
from ..agent_overrides import AgentOverrides
from ..connector_registry import ConnectorRegistry
from ..org_identity import IdpConfig
from ..principal import ANONYMOUS_PRINCIPAL, LOCAL_PRINCIPAL, Principal, current_principal, principal_scope
from ..settings_controller import SettingsController, set_main_dispatcher
from ..step_up_config import StepUpConfig
from ..web_approval_ui import WebApprovalUI
from . import org_session
from . import routes_connect
from . import routes_downloads
from .routes_file_bridge import mount_capability_routes, mount_file_bridge
from . import routes_org_identity
from . import state_stream as _state_stream
from .control_channel import (
    WEB_BASE_URL_FILE_NAME,
    ControlChannelServer,
    request_enrollment_confirmation,
    request_recovery_confirmation,
    send_recovery_code,
)
from .csp import build_csp
from .csp import new_nonce as _new_csp_nonce
from . import mcp_auth
from .mcp_auth import PerUserTokenVerifier, load_or_create_mcp_token
from .mcp_dispatch import McpDispatcher
from .oauth_provider import OrgOAuthProvider
from .org_session import OrgSessionStore
from .routes_approvals import create_app as create_approvals_app
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp, mount_org_oauth, protected_resource_metadata_url
from .routes_settings import build_routes as build_settings_routes
from .session_auth import BOOTSTRAP_QUERY_PARAM, BootstrapStore, LocalSessionStore
from .session_auth import SESSION_COOKIE as _SESSION_COOKIE
from .session_auth import authenticated as _session_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import is_human_session as _is_human_session
from .session_auth import set_session_cookie as _set_session_cookie
from .session_auth import unauthorized_html as _unauthorized_response
from .state_stream import StateStream

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
MCP_URL_FILE_NAME = "mcp_url"

# Content-Security-Policy: see web/csp.py's own module docstring for the
# full policy and the reasoning behind each directive -- script-src and
# style-src take a nonce rather than a blanket 'unsafe-inline' grant. Built
# per-response, from that request's own nonce (_SecurityHeadersMiddleware
# below), not a fixed module-level constant.

# Every
# browser feature this app never uses, denied outright -- the same "narrow,
# explicit exceptions for exactly what these pages actually use" posture
# web/csp.py's build_csp() already takes. ``publickey-credentials-get``/
# ``-create`` are the one exception left at their browser default (``self``)
# rather than denied: web/routes_security.py's step-up flow calls
# ``navigator.credentials.get``/``.create`` from this same origin, and
# Permissions-Policy's own default allowlist for both directives is
# already ``self`` -- naming them explicitly here just documents that on
# purpose instead of leaving a future reader to wonder whether they were
# overlooked.
_PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), encrypted-media=(), "
    "fullscreen=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "midi=(), payment=(), picture-in-picture=(), publickey-credentials-create=(self), "
    "publickey-credentials-get=(self), screen-wake-lock=(), usb=(), xr-spatial-tracking=()"
)

# One year, subdomains included -- the usual conservative starting
# point (see e.g. OWASP's HSTS cheat sheet) short of the two-year "preload"
# submission length, which this app has no business requesting: preload is
# a permanent, browser-vendor-controlled commitment keyed on the exact
# public hostname, wrong for a value that varies per self-hosted org
# deployment (``org.issuer_url``'s host) rather than being one fixed domain
# this project itself controls. Only ever added in org mode -- see
# _SecurityHeadersMiddleware's own docstring for why local mode never sends
# this header at all.
_HSTS = "max-age=31536000; includeSubDomains"


def confirm_first_passkey_enrollment() -> tuple[bool, str]:
    """Local mode's ``confirm_first_enrollment`` -- what web/routes_security.py
    calls when an enrollment has no already-enrolled credential to be gated on
    asserting with. See that module's own docstring for why a first enrollment needs a
    gate of its own at all, and web/control_channel.py's for why the
    companion is the thing that can answer: it runs in the human's login
    session, and on a separated install nothing but the daemon can reach the
    channel it answers on.

    The one bypass is the escape hatch ADR 0003 decision 7 already gives the
    developer path -- ``PRIVACYFENCE_DEV_ALLOW_UNSEPARATED`` on a
    *non-packaged* build, the same pairing ``step_up_config.py``'s
    ``from_local_config()`` uses for ``require_passkey`` (and the same
    reasoning: a checkout somebody is working on has no companion autostarted
    for it, while a packaged install always does -- ADR 0003 decisions 3-5 --
    so honoring it there would hand a real shipped product a way around its
    own gate). ``paths.is_bundled()`` is checked first for exactly that
    reason: the variable is not consulted at all on a packaged build.
    """
    if not paths.is_bundled() and privilege_separation.dev_allows_unseparated():
        logger.warning(
            "%s is set on this non-packaged install: enrolling a first passkey without asking "
            "the companion to confirm it.", privilege_separation.DEV_ALLOW_UNSEPARATED_ENV,
        )
        return True, ""
    confirmed, reason = request_enrollment_confirmation()
    if not confirmed and not paths.is_bundled():
        # A source checkout or pip install autostarts no companion (ADR 0003
        # decision 7 -- decisions 3-5's autostart wiring is the packaged
        # installers' half), so "start the companion" is not the whole answer
        # here and the escape hatch is. Only added on a non-packaged build,
        # where it is the honest next step; a packaged install must never be
        # told about a variable it does not honor.
        reason = (
            f"{reason} On a source checkout you can also run "
            f"`privacyfence-companion --serve`, or set "
            f"{privilege_separation.DEV_ALLOW_UNSEPARATED_ENV}=1 for local development."
        )
    return confirmed, reason


def present_recovery_code(code: str) -> tuple[bool, str]:
    """Local mode's ``deliver_recovery_code`` (web/routes_security.py):
    hand a freshly minted one-time recovery code to the companion, which
    puts it in front of whoever is at this machine's own login session
    (ADR 0003's 2026-09-19 Out-of-scope amendment).

    Wired only on a *packaged* build -- ``build_app`` below decides that --
    for the same reason ``confirm_first_passkey_enrollment`` above honors a
    dev escape hatch: ADR 0003 decisions 3-5 guarantee a companion on every
    shipped install and nothing guarantees one for a source checkout, so a
    checkout keeps the pre-1.3 behavior (the code comes back in the
    response, and ``_PAGE_JS`` shows it) rather than being unable to issue
    one at all.

    Returns ``(shown, reason)`` straight through: the caller stores the
    code only on a ``True``, so a companion that could not be reached costs
    an enrollment its recovery code rather than leaving one on file that
    nobody has.
    """
    return send_recovery_code(code)


def reissue_local_recovery_code() -> tuple[bool, str]:
    """The companion's ``RECOVERY`` command, from the daemon's side: confirm
    with the human, mint, show, and only then store (the
    "re-presented by the companion" half).

    This is the *only* way a second recovery code is ever produced, and it
    deliberately produces a new one rather than reproducing the old: nothing
    keeps the plaintext, by design -- ``webauthn_stepup`` stores a salted
    hash and nothing else. The human gets a working code; whatever they
    wrote down before stops working, which is what the confirmation dialog
    says in as many words.

    The confirmation is what makes this safe to expose on a channel the
    agent shares (web/control_channel.py's own ``0660`` paragraph): the
    reply carries no code, so triggering this learns an attacker nothing,
    and the dialog means it cannot even invalidate the human's saved code
    without somebody at the keyboard agreeing to it. See ADR 0003's
    2026-09-19 Out-of-scope amendment.
    """
    confirmed, reason = request_recovery_confirmation()
    if not confirmed:
        return False, reason or "the companion did not confirm a new recovery code"
    code = webauthn_stepup.mint_recovery_code()
    shown, reason = send_recovery_code(code)
    if not shown:
        return False, reason or "the companion could not show the recovery code"
    webauthn_stepup.store_recovery_code(LOCAL_PRINCIPAL, code)
    return True, ""


def local_enrollment_state(step_up: StepUpConfig | None) -> str:
    """The companion's ``ENROLLMENT`` command, from the daemon's side:
    ``"pending"`` when this install requires a passkey and has none
    enrolled, ``"ok"`` otherwise.

    "Requires" is ``enabled and require_passkey`` together, the same
    pairing every other consumer of this config treats as in force (see
    ``webauthn_stepup.observe_step_up_requirement``) -- an install with
    step-up off is not waiting for an enrollment, it is simply not using
    one, and the companion must not nag about it.

    Says nothing else on purpose. The credential store itself lives under
    ``authority_dir()`` and is unreadable to the logged-in user on a
    separated install, which is why the companion has to ask at all; the
    answer it gets back is one of two words, both of which anybody looking
    at the banner on ``/security`` could already read off the screen.
    """
    if step_up is None or not (step_up.enabled and step_up.require_passkey):
        return "ok"
    return "ok" if webauthn_stepup.has_credentials(LOCAL_PRINCIPAL) else "pending"


def local_status_payload(started_at: str) -> str:
    """The companion's ``STATUS`` command, from the daemon's side: a
    compact JSON object -- ``daemon_status.probe()``'s "the control
    channel answered" case, and what the companion's tray menu and
    ``Service Details...`` dialog are actually built from.

    Read-only and free of anything a local process couldn't already infer
    (``web/control_channel.py``'s own ``STATUS`` handler answers it to
    anyone who can reach this socket at all, gated on nothing): a version
    string, this process's own pid, when it started, which mode it's
    running in, whether it's privilege-separated, and -- per connector --
    only whether that connector has a stored grant (``"ok"``) or not
    (``"needs_auth"``), never the grant itself. ``routes_connect.py``'s
    ``_is_connected()``/``SERVICE_LABELS`` are reused rather than
    reimplemented so this can never disagree with what ``/connect`` already
    shows the same human.
    """
    connectors = {
        service: ("ok" if routes_connect._is_connected(LOCAL_PRINCIPAL, service) else "needs_auth")
        for service in routes_connect.SERVICE_LABELS
    }
    payload = {
        "version": __version__,
        "pid": os.getpid(),
        "started_at": started_at,
        "mode": "local",
        "separated": privilege_separation.is_enabled(),
        "connectors": connectors,
    }
    return json.dumps(payload, separators=(",", ":"))


def _write_mcp_url_file(url: str) -> None:
    """The discovery file a client that talks to /mcp reads to find it --
    see mcpb/shim/src/protocol.ts's module docstring, which reads this same
    file: WebServer.start() writes it when it binds, and clears it on
    shutdown. 0600 for the
    same reason web_token/mcp_token are: not a secret itself, but written
    alongside them under the same directory.

    ``handoff_dir()`` rather than ``data_dir()``: every
    discovery file this module writes exists to be read by something in the
    user's *own* session (the MCPB shim, the companion, a human following the
    not-authorized page), so all of them stay on the user-reachable side of a
    privilege-separated install. On every other install the two functions
    return the same directory and nothing moves."""
    path = paths.handoff_dir() / MCP_URL_FILE_NAME
    privilege_separation.write_handoff_file(path, url)


def _clear_mcp_url_file() -> None:
    """Called on WebServer.stop() so a shim launched after this daemon exits
    finds no file rather than a stale, now-dead URL -- the same reasoning
    ipc_server.py's own shutdown has for not leaving a dangling PORT_FILE
    behind."""
    (paths.handoff_dir() / MCP_URL_FILE_NAME).unlink(missing_ok=True)


def _write_web_base_url_file(base_url: str) -> None:
    """The companion's own way to learn this install's base
    URL (see ``web/control_channel.py``'s ``read_base_url()``) without
    importing this module -- written unconditionally alongside ``mcp_url``
    whenever this server runs local mode's own control channel (``self.
    control_channel is not None`` -- org mode has neither)."""
    path = paths.handoff_dir() / WEB_BASE_URL_FILE_NAME
    privilege_separation.write_handoff_file(path, base_url)


def _clear_web_base_url_file() -> None:
    (paths.handoff_dir() / WEB_BASE_URL_FILE_NAME).unlink(missing_ok=True)


class _SecurityHeadersMiddleware:
    """Plain ASGI middleware (not starlette.middleware.base.
    BaseHTTPMiddleware, which buffers the whole response) adding the fixed
    header set every response from this app needs -- see, for
    Permissions-Policy and HSTS, the module-level
    ``_PERMISSIONS_POLICY``/``_HSTS`` constants' own comments.
    Cache-Control: no-store is also set per-route (web/routes_approvals.py
    and friends) for the routes that actually carry sensitive content,
    since a static blanket no-store here would be redundant with, not a
    replacement for, being deliberate about it at the route that matters --
    see test_server.py's ``TestCacheControlOnSensitivePages`` for the sweep
    that checks every one of those routes actually does.

    ``hsts`` gates ``Strict-Transport-Security`` alone: local mode (``org``
    is ``None`` in build_app()) binds to plain ``http://localhost`` by
    design (this module's own docstring), where the header is at best
    inert and at worst actively wrong -- a browser that honored it would
    start refusing plain-HTTP connections to ``localhost`` for every *other*
    app on the machine too, since HSTS is scoped by hostname, not by port
    or origin. Org mode is the one caller that passes ``hsts=True``: its
    ``issuer_url`` is a real, single-purpose public hostname reached over
    HTTPS (TLS terminated here or, just as often, at a reverse proxy in
    front of this daemon -- either way the browser's own connection is
    HTTPS, which is what HSTS actually governs), so pinning that hostname
    to HTTPS-only is exactly the win intended, with none of local
    mode's collateral risk.

    **CSP nonce.** A fresh nonce is minted for every
    HTTP request and placed on ``scope["state"]`` (readable downstream as
    ``request.state.csp_nonce``, see web/csp.py's ``nonce_for``) *before*
    the wrapped app runs -- every route that builds its document fresh
    per-request (``/approvals``, ``/settings``, the org-mode browser pages)
    just uses that value as-is. A route serving a document that was
    rendered once, well before this response existed (an approval card --
    see approval_window_html.py's own module docstring), overrides it via
    ``web/csp.py``'s ``set_nonce`` to whatever nonce is already baked into
    that specific document. Either way, the actual header value below is
    read from ``scope["state"]`` at send time, *after* the app has already
    run and had a chance to override it -- not the value minted up front --
    so the header always matches whichever nonce actually ended up in the
    response body. Why a nonce and not ``'unsafe-inline'``: ADR 0063.

    **Replace, not extend.** Appending the fixed header set onto whatever
    the wrapped app already sent would silently emit *two* headers of the same name -- ambiguous at
    best, and for Content-Security-Policy specifically, most browsers
    intersect multiple CSP headers into their most-restrictive combination,
    which is not the same thing as "the value this middleware computed"
    and isn't something any caller here actually wants. Using
    ``MutableHeaders`` instead makes each of these headers authoritative --
    set exactly once, overriding rather than accumulating alongside
    anything a route handler already added under the same name.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self._app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state.setdefault("csp_nonce", _new_csp_nonce())

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                nonce = scope.get("state", {}).get("csp_nonce") or _new_csp_nonce()
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["x-frame-options"] = "DENY"
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["content-security-policy"] = build_csp(nonce)
                headers["permissions-policy"] = _PERMISSIONS_POLICY
                headers["cross-origin-opener-policy"] = "same-origin"
                if self._hsts:
                    headers["strict-transport-security"] = _HSTS
            await send(message)

        await self._app(scope, receive, send_with_headers)


@dataclass(frozen=True)
class OrgAuth:
    """Everything build_app()/WebServer need to run org mode -- one
    bundle instead of four separate parameters, so a caller either opts
    into the whole org-mode picture (a real OAuth 2.1 AS, real sessions,
    the IdP config both go through) or passes ``org=None`` and gets local
    mode's own unchanged behavior. See this module's own docstring for
    what org mode does and deliberately does not mount yet.
    """

    provider: OrgOAuthProvider
    sessions: OrgSessionStore
    idp: IdpConfig
    issuer_url: str
    # Per-user service
    # authorization (Google/Slack/Salesforce/Atlassian/Telegram) and the
    # /connect page that drives it. Both default to None/{} so every
    # existing caller of OrgAuth (this module's own tests included) keeps
    # constructing one without them -- _build_org_app below simply doesn't
    # mount web/routes_connect.py's routes when connector_registry is
    # absent, the same "additive, opt-in" posture every other new surface
    # in this codebase takes. daemon_main.py's real _start_org_web_server
    # always supplies both.
    connector_registry: ConnectorRegistry | None = None
    org_config: dict = field(default_factory=dict)
    # The server's own install-wide settings.yaml (run_app()'s
    # ``config``) -- routes_settings.build_org_routes()'s admin-only
    # privacy-policy view reads this directly, and
    # daemon_main._start_org_web_server threads the
    # same dict into every org principal's own privacy-filter registration
    # (see _load_principal_settings()'s docstring). Defaults to {} for the
    # same "every existing OrgAuth() caller keeps working" reason
    # connector_registry/org_config already do.
    install_wide_settings: dict = field(default_factory=dict)
    # Where that dict was loaded from, so the admin privacy page
    # can write it back. Separate from the dict rather than derived from it
    # because nothing in a parsed settings.yaml records its own path.
    # Empty means "read-only": routes_settings.build_org_routes() renders
    # the policy without edit controls and rejects a hand-written write, which is
    # exactly what an OrgAuth built by a test that never had a real
    # settings.yaml on disk should do.
    install_wide_settings_path: str = ""


def _local_principal_resolver(sessions: LocalSessionStore) -> Callable[[Request], Principal]:
    """Local mode's own resolver (ADR 0008): a valid ``pf_session`` cookie
    resolves to whichever principal minted it (``LocalSessionStore.
    principal_id`` -- set at mint time from the control channel's own peer
    credentials, see ``web/control_channel.py``'s ``MINT``/``MINT
    COMPANION``/``MINT CONSOLE``); anything else -- no cookie, an unknown or
    expired one, or a session that predates this field -- resolves to
    ``LOCAL_PRINCIPAL``, the same thing every local-mode session resolved to
    before this ADR. Unlike ``_org_principal_resolver`` below, an
    unauthenticated request never resolves to ``ANONYMOUS_PRINCIPAL`` here:
    local mode's own authentication check (``_session_authenticated``) is a
    separate, later gate every local route already runs on its own, and
    local mode has never needed a distinct "not yet authenticated" identity
    the way a multi-tenant org deployment does (see ``ANONYMOUS_PRINCIPAL``'s
    own docstring)."""

    def resolve(request: Request) -> Principal:
        session_id = request.cookies.get(_SESSION_COOKIE, "")
        if not session_id:
            return LOCAL_PRINCIPAL
        principal_id = sessions.principal_id(session_id)
        if principal_id is None or principal_id == LOCAL_PRINCIPAL.id:
            return LOCAL_PRINCIPAL
        return Principal(id=principal_id)

    return resolve


def _org_principal_resolver(sessions: OrgSessionStore) -> Callable[[Request], Principal]:
    """Org mode's default resolver: a valid ``pf_org_session`` cookie
    (web/org_session.py) resolves to the human who signed in via
    ``/login``; anything else -- no cookie, an unknown or expired one --
    resolves to ``ANONYMOUS_PRINCIPAL``, never ``LOCAL_PRINCIPAL`` (see
    that constant's own docstring for why conflating "not authenticated"
    with "the local single-user principal" would be actively misleading in
    a multi-user deployment). The route itself is what actually rejects an
    unauthenticated request; this only decides what current_principal()
    resolves to in the brief window before that check runs.
    """

    def resolve(request: Request) -> Principal:
        return org_session.authenticated(request, sessions) or ANONYMOUS_PRINCIPAL

    return resolve


class _PrincipalScopeMiddleware:
    """The browser surface's principal_scope() entry point -- entered
    once per HTTP request, in exactly one place per surface; the MCP
    endpoint's own entry point is routes_mcp.py's handle_call_tool. Every
    per-principal registry
    downstream (auto_accept.py, audit_log.py, pii_detector.py,
    privacy_filter.py, resource_names.py) resolves against whatever
    ``resolve`` returns for the rest of the request.

    ``resolve`` defaults to _local_principal_resolver(sessions) in local
    mode (ADR 0008: LOCAL_PRINCIPAL, or whichever principal minted the
    request's own session), or _org_principal_resolver in org mode --
    parameterized rather than hardcoded so a test can inject a resolver
    that varies by request to prove two principals stay isolated all the
    way through the real HTTP routes, not just via principal_scope()
    called directly.
    """

    def __init__(self, app: ASGIApp, resolve: Callable[[Request], Principal]) -> None:
        self._app = app
        self._resolve = resolve

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        principal = self._resolve(Request(scope))
        with principal_scope(principal):
            await self._app(scope, receive, send)


def _owner_only_endpoint(endpoint: Callable) -> Callable:  # noqa: ANN401 -- Starlette's own endpoint signature isn't typed
    """Wraps a Starlette endpoint so any principal other than
    ``LOCAL_PRINCIPAL`` gets a 404 instead of reaching it -- ADR 0008's
    "What this phase deliberately does not do": ``/settings`` administers
    ``SettingsController``'s one connector set and one config file, both
    the install owner's, and turning that into a genuinely per-principal
    surface is a follow-up this phase does not attempt. A non-owner
    principal (an ``os-<uid>``/``os-<sid>`` OS user, ADR 0008) gets the same
    404 -- not 403 -- every other cross-principal lookup in this codebase
    already returns, so the settings page's existence is not itself a
    signal to a principal it does not belong to."""

    @functools.wraps(endpoint)
    async def wrapped(request: Request) -> Response:
        if current_principal().id != LOCAL_PRINCIPAL.id:
            return PlainTextResponse("Not Found", status_code=404)
        return await endpoint(request)

    return wrapped


def _owner_only_routes(routes: list) -> list:  # noqa: ANN401 -- list[BaseRoute], typed loosely to avoid importing BaseRoute just for this
    """Rebuilds each ``Route`` in ``routes`` (``build_settings_routes()``'s
    own return value -- see that function's own docstring: always plain
    ``Route`` objects, never a ``Mount``/``WebSocketRoute``) with its
    endpoint wrapped by ``_owner_only_endpoint``. A list comprehension
    rather than mutating in place: ``Route.endpoint`` has no public setter,
    and rebuilding is what ``build_settings_routes()``'s own
    ``_BESPOKE_SENSITIVE_ROUTE_PATHS``/assertion pass already assumes is
    safe to do to its output (``routes_settings.py``'s own docstring on that
    loop)."""
    rebuilt = []
    for route in routes:
        assert isinstance(route, Route), (  # nosec B101 -- build_settings_routes() only ever returns plain Route objects
            f"expected a plain Route from build_settings_routes(), got {type(route)!r}"
        )
        rebuilt.append(Route(
            route.path, _owner_only_endpoint(route.endpoint),
            methods=sorted(route.methods) if route.methods else None, name=route.name,
        ))
    return rebuilt


def _parse_host_header(raw: str) -> str | None:
    """Standards-aware ``Host`` header -> bare hostname. A manual
    ``split(":", 1)[0]`` would assume the first colon always separates host
    from port, which is only true for a bare name or IPv4 address -- an
    IPv6 literal has colons *in* the host itself (``[::1]:8443``, or even a
    port-less ``[::1]``), so that split would return the literal ``"["``
    and every IPv6 request would either be rejected outright, or wrongly
    let through by whatever else happened to compare equal to ``"["``.

    Delegating to ``urlsplit`` on a synthesized ``//<raw>`` authority
    gets RFC 3986's bracket-aware host/port grammar for free -- its
    ``.hostname`` is already lowercased and has the brackets stripped, so
    ``"[::1]"`` and ``"[::1]:8443"`` both normalize to ``"::1"``, matching
    the plain ``urlsplit(...).hostname`` used for the org issuer host
    below.

    Returns ``None`` -- treat exactly like a disallowed host -- for
    anything that isn't a clean ``host[:port]`` authority: an unparsable
    port, userinfo (``user@host``, never valid in a Host header), or a
    stray path/query/fragment component smuggled in behind the host.
    """
    if not raw:
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        parsed.port  # noqa: B018 -- validated lazily; unparsable ports raise here, not on urlsplit()
    except ValueError:
        return None
    if parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
        return None
    return parsed.hostname


class _HostAllowlistMiddleware:
    """DNS-rebinding defense: reject any request whose Host header isn't in the configured
    allowlist, *before* it reaches any route -- a page served from a
    malicious domain that gets a victim's browser to send a request to
    ``http://localhost:PORT`` with a forged Host header is exactly what
    this stops from resolving as a "same server" request routing-wise.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self._app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        host = _parse_host_header(request.headers.get("host") or "")
        if host is None or host not in self._allowed_hosts:
            response = PlainTextResponse("Invalid Host header", status_code=400)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _BootstrapMiddleware:
    """The
    single place a ``?bootstrap=<code>`` query string is ever honored,
    ahead of every route in the local-mode app. A live, unexpired code is
    consumed (so it can never be replayed -- successful exchange or not)
    and exchanged for a brand-new, independent session id, set as the
    ``pf_session`` cookie; either way the response is a redirect to the
    *same path* with no query string at all, so neither the code nor the
    session id it minted ever appears in a URL, browser history, or a
    Referer header. A request that carries no ``bootstrap``
    param passes straight through untouched -- this only ever intercepts
    the one-time exchange, never ordinary authenticated traffic, which
    keeps proving itself out via the ``pf_session`` cookie.
    """

    def __init__(self, app: ASGIApp, *, bootstrap: BootstrapStore, sessions: LocalSessionStore) -> None:
        self._app = app
        self._bootstrap = bootstrap
        self._sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        code = request.query_params.get(BOOTSTRAP_QUERY_PARAM, "")
        if not code:
            await self._app(scope, receive, send)
            return
        response = RedirectResponse(request.url.path, status_code=303)
        # The code carries its own provenance (web/session_auth.py's
        # ``PROVENANCE_*``) and, since ADR 0008, the principal that minted it
        # (which OS user's control-channel connection asked), and the session
        # inherits both unchanged: the mint is the only moment anything knew
        # how this credential came to exist and for whom, and a middleware
        # reading a query string is in no position to improve on that.
        consumed = self._bootstrap.consume(code)
        if consumed is not None:
            provenance, principal_id = consumed
            _set_session_cookie(response, self._sessions.create(provenance=provenance, principal_id=principal_id))
        await response(scope, receive, send)


def _state_stream_route(stream: StateStream, *, sessions: LocalSessionStore) -> Route:
    """``GET /api/state/stream`` -- the one push channel the settings page
    and the approval list share; see web/state_stream.py's own module
    docstring for what it carries. Built here (not in state_stream.py itself) purely because
    every other route factory in this module already lives beside
    _HostAllowlistMiddleware/build_app -- state_stream.py stays focused on
    the stream's own state and SSE-formatting logic.

    A tab holding this connection open for the whole idle timeout must not
    be evicted, which it would be if the session were touched only once,
    at connect time, and never again inside ``stream.subscribe``. The
    session id is read here, once, so the same ``sessions.touch`` this
    handler already runs for the initial auth check can be handed to
    ``subscribe`` as its per-tick ``touch`` callback -- an open connection
    is itself proof of activity, so the idle timer now resets on every
    poll tick instead of only on reconnect."""

    async def handler(request: Request) -> Response:
        session_id = request.cookies.get(_SESSION_COOKIE, "")
        if not session_id or not sessions.touch(session_id):
            return _unauthorized_response(request)
        return StreamingResponse(
            stream.subscribe(request.is_disconnected, touch=lambda: sessions.touch(session_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    return Route("/api/state/stream", handler)


@contextlib.asynccontextmanager
async def _combined_lifespan(managers: list) -> AsyncIterator[None]:
    """Compose however many of {mcp_lifespan, the state-stream loop-capture
    below} this build actually needs into the one ``lifespan`` Starlette
    accepts -- build_app() constructs `managers` from whichever of
    mcp_dispatcher/state_stream were actually passed in, so a caller that
    passes neither (many tests in this repo) gets an empty list and this is
    a no-op context manager."""
    async with contextlib.AsyncExitStack() as stack:
        for cm in managers:
            await stack.enter_async_context(cm)
        yield


@contextlib.asynccontextmanager
async def _state_stream_loop_lifespan(ready_event: threading.Event | None = None) -> AsyncIterator[None]:
    """Captures this ASGI app's own running event loop into
    web/state_stream.py's module-level ``_loop`` for the app's whole
    lifetime -- settings_controller.call_on_main's fallback dispatcher
    needs it to marshal a background-thread callback onto this
    loop rather than running inline. Cleared on shutdown so a stale loop
    reference from a previous server instance (e.g. across daemon restarts
    in a single test process) is never mistaken for a live one.

    ``ready_event``, when given, is set right after the loop is captured --
    WebServer's own ``wait_until_ready`` is what a synchronous caller on
    another thread (daemon_main.py's run_app()) blocks on to learn this
    loop, the one every connector call actually runs on."""
    loop = asyncio.get_running_loop()
    _state_stream.set_loop(loop)
    if ready_event is not None:
        ready_event.set()
    try:
        yield
    finally:
        _state_stream.set_loop(None)


def build_app(
    web_ui: WebApprovalUI,
    *,
    sessions: LocalSessionStore | None = None,
    bootstrap: BootstrapStore | None = None,
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    mcp_dispatcher: McpDispatcher | None = None,
    mcp_token: str | None = None,
    mcp_verifier: PerUserTokenVerifier | None = None,
    controller: SettingsController | None = None,
    allow_quit: bool = True,
    state_stream: StateStream | None = None,
    notifications_enabled: bool = True,
    notifications_detail: str = "minimal",
    loop_ready: threading.Event | None = None,
    principal_resolver: Callable[[Request], Principal] | None = None,
    org: OrgAuth | None = None,
    step_up: StepUpConfig | None = None,
    step_up_issuer_url: str = "",
    agent_overrides: AgentOverrides | None = None,
) -> ASGIApp:
    """The approval routes, wrapped with the Host allowlist and security
    headers every real deployment needs -- routes_approvals.create_app()
    alone (no wrapping) is what tests reach for when they want to exercise
    the routes without also exercising this middleware stack.

    ``org`` switches this into org mode: ``sessions``/
    ``bootstrap``/``mcp_token``/``controller``/``state_stream`` are all
    ignored (org mode doesn't mount the local-session-authenticated
    approval/settings surface at all -- see this module's own docstring for
    why), ``/mcp`` is authenticated by ``org.provider`` instead of a shared
    secret, and the OAuth 2.1 authorization-server + browser-login routes
    are mounted alongside it. Local mode is ``org=None``, the default.

    ``principal_resolver`` defaults to _local_principal_resolver(sessions)
    (local mode, ADR 0008) or _org_principal_resolver (org mode) --
    pass an explicit one only to prove per-principal isolation over real
    HTTP in a test.

    ``mcp_dispatcher`` folds
    the ``/mcp`` Streamable HTTP endpoint into this same app. In local
    mode it's authenticated by its own ``mcp_token`` -- a secret
    independent of ``sessions`` (the approval surface's own session/CSRF
    store), which is what makes the audience separation ("the
    MCP access token must never be accepted on approval-decision endpoints,
    and the browser session cookie must never be accepted on /mcp") hold
    structurally rather than by convention (ADR 0061). In org mode the same
    separation holds because ``org.provider``'s tokens and
    ``org.sessions``' cookies are two entirely different stores with
    nothing that compares one against the other (see
    web/test_org_mcp_e2e.py's own audience-separation tests).

    ``controller`` folds ``/settings`` and its ``/api/settings/*``
    actions into the same app, on the *same* ``sessions`` store -- unlike
    MCP, the settings surface shares the approval surface's own session/
    CSRF cookie by design: /approvals and /settings are one application,
    with one header, one nav, one palette and one session. ``state_stream`` (built by WebServer when either
    ``controller`` or ``web_ui`` needs the push channel) backs
    ``GET /api/state/stream`` either way. Minting a fresh bootstrap code on
    demand is not an HTTP route this app exposes at all -- that is
    ``web/control_channel.py``'s ``ControlChannelServer``, which
    ``WebServer`` runs alongside this ASGI app rather than inside it.
    ``sessions``/``bootstrap`` default to a fresh store each when omitted,
    since every real (non-test) local-mode caller is ``WebServer``, which
    always constructs and shares one pair for its whole lifetime. Every
    optional surface's parameter defaults to ``None``, so a caller (a test,
    usually) that omits it simply does not get that surface.

    ``step_up``/``step_up_issuer_url`` mount ``/security`` in
    local mode -- ignored when ``org`` is given, since ``_build_org_app``
    resolves its own ``StepUpConfig`` from ``org.org_config`` directly (see
    that function's own step_up handling). ``step_up_issuer_url`` is the
    WebAuthn ceremony's expected origin (``http://<host>:<port>``, the same
    role ``org.issuer_url`` plays for org mode's own mount below) -- passed
    separately from ``step_up`` itself since local mode's origin depends on
    ``WebServer``'s own ``host``/``port``, which this function has no other
    way to see. The same two values also gate
    ``create_approvals_app``'s own decide endpoint on a fresh WebAuthn
    assertion, and ``build_settings_routes``'s own sensitive
    settings actions when ``step_up.require_passkey`` is on -- one
    ``StepUpConfig``, read once here, drives the enrollment surface, the
    decide-time check, and the settings-action check alike.
    """
    if org is not None:
        return _build_org_app(
            org, web_ui=web_ui, mcp_dispatcher=mcp_dispatcher, allowed_hosts=allowed_hosts,
            principal_resolver=principal_resolver,
        )

    sessions = sessions or LocalSessionStore()
    bootstrap = bootstrap or BootstrapStore()

    extra_routes: list[Route] = []
    lifespans = []
    if mcp_dispatcher is not None:
        # ADR 0008: real local-mode traffic always passes mcp_verifier (a
        # long-lived PerUserTokenVerifier MINT MCP registers new principals
        # into); mcp_token is kept for a caller with no multi-principal
        # registration to grow (most of this module's own tests) --
        # mount_mcp/mount_file_bridge's own token/verifier seam does the
        # rest, exactly as it already does for org mode's OrgOAuthProvider.
        if not mcp_token and mcp_verifier is None:
            raise ValueError("mcp_token or mcp_verifier is required when mcp_dispatcher is given")
        mcp_route, session_manager = mount_mcp(
            mcp_dispatcher, token=mcp_token, verifier=mcp_verifier, overrides=agent_overrides,
        )
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))
        # ADR 0007: the local file bridge's own upload/download endpoints,
        # authenticated exactly like /mcp (same bearer-token verifier) --
        # see routes_file_bridge.py's own module docstring for why this
        # can't just be more routes on the main approval-surface app. Also
        # includes the unauthenticated capability pair (ADR 0028)
        # (privacyfence_create_upload_slot and the no-bridge download
        # fallback hand out /mcp-files/slots|fetch URLs a client with no
        # shim -- and possibly no way to set a custom header at all -- can
        # still use) -- combined into this same Mount rather than a second
        # one at the same prefix, see mount_file_bridge's own docstring.
        extra_routes.extend(mount_file_bridge(token=mcp_token, verifier=mcp_verifier))

    # One answer, read once here, for
    # both gates below: an approving decision (web/routes_approvals.py) and
    # a sensitive settings action (web/routes_settings.py) require a session
    # this daemon can attribute to a person. See either module's own
    # ``require_human_session`` paragraph for why privilege separation is
    # the line: it is what ADR 0003 makes mandatory on every packaged
    # install, and what guarantees the companion that mints such a session
    # exists at all. See ADR 0062.
    require_human_session = privilege_separation.is_enabled()

    if controller is not None:
        extra_routes.extend(_owner_only_routes(build_settings_routes(
            controller, sessions=sessions, allow_quit=allow_quit, notifications_enabled=notifications_enabled,
            notifications_detail=notifications_detail, step_up=step_up, step_up_origin=step_up_issuer_url,
            require_human_session=require_human_session,
        )))

    # The /security enrollment surface: mounted whenever step_up.rp_id is
    # set -- which, unlike org mode, local mode's own
    # StepUpConfig.from_local_config() always gives it (DEFAULT_LOCAL_RP_ID),
    # so this is unconditional in practice for every real (non-test) caller.
    # The decide and settings step-up gates consult what it enrolls.
    if step_up is not None and step_up.rp_id:
        from . import routes_security

        _resolve_local_principal = _local_principal_resolver(sessions)
        extra_routes.extend(routes_security.build_routes(
            resolve_principal=lambda request: (
                _resolve_local_principal(request) if _session_authenticated(request, sessions) else None
            ),
            check_csrf=_csrf_matches,
            check_origin=_origin_ok,
            unauthenticated_response=_unauthorized_response,
            session_cookie_name=_SESSION_COOKIE,
            step_up=step_up, issuer_url=step_up_issuer_url,
            # Local mode has no /connect route (that's routes_connect.py's
            # org-mode-only surface) -- settings_window_html.py's Connectors
            # tab is this mode's own equivalent, see routes_security.py's
            # build_routes docstring on back_link.
            back_link=("/settings/connectors", "Back to Connectors"),
            # ADR 0003 decision 7's /security half -- daemon_main.py logs
            # the same fact at startup; see privilege_separation.
            # dev_unseparated_notice()'s own docstring.
            dev_unseparated_notice=privilege_separation.dev_unseparated_notice(),
            # The enrollment gate: local mode's own first-enrollment gate. Org mode's
            # call below passes nothing -- see routes_security.py's
            # build_routes docstring on why the two differ here.
            confirm_first_enrollment=confirm_first_passkey_enrollment,
            # The companion shows the one-time recovery code
            # instead of this response carrying it -- on a packaged build
            # only, which is the only kind of install ADR 0003 guarantees a
            # companion for. Everywhere else this stays None and the code
            # comes back in the body exactly as it always did. See
            # present_recovery_code() and routes_security.build_routes.
            deliver_recovery_code=present_recovery_code if paths.is_bundled() else None,
            # Trading in a recovery code wipes every enrolled passkey, so it
            # asks the same human-session question as an approving decision,
            # on the same installs -- see routes_security.build_routes'
            # docstring on is_human_session. Org mode's call below passes
            # nothing: an org session is itself an IdP sign-in.
            is_human_session=(
                (lambda request: _is_human_session(request, sessions)) if require_human_session else None
            ),
        ))

    if state_stream is not None:
        extra_routes.append(_state_stream_route(state_stream, sessions=sessions))
        lifespans.append(_state_stream_loop_lifespan(loop_ready))
        set_main_dispatcher(_state_stream.call_soon_threadsafe)

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app = create_approvals_app(
        web_ui, sessions=sessions, extra_routes=extra_routes, lifespan=lifespan,
        notifications_enabled=notifications_enabled, notifications_detail=notifications_detail,
        step_up=step_up, step_up_origin=step_up_issuer_url,
        # Only the settings controller knows whether anything is
        # authenticated. With no controller mounted, the approvals page
        # keeps its steady-state empty text -- see create_app's docstring.
        any_connector_authenticated=(
            controller.any_connector_authenticated if controller is not None else None
        ),
        require_human_session=require_human_session,
    )
    bootstrapped: ASGIApp = _BootstrapMiddleware(app, bootstrap=bootstrap, sessions=sessions)
    scoped: ASGIApp = _PrincipalScopeMiddleware(
        bootstrapped, principal_resolver or _local_principal_resolver(sessions),
    )
    wrapped: ASGIApp = _HostAllowlistMiddleware(scoped, allowed_hosts)
    return _SecurityHeadersMiddleware(wrapped)


def _build_org_app(
    org: OrgAuth, *, web_ui: WebApprovalUI, mcp_dispatcher: McpDispatcher | None, allowed_hosts: frozenset[str],
    principal_resolver: Callable[[Request], Principal] | None,
) -> ASGIApp:
    """org mode's own route set -- see build_app()'s and this module's own
    docstrings for what's deliberately absent (the local-token settings
    surface's ~30-action dispatcher, still -- only its own purpose-built
    replacement is mounted, see build_org_routes below).
    ``/approvals`` and ``/security``
    (web/routes_approvals.py/web/routes_security.py) are mounted
    unconditionally here -- unlike ``/connect`` (below), they need nothing
    from ``org.connector_registry``, only ``web_ui`` (already a required
    parameter of build_app() in both modes) and ``org.org_config`` for
    ``StepUpConfig``."""
    from urllib.parse import urlparse

    from ..org_mode import AuthzPolicyConfig
    from . import routes_approvals, routes_org_stepup, routes_security
    from .routes_settings import build_org_routes

    extra_routes: list[Route] = []
    lifespans = []
    if mcp_dispatcher is not None:
        mcp_route, session_manager = mount_mcp(
            mcp_dispatcher, verifier=org.provider,
            resource_metadata_url=protected_resource_metadata_url(org.issuer_url),
            client_names=org.provider.client_name, pinned_agents=org.provider.pinned_agent_id,
        )
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))
        # ADR 0028: org mode's own privacyfence_create_upload_slot and
        # DownloadDeliveryConfig.agent_links need the same two
        # unauthenticated capability routes local mode mounts above --
        # see routes_file_bridge.py's own module docstring. Gated on
        # mcp_dispatcher exactly like /mcp itself: with no dispatcher,
        # neither the upload-slot meta-tool nor a connector download tool
        # is reachable to mint a capability link in the first place.
        extra_routes.extend(mount_capability_routes())

    extra_routes.extend(mount_org_oauth(org.provider, issuer_url=org.issuer_url))
    # Mounted unconditionally here (every _build_org_app call is already
    # org mode) -- needs nothing from org.connector_registry, only
    # org.sessions, same reasoning /approvals'/security's own unconditional
    # mount below gives for needing only web_ui/org.org_config.
    extra_routes.extend(routes_downloads.build_routes(sessions=org.sessions))
    # Only mounted once a
    # real ConnectorRegistry exists to evict on a successful authorization
    # -- see OrgAuth's own docstring. daemon_main.py's real org-mode boot
    # path always supplies one; a hand-built OrgAuth in a test that only
    # cares about the OAuth-AS/session-login surface can omit it and get
    # that surface alone, with /connect and /oauth/start|callback left out
    # (see test_server_org_mode.py's TestConnectSurfaceOrgMode).
    default_next_path = routes_org_identity.DEFAULT_NEXT_PATH
    if org.connector_registry is not None:
        extra_routes.extend(routes_connect.build_routes(
            sessions=org.sessions, connector_registry=org.connector_registry,
            org_config=org.org_config, issuer_url=org.issuer_url,
        ))
        default_next_path = "/connect"
    extra_routes.extend(routes_org_identity.build_routes(
        idp=org.idp, sessions=org.sessions, base_url=org.issuer_url, default_next_path=default_next_path,
        # Derived from org.org_config directly here, the same
        # "self-contained, cheap re-parse" pattern StepUpConfig below
        # already uses -- see that call's own comment.
        policy=AuthzPolicyConfig.from_org_config(org.org_config),
    ))

    issuer_host = urlparse(org.issuer_url).hostname or ""
    step_up = StepUpConfig.from_org_config(org.org_config, default_rp_id=issuer_host)
    # One approval route module builds both modes' routes (ADR 0033) --
    # only the IdP step-up routes (no local-mode analogue at all) stay a
    # separate mount, see routes_org_stepup.py's own module docstring.
    extra_routes.extend(routes_approvals.build_routes(
        web_ui=web_ui, sessions=org.sessions, step_up=step_up, issuer_url=org.issuer_url,
    ))
    extra_routes.extend(routes_org_stepup.build_routes(
        web_ui=web_ui, sessions=org.sessions, step_up=step_up, idp=org.idp, issuer_url=org.issuer_url,
    ))
    if step_up.rp_id:
        extra_routes.extend(routes_security.build_routes(
            resolve_principal=lambda request: org_session.authenticated(request, org.sessions),
            check_csrf=org_session.check_csrf,
            check_origin=org_session.check_origin,
            unauthenticated_response=lambda request: RedirectResponse(
                "/login?next=/security", status_code=302, headers={"Cache-Control": "no-store"},
            ),
            session_cookie_name=org_session.SESSION_COOKIE,
            step_up=step_up, issuer_url=org.issuer_url,
            # Persistent header/nav (web_shell.ORG_NAV_ITEMS), same shell
            # /approvals/connect/settings all use -- see routes_security.py's
            # own module docstring on ``nav_items``.
            nav_items=web_shell.ORG_NAV_ITEMS,
        ))
    # Mounted unconditionally, same reasoning as /approvals above --
    # needs only org.sessions and the install-wide settings dict, both
    # already required parameters of this function either way.
    # routes_settings.py builds both modes' settings routes and renders
    # both through the same settings_window_html.build_html() (ADR 0033)
    # -- see build_org_routes's own docstring.
    extra_routes.extend(build_org_routes(
        sessions=org.sessions, install_wide_settings=org.install_wide_settings,
        install_wide_settings_path=org.install_wide_settings_path,
        # The same StepUpConfig/origin routes_approvals.build_routes
        # above already resolves from org.org_config -- see that call and
        # this function's own build_org_routes docstring.
        step_up=step_up, step_up_origin=org.issuer_url,
        # The admin's "AI systems" pin page (ADR 0035) lists and pins this provider's DCR clients.
        oauth_provider=org.provider,
        # Auto-accept rule values resolve to names through the viewing principal's own connectors.
        connector_registry=org.connector_registry,
    ))

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app: ASGIApp = Starlette(routes=extra_routes, lifespan=lifespan)
    resolver = principal_resolver or _org_principal_resolver(org.sessions)
    scoped: ASGIApp = _PrincipalScopeMiddleware(app, resolver)
    wrapped: ASGIApp = _HostAllowlistMiddleware(scoped, allowed_hosts)
    return _SecurityHeadersMiddleware(wrapped, hsts=True)


class WebServer:
    """Runs the embedded HTTP server on its own daemon thread -- always
    started in local mode (daemon_main.py's own
    ``_maybe_start_web_server``), since the web approval UI is the only one
    there is (ADR 0001 removed the native popup)."""

    def __init__(
        self,
        web_ui: WebApprovalUI,
        *,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        mcp_dispatcher: McpDispatcher | None = None,
        mcp_token: str | None = None,
        controller: SettingsController | None = None,
        allow_quit: bool = True,
        notifications_enabled: bool = True,
        notifications_detail: str = "minimal",
        principal_resolver: Callable[[Request], Principal] | None = None,
        org: OrgAuth | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
        trusted_proxies: tuple[str, ...] = (),
        step_up: StepUpConfig | None = None,
        agent_overrides: AgentOverrides | None = None,
    ) -> None:
        """``org``, ``ssl_certfile``/``ssl_keyfile`` and ``trusted_proxies``
        are org mode's own -- every local-mode caller leaves them unset.
        ``ssl_certfile``/``ssl_keyfile`` (both required together, or
        neither) terminate TLS directly in uvicorn; leave both
        unset when a reverse proxy in front of this daemon terminates TLS
        instead. ``trusted_proxies`` is the explicit allowlist required
        before ``X-Forwarded-For``/``X-Forwarded-Proto`` are
        honored at all -- empty (the default) means never, regardless of
        mode.

        ``step_up`` is local mode's own ``StepUpConfig`` --
        ``None`` (the default) mounts no ``/security`` route at all;
        daemon_main.py's real
        boot path always passes one (``StepUpConfig.from_local_config``).
        Ignored in org mode, which resolves its own from ``org.org_config``
        (see ``_build_org_app``).

        ``agent_overrides`` (local mode only) is ``settings.yaml``'s ``agent_overrides:`` section,
        parsed once by daemon_main.py (``agent_overrides.from_config``) -- a relabel only, never
        an attested source (see that module).
        """
        self.host = host
        self.port = port
        self.org = org
        # Local mode's real session/bootstrap-code stores, built
        # once here and shared with build_app() below -- None in org mode,
        # which has its own OrgSessionStore (org.sessions) and no bootstrap
        # concept at all (org mode's entry point is /login, not a one-time
        # link). Nothing in this class hands a code out: the
        # control channel is the only way one is minted, and who may ask for
        # an *attested* one is web/control_channel.py's own business.
        #
        # self.control_channel is how a fresh code gets minted on demand
        # (see web/control_channel.py's module docstring) -- also None in org
        # mode, same reasoning (nothing to mint a code for). Built from a
        # local `bootstrap` variable, not `self.bootstrap` directly, purely
        # so its type stays a plain `BootstrapStore` in this branch --
        # `self.bootstrap`'s own type is `BootstrapStore | None`, since it's
        # assigned once for both modes. See web/control_channel.py's own
        # module docstring.
        # ADR 0008: the shared, growable {token: principal_id} map every
        # /mcp and file-bridge call verifies against, and the one MINT
        # MCP/ROTATE MCP register new principals into over the control
        # channel below -- built here, ahead of ControlChannelServer, so
        # the callback that channel is given and the verifier build_app()
        # mounts are the exact same object. None whenever there is no /mcp
        # surface to mint a token for at all (org mode, which has its own
        # OAuth 2.1 authorization server instead; local mode with mcp
        # disabled).
        self.mcp_verifier: PerUserTokenVerifier | None = (
            PerUserTokenVerifier() if org is None and mcp_dispatcher is not None else None
        )
        if org is not None:
            self.sessions = None
            self.bootstrap = None
            self.control_channel = None
        else:
            self.sessions = LocalSessionStore()
            bootstrap = BootstrapStore()
            self.bootstrap = bootstrap
            # For STATUS: captured once, here,
            # rather than read fresh per STATUS call -- this *is* when the
            # daemon started, for exactly
            # as long as this WebServer instance is the one serving.
            started_at = datetime.now(timezone.utc).isoformat()
            self.control_channel = ControlChannelServer(
                bootstrap=bootstrap, allow_quit=allow_quit,
                # Plan items 1.2/1.3: the two questions the companion asks
                # this daemon that only this daemon can answer -- whether a
                # first enrollment is still outstanding, and "issue me a
                # replacement recovery code". Bound to the same StepUpConfig
                # every other consumer got (a LiveStepUpConfig on the real
                # boot path, so this reads the current value rather than the
                # one loaded at startup).
                enrollment_state=lambda: local_enrollment_state(step_up),
                reissue_recovery_code=reissue_local_recovery_code,
                status=lambda: local_status_payload(started_at),
                mint_mcp_token=self._mint_mcp_token if self.mcp_verifier is not None else None,
            )
        self.mcp_dispatcher = mcp_dispatcher
        self.mcp_token = (
            None if org is not None
            else ((mcp_token or load_or_create_mcp_token()) if mcp_dispatcher is not None else None)
        )
        if self.mcp_verifier is not None:
            # Preload every already-provisioned principal's own persisted
            # token (a previous run's MINT MCP/ROTATE MCP, or an unseparated
            # install's handoff/mcp_token -- see mcp_auth.py's
            # _mcp_token_path()), then make sure the local/owner
            # principal specifically has one registered even on a install's
            # very first start, before anyone has ever called MINT MCP.
            mcp_auth.preload_verifier(self.mcp_verifier)
            self.mcp_verifier.register(self.mcp_token or load_or_create_mcp_token(), LOCAL_PRINCIPAL.id)
        self.controller = controller
        self.allow_quit = allow_quit
        self.notifications_enabled = notifications_enabled
        self.notifications_detail = notifications_detail
        self.principal_resolver = principal_resolver
        # The state-push channel backs both /settings (async
        # outcomes reaching an open tab) and /approvals (the list, via
        # the same "approvals" event) -- built whenever either surface is
        # actually being served, not gated on mcp_dispatcher, which has
        # nothing to do with either page. Not built in org mode: neither
        # surface is mounted there yet (see module docstring), and nothing
        # in this class reads it in that case either.
        self.state_stream: StateStream | None = None
        if org is None and (controller is not None or web_ui is not None):
            self.state_stream = StateStream(
                settings_snapshot=(controller.snapshot if controller is not None else lambda: None),
                # ADR 0008: filtered to whichever principal this SSE
                # connection's own request is scoped to -- an unfiltered
                # list_pending() would push every principal's pending
                # approvals to whoever has a tab open, exactly the leak
                # _list_rows() in routes_approvals.py closes for the
                # equivalent poll-based endpoint.
                list_pending=lambda: web_ui.deferred_registry.list_pending(principal_id=current_principal().id),
            )
            if controller is not None:
                controller.add_change_listener(self.state_stream.push_settings)
        # Set once this server's own ASGI event loop is captured (see
        # _state_stream_loop_lifespan) -- wait_until_ready() below is what a
        # synchronous caller on another thread (daemon_main.py's run_app(),
        # the direct successor of the old IPCServerThread's own ``_ready``
        # Event) blocks on to learn that loop.
        self._loop_ready = threading.Event()
        # "::1" (not the bracketed "[::1]" a Host header would spell it
        # as) -- _parse_host_header normalizes every incoming Host header
        # the same way urlsplit's own .hostname does below, brackets
        # stripped, so the allowlist has to speak that same bare form.
        allowed_hosts = frozenset({host, "127.0.0.1", "::1"})
        if org is not None:
            issuer_host = urlsplit(org.issuer_url).hostname
            if issuer_host:
                allowed_hosts = allowed_hosts | {issuer_host}
        # Kept on the instance purely so a caller can report it: an
        # issuer_url whose hostname isn't the one users actually address
        # makes every request 400 with "Invalid Host header" and nothing
        # anywhere names the hosts that *would* have worked. daemon_main.
        # py's org-mode startup line logs this set for exactly that case.
        self.allowed_hosts = allowed_hosts
        wrapped = build_app(
            web_ui,
            sessions=self.sessions,
            bootstrap=self.bootstrap,
            allowed_hosts=allowed_hosts,
            mcp_dispatcher=mcp_dispatcher,
            mcp_token=self.mcp_token,
            mcp_verifier=self.mcp_verifier,
            controller=controller,
            allow_quit=allow_quit,
            state_stream=self.state_stream,
            notifications_enabled=notifications_enabled,
            notifications_detail=notifications_detail,
            loop_ready=self._loop_ready,
            principal_resolver=principal_resolver,
            org=org,
            step_up=step_up,
            step_up_issuer_url=f"http://{host}:{port}",
            agent_overrides=agent_overrides,
        )
        if trusted_proxies:
            # Honored only when this explicit list is non-empty --
            # never by default, in either mode.
            wrapped = ProxyHeadersMiddleware(wrapped, trusted_hosts=list(trusted_proxies))
        # proxy_headers=False: uvicorn otherwise applies its own
        # ProxyHeadersMiddleware, trusting 127.0.0.1/::1 (or
        # $FORWARDED_ALLOW_IPS) whatever trusted_proxies says -- the wrap
        # above must be the only one.
        config = uvicorn.Config(
            wrapped, host=host, port=port, log_level="warning",
            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile,
            proxy_headers=False,
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    def _mint_mcp_token(self, rotate: bool) -> str:
        """``ControlChannelServer``'s own ``mint_mcp_token`` callback (ADR
        0008): mints (or rotates) the connecting peer's own MCP token,
        registers it into ``self.mcp_verifier``, and returns it -- called
        only while that connection's own ``principal_scope`` is active (see
        ``_LineProtocolServer._dispatch``), so ``current_principal()`` here
        is exactly the peer's own kernel-verified identity, never a
        hardcoded default."""
        principal = current_principal()
        assert self.mcp_verifier is not None  # nosec B101  # only ever wired in when mcp_verifier exists
        token = (
            load_or_create_mcp_token(principal) if not rotate
            else mcp_auth.rotate_mcp_token(principal, verifier=self.mcp_verifier)
        )
        self.mcp_verifier.register(token, principal.id)
        return token

    @property
    def base_url(self) -> str:
        # Org mode's issuer_url is the authoritative externally-reachable
        # origin (it may differ from this process's own bind host/port
        # entirely, e.g. behind a reverse proxy or load balancer) -- local
        # mode has no such indirection, so it keeps computing this from
        # what it's actually bound to.
        if self.org is not None:
            return self.org.issuer_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    def wait_until_ready(self, timeout: float = 5.0) -> asyncio.AbstractEventLoop | None:
        """Blocks (from any thread) until this server's own ASGI event loop
        has been captured, or ``timeout`` elapses -- the direct successor of
        the old IPCServerThread's own ``_ready.wait(timeout=5)`` pattern,
        for the same reason: daemon_main.py's cache-warming needs a real,
        already-running loop to schedule Telegram's async warm on (see
        daemon_main._warm_connector_caches), not just "the thread has
        started". Returns ``None`` on a timeout, or if this server's own
        state_stream was never built (nothing to wait for -- see __init__:
        that only happens when ``web_ui`` is falsy, which no real caller
        passes)."""
        if self.state_stream is None:
            return None
        self._loop_ready.wait(timeout=timeout)
        return _state_stream.get_loop()

    @property
    def mcp_url(self) -> str | None:
        """``None`` unless this server was built with ``mcp_dispatcher`` --
        the URL to configure in a Streamable HTTP MCP client, e.g.
        ``claude mcp add --transport http privacyfence <mcp_url> --header
        "Authorization: Bearer <mcp_token>"``."""
        if self.mcp_dispatcher is None:
            return None
        return f"{self.base_url}{MCP_PATH}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="web-server", daemon=True)
        self._thread.start()
        logger.info("Web approval server listening on %s", self.base_url)
        if self.mcp_url is not None:
            _write_mcp_url_file(self.mcp_url)
        if self.control_channel is not None:
            self.control_channel.start()
            _write_web_base_url_file(self.base_url)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.control_channel is not None:
            self.control_channel.stop()
            _clear_web_base_url_file()
        if self.mcp_url is not None:
            _clear_mcp_url_file()
