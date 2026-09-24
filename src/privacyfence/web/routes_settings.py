"""Settings on the web (W3/W4). One module now builds the route list for
*both* local mode's ~30-action dispatcher and org mode's own settings
surface (policy surface consolidation, PSC-4b/PSC-5) -- formerly a full
second module, web/routes_org_settings.py. ``build_routes()`` (below) is
still local mode's entry point (unchanged signature); ``build_org_routes()``
is org mode's, replacing ``routes_org_settings.build_routes``.

Org-mode page *rendering* used to live in its own web/org_settings_pages.py
(PSC-4b moved it there verbatim, deliberately not merging it, since that was
PSC-5's job) -- PSC-5 deletes that module. Both ``GET /settings`` and
``GET /settings/privacy`` now serve settings_window_html.build_html(), the
exact same capability-filtered renderer local mode's own settings page uses
(``mode="org"``, ``is_admin=principal.is_admin`` -- see that function's own
docstring for exactly what each combination hides), and org mode's four
former bespoke POST routes are one generic ``POST /api/settings/{action}``,
restricted to ``_ORG_ALLOWED_ACTIONS`` -- the same path and JSON body shape
local mode's own dispatcher already answers, since it's the same JS bridge
posting either way now.

``web/org_settings_scope.py``'s ``ACTION_SCOPES`` is the one declaration both
modes project from: which of ``_ALLOWED_ACTIONS`` below has a real route in
which mode, and which of those need ``principal.is_admin`` in org mode.
``_ALLOWED_ACTIONS`` itself is now *projected* from that table (every action
naming ``org_settings_scope.LOCAL_MODE``, which is every one there is --
local mode's own dispatcher predates the split and was never gated by it),
rather than being the primary list org mode's own module used to filter.
Three pieces the two modes shared duplicated copies of before this merge are
now module-level helpers both call: ``_needs_step_up``/
``_settings_step_up_response`` (the "does this sensitive action need a fresh
WebAuthn assertion, and what does asking for one look like" pair -- byte-for-
byte identical between modes once parameterized by ``principal``) and
``_apply_step_up_gate`` (built on PSC-2a's shared
``web/approval_step_up._verify_or_challenge``, the same ceremony primitive
``web/routes_approvals.py``'s own ``decide()`` already reduces to -- local
mode's own inline try/except around ``step_up_decide.verify_step_up`` is
gone, folded into this one call). ``_record_settings_audit`` (org mode's
own, moved in unchanged) is available to both for the same reason, and both
modes' dispatch now calls it -- see that function's own docstring.

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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from starlette.routing import BaseRoute, Route

from .. import approval_icons, auto_accept, settings_window_html, webauthn_stepup, web_shell
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..policy import catalogue as policy_catalogue
from ..principal import LOCAL_PRINCIPAL, Principal, principal_scope
from ..agent_identity import REGISTRY, entry_for_id, sanitize_client_string
from ..settings_controller import (
    REPO_URL, SettingsController, _about_state_dict, _auto_accept_state_from_rules,
    _parse_value_list, _pii_general_fields, _privacy_state_from_config, _relative_time, _rule_usage_map,
    audit_rows,
)
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore
from . import org_install_policy, org_session, step_up_decide
from .approval_step_up import _verify_or_challenge
from .csp import nonce_for as _csp_nonce_for
from .org_session import OrgSessionStore
from .org_settings_scope import ACTION_SCOPES, LOCAL_MODE, ORG_MODE, is_action_permitted
from .routes_security import PF_WEBAUTHN_JS
from .session_auth import SESSION_COOKIE as _SESSION_COOKIE
from .session_auth import LocalSessionStore
from .session_auth import authenticated as _session_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import human_session_required_json as _human_session_required_json
from .session_auth import is_human_session as _is_human_session
from .session_auth import unauthorized_html as _unauthorized_response

if TYPE_CHECKING:
    from .oauth_provider import OrgOAuthProvider

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

# Projected from web/org_settings_scope.py's ACTION_SCOPES -- every action
# naming LOCAL_MODE, which is every action there is except AGT-5's two
# org-only AI-system pin actions (local mode's own dispatcher predates that
# module's mode split and was never gated by it). That module is now the primary declaration; this is the
# view of it local mode's own dispatch below actually consults.
_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    action for action, scope in ACTION_SCOPES.items() if LOCAL_MODE in scope.modes
)

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
    "add_policy_rule", "remove_policy_rule",
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
    # Changes what a draft *contains* (the user's own signature), never
    # whether or how it is gated -- every draft still raises its popup.
    "toggle_gmail_signature",
})

# AGT-5 (ADR 0035 decision 3): the org-only actions, classified the same
# explicit way. Pinning a DCR client to an AI system creates attested
# identity -- the only kind a rule may key on (ADR 0006 decision 3) -- and
# unpinning takes it away, so both are sensitive: step-up gated in org mode
# under ADR 0034. TestOrgOnlyActionsAreClassified fails the moment an
# ORG_MODE-only ACTION_SCOPES entry lands without a matching entry here.
_ORG_ONLY_SENSITIVE_ACTIONS: frozenset[str] = frozenset({
    "pin_agent_client", "unpin_agent_client",
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
# raises on every bespoke POST route it constructs that isn't in one of
# them -- a real, load-bearing invariant checked every time this app is
# built, not only under pytest, and not stripped under `python -O`/
# PYTHONOPTIMIZE the way an `assert` would be -- and TestBespokeRoutesAreClassified
# re-asserts the same thing against the actual Route objects it gets back,
# as a named, always-collected regression test rather than only a check a
# test run could otherwise skip past. Either one alone would leave a future
# bespoke POST route free to land unclassified -- the raise here catches it
# at runtime (this call already raises on an unclassified path, before the
# app ever serves it), the test catches it at review/CI time -- the same way
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
    SettingsController's own annotations (remove_policy_rule(rule_id: str),
    etc.) rather than a single hardcoded "idx is always an int" special
    case -- a wrong type on *any* parameter of *any* allowed action is
    rejected the same way, not just the one the native dispatcher happened
    to guard."""
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
    assertion obtained for e.g. ``remove_policy_rule`` can't be replayed to
    authorize a differently-shaped ``add_policy_rule`` (or the same action with
    different arguments). ``body`` must already have ``csrf``/
    ``webauthn_assertion`` stripped -- neither is part of what a human
    approved by completing the ceremony, and including the assertion itself
    would make the first (options-only) and second (assertion-carrying)
    request's own fingerprints diverge."""
    payload = f"{action}|{json.dumps(body, sort_keys=True, default=str)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_settings_audit(principal: Principal, summary: str) -> None:
    """Moved in unchanged from the former web/routes_org_settings.py, where
    #400 called for it by name: "Changing an org's privacy policy is
    exactly the kind of act that belongs in the audit log under the
    principal who did it." PSC-4b left local mode's own generic dispatch
    silent on purpose, flagging the asymmetry as a decision for the
    maintainer rather than resolving it -- now resolved: ``settings_action``
    below calls this for every successful mutation and every step-up
    refusal too, under ``LOCAL_PRINCIPAL``, the same shape org mode's own
    routes already use."""
    # Same free-form AuditEntry shape daemon_main.log_org_config_bundle_hash
    # uses for an install-level event that isn't a connector call -- this is
    # a settings mutation, not a gated tool call, so "connector"/"tool" stay
    # empty and "decision" carries a custom, non-approval value.
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="", tool="", tool_name="",
            summary=summary,
            sender=principal.email or principal.id,
            decision="settings_change",
            auto_accept_rule="", latency_seconds=0.0, pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for a settings change: %s", exc)


