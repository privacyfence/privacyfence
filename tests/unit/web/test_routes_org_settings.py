"""Tests for web/routes_org_settings.py -- org mode's settings surface
(#400): every signed-in principal's own auto-accept rules/grants (read +
remove), and the admin-only install-wide PII/privacy policy, read (C3d) and
edited (C3e).
"""
from __future__ import annotations

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import auto_accept, paths, privacy_filter
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


def _app(*, install_wide_settings=None, sessions=None, install_wide_settings_path=""):
    sessions = sessions or org_session.OrgSessionStore()
    routes = ros.build_routes(
        sessions=sessions, install_wide_settings=install_wide_settings if install_wide_settings is not None else {},
        install_wide_settings_path=install_wide_settings_path,
    )
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

    def test_add_rule_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post(
            "/api/settings/rules/add", data={"rule_choice": "contacts.edit|no_contact_info_change"},
        )
        assert r.status_code == 302

    def test_remove_rule_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/settings/rules/remove", data={"op_key": "x", "rule": "y"})
        assert r.status_code == 302

    def test_remove_grant_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/settings/grants/remove", data={"connector": "drive", "config_key": "folders"})
        assert r.status_code == 302

    @pytest.mark.parametrize("url", ["/api/settings/privacy/policy", "/api/settings/privacy/pii"])
    def test_install_wide_writes_redirect_to_login_when_signed_out(self, url):
        app, _sessions = _app()
        r = _client(app).post(url, data={"action": "set_default_policy"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings/privacy"


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
        # "always_allow" alone isn't a safe marker any more -- it's also a
        # real rule type the static "Add a rule" picker below always lists
        # for e.g. gmail.create_draft, regardless of whose rules are shown.
        # "gmail.send" is not a real operation key (RULES_BY_OPERATION has
        # no such entry), so it can only appear here as Alice's own
        # configured rule row, never as one of the picker's own options.
        assert "gmail.send" not in r.text
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


class TestAddRule:
    def test_adds_the_rule_and_redirects(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|no_contact_info_change", "value": "", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_current_config()["auto_accept_rules"] == {
                "contacts.edit": [{"rule": "no_contact_info_change"}],
            }

    def test_adds_a_list_value_rule_from_comma_separated_text(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={
                "rule_choice": "gmail.read_message|trusted_sender_domain",
                "value": "example.com, example.org",
                "csrf": csrf,
            },
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            cfg = auto_accept.get_current_config()["auto_accept_rules"]
        assert cfg["gmail.read_message"] == [
            {"rule": "trusted_sender_domain", "value": ["example.com", "example.org"]},
        ]

    def test_unknown_rule_name_is_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|not_a_real_rule", "csrf": csrf},
        )
        assert r.status_code == 400
        assert _on_disk_rules(tmp_path, "alice") == {}

    def test_a_rule_type_not_valid_for_the_chosen_operation_is_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            # always_allow is a real rule name, just not one RULES_BY_OPERATION
            # lists for contacts.edit -- must be rejected the same way an
            # outright-unknown rule name is.
            data={"rule_choice": "contacts.edit|always_allow", "csrf": csrf},
        )
        assert r.status_code == 400
        assert _on_disk_rules(tmp_path, "alice") == {}

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|no_contact_info_change", "csrf": "not-the-real-token"},
        )
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") == {}

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|no_contact_info_change", "csrf": csrf},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_second_principal_cannot_add_to_the_first_principals_rules(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "bob")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|no_contact_info_change", "csrf": csrf},
        )
        assert r.status_code == 303
        assert _on_disk_rules(tmp_path, "alice") == {}
        with principal_scope(BOB):
            assert auto_accept.get_current_config()["auto_accept_rules"] == {
                "contacts.edit": [{"rule": "no_contact_info_change"}],
            }

    def test_the_page_renders_an_add_rule_form(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.get("/settings")
        assert "/api/settings/rules/add" in r.text
        assert "rule_choice" in r.text

    def test_adds_an_int_value_rule_from_numeric_text(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.read_message|age_threshold_days", "value": "30", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            cfg = auto_accept.get_current_config()["auto_accept_rules"]
        assert cfg["gmail.read_message"] == [{"rule": "age_threshold_days", "value": 30}]

    def test_a_non_numeric_value_for_an_int_rule_is_kept_as_typed(self, tmp_path, monkeypatch):
        # _parse_rule_value_field's own fallback: a non-numeric value for an
        # int-value rule is stored as-typed rather than rejected -- the
        # evaluator simply won't match it, a softer failure than blocking
        # the submission outright.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.read_message|age_threshold_days", "value": "soon", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            cfg = auto_accept.get_current_config()["auto_accept_rules"]
        assert cfg["gmail.read_message"] == [{"rule": "age_threshold_days", "value": "soon"}]

    def test_forbidden_when_is_action_permitted_denies_it(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        monkeypatch.setattr(ros, "is_action_permitted", lambda action, principal: False)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "contacts.edit|no_contact_info_change", "csrf": csrf},
        )
        assert r.status_code == 403
        assert _on_disk_rules(tmp_path, "alice") == {}


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

    def test_forbidden_when_is_action_permitted_denies_it(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        monkeypatch.setattr(ros, "is_action_permitted", lambda action, principal: False)

        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": csrf},
        )
        assert r.status_code == 403
        assert _on_disk_rules(tmp_path, "alice") == {"gmail.send": [{"rule": "always_allow"}]}


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

    def test_forbidden_when_is_action_permitted_denies_it(self, tmp_path, monkeypatch):
        _seed(
            tmp_path, monkeypatch, "alice",
            grants={"drive": {"folders": [{"id": "folder-1", "name": "Sandbox", "read": True}]}},
        )
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        monkeypatch.setattr(ros, "is_action_permitted", lambda action, principal: False)

        r = client.post(
            "/api/settings/grants/remove",
            data={"connector": "drive", "config_key": "folders", "resource_id": "folder-1", "csrf": csrf},
        )
        assert r.status_code == 403


class TestInstallWidePolicyEditing:
    """#400 C3e -- the admin-only write surface on /settings/privacy."""

    @staticmethod
    def _editable(tmp_path, monkeypatch, settings: dict):
        _seed(tmp_path, monkeypatch, "carol")
        path = tmp_path / "install-settings.yaml"
        path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        app, sessions = _app(install_wide_settings=settings, install_wide_settings_path=str(path))
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)
        return client, csrf, settings, path

    def test_admin_sees_edit_controls(self, tmp_path, monkeypatch):
        client, _csrf, _settings, _path = self._editable(tmp_path, monkeypatch, {})
        body = client.get("/settings/privacy").text
        assert "/api/settings/privacy/policy" in body
        assert "/api/settings/privacy/pii" in body
        assert "no daemon restart" in body

    def test_page_falls_back_to_read_only_without_a_settings_path(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app(install_wide_settings={})
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        body = client.get("/settings/privacy").text
        assert "/api/settings/privacy/policy" not in body
        assert "nothing here to write back to" in body

    def test_admin_can_set_a_group_default_policy(self, tmp_path, monkeypatch):
        client, csrf, settings, path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "redact"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/settings/privacy"
        assert settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load(path.read_text())["privacy"]["default_policy"] == "redact"

    def test_admin_can_set_a_category_policy(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "allow"}},
        )
        r = client.post(
            "/api/settings/privacy/policy",
            data={
                "csrf": csrf, "action": "set_category_policy",
                "group": "privacy", "category": "body", "policy": "block",
            },
        )
        assert r.status_code == 303
        assert settings["privacy"]["categories"] == {"body": "block"}

    def test_admin_can_toggle_the_pii_gate(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"pii_detection": {"enabled": True}},
        )
        r = client.post(
            "/api/settings/privacy/pii",
            data={"csrf": csrf, "action": "toggle_pii_detection", "enabled": "false"},
        )
        assert r.status_code == 303
        assert settings["pii_detection"]["enabled"] is False

    def test_a_non_admin_cannot_write_even_with_a_valid_csrf(self, tmp_path, monkeypatch):
        # The page's own 403 governs rendering; this is the one that governs
        # doing. A hand-written POST from any signed-in principal must not
        # reach org_install_policy at all.
        _seed(tmp_path, monkeypatch, "bob")
        settings = {"privacy": {"default_policy": "block"}}
        path = tmp_path / "install-settings.yaml"
        path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        app, sessions = _app(install_wide_settings=settings, install_wide_settings_path=str(path))
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "allow"},
        )
        assert r.status_code == 403
        assert settings["privacy"]["default_policy"] == "block"

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        client, _csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": "not-the-session", "action": "set_default_policy",
                  "group": "privacy", "policy": "allow"},
        )
        assert r.status_code == 401
        assert settings["privacy"]["default_policy"] == "block"

    def test_cross_origin_is_rejected(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "allow"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert settings["privacy"]["default_policy"] == "block"

    @pytest.mark.parametrize("path,action", [
        ("/api/settings/privacy/policy", "toggle_pii_detection"),
        ("/api/settings/privacy/pii", "set_default_policy"),
        ("/api/settings/privacy/policy", "remove_rule_row"),
    ])
    def test_an_action_this_route_does_not_own_is_a_404(self, tmp_path, monkeypatch, path, action):
        client, csrf, _settings, _p = self._editable(tmp_path, monkeypatch, {})
        r = client.post(path, data={"csrf": csrf, "action": action, "enabled": "false"})
        assert r.status_code == 404

    def test_a_bad_payload_is_a_400(self, tmp_path, monkeypatch):
        client, csrf, _settings, _path = self._editable(tmp_path, monkeypatch, {})
        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "nope"},
        )
        assert r.status_code == 400

    def test_a_change_is_audit_logged_under_the_admin(self, tmp_path, monkeypatch):
        client, csrf, _settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "allow"},
        )

        assert len(recorded) == 1
        principal, summary = recorded[0]
        assert principal.id == ADMIN.id
        assert "allow" in summary
        assert "admin=carol" in summary

    def test_the_change_is_live_for_another_principal_immediately(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "allow"}},
        )
        with principal_scope(BOB):
            privacy_filter.init_privacy_filter(settings, org_managed=True)
            assert privacy_filter.category_policy("privacy", "body") == "allow"

        client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "block"},
        )

        with principal_scope(BOB):
            assert privacy_filter.category_policy("privacy", "body") == "block"


