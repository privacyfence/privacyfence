"""Tests for org mode's settings surface (#400): every signed-in principal's
own auto-accept rules (read + add + remove), and the admin-only install-wide
PII/privacy policy, read (C3d) and edited (C3e). Exercises
web/routes_settings.py's ``build_org_routes`` (PSC-4b folded the former
web/routes_org_settings.py's routes in there; PSC-5 folds its page
*rendering* into the exact same settings_window_html.build_html() local
mode's own settings page uses, and its four bespoke POST routes into one
generic ``POST /api/settings/{action}`` -- see that function's own
docstring) rather than the module this file is still named for.

PSC-5's own behavior changes from the four-bespoke-route shape this file
used to test:

- Every write is now ``POST /api/settings/<action>`` with a JSON body
  (``{**payload, "csrf": csrf}``), the exact same path/shape local mode's
  own dispatcher answers -- not a form POST to
  ``/api/settings/rules/add``/``/api/settings/rules/remove``/
  ``/api/settings/privacy/policy``/``/api/settings/privacy/pii``.
- A successful write returns ``200`` with the fresh page state as JSON
  (matching local mode's own dispatcher), not a ``303`` redirect back to a
  form-POST page.
- ``add_policy_rule`` takes ``{group, value, verbs: [...]}`` (a list of
  verbs, matching the shared JS's own multi-verb-checkbox "Add a rule"
  form) rather than a single ``rule_choice`` ("{group}|{verb}") pair.
- ``toggle_pii_detection``/``toggle_pii_category`` flip the *current*
  value server-side (matching local mode's own toggle semantics, and what
  the shared JS's toggle control actually posts -- an empty payload) --
  they no longer take an explicit ``enabled`` field the way
  ``org_install_policy.apply_change``'s own contract otherwise requires;
  see ``routes_settings.py``'s own ``_current_pii_flag``.
- An unauthenticated write now gets a JSON ``401`` (the generic dispatcher
  is driven by ``fetch()``, not a real browser navigation, so a redirect
  to an HTML login page would break the bridge's own ``.json()`` parse) --
  the GET pages (``/settings``/``/settings/privacy``) still redirect to
  ``/login`` exactly as before.

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
import re
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
from privacyfence.settings_controller import _privacy_state_from_config
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session
from privacyfence.web import routes_settings as ros


BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")
ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)


def _catalogue_rules(group: str, verb: str, value: list | None = None) -> list:
    """The ``PolicyRule``s one ``group``/``[verb]`` "add a rule" submission compiles to -- the same
    call the dispatcher itself makes, so a test can seed or predict exactly what a submission with
    this group/verbs produces."""
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


def _post_action(client: TestClient, action: str, payload: dict, csrf: str, **kwargs):
    return client.post(f"/api/settings/{action}", json={**payload, "csrf": csrf}, **kwargs)


def _capabilities(body: str) -> dict:
    match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", body, re.DOTALL)
    assert match, "window.__pfCapabilities assignment not found in the settings page"
    return json.loads(match.group(1))


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

    @pytest.mark.parametrize("action", [
        "add_policy_rule", "remove_policy_rule",
        "set_default_policy", "toggle_pii_detection",
    ])
    def test_writes_are_a_json_401_when_signed_out(self, action):
        # Unlike the two GET pages above, this is the generic fetch()-driven
        # dispatcher the shared page's own JS bridge posts to -- a redirect
        # to an HTML login page here would break its .json() parse, so this
        # answers the same way local mode's own dispatcher does.
        app, _sessions = _app()
        r = _client(app).post(f"/api/settings/{action}", json={"csrf": "whatever"})
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}


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

    def test_gmail_signature_toggle_is_left_out_of_orgs_state(self, tmp_path, monkeypatch):
        # toggle_gmail_signature is LOCAL_MODE-only, like the calendar
        # toggle -- the page renders its card only when this key exists.
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        body = client.get("/settings/privacy").text
        assert '"gmail_append_signature"' not in body
        assert '"calendar_free_busy"' in body


class TestPrivacyStateFromConfig:
    """PSC-5: org mode's own Privacy Filter state now comes from the exact
    same ``_privacy_state_from_config`` local mode's page uses (see
    settings_controller.py), called with ``fail_safe_default="block"`` --
    #400's own fail-closed posture for an install-wide group nobody
    configured, kept distinct from local mode's own ``"allow"`` default."""

    def test_a_group_absent_from_install_wide_settings_falls_back_to_block(self):
        state = _privacy_state_from_config({}, fail_safe_default="block")
        assert state["default_policy"]["privacy"] == "block"

    def test_local_modes_own_default_stays_allow(self):
        # The regression this split exists to prevent: passing no
        # fail_safe_default at all (local mode's own call site) must not
        # silently pick up org mode's fail-closed default.
        state = _privacy_state_from_config({})
        assert state["default_policy"]["privacy"] == "allow"

    def test_an_explicitly_configured_group_is_used_as_is(self):
        state = _privacy_state_from_config({"privacy": {"default_policy": "allow"}}, fail_safe_default="block")
        assert state["default_policy"]["privacy"] == "allow"

    def test_a_malformed_group_fails_closed_to_allow_rather_than_500ing(self):
        # settings_controller._privacy_state_from_config's own defensive
        # posture (init_privacy_filter/SEC-07 already refused to start the
        # daemon on a malformed group, so reaching this only happens if
        # settings.yaml was hand-edited after that) renders "allow" rather
        # than raising, regardless of fail_safe_default -- unchanged by
        # this phase, kept here as the org-mode-shaped regression guard.
        state = _privacy_state_from_config(
            {"privacy": {"default_policy": "not_a_real_policy"}}, fail_safe_default="block",
        )
        assert state["default_policy"]["privacy"] == "allow"

    def test_calendar_is_not_among_orgs_own_groups(self):
        # toggle_calendar_free_busy is LOCAL_MODE-only (org_settings_scope.
        # ACTION_SCOPES) -- routes_settings._org_state drops the group
        # entirely rather than render a control with nothing to post to;
        # this only asserts the underlying shared function still offers it
        # (local mode keeps it), the dropping itself is an org_state test.
        state = _privacy_state_from_config({}, fail_safe_default="block")
        assert any(g["key"] == "calendar" for g in state["groups"])


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

    def test_shows_the_signed_in_principals_own_rule(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender_domain", "read", ["widgetmakers.test"])
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 200
        assert policy_describe.rule_sentence(rules[0]) in r.text

    def test_admin_sees_privacy_and_general_capabilities_non_admin_does_not(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)

        _signed_in(client, sessions, ALICE)
        caps = _capabilities(client.get("/settings").text)
        assert caps["mode"] == "org"
        assert caps["is_admin"] is False
        assert caps["sections"]["privacy"] is False
        assert caps["sections"]["general"] is False
        assert caps["sections"]["auto_accept"] is True

        client2 = _client(app)
        _signed_in(client2, sessions, ADMIN)
        caps2 = _capabilities(client2.get("/settings").text)
        assert caps2["is_admin"] is True
        assert caps2["sections"]["privacy"] is True
        assert caps2["sections"]["general"] is True


class TestAddRule:
    def test_adds_the_rule(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read"]}, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")

    def test_adds_a_rule_with_a_list_value_from_comma_separated_text(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "add_policy_rule", {
            "group": "gmail.sender_domain", "value": "example.com, example.org", "verbs": ["read"],
        }, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            rules = auto_accept.get_policy_v2_rules()
        assert rules == _catalogue_rules("gmail.sender_domain", "read", ["example.com", "example.org"])

    def test_multiple_verbs_in_one_submission_all_land(self, tmp_path, monkeypatch):
        # The shared JS's own "Add a rule" form (settings_window_html.py's
        # renderAutoAccept) lets more than one verb chip be checked at
        # once -- a capability org's old single-`rule_choice`-pair form
        # never had.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(
            client, "add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read", "download"]}, csrf,
        )
        assert r.status_code == 200
        with principal_scope(ALICE):
            rules = auto_accept.get_policy_v2_rules()
        assert len(rules) == 1
        assert {v.value for v in policy_describe.rule_verbs(rules[0])} >= {"read", "download"}

    def test_an_unknown_scope_group_is_a_no_op_not_an_error(self, tmp_path, monkeypatch):
        # Matches SettingsController.add_policy_rule's own posture (the
        # shared renderer's picker constrains the dropdown, so this is a
        # hand-crafted request, not a real user path) -- silently returns
        # the unchanged state rather than a 400, unlike the old bespoke
        # route's own rule_choice-parsing 400. rules_for_catalogue_entry
        # itself already treats an unknown group as "no rules", so nothing
        # written is the same either way; only the status code differs.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "add_policy_rule", {"group": "not_a_real_group", "verbs": ["read"]}, csrf)
        assert r.status_code == 200
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_a_verb_the_chosen_scope_does_not_govern_is_a_no_op(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(
            # "delete" is a real verb, just not one the catalogue lists for
            # gmail.sender (['read', 'download', 'archive']).
            client, "add_policy_rule", {"group": "gmail.sender", "verbs": ["delete"]}, csrf,
        )
        assert r.status_code == 200
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_a_value_needing_scope_submitted_with_no_value_is_a_no_op(self, tmp_path, monkeypatch):
        # apps_script.project is one of the EXTRA_SCOPES entries that require
        # a value (needs_value=True) -- rules_for_catalogue_entry's own
        # fail-closed-and-quiet posture for that case.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "add_policy_rule", {"group": "apps_script.project", "value": "", "verbs": ["read"]}, csrf)
        assert r.status_code == 200
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "verbs": ["read"]}, "not-the-real-token")
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") == []

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(
            client, "add_policy_rule", {"group": "gmail.sender", "verbs": ["read"]}, csrf,
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_second_principal_cannot_add_to_the_first_principals_rules(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "bob")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "verbs": ["read"]}, csrf)
        assert r.status_code == 200
        assert _on_disk_rules(tmp_path, "alice") == []
        with principal_scope(BOB):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")

    def test_the_page_renders_an_add_rule_form(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.get("/settings")
        assert "'add_policy_rule'" in r.text
        assert "data-aa-group-select" in r.text

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

        r = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "verbs": ["read"]}, csrf)
        assert r.status_code == 403
        assert _on_disk_rules(tmp_path, "alice") == []
        assert recorded == ["add_policy_rule"]

    def test_an_action_not_in_org_mode_is_a_404(self, tmp_path, monkeypatch):
        # toggle_update_check is LOCAL_MODE-only (org_settings_scope.
        # ACTION_SCOPES) -- never a real org route, whatever a hand-crafted
        # request names.
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        r = _post_action(client, "toggle_update_check", {}, csrf)
        assert r.status_code == 404


