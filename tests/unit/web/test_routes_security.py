"""Tests for web/routes_security.py: passkey enrollment (P9; mode-agnostic since #426 Phase 1)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient

from privacyfence import paths, webauthn_stepup as wa
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_security as rs, session_auth

ISSUER = "https://pf.example.com"
LOCAL_ISSUER = "http://localhost:8765"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    # #426 Phase 4: register_verify/delete_credential/recover_credential
    # now write audit entries -- an un-initialized logger falls back to
    # the real ~/.privacyfence/audit (audit_log.py's own
    # _fallback_log_dir), which testing-policy.md is explicit tests must
    # never touch. init_audit_logger(str(tmp_path)) is that module's own
    # documented isolation pattern.
    init_audit_logger(str(tmp_path / "audit"))
    return tmp_path


def _app(*, step_up=None, sessions=None):
    """org mode's own wiring -- org_session's three functions bound to an
    ``OrgSessionStore``, and a redirect to ``/login`` when unauthenticated,
    exactly what web/server.py's ``_build_org_app`` passes."""
    sessions = sessions or org_session.OrgSessionStore()
    step_up = step_up or StepUpConfig(rp_id="pf.example.com", rp_name="PrivacyFence")
    routes = rs.build_routes(
        resolve_principal=lambda request: org_session.authenticated(request, sessions),
        check_csrf=org_session.check_csrf,
        check_origin=org_session.check_origin,
        unauthenticated_response=lambda request: RedirectResponse(
            "/login?next=/security", status_code=302, headers={"Cache-Control": "no-store"},
        ),
        session_cookie_name=org_session.SESSION_COOKIE,
        step_up=step_up, issuer_url=ISSUER,
    )
    app = Starlette(routes=routes)
    return app, sessions


def _local_app(*, step_up=None, sessions=None, dev_unseparated_notice=None):
    """local mode's own wiring -- session_auth's three functions bound to a
    ``LocalSessionStore``, always resolving to ``LOCAL_PRINCIPAL``, exactly
    what web/server.py's local branch of ``build_app`` passes."""
    sessions = sessions or session_auth.LocalSessionStore()
    step_up = step_up or StepUpConfig(rp_id="localhost", rp_name="PrivacyFence")
    routes = rs.build_routes(
        resolve_principal=lambda request: (
            LOCAL_PRINCIPAL if session_auth.authenticated(request, sessions) else None
        ),
        check_csrf=session_auth.check_csrf,
        check_origin=session_auth.check_origin,
        unauthenticated_response=session_auth.unauthorized_html,
        session_cookie_name=session_auth.SESSION_COOKIE,
        step_up=step_up, issuer_url=LOCAL_ISSUER,
        # Local mode has no /connect route (that's routes_connect.py's
        # org-mode-only surface) -- matches web/server.py's actual wiring.
        back_link=("/settings/connectors", "Back to Connectors"),
        dev_unseparated_notice=dev_unseparated_notice,
    )
    app = Starlette(routes=routes)
    return app, sessions


def _client(app, follow_redirects=False) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=follow_redirects)


def _signed_in(client, sessions, principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


def _signed_in_local(client, sessions) -> str:
    session_id = sessions.create()
    client.cookies.set(session_auth.SESSION_COOKIE, session_id)
    return session_id


def _audit_decisions(tmp_path) -> list[str]:
    """Reads back every ``decision`` recorded this week in the isolated
    audit log ``_fake_data_dir`` points ``init_audit_logger`` at -- see
    that fixture's own comment."""
    path = tmp_path / "audit" / f"{current_week()}.jsonl"
    if not path.exists():
        return []
    import json as _json
    return [_json.loads(line)["decision"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _register_first_credential(client, session_id, *, fake_verified=None) -> dict:
    """Drives a full options->verify round trip and returns the parsed
    JSON response -- shared by every Phase 4 test below that needs a
    freshly enrolled credential."""
    client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
    fake_verified = fake_verified or type("V", (), {
        "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
        "credential_device_type": type("D", (), {"value": "single_device"})(),
        "credential_backed_up": False,
    })()
    with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
        r = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id, "credential": {"id": "x"}, "label": "My Laptop",
        })
    assert r.status_code == 200
    return r.json()


class TestAuthRequired:
    def test_page_redirects_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).get("/security")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_register_options_is_401_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/security/webauthn/register/options", json={})
        assert r.status_code == 401


