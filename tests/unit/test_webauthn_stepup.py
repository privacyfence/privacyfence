"""Tests for webauthn_stepup.py.

The ``webauthn`` package's own CBOR/COSE attestation parsing and signature
verification is not re-tested here (D2's own reasoning: it's a maintained
library, not this repo's code) -- ``verify_registration_response``/
``verify_authentication_response`` are mocked at the module boundary so
these tests cover what this module is actually responsible for: credential
storage, challenge binding/single-use/TTL, the decision fingerprint, and
the writes-vs-reads scoping rule. ``begin_registration``/``begin_assertion``
are exercised for real (no mocking) since they only build options, never
verify anything.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import sys

import pytest
from webauthn.helpers import bytes_to_base64url

from privacyfence import paths, webauthn_stepup as wa
from privacyfence.principal import Principal

ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _b64u(raw: bytes) -> str:
    return bytes_to_base64url(raw)


# credential_id must be real base64url (webauthn_stepup.py round-trips it
# through base64url_to_bytes for begin_assertion's own exclude/allow lists),
# not an arbitrary test label -- "cred1"/"cred2" below are byte payloads
# encoded the same way a real credential id would be.
CRED1_ID = _b64u(b"cred-1-raw-id")


def _credential(credential_id: str = CRED1_ID, **overrides) -> wa.WebAuthnCredential:
    defaults = dict(
        credential_id=credential_id, public_key="pk", sign_count=0,
        device_type="single_device", backed_up=False, label="Passkey",
    )
    defaults.update(overrides)
    return wa.WebAuthnCredential(**defaults)


class TestCredentialStorage:
    def test_no_file_means_no_credentials(self):
        assert wa.list_credentials(ALICE) == []
        assert not wa.has_credentials(ALICE)

    def test_add_then_list_round_trips(self):
        wa.add_credential(ALICE, _credential())
        creds = wa.list_credentials(ALICE)
        assert len(creds) == 1
        assert creds[0].credential_id == CRED1_ID
        assert wa.has_credentials(ALICE)

    def test_adding_same_id_twice_replaces_not_duplicates(self):
        wa.add_credential(ALICE, _credential(label="First"))
        wa.add_credential(ALICE, _credential(label="Second"))
        creds = wa.list_credentials(ALICE)
        assert len(creds) == 1
        assert creds[0].label == "Second"

    def test_remove_credential(self):
        wa.add_credential(ALICE, _credential())
        assert wa.remove_credential(ALICE, CRED1_ID) is True
        assert wa.list_credentials(ALICE) == []

    def test_remove_unknown_credential_returns_false(self):
        assert wa.remove_credential(ALICE, "nope") is False

    def test_credentials_are_isolated_per_principal(self):
        wa.add_credential(ALICE, _credential(credential_id="alice-cred"))
        wa.add_credential(BOB, _credential(credential_id="bob-cred"))
        assert [c.credential_id for c in wa.list_credentials(ALICE)] == ["alice-cred"]
        assert [c.credential_id for c in wa.list_credentials(BOB)] == ["bob-cred"]

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_credentials_file_is_0600(self):
        wa.add_credential(ALICE, _credential())
        path = paths.authority_dir(ALICE) / wa.CREDENTIALS_FILE_NAME
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_corrupt_credentials_file_reads_as_empty_not_a_crash(self):
        path = paths.authority_dir(ALICE) / wa.CREDENTIALS_FILE_NAME
        path.write_text("not json", encoding="utf-8")
        assert wa.list_credentials(ALICE) == []


class TestBeginRegistration:
    def test_options_carry_the_configured_rp(self):
        options_json, challenge = wa.begin_registration(ALICE, rp_id="pf.example.com", rp_name="PrivacyFence")
        assert isinstance(challenge, bytes) and len(challenge) >= 16
        assert '"id": "pf.example.com"' in options_json or '"id":"pf.example.com"' in options_json
        assert "PrivacyFence" in options_json

    def test_existing_credentials_are_excluded(self):
        wa.add_credential(ALICE, _credential(credential_id=CRED1_ID))
        options_json, _ = wa.begin_registration(ALICE, rp_id="pf.example.com", rp_name="PrivacyFence")
        assert "excludeCredentials" in options_json


class TestFinishRegistration:
    def test_verified_registration_is_stored(self):
        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified) as mocked:
            cred = wa.finish_registration(
                ALICE, {"id": "x"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com", label="My Phone",
            )
        mocked.assert_called_once()
        assert mocked.call_args.kwargs["require_user_verification"] is True
        assert cred.label == "My Phone"
        assert wa.list_credentials(ALICE) == [cred]

    def test_verification_failure_raises_webauthn_error_not_the_libs_own(self):
        with patch.object(wa.webauthn, "verify_registration_response", side_effect=ValueError("bad signature")):
            with pytest.raises(wa.WebAuthnError):
                wa.finish_registration(
                    ALICE, {"id": "x"}, expected_challenge=b"chal", rp_id="pf.example.com",
                    origin="https://pf.example.com",
                )
        assert wa.list_credentials(ALICE) == []


class TestBeginAssertion:
    def test_none_when_no_credentials_enrolled(self):
        assert wa.begin_assertion(ALICE, rp_id="pf.example.com") is None

    def test_options_allow_the_enrolled_credential(self):
        wa.add_credential(ALICE, _credential(credential_id=CRED1_ID))
        result = wa.begin_assertion(ALICE, rp_id="pf.example.com")
        assert result is not None
        options_json, challenge = result
        assert isinstance(challenge, bytes)
        assert "allowCredentials" in options_json


class TestVerifyAssertion:
    def test_unknown_credential_id_raises(self):
        with pytest.raises(wa.WebAuthnError):
            wa.verify_assertion(
                ALICE, {"id": "unknown"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com",
            )

    def test_successful_assertion_updates_sign_count(self):
        wa.add_credential(ALICE, _credential(credential_id="cred1", sign_count=5))
        fake_verified = type("V", (), {"new_sign_count": 6, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified) as mocked:
            wa.verify_assertion(
                ALICE, {"id": "cred1"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com",
            )
        assert mocked.call_args.kwargs["require_user_verification"] is True
        assert mocked.call_args.kwargs["credential_current_sign_count"] == 5
        assert wa.list_credentials(ALICE)[0].sign_count == 6

    def test_verification_failure_raises_webauthn_error(self):
        wa.add_credential(ALICE, _credential(credential_id="cred1"))
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("nope")):
            with pytest.raises(wa.WebAuthnError):
                wa.verify_assertion(
                    ALICE, {"id": "cred1"}, expected_challenge=b"chal", rp_id="pf.example.com",
                    origin="https://pf.example.com",
                )
        # A failed verification must not silently bump the stored sign count.
        assert wa.list_credentials(ALICE)[0].sign_count == 0


class TestDecisionFingerprint:
    def test_stable_for_the_same_inputs(self):
        a = wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept", choice=None)
        b = wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept", choice=None)
        assert a == b

    @pytest.mark.parametrize("field, value", [
        ("approval_id", "a2"), ("principal_id", "bob"), ("result", "deny"),
    ])
    def test_changes_when_any_bound_field_changes(self, field, value):
        base = dict(approval_id="a1", principal_id="alice", result="accept", choice=None)
        overridden = dict(base, **{field: value})
        assert wa.decision_fingerprint(**base) != wa.decision_fingerprint(**overridden)

    def test_choice_none_differs_from_choice_zero(self):
        assert (
            wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept_all", choice=None)
            != wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept_all", choice=0)
        )


class TestBatchDecisionFingerprint:
    """The approval binder's own binding (Phase 3): the role
    decision_fingerprint plays for one decision, over a whole submitted
    set."""

    def test_stable_for_the_same_inputs(self):
        items = [("a1", "accept"), ("a2", "deny")]
        a = wa.batch_decision_fingerprint(principal_id="alice", items=items)
        b = wa.batch_decision_fingerprint(principal_id="alice", items=list(items))
        assert a == b

    def test_order_independent(self):
        # A batch has no meaningful order -- resubmitting the identical set
        # in a different order must fingerprint identically.
        forward = wa.batch_decision_fingerprint(principal_id="alice", items=[("a1", "accept"), ("a2", "deny")])
        backward = wa.batch_decision_fingerprint(principal_id="alice", items=[("a2", "deny"), ("a1", "accept")])
        assert forward == backward

    def test_changes_when_principal_changes(self):
        items = [("a1", "accept")]
        assert (
            wa.batch_decision_fingerprint(principal_id="alice", items=items)
            != wa.batch_decision_fingerprint(principal_id="bob", items=items)
        )

    def test_changes_when_an_item_is_added(self):
        smaller = [("a1", "accept")]
        larger = [("a1", "accept"), ("a2", "accept")]
        assert (
            wa.batch_decision_fingerprint(principal_id="alice", items=smaller)
            != wa.batch_decision_fingerprint(principal_id="alice", items=larger)
        )

    def test_changes_when_an_items_result_flips(self):
        accept = [("a1", "accept"), ("a2", "deny")]
        flipped = [("a1", "deny"), ("a2", "deny")]
        assert (
            wa.batch_decision_fingerprint(principal_id="alice", items=accept)
            != wa.batch_decision_fingerprint(principal_id="alice", items=flipped)
        )

    def test_binds_deny_items_too_not_just_the_accepting_ones(self):
        # §10.6's own binding covers the whole set a human saw, not merely
        # the subset that happened to need step-up -- an assertion for
        # {accept A, deny B} must not cover {accept A, deny C} either.
        with_b = [("a1", "accept"), ("b", "deny")]
        with_c = [("a1", "accept"), ("c", "deny")]
        assert (
            wa.batch_decision_fingerprint(principal_id="alice", items=with_b)
            != wa.batch_decision_fingerprint(principal_id="alice", items=with_c)
        )


class TestStepUpChallengeStore:
    def test_put_then_pop_returns_the_entry(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        entry = store.pop("alice", "a1")
        assert entry is not None
        assert entry.challenge == b"chal"
        assert entry.fingerprint == "fp"

    def test_pop_is_single_use(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        store.pop("alice", "a1")
        assert store.pop("alice", "a1") is None

    def test_missing_entry_returns_none(self):
        assert wa.StepUpChallengeStore().pop("alice", "a1") is None

    def test_expired_entry_returns_none(self):
        store = wa.StepUpChallengeStore(ttl=0.01)
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        time.sleep(0.02)
        assert store.pop("alice", "a1") is None

    def test_scoped_per_principal_and_approval(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        assert store.pop("bob", "a1") is None
        assert store.pop("alice", "a2") is None
        assert store.pop("alice", "a1") is not None

    def test_put_sweeps_expired_entries_so_unpopped_keys_dont_accumulate(self):
        """B26: a client-chosen key (routes_approvals.py's batch decide
        mints one straight from the request body) that is never popped
        must not grow the store without bound -- ``put()`` itself has to
        evict anything past the TTL, since nothing else ever will."""
        store = wa.StepUpChallengeStore(ttl=0.01)
        for i in range(50):
            store.put("alice", f"batch:{i}", challenge=b"chal", fingerprint="fp")
        time.sleep(0.02)
        store.put("alice", "batch:new", challenge=b"chal", fingerprint="fp")
        assert len(store._pending) == 1


class TestRegistrationChallengeStore:
    def test_put_then_pop(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"chal")
        assert store.pop("alice") == b"chal"

    def test_pop_is_single_use(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"chal")
        store.pop("alice")
        assert store.pop("alice") is None

    def test_a_second_put_replaces_the_first(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"first")
        store.put("alice", b"second")
        assert store.pop("alice") == b"second"


class TestIsStepUpRequired:
    def test_write_always_requires_it(self):
        assert wa.is_step_up_required(gate_kind="popup", pii_detected=False, scope="writes") is True
        assert wa.is_step_up_required(gate_kind="popup", pii_detected=False, scope="writes_and_pii_reads") is True

    def test_plain_read_never_requires_it(self):
        assert wa.is_step_up_required(gate_kind="review", pii_detected=False, scope="writes") is False
        assert wa.is_step_up_required(gate_kind="review", pii_detected=False, scope="writes_and_pii_reads") is False

    def test_pii_read_requires_it_only_in_the_wider_scope(self):
        assert wa.is_step_up_required(gate_kind="review", pii_detected=True, scope="writes") is False
        assert wa.is_step_up_required(gate_kind="review", pii_detected=True, scope="writes_and_pii_reads") is True


class TestRecoveryCode:
    def test_no_code_generated_yet(self):
        assert wa.has_recovery_code(ALICE) is False

    def test_generate_then_has_code(self):
        wa.generate_recovery_code(ALICE)
        assert wa.has_recovery_code(ALICE) is True

    def test_generated_code_is_human_typeable_and_not_the_stored_value(self):
        code = wa.generate_recovery_code(ALICE)
        assert len(code.replace("-", "")) == 16
        path = paths.authority_dir(ALICE) / wa.RECOVERY_CODE_FILE_NAME
        stored = path.read_text(encoding="utf-8")
        assert code not in stored

    def test_correct_code_is_consumed_successfully(self):
        code = wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(ALICE, code) is True

    def test_consuming_marks_it_used_so_it_cannot_be_reused(self):
        code = wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(ALICE, code) is True
        assert wa.consume_recovery_code(ALICE, code) is False

    def test_has_recovery_code_is_false_once_spent(self):
        code = wa.generate_recovery_code(ALICE)
        wa.consume_recovery_code(ALICE, code)
        assert wa.has_recovery_code(ALICE) is False

    def test_wrong_code_is_rejected(self):
        wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(ALICE, "0000-0000-0000-0000") is False

    def test_no_code_on_file_is_rejected_not_a_crash(self):
        assert wa.consume_recovery_code(ALICE, "anything") is False

    def test_consuming_is_case_and_whitespace_insensitive(self):
        code = wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(ALICE, f"  {code.lower()}  ") is True

    def test_generating_a_new_code_invalidates_the_old_one(self):
        first = wa.generate_recovery_code(ALICE)
        wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(ALICE, first) is False

    def test_recovery_codes_are_isolated_per_principal(self):
        code = wa.generate_recovery_code(ALICE)
        assert wa.consume_recovery_code(BOB, code) is False
        assert wa.has_recovery_code(ALICE) is True

    def test_corrupt_recovery_code_file_is_treated_as_none_not_a_crash(self):
        path = paths.authority_dir(ALICE) / wa.RECOVERY_CODE_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert wa.has_recovery_code(ALICE) is False
        assert wa.consume_recovery_code(ALICE, "anything") is False

    def test_non_hex_salt_is_rejected_not_a_crash(self):
        # A hand-tampered or corrupted stored record -- "salt" that isn't
        # valid hex must fail closed, not raise past consume_recovery_code.
        wa.generate_recovery_code(ALICE)
        path = paths.authority_dir(ALICE) / wa.RECOVERY_CODE_FILE_NAME
        import json as _json
        raw = _json.loads(path.read_text(encoding="utf-8"))
        raw["salt"] = "not-hex-zz"
        path.write_text(_json.dumps(raw), encoding="utf-8")
        assert wa.consume_recovery_code(ALICE, "anything") is False

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_recovery_code_file_is_0600(self):
        wa.generate_recovery_code(ALICE)
        path = paths.authority_dir(ALICE) / wa.RECOVERY_CODE_FILE_NAME
        assert (path.stat().st_mode & 0o777) == 0o600


class TestObserveStepUpRequirement:
    def test_first_startup_with_requirement_off_is_not_a_change(self):
        assert wa.observe_step_up_requirement(ALICE, enabled=False, require_passkey=False) is None

    def test_first_startup_with_requirement_on_is_reported_as_enabled(self):
        change = wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        assert change is not None
        assert change.was_required is False
        assert change.is_required is True

    def test_enabled_without_require_passkey_is_not_required(self):
        assert wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False) is None

    def test_repeated_startup_with_no_change_reports_nothing(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        assert wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True) is None

    def test_transition_to_disabled_is_reported(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        change = wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        assert change is not None
        assert change.was_required is True
        assert change.is_required is False

    def test_disabling_enabled_flag_while_require_passkey_stays_true_is_also_a_disable(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        change = wa.observe_step_up_requirement(ALICE, enabled=False, require_passkey=True)
        assert change is not None
        assert change.is_required is False

    def test_state_is_isolated_per_principal(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        # Bob's own first observation is still a fresh "was False" baseline,
        # unaffected by Alice's already-required state.
        change = wa.observe_step_up_requirement(BOB, enabled=True, require_passkey=True)
        assert change is not None
        assert change.was_required is False


class TestStepUpDisabledNotice:
    def test_no_notice_before_anything_is_observed(self):
        assert wa.step_up_disabled_notice(ALICE) is None

    def test_no_notice_while_still_required(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        assert wa.step_up_disabled_notice(ALICE) is None

    def test_notice_appears_after_a_disable_transition(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        assert wa.step_up_disabled_notice(ALICE) is not None

    def test_notice_persists_across_a_repeated_still_disabled_startup(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        assert wa.step_up_disabled_notice(ALICE) is not None

    def test_notice_clears_once_re_enabled(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        assert wa.step_up_disabled_notice(ALICE) is None

    def test_notice_is_isolated_per_principal(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=False)
        assert wa.step_up_disabled_notice(BOB) is None

    def test_corrupt_state_file_is_treated_as_unset_not_a_crash(self):
        wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        path = paths.authority_dir(ALICE) / wa.STEP_UP_STATE_FILE_NAME
        path.write_text("not json", encoding="utf-8")
        assert wa.step_up_disabled_notice(ALICE) is None
        # A fresh observation on a corrupt file is treated as a first-ever
        # startup (was_required defaults to False, same as a missing
        # file), so re-observing "True" still reads as a fresh enable.
        change = wa.observe_step_up_requirement(ALICE, enabled=True, require_passkey=True)
        assert change is not None
        assert change.was_required is False
        assert change.is_required is True
