"""privacyfence.settings_controller -- the domain/business logic behind the
webview settings window (issue #120).

This module's coverage was moved here from test_menu_bar.py's pre-#120 rule/
grant/PII/privacy/connector/audit/org-config tests (see git history) --
same behavior, now exercised through SettingsController's methods instead of
PrivacyFenceMenuBar's. Native-picker-specific tests (_osascript_pick-driven
rule/policy selection, the old int-value/list-value rumps.Window prompts)
were dropped rather than ported -- there is no native picker left to test.
The webview-dropdown rule editor #120 replaced them with (rule_type from a
RULES_BY_OPERATION-constrained <select>, value as plain text) is itself gone
as of the policy v2 redesign's P6 (see TestAddPolicyRule/TestRemovePolicyRule
below for its replacement, and settings_controller.py's own "Auto-accept
(policy v2)" section docstring); RULES_BY_OPERATION/RULES_LIST_VALUE/
RULES_INT_VALUE survive only because org mode's own separate settings
surface (web/routes_org_settings.py) still reads them.

Also covers the cross-thread AppHelper.callAfter marshaling contract
(_run_async/on_change) that used to live in test_menu_bar.py's "P6" module
docstring -- see TestRunAsyncMarshaling and TestOnChangeMarshaling below for
why that still matters here.

One follow-up feature rebuilt after the initial #120 pass per user
direction (see PR history): Telegram's in-webview multi-step sign-in
(TestTelegramStartAuth/TestTelegramSubmitCode/TestTelegramSubmit2FA/
TestTelegramCancelAuth, replacing the native rumps.Window-based flow --
telethon is mocked via MagicMock/AsyncMock, the same house style
test_telegram_client.py's own tests already use, rather than the
hand-rolled fake class the deleted native-prompt tests used).

Suggestion-priority reordering (the Rules page's old "Always-allow
Suggestion Order" section: move up/down, exclude/re-include) was one such
restored feature but is gone again as of issue #151 -- every auto-accept
rule that plausibly matches an item now gets its own "Always allow" button
in the popup, so there's nothing left to prioritize or exclude. See
git history for the removed TestSuggestionPriorityState/
TestSuggestionPriorityMutators coverage.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from privacyfence import auto_accept, daemon_main, org_mode, resource_names, settings_controller as sc, update_checker
from privacyfence.auto_accept import ReviewContext
from privacyfence.policy import engine as policy_engine
from privacyfence.policy import propose as policy_propose
from privacyfence.policy import store as policy_store


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _drain_run_async(recorded) -> None:
    while recorded:
        func, args, kwargs = recorded.pop(0)
        func(*args, **kwargs)


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

    host_calls = []
    connector_host = SimpleNamespace(set_connectors=lambda conns: host_calls.append(conns))

    ctrl = sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)
    ctrl._host_calls = host_calls
    return ctrl


class TestRunAsyncMarshaling:
    """_run_async is the mechanism every threaded flow in this module funnels
    through. If it ever regresses to invoking on_done directly on the worker
    thread, the "on_done touches self/the page, so it must run on the main
    thread" invariant every mutation depends on breaks silently."""

    def test_success_result_never_delivered_directly_on_worker_thread(self, monkeypatch):
        recorded = []
        sc.set_main_dispatcher(lambda f, *a, **k: recorded.append((f, a, k)))

        done_calls = []
        work_thread = {}

        def work():
            work_thread["thread"] = threading.current_thread()
            return "alice@example.com"

        def done(ok, result):
            done_calls.append((ok, result))

        try:
            sc._run_async(work, done)

            assert wait_until(lambda: recorded)
            assert work_thread["thread"] is not threading.current_thread()
            assert done_calls == []

            func, args, kwargs = recorded[0]
            assert args == (True, "alice@example.com")
            func(*args, **kwargs)
            assert done_calls == [(True, "alice@example.com")]
        finally:
            sc.set_main_dispatcher(None)

    def test_exception_in_work_is_also_marshaled_not_raised_on_worker_thread(self, monkeypatch):
        recorded = []
        sc.set_main_dispatcher(lambda f, *a, **k: recorded.append((f, a, k)))
        boom = ValueError("auth failed")

        def work():
            raise boom

        done_calls = []
        try:
            sc._run_async(work, lambda ok, result: done_calls.append((ok, result)))

            assert wait_until(lambda: recorded)
            func, args, kwargs = recorded[0]
            assert args == (False, boom)
            func(*args, **kwargs)
            assert done_calls == [(False, boom)]
        finally:
            sc.set_main_dispatcher(None)


class TestOnChangeMarshaling:
    """Rule changes from a background thread (e.g. the web server's own
    asyncio thread, for an "Always allow" confirmation reached over /mcp)
    must marshal onto the main thread via the registered dispatcher before
    touching on_change (which may push into a live web page)."""

    def test_reload_from_background_thread_schedules_but_does_not_push_inline(self, controller, monkeypatch):
        recorded = []
        sc.set_main_dispatcher(
            lambda f, *a, **k: recorded.append((f, a, k, threading.current_thread()))
        )
        pushed = []
        controller.on_change = lambda state: pushed.append(threading.current_thread())

        bg_done = threading.Event()

        def background_thread_body():
            auto_accept.notify_rules_changed()
            bg_done.set()

        try:
            t = threading.Thread(target=background_thread_body)
            t.start()
            t.join(timeout=2)
            assert bg_done.is_set()

            assert pushed == []
            assert len(recorded) == 1
            func, args, kwargs, calling_thread = recorded[0]
            assert calling_thread is not threading.current_thread()

            func(*args, **kwargs)
            assert pushed == [threading.current_thread()]
        finally:
            sc.set_main_dispatcher(None)


class TestCallOnMain:
    """The dispatcher seam: call_on_main resolves to a registered
    set_main_dispatcher(), else runs inline -- the inline case is what makes
    _run_async's on_done actually observable when nothing has attached a
    dispatcher yet (a standalone import, or a test with no web server
    running), instead of dying with AttributeError inside the worker thread
    where nothing surfaces it (the exact bug this seam fixes -- see this
    module's git history).
    Through P9 a third path existed (AppKit's own run loop, via
    PyObjCTools.AppHelper.callAfter, when the native settings window was
    hosting); P10 deleted that host along with the rest of the AppKit UI
    layer."""

    def test_dispatcher_registered_is_used(self, monkeypatch):
        recorded = []
        sc.set_main_dispatcher(lambda f, *a: recorded.append((f, a)))
        try:
            calls = []
            sc.call_on_main(lambda x: calls.append(x), "hi")
            assert calls == []
            func, args = recorded[0]
            func(*args)
            assert calls == ["hi"]
        finally:
            sc.set_main_dispatcher(None)

    def test_no_dispatcher_runs_inline(self, monkeypatch):
        # This is the case that used to raise AttributeError against a None
        # AppHelper -- authenticate_connector's failure path (and every
        # other _run_async caller) has to surface an error when nothing has
        # attached a dispatcher, not silently die on the worker thread.
        sc.set_main_dispatcher(None)
        calls = []
        sc.call_on_main(lambda x: calls.append(x), "hi")
        assert calls == ["hi"]

    def test_run_async_on_done_runs_with_no_dispatcher_registered(self, monkeypatch):
        sc.set_main_dispatcher(None)
        done_calls = []
        boom = RuntimeError("auth failed")

        def work():
            raise boom

        sc._run_async(work, lambda ok, result: done_calls.append((ok, result)))
        assert wait_until(lambda: done_calls)
        assert done_calls == [(False, boom)]


class TestChangeListeners:
    """§16.8's risk #2: two on_change consumers (the native window's single
    on_change slot, plus web/state_stream.py's subscription) after this
    phase -- both must fire from one _push_snapshot."""

    def test_on_change_and_extra_listeners_both_fire(self, controller):
        on_change_calls = []
        listener_calls = []
        controller.on_change = on_change_calls.append
        controller.add_change_listener(listener_calls.append)

        controller._push_snapshot()

        assert len(on_change_calls) == 1
        assert len(listener_calls) == 1
        assert on_change_calls[0] == listener_calls[0]

    def test_remove_change_listener_stops_delivery(self, controller):
        listener_calls = []
        fn = listener_calls.append
        controller.add_change_listener(fn)
        controller.remove_change_listener(fn)

        controller._push_snapshot()

        assert listener_calls == []

    def test_remove_unknown_listener_is_a_no_op(self, controller):
        controller.remove_change_listener(lambda state: None)  # never raises


