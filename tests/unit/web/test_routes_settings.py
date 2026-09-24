"""Tests for web/routes_settings.py -- settings on the web (W3/W4): the
allowlisted action dispatcher, CSRF/Origin checks, per-action argument
validation, the org config upload, the audit log download, and quit_app's
confirmation gate.

SEC-06: this module's own create_app() no longer takes a shared ``token``
-- it authenticates
against a web/session_auth.py ``LocalSessionStore`` instead, the same store
test_routes_approvals.py's own tests use (this surface shares the approval
surface's one session by design, see build_routes()'s own docstring).
``_authed()`` below signs a session in and returns its id, which now
doubles as the CSRF value every mutating request below sends -- the direct
replacement for the old shared ``TOKEN`` constant.
"""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

import sys

import pytest
from starlette.testclient import TestClient

from privacyfence import daemon_main, org_bundle_signing, paths, privilege_separation, resource_names, update_checker
from privacyfence import settings_controller as sc
from privacyfence import webauthn_stepup as wa
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import routes_settings as rs
from privacyfence.web.routes_settings import (
    _ALLOWED_ACTIONS,
    _BESPOKE_EXEMPT_ROUTE_PATHS,
    _BESPOKE_SENSITIVE_ROUTE_PATHS,
    _NON_SENSITIVE_ACTIONS,
    _SENSITIVE_ACTIONS,
    _BadAction,
    _coerce,
    build_routes,
    create_app,
)
from privacyfence.web.session_auth import (
    PROVENANCE_HUMAN,
    PROVENANCE_UNATTESTED,
    SESSION_COOKIE,
    LocalSessionStore,
)

ORIGIN = "http://localhost"


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")
    monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "update_check_cache.json")
    monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
    monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})

    org_dir_path = tmp_path / "org"
    org_dir_path.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
    data_dir_path = tmp_path / "data"
    data_dir_path.mkdir()
    monkeypatch.setattr(sc, "data_dir", lambda: data_dir_path)

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    connector_host = SimpleNamespace(set_connectors=lambda conns: None)
    return sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)


@pytest.fixture
def sessions():
    return LocalSessionStore()


@pytest.fixture
def client(controller, sessions):
    app = create_app(controller, sessions=sessions)
    return TestClient(app, base_url="http://localhost")


def _authed(client: TestClient, sessions: LocalSessionStore) -> str:
    """Signs `client` in and returns the session id -- also the CSRF value
    every mutating request below sends, per session_auth.py's own
    double-submit design (the session id doubles as its own CSRF token)."""
    session_id = sessions.create()
    client.cookies.set(SESSION_COOKIE, session_id)
    return session_id


