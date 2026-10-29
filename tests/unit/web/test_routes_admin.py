"""The Admin JSON API in organization mode: who may call it, how a change is gated, applied and
audited, and what the page state carries."""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
import yaml
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from privacyfence import admin_config, daemon_main, data_format, oauth_apps, paths, privacy_filter, relay_client
from privacyfence import webauthn_stepup as wa
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal, principal_scope
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, org_stats, routes_admin

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
    "toggle_pii_detection": {},
    "toggle_pii_category": {"category_key": "detect_ip_addresses"},
    "set_default_policy": {"group": "privacy", "policy": "redact"},
    "set_category_policy": {"group": "privacy", "category": "body", "policy": "block"},
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
    def __init__(self, *, step_up=None, registry=None, settings=None, settings_path=""):
        self.settings = settings if settings is not None else {}
        self.bundle = json.loads(json.dumps(BUNDLE))
        self.admin = admin_config.ensure(self.bundle, mode="org", single_user=False)[0]
        self.org_config = oauth_apps.effective_org_config(self.bundle, self.admin, mode="org")
        self.sessions = org_session.OrgSessionStore()
        self.registry = registry if registry is not None else Registry()
        self.routes = routes_admin.build_org_routes(
            sessions=self.sessions, step_up=step_up or StepUpConfig(rp_id="pf.example.com"),
            step_up_origin=BASE_URL, connector_registry=self.registry, org_config=self.org_config,
            bundle=self.bundle, startup_admin=self.admin,
            install_wide_settings=self.settings, install_wide_settings_path=settings_path,
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
            install_wide_settings=world.settings,
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
        assert {"privacy", "stats"} <= set(state)
        assert state["restart_needed"] == []

    def test_state_carries_the_upgrade_readiness(self, monkeypatch):
        monkeypatch.setitem(data_format._status, "ready", False)
        monkeypatch.setitem(data_format._status, "problems", ["alice: settings.yaml cannot be read"])
        world = World()
        world.sign_in(ADMIN)
        state = world.client.get("/api/admin/state").json()
        assert state["upgrade_ready"] == {"ready": False, "problems": ["alice: settings.yaml cannot be read"]}

    def test_a_section_the_startup_copy_lacks_is_not_compared(self):
        assert routes_admin._restart_needed({"web_push": {"enabled": True}}, {}) == []


def _write_settings(tmp_path, settings):
    path = tmp_path / "install-settings.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return path


class TestPrivacyActions:
    """The install-wide privacy policy and PII gate, which an organization's administrator changes
    here rather than on Settings."""

    @staticmethod
    def _editable(tmp_path, settings):
        path = _write_settings(tmp_path, settings)
        world = World(settings=settings, settings_path=str(path))
        _enrol(ADMIN)
        return world, world.sign_in(ADMIN), path

    def test_an_admin_sets_a_group_default_policy(self, tmp_path, audit):
        world, csrf, path = self._editable(tmp_path, {"privacy": {"default_policy": "block"}})
        r = _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "redact"}, csrf)
        assert r.status_code == 200
        assert world.settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load(path.read_text())["privacy"]["default_policy"] == "redact"
        assert r.json()["privacy"]["default_policy"]["privacy"] == "redact"
        assert audit[-1][0] == "carol" and "redact" in audit[-1][1] and audit[-1][1].endswith("(admin=carol)")

    def test_an_admin_sets_a_category_policy(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {"privacy": {"default_policy": "allow"}})
        r = _passkey_post(world, "set_category_policy", {"group": "privacy", "category": "body", "policy": "block"}, csrf)
        assert r.status_code == 200
        assert world.settings["privacy"]["categories"] == {"body": "block"}

    def test_an_admin_toggles_the_pii_gate(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {"pii_detection": {"enabled": True}})
        # No `enabled` field: the page's switch posts an empty body and the server works out the flip.
        r = _passkey_post(world, "toggle_pii_detection", {}, csrf)
        assert r.status_code == 200
        assert world.settings["pii_detection"]["enabled"] is False
        assert r.json()["privacy"]["pii_enabled"] is False

    def test_an_admin_toggles_a_pii_category(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {"pii_detection": {"enabled": True}})
        r = _passkey_post(world, "toggle_pii_category", {"category_key": "detect_ip_addresses"}, csrf)
        assert r.status_code == 200
        assert world.settings["pii_detection"]["detect_ip_addresses"] is False
        assert r.json()["privacy"]["pii_ip"] is False

    def test_a_client_supplied_enabled_value_is_not_a_parameter(self, tmp_path):
        # The toggle always flips the current value; a request cannot pin it.
        world, csrf, _path = self._editable(tmp_path, {"pii_detection": {"enabled": True}})
        r = world.post("toggle_pii_detection", {"enabled": True}, csrf)
        assert r.status_code == 400
        assert world.settings["pii_detection"]["enabled"] is True

    def test_a_non_admin_cannot_write_even_with_a_valid_csrf(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        world = World(settings=settings, settings_path=str(_write_settings(tmp_path, settings)))
        csrf = world.sign_in(ALICE)
        r = world.post("set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)
        assert r.status_code == 403
        assert settings["privacy"]["default_policy"] == "block"

    def test_wrong_csrf_and_cross_origin_are_rejected(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {"privacy": {"default_policy": "block"}})
        body = {"group": "privacy", "policy": "allow"}
        assert world.post("set_default_policy", body, "not-the-session").status_code == 401
        r = world.post("set_default_policy", body, csrf, headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 403
        assert world.settings["privacy"]["default_policy"] == "block"

    def test_a_passkey_is_needed_and_nothing_is_written_without_one(self, tmp_path):
        settings = {"privacy": {"default_policy": "allow"}, "pii_detection": {"enabled": True}}
        path = _write_settings(tmp_path, settings)
        world = World(settings=settings, settings_path=str(path))
        csrf = world.sign_in(ADMIN)
        for action in ("set_default_policy", "set_category_policy", "toggle_pii_detection", "toggle_pii_category"):
            r = world.post(action, ACTION_BODIES[action], csrf)
            assert (r.status_code, r.json()) == (
                403, {"error": "Add a passkey on /security first.", "enroll_url": "/security"},
            )
        assert settings == {"privacy": {"default_policy": "allow"}, "pii_detection": {"enabled": True}}
        assert yaml.safe_load(path.read_text()) == settings

    def test_a_bad_payload_is_a_400(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {})
        r = _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "nope"}, csrf)
        assert r.status_code == 400

    def test_a_server_without_a_settings_path_refuses(self, tmp_path):
        world = World(settings={})
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        r = _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)
        assert r.status_code == 400 and "settings.yaml path" in r.json()["error"]

    def test_an_unwritable_settings_yaml_is_a_500_with_the_policy_unchanged(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        unwritable = tmp_path / "install-settings.yaml"
        unwritable.mkdir()  # a directory where the file should be: the write raises
        world = World(settings=settings, settings_path=str(unwritable))
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        r = _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)
        assert r.status_code == 500
        assert settings["privacy"]["default_policy"] == "block"

    def test_the_change_is_live_for_another_principal_immediately(self, tmp_path):
        world, csrf, _path = self._editable(tmp_path, {"privacy": {"default_policy": "allow"}})
        with principal_scope(ALICE):
            privacy_filter.init_privacy_filter(world.settings, org_managed=True)
            assert privacy_filter.category_policy("privacy", "body") == "allow"
        _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "block"}, csrf)
        with principal_scope(ALICE):
            assert privacy_filter.category_policy("privacy", "body") == "block"

    def test_a_privacy_action_does_not_touch_connectors_or_the_relay(self, tmp_path, relay_checks):
        world, csrf, _path = self._editable(tmp_path, {})
        _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "block"}, csrf)
        assert world.registry.evicted == 0 and relay_checks == []

    def test_a_privacy_action_is_unknown_in_local_mode(self):
        world = World()
        r = routes_admin._dispatch(
            ADMIN, "set_default_policy", {"group": "privacy", "policy": "allow"}, mode="local", bundle=world.bundle,
            org_config=world.org_config, startup_admin=world.admin, step_up=StepUpConfig(rp_id="x"),
            step_up_origin=BASE_URL, challenges=wa.StepUpChallengeStore(), refresh_live=lambda changed: None,
        )
        assert r.status_code == 404


