"""Settings on the web (W3/W4):
``GET /settings`` serves settings_window_html.build_html(), wrapped in
web_shell.wrap() so it reads as the same application as ``/approvals``;
``GET /settings/connectors`` serves the identical document with its
Connectors section pre-selected server-side (issue #396 Part C -- the
first-run destination privacyfence_status points an un-onboarded install's
human at, since Connectors is the screen that actually unblocks one; it is
reached through the companion's Open Settings now that the sign-in-link tool
that used to mint a link straight to it is retired);
``POST /api/settings/{action}`` is the mechanical two-thirds of
SettingsController's ~30 actions, dispatched through an **explicit
allowlist** rather than the native dispatcher's bare
``getattr(controller, action)`` -- see _ALLOWED_ACTIONS below for why a
frozenset here, not a decorator on the controller.

Everything that isn't "POST an action, get a fresh snapshot back" gets its
own route instead of being force-fit into that shape: the org
config bundle is a multipart upload, not a JSON action (there is no
osascript "choose file" dialog to trigger from an HTTP request -- see
settings_controller.install_org_config_bytes's own docstring); the audit
log export is a file download, not a JSON response; ``quit_app`` gets its
own route so it can carry §16.2.8's confirmation + local-mode-only gate
without contaminating the generic dispatcher with one action's special
case.

**Standing rule this module exists to keep true (§16.2.4):** no route here
ever calls ``subprocess.run``/``os.system``/``open`` -- the four call sites
that used to (install_org_config's osascript picker, export_audit_log's
``open <file>``, the update-available alert's ``open <url>``,
settings_window.py's ``open_repo``) are each replaced by a route or a
client-side link/window.open, never a shell-out reachable from this
process's HTTP listener. TestNoSubprocessFromHttp in this module's test
file is what a security review gets to point at instead of re-reading this
comment.

**Sensitive-action step-up (#426 Phase 3):** gating the local decide
endpoint on ``step_up.require_passkey`` (web/routes_approvals.py) means
nothing releasing a *write approval* can happen without a passkey -- but an
agent that cannot forge an approval doesn't need to if it can just add an
always-allow rule instead, and this dispatcher's ``POST /api/settings/
{action}`` was that open door: none of its ~30 actions ever asked for
step-up. ``_SENSITIVE_ACTIONS`` below is the subset that can change *what
gets gated* (rule/grant/policy/PII actions) rather than merely how the app
looks or behaves -- update checks, log level, notification detail, and
connector auth all stay ungated, since gating all of them would make
routine settings use miserable for the one real cost (an agent that already
has connector access gaining nothing new by flipping those). With
``step_up.require_passkey`` on, a sensitive action is verified the same
two-round-trip way web/routes_approvals.py's own decide() is: a first POST
with no ``webauthn_assertion`` gets a ``428`` carrying fresh options (or a
``403`` naming ``/security`` if nothing is enrolled -- the same hard fail,
never a silent skip), and a second POST carrying the completed assertion is
verified and, on success, actually runs the action.
``_NON_SENSITIVE_ACTIONS`` is the deliberately explicit complement, not
``_ALLOWED_ACTIONS - _SENSITIVE_ACTIONS`` -- see TestSensitiveActionsCoverAllAllowedActions
in this module's test file for why a derived set would silently swallow a
future action nobody classified either way.

**Self-approval review, Phase 3 (F5/F6):** the sensitive-action net above
only ever covered the generic dispatcher. ``org_config_upload`` had its own
route and bypassed both ``_needs_step_up`` and ``require_human_session``
entirely, even though an uploaded bundle can rewrite the PII policy, every
auto-accept rule and every connector's OAuth client config in one shot
(F5) -- it's now gated the same two ways, by hand, inside its own route
function, plus an explicit ``confirm_pin`` round trip before a first signed
bundle's key gets pinned (``SettingsController.would_pin_new_org_signing_
key``), rather than letting that TOFU pin happen as a side effect of an
upload. ``toggle_connector`` was classified non-sensitive in both
directions, but the classification comment's own reasoning -- "an agent
that already has connector access gains nothing new" -- only holds for
*disabling* one; it's now split into ``enable_connector`` (sensitive) and
``disable_connector`` (not), see ``SettingsController.enable_connector``'s
own docstring (F6). ``_BESPOKE_SENSITIVE_ROUTE_PATHS``/``_BESPOKE_EXEMPT_
ROUTE_PATHS`` below widen ``TestSensitiveActionsCoverAllAllowedActions``'s
own ratchet from action names to actual ``Route`` objects, so the next
route added here can't repeat org_config_upload's mistake by existing.

**B9:** ``enable_step_up`` (SettingsController's own new method) is the one
``_ALLOWED_ACTIONS`` entry that can turn ``step_up.require_passkey`` on in
the first place -- previously only a hand edit of ``config/settings.yaml``
plus a daemon restart could. It's listed in ``_SENSITIVE_ACTIONS`` for the
same reason every rule/grant/policy/PII action is, but ``_needs_step_up``
below never gates its own *first* call: that check only fires once
``step_up.enabled``/``require_passkey`` are already both true, which by
definition isn't the case yet the first time this action runs. See
SettingsController.enable_step_up's own docstring for what does gate it
(an already-enrolled passkey) and step_up_config.py's ``LiveStepUpConfig``
for how the change reaches this dispatcher's own ``step_up`` without a
restart.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import typing
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .. import approval_icons, settings_window_html, webauthn_stepup, web_shell
from ..principal import LOCAL_PRINCIPAL
from ..settings_controller import REPO_URL, SettingsController
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore, WebAuthnError
from . import step_up_decide
from .csp import nonce_for as _csp_nonce_for
from .routes_security import PF_WEBAUTHN_JS
from .session_auth import SESSION_COOKIE as _SESSION_COOKIE
from .session_auth import LocalSessionStore
from .session_auth import authenticated as _session_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import human_session_required_json as _human_session_required_json
from .session_auth import is_human_session as _is_human_session
from .session_auth import unauthorized_html as _unauthorized_response

logger = logging.getLogger(__name__)

# Max upload size for an organization config bundle -- generous for a JSON
# document that is, in practice, a handful of OAuth client IDs/secrets per
# service (org_config.json today runs well under 10KB), while still a real
# bound against something absurd arriving on this endpoint.
MAX_ORG_CONFIG_BYTES = 1_000_000

# ---------------------------------------------------------------------------- #
# §16.2.5's allowlist. A frozenset here, not a @web_action decorator on
# SettingsController -- that class's docstring is proud of having no web
# concerns ("No unguarded AppKit/WebKit imports at module level"), and a
# decorator naming an HTTP-shaped concept would be exactly that. This list
# is every "mechanical" action from §16.4's own table, plus skip_update/
# remind_later_update (§16.2.4's update banner). install_org_config,
# export_audit_log, and quit_app are deliberately absent -- each has its
# own route below instead of going through generic dispatch (see module
# docstring); install_org_config_bytes is never web-reachable by this name
# at all (only the multipart upload route calls it directly, with real
# bytes no JSON body could carry).
# ---------------------------------------------------------------------------- #

_ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "toggle_pii_detection", "toggle_pii_category",
    "toggle_update_check", "toggle_update_check_beta", "check_for_updates_now",
    "skip_update", "remind_later_update",
    "enable_connector", "disable_connector", "refresh_connectors", "authenticate_connector",
    "telegram_start_auth", "telegram_submit_code", "telegram_submit_2fa", "telegram_cancel_auth",
    "update_rule_row", "add_rule_row", "remove_rule_row",
    "toggle_grant_capability", "add_grant_row", "update_grant_row", "remove_grant_row",
    "set_default_policy", "set_category_policy", "toggle_calendar_free_busy",
    "set_log_level", "set_notifications_detail", "enable_step_up",
})

# ---------------------------------------------------------------------------- #
# #426 Phase 3's own allowlist-within-the-allowlist -- see module docstring.
# Every rule-row/grant/policy/PII action can change *what a future write
# reaches decide() at all* (an always-allow rule, a granted capability, a
# relaxed default policy), which is exactly the bypass a passkey requirement
# on decide() alone leaves open; the remainder is either read-only, informs
# no policy decision (update checks, log level, notification detail), or is
# connector auth an agent with connector access already has no need to
# forge. Both sets are explicit, not derived from each other, so
# TestSensitiveActionsCoverAllAllowedActions below fails the moment a new
# action lands in _ALLOWED_ACTIONS without a matching entry in either.
# ---------------------------------------------------------------------------- #

_SENSITIVE_ACTIONS: frozenset[str] = frozenset({
    "update_rule_row", "add_rule_row", "remove_rule_row",
    "toggle_grant_capability", "add_grant_row", "update_grant_row", "remove_grant_row",
    "set_default_policy", "set_category_policy", "toggle_calendar_free_busy",
    "toggle_pii_detection", "toggle_pii_category",
    # B9: changes *what gets gated* the same way every other entry here
    # does -- once step-up is already required, turning it on again (a
    # no-op SettingsController.enable_step_up already tolerates) still
    # demands a fresh assertion like any other sensitive action. The very
    # first enable is never gated this way -- _needs_step_up below only
    # fires once step_up.enabled and require_passkey are *already* both
    # true, which by definition isn't the case yet on that first call; its
    # own precondition (a passkey enrolled) is what SettingsController.
    # enable_step_up itself enforces instead. See that method's own
    # docstring.
    "enable_step_up",
    # F6 of the self-approval review: re-enabling a connector a human
    # deliberately switched off is access an agent did not already have
    # -- unlike disable_connector below, this is not a no-op change of
    # nothing.
    "enable_connector",
})

_NON_SENSITIVE_ACTIONS: frozenset[str] = frozenset({
    "toggle_update_check", "toggle_update_check_beta", "check_for_updates_now",
    "skip_update", "remind_later_update",
    # F6: an agent that already has connector access gains nothing new by
    # disabling one -- see SettingsController.enable_connector's own
    # docstring for the asymmetry with the sensitive direction above.
    "disable_connector", "refresh_connectors", "authenticate_connector",
    "telegram_start_auth", "telegram_submit_code", "telegram_submit_2fa", "telegram_cancel_auth",
    "set_log_level", "set_notifications_detail",
})

# ---------------------------------------------------------------------------- #
# F5/3.3 of the self-approval review: _SENSITIVE_ACTIONS/_NON_SENSITIVE_ACTIONS
# above only ever covered the generic POST /api/settings/{action}
# dispatcher -- org_config_upload had a route of its own (module docstring's
# own list of why: a multipart upload, not a JSON action) and, for that
# reason alone, never passed through either _needs_step_up or
# require_human_session at all, regardless of how much policy an uploaded
# bundle could rewrite (F5). Every path in _BESPOKE_SENSITIVE_ROUTE_PATHS is
# wired through the same _needs_step_up-shaped/require_human_session-shaped
# gates as a _SENSITIVE_ACTIONS action, by hand, inside its own route
# function -- request shapes differ too much (multipart vs. JSON) to share
# settings_action's own dispatch loop.
#
# Two things consume these sets, deliberately both: build_routes() below
# asserts every bespoke POST route it constructs is in one of them --
# a real, load-bearing invariant checked every time this app is built, not
# only under pytest -- and TestBespokeRoutesAreClassified re-asserts the
# same thing against the actual Route objects it gets back, as a named,
# always-collected regression test rather than only an assert a test run
# could otherwise skip past. Either one alone would leave a future bespoke
# POST route free to land unclassified -- the assert here catches it at
# runtime (this call already raises on an unclassified path, before the app
# ever serves it), the test catches it at review/CI time -- the same way
# TestSensitiveActionsCoverAllAllowedActions already fails the moment a new
# _ALLOWED_ACTIONS entry lands unclassified.
# ---------------------------------------------------------------------------- #

_BESPOKE_SENSITIVE_ROUTE_PATHS: frozenset[str] = frozenset({
    "/api/settings/org_config/upload",
})

# Every other bespoke (non-generic-dispatch) route, and why it doesn't need
# the same gates: quit_app carries its own §16.2.8 confirmation dialog and
# doesn't change what gets gated at all; the rest are GETs, not mutations.
_BESPOKE_EXEMPT_ROUTE_PATHS: dict[str, str] = {
    "/api/settings/quit_app": "its own confirmed=true gate (§16.2.8) -- doesn't change what gets gated",
    "/api/settings/audit_log/download": "a GET -- read-only export, no mutation",
    "/settings": "a GET -- renders the page",
    "/settings/connectors": "a GET -- renders the page",
}


class _BadAction(Exception):
    """Raised by _call_action for a wrong-typed/missing argument -- mapped
    to a 400 at the route, never a 500 (§16.7: "a wrong-typed argument
    returns 400 rather than raising")."""


def _coerce(value: Any, annotation: Any) -> Any:
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise _BadAction("expected an integer")
        try:
            return int(value)
        except ValueError as exc:
            raise _BadAction("expected an integer") from exc
    if annotation is str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise _BadAction("expected a string")
        return value
    if annotation is bool:
        return bool(value)
    return value


def _call_action(controller: SettingsController, action: str, payload: dict[str, Any]) -> Any:
    """Real per-action argument validation (§16.2.5's own "not a copy of
    the pyobjc workaround"): every parameter's type comes from
    SettingsController's own annotations (update_rule_row(op_key: str, idx:
    int, ...), etc.) rather than a single hardcoded "idx is always an int"
    special case -- a wrong type on *any* parameter of *any* allowed action
    is rejected the same way, not just the one the native dispatcher
    happened to guard."""
    method = getattr(controller, action)
    sig = inspect.signature(method)
    # settings_controller.py is `from __future__ import annotations`, so
    # Signature.parameters[name].annotation is the *string* "int"/"str",
    # not the type object -- get_type_hints() is what actually resolves
    # those against the function's own module globals, the way this
    # module's real per-action validation needs (§16.2.5: "not a copy of
    # the pyobjc workaround").
    hints = typing.get_type_hints(method)
    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name not in payload:
            if param.default is inspect.Parameter.empty:
                raise _BadAction(f"missing required argument: {name}")
            continue
        kwargs[name] = _coerce(payload[name], hints.get(name))
    return method(**kwargs)


def _augment_connectors_with_icons(state: dict[str, Any]) -> dict[str, Any]:
    """§16.2.6: the web equivalent of settings_window.py's
    _augment_connectors_with_icons, ported onto approval_icons.py (P1's
    PyObjC-free icon loader, already serving the approval card) instead of
    approval_window's AppKit-tainted private functions -- SettingsController
    itself stays free of any icon-loading concern either way."""
    for connector in state.get("connectors", []):
        icon_path = approval_icons.connector_icon_path(connector.get("icon", ""))
        connector["icon_data_uri"] = approval_icons.icon_data_uri(icon_path)
    return state


def _snapshot(controller: SettingsController) -> dict[str, Any]:
    return _augment_connectors_with_icons(controller.snapshot())


# ---------------------------------------------------------------------------- #
# The bridge shim -- swaps settings_window_html.py's own
# ``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``
# for a ``fetch()`` POST to /api/settings/<action>, the same technique
# web/routes_approvals.py's own _bridge_shim already uses for the approval
# card (see that module's docstring). Four actions are intercepted here
# instead of forwarded, because none of them is "POST an action, get a
# snapshot back" (§16.2.4):
#   - open_repo: a plain link, opened client-side -- no request at all.
#   - install_org_config: triggers the hidden <input type=file> below,
#     which itself POSTs a multipart body to /api/settings/org_config/upload.
#   - export_audit_log: a same-origin navigation to the download route.
#   - quit_app: a client-side confirm() first (§16.2.8), then still POSTed
#     through as an action, but to its own /api/settings/quit_app route
#     (see build_routes below) rather than the generic dispatcher.
# ---------------------------------------------------------------------------- #

def _settings_bridge_shim(*, csrf: str, repo_url: str, nonce: str) -> str:
    """``pfSettingsPost``'s own ``428``/``403`` branches (#426 Phase 3) are
    the settings-page counterpart of web/routes_approvals.py's own
    ``_bridge_shim`` step-up handling -- see that function's docstring for
    the shared shape (``428`` carries fresh ``webauthn_options`` to complete
    with ``window.pfWebauthnGet``, defined by ``PF_WEBAUTHN_JS``, and retry;
    a ``403`` names ``/security`` because nothing is enrolled at all). The
    one real difference: this page stays open across the ceremony (it is
    not a one-shot card), so failures surface via ``window.alert`` rather
    than replacing the whole document's markup."""
    return (
        "<input type=\"file\" id=\"pf-org-config-input\" accept=\".json,application/json\" style=\"display:none\">"
        f'<script nonce="{nonce}">(function(){{'
        f"var CSRF = {csrf!r};"
        "var fileInput = document.getElementById('pf-org-config-input');"
        "fileInput.addEventListener('change', function(){"
        "  if (!fileInput.files || !fileInput.files[0]) return;"
        "  var file = fileInput.files[0];"
        # F5/3.1: org_config_upload can now answer 409 (would pin a new
        # signing key -- ask first) and 428/403 (step-up, same shape
        # pfSettingsPost's own retry chain below already handles for JSON
        # actions) instead of only ever succeeding or 401/400/403-on-
        # cross-origin. attemptOrgUpload() re-POSTs the same file as a
        # fresh FormData each round -- multipart has no way to resume a
        # partially-approved request the way a JSON body's own retry does
        # by copying `body`.
        "  function attemptOrgUpload(confirmPin, assertion) {"
        "    var fd = new FormData();"
        "    fd.append('file', file);"
        "    fd.append('csrf', CSRF);"
        "    if (confirmPin) { fd.append('confirm_pin', 'true'); }"
        "    if (assertion) { fd.append('webauthn_assertion', JSON.stringify(assertion)); }"
        "    return fetch('/api/settings/org_config/upload', {method:'POST', credentials:'same-origin', body: fd})"
        "      .then(function(r){"
        "        if (r.status === 409) {"
        "          return r.json().then(function(data){"
        "            if (data.error === 'pin_confirmation_required' && window.confirm(data.detail + ' Continue?')) {"
        "              return attemptOrgUpload(true, assertion);"
        "            }"
        "            return null;"
        "          });"
        "        }"
        "        if (r.status === 428) {"
        "          return r.json().then(function(data){"
        "            if (data.webauthn_options && window.PublicKeyCredential) {"
        "              return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(newAssertion){"
        "                return attemptOrgUpload(confirmPin, newAssertion);"
        "              }).catch(function(err){"
        "                window.alert('This change needs your passkey, and the prompt failed: ' + err.message);"
        "                return null;"
        "              });"
        "            }"
        "            window.alert('This change needs a passkey, and none is available in this browser.');"
        "            return null;"
        "          });"
        "        }"
        "        if (r.status === 403) {"
        "          return r.json().then(function(data){"
        "            if (data.enroll_url) {"
        "              window.alert('This change requires a passkey. Set one up at ' + data.enroll_url + '.');"
        "            }"
        "            return null;"
        "          });"
        "        }"
        "        return r.json();"
        "      });"
        "  }"
        "  attemptOrgUpload(false, null).then(function(state){"
        "    if (state && window.__pfRender) { window.__pfRender(state); }"
        "  }).finally(function(){ fileInput.value = ''; });"
        "});"
        "function pfSettingsPost(url, body) {"
        "  return fetch(url, {method:'POST', credentials:'same-origin',"
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});"
        "}"
        "window.webkit = window.webkit || {};"
        "window.webkit.messageHandlers = window.webkit.messageHandlers || {};"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        "  var action = payload.action; var rest = Object.assign({}, payload); delete rest.action;"
        f"  if (action === 'open_repo') {{ window.open({repo_url!r}, '_blank', 'noopener'); return; }}"
        "  if (action === 'install_org_config') { fileInput.click(); return; }"
        "  if (action === 'export_audit_log') { window.location = '/api/settings/audit_log/download'; return; }"
        "  var url = '/api/settings/' + encodeURIComponent(action);"
        "  if (action === 'quit_app') {"
        "    if (!window.confirm('Quit PrivacyFence? This stops the daemon, including every open approval and settings page.')) { return; }"
        "    rest.confirmed = true;"
        "  }"
        "  var body = Object.assign({}, rest, {csrf: CSRF});"
        "  pfSettingsPost(url, body).then(function(r){"
        "    if (r.status === 428) {"
        "      return r.json().then(function(data){"
        "        if (data.webauthn_options && window.PublicKeyCredential) {"
        "          return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(assertion){"
        "            var retryBody = Object.assign({}, body, {webauthn_assertion: assertion});"
        "            return pfSettingsPost(url, retryBody);"
        "          }).catch(function(err){"
        "            window.alert('This change needs your passkey, and the prompt failed: ' + err.message);"
        "            return null;"
        "          });"
        "        }"
        "        window.alert('This change needs a passkey, and none is available in this browser.');"
        "        return null;"
        "      });"
        "    }"
        "    if (r.status === 403) {"
        "      return r.json().then(function(data){"
        "        if (data.enroll_url) {"
        "          window.alert('This change requires a passkey. Set one up at ' + data.enroll_url + '.');"
        "        }"
        "        return null;"
        "      });"
        "    }"
        "    return r;"
        "  }).then(function(r){"
        "    if (r === null) { return; }"
        "    return r.json().then(function(state){ return {ok: r.ok, state: state}; });"
        "  }).then(function(res){"
        "    if (!res) { return; }"
        "    if (res.ok && window.__pfRender) { window.__pfRender(res.state); } "
        "else if (!res.ok) { console.error('PrivacyFence action failed:', action, res.state); } });"
        "}};"
        "})();</script>"
    )