class TestSettingsPage:
    def test_unauthenticated_is_rejected(self, client):
        r = client.get("/settings")
        assert r.status_code == 401

    def test_authenticated_renders_the_shell_and_the_settings_document(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "PrivacyFence — Settings" in r.text
        assert "pf-shell-nav" in r.text
        assert "__pfInitialState" in r.text

    def test_response_is_never_cached(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings")
        assert r.headers.get("cache-control") == "no-store"

    def test_notifications_enabled_config_reaches_the_page(self, controller, sessions):
        # settings_page reads this off the controller's own live snapshot
        # (general.notifications_enabled), not the notifications_enabled=
        # closure arg below -- that arg is only ever the daemon-startup
        # default a snapshot's own general dict doesn't have yet, which
        # never actually happens once a controller is wired (see
        # test_notifications_detail_reflects_a_live_config_edit_without_
        # restart below for why the two must agree). So this test edits
        # the same config file the controller reads, the same way a real
        # `web.notifications.enabled: false` in settings.yaml would.
        cfg = controller._load_config()
        cfg.setdefault("web", {}).setdefault("notifications", {})["enabled"] = False
        controller._save_config(cfg)
        app = create_app(controller, sessions=sessions, notifications_enabled=True)
        c = TestClient(app, base_url="http://localhost")
        _authed(c, sessions)
        r = c.get("/settings")
        assert "NOTIFICATIONS_ENABLED = false" in r.text

    def test_notifications_detail_reflects_a_live_config_edit_without_restart(self, controller, sessions):
        # settings_page reads notifications_enabled/detail off this
        # request's own fresh snapshot, not the notifications_detail=
        # "minimal" default create_app was built with (server.py's
        # daemon-startup value) -- so a set_notifications_detail() call in
        # between two GETs must change what the *second* GET renders, with
        # no server restart and no create_app() rebuild.
        app = create_app(controller, sessions=sessions, notifications_detail="minimal")
        c = TestClient(app, base_url="http://localhost")
        _authed(c, sessions)
        before = c.get("/settings")
        assert 'NOTIFICATIONS_DETAIL = "minimal"' in before.text

        controller.set_notifications_detail("detailed")

        after = c.get("/settings")
        assert 'NOTIFICATIONS_DETAIL = "detailed"' in after.text

    def test_policy_v2_migration_notice_renders_as_a_dismissible_notice(self, controller, client, sessions):
        # P4 of the policy v2 redesign: settings_page wires
        # controller.policy_v2_migration_notice_html() into web_shell.wrap's
        # dismissible_notice_html, not the persistent banner_html strip --
        # see settings_controller.py's own docstring on why. pf-shell-notice
        # is that mechanism's own container id/class (web_shell.py); a
        # config with no v2 migration marker at all must render neither.
        _authed(client, sessions)
        assert 'id="pf-shell-notice"' not in client.get("/settings").text

        cfg = controller._load_config()
        cfg["migrated_to_policy_v2"] = True
        cfg["auto_accept"] = {
            "version": 2,
            "rules": [{"id": "r-delete", "predicate": "always_allow", "operations": ["sheets.delete_dimensions"]}],
        }
        controller._save_config(cfg)

        r = client.get("/settings")
        assert 'id="pf-shell-notice"' in r.text
        assert "r-delete" in r.text
        assert 'data-dismiss-key="pf_policy_v2_migration_dismissed"' in r.text


class TestConnectorsPage:
    """GET /settings/connectors -- issue #396 Part C's first-run
    destination: the same document as GET /settings, but with the
    Connectors section pre-selected server-side rather than defaulting to
    General, and no query string to lose across _BootstrapMiddleware's
    redirect (see that class's own docstring in web/server.py)."""

    def test_unauthenticated_is_rejected(self, client):
        r = client.get("/settings/connectors")
        assert r.status_code == 401

    def test_authenticated_renders_the_shell_and_the_settings_document(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings/connectors")
        assert r.status_code == 200
        assert "PrivacyFence — Settings" in r.text
        assert "pf-shell-nav" in r.text
        assert "__pfInitialState" in r.text

    def test_initial_section_is_connectors(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings/connectors")
        assert "window.__pfInitialSection = \"connectors\";" in r.text

    def test_plain_settings_page_carries_no_initial_section(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings")
        assert "window.__pfInitialSection =" not in r.text

    def test_response_is_never_cached(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/settings/connectors")
        assert r.headers.get("cache-control") == "no-store"


class TestActionDispatch:
    def test_unlisted_action_is_404_before_any_getattr(self, client, controller, sessions, monkeypatch):
        csrf = _authed(client, sessions)
        called = []
        monkeypatch.setattr(sc.SettingsController, "__getattribute__", lambda self, name: (
            called.append(name) or object.__getattribute__(self, name)
        ))
        r = client.post("/api/settings/_load_config", json={"csrf": csrf})
        assert r.status_code == 404
        assert "_load_config" not in called

    def test_dunder_and_snapshot_are_rejected(self, client, sessions):
        csrf = _authed(client, sessions)
        for action in ("snapshot", "__init__", "_save_config", "on_change"):
            r = client.post(f"/api/settings/{action}", json={"csrf": csrf})
            assert r.status_code == 404, action

    def test_every_allowed_action_actually_exists_on_the_controller(self, controller):
        for action in _ALLOWED_ACTIONS:
            assert callable(getattr(controller, action, None)), action

    def test_mechanical_action_returns_a_fresh_snapshot(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 200
        assert r.json()["general"]["pii_enabled"] is False

    def test_action_with_arguments_dispatches_str_and_list_arguments(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/add_policy_rule",
            json={"group": "gmail.sender_domain", "value": "acme.com", "verbs": ["read"], "csrf": csrf},
        )
        assert r.status_code == 200
        rules = r.json()["auto_accept"]["rules"]
        assert any(row["connector"] == "gmail" for row in rules)

    def test_bad_argument_type_is_400_not_500(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/add_policy_rule",
            json={"group": 123, "value": "acme.com", "verbs": ["read"], "csrf": csrf},
        )
        assert r.status_code == 400

    def test_missing_required_argument_is_400(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/add_policy_rule", json={"csrf": csrf})
        assert r.status_code == 400

    def test_coerce_parses_int_arguments_and_rejects_non_numeric(self):
        assert _coerce("30", int) == 30
        assert _coerce(30, int) == 30
        with pytest.raises(_BadAction):
            _coerce("not-a-number", int)
        with pytest.raises(_BadAction):
            _coerce(True, int)  # bool is a subclass of int but not a valid idx

    def test_connector_icons_are_augmented(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/refresh_connectors", json={"csrf": csrf})
        connectors = r.json()["connectors"]
        assert any("icon_data_uri" in c for c in connectors)

    def test_missing_csrf_is_401(self, client, sessions):
        _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={})
        assert r.status_code == 401

    def test_wrong_csrf_is_401(self, client, sessions):
        _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": "wrong"})
        assert r.status_code == 401

    def test_cross_origin_is_403(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/toggle_pii_detection", json={"csrf": csrf},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403

    def test_unauthenticated_is_401(self, client):
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": "irrelevant"})
        assert r.status_code == 401

    def test_malformed_json_is_400(self, client, sessions):
        _authed(client, sessions)
        r = client.post(
            "/api/settings/toggle_pii_detection", content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_set_notifications_detail_persists_and_returns_it_in_general(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/set_notifications_detail", json={"level": "detailed", "csrf": csrf})
        assert r.status_code == 200
        assert r.json()["general"]["notifications_detail"] == "detailed"

    def test_set_notifications_detail_rejects_an_unknown_level(self, client, controller, sessions):
        csrf = _authed(client, sessions)
        client.post("/api/settings/set_notifications_detail", json={"level": "standard", "csrf": csrf})
        r = client.post("/api/settings/set_notifications_detail", json={"level": "bogus", "csrf": csrf})
        assert r.status_code == 200
        # Same shape as set_log_level's own bad-value handling -- an
        # invalid value is a silent no-op snapshot, not a 400 (the
        # segmented control only ever sends its own three literals).
        assert r.json()["general"]["notifications_detail"] == "standard"

    def test_a_mutation_is_audit_logged_under_the_local_principal(self, client, sessions, monkeypatch):
        csrf = _authed(client, sessions)
        recorded = []
        monkeypatch.setattr(rs, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})

        assert r.status_code == 200
        assert len(recorded) == 1
        principal, summary = recorded[0]
        assert principal.id == LOCAL_PRINCIPAL.id
        assert "toggle_pii_detection" in summary

    def test_a_read_error_from_a_bad_argument_is_not_audited(self, client, sessions, monkeypatch):
        # _call_action raising _BadAction (400) never reaches the mutation
        # itself -- nothing changed, so nothing should be recorded.
        csrf = _authed(client, sessions)
        recorded = []
        monkeypatch.setattr(rs, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        r = client.post("/api/settings/add_policy_rule", json={"csrf": csrf})

        assert r.status_code == 400
        assert recorded == []


class TestSensitiveActionsCoverAllAllowedActions:
    """#426 Phase 3's own allowlist-within-the-allowlist -- see module
    docstring on why _SENSITIVE_ACTIONS/_NON_SENSITIVE_ACTIONS are both
    explicit rather than one being derived as the other's complement: a
    future action landing in _ALLOWED_ACTIONS with no matching entry in
    either set must fail here, not silently default to unclassified."""

    def test_every_allowed_action_is_classified_sensitive_or_not(self):
        assert _SENSITIVE_ACTIONS | _NON_SENSITIVE_ACTIONS == _ALLOWED_ACTIONS

    def test_no_action_is_classified_as_both(self):
        assert _SENSITIVE_ACTIONS & _NON_SENSITIVE_ACTIONS == frozenset()


def _step_up_client(controller, sessions, *, step_up: StepUpConfig) -> TestClient:
    app = create_app(controller, sessions=sessions, step_up=step_up, step_up_origin=ORIGIN)
    return TestClient(app, base_url=ORIGIN)


class TestSensitiveActionStepUp:
    """#426 Phase 3: with ``step_up.require_passkey`` on, a sensitive action
    (module docstring's ``_SENSITIVE_ACTIONS``) needs a fresh WebAuthn
    assertion the same two-round-trip way web/routes_approvals.py's decide()
    does; a non-sensitive one is untouched. Mirrors test_routes_approvals.py's
    own TestRequirePasskeyHardFail/TestStepUpWebAuthnFlow."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def test_a_non_sensitive_action_is_never_gated(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/set_log_level", json={"level": "DEBUG", "csrf": csrf})
        assert r.status_code == 200

    def test_disabled_step_up_never_gates_a_sensitive_action(self, controller, sessions):
        client = _step_up_client(controller, sessions, step_up=StepUpConfig(enabled=False, require_passkey=True))
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 200

    def test_require_passkey_off_never_gates_a_sensitive_action(self, controller, sessions):
        client = _step_up_client(controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 200

    def test_no_credential_hard_fails_a_sensitive_action(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"

    def test_a_refusal_is_audit_logged_under_the_local_principal(self, controller, sessions, monkeypatch):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        recorded = []
        monkeypatch.setattr(rs, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})

        assert r.status_code == 403
        assert len(recorded) == 1
        principal, summary = recorded[0]
        assert principal.id == LOCAL_PRINCIPAL.id
        assert "toggle_pii_detection" in summary

    def test_a_completed_ceremony_is_not_also_recorded_as_a_refusal(self, controller, sessions, monkeypatch):
        self._enroll()
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        recorded = []
        monkeypatch.setattr(rs, "_record_settings_audit", lambda p, s: recorded.append((p, s)))

        first = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert first.status_code == 428
        assert len(recorded) == 1  # the challenge offer -- still a refusal of *this* request

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post("/api/settings/toggle_pii_detection", json={
                "csrf": csrf, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert len(recorded) == 2
        principal, summary = recorded[1]
        assert "Changed setting" in summary
        assert "toggle_pii_detection" in summary

    def test_with_a_credential_offers_webauthn_options(self, controller, sessions):
        self._enroll()
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        # Not actually toggled yet -- the ceremony hasn't completed.
        assert controller.snapshot()["general"]["pii_enabled"] is True

    def test_a_valid_assertion_completes_a_sensitive_action(self, controller, sessions):
        self._enroll()
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        first = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post("/api/settings/toggle_pii_detection", json={
                "csrf": csrf, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json()["general"]["pii_enabled"] is False

    def test_an_assertion_for_a_different_action_is_rejected(self, controller, sessions):
        # Fingerprint binding (mirrors webauthn_stepup.decision_fingerprint):
        # a challenge minted for toggle_pii_detection cannot be reused to
        # authorize toggle_pii_category.
        self._enroll()
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        r = client.post("/api/settings/toggle_pii_category", json={
            "category_key": "email", "csrf": csrf, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400

    def test_a_failed_assertion_does_not_apply_the_change(self, controller, sessions):
        self._enroll()
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post("/api/settings/toggle_pii_detection", json={
                "csrf": csrf, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert r.status_code == 401
        assert controller.snapshot()["general"]["pii_enabled"] is True


class TestEnableStepUpAction:
    """B9: the dispatcher-level half of SettingsController.enable_step_up --
    ``create_app``'s own ``step_up`` and ``controller._step_up`` (wired via
    ``wire_step_up``) are two independently-passed things; daemon_main.py's
    real boot path always hands the *same* LiveStepUpConfig to both (see
    that module's own ``_maybe_start_web_server``), which is what these
    tests set up too, so enabling step-up through this action is visible to
    this same dispatcher's own ``_needs_step_up`` gate on the very next
    request -- no restart."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        # ADR 0003: StepUpConfig.from_local_config() now refuses
        # require_passkey on an unseparated install -- this class is about
        # the enable_step_up action's own dispatch/gating behavior, not
        # that separate check (covered by
        # TestRequirePasskeyNeedsSeparation in
        # tests/unit/test_step_up_config.py).
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        return tmp_path

    def _enroll(self):
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def _live_client(self, controller, sessions):
        from privacyfence.step_up_config import LiveStepUpConfig
        live = LiveStepUpConfig(StepUpConfig(rp_id="localhost"))
        controller.wire_step_up(live)
        app = create_app(controller, sessions=sessions, step_up=live, step_up_origin=ORIGIN)
        return TestClient(app, base_url=ORIGIN), live

    def test_is_listed_in_allowed_and_sensitive_actions(self):
        assert "enable_step_up" in _ALLOWED_ACTIONS
        assert "enable_step_up" in _SENSITIVE_ACTIONS

    def test_refused_without_an_enrolled_passkey(self, controller, sessions):
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)

        r = client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        assert r.status_code == 200
        assert live.enabled is False
        assert r.json()["error"]

    def test_the_first_enable_is_never_step_up_gated(self, controller, sessions):
        # _needs_step_up only fires once step_up.enabled/require_passkey are
        # *already* both true -- the very first call can't be gated on a
        # ceremony that isn't active yet (module docstring's own B9 note).
        self._enroll()
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)

        r = client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        assert r.status_code == 200
        assert live.enabled is True
        assert live.require_passkey is True
        assert r.json()["general"]["step_up_on"] is True

    def test_takes_effect_immediately_for_the_next_sensitive_action(self, controller, sessions):
        # The point of LiveStepUpConfig: this dispatcher's own step_up
        # object is the one enable_step_up just mutated, so the very next
        # request already sees it -- no daemon restart.
        self._enroll()
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)
        client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})

        assert r.status_code == 428
        assert "webauthn_options" in r.json()

    def test_reenabling_once_already_on_is_itself_step_up_gated(self, controller, sessions):
        self._enroll()
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)
        client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        r = client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        assert r.status_code == 428
        assert "webauthn_options" in r.json()


class TestEnableStepUpRefusesOnAnUnseparatedInstall:
    """ADR 0003, Context #2: "step_up.require_passkey is reachable from the
    Settings page of an unseparated install" -- this is that path's fix.
    Unlike TestEnableStepUpAction above, this class does *not* set the dev
    override, so the real gate in StepUpConfig.from_local_config() fires."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def _live_client(self, controller, sessions):
        from privacyfence.step_up_config import LiveStepUpConfig
        live = LiveStepUpConfig(StepUpConfig(rp_id="localhost"))
        controller.wire_step_up(live)
        app = create_app(controller, sessions=sessions, step_up=live, step_up_origin=ORIGIN)
        return TestClient(app, base_url=ORIGIN), live

    def test_refuses_and_leaves_the_live_config_untouched(self, controller, sessions):
        self._enroll()
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)

        r = client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        assert r.status_code == 200
        assert live.enabled is False
        assert live.require_passkey is False
        assert "privilege-separated" in r.json()["error"]

    def test_does_not_persist_the_refused_change_to_disk(self, controller, sessions, tmp_path):
        self._enroll()
        client, live = self._live_client(controller, sessions)
        csrf = _authed(client, sessions)

        client.post("/api/settings/enable_step_up", json={"csrf": csrf})

        on_disk = (tmp_path / "settings.yaml").read_text(encoding="utf-8")
        assert "require_passkey: true" not in on_disk


class TestRequirePasskeyBanner:
    """#426 Phase 3: the settings page carries the same banner the
    approvals list does -- see test_routes_approvals.py's own
    TestRequirePasskeyBanner and step_up_config.py's
    local_enrollment_banner()."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_banner_shown_when_require_passkey_unmet(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        _authed(client, sessions)
        r = client.get("/settings")
        assert '<div class="pf-shell-banner"' in r.text
        assert "/security" in r.text

    def test_no_banner_once_a_credential_is_enrolled(self, controller, sessions):
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        _authed(client, sessions)
        r = client.get("/settings")
        assert '<div class="pf-shell-banner"' not in r.text

    def test_disabled_requirement_notice_is_shown(self, controller, sessions):
        """#426 Phase 4: webauthn_stepup.observe_step_up_requirement's own
        persistent notice, surfaced through this page's banner the same
        way the Phase 3 enrollment one is."""
        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=False)
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=False),
        )
        _authed(client, sessions)
        r = client.get("/settings")
        assert '<div class="pf-shell-banner"' in r.text
        assert "turned off" in r.text

    def test_no_disabled_notice_when_never_required(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=False, rp_id="localhost", require_passkey=False),
        )
        _authed(client, sessions)
        r = client.get("/settings")
        assert '<div class="pf-shell-banner"' not in r.text


class TestConnectorAuthenticationEndToEnd:
    """§16.5's W6 "Done when": a connector can be authenticated from a
    browser, start to finish, with the page reflecting each step -- proven
    here for Slack (representative of the single-click OAuth connectors;
    Atlassian's own picker flow is covered end-to-end in
    test_settings_controller.py's TestPickResourceIndexWebMode, Telegram's
    multi-step flow in its own TestTelegramStartAuth/... classes there)."""

    def test_authenticate_connector_reflects_busy_then_connected(self, client, controller, sessions, monkeypatch):
        import threading

        release = threading.Event()

        def fake_authorize(**kwargs):
            release.wait(timeout=2)
            return {"access_token": "tok"}

        # _authenticate_slack's background thread marshals its `done`
        # callback back via call_on_main, which resolves to whatever
        # dispatcher set_main_dispatcher() registered -- the ``client``
        # fixture's real WebServer/TestClient lifespan already registers
        # state_stream.call_soon_threadsafe onto a live ASGI event loop
        # (see web/server.py's _state_stream_loop_lifespan), so `done`
        # actually gets delivered without needing a fake dispatcher here.
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"slack": {"client_id": "cid"}})
        monkeypatch.setattr(sc, "slack_authorize_interactive", fake_authorize)
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller._push_snapshot())
        csrf = _authed(client, sessions)

        r = client.post(
            "/api/settings/authenticate_connector", json={"connector": "slack", "csrf": csrf},
        )
        assert r.status_code == 200
        assert any(c["key"] == "slack" and c["busy"] for c in r.json()["connectors"])

        release.set()
        assert wait_until(lambda: "slack" not in controller._busy_connectors)

    def test_missing_org_config_surfaces_an_error_not_a_500(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/authenticate_connector", json={"connector": "slack", "csrf": csrf},
        )
        assert r.status_code == 200
        assert r.json()["error"]


