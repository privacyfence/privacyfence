"""One step-up orchestration for both approval modes (policy surface
consolidation, PSC-2a): web/routes_approvals.py's local-mode and org-mode
routes each grew their own copy of the sequence around an approval decision --
"does a sensitive confirm or an ordinary approving decision need a fresh
WebAuthn assertion, and if so, challenge or verify one" -- built on top of the
ceremony primitives step_up_decide.py already shares (see that module's own
docstring for why it exists). This module is that sequence, factored out
once, with no route move and no file deletion: both callers still resolve
their own ``Principal`` (ambient in local mode, from
``org_session.authenticated()`` in org mode -- ADR 0008) and still build
their own "step-up required" response (local: passkey-only ``428``/``403``;
org: a ``428`` that can also carry an IdP-reauth link, closed by
``require_passkey``) -- both passed in rather than duplicated here.

``batch_step_up_response`` has no such per-mode difference at all: comparing
the two modules' former ``_batch_step_up_response``, the only difference was
where ``principal`` came from (a parameter here, same as everywhere else in
this module), so that one is a plain shared function, never a callback.
"""
from __future__ import annotations

import json
from typing import Callable

from starlette.responses import JSONResponse

from .. import webauthn_stepup
from ..approvals import CONFIRM_RESULTS, PendingApproval, PendingApprovalRegistry
from ..principal import Principal
from ..step_up_config import StepUpConfig
from ..webauthn_stepup import StepUpChallengeStore, WebAuthnError
from . import step_up_decide

# Called with no arguments to mint a fresh challenge (or a hard "nothing
# enrolled" refusal) for the decision/batch the caller already closed this
# over -- local mode's own passkey-only shape, or org mode's passkey-or-IdP
# one. ``None`` means "nothing enrolled, and no requirement to fall back
# to" -- the caller's evadable fall-through, see module docstring.
StepUpResponder = Callable[[], "JSONResponse | None"]


def _verify_or_challenge(
    principal: Principal,
    *,
    step_up: StepUpConfig,
    origin: str,
    subject_key: str,
    fingerprint: str,
    assertion: object,
    challenges: StepUpChallengeStore,
    step_up_response: StepUpResponder,
) -> JSONResponse | None:
    """The one primitive every step-up check here reduces to, once a caller
    has already decided a check applies and bound ``fingerprint`` to
    ``subject_key``: no resubmitted assertion yet -- ask ``step_up_response``
    for a fresh challenge (or a hard refusal); a resubmitted one -- verify it
    against the live challenge ``step_up_decide.verify_step_up`` looks up
    under that same ``subject_key``, turning its two failure modes into the
    JSON error shapes every caller here already agreed on (``400
    step_up_expired``, ``401`` carrying ``WebAuthnError``'s own message).
    Returns ``None`` on a successful verification -- the caller resolves the
    decision itself."""
    if not isinstance(assertion, dict):
        return step_up_response()
    try:
        step_up_decide.verify_step_up(
            principal, rp_id=step_up.rp_id, origin=origin, subject_key=subject_key,
            fingerprint=fingerprint, assertion=assertion, challenges=challenges,
        )
    except step_up_decide.StepUpExpired:
        return JSONResponse({"error": "step_up_expired"}, status_code=400)
    except WebAuthnError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    return None


def guard_decision(
    principal: Principal,
    step_up: StepUpConfig | None,
    *,
    approval: PendingApproval | None,
    approval_id: str,
    result: str,
    choice: int | None,
    step_up_results: tuple[str, ...],
    assertion: object,
    origin: str,
    challenges: StepUpChallengeStore,
    step_up_response: StepUpResponder,
) -> JSONResponse | None:
    """The one function both modules' ``decide()`` calls in place of their
    former independent ``sensitive_confirm``/ordinary-step-up blocks.
    Covers a single decision's two independent step-up triggers, in the
    same order both former implementations checked them:

    - a **sensitive confirm**: ``approval.sensitive`` and ``result`` is a
      rule-creating confirm (``CONFIRM_RESULTS[0]``) -- gated on ``step_up.require_passkey``
      directly, never on ``scope``, since a rule change is not a read or a
      write, it is what decides which of those get asked about at all (both
      modules' own module docstrings);
    - an **ordinary approving decision** (``result`` in ``step_up_results``)
      that ``webauthn_stepup.is_step_up_required`` says this install's/org's
      ``scope`` covers.

    Returns ``None`` when neither trigger applies, when step-up is off
    entirely (``step_up is None`` or ``step_up.enabled`` is ``False``), when
    there is no pending approval to check at all, or when a resubmitted
    assertion verifies -- every one of those is the caller's cue to resolve
    the decision itself, exactly as before. Otherwise returns the response
    to send as-is: a fresh challenge, a hard "nothing enrolled" refusal, or
    a verification failure."""
    if step_up is None or not step_up.enabled or approval is None:
        return None

    if approval.sensitive and result == CONFIRM_RESULTS[0] and step_up.require_passkey:
        fingerprint = webauthn_stepup.decision_fingerprint(
            approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
        )
        response = _verify_or_challenge(
            principal, step_up=step_up, origin=origin, subject_key=approval_id, fingerprint=fingerprint,
            assertion=assertion, challenges=challenges, step_up_response=step_up_response,
        )
        if response is not None:
            return response

    if result in step_up_results and webauthn_stepup.is_step_up_required(
        gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
    ):
        fingerprint = webauthn_stepup.decision_fingerprint(
            approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
        )
        return _verify_or_challenge(
            principal, step_up=step_up, origin=origin, subject_key=approval_id, fingerprint=fingerprint,
            assertion=assertion, challenges=challenges, step_up_response=step_up_response,
        )
    return None


