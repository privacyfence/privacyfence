"""Tests for web/routes_org_approvals.py: the principal-aware /approvals
surface in org mode (P9), including the WebAuthn/IdP step-up gate on write
decisions (§10.6, D7).
"""
from __future__ import annotations

import urllib.parse as up
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import org_identity as oi, paths, webauthn_stepup as wa
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.principal import Principal, principal_scope
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_org_approvals as roa
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


def _idp(**overrides) -> oi.IdpConfig:
    defaults = dict(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )
    defaults.update(overrides)
    return oi.IdpConfig(**defaults)


def _app(*, step_up: StepUpConfig | None = None, sessions=None, idp=None, web_ui=None):
    sessions = sessions or org_session.OrgSessionStore()
    web_ui = web_ui or WebApprovalUI(registry=PendingApprovalRegistry())
    step_up = step_up or StepUpConfig()
    routes = roa.build_routes(web_ui=web_ui, sessions=sessions, step_up=step_up, idp=idp or _idp(), issuer_url=ISSUER)
    app = Starlette(routes=routes)
    return app, sessions, web_ui


def _client(app, follow_redirects=False) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=follow_redirects)


def _signed_in(client: TestClient, sessions: org_session.OrgSessionStore, principal: Principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


def _register(web_ui, principal: Principal, *, gate_kind="review", pii_detected=False, dedupe_key="k1"):
    with principal_scope(principal):
        approval, _ = web_ui.deferred_registry.register_or_coalesce(
            dedupe_key=dedupe_key, connector="gmail", tool="gmail_get_message", gate_kind=gate_kind,
            request_id="r1", summary="a message", tool_name="Get message", pii_detected=pii_detected,
        )
    web_ui.deferred_registry.set_html(approval.id, "<!doctype html><html><head></head><body>CARD</body></html>")
    return approval


class TestIndexAndServiceWorker:
    def test_root_redirects_to_approvals(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/")
        assert r.status_code == 302
        assert r.headers["location"] == "/approvals"

    def test_service_worker_is_served_with_no_auth(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")


class TestAuthRequired:
    def test_list_redirects_to_login_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"

    def test_show_redirects_to_login_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/approvals/abc123")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals/abc123"

    def test_decide_is_401_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).post("/api/approvals/abc123/decide", json={"result": "deny"})
        assert r.status_code == 401

    def test_stream_is_401_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/api/approvals/stream")
        assert r.status_code == 401

    def test_preview_is_401_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/api/approvals/abc123/preview")
        assert r.status_code == 401


class TestApprovalPreview:
    """GET /api/approvals/{id}/preview -- the org-mode counterpart of
    web/routes_approvals.py's own preview fragment (Phase 1), scoped to
    current_principal() the same way every other read here is (§10.5)."""

    def test_returns_the_owning_principals_preview(self):
        app, sessions, web_ui = _app()
        with principal_scope(ALICE):
            approval, _ = web_ui.deferred_registry.register_or_coalesce(
                dedupe_key="a1", connector="gmail", tool="gmail_get_message", gate_kind="review",
                request_id="r1", preview={"From": "alice@example.com"},
            )
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/preview")
        assert r.status_code == 200
        assert r.json() == {"id": approval.id, "preview": {"From": "alice@example.com"}}

    def test_a_foreign_principals_approval_reads_as_a_plain_404(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get(f"/api/approvals/{approval.id}/preview")
        assert r.status_code == 404

    def test_unknown_id_is_404(self):
        app, sessions, _web_ui = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/api/approvals/nope/preview")
        assert r.status_code == 404

    def test_never_carries_details_text_or_html(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/preview")
        body = r.json()
        assert set(body.keys()) == {"id", "preview"}


class TestPrincipalScopedList:
    def test_only_shows_the_signed_in_principals_own_approvals(self):
        app, sessions, web_ui = _app()
        _register(web_ui, ALICE, dedupe_key="a1")
        _register(web_ui, BOB, dedupe_key="b1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert "a message" in r.text or "Get message" in r.text  # her own row rendered

    def test_show_approval_404s_a_foreign_principals_card(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "no longer pending" in r.text

    def test_show_approval_renders_the_owning_principals_card(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "CARD" in r.text

    def test_show_approval_of_an_unrendered_card_is_a_placeholder_not_a_500(self):
        # Regression: same defect as web/routes_approvals.py's own
        # show_approval -- card HTML is only built on gate.py's
        # _popup_executor, so a card whose worker hasn't run yet has
        # card.html == "". _inject_shim's html.index("</head>") raised
        # ValueError on that empty string; this must never 500.
        app, sessions, web_ui = _app()
        with principal_scope(ALICE):
            approval, _ = web_ui.deferred_registry.register_or_coalesce(
                dedupe_key="a1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
            )
        assert approval.html == ""
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "Preparing this request" in r.text

    def test_show_approval_wraps_the_webauthn_helper_js_in_a_script_tag(self):
        """Regression: _org_bridge_shim used to concatenate PF_WEBAUTHN_JS
        *before* its own <script> tag opened, so the helper functions
        (pfB64uToBuf/pfWebauthnCreate/pfWebauthnGet) landed in the document
        as literal visible text at the top of the rendered card instead of
        executing -- see routes_org_approvals.py's own _org_bridge_shim."""
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        body_start = r.text.index("<body>") + len("<body>")
        assert r.text[body_start : body_start + len("<script")] == "<script"
        assert "function pfB64uToBuf" not in r.text.split("<script", 1)[0]


class TestDecideWithoutStepUp:
    def test_deny_a_read_succeeds(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_a_foreign_principal_cannot_decide_it(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, BOB)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 409  # indistinguishable from "already decided"

    def test_wrong_csrf_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": "wrong"})
        assert r.status_code == 401

    def test_second_decision_is_already_decided(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 409

    def test_invalid_json_body_is_a_clean_400(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            f"/api/approvals/{approval.id}/decide", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_missing_result_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"csrf": session_id})
        assert r.status_code == 400

    def test_mismatched_origin_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403

    def test_numeric_choice_result_is_normalized_to_its_string_form(self):
        # dialog_window_html.build_choice_html's own JS posts a bare number
        # for `result` instead of a string -- see decide()'s own comment.
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": 1, "csrf": session_id})
        assert r.status_code == 200
        assert web_ui.deferred_registry.get(approval.id).result == "1"


class TestBatchDecide:
    """Phase 2 of the approval binder plan: the org-mode counterpart of
    test_routes_approvals.py's own TestBatchDecide -- same mechanics, with
    every read/write additionally scoped to current_principal() (module
    docstring, §10.5). No step-up yet -- that's Phase 3."""

    def test_a_mixed_batch_applies_each_item_and_reports_its_own_outcome(self):
        app, sessions, web_ui = _app()
        accept_me = _register(web_ui, ALICE, gate_kind="review", dedupe_key="k1")
        deny_me = _register(web_ui, ALICE, gate_kind="review", dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [
                {"id": accept_me.id, "result": "accept"},
                {"id": deny_me.id, "result": "deny"},
            ],
        })

        assert r.status_code == 200
        body = r.json()
        assert body["results"] == [
            {"id": accept_me.id, "outcome": "applied"},
            {"id": deny_me.id, "outcome": "applied"},
        ]
        assert accept_me.decided_via == "binder"
        assert accept_me.batch_id == deny_me.batch_id == body["batch_id"]

    def test_a_client_supplied_batch_id_is_never_recorded_verbatim(self):
        # B27: batch_id is documented (audit_log.py) as server-minted. With
        # no step-up in play there is never a live challenge to prove a
        # submitted batch_id against, so a forged one must be replaced
        # rather than trusted straight into the audit trail.
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
            "batch_id": "attacker-forged-batch-id",
        })

        assert r.status_code == 200
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]
        assert approval.batch_id != "attacker-forged-batch-id"

    def test_another_principals_id_reads_as_unknown_not_forbidden(self):
        # §10.5: indistinguishable from a nonexistent id -- never "exists
        # but you can't touch it".
        app, sessions, web_ui = _app()
        alices = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, BOB)

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": alices.id, "result": "accept"}],
        })

        assert r.status_code == 200
        assert r.json()["results"] == [{"id": alices.id, "outcome": "unknown"}]
        assert not alices.event.is_set()

    def test_unauthenticated_request_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": "whatever", "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 401

    def test_wrong_csrf_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": "wrong", "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 401
        assert not approval.event.is_set()

    def test_mismatched_origin_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/approvals/batch/decide",
            json={"csrf": session_id, "items": [{"id": approval.id, "result": "accept"}]},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert not approval.event.is_set()

    def test_oversize_batch_is_rejected_with_nothing_applied(self):
        registry = PendingApprovalRegistry(max_pending=3, max_pending_per_principal=3)
        app, sessions, web_ui = _app(web_ui=WebApprovalUI(registry=registry))
        approvals = [_register(web_ui, ALICE, gate_kind="review", dedupe_key=f"k{i}") for i in range(3)]
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        items = [{"id": a.id, "result": "accept"} for a in approvals] + [{"id": "extra", "result": "accept"}]

        r = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})

        assert r.status_code == 400
        assert all(not a.event.is_set() for a in approvals)

    def test_missing_items_is_rejected(self):
        app, sessions, web_ui = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": []})
        assert r.status_code == 400

    def test_an_invalid_item_result_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept_all"}],
        })
        assert r.status_code == 400
        assert not approval.event.is_set()