class TestConfigHelpers:
    def test_load_config_round_trips_yaml(self, controller, tmp_path):
        config_path = tmp_path / "other.yaml"
        config_path.write_text("connectors:\n  gmail:\n    enabled: false\n", encoding="utf-8")
        controller._config_path = str(config_path)

        assert controller._load_config() == {"connectors": {"gmail": {"enabled": False}}}

    def test_load_config_missing_file_returns_empty_dict(self, controller, tmp_path):
        controller._config_path = str(tmp_path / "does-not-exist.yaml")
        assert controller._load_config() == {}

    def test_load_config_malformed_yaml_returns_empty_dict_not_raise(self, controller, tmp_path):
        config_path = tmp_path / "bad.yaml"
        config_path.write_text(":\n  - not: [valid yaml", encoding="utf-8")
        controller._config_path = str(config_path)

        assert controller._load_config() == {}

    def test_save_config_writes_yaml_readable_back(self, controller):
        controller._save_config({"connectors": {"slack": {"enabled": True}}})

        assert controller._load_config() == {"connectors": {"slack": {"enabled": True}}}

    def test_save_config_write_failure_is_logged_not_raised(self, controller, monkeypatch):
        monkeypatch.setattr(
            sc, "atomic_write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )

        controller._save_config({"a": 1})  # must not raise

    def test_save_config_updates_audit_logger_security_config_hash(self, controller):
        # SEC-23: every settings.yaml write is a privacy-policy change, so
        # the audit log's per-decision fingerprint (AuditEntry.
        # security_config_hash) must move with it.
        from privacyfence.audit_log import compute_security_config_hash, get_audit_logger

        cfg = {"privacy": {"gmail": "block"}}
        controller._save_config(cfg)

        assert get_audit_logger()._security_config_hash == compute_security_config_hash(cfg)

    def test_save_config_does_not_update_hash_when_write_fails(self, controller, monkeypatch):
        from privacyfence.audit_log import get_audit_logger

        get_audit_logger().set_security_config_hash("unchanged")
        monkeypatch.setattr(
            sc, "atomic_write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )

        controller._save_config({"a": 1})

        assert get_audit_logger()._security_config_hash == "unchanged"

    def test_save_config_hash_update_failure_is_logged_not_raised(self, controller, monkeypatch):
        from privacyfence.audit_log import AuditLogger

        monkeypatch.setattr(
            AuditLogger, "set_security_config_hash",
            lambda self, value: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        controller._save_config({"a": 1})  # must not raise

    def test_save_and_reload_persists_and_triggers_rule_reload(self, controller, monkeypatch):
        reload_calls = []
        monkeypatch.setattr(sc, "notify_rules_changed", lambda: reload_calls.append(True))

        controller._save_and_reload({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}})

        assert reload_calls == [True]
        assert controller._load_config()["auto_accept_rules"] == {"gmail.read_message": [{"rule": "i_am_sender"}]}

    def test_save_and_reload_swallows_reload_failures(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "notify_rules_changed", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        controller._save_and_reload({})  # must not raise


class TestExtractDriveId:
    def test_bare_id_is_accepted_as_is(self):
        assert sc._extract_drive_id("FOLDER1") == "FOLDER1"

    def test_folder_url_extracts_the_id(self):
        url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms?usp=sharing"
        assert sc._extract_drive_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_file_url_extracts_the_id(self):
        url = "https://docs.google.com/spreadsheets/d/1AbCdEf12345/edit#gid=0"
        assert sc._extract_drive_id(url) == "1AbCdEf12345"

    def test_unparseable_text_returns_empty_string(self):
        assert sc._extract_drive_id("not a url or id, has spaces") == ""


class TestShortId:
    def test_short_id_passes_through_unchanged(self):
        assert sc._short_id("F1") == "F1"

    def test_long_id_is_truncated_with_ellipsis(self):
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        result = sc._short_id(long_id)
        assert result.startswith("1BxiMVs0")
        assert result.endswith("E2upms")
        assert "…" in result


class TestParseValueList:
    def test_splits_on_comma(self):
        assert sc._parse_value_list("a.com, b.com") == ["a.com", "b.com"]

    def test_empty_text_is_none(self):
        assert sc._parse_value_list("   ") is None

    def test_blank_entries_are_dropped(self):
        assert sc._parse_value_list("a.com, , b.com,") == ["a.com", "b.com"]


class TestPiiDetection:
    def test_toggle_flips_and_saves(self, controller):
        controller.toggle_pii_detection()
        assert controller._load_config()["pii_detection"]["enabled"] is False

    def test_toggling_twice_re_enables(self, controller):
        controller.toggle_pii_detection()
        controller.toggle_pii_detection()
        assert controller._load_config()["pii_detection"]["enabled"] is True

    def test_hot_reloads_live_detector_state(self, controller):
        from privacyfence import pii_detector

        assert pii_detector.is_pii_detection_enabled() is True
        controller.toggle_pii_detection()
        assert pii_detector.is_pii_detection_enabled() is False

    def test_category_flips_and_saves(self, controller):
        controller.toggle_pii_category("detect_ip_addresses")
        assert controller._load_config()["pii_detection"]["detect_ip_addresses"] is False

    def test_category_toggle_is_a_no_op_while_master_switch_off(self, controller):
        controller.toggle_pii_detection()  # now disabled
        before = controller._load_config()

        controller.toggle_pii_category("detect_ip_addresses")

        assert controller._load_config() == before

    def test_categories_toggle_independently(self, controller):
        controller.toggle_pii_category("detect_ip_addresses")

        cfg = controller._load_config()
        assert cfg["pii_detection"]["detect_ip_addresses"] is False
        assert cfg["pii_detection"].get("detect_financial_figures", True) is True


class TestEnableStepUp:
    """B9: the browser-reachable counterpart to hand-editing config/
    settings.yaml's own step_up: section. See SettingsController.
    enable_step_up's own docstring for what it refuses and why."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        # webauthn_stepup.has_credentials/add_credential resolve the
        # credential store off paths.authority_dir(), which is derived from
        # paths.data_dir() -- distinct from the controller fixture's own
        # sc.data_dir monkeypatch (settings_controller.py's own config/org
        # reads), so this needs its own patch, the same one
        # TestSensitiveActionStepUp in test_routes_settings.py uses.
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        from privacyfence import webauthn_stepup as wa
        from privacyfence.principal import LOCAL_PRINCIPAL
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def test_no_step_up_wired_is_refused(self, controller):
        before = controller._load_config()

        state = controller.enable_step_up()

        assert controller._load_config() == before
        assert controller.error
        assert state["general"]["step_up_available"] is False
        assert state["general"]["step_up_on"] is False

    def test_no_passkey_enrolled_is_refused(self, controller):
        from privacyfence.step_up_config import LiveStepUpConfig, StepUpConfig
        live = LiveStepUpConfig(StepUpConfig())
        controller.wire_step_up(live)
        before = controller._load_config()

        state = controller.enable_step_up()

        assert controller._load_config() == before
        assert controller.error
        assert live.enabled is False
        assert state["general"]["step_up_has_passkey"] is False
        assert state["general"]["step_up_on"] is False

    def test_enables_and_makes_mandatory_together(self, controller):
        from privacyfence.step_up_config import LiveStepUpConfig, StepUpConfig
        self._enroll()
        live = LiveStepUpConfig(StepUpConfig())
        controller.wire_step_up(live)

        state = controller.enable_step_up()

        cfg = controller._load_config()
        assert cfg["step_up"]["enabled"] is True
        assert cfg["step_up"]["require_passkey"] is True
        assert live.enabled is True
        assert live.require_passkey is True
        assert controller.error == ""
        assert state["general"]["step_up_on"] is True
        assert state["general"]["step_up_has_passkey"] is True

    def test_records_an_audit_entry_for_the_transition(self, controller):
        from privacyfence.audit_log import init_audit_logger
        from privacyfence.step_up_config import LiveStepUpConfig, StepUpConfig
        self._enroll()
        controller.wire_step_up(LiveStepUpConfig(StepUpConfig()))
        # Same setup TestAuditLog.test_snapshot_recent_entries_reflect_the_
        # audit_log above uses: init_audit_logger() registers the instance
        # get_audit_logger() (this method's own write path) returns, at the
        # exact directory the controller's own snapshot()/_audit_state reads
        # back from (authority_root(sc.data_dir())/logs/audit).
        log_dir = sc.authority_root(sc.data_dir()) / "logs" / "audit"
        init_audit_logger(str(log_dir))

        controller.enable_step_up()

        recent = controller.snapshot()["audit"]["recent"]
        assert any(e["decision"] == "step_up_requirement_enabled" for e in recent)

    def test_reenabling_an_already_enabled_install_writes_no_second_audit_entry(self, controller):
        # observe_step_up_requirement only reports a *transition* -- calling
        # enable_step_up again while already on must not raise or duplicate
        # the audit trail (it's also SettingsController's own idempotent
        # no-op precedent, same as toggle_pii_category's master-switch
        # guard above).
        from privacyfence.step_up_config import LiveStepUpConfig, StepUpConfig
        self._enroll()
        live = LiveStepUpConfig(StepUpConfig())
        controller.wire_step_up(live)
        controller.enable_step_up()

        state = controller.enable_step_up()

        assert live.enabled is True
        assert live.require_passkey is True
        assert state["general"]["step_up_on"] is True


class TestNotificationsDetail:
    def test_persists_under_web_notifications_detail(self, controller):
        controller.set_notifications_detail("detailed")

        assert controller._load_config()["web"]["notifications"]["detail"] == "detailed"

    def test_rejects_an_unknown_level(self, controller):
        before = controller._load_config()

        controller.set_notifications_detail("chatty")

        assert controller._load_config() == before

    def test_creates_the_web_notifications_block_if_absent(self, controller):
        # The fixture's config has no `web:` key at all -- setdefault chain
        # must create it rather than assuming toggle_settings_enabled or
        # some other action already has.
        assert "web" not in controller._load_config()

        controller.set_notifications_detail("standard")

        assert controller._load_config()["web"]["notifications"]["detail"] == "standard"

    def test_does_not_disturb_an_existing_enabled_flag(self, controller):
        cfg = controller._load_config()
        cfg.setdefault("web", {}).setdefault("notifications", {})["enabled"] = False
        controller._save_config(cfg)

        controller.set_notifications_detail("minimal")

        saved = controller._load_config()["web"]["notifications"]
        assert saved == {"enabled": False, "detail": "minimal"}

    def test_snapshot_general_reflects_the_current_level(self, controller):
        controller.set_notifications_detail("detailed")

        general = controller.snapshot()["general"]
        assert general["notifications_detail"] == "detailed"
        assert general["notifications_enabled"] is True

    def test_snapshot_general_defaults_when_unconfigured(self, controller):
        general = controller.snapshot()["general"]
        assert general["notifications_detail"] == "minimal"
        assert general["notifications_enabled"] is True


class TestUpdateCheck:
    def test_toggle_enabled_flips_and_saves(self, controller):
        controller.toggle_update_check()
        assert controller._load_config()["update_check"]["enabled"] is False

    def test_toggle_beta_flips_saves_and_checks_immediately(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1) or controller.snapshot())

        controller.toggle_update_check_beta()

        assert controller._load_config()["update_check"]["include_beta"] is True
        assert calls == [1]

    def test_timer_disabled_never_checks(self, controller, monkeypatch):
        controller._save_config({"update_check": {"enabled": False}})
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1))

        controller.on_update_check_timer()

        assert calls == []

    def test_timer_enabled_checks(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1))

        controller.on_update_check_timer()

        assert calls == [1]

    def test_check_for_updates_now_passes_include_beta(self, controller, monkeypatch):
        controller._save_config({"update_check": {"enabled": True, "include_beta": True}})
        captured = {}

        def fake_run_async(work, on_done):
            captured["include_beta"] = work()

        monkeypatch.setattr(sc, "_run_async", fake_run_async)
        monkeypatch.setattr(sc, "check_for_update", lambda **kw: kw.get("include_beta"))

        controller.check_for_updates_now()

        assert captured["include_beta"] is True

    def test_update_check_done_failure_does_not_touch_latest_update(self, controller):
        controller._latest_update = None
        controller._on_update_check_done(False, RuntimeError("boom"))
        assert controller._latest_update is None

    def test_update_check_done_success_pushes_state(self, controller):
        pushed = []
        controller.on_change = lambda state: pushed.append(state)

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x", is_beta=False, is_update_available=False,
        )
        controller._on_update_check_done(True, result)

        assert controller._latest_update is result
        assert len(pushed) == 1

    def test_update_available_pushes_state_for_the_web_banner(self, controller):
        # Through P9 an update found here also popped a native rumps.alert()
        # -- deleted at P10 (see _on_update_check_done's own comment). The
        # web General page's own banner is driven entirely by _general_state
        # (below), which reads straight off _latest_update -- there's
        # nothing else for _on_update_check_done itself to do beyond
        # recording the result and pushing a fresh snapshot.
        pushed = []
        controller.on_change = lambda state: pushed.append(state)

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x/tag/v2.2.0", is_beta=False, is_update_available=True,
        )
        controller._on_update_check_done(True, result)

        assert controller._latest_update is result
        assert len(pushed) == 1
        assert pushed[0]["general"]["update_available"] is True


class TestOrgConfigInstall:
    """install_org_config_bytes is the validate-then-write step behind
    web/routes_settings.py's multipart upload -- through P9 also reachable
    from a native "choose file" picker (install_org_config()), deleted at
    P10 along with the rest of the AppKit UI layer."""

    def test_non_json_file_sets_error(self, controller):
        controller.install_org_config_bytes(b"not valid json")

        assert controller.error
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_json_without_version_field_is_rejected(self, controller):
        controller.install_org_config_bytes(json.dumps({"google": {"client_id": "x"}}).encode())

        assert controller.error
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_valid_bundle_is_installed(self, controller):
        bundle = {"version": 1, "org_name": "Acme", "google": {"client_id": "x", "client_secret": "y"}}

        controller.install_org_config_bytes(json.dumps(bundle).encode())

        installed = json.loads((sc.org_dir() / "org_config.json").read_text(encoding="utf-8"))
        assert installed == bundle
        assert controller.error == ""

    def test_snapshot_reflects_installed_state(self, controller):
        controller.install_org_config_bytes(json.dumps({"version": 1}).encode())

        state = controller.snapshot()

        assert state["general"]["org_installed"] is True
        assert state["general"]["org_button_label"] == "Install/Update Organization Config…"

    def test_snapshot_not_installed_label(self, controller):
        state = controller.snapshot()
        assert state["general"]["org_installed"] is False
        assert state["general"]["org_button_label"] == "Install Organization Config…"


class TestOrgConfigInstallSigning:
    """SEC-05 (full signing): install_org_config_bytes runs every bundle
    through org_bundle_signing.verify_and_maybe_pin() before writing it to
    disk -- see that module's own docstring for the trust-on-first-use
    model this mirrors daemon_main.load_org_config's own enforcement of."""

    def test_unsigned_bundle_is_still_installed_when_nothing_is_pinned(self, controller):
        bundle = {"version": 1, "google": {"client_id": "x", "client_secret": "y"}}

        controller.install_org_config_bytes(json.dumps(bundle).encode())

        assert controller.error == ""
        installed = json.loads((sc.org_dir() / "org_config.json").read_text(encoding="utf-8"))
        assert installed == bundle

    def test_org_mode_bundle_without_signature_is_rejected(self, controller):
        bundle = {"version": 1, "mode": "org", "server": {}, "idp": {}}

        controller.install_org_config_bytes(json.dumps(bundle).encode())

        assert "requires a signed" in controller.error
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_first_signed_bundle_is_installed_and_pins_its_key(self, controller):
        from privacyfence import org_bundle_signing

        private_key, _ = org_bundle_signing.generate_keypair()
        bundle = org_bundle_signing.sign_bundle(
            {"version": 1, "mode": "org", "server": {}, "idp": {}}, private_key,
        )

        controller.install_org_config_bytes(json.dumps(bundle).encode())

        assert controller.error == ""
        assert org_bundle_signing.pinned_public_key_path(sc.org_dir()).exists()

    def test_bundle_signed_by_a_different_key_after_pin_is_rejected(self, controller):
        from privacyfence import org_bundle_signing

        first_key, _ = org_bundle_signing.generate_keypair()
        first_bundle = org_bundle_signing.sign_bundle({"version": 1, "org_name": "Acme"}, first_key)
        controller.install_org_config_bytes(json.dumps(first_bundle).encode())
        assert controller.error == ""

        other_key, _ = org_bundle_signing.generate_keypair()
        second_bundle = org_bundle_signing.sign_bundle({"version": 1, "org_name": "Evil Corp"}, other_key)
        controller.install_org_config_bytes(json.dumps(second_bundle).encode())

        assert "verification" in controller.error
        installed = json.loads((sc.org_dir() / "org_config.json").read_text(encoding="utf-8"))
        assert installed == first_bundle  # unchanged -- the bad update was never written


class TestToggleConnector:
    def test_flips_enabled_flag_and_refreshes(self, controller, monkeypatch):
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1) or controller.snapshot())

        controller.toggle_connector("gmail")

        cfg = controller._load_config()
        assert cfg["connectors"]["gmail"]["enabled"] is False
        assert refresh_calls == [1]

    def test_toggling_twice_re_enables(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller.snapshot())

        controller.toggle_connector("gmail")
        controller.toggle_connector("gmail")

        assert controller._load_config()["connectors"]["gmail"]["enabled"] is True


class TestRefreshConnectors:
    def test_updates_connectors_and_pushes_to_connector_host_after_drain(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: ([SimpleNamespace(name="drive")], {}))

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert controller._connectors == ["drive"]
        assert controller._host_calls == [[SimpleNamespace(name="drive")]]

    def test_connectors_changed_listener_fires_after_a_successful_refresh(self, controller, monkeypatch):
        # issue #396 Part C: wired to McpDispatcher.notify_tools_changed in
        # production (daemon_main.py) -- fires after the connector set is
        # actually swapped, so a listener reading fresh state sees it.
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: ([SimpleNamespace(name="drive")], {}))
        events = []
        controller.set_connectors_changed_listener(lambda: events.append(1))

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert events == [1]

    def test_connectors_changed_listener_does_not_fire_on_a_failed_refresh(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        def _raise(cfg, org):
            raise RuntimeError("boom")

        monkeypatch.setattr(daemon_main, "build_connectors", _raise)
        events = []
        controller.set_connectors_changed_listener(lambda: events.append(1))

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert events == []

    def test_unwired_listener_is_a_no_op(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: ([SimpleNamespace(name="drive")], {}))

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)  # must not raise with no listener wired

    def test_survives_a_broken_org_config_and_builds_with_an_empty_one(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        seen_org_configs = []

        def _build_connectors(cfg, org):
            seen_org_configs.append(org)
            return [SimpleNamespace(name="drive")], {}

        monkeypatch.setattr(daemon_main, "build_connectors", _build_connectors)
        monkeypatch.setattr(
            daemon_main, "load_org_config",
            lambda: (_ for _ in ()).throw(org_mode.ConfigurationError("bad org config")),
        )

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert controller._connectors == ["drive"]
        assert seen_org_configs == [{}]
        # The trailing self.snapshot() call inside refresh_connectors is
        # what surfaces this to the user -- see TestOrgConfigOrEmpty.
        assert controller.error == "bad org config"


class TestOrgConfigOrEmpty:
    """SEC-04 made load_org_config() raise org_mode.ConfigurationError for
    a present-but-broken org_config.json instead of silently treating it
    as absent. This settings surface is local-mode-only (org mode mounts
    no local settings page at all), so a broken org bundle here isn't the
    security-relevant "silently downgrade to no auth" case that startup
    must refuse to run with -- it should degrade to an error banner
    instead of taking the whole settings page down."""

    def test_returns_empty_dict_and_sets_error_when_config_is_broken(self, controller, monkeypatch):
        def _raise():
            raise org_mode.ConfigurationError("Organization config at /x is not valid JSON")
        monkeypatch.setattr(daemon_main, "load_org_config", _raise)

        assert controller._org_config_or_empty() == {}
        assert "not valid JSON" in controller.error

    def test_passes_through_the_parsed_config_when_valid(self, controller, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"slack": {"client_id": "abc"}})

        assert controller._org_config_or_empty() == {"slack": {"client_id": "abc"}}

    def test_snapshot_surfaces_the_error_instead_of_raising(self, controller, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "load_org_config",
            lambda: (_ for _ in ()).throw(org_mode.ConfigurationError("bad org config")),
        )

        state = controller.snapshot()

        assert state["error"] == "bad org config"

    def test_authenticate_connector_does_not_raise_on_broken_config(self, controller, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "load_org_config",
            lambda: (_ for _ in ()).throw(org_mode.ConfigurationError("bad org config")),
        )

        result = controller.authenticate_connector("slack")

        assert result["error"] == "bad org config"


