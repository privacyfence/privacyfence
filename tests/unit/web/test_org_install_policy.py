"""Tests for web/org_install_policy.py -- applying an install-wide
privacy/PII policy change from org mode's admin settings surface (#400 C3e).

The behaviour these pin down is mostly "the change reaches everyone": a
policy with no per-user dimension, edited by one admin, has to become true
for every principal in the process, not just the one whose registry entry
``init_privacy_filter``/``init_pii_detection`` happen to write.
"""
from __future__ import annotations

import pytest
import yaml

from privacyfence import audit_log, pii_detector, privacy_filter
from privacyfence.principal import Principal, principal_scope
from privacyfence.web import org_install_policy, org_settings_scope


ALICE = Principal(id="alice")
BOB = Principal(id="bob")


def _settings_file(tmp_path, settings: dict) -> str:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return str(path)


def _seed_registries(settings: dict, *principals: Principal) -> None:
    """Give each principal a privacy-filter and PII entry, the way
    daemon_main._load_principal_settings does on their first request."""
    pii_cfg = settings.get("pii_detection", {}) or {}
    for principal in principals:
        with principal_scope(principal):
            privacy_filter.init_privacy_filter(settings, org_managed=True)
            pii_detector.init_pii_detection(
                pii_cfg.get("enabled", True),
                detect_ip_addresses=pii_cfg.get("detect_ip_addresses", True),
                detect_financial_figures=pii_cfg.get("detect_financial_figures", True),
            )


class TestActionSurface:
    def test_every_supported_action_is_one_the_scope_split_calls_admin_only(self):
        # The whole point of routing these through the same action names
        # local mode dispatches (#400 C3b/C3c): a route authorizes with
        # is_action_permitted and applies with apply_change, using one
        # string. An action this module could apply but that split doesn't
        # gate on is_admin would be a hole.
        assert org_install_policy.SUPPORTED_ACTIONS <= org_settings_scope.ADMIN_ONLY_ACTIONS

    def test_an_unsupported_action_is_rejected(self, tmp_path):
        settings: dict = {}
        with pytest.raises(org_install_policy.PolicyChangeRejected):
            org_install_policy.apply_change(
                settings, _settings_file(tmp_path, settings),
                action="remove_rule_row", payload={},
            )

    def test_no_config_path_is_rejected_rather_than_guessed_at(self):
        settings: dict = {}
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="settings.yaml path"):
            org_install_policy.apply_change(
                settings, "", action="set_default_policy",
                payload={"group": "privacy", "policy": "allow"},
            )
        assert settings == {}


class TestDefaultPolicy:
    def test_writes_settings_yaml_and_updates_the_live_dict(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        path = _settings_file(tmp_path, settings)

        summary = org_install_policy.apply_change(
            settings, path, action="set_default_policy",
            payload={"group": "privacy", "policy": "redact"},
        )

        assert "redact" in summary
        assert settings["privacy"]["default_policy"] == "redact"
        assert yaml.safe_load((tmp_path / "settings.yaml").read_text())["privacy"]["default_policy"] == "redact"

    def test_takes_effect_for_every_principal_not_just_the_editor(self, tmp_path):
        settings = {"privacy": {"default_policy": "allow"}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE, BOB)

        with principal_scope(ALICE):
            org_install_policy.apply_change(
                settings, path, action="set_default_policy",
                payload={"group": "privacy", "policy": "block"},
            )

        for principal in (ALICE, BOB):
            with principal_scope(principal):
                assert privacy_filter.category_policy("privacy", "body") == "block"

    def test_editing_one_group_does_not_reopen_an_absent_group(self, tmp_path):
        # The org_managed=False trap: init_privacy_filter's own default
        # resolves a genuinely absent group to "allow". Reached from org
        # mode that would turn every unconfigured group's fail-closed
        # "block" into "allow" as a side effect of editing an unrelated one.
        settings = {"privacy": {"default_policy": "block"}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE)

        with principal_scope(ALICE):
            org_install_policy.apply_change(
                settings, path, action="set_default_policy",
                payload={"group": "privacy", "policy": "allow"},
            )
            assert privacy_filter.category_policy("privacy", "body") == "allow"
            # slack_privacy was never in settings.yaml at all.
            assert privacy_filter.category_policy("slack_privacy", "message_content") == "block"

    @pytest.mark.parametrize("payload", [
        {"group": "not_a_group", "policy": "allow"},
        {"group": "privacy", "policy": "not_a_policy"},
        {"group": "privacy"},
    ])
    def test_rejects_a_bad_payload_without_writing(self, tmp_path, payload):
        settings = {"privacy": {"default_policy": "block"}}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected):
            org_install_policy.apply_change(
                settings, path, action="set_default_policy", payload=payload,
            )
        assert settings == {"privacy": {"default_policy": "block"}}
        assert yaml.safe_load((tmp_path / "settings.yaml").read_text())["privacy"]["default_policy"] == "block"


