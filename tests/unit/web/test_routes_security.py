"""Tests for web/routes_security.py: passkey enrollment, org mode."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient

from privacyfence import paths, web_shell, webauthn_stepup as wa
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_security as rs

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    # register_verify/delete_credential/recover_credential write audit
    # entries -- an un-initialized logger falls back to the real
    # ~/.privacyfence/audit (audit_log.py's own _fallback_log_dir), which
    # coding-and-testing-guidelines.md's "Fixtures & isolation" is explicit
    # tests must never touch. init_audit_logger(str(tmp_path)) is that module's own
    # documented isolation pattern.
    init_audit_logger(str(tmp_path / "audit"))
    return tmp_path


def _app(*, step_up=None, sessions=None, recovery_limiter=None):
    """org mode's own wiring -- org_session's three functions bound to an
    ``OrgSessionStore``, and a redirect to ``/login`` when unauthenticated,
    exactly what web/server.py's ``_build_org_app`` passes -- including
    ``nav_items=web_shell.ORG_NAV_ITEMS``, since that's what makes this page
    carry the same persistent header/nav as /approvals/connect/settings."""
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
        nav_items=web_shell.ORG_NAV_ITEMS,
        recovery_limiter=recovery_limiter,
    )
    app = Starlette(routes=routes)
    return app, sessions


def _client(app, follow_redirects=False) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=follow_redirects)


def _signed_in(client, sessions, principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
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


def _audit_summaries(tmp_path) -> list[str]:
    """``_audit_decisions``' sibling, for the one place the ``decision`` value
    alone does not carry the fact worth asserting: a *first* enrollment and a
    later one share the ``webauthn_credential_enrolled`` decision and differ in
    their summary."""
    path = tmp_path / "audit" / f"{current_week()}.jsonl"
    if not path.exists():
        return []
    import json as _json
    return [_json.loads(line)["summary"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _register_another_credential(client, session_id, *, credential_id=b"second-raw-id", label="Second Key"):
    """A second enrollment, through the enrollment gate's own gate: the options call is
    428'd for a fresh assertion once a credential exists, so a test that only
    wants *a second credential on file* has to answer that first. The
    assertion itself is verified for real in TestEnrollmentGate below; here
    ``verify_assertion`` is patched, the same way ``verify_registration_
    response`` already is, so a test about recovery codes stays about recovery
    codes.

    Returns the parsed ``register/verify`` response.
    """
    r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
    assert r.status_code == 428, r.text
    with patch.object(wa, "verify_assertion", return_value=None):
        r = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": {"id": "already-enrolled"},
        })
    assert r.status_code == 200, r.text
    fake_verified = type("V", (), {
        "credential_id": credential_id, "credential_public_key": b"pub-key-2", "sign_count": 0,
        "credential_device_type": type("D", (), {"value": "single_device"})(),
        "credential_backed_up": False,
    })()
    with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
        return client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id, "credential": {"id": "y"}, "label": label,
        })


def _register_first_credential(client, session_id, *, fake_verified=None) -> dict:
    """Drives a full options->verify round trip and returns the parsed
    JSON response -- shared by every test below that needs a
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

    def test_a_safe_next_gets_a_continue_link_once_a_passkey_is_enrolled(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        next_path = "/oauth/single-user/continue?state=abc"
        before = client.get("/security", params={"next": next_path})
        assert 'id="pf-continue"' not in before.text
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0,
            device_type="single_device", backed_up=False, label="My Phone",
        ))
        after = client.get("/security", params={"next": next_path})
        assert 'id="pf-continue" href="/oauth/single-user/continue?state=abc"' in after.text

    @pytest.mark.parametrize("next_path", [
        "//evil.example", "/\\evil.example", "https://evil.example/", "/\t/evil.example", "/\n/evil.example",
        "/ok path",
    ])
    def test_an_unsafe_next_gets_no_continue_link(self, next_path):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0,
            device_type="single_device", backed_up=False, label="My Phone",
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security", params={"next": next_path})
        assert r.status_code == 200
        assert 'id="pf-continue"' not in r.text

    def test_safe_next_path_keeps_a_plain_path_and_refuses_control_characters(self):
        assert rs.safe_next_path("/approvals?x=1", default="") == "/approvals?x=1"
        assert rs.safe_next_path("/a\x7fb", default="d") == "d"
        assert rs.safe_next_path(None, default="d") == "d"

    def test_org_mode_links_back_to_connect_by_default(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert 'href="/connect"' in r.text

    def test_org_mode_carries_the_persistent_shell_nav_not_a_footer_link(self):
        # web_shell.wrap()'d (web_shell.ORG_NAV_ITEMS) since the fix that
        # made /approvals'/connect's/settings' shared header/nav survive
        # navigating into /security too -- previously this page was a bare
        # doctype+tokens.css document with a single "Back to connections"
        # link at the bottom.
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert 'class="pf-shell-nav-item active" href="/security"' in r.text
        for href in ("/approvals", "/connect", "/settings"):
            assert f'class="pf-shell-nav-item" href="{href}"' in r.text
        assert "Back to connections" not in r.text

    def test_admin_sees_the_admin_nav_item_and_a_member_does_not(self):
        # The page is mounted once with ORG_NAV_ITEMS, so the admin item has to be added per
        # principal; single-user mode lands here after the first sign-in.
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        assert 'href="/admin"' not in client.get("/security").text
        admin = Principal(id="root", email="root@example.com", display_name="Root", is_admin=True)
        client = _client(app)
        _signed_in(client, sessions, admin)
        assert 'class="pf-shell-nav-item" href="/admin"' in client.get("/security").text

    def test_org_mode_signed_in_principal_is_shown_in_the_shell_header(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert f'<div class="pf-shell-principal">{ALICE.email}</div>' in r.text

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
        # A JSON/fetch endpoint now, not a plain form submit
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
    """Removing your *only* enrolled passkey needs a fresh
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
    """Every route here resolves ``principal`` from the request's own session
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
# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestEnrollAndRemoveAreAudited:
    """Every enroll/remove writes its own audit entry."""

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
    """register_verify hands back a one-time recovery code
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

        r = _register_another_credential(client, session_id)
        assert r.status_code == 200
        assert "recovery_code" not in r.json()

    def test_re_enrolling_after_the_code_was_spent_issues_a_fresh_one(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        data = _register_first_credential(client, session_id)
        wa.consume_recovery_code(ALICE, data["recovery_code"])
        assert wa.has_recovery_code(ALICE) is False

        r = _register_another_credential(client, session_id)
        assert r.status_code == 200
        assert "recovery_code" in r.json()


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.
@pytest.mark.unit
class TestRecoverCredential:
    """POST /security/recover -- the recovery-code path for
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


def _audit_entries(tmp_path) -> list[dict]:
    """Every whole audit record this week -- for the recovery tests, which
    assert on more than one field of the same entry."""
    path = tmp_path / "audit" / f"{current_week()}.jsonl"
    if not path.exists():
        return []
    import json as _json
    return [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.unit
class TestRecoverCredentialHardening:
    """POST /security/recover's guards: every attempt is audited, and attempts
    are rate-limited per session and globally."""

    WRONG = "0000-0000-0000-0000"

    def test_a_wrong_code_is_audited_with_who_and_why_but_not_the_code(self, tmp_path):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        _register_first_credential(client, session_id)

        r = client.post("/security/recover", json={"csrf": session_id, "code": self.WRONG})

        assert r.status_code == 401
        refused = [e for e in _audit_entries(tmp_path) if e["decision"] == "webauthn_recovery_refused"]
        assert len(refused) == 1
        assert refused[0]["sender"] == "alice@example.com"
        assert refused[0]["summary"] == "Recovery code refused: invalid or already-used recovery code"
        assert self.WRONG not in json.dumps(_audit_entries(tmp_path))

    def test_a_successful_trade_in_is_audited_and_the_code_is_not_logged(self, tmp_path):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        code = _register_first_credential(client, session_id)["recovery_code"]

        r = client.post("/security/recover", json={"csrf": session_id, "code": code})

        assert r.status_code == 200
        used = [e for e in _audit_entries(tmp_path) if e["decision"] == "webauthn_recovery_code_used"]
        assert [e["sender"] for e in used] == ["alice@example.com"]
        assert "webauthn_recovery_refused" not in _audit_decisions(tmp_path)
        assert code not in json.dumps(_audit_entries(tmp_path))

    def test_a_missing_code_is_neither_audited_nor_counted(self, tmp_path):
        app, sessions = _app(recovery_limiter=rs.RecoveryAttemptLimiter(per_key=1))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        for _ in range(3):
            assert client.post("/security/recover", json={"csrf": session_id}).status_code == 400
        assert "webauthn_recovery_refused" not in _audit_decisions(tmp_path)
        r = client.post("/security/recover", json={"csrf": session_id, "code": self.WRONG})
        assert r.status_code == 401

    def test_the_attempt_after_the_per_session_budget_is_refused_even_with_the_right_code(self, tmp_path):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        code = _register_first_credential(client, session_id)["recovery_code"]

        for _ in range(rs.RECOVERY_MAX_ATTEMPTS_PER_SESSION):
            assert client.post("/security/recover", json={"csrf": session_id, "code": self.WRONG}).status_code == 401
        r = client.post("/security/recover", json={"csrf": session_id, "code": code})

        assert r.status_code == 429
        assert r.json()["error"] == "too_many_attempts"
        assert 0 < int(r.headers["Retry-After"]) <= rs.RECOVERY_WINDOW_SECONDS
        assert wa.list_credentials(ALICE) != []
        assert wa.has_recovery_code(ALICE) is True
        assert _audit_summaries(tmp_path)[-1] == "Recovery code refused: too many recovery attempts, try again later"

    def test_a_fresh_session_does_not_reset_the_global_budget(self):
        limiter = rs.RecoveryAttemptLimiter(per_key=10, global_limit=2)
        app, sessions = _app(recovery_limiter=limiter)
        for _ in range(2):
            client = _client(app)
            session_id = _signed_in(client, sessions, ALICE)
            assert client.post("/security/recover", json={"csrf": session_id, "code": self.WRONG}).status_code == 401

        client = _client(app)
        session_id = _signed_in(client, sessions, BOB)
        r = client.post("/security/recover", json={"csrf": session_id, "code": self.WRONG})

        assert r.status_code == 429


@pytest.mark.unit
class TestRecoveryAttemptLimiter:
    def _limiter(self, **kwargs):
        now = {"t": 1000.0}
        return rs.RecoveryAttemptLimiter(clock=lambda: now["t"], **kwargs), now

    def test_per_key_budget_and_retry_after(self):
        limiter, now = self._limiter(window_seconds=60, per_key=2, global_limit=100)
        assert limiter.try_acquire("a") is None
        now["t"] += 10
        assert limiter.try_acquire("a") is None
        now["t"] += 5
        # The first attempt (t=1000) leaves the window at t=1060.
        assert limiter.try_acquire("a") == 45
        # Another key is unaffected.
        assert limiter.try_acquire("b") is None

    def test_a_refused_attempt_is_not_recorded_and_the_window_slides(self):
        limiter, now = self._limiter(window_seconds=60, per_key=1, global_limit=100)
        assert limiter.try_acquire("a") is None
        now["t"] += 30
        assert limiter.try_acquire("a") == 30
        now["t"] += 30
        assert limiter.try_acquire("a") is None

    def test_global_budget_spans_keys(self):
        limiter, now = self._limiter(window_seconds=60, per_key=5, global_limit=2)
        assert limiter.try_acquire("a") is None
        assert limiter.try_acquire("b") is None
        assert limiter.try_acquire("c") == 60
        now["t"] += 60
        assert limiter.try_acquire("c") is None

    def test_when_both_budgets_are_full_the_later_one_decides(self):
        limiter, now = self._limiter(window_seconds=60, per_key=1, global_limit=2)
        assert limiter.try_acquire("b") is None
        now["t"] += 20
        assert limiter.try_acquire("a") is None
        # Global frees at t=1060, "a"'s own budget only at t=1080.
        assert limiter.try_acquire("a") == 60

    def test_retry_after_is_at_least_one_second(self):
        limiter, now = self._limiter(window_seconds=60, per_key=1, global_limit=100)
        assert limiter.try_acquire("a") is None
        now["t"] += 59.9
        assert limiter.try_acquire("a") == 1


# ===================================================================== #
# the enrollment gate: enrolling a passkey is
# gated too. See routes_security.py's own module docstring for the
# self-enrollment bypass these classes are the regression test for.
# ===================================================================== #

RP_ID = "pf.example.com"


def _b64u_challenge(value: str) -> bytes:
    """A challenge as it appears in the options JSON these routes return, back
    in the ``bytes`` form ``SoftwareAuthenticator`` signs over -- the decode a
    browser does inside ``PF_WEBAUTHN_JS``'s own ``pfB64uToBuf``."""
    from webauthn.helpers import base64url_to_bytes

    return base64url_to_bytes(value)


def _software_authenticator(**kwargs):
    from tests.software_authenticator import SoftwareAuthenticator

    return SoftwareAuthenticator(**kwargs)


def _enroll_for_real(principal, authenticator, *, label="Alice's Touch ID", rp_id=RP_ID, origin=ISSUER):
    """Put a credential on file the way a real authenticator would -- through
    ``webauthn_stepup`` itself, with real signatures, bypassing the HTTP
    routes. Stands in for "the human has already enrolled their hardware
    passkey", which is the starting state every gate test below needs and
    which no route can now reach without the gate it is testing."""
    _options_json, challenge = wa.begin_registration(principal, rp_id=rp_id, rp_name="PrivacyFence")
    credential = authenticator.register(challenge=challenge, rp_id=rp_id, origin=origin)
    return wa.finish_registration(
        principal, credential, expected_challenge=challenge, rp_id=rp_id, origin=origin, label=label,
    )


class TestEnrollmentGateWithACredentialAlreadyEnrolled:
    """0.1: ``register_options`` demands a fresh assertion with a credential
    the principal already has, through the same 428-then-retry protocol
    ``delete_credential`` and ``decide`` already share.

    Driven end to end against real py_webauthn, with no mocking of either
    ceremony -- the whole point is that the adversary here is exactly as
    capable as the proof-of-concept was, and only the gate is new.
    """

    def test_a_session_alone_no_longer_enrolls_a_second_passkey(self, _fake_data_dir):
        # The self-enrollment bypass, as a regression test. Setup is the paranoid
        # configuration: a genuine credential already enrolled, and an agent
        # holding nothing but the session cookie.
        _enroll_for_real(ALICE, _software_authenticator())
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        assert r.status_code == 428
        assert r.json()["error"] == "step_up_required"
        # And no ceremony was opened, so the second half cannot be reached
        # either -- not even with a credential the caller generated itself.
        agent_key = _software_authenticator()
        verify = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id,
            "credential": agent_key.register(challenge=b"whatever", rp_id=RP_ID, origin=ISSUER),
            "label": "Backup key",
        })
        assert verify.status_code == 400
        assert len(wa.list_credentials(ALICE)) == 1

    def test_the_428_carries_options_naming_the_enrolled_credential(self, _fake_data_dir):
        enrolled = _enroll_for_real(ALICE, _software_authenticator())
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        options = r.json()["webauthn_options"]
        assert options["rpId"] == RP_ID
        assert options["userVerification"] == "required"
        assert [c["id"] for c in options["allowCredentials"]] == [enrolled.credential_id]

    def test_asserting_with_the_enrolled_credential_opens_the_ceremony(self, _fake_data_dir):
        # The other direction: the gate is a gate, not a wall. A human who can
        # prove possession of the passkey they already have gets their second
        # one, in one extra round trip.
        authenticator = _software_authenticator()
        _enroll_for_real(ALICE, authenticator)
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        challenge = _b64u_challenge(
            client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
            .json()["webauthn_options"]["challenge"]
        )
        assertion = authenticator.assert_(challenge=challenge, rp_id=RP_ID, origin=ISSUER)
        opened = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": assertion,
        })
        assert opened.status_code == 200

        second = _software_authenticator()
        registration_challenge = _b64u_challenge(opened.json()["options"]["challenge"])
        verify = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id,
            "credential": second.register(challenge=registration_challenge, rp_id=RP_ID, origin=ISSUER),
            "label": "Second Key",
        })
        assert verify.status_code == 200, verify.text
        assert {c.label for c in wa.list_credentials(ALICE)} == {"Alice's Touch ID", "Second Key"}

    def test_an_assertion_from_an_unenrolled_key_is_refused(self, _fake_data_dir):
        # The agent's own synthetic key, signing the real challenge. Correct
        # cryptography over the right challenge, and still refused: it is not
        # in this principal's credential store, which is the only thing the
        # signature can be checked against.
        _enroll_for_real(ALICE, _software_authenticator())
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        challenge = _b64u_challenge(
            client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
            .json()["webauthn_options"]["challenge"]
        )
        forged = _software_authenticator().assert_(challenge=challenge, rp_id=RP_ID, origin=ISSUER)
        r = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": forged,
        })

        assert r.status_code == 401
        assert len(wa.list_credentials(ALICE)) == 1

    def test_a_refused_assertion_is_audited(self, _fake_data_dir):
        _enroll_for_real(ALICE, _software_authenticator())
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        challenge = _b64u_challenge(
            client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
            .json()["webauthn_options"]["challenge"]
        )
        forged = _software_authenticator().assert_(challenge=challenge, rp_id=RP_ID, origin=ISSUER)

        client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": forged,
        })

        assert "webauthn_enrollment_refused" in _audit_decisions(_fake_data_dir)

    def test_the_428_itself_is_not_audited(self, _fake_data_dir):
        # A 428 is a round trip in the ordinary protocol -- every legitimate
        # second enrollment produces one. Recording it would bury the
        # refusals that matter.
        _enroll_for_real(ALICE, _software_authenticator())
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        assert "webauthn_enrollment_refused" not in _audit_decisions(_fake_data_dir)

    def test_an_assertion_with_no_ceremony_in_flight_is_refused(self, _fake_data_dir):
        # No 428 was ever asked for, so there is no server-side challenge this
        # assertion could have been bound to.
        authenticator = _software_authenticator()
        _enroll_for_real(ALICE, authenticator)
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id,
            "webauthn_assertion": authenticator.assert_(challenge=b"self-chosen", rp_id=RP_ID, origin=ISSUER),
        })

        assert r.status_code == 400
        assert r.json()["error"] == "step_up_expired"

    def test_an_assertion_obtained_for_a_delete_cannot_be_replayed_into_an_enrollment(self, _fake_data_dir):
        # _enroll_fingerprint's own job, and the reason the two ceremonies get
        # separate stores: the delete route's 428 is a real prompt a human
        # answers, and its answer must not also buy an enrollment.
        authenticator = _software_authenticator()
        enrolled = _enroll_for_real(ALICE, authenticator)
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        delete_428 = client.post(
            f"/security/credentials/{enrolled.credential_id}/delete", json={"csrf": session_id},
        )
        assert delete_428.status_code == 428
        challenge = _b64u_challenge(delete_428.json()["webauthn_options"]["challenge"])
        assertion = authenticator.assert_(challenge=challenge, rp_id=RP_ID, origin=ISSUER)

        r = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": assertion,
        })

        assert r.status_code == 400
        assert r.json()["error"] == "step_up_expired"

    def test_the_assertion_is_single_use(self, _fake_data_dir):
        authenticator = _software_authenticator()
        _enroll_for_real(ALICE, authenticator)
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        challenge = _b64u_challenge(
            client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
            .json()["webauthn_options"]["challenge"]
        )
        assertion = authenticator.assert_(challenge=challenge, rp_id=RP_ID, origin=ISSUER)
        assert client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": assertion,
        }).status_code == 200

        replayed = client.post("/api/security/webauthn/register/options", json={
            "csrf": session_id, "webauthn_assertion": assertion,
        })

        assert replayed.status_code == 400