class TestRemoveRule:
    def test_removes_the_matching_rule(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == []

    def test_removes_exactly_that_rule_and_nothing_else(self, tmp_path, monkeypatch):
        sender_rule = _catalogue_rules("gmail.sender", "read")[0]
        folder_rule = _catalogue_rules("drive.folder", "read", ["folder-1"])[0]
        _seed(tmp_path, monkeypatch, "alice", rules=[sender_rule, folder_rule])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "remove_policy_rule", {"rule_id": sender_rule.id}, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == [folder_rule]

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, "not-the-real-token")
        assert r.status_code == 401
        assert _on_disk_rules(tmp_path, "alice") != []

    def test_mismatched_origin_is_rejected(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(
            client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf,
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_a_nonexistent_rule_id_still_succeeds_without_erroring(self, tmp_path, monkeypatch):
        # A stale request from a tab that already removed that rule --
        # remove_policy_v2_rule reports "nothing removed" and this must not
        # write an audit entry for a removal that never happened.
        _seed(tmp_path, monkeypatch, "alice", rules=[])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "remove_policy_rule", {"rule_id": "r-doesnotexist"}, csrf)
        assert r.status_code == 200

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

        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf)
        assert r.status_code == 200
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

        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf)
        assert r.status_code == 403
        assert len(_on_disk_rules(tmp_path, "alice")) == 1
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
        assert "'set_default_policy'" in body
        assert "'toggle_pii_detection'" in body

    def test_admin_can_set_a_group_default_policy(self, tmp_path, monkeypatch):
        client, csrf, settings, path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "redact"}, csrf)
        assert r.status_code == 200
        assert settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load(path.read_text())["privacy"]["default_policy"] == "redact"

    def test_admin_can_set_a_category_policy(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "allow"}},
        )
        r = _post_action(client, "set_category_policy", {"group": "privacy", "category": "body", "policy": "block"}, csrf)
        assert r.status_code == 200
        assert settings["privacy"]["categories"] == {"body": "block"}

    def test_admin_can_toggle_the_pii_gate(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"pii_detection": {"enabled": True}},
        )
        # No `enabled` field at all -- matches the shared JS toggle
        # control's own empty payload; the dispatcher computes the flip.
        r = _post_action(client, "toggle_pii_detection", {}, csrf)
        assert r.status_code == 200
        assert settings["pii_detection"]["enabled"] is False

    def test_toggle_ignores_a_client_supplied_enabled_value(self, tmp_path, monkeypatch):
        # A client-supplied `enabled` (org_install_policy.apply_change's
        # own plain-form contract) must not let a hand-crafted request pin
        # the toggle to whatever it wants -- the dispatcher always computes
        # the flip itself, the same "current value, inverted" semantics
        # local mode's own menu-item toggle has.
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"pii_detection": {"enabled": True}},
        )
        r = _post_action(client, "toggle_pii_detection", {"enabled": True}, csrf)
        assert r.status_code == 200
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

        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)
        assert r.status_code == 403
        assert settings["privacy"]["default_policy"] == "block"

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        client, _csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "allow"}, "not-the-session")
        assert r.status_code == 401
        assert settings["privacy"]["default_policy"] == "block"

    def test_cross_origin_is_rejected(self, tmp_path, monkeypatch):
        client, csrf, settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        r = _post_action(
            client, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf,
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert settings["privacy"]["default_policy"] == "block"

    def test_a_bad_payload_is_a_400(self, tmp_path, monkeypatch):
        client, csrf, _settings, _path = self._editable(tmp_path, monkeypatch, {})
        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "nope"}, csrf)
        assert r.status_code == 400

    def test_a_change_is_audit_logged_under_the_admin(self, tmp_path, monkeypatch):
        client, csrf, _settings, _path = self._editable(
            tmp_path, monkeypatch, {"privacy": {"default_policy": "block"}},
        )
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        _post_action(client, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)

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

        _post_action(client, "set_default_policy", {"group": "privacy", "policy": "block"}, csrf)

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
    """web_shell.wrap()'d (web_shell.ORG_NAV_ITEMS) -- the same shared
    header/nav every other org-mode page (/approvals, /connect, /security)
    carries."""

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

        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)
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
        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf)
        assert r.status_code == 200
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == []


