"""Settings on the web for an organization server (ADR 0033).

``build_org_routes()`` is the one entry point. ``GET /settings`` serves
settings_window_html.build_html() (``is_admin=principal.is_admin``) wrapped in
web_shell.wrap(), and ``POST /api/settings/{action}`` is a generic dispatcher restricted to
``_ORG_ALLOWED_ACTIONS``, the actions ``web/org_settings_scope.py``'s ``ACTION_SCOPES`` declares
a route for. The install-wide privacy policy and the usage stats are Admin sections
(``GET /settings/privacy`` forwards there).

Helpers shared with ``web/routes_admin.py``: ``_needs_step_up``/``_settings_step_up_response``
(does this sensitive action need a fresh WebAuthn assertion, and what does asking for one look
like), ``_apply_step_up_gate`` (built on the shared ``web/approval_step_up._verify_or_challenge``,
the ceremony primitive ``web/routes_approvals.py``'s ``decide()`` also reduces to),
``_gate_upload`` (the same for a multipart upload, bound to the file's hash),
``read_bounded_form`` and ``_record_settings_audit``.

**Standing rule this module exists to keep true:** no route here ever calls
``subprocess.run``/``os.system``/``open``. TestNoSubprocessFromHttp in this module's test file is
what a security review gets to point at instead of re-reading this comment.

**Sensitive-action step-up (ADR 0034):** an agent that cannot forge an approval does not need to
if it can add an always-allow rule instead, so ``_SENSITIVE_ACTIONS`` (the rule and step-up-scope
actions) are gated on a fresh WebAuthn assertion whenever ``step_up.require_passkey`` is on: a
first POST with no ``webauthn_assertion`` gets a ``428`` carrying fresh options (or a ``403``
naming ``/security`` if nothing is enrolled), and a second POST carrying the completed assertion
is verified and, on success, runs the action. Importing personal settings widens what is
auto-accepted exactly as adding a rule does, so it goes through the same gates, bound to the
file's hash.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import threading
import typing
import uuid
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from starlette.routing import BaseRoute, Route

from .. import (
    agent_runtimes, auto_accept, runtime_credential_sweeper, runtime_credentials,
    scoped_token_proposals, settings_window_html, web_shell, whats_new,
)
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..policy import catalogue as policy_catalogue
from ..policy import scope_rows
from ..policy import store as policy_store
from ..policy.value_format import RuleValueError
from ..connector_registry import ConnectorRegistry
from ..principal import Principal, principal_scope
from ..resource_names import get_resolver
from ..scoped_tokens import (
    DEFAULT_DAYS, PROPOSALS_DISABLED_MESSAGE, ScopedToken, ScopedTokenError, ScopedTokenPolicy, ScopedTokenStore,
    get_store, normalise_create_request,
)
from ..settings_state import (
    REPO_URL, _about_state_dict, _auto_accept_state_from_rows,
    _parse_value_list, _rule_usage_map,
    audit_rows, cached_row_value, rule_resource_ids,
)
from ..step_up_config import StepUpConfig, effective_scope, set_personal_scope, step_up_scope_fields
from ..webauthn_stepup import StepUpChallengeStore
from . import org_install_policy, org_session, step_up_decide
from .personal_settings_shim import personal_transfer_shim
from .personal_settings_routes import STEP_UP_SUBJECT_KEY, PersonalSettingsHooks, build_personal_routes
from .approval_step_up import _verify_or_challenge
from .csp import nonce_for as _csp_nonce_for
from .org_session import OrgSessionStore
from .org_settings_scope import ORG_MODE, is_action_permitted
from .routes_security import PF_WEBAUTHN_JS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- #
# The actions of the generic dispatcher that can change *what a future write
# reaches decide() at all* (ADR 0034): an always-allow rule or a narrower
# step-up scope. A passkey requirement on decide() alone would leave them as
# the bypass, so each needs a fresh WebAuthn assertion whenever step-up is
# required. It is the subset of _ORG_ALLOWED_ACTIONS below that needs one;
# TestOrgActionsAreSensitive keeps the two in step.
# ---------------------------------------------------------------------------- #

_SENSITIVE_ACTIONS: frozenset[str] = frozenset({
    "add_policy_rule", "remove_policy_rule", "update_policy_rule", "set_policy_rule_verbs",
    # Narrowing one's own step-up scope back towards the configured minimum
    # makes fewer approvals ask for a passkey, so it needs one itself. The
    # widening direction goes through the same gate: one action, one rule.
    # Nothing here can go below the configured minimum either way
    # (step_up_config.set_personal_scope, ADR 0091).
    "set_step_up_scope",
})


def _scoped_revoke_fingerprint_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    token_id = payload.get("token_id")
    return {"token_id": token_id} if isinstance(token_id, str) else None


def _scoped_rename_fingerprint_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    token_id, label = payload.get("token_id"), payload.get("label")
    if isinstance(token_id, str) and isinstance(label, str):
        return {"token_id": token_id, "label": label}
    return None


def _scoped_extend_fingerprint_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    token_id, days = payload.get("token_id"), payload.get("days")
    if isinstance(token_id, str) and isinstance(days, int) and not isinstance(days, bool):
        return {"token_id": token_id, "days": days}
    return None


class _BadAction(Exception):
    """A wrong-typed or missing argument; the admin dispatcher maps it to a 400."""


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


# ---------------------------------------------------------------------------- #
# The bridge shim -- swaps settings_window_html.py's own
# ``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``
# for a ``fetch()`` POST to /api/settings/<action>, the same technique
# web/routes_approvals.py's own _bridge_shim already uses for the approval
# card (see that module's docstring). Five actions are intercepted here
# instead of forwarded, because none of them is "POST an action, get a
# snapshot back":
#   - open_repo: a plain link, opened client-side -- no request at all.
#   - install_org_config: triggers the hidden <input type=file> below,
#     which itself POSTs a multipart body to <endpoint>/org_config/upload.
#   - upload_rooms: the same, for the room directory, to <endpoint>/rooms/upload.
#   - upload_connector_config: the same, for a signed connector config, to
#     <endpoint>/connector_config/upload; its 409 carries the signing key's fingerprint, which
#     the next attempt sends back as the confirmation.
#   - export_audit_log: a same-origin navigation to the download route.
#   - quit_app: a client-side confirm() first, then POSTed as an action.
# ---------------------------------------------------------------------------- #

def _settings_bridge_shim(*, csrf: str, repo_url: str, nonce: str, endpoint: str = "/api/settings") -> str:
    """``endpoint`` is where the page's actions and uploads are posted: ``/api/settings`` for
    Settings, ``/api/admin`` for the Admin page, which shares this renderer.

    ``pfSettingsPost``'s own ``428``/``403`` branches are
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
        "<input type=\"file\" id=\"pf-rooms-input\" accept=\".json,application/json\" style=\"display:none\">"
        "<input type=\"file\" id=\"pf-connector-config-input\" accept=\".pfconnector,.txt,text/plain\" style=\"display:none\">"
        f'<script nonce="{nonce}">(function(){{'
        f"var CSRF = {csrf!r};"
        f"var ENDPOINT = {endpoint!r};"
        # An upload can answer 409 (would pin a new signing key -- ask first) and 428/403
        # (step-up, same shape pfSettingsPost's own retry chain below already handles for JSON
        # actions) as well as succeeding or 401/400/403-on-cross-origin. attemptUpload() re-POSTs
        # the same file as a fresh FormData each round -- multipart has no way to resume a
        # partially-approved request the way a JSON body's own retry does by copying `body`.
        "function bindUpload(input, url) {"
        "  input.addEventListener('change', function(){"
        "    if (!input.files || !input.files[0]) return;"
        "    var file = input.files[0];"
        "    function attemptUpload(confirmPin, assertion) {"
        "      var fd = new FormData();"
        "      fd.append('file', file);"
        "      fd.append('csrf', CSRF);"
        "      if (confirmPin) { fd.append('confirm_pin', 'true'); }"
        "      if (typeof confirmPin === 'string') { fd.append('confirm_fingerprint', confirmPin); }"
        "      if (assertion) { fd.append('webauthn_assertion', JSON.stringify(assertion)); }"
        "      return fetch(url, {method:'POST', credentials:'same-origin', body: fd})"
        "        .then(function(r){"
        "          if (r.status === 409) {"
        "            return r.json().then(function(data){"
        "              if (data.error === 'pin_confirmation_required' && window.confirm(data.detail + ' Continue?')) {"
        "                return attemptUpload(data.fingerprint || true, assertion);"
        "              }"
        "              return null;"
        "            });"
        "          }"
        "          if (r.status === 428) {"
        "            return r.json().then(function(data){"
        "              if (data.webauthn_options && window.PublicKeyCredential) {"
        "                return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(newAssertion){"
        "                  return attemptUpload(confirmPin, newAssertion);"
        "                }).catch(function(err){"
        "                  window.alert('This change needs your passkey, and the prompt failed: ' + err.message);"
        "                  return null;"
        "                });"
        "              }"
        "              window.alert('This change needs a passkey, and none is available in this browser.');"
        "              return null;"
        "            });"
        "          }"
        "          if (r.status === 403) {"
        "            return r.json().then(function(data){"
        "              if (data.enroll_url) {"
        "                window.alert('This change requires a passkey. Set one up at ' + data.enroll_url + '.');"
        "              }"
        "              return null;"
        "            });"
        "          }"
        "          return r.json().then(function(state){ return {ok: r.ok, state: state}; });"
        "        });"
        "    }"
        "    attemptUpload(false, null).then(function(done){"
        "      if (!done) return;"
        "      if (done.ok && window.__pfRender) { window.__pfRender(done.state); }"
        "      else if (!done.ok && done.state && typeof done.state.error === 'string' && window.__pfShowError) {"
        "        window.__pfShowError(done.state.error);"
        "      }"
        "    }).finally(function(){ input.value = ''; });"
        "  });"
        "}"
        "var fileInput = document.getElementById('pf-org-config-input');"
        "var roomsInput = document.getElementById('pf-rooms-input');"
        "var connectorInput = document.getElementById('pf-connector-config-input');"
        "bindUpload(fileInput, ENDPOINT + '/org_config/upload');"
        "bindUpload(roomsInput, ENDPOINT + '/rooms/upload');"
        "bindUpload(connectorInput, ENDPOINT + '/connector_config/upload');"
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
        "  if (action === 'upload_rooms') { roomsInput.click(); return; }"
        "  if (action === 'upload_connector_config') { connectorInput.click(); return; }"
        "  if (action === 'export_audit_log') { window.location = '/api/settings/audit_log/download'; return; }"
        "  var url = ENDPOINT + '/' + encodeURIComponent(action);"
        # The scoped token actions (ADR 0164) have routes of their own and
        # answer with no page state: the page is rebuilt from the server.
        "  var scopedRoutes = {create_scoped_token: ['create', 'Create this token? It is shown once "
        "and cannot be shown again.'], revoke_scoped_token: ['revoke', 'Revoke this token? "
        "Clients using it stop working at once.'], rename_scoped_token: ['rename', null], extend_scoped_token: ['extend', null], revoke_all_scoped_tokens: ['revoke_all', 'Revoke all your "
        "scoped tokens?'], create_proposed_scoped_token: ['create_from_proposal', null], "
        "decline_scoped_token_proposal: ['decline_proposal', 'Decline this token proposal?']};"
        "  var scopedAction = scopedRoutes.hasOwnProperty(action);"
        "  if (scopedAction) {"
        "    if (scopedRoutes[action][1] && !window.confirm(scopedRoutes[action][1])) { return; }"
        "    url = '/api/settings/scoped_tokens/' + scopedRoutes[action][0]; rest.confirmed = true;"
        "  }"
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
        # An org page's create answers with the secret, once (ADR 0165): the dialog shows it
        # and the page reloads only when the dialog closes.
        # A proposal's review page is spent once it is created or declined: go to the section, after
        # the org secret dialog closes where there is one.
        "    var proposalAction = (action === 'create_proposed_scoped_token' || action === 'decline_scoped_token_proposal');"
        "    var toSection = function(){ window.location.assign('/settings/scoped_tokens'); };"
        "    if (res.ok && proposalAction) {"
        # A delivered token's secret went to n8n and is not in this answer (ADR 0278).
        "      if (res.state && res.state.delivered && res.state.created) {"
        "        window.alert('Created \\'' + res.state.created.label + '\\' and delivered it to n8n as \\''"
        " + res.state.delivered.credential_name + '\\'.');"
        "        toSection();"
        "      } else if (res.state && typeof res.state.secret === 'string' && window.__pfShowScopedSecret) {"
        "        window.__pfShowScopedSecret(res.state.secret, toSection);"
        "      } else { toSection(); }"
        "    } else if (res.ok && scopedAction && res.state && typeof res.state.secret === 'string' && window.__pfShowScopedSecret) {"
        "      window.__pfShowScopedSecret(res.state.secret, function(){ window.location.reload(); }); "
        "    } else if (res.ok && scopedAction) { window.location.reload(); } "
        "else if (res.ok && window.__pfRender) { window.__pfRender(res.state); } "
        "else if (!res.ok && scopedAction && res.state && res.state.error) { window.alert(res.state.error); } "
        "else if (!res.ok && res.state && typeof res.state.error === 'string' && window.__pfShowError) { window.__pfShowError(res.state.error); } "
        "else if (!res.ok) { console.error('PrivacyFence action failed:', action, res.state); } });"
        "}};"
        "})();</script>"
    )


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
    """Changing an org's privacy policy is exactly the kind of act that
    belongs in the audit log under the principal who did it: the dispatchers
    call this for every successful mutation and every step-up refusal too."""
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


