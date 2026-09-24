"""Tests for org mode's settings surface (#400): every signed-in principal's
own auto-accept rules (read + add + remove), and the admin-only install-wide
PII/privacy policy, read (C3d) and edited (C3e). Exercises
web/routes_settings.py's ``build_org_routes`` (PSC-4b folded the former
web/routes_org_settings.py's routes in there; its page rendering moved to
web/org_settings_pages.py) rather than the module this file is still named
for.

P9 of the policy v2 redesign rebuilt the rules half of this page onto the v2
``auto_accept:`` on-disk section (``policy/store.py``) exclusively, via
``auto_accept.get_policy_v2_rules``/``add_policy_v2_rules``/
``remove_policy_v2_rule`` and the shared scope+verb catalogue
(``policy/catalogue.py``). The v1-era "Trusted resources" (grants) section
and its ``/api/settings/grants/remove`` route are gone entirely, not
adapted -- every resource type it covered is a ``policy/scopes.py`` scope
now. The privacy/PII half of the page is untouched by any of this.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import auto_accept, paths, privacy_filter
from privacyfence import webauthn_stepup as wa
from privacyfence.policy import catalogue as policy_catalogue
from privacyfence.policy import describe as policy_describe
from privacyfence.policy import store as policy_store
from privacyfence.principal import Principal, principal_scope
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, org_settings_pages
from privacyfence.web import routes_settings as ros


BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")
ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)


def _catalogue_rules(group: str, verb: str, value: list | None = None) -> list:
    """The ``PolicyRule``s one ``group|verb`` "add a rule" submission compiles to -- the same call
    ``add_rule`` itself makes, so a test can seed or predict exactly what a form submission with
    this ``rule_choice`` produces."""
    return policy_catalogue.rules_for_catalogue_entry(group, value, policy_catalogue.parse_verbs([verb]))


def _on_disk_rules(tmp_path, principal_id: str) -> list:
    # Reads straight off disk rather than through auto_accept.get_policy_v2_rules(), which
    # requires that principal's config_path to already be registered -- true after a request
    # actually reaches that principal's scope, not for a principal who only attempted a rejected
    # (bad-CSRF/wrong-origin/forbidden) request in this test process. A refused request that still
    # touched this principal's WebAuthn credentials (a step-up challenge/verify attempt) triggers
    # paths.py's own one-time legacy-authority-files migration as a side effect -- settings.yaml
    # moves from config/ to authority/config/ the first time authority_dir() is asked for this
    # principal at all, whether or not the settings write itself was ever reached. Check the
    # post-migration location first since a migration, once it happens, is one-directional.
    settings_path = tmp_path / "users" / principal_id / "authority" / "config" / "settings.yaml"
    if not settings_path.exists():
        settings_path = tmp_path / "users" / principal_id / "config" / "settings.yaml"
    raw = yaml.safe_load(settings_path.read_text())
    return ((raw or {}).get("auto_accept") or {}).get("rules") or []


def _seed(tmp_path, monkeypatch, principal_id: str, *, rules=None) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_dir = tmp_path / "users" / principal_id / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if rules:
        cfg["auto_accept"] = policy_store.rules_to_config(list(rules))
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _app(
    *, install_wide_settings=None, sessions=None, install_wide_settings_path="",
    step_up: StepUpConfig | None = None,
):
    sessions = sessions or org_session.OrgSessionStore()
    routes = ros.build_org_routes(
        sessions=sessions, install_wide_settings=install_wide_settings if install_wide_settings is not None else {},
        install_wide_settings_path=install_wide_settings_path,
        step_up=step_up or StepUpConfig(), step_up_origin=BASE_URL,
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
        r = _client(app).post("/api/settings/rules/add", data={"rule_choice": "gmail.sender|read"})
        assert r.status_code == 302

    def test_remove_rule_redirects_to_login_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/settings/rules/remove", data={"rule_id": "r-doesnotmatter"})
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
        groups = org_settings_pages._privacy_policy_view({})
        privacy = next(g for g in groups if g["key"] == "privacy")
        assert privacy["default_policy"] == "block"
        assert privacy["falls_back_to_block_default"] is True

    def test_an_explicitly_configured_group_is_not_flagged(self):
        groups = org_settings_pages._privacy_policy_view({"privacy": {"default_policy": "allow"}})
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
        groups = org_settings_pages._privacy_policy_view({"privacy": {"default_policy": "not_a_real_policy"}})
        privacy = next(g for g in groups if g["key"] == "privacy")
        assert privacy["default_policy"] == "block"


class TestPrincipalScopedRules:
    def test_only_shows_the_signed_in_principals_own_rules(self, tmp_path, monkeypatch):
        # A domain distinct from either principal's own @example.com email,
        # so it can only appear here as Alice's own configured rule value --
        # the static "Add a rule" picker's options carry no example values.
        _seed(
            tmp_path, monkeypatch, "alice",
            rules=_catalogue_rules("gmail.sender_domain", "read", ["widgetmakers.test"]),
        )
        _seed(tmp_path, monkeypatch, "bob", rules=[])
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "widgetmakers.test" not in r.text
        assert "No auto-accept rules configured." in r.text

    def test_shows_the_signed_in_principals_own_rule(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender_domain", "read", ["widgetmakers.test"])
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 200
        assert policy_describe.rule_sentence(rules[0]) in r.text

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
            data={"rule_choice": "gmail.sender|read", "value": "", "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")

    def test_adds_a_rule_with_a_list_value_from_comma_separated_text(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={
                "rule_choice": "gmail.sender_domain|read",
                "value": "example.com, example.org",
                "csrf": csrf,
            },
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            rules = auto_accept.get_policy_v2_rules()
        assert rules == _catalogue_rules("gmail.sender_domain", "read", ["example.com", "example.org"])

    def test_an_unknown_scope_group_is_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "not_a_real_group|read", "csrf": csrf},
        )
        assert r.status_code == 400
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_a_verb_the_chosen_scope_does_not_govern_is_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            # "delete" is a real verb, just not one the catalogue lists for
            # gmail.sender (['read', 'download', 'archive']) -- must be
            # rejected the same way an outright-unknown group is.
            data={"rule_choice": "gmail.sender|delete", "csrf": csrf},
        )
        assert r.status_code == 400
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_a_value_needing_scope_submitted_with_no_value_is_400(self, tmp_path, monkeypatch):
        # apps_script.project is one of the EXTRA_SCOPES entries that require
        # a value (needs_value=True) -- rules_for_catalogue_entry's own
        # fail-closed-and-quiet posture for that case.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "apps_script.project|read", "value": "", "csrf": csrf},
        )
        assert r.status_code == 400
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "csrf": "not-the-real-token"},
        )
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "csrf": csrf},
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
            data={"rule_choice": "gmail.sender|read", "csrf": csrf},
        )
        assert r.status_code == 303
        assert _on_disk_rules(tmp_path, "alice") == []
        with principal_scope(BOB):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")

    def test_the_page_renders_an_add_rule_form(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.get("/settings")
        assert "/api/settings/rules/add" in r.text
        assert "rule_choice" in r.text
        assert "gmail.sender|read" in r.text  # one real (scope group, verb) option value

    def test_forbidden_when_is_action_permitted_denies_it(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []

        def _deny(action, principal, *, mode):
            recorded.append(action)
            return False

        monkeypatch.setattr(ros, "is_action_permitted", _deny)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "csrf": csrf},
        )
        assert r.status_code == 403
        assert _on_disk_rules(tmp_path, "alice") == []
        # Gated under the new, moved-in action name (org_settings_scope.py's
        # ACTION_SCOPES), not the old add_rule_row it replaced.
        assert recorded == ["add_policy_rule"]


class TestRemoveRule:
    def test_removes_the_matching_rule_and_redirects(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == []

    def test_removes_exactly_that_rule_and_nothing_else(self, tmp_path, monkeypatch):
        sender_rule = _catalogue_rules("gmail.sender", "read")[0]
        folder_rule = _catalogue_rules("drive.folder", "read", ["folder-1"])[0]
        _seed(tmp_path, monkeypatch, "alice", rules=[sender_rule, folder_rule])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": sender_rule.id, "csrf": csrf},
        )
        assert r.status_code == 303

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == [folder_rule]

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": "not-the-real-token"},
        )
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") != []

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": csrf},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_nonexistent_rule_id_still_redirects_without_erroring(self, tmp_path, monkeypatch):
        # A stale form from a page the principal had open in another tab
        # after removing that rule there first -- remove_policy_v2_rule
        # reports "nothing removed" and this must not write an audit entry
        # for a removal that never happened.
        _seed(tmp_path, monkeypatch, "alice", rules=[])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": "r-doesnotexist", "csrf": csrf},
        )
        assert r.status_code == 303

    def test_a_second_principal_cannot_remove_the_first_principals_rule(self, tmp_path, monkeypatch):
        # Bob signs in and posts a removal naming Alice's own rule id -- the
        # route always acts on current_principal() (Bob), so this can only
        # ever touch Bob's own (empty) rule set, never Alice's.
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        _seed(tmp_path, monkeypatch, "bob", rules=[])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": csrf},
        )
        assert r.status_code == 303
        assert _on_disk_rules(tmp_path, "alice")[0]["id"] == rules[0].id

    def test_forbidden_when_is_action_permitted_denies_it(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []

        def _deny(action, principal, *, mode):
            recorded.append(action)
            return False

        monkeypatch.setattr(ros, "is_action_permitted", _deny)

        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": csrf},
        )
        assert r.status_code == 403
        assert len(_on_disk_rules(tmp_path, "alice")) == 1
        # Gated under the new, moved-in action name (org_settings_scope.py's
        # ACTION_SCOPES), not the old remove_rule_row it replaced.
        assert recorded == ["remove_policy_rule"]


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
        ("/api/settings/privacy/policy", "remove_policy_rule"),
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


class TestPersistentNav:
    """web_shell.wrap()'d (web_shell.ORG_NAV_ITEMS) since the fix that made
    /approvals'/connect's/security's shared header/nav survive navigating
    into /settings too -- previously this page was a bare doctype+tokens.css
    document with a single centred "Approvals" link at the bottom."""

    def test_settings_and_privacy_pages_both_carry_the_shell_nav(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        for url in ("/settings", "/settings/privacy"):
            body = client.get(url).text
            assert 'class="pf-shell-nav-item active" href="/settings"' in body, url
            for href in ("/approvals", "/connect", "/security"):
                assert f'class="pf-shell-nav-item" href="{href}"' in body, url
            assert '<p style="text-align:center;margin-top:2em">' not in body, url

    def test_signed_in_principal_is_shown_in_the_shell_header(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        body = client.get("/settings").text
        assert f'<div class="pf-shell-principal">{ALICE.email}</div>' in body


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
        # The rule removal is already on disk by the time the audit entry is
        # written; losing the entry is worth a warning, not a 500 that tells
        # the principal their applied change didn't apply.
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        def _boom():
            raise RuntimeError("audit log is on fire")

        monkeypatch.setattr(ros, "get_audit_logger", _boom)
        r = client.post(
            "/api/settings/rules/remove",
            data={"rule_id": rules[0].id, "csrf": csrf},
        )
        assert r.status_code == 303
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == []


class TestStepUpRequirePasskey:
    """#579: closes the gap where every org-routed ``_SENSITIVE_ACTIONS``
    member (``add_policy_rule``, ``remove_policy_rule``, and the two
    install-wide privacy/PII writes) bypassed step-up entirely, unlike local
    mode's own generic dispatcher (gated since #426 Phase 3). An agent that
    cannot forge a WebAuthn assertion could otherwise add an always-allow
    rule, or flip the install-wide PII/privacy policy, once step-up is
    supposed to be in force."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    @staticmethod
    def _step_up(**overrides) -> StepUpConfig:
        return StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True, **overrides)

    @staticmethod
    def _install_wide_app(tmp_path, settings: dict, *, step_up: StepUpConfig):
        settings_path = tmp_path / "install-settings.yaml"
        settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        app, sessions = _app(
            install_wide_settings=settings, install_wide_settings_path=str(settings_path), step_up=step_up,
        )
        return app, sessions, settings_path

    @pytest.mark.parametrize("path,data", [
        ("/api/settings/rules/add", {"rule_choice": "gmail.sender|read", "value": ""}),
        ("/api/settings/rules/remove", {"rule_id": "r-whatever"}),
    ])
    def test_per_principal_action_hard_fails_with_no_credential_and_writes_nothing(
        self, tmp_path, monkeypatch, path, data,
    ):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(path, data={**data, "csrf": csrf})

        assert r.status_code == 403
        assert r.json() == {"error": "passkey_enrollment_required", "enroll_url": "/security"}
        assert _on_disk_rules(tmp_path, "alice") == []

    @pytest.mark.parametrize("path,data", [
        ("/api/settings/privacy/policy", {"action": "set_default_policy", "group": "privacy", "policy": "redact"}),
        (
            "/api/settings/privacy/policy",
            {"action": "set_category_policy", "group": "privacy", "category": "body", "policy": "block"},
        ),
        ("/api/settings/privacy/pii", {"action": "toggle_pii_detection", "enabled": "false"}),
        (
            "/api/settings/privacy/pii",
            {"action": "toggle_pii_category", "category_key": "medical", "enabled": "false"},
        ),
    ])
    def test_admin_only_action_hard_fails_with_no_credential_and_writes_nothing(
        self, tmp_path, monkeypatch, path, data,
    ):
        _seed(tmp_path, monkeypatch, "carol")
        settings = {"privacy": {"default_policy": "allow"}, "pii_detection": {"enabled": True}}
        app, sessions, settings_path = self._install_wide_app(tmp_path, settings, step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)

        r = client.post(path, data={**data, "csrf": csrf})

        assert r.status_code == 403
        assert r.json() == {"error": "passkey_enrollment_required", "enroll_url": "/security"}
        assert settings["privacy"]["default_policy"] == "allow"
        assert settings["pii_detection"]["enabled"] is True
        assert yaml.safe_load(settings_path.read_text()) == settings

    def test_a_valid_assertion_completes_a_per_principal_action(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self._step_up())
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        first = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "value": "", "csrf": csrf},
        )
        assert first.status_code == 428
        assert "webauthn_options" in first.json()
        assert _on_disk_rules(tmp_path, "alice") == []

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(
                "/api/settings/rules/add",
                data={
                    "rule_choice": "gmail.sender|read", "value": "", "csrf": csrf,
                    "webauthn_assertion": json.dumps({"id": "Y3JlZC0x"}),
                },
            )
        assert second.status_code == 303
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")

    def test_a_valid_assertion_completes_an_admin_only_action(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        settings = {"privacy": {"default_policy": "block"}}
        app, sessions, settings_path = self._install_wide_app(tmp_path, settings, step_up=self._step_up())
        wa.add_credential(ADMIN, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)

        first = client.post(
            "/api/settings/privacy/policy",
            data={"action": "set_default_policy", "group": "privacy", "policy": "redact", "csrf": csrf},
        )
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(
                "/api/settings/privacy/policy",
                data={
                    "action": "set_default_policy", "group": "privacy", "policy": "redact", "csrf": csrf,
                    "webauthn_assertion": json.dumps({"id": "Y3JlZC0x"}),
                },
            )
        assert second.status_code == 303
        assert settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load(settings_path.read_text())["privacy"]["default_policy"] == "redact"

    def test_a_refusal_is_audit_logged_under_the_refused_principal(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "value": "", "csrf": csrf},
        )

        assert len(recorded) == 1
        principal, summary = recorded[0]
        assert principal.id == "alice"
        assert "add_policy_rule" in summary

    def test_a_non_admin_still_cannot_reach_an_admin_only_action_under_step_up(self, tmp_path, monkeypatch):
        # Authorization is checked before step-up (routes_settings.py's own
        # require_human_session-before-_needs_step_up ordering) -- a
        # non-admin gets the same 403 "forbidden" it always got, never a
        # passkey prompt for an action it could never take either way.
        _seed(tmp_path, monkeypatch, "bob")
        settings = {"privacy": {"default_policy": "block"}}
        app, sessions, settings_path = self._install_wide_app(tmp_path, settings, step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = client.post(
            "/api/settings/privacy/policy",
            data={"action": "set_default_policy", "group": "privacy", "policy": "allow", "csrf": csrf},
        )

        assert r.status_code == 403
        assert r.json() == {"error": "forbidden"}
        assert settings["privacy"]["default_policy"] == "block"
        assert yaml.safe_load(settings_path.read_text())["privacy"]["default_policy"] == "block"

    def test_step_up_off_does_not_change_existing_behavior(self, tmp_path, monkeypatch):
        # step_up.enabled=False (the _app() default) -- every existing
        # TestAddRule/TestInstallWidePolicyEditing test already covers this
        # implicitly; this is the explicit regression guard for the knob
        # itself, since _needs_step_up now gates on it.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=StepUpConfig(enabled=False, rp_id="pf.example.com", require_passkey=True))
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = client.post(
            "/api/settings/rules/add",
            data={"rule_choice": "gmail.sender|read", "value": "", "csrf": csrf},
        )

        assert r.status_code == 303
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")
