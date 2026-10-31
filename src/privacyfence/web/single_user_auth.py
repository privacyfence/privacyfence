"""Browser sign-in for single-user mode: organization mode with no identity provider, one
administrator, served only on http://localhost. Mounted instead of web/routes_org_identity.py.

There is nobody to redirect to, so the first sign-in proves possession of a **setup code** that
only the person running the server can read, because it goes nowhere but the server log. That
sign-in lands on ``/security`` to enroll a passkey, and every later sign-in is a WebAuthn assertion
with that passkey, the same one step-up checks. What the code protects against, and what it does
not:

- **It is single-use and short-lived in practice.** It is consumed by its first successful use,
  used up by five wrong ones, kept only in memory, and stops working for good the moment a passkey
  exists. ``has_credentials`` is read on every request, so a passkey enrolled through some other
  path, or a later recovery wipe of the passkey store, never brings an old code back: only a
  restart with no passkey enrolled issues a new one.
- **It never leaves the log.** No response, audit entry or other log line carries it, and a
  refused attempt does not record what was typed.
- **Each form post is bound to the browser that loaded the form.** ``GET /login`` sets
  ``pf_setup_binding``, a random value echoed in the page, and the posts must carry both copies
  (``SameSite=Strict``, ``HttpOnly``), on top of ``org_session.check_origin``. A page on another
  site can neither read the value nor get the cookie sent, so it cannot sign this browser in to
  anything (login CSRF), with the code or with a passkey.
- **A session id is always fresh.** Every successful sign-in mints a new id through
  ``OrgSessionStore.create``; nothing a browser presents is ever promoted to a session.
- **The code does not protect against a local agent.** Anyone who can read the server log, or run
  ``docker`` on this computer, can sign in. That is what single-user mode is: one person's server
  on their own computer. Use an organization server with an identity provider to defend against
  that.

Cookies are ``Secure`` only for an https issuer. Single-user mode is plain http on localhost, and
Safari does not keep a ``Secure`` cookie on ``http://localhost``.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape as _esc
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import grist_provisioning, peek_provisioning, web_shell, webauthn_stepup
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..principal import Principal, principal_scope
from ..step_up_config import StepUpConfig
from ..web_push import PushSubscriptionStore, session_tag
from . import org_session
from .csp import nonce_for
from .routes_org_identity import DEFAULT_NEXT_PATH, SIGNED_OUT_PATH
from .routes_security import PF_WEBAUTHN_JS, safe_next_path

logger = logging.getLogger(__name__)

SINGLE_USER_PRINCIPAL = Principal(
    id="single-user", email="admin@localhost", display_name="Administrator", is_admin=True,
)

SETUP_BINDING_COOKIE = "pf_setup_binding"
# Covers a person reading the code out of the log and typing it, or a passkey prompt.
_BINDING_TTL_SECONDS = 10 * 60
# Only the paths the posts go to, so the value never travels with any other request.
_BINDING_COOKIE_PATH = "/login"

MAX_WRONG_SETUP_ATTEMPTS = 5
_SETUP_CODE_SHAPE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")

# Bounds the passkey challenges in flight. Each GET /login hands out a new binding, so without a
# cap a loop of options requests could grow the store until the entries expire.
_MAX_PENDING_PASSKEY_CHALLENGES = 64

WRONG_CODE_MESSAGE = "That code is not right."
USED_UP_MESSAGE = "The setup code is used up. Restart PrivacyFence to get a new one."
PASSKEY_ENROLLED_MESSAGE = "A passkey is already set up here. Sign in with it instead."


def normalize_setup_code(typed: str) -> str | None:
    """The typed value as ``XXXX-XXXX``, or None when it cannot be a setup code. Stripped,
    upper-cased, and given its dash when it was typed without one. Only ASCII hex survives the
    shape check, so the comparison never sees a value ``hmac.compare_digest`` would refuse."""
    value = typed.strip().upper()
    if len(value) == 8 and "-" not in value:
        value = f"{value[:4]}-{value[4:]}"
    return value if _SETUP_CODE_SHAPE.fullmatch(value) else None


class SetupCode:
    """The one setup code of this process. ``check`` both compares and consumes under one lock,
    so two correct submissions racing each other give exactly one session."""

    ACCEPTED = "accepted"
    WRONG = "wrong"
    USED_UP = "used_up"

    def __init__(self, code: str | None) -> None:
        self._lock = threading.Lock()
        self._code = code
        self._wrong = 0

    @staticmethod
    def generate() -> str:
        raw = secrets.token_hex(4).upper()
        return f"{raw[:4]}-{raw[4:]}"

    @property
    def available(self) -> bool:
        with self._lock:
            return self._code is not None

    def retire(self) -> None:
        """A passkey exists: the code never works again in this process."""
        with self._lock:
            self._code = None

    def check(self, typed: str) -> str:
        normalized = normalize_setup_code(typed)
        with self._lock:
            if self._code is None:
                return self.USED_UP
            if normalized is not None and hmac.compare_digest(normalized.encode(), self._code.encode()):
                self._code = None
                return self.ACCEPTED
            self._wrong += 1
            if self._wrong >= MAX_WRONG_SETUP_ATTEMPTS:
                self._code = None
                logger.warning(
                    "Setup code used up after %d wrong attempts; restart PrivacyFence for a new one.",
                    MAX_WRONG_SETUP_ATTEMPTS,
                )
                return self.USED_UP
            return self.WRONG


@dataclass
class _PendingPasskeySignIn:
    challenge: bytes
    next_path: str
    created_at: float


class _PasskeyChallengeStore:
    """One assertion challenge per browser binding, single-use and short-lived."""

    def __init__(self, ttl: float = webauthn_stepup.STEP_UP_CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingPasskeySignIn] = {}

    def put(self, binding: str, *, challenge: bytes, next_path: str) -> None:
        now = time.time()
        with self._lock:
            for key in [k for k, p in self._pending.items() if (now - p.created_at) > self._ttl]:
                del self._pending[key]
            if binding not in self._pending and len(self._pending) >= _MAX_PENDING_PASSKEY_CHALLENGES:
                oldest = min(self._pending, key=lambda k: self._pending[k].created_at)
                del self._pending[oldest]
            self._pending[binding] = _PendingPasskeySignIn(challenge=challenge, next_path=next_path, created_at=now)

    def pop(self, binding: str) -> _PendingPasskeySignIn | None:
        with self._lock:
            entry = self._pending.pop(binding, None)
        if entry is None or (time.time() - entry.created_at) > self._ttl:
            return None
        return entry


def _audit(decision: str, summary: str) -> None:
    """Written the way web/routes_security.py's ``_audit`` writes, into the administrator's own
    audit log: the request has no session yet, so the scope is entered here. Never blocks or fails
    the sign-in it describes."""
    try:
        with principal_scope(SINGLE_USER_PRINCIPAL):
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary=summary,
                sender=SINGLE_USER_PRINCIPAL.email,
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
            ))
    except Exception as exc:
        logger.warning("Audit log write failed for %s: %s", decision, exc)


def _bound(request: Request, echoed: object) -> str | None:
    """The binding value when the post carries the same one as this browser's cookie, else None."""
    cookie = request.cookies.get(SETUP_BINDING_COOKIE, "")
    if not cookie or not isinstance(echoed, str) or not echoed:
        return None
    return cookie if hmac.compare_digest(cookie.encode(), echoed.encode()) else None


