"""The web approval surface: WebApprovalUI (web_approval_ui.py) registers a
pending card or confirmation, and these routes are what let a human actually
see and decide it in a browser (ADR 0001). ``build_routes()`` returns the
organization ``/approvals`` route list (a plain ``list[Route]``, extended into
``_build_org_app``'s larger app), built by ``_build_route_list()``, which takes
every adapter as a plain keyword argument: ``resolve_principal``,
``check_csrf``/``check_origin``, the unauthenticated responses,
``step_up_response`` (``_org_step_up_response``: the passkey challenge plus an
IdP-reauth link while ``step_up.require_passkey`` is off), ``bridge_shim``,
``render_list_page`` and ``unenrolled_batch_message``.

**Step-up** (ADR 0002 decision 6: a session alone must not be enough to
release an approval) is otherwise identical between modes and
lives in web/approval_step_up.py: before releasing a *write*
decision (or, with ``step_up.scope == "writes_and_pii_reads"``, a
PII-flagged read too -- or, with ``"writes_and_reads"``, every gated read),
the decide endpoint demands proof of a fresh WebAuthn platform-authenticator
assertion -- webauthn_stepup.py's own module docstring covers the ceremony
and the decision-fingerprint binding; the ``step_up_response`` factory above
is only the HTTP protocol wrapping it. **Deny needs no step-up** -- denying
leaks nothing, so step-up is scoped to the two approving results
(``accept``/``accept_all``) only.

**A confirm dialog that is itself the gate** is the one place
``_STEP_UP_RESULTS``' "only an approving decision" rule under-reaches:
``gate.propose_policy_change`` raises no card at all, so confirming one is the whole gate on a rule that
decides what auto-accepts from here on. Those register with
``sensitive=True`` (approvals.PendingApprovalRegistry.register_confirm);
``approval_step_up.guard_decision`` takes the passkey half of that (gated on
``require_passkey`` rather than ``scope``, the same line
web/routes_settings.py's own ``_needs_step_up`` draws); ``decide()`` below
applies the same guards as for an ordinary approving decision.

**Org-only IdP step-up** (``POST /api/approvals/{id}/stepup/idp``,
``GET /oauth/stepup/callback``) lives in web/routes_org_stepup.py, mounted by
``_build_org_app`` alongside this module's own route list.

approval_window_html.py's/dialog_window_html.py's documents post their
decision through ``window.webkit.messageHandlers.pf.postMessage``; a small
shim script injected here defines that as a ``fetch()`` POST to this
module's own decide endpoint, carrying the session's CSRF token, so the
documents themselves know nothing about the route, the session or the
mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import approval_list_html, approval_window_html, deny_feedback, web_shell, webauthn_stepup, whats_new
from ..approvals import BATCH_RESULTS
from ..deny_feedback import DenialFeedback
from ..principal import Principal
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore
from ..web_approval_ui import WebApprovalUI
from . import approval_step_up, org_session, step_up_decide
from .csp import nonce_for as _csp_nonce_for
from .csp import set_nonce as _set_csp_nonce
from .routes_security import PF_WEBAUTHN_JS

# Only an *approving* decision needs step-up: denying leaks nothing, so
# step-up is scoped to the two approving results only.
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

# The way back from a fallback page (web_shell.plain_page), as a button-sized target.
_BACK_TO_APPROVALS = (
    '<div class="actions cluster"><a class="button secondary" href="/approvals">Back to approvals</a></div>'
)

# resources/sw.js -- tier 0/1 notifications. Served at the origin root, not under /api, so its default scope
# covers the whole app (a service worker's scope can never be wider than the
# path it's served from) -- see web_shell.py's own registration call. Shared
# by both modes -- neither the document nor its own no-auth-required posture
# differs between them.
_SW_JS = (Path(__file__).parent.parent / "resources" / "sw.js").read_text(encoding="utf-8")

_DECIDED_MESSAGE = "Decision recorded."
_DENIED_MESSAGE = "Denied."
# After a deny that carried a note or an intent (the card's "Deny and send", ADR 0083).
_DENIED_WITH_NOTE_MESSAGE = "Denied. Your note will go to the agent."
_ALREADY_DECIDED_MESSAGE = "Already decided elsewhere."
_FAILED_MESSAGE = "Could not record this decision — please reload and try again."

# StepUpResponseFactory: mints (or hard-refuses) the challenge for a single
# decision that needs step-up: the passkey-or-IdP shape of
# ``_org_step_up_response``. ``None`` would mean "nothing enrolled, and no
# requirement to fall back to"; that factory never returns it.
StepUpResponseFactory = Callable[..., "JSONResponse | None"]


def _org_step_up_response(
    principal: Principal, approval_id: str, *, result: str, choice: int | None,
    step_up: StepUpConfig, challenges: StepUpChallengeStore,
) -> JSONResponse:
    """Org mode's step-up challenge factory: the passkey ceremony, plus an
    IdP-reauth fallback link whenever ``step_up.require_passkey`` is off --
    so this never lets a decision through unguarded; see
    web/routes_org_stepup.py's own module docstring for the redirect this
    URL leads to. ``require_passkey`` closes that fallback instead:
    with nothing enrolled, this hard-refuses with a ``403`` naming
    ``/security`` (ADR 0066)."""
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
        # fallback -- hard-fail rather than silently downgrading to a
        # weaker step-up than what was configured.
        return JSONResponse(
            {"error": "passkey_enrollment_required", "enroll_url": "/security"}, status_code=403,
        )
    if not step_up.require_passkey:
        # No query string: the card POSTs result/choice/csrf itself, so a
        # link can never start the step-up (web/routes_org_stepup.py).
        body["idp_stepup_url"] = f"/api/approvals/{quote(approval_id)}/stepup/idp"
    return JSONResponse(body, status_code=428)


def _org_decide_shim(*, decide_url: str, csrf: str, stepup_options_url: str, nonce: str) -> str:
    """Org mode's runtime shim: the
    ``window.webkit.messageHandlers.pf.postMessage`` swap for a ``fetch()``
    POST, plus the two branches ``_org_step_up_response``/``decide()``
    responses need: a ``428``'s ``idp_stepup_url``
    fallback button (it POSTs to that URL), and a ``403``'s ``enroll_url``
    (``step_up.require_passkey``). ``stepup_options_url`` is unused by the JS
    below directly (a ``428`` body already carries fresh options inline) but
    is threaded through so a future retry-without-a-body variant has
    somewhere to fetch a fresh challenge from without a second server-side
    endpoint to design -- today's flow never needs it because the first
    ``428`` already includes everything the client needs.

    ``nonce``: this is a real
    ``<script>`` element injected into an already-rendered card document, so
    it must carry that document's own nonce."""
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
        "function pfIdpStepUp(url, payload){"
        "return fetch(url, {method:'POST', credentials:'same-origin',"
        "headers:{'Content-Type':'application/json'},"
        "body: JSON.stringify({result: payload.result, choice: payload.choice == null ? null : payload.choice,"
        f" csrf: {csrf!r}}})}}).then(function(r){{"
        "if (!r.ok) { throw new Error('step-up refused'); }"
        "return r.json();"
        "}).then(function(data){"
        "window.location.assign(data.redirect);"
        f"}}).catch(function(){{ document.body.innerHTML = {_FAILED_MESSAGE!r}; }});"
        "}"
        "function pfIdpStepUpCard(url, payload){"
        "document.body.innerHTML = 'This approval needs extra verification. "
        "<button type=\"button\" id=\"pf-idp-stepup\">Verify by signing in again</button>';"
        "document.getElementById('pf-idp-stepup').addEventListener('click', function(){"
        "pfIdpStepUp(url, payload);"
        "});"
        "}"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        f"var body = Object.assign({{}}, payload, {{csrf: {csrf!r}}});"
        "var isDeny = payload.result === 'deny' || payload.result === 'cancel';"
        "var withNote = payload.result === 'deny' && !!(payload.note || payload.intent);"
        "pfDecide(body).then(function(r){"
        "  if (r.status === 428) {"
        "    return r.json().then(function(data){"
        "      if (data.webauthn_options && window.PublicKeyCredential) {"
        "        return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(assertion){"
        "          var retryBody = Object.assign({}, body, {webauthn_assertion: assertion});"
        "          return pfDecide(retryBody);"
        "        }).catch(function(err){"
        "          if (data.idp_stepup_url) {"
        "            pfIdpStepUpCard(data.idp_stepup_url, payload);"
        "          } else {"
        f"            document.body.innerHTML = {_FAILED_MESSAGE!r} + ' (' + err.message + ')';"
        "          }"
        "          return null;"
        "        });"
        "      }"
        "      if (data.idp_stepup_url) {"
        "        pfIdpStepUpCard(data.idp_stepup_url, payload);"
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
        f"  if (r.ok) {{ msg = withNote ? {_DENIED_WITH_NOTE_MESSAGE!r} : isDeny ? {_DENIED_MESSAGE!r} : {_DECIDED_MESSAGE!r}; }}"
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
    substring well before the real tag, inside the document's one <style>
    block. Landing the shim there instead of in the real body makes the
    browser parse it as inert CSS text, not a script element -- it silently
    never runs, so window.webkit stays undefined and the button-row JS's own
    ``if (window.webkit && ...)`` guard just no-ops on every click. Found by
    actually driving a served card in headless Chromium and clicking Allow
    -- see the regression test below."""
    head_end = html.index("</head>")
    body_start = html.index("<body>", head_end) + len("<body>")
    return html[:body_start] + shim + html[body_start:]


def _render_org_list_page(
    rows: list, *, csrf: str, nonce: str, principal: Principal, push_public_key: str = "",
    whats_new_version: str = "",
) -> str:
    """Org mode's ``/approvals`` page.

    Live updates come from ``GET /api/approvals/stream`` (``stream_url``),
    not a state stream (org mode mounts none: there is no settings snapshot to
    push); this module's own approvals stream is scoped
    to the signed-in principal, and emits the same ``approvals`` event the
    shell script and ``window.__pfRenderApprovals`` already consume -- so a
    new approval, or one decided from another tab or device, shows up
    without a manual reload, and the live indicator reflects a real
    connection. Tier-0/1 notifications stay off: org mode has no per-principal
    setting for them yet. Org mode's notifications are web push instead
    (ADR 0081): ``push_public_key`` is the server's VAPID public key when the
    org has push on, and the shell's permission pre-prompt then subscribes
    this browser (web_shell.wrap's own docstring). Empty means push is off.

    ``principal_label``: every read and write on this page is authorized
    against this principal, and the page never said whose queue it was --
    the cosmetic fields first (see principal.py), falling back to the opaque
    id rather than rendering an unlabelled header."""
    body = approval_list_html.build_list_html(rows, csrf=csrf, nonce=nonce)
    # PF_WEBAUTHN_JS: needed here whenever
    # Approve-selected's own 428 branch (approval_list_html.py's own JS) has
    # to run a ceremony -- always injected, same reasoning show_approval's
    # own shim below gives.
    body += f'<script nonce="{nonce}">{PF_WEBAUTHN_JS}</script>'
    return web_shell.wrap(
        body,
        title="PrivacyFence — Approvals",
        active="approvals",
        nonce=nonce,
        nav_items=web_shell.org_nav_items(principal.is_admin),
        principal_label=principal.email or principal.display_name or principal.id,
        stream_url="/api/approvals/stream",
        notifications_enabled=False,
        push_public_key=push_public_key,
        csrf=csrf,
        whats_new_version=whats_new_version,
    )


def _build_route_list(
    web_ui: WebApprovalUI,
    *,
    resolve_principal: Callable[[Request], Principal | None],
    check_csrf: Callable[[Request, Any], bool],
    check_origin: Callable[[Request], bool],
    session_cookie_name: str,
    unauthenticated_page_response: Callable[[Request, str], Response],
    unauthenticated_read_response: Callable[[Request], Response],
    step_up: StepUpConfig | None,
    step_up_origin: str,
    step_up_response: StepUpResponseFactory,
    bridge_shim: Callable[..., str],
    render_list_page: Callable[..., str],
    per_item_message: str,
    unenrolled_batch_message: str | None,
    stopping: threading.Event | None = None,
    whats_new_version_for: Callable[[Principal], str] = lambda principal: "",
) -> list[Route]:
    """The route list shared by both modes -- see module docstring for what
    each keyword argument covers and why it has to be mode-specific.
    ``resolve_principal``/``unauthenticated_*_response`` are this module's
    own version of the "principal-or-reject" seam
    web/routes_security.py's ``build_routes()`` already established;
    ``step_up_response``/``bridge_shim``/``render_list_page`` are the three
    places the shared web/approval_step_up.py leaves to the
    caller, here supplied by ``build_routes``.
    """
    challenges = StepUpChallengeStore()
    origin = step_up_origin.rstrip("/")
    registry = web_ui.deferred_registry

    async def index(request: Request) -> Response:
        return RedirectResponse("/approvals", status_code=302, headers={"Cache-Control": "no-store"})

    async def list_approvals(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return unauthenticated_page_response(request, "/approvals")
        csrf = request.cookies.get(session_cookie_name, "")
        rows = [approval_list_html.row_from_approval(card) for card in registry.list_pending(principal.id)]
        whats_new_version = await asyncio.to_thread(whats_new_version_for, principal)
        html = render_list_page(
            rows, csrf=csrf, nonce=_csp_nonce_for(request), principal=principal,
            whats_new_version=whats_new_version,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def show_approval(request: Request) -> Response:
        approval_id = request.path_params["id"]
        principal = resolve_principal(request)
        if principal is None:
            return unauthenticated_page_response(request, f"/approvals/{approval_id}")
        card = registry.get(approval_id, principal_id=principal.id)
        if card is None or card.event.is_set():
            # Covers both "never existed" and "already decided" -- another
            # principal's id is indistinguishable from either, same as every
            # other lookup here (module docstring).
            return HTMLResponse(
                web_shell.plain_page(
                    "<h1>No longer pending</h1>"
                    "<p>This approval is no longer pending — it may already have been decided, "
                    "or the link has expired.</p>" + _BACK_TO_APPROVALS,
                    title="PrivacyFence — No longer pending", nonce=_csp_nonce_for(request),
                ),
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        if not card.html:
            # Registered, but not yet rendered: card HTML is only built
            # inside gate.py's _popup_executor, so a card whose worker
            # hasn't been scheduled yet has card.html == "". Past that
            # executor's worker count, that's routine, not exceptional --
            # _inject_shim below assumes a real document, so serve a
            # placeholder instead of letting that raise into a 500.
            logger.info(
                "Approval %s (%s) not rendered yet, %.1fs after it was registered -- served the placeholder",
                card.id, card.kind, time.time() - card.created_at,
            )
            return HTMLResponse(
                web_shell.plain_page(
                    "<h1>Preparing this request</h1>"
                    "<p>Preparing this request — it will be ready in a moment.</p>" + _BACK_TO_APPROVALS,
                    title="PrivacyFence — Preparing", nonce=_csp_nonce_for(request),
                    head_html='<meta http-equiv="refresh" content="2">',
                ),
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        csrf = request.cookies.get(session_cookie_name, "")
        # card.html was rendered once, at approval-creation time --
        # long before this request/response existed -- so its own nonce was
        # picked then, not now. Recover it and make *this* response's CSP
        # header match it, rather than the fresh per-request nonce
        # _SecurityHeadersMiddleware assigned by default; a mismatch would
        # make the browser reject the card's own already-rendered
        # <style>/<script> tags outright.
        nonce = approval_window_html.extract_csp_nonce(card.html) or _csp_nonce_for(request)
        _set_csp_nonce(request, nonce)
        shim = bridge_shim(
            decide_url=f"/api/approvals/{card.id}/decide", csrf=csrf,
            stepup_options_url=f"/api/approvals/{card.id}/stepup/idp", nonce=nonce,
        )
        return HTMLResponse(_inject_shim(card.html, shim), headers={"Cache-Control": "no-store"})

    async def approval_preview(request: Request) -> Response:
        """Read-only inline-disclosure fragment for the approval binder:
        the ``preview`` dict ``gate.py`` stamped onto this
        approval at registration -- metadata only, never
        ``details_text``/``html``/full body content -- so a binder row can
        disclose what it's about without waiting on ``card.html``. Same auth
        as every other read here; no CSRF needed, same as ``show_approval``'s
        own GET."""
        principal = resolve_principal(request)
        if principal is None:
            return unauthenticated_read_response(request)
        approval = registry.get(request.path_params["id"], principal_id=principal.id)
        if approval is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {"id": approval.id, "preview": approval.preview},
            headers={"Cache-Control": "no-store"},
        )

    async def approvals_stream(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return unauthenticated_read_response(request)

        # Same ``event: approvals`` payload -- full summary dicts, not a
        # patch -- that web/state_stream.py's ``/api/state/stream`` sends,
        # so web_shell.py's stream script and approval_list_html.py's
        # ``window.__pfRenderApprovals`` consume either one unchanged. This
        # is the stream org mode's list page subscribes to (org mode mounts
        # no ``/api/state/stream``), scoped to the signed-in principal the
        # same way every other read here is.
        #
        # The principal is re-resolved on every tick, not just at connect
        # time -- the reasoning server.py's ``_state_stream_route``
        # gives: resolving touches the session (an
        # open, watching tab is itself activity), and once the session has
        # idle-/absolute-expired or been signed out, the stream ends
        # instead of continuing to serve a queue nobody is authorized to see.
        async def event_source():
            last_ids: tuple[str, ...] | None = None
            while True:
                if stopping is not None and stopping.is_set():
                    break
                if await request.is_disconnected():
                    break
                current = resolve_principal(request)
                if current is None or current.id != principal.id:
                    break
                pending = registry.list_pending(principal.id)
                ids = tuple(card.id for card in pending)
                if ids != last_ids:
                    last_ids = ids
                    summaries = [card.to_summary_dict() for card in pending]
                    yield f"event: approvals\ndata: {json.dumps(summaries)}\n\n"
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    async def decide(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Origin check on top of the double-submit token above -- the two
        # are independent defenses: a same-site page couldn't forge the
        # cookie value into its own request body, but this also stops a
        # same-origin-cookie-jar edge case from ever mattering.
        if not check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        # A card/confirm result is one of approvals.CARD_RESULTS/
        # CONFIRM_RESULTS -- always a string. A *choice* dialog posts its
        # selected option's index as a bare number instead, so an int/float
        # here is accepted too and normalized to its string form.
        if isinstance(result, bool) or not isinstance(result, (str, int, float)):
            return JSONResponse({"error": "missing result"}, status_code=400)
        if not isinstance(result, str):
            result = str(int(result))

        # A human's deny feedback (ADR 0083). Only a deny may carry it: a
        # note riding along on an approval would reach the agent together
        # with the data it was just given, so any other result with either
        # key is refused outright rather than silently dropping it.
        feedback = DenialFeedback()
        if "note" in payload or "intent" in payload:
            if result != "deny":
                return JSONResponse(
                    {"status": "error", "error": "a note or intent can only accompany a deny"}, status_code=400,
                )
            try:
                feedback = deny_feedback.parse(payload)
            except ValueError as exc:
                # parse() raises only static messages -- never the note itself.
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)

        approval = registry.get(approval_id, principal_id=principal.id)
        stepup_response = approval_step_up.guard_decision(
            principal, step_up,
            approval=approval, approval_id=approval_id, result=result, choice=choice,
            step_up_results=_STEP_UP_RESULTS, assertion=payload.get("webauthn_assertion"), origin=origin,
            challenges=challenges,
            step_up_response=lambda: step_up_response(
                principal, approval_id, result=result, choice=choice, step_up=step_up, challenges=challenges,
            ),
        )
        if stepup_response is not None:
            return stepup_response

        accepted = web_ui.resolve(approval_id, result, choice, principal_id=principal.id, feedback=feedback)
        if not accepted:
            # Idempotent by design: the first accepted decision for
            # an id wins, any later one -- including a genuine double-submit
            # from a slow network retry -- is rejected here, not treated as
            # an error worth alarming over.
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    async def batch_decide(request: Request) -> Response:
        """The approval binder's own batch decide endpoint: approve or
        deny a whole selected set in one request, gating an approving
        batch on one WebAuthn assertion bound to the exact submitted set
        (ADR 0065). Deliberately narrower than ``decide``: no
        ``choice`` (a choice dialog is never batchable), no ``accept_all``
        (rule creation needs its own scoped confirmation)."""
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        # Deny feedback goes through the per-id decide route only (the list's
        # "Deny selected" fans out to it); refused here so nobody builds on an
        # accidental pass-through (ADR 0083).
        if "note" in payload or "intent" in payload:
            return JSONResponse(
                {"status": "error", "error": "the batch endpoint does not take a note or intent"}, status_code=400,
            )
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

        stepup_response, batch_id_verified = approval_step_up.guard_batch_decision(
            principal, step_up,
            parsed=parsed, registry=registry, batch_id=batch_id, batch_step_up_results=_BATCH_STEP_UP_RESULTS,
            per_item_message=per_item_message, unenrolled_batch_message=unenrolled_batch_message,
            assertion=payload.get("webauthn_assertion"), origin=origin, challenges=challenges,
        )
        if stepup_response is not None:
            return stepup_response

        if not batch_id_verified:
            batch_id = uuid.uuid4().hex

        results = registry.answer_batch(parsed, principal_id=principal.id, decided_via="binder", batch_id=batch_id)
        return JSONResponse({"batch_id": batch_id, "results": results})

    async def service_worker(request: Request) -> Response:
        # No auth check -- a service worker script itself carries no gated
        # data, and browsers require it be reachable with no special
        # headers to register at all.
        return PlainTextResponse(
            _SW_JS, media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    return [
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


def build_routes(
    *, web_ui: WebApprovalUI, sessions: org_session.OrgSessionStore, step_up: StepUpConfig, issuer_url: str,
    push_public_key: str = "", stopping: threading.Event | None = None,
) -> list[Route]:
    """Build org mode's own ``/approvals`` route list -- extended into
    ``_build_org_app``'s larger app the same way web/routes_security.py's
    own ``build_routes()`` already is. ``sessions`` is an
    ``OrgSessionStore`` (ADR 0033). Every read and
    write here is authorized against ``current_principal()`` (org_session's
    own ``authenticated()``), filtered through ``PendingApprovalRegistry``'s
    own principal dimension.

    The org-only IdP step-up routes (``/api/approvals/{id}/stepup/idp``,
    ``/oauth/stepup/callback``) are *not* included here -- see module
    docstring: they live in web/routes_org_stepup.py, mounted separately by
    ``_build_org_app`` alongside this function's own return value, .

    ``push_public_key`` is the server's VAPID public key when the org has web
    push on (ADR 0081), else empty; see ``_render_org_list_page``.

    ``stopping``, when set, ends ``/api/approvals/stream`` on its next tick, so a server that is
    shutting down is not held for its graceful-shutdown period."""

    def _resolve_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    def _render_list_page(
        rows: list, *, csrf: str, nonce: str, principal: Principal, whats_new_version: str = "",
    ) -> str:
        return _render_org_list_page(
            rows, csrf=csrf, nonce=nonce, principal=principal, push_public_key=push_public_key,
            whats_new_version=whats_new_version,
        )

    def _unauthenticated_page(request: Request, next_path: str) -> Response:
        return RedirectResponse(
            f"/login?next={next_path}", status_code=302, headers={"Cache-Control": "no-store"},
        )

    def _unauthenticated_read(request: Request) -> Response:
        del request
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return _build_route_list(
        web_ui,
        resolve_principal=_resolve_principal,
        check_csrf=org_session.check_csrf,
        check_origin=org_session.check_origin,
        session_cookie_name=org_session.SESSION_COOKIE,
        unauthenticated_page_response=_unauthenticated_page,
        unauthenticated_read_response=_unauthenticated_read,
        step_up=step_up,
        step_up_origin=issuer_url,
        step_up_response=_org_step_up_response,
        bridge_shim=_org_decide_shim,
        render_list_page=_render_list_page,
        per_item_message=(
            "This organization requires a separate passkey check per decision -- "
            "decide these individually instead of as a batch."
        ),
        unenrolled_batch_message=(
            "Approving several requests at once needs a passkey. Set one up at /security, "
            "or approve each request from its card, where you can verify by signing in again."
        ),
        stopping=stopping,
        whats_new_version_for=whats_new.pending_version,
    )


__all__ = ["build_routes"]
