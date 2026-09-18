"""The web approval surface: WebApprovalUI (web_approval_ui.py) registers a
pending card or confirmation, and these routes are what let a human actually
see and decide it from a browser instead of a native dialog.

P3: ``GET /approvals`` lists every currently-unanswered card/confirmation
(approvals.PendingApprovalRegistry.list_pending()), not just one -- several
can genuinely be pending at once now that gate.py's ``_popup_lock`` is gone,
each independently decidable from its own ``/approvals/{id}`` link. ``GET
/api/approvals/stream`` is the SSE counterpart so the list page (or
whatever's showing it) updates live as approvals appear and get decided,
without polling.

The one JS change to approval_window_html.py's/dialog_window_html.py's
otherwise-untouched documents: a small shim script, injected here rather
than editing either module, defines ``window.webkit.messageHandlers.pf.
postMessage`` as a ``fetch()`` POST to this module's own decide endpoint --
the two shipped documents never need to know whether they're running in a
WKWebView or a browser tab.

**Step-up (#426 Phase 2)** ports web/routes_org_approvals.py's own
decide-time WebAuthn gate here, **minus the IdP re-auth fallback** -- local
mode has no IdP to re-authenticate against, so the ``428`` this module's
``decide()`` returns while step-up is outstanding never carries an
``idp_stepup_url``, only ``webauthn_options`` (org mode's own
``require_passkey=True`` shape is the only one available here, see
step_up_config.py's own docstring). When no passkey is enrolled at all and
``step_up.require_passkey`` is off, ``_step_up_response`` below returns
``None`` and the decision is let through unguarded rather than left
permanently stuck behind a ceremony nobody could ever complete -- simply
never enrolling a passkey dodges the gate entirely in that configuration.

**``step_up.require_passkey`` (#426 Phase 3)** closes that gap: with it on,
a decision that needs step-up and finds no enrolled credential gets a
``403`` naming ``/security`` instead of ``None`` -- the write is never
released, mirroring web/routes_org_approvals.py's own ``require_passkey``
handling exactly (local mode already has no IdP fallback to disable, unlike
org mode's own extra ``stepup_idp_start``/``stepup_callback`` refusal, so
this is the only behavior change needed here).

**The approval binder's own batch step-up (Phase 3 of the binder plan)**
gates ``POST /api/approvals/batch/decide`` (Phase 2) the same way, but on
*one* assertion bound to the whole submitted set
(``webauthn_stepup.batch_decision_fingerprint``) rather than one per item --
see ``batch_decide``'s own docstring for the ``428``/resubmit shape, which
mirrors this module's single-decision flow but carries a server-minted
``batch_id`` instead of an approval id, and, like web/routes_settings.py's
own sensitive-action ceremony, never offers an IdP fallback even in org
mode's counterpart of this endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from .. import approval_list_html, approval_window_html, web_shell, webauthn_stepup
from ..approvals import BATCH_RESULTS
from ..principal import LOCAL_PRINCIPAL
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore, WebAuthnError
from ..web_approval_ui import WebApprovalUI
from . import step_up_decide
from .csp import nonce_for as _csp_nonce_for
from .csp import set_nonce as _set_csp_nonce
from .routes_security import PF_WEBAUTHN_JS
from .session_auth import SESSION_COOKIE as _SESSION_COOKIE
from .session_auth import LocalSessionStore
from .session_auth import authenticated as _session_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import unauthorized_html as _unauthorized_response

# Only an *approving* decision needs step-up -- same as
# web/routes_org_approvals.py's own _STEP_UP_RESULTS: denying leaks
# nothing, so step-up is scoped to the two approving results only.
_STEP_UP_RESULTS = ("accept", "accept_all")

# The batch decide endpoint's own vocabulary (approvals.BATCH_RESULTS) has
# no "accept_all" -- see that constant's own comment -- so the approving
# result step-up applies to is just this one.
_BATCH_STEP_UP_RESULTS = ("accept",)

# How often the SSE stream below checks for a change in what's pending --
# not a hard real-time guarantee, just short enough that a human watching
# the list page doesn't perceive a lag. Polling the registry's own
# in-memory state (cheap) rather than adding a pub/sub mechanism.
_STREAM_POLL_SECONDS = 1.0

logger = logging.getLogger(__name__)

# resources/sw.js -- tier 0/1 notifications (docs/approval-list-ui-ux.md
# §4). Served at the
# origin root, not under /api, so its default scope covers the whole app
# (a service worker's scope can never be wider than the path it's served
# from) -- see web_shell.py's own registration call.
_SW_JS = (Path(__file__).parent.parent / "resources" / "sw.js").read_text(encoding="utf-8")

# _SESSION_COOKIE re-exported (see the session_auth import above) purely so
# this module's own docstring/history referencing "pf_session" as a local
# name still resolves -- session_auth.py is the actual definition now,
# shared with web/routes_settings.py. SameSite=Strict + HttpOnly: never
# sent cross-site, never readable from page JS. See session_auth.py's own
# module docstring (SEC-06) for how a session actually gets established --
# web/server.py's ``_BootstrapMiddleware`` sets this cookie via the
# ``?bootstrap=`` exchange; nothing in this module ever sets it itself any
# more.

_DECIDED_MESSAGE = "Decision recorded."
_DENIED_MESSAGE = "Denied."
_ALREADY_DECIDED_MESSAGE = "Already decided elsewhere."
_FAILED_MESSAGE = "Could not record this decision — please reload and try again."


def _bridge_shim(*, decide_url: str, csrf: str, nonce: str) -> str:
    """Runtime shim swapping approval_window_html.py's/dialog_window_html.py's
    own ``window.webkit.messageHandlers.pf.postMessage(payload)`` call for a
    ``fetch()`` POST here -- see module docstring. ``csrf`` is folded into
    every posted payload (double-submit: the same value also has to match
    the session cookie server-side, see _csrf_matches below) rather than
    trusted from the cookie alone.

    docs/approval-list-ui-ux.md §3 ("After a decision: back to the list"):
    on a 2xx or a 409 (``already_decided`` -- a rule elsewhere resolved
    this one first, a genuinely common case once rules-changed
    re-evaluation is live, not an error), navigate straight back to
    ``/approvals`` via ``location.replace`` (not a push -- the browser back
    button must not walk into a card that no longer exists) with a toast
    message stashed in ``sessionStorage`` for the list page to show once
    (see approval_list_html.py's own JS). Only a genuine failure (network
    error, an unexpected status) leaves the card on screen with an inline
    message -- there is nothing to navigate back to for those.

    A ``428`` (#426 Phase 2) means step-up is outstanding: when the body
    carries ``webauthn_options``, run the assertion ceremony
    (``window.pfWebauthnGet``, defined by ``PF_WEBAUTHN_JS`` below --
    mirrors web/routes_org_approvals.py's own ``_org_bridge_shim``) and
    retry the same decide POST with a ``webauthn_assertion`` attached; a
    ``428`` with no options (no passkey enrolled -- see module docstring)
    or a failed ceremony both fall through to the generic failure message,
    since local mode has no IdP link to offer instead.

    ``nonce`` (SEC-08): this
    shim is a real ``<script>`` element injected into an already-rendered
    card document (see ``_inject_shim`` below), so it has to carry the same
    nonce that document's own ``<script>``/``<style>`` tags already do --
    ``show_approval`` below recovers that value via
    approval_window_html.extract_csp_nonce and passes it straight through.
    """
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
        f"          document.body.innerHTML = {_FAILED_MESSAGE!r} + ' (' + err.message + ')';"
        "          return null;"
        "        });"
        "      }"
        f"      document.body.innerHTML = {_FAILED_MESSAGE!r};"
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


def _inject_shim(html: str, shim: str) -> str:
    """Insert ``shim`` as the first child of the document's real ``<body>``
    tag. First child, not appended at the end -- it must define
    window.webkit before approval_window_html.py's/dialog_window_html.py's
    own <script> (also a direct child of <body>, added after body_html)
    runs its DOMContentLoaded handler; script tags execute in document
    order as parsed, so this ordering alone is enough, no defer/async
    needed.

    Searches for ``<body>`` only *after* ``</head>`` closes, not from the
    start of the document -- a plain ``html.replace("<body>", ..., 1)``
    finds whichever "<body>" comes first in the raw string, and the
    embedded stylesheet's own CSS comments genuinely contain that literal
    substring (e.g. styles.css: "the same-colored rail on <body>",
    "racing it on source order across two separate <style> blocks") well
    before the real tag, inside the document's one <style> block. Landing
    the shim there instead of in the real body makes the browser parse it
    as inert CSS text, not a script element -- it silently never runs, so
    window.webkit stays undefined and the button-row JS's own
    ``if (window.webkit && ...)`` guard just no-ops on every click. Found
    by actually driving a served card in headless Chromium and clicking
    Allow -- see the regression test below.
    """
    head_end = html.index("</head>")
    body_start = html.index("<body>", head_end) + len("<body>")
    return html[:body_start] + shim + html[body_start:]


def create_app(
    web_ui: WebApprovalUI,
    *,
    sessions: LocalSessionStore,
    extra_routes: list[BaseRoute] | None = None,
    lifespan=None,
    notifications_enabled: bool = True,
    notifications_detail: str = "minimal",
    step_up: StepUpConfig | None = None,
    step_up_origin: str = "",
) -> Starlette:
    """Build the Starlette app serving the approval surface. ``sessions``
    (SEC-06, see session_auth.py's own module docstring) is the local-mode
    session store -- this function takes it as a plain argument rather than
    reading paths.py itself, so tests can construct an app against an
    isolated WebApprovalUI/session-store pair with no filesystem or
    global-singleton dependency. A request's own ``pf_session`` cookie
    value doubles as the CSRF token baked into every rendered page below --
    it is only ever set by web/server.py's ``_BootstrapMiddleware``, which
    is what actually authenticates the one-time ``?bootstrap=`` exchange;
    nothing in this module ever mints or sets that cookie itself.

    ``extra_routes``/``lifespan`` are how server.py folds the ``/mcp``
    endpoint (routes_mcp.py, P2) into this same combined app rather than
    running a second ASGI app/server on a second port -- one embedded HTTP
    server. Both default to nothing so every existing caller
    (including this module's own tests) is unaffected.

    ``notifications_enabled`` is settings.yaml.example's
    ``web.notifications.enabled`` (default true) -- see web_shell.wrap's
    own docstring for what it turns off. ``notifications_detail`` is that
    same config block's ``detail`` (minimal/standard/detailed, P5 --
    docs/approval-list-ui-ux.md §4.3) -- also passed straight through.

    ``step_up``/``step_up_origin`` (#426 Phase 2) gate an approving
    decision on a fresh WebAuthn assertion, mirroring web/
    routes_org_approvals.py's own decide-time check -- see this module's
    own docstring for what's deliberately different (no IdP fallback,
    still evadable by not enrolling). Both default to "off" so every
    existing caller of this function is unaffected; web/server.py's
    ``build_app`` is the one real (non-test) caller that passes them,
    using the same ``StepUpConfig`` it already resolves for web/
    routes_security.py's own ``/security`` mount.
    """
    challenges = StepUpChallengeStore()
    origin = step_up_origin.rstrip("/")

    def _authenticated(request: Request) -> bool:
        return _session_authenticated(request, sessions)

    def _unauthorized(request: Request) -> Response:
        return _unauthorized_response(request)

    async def index(request: Request) -> Response:
        # No ``?bootstrap=``/``?token=`` handling here any more -- SEC-06
        # moved that one-time exchange into web/server.py's
        # ``_BootstrapMiddleware``, which runs ahead of every route
        # (including this one) and already turned a valid code into a real
        # session before this ever executes. This just sends whatever the
        # request's own cookie already resolves to (authenticated or not --
        # list_approvals below is what actually enforces that) on to the
        # one real landing page.
        return RedirectResponse("/approvals")

    def _list_rows() -> list:
        return web_ui.deferred_registry.list_pending()

    def _banner_html() -> str | None:
        if step_up is None:
            return None
        # #426 Phase 4: the persistent "requirement was turned off" notice
        # stands alongside the Phase 3 "nothing enrolled yet" one -- see
        # web/routes_settings.py's own _banner_html for the same pairing.
        parts = [
            step_up.local_enrollment_banner(has_credentials=webauthn_stepup.has_credentials(LOCAL_PRINCIPAL)),
            webauthn_stepup.step_up_disabled_notice(LOCAL_PRINCIPAL),
        ]
        parts = [p for p in parts if p]
        return " ".join(parts) if parts else None

    def _off_notice_html() -> str | None:
        # B23 of the 4.1.0 action plan: unlike _banner_html above (a live
        # problem, re-derived every request), this is an invitation --
        # step-up existing and being off is this install's ordinary
        # default, not a defect -- so it's rendered as a dismissible
        # notice (web_shell.wrap's own dismissible_notice_html) instead of
        # stacked into the non-dismissable banner.
        return None if step_up is None else step_up.off_notice()

    async def list_approvals(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized(request)
        # SEC-08: this page is
        # rendered fresh every request, so it just takes the nonce
        # _SecurityHeadersMiddleware already generated for this response
        # (web/csp.py's nonce_for) -- both build_list_html's own <style>/
        # <script> and web_shell.wrap's shell chrome must carry the exact
        # same value.
        nonce = _csp_nonce_for(request)
        csrf = request.cookies.get(_SESSION_COOKIE, "")
        rows = [approval_list_html.row_from_approval(card) for card in _list_rows()]
        body = approval_list_html.build_list_html(rows, csrf=csrf, nonce=nonce)
        # PF_WEBAUTHN_JS (#426 Phase 3, approval binder Phase 3): the same
        # ceremony helpers web/routes_settings.py's own settings page
        # carries, needed here whenever Approve-selected's own 428 branch
        # (approval_list_html.py's own JS) has to run one -- always
        # injected regardless of whether step-up is actually enabled for
        # this install, same reasoning TestStepUpBridgeShim already gives
        # for the per-card shim.
        body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
        html = web_shell.wrap(
            body, title="PrivacyFence — Approvals", active="approvals", nonce=nonce,
            notifications_enabled=notifications_enabled, notifications_detail=notifications_detail,
            banner_html=_banner_html(),
            dismissible_notice_html=_off_notice_html(), dismissible_notice_key="pf_step_up_off_dismissed",
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def show_approval(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized(request)
        approval_id = request.path_params["id"]
        card = web_ui.deferred_registry.get(approval_id)
        if card is None or card.event.is_set():
            # Covers both "never existed" and "already decided" -- an
            # answered card is left in the registry a while longer now (it
            # may still be feeding the decision ledger, see approvals.py),
            # but there's nothing left here for a human to decide, so this
            # says so rather than 404-ing.
            return HTMLResponse(
                "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
                "This approval is no longer pending — it may already have been decided, "
                "or the link has expired. <a href=\"/approvals\">Back to approvals</a>"
                "</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        if not card.html:
            # Registered, but not yet rendered: card HTML is only built
            # inside gate.py's _popup_executor (build_card_html runs from
            # within show_popup/show_read_popup, on that worker thread), so
            # a card whose worker hasn't been scheduled yet has card.html ==
            # "". Past _popup_executor's worker count, that's routine, not
            # exceptional -- _inject_shim below assumes a real document
            # (its first statement is html.index("</head>")), so serve a
            # placeholder instead of letting that raise into a 500. The list
            # page's own SSE stream already re-renders every ~1s, so a
            # human landing here early just needs a moment.
            return HTMLResponse(
                "<!DOCTYPE html><html><head><meta http-equiv=\"refresh\" content=\"2\">"
                "</head><body style=\"font:15px sans-serif;padding:40px\">"
                "Preparing this request — it will be ready in a moment. "
                "<a href=\"/approvals\">Back to approvals</a>"
                "</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        csrf = request.cookies.get(_SESSION_COOKIE, "")
        # SEC-08: card.html
        # was rendered once, at approval-creation time -- long before this
        # request/response existed -- so its own nonce was picked then, not
        # now (see approval_window_html.py's module docstring). Recover it
        # and make *this* response's CSP header match it, rather than the
        # fresh per-request nonce _SecurityHeadersMiddleware assigned by
        # default; a mismatch would make the browser reject the card's own
        # already-rendered <style>/<script> tags outright.
        nonce = approval_window_html.extract_csp_nonce(card.html) or _csp_nonce_for(request)
        _set_csp_nonce(request, nonce)
        shim = _bridge_shim(decide_url=f"/api/approvals/{card.id}/decide", csrf=csrf, nonce=nonce)
        return HTMLResponse(_inject_shim(card.html, shim), headers={"Cache-Control": "no-store"})

    async def approval_preview(request: Request) -> Response:
        """Read-only inline-disclosure fragment for the approval binder
        (Phase 1): the ``preview`` dict ``gate.py`` stamped onto this
        approval at registration -- metadata only (sender, subject, size,
        destination path; docs/coding-and-testing-guidelines.md §1.5), never
        ``details_text``/``html``/full body content -- so a binder row can
        disclose what it's about without waiting on ``card.html`` (only
        built once gate.py's ``_popup_executor`` gets around to it) and
        without an ``<iframe>`` onto the real card document (which would
        mean weakening web/csp.py's ``frame-ancestors 'none'`` for
        cosmetics -- see the binder plan's own "Rejected alternatives").
        Same auth as every other read here; no CSRF needed, same as
        ``show_approval``'s own GET."""
        if not _authenticated(request):
            return _unauthorized(request)
        approval = web_ui.deferred_registry.get(request.path_params["id"])
        if approval is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {"id": approval.id, "preview": approval.preview},
            headers={"Cache-Control": "no-store"},
        )

    async def approvals_stream(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized(request)

        async def event_source():
            last_ids: tuple[str, ...] | None = None
            while True:
                if await request.is_disconnected():
                    break
                ids = tuple(card.id for card in _list_rows())
                if ids != last_ids:
                    last_ids = ids
                    yield f"data: {json.dumps(list(ids))}\n\n"
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    def _step_up_response(approval_id: str, *, result: str, choice: int | None) -> JSONResponse | None:
        """The local-mode counterpart of web/routes_org_approvals.py's own
        ``_step_up_response``. ``None`` when there is no enrolled passkey to
        challenge *and* ``step_up.require_passkey`` is off, which the caller
        takes as "let the decision through unguarded" (module docstring:
        Phase 2's own evadable-by-not-enrolling behavior, kept only for that
        configuration). With ``require_passkey`` on and nothing enrolled,
        this hard-fails with a ``403`` instead -- #426 Phase 3, mirroring
        org mode's own ``require_passkey`` branch exactly."""
        fingerprint = webauthn_stepup.decision_fingerprint(
            approval_id=approval_id, principal_id=LOCAL_PRINCIPAL.id, result=result, choice=choice,
        )
        options_json = step_up_decide.begin_step_up(
            LOCAL_PRINCIPAL, rp_id=step_up.rp_id, subject_key=approval_id, fingerprint=fingerprint,
            challenges=challenges,
        )
        if options_json is not None:
            return JSONResponse(
                {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
            )
        if step_up.require_passkey:
            return JSONResponse(
                {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
            )
        return None

    async def decide(request: Request) -> Response:
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not _csrf_matches(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Origin check on top of the double-submit token above -- the two
        # are independent defenses: a same-site page couldn't forge the cookie
        # value into its own request body, but this also stops a
        # same-origin-cookie-jar edge case from ever mattering.
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        # A card/confirm result is one of approvals.CARD_RESULTS/
        # CONFIRM_RESULTS -- always a string. A *choice* dialog
        # (dialog_window_html.build_choice_html, W5's web_prompt.py picker)
        # posts its selected option's index as a bare number instead (see
        # that module's own JS -- there is no separate "choice" field), so
        # an int/float here is accepted too and normalized to its string
        # form before being handed to web_ui.resolve(); web_prompt.py's own
        # reader parses it back with int().
        if isinstance(result, bool) or not isinstance(result, (str, int, float)):
            return JSONResponse({"error": "missing result"}, status_code=400)
        if not isinstance(result, str):
            result = str(int(result))

        if step_up is not None and step_up.enabled and result in _STEP_UP_RESULTS:
            approval = web_ui.deferred_registry.get(approval_id)
            if approval is not None and webauthn_stepup.is_step_up_required(
                gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
            ):
                assertion = payload.get("webauthn_assertion")
                if not isinstance(assertion, dict):
                    stepup_response = _step_up_response(approval_id, result=result, choice=choice)
                    if stepup_response is not None:
                        return stepup_response
                else:
                    expected_fp = webauthn_stepup.decision_fingerprint(
                        approval_id=approval_id, principal_id=LOCAL_PRINCIPAL.id, result=result, choice=choice,
                    )
                    try:
                        step_up_decide.verify_step_up(
                            LOCAL_PRINCIPAL, rp_id=step_up.rp_id, origin=origin, subject_key=approval_id,
                            fingerprint=expected_fp, assertion=assertion, challenges=challenges,
                        )
                    except step_up_decide.StepUpExpired:
                        return JSONResponse({"error": "step_up_expired"}, status_code=400)
                    except WebAuthnError as exc:
                        return JSONResponse({"error": str(exc)}, status_code=401)

        accepted = web_ui.resolve(approval_id, result, choice)
        if not accepted:
            # Idempotent by design (§7.1): the first accepted decision for
            # an id wins, any later one -- including a genuine double-submit
            # from a slow network retry -- is rejected here, not treated as
            # an error worth alarming over.
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    def _batch_needs_step_up(parsed: list[tuple[str, str]], registry) -> bool:
        """True iff at least one *known, batchable, approving* item in this
        batch actually needs step-up (webauthn_stepup.is_step_up_required) --
        an unknown id or a non-batchable item contributes nothing here,
        since answer_batch() below will never apply either one regardless
        of step-up. A deny-only batch never reaches this at all (see
        ``batch_decide``'s own caller), but a batch that mixes accepts and
        denies is checked only against its accepts."""
        for approval_id, result in parsed:
            if result not in _BATCH_STEP_UP_RESULTS:
                continue
            approval = registry.get(approval_id)
            if (
                approval is not None and approval.is_batchable()
                and webauthn_stepup.is_step_up_required(
                    gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
                )
            ):
                return True
        return False

    def _batch_step_up_response(batch_id: str, *, fingerprint: str) -> JSONResponse | None:
        """The batch counterpart of ``_step_up_response`` -- same "no
        enrolled credential and require_passkey off" evadable fall-through
        (returns ``None``), same ``403`` naming ``/security`` when
        ``require_passkey`` is on instead. Deliberately no IdP-reauth
        fallback even in org mode's own version of this function -- the
        binder plan's own Phase 3 text: this ceremony is a page-level one
        (like web/routes_settings.py's own sensitive actions), not a
        per-card one, and page-level step-up here never offered an IdP link
        either."""
        options_json = step_up_decide.begin_step_up(
            LOCAL_PRINCIPAL, rp_id=step_up.rp_id, subject_key=f"batch:{batch_id}", fingerprint=fingerprint,
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
        """The approval binder's own batch decide endpoint (Phase 2:
        approve or deny a whole selected set in one request; Phase 3: gate
        an approving batch on one WebAuthn assertion bound to the exact
        submitted set, webauthn_stepup.batch_decision_fingerprint).

        Deliberately narrower than ``decide``: no ``choice`` (a choice
        dialog is never batchable, see approvals.PendingApproval.
        is_batchable), no ``accept_all`` (rule creation needs its own
        scoped confirmation -- BATCH_RESULTS is exactly ``decide``'s own
        ``result`` vocabulary minus that one value).

        Step-up flow: a first submission carries no ``batch_id``/
        ``webauthn_assertion`` -- this mints a fresh ``batch_id``, begins a
        challenge bound to it and to the fingerprint of the *whole*
        submitted ``items`` list (deny items included, so the bound set
        can't be silently narrowed or widened on resubmission), and
        returns a ``428`` carrying both. The client resubmits the identical
        ``items`` plus that same ``batch_id`` and the completed assertion;
        the fingerprint is recomputed here from the resubmitted items and
        compared, never trusted from the client. ``step_up.batch ==
        "per_item"`` (step_up_config.py) refuses the whole request instead
        of ever minting that single-assertion challenge -- see that
        field's own comment."""
        if not _authenticated(request):
            return _unauthorized(request)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not _csrf_matches(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return JSONResponse({"error": "missing items"}, status_code=400)
        registry = web_ui.deferred_registry
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

        if step_up is not None and step_up.enabled and _batch_needs_step_up(parsed, registry):
            if step_up.batch == "per_item":
                return JSONResponse(
                    {
                        "error": "batch_step_up_per_item",
                        "message": "This install requires a separate passkey check per decision -- "
                        "decide these individually instead of as a batch.",
                    },
                    status_code=400,
                )
            fingerprint = webauthn_stepup.batch_decision_fingerprint(principal_id=LOCAL_PRINCIPAL.id, items=parsed)
            assertion = payload.get("webauthn_assertion")
            if not isinstance(assertion, dict):
                stepup_response = _batch_step_up_response(batch_id, fingerprint=fingerprint)
                if stepup_response is not None:
                    return stepup_response
            else:
                try:
                    step_up_decide.verify_step_up(
                        LOCAL_PRINCIPAL, rp_id=step_up.rp_id, origin=origin, subject_key=f"batch:{batch_id}",
                        fingerprint=fingerprint, assertion=assertion, challenges=challenges,
                    )
                except step_up_decide.StepUpExpired:
                    return JSONResponse({"error": "step_up_expired"}, status_code=400)
                except WebAuthnError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)
                # The challenge store lookup inside verify_step_up() only succeeds for a
                # batch_id this server minted a live challenge under -- that's the one case
                # a client-supplied batch_id is provably genuine rather than an arbitrary string.
                batch_id_verified = True

        if not batch_id_verified:
            batch_id = uuid.uuid4().hex

        results = registry.answer_batch(parsed, decided_via="binder", batch_id=batch_id)
        return JSONResponse({"batch_id": batch_id, "results": results})

    async def service_worker(request: Request) -> Response:
        # No auth check -- a service worker script itself carries no
        # gated data (see resources/sw.js's own docstring: no push
        # handler, no cache, nothing fetched), and browsers require it be
        # reachable with no special headers to register at all.
        return PlainTextResponse(
            _SW_JS, media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    routes: list[BaseRoute] = [
        Route("/", index),
        Route("/approvals", list_approvals),
        Route("/approvals/{id}", show_approval),
        # Registered ahead of "/api/approvals/{id}/decide" -- Starlette
        # matches routes in registration order, and "{id}" would otherwise
        # swallow the literal "batch" segment first.
        Route("/api/approvals/batch/decide", batch_decide, methods=["POST"]),
        Route("/api/approvals/{id}/decide", decide, methods=["POST"]),
        Route("/api/approvals/{id}/preview", approval_preview),
        Route("/api/approvals/stream", approvals_stream),
        Route("/sw.js", service_worker),
    ]
    routes.extend(extra_routes or [])
    return Starlette(routes=routes, lifespan=lifespan)