_PAGE_CSS = """
.pf-login .meta{color:var(--muted);font-size:var(--step-small)}
.pf-login .error{color:var(--danger, #b42318)}
.pf-login input[name=code]{font-family:var(--font-mono, monospace);letter-spacing:.08em;
  text-transform:uppercase;min-height:var(--tap);padding-inline:var(--space-xs)}
"""

_PAGE_JS = """
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('pf-passkey-signin');
  if (!btn) { return; }
  var page = document.getElementById('pf-login-page');
  var binding = page.getAttribute('data-binding');
  var next = page.getAttribute('data-next');
  var status = document.getElementById('pf-login-status');
  if (!window.PublicKeyCredential) {
    btn.disabled = true;
    status.textContent = 'This browser does not support passkeys.';
    return;
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok || data.error) { throw new Error(data.error || 'sign-in failed'); }
        return data;
      });
    });
  }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    status.textContent = 'Follow your browser\\'s prompt...';
    post('/login/passkey/options', {binding: binding, next: next}).then(function (data) {
      return pfWebauthnGet(JSON.stringify(data.options));
    }).then(function (assertion) {
      return post('/login/passkey/verify', {binding: binding, credential: assertion});
    }).then(function (data) {
      window.location.assign(data.redirect);
    }).catch(function (err) {
      btn.disabled = false;
      status.textContent = 'Could not sign in: ' + err.message;
    });
  });
});
""" + PF_WEBAUTHN_JS