class TestConnectorToggleDirectional:
    """F6 of the self-approval review: toggle_connector split into
    enable_connector (sensitive) and disable_connector (not) -- see
    SettingsController.enable_connector's own docstring for why the two
    directions aren't symmetric."""

    def test_toggle_connector_no_longer_exists_as_a_dispatchable_action(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_connector", json={"connector": "gmail", "csrf": csrf})
        assert r.status_code == 404

    def test_disable_connector_flips_it_off(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/disable_connector", json={"connector": "gmail", "csrf": csrf})
        assert r.status_code == 200
        assert next(c for c in r.json()["connectors"] if c["key"] == "gmail")["enabled"] is False

    def test_enable_connector_flips_it_on(self, client, controller, sessions):
        controller.disable_connector("gmail")
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/enable_connector", json={"connector": "gmail", "csrf": csrf})
        assert r.status_code == 200
        assert next(c for c in r.json()["connectors"] if c["key"] == "gmail")["enabled"] is True

    def test_enable_connector_is_sensitive_disable_is_not(self):
        assert "enable_connector" in _SENSITIVE_ACTIONS
        assert "disable_connector" in _NON_SENSITIVE_ACTIONS

    def test_disabling_is_never_step_up_gated(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/disable_connector", json={"connector": "gmail", "csrf": csrf})
        assert r.status_code == 200

    def test_enabling_is_step_up_gated(self, controller, sessions):
        client = _step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/enable_connector", json={"connector": "gmail", "csrf": csrf})
        assert r.status_code == 403
        assert r.json()["error"] == "passkey_enrollment_required"


class TestOrgConfigUpload:
    def test_valid_bundle_is_installed(self, client, controller, sessions, tmp_path):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("org_config.json", b'{"version": 1, "google": {}}', "application/json")},
        )
        assert r.status_code == 200
        assert (sc.org_dir() / "org_config.json").exists()
        assert r.json()["error"] == ""

    def test_non_json_is_rejected_with_an_error_not_installed(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("x.json", b"not json at all", "application/json")},
        )
        assert r.status_code == 200
        assert r.json()["error"]
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_json_without_version_key_is_rejected(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("x.json", b'{"google": {}}', "application/json")},
        )
        assert r.json()["error"]
        assert not (sc.org_dir() / "org_config.json").exists()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_installed_file_is_0600(self, client, sessions):
        csrf = _authed(client, sessions)
        client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("org_config.json", b'{"version": 1}', "application/json")},
        )
        mode = (sc.org_dir() / "org_config.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_oversized_upload_is_rejected(self, client, sessions, monkeypatch):
        from privacyfence.web import routes_settings
        monkeypatch.setattr(routes_settings, "MAX_ORG_CONFIG_BYTES", 10)
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1, "padding": "xxxxxxxxxxxxxxxxxxxx"}', "application/json")},
        )
        assert r.status_code == 400

    def test_missing_csrf_is_401(self, client, sessions):
        _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload",
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 401