class TestFirstEnrollmentGate:
    """With nothing enrolled there is no credential to assert with."""

    def test_a_first_enrollment_proceeds_on_the_idp_session_alone(self, _fake_data_dir):
        # Deliberate, and documented as such in docs/security-and-compliance.md
        # rather than papered over. This test exists so that stays a decision
        # rather than an oversight nobody notices.
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        assert r.status_code == 200


class _UnauthorizedChallengeStore(wa.RegistrationChallengeStore):
    """Stands in for a future code path that issues a registration challenge
    without passing the enrollment gate -- it drops the ``authorized`` flag on the
    floor, which is precisely the mistake ``register_verify``'s tripwire
    exists to catch. Substituted for the real store at ``build_routes`` time,
    since nothing reachable through the routes themselves can produce an
    unauthorized challenge any more (which is the point)."""

    def put(self, principal_id, challenge, *, authorized=False):  # noqa: ARG002 -- dropped on purpose
        super().put(principal_id, challenge, authorized=False)


class TestTheFirstEnrollmentIsNamedInTheAuditTrail:
    """The one enrollment no already-enrolled credential could have gated is
    also the one that decides what every later step-up check is satisfied by,
    so it is worth picking out of the log by eye."""

    def test_a_first_enrollment_says_so(self, _fake_data_dir):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        _register_first_credential(client, session_id)

        assert any(s.startswith("First passkey enrolled:") for s in _audit_summaries(_fake_data_dir))

    def test_a_later_enrollment_does_not(self, _fake_data_dir):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        _register_first_credential(client, session_id)

        _register_another_credential(client, session_id, label="Second Key")

        summaries = _audit_summaries(_fake_data_dir)
        assert "Passkey enrolled: 'Second Key'" in summaries
        assert not any(s.startswith("First passkey enrolled: 'Second Key'") for s in summaries)


