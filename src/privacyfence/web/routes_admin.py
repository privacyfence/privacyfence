"""The Admin JSON API: what an organization offers and how it is gated.

``POST /api/admin/{action}`` changes one thing and answers with the page state; ``GET
/api/admin/state`` answers with the state alone; ``POST /api/admin/rooms/upload`` replaces the
room directory. ``build_org_routes`` and ``build_routes`` (local mode) are the two adapters around
a mode-independent core (``_dispatch`` and ``_admin_state``), the way ``routes_settings`` keeps one
route layer with an authentication adapter per mode (ADR 0033). Local mode also mounts
``POST /api/admin/org_config/upload``, the bundle upload that used to live on Settings.

``GET /admin`` is the page itself: ``settings_window_html`` rendering the page state, with the
bridge shim posting to ``/api/admin``. In organization mode a signed-out visitor is sent to the
login page and a signed-in non-administrator gets a 403 page.

Who may use it. In organization mode, a signed-in principal with ``is_admin``; anyone else gets a
JSON 401 or 403. In local mode, the install owner (the caller mounts these routes behind the same
owner-only wrapper as Settings), and sensitive actions and both uploads also need a session a
person is behind where the install asks for one. The CSRF and origin checks are the ones each
mode's other routes use.

The bundle. Organization mode holds it in memory; local mode has no such holder and reads the
installed file on each request, because a bundle can be installed while the daemon runs.

How a change is gated. Every sensitive action needs a fresh WebAuthn assertion whatever the
step-up configuration says (``always=True``): the Admin page decides which services an agent can
reach and where audit records go, so it must not depend on an administrator having switched
step-up on. A principal with no passkey is told to add one.

What a request may carry. Only the action's declared parameters, plus ``csrf`` and
``webauthn_assertion``. Anything else is a 400, so server state (the bundle, the mode) can never
come from the request.

How a change takes effect (``AdminAction.applies``):

* ``live``: the shared organization config is refreshed in place and connectors are rebuilt
  (organization mode: every principal's are dropped and the relay is asked to re-check its
  registration; local mode: the owner's are rebuilt through the controller, and the dict other
  OS accounts' connector factories read is the one refreshed);
* ``restart``: the change is only saved, and the page state lists it in ``restart_needed``
  until the server restarts;
* ``self``: the action applies itself. The four install-wide privacy actions do: they rewrite the
  server's settings.yaml and reload the privacy filter and PII detector for every principal
  (``org_install_policy.apply_change``), so an organization's privacy policy is changed here by an
  administrator and Settings stays the same page for everyone.

A route that mutates must be listed in ``ROUTE_CLASSIFICATION``; ``build_org_routes`` raises for
one that is not (ADR 0014).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from .. import admin_config, data_format, oauth_apps, org_mode, relay_client, settings_window_html, web_shell
from ..connector_registry import ConnectorRegistry
from ..org_mode import Mode
from ..principal import LOCAL_PRINCIPAL, Principal, principal_scope
from ..settings_controller import REPO_URL, SettingsController, _pii_general_fields, _privacy_state_from_config
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore
from . import org_install_policy, org_session, org_stats
from .csp import nonce_for as _csp_nonce_for
from .routes_security import PF_WEBAUTHN_JS
from .routes_settings import (
    _apply_step_up_gate,
    _BadAction,
    _coerce,
    _gate_upload,
    _parse_form_assertion,
    _record_settings_audit,
    _settings_bridge_shim,
    handle_org_config_upload,
)
from .session_auth import SESSION_COOKIE as _LOCAL_SESSION_COOKIE
from .session_auth import LocalSessionStore
from .session_auth import authenticated as _session_authenticated
from .session_auth import check_csrf as _local_csrf_matches
from .session_auth import check_origin as _local_origin_ok
from .session_auth import human_session_required_json as _human_session_required_json
from .session_auth import is_human_session as _is_human_session
from .session_auth import unauthorized_html as _unauthorized_response

logger = logging.getLogger(__name__)

_LOCAL_AND_ORG: frozenset[str] = frozenset({"local", "org"})
_ORG_ONLY: frozenset[str] = frozenset({"org"})

# Keys every request may carry besides an action's own parameters.
_ENVELOPE_KEYS = frozenset({"csrf", "webauthn_assertion"})


@dataclass(frozen=True)
class AdminAction:
    modes: frozenset[str]
    sensitive: bool
    applies: Literal["live", "restart", "self"]


ADMIN_ACTIONS: dict[str, AdminAction] = {
    "offer_service": AdminAction(_LOCAL_AND_ORG, True, "live"),
    "withdraw_service": AdminAction(_LOCAL_AND_ORG, False, "live"),
    "set_atlassian_assets": AdminAction(_LOCAL_AND_ORG, True, "live"),
    "set_server_address": AdminAction(_LOCAL_AND_ORG, True, "live"),
    "set_auto_provisioning": AdminAction(_ORG_ONLY, True, "restart"),
    "set_unattended_sessions": AdminAction(_LOCAL_AND_ORG, True, "restart"),
    "clear_rooms": AdminAction(_LOCAL_AND_ORG, False, "live"),
    "set_download_delivery": AdminAction(_ORG_ONLY, True, "restart"),
    "set_web_push": AdminAction(_ORG_ONLY, True, "restart"),
    "set_authz": AdminAction(_ORG_ONLY, True, "restart"),
    "tighten_step_up": AdminAction(_ORG_ONLY, True, "restart"),
    "set_scoped_tokens": AdminAction(_ORG_ONLY, True, "restart"),
    "set_audit_forwarding": AdminAction(_ORG_ONLY, True, "restart"),
    "toggle_pii_detection": AdminAction(_ORG_ONLY, True, "self"),
    "toggle_pii_category": AdminAction(_ORG_ONLY, True, "self"),
    "set_default_policy": AdminAction(_ORG_ONLY, True, "self"),
    "set_category_policy": AdminAction(_ORG_ONLY, True, "self"),
}

# The actions ``org_install_policy.apply_change`` carries out.
_PRIVACY_ACTIONS = frozenset(org_install_policy.SUPPORTED_ACTIONS)

# (name, type, required) for each declared parameter, in the order the page posts them.
_Param = tuple[str, type, bool]
ADMIN_PARAMS: dict[str, tuple[_Param, ...]] = {
    "offer_service": (("service", str, True),),
    "withdraw_service": (("service", str, True),),
    "set_atlassian_assets": (("enabled", bool, True), ("workspace_id", str, False)),
    "set_server_address": (("service", str, True), ("value", str, True)),
    "set_auto_provisioning": (
        ("service", str, True), ("enabled", bool, True), ("trust_unverified_email", bool, True),
    ),
    "set_unattended_sessions": (("enabled", bool, True),),
    "clear_rooms": (),
    "set_download_delivery": (
        ("inline_max_bytes", int, True), ("link_ttl_seconds", int, True),
        ("allow_disk_staging", bool, True), ("agent_links", bool, True), ("allow_exports", bool, True),
    ),
    "set_web_push": (("enabled", bool, True),),
    "set_authz": (
        ("allowed_domains", str, True), ("groups_claim", str, True), ("required_groups", str, True),
    ),
    "tighten_step_up": (("enabled", bool, True), ("require_passkey", bool, True), ("scope", str, True)),
    "set_scoped_tokens": (("enabled", bool, True), ("max_days", int, True), ("allow_ai_proposals", bool, True)),
    "set_audit_forwarding": (
        ("enabled", bool, True), ("kind", str, True), ("syslog_host", str, True), ("syslog_port", int, True),
        ("syslog_protocol", str, True), ("http_url", str, True),
    ),
    "toggle_pii_detection": (),
    "toggle_pii_category": (("category_key", str, True),),
    "set_default_policy": (("group", str, True), ("policy", str, True)),
    "set_category_policy": (("group", str, True), ("category", str, True), ("policy", str, True)),
}

# The sections that take effect only when the server starts again, with the name the page shows.
# ``auto_provision`` lives under each Peek or Grist service rather than at the top level.
RESTART_LABELS: dict[str, str] = {
    "unattended_sessions": "Unattended sessions",
    "download_delivery": "File delivery",
    "web_push": "Web push",
    "authz": "Sign-in restrictions",
    "step_up": "Step-up",
    "scoped_tokens": "Scoped tokens",
    "audit_forwarding": "Audit forwarding",
    "auto_provision": "Automatic accounts",
}

# Every POST route this module builds, and how its request is gated (ADR 0014).
ROUTE_CLASSIFICATION: dict[str, Literal["action-dispatcher", "bespoke-sensitive"]] = {
    "/api/admin/{action}": "action-dispatcher",
    # Room names become tool output an agent reads, so replacing them is gated like a sensitive
    # action: a fresh assertion bound to the file's hash, and a human session in local mode.
    "/api/admin/rooms/upload": "bespoke-sensitive",
    # A bundle can rewrite the policy and every connector's OAuth client; local mode only.
    "/api/admin/org_config/upload": "bespoke-sensitive",
}

# What the room directory upload accepts, in bytes.
MAX_ROOMS_UPLOAD_BYTES = 2 * 1024 * 1024

# The sections only an organization server has.
_ORG_STATE_KEYS = ("download_delivery", "web_push", "authz", "step_up", "scoped_tokens", "audit_forwarding")


_ADMIN_PAGE_TITLE = "PrivacyFence — Admin"


def _admin_page_body(
    state: dict[str, Any], *, mode: Mode, is_admin: bool, csrf: str, nonce: str,
) -> str:
    """The page's markup: the shared renderer on the admin surface, the WebAuthn helpers a
    sensitive action's passkey prompt needs, and the bridge shim that posts to ``/api/admin``."""
    body = settings_window_html.build_html(
        state, nonce=nonce, initial_section="services", mode=mode, is_admin=is_admin, surface="admin",
    )
    body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
    return body + _settings_bridge_shim(csrf=csrf, repo_url=REPO_URL, nonce=nonce, endpoint="/api/admin")


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _restart_needed(admin: dict[str, Any], startup_admin: dict[str, Any]) -> list[str]:
    """Labels of the restart-only sections whose stored value is not the one the server started
    with. A section the startup copy does not have cannot be compared and is left out."""
    changed: set[str] = set()
    for key in RESTART_LABELS:
        if key == "auto_provision":
            continue
        if key in startup_admin and admin.get(key) != startup_admin[key]:
            changed.add(key)
    started = startup_admin.get("services", {})
    stored = admin.get("services", {})
    for name in ("peek", "grist"):
        before = started.get(name, {}).get("auto_provision") if isinstance(started.get(name), dict) else None
        now = stored.get(name, {}).get("auto_provision") if isinstance(stored.get(name), dict) else None
        if before is not None and now != before:
            changed.add("auto_provision")
    return [label for key, label in RESTART_LABELS.items() if key in changed]