class TestCategoryPolicy:
    def test_sets_one_category_leaving_the_group_default_alone(self, tmp_path):
        settings = {"privacy": {"default_policy": "allow"}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE)

        org_install_policy.apply_change(
            settings, path, action="set_category_policy",
            payload={"group": "privacy", "category": "body", "policy": "block"},
        )

        assert settings["privacy"]["categories"] == {"body": "block"}
        with principal_scope(ALICE):
            assert privacy_filter.category_policy("privacy", "body") == "block"
            assert privacy_filter.category_policy("privacy", "metadata") == "allow"

    def test_rejects_a_group_section_that_is_not_a_mapping(self, tmp_path):
        # init_privacy_filter (SEC-07) refuses to start the daemon on this,
        # so it only exists if settings.yaml was hand-edited afterwards --
        # reject rather than crash on `setdefault` against a string.
        settings = {"privacy": "not a mapping"}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="not a mapping"):
            org_install_policy.apply_change(
                settings, path, action="set_category_policy",
                payload={"group": "privacy", "category": "body", "policy": "block"},
            )
        assert settings == {"privacy": "not a mapping"}

    def test_rejects_a_category_that_belongs_to_another_group(self, tmp_path):
        settings: dict = {}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="unknown category"):
            org_install_policy.apply_change(
                settings, path, action="set_category_policy",
                # file_content is a drive_privacy category, not a privacy one.
                payload={"group": "privacy", "category": "file_content", "policy": "block"},
            )


class TestPiiGate:
    def test_disabling_detection_reaches_every_principal(self, tmp_path):
        settings = {"pii_detection": {"enabled": True}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE, BOB)

        org_install_policy.apply_change(
            settings, path, action="toggle_pii_detection", payload={"enabled": "false"},
        )

        assert settings["pii_detection"]["enabled"] is False
        for principal in (ALICE, BOB):
            with principal_scope(principal):
                assert pii_detector.is_pii_detection_enabled() is False

    def test_an_optional_category_can_be_turned_off(self, tmp_path):
        settings = {"pii_detection": {"enabled": True}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE)

        org_install_policy.apply_change(
            settings, path, action="toggle_pii_category",
            payload={"category_key": "detect_ip_addresses", "enabled": "false"},
        )

        assert settings["pii_detection"]["detect_ip_addresses"] is False
        with principal_scope(ALICE):
            assert "IP address" not in pii_detector.detect_pii_categories("ping 10.1.2.3 please")

    def test_a_category_cannot_be_changed_while_the_master_switch_is_off(self, tmp_path):
        settings = {"pii_detection": {"enabled": False}}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="enable PII detection"):
            org_install_policy.apply_change(
                settings, path, action="toggle_pii_category",
                payload={"category_key": "detect_ip_addresses", "enabled": "false"},
            )

    def test_rejects_an_unknown_category(self, tmp_path):
        settings = {"pii_detection": {"enabled": True}}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="unknown PII category"):
            org_install_policy.apply_change(
                settings, path, action="toggle_pii_category",
                payload={"category_key": "detect_unicorns", "enabled": "false"},
            )

    def test_re_enabling_detection_is_the_same_path(self, tmp_path):
        settings = {"pii_detection": {"enabled": False}}
        path = _settings_file(tmp_path, settings)
        _seed_registries(settings, ALICE)

        org_install_policy.apply_change(
            settings, path, action="toggle_pii_detection", payload={"enabled": "true"},
        )

        assert settings["pii_detection"]["enabled"] is True
        with principal_scope(ALICE):
            assert pii_detector.is_pii_detection_enabled() is True

    def test_rejects_a_non_boolean_enabled_value(self, tmp_path):
        settings = {"pii_detection": {"enabled": True}}
        path = _settings_file(tmp_path, settings)
        with pytest.raises(org_install_policy.PolicyChangeRejected, match="must be true or false"):
            org_install_policy.apply_change(
                settings, path, action="toggle_pii_detection", payload={"enabled": "maybe"},
            )


class TestAuditFingerprint:
    def test_every_principals_logger_starts_stamping_the_new_policy_hash(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        path = _settings_file(tmp_path, settings)
        for principal in (ALICE, BOB):
            with principal_scope(principal):
                audit_log.init_audit_logger(
                    str(tmp_path / principal.id), security_config_hash="stale",
                )

        org_install_policy.apply_change(
            settings, path, action="set_default_policy",
            payload={"group": "privacy", "policy": "allow"},
        )

        expected = audit_log.compute_security_config_hash(settings)
        for principal in (ALICE, BOB):
            with principal_scope(principal):
                assert audit_log.get_audit_logger()._security_config_hash == expected


class TestFailedWrite:
    def test_an_unwritable_settings_yaml_leaves_everything_unchanged(self, tmp_path):
        settings = {"privacy": {"default_policy": "block"}}
        _seed_registries(settings, ALICE)
        # A directory where the file should be: the atomic write fails, and
        # nothing may have been adopted into the live dict or reloaded.
        unwritable = tmp_path / "settings.yaml"
        unwritable.mkdir()

        with pytest.raises(OSError):
            org_install_policy.apply_change(
                settings, str(unwritable), action="set_default_policy",
                payload={"group": "privacy", "policy": "allow"},
            )

        assert settings == {"privacy": {"default_policy": "block"}}
        with principal_scope(ALICE):
            assert privacy_filter.category_policy("privacy", "body") == "block"
