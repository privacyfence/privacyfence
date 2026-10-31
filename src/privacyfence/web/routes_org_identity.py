"""Browser login for org mode -- PrivacyFence acting as its own OIDC relying party against the
org's IdP, for a human visiting the web approval/settings surface
directly. This is a *separate* IdP-facing redirect_uri from web/
oauth_provider.py's ``OrgOAuthProvider`` (which does the same IdP dance,
against the same IdP registration, but on Claude's behalf as part of
PrivacyFence's own OAuth 2.1 authorization-server role) -- both go through
org_identity.py's identical claims-to-Principal mapping (see that module's
own docstring on why that sharing matters), but they're two distinct flows
with two distinct redirect URIs an org admin registers once with the IdP:

- ``/oauth/idp/login-callback`` -- this module, a browser's own sign-in.
- ``/oauth/idp/callback`` -- web/oauth_provider.py, on behalf of a pending
  Claude/MCP-client authorization.

With PrivacyFence's built-in Google sign-in both go through the PrivacyFence relay instead: the IdP
redirects to the relay's fixed URI, and the relay forwards the browser to the same path here,
reading the host from the ``state``. Every server using that client shares it, so an attempt only
finishes with its own single-use state, its own PKCE verifier and its own ID-token nonce.

Not wired into routes_approvals.py's or routes_settings.py's own auth
checks in this phase -- see org_session.py's module docstring for what
that means and doesn't mean.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import grist_provisioning, org_identity, peek_provisioning, relay_client, web_shell
from ..org_identity import IdpConfig
from ..org_mode import AuthzPolicyConfig
from ..web_push import PushSubscriptionStore, session_tag
from . import org_session
from .csp import nonce_for

logger = logging.getLogger(__name__)

LOGIN_CALLBACK_PATH = "/oauth/idp/login-callback"
# Set by /login on its redirect to the IdP, checked by the callback: the sign-in finishes only in
# the browser that started it (ADR 0178). One slot, so of two /login tabs open at once only the
# later one completes.
LOGIN_BINDING_COOKIE = "pf_org_login"
# Where a sign-out lands, and where a form post with no session left ends (web/routes_connect.py).
# A page on this origin, not /login: /login redirects on to the IdP, and a browser checks the
# page's form-action on every redirect a form submission follows, so that bounce would need the
# IdP's origin in form-action (ADR 0082). It also means signing out stays signed out, rather than
# the IdP's own session signing the person straight back in.
SIGNED_OUT_PATH = "/signed-out"
DEFAULT_NEXT_PATH = "/approvals"

# Shown by /login instead of a redirect to the IdP while a relayed sign-in could not finish: the
# relay forwards a sign-in only for a registered server, and with the built-in Google sign-in only
# for accounts of a domain proven in DNS.
STARTING_UP_TEXT = (
    "Sign-in is starting up: this server is registering with the PrivacyFence relay. Try again in a minute."
)
WAITING_FOR_DNS_TEXT = (
    "Sign-in is waiting for DNS: the administrator must publish the record shown in the server log."
)

# A login attempt outlives one browser round trip to the IdP and back --
# generous but bounded, so an abandoned /login tab doesn't leak the
# in-memory store forever.
_LOGIN_ATTEMPT_TTL_SECONDS = 5 * 60


@dataclass
class _LoginAttempt:
    nonce: str
    code_verifier: str
    next_path: str
    created_at: float
    # The redirect_uri sent with the authorization request; the code exchange must send the same.
    redirect_uri: str


class _LoginAttemptStore:
    """In-memory, short-lived -- same "a daemon restart invalidates it"
    posture as OrgSessionStore (see that module's own docstring): losing an
    in-flight login attempt on restart just means starting over at
    ``/login``, not a security gap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, _LoginAttempt] = {}

    def create(self, *, next_path: str, redirect_uri: str, state: str = "") -> tuple[str, _LoginAttempt, str]:
        """Returns ``(state, attempt, code_challenge)`` -- ``state`` is the
        dict key (and the OAuth ``state`` param sent to the IdP), a fresh random one unless the
        caller passes its own (the relay's ``host~nonce`` form);
        ``code_challenge`` is derived from the attempt's own
        ``code_verifier`` and handed back so the caller can build the
        authorization URL without generating PKCE material twice."""
        self._prune()
        state = state or secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        attempt = _LoginAttempt(
            nonce=nonce, code_verifier=code_verifier, next_path=next_path, created_at=time.time(),
            redirect_uri=redirect_uri,
        )
        with self._lock:
            self._attempts[state] = attempt
        return state, attempt, code_challenge

    def pop(self, state: str) -> _LoginAttempt | None:
        """Single-use: a state value is consumed the moment it's looked up,
        successfully or not, so the same IdP redirect can never be replayed
        to mint a second session. An attempt past its TTL reads as absent."""
        with self._lock:
            attempt = self._attempts.pop(state, None)
        if attempt is None or (time.time() - attempt.created_at) > _LOGIN_ATTEMPT_TTL_SECONDS:
            return None
        return attempt

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, a in self._attempts.items() if (now - a.created_at) > _LOGIN_ATTEMPT_TTL_SECONDS]
            for s in stale:
                del self._attempts[s]


