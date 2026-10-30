"""Org mode's IdP re-authentication fallback for decide-time step-up -- the
one piece of web/routes_org_approvals.py's former surface with no
local-mode analogue at all (local mode has no IdP to re-authenticate
against), so merging the rest of that module into
web/routes_approvals.py (ADR 0033) left this behind as its own small module rather than
forcing it behind a mode adapter with nothing on the other side. Why org
mode falls back to the IdP at all, and local mode does not, is ADR 0066.

``POST /api/approvals/{id}/stepup/idp`` -> ``GET /oauth/stepup/callback``
mirrors web/routes_org_identity.py's own ``/login`` flow almost exactly
(same org_identity.py functions, same single-use ``state``-keyed pending-
attempt store) with one addition: the callback must re-derive the *same*
principal the step-up was started for, not merely *a* signed-in principal --
otherwise a second IdP account signing in through a leaked step-up link
could authorize someone else's pending decision. See
``_StepUpAuthAttemptStore``/``stepup_callback`` below.

The start is a CSRF-checked JSON ``POST`` the card's own button makes
(``{"result", "choice", "csrf"}``, answered with ``{"redirect": <IdP url>}``),
never a link: a ``GET`` an agent-supplied URL could trigger would let a page
the user merely visited start a step-up that releases an approval. A pending
attempt older than ``_STEP_UP_ATTEMPT_TTL_SECONDS`` is refused at the callback.

``step_up.require_passkey`` closes this fallback entirely for orgs
that want hardware-bound WebAuthn as a hard requirement: with it set,
web/routes_approvals.py's own ``_org_step_up_response`` never advertises
``idp_stepup_url``, and ``stepup_idp_start`` below refuses outright (not
just "unadvertised" -- a client hitting it directly gets a ``403`` too).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import org_identity
from ..org_identity import IdpConfig
from ..org_mode import AuthzPolicyConfig
from ..step_up_config import StepUpConfig
from ..web_approval_ui import WebApprovalUI
from . import org_session
from .org_session import OrgSessionStore
from .routes_approvals import _STEP_UP_RESULTS

logger = logging.getLogger(__name__)

_STEP_UP_ATTEMPT_TTL_SECONDS = 10 * 60
STEPUP_CALLBACK_PATH = "/oauth/stepup/callback"


@dataclass
class _StepUpAuthAttempt:
    principal_id: str
    approval_id: str
    result: str
    choice: int | None
    nonce: str
    code_verifier: str
    # The redirect_uri sent with the authorization request; the code exchange must send the same.
    redirect_uri: str
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
            attempt = self._pending.pop(state, None)
        if attempt is None or (time.time() - attempt.created_at) > self._ttl:
            return None
        return attempt

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, a in self._pending.items() if (now - a.created_at) > self._ttl]
            for s in stale:
                del self._pending[s]


def build_routes(
    *, web_ui: WebApprovalUI, sessions: OrgSessionStore, step_up: StepUpConfig, idp: IdpConfig, issuer_url: str,
    policy: AuthzPolicyConfig | None = None,
) -> list[Route]:
    """``policy`` is the sign-in policy (allowed domains, required groups): a step-up is a fresh
    sign-in, so it applies the same check before it accepts the re-authentication."""
    policy = policy or AuthzPolicyConfig()
    stepup_attempts = _StepUpAuthAttemptStore()
    # With the built-in Google sign-in the IdP redirects to the relay's "stepup" URI, and the relay
    # forwards the browser to this server's callback below.
    redirect_uri = org_identity.signin_redirect_uri(
        idp, issuer_url=issuer_url, grant="stepup", callback_path=STEPUP_CALLBACK_PATH,
    )

    async def stepup_idp_start(request: Request) -> Response:
        """The card's "Verify by signing in again" button posts here (JSON
        ``{"result", "choice", "csrf"}``) and navigates to the ``redirect`` it
        gets back -- the session cookie *is* present here (unlike the eventual
        callback, see module docstring)."""
        no_store = {"Cache-Control": "no-store"}
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return JSONResponse({"error": "signed_out"}, status_code=401, headers=no_store)
        if not step_up.enabled:
            return PlainTextResponse("Not found.", status_code=404, headers=no_store)
        if not org_session.check_origin(request):
            return PlainTextResponse("Cross-origin request refused.", status_code=403, headers=no_store)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return PlainTextResponse("Invalid step-up request.", status_code=400, headers=no_store)
        if not org_session.check_csrf(request, body.get("csrf")):
            return PlainTextResponse("Invalid CSRF token.", status_code=403, headers=no_store)
        if step_up.require_passkey:
            # Not merely unadvertised (_org_step_up_response omits
            # idp_stepup_url) -- the endpoint itself refuses, so a client
            # hitting it directly can't use it as a bypass. See module
            # docstring's note on ``step_up.require_passkey``.
            return PlainTextResponse(
                "This organization requires a passkey for step-up verification; "
                "IdP re-authentication cannot be used instead.", status_code=403, headers=no_store,
            )
        approval_id = request.path_params["id"]
        result = body.get("result", "")
        if result not in _STEP_UP_RESULTS:
            return PlainTextResponse("Invalid step-up request.", status_code=400, headers=no_store)
        raw = body.get("choice")
        choice = raw if isinstance(raw, int) and not isinstance(raw, bool) else None

        state = org_identity.new_signin_state(idp, issuer_url=issuer_url)
        nonce = secrets.token_urlsafe(16)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        stepup_attempts.put(state, _StepUpAuthAttempt(
            principal_id=principal.id, approval_id=approval_id, result=result, choice=choice,
            nonce=nonce, code_verifier=code_verifier, redirect_uri=redirect_uri,
        ))
        extra_params = {"prompt": "login", "max_age": "0"}
        if idp.step_up_acr_values:
            extra_params["acr_values"] = " ".join(idp.step_up_acr_values)
        url = org_identity.build_authorization_url(
            idp, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge, nonce=nonce,
            extra_params=extra_params,
        )
        return JSONResponse({"redirect": url}, headers=no_store)

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
        try:
            tokens = await asyncio.to_thread(
                org_identity.exchange_code_for_tokens,
                idp, code=code, redirect_uri=attempt.redirect_uri, code_verifier=attempt.code_verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token:
                raise ValueError("IdP token response carried no id_token")
            claims = await asyncio.to_thread(org_identity.verify_id_token, idp, id_token, nonce=attempt.nonce)
            reauthed = org_identity.principal_from_claims(claims, idp)
            # The same check as sign-in: a person whose domain or group was removed since they
            # signed in cannot approve with a fresh IdP sign-in either.
            org_identity.check_authz_policy(reauthed, claims, policy)
        except Exception as exc:  # noqa: BLE001 -- any IdP-side failure ends the same way
            logger.warning("Step-up re-authentication failed: %s", exc)
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        # The human who just re-authenticated must be the *same* one this
        # step-up was started for -- otherwise a leaked step-up link (the
        # approval URL itself is not a secret) could be completed
        # by signing in as someone else entirely. See module docstring's own
        # note on why this check exists.
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

    return [
        Route("/api/approvals/{id}/stepup/idp", stepup_idp_start, methods=["POST"]),
        Route(STEPUP_CALLBACK_PATH, stepup_callback),
    ]


__all__ = ["STEPUP_CALLBACK_PATH", "build_routes"]