class TestSecurityPage:
    def test_lists_no_credentials_by_default(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert r.status_code == 200
        assert "No passkeys added yet." in r.text

    def test_lists_an_enrolled_credential(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0,
            device_type="single_device", backed_up=False, label="My Phone",
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert "My Phone" in r.text

    def test_org_mode_links_back_to_connect_by_default(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert 'href="/connect"' in r.text

    def test_the_lead_names_what_the_configured_scope_actually_covers(self):
        for scope, expected in (
            ("writes", "Required to approve a write."),
            ("writes_and_pii_reads", "Required to approve a write, or a read that detected personal data."),
            ("writes_and_reads", "Required to approve a write or a read."),
        ):
            app, sessions = _app(step_up=StepUpConfig(rp_id="pf.example.com", scope=scope))
            client = _client(app)
            _signed_in(client, sessions, ALICE)
            assert expected in client.get("/security").text

    def test_local_mode_links_back_to_the_connectors_settings_tab(self):
        app, sessions = _local_app()
        client = _client(app)
        _signed_in_local(client, sessions)
        r = client.get("/security")
        assert 'href="/settings/connectors"' in r.text
        assert 'href="/connect"' not in r.text

    def test_no_dev_unseparated_notice_by_default(self):
        app, sessions = _local_app()
        client = _client(app)
        _signed_in_local(client, sessions)
        r = client.get("/security")
        assert "DEV_ALLOW_UNSEPARATED" not in r.text

    def test_shows_the_dev_unseparated_notice_when_given_one(self):
        # ADR 0003 decision 7's /security half -- daemon_main.py's startup
        # log carries the same fact, see privilege_separation.
        # dev_unseparated_notice()'s own docstring.
        app, sessions = _local_app(
            dev_unseparated_notice="PRIVACYFENCE_DEV_ALLOW_UNSEPARATED is set -- not protected",
        )
        client = _client(app)
        _signed_in_local(client, sessions)
        r = client.get("/security")
        assert "not protected" in r.text


class TestRegisterOptions:
    def test_returns_options_bound_to_the_configured_rp(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert r.status_code == 200
        assert r.json()["options"]["rp"]["id"] == "pf.example.com"

    def test_wrong_csrf_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": "wrong"})
        assert r.status_code == 401

    def test_unconfigured_rp_id_is_a_clean_400(self):
        app, sessions = _app(step_up=StepUpConfig(rp_id=""))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert r.status_code == 400

    def test_invalid_json_body_is_treated_as_unauthorized_not_a_crash(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/options", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 401

    def test_mismatched_origin_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/options", json={"csrf": session_id},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403


class TestRegisterVerify:
    def test_unauthenticated_is_401(self):
        app, _sessions = _app()
        r = _client(app).post("/api/security/webauthn/register/verify", json={"credential": {"id": "x"}})
        assert r.status_code == 401

    def test_invalid_json_body_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/verify", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_object_json_body_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/verify", json="just a string")
        assert r.status_code == 400

    def test_missing_credential_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        r = client.post("/api/security/webauthn/register/verify", json={"csrf": session_id})
        assert r.status_code == 400

    def test_a_webauthn_verification_failure_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        with patch.object(wa.webauthn, "verify_registration_response", side_effect=ValueError("bad sig")):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "x"},
            })
        assert r.status_code == 400
        assert wa.list_credentials(ALICE) == []

    def test_verify_without_a_prior_options_call_fails(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id, "credential": {"id": "x"},
        })
        assert r.status_code == 400

    def test_successful_verification_stores_the_credential(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "x"}, "label": "My Laptop",
            })
        assert r.status_code == 200
        assert wa.list_credentials(ALICE)[0].label == "My Laptop"

    def test_the_challenge_is_single_use(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            client.post("/api/security/webauthn/register/verify", json={"csrf": session_id, "credential": {"id": "x"}})
            second = client.post(
                "/api/security/webauthn/register/verify", json={"csrf": session_id, "credential": {"id": "x"}},
            )
        assert second.status_code == 400


class TestDeleteCredential:
    def test_unauthenticated_is_401(self):
        # A JSON/fetch endpoint now (#426 Phase 3), not a plain form submit
        # -- see module docstring -- so this is a clean 401, not org mode's
        # own /login redirect (a fetch() wouldn't usefully follow that
        # anyway).
        app, _sessions = _app()
        r = _client(app).post("/security/credentials/Y3JlZC0x/delete", json={"csrf": "x"})
        assert r.status_code == 401

    def test_removes_a_credential_when_another_remains(self):
        # Not the *last* credential -- see TestDeleteLastCredentialRequiresStepUp
        # below for the gated case.
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0y", public_key="cGs2", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert r.status_code == 200
        assert [c.credential_id for c in wa.list_credentials(ALICE)] == ["Y3JlZC0y"]

    def test_wrong_csrf_does_not_delete(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": "wrong"})
        assert r.status_code == 401
        assert len(wa.list_credentials(ALICE)) == 1


class TestDeleteLastCredentialRequiresStepUp:
    """#426 Phase 3: removing your *only* enrolled passkey needs a fresh
    assertion first, regardless of ``step_up.require_passkey`` -- see
    module docstring. Mirrors test_routes_approvals.py's own
    TestStepUpWebAuthnFlow."""

    def test_malformed_json_body_is_treated_as_empty_not_a_crash(self):
        # No csrf in an unparseable body -- rejected as unauthorized, same
        # as every other route's malformed-body handling, not a 500.
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/security/credentials/Y3JlZC0x/delete", content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401
        assert len(wa.list_credentials(ALICE)) == 1

    def test_unconfigured_rp_id_is_a_clean_400(self):
        app, sessions = _app(step_up=StepUpConfig(rp_id="", rp_name="PrivacyFence"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert r.status_code == 400
        assert len(wa.list_credentials(ALICE)) == 1

    def test_an_assertion_with_no_matching_pending_challenge_is_rejected(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        # No prior 428 round-trip -- nothing pending in the delete-challenge
        # store.
        r = client.post("/security/credentials/Y3JlZC0x/delete", json={
            "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400
        assert len(wa.list_credentials(ALICE)) == 1

    def test_no_assertion_offers_webauthn_options(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        assert wa.list_credentials(ALICE) != []

    def test_valid_assertion_completes_the_deletion(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        first = client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post("/security/credentials/Y3JlZC0x/delete", json={
                "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert wa.list_credentials(ALICE) == []

    def test_a_failed_assertion_does_not_delete(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post("/security/credentials/Y3JlZC0x/delete", json={
                "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert r.status_code == 401
        assert len(wa.list_credentials(ALICE)) == 1


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestCrossPrincipalIsolation:
    """TST-10: every route
    here resolves ``principal`` from the request's own session
    (``_current_principal``) and never takes an id from the request body/
    path, so every store this module touches -- webauthn_stepup.py's
    per-principal credential file, and this module's own
    RegistrationChallengeStore -- is keyed by *that* principal, not
    whatever the request happens to mention. These tests prove that
    binding holds even when two different signed-in principals are active
    against the same running app/challenge-store instance at once, not
    just that each principal's own flow works in isolation (every test
    above only ever exercises one principal at a time).
    """

    def test_a_principals_enrolled_credential_is_invisible_to_another_signed_in_principal(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0,
            device_type="single_device", backed_up=False, label="Alice's Phone",
        ))
        bob_client = _client(app)
        _signed_in(bob_client, sessions, BOB)
        r = bob_client.get("/security")
        assert r.status_code == 200
        assert "Alice's Phone" not in r.text
        assert "No passkeys added yet." in r.text

    def test_a_principal_cannot_delete_another_principals_credential(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        bob_client = _client(app)
        bob_session_id = _signed_in(bob_client, sessions, BOB)
        # Bob's own CSRF token is valid for Bob's own session -- this isn't
        # a CSRF-bypass attempt, it's Bob legitimately POSTing to delete a
        # credential_id that happens to belong to Alice's account, not his
        # own. remove_credential() must scope to Bob's own credential list
        # regardless -- and since Bob has *no* credentials of his own,
        # is_last (module docstring) is false, so this never even reaches
        # the step-up gate.
        r = bob_client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": bob_session_id})
        assert r.status_code == 200
        assert len(wa.list_credentials(ALICE)) == 1
        assert wa.list_credentials(ALICE)[0].credential_id == "Y3JlZC0x"

    def test_a_registration_challenge_started_by_one_principal_cannot_be_completed_by_another(self):
        app, sessions = _app()
        alice_client = _client(app)
        alice_session_id = _signed_in(alice_client, sessions, ALICE)
        alice_client.post("/api/security/webauthn/register/options", json={"csrf": alice_session_id})

        bob_client = _client(app)
        bob_session_id = _signed_in(bob_client, sessions, BOB)
        # Bob has never called register/options himself -- his own session
        # has no pending challenge, regardless of what Alice's does.
        # RegistrationChallengeStore.pop() is keyed by principal.id, so
        # this must fail exactly like "no prior options call" would for a
        # single principal, not silently consume Alice's challenge on
        # Bob's behalf.
        r = bob_client.post("/api/security/webauthn/register/verify", json={
            "csrf": bob_session_id, "credential": {"id": "attacker-supplied"},
        })
        assert r.status_code == 400
        assert wa.list_credentials(BOB) == []

        # Alice's own still-pending challenge must be unaffected by Bob's
        # failed attempt -- her genuine follow-up verify still succeeds.
        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = alice_client.post("/api/security/webauthn/register/verify", json={
                "csrf": alice_session_id, "credential": {"id": "x"},
            })
        assert r.status_code == 200
        assert wa.list_credentials(ALICE) != []


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestLocalModeEnrollment:
    """#426 Phase 1: the exact same ``build_routes`` wired to web/
    session_auth.py instead of org_session -- local mode has exactly one
    identity (``LOCAL_PRINCIPAL``), so there's no cross-principal isolation
    to prove the way ``TestCrossPrincipalIsolation`` above does for org
    mode; this proves the generalized signature drives a real enroll/list/
    delete flow end to end against local mode's own session store, and that
    the two modes differ where they're supposed to (the unauthenticated
    response)."""

    def test_page_is_the_local_unauthorized_page_when_signed_out(self):
        # Not a redirect to /login (org mode's own page) -- local mode has
        # no such route, and shows session_auth.py's own recovery page
        # instead (401, not 302).
        app, _sessions = _local_app()
        r = TestClient(app, base_url=LOCAL_ISSUER).get("/security")
        assert r.status_code == 401
        assert "Not authorized" in r.text

    def test_enroll_list_and_delete_as_local_principal(self):
        app, sessions = _local_app()
        client = TestClient(app, base_url=LOCAL_ISSUER, follow_redirects=False)
        session_id = _signed_in_local(client, sessions)

        r = client.get("/security")
        assert r.status_code == 200
        assert "No passkeys added yet." in r.text

        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert r.status_code == 200
        assert r.json()["options"]["rp"]["id"] == "localhost"

        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "x"}, "label": "My Laptop",
            })
        assert r.status_code == 200
        assert wa.list_credentials(LOCAL_PRINCIPAL)[0].label == "My Laptop"

        # The only enrolled credential -- deleting it needs a fresh
        # assertion first (#426 Phase 3, TestDeleteLastCredentialRequiresStepUp).
        cred_id = wa.list_credentials(LOCAL_PRINCIPAL)[0].credential_id
        first = client.post(f"/security/credentials/{cred_id}/delete", json={"csrf": session_id})
        assert first.status_code == 428
        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            r = client.post(f"/security/credentials/{cred_id}/delete", json={
                "csrf": session_id, "webauthn_assertion": {"id": cred_id},
            })
        assert r.status_code == 200
        assert wa.list_credentials(LOCAL_PRINCIPAL) == []


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestEnrollAndRemoveAreAudited:
    """#426 Phase 4: every enroll/remove writes its own audit entry."""

    def test_enrollment_is_audited(self, tmp_path):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        _register_first_credential(client, session_id)
        assert "webauthn_credential_enrolled" in _audit_decisions(tmp_path)

    def test_removal_of_a_non_last_credential_is_audited(self, tmp_path):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0y", public_key="cGs2", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert "webauthn_credential_removed" in _audit_decisions(tmp_path)

    def test_removal_of_the_last_credential_is_audited_only_after_the_assertion_succeeds(self, tmp_path):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        assert "webauthn_credential_removed" not in _audit_decisions(tmp_path)

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            client.post("/security/credentials/Y3JlZC0x/delete", json={
                "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert "webauthn_credential_removed" in _audit_decisions(tmp_path)

    def test_a_failed_delete_assertion_is_not_audited_as_a_removal(self, tmp_path):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/security/credentials/Y3JlZC0x/delete", json={"csrf": session_id})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            client.post("/security/credentials/Y3JlZC0x/delete", json={
                "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert "webauthn_credential_removed" not in _audit_decisions(tmp_path)


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestRecoveryCodeIssuedOnEnrollment:
    """#426 Phase 4: register_verify hands back a one-time recovery code
    exactly when this principal doesn't already have an unused one."""

    def test_first_ever_enrollment_returns_a_recovery_code(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        assert "recovery_code" in data
        assert wa.has_recovery_code(ALICE) is True

    def test_a_second_enrollment_does_not_reissue_one(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        _register_first_credential(client, session_id)

        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        fake_verified = type("V", (), {
            "credential_id": b"second-raw-id", "credential_public_key": b"pub-key-2", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "y"}, "label": "Second Key",
            })
        assert "recovery_code" not in r.json()

    def test_re_enrolling_after_the_code_was_spent_issues_a_fresh_one(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        wa.consume_recovery_code(ALICE, data["recovery_code"])
        assert wa.has_recovery_code(ALICE) is False

        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        fake_verified = type("V", (), {
            "credential_id": b"second-raw-id", "credential_public_key": b"pub-key-2", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "y"}, "label": "Second Key",
            })
        assert "recovery_code" in r.json()


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestRecoverCredential:
    """#426 Phase 4: POST /security/recover -- the recovery-code path for
    when the only enrolled authenticator is lost, no WebAuthn ceremony
    involved."""

    def test_unauthenticated_is_401(self):
        app, _sessions = _app()
        r = _client(app).post("/security/recover", json={"code": "x"})
        assert r.status_code == 401

    def test_wrong_csrf_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/security/recover", json={"csrf": "wrong", "code": "x"})
        assert r.status_code == 401

    def test_missing_code_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/security/recover", json={"csrf": session_id})
        assert r.status_code == 400

    def test_malformed_json_body_is_treated_as_empty_not_a_crash(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/security/recover", content=b"not json", headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401

    def test_wrong_code_is_rejected_and_leaves_credentials_intact(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        assert data["recovery_code"]
        r = client.post("/security/recover", json={"csrf": session_id, "code": "0000-0000-0000-0000"})
        assert r.status_code == 401
        assert wa.list_credentials(ALICE) != []

    def test_correct_code_clears_every_enrolled_credential(self, tmp_path):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        r = client.post("/security/recover", json={"csrf": session_id, "code": data["recovery_code"]})
        assert r.status_code == 200
        assert wa.list_credentials(ALICE) == []
        assert "webauthn_recovery_code_used" in _audit_decisions(tmp_path)

    def test_code_is_single_use_even_for_recovery(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        code = data["recovery_code"]
        client.post("/security/recover", json={"csrf": session_id, "code": code})
        # Re-enroll, then try to reuse the already-spent code again.
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        fake_verified = type("V", (), {
            "credential_id": b"second-raw-id", "credential_public_key": b"pub-key-2", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "y"}, "label": "Second Key",
            })
        r = client.post("/security/recover", json={"csrf": session_id, "code": code})
        assert r.status_code == 401
        assert wa.list_credentials(ALICE) != []

    def test_a_principal_cannot_recover_using_another_principals_code(self):
        app, sessions = _app()
        alice_client = _client(app)
        alice_session_id = _signed_in(alice_client, sessions, ALICE)
        data = _register_first_credential(alice_client, alice_session_id)

        bob_client = _client(app)
        bob_session_id = _signed_in(bob_client, sessions, BOB)
        r = bob_client.post("/security/recover", json={"csrf": bob_session_id, "code": data["recovery_code"]})
        assert r.status_code == 401
        assert wa.list_credentials(ALICE) != []