class TestRegisterVerifyRequiresAnAuthorizedCeremony:
    """``register_verify``'s own half of 0.1 -- the invariant tripwire, not a
    second prompt. See routes_security.py's module docstring."""

    def _app_with_an_unauthorizing_store(self, monkeypatch):
        monkeypatch.setattr(rs, "RegistrationChallengeStore", _UnauthorizedChallengeStore)
        return _app()

    def test_a_challenge_that_never_passed_the_gate_is_refused(self, _fake_data_dir, monkeypatch):
        app, sessions = self._app_with_an_unauthorizing_store(monkeypatch)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        opened = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert opened.status_code == 200

        r = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id,
            "credential": _software_authenticator().register(
                challenge=_b64u_challenge(opened.json()["options"]["challenge"]),
                rp_id=RP_ID, origin=ISSUER,
            ),
            "label": "Sneaked in",
        })

        assert r.status_code == 403
        assert r.json()["error"] == "enrollment_not_authorized"
        assert wa.list_credentials(ALICE) == []

    def test_that_refusal_is_audited(self, _fake_data_dir, monkeypatch):
        app, sessions = self._app_with_an_unauthorizing_store(monkeypatch)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        opened = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id,
            "credential": _software_authenticator().register(
                challenge=_b64u_challenge(opened.json()["options"]["challenge"]),
                rp_id=RP_ID, origin=ISSUER,
            ),
        })

        assert "webauthn_enrollment_refused" in _audit_decisions(_fake_data_dir)