def _signed_org_bundle():
    private_key, _ = org_bundle_signing.generate_keypair()
    return org_bundle_signing.sign_bundle({"version": 1, "org_name": "Acme"}, private_key)


class TestOrgConfigUploadPinConfirmation:
    """F5 of the self-approval review: install_org_config_bytes still
    pins a first signed bundle's key unconditionally -- daemon_main.
    load_org_config's own hand-edited-file path needs that -- but this
    route, the only one reachable by an unsupervised local process, asks
    for an explicit ``confirm_pin`` first rather than letting the TOFU
    pin happen silently as a side effect of an upload."""

    def test_first_signed_bundle_without_confirmation_is_409_and_not_installed(self, client, sessions):
        csrf = _authed(client, sessions)
        bundle = _signed_org_bundle()

        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("org_config.json", json.dumps(bundle).encode(), "application/json")},
        )

        assert r.status_code == 409
        assert r.json()["error"] == "pin_confirmation_required"
        assert not (sc.org_dir() / "org_config.json").exists()
        assert org_bundle_signing.load_pinned_public_key(sc.org_dir()) is None

    def test_confirmed_upload_installs_and_pins(self, client, sessions):
        csrf = _authed(client, sessions)
        bundle = _signed_org_bundle()

        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf, "confirm_pin": "true"},
            files={"file": ("org_config.json", json.dumps(bundle).encode(), "application/json")},
        )

        assert r.status_code == 200
        assert r.json()["error"] == ""
        assert (sc.org_dir() / "org_config.json").exists()
        assert org_bundle_signing.load_pinned_public_key(sc.org_dir()) is not None

    def test_unconfirmed_flag_value_is_not_treated_as_consent(self, client, sessions):
        csrf = _authed(client, sessions)
        bundle = _signed_org_bundle()

        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf, "confirm_pin": "false"},
            files={"file": ("org_config.json", json.dumps(bundle).encode(), "application/json")},
        )

        assert r.status_code == 409
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_unsigned_bundle_never_needs_confirmation(self, client, sessions):
        csrf = _authed(client, sessions)

        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf},
            files={"file": ("org_config.json", b'{"version": 1, "google": {}}', "application/json")},
        )

        assert r.status_code == 200
        assert (sc.org_dir() / "org_config.json").exists()

    def test_a_bundle_matching_an_already_pinned_key_never_needs_confirmation(self, client, sessions):
        csrf = _authed(client, sessions)
        bundle = _signed_org_bundle()
        client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf, "confirm_pin": "true"},
            files={"file": ("org_config.json", json.dumps(bundle).encode(), "application/json")},
        )

        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("org_config.json", json.dumps(bundle).encode(), "application/json")},
        )

        assert r.status_code == 200
        assert r.json()["error"] == ""