def _is_confirmed(value: Any) -> bool:
    """A multipart form field is always a string (or absent) -- there is
    no JSON `true` to compare against the way quit_app's own `confirmed`
    check gets to. Mirrors that check's intent: an explicit, affirmative
    value only, never merely "present" (an empty string, e.g. a
    same-named but unchecked form control, is not consent)."""
    return isinstance(value, str) and value.lower() in ("true", "1", "on")


def _parse_form_assertion(value: Any) -> dict[str, Any] | None:
    """org_config_upload's webauthn_assertion arrives as a JSON-encoded
    string form field (multipart has no native nested-object type the way
    the generic dispatcher's JSON body does) -- parsed the same
    permissive way settings_action's own JSON body already handles a
    malformed/missing assertion: fall through to "no assertion", not a
    500."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _action_fingerprint(action: str, body: dict[str, Any]) -> str:
    """The settings-action counterpart of webauthn_stepup.decision_
    fingerprint -- binds a step-up ceremony to *this exact action and
    payload* rather than merely "some sensitive action", so a WebAuthn
    assertion obtained for e.g. ``remove_grant_row`` can't be replayed to
    authorize a differently-shaped ``add_rule_row`` (or the same action with
    different arguments). ``body`` must already have ``csrf``/
    ``webauthn_assertion`` stripped -- neither is part of what a human
    approved by completing the ceremony, and including the assertion itself
    would make the first (options-only) and second (assertion-carrying)
    request's own fingerprints diverge."""
    payload = f"{action}|{json.dumps(body, sort_keys=True, default=str)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_routes(
    controller: SettingsController,
    *,
    sessions: LocalSessionStore,
    allow_quit: bool = True,
    notifications_enabled: bool = True,
    notifications_detail: str = "minimal",
    step_up: StepUpConfig | None = None,
    step_up_origin: str = "",
    require_human_session: bool = False,
) -> list[BaseRoute]:
    """The Route objects themselves, for server.py to fold into the one
    combined app (extra_routes, same pattern web/routes_mcp.py's
    mount_mcp() already established) -- see create_app() below for a
    standalone Starlette app wrapping the same routes, which is what this
    module's own tests construct against. ``sessions`` (SEC-06, see
    session_auth.py's own module docstring) is the same session store
    web/routes_approvals.py authenticates against -- one session for the
    whole combined app, per this module's own docstring.

    A successful mutation's own snapshot is returned directly in this
    request's response, *and* reaches every other open tab via
    web/state_stream.py's StateStream.push_settings, wired as a
    controller.add_change_listener by web/server.py's WebServer -- see that
    class's own docstring. Nothing here needs to know about the stream at
    all; SettingsController's existing on_change/_push_snapshot mechanism
    already fires for every mutating call, this request's own included.

    ``step_up``/``step_up_origin`` (#426 Phase 3) gate ``_SENSITIVE_ACTIONS``
    on a fresh WebAuthn assertion whenever ``step_up.require_passkey`` is on
    -- see module docstring. Both default to "off" so every existing caller
    of this function is unaffected; web/server.py's ``build_app`` passes the
    same ``StepUpConfig``/origin it already resolves for web/
    routes_approvals.py's own decide-time check and web/routes_security.py's
    ``/security`` mount.

    ``require_human_session`` (the self-approval plan's Phase 2) refuses
    every ``_SENSITIVE_ACTIONS`` action -- the same set ``_needs_step_up``
    already names, i.e. everything that can change *what gets gated* -- to a
    session web/session_auth.py cannot attribute to a person. Unlike
    ``_needs_step_up`` it does not wait on ``step_up.require_passkey``: an
    install with no passkey requirement still has a policy an agent should
    not be able to rewrite on its own say-so. See web/routes_approvals.py's
    own ``require_human_session`` paragraph for why web/server.py turns this
    on for privilege-separated installs only.
    """
    challenges = StepUpChallengeStore()

    def _authenticated(request: Request) -> bool:
        return _session_authenticated(request, sessions)

    def _banner_html() -> str | None:
        if step_up is None:
            return None
        # #426 Phase 4: the persistent "requirement was turned off" notice
        # (webauthn_stepup.step_up_disabled_notice) stands alongside the
        # Phase 3 "nothing enrolled yet" one -- either, both, or neither
        # can be true at once, so both render together when present.
        parts = [
            step_up.local_enrollment_banner(has_credentials=webauthn_stepup.has_credentials(LOCAL_PRINCIPAL)),
            webauthn_stepup.step_up_disabled_notice(LOCAL_PRINCIPAL),
        ]
        parts = [p for p in parts if p]
        return " ".join(parts) if parts else None

    async def _render_settings_page(request: Request, *, initial_section: str | None) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        # SEC-08: one nonce
        # for the whole document -- web/server.py's _SecurityHeadersMiddleware
        # already put one in request.state for this exact response, and
        # every <style>/<script> tag below (settings_window_html.build_html's
        # own, this page's bridge shim, and web_shell.wrap's shell chrome)
        # has to carry it for the matching Content-Security-Policy header
        # to actually allow any of them.
        nonce = _csp_nonce_for(request)
        state = _snapshot(controller)
        body = settings_window_html.build_html(state, nonce=nonce, initial_section=initial_section)
        csrf = request.cookies.get(_SESSION_COOKIE, "")
        # PF_WEBAUTHN_JS (#426 Phase 3): the same ceremony helpers web/
        # routes_approvals.py's card page carries, needed here whenever a
        # sensitive action's own 428/403 branch below (_settings_bridge_shim)
        # has to run one -- always injected, same reasoning that module's
        # own docstring gives for TestStepUpBridgeShim: whether it's ever
        # exercised depends on config, not on whether the helpers exist.
        body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
        body += _settings_bridge_shim(csrf=csrf, repo_url=REPO_URL, nonce=nonce)
        # Read off this request's own fresh snapshot, not the notifications_
        # enabled/detail closure args above -- those are only the daemon-
        # startup defaults (server.py's own initial config read), and the
        # Approval Notifications card's segmented control
        # (set_notifications_detail) needs an edit to reach the very next
        # render of this same page without restarting the daemon.
        general = state.get("general", {})
        html = web_shell.wrap(
            body, title="PrivacyFence — Settings", active="settings", nonce=nonce,
            notifications_enabled=general.get("notifications_enabled", notifications_enabled),
            notifications_detail=general.get("notifications_detail", notifications_detail),
            banner_html=_banner_html(),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def settings_page(request: Request) -> Response:
        return await _render_settings_page(request, initial_section=None)

    async def settings_connectors_page(request: Request) -> Response:
        # issue #396 Part C: a real route (not a #fragment) so the initial
        # section survives web/server.py's _BootstrapMiddleware, which
        # redirects a consumed ?bootstrap= code to `request.url.path` with
        # its query string stripped but the path itself untouched -- see
        # that middleware's own docstring. This is the screen an
        # un-onboarded install's human is sent to -- by privacyfence_status's
        # own message, and by the companion's Open Settings item.
        return await _render_settings_page(request, initial_section="connectors")

    def _check_mutation(request: Request, payload: Any) -> Response | None:
        if not isinstance(payload, dict) or not _csrf_matches(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    def _settings_step_up_response(action: str, fingerprint_body: dict[str, Any]) -> JSONResponse:
        """The settings-action counterpart of web/routes_approvals.py's own
        ``_step_up_response`` -- with no enrolled passkey this hard-fails
        (``403``) rather than falling through unguarded, since this gate
        only ever runs when ``step_up.require_passkey`` is already on (see
        ``_needs_step_up`` below); there is no Phase-2-style "let it through"
        configuration to fall back to here."""
        assert step_up is not None  # nosec B101  # _needs_step_up() already proved this before calling us
        fingerprint = _action_fingerprint(action, fingerprint_body)
        options_json = step_up_decide.begin_step_up(
            LOCAL_PRINCIPAL, rp_id=step_up.rp_id, subject_key=action, fingerprint=fingerprint, challenges=challenges,
        )
        if options_json is None:
            return JSONResponse(
                {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
            )
        return JSONResponse(
            {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
        )

    def _needs_step_up(action: str) -> bool:
        return step_up is not None and step_up.enabled and step_up.require_passkey and action in _SENSITIVE_ACTIONS

    async def settings_action(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        action = request.path_params["action"]
        # The allowlist check happens *before* anything resembling
        # getattr(controller, action) runs -- an unlisted name (including
        # dunders, _load_config, snapshot itself) is a 404, not a lookup
        # that then gets rejected (§16.2.5/§16.7's own required test).
        if action not in _ALLOWED_ACTIONS:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_mutation(request, payload)
        if rejected is not None:
            return rejected
        body = {k: v for k, v in payload.items() if k != "csrf"}
        if require_human_session and action in _SENSITIVE_ACTIONS and not _is_human_session(request, sessions):
            # Before the step-up ceremony for the same reason
            # web/routes_approvals.py's own check is: a passkey prompt for
            # an action this session cannot take either way is a worse
            # refusal than the refusal itself.
            body, status = _human_session_required_json("change this setting")
            return JSONResponse(body, status_code=status)
        if _needs_step_up(action):
            fingerprint_body = {k: v for k, v in body.items() if k != "webauthn_assertion"}
            assertion = payload.get("webauthn_assertion")
            if not isinstance(assertion, dict):
                return _settings_step_up_response(action, fingerprint_body)
            expected_fp = _action_fingerprint(action, fingerprint_body)
            try:
                step_up_decide.verify_step_up(
                    LOCAL_PRINCIPAL, rp_id=step_up.rp_id, origin=step_up_origin.rstrip("/"), subject_key=action,
                    fingerprint=expected_fp, assertion=assertion, challenges=challenges,
                )
            except step_up_decide.StepUpExpired:
                return JSONResponse({"error": "step_up_expired"}, status_code=400)
            except WebAuthnError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
        try:
            result = _call_action(controller, action, body)
        except _BadAction as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        state = result if isinstance(result, dict) else controller.snapshot()
        return JSONResponse(_augment_connectors_with_icons(state))

    async def quit_action(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_mutation(request, payload)
        if rejected is not None:
            return rejected
        if not allow_quit:
            return JSONResponse({"error": "quitting PrivacyFence from the web is disabled"}, status_code=403)
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            # §16.2.8: behind an explicit confirmation -- the page's own
            # confirm() dialog sets this before it ever POSTs; a request
            # without it (a stray script, a replayed form) is refused
            # rather than treated as consent.
            return JSONResponse({"error": "confirmation required"}, status_code=400)
        # Shutdown runs as a background task, i.e. *after* this response's
        # body has been written to the socket -- not inline above it.
        # controller.quit_app() signals daemon_main's own shutdown wait, so
        # calling it first raced the server writing these 21 bytes: the
        # process could be torn down mid-write and the client saw
        # "peer closed connection without sending complete message body"
        # instead of its confirmation. Anyone clicking "Quit PrivacyFence"
        # could hit that, and tests/system/test_local_mode_system.py's own
        # quit step did, intermittently, in CI.
        return JSONResponse({"status": "quitting"}, background=BackgroundTask(controller.quit_app))

    def _org_config_step_up_active() -> bool:
        # Unlike _needs_step_up(action) above, org_config_upload has no
        # _ALLOWED_ACTIONS/_SENSITIVE_ACTIONS membership to check -- it's
        # the one path listed in _BESPOKE_SENSITIVE_ROUTE_PATHS, and it's
        # unconditionally sensitive whenever step-up is actually in force
        # (F5): there's no non-sensitive shape this upload could take.
        return step_up is not None and step_up.enabled and step_up.require_passkey

    async def org_config_upload(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        form = await request.form()
        if not _csrf_matches(request, form.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if require_human_session and not _is_human_session(request, sessions):
            # Same rationale as settings_action's own check above: an
            # organization config bundle can rewrite the PII policy, every
            # auto-accept rule, and every connector's OAuth client config
            # in one shot -- at least as much "what gets gated" as any
            # single _SENSITIVE_ACTIONS entry.
            body, status = _human_session_required_json("install an organization config bundle")
            return JSONResponse(body, status_code=status)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing file"}, status_code=400)
        raw = await upload.read()
        if len(raw) > MAX_ORG_CONFIG_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=400)
        # F5: pinning a signing key for the first time is a one-way trust
        # decision (every future bundle is refused until an administrator
        # deletes the pinned key file by hand) -- controller.
        # install_org_config_bytes still performs it unconditionally
        # (daemon_main.load_org_config's own hand-edited-file path needs
        # that), but this route, the only one reachable by an
        # unsupervised local process, first asks for it explicitly rather
        # than letting it happen as a side effect of an upload.
        if controller.would_pin_new_org_signing_key(raw) and not _is_confirmed(form.get("confirm_pin")):
            return JSONResponse(
                {
                    "error": "pin_confirmation_required",
                    "detail": (
                        "This is the first signed organization config bundle seen by this "
                        "install. Installing it will trust and permanently pin its signing key "
                        "for every future upload."
                    ),
                },
                status_code=409,
            )
        if _org_config_step_up_active():
            fingerprint_body = {"sha256": hashlib.sha256(raw).hexdigest()}
            assertion = _parse_form_assertion(form.get("webauthn_assertion"))
            if not isinstance(assertion, dict):
                return _settings_step_up_response("org_config_upload", fingerprint_body)
            expected_fp = _action_fingerprint("org_config_upload", fingerprint_body)
            try:
                step_up_decide.verify_step_up(
                    LOCAL_PRINCIPAL, rp_id=step_up.rp_id, origin=step_up_origin.rstrip("/"),
                    subject_key="org_config_upload", fingerprint=expected_fp, assertion=assertion,
                    challenges=challenges,
                )
            except step_up_decide.StepUpExpired:
                return JSONResponse({"error": "step_up_expired"}, status_code=400)
            except WebAuthnError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
        controller.install_org_config_bytes(raw)
        return JSONResponse(_snapshot(controller))

    async def audit_log_download(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        xlsx_path = controller.export_audit_log_path()
        if xlsx_path is None:
            return JSONResponse({"error": controller.error or "No audit log to export yet."}, status_code=404)
        return FileResponse(
            xlsx_path,
            filename=Path(xlsx_path).name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "no-store"},
        )

    routes: list[BaseRoute] = [
        Route("/settings", settings_page),
        Route("/settings/connectors", settings_connectors_page),
        Route("/api/settings/quit_app", quit_action, methods=["POST"]),
        Route("/api/settings/org_config/upload", org_config_upload, methods=["POST"]),
        Route("/api/settings/audit_log/download", audit_log_download),
        Route("/api/settings/{action}", settings_action, methods=["POST"]),
    ]
    for route in routes:
        if "POST" not in (getattr(route, "methods", None) or set()) or route.path == "/api/settings/{action}":
            continue
        # nosec B101 -- a real invariant, not a stripped-under-`-O` optimization:
        # a bespoke POST route this function itself just built, with no
        # matching _BESPOKE_SENSITIVE_ROUTE_PATHS/_BESPOKE_EXEMPT_ROUTE_PATHS
        # entry, must never reach the app it's about to be mounted into.
        assert route.path in _BESPOKE_SENSITIVE_ROUTE_PATHS or route.path in _BESPOKE_EXEMPT_ROUTE_PATHS, (
            f"{route.path} is a new bespoke POST route with no _BESPOKE_SENSITIVE_ROUTE_PATHS/"
            "_BESPOKE_EXEMPT_ROUTE_PATHS classification (3.3 of the self-approval review) -- "
            "add it to one of the two above before it can bypass _SENSITIVE_ACTIONS-shaped gating "
            "the way org_config_upload used to (F5)"
        )
    return routes


def create_app(
    controller: SettingsController, *, sessions: LocalSessionStore, allow_quit: bool = True,
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
    step_up: StepUpConfig | None = None, step_up_origin: str = "",
    require_human_session: bool = False,
) -> Starlette:
    """Standalone Starlette app wrapping build_routes() -- what this
    module's own tests construct against, the same "no filesystem/global-
    singleton dependency" convention web/routes_approvals.py's create_app()
    already established."""
    return Starlette(routes=build_routes(
        controller, sessions=sessions, allow_quit=allow_quit, notifications_enabled=notifications_enabled,
        notifications_detail=notifications_detail, step_up=step_up, step_up_origin=step_up_origin,
        require_human_session=require_human_session,
    ))
