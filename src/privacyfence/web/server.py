"""Embedded HTTP(S) server lifecycle: bind policy, security headers, and
starting/stopping the ASGI app (uvicorn) on its own thread -- see daemon_
main.py's own module docstring's "Threading model" section (its "Web
thread") for how this fits alongside the daemon's other threads.

**Organization mode** (the only mode): a
configurable bind host/port, optional TLS termination, optional
``X-Forwarded-*`` trust for a small explicit set of reverse-proxy
addresses (never by default), and ``/mcp`` authenticated by
``web/oauth_provider.py``'s real OAuth 2.1 authorization server (ADR 0011)
instead of one shared secret. Passed to ``build_app``/``WebServer`` as one
``OrgAuth`` bundle (see that class) rather than four separate parameters,
so a caller either opts into the whole org-mode picture or none of it.

**`/approvals`** (web/routes_approvals.py's ``build_routes()``) is a
principal-aware route set (ADR 0033), since many principals share one
daemon and none may see another's pending approvals: every read and
write is authorized against ``current_principal()`` via ``org_session``,
and a *write* decision additionally demands a fresh WebAuthn step-up when
the admin config's ``step_up.enabled`` is set --
web/routes_approvals.py's own module docstring covers both.

**`/settings`**: ``routes_settings.build_org_routes()`` mounts a
capability-filtered set of actions, restricted to ``org_settings_scope.ACTION_SCOPES``'s own ``ORG_MODE``
members: ``GET /settings``, every signed-in principal's own auto-accept
rules, read-only except for adding or removing one. The install-wide PII/privacy
policy (web/org_install_policy.py) is an administrator's, changed on the Admin page
(web/routes_admin.py); ``GET /settings/privacy`` redirects there.
That set renders through settings_window_html.build_html() (capability-filtered
per ``is_admin``) and dispatches every write through the generic
``POST /api/settings/{action}`` (ADR 0032), restricted to
``routes_settings._ORG_ALLOWED_ACTIONS``. Actions outside
web/org_settings_scope.py's own ``ACTION_SCOPES`` are not served, nor are
the admin-only actions that are install-wide but
aren't privacy policy (``set_log_level``, ``toggle_calendar_free_busy``,
``toggle_gmail_signature``) --
``ACTION_SCOPES`` is the one place that split is declared now, consulted
by both ``build_routes``/``build_org_routes`` rather than filtered
separately by each.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .. import (
    agent_label, agent_runtimes, auto_accept, gate, latency, paths, relay_client,
    runtime_credentials, scoped_token_proposals, scoped_tokens, secure_files, web_shell, write_effects,
)
from ..connector_registry import ConnectorRegistry
from ..org_identity import IdpAuthzPolicy, IdpConfig
from ..policy import catalogue as policy_catalogue
from ..policy import describe
from ..policy import scope_rows
from ..policy import scopes as policy_scopes
from ..policy import propose as policy_propose
from ..policy import registry as policy_registry
from ..policy.registry import Verb
from ..principal import ANONYMOUS_PRINCIPAL, Principal, principal_scope
from ..resource_names import get_resolver
from ..scoped_tokens import ScopedToken
from ..settings_state import cached_row_value, row_value_items, sentence_parts
from ..step_up_config import StepUpConfig
from ..web_approval_ui import WebApprovalUI
from ..web_push import PushNotifier, PushSubscriptionStore
from . import org_session
from . import routes_connect
from . import routes_downloads
from .routes_file_bridge import mount_capability_routes
from . import routes_oidc
from . import routes_org_identity
from .csp import build_csp
from .csp import new_nonce as _new_csp_nonce
from . import mcp_auth
from .mcp_auth import (
    CompositeTokenVerifier,
    ScopedTokenVerifier,
)
from .mcp_dispatch import McpDispatcher
from .oauth_provider import CONSENT_PATH, IDP_CALLBACK_PATH, OrgOAuthProvider
from .org_session import OrgSessionStore
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp, mount_org_oauth, protected_resource_metadata_url

if TYPE_CHECKING:
    from ..oidc_provider import OidcProvider

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
# uvicorn otherwise waits for every in-flight response before it stops, and an open event stream
# (an approvals tab, an MCP client's notification stream) never finishes on its own.
SHUTDOWN_GRACE_SECONDS = 2
_STOP_JOIN_SECONDS = 5.0
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
# this project itself controls. Only added where ``hsts`` is on -- see
# _SecurityHeadersMiddleware's own docstring.
_HSTS = "max-age=31536000; includeSubDomains"


def _scoped_rule_entry(
    row: scope_rows.ScopeRow, value_display: str, value_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """One row as the scoped token form shows it: ``title`` says which resource it covers,
    ``verbs`` what it allows there, ``tools`` which tools those verbs govern. ``title_parts`` and
    ``sentence_parts`` are the same texts split around the value and ``value_items`` the value
    itself, so the page can show it by id or by name (``row_value_items``)."""
    compiled = scope_rows.compile_row(row)
    connectors = sorted({policy_propose.connector_of_operation(op) for op in row.operations})
    connector = describe.connector_label(connectors[0]) if connectors else ""
    scope_type = describe.scope_type_of(compiled[0]) if compiled else ""

    def title(display: str) -> str:
        if scope_type:
            joiner = " except " if scope_type in describe.EXCLUSION_SCOPE_TYPES and row.value else " "
            subject = f"{describe.scope_type_label(scope_type)}{joiner}{display}".strip()
        else:
            subject = compiled[0].predicate if compiled else (row.predicate or row.scope or "")
        return f"{connector} — {subject}" if connector else subject

    marker = "\x00"
    before, found, after = title(marker).partition(marker) if scope_type and row.value else ("", "", "")
    return {
        "id": row.id,
        "title": title(value_display),
        "title_parts": [before, after] if found else None,
        "sentence_parts": sentence_parts(row),
        "value_items": value_items,
        "verbs": [verb.value for verb in sorted(row.verbs, key=policy_propose.verb_sort_key)],
        "sentence": describe.row_sentence(row, value_display=value_display or None),
        "tools": sorted({tool for rule in compiled for tool in describe.covered_tools(rule)}),
    }


def _tool_connector(name: str, connector_names: set[str]) -> str | None:
    """The enabled connector a tool belongs to: the longest connector name the tool name starts
    with, so ``apps_script_*`` tools are not filed under a connector called ``apps``."""
    matches = [c for c in connector_names if name.startswith(c + "_")]
    return max(matches, key=len) if matches else None


def _verb_tools(group_id: str, verbs: list[str]) -> dict[str, list[str]]:
    """For each verb of a scope group, the tools a rule of that group and verb covers (the New
    token form's coverage preview)."""
    tools: dict[str, list[str]] = {}
    for name in verbs:
        verb = Verb(name)
        row = scope_rows.ScopeRow(
            id="", scope=group_id, value=None, verbs=frozenset({verb}),
            operations=scope_rows.implied_operations(group_id, [verb]),
        )
        tools[name] = sorted({t for rule in scope_rows.compile_row(row) for t in describe.covered_tools(rule)})
    return tools


_APPROVAL_PREDICATE = "upstream.approved_tool"
_REVIEW_EFFECT = "The result goes to the token without a person seeing it. Personal data in it still blocks the call."


def _gate_effect(name: str, tool_gate: str) -> str:
    return write_effects.EFFECT_BY_TOOL.get(name, "") if tool_gate == "popup" else _REVIEW_EFFECT


def _gated_tools(upstream_id: str) -> list[dict[str, str]]:
    """The ``popup`` and ``review`` tools of one upstream an administrator may approve for a token
    (ADR 0300), each with the digest of the operator's declaration the approval is pinned to."""
    out = []
    for name in sorted(auto_accept.TOOL_TO_GATE):
        if not name.startswith(upstream_id + "_") or not gate.is_gated_upstream_tool(name):
            continue
        operation = auto_accept.TOOL_TO_OPERATION[name]
        if not operation.startswith(upstream_id + "."):
            continue
        tool_gate = auto_accept.TOOL_TO_GATE[name]
        out.append({
            "name": name, "gate": tool_gate, "verb": policy_registry.TOOL_TO_VERB[name].value,
            "effect": _gate_effect(name, tool_gate), "digest": policy_scopes.UPSTREAM_DECLARATIONS[operation],
        })
    return out


def _approved_upstream_tools(token: ScopedToken) -> list[dict[str, str]]:
    """The tools a token's approval rows approve, for the token list."""
    by_operation = {op: name for name, op in auto_accept.TOOL_TO_OPERATION.items()}
    out = []
    for row in token.rules:
        if row.predicate != _APPROVAL_PREDICATE:
            continue
        for operation in sorted(row.operations):
            name = by_operation.get(operation)
            if name is None:
                continue
            tool_gate = auto_accept.TOOL_TO_GATE.get(name, "popup")
            out.append({"name": name, "gate": tool_gate, "effect": _gate_effect(name, tool_gate)})
    return sorted(out, key=lambda item: item["name"])


def scoped_token_page_data(
    principal: Principal, *, policy: scoped_tokens.ScopedTokenPolicy, connector_names: set[str],
) -> dict[str, Any]:
    """What ``window.__pfScopedTokens`` carries for ``principal``: their
    tokens, the rules they may carry and the tools of their enabled
    connectors. A new secret is shown to the person once (``delivery``
    ``"once"``, ADR 0165). Must not be called under another principal's scope.

    Each rule's resource ids are shown by the names the Auto-accept page has
    cached (``cached_row_value``), so the two pages name a rule alike. A
    rule's ``title`` names what it covers without its verbs, which the form
    shows separately; ``auto_tools`` are the tools that need no rule at all.
    """
    now = time.time()
    store = scoped_tokens.get_store()
    own_tokens = store.tokens_for(principal)
    with principal_scope(principal):
        resolver = get_resolver()
        rules = [
            _scoped_rule_entry(row, cached_row_value(row, resolver), row_value_items(row, resolver))
            for row in auto_accept.get_policy_rows()
        ]
        # A token's rules are its own rows (ADR 0236), named the way the Auto-accept page names them.
        token_rules = {
            token.id: [
                _scoped_rule_entry(row, cached_row_value(row, resolver), row_value_items(row, resolver))
                for row in token.rules
                if row.predicate != _APPROVAL_PREDICATE
            ]
            for token in own_tokens
        }
    by_connector: dict[str, list[str]] = {}
    for name in sorted(auto_accept.TOOL_TO_GATE):
        if gate.is_gated_upstream_tool(name):
            continue  # Offered only as an administrator's approval below (ADR 0300).
        connector = _tool_connector(name, connector_names)
        if connector is not None:
            by_connector.setdefault(connector, []).append(name)
    tokens = []
    activity = store.activity_for(principal)
    delivered = {
        r.token_id: r for r in runtime_credentials.get_store().all()
        if r.principal_id == principal.id and r.state == runtime_credentials.DELIVERED
    }
    for token in own_tokens:
        seen = activity.get(token.id, scoped_tokens.TokenActivity())
        expired = token.expired(now)
        days_left = token.days_left(now)
        tokens.append({
            "id": token.id, "label": token.label, "created_at": token.created_at,
            "expires_at": token.expires_at, "days_left": days_left,
            "expiring_soon": not expired and days_left <= scoped_tokens.WARN_DAYS, "expired": expired,
            "rules": token_rules[token.id],
            "approved_upstream_tools": _approved_upstream_tools(token),
            "tools": sorted(token.tools),
            "allow_personal_data": token.allow_personal_data,
            "allow_personal_data_reads": token.allow_personal_data_reads,
            "upstream_audiences": {uid: sorted(doms) for uid, doms in sorted(token.upstream_audiences.items())},
            "last_used_at": seen.last_used_at, "last_denied_at": seen.last_denied_at,
            "last_denied_tool": seen.last_denied_tool,
            "runtime_credential": (
                None if token.id not in delivered else {
                    "runtime": delivered[token.id].runtime,
                    "credential_name": delivered[token.id].credential_name,
                    "workflow_id": delivered[token.id].workflow_id,
                }
            ),
        })
    proposals = [
        {"id": p.id, "label": p.request["label"], "agent_label": agent_label.label_for(p.agent).text,
         "expires_at": p.expires_at}
        for p in scoped_token_proposals.get_registry().pending_for(principal.id)
    ]
    page: dict[str, Any] = {
        "delivery": "once", "proposals": proposals, "proposals_enabled": policy.allow_ai_proposals,
        "max_days": policy.max_days, "default_days": min(scoped_tokens.DEFAULT_DAYS, policy.max_days),
        "warn_days": scoped_tokens.WARN_DAYS, "tokens": tokens, "rules": rules,
        "auto_tools": sorted(
            name for name, tool_gate in auto_accept.TOOL_TO_GATE.items()
            if tool_gate == "auto" and not gate.is_gated_upstream_tool(name) and _tool_connector(name, connector_names) is not None
        ),
        "tools": [{"connector": connector, "tools": names} for connector, names in sorted(by_connector.items())],
        "scope_groups": [
            {**entry, "verb_tools": _verb_tools(entry["id"], entry["verbs"])}
            for entry in policy_catalogue.scope_catalogue() if entry["connector"] in connector_names
        ],
    }
    from .. import upstream_mcp_runtime  # function-local, as in _build_org_app

    if upstream_mcp_runtime.configured():
        # The audiences a token may narrow are the ones the person holds themselves.
        held = upstream_mcp_runtime.held_audiences(principal, principal.email_verified)
        page["upstreams"] = [
            {
                "id": row["id"], "label": row["label"], "tools": by_connector.get(str(row["id"]), []),
                "audiences": sorted(held.get(str(row["id"]), frozenset())),
                "gated_tools": _gated_tools(str(row["id"])) if principal.is_admin else [],
            }
            for row in upstream_mcp_runtime.status()
        ]
    return page


def _write_mcp_url_file(url: str) -> None:
    """The discovery file naming the /mcp URL: WebServer.start() writes it
    when it binds, and clears it on shutdown. 0600: not a secret itself,
    but written alongside the server's other files under the same
    directory."""
    path = paths.data_dir() / MCP_URL_FILE_NAME
    secure_files.atomic_write_text(path, url)


def _clear_mcp_url_file() -> None:
    """Called on WebServer.stop() so a reader after this daemon exits finds
    no file rather than a stale, now-dead URL."""
    (paths.data_dir() / MCP_URL_FILE_NAME).unlink(missing_ok=True)


# The two org-mode OAuth paths a client's sign-in popup passes through on
# its way back to the client's own redirect URI. Cross-Origin-Opener-Policy
# on any response in that chain, a 302 included, moves the popup into a new
# browsing-context group and nulls its ``window.opener`` -- and a client
# whose redirect page hands the code back through ``window.opener``
# (Gemini Enterprise's does) then never exchanges it. Neither path renders
# a document, so COOP protected nothing on them. ADR 0108. The consent pages
# (``CONSENT_PATH``/<id>, ADR 0177) are the one exception to "neither renders a
# document": they do, but they sit in the same chain and must keep the opener too.
_OAUTH_POPUP_PATHS = frozenset({"/authorize", IDP_CALLBACK_PATH})


def _keeps_popup_opener(path: str) -> bool:
    return path in _OAUTH_POPUP_PATHS or path.startswith(CONSENT_PATH + "/")


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

    ``hsts`` gates ``Strict-Transport-Security`` alone: on plain
    ``http://localhost`` the header is at best inert and at worst actively
    wrong -- a browser that honored it would start refusing plain-HTTP
    connections to ``localhost`` for every *other* app on the machine too,
    since HSTS is scoped by hostname, not by port or origin. With ``hsts=True``
    ``issuer_url`` is a real, single-purpose public hostname reached over
    HTTPS (TLS terminated here or, just as often, at a reverse proxy in
    front of this daemon -- either way the browser's own connection is
    HTTPS, which is what HSTS actually governs), so pinning that hostname
    to HTTPS-only is exactly the win intended.

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

    def __init__(self, app: ASGIApp, *, hsts: bool = False, app_origin: str = "") -> None:
        self._app = app
        self._hsts = hsts
        self._app_origin = app_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state.setdefault("csp_nonce", _new_csp_nonce())

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                nonce = scope.get("state", {}).get("csp_nonce") or _new_csp_nonce()
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["x-frame-options"] = "DENY"
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["content-security-policy"] = build_csp(nonce, app_origin=self._app_origin)
                headers["permissions-policy"] = _PERMISSIONS_POLICY
                headers["cross-origin-opener-policy"] = (
                    "unsafe-none" if _keeps_popup_opener(scope.get("path") or "") else "same-origin"
                )
                if self._hsts:
                    headers["strict-transport-security"] = _HSTS
            await send(message)

        await self._app(scope, receive, send_with_headers)


_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::"})  # nosec B104  # names to keep out of the allowlist, not an address bound


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
    # None in single-user mode, where there is no identity provider to sign in through.
    idp: IdpConfig | None
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
    # ``config``) -- routes_admin.build_org_routes()'s privacy section
    # reads and rewrites this directly, and
    # daemon_main._start_org_web_server threads the
    # same dict into every org principal's own privacy-filter registration
    # (see _load_principal_settings()'s docstring). Defaults to {} for the
    # same "every existing OrgAuth() caller keeps working" reason
    # connector_registry/org_config already do.
    install_wide_settings: dict = field(default_factory=dict)
    # Where that dict was loaded from, so the Admin page's privacy
    # actions can write it back. Separate from the dict rather than derived from it
    # because nothing in a parsed settings.yaml records its own path.
    # Empty means "read-only": routes_admin.build_org_routes() shows
    # the policy and rejects a write, which is
    # exactly what an OrgAuth built by a test that never had a real
    # settings.yaml on disk should do.
    install_wide_settings_path: str = ""
    # Org mode's web push (ADR 0081): the notifier daemon_main.py registered on the approval
    # registry, and the per-principal subscription store it reads. Both None when the org has
    # turned push off (the admin config's web_push.enabled: false) and in any test that does not
    # care about push: the subscription routes are then not mounted and no page offers to
    # subscribe. The manifest is mounted either way.
    push_notifier: "PushNotifier | None" = None
    push_store: "PushSubscriptionStore | None" = None
    # The raw signed bundle, and the admin config as loaded at startup. org_config above is
    # composed from both; the Admin page recomputes it after a change and needs the inputs.
    bundle: dict[str, Any] = field(default_factory=dict)
    startup_admin: dict[str, Any] = field(default_factory=dict)
    # The OpenID Connect provider for the web apps the operator named in the environment (ADR 0273).
    # None when none were named: the /oidc routes are then not mounted.
    oidc: "OidcProvider | None" = None


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


def build_app(
    web_ui: WebApprovalUI,
    *,
    org: OrgAuth,
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    mcp_dispatcher: McpDispatcher | None = None,
    stopping: threading.Event | None = None,
    principal_resolver: Callable[[Request], Principal] | None = None,
) -> ASGIApp:
    """The organization app, wrapped with the Host allowlist and security headers every real
    deployment needs. ``org`` bundles the OAuth 2.1 provider, the sessions and the identity
    provider config, so ``/mcp`` is authenticated by ``org.provider`` and the sign-in routes are
    mounted beside it (see ``_build_org_app``).

    ``principal_resolver`` defaults to ``_org_principal_resolver``; pass an explicit one only to
    prove per-principal isolation over real HTTP in a test.
    """
    return _build_org_app(
        org, web_ui=web_ui, mcp_dispatcher=mcp_dispatcher, allowed_hosts=allowed_hosts,
        principal_resolver=principal_resolver, stopping=stopping,
    )


async def _relay_well_known(request: Request) -> Response:
    """The SHA-256 of the relay secret, which the sign-in relay fetches to check that this server
    controls its host name. Public by design: only the secret, which the server sends to the relay
    alone, can register the host. It is withheld until a service actually uses the relay, so
    nobody else can register a server whose administrator never offered a built-in service."""
    if not relay_client.status()["needed"]:
        return PlainTextResponse("Not Found", status_code=404, headers={"Cache-Control": "no-store"})
    proof = await asyncio.to_thread(relay_client.well_known_proof)
    return PlainTextResponse(proof, headers={"Cache-Control": "no-store"})


def _build_org_app(
    org: OrgAuth, *, web_ui: WebApprovalUI, mcp_dispatcher: McpDispatcher | None, allowed_hosts: frozenset[str],
    principal_resolver: Callable[[Request], Principal] | None, stopping: threading.Event | None = None,
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
    from . import (
        routes_admin, routes_approvals, routes_org_stepup, routes_push, routes_security, routes_upstream_mcp,
        routes_whats_new, single_user_auth,
    )
    from .. import upstream_mcp_runtime
    from .routes_settings import build_org_routes

    if org.provider.single_user != (org.idp is None):
        # The sign-in routes follow org.idp and the authorize path follows the provider; the two
        # must agree on whether there is an identity provider.
        raise ValueError("OrgAuth.idp and OrgOAuthProvider.single_user disagree about single-user mode")
    extra_routes: list[BaseRoute] = []
    lifespans = []
    if mcp_dispatcher is not None:
        mcp_route, session_manager = mount_mcp(
            mcp_dispatcher, verifier=CompositeTokenVerifier(ScopedTokenVerifier(), org.provider),
            resource_metadata_url=protected_resource_metadata_url(org.issuer_url),
            client_names=org.provider.client_name, attested_agents=org.provider.attested_agent_id,
        )
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))
        # Only a real dispatcher-backed server carries gated calls worth watching -- see latency.py.
        lifespans.append(latency.loop_health_lifespan())
        # ADR 0028: privacyfence_create_upload_slot and
        # DownloadDeliveryConfig.agent_links need the two unauthenticated
        # capability routes -- see routes_file_bridge.py's own module docstring. Gated on
        # mcp_dispatcher exactly like /mcp itself: with no dispatcher,
        # neither the upload-slot meta-tool nor a connector download tool
        # is reachable to mint a capability link in the first place.
        extra_routes.extend(mount_capability_routes())

    extra_routes.extend(mount_org_oauth(org.provider, issuer_url=org.issuer_url, sessions=org.sessions))
    if org.oidc is not None:
        extra_routes.extend(routes_oidc.build_routes(org.oidc, sessions=org.sessions))
    claim_signer = upstream_mcp_runtime.signer()
    if upstream_mcp_runtime.configured() and claim_signer is not None:
        # ADR 0286: the key set an upstream verifies claims against; the lifespan fetches each
        # upstream's tool list once the server runs.
        extra_routes.extend(routes_upstream_mcp.build_routes(claim_signer))
        if mcp_dispatcher is not None:
            lifespans.append(upstream_mcp_runtime.lifespan(mcp_dispatcher.notify_tools_changed))
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
    extra_routes.append(Route("/.well-known/privacyfence-relay", _relay_well_known, methods=["GET"]))
    if org.connector_registry is not None:
        extra_routes.extend(routes_connect.build_routes(
            sessions=org.sessions, connector_registry=org.connector_registry,
            org_config=org.org_config, issuer_url=org.issuer_url,
        ))
        default_next_path = "/connect"
    issuer_host = urlparse(org.issuer_url).hostname or ""
    step_up = StepUpConfig.from_org_config(org.org_config, default_rp_id=issuer_host)
    if org.idp is not None:
        extra_routes.extend(routes_org_identity.build_routes(
            idp=org.idp, sessions=org.sessions, base_url=org.issuer_url, default_next_path=default_next_path,
            # Derived from org.org_config directly here, the same
            # "self-contained, cheap re-parse" pattern StepUpConfig above
            # already uses -- see that call's own comment. Bound to the IdP like the OAuth
            # provider's copy, so browser sign-in applies the same rules.
            policy=IdpAuthzPolicy.bind(AuthzPolicyConfig.from_org_config(org.org_config), org.idp),
            push_store=org.push_store,
        ))
    else:
        # Single-user mode: a setup code, then the passkey /security enrolls, with the same
        # relying-party ID and origin.
        extra_routes.extend(single_user_auth.build_routes(
            sessions=org.sessions, issuer_url=org.issuer_url, step_up=step_up,
            default_next_path=default_next_path, push_store=org.push_store,
        ))
    # One approval route module builds the approval routes (ADR 0033) --
    # only the IdP step-up routes stay a separate mount, see routes_org_stepup.py's own module docstring.
    # The installable app (manifest, icons) always; the push subscription routes only when the
    # org has push on (ADR 0081). The approvals page is the one that offers to subscribe.
    push_store = org.push_store if org.push_notifier is not None else None
    push_public_key = org.push_notifier.public_key if org.push_notifier is not None and push_store else ""
    extra_routes.extend(routes_push.build_routes(sessions=org.sessions, store=push_store))
    extra_routes.extend(routes_whats_new.build_routes(sessions=org.sessions))
    extra_routes.extend(routes_approvals.build_routes(
        web_ui=web_ui, sessions=org.sessions, step_up=step_up, issuer_url=org.issuer_url,
        push_public_key=push_public_key, stopping=stopping,
    ))
    if org.idp is not None:
        extra_routes.extend(routes_org_stepup.build_routes(
            web_ui=web_ui, sessions=org.sessions, step_up=step_up, idp=org.idp, issuer_url=org.issuer_url,
            policy=IdpAuthzPolicy.bind(AuthzPolicyConfig.from_org_config(org.org_config), org.idp),
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
    scoped_token_policy = scoped_tokens.policy_from_org_config(org.org_config)
    agent_runtime = agent_runtimes.N8nRuntimeConfig.from_org_config(org.org_config)
    org_scoped_token_page_data: Callable[[Principal], dict[str, Any]] | None = None
    if mcp_dispatcher is not None:
        dispatcher = mcp_dispatcher

        def org_scoped_token_page_data(principal: Principal) -> dict[str, Any]:
            # The dispatcher's connector set is the principal's own while scoped to them.
            return scoped_token_page_data(
                principal, policy=scoped_token_policy, connector_names=set(dispatcher.connectors),
            )

    # Mounted unconditionally, same reasoning as /approvals above --
    # needs only org.sessions and the install-wide settings dict, both
    # already required parameters of this function either way.
    # routes_settings.py builds both modes' settings routes and renders
    # both through the same settings_window_html.build_html() (ADR 0033)
    # -- see build_org_routes's own docstring.
    extra_routes.extend(build_org_routes(
        sessions=org.sessions, install_wide_settings=org.install_wide_settings,
        # The same StepUpConfig/origin routes_approvals.build_routes
        # above already resolves from org.org_config -- see that call and
        # this function's own build_org_routes docstring.
        step_up=step_up, step_up_origin=org.issuer_url,
        # Auto-accept rule values resolve to names through the viewing principal's own connectors.
        connector_registry=org.connector_registry,
        # ADR 0165: the administrator's opt-in and cap from the org config bundle; the section
        # needs /mcp, so without a dispatcher the page offers none.
        scoped_token_policy=scoped_token_policy, scoped_token_store=scoped_tokens.get_store(),
        scoped_token_page_data=org_scoped_token_page_data,
        agent_runtime=agent_runtime,
    ))
    # The Admin API shares the effective config dict, so a live change reaches every holder.
    extra_routes.extend(routes_admin.build_org_routes(
        sessions=org.sessions, step_up=step_up, step_up_origin=org.issuer_url,
        connector_registry=org.connector_registry, org_config=org.org_config,
        bundle=org.bundle, startup_admin=org.startup_admin,
        # The privacy actions rewrite the server's settings.yaml; the Stats section (ADR 0217)
        # lists the connectors org_config offers.
        install_wide_settings=org.install_wide_settings,
        install_wide_settings_path=org.install_wide_settings_path,
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
    return _SecurityHeadersMiddleware(wrapped, hsts=True, app_origin=org.issuer_url)


class WebServer:
    """Runs the embedded HTTP server on its own daemon thread, for organization mode
    (``daemon_main.py``'s ``_start_org_web_server``)."""

    def __init__(
        self,
        web_ui: WebApprovalUI,
        *,
        org: OrgAuth,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        mcp_dispatcher: McpDispatcher | None = None,
        principal_resolver: Callable[[Request], Principal] | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
        trusted_proxies: tuple[str, ...] = (),
    ) -> None:
        """``ssl_certfile``/``ssl_keyfile`` (both required together, or neither) terminate TLS
        directly in uvicorn; leave both unset when a reverse proxy in front of this daemon
        terminates TLS instead. ``trusted_proxies`` is the explicit allowlist required before
        ``X-Forwarded-For``/``X-Forwarded-Proto`` are honored at all -- empty (the default) means
        never.
        """
        self.host = host
        self.port = port
        self.org = org
        self.mcp_dispatcher = mcp_dispatcher
        self.principal_resolver = principal_resolver
        if mcp_dispatcher is not None:
            # ADR 0164/0165: the scoped tokens /mcp accepts, loaded for every principal with a
            # folder, so tokens created by a previous run verify again on this one.
            store = scoped_tokens.ScopedTokenStore(mcp_auth.scoped_token_principals)
            store.load()
            scoped_tokens.set_store(store)
        # Set first thing in stop(): ends the approvals event stream, which polls it once a second.
        self._stopping = threading.Event()
        # "::1" (not the bracketed "[::1]" a Host header would spell it
        # as) -- _parse_host_header normalizes every incoming Host header
        # the same way urlsplit's own .hostname does below, brackets
        # stripped, so the allowlist has to speak that same bare form.
        # A wildcard bind address is where the socket listens, never a name a client addresses it
        # by, so it stays out of the allowlist.
        allowed_hosts = frozenset({host, "127.0.0.1", "::1"}) - _WILDCARD_BIND_HOSTS
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
            org=org,
            allowed_hosts=allowed_hosts,
            mcp_dispatcher=mcp_dispatcher,
            stopping=self._stopping,
            principal_resolver=principal_resolver,
        )
        if trusted_proxies:
            # Honored only when this explicit list is non-empty -- never by default.
            wrapped = ProxyHeadersMiddleware(wrapped, trusted_hosts=list(trusted_proxies))  # type: ignore[arg-type,assignment]  # uvicorn's ASGI types are narrower than Starlette's
        # proxy_headers=False: uvicorn otherwise applies its own
        # ProxyHeadersMiddleware, trusting 127.0.0.1/::1 (or
        # $FORWARDED_ALLOW_IPS) whatever trusted_proxies says -- the wrap
        # above must be the only one.
        config = uvicorn.Config(
            wrapped, host=host, port=port, log_level="warning",
            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile,
            proxy_headers=False,
            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        # The issuer_url is the authoritative externally-reachable origin (it may differ from this
        # process's own bind host/port entirely, e.g. behind a reverse proxy or load balancer).
        return self.org.issuer_url.rstrip("/")

    def wait_until_ready(self, timeout: float = 5.0) -> asyncio.AbstractEventLoop | None:
        """Always ``None``: the organization server captures no event loop for the daemon's
        connector cache warming."""
        return None

    @property
    def stopped(self) -> bool:
        """True once the server thread has ended, or was never started."""
        return self._thread is None or not self._thread.is_alive()

    @property
    def mcp_url(self) -> str | None:
        """``None`` unless this server was built with ``mcp_dispatcher`` -- the URL to configure
        in a Streamable HTTP MCP client."""
        if self.mcp_dispatcher is None:
            return None
        return f"{self.base_url}{MCP_PATH}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="web-server", daemon=True)
        self._thread.start()
        logger.info("Web approval server listening on %s", self.base_url)
        if self.mcp_url is not None:
            _write_mcp_url_file(self.mcp_url)

    def stop(self) -> None:
        self._stopping.set()
        open_connections = len(self._server.server_state.connections)
        started = time.monotonic()
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=SHUTDOWN_GRACE_SECONDS + _STOP_JOIN_SECONDS)
            if self._thread.is_alive():
                self._server.force_exit = True
                self._thread.join(timeout=_STOP_JOIN_SECONDS)
            if self._thread.is_alive():
                logger.warning("The web server thread is still running after stop()")
        elapsed = time.monotonic() - started
        if elapsed >= SHUTDOWN_GRACE_SECONDS:
            logger.warning(
                "The web server took %.1f s to stop; %d connection(s) were open when it was asked to",
                elapsed, open_connections,
            )
        if self.mcp_url is not None:
            _clear_mcp_url_file()