def _org_config_step_up_client(controller, sessions, *, step_up: StepUpConfig) -> TestClient:
    app = create_app(controller, sessions=sessions, step_up=step_up, step_up_origin=ORIGIN)
    return TestClient(app, base_url=ORIGIN)


class TestOrgConfigUploadStepUp:
    """F5/3.1 of the self-approval review: org_config_upload is the one
    path in _BESPOKE_SENSITIVE_ROUTE_PATHS -- with step_up.require_passkey
    on, it needs a fresh WebAuthn assertion the same two-round-trip way a
    _SENSITIVE_ACTIONS action does (mirrors TestSensitiveActionStepUp
    above), bound to this exact file's content so a ceremony completed for
    one upload can't authorize installing a different one."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def test_disabled_step_up_never_gates_the_upload(self, controller, sessions):
        client = _org_config_step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=False, require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 200

    def test_no_credential_hard_fails_the_upload(self, controller, sessions):
        client = _org_config_step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_with_a_credential_offers_webauthn_options(self, controller, sessions):
        self._enroll()
        client = _org_config_step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_a_valid_assertion_completes_the_upload(self, controller, sessions):
        self._enroll()
        client = _org_config_step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        raw = b'{"version": 1}'
        first = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", raw, "application/json")},
        )
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(
                "/api/settings/org_config/upload",
                data={"csrf": csrf, "webauthn_assertion": json.dumps({"id": "Y3JlZC0x"})},
                files={"file": ("x.json", raw, "application/json")},
            )
        assert second.status_code == 200
        assert second.json()["error"] == ""
        assert (sc.org_dir() / "org_config.json").exists()

    def test_an_assertion_bound_to_a_different_file_is_rejected(self, controller, sessions):
        # Fingerprint binding, mirroring TestSensitiveActionStepUp's own
        # test_an_assertion_for_a_different_action_is_rejected: a
        # ceremony completed for one upload's bytes can't be replayed to
        # install different bytes.
        self._enroll()
        client = _org_config_step_up_client(
            controller, sessions, step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        csrf = _authed(client, sessions)
        client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": csrf, "webauthn_assertion": json.dumps({"id": "Y3JlZC0x"})},
            files={"file": ("x.json", b'{"version": 2}', "application/json")},
        )
        assert r.status_code == 400
        assert not (sc.org_dir() / "org_config.json").exists()


class TestOrgConfigUploadHumanSession:
    """The self-approval plan's Phase 2, on the bespoke-route half: an
    organization config bundle changes *what gets gated* at least as much
    as any _SENSITIVE_ACTIONS entry, so it needs a session PrivacyFence
    can attribute to a person too. Mirrors
    TestHumanSessionRequiredForSensitiveActions above."""

    def _client(self, controller, sessions):
        app = create_app(controller, sessions=sessions, require_human_session=True)
        return TestClient(app, base_url=ORIGIN)

    def _sign_in(self, client, sessions, provenance):
        session_id = sessions.create(provenance=provenance)
        client.cookies.set(SESSION_COOKIE, session_id)
        return session_id

    def test_an_unattested_session_cannot_upload(self, controller, sessions):
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)

        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )

        assert r.status_code == 403
        assert r.json()["error"] == "human_session_required"
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_an_attested_session_uploads_exactly_as_before(self, controller, sessions):
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_HUMAN)

        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )

        assert r.status_code == 200

    def test_the_gate_is_off_unless_the_install_asks_for_it(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 200


class TestAuditLogDownload:
    def test_nothing_to_export_is_404(self, client, sessions):
        _authed(client, sessions)
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 404

    def test_current_week_activity_downloads_with_content_disposition(self, client, controller, sessions):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.authority_root(sc.data_dir()) / "logs" / "audit"
        log_dir.mkdir(parents=True)
        week = current_week()
        AuditLogger(str(log_dir)).record(AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="approved", auto_accept_rule="", latency_seconds=1.0,
        ))
        _authed(client, sessions)
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert r.headers.get("cache-control") == "no-store"

    def test_unauthenticated_is_401(self, client):
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 401


class TestQuitApp:
    def test_unconfirmed_is_rejected(self, client, sessions, monkeypatch):
        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/quit_app", json={"csrf": csrf})
        assert r.status_code == 400
        assert called == []

    def test_confirmed_calls_quit(self, client, sessions, monkeypatch):
        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/quit_app", json={"csrf": csrf, "confirmed": True})
        assert r.status_code == 200
        assert called == [True]

    def test_disabled_by_allow_quit_config(self, controller, sessions, monkeypatch):
        from privacyfence.web.routes_settings import create_app as _create_app
        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        app = _create_app(controller, sessions=sessions, allow_quit=False)
        client = TestClient(app, base_url="http://localhost")
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/quit_app", json={"csrf": csrf, "confirmed": True})
        assert r.status_code == 403
        assert called == []

    def test_shutdown_is_signalled_only_after_the_response_body_is_written(
        self, controller, sessions, monkeypatch,
    ):
        """controller.quit_app() signals daemon_main's own shutdown wait, so
        calling it inline -- before this route's 21-byte body reaches the
        socket -- let the process be torn down mid-write: the client got
        "peer closed connection without sending complete message body
        (received 0 bytes, expected 21)" instead of its confirmation.
        Anyone clicking "Quit PrivacyFence" could hit that, and
        tests/system/test_local_mode_system.py's own quit step did,
        intermittently, in CI. Asserted as an ordering of real ASGI events
        rather than by inspecting the response object, since the ordering
        is the whole guarantee."""
        from privacyfence.web.routes_settings import create_app as _create_app

        events: list[str] = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: events.append("shutdown"))

        inner = _create_app(controller, sessions=sessions)

        async def recording_app(scope, receive, send):
            async def _send(message):
                await send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    events.append("body")
            await inner(scope, receive, _send)

        client = TestClient(recording_app, base_url="http://localhost")
        csrf = _authed(client, sessions)
        events.clear()

        r = client.post("/api/settings/quit_app", json={"csrf": csrf, "confirmed": True})

        assert r.status_code == 200
        assert events == ["body", "shutdown"], events

    def test_generic_dispatch_never_reaches_quit_app(self, client):
        # quit_app is deliberately absent from _ALLOWED_ACTIONS -- it only
        # has its own dedicated route (above), which carries the
        # confirmation gate the generic dispatcher doesn't know about.
        assert "quit_app" not in _ALLOWED_ACTIONS


class TestNoSubprocessFromHttp:
    """§16.2.4's standing rule: no route in this module ever shells out.
    Patches subprocess.run to explode, then exercises every route that used
    to (or plausibly could) reach one."""

    def test_no_route_here_calls_subprocess(self, client, sessions, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError(f"subprocess.run reached from an HTTP route: {a!r}")

        monkeypatch.setattr(subprocess, "run", _boom)
        csrf = _authed(client, sessions)

        client.get("/settings")
        client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        client.post(
            "/api/settings/org_config/upload", data={"csrf": csrf},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        client.get("/api/settings/audit_log/download")


class TestHumanSessionRequiredForSensitiveActions:
    """The self-approval plan's Phase 2, on the settings half: an action that
    changes *what gets gated* needs a session PrivacyFence can attribute to a
    person, not merely a valid one. Deliberately independent of
    ``step_up.require_passkey`` -- an install with no passkey requirement
    still has a policy an agent should not be able to rewrite on its own say
    so. See web/routes_approvals.py's own gate for the decide-time half."""

    def _client(self, controller, sessions):
        app = create_app(controller, sessions=sessions, require_human_session=True)
        return TestClient(app, base_url=ORIGIN)

    def _sign_in(self, client, sessions, provenance):
        session_id = sessions.create(provenance=provenance)
        client.cookies.set(SESSION_COOKIE, session_id)
        return session_id

    def test_an_unattested_session_cannot_change_a_sensitive_setting(self, controller, sessions):
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)

        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})

        assert r.status_code == 403
        assert r.json()["error"] == "human_session_required"

    def test_an_attested_session_changes_it_exactly_as_before(self, controller, sessions):
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_HUMAN)

        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})

        assert r.status_code == 200

    def test_a_non_sensitive_action_is_untouched(self, controller, sessions):
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)

        r = client.post("/api/settings/set_log_level", json={"level": "DEBUG", "csrf": csrf})

        assert r.status_code == 200

    def test_every_sensitive_action_is_refused_the_same_way(self, controller, sessions):
        """The set, not a sample: the ratchet
        (TestSensitiveActionsCoverAllAllowedActions) guarantees a new action
        lands in one of the two sets, and this guarantees landing in the
        sensitive one actually gates it."""
        client = self._client(controller, sessions)
        csrf = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)

        for action in sorted(_SENSITIVE_ACTIONS):
            r = client.post(f"/api/settings/{action}", json={"csrf": csrf})
            assert r.status_code == 403, action
            assert r.json()["error"] == "human_session_required", action

    def test_the_gate_is_off_unless_the_install_asks_for_it(self, client, sessions):
        csrf = _authed(client, sessions)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": csrf})
        assert r.status_code == 200