def _needs_step_up(action: str, step_up: StepUpConfig | None) -> bool:
    """Shared by both modes (PSC-4b) -- identical once parameterized by
    ``step_up``: local mode's own is optional (``None`` until web/server.py
    resolves one), org mode's is always given, but "no config yet" and "a
    config that hasn't turned require_passkey on" both mean the same thing
    here, so one optional-typed check covers both without either caller
    special-casing the other's default."""
    return (
        step_up is not None and step_up.enabled and step_up.require_passkey
        and (action in _SENSITIVE_ACTIONS or action in _ORG_ONLY_SENSITIVE_ACTIONS)
    )


def _settings_step_up_response(
    principal: Principal, action: str, fingerprint_body: dict[str, Any], *,
    step_up: StepUpConfig, challenges: StepUpChallengeStore,
) -> JSONResponse:
    """Shared by both modes (PSC-4b, formerly one near-identical copy each:
    local mode's own hardcoded ``LOCAL_PRINCIPAL``, the former
    web/routes_org_settings.py's already took ``principal`` as a parameter --
    this is that shape, made the only one). With no enrolled
    passkey this hard-fails (``403``) rather than falling through unguarded,
    since this is only ever reached once ``step_up.require_passkey`` is
    already on (``_needs_step_up`` above) -- there is no weaker fallback
    configuration to fall back to here."""
    fingerprint = _action_fingerprint(action, fingerprint_body)
    options_json = step_up_decide.begin_step_up(
        principal, rp_id=step_up.rp_id, subject_key=action, fingerprint=fingerprint, challenges=challenges,
    )
    if options_json is None:
        return JSONResponse(
            {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
        )
    return JSONResponse(
        {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
    )


def _apply_step_up_gate(
    principal: Principal, action: str, form_body: dict[str, Any], assertion: object, *,
    step_up: StepUpConfig | None, step_up_origin: str, challenges: StepUpChallengeStore,
) -> JSONResponse | None:
    """The one settings-action step-up check both modes' dispatch reduces
    to (PSC-4b): ``None`` when ``action`` doesn't need a fresh assertion at
    all, or (org mode's own former ``_guard_step_up``) once
    ``approval_step_up._verify_or_challenge`` -- the same ceremony
    primitive web/routes_approvals.py's own ``decide()`` already
    uses -- has verified a resubmitted one. Local mode's own former inline
    try/except around ``step_up_decide.verify_step_up`` reduced to exactly
    this same primitive too, just never factored out until now.

    ``form_body`` is the request's own body with ``csrf`` already stripped
    (local: the JSON payload; org: the form fields) -- ``webauthn_assertion``
    is stripped here, once, for the fingerprint every caller binds the
    ceremony to. ``assertion`` is the resubmitted assertion itself, already
    resolved to a ``dict | None`` by the caller: local mode's JSON body
    carries it natively; org mode's form field is a JSON-encoded string
    (``_parse_form_assertion``) -- a difference in request shape the two
    modes still don't share, so left to each call site.
    """
    if not _needs_step_up(action, step_up):
        return None
    assert step_up is not None  # nosec B101  # _needs_step_up() already proved this before calling us
    # A fresh, non-Optional-typed name for the lambda below to close over --
    # mypy strict mode doesn't carry the `assert` above's narrowing into a
    # nested function's free variables, since it can't prove `step_up`
    # itself isn't reassigned before the lambda runs.
    confirmed_step_up: StepUpConfig = step_up
    fingerprint_body = {k: v for k, v in form_body.items() if k != "webauthn_assertion"}
    return _verify_or_challenge(
        principal, step_up=confirmed_step_up, origin=step_up_origin.rstrip("/"), subject_key=action,
        fingerprint=_action_fingerprint(action, fingerprint_body), assertion=assertion, challenges=challenges,
        step_up_response=lambda: _settings_step_up_response(
            principal, action, fingerprint_body, step_up=confirmed_step_up, challenges=challenges,
        ),
    )


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

    async def settings_action(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response(request)
        action = request.path_params["action"]
        # The allowlist check happens *before* anything resembling
        # getattr(controller, action) runs -- an unlisted name (including
        # dunders, _load_config, snapshot itself) is a 404, not a lookup
        # that then gets rejected (§16.2.5/§16.7's own required test).
        # _ALLOWED_ACTIONS is itself projected from org_settings_scope.
        # ACTION_SCOPES (PSC-4b) -- local mode has no admin concept to gate
        # on (that module's own docstring), so consulting the declaration
        # here is exactly this membership check, not a separate
        # is_action_permitted(..., mode=LOCAL_MODE) call.
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
        step_up_response = _apply_step_up_gate(
            LOCAL_PRINCIPAL, action, body, payload.get("webauthn_assertion"),
            step_up=step_up, step_up_origin=step_up_origin, challenges=challenges,
        )
        if step_up_response is not None:
            with principal_scope(LOCAL_PRINCIPAL):
                _record_settings_audit(
                    LOCAL_PRINCIPAL, f"Step-up required for {action!r}, refused (principal={LOCAL_PRINCIPAL.id})",
                )
            return step_up_response
        try:
            result = _call_action(controller, action, body)
        except _BadAction as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        with principal_scope(LOCAL_PRINCIPAL):
            _record_settings_audit(LOCAL_PRINCIPAL, f"Changed setting {action!r} (principal={LOCAL_PRINCIPAL.id})")
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
            assert step_up is not None  # nosec B101  # _org_config_step_up_active() already proved this
            # See _apply_step_up_gate's own comment on why the lambda below
            # needs this fresh, non-Optional-typed name rather than closing
            # over `step_up` directly.
            confirmed_step_up: StepUpConfig = step_up
            fingerprint_body = {"sha256": hashlib.sha256(raw).hexdigest()}
            assertion = _parse_form_assertion(form.get("webauthn_assertion"))
            # org_config_upload has no _ALLOWED_ACTIONS/_SENSITIVE_ACTIONS
            # membership (module docstring) -- unconditionally sensitive
            # whenever step-up is active, so this goes straight to
            # _verify_or_challenge rather than through _apply_step_up_gate,
            # which gates on that membership.
            response = _verify_or_challenge(
                LOCAL_PRINCIPAL, step_up=confirmed_step_up, origin=step_up_origin.rstrip("/"),
                subject_key="org_config_upload",
                fingerprint=_action_fingerprint("org_config_upload", fingerprint_body), assertion=assertion,
                challenges=challenges,
                step_up_response=lambda: _settings_step_up_response(
                    LOCAL_PRINCIPAL, "org_config_upload", fingerprint_body,
                    step_up=confirmed_step_up, challenges=challenges,
                ),
            )
            if response is not None:
                return response
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
        # isinstance, not getattr: every entry above is a plain Route (never
        # a Mount/WebSocketRoute), and narrowing this way -- rather than
        # getattr(route, "path", ...) -- is what gives mypy route.path/
        # route.methods below as real attributes instead of BaseRoute's own,
        # narrower interface.
        if not isinstance(route, Route) or "POST" not in (route.methods or set()):
            continue
        if route.path == "/api/settings/{action}":
            continue
        # A real invariant, enforced with an explicit raise rather than
        # `assert` so it still holds under `python -O`/PYTHONOPTIMIZE, which
        # strips assert statements: a bespoke POST route this function
        # itself just built, with no matching _BESPOKE_SENSITIVE_ROUTE_PATHS/
        # _BESPOKE_EXEMPT_ROUTE_PATHS entry, must never reach the app it's
        # about to be mounted into.
        if route.path not in _BESPOKE_SENSITIVE_ROUTE_PATHS and route.path not in _BESPOKE_EXEMPT_ROUTE_PATHS:
            raise RuntimeError(
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


_ORG_ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "add_policy_rule", "remove_policy_rule",
    "toggle_pii_detection", "toggle_pii_category",
    "set_default_policy", "set_category_policy",
    "pin_agent_client", "unpin_agent_client",
})


def build_org_routes(
    *,
    sessions: OrgSessionStore,
    install_wide_settings: dict[str, Any],
    install_wide_settings_path: str = "",
    step_up: StepUpConfig,
    step_up_origin: str,
    oauth_provider: OrgOAuthProvider | None = None,
) -> list[Route]:
    """Org mode's own settings route list (#400; PSC-4b merged its dispatch
    into this module; PSC-5 merges its *rendering* -- both ``GET /settings``
    and ``GET /settings/privacy`` now serve the exact same settings_window_
    html.build_html() local mode's own ``_render_settings_page`` above
    does, capability-filtered for org mode (``mode="org"``,
    ``is_admin=principal.is_admin`` -- see that function's own docstring),
    with the former web/org_settings_pages.py deleted).

    That merge folds the four bespoke, form-POSTing routes it used to
    dispatch through (``add_rule``/``remove_rule``/``set_privacy_policy``/
    ``set_pii_policy``, one path and payload shape each) into one generic
    ``POST /api/settings/{action}`` -- the exact same path and JSON body
    shape local mode's own dispatcher already answers, restricted to
    ``_ORG_ALLOWED_ACTIONS`` (every action ``org_settings_scope.
    ACTION_SCOPES`` actually routes for ``ORG_MODE``, which is also
    everything the shared page's own JS bridge ever posts for a signed-in
    org principal). That's what closes #579's own remaining follow-up
    (PSC-4a/PSC-4b both flagged it, see this phase's own PR description):
    the shared renderer's page now carries the exact same PF_WEBAUTHN_JS/
    step-up bridge shim local mode's own page does, so a 428/403 step-up
    refusal here shows the same passkey prompt local mode's does, instead
    of a raw JSON body replacing the page.

    This module still contributes the auth/CSRF/authorization/step-up/
    audit machinery, shared with local mode's own ``build_routes`` above
    where the two modes' dispatch is the same shape (``_needs_step_up``/
    ``_settings_step_up_response``/``_apply_step_up_gate``,
    ``_record_settings_audit``, ``org_settings_scope.is_action_permitted``).

    ``install_wide_settings_path`` is the resolved path of the *server's
    own* settings.yaml -- the file ``install_wide_settings`` was loaded
    from, threaded down from ``daemon_main.run_app``'s ``--config``. The
    install-wide privacy/PII write routes need it and nothing else here
    does, so it defaults to empty: a caller that only wants the read
    surface (this module's own tests, a hand-built ``OrgAuth``) keeps
    working, and a policy write attempted without one is rejected with a
    400 explaining exactly that rather than guessing at a path to
    overwrite.

    ``step_up``/``step_up_origin`` (#579) close the gap local mode's own
    dispatch has closed since #426 Phase 3: every org-routed
    ``_SENSITIVE_ACTIONS`` member below (``add_policy_rule``,
    ``remove_policy_rule``, and the two install-wide privacy/PII writes)
    demands the same fresh WebAuthn assertion whenever
    ``step_up.require_passkey`` is on. Required, as
    ``routes_approvals.build_routes``'s own ``step_up``/``issuer_url`` pair
    already is: web/server.py resolves one ``StepUpConfig`` per org and
    threads it to every step-up-aware org route, this one included, rather
    than leaving a default that would silently reopen #579 for a caller
    that forgets to pass it.

    ``oauth_provider`` (AGT-5, ADR 0035 decision 3) backs the admin-only
    "AI systems" page: its DCR registrations are what an admin pins to a
    registry AI system (``pin_agent_client``/``unpin_agent_client``, both
    admin-only and step-up gated), and its ``agent_pins`` store is where the
    pins land. ``None`` (a caller with no OAuth server, this module's own
    older tests) shows an empty page and rejects a pin with a 400.
    """
    challenges = StepUpChallengeStore()

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    def _guard_step_up(principal: Principal, action: str, form_body: dict[str, Any]) -> JSONResponse | None:
        """Called after ``is_action_permitted`` already passed -- a passkey
        prompt for an action this principal isn't authorized to take either
        way would be a worse refusal than the authorization refusal itself,
        the same ordering local mode's own ``require_human_session`` check
        keeps ahead of its step-up check. Returns ``None`` to let the
        caller proceed; records the refusal in the audit log, under this
        principal, for every other case before returning the response to
        send as-is -- the same step local mode's own ``settings_action``
        above now takes too (see ``_record_settings_audit``'s own
        docstring), just already factored out here since org mode's
        dispatch always needed it.

        ``form_body`` is, despite the name, a JSON body's already-decoded
        dict (PSC-5 -- ``settings_action`` below, this function's only
        caller since the four bespoke form-POST routes it used to serve
        are gone) -- ``webauthn_assertion``, if present, is already a
        ``dict``, the same as local mode's own ``payload.get(
        "webauthn_assertion")``, not the JSON-encoded *string* a multipart
        form field carried it as (``_parse_form_assertion``, still used by
        ``org_config_upload`` above, which is still a real multipart
        route)."""
        assertion = form_body.get("webauthn_assertion")
        response = _apply_step_up_gate(
            principal, action, form_body, assertion if isinstance(assertion, dict) else None,
            step_up=step_up, step_up_origin=step_up_origin, challenges=challenges,
        )
        if response is not None:
            with principal_scope(principal):
                _record_settings_audit(
                    principal, f"Step-up required for {action!r}, refused (principal={principal.id})",
                )
        return response

    def _ensure_principal_settings_loaded() -> None:
        # Lazy import -- daemon_main.py pulls in the whole connector/client
        # stack at module level, the same reason every other cross-module
        # call into it from web/*.py (e.g. settings_controller.py's own
        # telegram_submit_2fa) imports it inside the function that needs it
        # rather than at module scope.
        from ..daemon_main import _load_principal_settings

        _load_principal_settings(install_wide_config=install_wide_settings)

    def _org_state(principal: Principal) -> dict[str, Any]:
        """PSC-5's own org-mode counterpart of ``_snapshot(controller)``
        above -- the exact same settings_window_html.build_html() shape,
        built fresh per request (org mode is stateless per request, unlike
        local mode's single long-lived ``SettingsController``) rather than
        cached anywhere. General's PII fields and Privacy Filter both read
        the install-wide config directly (admin-only in this mode, #400's
        own fail-closed ``"block"`` default for an unconfigured group --
        unlike local mode's ``"allow"``, see ``_privacy_state_from_config``);
        Auto-accept reads this principal's own on-disk rules; Audit Log
        (AGT-5) this principal's own recent decisions; AI systems (AGT-5,
        admin only) the OAuth provider's registrations and pins. Sections
        this mode never shows at all (Connectors) get an inert
        placeholder -- settings_window_html._capabilities_for is what
        actually keeps them from ever rendering, not the shape of a
        placeholder nothing reads. Must already be called inside
        ``with principal_scope(principal):`` -- ``auto_accept.
        get_policy_v2_rules()``/``_rule_usage_map()`` both read this
        principal's own on-disk files.
        """
        about = _about_state_dict()
        privacy = _privacy_state_from_config(install_wide_settings, fail_safe_default="block")
        # toggle_calendar_free_busy is LOCAL_MODE-only, admin_only, and
        # hardcoded to a single desktop's own config (org_settings_scope.
        # ACTION_SCOPES's own comment on it) -- Calendar has no install-
        # wide/org-mode equivalent at all, so it's dropped from the group
        # list entirely rather than rendered with a control that could
        # only ever 404.
        privacy["groups"] = [g for g in privacy["groups"] if g["key"] != "calendar"]
        # Same for toggle_gmail_signature: the Gmail group stays (its
        # category policy is install-wide), only the toggle card goes --
        # settings_window_html renders it only when this key is present.
        privacy.pop("gmail_append_signature", None)

        def _resolve_raw(rule: Any) -> str:
            values = rule.value if isinstance(rule.value, list) else ([rule.value] if rule.value else [])
            return ", ".join(str(v) for v in values)

        rules = auto_accept.get_policy_v2_rules()
        # AGT-5: the viewing principal's own recent decisions -- per-principal, exactly like
        # the Auto-accept rules above (get_audit_logger() resolves this principal's own log),
        # with the same agent column local mode's page gets.
        # A log that cannot be read costs the page its list, never the request -- the same
        # posture _record_settings_audit takes for a write.
        try:
            recent = audit_rows(get_audit_logger().recent_entries(20))
        except Exception as exc:
            logger.warning("Could not read recent audit entries for %s: %s", principal.id, exc)
            recent = []
        return {
            "error": "",
            "general": {
                **_pii_general_fields(install_wide_settings),
                "update_check_enabled": True, "update_check_beta": False,
                "org_installed": False, "org_installed_date": "",
                "org_button_label": "", "version": about["version"],
                "notifications_enabled": True, "notifications_detail": "minimal",
                "step_up_available": False, "step_up_on": False, "step_up_has_passkey": False,
            },
            "connectors": [],
            "telegram_auth": {"step": None, "error": ""},
            "auto_accept": _auto_accept_state_from_rules(rules, _rule_usage_map(), resolve_value=_resolve_raw),
            "privacy": privacy,
            "audit": {"log_level": "", "log_file": "", "export_hint": "", "recent": recent},
            "agents": _agents_state() if principal.is_admin else {"clients": [], "stale_pins": [], "registry": []},
            "about": about,
        }

    def _agents_state() -> dict[str, Any]:
        """The admin's "AI systems" page (AGT-5): every current DCR registration with its
        claimed name and pin, every pin whose registration the TTL prune removed (stale --
        inert, shown so an admin can clear it; ADR 0035 decision 3), and the registry an admin
        can pin to. ``client_name`` is the caller's own string: sanitized here, escaped by the
        page."""
        registry = [{"id": entry.agent_id, "name": entry.display_name} for entry in REGISTRY]
        if oauth_provider is None:
            return {"clients": [], "stale_pins": [], "registry": registry}
        pins = oauth_provider.agent_pins.pins()
        clients = []
        for client in sorted(oauth_provider.list_clients(), key=lambda c: -c.last_used_at):
            pin = pins.get(client.client_id)
            entry = entry_for_id(pin.agent_id) if pin is not None else None
            clients.append({
                "client_id": client.client_id,
                "client_name": sanitize_client_string(client.client_name),
                "last_used": _relative_time(datetime.fromtimestamp(client.last_used_at, timezone.utc).isoformat()),
                "pinned_agent_id": entry.agent_id if entry is not None else "",
                "pinned_agent_name": entry.display_name if entry is not None else "",
            })
        live = {c["client_id"] for c in clients}
        stale = []
        for client_id, pin in sorted(pins.items()):
            entry = entry_for_id(pin.agent_id)
            if client_id not in live and entry is not None:
                stale.append({"client_id": client_id, "agent_id": entry.agent_id, "agent_name": entry.display_name})
        return {"clients": clients, "stale_pins": stale, "registry": registry}

    def _wrap_org_settings(request: Request, principal: Principal, *, initial_section: str) -> Response:
        nonce = _csp_nonce_for(request)
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            state = _org_state(principal)
        body = settings_window_html.build_html(
            state, nonce=nonce, initial_section=initial_section,
            mode=ORG_MODE, is_admin=principal.is_admin,
        )
        csrf = request.cookies.get(org_session.SESSION_COOKIE, "")
        # PF_WEBAUTHN_JS/_settings_bridge_shim (PSC-5, closing #579's own
        # remaining follow-up): the exact same ceremony helpers local
        # mode's own _render_settings_page carries -- see that function's
        # own comment. Whether it's ever exercised depends on this org's
        # StepUpConfig, not on whether the helpers exist.
        body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
        body += _settings_bridge_shim(csrf=csrf, repo_url=REPO_URL, nonce=nonce)
        html = web_shell.wrap(
            body, title="PrivacyFence — Settings", active="settings", nonce=nonce,
            nav_items=web_shell.ORG_NAV_ITEMS, principal_label=principal.email or principal.display_name or principal.id,
            # Org mode has no per-request settings push to subscribe a
            # live stream to (state_stream.py's own StateStream.push_
            # settings is wired to one local SettingsController's own
            # on_change, module docstring) -- unchanged from before PSC-5.
            live_updates=False, notifications_enabled=False,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def settings_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        # Auto-accept is the one section every org principal, admin or
        # not, always gets (settings_window_html._capabilities_for) --
        # General itself is empty (hidden entirely) for a non-admin.
        initial_section = "general" if principal.is_admin else "auto_accept"
        return _wrap_org_settings(request, principal, initial_section=initial_section)

    async def privacy_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/settings/privacy", status_code=302, headers={"Cache-Control": "no-store"},
            )
        if not principal.is_admin:
            return PlainTextResponse("Forbidden -- administrator access required.", status_code=403)
        return _wrap_org_settings(request, principal, initial_section="privacy")

    def _current_pii_flag(action: str, body: dict[str, Any]) -> bool:
        """toggle_pii_detection/toggle_pii_category flip the *current*
        value -- the same semantics local mode's own menu-item toggle
        uses, and what the shared JS template's own toggleHtml() call
        sites for these two actions already post (an empty payload,
        exactly like local mode's), rather than org_install_policy.
        apply_change's own explicit-``enabled``-field contract (right for
        a plain HTML form that could be submitted stale or twice, see that
        module's own ``_bool_field`` docstring -- not what this bridge-
        driven control sends). Computing the flip here, once, keeps the
        shared renderer's own JS unaware of this one mode-specific
        difference."""
        pii_cfg = install_wide_settings.get("pii_detection", {}) or {}
        if action == "toggle_pii_detection":
            return bool(pii_cfg.get("enabled", True))
        return bool(pii_cfg.get(str(body.get("category_key", "")), True))

    def _apply_org_action(principal: Principal, action: str, body: dict[str, Any]) -> str | None:
        """Applies one ``_ORG_ALLOWED_ACTIONS`` mutation, scoped to
        ``principal`` (already entered by the caller), returning the exact
        summary string to audit-log, or ``None`` when nothing actually
        changed -- the same "only audit a real change" behavior the former
        per-action routes kept before PSC-5 folded them into this one
        dispatcher. May raise ``org_install_policy.PolicyChangeRejected``/
        ``OSError``, left for the caller to map to a response the same way
        the former ``_apply_install_wide`` did.
        """
        if action == "add_policy_rule":
            group = str(body.get("group", ""))
            verb_enums = policy_catalogue.parse_verbs(body.get("verbs") or [])
            if not verb_enums:
                return None
            value = _parse_value_list(str(body.get("value") or ""))
            new_rules = policy_catalogue.rules_for_catalogue_entry(group, value, verb_enums)
            if not new_rules or not auto_accept.add_policy_v2_rules(new_rules):
                return None
            verb_names = ", ".join(verb.value for verb in verb_enums)
            return f"Added auto-accept rule ({group!r}, allow {verb_names!r}) (principal={principal.id})"
        if action == "remove_policy_rule":
            rule_id = str(body.get("rule_id", ""))
            if not auto_accept.remove_policy_v2_rule(rule_id):
                return None
            return f"Removed auto-accept rule {rule_id!r} (principal={principal.id})"
        if action == "pin_agent_client":
            return _pin_agent_client(principal, body)
        if action == "unpin_agent_client":
            client_id = str(body.get("client_id", ""))
            if oauth_provider is None or not oauth_provider.agent_pins.unpin(client_id):
                return None
            return f"Unpinned OAuth client {client_id!r} from its AI system (admin={principal.id})"
        # The remaining four actions are all install-wide admin writes,
        # applied through the same org_install_policy.apply_change #400
        # C3e's own routes already used.
        if action in ("toggle_pii_detection", "toggle_pii_category"):
            body = {**body, "enabled": not _current_pii_flag(action, body)}
        summary = org_install_policy.apply_change(
            install_wide_settings, install_wide_settings_path, action=action, payload=body,
        )
        return f"{summary} (admin={principal.id})"

    def _pin_agent_client(principal: Principal, body: dict[str, Any]) -> str | None:
        """Pin one *current* registration to a registry AI system. Both halves are validated
        against server-side state, never taken on trust from the body: the ``client_id`` must
        be a live registration (a pin names a registration, not a name) and the ``agent_id``
        a registry entry. The audit line carries the registration's claimed name so a
        reviewer can see what the admin was looking at when they pinned it."""
        client_id = str(body.get("client_id", ""))
        agent_id = str(body.get("agent_id", ""))
        if oauth_provider is None or not oauth_provider.has_client(client_id):
            raise org_install_policy.PolicyChangeRejected(f"no registered OAuth client {client_id!r}")
        entry = entry_for_id(agent_id)
        if entry is None:
            raise org_install_policy.PolicyChangeRejected(f"{agent_id!r} is not a known AI system")
        if oauth_provider.agent_pins.pinned_agent_id(client_id) == entry.agent_id:
            return None
        oauth_provider.agent_pins.pin(client_id, entry.agent_id, pinned_by=principal.id)
        claimed = sanitize_client_string(oauth_provider.client_name(client_id))
        return (
            f"Pinned OAuth client {client_id!r} (registered as {claimed!r}) to AI system "
            f"{entry.agent_id!r} (admin={principal.id})"
        )

    async def settings_action(request: Request) -> Response:
        """The generic ``POST /api/settings/{action}`` dispatcher PSC-5
        gives org mode -- the same path and JSON body shape local mode's
        own ``settings_action`` above answers (this SPA's shared bridge,
        settings_window_html.py's own ``pfSettingsPost``, posts the same
        way regardless of mode), restricted to ``_ORG_ALLOWED_ACTIONS``.
        Gate order mirrors every other mutating route in this module:
        authenticated, then CSRF, then origin, then authorization, then
        step-up, then the mutation itself.
        """
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        action = request.path_params["action"]
        if action not in _ORG_ALLOWED_ACTIONS:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if not is_action_permitted(action, principal, mode=ORG_MODE):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = {k: v for k, v in payload.items() if k != "csrf"}
        step_up_response = _guard_step_up(principal, action, body)
        if step_up_response is not None:
            return step_up_response
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            try:
                summary = _apply_org_action(principal, action, body)
            except org_install_policy.PolicyChangeRejected as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except OSError as exc:
                # The settings.yaml write itself failed -- nothing was
                # applied (apply_change's own docstring), so this is a 500
                # with the policy unchanged, not a partially-applied one.
                logger.error("Could not write the install-wide settings.yaml: %s", exc)
                return JSONResponse({"error": "could not write settings.yaml"}, status_code=500)
            if summary is not None:
                _record_settings_audit(principal, summary)
            state = _org_state(principal)
        return JSONResponse(state)

    return [
        Route("/settings", settings_page),
        Route("/settings/privacy", privacy_page),
        Route("/api/settings/{action}", settings_action, methods=["POST"]),
    ]