def _needs_step_up(
    action: str, step_up: StepUpConfig | None, *, sensitive: bool | None = None, always: bool = False,
) -> bool:
    """Whether ``action`` needs a fresh assertion (ADR 0033). "No config" and "a
    config that hasn't turned require_passkey on" both mean no, so one optional-typed check
    covers every caller."""
    # A caller whose actions are not in _SENSITIVE_ACTIONS (the Admin page) says for itself
    # whether this one is sensitive. ``always`` asks for an assertion whatever the step-up
    # configuration says, so the default configuration cannot leave such an action unguarded.
    is_sensitive = action in _SENSITIVE_ACTIONS if sensitive is None else sensitive
    if step_up is None or not is_sensitive:
        return False
    return always or (step_up.enabled and step_up.require_passkey)


def _settings_step_up_response(
    principal: Principal, action: str, fingerprint_body: dict[str, Any], *,
    step_up: StepUpConfig, challenges: StepUpChallengeStore, always: bool = False,
) -> JSONResponse:
    """The 428 (or 403) answer for a sensitive action (ADR 0033). With no enrolled
    passkey this hard-fails (``403``) rather than falling through unguarded,
    since this is only ever reached once ``step_up.require_passkey`` is
    already on (``_needs_step_up`` above) -- there is no weaker fallback
    configuration to fall back to here."""
    fingerprint = _action_fingerprint(action, fingerprint_body)
    options_json = step_up_decide.begin_step_up(
        principal, rp_id=step_up.rp_id, subject_key=action, fingerprint=fingerprint, challenges=challenges,
    )
    if options_json is None:
        error = "Add a passkey on /security first." if always else "passkey_enrollment_required"
        return JSONResponse({"error": error, "enroll_url": "/security"}, status_code=403)
    return JSONResponse(
        {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
    )


def _apply_step_up_gate(
    principal: Principal, action: str, form_body: dict[str, Any], assertion: object, *,
    step_up: StepUpConfig | None, step_up_origin: str, challenges: StepUpChallengeStore,
    sensitive: bool | None = None, always: bool = False,
) -> JSONResponse | None:
    """The one settings-action step-up check both modes' dispatch reduces
    to (ADR 0033): ``None`` when ``action`` doesn't need a fresh assertion at
    all, or (org mode's own former ``_guard_step_up``) once
    ``approval_step_up._verify_or_challenge`` -- the same ceremony
    primitive web/routes_approvals.py's own ``decide()`` already
    uses -- has verified a resubmitted one.

    ``form_body`` is the request's own body with ``csrf`` already stripped
    (the JSON payload or the form fields) -- ``webauthn_assertion``
    is stripped here, once, for the fingerprint every caller binds the
    ceremony to. ``assertion`` is the resubmitted assertion itself, already
    resolved to a ``dict | None`` by the caller: a JSON body carries it natively; a
    form field is a JSON-encoded string (``_parse_form_assertion``).
    """
    if not _needs_step_up(action, step_up, sensitive=sensitive, always=always):
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
            principal, action, fingerprint_body, step_up=confirmed_step_up, challenges=challenges, always=always,
        ),
    )


