"""Passkey enrollment, for org mode and local mode alike:
``GET /security`` lets a signed-in principal see and manage their own
enrolled WebAuthn credentials, and the ``/api/security/webauthn/*`` routes
drive the two ceremonies webauthn_stepup.py implements. web/routes_
approvals.py's decide endpoint is the other, later consumer of an enrolled
credential (the actual step-up check on a write approval), in both modes --
this module only ever registers or removes a credential.

Same posture as web/routes_connect.py (not a port of routes_settings.py's
whole surface, a session-cookie CSRF model) -- see that module's own
docstring for the reasoning, which applies here unchanged. Mode-agnostic:
``build_routes`` takes a principal/session resolver
rather than an ``OrgSessionStore`` directly, so org mode passes ``org_
session``'s functions and local mode passes web/session_auth.py's -- the
two modules already have matching shapes (``authenticated``/``check_csrf``/
``check_origin``) for exactly this reason (session_auth.py's own module
docstring: "mirroring web/org_session.py's own real-session model").

``nav_items`` (optional, default ``None``) is one of the two places this
module's two callers genuinely differ (``confirm_first_enrollment``, below, is
the other): org mode's (web/server.py's
``_build_org_app``) passes ``web_shell.ORG_NAV_ITEMS`` so this page carries
the same persistent header/nav as ``/approvals``/``/connect``/``/settings``,
replacing the plain ``back_link`` footer paragraph those three pages also
used to be the only way back from. Local mode passes nothing -- it has no
web_shell-wrapped page of its own to be consistent with (settings_window_
html.py's Connectors tab is that mode's own equivalent, see ``back_link``
below), so ``/security`` stays the small, unwrapped document it always was
there, with its footer ``back_link`` intact.

``PF_WEBAUTHN_JS`` (the base64url <-> ArrayBuffer conversions and the two
``navigator.credentials`` wrapper calls) is defined here and imported by
web/routes_approvals.py's own step-up shim rather than duplicated --
this module owns it only because enrollment is where the ceremony's shape
first has to exist; there is nothing enrollment-specific about the helpers
themselves.

**Deleting your last credential** is gated behind a fresh
assertion, regardless of ``step_up.require_passkey`` -- belt-and-braces on
a privilege-separated install, where the flag is not agent-writable, but
also the right behavior for a human at the keyboard: removing the *only* enrolled passkey is what would silently turn a
"mandatory" install back into an unenforced one, so ``delete_credential``
below demands proof of possession of that very credential first, the same
428-then-retry protocol web/routes_approvals.py's decide() uses. Removing
one of *several* enrolled credentials stays a plain, ungated request --
there's no enforcement gap to close when at least one other credential
would remain.

**Enrolling a passkey is itself gated.** The asymmetry this closes is the
one the paragraph above describes from the other side: removing your *last*
credential demands proof of possession of it, on the stated grounds that
leaving that ungated "would silently turn a 'mandatory' install back into an
unenforced one" -- and *adding* one has exactly the same effect. Ungated, it
is the shorter route to the same place: a local process holding a session
(ADR 0002 decision 6 names three ways one is reachable, all by design)
enrolls a credential it generated itself and satisfies every later step-up
check with it, on the strongest configuration this product offers. Nothing
downstream can tell such a credential from a real one: registration uses
``none`` attestation and the user-verified flag is a bit the authenticator
sets about itself, so ``require_user_verification=True`` is a claim, not a
proof -- see webauthn_stepup.py's own "five things" list. This gate is
what made defaulting ``require_passkey`` on defensible (ADR 0003's *Out of
scope* amendment).

So ``register_options`` gates the ceremony before it starts, in whichever of
two ways the credential store's own state allows:

- **A credential is already enrolled** -- the gate is a fresh assertion with
  one of them, over a challenge bound to ``enroll-credential|<principal>``
  (``_enroll_fingerprint`` below), through the same 428-then-retry protocol
  ``delete_credential`` and web/routes_approvals.py's ``decide`` already
  share. Identical in both modes: an org-mode IdP session gets no more
  latitude here than a local ``pf_session``.
- **Nothing is enrolled yet** -- there is nothing to assert with, so the
  gate is ``confirm_first_enrollment`` (``build_routes``' own parameter):
  local mode passes web/server.py's companion confirmation, which asks
  whoever is at the login session (web/control_channel.py's
  ``CONFIRM ENROLL``). That is not authentication of the companion and
  cannot be -- companion and agent share a uid -- so be careful what it is
  claimed to buy; docs/security-and-compliance.md states the limit exactly.
  Org mode passes nothing, and its first enrollment rests on the IdP session
  that reached ``/security``, stated as such in that same document rather
  than papered over.

``register_verify`` does not re-run either gate (that would mean a second
prompt for one enrollment); it requires that the registration challenge it
is completing was issued by a ``register_options`` call that *passed* one --
``RegistrationChallengeStore``'s own ``authorized`` flag, see that class's
docstring. A refusal by either gate is audited
(``webauthn_enrollment_refused``), which is the signal that makes an
enrollment nobody asked for distinguishable from one a human made -- the
``webauthn_credential_enrolled`` entry alone cannot tell them apart.

**Tamper-evidence and recovery**: every enroll and remove
here writes an audit entry (see ``_audit`` below), and ``register_verify``
issues a one-time recovery code whenever this principal doesn't currently
have an unused one on file. Who shows it is ``deliver_recovery_code``'s
answer (see ``build_routes``' own docstring and ADR 0003's *Out of scope*
amendment): the browser,
once, in that same response -- or, on a packaged local-mode install, the
companion app on the human's own desktop, with nothing about it in the
response at all. ``recover_credential`` is the code's only consumer:
trading it in removes every credential this principal has enrolled, for
the case webauthn_stepup.py's own module docstring describes (the only
authenticator lost to a new machine or a wiped TPM, with no IdP in local
mode to fall back on). See that module's own docstring for the storage
side of both. Because that trade-in wipes the credential store, the route
asks the same human-session question approving a decision does (where the
caller supplies one), spends a per-session and global attempt budget
(``RecoveryAttemptLimiter``), and audits every refusal as well as every
success -- see ``recover_credential``'s own docstring.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .. import web_shell, webauthn_stepup
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..principal import Principal
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import RegistrationChallengeStore, StepUpChallengeStore, WebAuthnError
from .csp import nonce_for as _csp_nonce_for
from .session_auth import human_session_required_json

logger = logging.getLogger(__name__)

# StepUpChallengeStore is keyed by (principal_id, approval_id) -- an
# enrollment has no approval, and only one enrollment ceremony per principal
# is ever meaningfully in flight, so the second half of the key is this one
# constant. The string is the same one _enroll_fingerprint() binds to, so a
# reader grepping either finds the other.
_ENROLL_CEREMONY = "enroll-credential"

# ``recover_credential``'s attempt budget. A recovery code is 64 random bits,
# so guessing is not the realistic threat; what these bound is a local
# process (or a browser tab left open) hammering the one route that clears a
# principal's whole credential store. Per session: a human retyping a code
# they misread needs a few tries, not dozens. Globally: minting fresh
# sessions must not reset the budget. Both count every attempt that presents
# a code, successful or not, over a sliding window of this length.
RECOVERY_WINDOW_SECONDS = 15 * 60
RECOVERY_MAX_ATTEMPTS_PER_SESSION = 5
RECOVERY_MAX_ATTEMPTS_GLOBAL = 20


class RecoveryAttemptLimiter:
    """An in-memory sliding-window limiter for ``POST /security/recover``:
    at most ``per_key`` attempts per session and ``global_limit`` across all
    sessions within any ``window_seconds``. Deliberately process-local and
    unpersisted -- a daemon restart resets it, which costs an attacker a
    restart they cannot trigger from a session, and there is no other
    limiter in this package to share. ``clock`` exists for tests."""

    def __init__(
        self,
        *,
        window_seconds: float = RECOVERY_WINDOW_SECONDS,
        per_key: int = RECOVERY_MAX_ATTEMPTS_PER_SESSION,
        global_limit: int = RECOVERY_MAX_ATTEMPTS_GLOBAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._per_key = per_key
        self._global_limit = global_limit
        self._clock = clock
        self._lock = threading.Lock()
        self._global: deque[float] = deque()
        self._by_key: dict[str, deque[float]] = {}

    def _prune(self, stamps: deque[float], now: float) -> None:
        while stamps and stamps[0] <= now - self._window:
            stamps.popleft()

    def try_acquire(self, key: str) -> int | None:
        """Records an attempt for ``key`` and returns ``None`` when it is
        within budget; otherwise records nothing and returns the whole
        seconds until the earliest blocking attempt leaves the window (the
        ``Retry-After`` value)."""
        with self._lock:
            now = self._clock()
            self._prune(self._global, now)
            for k in list(self._by_key):
                self._prune(self._by_key[k], now)
                if not self._by_key[k]:
                    del self._by_key[k]
            mine = self._by_key.get(key, deque())
            # Each full window frees up once its oldest stamp ages out; when
            # both are full, the request waits for the later of the two.
            frees_at = [
                stamps[0] + self._window
                for stamps, limit in ((mine, self._per_key), (self._global, self._global_limit))
                if len(stamps) >= limit
            ]
            if frees_at:
                return max(1, math.ceil(max(frees_at) - now))
            mine.append(now)
            self._by_key[key] = mine
            self._global.append(now)
            return None

# Shared with web/routes_approvals.py's decide-time step-up shim -- see
# module docstring. Defines window.pfWebauthnCreate(optionsJson) and
# window.pfWebauthnGet(optionsJson), each returning a Promise of the plain
# JSON-shaped credential object webauthn_stepup.py's finish_registration()/
# verify_assertion() expect (the same camelCase field names @simplewebauthn/
# browser uses, since py_webauthn's own JSON parsing matches that shape --
# see webauthn_stepup.py's module docstring on why hand-rolling the
# verification side, but not this encode/decode plumbing, would be a
# mistake). Written by hand, not loaded from a CDN: web/server.py's CSP
# (script-src is a per-response nonce, no external host) allows
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
  }, pfExplainCreateError);
}
// The browser's own NotAllowedError text ("The operation either timed out or
// was not allowed") is deliberately vague -- WebAuthn forbids saying more to
// the page -- and is also what a user sees who has no built-in authenticator
// set up (Windows without Windows Hello, most Linux desktops) and dismissed
// the browser's offer of a security key or phone. Asking the browser whether
// a built-in one is available at all tells those two cases apart after the
// fact; the error name stays in the message so a bug report still carries it.
function pfExplainCreateError(err) {
  var name = err && err.name;
  if (name === 'InvalidStateError') {
    throw new Error('this device already has a passkey enrolled here (' + name + ')');
  }
  if (name !== 'NotAllowedError') { throw err; }
  var pkc = window.PublicKeyCredential;
  var probe = (pkc && pkc.isUserVerifyingPlatformAuthenticatorAvailable)
    ? pkc.isUserVerifyingPlatformAuthenticatorAvailable().catch(function () { return true; })
    : Promise.resolve(false);
  return probe.then(function (available) {
    if (!available) {
      throw new Error('this device has no built-in passkey authenticator ready to use. ' +
        'Try again with a security key or your phone, or on Windows set up Windows Hello ' +
        '(Settings > Accounts > Sign-in options, add a PIN) (' + name + ')');
    }
    throw new Error('the passkey prompt was cancelled, timed out, or was blocked. ' +
      'Try again and finish the passkey prompt -- it can open behind ' +
      'this browser window (' + name + ')');
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
    nav_items: tuple[tuple[str, str, str], ...] | None = None,
    confirm_first_enrollment: Callable[[], tuple[bool, str]] | None = None,
    deliver_recovery_code: Callable[[str], tuple[bool, str]] | None = None,
    is_human_session: Callable[[Request], bool] | None = None,
    recovery_limiter: RecoveryAttemptLimiter | None = None,
) -> list[Route]:
    """``resolve_principal``/``check_csrf``/``check_origin`` are the
    mode-specific half of this module -- org mode's caller
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
    page's own footer link, rendered only when ``nav_items`` is ``None``
    (see module docstring) -- it defaults to org mode's ``/connect`` (routes_
    connect.py);
    local mode's caller overrides it to ``/settings/connectors``, since it
    has no ``/connect`` route to link to (settings_window_html.py's
    Connectors tab is that mode's own equivalent). ``nav_items`` is ``None``
    unless the caller wants ``/security`` wrapped in web_shell.wrap()'s
    persistent header/nav instead (module docstring) -- when given, it takes
    over entirely from ``back_link``, which is then never rendered.
    ``confirm_first_enrollment`` (see the module docstring's own enrollment-gate
    section) is the
    gate on an enrollment that has no already-enrolled credential to assert
    against, and is the second place the two modes genuinely differ. Local
    mode passes web/server.py's ``confirm_first_passkey_enrollment``, which
    asks the companion; it returns ``(confirmed, reason)``, where ``reason``
    is shown to whoever is trying to enroll so a refusal is actionable
    rather than mysterious. Org mode passes ``None``, which means a first
    enrollment proceeds on the strength of the IdP session alone -- that
    mode has no companion, and an IdP session is at least an external
    authentication, which a local-mode bootstrap cookie is not. Called off
    the event loop (``asyncio.to_thread``), since what it does is put a
    dialog in front of a human and wait.

    ``deliver_recovery_code`` is the third and last place
    the two modes differ, and it is packaging-dependent rather than
    mode-dependent. ``None`` -- org mode, and any local-mode install that is
    not a packaged build -- keeps what this always did: the one-time
    recovery code goes back in ``register_verify``'s own JSON body and
    ``_PAGE_JS`` shows it. A packaged local-mode install passes web/
    server.py's ``present_recovery_code``, which hands the code to the
    companion to put on the human's own desktop instead, so a credential-
    store reset token never travels in a response body a process holding a
    ``pf_session`` can read (ADR 0003's amendment on the recovery trade). It
    returns ``(delivered, reason)``, and a ``False`` is load-bearing: the
    code is only *stored* once somebody has been shown it, so a failed
    delivery leaves the principal with no code rather than one nobody has.
    Called off the event loop (``asyncio.to_thread``) for the same reason
    ``confirm_first_enrollment`` is -- what it does is put a dialog in
    front of a human.

    ``is_human_session`` gates ``recover_credential`` only: when given, a
    request it answers ``False`` for is refused (403, web/session_auth.py's
    ``human_session_required_json`` body) and audited, the same check web/
    routes_approvals.py's decide and web/routes_settings.py's sensitive
    actions make before they act -- trading in a recovery code wipes every
    enrolled passkey, which is at least as sensitive as either. Local mode
    passes session_auth.py's ``is_human_session`` on a privilege-separated
    install, the same ``require_human_session`` line web/server.py draws for
    those two routes, and ``None`` elsewhere (an unseparated install has no
    companion that could mint a human session at all). Org mode passes
    ``None``: every org session was established by an IdP sign-in (web/
    routes_org_identity.py's ``/login`` is ``OrgSessionStore.create``'s only
    caller), which is that mode's equivalent of attestation -- the same
    reason web/routes_approvals.py keeps ``require_human_session``
    local-only. ``recovery_limiter`` is the attempt budget for that same
    route (``RecoveryAttemptLimiter``, above); ``None`` builds a fresh one
    with the module's default window and limits.

    ``dev_unseparated_notice`` (ADR 0003 decision 7) is local mode's own
    ``privilege_separation.dev_unseparated_notice()`` result, shown verbatim
    at the top of the page when not ``None``; org mode's caller leaves it
    unset since that function is never non-``None`` there (org mode is never
    a packaged build). The two are independent: local mode passes the notice
    and no ``nav_items``, org mode the reverse, and the renderer places the
    notice inside whichever of the two page shells it ends up using.
    """
    challenges = RegistrationChallengeStore()
    delete_challenges = StepUpChallengeStore()
    # The enrollment gate's own binding (module docstring). Its own store rather than a
    # second key space inside delete_challenges: the two ceremonies are
    # concurrent-capable in principle (a page open in two tabs), and keeping
    # them apart means an assertion obtained for one can never be popped by
    # the other even if the fingerprints were ever to collide.
    enroll_challenges = StepUpChallengeStore()
    origin = issuer_url.rstrip("/")
    recovery_attempts = recovery_limiter or RecoveryAttemptLimiter()

    def _delete_fingerprint(principal_id: str, credential_id: str) -> str:
        payload = f"delete-credential|{principal_id}|{credential_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _enroll_fingerprint(principal_id: str) -> str:
        """``delete_credential``'s ``_delete_fingerprint`` binds a ceremony
        to one specific credential because that is what the decision is
        *about*; an enrollment's subject is the principal's credential store
        as a whole (the credential being added does not exist yet, and which
        enrolled credential answers the challenge is the browser's pick), so
        this binds to the principal and the operation and nothing else. It
        is still what stops an assertion obtained for a *delete* -- or for a
        decide-time step-up -- from being replayed into an enrollment."""
        return hashlib.sha256(f"enroll-credential|{principal_id}".encode("utf-8")).hexdigest()

    def _check_post(request: Request, csrf: Any) -> Response | None:
        if not check_csrf(request, csrf):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    def _audit(principal: Principal, decision: str, summary: str) -> None:
        """One audit entry per credential-store lifecycle
        event -- enroll, remove, and (recover_credential, below) a spent
        recovery code. Never allowed to block or fail the request it's
        attached to -- same posture as every other non-critical audit call
        in this codebase (see
        docs/coding-and-testing-guidelines.md §1.4's "non-critical side
        effects" rule)."""
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
            nonce=_csp_nonce_for(request), back_link=back_link, nav_items=nav_items,
            dev_unseparated_notice=dev_unseparated_notice,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def _enrollment_gate(principal: Principal, payload: Any) -> Response | None:
        """The enrollment gate, in full -- see the module docstring for why it
        exists and which of its two halves applies when. Returns the refusal
        to send, or None to let the enrollment ceremony start.

        Called from ``register_options`` only: it is the first of the two
        round trips, so refusing here costs the caller nothing it would
        otherwise have done, and it is the only one of the two that can ask
        for something (a 428 carrying assertion options) rather than merely
        say no.
        """
        existing = webauthn_stepup.list_credentials(principal)
        if not existing:
            # Nothing to assert with. Whether that is gated at all is the
            # caller's own decision -- see build_routes' docstring on
            # confirm_first_enrollment, and docs/security-and-compliance.md
            # on what org mode's own answer to this rests on instead.
            if confirm_first_enrollment is None:
                return None
            confirmed, reason = await asyncio.to_thread(confirm_first_enrollment)
            if confirmed:
                return None
            _audit(
                principal, "webauthn_enrollment_refused",
                f"First passkey enrollment was not confirmed: {reason}",
            )
            return JSONResponse(
                {"error": "first_enrollment_not_confirmed", "detail": reason}, status_code=403,
            )
        assertion = payload.get("webauthn_assertion") if isinstance(payload, dict) else None
        fingerprint = _enroll_fingerprint(principal.id)
        if not isinstance(assertion, dict):
            begun = webauthn_stepup.begin_assertion(principal, rp_id=step_up.rp_id)
            if begun is None:  # pragma: no cover -- see below
                # begin_assertion() returns None only for a principal with no
                # enrolled credential, and ``existing`` above just proved there
                # is one. Reachable only if the credential file is emptied
                # between those two reads, which is a refusal either way --
                # and not one to wave through as "nothing to assert with", or
                # a local process would have found its own way to skip this
                # gate.
                return JSONResponse({"error": "step_up_expired"}, status_code=400)
            options_json, challenge = begun
            enroll_challenges.put(principal.id, _ENROLL_CEREMONY, challenge=challenge, fingerprint=fingerprint)
            return JSONResponse(
                {"error": "step_up_required", "webauthn_options": json.loads(options_json)}, status_code=428,
            )
        pending = enroll_challenges.pop(principal.id, _ENROLL_CEREMONY)
        if pending is None or pending.fingerprint != fingerprint:
            return JSONResponse({"error": "step_up_expired"}, status_code=400)
        try:
            webauthn_stepup.verify_assertion(
                principal, assertion, expected_challenge=pending.challenge,
                rp_id=step_up.rp_id, origin=origin,
            )
        except WebAuthnError as exc:
            _audit(
                principal, "webauthn_enrollment_refused",
                f"Passkey enrollment refused: the step-up assertion failed ({exc})",
            )
            return JSONResponse({"error": str(exc)}, status_code=401)
        return None

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
        gate_refusal = await _enrollment_gate(principal, payload)
        if gate_refusal is not None:
            return gate_refusal
        options_json, challenge = webauthn_stepup.begin_registration(
            principal, rp_id=step_up.rp_id, rp_name=step_up.rp_name,
        )
        # authorized=True is the whole of what register_verify checks --
        # reaching this line means whichever gate applied has already passed
        # (module docstring; RegistrationChallengeStore's own docstring).
        challenges.put(principal.id, challenge, authorized=True)
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
        pending_registration = challenges.pop(principal.id)
        if pending_registration is None:
            return JSONResponse({"error": "Registration attempt expired -- try again."}, status_code=400)
        if not pending_registration.authorized:
            # Unreachable from register_options, which only ever stores an
            # authorized challenge -- kept as the invariant's own tripwire so
            # a future path that issues a registration challenge without
            # passing the enrollment gate fails closed here rather than silently
            # enrolling an unvetted credential. See the module docstring and
            # ADR 0003's *Out of scope* amendment.
            logger.warning("Refused to complete an enrollment whose challenge was never authorized.")
            _audit(
                principal, "webauthn_enrollment_refused",
                "Passkey enrollment refused: the registration challenge was never authorized",
            )
            return JSONResponse({"error": "enrollment_not_authorized"}, status_code=403)
        challenge = pending_registration.challenge
        credential = payload.get("credential")
        label = str(payload.get("label") or "Passkey")[:64]
        if not isinstance(credential, dict):
            return JSONResponse({"error": "missing credential"}, status_code=400)
        # Read before finish_registration, which is what makes it false: which
        # of the gate's two halves authorized this ceremony is not recorded
        # anywhere else, and "the first passkey on this install was enrolled"
        # is the entry a reviewer cares most about -- it is the one that
        # decides what every later step-up check is satisfied by.
        was_first = not webauthn_stepup.has_credentials(principal)
        try:
            saved = webauthn_stepup.finish_registration(
                principal, credential, expected_challenge=challenge,
                rp_id=step_up.rp_id, origin=origin, label=label,
            )
        except WebAuthnError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        _audit(
            principal, "webauthn_credential_enrolled",
            f"{'First passkey' if was_first else 'Passkey'} enrolled: {saved.label!r}",
        )
        response: dict[str, Any] = {"status": "ok", "credential_id": saved.credential_id, "label": saved.label}
        # Issue a recovery code the moment there stops being an unused one
        # on file -- covers both the first-ever enrollment and a store that
        # has credentials but no code. It is shown exactly once either way;
        # what differs (see
        # build_routes' docstring on deliver_recovery_code) is who shows it.
        if not webauthn_stepup.has_recovery_code(principal):
            code = webauthn_stepup.mint_recovery_code()
            if deliver_recovery_code is None:
                webauthn_stepup.store_recovery_code(principal, code)
                response["recovery_code"] = code
            else:
                delivered, reason = await asyncio.to_thread(deliver_recovery_code, code)
                if delivered:
                    # Stored only now. A code that reached nobody is not
                    # this principal's recovery code -- see
                    # webauthn_stepup.mint_recovery_code()'s own docstring.
                    webauthn_stepup.store_recovery_code(principal, code)
                    response["recovery_shown_by_companion"] = True
                else:
                    response["recovery_shown_by_companion"] = False
                    response["recovery_detail"] = reason
                    logger.warning("No recovery code was issued at enrollment: %s", reason)
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
        """The sanctioned way back in when the only enrolled
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
        every attempt that gets as far as presenting a code is recorded,
        refused or not.

        Three guards run before the code is checked: the
        session must be one ``is_human_session`` attributes to a person
        where the caller supplies that check (``build_routes``' docstring);
        the attempt must fit ``recovery_attempts``' budget, per session and
        across all sessions; and a missing code is a 400 that spends
        nothing. Each refusal after CSRF/origin -- an unattested session, an
        exhausted budget, a wrong or spent code -- is audited as
        ``webauthn_recovery_refused`` with the reason in its summary, never
        the code itself; a successful trade-in stays
        ``webauthn_recovery_code_used``. Which check a wrong code failed
        (none on file, already spent, mismatch) is still not revealed, to
        the caller or in the log -- ``consume_recovery_code`` does not say."""
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
        if is_human_session is not None and not is_human_session(request):
            _audit(
                principal, "webauthn_recovery_refused",
                "Recovery code refused: the session was not opened by a person (unattested)",
            )
            body, status = human_session_required_json("use a recovery code")
            return JSONResponse(body, status_code=status)
        code = payload.get("code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or not code.strip():
            return JSONResponse({"error": "missing recovery code"}, status_code=400)
        retry_after = recovery_attempts.try_acquire(request.cookies.get(session_cookie_name, ""))
        if retry_after is not None:
            _audit(
                principal, "webauthn_recovery_refused",
                "Recovery code refused: too many recovery attempts, try again later",
            )
            return JSONResponse(
                {
                    "error": "too_many_attempts",
                    "message": f"Too many recovery attempts. Try again in {retry_after} seconds.",
                },
                status_code=429, headers={"Retry-After": str(retry_after)},
            )
        if not webauthn_stepup.consume_recovery_code(principal, code):
            _audit(
                principal, "webauthn_recovery_refused",
                "Recovery code refused: invalid or already-used recovery code",
            )
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
# Page rendering -- org mode's caller (nav_items given) wraps this in
# web_shell.wrap()'s persistent header/nav, same as routes_connect.py's own
# _render_connect_page; local mode's (nav_items=None) stays the small,
# self-contained document it always was -- see module docstring's own
# ``nav_items`` paragraph and build_routes' docstring on ``back_link``.
# --------------------------------------------------------------------- #

_STYLE = """
#pf-security-page{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:640px;margin:0 auto;
  padding:24px 20px 64px}
h1{font-size:20px;margin:0 0 4px}
p.lead{color:var(--muted, #555);margin-top:0}
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
h2{font-size:16px;margin:32px 0 4px}
ul.mints{list-style:none;padding:0;margin:8px 0 0}
li.mint{padding:8px 0;border-bottom:1px solid #eee;font-size:13px}
li.mint:last-child{border-bottom:none}
li.mint .when{color:#888;font-size:12px;display:block}
"""

_PAGE_JS = """
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('pf-add-passkey');
  var status = document.getElementById('pf-passkey-status');
  var csrf = document.getElementById('pf-security-page').getAttribute('data-csrf');
  if (!btn) { return; }
  if (!window.PublicKeyCredential) {
    btn.disabled = true;
    if (status) { status.textContent = 'This browser does not support passkeys.'; }
    return;
  }
  // The enrollment gate: enrolling is gated too, and the options call is where the gate
  // runs. With a credential already enrolled the server answers 428 with
  // assertion options -- prove possession of one you already have, then ask
  // again with the assertion attached, exactly the shape pfDeleteCredential
  // below already uses for removing your last one. With nothing enrolled the
  // gate is the companion's own dialog (local mode), which needs no round
  // trip here: the same call either returns options or a 403 whose `detail`
  // says what to do about it.
  function pfRegisterOptions(assertion) {
    var body = {csrf: csrf};
    if (assertion) { body.webauthn_assertion = assertion; }
    return fetch('/api/security/webauthn/register/options', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.status === 428) {
        return r.json().then(function (data) {
          if (!data.webauthn_options || !window.PublicKeyCredential) {
            throw new Error('adding a passkey needs a prompt from one you already have, and none is available');
          }
          if (status) { status.textContent = 'First, confirm with a passkey you already have...'; }
          return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function (newAssertion) {
            if (status) { status.textContent = 'Follow your browser\\'s prompt...'; }
            return pfRegisterOptions(newAssertion);
          });
        });
      }
      return r.json().then(function (data) {
        if (data.error) { throw new Error(data.detail || data.error); }
        return data.options;
      });
    });
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    if (status) { status.textContent = 'Follow your browser\\'s prompt...'; }
    pfRegisterOptions(null).then(function (options) {
      return pfWebauthnCreate(JSON.stringify(options));
    }).then(function (credential) {
      return fetch('/api/security/webauthn/register/verify', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: csrf, credential: credential, label: 'Passkey'})
      });
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) { throw new Error(data.error); }
      // A recovery code is issued the moment none is
      // currently unused -- shown exactly once, here, since the server
      // never returns it again after this response.
      if (data.recovery_code) {
        window.alert(
          'Save this recovery code somewhere safe -- it will not be shown again.\\n\\n' +
          data.recovery_code +
          '\\n\\nIf you ever lose every passkey enrolled here, this code is the only way back in.'
        );
      }
      // Plan item 1.3: on a packaged install the code is never in this
      // response at all -- the companion put it on the desktop. Only the
      // failure is worth a word here, since the success already happened
      // somewhere the human was looking.
      if (data.recovery_shown_by_companion === false) {
        window.alert(
          'Your passkey was added, but no recovery code could be issued:\\n\\n' +
          (data.recovery_detail || 'the companion app did not show it') +
          '\\n\\nAsk for one from the PrivacyFence companion once that is sorted out.'
        );
      }
      window.location.reload();
    }).catch(function (err) {
      btn.disabled = false;
      if (status) { status.textContent = 'Could not add a passkey: ' + err.message; }
    });
  });

  // The recovery-code path for a lost/unusable authenticator
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
          if (!result.ok) {
            throw new Error(result.data.message || result.data.error || 'recovery code was not accepted');
          }
          window.location.reload();
        }).catch(function (err) {
          recoverBtn.disabled = false;
          if (recoverStatus) { recoverStatus.textContent = 'Could not recover: ' + err.message; }
        });
    });
  }

  // Removing your *only* enrolled passkey demands a fresh
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


# How many of them the page shows. Long enough to cover "did anything mint a
# session while I was away from this machine this morning", short enough that
# the list stays something a human reads rather than scrolls -- the full trail
# is the audit log's own export, which this is a glance at, not a browser for
# (audit_log.recent_entries' own docstring draws the same line).
_RECENT_MINTS_SHOWN = 5

# How far back through recent_entries() to look for them. A busy install can
# put a lot of ordinary approvals between two mints, and a mint that scrolled
# off the end would be exactly the one worth seeing.
_RECENT_MINTS_SCANNED = 200


def _recent_mints() -> list[tuple[str, str]]:
    """``(when, what)`` for the most recent sign-in code mints and refusals,
    newest first -- this is what makes an unexpected mint *visible* rather
    than merely inferable.

    Every path to a session is audited (web/control_channel.py's
    ``_audit_mint``), but an audit entry nobody reads is evidence after the
    fact and not much else; this puts them on the one page a human already
    visits to reason about what can approve on this install.

    Returns an empty list rather than raising for any reason at all --
    including org mode, where nothing ever mints one of these and the
    section simply does not render. A page that fails to load because its
    least important section could not be built would be a poor trade.
    """
    try:
        from .control_channel import SIGN_IN_MINT_DECISION

        entries = get_audit_logger().recent_entries(_RECENT_MINTS_SCANNED)
    except Exception as exc:  # noqa: BLE001 -- see this function's own docstring
        logger.warning("Could not read recent sign-in mints for /security: %s", exc)
        return []
    rows: list[tuple[str, str]] = []
    for entry in entries:
        if entry.decision != SIGN_IN_MINT_DECISION:
            continue
        rows.append((entry.timestamp.replace("T", " ")[:19] + " UTC", entry.summary))
        if len(rows) == _RECENT_MINTS_SHOWN:
            break
    return rows


def _recent_mints_html(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    items = "".join(
        f'<li class="mint">{_esc(what)}<span class="when">{_esc(when)}</span></li>'
        for when, what in rows
    )
    return (
        "<h2>Recent sign-ins</h2>"
        '<p class="meta">Every sign-in link this install has issued, and every one it refused. '
        "A link that can approve is only ever issued through PrivacyFence's companion app -- if "
        "you see one here you did not ask for, treat this install as compromised.</p>"
        f'<ul class="mints">{items}</ul>'
    )


def _render_security_page(
    *, principal: Principal, creds: list, csrf: str, step_up: StepUpConfig, nonce: str,
    back_link: tuple[str, str], nav_items: tuple[tuple[str, str, str], ...] | None = None,
    dev_unseparated_notice: str | None = None,
) -> str:
    who = principal.email or principal.display_name or principal.id
    who_esc = _esc(who)
    rows = "".join(_credential_row_html(c) for c in creds)
    creds_html = f'<ul class="creds">{rows}</ul>' if creds else '<div class="empty">No passkeys added yet.</div>'
    scope_note = _SCOPE_NOTES.get(step_up.scope, _SCOPE_NOTES["writes"])
    # ADR 0003 decision 7: shown at the top of the page body, so it lands
    # inside whichever shell is used below -- web_shell.wrap()'s nav in org
    # mode, the bare document in local mode, which is the only one that ever
    # passes a non-None notice.
    dev_notice_html = (
        f'<p class="flash err">{_esc(dev_unseparated_notice)}</p>' if dev_unseparated_notice else ""
    )
    # Only rendered when there's no persistent nav to get back with (module
    # docstring's own ``nav_items`` paragraph) -- with one, this link would
    # just duplicate the nav's own "Connections"/"Settings" items.
    back_link_html = (
        "" if nav_items is not None else f'<p><a href="{_esc(back_link[0])}">{_esc(back_link[1])}</a></p>'
    )
    content = (
        f'<div id="pf-security-page" data-csrf="{_esc(csrf)}">'
        "<h1>Passkeys</h1>"
        f"{dev_notice_html}"
        f'<p class="lead">Signed in as {who_esc}. A passkey (Face ID, Touch ID, fingerprint, or Windows Hello) proves it\'s '
        f"really you before a write approval is released, even if someone else has your unlocked phone. {_esc(scope_note)}</p>"
        f"{creds_html}"
        '<p><button type="button" class="add" id="pf-add-passkey">Add a passkey</button>'
        '<span id="pf-passkey-status" class="meta"></span></p>'
        '<p>Lost every passkey enrolled here? <button type="button" class="remove" id="pf-use-recovery-code">Use your recovery code</button>'
        '<span id="pf-recovery-status" class="meta"></span></p>'
        f"{_recent_mints_html(_recent_mints())}"
        f"{back_link_html}"
        f'<script nonce="{nonce}">{_PAGE_JS}</script>'
        "</div>"
    )
    if nav_items is not None:
        return web_shell.wrap(
            f'<style nonce="{nonce}">{_STYLE}</style>{content}',
            title="PrivacyFence — Passkeys", active="passkeys", nonce=nonce,
            nav_items=nav_items, principal_label=who, live_updates=False, notifications_enabled=False,
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrivacyFence -- Security</title><style nonce="{nonce}">{_STYLE}</style></head>
<body>
{content}
</body></html>"""


__all__ = ["PF_WEBAUTHN_JS", "build_routes"]