def _binding_value(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _safe_next_path(raw: str | None, *, default: str = DEFAULT_NEXT_PATH) -> str:
    """Open-redirect defense: ``next`` must be a same-origin relative path
    (a leading ``/`` that isn't a scheme-relative ``//host/...``), or
    ``default`` is used instead. Never trusts ``raw`` far enough to
    redirect to it verbatim.

    Checked on a backslash-normalized copy, not the raw string: browsers
    treat a leading backslash the same as a forward slash when resolving a
    redirect target (a well-known open-redirect bypass), so ``"/\\evil.
    example.com"`` -- which reads as an ordinary same-origin path to
    Python's own string/URL handling -- must still be rejected, because a
    real browser turns it into ``//evil.example.com``, a protocol-relative
    redirect off-site, before it ever gets there.
    """
    if not raw or not raw.startswith("/"):
        return default
    if raw.replace("\\", "/").startswith("//"):
        return default
    return raw


def build_routes(
    *, idp: IdpConfig, sessions: org_session.OrgSessionStore, base_url: str,
    default_next_path: str = DEFAULT_NEXT_PATH, policy: AuthzPolicyConfig | None = None,
    push_store: PushSubscriptionStore | None = None,
) -> list[Route]:
    """``base_url`` is this daemon's own externally-reachable origin (org
    mode's configured issuer/server URL) -- the redirect_uri PrivacyFence
    presents to the IdP has to be this fixed, pre-registered value, never
    derived from a request's own (spoofable) Host header.

    ``default_next_path``
    is where a sign-in with no explicit ``?next=`` lands. ``web/server.py``'s
    ``_build_org_app`` passes ``"/connect"`` (the per-principal connections
    page) here whenever it mounts that page; every other caller keeps
    ``DEFAULT_NEXT_PATH`` ("/approvals").

    ``policy`` (the org's sign-in policy: allowed domains, required groups)
    is checked against every claims/principal a login attempt resolves, on
    top of the IdP's own authentication -- absent (the default, an
    unconfigured/disabled ``AuthzPolicyConfig``), every IdP-authenticated
    principal is admitted.

    ``push_store`` is org mode's web push subscriptions (ADR 0081), ``None`` when push is off. A
    session that ends here, by ``/logout`` or by a new sign-in replacing it in the same browser,
    takes the push subscriptions it posted with it, so a signed-out browser stops showing the
    previous person's notices. This is server-side, so it holds when the page's own
    ``unsubscribe()`` never ran. A session that only expires keeps them: a phone's session idles
    out after 30 minutes, and reaching that phone anyway is what push is for.
    """
    policy = policy or AuthzPolicyConfig()

    def end_browser_push(old_session_id: str, *, new_session_id: str = "", principal_id: str = "") -> None:
        if push_store is None or not old_session_id:
            return
        try:
            if new_session_id:
                push_store.replace_session(session_tag(old_session_id), session_tag(new_session_id), principal_id)
            else:
                push_store.remove_session(session_tag(old_session_id))
        except (OSError, ValueError) as exc:
            # The sign-in or sign-out still completes; the subscription then lives on until it
            # expires or another person subscribes this browser.
            logger.warning("Could not update this browser's push subscriptions: %s", type(exc).__name__)

    attempts = _LoginAttemptStore()
    redirect_uri = org_identity.signin_redirect_uri(
        idp, issuer_url=base_url, grant="idp-login", callback_path=LOGIN_CALLBACK_PATH,
    )

    def relay_not_ready(request: Request) -> Response | None:
        """The page /login shows instead of sending the person to the IdP, while the relay would
        refuse the sign-in; ``None`` once it can go through."""
        if not idp.relay:
            return None
        status = relay_client.status()
        if status["last_ok_at"] is None:
            text = STARTING_UP_TEXT
        elif idp.builtin and not status["signin_domains_verified"]:
            text = WAITING_FOR_DNS_TEXT
        else:
            return None
        body = f"<h1>Sign-in is not available yet</h1><p>{text}</p>"
        return HTMLResponse(
            web_shell.plain_page(body, title="PrivacyFence — Sign-in", nonce=nonce_for(request)),
            status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "60"},
        )

    async def login(request: Request) -> Response:
        not_ready = relay_not_ready(request)
        if not_ready is not None:
            return not_ready
        next_path = _safe_next_path(request.query_params.get("next"), default=default_next_path)
        state, attempt, code_challenge = attempts.create(
            next_path=next_path, state=org_identity.new_signin_state(idp, issuer_url=base_url),
            redirect_uri=redirect_uri,
        )
        url = org_identity.build_authorization_url(
            idp, redirect_uri=attempt.redirect_uri, state=state, code_challenge=code_challenge,
            nonce=attempt.nonce,
        )
        response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            LOGIN_BINDING_COOKIE, _binding_value(state), httponly=True, secure=True, samesite="lax",
            path=LOGIN_CALLBACK_PATH, max_age=_LOGIN_ATTEMPT_TTL_SECONDS,
        )
        return response

    async def login_callback(request: Request) -> Response:
        idp_error = request.query_params.get("error")
        if idp_error:
            logger.info("Org sign-in declined by IdP: %s", idp_error)
            return PlainTextResponse("Sign-in was not completed.", status_code=400)
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        attempt = attempts.pop(state) if state else None
        # The state is burnt above either way; a callback without this browser's binding cookie
        # (a link handed to someone else) gets the same answer as an unknown state.
        bound = hmac.compare_digest(request.cookies.get(LOGIN_BINDING_COOKIE, ""), _binding_value(state))
        if attempt is None or not code or not bound:
            return PlainTextResponse("Sign-in failed: invalid or expired login attempt.", status_code=400)
        try:
            tokens = await asyncio.to_thread(
                org_identity.exchange_code_for_tokens,
                idp, code=code, redirect_uri=attempt.redirect_uri, code_verifier=attempt.code_verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token:
                raise ValueError("IdP token response carried no id_token")
            claims = await asyncio.to_thread(org_identity.verify_id_token, idp, id_token, nonce=attempt.nonce)
            principal = org_identity.principal_from_claims(claims, idp)
            org_identity.check_authz_policy(principal, claims, policy)
        except Exception as exc:  # noqa: BLE001 -- an IdP failure or sign-in policy denial: sign-in didn't complete
            logger.warning("Org sign-in failed: %s", exc)
            return PlainTextResponse("Sign-in failed. Please try again.", status_code=400)
        session_id = sessions.create(
            principal, email_verified=org_identity.session_email_verified(claims, principal, policy),
        )
        peek_provisioning.notify_sign_in(principal, email_verified=peek_provisioning.email_verified_claim(claims))
        await asyncio.to_thread(
            grist_provisioning.notify_sign_in, principal,
            email_verified=grist_provisioning.email_verified_claim(claims),
        )
        # The session this browser held before, if any (typically one that expired without a
        # sign-out): a different person signing in here does not inherit its push
        # subscriptions, and the same person's move to the new session.
        end_browser_push(
            request.cookies.get(org_session.SESSION_COOKIE, ""),
            new_session_id=session_id, principal_id=principal.id,
        )
        response = RedirectResponse(attempt.next_path, status_code=302, headers={"Cache-Control": "no-store"})
        org_session.set_session_cookie(response, session_id)  # first Set-Cookie header: the smoke test reads it
        response.delete_cookie(LOGIN_BINDING_COOKIE, path=LOGIN_CALLBACK_PATH)
        return response

    async def logout(request: Request) -> Response:
        # POST-only, no CSRF token required: the worst a forged cross-site
        # POST here can do is log the victim out (which also stops that
        # browser's push notices), not disclose or change anything -- a
        # widely-accepted trade-off for a logout endpoint specifically
        # (unlike every state-changing route routes_approvals.py/
        # routes_settings.py protect with a real CSRF check).
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
        Route("/login", login),
        Route(LOGIN_CALLBACK_PATH, login_callback),
        Route("/logout", logout, methods=["POST"]),
        Route(SIGNED_OUT_PATH, signed_out),
    ]


__all__ = ["DEFAULT_NEXT_PATH", "LOGIN_CALLBACK_PATH", "SIGNED_OUT_PATH", "build_routes"]
