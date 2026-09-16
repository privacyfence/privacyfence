"""Tests for web/routes_org_settings.py -- the org-mode read-only settings
surface (#400): every signed-in principal's own auto-accept rules/grants
(read + remove) and the admin-only install-wide PII/privacy policy view.
"""
from __future__ import annotations

import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import auto_accept, paths
from privacyfence.principal import Principal, principal_scope
from privacyfence.web import org_session, routes_org_settings as ros


BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")
ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)


def _on_disk_rules(tmp_path, principal_id: str) -> dict:
    # Reads straight off disk rather than through auto_accept.
    # get_current_config(), which requires that principal's config_path to
    # already be registered -- true after a request actually reaches that
    # principal's scope, not for a principal who only attempted a rejected
    # (bad-CSRF/wrong-principal) request in this test process.
    raw = yaml.safe_load((tmp_path / "users" / principal_id / "config" / "settings.yaml").read_text())
    return raw.get("auto_accept_rules") or {}


def _seed(tmp_path, monkeypatch, principal_id: str, *, rules=None, grants=None) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_dir = tmp_path / "users" / principal_id / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump({"auto_accept_rules": rules or {}, "auto_accept_grants": grants or {}}),
        encoding="utf-8",
    )


def _app(*, install_wide_settings=None, sessions=None):
    sessions = sessions or org_session.OrgSessionStore()
    routes = ros.build_routes(sessions=sessions, install_wide_settings=install_wide_settings or {})
    return Starlette(routes=routes), sessions


def _client(app) -> TestClient:
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


def _signed_in(client: TestClient, sessions: org_session.OrgSessionStore, principal: Principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


class TestAuthRequired:
    def test_settings_page_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).get("/settings")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings"

    def test_privacy_page_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).get("/settings/privacy")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings/privacy"

    def test_remove_rule_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/settings/rules/remove", data={"op_key": "x", "rule": "y"})
        assert r.status_code == 302

    def test_remove_grant_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/settings/grants/remove", data={"connector": "drive", "config_key": "folders"})
        assert r.status_code == 302


class TestPrivacyPageAdminGating:
    def test_non_admin_gets_403(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "bob")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get("/settings/privacy")
        assert r.status_code == 403

    def test_admin_gets_200(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        r = client.get("/settings/privacy")
        assert r.status_code == 200


class TestPrivacyPolicyView:
    def test_a_group_absent_from_install_wide_settings_is_flagged_as_falling_back(self):
        groups = ros._privacy_policy_view({})
        privacy = next(g for g in groups if g["key"] == "privacy")
        assert privacy["default_policy"] == "block"
        assert privacy["falls_back_to_block_default"] is True

    def test_an_explicitly_configured_group_is_not_flagged(self):
        groups = ros._privacy_policy_view({"privacy": {"default_policy": "allow"}})
        privacy = next(g for g in groups if g["key"] == "privacy")
        assert privacy["default_policy"] == "allow"
        assert privacy["falls_back_to_block_default"] is False

    def test_the_admin_page_names_a_fallback_group(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app(install_wide_settings={})
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        r = client.get("/settings/privacy")
        assert "falling back to the org-mode block default" in r.text

    def test_a_malformed_group_fails_closed_to_block_rather_than_500ing(self):
        # Mirrors settings_controller._privacy_state's own defensive
        # posture: init_privacy_filter (SEC-07) already refused to start the
        # daemon on a malformed group, so reaching this here only happens if
        # settings.yaml was hand-edited after that -- render "block", the
        # fail-closed default, rather than raising.
        groups = ros._privacy_policy_view({"privacy": {"default_policy": "not_a_real_policy"}})
        privacy = next(g for g in groups if g["key"] == "privacy")
        assert privacy["default_policy"] == "block"


class TestPrincipalScopedRulesAndGrants:
    def test_only_shows_the_signed_in_principals_own_rules(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        _seed(tmp_path, monkeypatch, "bob", rules={})
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "always_allow" not in r.text
        assert "No auto-accept rules configured." in r.text

    def test_shows_the_signed_in_principals_own_rule(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "always_allow" in r.text

    def test_shows_the_signed_in_principals_own_grant(self, tmp_path, monkeypatch):
        _seed(
            tmp_path, monkeypatch, "alice",
            grants={"drive": {"folders": [{"id": "folder-1", "name": "Sandbox", "read": True, "write": False}]}},
        )
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Sandbox" in r.text
        assert "Read auto-accept" in r.text  # the capability label, not the raw "read" key

    def test_admin_link_only_shown_for_an_admin(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)

        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert "/settings/privacy" not in r.text

        client2 = _client(app)
        _signed_in(client2, sessions, ADMIN)
        r2 = client2.get("/settings")
        assert "/settings/privacy" in r2.text


class TestRemoveRule:
    def test_removes_the_matching_rule_and_redirects(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_current_config()["auto_accept_rules"] == {}

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": "not-the-real-token"},
        )
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") != {}

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": csrf},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_malformed_value_is_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "{not json", "csrf": csrf},
        )
        assert r.status_code == 400

    def test_a_second_principal_cannot_remove_the_first_principals_rule(self, tmp_path, monkeypatch):
        # Bob signs in and posts a removal naming Alice's own rule -- the
        # route always acts on current_principal() (Bob), so this can only
        # ever touch Bob's own (empty) rule set, never Alice's.
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        _seed(tmp_path, monkeypatch, "bob", rules={})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": csrf},
        )
        assert r.status_code == 303
        assert _on_disk_rules(tmp_path, "alice")["gmail.send"] == [{"rule": "always_allow"}]


class TestRemoveGrant:
    def test_removes_the_matching_grant_and_redirects(self, tmp_path, monkeypatch):
        _seed(
            tmp_path, monkeypatch, "alice",
            grants={"drive": {"folders": [{"id": "folder-1", "name": "Sandbox", "read": True}]}},
        )
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/grants/remove",
            data={"connector": "drive", "config_key": "folders", "resource_id": "folder-1", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_current_config()["auto_accept_grants"] == {}

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        _seed(
            tmp_path, monkeypatch, "alice",
            grants={"drive": {"folders": [{"id": "folder-1", "name": "Sandbox", "read": True}]}},
        )
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/grants/remove",
            data={
                "connector": "drive", "config_key": "folders", "resource_id": "folder-1",
                "csrf": "not-the-real-token",
            },
        )
        assert r.status_code == 401

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        _seed(
            tmp_path, monkeypatch, "alice",
            grants={"drive": {"folders": [{"id": "folder-1", "name": "Sandbox", "read": True}]}},
        )
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/grants/remove",
            data={"connector": "drive", "config_key": "folders", "resource_id": "folder-1", "csrf": csrf},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_nonexistent_grant_still_redirects_without_erroring(self, tmp_path, monkeypatch):
        # A valid resource type but no matching entry -- e.g. a stale form
        # from a page the principal had open in another tab after removing
        # it there first. apply_grant_removal reports "nothing to do" and
        # this must not write an audit entry for a removal that never
        # happened.
        _seed(tmp_path, monkeypatch, "alice", grants={})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/grants/remove",
            data={"connector": "drive", "config_key": "folders", "resource_id": "nope", "csrf": csrf},
        )
        assert r.status_code == 303

    def test_unknown_resource_type_is_404(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/grants/remove",
            data={"connector": "not_a_real_connector", "config_key": "nope", "resource_id": "x", "csrf": csrf},
        )
        assert r.status_code == 404
