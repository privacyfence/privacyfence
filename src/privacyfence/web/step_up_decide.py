"""Shared decide-time WebAuthn step-up sequence: web/routes_approvals.py's
local-mode routes, its org-mode routes, web/routes_settings.py and the batch
decide endpoint (approvals.PendingApprovalRegistry.answer_batch) all need
"begin a challenge, store it, verify a resubmitted assertion against it",
and this module is the one copy of it.

Each caller keeps its own response shaping -- a plain 428, a 428 with an
IdP-reauth fallback (org mode), or an unconditional 403 fail-closed
(settings actions, which only ever call this once ``require_passkey`` is
already known to be on) -- only the two ceremony primitives are shared:
``begin_step_up`` (mint a challenge, bound to a caller-chosen
``subject_key``/``fingerprint`` pair) and ``verify_step_up`` (consume that
challenge against a resubmitted assertion), which wrap webauthn_stepup.py's
``begin_assertion``/``verify_assertion``. Every caller keeps its own
``StepUpChallengeStore`` instance.
"""
from __future__ import annotations

from ..principal import Principal
from ..webauthn_stepup import StepUpChallengeStore, begin_assertion, verify_assertion


class StepUpExpired(Exception):
    """Raised by ``verify_step_up`` when no live challenge matches
    ``subject_key``/``fingerprint`` -- never began, already consumed by an
    earlier attempt, past ``StepUpChallengeStore``'s own TTL, or minted for
    a different decision (a different fingerprint) than the one now being
    confirmed. Callers turn this into a ``400 step_up_expired``, distinct
    from a ``WebAuthnError`` (``401``) -- "your last attempt was too slow,
    or for something else" reads differently from "that assertion didn't
    verify"."""


def begin_step_up(
    principal: Principal, *, rp_id: str, subject_key: str, fingerprint: str, challenges: StepUpChallengeStore,
) -> str | None:
    """Starts a fresh assertion ceremony for one decision (or settings
    action, or batch) and stores its challenge, keyed by ``subject_key``
    (an approval id, a settings action name, or a batch id -- whatever the
    caller's own ``challenges`` store keys its decisions by) bound to
    ``fingerprint`` (webauthn_stepup.decision_fingerprint()'s single-item
    form, or a caller's own equivalent computed over a larger payload).

    Returns the options JSON to embed in a ``428`` body, or ``None`` when
    this principal has no enrolled credential at all -- the caller decides
    what "no credential" means for it (local mode's own evadable
    fall-through, org mode's IdP link, or settings' unconditional
    ``403``)."""
    begun = begin_assertion(principal, rp_id=rp_id) if rp_id else None
    if begun is None:
        return None
    options_json, challenge = begun
    challenges.put(principal.id, subject_key, challenge=challenge, fingerprint=fingerprint)
    return options_json


def verify_step_up(
    principal: Principal, *, rp_id: str, origin: str, subject_key: str, fingerprint: str,
    assertion: dict, challenges: StepUpChallengeStore,
) -> None:
    """Completes a ceremony ``begin_step_up`` started for the same
    ``subject_key``. Raises ``StepUpExpired`` when no live challenge
    matches ``subject_key``/``fingerprint``, or ``WebAuthnError`` (from
    ``webauthn_stepup.verify_assertion``) on a signature/user-verification
    failure. Returns normally on success -- the caller applies whatever
    decision it was gating."""
    pending = challenges.pop(principal.id, subject_key)
    if pending is None or pending.fingerprint != fingerprint:
        raise StepUpExpired()
    verify_assertion(principal, assertion, expected_challenge=pending.challenge, rp_id=rp_id, origin=origin)


__all__ = ["StepUpExpired", "begin_step_up", "verify_step_up"]