async def read_bounded_form(
    request: Request, *, max_bytes: int, slack: int = 32 * 1024, too_large: str = "The file is too large.",
    refuse: Callable[[str, int], Response],
) -> Any:  # noqa: ANN401 -- starlette's FormData
    """An upload's multipart form, read only when its declared size is plausible and with a cap on
    the parts, or the ``Response`` ``refuse`` builds. A body that does not declare its size is
    refused, because there is no way to bound it before it is spooled to disk. ``max_bytes`` bounds
    the one file; ``slack`` covers the multipart framing and the form's other fields."""
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        return refuse("The upload must declare its size.", 411)
    if int(declared) > max_bytes + slack:
        return refuse(too_large, 413)
    try:
        return await request.form(max_files=1, max_fields=8, max_part_size=max_bytes)
    except (MultiPartException, HTTPException):
        return refuse("Upload exactly one file.", 400)


def _gate_upload(
    subject: str, fingerprint_body: dict[str, Any], assertion: dict[str, Any] | None, *,
    principal: Principal, step_up: StepUpConfig | None, step_up_origin: str,
    challenges: StepUpChallengeStore, always: bool,
) -> JSONResponse | None:
    """The step-up check of a multipart upload, bound to the file's hash so a ceremony completed
    for one file cannot authorize another. ``None`` when nothing more is needed."""
    in_force = step_up is not None and (always or (step_up.enabled and step_up.require_passkey))
    if not in_force:
        return None
    assert step_up is not None  # nosec B101  # in_force already proved this
    # A fresh, non-Optional-typed name for the lambda to close over -- see _apply_step_up_gate.
    confirmed_step_up: StepUpConfig = step_up
    return _verify_or_challenge(
        principal, step_up=confirmed_step_up, origin=step_up_origin.rstrip("/"), subject_key=subject,
        fingerprint=_action_fingerprint(subject, fingerprint_body), assertion=assertion, challenges=challenges,
        step_up_response=lambda: _settings_step_up_response(
            principal, subject, fingerprint_body, step_up=confirmed_step_up, challenges=challenges,
            always=always,
        ),
    )


# How long an org-mode settings render waits for this principal's missing auto-accept rule names
# (build_org_routes's _await_rule_names) before showing shortened ids instead -- one connector
# read per unresolved id, normally well under this; anything slower finishes in the background.
_RULE_NAME_WAIT_SECONDS = 3.0

_SCOPED_TOKENS_DISABLED = (
    "Scoped tokens are not enabled on this install. "
    "Your administrator turns them on on the Admin page."
)

_ORG_ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "add_policy_rule", "remove_policy_rule", "update_policy_rule", "set_policy_rule_verbs",
    "set_step_up_scope",
})



def _approved_upstream_audit(approvals: Sequence[Mapping[str, str]]) -> str:
    """The settings audit-line clause naming each approved upstream tool and its gate, sorted by name;
    empty when nothing was approved."""
    names = sorted({a["tool"] for a in approvals})
    if not names:
        return ""
    return ", upstream tools approved: " + ", ".join(
        f"{name} ({auto_accept.TOOL_TO_GATE.get(name, 'unknown')})" for name in names
    )


