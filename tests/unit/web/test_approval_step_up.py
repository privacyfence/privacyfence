"""Tests for web/approval_step_up.py (PSC-2a): the one step-up orchestration
web/routes_approvals.py's local-mode and org-mode routes both call now,
exercised directly against the module rather than through either mode's
HTTP surface -- test_routes_approvals.py's and test_routes_org_approvals.py's
own step-up tests already cover this end to end for each mode and are
unchanged by this module's existence (see their own docstrings).

Real ``webauthn_stepup``/``step_up_decide`` primitives throughout (no
mocking of this module's own collaborators) -- enrolling a credential
(``wa.add_credential``) and patching ``wa.webauthn.verify_authentication_
response`` is exactly what the two route-level test files already do for
the same ceremony, so these tests stay consistent with that pattern rather
than inventing a second way to fake a passkey.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.responses import JSONResponse

from privacyfence import paths
from privacyfence import webauthn_stepup as wa
from privacyfence.approvals import PendingApproval
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import approval_step_up, step_up_decide
from privacyfence.webauthn_stepup import StepUpChallengeStore

ORIGIN = "http://localhost"
PRINCIPAL = Principal(id="p1")
STEP_UP_RESULTS = ("accept", "accept_all")
BATCH_STEP_UP_RESULTS = ("accept",)


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    # has_credentials()/begin_assertion()/verify_assertion() all resolve a
    # per-principal credential file under paths.data_dir() -- same fixture
    # test_routes_approvals.py's own step-up tests use.
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _enroll(principal: Principal = PRINCIPAL) -> None:
    wa.add_credential(principal, wa.WebAuthnCredential(
        credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
    ))


def _verified_assertion():
    return type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()


class _FakeRegistry:
    """Just enough of PendingApprovalRegistry's own ``get`` for
    batch_needs_step_up/guard_batch_decision -- neither one calls anything
    else on it, and approvals.py's own registry (locking, TTLs, dedupe) is
    already exercised by test_approvals.py."""

    def __init__(self, approvals: dict[str, PendingApproval]):
        self._approvals = approvals

    def get(self, approval_id: str, *, principal_id: str | None = None) -> PendingApproval | None:
        return self._approvals.get(approval_id)


def _card(approval_id: str, **kwargs) -> PendingApproval:
    kwargs.setdefault("kind", "card")
    kwargs.setdefault("gate_kind", "popup")
    return PendingApproval(id=approval_id, principal_id=PRINCIPAL.id, **kwargs)


class TestGuardDecision:
    def test_step_up_none_lets_a_sensitive_confirm_through(self):
        approval = _card("a1", kind="confirm", sensitive=True, gate_kind="")
        response = approval_step_up.guard_decision(
            PRINCIPAL, None,
            approval=approval, approval_id="a1", result="confirm", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("step-up is off -- must not be consulted"),
        )
        assert response is None

    def test_step_up_disabled_lets_a_write_accept_through(self):
        approval = _card("a1")
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=False, require_passkey=True),
            approval=approval, approval_id="a1", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("disabled -- must not be consulted"),
        )
        assert response is None

    def test_unknown_approval_is_never_gated(self):
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
            approval=None, approval_id="ghost", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("no approval -- must not be consulted"),
        )
        assert response is None

    def test_sensitive_confirm_with_require_passkey_offers_a_challenge(self):
        """"sensitive with passkey required" -- nothing enrolled, so the
        caller's own responder (here: a stand-in for a mode's real
        ``_step_up_response``) is what decides the shape; guard_decision
        just returns it untouched."""
        approval = _card("a1", kind="confirm", sensitive=True, gate_kind="")
        sentinel = JSONResponse({"error": "step_up_required"}, status_code=428)
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
            approval=approval, approval_id="a1", result="confirm", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: sentinel,
        )
        assert response is sentinel

    def test_an_ordinary_decisions_response_can_carry_an_idp_fallback(self):
        """Org mode's own ``_step_up_response`` can offer an IdP-reauth link
        instead of a hard refusal for an *ordinary* approving decision --
        guard_decision has no opinion on that shape at all, it only ever
        forwards whatever the callable returns. (Org mode's sensitive-
        confirm branch never reaches this: it only runs at all with
        ``require_passkey`` on, at which point its own ``_step_up_response``
        never offers an IdP link either -- see that function's own
        docstring.)"""
        approval = _card("a1")
        idp_response = JSONResponse(
            {"error": "step_up_required", "idp_stepup_url": "/api/approvals/a1/stepup/idp"}, status_code=428,
        )
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=False),
            approval=approval, approval_id="a1", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: idp_response,
        )
        assert response is idp_response

    def test_sensitive_confirm_needs_no_passkey_when_require_passkey_is_off_and_responder_lets_it_through(self):
        approval = _card("a1", kind="confirm", sensitive=True, gate_kind="")
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", require_passkey=False),
            approval=approval, approval_id="a1", result="confirm", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("require_passkey off -- sensitive check must not run"),
        )
        assert response is None

    def test_cancel_never_triggers_the_sensitive_confirm_check(self):
        approval = _card("a1", kind="confirm", sensitive=True, gate_kind="")
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
            approval=approval, approval_id="a1", result="cancel", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("cancel -- sensitive check must not run"),
        )
        assert response is None

    def test_sensitive_confirm_with_a_verified_assertion_falls_through_to_resolve(self):
        _enroll()
        approval = _card("a1", kind="confirm", sensitive=True, gate_kind="")
        challenges = StepUpChallengeStore()
        step_up = StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True)
        fingerprint = wa.decision_fingerprint(
            approval_id="a1", principal_id=PRINCIPAL.id, result="confirm", choice=None,
        )
        step_up_decide.begin_step_up(
            PRINCIPAL, rp_id=step_up.rp_id, subject_key="a1", fingerprint=fingerprint, challenges=challenges,
        )

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=_verified_assertion()):
            response = approval_step_up.guard_decision(
                PRINCIPAL, step_up,
                approval=approval, approval_id="a1", result="confirm", choice=None,
                step_up_results=STEP_UP_RESULTS, assertion={"id": "Y3JlZC0x"}, origin=ORIGIN, challenges=challenges,
                step_up_response=lambda: pytest.fail("assertion supplied -- must verify, not re-challenge"),
            )
        assert response is None

    def test_expired_challenge_reports_400(self):
        _enroll()
        approval = _card("a1")
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        response = approval_step_up.guard_decision(
            PRINCIPAL, step_up,
            approval=approval, approval_id="a1", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion={"id": "Y3JlZC0x"}, origin=ORIGIN,
            challenges=StepUpChallengeStore(),  # nothing pending -- never went through the 428 round trip
            step_up_response=lambda: pytest.fail("assertion supplied -- must verify, not re-challenge"),
        )
        assert response is not None
        assert response.status_code == 400

    def test_a_failed_assertion_reports_401_and_does_not_proceed(self):
        _enroll()
        approval = _card("a1")
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        challenges = StepUpChallengeStore()
        fingerprint = wa.decision_fingerprint(
            approval_id="a1", principal_id=PRINCIPAL.id, result="accept", choice=None,
        )
        step_up_decide.begin_step_up(
            PRINCIPAL, rp_id=step_up.rp_id, subject_key="a1", fingerprint=fingerprint, challenges=challenges,
        )

        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            response = approval_step_up.guard_decision(
                PRINCIPAL, step_up,
                approval=approval, approval_id="a1", result="accept", choice=None,
                step_up_results=STEP_UP_RESULTS, assertion={"id": "Y3JlZC0x"}, origin=ORIGIN, challenges=challenges,
                step_up_response=lambda: pytest.fail("assertion supplied -- must verify, not re-challenge"),
            )
        assert response is not None
        assert response.status_code == 401

    def test_ordinary_result_outside_scope_needs_nothing(self):
        _enroll()
        approval = _card("a1", gate_kind="review", pii_detected=False)
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", scope="writes"),
            approval=approval, approval_id="a1", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("out of scope -- must not be consulted"),
        )
        assert response is None

    def test_deny_never_needs_step_up(self):
        _enroll()
        approval = _card("a1")
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost"),
            approval=approval, approval_id="a1", result="deny", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: pytest.fail("deny -- must not be consulted"),
        )
        assert response is None

    def test_ordinary_write_accept_offers_a_challenge(self):
        approval = _card("a1")
        sentinel = JSONResponse({"error": "step_up_required"}, status_code=428)
        response = approval_step_up.guard_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost"),
            approval=approval, approval_id="a1", result="accept", choice=None,
            step_up_results=STEP_UP_RESULTS, assertion=None, origin=ORIGIN,
            challenges=StepUpChallengeStore(),
            step_up_response=lambda: sentinel,
        )
        assert response is sentinel


class TestBatchNeedsStepUp:
    def test_empty_batch_needs_nothing(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        needs = approval_step_up.batch_needs_step_up(
            [], principal_id=PRINCIPAL.id, registry=_FakeRegistry({}), step_up=step_up,
            batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is False

    def test_deny_only_items_are_skipped(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        deny_me = _card("d1")
        needs = approval_step_up.batch_needs_step_up(
            [("d1", "deny")], principal_id=PRINCIPAL.id, registry=_FakeRegistry({"d1": deny_me}), step_up=step_up,
            batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is False

    def test_mixed_batch_needs_step_up_only_for_its_accepting_item(self):
        """"batch with mixed sensitivity" -- a batch mixing an accept that
        needs step-up with a deny that never could still needs it overall,
        driven purely by the accept."""
        step_up = StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_pii_reads")
        accept_me = _card("a1", gate_kind="review", pii_detected=True)
        deny_me = _card("d1", gate_kind="review", pii_detected=False)
        needs = approval_step_up.batch_needs_step_up(
            [("a1", "accept"), ("d1", "deny")], principal_id=PRINCIPAL.id,
            registry=_FakeRegistry({"a1": accept_me, "d1": deny_me}), step_up=step_up,
            batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is True

    def test_an_accept_outside_scope_needs_nothing(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_pii_reads")
        plain_read = _card("a1", gate_kind="review", pii_detected=False)
        needs = approval_step_up.batch_needs_step_up(
            [("a1", "accept")], principal_id=PRINCIPAL.id, registry=_FakeRegistry({"a1": plain_read}),
            step_up=step_up, batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is False

    def test_a_non_batchable_item_needs_nothing(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        choice_dialog = _card("a1", kind="choice", gate_kind="popup")
        needs = approval_step_up.batch_needs_step_up(
            [("a1", "accept")], principal_id=PRINCIPAL.id, registry=_FakeRegistry({"a1": choice_dialog}),
            step_up=step_up, batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is False

    def test_an_unknown_id_needs_nothing(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        needs = approval_step_up.batch_needs_step_up(
            [("ghost", "accept")], principal_id=PRINCIPAL.id, registry=_FakeRegistry({}), step_up=step_up,
            batch_step_up_results=BATCH_STEP_UP_RESULTS,
        )
        assert needs is False


class TestBatchStepUpResponse:
    def test_nothing_enrolled_and_require_passkey_off_lets_it_through(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        response = approval_step_up.batch_step_up_response(
            PRINCIPAL, step_up, batch_id="b1", fingerprint="fp", challenges=StepUpChallengeStore(),
        )
        assert response is None

    def test_nothing_enrolled_and_require_passkey_on_hard_fails(self):
        step_up = StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True)
        response = approval_step_up.batch_step_up_response(
            PRINCIPAL, step_up, batch_id="b1", fingerprint="fp", challenges=StepUpChallengeStore(),
        )
        assert response is not None
        assert response.status_code == 403
        assert response.body and b"passkey_enrollment_required" in response.body

    def test_a_credential_offers_a_428_carrying_the_batch_id(self):
        _enroll()
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        response = approval_step_up.batch_step_up_response(
            PRINCIPAL, step_up, batch_id="b1", fingerprint="fp", challenges=StepUpChallengeStore(),
        )
        assert response is not None
        assert response.status_code == 428
        assert b'"batch_id":"b1"' in response.body


class TestGuardBatchDecision:
    def test_step_up_disabled_proceeds_unverified(self):
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, StepUpConfig(enabled=False),
            parsed=[("a1", "accept")], registry=_FakeRegistry({"a1": _card("a1")}), batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert response is None
        assert verified is False

    def test_deny_only_batch_proceeds_unverified(self):
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost"),
            parsed=[("d1", "deny")], registry=_FakeRegistry({"d1": _card("d1")}), batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert response is None
        assert verified is False

    def test_per_item_mode_refuses_the_whole_batch(self):
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", batch="per_item"),
            parsed=[("a1", "accept")], registry=_FakeRegistry({"a1": _card("a1")}), batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert verified is False
        assert response is not None
        assert response.status_code == 400
        assert b"split them up" in response.body

    def test_no_assertion_offers_a_challenge_unverified(self):
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost"),
            parsed=[("a1", "accept")], registry=_FakeRegistry({"a1": _card("a1")}), batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert verified is False
        assert response is None  # nothing enrolled, require_passkey off -- evadable fall-through

    def test_require_passkey_with_nothing_enrolled_hard_fails_unverified(self):
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
            parsed=[("a1", "accept")], registry=_FakeRegistry({"a1": _card("a1")}), batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert verified is False
        assert response is not None
        assert response.status_code == 403

    def test_a_valid_assertion_verifies_the_batch(self):
        _enroll()
        challenges = StepUpChallengeStore()
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        parsed = [("a1", "accept")]
        registry = _FakeRegistry({"a1": _card("a1")})
        first_response, first_verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, step_up, parsed=parsed, registry=registry, batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=challenges,
        )
        assert first_verified is False
        assert first_response is not None and first_response.status_code == 428

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=_verified_assertion()):
            response, verified = approval_step_up.guard_batch_decision(
                PRINCIPAL, step_up, parsed=parsed, registry=registry, batch_id="b1",
                batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
                assertion={"id": "Y3JlZC0x"}, origin=ORIGIN, challenges=challenges,
            )
        assert response is None
        assert verified is True

    def test_an_expired_assertion_reports_400_unverified(self):
        _enroll()
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        response, verified = approval_step_up.guard_batch_decision(
            PRINCIPAL, step_up,
            parsed=[("a1", "accept")], registry=_FakeRegistry({"a1": _card("a1")}), batch_id="made-up-batch-id",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion={"id": "Y3JlZC0x"}, origin=ORIGIN, challenges=StepUpChallengeStore(),
        )
        assert verified is False
        assert response is not None
        assert response.status_code == 400

    def test_a_failed_assertion_reports_401_unverified(self):
        _enroll()
        challenges = StepUpChallengeStore()
        step_up = StepUpConfig(enabled=True, rp_id="localhost")
        parsed = [("a1", "accept")]
        registry = _FakeRegistry({"a1": _card("a1")})
        approval_step_up.guard_batch_decision(
            PRINCIPAL, step_up, parsed=parsed, registry=registry, batch_id="b1",
            batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
            assertion=None, origin=ORIGIN, challenges=challenges,
        )
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            response, verified = approval_step_up.guard_batch_decision(
                PRINCIPAL, step_up, parsed=parsed, registry=registry, batch_id="b1",
                batch_step_up_results=BATCH_STEP_UP_RESULTS, per_item_message="split them up",
                assertion={"id": "Y3JlZC0x"}, origin=ORIGIN, challenges=challenges,
            )
        assert verified is False
        assert response is not None
        assert response.status_code == 401
