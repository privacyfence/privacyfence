"""Org mode's IdP re-authentication fallback for decide-time step-up (§10.6,
D7) -- the one piece of web/routes_org_approvals.py's former surface with no
local-mode analogue at all (local mode has no IdP to re-authenticate
against), so PSC-2b's merge of the rest of that module into
web/routes_approvals.py left this behind as its own small module rather than
forcing it behind a mode adapter with nothing on the other side.

``GET /api/approvals/{id}/stepup/idp`` -> ``GET /oauth/stepup/callback``
mirrors web/routes_org_identity.py's own ``/login`` flow almost exactly
(same org_identity.py functions, same single-use ``state``-keyed pending-
attempt store) with one addition: the callback must re-derive the *same*
principal the step-up was started for, not merely *a* signed-in principal --
otherwise a second IdP account signing in through a leaked step-up link
could authorize someone else's pending decision. See
``_StepUpAuthAttemptStore``/``stepup_callback`` below.

``step_up.require_passkey`` (#406) closes this fallback entirely for orgs
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
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import org_identity
from ..org_identity import IdpConfig
from ..step_up_config import StepUpConfig
from ..web_approval_ui import WebApprovalUI
from . import org_session
from .org_session import OrgSessionStore
from .routes_approvals import _STEP_UP_RESULTS

logger = logging.getLogger(__name__)

_STEP_UP_ATTEMPT_TTL_SECONDS = 10 * 60


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


def build_routes(
    *, web_ui: WebApprovalUI, sessions: OrgSessionStore, step_up: StepUpConfig, idp: IdpConfig, issuer_url: str,
) -> list[Route]:
    stepup_attempts = _StepUpAuthAttemptStore()
    origin = issuer_url.rstrip("/")

    async def stepup_idp_start(request: Request) -> Response:
        """A same-site navigation the user's own click on the failed card's
        "Verify by signing in again" link makes -- the session cookie *is*
        present here (unlike the eventual callback, see module docstring)."""
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return RedirectResponse(
                f"/login?next=/approvals/{quote(request.path_params['id'])}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        if step_up.require_passkey:
            # Not merely unadvertised (_org_step_up_response omits
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
        # approval URL itself is not a secret, per §10.4) could be completed
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
        Route("/api/approvals/{id}/stepup/idp", stepup_idp_start),
        Route("/oauth/stepup/callback", stepup_callback),
    ]


__all__ = ["build_routes"]
