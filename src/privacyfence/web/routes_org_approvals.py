"""The web approval surface, for org mode (P9, docs/https-connector-
refactor-plan.md's own P9 section). Not mounted through P8 -- web/server.py's
own module docstring explained why: the local-mode surface
(web/routes_approvals.py) authenticates with one shared secret and lists
*every* pending approval with no principal filtering, so exposing it as-is
under org mode would leak every principal's pending approvals to whoever
holds any valid token. This module is the principal-aware replacement P7's
own writeup named as real, scoped follow-up work: ``GET /approvals``,
``GET /approvals/{id}``, ``POST /api/approvals/{id}/decide`` and
``GET /api/approvals/stream`` all authorize against
``org_session.authenticated()`` and filter/authorize every read and write
through ``current_principal()`` (approvals.PendingApprovalRegistry's own P9
principal dimension -- see that module's docstring), exactly §10.5's
"every approval ... read is authorized against current_principal()".

Reuses web/routes_approvals.py's own ``_inject_shim``/``_SW_JS`` and its
decided-message vocabulary rather than duplicating them -- the card
document itself (approval_window_html.py's output) and the service worker
are principal-agnostic; only the *auth model* around them differs between
local and org mode, same as web/routes_connect.py's own relationship to
routes_settings.py.

**Step-up (§10.6, D7)** is the one piece with no local-mode analogue at
all: before releasing a *write* decision (or, with ``step_up.scope ==
"writes_and_pii_reads"``, a PII-flagged read too -- or, with
``"writes_and_reads"``, every gated read), the decide endpoint
demands proof of a fresh WebAuthn platform-authenticator assertion --
webauthn_stepup.py's own module docstring covers the ceremony and the
decision-fingerprint binding; this module is only the HTTP protocol
wrapping it: a first decide attempt with no ``webauthn_assertion`` gets a
``428`` carrying fresh assertion options (when a passkey is enrolled) and
an IdP re-auth link (as D7's own fallback), and a second attempt
carrying the completed assertion is verified and, on success, treated as
the original decision. **Deny needs no step-up** -- denying leaks nothing
(the same reasoning approval_list_html.py's own module docstring gives for
letting Deny live on the list row with no card at all), so step-up is
scoped to the two approving results (``accept``/``accept_all``) only.

**``step_up.require_passkey`` (#406)** closes the IdP-reauth fallback for
orgs that want hardware-bound WebAuthn as a hard requirement. With it set,
the ``428`` never carries ``idp_stepup_url``, ``/api/approvals/{id}/
stepup/idp`` refuses outright (not just "unadvertised" -- a client hitting
it directly gets a ``403`` too, see ``stepup_idp_start`` below), and a
principal with no enrolled passkey gets a ``403`` naming ``/security`` as
where to enroll one instead of the usual ``428``. See ``_step_up_response``.

The IdP re-auth path (``GET /api/approvals/{id}/stepup/idp`` ->
``GET /oauth/stepup/callback``) mirrors web/routes_org_identity.py's own
``/login`` flow almost exactly (same org_identity.py functions, same
single-use ``state``-keyed pending-attempt store) with one addition: the
callback must re-derive the *same* principal the step-up was started for,
not merely *a* signed-in principal -- otherwise a second IdP account
signing in through a leaked step-up link could authorize someone else's
pending decision. See ``_StepUpAuthAttemptStore``/``stepup_callback``
below.

**The approval binder's own batch step-up (Phase 3 of the binder plan)**
gates ``POST /api/approvals/batch/decide`` (Phase 2) on one WebAuthn
assertion bound to the whole submitted set
(``webauthn_stepup.batch_decision_fingerprint``), scoped to
``current_principal()`` the same way everything else here is. Unlike this
module's own single-decision ``_step_up_response``, its batch counterpart
never offers the IdP-reauth fallback -- see ``_batch_step_up_response``'s
own docstring for what that costs when nothing is enrolled and
``require_passkey`` is off.

**A confirm dialog that is itself the gate (the self-approval review's
Phase 4)** is the one place ``_STEP_UP_RESULTS``' "only an approving
decision" rule under-reaches. It is right about every confirm dialog that
existed when it was written -- the PII one and the popup's "Always allow"
one, both raised from inside a ``gate.gated_call`` whose own card already
took this gate. It is wrong about ``gate.propose_policy_change`` (P7 of the
policy v2 redesign) and its deprecated predecessor ``propose_rule_change``:
an MCP client asks for a rule, no card is shown, and the dialog is the whole
gate on a change to what auto-accepts for this principal from here on. Those
register with ``sensitive=True`` (approvals.PendingApprovalRegistry.
register_confirm) and are held to the same passkey check a write decision
takes -- gated on ``require_passkey`` rather than on ``scope``, since a rule
is not a read or a write but the thing that decides which of those get asked
about at all, the same line web/routes_settings.py's own ``_needs_step_up``
draws. Where ``require_passkey`` is off, a step-up here would fall back to
the IdP re-auth this session already is, so it does not run."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import approval_list_html, approval_window_html, org_identity, web_shell, webauthn_stepup
from ..approvals import BATCH_RESULTS, CONFIRM_RESULTS
from ..org_identity import IdpConfig
from ..principal import Principal
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore, WebAuthnError
from ..web_approval_ui import WebApprovalUI
from . import org_session, step_up_decide
from .csp import nonce_for as _csp_nonce_for
from .csp import set_nonce as _set_csp_nonce
from .org_session import OrgSessionStore
from .routes_approvals import _DECIDED_MESSAGE, _DENIED_MESSAGE, _ALREADY_DECIDED_MESSAGE, _FAILED_MESSAGE
from .routes_approvals import _inject_shim, _SW_JS
from .routes_security import PF_WEBAUTHN_JS

logger = logging.getLogger(__name__)

_STREAM_POLL_SECONDS = 1.0
_STEP_UP_ATTEMPT_TTL_SECONDS = 10 * 60

# Only an *approving* decision needs step-up -- see module docstring.
_STEP_UP_RESULTS = ("accept", "accept_all")

# The batch decide endpoint's own vocabulary (approvals.BATCH_RESULTS) has
# no "accept_all" -- see that constant's own comment -- so the approving
# result step-up applies to is just this one.
_BATCH_STEP_UP_RESULTS = ("accept",)


# --------------------------------------------------------------------- #
# IdP re-auth attempt state -- mirrors web/routes_org_identity.py's own
# _LoginAttemptStore / web/routes_connect.py's own _PendingAuthStore.
# --------------------------------------------------------------------- #

@dataclass
class _StepUpAuthAttempt:
    principal_id: str
    approval_id: str
    result: str
    choice: int | None
    nonce: str
    code_verifier: str
    created_at: float = field(default_factory=time.time)


class _StepUpAuthAttemptStore:
    def __init__(self, ttl: float = _STEP_UP_ATTEMPT_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _StepUpAuthAttempt] = {}

    def put(self, state: str, attempt: _StepUpAuthAttempt) -> None:
        self._prune()
        with self._lock:
            self._pending[state] = attempt

    def pop(self, state: str) -> _StepUpAuthAttempt | None:
        with self._lock:
            return self._pending.pop(state, None)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, a in self._pending.items() if (now - a.created_at) > self._ttl]
            for s in stale:
                del self._pending[s]


def _org_bridge_shim(*, decide_url: str, csrf: str, stepup_options_url: str, nonce: str) -> str:
    """The org-mode counterpart of web/routes_approvals.py's own
    ``_bridge_shim`` -- same ``window.webkit.messageHandlers.pf.postMessage``
    swap, plus the step-up branch a ``428`` response triggers (see module
    docstring). ``stepup_options_url`` is unused by the JS below directly
    (the ``428`` body already carries fresh options inline, see
    ``decide()``) but is threaded through so a future retry-without-a-body
    variant has somewhere to fetch a fresh challenge from without a second
    server-side endpoint to design; today's flow never needs it because the
    first ``428`` already includes everything the client needs.

    ``nonce`` (SEC-08): same
    role as web/routes_approvals.py's own ``_bridge_shim`` -- this is a real
    ``<script>`` element injected into an already-rendered card document,
    so it must carry that document's own nonce (``show_approval`` below
    recovers it via approval_window_html.extract_csp_nonce).
    """
    del stepup_options_url  # reserved -- see docstring
    return (
        f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
        + f'<script nonce="{nonce}">(function(){{'
        "window.webkit = window.webkit || {};"
        "window.webkit.messageHandlers = window.webkit.messageHandlers || {};"
        "function pfDecide(body){"
        f"return fetch({decide_url!r}, {{method:'POST', credentials:'same-origin',"
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});"
        "}"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        f"var body = Object.assign({{}}, payload, {{csrf: {csrf!r}}});"
        "var isDeny = payload.result === 'deny' || payload.result === 'cancel';"
        "pfDecide(body).then(function(r){"
        "  if (r.status === 428) {"
        "    return r.json().then(function(data){"
        "      if (data.webauthn_options && window.PublicKeyCredential) {"
        "        return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(assertion){"
        "          var retryBody = Object.assign({}, body, {webauthn_assertion: assertion});"
        "          return pfDecide(retryBody);"
        "        }).catch(function(err){"
        "          if (data.idp_stepup_url) {"
        "            document.body.innerHTML = 'This approval needs extra verification. "
        "<a href=\"' + data.idp_stepup_url + '\">Verify by signing in again</a>';"
        "          } else {"
        f"            document.body.innerHTML = {_FAILED_MESSAGE!r} + ' (' + err.message + ')';"
        "          }"
        "          return null;"
        "        });"
        "      }"
        "      if (data.idp_stepup_url) {"
        "        document.body.innerHTML = 'This approval needs extra verification. "
        "<a href=\"' + data.idp_stepup_url + '\">Verify by signing in again</a>';"
        "        return null;"
        "      }"
        f"      document.body.innerHTML = {_FAILED_MESSAGE!r};"
        "      return null;"
        "    });"
        "  }"
        "  if (r.status === 403) {"
        "    return r.json().then(function(data){"
        "      if (data.enroll_url) {"
        "        document.body.innerHTML = 'This organization requires a passkey for this approval. "
        "<a href=\"' + data.enroll_url + '\">Set up a passkey</a>';"
        "      } else {"
        f"        document.body.innerHTML = {_FAILED_MESSAGE!r};"
        "      }"
        "      return null;"
        "    });"
        "  }"
        "  return r;"
        "}).then(function(r){"
        "  if (r === null) { return; }"
        "  var msg = null;"
        f"  if (r.ok) {{ msg = isDeny ? {_DENIED_MESSAGE!r} : {_DECIDED_MESSAGE!r}; }}"
        f"  else if (r.status === 409) {{ msg = {_ALREADY_DECIDED_MESSAGE!r}; }}"
        "  if (msg !== null) {"
        "    try { sessionStorage.setItem('pf_toast', JSON.stringify({msg: msg})); } catch (e) {}"
        "    window.location.replace('/approvals');"
        "    return;"
        "  }"
        f"  document.body.innerHTML = {_FAILED_MESSAGE!r};"
        "})"
        f".catch(function(){{ document.body.innerHTML = {_FAILED_MESSAGE!r}; }});"
        "}};"
        "})();</script>"
    )


def _render_list_page(rows: list, *, csrf: str, nonce: str, principal_label: str = "") -> str:
    """The org-mode ``/approvals`` page, in the same shell local mode uses.

    This used to be a bare document: tokens, one body font rule, the list,
    and a centred footer of three links that nothing styled -- so they
    rendered browser-default blue against a warm grey palette. No header,
    no brand, no nav, no favicon, and on a phone no navigation at all. It
    is also the surface a paying organization actually looks at.

    ``live_updates=False`` is not a simplification. Org mode's app mounts
    no ``GET /api/state/stream`` at all (web/server.py's
    ``_build_org_app``), so there is nothing behind the shell's live
    indicator here; rendering it anyway would either claim a liveness that
    doesn't exist or sit permanently on a connection error, on the one
    surface whose whole job is to be trusted. The list is still correct on
    load -- it just no longer claims to be self-updating. Tier-0/1
    notifications ride the same stream, so they go with it.
    """
    body = approval_list_html.build_list_html(rows, csrf=csrf, nonce=nonce)
    # PF_WEBAUTHN_JS (approval binder Phase 3): needed here whenever
    # Approve-selected's own 428 branch (approval_list_html.py's own JS)
    # has to run a ceremony -- always injected, same reasoning
    # show_approval's own shim below gives.
    body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
    return web_shell.wrap(
        body,
        title="PrivacyFence — Approvals",
        active="approvals",
        nonce=nonce,
        nav_items=web_shell.ORG_NAV_ITEMS,
        principal_label=principal_label,
        live_updates=False,
        notifications_enabled=False,
    )


def build_routes(
    *, web_ui: WebApprovalUI, sessions: OrgSessionStore, step_up: StepUpConfig, idp: IdpConfig, issuer_url: str,
) -> list[Route]:
    challenges = StepUpChallengeStore()
    stepup_attempts = _StepUpAuthAttemptStore()
    origin = issuer_url.rstrip("/")
    registry = web_ui.deferred_registry

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    async def index(request: Request) -> Response:
        return RedirectResponse("/approvals", status_code=302, headers={"Cache-Control": "no-store"})

    async def list_approvals(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/approvals", status_code=302, headers={"Cache-Control": "no-store"})
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        rows = [approval_list_html.row_from_approval(card) for card in registry.list_pending(principal.id)]
        html = _render_list_page(
            rows, csrf=session_id, nonce=_csp_nonce_for(request),
            # Every read and write on this page is authorized against this
            # principal, and the page never said whose queue it was. The
            # cosmetic fields first (see principal.py), falling back to the
            # opaque id rather than rendering an unlabelled header.
            principal_label=principal.email or principal.display_name or principal.id,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def show_approval(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                f"/login?next=/approvals/{request.path_params['id']}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        approval_id = request.path_params["id"]
        card = registry.get(approval_id, principal_id=principal.id)
        if card is None or card.event.is_set():
            return HTMLResponse(
                "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
                "This approval is no longer pending — it may already have been decided, "
                "or the link has expired. <a href=\"/approvals\">Back to approvals</a>"
                "</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        if not card.html:
            # See web/routes_approvals.py's own show_approval: card HTML is
            # only built on gate.py's _popup_executor, so a card whose
            # worker hasn't run yet has card.html == "" -- routine past that
            # executor's worker count, not exceptional. Serve a placeholder
            # rather than let _inject_shim's html.index("</head>") raise
            # into a 500.
            return HTMLResponse(
                "<!DOCTYPE html><html><head><meta http-equiv=\"refresh\" content=\"2\">"
                "</head><body style=\"font:15px sans-serif;padding:40px\">"
                "Preparing this request — it will be ready in a moment. "
                "<a href=\"/approvals\">Back to approvals</a>"
                "</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        # SEC-08 -- see
        # web/routes_approvals.py's own show_approval for why this document's
        # nonce has to be recovered from the body rather than taken fresh.
        nonce = approval_window_html.extract_csp_nonce(card.html) or _csp_nonce_for(request)
        _set_csp_nonce(request, nonce)
        shim = _org_bridge_shim(
            decide_url=f"/api/approvals/{card.id}/decide", csrf=session_id,
            stepup_options_url=f"/api/approvals/{card.id}/stepup/idp", nonce=nonce,
        )
        return HTMLResponse(_inject_shim(card.html, shim), headers={"Cache-Control": "no-store"})

    async def approval_preview(request: Request) -> Response:
        """The org-mode counterpart of web/routes_approvals.py's own
        ``approval_preview`` -- same read-only inline-disclosure fragment
        for the approval binder (Phase 1), scoped to ``current_principal()``
        the same way every other read here is (module docstring, §10.5):
        another principal's id reads as a plain 404, indistinguishable from
        one that never existed."""
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        approval = registry.get(request.path_params["id"], principal_id=principal.id)
        if approval is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {"id": approval.id, "preview": approval.preview},
            headers={"Cache-Control": "no-store"},
        )

    async def approvals_stream(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        async def event_source():
            last_ids: tuple[str, ...] | None = None
            while True:
                if await request.is_disconnected():
                    break
                ids = tuple(card.id for card in registry.list_pending(principal.id))
                if ids != last_ids:
                    last_ids = ids
                    yield f"data: {json.dumps(list(ids))}\n\n"
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    def _step_up_response(principal: Principal, approval_id: str, *, result: str, choice: int | None) -> JSONResponse:
        body: dict = {"error": "step_up_required"}
        fingerprint = webauthn_stepup.decision_fingerprint(
            approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
        )
        options_json = step_up_decide.begin_step_up(
            principal, rp_id=step_up.rp_id, subject_key=approval_id, fingerprint=fingerprint, challenges=challenges,
        )
        if options_json is not None:
            body["webauthn_options"] = json.loads(options_json)
        elif step_up.require_passkey:
            # No enrolled passkey, and this org has closed the IdP-reauth
            # fallback (#406) -- hard-fail rather than silently downgrading
            # to a weaker step-up than what was configured.
            return JSONResponse(
                {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
            )
        if not step_up.require_passkey:
            choice_q = "" if choice is None else str(int(choice))
            body["idp_stepup_url"] = (
                f"/api/approvals/{quote(approval_id)}/stepup/idp?result={quote(result)}&choice={quote(choice_q)}"
            )
        return JSONResponse(body, status_code=428)

    async def decide(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        if isinstance(result, bool) or not isinstance(result, (str, int, float)):
            return JSONResponse({"error": "missing result"}, status_code=400)
        if not isinstance(result, str):
            result = str(int(result))

        approval = registry.get(approval_id, principal_id=principal.id)
        # The self-approval review's Phase 4, org mode's half: a confirm
        # dialog raised by an MCP bridge proposal (gate.propose_policy_change
        # / propose_rule_change) has no card in front of it, so confirming it
        # is the whole gate on a rule that changes what auto-accepts for this
        # principal from here on. ``_STEP_UP_RESULTS`` does not cover
        # ``confirm``, for a reason that holds of every *other* confirm dialog
        # -- see approvals.PendingApprovalRegistry.register_confirm and
        # web/routes_approvals.py's module docstring, whose local-mode
        # counterpart of this block carries the full reasoning. Gated on
        # ``require_passkey`` rather than ``scope``, exactly as
        # web/routes_settings.py's own ``_needs_step_up``; where that is off,
        # org mode's IdP-reauth fallback is what a step-up would fall back to
        # anyway, which is the session that is already here.
        sensitive_confirm = (
            approval is not None
            and approval.sensitive
            and result == CONFIRM_RESULTS[0]
            and step_up.enabled
            and step_up.require_passkey
        )
        if sensitive_confirm:
            assertion = payload.get("webauthn_assertion")
            if not isinstance(assertion, dict):
                return _step_up_response(principal, approval_id, result=result, choice=choice)
            expected_fp = webauthn_stepup.decision_fingerprint(
                approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
            )
            try:
                step_up_decide.verify_step_up(
                    principal, rp_id=step_up.rp_id, origin=origin, subject_key=approval_id,
                    fingerprint=expected_fp, assertion=assertion, challenges=challenges,
                )
            except step_up_decide.StepUpExpired:
                return JSONResponse({"error": "step_up_expired"}, status_code=400)
            except WebAuthnError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)

        if step_up.enabled and result in _STEP_UP_RESULTS:
            if approval is not None and webauthn_stepup.is_step_up_required(
                gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
            ):
                assertion = payload.get("webauthn_assertion")
                if not isinstance(assertion, dict):
                    return _step_up_response(principal, approval_id, result=result, choice=choice)
                expected_fp = webauthn_stepup.decision_fingerprint(
                    approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
                )
                try:
                    step_up_decide.verify_step_up(
                        principal, rp_id=step_up.rp_id, origin=origin, subject_key=approval_id,
                        fingerprint=expected_fp, assertion=assertion, challenges=challenges,
                    )
                except step_up_decide.StepUpExpired:
                    return JSONResponse({"error": "step_up_expired"}, status_code=400)
                except WebAuthnError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)

        accepted = web_ui.resolve(approval_id, result, choice, principal_id=principal.id)
        if not accepted:
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    def _batch_needs_step_up(principal: Principal, parsed: list[tuple[str, str]]) -> bool:
        """The org-mode counterpart of web/routes_approvals.py's own
        ``_batch_needs_step_up`` -- scoped to ``principal`` the same way
        every other read here is, so another principal's id (an "unknown"
        item, per ``answer_batch``) never contributes to this check."""
        for approval_id, result in parsed:
            if result not in _BATCH_STEP_UP_RESULTS:
                continue
            approval = registry.get(approval_id, principal_id=principal.id)
            if (
                approval is not None and approval.is_batchable()
                and webauthn_stepup.is_step_up_required(
                    gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
                )
            ):
                return True
        return False

    def _batch_step_up_response(principal: Principal, batch_id: str, *, fingerprint: str) -> JSONResponse | None:
        """The org-mode counterpart of web/routes_approvals.py's own
        ``_batch_step_up_response`` -- deliberately no IdP-reauth fallback
        even here, unlike this module's own single-decision
        ``_step_up_response``: the binder plan's own Phase 3 text treats
        this as a page-level ceremony (like web/routes_settings.py's
        sensitive actions), not a per-card one, and a page-level step-up
        never offered an IdP link either. That absence means this can't
        lean on ``require_passkey`` being off to fall back to an IdP link
        the way single-decision does -- with nothing enrolled and
        ``require_passkey`` off, this returns ``None`` (mirroring local
        mode's own evadable fall-through) rather than inventing a third
        behavior the binder plan explicitly rejects."""
        options_json = step_up_decide.begin_step_up(
            principal, rp_id=step_up.rp_id, subject_key=f"batch:{batch_id}", fingerprint=fingerprint,
            challenges=challenges,
        )
        if options_json is not None:
            return JSONResponse(
                {"error": "step_up_required", "batch_id": batch_id, "webauthn_options": json.loads(options_json)},
                status_code=428,
            )
        if step_up.require_passkey:
            return JSONResponse(
                {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
            )
        return None

    async def batch_decide(request: Request) -> Response:
        """The org-mode counterpart of web/routes_approvals.py's own
        ``batch_decide`` -- see that module's own docstring for the shape
        (Phase 2: the plain batch mechanics; Phase 3: one WebAuthn
        assertion bound to the whole submitted set gates an approving
        batch, mirrored here). Scoped to ``current_principal()`` the same
        way every other read/write here is (module docstring, §10.5):
        another principal's id reports "unknown" via ``answer_batch``,
        never "exists but forbidden" -- including as an input to whether
        step-up is even needed, see ``_batch_needs_step_up``."""
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
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return JSONResponse({"error": "missing items"}, status_code=400)
        if len(items) > registry.max_pending:
            return JSONResponse({"error": "too many items"}, status_code=400)
        parsed: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("result") not in BATCH_RESULTS:
                return JSONResponse({"error": "invalid item"}, status_code=400)
            parsed.append((item["id"], item["result"]))

        raw_batch_id = payload.get("batch_id")
        batch_id = raw_batch_id if isinstance(raw_batch_id, str) and raw_batch_id else uuid.uuid4().hex
        batch_id_verified = False

        if step_up.enabled and _batch_needs_step_up(principal, parsed):
            if step_up.batch == "per_item":
                return JSONResponse(
                    {
                        "error": "batch_step_up_per_item",
                        "message": "This organization requires a separate passkey check per decision -- "
                        "decide these individually instead of as a batch.",
                    },
                    status_code=400,
                )
            fingerprint = webauthn_stepup.batch_decision_fingerprint(principal_id=principal.id, items=parsed)
            assertion = payload.get("webauthn_assertion")
            if not isinstance(assertion, dict):
                stepup_response = _batch_step_up_response(principal, batch_id, fingerprint=fingerprint)
                if stepup_response is not None:
                    return stepup_response
            else:
                try:
                    step_up_decide.verify_step_up(
                        principal, rp_id=step_up.rp_id, origin=origin, subject_key=f"batch:{batch_id}",
                        fingerprint=fingerprint, assertion=assertion, challenges=challenges,
                    )
                except step_up_decide.StepUpExpired:
                    return JSONResponse({"error": "step_up_expired"}, status_code=400)
                except WebAuthnError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)
                # See routes_approvals.py's own batch_decide -- the challenge store lookup
                # inside verify_step_up() is what proves this batch_id is one the server
                # actually minted a live challenge under, not just a client-chosen string.
                batch_id_verified = True

        if not batch_id_verified:
            batch_id = uuid.uuid4().hex

        results = registry.answer_batch(parsed, principal_id=principal.id, decided_via="binder", batch_id=batch_id)
        return JSONResponse({"batch_id": batch_id, "results": results})

    async def stepup_idp_start(request: Request) -> Response:
        """A same-site navigation the user's own click on the failed
        card's "Verify by signing in again" link makes -- the session
        cookie *is* present here (unlike the eventual callback, see module
        docstring)."""
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                f"/login?next=/approvals/{quote(request.path_params['id'])}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        if step_up.require_passkey:
            # Not merely unadvertised (_step_up_response omits
            # idp_stepup_url) -- the endpoint itself refuses, so a client
            # hitting it directly can't use it as a bypass. See module
            # docstring's #406 note.
            return PlainTextResponse(
                "This organization requires a passkey for step-up verification; "
                "IdP re-authentication cannot be used instead.", status_code=403,
            )
        approval_id = request.path_params["id"]
        result = request.query_params.get("result", "")
        choice_raw = request.query_params.get("choice", "")
        choice = int(choice_raw) if choice_raw.strip().lstrip("-").isdigit() else None
        if result not in _STEP_UP_RESULTS:
            return PlainTextResponse("Invalid step-up request.", status_code=400)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        stepup_attempts.put(state, _StepUpAuthAttempt(
            principal_id=principal.id, approval_id=approval_id, result=result, choice=choice,
            nonce=nonce, code_verifier=code_verifier,
        ))
        redirect_uri = f"{origin}/oauth/stepup/callback"
        extra_params = {"prompt": "login", "max_age": "0"}
        if idp.step_up_acr_values:
            extra_params["acr_values"] = " ".join(idp.step_up_acr_values)
        url = org_identity.build_authorization_url(
            idp, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge, nonce=nonce,
            extra_params=extra_params,
        )
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    async def stepup_callback(request: Request) -> Response:
        # Deliberately does not read the session cookie -- see module
        # docstring's own note (same reasoning as web/routes_connect.py's
        # own callback).
        idp_error = request.query_params.get("error")
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        attempt = stepup_attempts.pop(state) if state else None
        if attempt is None:
            return PlainTextResponse("Step-up verification failed: invalid or expired attempt.", status_code=400)
        if idp_error or not code:
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        redirect_uri = f"{origin}/oauth/stepup/callback"
        try:
            tokens = await asyncio.to_thread(
                org_identity.exchange_code_for_tokens,
                idp, code=code, redirect_uri=redirect_uri, code_verifier=attempt.code_verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token:
                raise ValueError("IdP token response carried no id_token")
            claims = await asyncio.to_thread(org_identity.verify_id_token, idp, id_token, nonce=attempt.nonce)
            reauthed = org_identity.principal_from_claims(claims, idp)
        except Exception as exc:  # noqa: BLE001 -- any IdP-side failure ends the same way
            logger.warning("Step-up re-authentication failed: %s", exc)
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        # The human who just re-authenticated must be the *same* one this
        # step-up was started for -- otherwise a leaked step-up link (the
        # approval URL itself is not a secret, per §10.4) could be
        # completed by signing in as someone else entirely. See module
        # docstring's own note on why this check exists.
        if reauthed.id != attempt.principal_id:
            logger.warning(
                "Step-up re-authentication resolved to a different principal (%s != %s) -- rejecting",
                reauthed.id, attempt.principal_id,
            )
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        if idp.step_up_acr_values and claims.get("acr") not in idp.step_up_acr_values:
            logger.warning("Step-up re-authentication did not satisfy the configured acr_values -- rejecting")
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        accepted = web_ui.resolve(attempt.approval_id, attempt.result, attempt.choice, principal_id=attempt.principal_id)
        status = "ok" if accepted else "already_decided"
        return RedirectResponse(
            f"/approvals?stepup={status}", status_code=302, headers={"Cache-Control": "no-store"},
        )

    async def service_worker(request: Request) -> Response:
        return PlainTextResponse(
            _SW_JS, media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    return [
        Route("/", index),
        Route("/approvals", list_approvals),
        Route("/approvals/{id}", show_approval),
        # Registered ahead of "/api/approvals/{id}/decide" -- see web/
        # routes_approvals.py's own copy of this comment.
        Route("/api/approvals/batch/decide", batch_decide, methods=["POST"]),
        Route("/api/approvals/{id}/decide", decide, methods=["POST"]),
        Route("/api/approvals/{id}/preview", approval_preview),
        Route("/api/approvals/stream", approvals_stream),
        Route("/api/approvals/{id}/stepup/idp", stepup_idp_start),
        Route("/oauth/stepup/callback", stepup_callback),
        Route("/sw.js", service_worker),
    ]


__all__ = ["build_routes"]
