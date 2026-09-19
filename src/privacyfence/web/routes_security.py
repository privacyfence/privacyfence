"""Passkey enrollment, for org mode and (#426 Phase 1) local mode alike:
``GET /security`` lets a signed-in principal see and manage their own
enrolled WebAuthn credentials, and the ``/api/security/webauthn/*`` routes
drive the two ceremonies webauthn_stepup.py implements. web/routes_
org_approvals.py's decide endpoint (org mode) is the other, later consumer
of an enrolled credential (the actual step-up check on a write approval);
local mode's own decide-time check is #426 Phase 2 -- this module only ever
registers or removes a credential, in either mode.

Same posture as web/routes_connect.py (not a port of routes_settings.py's
whole surface, a session-cookie CSRF model, not web_shell.wrap()'d) -- see
that module's own docstring for the reasoning, which applies here
unchanged. Mode-agnostic since #426 Phase 1: ``build_routes`` takes a
principal/session resolver rather than an ``OrgSessionStore`` directly, so
org mode passes ``org_session``'s functions and local mode passes web/
session_auth.py's -- the two modules already have matching shapes
(``authenticated``/``check_csrf``/``check_origin``) for exactly this reason
(session_auth.py's own module docstring: "mirroring web/org_session.py's
own real-session model").

``PF_WEBAUTHN_JS`` (the base64url <-> ArrayBuffer conversions and the two
``navigator.credentials`` wrapper calls) is defined here and imported by
web/routes_org_approvals.py's own step-up shim rather than duplicated --
this module owns it only because enrollment is where the ceremony's shape
first has to exist; there is nothing enrollment-specific about the helpers
themselves.

**Deleting your last credential (#426 Phase 3)** is gated behind a fresh
assertion, regardless of ``step_up.require_passkey`` -- "belt-and-braces
once #428 lands, since the flag is no longer agent-writable, but also the
right behavior for a human at the keyboard" (issue #426's own Phase 3
text): removing the *only* enrolled passkey is what would silently turn a
"mandatory" install back into an unenforced one, so ``delete_credential``
below demands proof of possession of that very credential first, the same
428-then-retry protocol web/routes_approvals.py's decide() uses. Removing
one of *several* enrolled credentials stays a plain, ungated request --
there's no enforcement gap to close when at least one other credential
would remain.

**Tamper-evidence and recovery (#426 Phase 4)**: every enroll and remove
here writes an audit entry (see ``_audit`` below), and ``register_verify``
issues a one-time recovery code -- shown to the browser exactly once, in
that same response -- whenever this principal doesn't currently have an
unused one on file. ``recover_credential`` is the code's only consumer:
trading it in removes every credential this principal has enrolled, for
the case webauthn_stepup.py's own module docstring describes (the only
authenticator lost to a new machine or a wiped TPM, with no IdP in local
mode to fall back on). See that module's own docstring for the storage
side of both.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .. import webauthn_stepup
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..principal import Principal
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import RegistrationChallengeStore, StepUpChallengeStore, WebAuthnError
from .csp import nonce_for as _csp_nonce_for

logger = logging.getLogger(__name__)

# Shared with web/routes_org_approvals.py's decide-time step-up shim -- see
# module docstring. Defines window.pfWebauthnCreate(optionsJson) and
# window.pfWebauthnGet(optionsJson), each returning a Promise of the plain
# JSON-shaped credential object webauthn_stepup.py's finish_registration()/
# verify_assertion() expect (the same camelCase field names @simplewebauthn/
# browser uses, since py_webauthn's own JSON parsing matches that shape --
# see webauthn_stepup.py's module docstring on why hand-rolling the
# verification side, but not this encode/decode plumbing, would be a
# mistake). Written by hand, not loaded from a CDN: web/server.py's CSP
# (script-src is a per-response nonce, no external host -- SEC-08) allows
# no external script on any page this daemon serves -- see web/csp.py's own
# module docstring.
PF_WEBAUTHN_JS = """
function pfB64uToBuf(s) {
  var b64 = s.replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4) { b64 += '='; }
  var bin = atob(b64);
  var buf = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) { buf[i] = bin.charCodeAt(i); }
  return buf.buffer;
}
function pfBufToB64u(buf) {
  var bytes = new Uint8Array(buf);
  var bin = '';
  for (var i = 0; i < bytes.length; i++) { bin += String.fromCharCode(bytes[i]); }
  return btoa(bin).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
}
function pfWebauthnCreate(optionsJson) {
  var o = JSON.parse(optionsJson);
  var publicKey = {
    rp: o.rp,
    user: { id: pfB64uToBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
    challenge: pfB64uToBuf(o.challenge),
    pubKeyCredParams: o.pubKeyCredParams,
    timeout: o.timeout,
    excludeCredentials: (o.excludeCredentials || []).map(function (c) {
      return { id: pfB64uToBuf(c.id), type: c.type };
    }),
    authenticatorSelection: o.authenticatorSelection,
    attestation: o.attestation
  };
  return navigator.credentials.create({ publicKey: publicKey }).then(function (cred) {
    return {
      id: cred.id, rawId: pfBufToB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: pfBufToB64u(cred.response.clientDataJSON),
        attestationObject: pfBufToB64u(cred.response.attestationObject)
      }
    };
  });
}
function pfWebauthnGet(optionsJson) {
  var o = JSON.parse(optionsJson);
  var publicKey = {
    rpId: o.rpId,
    challenge: pfB64uToBuf(o.challenge),
    timeout: o.timeout,
    allowCredentials: (o.allowCredentials || []).map(function (c) {
      return { id: pfB64uToBuf(c.id), type: c.type };
    }),
    userVerification: o.userVerification
  };
  return navigator.credentials.get({ publicKey: publicKey }).then(function (cred) {
    return {
      id: cred.id, rawId: pfBufToB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: pfBufToB64u(cred.response.clientDataJSON),
        authenticatorData: pfBufToB64u(cred.response.authenticatorData),
        signature: pfBufToB64u(cred.response.signature),
        userHandle: cred.response.userHandle ? pfBufToB64u(cred.response.userHandle) : null
      }
    };
  });
}
"""


def build_routes(
    *,
    resolve_principal: Callable[[Request], "Principal | None"],
    check_csrf: Callable[[Request, Any], bool],
    check_origin: Callable[[Request], bool],
    unauthenticated_response: Callable[[Request], Response],
    session_cookie_name: str,
    step_up: StepUpConfig,
    issuer_url: str,
    back_link: tuple[str, str] = ("/connect", "Back to connections"),
    dev_unseparated_notice: str | None = None,
) -> list[Route]:
    """``resolve_principal``/``check_csrf``/``check_origin`` are the
    mode-specific half of this module (#426 Phase 1) -- org mode's caller
    (web/server.py's ``_build_org_app``) passes ``org_session``'s three
    functions bound to its own ``OrgSessionStore``; local mode's caller
    passes web/session_auth.py's, bound to its own ``LocalSessionStore``.
    ``unauthenticated_response`` covers the one place the two modes
    genuinely differ in *behavior*, not just which store backs the check:
    org mode redirects an unauthenticated page view to ``/login``, which
    local mode has no equivalent of -- it shows session_auth.py's own
    ``unauthorized_html`` recovery page instead. ``session_cookie_name`` is
    only for embedding the right cookie's value as this page's own CSRF
    token (the double-submit scheme's session-id-doubles-as-token design,
    see either session module's own ``check_csrf`` docstring) -- reading it
    directly here rather than through another callable, since it's a bare
    string either way. ``back_link`` is an ``(href, label)`` pair for the
    page's own footer link -- it defaults to org mode's ``/connect`` (routes_
    connect.py) since that was this module's only caller until #426 Phase 1;
    local mode's caller overrides it to ``/settings/connectors``, since it
    has no ``/connect`` route to link to (settings_window_html.py's
    Connectors tab is that mode's own equivalent). ``dev_unseparated_notice``
    (ADR 0003 decision 7) is local mode's own
    ``privilege_separation.dev_unseparated_notice()`` result, shown verbatim
    at the top of the page when not ``None``; org mode's caller leaves it
    unset since that function is never non-``None`` there (org mode is never
    a packaged build).
    """
    challenges = RegistrationChallengeStore()
    delete_challenges = StepUpChallengeStore()
    origin = issuer_url.rstrip("/")

    def _delete_fingerprint(principal_id: str, credential_id: str) -> str:
        payload = f"delete-credential|{principal_id}|{credential_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _check_post(request: Request, csrf: Any) -> Response | None:
        if not check_csrf(request, csrf):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    def _audit(principal: Principal, decision: str, summary: str) -> None:
        """#426 Phase 4: one audit entry per credential-store lifecycle
        event -- enroll, remove, and (recover_credential, below) a spent
        recovery code. Never allowed to block or fail the request it's
        attached to -- same posture as every other non-critical audit call
        in this codebase (see docs/coding-and-testing-guidelines.md
        §1.4's "non-critical side effects" rule)."""
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary=summary,
                sender=principal.email or principal.display_name or principal.id,
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for %s: %s", decision, exc)

    async def security_page(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return unauthenticated_response(request)
        session_id = request.cookies.get(session_cookie_name, "")
        creds = webauthn_stepup.list_credentials(principal)
        html = _render_security_page(
            principal=principal, creds=creds, csrf=session_id, step_up=step_up,
            nonce=_csp_nonce_for(request), back_link=back_link,
            dev_unseparated_notice=dev_unseparated_notice,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def register_options(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rejected = _check_post(request, payload.get("csrf") if isinstance(payload, dict) else None)
        if rejected is not None:
            return rejected
        if not step_up.rp_id:
            return JSONResponse({"error": "WebAuthn is not configured on this server"}, status_code=400)
        options_json, challenge = webauthn_stepup.begin_registration(
            principal, rp_id=step_up.rp_id, rp_name=step_up.rp_name,
        )
        challenges.put(principal.id, challenge)
        return JSONResponse({"options": json.loads(options_json)})

    async def register_verify(request: Request) -> Response:
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_post(request, payload.get("csrf"))
        if rejected is not None:
            return rejected
        challenge = challenges.pop(principal.id)
        if challenge is None:
            return JSONResponse({"error": "Registration attempt expired -- try again."}, status_code=400)
        credential = payload.get("credential")
        label = str(payload.get("label") or "Passkey")[:64]
        if not isinstance(credential, dict):
            return JSONResponse({"error": "missing credential"}, status_code=400)
        try:
            saved = webauthn_stepup.finish_registration(
                principal, credential, expected_challenge=challenge,
                rp_id=step_up.rp_id, origin=origin, label=label,
            )
        except WebAuthnError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        _audit(principal, "webauthn_credential_enrolled", f"Passkey enrolled: {saved.label!r}")
        response: dict[str, Any] = {"status": "ok", "credential_id": saved.credential_id, "label": saved.label}
        # #426 Phase 4: issue a recovery code the moment there stops being
        # an unused one on file -- covers both the first-ever enrollment
        # and an upgrade from before this feature existed. Returned exactly
        # once, in this response only; _PAGE_JS is what actually shows it
        # to the human -- see this module's own docstring on why it can
        # never be recovered again after this.
        if not webauthn_stepup.has_recovery_code(principal):
            response["recovery_code"] = webauthn_stepup.generate_recovery_code(principal)
        return JSONResponse(response)

    async def delete_credential(request: Request) -> Response:
        """A JSON/fetch endpoint (not a plain form submit) since removing
        the *last* enrolled credential needs a two-round-trip WebAuthn
        ceremony -- see module docstring. Removing one of several stays a
        single request: ``is_last`` below is false, so the gate never
        triggers and this behaves exactly like the old form-POST did."""
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rejected = _check_post(request, payload.get("csrf") if isinstance(payload, dict) else None)
        if rejected is not None:
            return rejected
        credential_id = request.path_params["credential_id"]
        existing = webauthn_stepup.list_credentials(principal)
        is_last = len(existing) == 1 and existing[0].credential_id == credential_id
        if is_last:
            assertion = payload.get("webauthn_assertion") if isinstance(payload, dict) else None
            if not isinstance(assertion, dict):
                begun = webauthn_stepup.begin_assertion(principal, rp_id=step_up.rp_id) if step_up.rp_id else None
                if begun is None:
                    # is_last already proved a credential exists to assert
                    # against -- only an unconfigured rp_id lands here, and
                    # /security itself is never mounted without one (see
                    # web/server.py's own step_up.rp_id gate).
                    return JSONResponse({"error": "WebAuthn is not configured on this server"}, status_code=400)
                options_json, challenge = begun
                fingerprint = _delete_fingerprint(principal.id, credential_id)
                delete_challenges.put(principal.id, credential_id, challenge=challenge, fingerprint=fingerprint)
                return JSONResponse(
                    {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
                )
            pending = delete_challenges.pop(principal.id, credential_id)
            expected_fp = _delete_fingerprint(principal.id, credential_id)
            if pending is None or pending.fingerprint != expected_fp:
                return JSONResponse({"error": "step_up_expired"}, status_code=400)
            try:
                webauthn_stepup.verify_assertion(
                    principal, assertion, expected_challenge=pending.challenge, rp_id=step_up.rp_id, origin=origin,
                )
            except WebAuthnError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
        webauthn_stepup.remove_credential(principal, credential_id)
        _audit(principal, "webauthn_credential_removed", f"Passkey removed: {credential_id}")
        return JSONResponse({"status": "ok"})

    async def recover_credential(request: Request) -> Response:
        """#426 Phase 4: the sanctioned way back in when the only enrolled
        authenticator is lost (new machine, wiped TPM) -- see module
        docstring and webauthn_stepup.py's own on generate_recovery_code/
        consume_recovery_code. A still-signed-in principal (this route
        needs no WebAuthn ceremony of its own -- that's the whole point:
        an assertion is exactly what a locked-out human can no longer
        produce) who cannot pass a step-up challenge exchanges the
        one-time recovery code shown at their first enrollment for a clean
        slate: every credential on file for them is removed, so /security
        lets them enroll a fresh passkey immediately afterward. The code
        itself is single-use (webauthn_stepup.consume_recovery_code) and
        this always records who spent it, success or not revealing which
        specific check failed."""
        principal = resolve_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rejected = _check_post(request, payload.get("csrf") if isinstance(payload, dict) else None)
        if rejected is not None:
            return rejected
        code = payload.get("code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or not code.strip():
            return JSONResponse({"error": "missing recovery code"}, status_code=400)
        if not webauthn_stepup.consume_recovery_code(principal, code):
            return JSONResponse({"error": "invalid or already-used recovery code"}, status_code=401)
        for cred in webauthn_stepup.list_credentials(principal):
            webauthn_stepup.remove_credential(principal, cred.credential_id)
        _audit(
            principal, "webauthn_recovery_code_used",
            "Recovery code used -- all enrolled passkeys removed, ready for fresh enrollment",
        )
        return JSONResponse({"status": "ok"})

    return [
        Route("/security", security_page),
        Route("/api/security/webauthn/register/options", register_options, methods=["POST"]),
        Route("/api/security/webauthn/register/verify", register_verify, methods=["POST"]),
        Route("/security/credentials/{credential_id}/delete", delete_credential, methods=["POST"]),
        Route("/security/recover", recover_credential, methods=["POST"]),
    ]


# --------------------------------------------------------------------- #
# Page rendering -- same small, self-contained-document style as
# web/routes_connect.py's own _render_connect_page (see that module's own
# note on why this isn't web_shell.wrap()'d).
# --------------------------------------------------------------------- #

_STYLE = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:640px;margin:0 auto;
  padding:32px 20px 64px;color:#1b1b1f;background:#fff}
h1{font-size:20px;margin:0 0 4px}
p.lead{color:#555;margin-top:0}
.flash{border-radius:8px;padding:10px 14px;margin:16px 0;font-size:14px}
.flash.ok{background:#e6f4ea;color:#1e7e34}
.flash.err{background:#fdecea;color:#a02a2a}
ul.creds{list-style:none;padding:0;margin:24px 0}
li.cred{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #eee}
li.cred:last-child{border-bottom:none}
.name{font-weight:600}
.meta{color:#888;font-size:12px}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;margin-left:8px;background:#f1f1f3;color:#555}
button.add{padding:8px 16px;border:none;border-radius:6px;background:#2451c9;color:#fff;font-size:14px;cursor:pointer}
button.remove{background:none;color:#a02a2a;text-decoration:underline;border:none;padding:0;font-size:13px;cursor:pointer}
.empty{color:#888;padding:20px 0}
"""

_PAGE_JS = """
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('pf-add-passkey');
  var status = document.getElementById('pf-passkey-status');
  var csrf = document.body.getAttribute('data-csrf');
  if (!btn) { return; }
  if (!window.PublicKeyCredential) {
    btn.disabled = true;
    if (status) { status.textContent = 'This browser does not support passkeys.'; }
    return;
  }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    if (status) { status.textContent = 'Follow your browser\\'s prompt...'; }
    fetch('/api/security/webauthn/register/options', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({csrf: csrf})
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) { throw new Error(data.error); }
      return pfWebauthnCreate(JSON.stringify(data.options));
    }).then(function (credential) {
      return fetch('/api/security/webauthn/register/verify', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: csrf, credential: credential, label: 'Passkey'})
      });
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) { throw new Error(data.error); }
      // #426 Phase 4: a recovery code is issued the moment none is
      // currently unused -- shown exactly once, here, since the server
      // never returns it again after this response.
      if (data.recovery_code) {
        window.alert(
          'Save this recovery code somewhere safe -- it will not be shown again.\\n\\n' +
          data.recovery_code +
          '\\n\\nIf you ever lose every passkey enrolled here, this code is the only way back in.'
        );
      }
      window.location.reload();
    }).catch(function (err) {
      btn.disabled = false;
      if (status) { status.textContent = 'Could not add a passkey: ' + err.message; }
    });
  });

  // #426 Phase 4: the recovery-code path for a lost/unusable authenticator
  // -- no WebAuthn ceremony, just the one-time code from enrollment.
  var recoverBtn = document.getElementById('pf-use-recovery-code');
  var recoverStatus = document.getElementById('pf-recovery-status');
  if (recoverBtn) {
    recoverBtn.addEventListener('click', function () {
      var code = window.prompt('Enter your recovery code:');
      if (!code) { return; }
      recoverBtn.disabled = true;
      fetch('/security/recover', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: csrf, code: code})
      }).then(function (r) { return r.json().then(function (data) { return {ok: r.ok, data: data}; }); })
        .then(function (result) {
          if (!result.ok) { throw new Error(result.data.error || 'recovery code was not accepted'); }
          window.location.reload();
        }).catch(function (err) {
          recoverBtn.disabled = false;
          if (recoverStatus) { recoverStatus.textContent = 'Could not recover: ' + err.message; }
        });
    });
  }

  // #426 Phase 3: removing your *only* enrolled passkey demands a fresh
  // assertion first (module docstring) -- the server's own 428 carries
  // options only in that case, so this same handler is a plain one-shot
  // delete whenever it isn't (a 200 with no 428 round trip at all).
  function pfDeleteCredential(credId, assertion) {
    var body = {csrf: csrf};
    if (assertion) { body.webauthn_assertion = assertion; }
    return fetch('/security/credentials/' + encodeURIComponent(credId) + '/delete', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.status === 428) {
        return r.json().then(function (data) {
          if (!data.webauthn_options || !window.PublicKeyCredential) {
            throw new Error('removing your only passkey needs a passkey prompt, and none is available');
          }
          return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function (newAssertion) {
            return pfDeleteCredential(credId, newAssertion);
          });
        });
      }
      return r.json().then(function (data) {
        if (!r.ok) { throw new Error(data.error || 'could not remove this passkey'); }
        window.location.reload();
      });
    });
  }
  document.querySelectorAll('button.remove[data-credential-id]').forEach(function (removeBtn) {
    removeBtn.addEventListener('click', function () {
      removeBtn.disabled = true;
      pfDeleteCredential(removeBtn.getAttribute('data-credential-id'), null).catch(function (err) {
        removeBtn.disabled = false;
        window.alert('Could not remove this passkey: ' + err.message);
      });
    });
  });
});
""" + PF_WEBAUTHN_JS


def _credential_row_html(cred) -> str:
    created = ""
    try:
        import datetime as _dt
        created = _dt.datetime.fromtimestamp(cred.created_at, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        pass
    synced = '<span class="badge">Synced</span>' if cred.backed_up else '<span class="badge">Device-bound</span>'
    # Plain JS-driven button, not a <form> -- removal is a fetch() POST
    # (module docstring: the last-credential case needs a 428-then-retry
    # WebAuthn round trip a plain form submit can't carry). data-credential-id
    # is read by _PAGE_JS's own remove handler below.
    return (
        f'<li class="cred" data-credential-id="{_esc(cred.credential_id)}"><span>'
        f'<span class="name">{_esc(cred.label)}</span>{synced}'
        f'<div class="meta">Added {_esc(created)}</div></span>'
        f'<button type="button" class="remove" data-credential-id="{_esc(cred.credential_id)}">Remove</button></li>'
    )


# Keyed by step_up_config.StepUpScope; an unrecognized value can only reach
# here from a StepUpConfig built by hand in a test, and reads as the
# narrowest scope rather than overstating what the passkey covers.
_SCOPE_NOTES = {
    "writes": "Required to approve a write.",
    "writes_and_pii_reads": "Required to approve a write, or a read that detected personal data.",
    "writes_and_reads": "Required to approve a write or a read.",
}


def _render_security_page(
    *, principal: Principal, creds: list, csrf: str, step_up: StepUpConfig, nonce: str,
    back_link: tuple[str, str], dev_unseparated_notice: str | None = None,
) -> str:
    who = _esc(principal.email or principal.display_name or principal.id)
    rows = "".join(_credential_row_html(c) for c in creds)
    body = f'<ul class="creds">{rows}</ul>' if creds else '<div class="empty">No passkeys added yet.</div>'
    scope_note = _SCOPE_NOTES.get(step_up.scope, _SCOPE_NOTES["writes"])
    dev_notice_html = (
        f'<p class="flash err">{_esc(dev_unseparated_notice)}</p>' if dev_unseparated_notice else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrivacyFence -- Security</title><style nonce="{nonce}">{_STYLE}</style></head>
<body data-csrf="{_esc(csrf)}">
<h1>Passkeys</h1>
{dev_notice_html}
<p class="lead">Signed in as {who}. A passkey (Face ID, Touch ID, fingerprint, or Windows Hello) proves it's
really you before a write approval is released, even if someone else has your unlocked phone. {_esc(scope_note)}</p>
{body}
<p><button type="button" class="add" id="pf-add-passkey">Add a passkey</button>
<span id="pf-passkey-status" class="meta"></span></p>
<p>Lost every passkey enrolled here? <button type="button" class="remove" id="pf-use-recovery-code">Use your recovery code</button>
<span id="pf-recovery-status" class="meta"></span></p>
<p><a href="{_esc(back_link[0])}">{_esc(back_link[1])}</a></p>
<script nonce="{nonce}">{_PAGE_JS}</script>
</body></html>"""


__all__ = ["PF_WEBAUTHN_JS", "build_routes"]
