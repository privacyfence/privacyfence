"""Tests for org mode's settings surface: every signed-in principal's
own auto-accept rules (read + add + remove), and the admin-only install-wide
PII/privacy policy, read and edited. Exercises web/routes_settings.py's
``build_org_routes``, which renders its pages with the exact same
settings_window_html.build_html() local mode's own settings page uses and
takes every write through one generic ``POST /api/settings/{action}``
(ADR 0032; see that function's own docstring). There is no
web/routes_org_settings.py module, despite this file's name.

What an org-mode settings write looks like:

- Every write is ``POST /api/settings/<action>`` with a JSON body
  (``{**payload, "csrf": csrf}``), the exact same path/shape local mode's
  own dispatcher answers.
- A successful write returns ``200`` with the fresh page state as JSON
  (matching local mode's own dispatcher), not a ``303`` redirect.
- ``add_policy_rule`` takes ``{group, value, verbs: [...]}`` (a list of
  verbs, matching the shared JS's own multi-verb-checkbox "Add a rule"
  form).
- ``toggle_pii_detection``/``toggle_pii_category`` flip the *current*
  value server-side (matching local mode's own toggle semantics, and what
  the shared JS's toggle control actually posts -- an empty payload) --
  they take no explicit ``enabled`` field, unlike
  ``org_install_policy.apply_change``'s own contract;
  see ``routes_settings.py``'s own ``_current_pii_flag``.
- An unauthenticated write gets a JSON ``401`` (the generic dispatcher
  is driven by ``fetch()``, not a real browser navigation, so a redirect
  to an HTML login page would break the bridge's own ``.json()`` parse) --
  the GET pages (``/settings``/``/settings/privacy``) redirect to
  ``/login``.

The rules half of this page reads and writes the v2 ``auto_accept:``
on-disk section (``policy/store.py``) exclusively, via
``auto_accept.get_policy_v2_rules``/``add_policy_v2_rules``/
``remove_policy_v2_rule`` and the shared scope+verb catalogue
(``policy/catalogue.py``). There is no "Trusted resources" (grants)
section and no ``/api/settings/grants/remove`` route -- every resource
type is a ``policy/scopes.py`` scope.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import (
    auto_accept, paths, personal_settings, privacy_filter, scoped_token_proposals, scoped_tokens, secure_files, step_up_config,
)
from privacyfence.agent_identity import AgentIdentity, AgentSource
from privacyfence import webauthn_stepup as wa
from privacyfence.policy import catalogue as policy_catalogue
from privacyfence.policy import describe as policy_describe
from privacyfence.policy import scope_rows
from privacyfence.policy.engine import PolicyRule
from privacyfence.principal import Principal, principal_scope
from privacyfence.settings_controller import _privacy_state_from_config, _short_id
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session
from privacyfence.web import org_stats
from privacyfence.web import routes_settings as ros


BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")
ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)


def _catalogue_rules(group: str, verb: str, value: list | None = None) -> list:
    """The ``PolicyRule``s one ``group``/``[verb]`` "add a rule" submission compiles to -- the same
    call the dispatcher itself makes, so a test can seed or predict exactly what a submission with
    this group/verbs produces."""
    return _stored(policy_catalogue.rules_for_catalogue_entry(group, value, policy_catalogue.parse_verbs([verb])))


def _stored(rules: list) -> list:
    """``rules`` as the stored row compiles them back: one rule per scope entry, carrying the row's
    id (``scope_rows.row_id_for``), which is the id a Remove or Update request names."""
    rows, _ = scope_rows.add_rules([], list(rules))
    return scope_rows.compile_rows(rows)


def _on_disk_rules(tmp_path, principal_id: str) -> list:
    # Reads straight off disk rather than through auto_accept.get_policy_v2_rules(), which
    # requires that principal's config_path to already be registered -- true after a request
    # actually reaches that principal's scope, not for a principal who only attempted a rejected
    # (bad-CSRF/wrong-origin/forbidden) request in this test process.
    settings_path = tmp_path / "users" / principal_id / "authority" / "config" / "settings.yaml"
    raw = yaml.safe_load(settings_path.read_text())
    return ((raw or {}).get("auto_accept") or {}).get("rules") or []


def _seed(tmp_path, monkeypatch, principal_id: str, *, rules=None) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_dir = tmp_path / "users" / principal_id / "authority" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if rules:
        seeded, _ = scope_rows.add_rules([], list(rules))
        cfg["auto_accept"] = scope_rows.rows_to_config(seeded)
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _app(
    *, install_wide_settings=None, sessions=None, install_wide_settings_path="",
    step_up: StepUpConfig | None = None, connector_registry=None,
    scoped_token_policy=None, scoped_token_store=None, scoped_token_page_data=None, org_config=None,
):
    sessions = sessions or org_session.OrgSessionStore()
    routes = ros.build_org_routes(
        sessions=sessions, install_wide_settings=install_wide_settings if install_wide_settings is not None else {},
        install_wide_settings_path=install_wide_settings_path,
        step_up=step_up or StepUpConfig(), step_up_origin=BASE_URL,
        connector_registry=connector_registry,
        scoped_token_policy=scoped_token_policy, scoped_token_store=scoped_token_store,
        scoped_token_page_data=scoped_token_page_data, org_config=org_config,
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
        "add_policy_rule", "remove_policy_rule", "update_policy_rule", "set_policy_rule_verbs",
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

    def test_state_carries_the_grist_stub_keys_with_signin_unavailable(self, tmp_path, monkeypatch):
        # The page script reads these three keys unconditionally; org mode has no Grist sign-in
        # of its own, so "unavailable" is what keeps the Authenticate link disabled there.
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        body = client.get("/settings/privacy").text
        assert '"grist_signin": "unavailable"' in body
        assert '"grist_server_url_pinned": ""' in body
        assert '"grist_auth": {"error": ""}' in body


def _initial_state(body: str) -> dict:
    match = re.search(r"window\.__pfInitialState = (\{.*?\});</script>", body, re.DOTALL)
    assert match, "window.__pfInitialState assignment not found in the settings page"
    return json.loads(match.group(1))


class TestStatsSection:
    """The admin's Stats page (ADR 0217): install-wide counts for an admin, nothing for anyone
    else -- not even the counts, since those describe other people's use."""

    def test_admin_state_counts_every_principal(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "carol")
        _seed(tmp_path, monkeypatch, "alice", rules=_catalogue_rules("drive.folder", "read", ["folder-a"]))
        (tmp_path / "users" / "alice" / "credentials").mkdir()
        (tmp_path / "users" / "alice" / "credentials" / "slack_token.json").write_text("{}", encoding="utf-8")
        app, sessions = _app(org_config={"slack": {"client_id": "scid"}})
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        body = client.get("/settings").text
        assert _capabilities(body)["sections"]["stats"] is True
        stats = _initial_state(body)["stats"]
        assert stats["users"] == {"total": 2, "with_connectors": 1, "with_rules": 1}
        assert {"service": "slack", "label": "Slack", "configured": True, "users": 1} in stats["connectors"]
        assert stats["rules"]["by_connector"] == [{"connector": "drive", "label": "Drive", "rules": 1, "users": 1}]

    def test_non_admin_gets_no_counts_and_no_section(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "bob")
        _seed(tmp_path, monkeypatch, "alice", rules=_catalogue_rules("drive.folder", "read", ["folder-a"]))
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, BOB)
        body = client.get("/settings").text
        assert _capabilities(body)["sections"]["stats"] is False
        assert _initial_state(body)["stats"] == org_stats.empty()


