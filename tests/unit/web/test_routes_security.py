"""Tests for web/routes_security.py: passkey enrollment (P9; mode-agnostic since #426 Phase 1)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient

from privacyfence import paths, webauthn_stepup as wa
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


def _local_app(*, step_up=None, sessions=None):
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
    def test_unauthenticated_redirects_to_login(self):
        app, _sessions = _app()
        r = _client(app).post("/security/credentials/Y3JlZC0x/delete", data={"csrf": "x"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_removes_the_credential(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            "/security/credentials/Y3JlZC0x/delete", data={"csrf": session_id},
        )
        assert r.status_code in (302, 303)
        assert wa.list_credentials(ALICE) == []

    def test_wrong_csrf_does_not_delete(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", data={"csrf": "wrong"})
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
        # regardless.
        r = bob_client.post("/security/credentials/Y3JlZC0x/delete", data={"csrf": bob_session_id})
        assert r.status_code in (302, 303)
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

        cred_id = wa.list_credentials(LOCAL_PRINCIPAL)[0].credential_id
        r = client.post(f"/security/credentials/{cred_id}/delete", data={"csrf": session_id})
        assert r.status_code in (302, 303)
        assert wa.list_credentials(LOCAL_PRINCIPAL) == []