class TestPrivacyAndStatsState:
    def test_privacy_rows_read_the_install_wide_settings(self, tmp_path):
        world = World(settings={"privacy": {"default_policy": "allow"}})
        world.sign_in(ADMIN)
        privacy = world.client.get("/api/admin/state").json()["privacy"]
        assert privacy["default_policy"]["privacy"] == "allow"
        # A group the file does not mention is the fail-closed default an organization enforces.
        assert privacy["default_policy"]["drive_privacy"] == "block"
        assert (privacy["pii_enabled"], privacy["pii_ip"], privacy["pii_financial"]) == (True, True, True)

    def test_calendar_and_the_gmail_signature_are_left_out(self):
        # toggle_calendar_free_busy and toggle_gmail_signature are local-mode settings.
        world = World()
        world.sign_in(ADMIN)
        privacy = world.client.get("/api/admin/state").json()["privacy"]
        assert all(group["key"] != "calendar" for group in privacy["groups"])
        assert "gmail_append_signature" not in privacy

    def test_stats_count_every_principal(self, tmp_path):
        for name in ("carol", "alice"):
            (tmp_path / "users" / name / "authority" / "config").mkdir(parents=True)
        (tmp_path / "users" / "alice" / "credentials").mkdir()
        (tmp_path / "users" / "alice" / "credentials" / "slack_token.json").write_text("{}", encoding="utf-8")
        world = World()
        world.sign_in(ADMIN)
        stats = world.client.get("/api/admin/state").json()["stats"]
        assert stats["users"]["total"] == 2 and stats["users"]["with_connectors"] == 1
        assert {"service": "slack", "label": "Slack", "configured": True, "users": 1} in stats["connectors"]

    def test_stats_read_an_independent_copy_of_the_shared_config(self, monkeypatch):
        seen = []
        monkeypatch.setattr(org_stats, "collect", lambda config: seen.append(config) or org_stats.empty())
        world = World()
        world.sign_in(ADMIN)
        world.client.get("/api/admin/state")
        assert len(seen) == 1 and seen[0] == world.org_config and seen[0] is not world.org_config

    def test_a_non_admin_gets_neither(self):
        world = World()
        world.sign_in(ALICE)
        r = world.client.get("/api/admin/state")
        assert r.status_code == 403 and "stats" not in r.text and "privacy" not in r.text

    def test_a_response_to_an_action_carries_the_new_privacy_rows(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        world = World(settings=settings, settings_path=str(_write_settings(tmp_path, settings)))
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        r = _passkey_post(world, "set_default_policy", {"group": "privacy", "policy": "redact"}, csrf)
        assert "stats" in r.json() and r.json()["privacy"]["default_policy"]["privacy"] == "redact"


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


# --------------------------------------------------------------------------- #
# The room directory upload, organization mode
# --------------------------------------------------------------------------- #

ROOMS = {"rooms": [{"resource_email": "r1@example.com", "name": "Aspen"}], "rooms_synced_at": "2026-01-01T00:00:00Z"}
ROOMS_BYTES = json.dumps(ROOMS).encode()
VERIFIED = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()


def _upload(client, url, csrf, raw, *, assertion=None, **fields):
    data = {"csrf": csrf, **fields}
    if assertion is not None:
        data["webauthn_assertion"] = json.dumps(assertion)
    return client.post(url, data=data, files={"file": ("rooms.json", raw, "application/json")})


def _passkey_upload(client, url, csrf, raw, **fields):
    first = _upload(client, url, csrf, raw, **fields)
    assert first.status_code == 428, first.text
    with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
        return _upload(client, url, csrf, raw, assertion={"id": "Y3JlZC0x"}, **fields)


class TestOrgRoomsUpload:
    URL = "/api/admin/rooms/upload"

    def test_it_is_classified_as_bespoke_sensitive(self):
        assert routes_admin.ROUTE_CLASSIFICATION[self.URL] == "bespoke-sensitive"

    def test_no_session_and_non_admin(self):
        world = World()
        assert _upload(world.client, self.URL, "x", ROOMS_BYTES).status_code == 401
        csrf = world.sign_in(ALICE)
        assert _upload(world.client, self.URL, csrf, ROOMS_BYTES).status_code == 403

    def test_csrf_and_origin_are_checked(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert _upload(world.client, self.URL, "wrong", ROOMS_BYTES).status_code == 401
        r = world.client.post(
            self.URL, data={"csrf": csrf}, files={"file": ("r.json", ROOMS_BYTES, "application/json")},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_passkey_is_always_needed(self, audit):
        world = World()
        csrf = world.sign_in(ADMIN)
        r = _upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert (r.status_code, r.json()) == (
            403, {"error": "Add a passkey on /security first.", "enroll_url": "/security"},
        )
        assert admin_config.load()["rooms"] == []
        assert audit == [("carol", "Step-up required for 'upload_rooms', refused (principal=carol)")]

    def test_it_replaces_the_rooms_with_step_up(self, audit):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        r = _passkey_upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert r.status_code == 200
        assert r.json()["rooms"] == {"count": 1, "synced_at": "2026-01-01T00:00:00Z"}
        assert admin_config.load()["rooms"] == ROOMS["rooms"]
        assert audit[-1] == ("carol", "Replaced the room directory (1 rooms) (admin=carol)")
        assert world.registry.evicted == 1

    def test_the_assertion_is_bound_to_the_file(self):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        assert _upload(world.client, self.URL, csrf, ROOMS_BYTES).status_code == 428
        other = json.dumps({"rooms": [{"resource_email": "other@example.com"}]}).encode()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
            r = _upload(world.client, self.URL, csrf, other, assertion={"id": "Y3JlZC0x"})
        assert r.status_code != 200
        assert admin_config.load()["rooms"] == []

    @pytest.mark.parametrize("raw", [
        b"not json", b"[]", b'{"rooms": "x"}', b'{"rooms": [{"name": "no email"}]}',
        b'{"rooms": [{"resource_email": "a@b", "name": {"nested": 1}}]}',
    ])
    def test_a_file_that_is_not_a_room_directory_is_refused(self, raw):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        first = _upload(world.client, self.URL, csrf, raw)
        if first.status_code == 428:
            with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
                first = _upload(world.client, self.URL, csrf, raw, assertion={"id": "Y3JlZC0x"})
        assert first.status_code == 400
        assert first.json() == {"error": "The file is not a room directory written by sync_room_directory.py."}
        assert admin_config.load()["rooms"] == []

    def test_a_file_over_2_mb_is_refused(self):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)
        big = b'{"rooms": [], "pad": "' + b"x" * routes_admin.MAX_ROOMS_UPLOAD_BYTES + b'"}'
        r = _upload(world.client, self.URL, csrf, big)
        assert (r.status_code, r.json()) == (400, {"error": "The file is larger than 2 MB."})

    def test_a_missing_file_is_400(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        r = world.client.post(self.URL, data={"csrf": csrf})
        assert r.status_code == 400

    def test_a_failed_write_is_500(self, monkeypatch):
        world = World()
        _enrol(ADMIN)
        csrf = world.sign_in(ADMIN)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(routes_admin.admin_config, "replace_rooms", boom)
        r = _passkey_upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert (r.status_code, r.json()) == (500, {"error": "Could not save the admin configuration."})

    def test_org_mode_has_no_bundle_upload(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        assert _upload(world.client, "/api/admin/org_config/upload", csrf, b"{}").status_code == 404


# --------------------------------------------------------------------------- #
# Local mode
# --------------------------------------------------------------------------- #

LOCAL_BUNDLE = {"slack": {"client_id": "slack-id", "client_secret": SECRET}}
LOCAL_ORIGIN = "http://localhost"


class LocalWorld:
    def __init__(self, controller, monkeypatch, *, step_up=None, require_human_session=False):
        from privacyfence.web.session_auth import LocalSessionStore

        self.bundle = json.loads(json.dumps(LOCAL_BUNDLE))
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: self.bundle)
        self.admin = admin_config.ensure(self.bundle, mode="local", single_user=False)[0]
        self.org_config = oauth_apps.effective_org_config(self.bundle, self.admin, mode="local")
        self.controller = controller
        self.refreshed = 0

        def refreshed():
            self.refreshed += 1
            return {}

        monkeypatch.setattr(controller, "refresh_connectors", refreshed)
        self.sessions = LocalSessionStore()
        self.routes = routes_admin.build_routes(
            controller=controller, sessions=self.sessions,
            step_up=step_up or StepUpConfig(rp_id="localhost"), step_up_origin=LOCAL_ORIGIN,
            require_human_session=require_human_session, startup_admin=self.admin, org_config=self.org_config,
        )
        self.client = TestClient(Starlette(routes=self.routes), base_url=LOCAL_ORIGIN)

    def sign_in(self, provenance=None):
        from privacyfence.web.session_auth import PROVENANCE_HUMAN, SESSION_COOKIE

        session_id = self.sessions.create(provenance=provenance or PROVENANCE_HUMAN)
        self.client.cookies.set(SESSION_COOKIE, session_id)
        return session_id

    def post(self, action, body, csrf, **kwargs):
        return self.client.post(f"/api/admin/{action}", json={**body, "csrf": csrf}, **kwargs)


@pytest.fixture
def local_controller(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from privacyfence import resource_names, update_checker
    from privacyfence import settings_controller as sc

    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")
    monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "update_check_cache.json")
    monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
    org_dir = tmp_path / "org"
    org_dir.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\nconnectors: {}\n", encoding="utf-8")
    return sc.SettingsController(
        str(config_path), connectors=[], connector_host=SimpleNamespace(set_connectors=lambda conns: None),
    )


def _enrol_local():
    from privacyfence.principal import LOCAL_PRINCIPAL

    _enrol(LOCAL_PRINCIPAL)


class TestLocalAccess:
    def test_no_session_is_refused(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        assert world.client.get("/api/admin/state").status_code == 401
        assert world.client.post("/api/admin/withdraw_service", json={"service": "slack"}).status_code == 401

    def test_org_only_and_unknown_actions_are_404(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        for action in ("set_web_push", "set_authz", "set_auto_provisioning", "not_an_action", "set_default_policy"):
            assert world.post(action, {}, csrf).status_code == 404, action

    def test_csrf_and_origin_are_checked(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        assert world.post("clear_rooms", {}, "wrong").status_code == 401
        assert world.post("clear_rooms", {}, csrf, headers={"Origin": "https://evil.example"}).status_code == 403
        assert world.client.post("/api/admin/clear_rooms", content=b"{").status_code == 400

    def test_unknown_body_keys_are_refused(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        assert world.post("withdraw_service", {"service": "slack", "bundle": {}}, csrf).status_code == 400

    def test_the_state_carries_the_controller_error_and_no_secret(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        world.sign_in()
        local_controller.error = "something to read"
        r = world.client.get("/api/admin/state")
        assert r.json()["error"] == "something to read"
        assert SECRET not in r.text
        assert "download_delivery" not in r.json() and "relay_status" not in r.json()
        assert r.json()["upgrade_ready"] == data_format.status()


class TestLocalHumanSession:
    def test_an_unattested_session_cannot_make_a_sensitive_change(self, local_controller, monkeypatch):
        from privacyfence.web.session_auth import PROVENANCE_UNATTESTED

        world = LocalWorld(local_controller, monkeypatch, require_human_session=True)
        csrf = world.sign_in(PROVENANCE_UNATTESTED)
        r = world.post("set_unattended_sessions", {"enabled": True}, csrf)
        assert (r.status_code, r.json()["error"]) == (403, "human_session_required")

    def test_a_non_sensitive_change_needs_no_human_session(self, local_controller, monkeypatch):
        from privacyfence.web.session_auth import PROVENANCE_UNATTESTED

        world = LocalWorld(local_controller, monkeypatch, require_human_session=True)
        csrf = world.sign_in(PROVENANCE_UNATTESTED)
        assert world.post("withdraw_service", {"service": "slack"}, csrf).status_code == 200

    def test_an_attested_session_reaches_the_passkey_step(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch, require_human_session=True)
        _enrol_local()
        csrf = world.sign_in()
        assert world.post("set_unattended_sessions", {"enabled": True}, csrf).status_code == 428


class TestLocalApplying:
    def test_offer_service_saves_refreshes_the_startup_dict_and_the_connectors(
        self, local_controller, monkeypatch, audit, relay_checks
    ):
        world = LocalWorld(local_controller, monkeypatch)
        _enrol_local()
        csrf = world.sign_in()
        shared = world.org_config
        assert world.post("withdraw_service", {"service": "slack"}, csrf).status_code == 200
        assert "slack" not in shared and world.refreshed == 1
        r = _local_passkey_post(world, "offer_service", {"service": "slack"}, csrf)
        assert r.status_code == 200
        assert world.org_config is shared and shared["slack"]["client_id"] == "slack-id"
        assert world.refreshed == 2
        assert admin_config.load()["services"]["slack"]["offered"] is True
        assert audit[-1] == ("local", "Offered Slack (principal=local)")
        assert relay_checks == []

    def test_a_sensitive_change_needs_a_passkey_whatever_step_up_says(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        r = world.post("set_unattended_sessions", {"enabled": True}, csrf)
        assert (r.status_code, r.json()) == (
            403, {"error": "Add a passkey on /security first.", "enroll_url": "/security"},
        )

    def test_a_restart_action_is_listed_until_the_restart(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        _enrol_local()
        csrf = world.sign_in()
        r = _local_passkey_post(world, "set_unattended_sessions", {"enabled": True}, csrf)
        assert r.status_code == 200
        assert r.json()["restart_needed"] == ["Unattended sessions"]
        assert world.refreshed == 0

    def test_a_bundle_that_fails_to_load_reads_as_empty(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        world.sign_in()

        def broken():
            raise daemon_main.org_mode.ConfigurationError("broken")

        monkeypatch.setattr(daemon_main, "load_org_config", broken)
        assert world.client.get("/api/admin/state").status_code == 200


def _local_passkey_post(world, action, body, csrf):
    first = world.post(action, body, csrf)
    assert first.status_code == 428, first.text
    with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
        return world.post(action, {**body, "webauthn_assertion": {"id": "Y3JlZC0x"}}, csrf)


class TestLocalRoomsUpload:
    URL = "/api/admin/rooms/upload"

    def test_it_replaces_the_rooms_with_step_up(self, local_controller, monkeypatch, audit):
        world = LocalWorld(local_controller, monkeypatch)
        _enrol_local()
        csrf = world.sign_in()
        r = _passkey_upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert r.status_code == 200
        assert r.json()["rooms"]["count"] == 1 and r.json()["error"] == ""
        assert audit[-1] == ("local", "Replaced the room directory (1 rooms) (principal=local)")
        assert world.refreshed == 1

    def test_a_passkey_is_always_needed(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        r = _upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert (r.status_code, r.json()["error"]) == (403, "Add a passkey on /security first.")

    def test_it_refuses_without_a_human_session(self, local_controller, monkeypatch):
        from privacyfence.web.session_auth import PROVENANCE_UNATTESTED

        world = LocalWorld(local_controller, monkeypatch, require_human_session=True)
        csrf = world.sign_in(PROVENANCE_UNATTESTED)
        r = _upload(world.client, self.URL, csrf, ROOMS_BYTES)
        assert (r.status_code, r.json()["error"]) == (403, "human_session_required")
        assert admin_config.load()["rooms"] == []

    def test_no_session_and_bad_csrf(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        assert _upload(world.client, self.URL, "x", ROOMS_BYTES).status_code == 401
        world.sign_in()
        assert _upload(world.client, self.URL, "wrong", ROOMS_BYTES).status_code == 401

    def test_a_non_room_file_and_a_large_file_are_refused(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        _enrol_local()
        csrf = world.sign_in()
        refused = _passkey_upload(world.client, self.URL, csrf, b"[1, 2]")
        assert (refused.status_code, refused.json()["error"]) == (
            400, "The file is not a room directory written by sync_room_directory.py.",
        )
        assert _upload(world.client, self.URL, csrf, b"not json").status_code == 400
        big = b'{"rooms": [], "pad": "' + b"x" * routes_admin.MAX_ROOMS_UPLOAD_BYTES + b'"}'
        assert _upload(world.client, self.URL, csrf, big).json() == {"error": "The file is larger than 2 MB."}


class TestLocalBundleUpload:
    URL = "/api/admin/org_config/upload"

    def _signed(self):
        from privacyfence import org_bundle_signing

        private_key, _ = org_bundle_signing.generate_keypair()
        return json.dumps(org_bundle_signing.sign_bundle({"version": 1, "org_name": "Acme"}, private_key)).encode()

    def test_a_first_signed_bundle_needs_confirmation_before_step_up(self, local_controller, monkeypatch):
        from privacyfence import settings_controller as sc

        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        r = _upload(world.client, self.URL, csrf, self._signed())
        assert (r.status_code, r.json()["error"]) == (409, "pin_confirmation_required")
        assert not (sc.org_dir() / "org_bundle.json").exists()

    def test_a_passkey_is_always_needed(self, local_controller, monkeypatch):
        from privacyfence import settings_controller as sc

        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        r = _upload(world.client, self.URL, csrf, b'{"version": 1}')
        assert (r.status_code, r.json()["error"]) == (403, "Add a passkey on /security first.")
        assert not (sc.org_dir() / "org_bundle.json").exists()

    def test_it_installs_with_step_up_and_answers_with_the_admin_state(self, local_controller, monkeypatch):
        from privacyfence import settings_controller as sc

        world = LocalWorld(local_controller, monkeypatch)
        _enrol_local()
        csrf = world.sign_in()
        # A bundle that still carries settings the admin config owns: the state says they are ignored.
        raw = json.dumps({"version": 1, "web_push": {"enabled": False}}).encode()
        r = _passkey_upload(world.client, self.URL, csrf, raw)
        assert r.status_code == 200
        state = r.json()
        assert "services" in state and "restart_needed" in state
        assert "are ignored" in state["error"]
        assert (sc.org_dir() / "org_bundle.json").exists()

    def test_it_refuses_without_a_human_session(self, local_controller, monkeypatch):
        from privacyfence.web.session_auth import PROVENANCE_UNATTESTED

        world = LocalWorld(local_controller, monkeypatch, require_human_session=True)
        csrf = world.sign_in(PROVENANCE_UNATTESTED)
        r = _upload(world.client, self.URL, csrf, b'{"version": 1}')
        assert (r.status_code, r.json()["error"]) == (403, "human_session_required")

    def test_it_is_classified_as_bespoke_sensitive(self):
        assert routes_admin.ROUTE_CLASSIFICATION[self.URL] == "bespoke-sensitive"


def _active_nav(page: str) -> list[str]:
    return sorted(set(re.findall(r'<a class="pf-shell-nav-item active" href="([^"]+)" aria-current="page">', page)))


class TestAdminPage:
    def test_an_org_visitor_with_no_session_is_sent_to_sign_in(self):
        world = World()
        r = world.client.get("/admin")
        assert (r.status_code, r.headers["location"]) == (302, "/login?next=/admin")

    def test_an_org_non_admin_gets_a_403_page_without_the_admin_nav_item(self):
        world = World()
        world.sign_in(ALICE)
        r = world.client.get("/admin")
        assert r.status_code == 403
        assert "Only administrators can open this page." in r.text
        assert 'href="/admin"' not in r.text

    def test_an_org_admin_gets_the_page_with_the_admin_nav_item_active(self):
        world = World()
        csrf = world.sign_in(ADMIN)
        r = world.client.get("/admin")
        assert r.status_code == 200
        assert _active_nav(r.text) == ["/admin"]
        assert "window.__pfInitialSection = \"services\"" in r.text
        assert '"features": true' in r.text
        assert '"surface": "admin"' in r.text or '"surface":"admin"' in r.text
        # Actions post under /api/admin, with the session's CSRF token.
        assert "var ENDPOINT = '/api/admin';" in r.text
        assert f"var CSRF = '{csrf}';" in r.text
        assert "pfWebauthnGet" in r.text
        assert SECRET not in r.text

    def test_the_page_state_is_the_admin_state(self):
        world = World()
        world.sign_in(ADMIN)
        r = world.client.get("/admin")
        assert '"restart_needed"' in r.text and '"relay_status"' in r.text
        initial = r.text.split("window.__pfInitialState = ")[1].split(";</script>")[0]
        assert not {"connectors", "telegram_auth", "peek_auth"} & set(json.loads(initial))

    def test_a_local_visitor_with_no_session_is_refused(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        assert world.client.get("/admin").status_code == 401

    def test_the_local_owner_gets_the_page_with_the_admin_nav_item_active(self, local_controller, monkeypatch):
        world = LocalWorld(local_controller, monkeypatch)
        csrf = world.sign_in()
        r = world.client.get("/admin")
        assert r.status_code == 200
        assert _active_nav(r.text) == ["/admin"]
        assert "var ENDPOINT = '/api/admin';" in r.text
        assert f"var CSRF = '{csrf}';" in r.text
        assert '"org_config": true' in r.text
        assert '"relay_status"' not in r.text
        assert SECRET not in r.text