class TestStepUpScoping:
    """§10.6/D7: step-up is off by default, and even when enabled, applies
    only to approving decisions on writes (or PII reads, in the wider
    scope) -- never to denies."""

    def test_disabled_step_up_never_blocks_a_write_accept(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=False))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_deny_never_needs_step_up_even_on_a_write(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200

    def test_plain_read_never_needs_step_up_in_default_scope(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes"))
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_pii_read_needs_step_up_in_the_wider_scope(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_pii_reads"),
        )
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428

    def test_unflagged_read_needs_step_up_only_in_the_widest_scope(self):
        """``writes_and_reads`` means here exactly what it means in local
        mode (tests/unit/web/test_routes_approvals.py's own counterpart):
        one ``is_step_up_required`` serves both surfaces, so the only
        difference this pair should ever show is what a ``428`` offers --
        never whether one is due."""
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_pii_reads"),
        )
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=False)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_reads"),
        )
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=False)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428


class TestStepUpWebAuthnFlow:
    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_first_attempt_with_no_credential_offers_only_the_idp_link(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" not in body
        assert body["idp_stepup_url"].startswith(f"/api/approvals/{approval.id}/stepup/idp")

    def test_first_attempt_with_a_credential_offers_webauthn_options_too(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert "idp_stepup_url" in body

    def test_valid_assertion_completes_the_decision(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        first = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert first.status_code == 428

        fake_verified = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json() == {"status": "ok"}

    def test_an_assertion_for_a_different_decision_is_rejected(self):
        # Fingerprint binding (§10.6): a challenge minted for "accept"
        # cannot be reused to authorize "accept_all" on the same approval.
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        r = client.post(f"/api/approvals/{approval.id}/decide", json={
            "result": "accept_all", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400

    def test_a_failed_assertion_does_not_release_the_decision(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert r.status_code == 401
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored is not None
        assert not stored.event.is_set()  # decision was never released


class TestStepUpRequirePasskey:
    """#406: an org can close the IdP-reauth fallback entirely."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_no_credential_hard_fails_with_no_idp_fallback(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True),
        )
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored is not None
        assert not stored.event.is_set()

    def test_with_a_credential_offers_webauthn_options_but_no_idp_url(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True),
        )
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert "idp_stepup_url" not in body

    def test_a_valid_assertion_still_completes_the_decision(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True),
        )
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        first = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert first.status_code == 428

        fake_verified = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200

    def test_idp_stepup_start_is_refused_even_hit_directly(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True),
        )
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        assert r.status_code == 403


class TestIdpStepUp:
    def test_start_requires_a_signed_in_session(self):
        app, _sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        r = _client(app).get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login?next=")

    def test_start_rejects_an_invalid_result(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=deny&choice=")
        assert r.status_code == 400

    def test_start_redirects_to_the_idp_with_reauth_hints(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        assert r.status_code == 302
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["prompt"] == "login"
        assert qs["max_age"] == "0"

    def test_callback_with_an_unknown_state_is_a_clean_400(self):
        app, _sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get("/oauth/stepup/callback?code=abc&state=does-not-exist")
        assert r.status_code == 400

    def test_callback_with_an_idp_error_redirects_back_with_stepup_error(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?error=access_denied&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"

    def test_callback_completes_the_decision_for_the_same_principal(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        # A fresh, cookie-less client -- mirrors the real cross-site landing.
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == "/approvals?stepup=ok"
        # web_ui.resolve() only ever completes the UI-step (registry.answer())
        # -- gate.py's own _drive_interaction is what finalizes an approval in
        # production, and nothing here spins one up (this test registered the
        # approval directly, bypassing gate.py entirely, same as every other
        # decide test in this file and in test_routes_approvals.py's own
        # local-mode equivalent). event.is_set()/.result is the UI-step
        # outcome this route is actually responsible for.
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored.event.is_set()
        assert stored.result == "accept"

    def test_callback_redirects_to_error_when_the_idp_exchange_raises(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        def _boom(*a, **kw):
            raise RuntimeError("IdP unreachable")

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", _boom)
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()

    def test_callback_rejects_a_different_principal_re_authenticating(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "bob", "nonce": nonce},  # a different human signs in
        )
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()

    def test_callback_requires_configured_acr_values_when_set(self, monkeypatch):
        idp = _idp(step_up_acr_values=("phr",))
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"), idp=idp)
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))
        assert qs["acr_values"] == "phr"

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},  # no "acr" claim at all
        )
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()


class TestBatchStepUp:
    """Phase 3 of the approval binder plan: the org-mode counterpart of
    test_routes_approvals.py's own TestBatchStepUp -- same mechanics,
    scoped to current_principal() the same way every other read/write
    here is, and deliberately with no IdP-reauth fallback even here (see
    ``_batch_step_up_response``'s own docstring)."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _verified_assertion(self):
        return type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()

    def test_deny_only_batch_never_steps_up(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "deny"}],
            "batch_id": "attacker-forged-batch-id",
        })
        assert r.status_code == 200
        # B27: _batch_needs_step_up() never runs verify_step_up() here, so a
        # client-supplied batch_id must not survive into the audit trail.
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]

    def test_no_assertion_offers_a_428_with_no_idp_url(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert "idp_stepup_url" not in body
        assert body["batch_id"]
        assert not approval.event.is_set()

    def test_valid_assertion_applies_the_whole_batch(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        accept_me = _register(web_ui, ALICE, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, ALICE, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        items = [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        assert first.status_code == 428
        batch_id = first.json()["batch_id"]

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=self._verified_assertion()):
            second = client.post("/api/approvals/batch/decide", json={
                "csrf": session_id, "items": items, "batch_id": batch_id,
                "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json()["results"] == [
            {"id": accept_me.id, "outcome": "applied"}, {"id": deny_me.id, "outcome": "applied"},
        ]

    def test_a_failed_assertion_does_not_release_the_batch(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        items = [{"id": approval.id, "result": "accept"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        batch_id = first.json()["batch_id"]

        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post("/api/approvals/batch/decide", json={
                "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"}, "items": items,
            })
        assert r.status_code == 401
        assert not approval.event.is_set()

    def test_an_assertion_for_a_smaller_set_is_rejected(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        accept_me = _register(web_ui, ALICE, gate_kind="popup", dedupe_key="k1")
        extra = _register(web_ui, ALICE, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        first = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": accept_me.id, "result": "accept"}],
        })
        batch_id = first.json()["batch_id"]

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": extra.id, "result": "deny"}],
        })
        assert r.status_code == 400
        assert not accept_me.event.is_set()
        assert not extra.event.is_set()

    def test_another_principals_id_never_contributes_to_the_step_up_check(self):
        # §10.5: BOB's own accept doesn't need step-up (ALICE registered
        # it) and can't be applied by ALICE's batch either -- it must read
        # as plain "unknown", not somehow trigger or ride along with
        # ALICE's own ceremony.
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        bobs = _register(web_ui, BOB, gate_kind="popup", dedupe_key="k1")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": bobs.id, "result": "accept"}],
        })
        assert r.status_code == 200
        assert r.json()["results"] == [{"id": bobs.id, "outcome": "unknown"}]
        assert not bobs.event.is_set()

    def test_require_passkey_with_nothing_enrolled_hard_fails_the_whole_batch(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True),
        )
        accept_me = _register(web_ui, ALICE, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, ALICE, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}],
        })
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"
        assert not accept_me.event.is_set()
        assert not deny_me.event.is_set()

    def test_require_passkey_off_with_nothing_enrolled_lets_it_through_with_no_idp_link(self):
        # Deliberately different from this module's own single-decision
        # behavior (which would offer idp_stepup_url here instead) -- see
        # module docstring's own note on why the batch endpoint has no IdP
        # fallback at all.
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
            "batch_id": "attacker-forged-batch-id",
        })
        assert r.status_code == 200
        assert r.json()["results"] == [{"id": approval.id, "outcome": "applied"}]
        # B27: this fallthrough (no enrolled credential, require_passkey off)
        # never verified the assertion, so the client-supplied batch_id must
        # not reach the audit trail either.
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]

    def test_per_item_mode_refuses_the_batch_with_nothing_applied(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", batch="per_item"),
        )
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        accept_me = _register(web_ui, ALICE, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, ALICE, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}],
        })
        assert r.status_code == 400
        assert not accept_me.event.is_set()
        assert not deny_me.event.is_set()