# In-process ASGI TestClient, no real socket -- unit per testing-policy.md's
# seven-layer taxonomy.

class TestNoRecentSignInsSection:
    def test_the_page_has_no_recent_sign_ins_section_and_logs_no_warning(self, caplog):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        with caplog.at_level("WARNING"):
            body = client.get("/security").text

        assert "Recent sign-ins" not in body
        assert "Passkeys" in body
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


class TestRegisterVerifyRequestChecks:
    def test_a_wrong_csrf_is_a_401(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post("/api/security/webauthn/register/verify", json={"csrf": "wrong", "credential": {}})

        assert r.status_code == 401


class TestAuditFailureNeverFailsTheRequest:
    def test_an_audit_logger_that_raises_is_logged_and_the_enrollment_completes(self, monkeypatch, caplog):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        monkeypatch.setattr(rs.get_audit_logger(), "record", lambda entry: (_ for _ in ()).throw(OSError("disk full")))

        with caplog.at_level("WARNING"):
            body = _register_first_credential(client, session_id)

        assert body["status"] == "ok"
        assert any("Audit log write failed" in r.getMessage() for r in caplog.records)


class TestRenderSecurityPageShapes:
    def _creds(self, created_at):
        return [type("C", (), {
            "credential_id": "c1", "label": "Key", "created_at": created_at, "backed_up": False,
            "last_used_at": None,
        })()]

    def _render(self, **kwargs):
        return rs._render_security_page(
            principal=ALICE, creds=kwargs.pop("creds", []), csrf="csrf", nonce="n",
            step_up=StepUpConfig(rp_id="pf.example.com"), back_link=("/connect", "Back to connections"), **kwargs,
        )

    def test_without_nav_items_the_page_is_the_plain_document_with_its_back_link(self):
        html = self._render()

        assert 'href="/connect"' in html
        assert 'class="pf-shell-nav"' not in html

    def test_an_unrepresentable_creation_time_still_renders_the_row(self):
        html = self._render(creds=self._creds(10**30))

        assert "Key" in html

    def test_custom_nav_items_are_used_as_given(self):
        html = self._render(nav_items=(("/connect", "Connections", "connect"),))

        assert "Connections" in html