class TestWireUnattendedListener:
    """P5: unattended-session changes are wired from outside the
    constructor now (by daemon_main.py, once it knows a McpDispatcher
    actually exists), not unconditionally inside it -- see
    SettingsController.__init__'s own docstring."""

    def test_registers_on_change_with_the_given_dispatcher(self, controller):
        registered = []
        dispatcher = SimpleNamespace(set_unattended_changed_listener=lambda cb: registered.append(cb))

        controller.wire_unattended_listener(dispatcher)

        assert registered == [controller._on_unattended_changed]


class TestAuthenticateDispatch:
    @pytest.mark.parametrize("cname,method", [
        ("gmail", "_authenticate_google"), ("drive", "_authenticate_google"),
        ("contacts", "_authenticate_google"), ("calendar", "_authenticate_google"),
        ("tasks", "_authenticate_google"), ("slack", "_authenticate_slack"),
        ("salesforce", "_authenticate_salesforce"), ("jira", "_authenticate_atlassian"),
        ("confluence", "_authenticate_atlassian"),
    ])
    def test_dispatches_to_the_right_per_service_method(self, controller, monkeypatch, cname, method):
        calls = []
        monkeypatch.setattr(controller, method, lambda *a: calls.append((cname, a)))

        controller.authenticate_connector(cname)

        assert len(calls) == 1

    def test_telegram_is_not_routed_through_the_generic_dispatch(self, controller, monkeypatch):
        # Telegram's phone/code/2FA flow is routed client-side into its own
        # modal instead (see settings_window_html.py) -- production JS never
        # posts authenticate_connector for it, but a stray call must still be
        # a harmless no-op rather than an error.
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.authenticate_connector("telegram")  # must not raise

        assert run_async_calls == []