def build_routes(
    *, sessions: org_session.OrgSessionStore, issuer_url: str, step_up: StepUpConfig,
    default_next_path: str = DEFAULT_NEXT_PATH, push_store: PushSubscriptionStore | None = None,
) -> list[Route]:
    """``issuer_url`` is the server's own address: the passkey's expected origin, the address the
    setup-code log line names, and whether cookies are ``Secure``. ``step_up.rp_id`` is the
    relying-party ID ``/security`` enrolls with, so the sign-in ceremony matches it.
    ``default_next_path`` and ``push_store`` mean what they mean for
    ``routes_org_identity.build_routes``.

    The setup code is made and logged here, once per process, unless a passkey is already
    enrolled."""
    origin = issuer_url.rstrip("/")
    secure = origin.startswith("https://")
    if webauthn_stepup.has_credentials(SINGLE_USER_PRINCIPAL):
        setup = SetupCode(None)
    else:
        code = SetupCode.generate()
        setup = SetupCode(code)
        logger.warning("Setup code: %s. Open %s/login to set up your passkey.", code, origin)
    challenges = _PasskeyChallengeStore()

    def passkey_enrolled() -> bool:
        """Read on every request; once true, the setup code is gone for this process."""
        if webauthn_stepup.has_credentials(SINGLE_USER_PRINCIPAL):
            setup.retire()
            return True
        return False

    def end_browser_push(old_session_id: str, *, new_session_id: str = "") -> None:
        # The same rule as routes_org_identity's: a new sign-in in this browser moves the
        # previous session's push subscriptions to it, and a sign-out removes them.
        if push_store is None or not old_session_id:
            return
        try:
            if new_session_id:
                push_store.replace_session(
                    session_tag(old_session_id), session_tag(new_session_id), SINGLE_USER_PRINCIPAL.id,
                )
            else:
                push_store.remove_session(session_tag(old_session_id))
        except (OSError, ValueError) as exc:
            logger.warning("Could not update this browser's push subscriptions: %s", type(exc).__name__)

    def start_session(request: Request, response: Response) -> None:
        session_id = sessions.create(SINGLE_USER_PRINCIPAL)
        end_browser_push(request.cookies.get(org_session.SESSION_COOKIE, ""), new_session_id=session_id)
        org_session.set_session_cookie(response, session_id, secure=secure)
        response.delete_cookie(SETUP_BINDING_COOKIE, path=_BINDING_COOKIE_PATH)

    def login_page(request: Request, *, next_path: str, message: str = "", status_code: int = 200) -> Response:
        binding = secrets.token_urlsafe(32)
        enrolled = passkey_enrolled()
        message_html = f'<p class="error" role="alert">{_esc(message)}</p>' if message else ""
        if enrolled:
            action_html = (
                "<p>Sign in with the passkey you set up for this server.</p>"
                '<div class="cluster"><button type="button" class="button primary" id="pf-passkey-signin">'
                "Sign in with your passkey</button></div>"
                '<p id="pf-login-status" class="meta" role="status"></p>'
            )
        elif setup.available:
            action_html = (
                "<p>Enter the setup code from the PrivacyFence log. You then add a passkey, "
                "which is how you sign in from then on.</p>"
                '<form method="post" action="/login/setup" class="stack">'
                f'<input type="hidden" name="binding" value="{_esc(binding)}">'
                f'<input type="hidden" name="next" value="{_esc(next_path)}">'
                '<label for="pf-setup-code">Setup code</label>'
                '<input id="pf-setup-code" name="code" autocomplete="off" autocapitalize="characters" '
                'spellcheck="false" maxlength="32" required>'
                '<div class="cluster"><button type="submit" class="button primary">Continue</button></div>'
                "</form>"
            )
        else:
            action_html = "" if message == USED_UP_MESSAGE else f'<p class="error" role="alert">{_esc(USED_UP_MESSAGE)}</p>'
        nonce = nonce_for(request)
        body = (
            f'<div id="pf-login-page" class="pf-login stack" data-binding="{_esc(binding)}" '
            f'data-next="{_esc(next_path)}">'
            "<h1>Sign in to PrivacyFence</h1>"
            f"{message_html}{action_html}"
            f'<script nonce="{nonce}">{_PAGE_JS}</script>'
            "</div>"
        )
        response = HTMLResponse(
            web_shell.plain_page(body, title="PrivacyFence — Sign in", nonce=nonce, page_css=_PAGE_CSS),
            status_code=status_code, headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            SETUP_BINDING_COOKIE, binding, httponly=True, secure=secure, samesite="strict",
            path=_BINDING_COOKIE_PATH, max_age=_BINDING_TTL_SECONDS,
        )
        return response

    def refused_page(request: Request, message: str, status_code: int) -> Response:
        body = (
            f'<h1>Sign in to PrivacyFence</h1><p class="error" role="alert">{_esc(message)}</p>'
            '<div class="actions"><a class="button primary" href="/login">Start again</a></div>'
        )
        return HTMLResponse(
            web_shell.plain_page(body, title="PrivacyFence — Sign in", nonce=nonce_for(request), page_css=_PAGE_CSS),
            status_code=status_code, headers={"Cache-Control": "no-store"},
        )

    async def login(request: Request) -> Response:
        next_path = safe_next_path(request.query_params.get("next"), default=default_next_path)
        if org_session.authenticated(request, sessions) == SINGLE_USER_PRINCIPAL:
            # Already signed in: an AI client's authorization goes straight on to its consent.
            return RedirectResponse(next_path, status_code=302, headers={"Cache-Control": "no-store"})
        return login_page(request, next_path=next_path)

    async def login_setup(request: Request) -> Response:
        if not org_session.check_origin(request):
            _audit("single_user_setup_refused", "Setup code sign-in refused: the request came from another site.")
            return refused_page(request, "This sign-in did not come from this server's own page.", 403)
        form = await request.form()
        code, next_raw = form.get("code"), form.get("next")
        next_path = safe_next_path(next_raw if isinstance(next_raw, str) else None, default=default_next_path)
        if _bound(request, form.get("binding")) is None:
            _audit(
                "single_user_setup_refused",
                "Setup code sign-in refused: the form was not loaded in this browser, or it expired.",
            )
            return refused_page(request, "This sign-in page has expired. Start again.", 403)
        if passkey_enrolled():
            _audit("single_user_setup_refused", "Setup code sign-in refused: a passkey is already enrolled.")
            return login_page(request, next_path=next_path, message=PASSKEY_ENROLLED_MESSAGE, status_code=403)
        result = setup.check(code if isinstance(code, str) else "")
        if result == SetupCode.WRONG:
            _audit("single_user_setup_refused", "Setup code sign-in refused: the code was not right.")
            return login_page(request, next_path=next_path, message=WRONG_CODE_MESSAGE, status_code=400)
        if result == SetupCode.USED_UP:
            _audit("single_user_setup_refused", "Setup code sign-in refused: the setup code is used up.")
            return login_page(request, next_path=next_path, message=USED_UP_MESSAGE, status_code=400)
        response = RedirectResponse(
            f"/security?next={quote(next_path, safe='/')}", status_code=303, headers={"Cache-Control": "no-store"},
        )
        start_session(request, response)
        # Keeps the provisioners' "signed in within 90 days" sweep honest (ADR 0272).
        peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)
        await asyncio.to_thread(grist_provisioning.notify_sign_in, SINGLE_USER_PRINCIPAL, email_verified=True)
        _audit("single_user_setup_signin", "Signed in with the setup code; the code is now used.")
        return response

    async def _json_body(request: Request) -> dict | None:
        try:
            payload = await request.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    async def passkey_options(request: Request) -> Response:
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        payload = await _json_body(request)
        if payload is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        binding = _bound(request, payload.get("binding"))
        if binding is None:
            return JSONResponse({"error": "This sign-in page has expired. Reload it."}, status_code=403)
        if not step_up.rp_id:
            return JSONResponse({"error": "Passkeys are not configured on this server."}, status_code=400)
        begun = webauthn_stepup.begin_assertion(SINGLE_USER_PRINCIPAL, rp_id=step_up.rp_id)
        if begun is None:
            return JSONResponse({"error": "No passkey is set up yet."}, status_code=400)
        options_json, challenge = begun
        next_raw = payload.get("next")
        next_path = safe_next_path(next_raw if isinstance(next_raw, str) else None, default=default_next_path)
        challenges.put(binding, challenge=challenge, next_path=next_path)
        return JSONResponse({"options": json.loads(options_json)}, headers={"Cache-Control": "no-store"})

    async def passkey_verify(request: Request) -> Response:
        if not org_session.check_origin(request):
            _audit("single_user_passkey_refused", "Passkey sign-in refused: the request came from another site.")
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        payload = await _json_body(request)
        if payload is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        binding = _bound(request, payload.get("binding"))
        if binding is None:
            _audit(
                "single_user_passkey_refused",
                "Passkey sign-in refused: the page was not loaded in this browser, or it expired.",
            )
            return JSONResponse({"error": "This sign-in page has expired. Reload it."}, status_code=403)
        pending = challenges.pop(binding)
        if pending is None:
            _audit("single_user_passkey_refused", "Passkey sign-in refused: the challenge expired or was already used.")
            return JSONResponse({"error": "This sign-in expired. Try again."}, status_code=400)
        credential = payload.get("credential")
        try:
            if not isinstance(credential, dict):
                raise webauthn_stepup.WebAuthnError("missing credential")
            webauthn_stepup.verify_assertion(
                SINGLE_USER_PRINCIPAL, credential, expected_challenge=pending.challenge,
                rp_id=step_up.rp_id, origin=origin,
            )
        except webauthn_stepup.WebAuthnError as exc:
            _audit("single_user_passkey_refused", f"Passkey sign-in refused: {exc}")
            return JSONResponse({"error": "The passkey could not be verified."}, status_code=401)
        response = JSONResponse({"redirect": pending.next_path}, headers={"Cache-Control": "no-store"})
        start_session(request, response)
        # Keeps the provisioners' "signed in within 90 days" sweep honest (ADR 0272).
        peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)
        await asyncio.to_thread(grist_provisioning.notify_sign_in, SINGLE_USER_PRINCIPAL, email_verified=True)
        _audit("single_user_passkey_signin", "Signed in with a passkey.")
        return response

    async def logout(request: Request) -> Response:
        # POST-only with no CSRF token, as routes_org_identity's: a forged post can only sign
        # this browser out.
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        if session_id:
            sessions.destroy(session_id)
            end_browser_push(session_id)
        response = RedirectResponse(SIGNED_OUT_PATH, status_code=303, headers={"Cache-Control": "no-store"})
        org_session.clear_session_cookie(response)
        return response

    async def signed_out(request: Request) -> Response:
        body = (
            "<h1>You're signed out</h1>"
            "<p>Sign in again to approve requests or manage your connections.</p>"
            '<div class="actions"><a class="button primary" href="/login">Sign in</a></div>'
        )
        return HTMLResponse(
            web_shell.plain_page(body, title="PrivacyFence — Signed out", nonce=nonce_for(request)),
            headers={"Cache-Control": "no-store"},
        )

    return [
        Route("/login", login, methods=["GET"]),
        Route("/login/setup", login_setup, methods=["POST"]),
        Route("/login/passkey/options", passkey_options, methods=["POST"]),
        Route("/login/passkey/verify", passkey_verify, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route(SIGNED_OUT_PATH, signed_out),
    ]


__all__ = ["SETUP_BINDING_COOKIE", "SINGLE_USER_PRINCIPAL", "SetupCode", "build_routes", "normalize_setup_code"]