def batch_needs_step_up(
    parsed: list[tuple[str, str]],
    *,
    principal_id: str,
    registry: PendingApprovalRegistry,
    step_up: StepUpConfig,
    batch_step_up_results: tuple[str, ...],
) -> bool:
    """True iff at least one *known, batchable, approving* item in ``parsed``
    actually needs step-up -- an unknown id or a non-batchable item
    contributes nothing here, since ``registry.answer_batch`` will never
    apply either one regardless of step-up."""
    for approval_id, result in parsed:
        if result not in batch_step_up_results:
            continue
        approval = registry.get(approval_id, principal_id=principal_id)
        if (
            approval is not None and approval.is_batchable()
            and webauthn_stepup.is_step_up_required(
                gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
            )
        ):
            return True
    return False


def batch_step_up_response(
    principal: Principal,
    step_up: StepUpConfig,
    *,
    batch_id: str,
    fingerprint: str,
    challenges: StepUpChallengeStore,
) -> JSONResponse | None:
    """The batch counterpart of a mode's own single-decision step-up
    response -- same "no enrolled credential and ``require_passkey`` off"
    evadable fall-through (``None``), same ``403`` naming ``/security`` when
    ``require_passkey`` is on instead. Deliberately no IdP-reauth fallback
    in either mode: a batch decision is a page-level ceremony (like web/routes_settings.py's own sensitive
    actions), not a per-card one, and a page-level step-up never offered an
    IdP link either -- unlike a mode's own single-decision response, this
    has no ``require_passkey``-off branch to fall back to an IdP link
    from, so this one function is genuinely identical between modes, not
    just parameterised the same way."""
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


def guard_batch_decision(
    principal: Principal,
    step_up: StepUpConfig | None,
    *,
    parsed: list[tuple[str, str]],
    registry: PendingApprovalRegistry,
    batch_id: str,
    batch_step_up_results: tuple[str, ...],
    per_item_message: str,
    assertion: object,
    origin: str,
    challenges: StepUpChallengeStore,
) -> tuple[JSONResponse | None, bool]:
    """The one function both modules' ``batch_decide()`` calls in place of
    their former independent ``_batch_needs_step_up``/step-up blocks.

    Returns ``(response, batch_id_verified)``: ``response`` is ``None`` when
    the batch may proceed (nothing needed step-up, or a resubmitted
    assertion verified) and the response to send otherwise (a per-item-mode
    refusal, a fresh challenge, a hard refusal, or a verification failure).
    ``batch_id_verified`` is ``True`` only once a resubmitted assertion has
    actually verified against a live challenge this server minted for
    ``batch_id`` -- the one case a client-supplied ``batch_id`` is provably
    genuine rather than an arbitrary string, and the caller's cue to keep it
    rather than mint a fresh one for the audit trail (``registry.
    answer_batch``'s own ``batch_id``)."""
    if step_up is None or not step_up.enabled or not batch_needs_step_up(
        parsed, principal_id=principal.id, registry=registry, step_up=step_up,
        batch_step_up_results=batch_step_up_results,
    ):
        return None, False

    if step_up.batch == "per_item":
        return JSONResponse(
            {"error": "batch_step_up_per_item", "message": per_item_message}, status_code=400,
        ), False

    fingerprint = webauthn_stepup.batch_decision_fingerprint(principal_id=principal.id, items=parsed)
    response = _verify_or_challenge(
        principal, step_up=step_up, origin=origin, subject_key=f"batch:{batch_id}", fingerprint=fingerprint,
        assertion=assertion, challenges=challenges,
        step_up_response=lambda: batch_step_up_response(
            principal, step_up, batch_id=batch_id, fingerprint=fingerprint, challenges=challenges,
        ),
    )
    if response is not None:
        return response, False
    # A ``None`` response means either a verified assertion or the evadable
    # fall-through (no assertion supplied at all, since ``step_up_response``
    # above only ever returns ``None`` for that case) -- only the former
    # actually proved this ``batch_id`` genuine.
    return None, isinstance(assertion, dict)


__all__ = [
    "StepUpResponder",
    "batch_needs_step_up",
    "batch_step_up_response",
    "guard_batch_decision",
    "guard_decision",
]