class TestBespokeRoutesAreClassified:
    """3.3 of the self-approval review: widens the ratchet from action
    names (TestSensitiveActionsCoverAllAllowedActions above) to actual
    Route objects, so a new bespoke POST route added to build_routes()
    below fails this test instead of silently bypassing both
    _needs_step_up and require_human_session the way org_config_upload
    used to (F5) -- by existing, with no matching entry in either set."""

    def test_every_post_route_is_the_generic_dispatcher_sensitive_or_explicitly_exempt(self, controller, sessions):
        routes = build_routes(controller, sessions=sessions)
        checked_any_bespoke = False
        for route in routes:
            methods = getattr(route, "methods", None) or set()
            if "POST" not in methods:
                continue
            path = route.path
            if path == "/api/settings/{action}":
                continue
            checked_any_bespoke = True
            assert path in _BESPOKE_SENSITIVE_ROUTE_PATHS or path in _BESPOKE_EXEMPT_ROUTE_PATHS, path
        assert checked_any_bespoke, "no bespoke POST route found -- this test would pass vacuously"

    def test_no_path_is_both_sensitive_and_exempt(self):
        assert not (_BESPOKE_SENSITIVE_ROUTE_PATHS & set(_BESPOKE_EXEMPT_ROUTE_PATHS))

    def test_build_routes_raises_on_an_unclassified_bespoke_route(self, controller, sessions, monkeypatch):
        """#614: the classification guard is an explicit `if ...: raise
        RuntimeError(...)`, not `assert`, precisely so it still fires under
        `python -O`/PYTHONOPTIMIZE (which strips assert statements). Drop
        org_config_upload's own entry out of both sets so build_routes()
        hits its own bespoke route unclassified, and pin that this raises
        rather than silently mounting it."""
        routes_settings_module = sys.modules[build_routes.__module__]
        monkeypatch.setattr(routes_settings_module, "_BESPOKE_SENSITIVE_ROUTE_PATHS", frozenset())
        with pytest.raises(RuntimeError, match=r"/api/settings/org_config/upload.*classification"):
            build_routes(controller, sessions=sessions)

    def test_classification_guard_fires_under_python_dash_o(self, tmp_path):
        """The regression #614 actually describes: with `assert`, this same
        scenario would silently mount the unclassified route under
        `python -O` instead of raising. Runs the guard in a real `-O`
        subprocess against a stripped-down copy of the two classification
        sets to confirm it's immune to assert-stripping."""
        script = tmp_path / "check_under_dash_o.py"
        script.write_text(
            "import privacyfence.web.routes_settings as rs\n"
            "from privacyfence.web.session_auth import LocalSessionStore\n"
            "rs._BESPOKE_SENSITIVE_ROUTE_PATHS = frozenset()\n"
            "try:\n"
            "    rs.build_routes(None, sessions=LocalSessionStore())\n"
            "except RuntimeError as exc:\n"
            "    assert '/api/settings/org_config/upload' in str(exc)\n"
            "    print('RAISED')\n"
            "else:\n"
            "    print('NOT-RAISED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-O", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "RAISED" in result.stdout, result.stdout + result.stderr
