"""The Admin JSON API in organization mode: who may call it, how a change is gated, applied and
audited, and what the page state carries."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from privacyfence import admin_config, oauth_apps, paths, relay_client
from privacyfence import webauthn_stepup as wa
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_admin

BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)
SECRET = "SENTINEL-CLIENT-SECRET"
ENV_SECRET = "SENTINEL-ENV-VALUE"

BUNDLE = {"slack": {"client_id": "slack-id", "client_secret": SECRET}}

WEB_PUSH_OFF = {"enabled": False}
SCOPED_TOKENS = {"enabled": True, "max_days": 30, "allow_ai_proposals": False}
AUDIT_FORWARDING = {
    "enabled": False, "kind": "syslog", "syslog_host": "", "syslog_port": 514,
    "syslog_protocol": "udp", "http_url": "",
}
ACTION_BODIES = {
    "offer_service": {"service": "google"},
    "set_atlassian_assets": {"enabled": True, "workspace_id": "w1"},
    "set_server_address": {"service": "peek", "value": "https://peek.example.com"},
    "set_auto_provisioning": {"service": "peek", "enabled": False, "trust_unverified_email": True},
    "set_unattended_sessions": {"enabled": True},
    "set_download_delivery": {
        "inline_max_bytes": 1000, "link_ttl_seconds": 600, "allow_disk_staging": False,
        "agent_links": False, "allow_exports": False,
    },
    "set_web_push": {"enabled": False},
    "set_authz": {"allowed_domains": "example.com", "groups_claim": "", "required_groups": ""},
    "tighten_step_up": {"enabled": True, "require_passkey": True, "scope": "writes"},
    "set_scoped_tokens": SCOPED_TOKENS,
    "set_audit_forwarding": {
        "enabled": False, "kind": "syslog", "syslog_host": "logs.example.com", "syslog_port": 514,
        "syslog_protocol": "udp", "http_url": "",
    },
}


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


class Registry:
    def __init__(self):
        self.evicted = 0

    def evict_all(self):
        self.evicted += 1


class World:
    def __init__(self, *, step_up=None, registry=None):
        self.bundle = json.loads(json.dumps(BUNDLE))
        self.admin = admin_config.ensure(self.bundle, mode="org", single_user=False)[0]
        self.org_config = oauth_apps.effective_org_config(self.bundle, self.admin, mode="org")
        self.sessions = org_session.OrgSessionStore()
        self.registry = registry if registry is not None else Registry()
        self.routes = routes_admin.build_org_routes(
            sessions=self.sessions, step_up=step_up or StepUpConfig(rp_id="pf.example.com"),
            step_up_origin=BASE_URL, connector_registry=self.registry, org_config=self.org_config,
            bundle=self.bundle, startup_admin=self.admin,
        )
        self.client = TestClient(Starlette(routes=self.routes), base_url=BASE_URL, follow_redirects=False)

    def sign_in(self, principal):
        session_id = self.sessions.create(principal)
        self.client.cookies.set(org_session.SESSION_COOKIE, session_id)
        return session_id

    def post(self, action, body, csrf, **kwargs):
        return self.client.post(f"/api/admin/{action}", json={**body, "csrf": csrf}, **kwargs)


def _enrol(principal):
    wa.add_credential(principal, wa.WebAuthnCredential(
        credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
    ))


def _passkey_post(world, action, body, csrf):
    """The two requests a sensitive action takes: the challenge, then the resubmitted assertion."""
    first = world.post(action, body, csrf)
    assert first.status_code == 428, first.text
    verified = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()
    with patch.object(wa.webauthn, "verify_authentication_response", return_value=verified):
        return world.post(action, {**body, "webauthn_assertion": {"id": "Y3JlZC0x"}}, csrf)


@pytest.fixture
def audit(monkeypatch):
    recorded = []
    monkeypatch.setattr(routes_admin, "_record_settings_audit", lambda p, s: recorded.append((p.id, s)))
    return recorded


@pytest.fixture
def relay_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(relay_client, "request_check", lambda: calls.append(1))
    return calls


class TestAccess:
    def test_no_session_is_401(self):
        world = World()
        assert world.client.post("/api/admin/withdraw_service", json={}).json() == {"error": "Sign in first."}
        assert world.client.get("/api/admin/state").status_code == 401

    def test_a_non_admin_is_403(self):
        world = World()
        csrf = world.sign_in(ALICE)
        r = world.post("withdraw_service", {"service": "slack"}, csrf)
        assert (r.status_code, r.json()) == (403, {"error": "Only administrators can do this."})
        assert world.client.get("/api/admin/state").status_code == 403

    def test_an_unknown_action_is_404(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("set_salesforce_login_url", {"value": "x"}, csrf).status_code == 404
        assert world.post("not_an_action", {}, csrf).status_code == 404

    def test_a_privacy_action_is_not_here_yet(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("set_default_policy", {}, csrf).status_code == 404

    def test_bad_csrf_origin_and_json(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("clear_rooms", {}, "wrong").status_code == 401
        assert world.post("clear_rooms", {}, csrf, headers={"Origin": "https://evil.example"}).status_code == 403
        assert world.client.post("/api/admin/clear_rooms", content=b"{").status_code == 400
        assert world.client.post("/api/admin/clear_rooms", json=[1]).status_code == 401


class TestRequests:
    def test_unknown_keys_are_refused(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        r = world.post("withdraw_service", {"service": "slack", "bundle": {"slack": {}}}, csrf)
        assert r.status_code == 400
        assert world.org_config["slack"]["client_id"] == "slack-id"

    def test_a_body_carrying_the_bundle_is_refused(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("clear_rooms", {"bundle": {}, "mode": "local"}, csrf).status_code == 400

    def test_a_missing_or_mistyped_parameter_is_400(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("withdraw_service", {}, csrf).json() == {"error": "Missing service."}
        wrong = world.post("withdraw_service", {"service": 5}, csrf)
        assert wrong.status_code == 400 and "service" in wrong.json()["error"]

    def test_an_admin_config_error_is_400_with_its_message(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        _enrol(ADMIN)
        r = _passkey_post(world, "set_scoped_tokens", {**SCOPED_TOKENS, "max_days": 9999}, csrf)
        assert (r.status_code, r.json()) == (400, {"error": "Scoped tokens can last from 1 to 365 days."})

    def test_a_failed_write_is_500(self, monkeypatch):
        world = World()
        csrf = world.sign_in(ADMIN)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(routes_admin.admin_config, "apply_change", boom)
        r = world.post("withdraw_service", {"service": "slack"}, csrf)
        assert (r.status_code, r.json()) == (500, {"error": "Could not save the admin configuration."})


class TestStepUp:
    def test_every_sensitive_action_is_gated_on_the_default_configuration(self, audit):
        # The default organization step-up configuration is off; the Admin page asks anyway.
        for action, body in ACTION_BODIES.items():
            world = World()
            _enrol(ADMIN)
            csrf = world.sign_in(ADMIN)
            r = world.post(action, body, csrf)
            assert r.status_code == 428, action
            assert "webauthn_options" in r.json()
        assert all("Step-up required for" in summary for _, summary in audit)
        assert len(audit) == len(ACTION_BODIES)

    def test_no_passkey_gets_the_enrolment_message(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        r = world.post("set_web_push", {"enabled": False}, csrf)
        assert (r.status_code, r.json()) == (
            403, {"error": "Add a passkey on /security first.", "enroll_url": "/security"},
        )

    def test_withdraw_and_clear_rooms_need_no_passkey(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert world.post("withdraw_service", {"service": "slack"}, csrf).status_code == 200
        assert world.post("clear_rooms", {}, csrf).status_code == 200

    def test_an_assertion_for_one_body_does_not_authorize_another(self):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        assert world.post("set_web_push", {"enabled": False}, csrf).status_code == 428
        verified = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=verified):
            r = world.post("set_web_push", {"enabled": True, "webauthn_assertion": {"id": "Y3JlZC0x"}}, csrf)
        assert r.status_code != 200


class TestApplying:
    def test_withdraw_service_refreshes_the_shared_config_and_connectors(self, relay_checks):
        world = World()
        csrf = world.sign_in(ADMIN)
        shared = world.org_config
        assert "slack" in shared
        r = world.post("withdraw_service", {"service": "slack"}, csrf)
        assert r.status_code == 200
        assert world.org_config is shared and "slack" not in shared
        assert world.registry.evicted == 1
        assert relay_checks == [1]
        slack = next(row for row in r.json()["services"] if row["key"] == "slack")
        assert slack["offered"] is False

    def test_clear_rooms_refreshes_without_asking_the_relay(self, relay_checks):
        world = World()
        csrf = world.sign_in(ADMIN)
        admin = admin_config.load()
        admin["rooms"], admin["rooms_synced_at"] = [{"resource_email": "r@example.com"}], "2026-01-01"
        admin_config.save(admin)
        assert world.client.get("/api/admin/state").json()["rooms"]["count"] == 1
        assert world.post("clear_rooms", {}, csrf).json()["rooms"] == {"count": 0, "synced_at": ""}
        assert world.registry.evicted == 1 and relay_checks == []

    def test_a_server_without_a_registry_still_refreshes(self):
        world = World()
        routes = routes_admin.build_org_routes(
            sessions=world.sessions, step_up=StepUpConfig(rp_id="pf.example.com"), step_up_origin=BASE_URL,
            connector_registry=None, org_config=world.org_config, bundle=world.bundle, startup_admin=world.admin,
        )
        client = TestClient(Starlette(routes=routes), base_url=BASE_URL)
        csrf = world.sessions.create(ADMIN)
        client.cookies.set(org_session.SESSION_COOKIE, csrf)
        r = client.post("/api/admin/withdraw_service", json={"service": "slack", "csrf": csrf})
        assert r.status_code == 200 and "slack" not in world.org_config

    def test_a_restart_action_saves_and_leaves_the_shared_config_alone(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        _enrol(ADMIN)
        before = dict(world.org_config)
        r = _passkey_post(world, "set_web_push", WEB_PUSH_OFF, csrf)
        assert r.status_code == 200
        assert world.org_config == before
        assert world.registry.evicted == 0
        assert world.client.get("/api/admin/state").json()["restart_needed"] == ["Web push"]
        assert admin_config.load()["web_push"]["enabled"] is False

    def test_auto_provisioning_has_a_restart_label(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        _enrol(ADMIN)
        with patch.object(admin_config, "_auto_provision_state", return_value=(True, "")):
            r = _passkey_post(
                world, "set_auto_provisioning",
                {"service": "peek", "enabled": True, "trust_unverified_email": False}, csrf,
            )
        assert r.status_code == 200
        assert r.json()["restart_needed"] == ["Automatic accounts"]


class TestAudit:
    def test_a_change_is_audited_once_with_the_admin(self, audit):
        world = World()
        csrf = world.sign_in(ADMIN)
        world.post("withdraw_service", {"service": "slack"}, csrf)
        assert audit == [("carol", "Stopped offering Slack (admin=carol)")]

    def test_a_no_op_is_not_audited_or_refreshed(self, audit, relay_checks):
        world = World()
        csrf = world.sign_in(ADMIN)
        world.post("withdraw_service", {"service": "slack"}, csrf)
        audit.clear()
        evicted = world.registry.evicted
        assert world.post("withdraw_service", {"service": "slack"}, csrf).status_code == 200
        assert audit == [] and world.registry.evicted == evicted

    def test_local_mode_audits_as_the_local_principal(self, audit):
        world = World()
        r = routes_admin._dispatch(
            ADMIN, "withdraw_service", {"service": "slack"}, mode="local", bundle=world.bundle,
            org_config=world.org_config, startup_admin=world.admin, step_up=StepUpConfig(rp_id="x"),
            step_up_origin=BASE_URL, challenges=wa.StepUpChallengeStore(), refresh_live=lambda changed: None,
        )
        assert r.status_code == 200
        assert audit == [("carol", "Stopped offering Slack (principal=local)")]

    def test_an_org_only_action_is_unknown_in_local_mode(self):
        world = World()
        r = routes_admin._dispatch(
            ADMIN, "set_web_push", {"enabled": False}, mode="local", bundle=world.bundle,
            org_config=world.org_config, startup_admin=world.admin, step_up=StepUpConfig(rp_id="x"),
            step_up_origin=BASE_URL, challenges=wa.StepUpChallengeStore(), refresh_live=lambda changed: None,
        )
        assert r.status_code == 404


class TestState:
    def test_state_never_carries_a_secret(self, monkeypatch):
        monkeypatch.setenv("SENTINEL_BEARER", ENV_SECRET)
        world = World()
        admin = admin_config.load()
        admin["audit_forwarding"]["http"] = {"url": "https://logs.example.com", "bearer_token_env": "SENTINEL_BEARER"}
        admin_config.save(admin)
        world.sign_in(ADMIN)
        text = world.client.get("/api/admin/state").text
        assert SECRET not in text and ENV_SECRET not in text
        assert "SENTINEL_BEARER" in text

    def test_state_shape(self):
        world = World()
        world.sign_in(ADMIN)
        state = world.client.get("/api/admin/state").json()
        assert {"services", "rooms", "unattended_sessions", "download_delivery", "web_push", "authz",
                "step_up", "scoped_tokens", "audit_forwarding", "restart_needed", "relay_status"} <= set(state)
        assert "privacy" not in state and "stats" not in state
        assert state["restart_needed"] == []

    def test_a_section_the_startup_copy_lacks_is_not_compared(self):
        assert routes_admin._restart_needed({"web_push": {"enabled": True}}, {}) == []


class TestRouteClassification:
    def test_the_dispatcher_is_classified(self):
        assert routes_admin.ROUTE_CLASSIFICATION["/api/admin/{action}"] == "action-dispatcher"

    def test_an_unclassified_post_route_fails_the_build(self):
        async def handler(request):
            raise AssertionError("never called")

        with pytest.raises(RuntimeError, match="ROUTE_CLASSIFICATION"):
            routes_admin._check_classified([Route("/api/admin/upload", handler, methods=["POST"])])

    def test_every_action_declares_its_parameters(self):
        assert set(routes_admin.ADMIN_ACTIONS) == set(routes_admin.ADMIN_PARAMS)

    def test_the_registry_type_is_the_real_one(self):
        assert hasattr(ConnectorRegistry, "evict_all")