class TestAuthenticateGoogle:
    def test_missing_org_config_sets_error_without_running_flow(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_google("gmail", {})

        assert controller.error
        assert run_async_calls == []

    def test_runs_authorize_and_check_connection_marks_busy_then_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        calls = []

        class FakeGmailClient:
            def __init__(self, client_config, token_file):
                calls.append(("init", client_config))

            def authorize_interactive(self):
                calls.append(("authorize",))

            def check_connection(self):
                calls.append(("check",))
                return "me@example.com"

        monkeypatch.setitem(sc._GOOGLE_CLIENTS, "gmail", FakeGmailClient)
        refresh_calls = []
        # refresh_connectors() itself runs a second _run_async hop -- stubbed
        # out here (TestRefreshConnectors below covers that hop directly) so
        # this test isn't racing a second background thread's append into
        # `recorded` against _drain_run_async's synchronous while-loop.
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert "gmail" in controller._busy_connectors
        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert calls == [("init", {"installed": {"client_id": "i", "client_secret": "s"}}), ("authorize",), ("check",)]
        assert "gmail" not in controller._busy_connectors
        assert refresh_calls == [1]

    def test_failed_auth_sets_error_and_clears_busy_without_refreshing(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        class FailingGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                raise RuntimeError("user closed browser")

        monkeypatch.setitem(sc._GOOGLE_CLIENTS, "gmail", FailingGmailClient)

        controller._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert "user closed browser" in controller.error
        assert refresh_calls == []
        assert "gmail" not in controller._busy_connectors


class TestAuthenticateSlack:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_slack({})

        assert controller.error
        assert run_async_calls == []

    def test_success_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(sc, "slack_authorize_interactive", lambda **kw: {"team_name": "Acme"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_slack({"slack": {"client_id": "id", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]
        assert controller.error == ""


class TestAuthenticateSalesforce:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_salesforce({})

        assert controller.error
        assert run_async_calls == []

    def test_success_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(sc, "salesforce_authorize_interactive", lambda **kw: {"instance_url": "https://x.salesforce.com"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_salesforce({"salesforce": {"consumer_key": "ck"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]


class TestAuthenticateAtlassian:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_atlassian({})

        assert controller.error
        assert run_async_calls == []

    def test_success_marks_busy_for_both_jira_and_confluence(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(sc, "atlassian_authorize_interactive", lambda **kw: {"site_url": "https://acme.atlassian.net"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert {"jira", "confluence"} <= controller._busy_connectors
        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]
        assert "jira" not in controller._busy_connectors
        assert "confluence" not in controller._busy_connectors

    def test_no_approval_ui_registered_falls_back_to_the_first_resource(self, controller, monkeypatch):
        # _pick_resource_index's own "no registry" case (approval_ui.py's
        # deferred_registry docstring) -- the default, unconfigured
        # ApprovalUI (no init_approval_ui() call in this test) returns None,
        # which pick_resource's own caller (atlassian_oauth.py) already
        # treats as "fall back to the first resource". Through P9 this same
        # outcome was also reachable via a cancelled native picker; P10
        # deleted that picker along with the rest of the AppKit UI layer,
        # so TestPickResourceIndexWebMode below covers the web-registry
        # equivalent (an explicit "cancel" answer) instead.
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(controller, "refresh_connectors", lambda: None)

        captured = {}

        def fake_authorize(**kwargs):
            resources = [
                {"url": "https://a.atlassian.net", "id": "a"},
                {"url": "https://b.atlassian.net", "id": "b"},
            ]
            chosen = kwargs["pick_resource"](resources)
            captured["site_url"] = chosen["url"]
            return {"site_url": chosen["url"]}

        monkeypatch.setattr(sc, "atlassian_authorize_interactive", fake_authorize)

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)
        assert captured["site_url"] == "https://a.atlassian.net"


class TestPickResourceIndexWebMode:
    """§16.2.2: the Atlassian picker routed through web_prompt.py when a
    WebApprovalUI (the only ApprovalUI implementation since P10) is the
    live ApprovalUI -- same picker, same cancelled-falls-back-to-first-
    resource/options-are-URLs contract, routed through the registry every
    approval card already uses."""

    def test_routes_through_the_web_registry_when_one_is_live(self, controller, monkeypatch):
        from privacyfence import approval_ui, web_approval_ui

        web_ui = web_approval_ui.WebApprovalUI()
        approval_ui.init_approval_ui(web_ui)
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: f(*a, **k))
        monkeypatch.setattr(controller, "refresh_connectors", lambda: None)

        # Assert on the options actually handed to the picker, not by
        # scanning the rendered HTML for the URL substrings -- same
        # structured-capture pattern the native-mode sibling test above
        # uses (picker_calls[0]["options"]), and it sidesteps CodeQL's
        # incomplete-URL-substring-sanitization heuristic, which pattern-
        # matches "<url> in html_string" regardless of context; there's no
        # sanitization here at all, just a rendered-content check, but the
        # heuristic can't tell the difference.
        choice_calls = []

        def fake_build_choice_html(**kwargs):
            choice_calls.append(kwargs)
            return "<div>fake choice dialog</div>"

        monkeypatch.setattr(sc.dialog_window_html, "build_choice_html", fake_build_choice_html)

        captured = {}

        def fake_authorize(**kwargs):
            resources = [
                {"url": "https://a.atlassian.net", "id": "a"},
                {"url": "https://b.atlassian.net", "id": "b"},
            ]
            chosen = kwargs["pick_resource"](resources)
            captured["site_url"] = chosen["url"]
            return {"site_url": chosen["url"]}

        monkeypatch.setattr(sc, "atlassian_authorize_interactive", fake_authorize)

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: web_ui.deferred_registry.list_pending())
        card = web_ui.deferred_registry.list_pending()[0]
        assert card.kind == "choice"
        assert choice_calls[0]["options"] == ["https://a.atlassian.net", "https://b.atlassian.net"]
        web_ui.deferred_registry.answer(card.id, "1")

        assert wait_until(lambda: "site_url" in captured)
        assert captured["site_url"] == "https://b.atlassian.net"

    def test_cancelled_choice_falls_back_to_the_first_resource(self, controller, monkeypatch):
        from privacyfence import approval_ui, web_approval_ui

        web_ui = web_approval_ui.WebApprovalUI()
        approval_ui.init_approval_ui(web_ui)
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: f(*a, **k))
        monkeypatch.setattr(controller, "refresh_connectors", lambda: None)

        captured = {}

        def fake_authorize(**kwargs):
            resources = [{"url": "https://a.atlassian.net", "id": "a"}, {"url": "https://b.atlassian.net", "id": "b"}]
            captured["site_url"] = kwargs["pick_resource"](resources)["url"]
            return {}

        monkeypatch.setattr(sc, "atlassian_authorize_interactive", fake_authorize)

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: web_ui.deferred_registry.list_pending())
        card = web_ui.deferred_registry.list_pending()[0]
        web_ui.deferred_registry.answer(card.id, "cancel")

        assert wait_until(lambda: "site_url" in captured)
        assert captured["site_url"] == "https://a.atlassian.net"


class TestTelegramStartAuth:
    """telethon is mocked the same way test_telegram_client.py's own tests
    do -- MagicMock() with AsyncMock() for the awaited methods, rather than
    a hand-rolled fake class (the pattern the pre-#120 native-prompt flow's
    tests used) -- see that file's TestCheckConnection etc. for the house
    style this follows."""

    def test_missing_credentials_sets_error_without_running_flow(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: None)
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_start_auth("+123456789")

        assert controller.error
        assert run_async_calls == []

    def test_empty_phone_sets_a_field_error_without_running_flow(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_start_auth("   ")

        assert controller._telegram_auth == {"step": "phone", "error": "Enter a phone number."}
        assert run_async_calls == []

    def test_happy_path_stores_phone_code_hash_and_advances_to_code_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash-123"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        state = controller.telegram_start_auth("+123456789")

        assert "telegram" in controller._busy_connectors
        assert state["telegram_auth"] == {"step": "phone", "error": ""}

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth == {
            "step": "code", "phone": "+123456789", "phone_code_hash": "hash-123", "error": "",
        }
        assert "telegram" not in controller._busy_connectors
        fake_client.send_code_request.assert_awaited_once_with("+123456789")
        fake_client.disconnect.assert_awaited_once()

    def test_connect_failure_keeps_phone_step_with_error(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock(side_effect=RuntimeError("network down"))
        fake_client.disconnect = AsyncMock()
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_start_auth("+123456789")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "phone"
        assert "network down" in controller._telegram_auth["error"]


class TestTelegramSubmitCode:
    def test_no_flow_in_progress_is_a_no_op(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_code("12345")

        assert run_async_calls == []

    def test_wrong_step_is_a_no_op(self, controller, monkeypatch):
        controller._telegram_auth = {"step": "password", "error": ""}
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_code("12345")

        assert run_async_calls == []

    def test_empty_code_sets_a_field_error(self, controller):
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}

        controller.telegram_submit_code("   ")

        assert controller._telegram_auth["error"] == "Enter the verification code."

    def test_happy_path_signs_in_and_refreshes(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {
            "step": "code", "phone": "+123456789", "phone_code_hash": "hash-123", "error": "",
        }
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("12345")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        fake_client.sign_in.assert_awaited_once_with("+123456789", "12345", phone_code_hash="hash-123")
        assert controller._telegram_auth is None
        assert controller.error == ""
        assert refresh_calls == [1]

    def test_2fa_required_advances_to_password_step_without_error(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        from telethon.errors import SessionPasswordNeededError

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("12345")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "password"
        assert controller._telegram_auth["error"] == ""

    def test_wrong_code_sets_error_and_stays_on_code_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=RuntimeError("invalid code"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("00000")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "code"
        assert "invalid code" in controller._telegram_auth["error"]


class TestTelegramSubmit2FA:
    def test_wrong_step_is_a_no_op(self, controller, monkeypatch):
        controller._telegram_auth = {"step": "code", "error": ""}
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_2fa("pw")

        assert run_async_calls == []

    def test_empty_password_sets_a_field_error(self, controller):
        controller._telegram_auth = {"step": "password", "error": ""}

        controller.telegram_submit_2fa("   ")

        assert controller._telegram_auth["error"]

    def test_happy_path_signs_in_and_refreshes(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "password", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_2fa("my-2fa-password")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        fake_client.sign_in.assert_awaited_once_with(password="my-2fa-password")
        assert controller._telegram_auth is None
        assert refresh_calls == [1]

    def test_wrong_password_sets_error_and_stays_on_password_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "password", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=RuntimeError("wrong password"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_2fa("nope")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "password"
        assert "wrong password" in controller._telegram_auth["error"]


class TestTelegramCancelAuth:
    def test_resets_to_no_active_flow(self, controller):
        controller._telegram_auth = {"step": "code", "error": ""}

        controller.telegram_cancel_auth()

        assert controller._telegram_auth is None

    def test_no_op_when_nothing_in_progress(self, controller):
        controller.telegram_cancel_auth()  # must not raise
        assert controller._telegram_auth is None


class TestTelegramAuthSnapshotState:
    def test_default_state_is_no_step_no_error(self, controller):
        state = controller.snapshot()
        assert state["telegram_auth"] == {"step": None, "error": ""}

    def test_reflects_in_progress_flow(self, controller):
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": "oops"}

        state = controller.snapshot()

        assert state["telegram_auth"] == {"step": "code", "error": "oops"}

    def test_never_echoes_a_login_code_or_2fa_password(self, controller):
        """§16.2.8: telegram_submit_code/telegram_submit_2fa carry a login
        code and an account password over loopback HTTP -- the snapshot
        this state feeds into every open settings page/window must never
        carry either back out, pinned by a test rather than left as a
        happy accident of _telegram_auth_state()'s current field list."""
        for step_state in (
            {"step": "phone", "error": ""},
            {"step": "code", "phone": "+15551234567", "phone_code_hash": "secret-hash", "error": ""},
            {"step": "password", "phone": "+15551234567", "phone_code_hash": "secret-hash", "error": ""},
        ):
            controller._telegram_auth = dict(step_state)
            state = controller.snapshot()
            assert set(state["telegram_auth"].keys()) == {"step", "error"}
            serialized = json.dumps(state)
            assert "+15551234567" not in serialized
            assert "secret-hash" not in serialized


class TestPolicyScopeCatalogue:
    """P6 of the policy v2 redesign: the Auto-accept page's "add a rule" scope picker -- one entry
    per policy.propose.SCOPES_BY_GROUP widening group, plus sc._POLICY_EXTRA_SCOPES."""

    def test_covers_every_propose_group_and_every_extra(self):
        ids = {entry["id"] for entry in sc._policy_scope_catalogue()}
        assert set(policy_propose.SCOPES_BY_GROUP) <= ids
        assert set(sc._POLICY_EXTRA_SCOPES) <= ids

    def test_apps_script_entry_needs_a_value_and_offers_read_and_update(self):
        entry = next(e for e in sc._policy_scope_catalogue() if e["id"] == "apps_script.project")
        assert entry["needs_value"] is True
        assert set(entry["verbs"]) == {"read", "update"}
        assert entry["connector"] == "apps_script"

    def test_gmail_and_slack_unconditional_extras_need_no_value(self):
        by_id = {e["id"]: e for e in sc._policy_scope_catalogue()}
        assert by_id["gmail.configure"]["needs_value"] is False
        assert by_id["gmail.configure"]["verbs"] == ["configure"]
        assert by_id["slack.share_anything"]["needs_value"] is False
        assert by_id["slack.share_anything"]["verbs"] == ["share"]

    def test_drive_folder_entry_unions_verbs_across_every_predicate_in_the_group(self):
        entry = next(e for e in sc._policy_scope_catalogue() if e["id"] == "drive.folder")
        # approved_folder (read/download) + approved_sandbox_folder (update/format/restructure/
        # comment/delete) + parent_folder_allowlist (create) + move_within_approved_folders (move).
        assert set(entry["verbs"]) == {
            "read", "download", "update", "format", "restructure", "comment", "delete", "create", "move",
        }


class TestAddPolicyRule:
    """P6: add_policy_rule is the Auto-accept page's one writer, straight to the on-disk v2
    ``auto_accept:`` section -- never through v1's auto_accept_rules/auto_accept_grants."""

    def test_adds_a_scope_rule_to_the_v2_section(self, controller):
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["read", "download"])

        rules = state["auto_accept"]["rules"]
        assert len(rules) == 1
        assert rules[0]["connector"] == "drive"
        assert {v["verb"] for v in rules[0]["verbs"]} == {"read", "download"}
        assert "FOLDER1" in rules[0]["value"]

        cfg = controller._load_config()
        assert cfg[policy_store.MIGRATED_TO_POLICY_V2_MARKER] is True
        assert cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY]["rules"][0]["predicate"] == "approved_folder"
        # Never *populates* the v1 sections -- a v2-only add writes nothing under auto_accept_rules.
        assert not cfg.get("auto_accept_rules")

    def test_adding_more_verbs_for_the_same_value_widens_the_existing_row(self, controller):
        controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["download"])

        rules = state["auto_accept"]["rules"]
        assert len(rules) == 1
        assert {v["verb"] for v in rules[0]["verbs"]} == {"read", "download"}

    def test_each_of_the_six_previously_unreachable_operations_becomes_addable(self, controller):
        state = controller.add_policy_rule("apps_script.project", "SCRIPT1", ["read", "update"])
        tools = {t for row in state["auto_accept"]["rules"] for t in row["covered_tools"]}
        assert {"apps_script_get_content", "apps_script_get_execution_log", "apps_script_write_content"} <= tools

        state = controller.add_policy_rule("gmail.configure", "", ["configure"])
        tools = {t for row in state["auto_accept"]["rules"] for t in row["covered_tools"]}
        assert "gmail_create_filter" in tools

        state = controller.add_policy_rule("slack.share_anything", "", ["share"])
        tools = {t for row in state["auto_accept"]["rules"] for t in row["covered_tools"]}
        assert "slack_create_group_chat" in tools

    def test_apps_script_rule_with_no_value_is_not_written(self, controller):
        state = controller.add_policy_rule("apps_script.project", "", ["read"])
        assert state["auto_accept"]["rules"] == []
        assert policy_store.AUTO_ACCEPT_CONFIG_KEY not in controller._load_config()

    def test_unknown_group_is_a_no_op(self, controller):
        state = controller.add_policy_rule("not.a.real.group", "X", ["read"])
        assert state["auto_accept"]["rules"] == []

    def test_unknown_verb_for_a_known_group_is_dropped_silently(self, controller):
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["read", "not-a-verb"])
        rules = state["auto_accept"]["rules"]
        assert len(rules) == 1
        assert {v["verb"] for v in rules[0]["verbs"]} == {"read"}

    def test_verb_the_group_does_not_govern_is_a_no_op(self, controller):
        # "share" is not among gmail.sender_domain's own verbs (read/download/archive).
        state = controller.add_policy_rule("gmail.sender_domain", "acme.com", ["share"])
        assert state["auto_accept"]["rules"] == []

    def test_engine_actually_matches_a_rule_written_here(self, controller):
        controller.add_policy_rule("apps_script.project", "SCRIPT1", ["read"])
        cfg = controller._load_config()
        rules = policy_store.compile_rules_from_config(cfg)
        ctx = ReviewContext(connector="apps_script", tool="apps_script_get_content",
                             args={"script_id": "SCRIPT1"}, raw_data=None, my_email="")
        matched, rule_id = policy_engine.evaluate(rules, "apps_script.read_content", ctx)
        assert matched is True
        assert rule_id == rules[0].id


class TestRemovePolicyRule:
    def test_removes_by_id(self, controller):
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        rule_id = state["auto_accept"]["rules"][0]["id"]

        state = controller.remove_policy_rule(rule_id)

        assert state["auto_accept"]["rules"] == []
        assert controller._load_config()[policy_store.AUTO_ACCEPT_CONFIG_KEY]["rules"] == []

    def test_unknown_id_is_a_no_op(self, controller):
        controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        before = controller._load_config()

        controller.remove_policy_rule("r-does-not-exist")

        assert controller._load_config() == before


class TestAutoAcceptRuleUsage:
    """P8 (rule attribution and staleness): each Auto-accept row's own match_count/last_matched/
    never_matched, from the audit log's rule_id field (see AuditEntry.rule_id's own docstring)."""

    def _record(self, controller, **overrides):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.authority_root(sc.data_dir()) / "logs" / "audit"
        log_dir.mkdir(parents=True, exist_ok=True)
        defaults = dict(
            timestamp="2026-07-06T12:00:00+00:00", week=current_week(), request_id="",
            connector="drive", tool="drive_write_file", tool_name="Write Drive file",
            summary="s", sender="", decision="auto_accepted", auto_accept_rule="approved_folder",
            latency_seconds=1.0,
        )
        defaults.update(overrides)
        AuditLogger(str(log_dir)).record(AuditEntry(**defaults))

    def test_never_matched_rule_reports_zero_and_no_last_matched(self, controller):
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        row = state["auto_accept"]["rules"][0]
        assert row["match_count"] == 0
        assert row["last_matched"] == ""
        assert row["never_matched"] is True

    def test_matched_rule_reports_count_and_last_matched(self, controller):
        state = controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        rule_id = state["auto_accept"]["rules"][0]["id"]
        self._record(controller, rule_id=rule_id)
        self._record(controller, rule_id=rule_id, timestamp="2026-07-07T12:00:00+00:00")

        state = controller.snapshot()
        row = state["auto_accept"]["rules"][0]
        assert row["match_count"] == 2
        assert row["never_matched"] is False
        assert row["last_matched"] != ""

    def test_usage_for_a_different_rule_id_does_not_leak_across_rows(self, controller):
        controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        self._record(controller, rule_id="r-some-other-rule")

        state = controller.snapshot()
        row = state["auto_accept"]["rules"][0]
        assert row["match_count"] == 0
        assert row["never_matched"] is True

    def test_entries_with_no_rule_id_do_not_count_as_a_match(self, controller):
        controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])
        self._record(controller, rule_id="")

        state = controller.snapshot()
        row = state["auto_accept"]["rules"][0]
        assert row["never_matched"] is True


class TestPolicyV2MigrationNotice:
    """P4 of the policy v2 redesign's Settings banner: policy_v2_migration_notice_html() -- see
    settings_controller.py's own docstring on it for why this is the dismissible-notice mechanism
    (web_shell.wrap's dismissible_notice_html), not the persistent banner."""

    def _seed(self, controller, *, migrated, rules):
        cfg = controller._load_config()
        if migrated:
            cfg[policy_store.MIGRATED_TO_POLICY_V2_MARKER] = True
        cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = {"version": 2, "rules": rules}
        controller._save_config(cfg)

    def test_no_notice_before_migration_even_with_a_destructive_rule_present(self, controller):
        self._seed(
            controller, migrated=False,
            rules=[{"id": "r1", "predicate": "always_allow", "operations": ["sheets.delete_dimensions"]}],
        )
        assert controller.policy_v2_migration_notice_html() is None

    def test_no_notice_after_migration_when_nothing_is_destructive_or_send(self, controller):
        self._seed(
            controller, migrated=True,
            rules=[{"id": "r1", "predicate": "approved_folder", "value": ["F1"],
                     "operations": ["drive.read_file_contents"]}],
        )
        assert controller.policy_v2_migration_notice_html() is None

    def test_notice_lists_destructive_rule_after_migration(self, controller):
        self._seed(
            controller, migrated=True,
            rules=[{"id": "r-delete", "predicate": "always_allow", "operations": ["sheets.delete_dimensions"]}],
        )
        notice = controller.policy_v2_migration_notice_html()
        assert notice is not None
        assert "r-delete" in notice
        assert "1 existing rule" in notice

    def test_notice_lists_send_rule_after_migration(self, controller):
        self._seed(
            controller, migrated=True,
            rules=[{"id": "r-send", "predicate": "always_allow", "operations": ["slack.send_message"]}],
        )
        notice = controller.policy_v2_migration_notice_html()
        assert notice is not None
        assert "r-send" in notice

    def test_notice_html_escapes_rule_id(self, controller):
        self._seed(
            controller, migrated=True,
            rules=[{"id": "<script>bad</script>", "predicate": "always_allow",
                     "operations": ["sheets.delete_dimensions"]}],
        )
        notice = controller.policy_v2_migration_notice_html()
        assert "<script>bad</script>" not in notice
        assert "&lt;script&gt;" in notice


class TestResolvedRuleValue:
    """P6: rule values resolve through the same cached-name machinery the old grant rows used,
    reusing RULE_NAME_TO_RESOURCE_TYPE for a predicate whose value is an opaque resource id."""

    def test_resolves_a_cached_name(self, controller):
        rt = sc.RULE_NAME_TO_RESOURCE_TYPE["approved_sandbox_folder"]
        controller._resolver._disk[resource_names._cache_key(rt, "F1")] = "Scratch"
        state = controller.add_policy_rule("drive.folder", "F1", ["update"])
        assert "Scratch" in state["auto_accept"]["rules"][0]["value"]

    def test_falls_back_to_a_short_id_when_uncached(self, controller):
        long_id = "1" * 40
        state = controller.add_policy_rule("drive.folder", long_id, ["update"])
        assert state["auto_accept"]["rules"][0]["value"] == sc._short_id(long_id)

    def test_predicate_with_no_resource_type_shows_the_raw_value(self, controller):
        state = controller.add_policy_rule("gmail.sender_domain", "acme.com", ["read"])
        assert state["auto_accept"]["rules"][0]["value"] == "acme.com"


class TestPrivacyFilter:
    def test_set_default_policy(self, controller):
        controller.set_default_policy("privacy", "block")

        assert controller._load_config()["privacy"]["default_policy"] == "block"
        from privacyfence import privacy_filter
        assert privacy_filter.category_policy("privacy", "body") == "block"

    def test_set_default_policy_invalid_value_is_a_no_op(self, controller):
        before = controller._load_config()

        controller.set_default_policy("privacy", "delete_everything")

        assert controller._load_config() == before

    def test_set_category_policy(self, controller):
        controller.set_category_policy("slack_privacy", "message_content", "block")

        cfg = controller._load_config()
        assert cfg["slack_privacy"]["categories"]["message_content"] == "block"

    def test_toggle_calendar_free_busy(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller.snapshot())

        controller.toggle_calendar_free_busy()

        assert controller._load_config()["calendar"]["free_busy_full_event_details"] is False

    def test_toggle_calendar_free_busy_refreshes_connectors(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: calls.append(1) or controller.snapshot())

        controller.toggle_calendar_free_busy()

        assert calls == [1]


class TestAuditLog:
    def test_set_log_level_persists_and_hot_applies(self, controller, monkeypatch):
        applied = []
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: applied.append(cfg.get("logging")))

        controller.set_log_level("DEBUG")

        assert controller._load_config()["logging"]["level"] == "DEBUG"
        assert applied == [{"level": "DEBUG"}]

    def test_set_log_level_rejects_unknown_levels(self, controller):
        before = controller._load_config()

        controller.set_log_level("NOT_A_LEVEL")

        assert controller._load_config() == before

    def test_export_audit_log_path_missing_dir_sets_error(self, controller):
        path = controller.export_audit_log_path()

        assert path is None
        assert controller.error

    def test_export_audit_log_path_no_activity_this_week_sets_error(self, controller):
        (sc.authority_root(sc.data_dir()) / "logs" / "audit").mkdir(parents=True)

        path = controller.export_audit_log_path()

        assert path is None
        assert controller.error

    def test_export_audit_log_path_exports_and_returns_the_current_weeks_path(self, controller):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.authority_root(sc.data_dir()) / "logs" / "audit"
        log_dir.mkdir(parents=True)
        week = current_week()
        entry = AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="approved", auto_accept_rule="", latency_seconds=1.0,
        )
        AuditLogger(str(log_dir)).record(entry)

        path = controller.export_audit_log_path()

        expected_xlsx = log_dir / f"{week}.xlsx"
        assert expected_xlsx.exists()
        assert path == str(expected_xlsx)
        assert controller.error == ""

    def test_snapshot_recent_entries_reflect_the_audit_log(self, controller):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.authority_root(sc.data_dir()) / "logs" / "audit"
        log_dir.mkdir(parents=True)
        entry = AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=current_week(), request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="auto_accepted", auto_accept_rule="i_am_sender",
            latency_seconds=1.0,
        )
        AuditLogger(str(log_dir)).record(entry)

        state = controller.snapshot()

        assert len(state["audit"]["recent"]) == 1
        assert state["audit"]["recent"][0]["tool"] == "Read Gmail message"
        assert state["audit"]["recent"][0]["decision"] == "auto_accepted"


class TestAbout:
    def test_quit_app_requests_daemon_shutdown(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: calls.append(1))

        controller.quit_app()

        assert calls == [1]


class TestSnapshotStructure:
    def test_snapshot_has_one_key_per_page(self, controller):
        state = controller.snapshot()
        assert set(state) == {
            "error", "general", "connectors", "telegram_auth", "auto_accept", "privacy", "audit", "about",
        }

    def test_connectors_cover_all_connectors(self, controller):
        state = controller.snapshot()
        assert {c["key"] for c in state["connectors"]} == set(sc.ALL_CONNECTORS)

    def test_auto_accept_state_has_rules_scope_groups_and_connectors(self, controller):
        state = controller.snapshot()
        auto_accept_state = state["auto_accept"]
        assert set(auto_accept_state) == {"rules", "scope_groups", "connectors"}
        assert auto_accept_state["rules"] == []
        assert auto_accept_state["scope_groups"]
        # Sheets/Docs tools ride Drive's own scope, so "drive" -- not "sheets"/"docs" -- is the
        # connector a drive.folder-scoped rule's own catalogue entry reports.
        assert "drive" in auto_accept_state["connectors"]
        assert "sheets" not in auto_accept_state["connectors"]

    def test_privacy_groups_include_calendar_and_the_six_category_groups(self, controller):
        state = controller.snapshot()
        keys = {g["key"] for g in state["privacy"]["groups"]}
        assert keys == set(sc.PRIVACY_GROUP_LABELS) | {"calendar"}


class TestConnectorsStateBlockedBy:
    """_connectors_state()'s blocked_by field (issue #396 Phase 1) -- what
    tells a connected connector, a deliberately disabled one, and one that
    failed to build apart, instead of every un-built connector reading the
    same "not connected" way to a client. Sourced from build_connectors()'s
    own per-connector failure map, threaded in via
    SettingsController._connector_failures (populated at __init__ and
    refreshed by refresh_connectors())."""

    def _row(self, controller, key: str) -> dict:
        rows = {row["key"]: row for row in controller.snapshot()["connectors"]}
        return rows[key]

    def test_none_for_a_connected_connector(self, controller):
        controller._connectors = ["gmail"]
        # A stale failure entry can linger from before the connector
        # authenticated -- refresh_connectors() replaces the whole map on
        # every run, but nothing should surface it once authed is true.
        controller._connector_failures = {"gmail": "no_org_config"}

        row = self._row(controller, "gmail")

        assert row["authed"] is True
        assert row["blocked_by"] is None

    def test_none_for_a_deliberately_disabled_connector_even_without_a_failure_entry(self, controller):
        cfg = controller._load_config()
        cfg.setdefault("connectors", {})["gmail"] = {"enabled": False}
        controller._save_config(cfg)

        row = self._row(controller, "gmail")

        assert row["enabled"] is False
        assert row["blocked_by"] is None

    def test_no_org_config_for_an_enabled_unbuilt_connector(self, controller):
        controller._connector_failures = {"gmail": "no_org_config"}

        row = self._row(controller, "gmail")

        assert row["authed"] is False
        assert row["enabled"] is True
        assert row["blocked_by"] == "no_org_config"

    def test_not_authenticated_for_an_enabled_unbuilt_connector(self, controller):
        controller._connector_failures = {"slack": "not_authenticated"}

        row = self._row(controller, "slack")

        assert row["blocked_by"] == "not_authenticated"

    def test_redacted_message_passed_through_for_an_enabled_unbuilt_connector(self, controller):
        controller._connector_failures = {"salesforce": "Tool call failed. See the PrivacyFence log for details."}

        row = self._row(controller, "salesforce")

        assert row["blocked_by"] == "Tool call failed. See the PrivacyFence log for details."

    def test_none_for_a_connector_that_hasnt_failed_or_connected(self, controller):
        # No entry at all in _connector_failures -- e.g. before the first
        # refresh_connectors() run has populated it for this connector.
        row = self._row(controller, "jira")

        assert row["blocked_by"] is None

    def test_refresh_connectors_replaces_the_failure_map(self, monkeypatch, controller):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(
            daemon_main, "build_connectors",
            lambda cfg, org: ([], {"gmail": "no_org_config"}),
        )

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert self._row(controller, "gmail")["blocked_by"] == "no_org_config"


class TestStatusConnectors:
    """status_connectors() (issue #396 Phase 2) -- the same per-connector
    state _connectors_state() derives for the settings page, reshaped into
    the {name, enabled, authenticated, blocked_by} rows privacyfence_status
    documents. web/mcp_dispatch.py's McpDispatcher wires this in as its
    connectors_state_provider; this suite just proves the reshape is
    faithful to the underlying state _connectors_state() itself already has
    its own dedicated coverage for (TestConnectorsStateBlockedBy above)."""

    def _row(self, controller, name: str) -> dict:
        rows = {row["name"]: row for row in controller.status_connectors()}
        return rows[name]

    def test_shape_has_exactly_the_documented_keys(self, controller):
        row = self._row(controller, "gmail")
        assert set(row) == {"name", "enabled", "authenticated", "blocked_by"}

    def test_authenticated_mirrors_authed(self, controller):
        controller._connectors = ["gmail"]
        assert self._row(controller, "gmail")["authenticated"] is True
        assert self._row(controller, "slack")["authenticated"] is False

    def test_enabled_mirrors_the_connectors_state_row(self, controller):
        cfg = controller._load_config()
        cfg.setdefault("connectors", {})["gmail"] = {"enabled": False}
        controller._save_config(cfg)
        assert self._row(controller, "gmail")["enabled"] is False

    def test_blocked_by_mirrors_the_connectors_state_row(self, controller):
        controller._connector_failures = {"gmail": "no_org_config"}
        assert self._row(controller, "gmail")["blocked_by"] == "no_org_config"

    def test_reflects_a_live_refresh(self, monkeypatch, controller):
        recorded = []
        monkeypatch.setattr(sc, "_main_dispatch", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: ([], {"slack": "not_authenticated"}))

        controller.refresh_connectors()
        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert self._row(controller, "slack")["blocked_by"] == "not_authenticated"


class TestRuleUiCompleteness:
    """Structural checks tying the settings window's rule UI to auto_accept's
    rule engine -- see test_menu_bar.py's pre-#120 version of this class for
    the original regressions these caught (calendar.set_visibility/
    non_private_event never reachable from the UI, "docs" missing from
    RULES_MENU_GROUPS)."""

    @staticmethod
    def _all_rule_names() -> set[str]:
        """Every v1 predicate name ``policy.compat.compile_rule_entry`` (the migration's own v1
        -> v2 compiler, and P9's sole remaining reader of v1 rule names, now that
        ``AutoAcceptEvaluator`` is gone) actually recognizes: every v2 scope selector's own id,
        plus every legacy name a ``ConditionSelector.replaces`` maps onto one -- see that module's
        own docstring for why a v1 predicate is always exactly one or the other."""
        from privacyfence.policy import conditions, scopes

        names = set(scopes.SCOPE_SELECTORS)
        for selector in conditions.CONDITION_SELECTORS.values():
            names.update(selector.replaces)
        return names

    @staticmethod
    def _rules_by_operation_names() -> set[str]:
        return {rule for rules in sc.RULES_BY_OPERATION.values() for rule in rules}

    def test_every_rule_is_reachable_from_some_operation(self):
        unreachable = self._all_rule_names() - self._rules_by_operation_names()
        assert unreachable == set()

    def test_no_stale_rule_names_in_rules_by_operation(self):
        stale = self._rules_by_operation_names() - self._all_rule_names()
        assert stale == set()

    def test_every_operation_label_is_a_real_operation_key(self):
        real_ops = set(auto_accept.TOOL_TO_OPERATION.values())
        fake = set(sc.OPERATION_LABELS) - real_ops
        assert fake == set()

    def test_every_rules_by_operation_key_has_a_label(self):
        unlabeled = set(sc.RULES_BY_OPERATION) - set(sc.OPERATION_LABELS)
        assert unlabeled == set()