class TestCspNonce:
    def test_both_pages_carry_the_response_nonce_on_their_style_element(self, tmp_path, monkeypatch):
        # Without it _SecurityHeadersMiddleware's style-src-elem drops the
        # whole <style> block and the page renders unstyled.
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        for url in ("/settings", "/settings/privacy"):
            body = client.get(url).text
            assert "<style nonce=\"" in body, url
            assert "<style>" not in body, url


class TestWriteFailures:
    def test_an_unwritable_settings_yaml_is_a_500_with_the_policy_unchanged(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        settings = {"privacy": {"default_policy": "block"}}
        # A directory where the file should be: atomic_write_text raises.
        unwritable = tmp_path / "install-settings.yaml"
        unwritable.mkdir()
        app, sessions = _app(install_wide_settings=settings, install_wide_settings_path=str(unwritable))
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)

        r = client.post(
            "/api/settings/privacy/policy",
            data={"csrf": csrf, "action": "set_default_policy", "group": "privacy", "policy": "allow"},
        )
        assert r.status_code == 500
        assert settings["privacy"]["default_policy"] == "block"

    def test_a_failing_audit_write_does_not_fail_the_request(self, tmp_path, monkeypatch):
        # The policy change is already on disk and live by the time the
        # audit entry is written; losing the entry is worth a warning, not a
        # 500 that tells the admin their applied change didn't apply.
        _seed(tmp_path, monkeypatch, "alice", rules={"gmail.send": [{"rule": "always_allow"}]})
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        def _boom():
            raise RuntimeError("audit log is on fire")

        monkeypatch.setattr(ros, "get_audit_logger", _boom)
        r = client.post(
            "/api/settings/rules/remove",
            data={"op_key": "gmail.send", "rule": "always_allow", "value": "null", "csrf": csrf},
        )
        assert r.status_code == 303
        with principal_scope(ALICE):
            assert auto_accept.get_current_config()["auto_accept_rules"] == {}