def _privacy_rows(install_wide_settings: dict[str, Any]) -> dict[str, Any]:
    """The install-wide privacy policy as the page draws it. A group the file does not mention
    reads as ``block``, the fail-closed default an organization server enforces."""
    privacy = _privacy_state_from_config(install_wide_settings, fail_safe_default="block")
    # Calendar's free/busy switch and the Gmail signature are one desktop's own settings; an
    # organization server has neither, so they are not drawn rather than drawn to 404.
    privacy["groups"] = [g for g in privacy["groups"] if g["key"] != "calendar"]
    privacy.pop("gmail_append_signature", None)
    # The PII Detection Gate's switches are part of the same install-wide policy.
    privacy.update(_pii_general_fields(install_wide_settings))
    return privacy


def _single_user(bundle: dict[str, Any]) -> bool:
    """Whether the server is in single-user mode; the local Admin page never is."""
    return org_mode.is_single_user(bundle)


def _admin_state(
    *, mode: Mode, bundle: dict[str, Any], org_config: dict[str, Any], startup_admin: dict[str, Any],
    principal: Principal, install_wide_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the Admin page renders from. It carries no secret: service rows hold a client id at
    most, and audit forwarding carries the name of its bearer-token variable, never the value.

    An organization server also reports its install-wide privacy policy (``install_wide_settings``,
    when the caller has it) and the usage stats counted from every principal's files."""
    admin = admin_config.config_for(bundle, mode=mode, single_user=_single_user(bundle))
    state: dict[str, Any] = {
        "services": admin_config.service_rows(bundle, admin, mode=mode, single_user=_single_user(bundle)),
        "rooms": {"count": len(admin.get("rooms") or []), "synced_at": str(admin.get("rooms_synced_at") or "")},
        "unattended_sessions": admin.get("unattended_sessions", {}),
        "restart_needed": _restart_needed(admin, startup_admin),
        "upgrade_ready": data_format.status(),
    }
    if mode == "org":
        for key in _ORG_STATE_KEYS:
            state[key] = admin.get(key, {})
        state["relay_status"] = relay_client.status()
        if install_wide_settings is not None:
            state["privacy"] = _privacy_rows(install_wide_settings)
        state["stats"] = org_stats.collect(oauth_apps.snapshot(org_config))
    return state


def _dispatch(
    principal: Principal, action: str, body: dict[str, Any], *,
    mode: Mode, bundle: dict[str, Any], org_config: dict[str, Any], startup_admin: dict[str, Any],
    step_up: StepUpConfig | None, step_up_origin: str, challenges: StepUpChallengeStore,
    refresh_live: Callable[[list[str]], None],
    state_extra: Callable[[], dict[str, Any]] | None = None,
    install_wide_settings: dict[str, Any] | None = None, install_wide_settings_path: str = "",
) -> Response:
    """Everything after the caller has authenticated the principal and checked CSRF and origin.

    ``body`` is the request's JSON object with ``csrf`` already removed. ``state_extra`` adds
    what only one mode's page state has. ``install_wide_settings`` and its file's path are what
    the privacy actions rewrite."""
    spec = ADMIN_ACTIONS.get(action)
    if spec is None or mode not in spec.modes:
        return _error("Unknown action.", 404)
    declared = ADMIN_PARAMS[action]
    unknown = sorted(set(body) - {name for name, _, _ in declared} - _ENVELOPE_KEYS)
    if unknown:
        return _error(f"Unexpected field: {unknown[0]}.")
    params: dict[str, Any] = {}
    for name, kind, required in declared:
        if name not in body:
            if required:
                return _error(f"Missing {name}.")
            continue
        try:
            params[name] = _coerce(body[name], kind)
        except _BadAction as exc:
            return _error(f"{name}: {exc}.")
    assertion = body.get("webauthn_assertion")
    refusal = _apply_step_up_gate(
        principal, action, body, assertion if isinstance(assertion, dict) else None,
        step_up=step_up, step_up_origin=step_up_origin, challenges=challenges,
        sensitive=spec.sensitive, always=True,
    )
    if refusal is not None:
        with principal_scope(principal):
            _record_settings_audit(principal, f"Step-up required for {action!r}, refused (principal={principal.id})")
        return refusal
    if action in _PRIVACY_ACTIONS:
        try:
            summary = _apply_privacy_action(
                action, params, install_wide_settings if install_wide_settings is not None else {},
                install_wide_settings_path,
            )
        except org_install_policy.PolicyChangeRejected as exc:
            return _error(str(exc))
        except OSError as exc:
            # Writing settings.yaml failed, so nothing was applied: the policy is unchanged.
            logger.error("Could not write the install-wide settings.yaml: %s", exc)
            return _error("could not write settings.yaml", 500)
        changed: list[str] = []
    else:
        try:
            summary, changed = admin_config.apply_change(
                action, params, bundle=bundle, mode=mode, single_user=_single_user(bundle),
            )
        except admin_config.AdminConfigError as exc:
            return _error(str(exc))
        except OSError as exc:
            logger.error("Could not save the admin configuration: %s", exc)
            return _error("Could not save the admin configuration.", 500)
    if summary:
        who = f"admin={principal.id}" if mode == "org" else "principal=local"
        with principal_scope(principal):
            _record_settings_audit(principal, f"{summary} ({who})")
        if spec.applies == "live":
            refresh_live(changed)
    return JSONResponse({
        **_admin_state(
            mode=mode, bundle=bundle, org_config=org_config, startup_admin=startup_admin, principal=principal,
            install_wide_settings=install_wide_settings,
        ),
        **(state_extra() if state_extra is not None else {}),
    })


def _current_pii_flag(action: str, params: dict[str, Any], settings: dict[str, Any]) -> bool:
    """toggle_pii_detection and toggle_pii_category flip the *current* value, as the menu toggle
    in local mode does, and as the shared page's switches post (no value in the body).
    ``org_install_policy.apply_change`` itself takes the wanted value, which suits a form that
    could be submitted stale or twice but not this control; working out the flip here keeps the
    page unaware of the difference."""
    pii_cfg = settings.get("pii_detection", {}) or {}
    if action == "toggle_pii_detection":
        return bool(pii_cfg.get("enabled", True))
    return bool(pii_cfg.get(str(params.get("category_key", "")), True))


def _apply_privacy_action(
    action: str, params: dict[str, Any], settings: dict[str, Any], settings_path: str,
) -> str:
    """Carries out one install-wide privacy action and returns its audit summary. May raise
    ``org_install_policy.PolicyChangeRejected`` (a group, category or policy this install does not
    have) or ``OSError`` (settings.yaml could not be written; nothing was applied)."""
    payload = dict(params)
    if action in ("toggle_pii_detection", "toggle_pii_category"):
        payload["enabled"] = not _current_pii_flag(action, params, settings)
    return org_install_policy.apply_change(settings, settings_path, action=action, payload=payload)


async def _replace_rooms_from_upload(
    form: Any, principal: Principal, *,  # noqa: ANN401 -- starlette's FormData
    mode: Mode, bundle: dict[str, Any], step_up: StepUpConfig | None, step_up_origin: str,
    challenges: StepUpChallengeStore, refresh_live: Callable[[list[str]], None],
    state: Callable[[], dict[str, Any]],
) -> Response:
    """Everything after the caller has authenticated the principal and checked CSRF, origin and,
    in local mode, the human session: size, parse, step-up bound to the file's hash, replace."""
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _error("Missing file.")
    raw = await upload.read(MAX_ROOMS_UPLOAD_BYTES + 1)
    if len(raw) > MAX_ROOMS_UPLOAD_BYTES:
        return _error("The file is larger than 2 MB.")
    try:
        data = json.loads(raw)
    except ValueError:
        # Not JSON at all: replace_rooms words the refusal for anything that is not a room file.
        data = None
        try:
            admin_config.replace_rooms(data, bundle=bundle, mode=mode, single_user=_single_user(bundle))
        except admin_config.AdminConfigError as exc:
            return _error(str(exc))
    refusal = _gate_upload(
        "upload_rooms", {"sha256": hashlib.sha256(raw).hexdigest()},
        _parse_form_assertion(form.get("webauthn_assertion")),
        principal=principal, step_up=step_up, step_up_origin=step_up_origin, challenges=challenges, always=True,
    )
    if refusal is not None:
        with principal_scope(principal):
            _record_settings_audit(principal, f"Step-up required for 'upload_rooms', refused (principal={principal.id})")
        return refusal
    try:
        summary = admin_config.replace_rooms(data, bundle=bundle, mode=mode, single_user=_single_user(bundle))
    except admin_config.AdminConfigError as exc:
        return _error(str(exc))
    except OSError as exc:
        logger.error("Could not save the admin configuration: %s", exc)
        return _error("Could not save the admin configuration.", 500)
    who = f"admin={principal.id}" if mode == "org" else "principal=local"
    with principal_scope(principal):
        _record_settings_audit(principal, f"{summary} ({who})")
    refresh_live(["rooms"])
    return JSONResponse(state())


def _check_classified(routes: list[BaseRoute]) -> None:
    for route in routes:
        if isinstance(route, Route) and "POST" in (route.methods or set()) and route.path not in ROUTE_CLASSIFICATION:
            raise RuntimeError(
                f"{route.path} is a new POST route with no ROUTE_CLASSIFICATION entry (ADR 0014) -- "
                "say whether it is an action dispatcher or a bespoke sensitive route, and gate it to match."
            )


def build_org_routes(
    *,
    sessions: org_session.OrgSessionStore,
    step_up: StepUpConfig,
    step_up_origin: str,
    connector_registry: ConnectorRegistry | None,
    org_config: dict[str, Any],
    bundle: dict[str, Any],
    startup_admin: dict[str, Any],
    install_wide_settings: dict[str, Any],
    install_wide_settings_path: str = "",
) -> list[BaseRoute]:
    """The Admin API for an organization server. ``org_config`` is the shared effective config
    every long-lived holder reads; a live change refreshes it in place.

    ``install_wide_settings`` is the server's live settings.yaml dict and
    ``install_wide_settings_path`` the file it was loaded from: the privacy actions rewrite both.
    Without a path they are refused, so a caller that only reads cannot overwrite a guessed file."""
    challenges = StepUpChallengeStore()

    def _admin_principal(request: Request) -> Principal | Response:
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return _error("Sign in first.", 401)
        if not principal.is_admin:
            return _error("Only administrators can do this.", 403)
        return principal

    def refresh_live(changed: list[str]) -> None:
        single_user = _single_user(bundle)
        effective = oauth_apps.effective_org_config(
            bundle, admin_config.config_for(bundle, mode="org", single_user=single_user), mode="org",
            single_user=single_user,
        )
        oauth_apps.refresh_in_place(org_config, effective)
        if connector_registry is not None:
            connector_registry.evict_all()
        if "services" in changed:
            relay_client.request_check()

    async def admin_state(request: Request) -> Response:
        principal = _admin_principal(request)
        if isinstance(principal, Response):
            return principal
        return JSONResponse(_admin_state(
            mode="org", bundle=bundle, org_config=org_config, startup_admin=startup_admin, principal=principal,
            install_wide_settings=install_wide_settings,
        ))

    async def admin_page(request: Request) -> Response:
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return RedirectResponse("/login?next=/admin", status_code=302, headers={"Cache-Control": "no-store"})
        if not principal.is_admin:
            body = '<section class="pf-settings-error" role="alert"><p>Only administrators can open this page.</p></section>'
            page = web_shell.wrap(
                body, title=_ADMIN_PAGE_TITLE, active="admin", nonce=_csp_nonce_for(request),
                nav_items=web_shell.org_nav_items(False),
                principal_label=principal.email or principal.display_name or principal.id,
                live_updates=False, notifications_enabled=False,
            )
            return HTMLResponse(page, status_code=403, headers={"Cache-Control": "no-store"})
        nonce = _csp_nonce_for(request)
        csrf = request.cookies.get(org_session.SESSION_COOKIE, "")
        state = _admin_state(
            mode="org", bundle=bundle, org_config=org_config, startup_admin=startup_admin, principal=principal,
            install_wide_settings=install_wide_settings,
        )
        html = web_shell.wrap(
            _admin_page_body(state, mode="org", is_admin=True, csrf=csrf, nonce=nonce),
            title=_ADMIN_PAGE_TITLE, active="admin", nonce=nonce,
            nav_items=web_shell.org_nav_items(True),
            principal_label=principal.email or principal.display_name or principal.id,
            live_updates=False, notifications_enabled=False, csrf=csrf,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def admin_action(request: Request) -> Response:
        principal = _admin_principal(request)
        if isinstance(principal, Response):
            return principal
        action = request.path_params["action"]
        if action not in ADMIN_ACTIONS or "org" not in ADMIN_ACTIONS[action].modes:
            return _error("Unknown action.", 404)
        try:
            payload = await request.json()
        except Exception:
            return _error("Invalid JSON body.")
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return _error("unauthorized", 401)
        if not org_session.check_origin(request):
            return _error("Cross-origin request rejected.", 403)
        body = {key: value for key, value in payload.items() if key != "csrf"}
        return _dispatch(
            principal, action, body, mode="org", bundle=bundle, org_config=org_config,
            startup_admin=startup_admin, step_up=step_up, step_up_origin=step_up_origin,
            challenges=challenges, refresh_live=refresh_live,
            install_wide_settings=install_wide_settings, install_wide_settings_path=install_wide_settings_path,
        )

    async def rooms_upload(request: Request) -> Response:
        principal = _admin_principal(request)
        if isinstance(principal, Response):
            return principal
        form = await request.form()
        csrf = form.get("csrf")
        if not isinstance(csrf, str) or not org_session.check_csrf(request, csrf):
            return _error("unauthorized", 401)
        if not org_session.check_origin(request):
            return _error("Cross-origin request rejected.", 403)
        return await _replace_rooms_from_upload(
            form, principal, mode="org", bundle=bundle, step_up=step_up, step_up_origin=step_up_origin,
            challenges=challenges, refresh_live=refresh_live,
            state=lambda: _admin_state(
                mode="org", bundle=bundle, org_config=org_config, startup_admin=startup_admin, principal=principal,
            install_wide_settings=install_wide_settings,
            ),
        )

    routes: list[BaseRoute] = [
        Route("/admin", admin_page),
        Route("/api/admin/state", admin_state),
        Route("/api/admin/rooms/upload", rooms_upload, methods=["POST"]),
        Route("/api/admin/{action}", admin_action, methods=["POST"]),
    ]
    _check_classified(routes)
    return routes


def _local_bundle() -> dict[str, Any]:
    """The installed bundle as it is now. Local mode has no ``OrgAuth`` to hold it, and a bundle
    can be installed while the daemon runs, so each request reads it afresh."""
    from ..daemon_main import load_org_config  # deferred: daemon_main imports the web layer

    try:
        return load_org_config()
    except org_mode.ConfigurationError:
        return {}


def build_routes(
    *,
    controller: SettingsController,
    sessions: LocalSessionStore,
    step_up: StepUpConfig | None,
    step_up_origin: str,
    require_human_session: bool,
    startup_admin: dict[str, Any],
    org_config: dict[str, Any],
) -> list[BaseRoute]:
    """The Admin API for a local install. The caller mounts it behind the owner-only wrapper, as
    it does the settings routes, so only the install owner gets here at all.

    ``org_config`` is the startup dict other principals' connector factories close over: a live
    change refreshes it in place so they see it too, and the owner's connectors are rebuilt
    through the controller."""
    challenges = StepUpChallengeStore()

    def refresh_live(_changed: list[str]) -> None:
        bundle = _local_bundle()
        effective = oauth_apps.effective_org_config(
            bundle, admin_config.config_for(bundle, mode="local", single_user=False), mode="local",
        )
        oauth_apps.refresh_in_place(org_config, effective)
        controller.refresh_connectors()

    def state_extra() -> dict[str, Any]:
        # Set when an installed bundle carries settings this version ignores.
        return {"error": controller.error}

    def state() -> dict[str, Any]:
        return {
            **_admin_state(
                mode="local", bundle=_local_bundle(), org_config=org_config, startup_admin=startup_admin,
                principal=LOCAL_PRINCIPAL,
            ),
            **state_extra(),
        }

    def _human_session_refusal(request: Request, what: str) -> Response | None:
        if require_human_session and not _is_human_session(request, sessions):
            body, status = _human_session_required_json(what)
            return JSONResponse(body, status_code=status)
        return None

    async def admin_state(request: Request) -> Response:
        if not _session_authenticated(request, sessions):
            return _unauthorized_response(request)
        return JSONResponse(state())

    async def admin_page(request: Request) -> Response:
        if not _session_authenticated(request, sessions):
            return _unauthorized_response(request)
        nonce = _csp_nonce_for(request)
        csrf = request.cookies.get(_LOCAL_SESSION_COOKIE, "")
        # Approval notifications follow the same setting as on Settings.
        general = controller.snapshot().get("general", {})
        html = web_shell.wrap(
            _admin_page_body(state(), mode="local", is_admin=False, csrf=csrf, nonce=nonce),
            title=_ADMIN_PAGE_TITLE, active="admin", nonce=nonce,
            notifications_enabled=general.get("notifications_enabled", True),
            notifications_detail=general.get("notifications_detail", "minimal"),
            deprecation_html=web_shell.local_mode_deprecation_html(),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def admin_action(request: Request) -> Response:
        if not _session_authenticated(request, sessions):
            return _unauthorized_response(request)
        action = request.path_params["action"]
        spec = ADMIN_ACTIONS.get(action)
        if spec is None or "local" not in spec.modes:
            return _error("Unknown action.", 404)
        try:
            payload = await request.json()
        except Exception:
            return _error("Invalid JSON body.")
        if not isinstance(payload, dict) or not _local_csrf_matches(request, payload.get("csrf")):
            return _error("unauthorized", 401)
        if not _local_origin_ok(request):
            return _error("Cross-origin request rejected.", 403)
        if spec.sensitive:
            # Before the passkey ceremony, for the reason settings_action gives: a prompt for a
            # change this session cannot make either way is a worse refusal than the refusal.
            refused = _human_session_refusal(request, "change this setting")
            if refused is not None:
                return refused
        body = {key: value for key, value in payload.items() if key != "csrf"}
        return _dispatch(
            LOCAL_PRINCIPAL, action, body, mode="local", bundle=_local_bundle(), org_config=org_config,
            startup_admin=startup_admin, step_up=step_up, step_up_origin=step_up_origin,
            challenges=challenges, refresh_live=refresh_live, state_extra=state_extra,
        )

    async def rooms_upload(request: Request) -> Response:
        if not _session_authenticated(request, sessions):
            return _unauthorized_response(request)
        form = await request.form()
        csrf = form.get("csrf")
        if not isinstance(csrf, str) or not _local_csrf_matches(request, csrf):
            return _error("unauthorized", 401)
        if not _local_origin_ok(request):
            return _error("Cross-origin request rejected.", 403)
        refused = _human_session_refusal(request, "replace the room directory")
        if refused is not None:
            return refused
        return await _replace_rooms_from_upload(
            form, LOCAL_PRINCIPAL, mode="local", bundle=_local_bundle(), step_up=step_up,
            step_up_origin=step_up_origin, challenges=challenges, refresh_live=refresh_live, state=state,
        )

    async def org_config_upload(request: Request) -> Response:
        return await handle_org_config_upload(
            request, controller=controller, sessions=sessions, step_up=step_up, step_up_origin=step_up_origin,
            challenges=challenges, require_human_session=require_human_session, state_builder=state,
            always=True,
        )

    routes: list[BaseRoute] = [
        Route("/admin", admin_page),
        Route("/api/admin/state", admin_state),
        Route("/api/admin/rooms/upload", rooms_upload, methods=["POST"]),
        Route("/api/admin/org_config/upload", org_config_upload, methods=["POST"]),
        Route("/api/admin/{action}", admin_action, methods=["POST"]),
    ]
    _check_classified(routes)
    return routes