class TestPrivacyStateFromConfig:
    """Org mode's own Privacy Filter state comes from the exact same
    ``_privacy_state_from_config`` local mode's page uses (see
    settings_controller.py), called with ``fail_safe_default="block"`` --
    org mode's fail-closed posture for an install-wide group nobody
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
        # posture (init_privacy_filter already refuses to start the daemon
        # on a malformed group, so reaching this only happens if
        # settings.yaml was hand-edited after that) renders "allow" rather
        # than raising, regardless of fail_safe_default -- kept here as the
        # org-mode-shaped regression guard.
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

    def test_admin_sees_privacy_capabilities_non_admin_does_not(self, tmp_path, monkeypatch):
        """General is every principal's (their own passkey scope lives there, ADR 0091); Privacy
        Filter stays admin-only."""
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)

        _signed_in(client, sessions, ALICE)
        caps = _capabilities(client.get("/settings").text)
        assert caps["mode"] == "org"
        assert caps["is_admin"] is False
        assert caps["sections"]["privacy"] is False
        assert caps["sections"]["general"] is True
        assert caps["sections"]["auto_accept"] is True

        client2 = _client(app)
        _signed_in(client2, sessions, ADMIN)
        caps2 = _capabilities(client2.get("/settings").text)
        assert caps2["is_admin"] is True
        assert caps2["sections"]["privacy"] is True
        assert caps2["sections"]["general"] is True


class TestMoveYourSettingsCard:
    def test_page_carries_the_card_capability_and_the_shim(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        body = client.get("/settings").text
        assert _capabilities(body)["personalTransfer"] is True
        assert "Move your settings" in body
        assert "/api/settings/personal/import" in body


class TestOrgStepUpScope:
    """ADR 0091: any org principal, admin or not, can widen the org's
    ``step_up.scope`` for their own approvals, and never narrow it."""

    ORG_STEP_UP = StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_pii_reads")

    def test_a_non_admin_widens_their_own_scope_only(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "bob")
        app, sessions = _app(step_up=self.ORG_STEP_UP)
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        r = _post_action(client, "set_step_up_scope", {"scope": "writes_and_reads"}, csrf)
        assert r.status_code == 200
        assert r.json()["general"]["step_up_scope_floor"] == "writes_and_pii_reads"
        assert r.json()["general"]["step_up_scope"] == "writes_and_reads"

        client2 = _client(app)
        csrf2 = _signed_in(client2, sessions, BOB)
        r2 = _post_action(client2, "set_step_up_scope", {"scope": "writes_and_pii_reads"}, csrf2)
        assert r2.json()["general"]["step_up_scope"] == "writes_and_pii_reads"

    def test_the_org_scope_is_the_minimum(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self.ORG_STEP_UP)
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        r = _post_action(client, "set_step_up_scope", {"scope": "writes"}, csrf)
        assert r.status_code == 200
        assert r.json()["general"]["step_up_scope"] == "writes_and_pii_reads"

    def test_an_unknown_scope_is_a_400(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=self.ORG_STEP_UP)
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        assert _post_action(client, "set_step_up_scope", {"scope": "everything"}, csrf).status_code == 400

    def test_it_needs_a_passkey_when_the_org_requires_one(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app(step_up=StepUpConfig(
            enabled=True, rp_id="pf.example.com", scope="writes", require_passkey=True,
        ))
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        r = _post_action(client, "set_step_up_scope", {"scope": "writes_and_reads"}, csrf)
        assert r.status_code in (403, 428)


class _FakeDriveClient:
    def __init__(self, names: dict[str, str]):
        self._names = names
        self.calls: list[str] = []

    def get_file_metadata(self, resource_id: str):
        self.calls.append(resource_id)
        return SimpleNamespace(name=self._names.get(resource_id, ""))


class _FakeConnectorRegistry:
    """Stands in for ``ConnectorRegistry``: one principal id -> that principal's connectors."""

    def __init__(self, clients_by_principal: dict[str, dict[str, object]]):
        self._clients = clients_by_principal
        self.requested: list[str] = []

    def get(self, principal: Principal):
        self.requested.append(principal.id)
        clients = self._clients.get(principal.id, {})
        return SimpleNamespace(connectors={name: SimpleNamespace(client=c) for name, c in clients.items()})