class TestStepUpRequirePasskey:
    """#579: closes the gap where every org-routed ``_SENSITIVE_ACTIONS``
    member (``add_policy_rule``, ``remove_policy_rule``, and the four
    install-wide privacy/PII writes) bypassed step-up entirely, unlike local
    mode's own generic dispatcher (gated since #426 Phase 3). An agent that
    cannot forge a WebAuthn assertion could otherwise add an always-allow
    rule, or flip the install-wide PII/privacy policy, once step-up is
    supposed to be in force. PSC-5 additionally gives this page the same
    PF_WEBAUTHN_JS/bridge-shim ceremony UI local mode's own settings page
    carries, so a 428/403 here shows the same passkey prompt instead of a
    raw JSON body -- TestStepUpBridgeShim below covers that the shim is
    present; this class stays server-side only, same as before."""

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

    @pytest.mark.parametrize("action,payload", [
        ("add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read"]}),
        ("remove_policy_rule", {"rule_id": "r-whatever"}),
    ])
    def test_per_principal_action_hard_fails_with_no_credential_and_writes_nothing(
        self, tmp_path, monkeypatch, action, payload,
    ):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, action, payload, csrf)

        assert r.status_code == 403
        assert r.json() == {"error": "passkey_enrollment_required", "enroll_url": "/security"}
        assert _on_disk_rules(tmp_path, "alice") == []

    @pytest.mark.parametrize("action,payload", [
        ("set_default_policy", {"group": "privacy", "policy": "redact"}),
        ("set_category_policy", {"group": "privacy", "category": "body", "policy": "block"}),
        ("toggle_pii_detection", {}),
        ("toggle_pii_category", {"category_key": "medical"}),
    ])
    def test_admin_only_action_hard_fails_with_no_credential_and_writes_nothing(
        self, tmp_path, monkeypatch, action, payload,
    ):
        _seed(tmp_path, monkeypatch, "carol")
        settings = {"privacy": {"default_policy": "allow"}, "pii_detection": {"enabled": True}}
        app, sessions, settings_path = self._install_wide_app(tmp_path, settings, step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)

        r = _post_action(client, action, payload, csrf)

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

        first = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read"]}, csrf)
        assert first.status_code == 428
        assert "webauthn_options" in first.json()
        assert _on_disk_rules(tmp_path, "alice") == []

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = _post_action(client, "add_policy_rule", {
                "group": "gmail.sender", "value": "", "verbs": ["read"],
                "webauthn_assertion": {"id": "Y3JlZC0x"},
            }, csrf)
        assert second.status_code == 200
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

        first = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "redact"}, csrf)
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = _post_action(client, "set_default_policy", {
                "group": "privacy", "policy": "redact",
                "webauthn_assertion": {"id": "Y3JlZC0x"},
            }, csrf)
        assert second.status_code == 200
        assert settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load(settings_path.read_text())["privacy"]["default_policy"] == "redact"

    def test_a_refusal_is_audit_logged_under_the_refused_principal(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        _post_action(client, "add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read"]}, csrf)

        assert len(recorded) == 1
        principal, summary = recorded[0]
        assert principal.id == "alice"
        assert "add_policy_rule" in summary

    def test_a_non_admin_still_cannot_reach_an_admin_only_action_under_step_up(self, tmp_path, monkeypatch):
        # Authorization is checked before step-up -- a non-admin gets the
        # same 403 "forbidden" it always got, never a passkey prompt for an
        # action it could never take either way.
        _seed(tmp_path, monkeypatch, "bob")
        settings = {"privacy": {"default_policy": "block"}}
        app, sessions, settings_path = self._install_wide_app(tmp_path, settings, step_up=self._step_up())
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = _post_action(client, "set_default_policy", {"group": "privacy", "policy": "allow"}, csrf)

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

        r = _post_action(client, "add_policy_rule", {"group": "gmail.sender", "value": "", "verbs": ["read"]}, csrf)

        assert r.status_code == 200
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == _catalogue_rules("gmail.sender", "read")


class TestStepUpBridgeShim:
    """PSC-5: closes PSC-4a/PSC-4b's own flagged follow-up -- org mode's
    settings pages had no WebAuthn ceremony UI wired in, so a step-up
    refusal returned raw JSON where local mode's own page shows a passkey
    prompt. Both pages now carry the exact same shim local mode's
    _render_settings_page does; this only asserts the shim itself is
    present (its own behavior -- retry-on-428, alert-on-403 -- is already
    covered by routes_settings.py's TestStepUpBridgeShim-equivalent for
    local mode and isn't mode-specific)."""

    def test_both_pages_carry_the_webauthn_bridge_shim(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        for url in ("/settings", "/settings/privacy"):
            body = client.get(url).text
            assert "pfWebauthnGet" in body, url
            assert "window.webkit.messageHandlers.pf" in body, url