def build_org_routes(
    *,
    sessions: OrgSessionStore,
    install_wide_settings: dict[str, Any],
    step_up: StepUpConfig,
    step_up_origin: str,
    connector_registry: ConnectorRegistry | None = None,
    scoped_token_policy: ScopedTokenPolicy | None = None,
    scoped_token_store: ScopedTokenStore | None = None,
    scoped_token_page_data: Callable[[Principal], dict[str, Any]] | None = None,
    agent_runtime: agent_runtimes.N8nRuntimeConfig | None = None,
) -> list[BaseRoute]:
    """Org mode's own settings route list (ADR 0032, ADR 0033) --
    ``GET /settings`` serves the exact same
    settings_window_html.build_html() local mode's own
    ``_render_settings_page`` above does, capability-filtered for org mode
    (``mode="org"``, ``is_admin=principal.is_admin`` -- see that function's
    own docstring). It is the same page for an administrator as for anyone
    else: the install-wide privacy policy and the usage stats are Admin
    sections (web/routes_admin.py), and ``GET /settings/privacy`` redirects
    there.

    Writes go through one generic
    ``POST /api/settings/{action}`` -- the exact same path and JSON body
    shape local mode's own dispatcher already answers, restricted to
    ``_ORG_ALLOWED_ACTIONS`` (every action ``org_settings_scope.
    ACTION_SCOPES`` actually routes for ``ORG_MODE``, which is also
    everything the shared page's own JS bridge ever posts for a signed-in
    org principal). The shared renderer's page carries the exact same PF_WEBAUTHN_JS/
    step-up bridge shim local mode's own page does, so a 428/403 step-up
    refusal here shows the same passkey prompt local mode's does, instead
    of a raw JSON body replacing the page.

    This module still contributes the auth/CSRF/authorization/step-up/
    audit machinery, shared with the Admin routes (``_needs_step_up``/
    ``_settings_step_up_response``/``_apply_step_up_gate``,
    ``_record_settings_audit``, ``org_settings_scope.is_action_permitted``).

    ``install_wide_settings`` is the server's live install-wide config; the page only reads it
    to load each signed-in principal's own settings on top of it.

    ``step_up``/``step_up_origin`` gate org mode the way local mode's own
    dispatch is gated (ADR 0034): every org-routed
    ``_SENSITIVE_ACTIONS`` member below (``add_policy_rule``,
    ``remove_policy_rule``) demands the same fresh WebAuthn assertion whenever
    ``step_up.require_passkey`` is on. Required, as
    ``routes_approvals.build_routes``'s own ``step_up``/``issuer_url`` pair
    already is: web/server.py resolves one ``StepUpConfig`` per org and
    threads it to every step-up-aware org route, this one included, rather
    than leaving a default that would silently leave org-mode settings
    writes ungated for a caller that forgets to pass it.

    ``connector_registry`` lets the Auto-accept page show a rule's resource ids (a Drive folder, a
    task list, a Jira project, ...) by name, the way local mode's page does: an id with no cached
    name yet is resolved through *this principal's own* connectors (``ConnectorRegistry.get``),
    into this principal's own ``resource_names.py`` cache -- see ``_await_rule_names`` below.
    ``None`` (a hand-built ``OrgAuth`` with no registry, this module's own older tests) shows
    whatever names are already cached and a shortened id otherwise, never a lookup.

    ``scoped_token_policy`` is what the administrator's org config bundle says about scoped
    tokens (ADR 0165); ``None`` is "not enabled". The four scoped token routes are always
    mounted and answer 403 until it is enabled. ``scoped_token_store`` is the process-wide
    store unless a test passes its own; ``scoped_token_page_data`` builds the Settings
    section's data for the signed-in principal (``None``: the section is not drawn).
    """
    challenges = StepUpChallengeStore()
    token_policy = scoped_token_policy or ScopedTokenPolicy(enabled=False, max_days=DEFAULT_DAYS)
    # Principals with a name lookup already running, so a reload while one is in flight doesn't
    # start a second thread doing the same lookups.
    resolving: set[str] = set()
    resolving_lock = threading.Lock()

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    def _session_email_verified(request: Request) -> bool:
        """Whether this browser session's sign-in verified the person's email."""
        session = sessions.get_session(request.cookies.get(org_session.SESSION_COOKIE, ""))
        return session is not None and session.email_verified

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
        dict (ADR 0032 -- ``settings_action`` below, this function's only
        caller since the four bespoke form-POST routes it used to serve
        are gone) -- ``webauthn_assertion``, if present, is already a
        ``dict``, the same as local mode's own ``payload.get(
        "webauthn_assertion")``, not the JSON-encoded *string* a multipart
        form field carried it as (``_parse_form_assertion``, still used by
        ``handle_org_config_upload`` above, which is still a real multipart
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
        """Org mode's counterpart of ``_snapshot(controller)`` (ADR 0032)
        above -- the exact same settings_window_html.build_html() shape,
        built fresh per request (org mode is stateless per request, unlike
        local mode's single long-lived ``SettingsController``) rather than
        cached anywhere. General's PII fields and Privacy Filter both read
        the install-wide config directly (admin-only in this mode, with a
        fail-closed ``"block"`` default for an unconfigured group --
        unlike local mode's ``"allow"``, see ``_privacy_state_from_config``);
        Auto-accept reads this principal's own on-disk rules; Audit Log
        this principal's own recent decisions; Stats (admin only,
        ADR 0217) install-wide counts from every principal's files on disk. Sections
        this mode never shows at all (Connectors) get an inert
        placeholder -- settings_window_html._capabilities_for is what
        actually keeps them from ever rendering, not the shape of a
        placeholder nothing reads. Must already be called inside
        ``with principal_scope(principal):`` -- ``auto_accept.
        get_policy_v2_rules()``/``_rule_usage_map()`` both read this
        principal's own on-disk files.
        """
        about = _about_state_dict()

        resolver = get_resolver()
        rows = auto_accept.get_policy_rows()
        # The viewing principal's own recent decisions -- per-principal, exactly like
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
                "update_check_enabled": True, "update_check_beta": False,
                "org_installed": False, "org_installed_date": "",
                "version": about["version"],
                "notifications_enabled": True, "notifications_detail": "minimal",
                "step_up_available": False, "step_up_on": False, "step_up_has_passkey": False,
                **step_up_scope_fields(step_up, principal),
            },
            "connectors": [],
            "telegram_auth": {"step": None, "error": ""},
            "peek_auth": {
                "step": None, "error": "", "user_code": "", "verification_url": "", "pinned_base_url": "",
            },
            "grist_signin": "unavailable",
            "grist_server_url_pinned": "",
            "grist_auth": {"error": ""},
            "auto_accept": _auto_accept_state_from_rows(
                rows, _rule_usage_map(), resolve_value=lambda row: cached_row_value(row, resolver),
                resolver=resolver,
            ),
            "audit": {"log_level": "", "log_file": "", "export_hint": "", "recent": recent},
            "about": about,
        }

    def _resolve_rule_names(principal: Principal, pending: list[tuple[Any, str]]) -> None:
        """Worker-thread half of ``_await_rule_names``: resolve each ``(resource type, id)``
        through this principal's own live connector client, caching the name for the next render.
        A connector this principal hasn't authorized has no client, so its ids stay unresolved --
        never an error. Runs under ``principal_scope`` explicitly: an executor thread doesn't
        inherit the request's context, and both the resolver and its on-disk cache are
        per-principal."""
        assert connector_registry is not None  # nosec B101  # only scheduled when a registry exists
        try:
            with principal_scope(principal):
                connectors = connector_registry.get(principal).connectors
                resolver = get_resolver()
                for rt, resource_id in pending:
                    conn = connectors.get(rt.connector)
                    client = getattr(conn, "client", None) if conn is not None else None
                    if client is not None:
                        resolver.resolve(rt, resource_id, client)
        except Exception as exc:  # noqa: BLE001 - a missing name only costs a label, never the page
            logger.warning("Could not resolve auto-accept rule names for %s: %s", principal.id, exc)
        finally:
            with resolving_lock:
                resolving.discard(principal.id)

    async def _await_rule_names(principal: Principal) -> None:
        """Org mode's counterpart of local mode's ``SettingsController._resolve_names_async``.
        Local mode resolves in the background and pushes a fresh snapshot to the open tab once
        done; org mode has no live push (``live_updates=False`` below), so this instead waits up
        to ``_RULE_NAME_WAIT_SECONDS`` for the lookup before the page renders. A slower lookup
        keeps running and fills the cache for the next load rather than holding the request."""
        if connector_registry is None:
            return
        with principal_scope(principal):
            resolver = get_resolver()
            pending: list[tuple[Any, str]] = []
            for row in auto_accept.get_policy_rows():
                compiled = scope_rows.compile_row(row)
                rt, resource_ids = rule_resource_ids(compiled[0]) if compiled else (None, [])
                if rt is not None:
                    pending.extend(
                        (rt, rid) for rid in resource_ids if rid and resolver.needs_resolve(rt, rid)
                    )
        if not pending:
            return
        with resolving_lock:
            if principal.id in resolving:
                return
            resolving.add(principal.id)
        # Its own daemon thread rather than the event loop's executor, so the lookup outliving the
        # wait below never holds up anything that joins that executor (a loop shutting down).
        done = threading.Event()

        def _run() -> None:
            try:
                _resolve_rule_names(principal, pending)
            finally:
                done.set()

        threading.Thread(target=_run, name=f"rule-names-{principal.id}", daemon=True).start()
        await asyncio.to_thread(done.wait, _RULE_NAME_WAIT_SECONDS)

    def _v1_settings_message(principal: Principal, exc: policy_store.V1PolicyConfigError) -> str:
        """What the page tells ``principal`` about a per-user settings.yaml ``reject_v1_sections``
        refused. The daemon loads that file per principal, on first use, so this is the only place
        the refusal can surface -- without it the page is a bare 500. An admin gets the full message
        (the file's path and the fix); anyone else gets only who can fix it, since the path is a
        server-side detail they cannot act on. Logged in full either way."""
        logger.error("Settings for principal %s could not be loaded: %s", principal.id, exc)
        if principal.is_admin:
            return str(exc)
        return (
            "Your PrivacyFence settings file uses an auto-accept format this version no longer reads. "
            "An administrator needs to update it; the server log names the file."
        )

    @contextlib.contextmanager
    def _open_personal_scope(principal: Principal) -> typing.Iterator[None]:
        """``principal_scope`` plus the lazy settings load; a ``V1PolicyConfigError`` propagates
        to ``_personal_scope_error``."""
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            yield

    def _personal_scope_error(principal: Principal, exc: Exception) -> Response | None:
        if isinstance(exc, policy_store.V1PolicyConfigError):
            return JSONResponse({"error": _v1_settings_message(principal, exc)}, status_code=500)
        return None

    def _personal_check_mutation(request: Request, csrf: str | None) -> Response | None:
        if not org_session.check_csrf(request, csrf):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    def _personal_step_up_gate(
        principal: Principal, fingerprint_body: dict[str, Any], assertion: object,
    ) -> Response | None:
        if not (step_up is not None and step_up.enabled and step_up.require_passkey):
            return None
        confirmed_step_up: StepUpConfig = step_up
        response = _verify_or_challenge(
            principal, step_up=confirmed_step_up, origin=step_up_origin.rstrip("/"),
            subject_key=STEP_UP_SUBJECT_KEY,
            fingerprint=_action_fingerprint(STEP_UP_SUBJECT_KEY, fingerprint_body), assertion=assertion,
            challenges=challenges,
            step_up_response=lambda: _settings_step_up_response(
                principal, STEP_UP_SUBJECT_KEY, fingerprint_body,
                step_up=confirmed_step_up, challenges=challenges,
            ),
        )
        if response is not None:
            with principal_scope(principal):
                _record_settings_audit(
                    principal,
                    f"Step-up required for {STEP_UP_SUBJECT_KEY!r}, refused (principal={principal.id})",
                )
        return response

    personal_hooks = PersonalSettingsHooks(
        authenticate=_current_principal,
        check_mutation=_personal_check_mutation,
        open_scope=_open_personal_scope,
        scope_error=_personal_scope_error,
        read_rows=lambda: auto_accept.get_policy_rows(),
        merge_rules=lambda rules: auto_accept.add_policy_v2_rules(rules),
        step_up_state=lambda: (step_up.enabled, step_up.scope),
        step_up_gate=_personal_step_up_gate,
        audit=_record_settings_audit,
        mode="org",
    )

    def _org_scoped_token_data(principal: Principal) -> dict[str, Any] | None:
        if not token_policy.enabled or scoped_token_page_data is None:
            return None
        with principal_scope(principal):
            return scoped_token_page_data(principal)

    async def _org_scoped_token_request(
        request: Request, action: str, fingerprint_body: Callable[[dict[str, Any]], dict[str, Any] | None],
        *, precheck: Callable[[Principal, dict[str, Any], dict[str, Any]], Response | None] | None = None,
    ) -> tuple[Principal, dict[str, Any]] | Response:
        """The gates the org scoped token routes share (ADR 0165), in
        ``settings_action``'s order: session, CSRF, origin, the administrator's
        opt-in, confirmation, then the passkey step-up when the org requires
        one. Returns the principal and the step-up fingerprint body, or the
        response that refuses the request. ``precheck`` runs after the
        fingerprint body and before step-up, so a request that is refused
        anyway never asks for a passkey."""
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if not token_policy.enabled:
            return JSONResponse({"error": _SCOPED_TOKENS_DISABLED}, status_code=403)
        if payload.get("confirmed") is not True:
            return JSONResponse({"error": "confirmation required"}, status_code=400)
        bound = fingerprint_body(payload)
        if bound is None:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        if precheck is not None:
            refused = precheck(principal, payload, bound)
            if refused is not None:
                return refused
        if step_up is not None and step_up.enabled and step_up.require_passkey:
            confirmed_step_up: StepUpConfig = step_up
            response = _verify_or_challenge(
                principal, step_up=confirmed_step_up, origin=step_up_origin.rstrip("/"),
                subject_key=action, fingerprint=_action_fingerprint(action, bound),
                assertion=payload.get("webauthn_assertion"), challenges=challenges,
                step_up_response=lambda: _settings_step_up_response(
                    principal, action, bound, step_up=confirmed_step_up, challenges=challenges,
                ),
            )
            if response is not None:
                with principal_scope(principal):
                    _record_settings_audit(
                        principal, f"Step-up required for {action!r}, refused (principal={principal.id})",
                    )
                return response
        return principal, bound

    async def _create_and_deliver(
        principal: Principal, bound: dict[str, Any], *, session_email_verified: bool = False, audit_suffix: str = "",
        upstream_approvals: Sequence[Mapping[str, str]] = (),
    ) -> tuple[ScopedToken | None, Response]:
        """Mint the token and answer with its secret, once. Returns the token (``None`` when
        nothing was created) and the response to give."""
        store = scoped_token_store or get_store()
        from .. import upstream_mcp_runtime  # function-local: only organization mode with upstreams uses it

        held_audiences = (
            upstream_mcp_runtime.held_audiences(principal, session_email_verified)
            if upstream_mcp_runtime.configured() else {}
        )
        with principal_scope(principal):
            try:
                _ensure_principal_settings_loaded()
            except policy_store.V1PolicyConfigError as exc:
                return None, JSONResponse({"error": _v1_settings_message(principal, exc)}, status_code=500)
            try:
                token, secret = await run_in_threadpool(
                    lambda: store.create(
                        principal, label=bound["label"], days=bound["days"], rules=bound["rules"],
                        tools=bound["tools"], policy=token_policy,
                        allow_personal_data=bound["allow_personal_data"],
                        allow_personal_data_reads=bound["allow_personal_data_reads"],
                        upstream_audiences=bound.get("upstream_audiences") or None,
                        principal_email_verified=session_email_verified,
                        held_audiences=held_audiences,
                        is_admin=principal.is_admin,
                        upstream_approvals=upstream_approvals,
                    ),
                )
            except ScopedTokenError as exc:
                return None, JSONResponse({"error": str(exc)}, status_code=400)
            # The summary names the token, never the secret (ADR 0165).
            _record_settings_audit(
                principal,
                f"Created scoped token '{token.label}' ({token.id}), {bound['days']} days, "
                f"{len(token.rules)} rule(s), {len(token.tools)} tool(s)"
                + (", personal data allowed" if token.allow_personal_data else "")
                + (", personal data in reads allowed" if token.allow_personal_data_reads else "")
                + _approved_upstream_audit(upstream_approvals)
                + audit_suffix
                + f" (principal={principal.id})",
            )
        # The only place the secret ever leaves the server: this one answer.
        return token, JSONResponse(
            {"created": {"id": token.id, "label": token.label, "expires_at": token.expires_at}, "secret": secret},
            headers={"Cache-Control": "no-store"},
        )

    async def scoped_token_create(request: Request) -> Response:
        gated = await _org_scoped_token_request(request, "create_scoped_token", normalise_create_request)
        if isinstance(gated, Response):
            return gated
        principal, bound = gated
        _token, response = await _create_and_deliver(
            principal, bound, session_email_verified=_session_email_verified(request),
            upstream_approvals=bound["upstream_approvals"],
        )
        return response

    def _proposal_fingerprint_body(payload: dict[str, Any]) -> dict[str, Any] | None:
        body = normalise_create_request(payload)
        proposal_id = payload.get("proposal_id")
        if body is None or not isinstance(proposal_id, str):
            return None
        return {**body, "proposal_id": proposal_id}

    def _proposal_precheck(principal: Principal, payload: dict[str, Any], bound: dict[str, Any]) -> Response | None:
        if not token_policy.allow_ai_proposals:
            return JSONResponse({"error": PROPOSALS_DISABLED_MESSAGE}, status_code=403)
        if payload.get("typed_label") != bound["label"]:
            return JSONResponse({"error": "Type the token's name exactly to confirm."}, status_code=400)
        proposal = scoped_token_proposals.get_registry().get(principal.id, bound["proposal_id"])
        if proposal is None:
            return JSONResponse({"error": scoped_token_proposals.GONE_MESSAGE}, status_code=410)
        if scoped_token_proposals.narrowing_error(proposal.request, bound) is not None:
            return JSONResponse({"error": scoped_token_proposals.WIDENED_MESSAGE}, status_code=400)
        return None

    async def _deliver_proposal(
        principal: Principal, bound: dict[str, Any], proposal: scoped_token_proposals.TokenProposal,
        *, session_email_verified: bool = False,
    ) -> Response:
        """Create the token from a delivery proposal and put it into the runtime. The secret goes to
        the runtime and is never in the answer (ADR 0278). The proposal comes back on a refusal that
        left nothing behind, not on a 500, where a token may still exist."""
        registry = scoped_token_proposals.get_registry()
        api = agent_runtimes.api_for(agent_runtime) if agent_runtime is not None else None
        if agent_runtime is None or api is None:
            registry.restore(proposal)
            return JSONResponse({"error": agent_runtimes.DELIVERY_UNAVAILABLE}, status_code=503)
        store = scoped_token_store or get_store()
        from .. import upstream_mcp_runtime  # function-local: only organization mode with upstreams uses it

        held_audiences = (
            upstream_mcp_runtime.held_audiences(principal, session_email_verified)
            if upstream_mcp_runtime.configured() else {}
        )
        with principal_scope(principal):
            try:
                _ensure_principal_settings_loaded()
            except policy_store.V1PolicyConfigError as exc:
                return JSONResponse({"error": _v1_settings_message(principal, exc)}, status_code=500)
            outcome = await run_in_threadpool(
                lambda: runtime_credentials.deliver(
                    principal=principal, bound=bound, proposal=proposal, config=agent_runtime, api=api,
                    token_store=store, policy=token_policy, on_abandoned=runtime_credential_sweeper.wake,
                    held_audiences=held_audiences, principal_email_verified=session_email_verified,
                ),
            )
            token = outcome.token
            record = outcome.record
            if token is None or record is None:
                if outcome.status != 500:
                    registry.restore(proposal)
                return JSONResponse({"error": outcome.error}, status_code=outcome.status)
            _record_settings_audit(
                principal,
                f"Created scoped token '{token.label}' ({token.id}), {bound['days']} days, "
                f"{len(token.rules)} rule(s), {len(token.tools)} tool(s)"
                + (", personal data allowed" if token.allow_personal_data else "")
                + (", personal data in reads allowed" if token.allow_personal_data_reads else "")
                + " from a proposal, delivered to n8n"
                + f" (principal={principal.id})",
            )
        scoped_token_proposals.record(
            "scoped_token_proposal_accepted", proposal,
            f"Token proposal '{proposal.request['label']}' accepted as '{token.label}' ({token.id})",
        )
        return JSONResponse(
            {
                "created": {"id": token.id, "label": token.label, "expires_at": token.expires_at},
                "delivered": {"runtime": record.runtime, "credential_name": record.credential_name},
            },
            headers={"Cache-Control": "no-store"},
        )

    async def scoped_token_create_from_proposal(request: Request) -> Response:
        gated = await _org_scoped_token_request(
            request, "create_proposed_scoped_token", _proposal_fingerprint_body, precheck=_proposal_precheck,
        )
        if isinstance(gated, Response):
            return gated
        principal, bound = gated
        registry = scoped_token_proposals.get_registry()
        proposal = registry.take(principal.id, bound["proposal_id"])
        if proposal is None:
            return JSONResponse({"error": scoped_token_proposals.GONE_MESSAGE}, status_code=410)
        if proposal.delivery is not None:
            return await _deliver_proposal(
                principal, bound, proposal, session_email_verified=_session_email_verified(request),
            )
        token, response = await _create_and_deliver(
            principal, bound, session_email_verified=_session_email_verified(request), audit_suffix=" from a proposal",
        )
        if token is None:
            registry.restore(proposal)
            return response
        scoped_token_proposals.record(
            "scoped_token_proposal_accepted", proposal,
            f"Token proposal '{proposal.request['label']}' accepted as '{token.label}' ({token.id})",
        )
        return response

    async def scoped_token_decline_proposal(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        proposal_id = payload.get("proposal_id")
        if not isinstance(proposal_id, str):
            return JSONResponse({"error": "invalid request"}, status_code=400)
        proposal = scoped_token_proposals.get_registry().take(principal.id, proposal_id)
        if proposal is None:
            return JSONResponse({"error": scoped_token_proposals.GONE_MESSAGE}, status_code=410)
        scoped_token_proposals.record(
            "scoped_token_proposal_declined", proposal,
            f"Token proposal '{proposal.request['label']}' declined",
        )
        return JSONResponse({"declined": True})

    async def scoped_token_revoke(request: Request) -> Response:
        gated = await _org_scoped_token_request(request, "revoke_scoped_token", _scoped_revoke_fingerprint_body)
        if isinstance(gated, Response):
            return gated
        principal, bound = gated
        store = scoped_token_store or get_store()
        token = store.get(principal.id, bound["token_id"])
        if token is None or not await run_in_threadpool(store.revoke, principal, bound["token_id"]):
            return JSONResponse({"error": "No such token."}, status_code=404)
        runtime_credential_sweeper.wake()
        with principal_scope(principal):
            _record_settings_audit(
                principal, f"Revoked scoped token '{token.label}' ({token.id}) (principal={principal.id})",
            )
        return JSONResponse({"revoked": {"id": token.id}})

    async def scoped_token_rename(request: Request) -> Response:
        gated = await _org_scoped_token_request(request, "rename_scoped_token", _scoped_rename_fingerprint_body)
        if isinstance(gated, Response):
            return gated
        principal, bound = gated
        store = scoped_token_store or get_store()
        try:
            result = await run_in_threadpool(store.rename, principal, bound["token_id"], bound["label"])
        except ScopedTokenError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if result is None:
            return JSONResponse({"error": "No such token."}, status_code=404)
        old_label, renamed = result
        if old_label != renamed.label:
            with principal_scope(principal):
                _record_settings_audit(
                    principal,
                    f"Renamed scoped token '{old_label}' to '{renamed.label}' ({renamed.id}) "
                    f"(principal={principal.id})",
                )
        return JSONResponse({"renamed": {"id": renamed.id, "label": renamed.label}})

    async def scoped_token_extend(request: Request) -> Response:
        gated = await _org_scoped_token_request(request, "extend_scoped_token", _scoped_extend_fingerprint_body)
        if isinstance(gated, Response):
            return gated
        principal, bound = gated
        store = scoped_token_store or get_store()
        try:
            result = await run_in_threadpool(
                store.extend, principal, bound["token_id"], bound["days"], policy=token_policy,
            )
        except ScopedTokenError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if result is None:
            return JSONResponse({"error": "No such token."}, status_code=404)
        _old_expiry, token = result
        date = datetime.fromtimestamp(token.expires_at, timezone.utc).strftime("%Y-%m-%d")
        text = f"Extended scoped token '{token.label}' ({token.id}) to {date}, {bound['days']} days"
        record = runtime_credentials.get_store().for_token(principal.id, token.id)
        if record is not None and record.state == runtime_credentials.DELIVERED:
            text += f", delivered to n8n as '{runtime_credentials.strip_control(record.credential_name)}'"
        with principal_scope(principal):
            _record_settings_audit(principal, f"{text} (principal={principal.id})")
        return JSONResponse({"extended": {"id": token.id, "label": token.label, "expires_at": token.expires_at}})

    async def scoped_token_revoke_all(request: Request) -> Response:
        gated = await _org_scoped_token_request(request, "revoke_all_scoped_tokens", lambda payload: {})
        if isinstance(gated, Response):
            return gated
        principal, _bound = gated
        store = scoped_token_store or get_store()
        count = await run_in_threadpool(store.revoke_all, principal.id)
        runtime_credential_sweeper.wake()
        with principal_scope(principal):
            _record_settings_audit(principal, f"Revoked all scoped tokens ({count}) (principal={principal.id})")
        return JSONResponse({"revoked": count})

    async def _wrap_org_settings(
        request: Request, principal: Principal, *, initial_section: str,
        scoped_proposal: dict[str, Any] | None = None,
    ) -> Response:
        nonce = _csp_nonce_for(request)
        with principal_scope(principal):
            try:
                _ensure_principal_settings_loaded()
            except policy_store.V1PolicyConfigError as exc:
                body = (
                    '<section class="pf-settings-error" role="alert"><h1>Settings could not be loaded</h1>'
                    f"<p>{_html_escape(_v1_settings_message(principal, exc))}</p></section>"
                )
                page = web_shell.wrap(
                    body, title="PrivacyFence — Settings", active="settings", nonce=nonce,
                    nav_items=web_shell.org_nav_items(principal.is_admin),
                    principal_label=principal.email or principal.display_name or principal.id,
                    live_updates=False, notifications_enabled=False,
                )
                return HTMLResponse(page, status_code=500, headers={"Cache-Control": "no-store"})
        await _await_rule_names(principal)
        with principal_scope(principal):
            state = _org_state(principal)
        body = settings_window_html.build_html(
            state, nonce=nonce, initial_section=initial_section,
            is_admin=principal.is_admin,
            scoped_tokens=_org_scoped_token_data(dataclasses.replace(principal, email_verified=_session_email_verified(request))),
            personal_transfer=True,
            scoped_proposal=scoped_proposal,
        )
        csrf = request.cookies.get(org_session.SESSION_COOKIE, "")
        whats_new_version = await asyncio.to_thread(whats_new.pending_version, principal)
        # PF_WEBAUTHN_JS/_settings_bridge_shim: the exact same ceremony helpers local
        # mode's own _render_settings_page carries -- see that function's
        # own comment. Whether it's ever exercised depends on this org's
        # StepUpConfig, not on whether the helpers exist.
        body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
        body += _settings_bridge_shim(csrf=csrf, repo_url=REPO_URL, nonce=nonce)
        body += personal_transfer_shim(csrf=csrf, nonce=nonce)
        html = web_shell.wrap(
            body, title="PrivacyFence — Settings", active="settings", nonce=nonce,
            nav_items=web_shell.org_nav_items(principal.is_admin), principal_label=principal.email or principal.display_name or principal.id,
            # Org mode has no per-request settings push to subscribe a
            # live stream to (state_stream.py's own StateStream.push_
            # settings is wired to one local SettingsController's own
            # on_change, module docstring).
            live_updates=False, notifications_enabled=False,
            csrf=csrf, whats_new_version=whats_new_version,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def settings_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        # An org admin's Settings is the same page as anyone else's: their own rules first.
        return await _wrap_org_settings(request, principal, initial_section="auto_accept")

    async def privacy_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/settings/privacy", status_code=302, headers={"Cache-Control": "no-store"},
            )
        # The install-wide privacy policy is an Admin section now.
        return RedirectResponse("/admin", status_code=302, headers={"Cache-Control": "no-store"})

    def _scoped_tokens_page_principal(request: Request, *, proposals: bool) -> Principal | Response:
        """The signed-in principal, or the redirect that sends the page elsewhere: to login (and
        back to this page) when signed out, to ``/settings`` when scoped tokens are switched off
        or, for a proposal's review page, when AI proposals are."""
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                f"/login?next={request.url.path}", status_code=302, headers={"Cache-Control": "no-store"},
            )
        if not token_policy.enabled or (proposals and not token_policy.allow_ai_proposals):
            return RedirectResponse("/settings", status_code=303, headers={"Cache-Control": "no-store"})
        return principal

    async def scoped_tokens_page(request: Request) -> Response:
        principal = _scoped_tokens_page_principal(request, proposals=False)
        if isinstance(principal, Response):
            return principal
        return await _wrap_org_settings(request, principal, initial_section="scoped_tokens")

    async def scoped_token_proposal_page(request: Request) -> Response:
        principal = _scoped_tokens_page_principal(request, proposals=True)
        if isinstance(principal, Response):
            return principal
        shown: dict[str, Any] = {"missing": True}
        proposal = scoped_token_proposals.get_registry().get(principal.id, request.path_params["proposal_id"])
        if proposal is not None:
            with principal_scope(principal):
                try:
                    _ensure_principal_settings_loaded()
                except policy_store.V1PolicyConfigError:
                    pass  # _wrap_org_settings meets the same error and renders it
                else:
                    shown = scoped_token_proposals.review_data(
                        proposal, auto_accept.get_policy_rows(),
                        runtime_host=(urlsplit(agent_runtime.base_url).hostname or "") if agent_runtime else "",
                        project_id=agent_runtime.project_id if agent_runtime else "",
                    )
        return await _wrap_org_settings(
            request, principal, initial_section="scoped_tokens", scoped_proposal=shown,
        )

    def _apply_org_action(principal: Principal, action: str, body: dict[str, Any]) -> str | None:
        """Applies one ``_ORG_ALLOWED_ACTIONS`` mutation, scoped to
        ``principal`` (already entered by the caller), returning the exact
        summary string to audit-log, or ``None`` when nothing actually
        changed -- the same "only audit a real change" behavior the former
        per-action routes kept before they were folded into this one
        dispatcher (ADR 0032). May raise ``org_install_policy.PolicyChangeRejected``/
        ``OSError``, left for the caller to map to a response the same way
        the former ``_apply_install_wide`` did.
        """
        if action == "add_policy_rule":
            group = str(body.get("group", ""))
            verb_enums = policy_catalogue.parse_verbs(body.get("verbs") or [])
            if not verb_enums:
                return None
            value = _parse_value_list(str(body.get("value") or ""), group)
            try:
                new_rules = policy_catalogue.rules_for_catalogue_entry(group, value, verb_enums)
            except RuleValueError as exc:
                raise org_install_policy.PolicyChangeRejected(str(exc)) from exc
            if not new_rules:
                return None
            row_ids = auto_accept.policy_row_ids_for(new_rules)
            if not auto_accept.add_policy_v2_rules(new_rules):
                return None
            verb_names = ", ".join(verb.value for verb in verb_enums)
            return (
                f"Added auto-accept rule ({group!r}, allow {verb_names!r}) to "
                f"{', '.join(row_ids)} (principal={principal.id})"
            )
        if action == "remove_policy_rule":
            rule_id = str(body.get("rule_id", ""))
            if not auto_accept.remove_policy_v2_rule(rule_id):
                return None
            return f"Removed auto-accept rule {rule_id!r} (principal={principal.id})"
        if action == "update_policy_rule":
            rule_id = str(body.get("rule_id", ""))
            added = auto_accept.update_policy_v2_rule(rule_id)
            if not added:
                return None
            return (
                f"Updated auto-accept rule {rule_id!r}, added {', '.join(sorted(added))} "
                f"(principal={principal.id})"
            )
        if action == "set_policy_rule_verbs":
            rule_id = str(body.get("rule_id", ""))
            verb_enums = policy_catalogue.parse_verbs(body.get("verbs") or [])
            if not auto_accept.set_policy_rule_verbs(rule_id, list(verb_enums)):
                return None
            verb_names = ", ".join(sorted(verb.value for verb in verb_enums))
            return (
                f"Changed auto-accept rule {rule_id!r} to allow {verb_names!r} "
                f"(principal={principal.id})"
            )
        if action == "set_step_up_scope":
            return _set_step_up_scope(principal, body)
        raise AssertionError(f"unrouted org action {action!r}")  # pragma: no cover -- _ORG_ALLOWED_ACTIONS gates it

    def _set_step_up_scope(principal: Principal, body: dict[str, Any]) -> str | None:
        """This principal's own step-up scope (ADR 0091): widens, never
        narrows, the org's ``step_up.scope`` for their approvals only."""
        before = effective_scope(step_up, principal)
        try:
            after = set_personal_scope(principal, str(body.get("scope", "")), floor=step_up.scope)
        except ValueError as exc:
            raise org_install_policy.PolicyChangeRejected(str(exc)) from exc
        if after == before:
            return None
        return f"Step-up scope for this principal changed from {before!r} to {after!r} (principal={principal.id})"

    async def settings_action(request: Request) -> Response:
        """The generic ``POST /api/settings/{action}`` dispatcher org
        mode shares (ADR 0032) -- the same path and JSON body shape local mode's
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
            try:
                _ensure_principal_settings_loaded()
            except policy_store.V1PolicyConfigError as exc:
                return JSONResponse({"error": _v1_settings_message(principal, exc)}, status_code=500)
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
        # A rule just added may name an id this principal's page has never resolved.
        await _await_rule_names(principal)
        with principal_scope(principal):
            state = _org_state(principal)
        return JSONResponse(state)

    return [
        Route("/settings", settings_page),
        Route("/settings/privacy", privacy_page),
        Route("/api/settings/scoped_tokens/create", scoped_token_create, methods=["POST"]),
        Route("/api/settings/scoped_tokens/revoke", scoped_token_revoke, methods=["POST"]),
        Route("/api/settings/scoped_tokens/rename", scoped_token_rename, methods=["POST"]),
        Route("/api/settings/scoped_tokens/extend", scoped_token_extend, methods=["POST"]),
        Route("/api/settings/scoped_tokens/revoke_all", scoped_token_revoke_all, methods=["POST"]),
        Route("/settings/scoped_tokens", scoped_tokens_page),
        Route("/settings/scoped_tokens/proposals/{proposal_id}", scoped_token_proposal_page),
        Route(
            "/api/settings/scoped_tokens/create_from_proposal", scoped_token_create_from_proposal,
            methods=["POST"],
        ),
        Route("/api/settings/scoped_tokens/decline_proposal", scoped_token_decline_proposal, methods=["POST"]),
        *build_personal_routes(personal_hooks),
        Route("/api/settings/{action}", settings_action, methods=["POST"]),
    ]