class TestRuleValueNames:
    """Org mode shows a rule's resource ids by name, the way local mode's page does (both render
    through ``settings_controller.cached_rule_value``), resolving a missing name through the
    viewing principal's own connectors."""

    FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    def _seed_folder_rule(self, tmp_path, monkeypatch, principal_id: str) -> None:
        _seed(
            tmp_path, monkeypatch, principal_id,
            rules=_catalogue_rules("drive.folder", "update", [self.FOLDER_ID]),
        )

    def test_the_page_shows_the_folder_name_not_its_id(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "alice")
        drive = _FakeDriveClient({self.FOLDER_ID: "Q3 Reports"})
        app, sessions = _app(connector_registry=_FakeConnectorRegistry({"alice": {"drive": drive}}))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        body = client.get("/settings").text
        assert '"value": "My Drive: Q3 Reports"' in body
        assert drive.calls == [self.FOLDER_ID]

    def test_a_cached_name_is_not_looked_up_again(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "alice")
        drive = _FakeDriveClient({self.FOLDER_ID: "Q3 Reports"})
        app, sessions = _app(connector_registry=_FakeConnectorRegistry({"alice": {"drive": drive}}))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        client.get("/settings")
        assert '"value": "My Drive: Q3 Reports"' in client.get("/settings").text
        assert drive.calls == [self.FOLDER_ID]

    def test_names_resolve_only_through_the_viewing_principals_own_connectors(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "bob")
        alices_drive = _FakeDriveClient({self.FOLDER_ID: "Alice's folder"})
        registry = _FakeConnectorRegistry({"alice": {"drive": alices_drive}})
        app, sessions = _app(connector_registry=registry)
        client = _client(app)
        _signed_in(client, sessions, BOB)
        body = client.get("/settings").text
        assert "Alice's folder" not in body
        assert alices_drive.calls == []
        assert registry.requested == ["bob"]

    def test_an_added_rule_comes_back_resolved(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        drive = _FakeDriveClient({self.FOLDER_ID: "Q3 Reports"})
        app, sessions = _app(connector_registry=_FakeConnectorRegistry({"alice": {"drive": drive}}))
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        r = _post_action(
            client, "add_policy_rule", {"group": "drive.folder", "value": self.FOLDER_ID, "verbs": ["update"]}, csrf,
        )
        assert r.status_code == 200
        assert [row["value"] for row in r.json()["auto_accept"]["rules"]] == ["My Drive: Q3 Reports"]

    def test_a_slow_lookup_renders_short_ids_then_names_on_the_next_load(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "alice")
        monkeypatch.setattr(ros, "_RULE_NAME_WAIT_SECONDS", 0.05)
        release = threading.Event()
        drive = _FakeDriveClient({self.FOLDER_ID: "Q3 Reports"})
        original = drive.get_file_metadata

        def slow_get_file_metadata(resource_id):
            assert release.wait(5)
            return original(resource_id)

        drive.get_file_metadata = slow_get_file_metadata
        app, sessions = _app(connector_registry=_FakeConnectorRegistry({"alice": {"drive": drive}}))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        short_value = '"value": ' + json.dumps(_short_id(self.FOLDER_ID))
        assert short_value in client.get("/settings").text
        # Still in flight: a reload renders without starting a second lookup.
        assert short_value in client.get("/settings").text
        release.set()
        for _ in range(100):
            if drive.calls:
                break
            time.sleep(0.01)
        for _ in range(200):
            if '"value": "My Drive: Q3 Reports"' in client.get("/settings").text:
                break
            time.sleep(0.01)
        assert '"value": "My Drive: Q3 Reports"' in client.get("/settings").text
        assert drive.calls == [self.FOLDER_ID]

    def test_without_a_registry_an_uncached_id_is_shortened(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        body = client.get("/settings").text
        assert '"value": ' + json.dumps(_short_id(self.FOLDER_ID)) in body

    def test_a_failing_connector_build_still_renders_the_page(self, tmp_path, monkeypatch):
        self._seed_folder_rule(tmp_path, monkeypatch, "alice")

        class _Broken:
            def get(self, principal):
                raise RuntimeError("simulated connector build failure")

        app, sessions = _app(connector_registry=_Broken())
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 200
        assert '"value": ' + json.dumps(_short_id(self.FOLDER_ID)) in r.text


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

    def test_add_policy_rule_keeps_a_gmail_query_whole(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        query = "in:inbox subject:(a, b)"

        r = _post_action(client, "add_policy_rule", {"group": "gmail.query", "value": query, "verbs": ["read"]}, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            values = [rule.value for rule in auto_accept.get_policy_v2_rules() if rule.predicate == "gmail.query"]
        assert values == [[query]]

    def test_a_title_instead_of_an_id_is_a_400_with_the_message_and_adds_no_rule(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(
            client, "add_policy_rule", {"group": "slack.channel", "value": "#general", "verbs": ["read"]}, csrf,
        )

        assert r.status_code == 400
        assert r.json()["error"].startswith("'#general' is not a Slack ID.")
        with principal_scope(ALICE):
            assert auto_accept.get_policy_v2_rules() == []

    def test_org_add_policy_rule_ignores_a_personal_data_field(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        r = _post_action(client, "add_policy_rule", {
            "group": "grist.document", "value": "DOC1", "verbs": ["read"], "personal_data": True,
        }, csrf)
        assert r.status_code == 200

        with principal_scope(ALICE):
            rules = auto_accept.get_policy_v2_rules()
        assert len(rules) == 1
        assert rules == _catalogue_rules("grist.document", "read", ["DOC1"])
        assert "personal data" not in recorded[0][1]

    def test_the_audit_text_names_the_row_the_rule_landed_in(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda p, s: recorded.append(s))

        _post_action(client, "add_policy_rule", {"group": "drive.folder", "value": "F1", "verbs": ["read"]}, csrf)
        _post_action(client, "add_policy_rule", {"group": "drive.folder", "value": "F1", "verbs": ["update"]}, csrf)

        row_id = scope_rows.row_id_for("drive.folder", ["F1"])
        assert recorded == [
            f"Added auto-accept rule ('drive.folder', allow 'read') to {row_id} (principal=alice)",
            f"Added auto-accept rule ('drive.folder', allow 'update') to {row_id} (principal=alice)",
        ]
        with principal_scope(ALICE):
            assert [row.id for row in auto_accept.get_policy_rows()] == [row_id]

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


def _stale_grist_rule():
    """A Grist document rule as saved before the CSV tools existed: its verbs still imply
    ``grist.import_csv``, its operations lack it."""
    (rule,) = _catalogue_rules_for("grist.document", ["read", "create", "update"], ["DOC1"])
    return dataclasses.replace(rule, operations=rule.operations - {"grist.import_csv"})


def _catalogue_rules_for(group: str, verbs: list[str], value: list | None = None) -> list:
    return _stored(policy_catalogue.rules_for_catalogue_entry(group, value, policy_catalogue.parse_verbs(verbs)))


class TestUpdateRule:
    """update_policy_rule in organization mode: the same widening as local mode, on the signed-in
    principal's own rules only, and audited with the rule and the operations added (ADR 0166)."""

    def test_adds_the_missing_operations_and_keeps_the_id(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "update_policy_rule", {"rule_id": stale.id}, csrf)

        assert r.status_code == 200
        with principal_scope(ALICE):
            (rule,) = auto_accept.get_policy_v2_rules()
        assert rule.id == stale.id
        assert rule.operations == stale.operations | {"grist.import_csv"}
        (row,) = r.json()["auto_accept"]["rules"]
        assert row["missing_tools"] == []

    def test_the_audit_entry_names_the_rule_and_the_operations_added(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: recorded.append(summary))

        _post_action(client, "update_policy_rule", {"rule_id": stale.id}, csrf)

        assert recorded == [
            f"Updated auto-accept rule {stale.id!r}, added grist.import_csv (principal=alice)",
        ]

    def test_an_unknown_id_is_harmless_and_not_audited(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: recorded.append(summary))

        r = _post_action(client, "update_policy_rule", {"rule_id": "r-doesnotexist"}, csrf)

        assert r.status_code == 200
        assert recorded == []
        with principal_scope(ALICE):
            (rule,) = auto_accept.get_policy_v2_rules()
        assert rule.operations == stale.operations

    def test_a_complete_rule_is_not_rewritten_or_audited(self, tmp_path, monkeypatch):
        (full,) = _catalogue_rules_for("grist.document", ["read", "create", "update"], ["DOC1"])
        _seed(tmp_path, monkeypatch, "alice", rules=[full])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: recorded.append(summary))

        r = _post_action(client, "update_policy_rule", {"rule_id": full.id}, csrf)

        assert r.status_code == 200
        assert recorded == []

    def test_a_second_principal_cannot_widen_the_first_principals_rule(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        _seed(tmp_path, monkeypatch, "bob", rules=[])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = _post_action(client, "update_policy_rule", {"rule_id": stale.id}, csrf)

        assert r.status_code == 200
        assert "grist.import_csv" not in _on_disk_rules(tmp_path, "alice")[0]["operations"]

    def test_wrong_csrf_is_rejected(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = _post_action(client, "update_policy_rule", {"rule_id": stale.id}, "not-the-real-token")

        assert r.status_code == 401
        assert "grist.import_csv" not in _on_disk_rules(tmp_path, "alice")[0]["operations"]

    def test_forbidden_when_the_scope_denies_it(self, tmp_path, monkeypatch):
        stale = _stale_grist_rule()
        _seed(tmp_path, monkeypatch, "alice", rules=[stale])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []

        def _deny(action, principal, *, mode):
            recorded.append(action)
            return False

        monkeypatch.setattr(ros, "is_action_permitted", _deny)

        r = _post_action(client, "update_policy_rule", {"rule_id": stale.id}, csrf)

        assert r.status_code == 403
        assert recorded == ["update_policy_rule"]
        assert "grist.import_csv" not in _on_disk_rules(tmp_path, "alice")[0]["operations"]


class TestSetPolicyRuleVerbs:
    """set_policy_rule_verbs in organization mode: the signed-in principal's own rows only, audited
    with the rule and the verbs it now allows."""

    def _seeded(self, tmp_path, monkeypatch, principal=ALICE):
        _seed(tmp_path, monkeypatch, principal.id, rules=_catalogue_rules("drive.folder", "read", ["FOLDER1"]))
        (row,) = scope_rows.add_rules([], _catalogue_rules("drive.folder", "read", ["FOLDER1"]))[0]
        return row

    @staticmethod
    def _on_disk_rows(tmp_path, principal_id="alice"):
        path = tmp_path / "users" / principal_id / "authority" / "config" / "settings.yaml"
        return scope_rows.rows_from_config(yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_adds_a_verb_and_audits_it(self, tmp_path, monkeypatch):
        row = self._seeded(tmp_path, monkeypatch)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: recorded.append(summary))

        r = _post_action(client, "set_policy_rule_verbs", {"rule_id": row.id, "verbs": ["update", "read"]}, csrf)

        assert r.status_code == 200
        (after,) = self._on_disk_rows(tmp_path)
        assert after.id == row.id
        assert {v.value for v in after.verbs} == {"read", "update"}
        assert recorded == [
            f"Changed auto-accept rule {row.id!r} to allow 'read, update' (principal=alice)",
        ]

    def test_a_refusal_writes_and_audits_nothing(self, tmp_path, monkeypatch):
        row = self._seeded(tmp_path, monkeypatch)
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        recorded = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: recorded.append(summary))

        for payload in (
            {"rule_id": row.id, "verbs": []},
            {"rule_id": row.id, "verbs": ["send"]},
            {"rule_id": "r-doesnotexist", "verbs": ["read", "update"]},
        ):
            assert _post_action(client, "set_policy_rule_verbs", payload, csrf).status_code == 200

        assert recorded == []
        assert self._on_disk_rows(tmp_path) == [row]

    def test_a_second_principal_cannot_edit_the_first_principals_rule(self, tmp_path, monkeypatch):
        row = self._seeded(tmp_path, monkeypatch)
        _seed(tmp_path, monkeypatch, "bob", rules=[])
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, BOB)

        r = _post_action(client, "set_policy_rule_verbs", {"rule_id": row.id, "verbs": ["read", "update"]}, csrf)

        assert r.status_code == 200
        assert self._on_disk_rows(tmp_path) == [row]


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

    def test_leaves_scoped_tokens_alone(self, tmp_path, monkeypatch):
        rules = _catalogue_rules("gmail.sender", "read")
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        _seed(tmp_path, monkeypatch, "bob", rules=rules)
        other = PolicyRule(
            id="r-other", predicate="gmail.anything", value=None, operations=frozenset({"gmail.read_message"}),
        )
        token_rules = [*rules, other]
        held = tuple(row for rule in token_rules for row in scope_rows.rows_for_rules([rule], rule.id))
        files = {}
        for principal in (ALICE, BOB):
            token = scoped_tokens.ScopedToken(
                id="st-000000000000", label="sync", digest="d", principal_id=principal.id,
                principal_email=principal.email, principal_display_name=principal.display_name,
                created_at=0.0, expires_at=1e12, tools=frozenset({"gmail_get_message"}), rules=held,
            )
            files[principal.id] = paths.authority_dir(principal) / scoped_tokens.STORE_FILE_NAME
            secure_files.atomic_write_json(
                files[principal.id], {"version": 2, "tokens": [scoped_tokens._to_json(token)]},
            )
        before = {pid: path.read_bytes() for pid, path in files.items()}
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        r = _post_action(client, "remove_policy_rule", {"rule_id": rules[0].id}, csrf)
        assert r.status_code == 200

        assert {pid: path.read_bytes() for pid, path in files.items()} == before
        carried = {pid: json.loads(raw)["tokens"][0]["rules"] for pid, raw in before.items()}
        assert carried == {pid: [scope_rows.row_to_dict(row) for row in held] for pid in files}
        assert {row["id"] for row in carried["alice"]} == {rules[0].id, "r-other"}

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
    """The admin-only write surface on /settings/privacy."""

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
    """Every org-routed ``_SENSITIVE_ACTIONS`` member (``add_policy_rule``,
    ``remove_policy_rule``, and the four install-wide privacy/PII writes)
    requires step-up, the same as local mode's own generic dispatcher
    (ADR 0034). An agent that cannot forge a WebAuthn assertion could
    otherwise add an always-allow rule, or flip the install-wide PII/privacy
    policy, while step-up is supposed to be in force. This page carries the
    same PF_WEBAUTHN_JS/bridge-shim ceremony UI local mode's own settings
    page does, so a 428/403 here shows the same passkey prompt instead of a
    raw JSON body -- TestStepUpBridgeShim below covers that the shim is
    present; this class is server-side only."""

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
    """Org mode's settings pages carry the WebAuthn ceremony UI, so a
    step-up refusal shows the same passkey prompt local mode's own page
    does rather than raw JSON. Both pages carry the exact same shim local
    mode's _render_settings_page does; this only asserts the shim itself is
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


class TestV1PerUserSettings:
    """A per-user settings.yaml still carrying a pre-``auto_accept:`` section is refused by
    ``policy.store.reject_v1_sections``. The daemon loads that file per principal, on first use, so
    the page must say what is wrong rather than fail with a bare 500."""

    @staticmethod
    def _seed_v1(tmp_path, monkeypatch, principal_id: str) -> None:
        _seed(tmp_path, monkeypatch, principal_id)
        settings_path = tmp_path / "users" / principal_id / "authority" / "config" / "settings.yaml"
        settings_path.write_text(
            yaml.safe_dump({"auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]}}),
            encoding="utf-8",
        )

    def test_admin_page_names_the_file_and_the_fix(self, tmp_path, monkeypatch):
        self._seed_v1(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ADMIN)
        r = client.get("/settings")
        assert r.status_code == 500
        assert "auto_accept_rules" in r.text
        assert str(Path("users", "carol", "authority", "config", "settings.yaml")) in r.text

    def test_non_admin_page_explains_without_the_server_path(self, tmp_path, monkeypatch):
        self._seed_v1(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/settings")
        assert r.status_code == 500
        assert "An administrator needs to update it" in r.text
        assert str(tmp_path) not in r.text

    def test_settings_action_returns_the_message_as_json(self, tmp_path, monkeypatch):
        self._seed_v1(tmp_path, monkeypatch, "carol")
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ADMIN)
        r = _post_action(client, "toggle_pii_detection", {}, csrf)
        assert r.status_code == 500
        assert "auto_accept_rules" in r.json()["error"]


class TestScopedTokenRoutes:
    """The four scoped token routes of org mode (ADR 0165): the administrator's opt-in, the
    org gates, the one-time secret and the token working on /mcp as its owner."""

    ENABLED = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=30)
    BODY = {"label": "sales-sync", "days": 7, "rules": [], "tools": ["gmail_get_message"]}

    @staticmethod
    def _store(*principals: Principal) -> scoped_tokens.ScopedTokenStore:
        return scoped_tokens.ScopedTokenStore(lambda: list(principals))

    def _setup(self, tmp_path, monkeypatch, *, policy=None, step_up=None, rules=None):
        _seed(tmp_path, monkeypatch, "alice", rules=rules)
        _seed(tmp_path, monkeypatch, "bob")
        store = self._store(ALICE, BOB)
        app, sessions = _app(
            scoped_token_policy=self.ENABLED if policy is None else policy, scoped_token_store=store,
            step_up=step_up,
        )
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        return client, csrf, store

    @staticmethod
    def _post(client, action: str, payload: dict, csrf: str, **kwargs):
        return client.post(f"/api/settings/scoped_tokens/{action}", json={**payload, "csrf": csrf}, **kwargs)

    def test_scoped_tokens_are_refused_when_the_admin_has_not_enabled_them(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(
            tmp_path, monkeypatch, policy=scoped_tokens.ScopedTokenPolicy(enabled=False, max_days=90),
        )
        for action, payload in (
            ("create", {**self.BODY, "confirmed": True}),
            ("revoke", {"token_id": "st-x", "confirmed": True}),
            ("revoke_all", {"confirmed": True}),
        ):
            r = self._post(client, action, payload, csrf)
            assert r.status_code == 403, action
            assert r.json() == {"error": ros._SCOPED_TOKENS_DISABLED}
        assert store.tokens_for(ALICE) == []
        # The route's text is the store's own.
        with pytest.raises(scoped_tokens.ScopedTokenError) as excinfo:
            store.create(ALICE, label="x", days=1, rules=[], tools=["gmail_get_message"],
                         policy=scoped_tokens.ScopedTokenPolicy(enabled=False, max_days=90))
        assert str(excinfo.value) == ros._SCOPED_TOKENS_DISABLED
        # Not signed in: 401, whatever the policy.
        anonymous = _client(_app()[0])
        assert anonymous.post("/api/settings/scoped_tokens/create", json=self.BODY).status_code == 401

    def test_the_gates_run_before_anything_is_created(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        assert self._post(client, "create", {**self.BODY, "confirmed": True}, "wrong").status_code == 401
        assert self._post(client, "create", self.BODY, csrf).json() == {"error": "confirmation required"}
        assert self._post(client, "create", {"confirmed": True, "label": 3}, csrf).status_code == 400
        assert store.tokens_for(ALICE) == []

    def test_the_personal_data_flag_reaches_the_store(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "allow_personal_data": True, "confirmed": True}, csrf)

        assert r.status_code == 200
        [token] = store.tokens_for(ALICE)
        assert token.allow_personal_data is True
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            summaries = [e.summary for e in get_audit_logger().recent_entries(20)]
        assert any(f"({token.id})" in text and ", personal data allowed (principal=alice)" in text for text in summaries)

    def test_org_create_stores_the_reads_flag(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "allow_personal_data_reads": True, "confirmed": True}, csrf)

        assert r.status_code == 200
        [token] = store.tokens_for(ALICE)
        assert token.allow_personal_data_reads is True
        assert token.allow_personal_data is False

    def test_org_audit_summary_names_the_reads_flag(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "allow_personal_data_reads": True, "confirmed": True}, csrf)

        assert r.status_code == 200
        [token] = store.tokens_for(ALICE)
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            summaries = [e.summary for e in get_audit_logger().recent_entries(20)]
        assert any(
            f"({token.id})" in text and ", personal data in reads allowed (principal=alice)" in text
            for text in summaries
        )

    def test_a_token_without_the_flag_does_not_allow_personal_data(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        assert self._post(client, "create", {**self.BODY, "confirmed": True}, csrf).status_code == 200
        [token] = store.tokens_for(ALICE)
        assert token.allow_personal_data is False
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            assert not any("personal data allowed" in e.summary for e in get_audit_logger().recent_entries(20))

    def test_a_non_bool_personal_data_flag_is_a_400(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "allow_personal_data": "yes", "confirmed": True}, csrf)

        assert r.status_code == 400
        assert store.tokens_for(ALICE) == []

    def test_create_returns_the_secret_once_with_no_store(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "confirmed": True}, csrf)

        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        secret = body["secret"]
        assert secret.startswith(scoped_tokens.TOKEN_PREFIX)
        assert body["created"]["label"] == "sales-sync"
        # Neither the list, nor the page, nor the file, nor the audit log ever has it again.
        [token] = store.tokens_for(ALICE)
        assert token.digest != secret
        page = client.get("/settings")
        assert secret not in page.text
        assert secret not in (tmp_path / "users" / "alice" / "authority" / "scoped_tokens.json").read_text()
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            assert secret not in json.dumps([e.summary for e in get_audit_logger().recent_entries(20)])
            summaries = [e.summary for e in get_audit_logger().recent_entries(20)]
        assert any(f"Created scoped token 'sales-sync' ({token.id})" in text for text in summaries)
        # Errors answer 400 with the store's own text and no secret.
        again = self._post(client, "create", {**self.BODY, "confirmed": True}, csrf)
        assert again.status_code == 400
        assert again.json() == {"error": "You already have a token with that name."}

    def test_create_takes_the_org_step_up(self, tmp_path, monkeypatch):
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)
        client, csrf, store = self._setup(tmp_path, monkeypatch, step_up=step_up)

        for action, payload in (
            ("create", {**self.BODY, "confirmed": True}),
            ("revoke", {"token_id": "st-x", "confirmed": True}),
            ("revoke_all", {"confirmed": True}),
        ):
            r = self._post(client, action, payload, csrf)
            assert r.status_code == 403, action
            assert r.json() == {"error": "passkey_enrollment_required", "enroll_url": "/security"}
        assert store.tokens_for(ALICE) == []

    def test_the_step_up_is_not_asked_for_when_the_org_does_not_require_a_passkey(self, tmp_path, monkeypatch):
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=False)
        client, csrf, store = self._setup(tmp_path, monkeypatch, step_up=step_up)
        assert self._post(client, "create", {**self.BODY, "confirmed": True}, csrf).status_code == 200

    def test_the_created_token_verifies_on_mcp_as_its_owner(self, tmp_path, monkeypatch):
        import asyncio

        from privacyfence.web import mcp_auth

        rule = _catalogue_rules("gmail.sender", "read")[0]
        client, csrf, store = self._setup(tmp_path, monkeypatch, rules=[rule])
        scoped_tokens.set_store(store)

        body = {**self.BODY, "rules": [{"kind": "copy", "rule_id": rule.id}], "confirmed": True}
        secret = self._post(client, "create", body, csrf).json()["secret"]

        access = asyncio.run(mcp_auth.ScopedTokenVerifier().verify_token(secret))
        assert access is not None
        principal = mcp_auth.principal_from_access_token(access)
        assert (principal.id, principal.email, principal.display_name) == ("alice", "alice@example.com", "Alice")
        assert principal.is_admin is False
        scope = mcp_auth.scope_from_access_token(access)
        assert scope is not None and scope.tools == {"gmail_get_message"}
        assert scope.compiled_rules() == [rule]  # its own copy of the rule
        # A rule the person does not have is refused, not silently dropped.
        bad = self._post(client, "create", {**self.BODY, "label": "other", "rules": [{"kind": "copy", "rule_id": "nope"}], "confirmed": True}, csrf)
        assert bad.status_code == 400

    def test_a_new_rule_request_creates_a_token_with_that_rule(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        new = {"kind": "new", "group": "gmail.sender", "value": "a@example.com", "verbs": ["read"]}

        r = self._post(client, "create", {**self.BODY, "rules": [new], "confirmed": True}, csrf)

        assert r.status_code == 200
        (token,) = store.tokens_for(ALICE)
        assert len(token.rules) == 1 and token.rules[0].scope == "gmail.sender"

    def test_a_body_with_rule_ids_and_no_rules_is_refused(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        body = {k: v for k, v in self.BODY.items() if k != "rules"}

        r = self._post(client, "create", {**body, "rule_ids": [], "confirmed": True}, csrf)

        assert r.status_code == 400
        assert store.tokens_for(ALICE) == []

    def test_days_over_the_admin_cap_are_refused(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)

        r = self._post(client, "create", {**self.BODY, "days": 31, "confirmed": True}, csrf)

        assert r.status_code == 400
        assert r.json() == {"error": "A token lasts 1 to 30 days."}
        assert store.tokens_for(ALICE) == []
        assert self._post(client, "create", {**self.BODY, "days": 30, "confirmed": True}, csrf).status_code == 200

    def test_revoke_and_revoke_all(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        for label in ("one", "two", "three"):
            assert self._post(client, "create", {**self.BODY, "label": label, "confirmed": True}, csrf).status_code == 200
        bob_token, _secret = store.create(
            BOB, label="bobs", days=5, rules=[], tools=["gmail_get_message"], policy=self.ENABLED,
        )
        one = next(t for t in store.tokens_for(ALICE) if t.label == "one")

        gone = self._post(client, "revoke", {"token_id": one.id, "confirmed": True}, csrf)
        assert gone.status_code == 200 and gone.json() == {"revoked": {"id": one.id}}
        assert sorted(t.label for t in store.tokens_for(ALICE)) == ["three", "two"]
        # Someone else's token, or none at all: 404, and nothing changes.
        for token_id in (bob_token.id, "st-missing"):
            miss = self._post(client, "revoke", {"token_id": token_id, "confirmed": True}, csrf)
            assert (miss.status_code, miss.json()) == (404, {"error": "No such token."})
        assert store.get("bob", bob_token.id) is not None

        everything = self._post(client, "revoke_all", {"confirmed": True}, csrf)
        assert everything.json() == {"revoked": 2}
        assert store.tokens_for(ALICE) == []
        assert store.get("bob", bob_token.id) is not None
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            summaries = [e.summary for e in get_audit_logger().recent_entries(20)]
        assert any("Revoked all scoped tokens (2)" in text for text in summaries)

    def test_rename(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        for label in ("one", "two"):
            assert self._post(client, "create", {**self.BODY, "label": label, "confirmed": True}, csrf).status_code == 200
        bob_token, _secret = store.create(
            BOB, label="bobs", days=5, rules=[], tools=["gmail_get_message"], policy=self.ENABLED,
        )
        one = next(t for t in store.tokens_for(ALICE) if t.label == "one")

        done = self._post(client, "rename", {"token_id": one.id, "label": "uno", "confirmed": True}, csrf)
        assert (done.status_code, done.json()) == (200, {"renamed": {"id": one.id, "label": "uno"}})
        assert sorted(t.label for t in store.tokens_for(ALICE)) == ["two", "uno"]
        miss = self._post(client, "rename", {"token_id": bob_token.id, "label": "x", "confirmed": True}, csrf)
        assert (miss.status_code, miss.json()) == (404, {"error": "No such token."})
        assert store.get("bob", bob_token.id).label == "bobs"
        taken = self._post(client, "rename", {"token_id": one.id, "label": "two", "confirmed": True}, csrf)
        assert (taken.status_code, taken.json()) == (400, {"error": "You already have a token with that name."})
        same = self._post(client, "rename", {"token_id": one.id, "label": "uno", "confirmed": True}, csrf)
        assert same.status_code == 200
        with principal_scope(ALICE):
            from privacyfence.audit_log import get_audit_logger

            summaries = [e.summary for e in get_audit_logger().recent_entries(20)]
        renamed = [t for t in summaries if "Renamed scoped token" in t]
        assert len(renamed) == 1
        assert renamed[0] == f"Renamed scoped token 'one' to 'uno' ({one.id}) (principal=alice)"

    def test_rename_is_refused_when_scoped_tokens_are_disabled(self, tmp_path, monkeypatch):
        client, csrf, _store = self._setup(
            tmp_path, monkeypatch, policy=scoped_tokens.ScopedTokenPolicy(enabled=False, max_days=90),
        )
        r = self._post(client, "rename", {"token_id": "st-x", "label": "y", "confirmed": True}, csrf)
        assert r.status_code == 403

    def test_the_page_offers_the_section_only_once_enabled(self, tmp_path, monkeypatch):
        data = {"delivery": "once", "max_days": 30, "default_days": 30, "warn_days": 14,
                "tokens": [], "rules": [], "tools": []}
        _seed(tmp_path, monkeypatch, "alice")
        for policy, expected in ((self.ENABLED, True), (scoped_tokens.ScopedTokenPolicy(False, 90), False)):
            app, sessions = _app(scoped_token_policy=policy, scoped_token_page_data=lambda _principal: data)
            client = _client(app)
            _signed_in(client, sessions, ALICE)
            body = client.get("/settings").text
            assert _capabilities(body)["sections"]["scoped_tokens"] is expected
            assert ("window.__pfScopedTokens = " in body) is expected
        # No page-data builder (no /mcp endpoint): no section, even when enabled.
        app, sessions = _app(scoped_token_policy=self.ENABLED)
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        assert _capabilities(client.get("/settings").text)["sections"]["scoped_tokens"] is False

    def test_the_bridge_shim_uses_the_org_confirm_text_and_the_dialog(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        body = client.get("/settings").text
        assert "Create this token? It is shown once and cannot be shown again." in body
        assert "window.__pfShowScopedSecret(res.state.secret" in body


class TestScopedTokenProposalRoutes:
    """Org mode's review page and the create_from_proposal and decline routes: the administrator's
    two opt-ins, create's gates bound to the proposal, and one proposal per principal."""

    CREATE = "/api/settings/scoped_tokens/create_from_proposal"
    DECLINE = "/api/settings/scoped_tokens/decline_proposal"
    ON = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=30, allow_ai_proposals=True)
    AGENT = AgentIdentity(id="claude-code", name="Claude Code", version="1", source=AgentSource.NONE)
    REQUEST = {
        "label": "sales-sync", "days": 7,
        "rules": [{"kind": "new", "group": "gmail.sender", "value": "a@example.com", "verbs": ["read", "update"]}],
        "tools": ["gmail_get_message", "gmail_list_messages"],
        "allow_personal_data": False, "allow_personal_data_reads": False,
    }

    def _setup(self, tmp_path, monkeypatch, *, policy=None, step_up=None):
        _seed(tmp_path, monkeypatch, "alice")
        _seed(tmp_path, monkeypatch, "bob")
        store = scoped_tokens.ScopedTokenStore(lambda: [ALICE, BOB])
        app, sessions = _app(
            scoped_token_policy=self.ON if policy is None else policy, scoped_token_store=store, step_up=step_up,
            scoped_token_page_data=lambda principal: {"tokens": [], "proposals": [], "proposals_enabled": True},
        )
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)
        return client, csrf, store

    def _propose(self, principal=ALICE, **over):
        request = scoped_tokens.normalise_create_request({**self.REQUEST, **over})
        proposal = scoped_token_proposals.new_proposal(
            principal=principal, request=request, source="bundle", agent=self.AGENT, reason="a weekly job",
        )
        scoped_token_proposals.get_registry().add(proposal)
        return proposal

    @staticmethod
    def _body(proposal, csrf, **over):
        body = {
            **proposal.request, "proposal_id": proposal.id, "typed_label": proposal.request["label"],
            "confirmed": True, "csrf": csrf,
        }
        return {**body, **over}

    @staticmethod
    def _live(proposal, principal=ALICE) -> bool:
        return scoped_token_proposals.get_registry().get(principal.id, proposal.id) is not None

    @staticmethod
    def _summaries(principal=ALICE) -> list[str]:
        from privacyfence.audit_log import get_audit_logger

        with principal_scope(principal):
            return [e.summary for e in get_audit_logger().recent_entries(50)]

    @staticmethod
    def _decisions(principal=ALICE) -> list[tuple[str, str]]:
        from privacyfence.audit_log import get_audit_logger

        with principal_scope(principal):
            return [(e.decision, e.summary) for e in get_audit_logger().recent_entries(50)]

    def test_refused_when_proposals_disabled_in_the_bundle(self, tmp_path, monkeypatch):
        off = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=30, allow_ai_proposals=False)
        client, csrf, store = self._setup(tmp_path, monkeypatch, policy=off)
        proposal = self._propose()

        r = client.post(self.CREATE, json=self._body(proposal, csrf))

        assert r.status_code == 403
        assert r.json() == {"error": scoped_tokens.PROPOSALS_DISABLED_MESSAGE}
        assert store.tokens_for(ALICE) == [] and self._live(proposal)
        # Scoped tokens off altogether: the older gate answers first.
        none = scoped_tokens.ScopedTokenPolicy(enabled=False, max_days=30, allow_ai_proposals=True)
        client, csrf, store = self._setup(tmp_path, monkeypatch, policy=none)
        r = client.post(self.CREATE, json=self._body(proposal, csrf))
        assert r.status_code == 403 and r.json() == {"error": ros._SCOPED_TOKENS_DISABLED}

    def test_secret_shown_once_with_no_store(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()

        r = client.post(self.CREATE, json=self._body(proposal, csrf))

        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        secret = body["secret"]
        assert secret.startswith(scoped_tokens.TOKEN_PREFIX)
        assert body["created"]["label"] == "sales-sync"
        [token] = store.tokens_for(ALICE)
        assert token.id == body["created"]["id"]
        assert not self._live(proposal)
        assert secret not in json.dumps(self._summaries())
        assert secret not in client.get("/settings").text
        # Spent: a second go is gone, and creates nothing.
        again = client.post(self.CREATE, json=self._body(proposal, csrf))
        assert again.status_code == 410
        assert again.json() == {"error": scoped_token_proposals.GONE_MESSAGE}
        assert len(store.tokens_for(ALICE)) == 1

    def test_org_step_up_bound_to_the_proposal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client, csrf, store = self._setup(tmp_path, monkeypatch, step_up=step_up)
        first, second = self._propose(), self._propose(label="other")
        narrowed = {"tools": ["gmail_get_message"]}
        verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()

        def _retry(asserted_for, submitted_for):
            """An assertion obtained for one request, replayed for another."""
            assert client.post(self.CREATE, json=self._body(asserted_for[0], csrf, **asserted_for[1])).status_code == 428
            with patch.object(wa.webauthn, "verify_authentication_response", return_value=verified):
                return client.post(self.CREATE, json={
                    **self._body(submitted_for[0], csrf, **submitted_for[1]), "webauthn_assertion": {"id": "Y3JlZC0x"},
                })

        # Obtained for another proposal, or for the un-narrowed request: refused for this one.
        assert _retry((first, narrowed), (second, narrowed)).status_code != 200
        assert _retry((second, {}), (second, narrowed)).status_code != 200
        assert store.tokens_for(ALICE) == []
        # Obtained for exactly this request: accepted.
        assert _retry((second, narrowed), (second, narrowed)).status_code == 200
        assert [t.label for t in store.tokens_for(ALICE)] == ["other"]

    def test_the_checks_run_before_step_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)
        client, csrf, store = self._setup(tmp_path, monkeypatch, step_up=step_up)
        proposal = self._propose()
        assert client.post(self.CREATE, json=self._body(proposal, csrf, typed_label="nope")).status_code == 400
        assert client.post(self.CREATE, json=self._body(proposal, csrf, days=8)).status_code == 400
        gone = dataclasses.replace(proposal, id="stp-unknown")
        assert client.post(self.CREATE, json=self._body(gone, csrf)).status_code == 410
        # Only a request that would succeed meets the passkey (here: no credential enrolled).
        assert client.post(self.CREATE, json=self._body(proposal, csrf)).status_code == 403
        assert store.tokens_for(ALICE) == []

    def test_typed_label_must_match(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()
        for typed in ("sales-syn", "Sales-Sync", "", None):
            r = client.post(self.CREATE, json=self._body(proposal, csrf, typed_label=typed))
            assert r.status_code == 400
            assert r.json() == {"error": "Type the token's name exactly to confirm."}
        assert store.tokens_for(ALICE) == [] and self._live(proposal)
        # A renamed token is confirmed by its own name.
        r = client.post(self.CREATE, json=self._body(proposal, csrf, label="shorter", typed_label="shorter"))
        assert r.status_code == 200
        assert [t.label for t in store.tokens_for(ALICE)] == ["shorter"]

    @pytest.mark.parametrize("change", [
        {"days": 8},
        {"tools": ["gmail_get_message", "gmail_list_messages", "tasks_list_tasks"]},
        {"allow_personal_data": True},
        {"allow_personal_data_reads": True},
        {"rules": [{"kind": "copy", "rule_id": "r2"}]},
        {"rules": [{"kind": "new", "group": "gmail.sender", "value": "b@example.com", "verbs": ["read"]}]},
        {"rules": [{"kind": "new", "group": "gmail.sender", "value": "a@example.com", "verbs": ["read", "delete"]}]},
    ])
    def test_widening_is_refused(self, tmp_path, monkeypatch, change):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()

        r = client.post(self.CREATE, json=self._body(proposal, csrf, **change))

        assert r.status_code == 400
        assert r.json() == {"error": scoped_token_proposals.WIDENED_MESSAGE}
        assert store.tokens_for(ALICE) == [] and self._live(proposal)

    def test_narrowing_is_accepted(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()
        narrowed = {
            "days": 3, "tools": ["gmail_get_message"],
            "rules": [{"kind": "new", "group": "gmail.sender", "value": "a@example.com", "verbs": ["read"]}],
        }

        assert client.post(self.CREATE, json=self._body(proposal, csrf, **narrowed)).status_code == 200
        [token] = store.tokens_for(ALICE)
        assert token.tools == {"gmail_get_message"} and len(token.rules) == 1

    def test_another_principals_proposal_is_gone(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        bobs = self._propose(principal=BOB)

        r = client.post(self.CREATE, json=self._body(bobs, csrf))
        assert r.status_code == 410
        assert r.json() == {"error": scoped_token_proposals.GONE_MESSAGE}
        d = client.post(self.DECLINE, json={"proposal_id": bobs.id, "csrf": csrf})
        assert d.status_code == 410
        page = client.get(f"/settings/scoped_tokens/proposals/{bobs.id}")
        assert 'window.__pfScopedProposal = {"missing": true}' in page.text
        assert store.tokens_for(ALICE) == [] and self._live(bobs, BOB)

    def test_create_failure_restores_the_proposal(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()
        assert client.post(self.CREATE, json=self._body(proposal, csrf)).status_code == 200
        second = self._propose()  # same label: the store refuses it

        r = client.post(self.CREATE, json=self._body(second, csrf))

        assert r.status_code == 400
        assert r.json() == {"error": "You already have a token with that name."}
        assert self._live(second)
        assert [d for d, _ in self._decisions() if d == "scoped_token_proposal_accepted"] == [
            "scoped_token_proposal_accepted",
        ]  # the first, successful one only

    def test_gates_need_a_session_csrf_and_confirmation(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()
        assert client.post(self.CREATE, json=self._body(proposal, "wrong")).status_code == 401
        unconfirmed = {k: v for k, v in self._body(proposal, csrf).items() if k != "confirmed"}
        assert client.post(self.CREATE, json=unconfirmed).json() == {"error": "confirmation required"}
        no_id = {k: v for k, v in self._body(proposal, csrf).items() if k != "proposal_id"}
        assert client.post(self.CREATE, json=no_id).json() == {"error": "invalid request"}
        anonymous = _client(_app(scoped_token_policy=self.ON)[0])
        assert anonymous.post(self.CREATE, json=self._body(proposal, csrf)).status_code == 401
        assert anonymous.post(self.DECLINE, json={"proposal_id": proposal.id, "csrf": csrf}).status_code == 401
        assert client.post(self.DECLINE, json={"proposal_id": proposal.id, "csrf": "wrong"}).status_code == 401
        assert store.tokens_for(ALICE) == [] and self._live(proposal)

    def test_review_page_redirects_when_disabled(self, tmp_path, monkeypatch):
        proposals_off = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=30, allow_ai_proposals=False)
        tokens_off = scoped_tokens.ScopedTokenPolicy(enabled=False, max_days=30, allow_ai_proposals=True)
        for policy in (proposals_off, tokens_off):
            client, _csrf, _store = self._setup(tmp_path, monkeypatch, policy=policy)
            proposal = self._propose()
            for path in ("/settings/scoped_tokens", f"/settings/scoped_tokens/proposals/{proposal.id}"):
                r = client.get(path)
                assert r.status_code == 303, path
                assert r.headers["location"] == "/settings"
        # Signed out: to the login page.
        anonymous = _client(_app(scoped_token_policy=self.ON)[0])
        assert anonymous.get("/settings/scoped_tokens").status_code == 302

    def test_review_page_embeds_the_proposal(self, tmp_path, monkeypatch):
        client, _csrf, _store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()

        section = client.get("/settings/scoped_tokens")
        assert section.status_code == 200
        assert 'window.__pfInitialSection = "scoped_tokens"' in section.text
        assert "window.__pfScopedProposal = " not in section.text

        r = client.get(f"/settings/scoped_tokens/proposals/{proposal.id}")
        assert r.status_code == 200
        assert 'window.__pfInitialSection = "scoped_tokens"' in r.text
        assert "window.__pfScopedProposal = " in r.text
        assert proposal.id in r.text and "sales-sync" in r.text and "a weekly job" in r.text
        assert self._live(proposal)
        gone = client.get("/settings/scoped_tokens/proposals/stp-nope")
        assert gone.status_code == 200 and 'window.__pfScopedProposal = {"missing": true}' in gone.text

    def test_decline_takes_and_audits(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()

        r = client.post(self.DECLINE, json={"proposal_id": proposal.id, "csrf": csrf})

        assert r.status_code == 200 and r.json() == {"declined": True}
        assert not self._live(proposal)
        assert ("scoped_token_proposal_declined", "Token proposal 'sales-sync' declined") in self._decisions()
        again = client.post(self.DECLINE, json={"proposal_id": proposal.id, "csrf": csrf})
        assert again.status_code == 410
        assert again.json() == {"error": scoped_token_proposals.GONE_MESSAGE}
        assert store.tokens_for(ALICE) == []

    def test_accept_audit_names_principal_and_proposal(self, tmp_path, monkeypatch):
        client, csrf, store = self._setup(tmp_path, monkeypatch)
        proposal = self._propose()

        r = client.post(self.CREATE, json=self._body(proposal, csrf, label="shorter", typed_label="shorter"))

        assert r.status_code == 200
        [token] = store.tokens_for(ALICE)
        decisions = self._decisions()
        assert (
            "scoped_token_proposal_accepted",
            f"Token proposal 'sales-sync' accepted as 'shorter' ({token.id})",
        ) in decisions
        [created] = [text for text in self._summaries() if text.startswith("Created scoped token")]
        assert created.startswith(f"Created scoped token 'shorter' ({token.id}), 7 days")
        assert created.endswith(" from a proposal (principal=alice)")
        assert r.json()["secret"] not in created


class TestPersonalSettingsRoutes:
    """Export/import of a person's own rules and step-up level in organization mode
    (web/personal_settings_routes.py, wired by build_org_routes)."""

    NOW = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    FILE_FIELD = ("privacyfence-personal-settings.json", None, "application/json")
    ENROLLED = SimpleNamespace(new_sign_count=1, credential_device_type=None, credential_backed_up=False)

    @pytest.fixture(autouse=True)
    def _data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    @staticmethod
    def _row(scope: str, value: list[str]) -> scope_rows.ScopeRow:
        verbs = scope_rows.scope_verbs(scope)
        return scope_rows.ScopeRow(
            id=scope_rows.row_id_for(scope, value), scope=scope, value=value,
            verbs=frozenset(verbs), operations=scope_rows.implied_operations(scope, verbs),
        )

    def _file(self, rows, step_up_scope=None, *, mode="org") -> bytes:
        return personal_settings.export_bytes(
            personal_settings.build_export(rows, step_up_scope, mode=mode, version="6.3.0", now=self.NOW),
        )

    @staticmethod
    def _import(client, raw: bytes, csrf: str, **fields):
        fields.setdefault("sha256", hashlib.sha256(raw).hexdigest())
        fields.setdefault("confirmed", "true")
        return client.post(
            "/api/settings/personal/import", data={"csrf": csrf, **fields},
            files={"file": ("privacyfence-personal-settings.json", raw, "application/json")},
        )

    def _setup(self, tmp_path, monkeypatch, principal=ALICE, *, step_up=None, seed=("alice", "bob")):
        for principal_id in seed:
            _seed(tmp_path, monkeypatch, principal_id)
        app, sessions = _app(step_up=step_up)
        client = _client(app)
        return client, _signed_in(client, sessions, principal)

    @staticmethod
    def _rows_of(principal: Principal) -> list:
        with principal_scope(principal):
            from privacyfence.daemon_main import _load_principal_settings
            _load_principal_settings(install_wide_config={})
            return auto_accept.get_policy_rows()

    def test_a_signed_in_user_exports_a_file_that_parses(self, tmp_path, monkeypatch):
        client, _csrf = self._setup(tmp_path, monkeypatch)
        r = client.get("/api/settings/personal/export")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f'attachment; filename="{personal_settings.FILE_NAME}"'
        document = personal_settings.parse_document(r.content)
        assert document.rows == ()
        assert document.source_mode == "org"

    def test_two_principals_are_isolated(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice", rules=_catalogue_rules("gmail.sender", "read", ["a@example.com"]))
        _seed(tmp_path, monkeypatch, "bob", rules=_catalogue_rules("gmail.sender", "read", ["b@example.com"]))
        app, sessions = _app()
        client = _client(app)
        csrf = _signed_in(client, sessions, ALICE)

        exported = client.get("/api/settings/personal/export").content.decode()
        assert "a@example.com" in exported and "b@example.com" not in exported

        bob_before = _on_disk_rules(tmp_path, "bob")
        raw = self._file([self._row("drive.folder", ["FOLDER"])])
        assert self._import(client, raw, csrf).status_code == 200
        assert _on_disk_rules(tmp_path, "bob") == bob_before
        assert len(self._rows_of(BOB)) == 1
        assert {tuple(row.value or ()) for row in self._rows_of(ALICE)} == {("a@example.com",), ("FOLDER",)}

    def test_import_by_a_non_admin_works(self, tmp_path, monkeypatch):
        assert not ALICE.is_admin
        client, csrf = self._setup(tmp_path, monkeypatch)
        r = self._import(client, self._file([self._row("drive.folder", ["F"])]), csrf)
        assert r.status_code == 200
        assert r.json()["added"] == 1

    def test_all_three_routes_are_401_when_signed_out(self, tmp_path, monkeypatch):
        app, _sessions = _app()
        client = _client(app)
        raw = self._file([])
        files = {"file": ("f.json", raw, "application/json")}
        assert client.get("/api/settings/personal/export").status_code == 401
        assert client.post("/api/settings/personal/preview", data={"csrf": "x"}, files=files).status_code == 401
        assert client.post("/api/settings/personal/import", data={"csrf": "x"}, files=files).status_code == 401

    def test_a_bad_csrf_is_refused(self, tmp_path, monkeypatch):
        client, _csrf = self._setup(tmp_path, monkeypatch)
        assert self._import(client, self._file([]), "wrong").status_code == 401

    def test_a_v1_settings_file_is_a_500_with_the_message(self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, "alice")
        path = tmp_path / "users" / "alice" / "authority" / "config" / "settings.yaml"
        path.write_text(yaml.safe_dump({"auto_accept_rules": [{"scope": "x"}]}), encoding="utf-8")
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/api/settings/personal/export")
        assert r.status_code == 500
        assert "auto_accept_rules" not in r.json()["error"]  # a non-admin gets the generic text

    def test_import_with_require_passkey_needs_the_assertion(self, tmp_path, monkeypatch):
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)
        client, csrf = self._setup(tmp_path, monkeypatch, step_up=step_up)
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        raw = self._file([self._row("drive.folder", ["NEW"])])

        r = self._import(client, raw, csrf)
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        assert _on_disk_rules(tmp_path, "alice") == []

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=self.ENROLLED):
            r = self._import(client, raw, csrf, webauthn_assertion=json.dumps({"id": "Y3JlZC0x"}))
        assert r.status_code == 200
        assert r.json()["added"] == 1
        assert len(_on_disk_rules(tmp_path, "alice")) == 1

    def test_a_refused_step_up_is_audited(self, tmp_path, monkeypatch):
        step_up = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)
        client, csrf = self._setup(tmp_path, monkeypatch, step_up=step_up)
        lines: list[str] = []
        monkeypatch.setattr(ros, "_record_settings_audit", lambda principal, summary: lines.append(summary))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        assert self._import(client, self._file([]), csrf).status_code == 428
        assert any("Step-up required" in line and "principal=alice" in line for line in lines)

    def test_the_file_step_up_scope_widens_the_persons_scope_and_never_goes_below_the_floor(
        self, tmp_path, monkeypatch,
    ):
        floor = StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_pii_reads")
        client, csrf = self._setup(tmp_path, monkeypatch, step_up=floor)

        wider = self._import(client, self._file([], step_up_scope="writes_and_reads"), csrf).json()
        assert wider["step_up_applied"] is True
        assert step_up_config.personal_scope(ALICE) == "writes_and_reads"
        assert step_up_config.personal_scope(BOB) is None

        narrower = self._import(client, self._file([], step_up_scope="writes"), csrf).json()
        assert narrower["step_up_applied"] is False
        assert step_up_config.personal_scope(ALICE) == "writes_and_reads"
        assert step_up_config.effective_scope(floor, ALICE) == "writes_and_reads"

        # A file narrower than the org floor, for someone with no choice of their own, changes nothing.
        other_sessions = org_session.OrgSessionStore()
        other_app, other_sessions = _app(step_up=floor, sessions=other_sessions)
        other_client = _client(other_app)
        bob_csrf = _signed_in(other_client, other_sessions, BOB)
        below = self._import(other_client, self._file([], step_up_scope="writes"), bob_csrf).json()
        assert below["step_up_applied"] is False
        assert step_up_config.personal_scope(BOB) is None
        assert step_up_config.effective_scope(floor, BOB) == "writes_and_pii_reads"

    def test_a_local_shaped_file_imports_into_an_org_principal(self, tmp_path, monkeypatch):
        client, csrf = self._setup(tmp_path, monkeypatch)
        raw = self._file([self._row("drive.folder", ["FROM-DESKTOP"])], mode="local")
        r = self._import(client, raw, csrf)
        assert r.status_code == 200
        assert r.json()["added"] == 1
        assert [tuple(row.value or ()) for row in self._rows_of(ALICE)] == [("FROM-DESKTOP",)]
        assert len(_on_disk_rules(tmp_path, "alice")) == 1
        assert _on_disk_rules(tmp_path, "bob") == []
