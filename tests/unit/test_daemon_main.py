"""Tests for daemon_main's connector-wiring and config-loading logic.

build_connectors() is the function that turns (settings.yaml, org_config.json,
per-user token files) into the live connector list the ConnectorHost exposes
to /mcp. Its contract, stated in the module docstring, is "graceful:
missing org config or auth -> connector skipped" -- a bug here means a
connector silently vanishes (or, worse, gets wired up without the gating it's
supposed to have). Every *Client class it touches is faked out at the
daemon_main import site so these tests exercise only the wiring, not the
real OAuth/HTTP clients (those are covered separately per-client).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import portalocker
import sys

import pytest
import yaml

from privacyfence import admin_config, app_credentials, daemon_main, org_mode
from privacyfence.connectors.slack import SlackConnector
from privacyfence.connectors.telegram import TelegramConnector
from privacyfence.paths import data_dir
from privacyfence.policy import store as policy_store
from privacyfence.policy.engine import PolicyRule
from privacyfence.safe_errors import GENERIC_PUBLIC_MESSAGE

from ..helpers import policy_rules


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fake_client_class(*, result=None, connection_error: Exception | None = None,
                       init_error: Exception | None = None, authorize_error: Exception | None = None):
    """A stand-in for a *Client class. Captures the kwargs it was
    constructed with (on the class, since daemon_main always constructs
    exactly one instance per connector) and controls check_connection()."""

    class _FakeClient:
        captured_kwargs: dict | None = None
        instantiated = False
        authorize_called = False
        directories_refreshed = False
        account_id = ""

        def __init__(self, **kwargs):
            type(self).instantiated = True
            type(self).captured_kwargs = kwargs
            if init_error is not None:
                raise init_error

        def authorize_interactive(self):
            type(self).authorize_called = True
            if authorize_error is not None:
                raise authorize_error

        def check_connection(self):
            if connection_error is not None:
                raise connection_error
            return result

        def ensure_directories_fresh(self):
            # Only SlackClient has this method for real -- harmless no-op
            # for every other *Client fake built from this same factory.
            type(self).directories_refreshed = True

    return _FakeClient


@pytest.fixture(autouse=True)
def _no_ambient_telegram(monkeypatch):
    """build_connectors() wires up Telegram independently of any org config --
    only telegram_app_credentials() (baked into the local checkout, or set via
    PRIVACYFENCE_TELEGRAM_API_ID/HASH) and a real credentials/telegram.session
    under PROJECT_ROOT gate it. Without this, tests that don't care about
    Telegram would silently pick up whatever real session a developer has
    authenticated from source with -- default it off here; the Telegram-
    specific tests below override this themselves via their own
    monkeypatch.setattr calls."""
    monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)


_GOOGLE_CLIENT_ATTRS = [
    "GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient",
    "AppsScriptClient",
]


@pytest.fixture(autouse=True)
def _no_ambient_google_clients(monkeypatch):
    """Google-family tests are parametrized to mock only the one *Client class
    under test, leaving the others as the real classes -- previously safe
    because they'd fail closed on a missing token file. A real, valid token
    for any of them in this checkout's credentials/ (e.g. from `--tasks-oauth`
    or the menu bar) would let that one actually construct and succeed,
    silently changing these tests' results. Default all six to fail closed;
    a test overrides one via its own monkeypatch.setattr, same as above."""
    for attr in _GOOGLE_CLIENT_ATTRS:
        monkeypatch.setattr(daemon_main, attr, fake_client_class(init_error=FileNotFoundError("no token file")))


# ---------------------------------------------------------------------------- #
# _resolve_path / _google_client_config
# ---------------------------------------------------------------------------- #

class TestResolvePath:
    def test_absolute_path_is_returned_unchanged(self, tmp_path):
        path = str(tmp_path / "x.json")
        assert daemon_main._resolve_path(path) == path

    def test_relative_path_is_joined_with_project_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        assert daemon_main._resolve_path("credentials/x.json") == os.path.join(str(tmp_path), "credentials/x.json")

    def test_relative_path_for_a_non_local_principal_uses_its_own_storage_root(self, monkeypatch, tmp_path):
        # The branch connector_registry.py's ConnectorRegistry.get() relies on;
        # PROJECT_ROOT (the local-principal path above) must stay
        # untouched by this.
        from privacyfence import paths
        from privacyfence.principal import Principal, principal_scope

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        with principal_scope(Principal(id="alice")):
            result = daemon_main._resolve_path("credentials/x.json")
        assert result == str(tmp_path / "users" / "alice" / "credentials" / "x.json")


# ---------------------------------------------------------------------------- #
# _resolve_authority_path
# ---------------------------------------------------------------------------- #

class TestResolveAuthorityPath:
    def test_absolute_path_is_returned_unchanged(self, tmp_path):
        path = str(tmp_path / "x.json")
        assert daemon_main._resolve_authority_path(path) == path

    def test_relative_path_is_joined_with_project_roots_authority_subdirectory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))

        result = daemon_main._resolve_authority_path("config/settings.yaml")

        assert result == str(tmp_path / "authority" / "config" / "settings.yaml")

    def test_relative_path_for_a_non_local_principal_uses_its_own_authority_subdirectory(self, monkeypatch, tmp_path):
        from privacyfence import paths
        from privacyfence.principal import Principal, principal_scope

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        with principal_scope(Principal(id="alice")):
            result = daemon_main._resolve_authority_path("config/settings.yaml")
        assert result == str(tmp_path / "users" / "alice" / "authority" / "config" / "settings.yaml")


class TestGoogleClientConfig:
    def test_empty_when_no_google_section(self):
        assert daemon_main._google_client_config({}) == {}

    def test_empty_when_client_id_missing(self):
        org_config = {"google": {"client_secret": "s"}}
        assert daemon_main._google_client_config(org_config) == {}

    def test_empty_when_client_secret_missing(self):
        org_config = {"google": {"client_id": "i"}}
        assert daemon_main._google_client_config(org_config) == {}

    def test_wraps_into_installed_shape_when_both_present(self):
        org_config = {"google": {"client_id": "i", "client_secret": "s", "extra": "x"}}
        assert daemon_main._google_client_config(org_config) == {
            "installed": {"client_id": "i", "client_secret": "s", "extra": "x"}
        }


# ---------------------------------------------------------------------------- #
# load_config / load_org_config
# ---------------------------------------------------------------------------- #

class TestLoadConfig:
    def test_bootstraps_default_when_missing(self, tmp_path):
        config_path = str(tmp_path / "settings.yaml")
        config = daemon_main.load_config(config_path)
        assert os.path.exists(config_path)
        assert isinstance(config, dict)

    def test_loads_existing_file_without_overwriting(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"connectors": {"gmail": {"enabled": False}}}))
        config = daemon_main.load_config(str(config_path))
        assert config == {"connectors": {"gmail": {"enabled": False}}}

    def test_raises_value_error_when_not_a_mapping(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump(["not", "a", "mapping"]))
        with pytest.raises(ValueError, match="did not parse to a mapping"):
            daemon_main.load_config(str(config_path))

    def test_empty_file_yields_empty_dict(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("")
        assert daemon_main.load_config(str(config_path)) == {}

    def test_bootstrapped_default_is_in_the_current_policy_format(self, tmp_path):
        # The packaged example is what every fresh install starts from: it must load, carry no v1
        # section, and its rules must compile to exactly the ids they were written with.
        config = daemon_main.load_config(str(tmp_path / "settings.yaml"))
        assert not set(config) & set(policy_store.V1_SECTION_KEYS)
        rules = policy_store.compile_rules_from_config(config)
        assert rules
        assert [rule.id for rule in rules] == [policy_store.rule_id_for_rule(rule) for rule in rules]

    def test_v1_policy_section_is_refused_with_a_clear_error(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        original = yaml.dump({"auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]}})
        config_path.write_text(original)
        with pytest.raises(policy_store.V1PolicyConfigError) as excinfo:
            daemon_main.load_config(str(config_path))
        assert str(config_path) in str(excinfo.value)
        assert "'auto_accept_rules'" in str(excinfo.value)
        # Refused, not converted: the file is untouched and no backup is written.
        assert config_path.read_text() == original
        assert not (tmp_path / "settings.yaml.bak").exists()

    def test_main_reports_a_v1_config_as_a_configuration_error(self, tmp_path, monkeypatch, capsys):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_grants": {}}))
        monkeypatch.setattr(sys, "argv", ["privacyfence-app", "--config", str(config_path)])
        monkeypatch.setattr(daemon_main.privilege_separation, "check_runtime_identity", lambda: None)
        assert daemon_main.main() == 1
        err = capsys.readouterr().err
        assert "Configuration error:" in err
        assert "'auto_accept_grants'" in err
        # And kept for a caller with no stderr: windows_service puts it in the Event Log.
        assert "'auto_accept_grants'" in daemon_main.last_startup_error()

    def test_main_keeps_a_runtime_identity_refusal_for_the_event_log(self, monkeypatch, capsys):
        def refuse():
            raise daemon_main.privilege_separation.PrivilegeSeparationError("wrong account")

        monkeypatch.setattr(daemon_main.privilege_separation, "check_runtime_identity", refuse)
        assert daemon_main.main([]) == 1
        assert daemon_main.last_startup_error() == "Configuration error: wrong account"
        assert "wrong account" in capsys.readouterr().err

    def test_v1_sections_an_earlier_release_converted_are_removed(self, tmp_path):
        # 4.1-4.4 converted auto_accept_rules into auto_accept: on startup, set the marker, and
        # left the v1 section on disk. Refusing that file refused every upgrade from those
        # releases, silently, on Windows (ADR 0047).
        current = policy_store.rules_to_config(
            policy_store.compile_rules_from_config(daemon_main.load_config(str(tmp_path / "seed.yaml")))
        )
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.safe_dump({
            "auto_accept": current,
            "auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]},
            "migrated_to_policy_v2": True,
            "logging": {"level": "INFO"},
        }))

        config = daemon_main.load_config(str(config_path))

        expected = {"auto_accept": current, "logging": {"level": "INFO"}}
        assert config == expected
        assert yaml.safe_load(config_path.read_text()) == expected
        assert len(daemon_main._deferred_warnings) == 1
        assert "auto_accept_rules" in daemon_main._deferred_warnings[0]

    def test_a_failed_rewrite_still_starts_with_the_sections_ignored(self, tmp_path, monkeypatch):
        config_path = tmp_path / "settings.yaml"
        original = yaml.safe_dump({"auto_accept_grants": {}, "migrated_to_policy_v2": True})
        config_path.write_text(original)

        def fail(*_args, **_kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(daemon_main, "atomic_write_text", fail)

        assert daemon_main.load_config(str(config_path)) == {}
        assert config_path.read_text() == original
        assert "could not rewrite" in daemon_main._deferred_warnings[0]

    def test_deferred_warnings_reach_the_log_once_logging_is_up(self, caplog):
        daemon_main._deferred_warnings.append("left over")
        with caplog.at_level(logging.WARNING, logger=daemon_main.logger.name):
            daemon_main._flush_deferred_warnings()
        assert "left over" in caplog.text
        assert daemon_main._deferred_warnings == []


class TestLoadOrgConfig:
    """Three states, not two -- absent is the only case that's
    still allowed to silently resolve to {} (local mode); anything present
    but broken must raise org_mode.ConfigurationError instead of quietly
    collapsing into the same {} local-mode result absence gets."""

    def test_returns_empty_dict_when_no_file_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        assert daemon_main.load_org_config() == {}

    def test_an_unsigned_bundle_without_mode_is_refused_under_the_org_mode_variable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setenv("PRIVACYFENCE_MODE", "org")
        (tmp_path / "org_bundle.json").write_text(json.dumps({"slack": {"client_id": "abc"}}))
        with pytest.raises(org_mode.ConfigurationError, match="not signed"):
            daemon_main.load_org_config()

    def test_returns_parsed_dict_when_valid_with_no_mode_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text(json.dumps({"slack": {"client_id": "abc"}}))
        assert daemon_main.load_org_config() == {"slack": {"client_id": "abc"}}

    def test_explicit_org_mode_unsigned_raises_configuration_error(self, tmp_path, monkeypatch):
        """Org mode requires a signed bundle (ADR 0016) -- see
        TestLoadOrgConfigSigning below for the signed-bundle path this
        gates behind."""
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text(json.dumps({"mode": "org"}))
        with pytest.raises(org_mode.ConfigurationError, match="requires a signed bundle"):
            daemon_main.load_org_config()

    def test_raises_configuration_error_on_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text("{not valid json")
        with pytest.raises(org_mode.ConfigurationError):
            daemon_main.load_org_config()

    def test_raises_configuration_error_when_top_level_not_an_object(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text(json.dumps(["not", "an", "object"]))
        with pytest.raises(org_mode.ConfigurationError):
            daemon_main.load_org_config()

    def test_raises_configuration_error_when_unreadable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        path = tmp_path / "org_bundle.json"
        path.write_text(json.dumps({"mode": "org"}))

        real_open = open

        def _denying_open(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError(13, "Permission denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _denying_open)
        with pytest.raises(org_mode.ConfigurationError):
            daemon_main.load_org_config()

    def test_a_broken_org_config_does_not_silently_fall_back_to_local_mode(self, tmp_path, monkeypatch):
        """A corrupted org_bundle.json (e.g. tampering, a botched deploy,
        disk corruption) must not be indistinguishable from no org config
        at all, or an org-mode install would silently start with no
        IdP-backed auth wired up rather than refusing to start."""
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text('{"mode": "org", corrupted')
        with pytest.raises(org_mode.ConfigurationError):
            org_config = daemon_main.load_org_config()
            org_mode.resolve_mode(org_config)


def _write_signed_bundle(tmp_path, bundle, private_key=None):
    """Signs ``bundle`` (with a fresh keypair, unless one is given) the
    same way scripts/build_org_bundle.py --sign-key does, and writes it
    to tmp_path/org_bundle.json. Returns the private key used, so a test
    can sign a second, deliberately different bundle with the same key."""
    from privacyfence import org_bundle_signing

    if private_key is None:
        private_key, _ = org_bundle_signing.generate_keypair()
    signed = org_bundle_signing.sign_bundle(bundle, private_key)
    (tmp_path / "org_bundle.json").write_text(json.dumps(signed))
    return private_key


class TestLoadOrgConfigSigning:
    """ADR 0016's signing layer: org_bundle_signing.verify_and_maybe_pin() is
    wired into every load_org_config() call -- trust-on-first-use of the
    first signed bundle an install ever sees, then mandatory verification
    against that pinned key for everything after."""

    def test_unsigned_bundle_in_local_mode_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text(json.dumps({"slack": {"client_id": "abc"}}))
        assert daemon_main.load_org_config() == {"slack": {"client_id": "abc"}}

    def test_first_signed_bundle_is_accepted_and_pins_its_key(self, tmp_path, monkeypatch):
        from privacyfence import org_bundle_signing

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}})

        loaded = daemon_main.load_org_config()

        assert loaded["mode"] == "org"
        assert org_bundle_signing.pinned_public_key_path(tmp_path).exists()

    def test_second_load_verifies_against_the_pinned_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}})
        daemon_main.load_org_config()  # pins

        loaded_again = daemon_main.load_org_config()

        assert loaded_again["mode"] == "org"

    def test_tampering_after_pin_raises_configuration_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}, "org_name": "Acme"})
        daemon_main.load_org_config()  # pins

        data = json.loads((tmp_path / "org_bundle.json").read_text())
        data["org_name"] = "Evil Corp"
        (tmp_path / "org_bundle.json").write_text(json.dumps(data))

        with pytest.raises(org_mode.ConfigurationError, match="signing-key verification"):
            daemon_main.load_org_config()

    def test_replacing_with_a_different_key_after_pin_raises_configuration_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}})
        daemon_main.load_org_config()  # pins the first key

        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}})  # signed with a NEW key

        with pytest.raises(org_mode.ConfigurationError, match="signing-key verification"):
            daemon_main.load_org_config()

    def test_downgrade_to_unsigned_after_pin_raises_configuration_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"mode": "org", "server": {}, "idp": {}})
        daemon_main.load_org_config()  # pins

        (tmp_path / "org_bundle.json").write_text(json.dumps({"mode": "org", "server": {}, "idp": {}}))

        with pytest.raises(org_mode.ConfigurationError, match="signing-key verification"):
            daemon_main.load_org_config()

    def test_org_mode_still_rejected_when_unsigned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text(json.dumps({"mode": "org"}))
        with pytest.raises(org_mode.ConfigurationError, match="requires a signed bundle"):
            daemon_main.load_org_config()

    def test_local_mode_signed_bundle_is_accepted(self, tmp_path, monkeypatch):
        """Signing is opt-in, not org-mode-exclusive -- an install can
        sign its bundle without turning org mode on at all."""
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}})

        loaded = daemon_main.load_org_config()

        assert loaded["slack"] == {"client_id": "abc"}


class TestLogOrgConfigBundleHash:
    """ADR 0016's hash-log layer: a sha256 of the installed bundle logged (and
    audited) once per daemon startup, independent of whether it's signed
    -- see log_org_config_bundle_hash's own docstring for why this isn't
    folded into load_org_config() itself."""

    def test_no_file_logs_absence_and_writes_no_audit_entry(self, tmp_path, monkeypatch, caplog):
        from privacyfence.audit_log import init_audit_logger

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        init_audit_logger(str(tmp_path / "audit"))
        with caplog.at_level(logging.INFO):
            daemon_main.log_org_config_bundle_hash({})
        assert "absent" in caplog.text
        assert not list((tmp_path / "audit").glob("*.jsonl"))

    def test_installed_bundle_logs_hash_and_writes_audit_entry(self, tmp_path, monkeypatch, caplog):
        from privacyfence.audit_log import init_audit_logger
        from privacyfence.org_bundle_signing import sha256_hex

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        raw = json.dumps({"slack": {"client_id": "abc"}}).encode("utf-8")
        (tmp_path / "org_bundle.json").write_bytes(raw)
        init_audit_logger(str(tmp_path / "audit"))

        with caplog.at_level(logging.INFO):
            daemon_main.log_org_config_bundle_hash({"slack": {"client_id": "abc"}})

        expected_hash = sha256_hex(raw)
        assert expected_hash in caplog.text

        jsonl_files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(jsonl_files) == 1
        entries = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["decision"] == "org_config_startup"
        assert expected_hash in entries[0]["summary"]

    def test_signed_bundle_is_recorded_as_signed(self, tmp_path, monkeypatch):
        from privacyfence import org_bundle_signing
        from privacyfence.audit_log import init_audit_logger

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        private_key, _ = org_bundle_signing.generate_keypair()
        signed = org_bundle_signing.sign_bundle({"slack": {"client_id": "abc"}}, private_key)
        (tmp_path / "org_bundle.json").write_text(json.dumps(signed))
        init_audit_logger(str(tmp_path / "audit"))

        daemon_main.log_org_config_bundle_hash(signed)

        jsonl_files = list((tmp_path / "audit").glob("*.jsonl"))
        entries = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        assert "signed=True" in entries[0]["summary"]


class TestAuditStepUpRequirementChange:
    """The audit half of webauthn_stepup.observe_step_up_
    requirement -- see that function's own tests in
    tests/unit/test_webauthn_stepup.py for the state-transition logic
    itself; this only proves daemon_main.py turns a reported change into
    the right audit entry."""

    def test_enabling_writes_the_enabled_decision(self, tmp_path):
        from privacyfence.audit_log import init_audit_logger
        from privacyfence.webauthn_stepup import StepUpRequirementChange

        init_audit_logger(str(tmp_path / "audit"))
        daemon_main._audit_step_up_requirement_change(
            StepUpRequirementChange(was_required=False, is_required=True),
        )
        jsonl_files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(jsonl_files) == 1
        entries = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["decision"] == "step_up_requirement_enabled"

    def test_disabling_writes_the_disabled_decision(self, tmp_path):
        from privacyfence.audit_log import init_audit_logger
        from privacyfence.webauthn_stepup import StepUpRequirementChange

        init_audit_logger(str(tmp_path / "audit"))
        daemon_main._audit_step_up_requirement_change(
            StepUpRequirementChange(was_required=True, is_required=False),
        )
        jsonl_files = list((tmp_path / "audit").glob("*.jsonl"))
        entries = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        assert entries[0]["decision"] == "step_up_requirement_disabled"


class TestGetOrCreateDeploymentId:
    """A stable, opaque per-install id persisted once at
    data_dir()/deployment_id and reused across restarts."""

    def test_no_existing_file_creates_and_returns_a_new_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        deployment_id = daemon_main.get_or_create_deployment_id()
        assert deployment_id
        assert (tmp_path / "deployment_id").read_text(encoding="utf-8") == deployment_id

    def test_existing_file_is_reused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "deployment_id").write_text("existing-id-123", encoding="utf-8")
        assert daemon_main.get_or_create_deployment_id() == "existing-id-123"

    def test_empty_existing_file_generates_a_fresh_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "deployment_id").write_text("", encoding="utf-8")
        deployment_id = daemon_main.get_or_create_deployment_id()
        assert deployment_id != ""

    def test_repeated_calls_return_the_same_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        first = daemon_main.get_or_create_deployment_id()
        second = daemon_main.get_or_create_deployment_id()
        assert first == second

    def test_unreadable_existing_file_falls_back_to_a_new_id(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        deployment_path = tmp_path / "deployment_id"
        deployment_path.write_text("some-id", encoding="utf-8")

        from pathlib import Path
        original_read_text = Path.read_text

        def failing_read_text(self, *args, **kwargs):
            if self == deployment_path:
                raise OSError("permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing_read_text)
        with caplog.at_level(logging.WARNING):
            deployment_id = daemon_main.get_or_create_deployment_id()
        assert "Could not read deployment id" in caplog.text
        assert deployment_id

    def test_persist_failure_still_returns_the_new_id(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with caplog.at_level(logging.WARNING):
            deployment_id = daemon_main.get_or_create_deployment_id()
        assert "Could not persist deployment id" in caplog.text
        assert deployment_id


class TestCheckStoragePermissions:
    """The storage-permissions startup check: local mode warns and keeps
    starting, org mode refuses to start -- same "detectable vs.
    preventable" split the org bundle's startup hash log draws (ADR 0016)
    for a similarly upgrade-sensitive problem."""

    def _patch_dirs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "user_dir", lambda: tmp_path)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="secure_files.audit_directory_permissions() flags every directory as insecure here because chmod does not restrict access on Windows -- same known, accepted permission-bits gap as test_secure_files.py, just surfacing through the org-mode startup check instead of a direct stat() assertion",
    )
    def test_no_warning_when_directory_is_already_0700(self, tmp_path, monkeypatch, caplog):
        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o700)

        with caplog.at_level(logging.WARNING):
            daemon_main.check_storage_permissions(org_mode_active=False)

        assert "Insecure storage permissions" not in caplog.text

    def test_local_mode_logs_warning_but_does_not_raise(self, tmp_path, monkeypatch, caplog):
        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o755)

        with caplog.at_level(logging.WARNING):
            daemon_main.check_storage_permissions(org_mode_active=False)  # must not raise

        assert "Insecure storage permissions" in caplog.text
        assert str(tmp_path) in caplog.text

    def test_org_mode_raises_insecure_permissions_error(self, tmp_path, monkeypatch):
        from privacyfence.secure_files import InsecurePermissionsError

        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o755)

        with pytest.raises(InsecurePermissionsError):
            daemon_main.check_storage_permissions(org_mode_active=True)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="secure_files.audit_directory_permissions() flags every directory as insecure here because chmod does not restrict access on Windows -- same known, accepted permission-bits gap as test_secure_files.py, just surfacing through the org-mode startup check instead of a direct stat() assertion",
    )
    def test_org_mode_with_correct_permissions_does_not_raise(self, tmp_path, monkeypatch):
        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o700)

        daemon_main.check_storage_permissions(org_mode_active=True)  # must not raise

    def test_dedupes_the_same_directory_named_more_than_once(self, tmp_path, monkeypatch, caplog):
        """user_dir() with no principal in scope resolves to data_dir()
        itself -- the same real directory named twice must only be warned
        about once."""
        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o755)

        with caplog.at_level(logging.WARNING):
            daemon_main.check_storage_permissions(org_mode_active=False)

        assert caplog.text.count("Insecure storage permissions") == 1

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX permission bits only -- Windows' separated layout is NTFS ACLs, audited by windows_acl.py instead",
    )
    def test_separated_layout_is_not_audited_against_the_flat_0700_rule(self, tmp_path, monkeypatch, caplog):
        # Privilege separation makes two of these directories deliberately looser than
        # 0700 -- the system root 0711 so the logged-in user can traverse to
        # the handoff directory, and the handoff directory 3770 so two
        # accounts can hand each other a socket. Reporting the design as a
        # defect on every startup would be noise; in org mode it would refuse
        # to start over it.
        handoff = tmp_path / "handoff"
        handoff.mkdir()
        tmp_path.chmod(0o711)
        handoff.chmod(0o3770)
        self._patch_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "handoff_dir", lambda: handoff)
        monkeypatch.setattr(daemon_main.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(daemon_main.privilege_separation, "audit_layout", list)

        with caplog.at_level(logging.WARNING):
            daemon_main.check_storage_permissions(org_mode_active=True)  # must not raise

        assert "Insecure storage permissions" not in caplog.text

    def test_separated_layout_problems_are_reported_by_the_startup_check(self, tmp_path, monkeypatch, caplog):
        # The separated layout gets its own audit instead -- including the one
        # a mode check can't see: authority/ still owned by the human.
        self._patch_dirs(monkeypatch, tmp_path)
        tmp_path.chmod(0o700)
        monkeypatch.setattr(daemon_main, "handoff_dir", lambda: tmp_path / "handoff")
        monkeypatch.setattr(daemon_main.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(
            daemon_main.privilege_separation, "audit_layout", lambda: ["authority is owned by 'alice'"],
        )

        with caplog.at_level(logging.WARNING):
            daemon_main.check_storage_permissions(org_mode_active=False)

        assert "Insecure storage permissions" in caplog.text
        assert "owned by 'alice'" in caplog.text


# ---------------------------------------------------------------------------- #
# build_connectors: the Google-backed connectors (gmail, drive, calendar,
# contacts, tasks, apps_script) all follow the same "needs installed google
# org config, then check_connection()" shape.
# ---------------------------------------------------------------------------- #

GOOGLE_CONNECTORS = [
    pytest.param("gmail", "GmailClient", "GmailClientError", "GmailConnector", id="gmail"),
    pytest.param("drive", "DriveClient", "DriveClientError", "DriveConnector", id="drive"),
    pytest.param("calendar", "CalendarClient", "CalendarClientError", "CalendarConnector", id="calendar"),
    pytest.param("contacts", "ContactsClient", "ContactsClientError", "ContactsConnector", id="contacts"),
    pytest.param("tasks", "TasksClient", "TasksClientError", "TasksConnector", id="tasks"),
    pytest.param("apps_script", "AppsScriptClient", "AppsScriptClientError", "AppsScriptConnector", id="apps_script"),
]

GOOGLE_ORG_CONFIG = {"google": {"client_id": "id", "client_secret": "secret"}}


class TestBuildConnectorsGoogleFamily:
    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_built_when_configured_and_reachable(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors, _failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert len(connectors) == 1
        assert connectors[0].name == name
        if name != "tasks":
            assert connectors[0].my_email == "user@example.com"
        assert fake.captured_kwargs["client_config"] == {"installed": GOOGLE_ORG_CONFIG["google"]}

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_google_org_config_absent(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors, _failures = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_disabled_via_config(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)
        config = {"connectors": {name: {"enabled": False}}}

        connectors, _failures = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors == []
        assert fake.instantiated is False

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_check_connection_raises(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        error_cls = getattr(daemon_main, error_attr)
        fake = fake_client_class(connection_error=error_cls("token expired"))
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors, _failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors == []

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_construction_raises_file_not_found(
        self, monkeypatch, name, client_attr, error_attr, connector_attr
    ):
        fake = fake_client_class(init_error=FileNotFoundError("no token file"))
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors, _failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors == []

    def test_only_this_connector_is_skipped_when_others_succeed(self, monkeypatch):
        # Gmail fails, Drive (also Google-backed) still succeeds independently.
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(
            connection_error=daemon_main.GmailClientError("boom")
        ))
        monkeypatch.setattr(daemon_main, "DriveClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "ContactsClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "TasksClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "AppsScriptClient", fake_client_class(result="user@example.com"))

        connectors, _failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        names = {c.name for c in connectors}
        assert names == {"drive", "calendar", "contacts", "tasks", "apps_script"}


class TestBuildConnectorsCalendarFreeBusySetting:
    """settings.yaml's calendar.free_busy_full_event_details is plumbed onto
    the built CalendarConnector -- see calendar.py's _get_free_busy /
    _downgrade_to_busy_only."""

    def test_defaults_to_true_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))

        connectors, _failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is True

    def test_reads_explicit_false_from_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        config = {"calendar": {"free_busy_full_event_details": False}}

        connectors, _failures = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is False

    def test_reads_explicit_true_from_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        config = {"calendar": {"free_busy_full_event_details": True}}

        connectors, _failures = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is True


class TestBuildConnectorsGmailSignatureSetting:
    """settings.yaml's gmail.append_signature_to_drafts is plumbed onto the
    built GmailConnector as the draft tools' include_signature default."""

    @pytest.mark.parametrize("config,expected", [
        ({}, False),
        ({"gmail": None}, False),
        ({"gmail": {"append_signature_to_drafts": True}}, True),
        ({"gmail": {"append_signature_to_drafts": False}}, False),
    ])
    def test_read_from_config(self, monkeypatch, config, expected):
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(result="user@example.com"))

        connectors, _failures = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors[0].append_signature is expected


# ---------------------------------------------------------------------------- #
# build_connectors: Slack
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsSlack:
    def _org_config(self):
        return {"slack": {"client_id": "abc"}}

    def test_built_when_configured_and_reachable(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1", "email": "me@x.com"})
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert len(connectors) == 1
        assert connectors[0].name == "slack"
        assert connectors[0].my_email == "me@x.com"
        assert fake.captured_kwargs == {
            "user_token": "xoxp-1",
            "user_cache_file": str(data_dir() / "slack_user_cache.json"),
            "channel_cache_file": str(data_dir() / "slack_channel_cache.json"),
        }
        # Directory-cache warming does not happen inline in
        # build_connectors() -- it's kicked off separately, in the
        # background, by _warm_connector_caches() (see run_app()), so a
        # large workspace's re-sync can't delay startup.
        assert fake.directories_refreshed is False

    def test_skipped_when_org_config_absent(self, monkeypatch):
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_token_missing(self, monkeypatch):
        def raise_missing(path):
            raise daemon_main.SlackClientError("no token")
        monkeypatch.setattr(daemon_main, "load_slack_token", raise_missing)
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_check_connection_raises(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        fake = fake_client_class(connection_error=daemon_main.SlackClientError("revoked"))
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []

    def test_skipped_when_disabled_via_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors, _failures = daemon_main.build_connectors({"connectors": {"slack": {"enabled": False}}}, self._org_config())

        assert connectors == []
        assert fake.instantiated is False


# ---------------------------------------------------------------------------- #
# build_connectors: Salesforce
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsSalesforce:
    def _org_config(self):
        return {"salesforce": {"consumer_key": "ck", "login_url": "https://login.salesforce.com"}}

    def test_built_when_configured_and_reachable_merges_org_and_token(self, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "load_salesforce_token",
            lambda path: {"access_token": "sf-tok", "instance_url": "https://my.salesforce.com"},
        )
        fake = fake_client_class(result="https://my.salesforce.com")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert len(connectors) == 1
        assert connectors[0].name == "salesforce"
        assert connectors[0].download_mode == "local"
        # config= must carry both the org registration and the per-user token.
        assert fake.captured_kwargs["config"] == {
            "consumer_key": "ck",
            "login_url": "https://login.salesforce.com",
            "access_token": "sf-tok",
            "instance_url": "https://my.salesforce.com",
        }

    def test_skipped_when_org_config_absent(self, monkeypatch):
        fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_token_missing(self, monkeypatch):
        def raise_missing(path):
            raise daemon_main.SalesforceClientError("no token")
        monkeypatch.setattr(daemon_main, "load_salesforce_token", raise_missing)
        fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []

    def test_skipped_when_check_connection_raises(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_salesforce_token", lambda path: {"access_token": "t"})
        fake = fake_client_class(connection_error=daemon_main.SalesforceClientError("expired"))
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []

    def _built_client(self, monkeypatch, config):
        monkeypatch.setattr(daemon_main, "load_salesforce_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake_client_class(result="ok"))
        connectors, _failures = daemon_main.build_connectors(config, self._org_config())
        return connectors[0]._sf

    def test_report_max_pages_defaults_to_50(self, monkeypatch):
        assert self._built_client(monkeypatch, {}).report_max_pages == 50

    def test_report_max_pages_from_config(self, monkeypatch):
        client = self._built_client(monkeypatch, {"salesforce": {"report_max_pages": 7}})
        assert client.report_max_pages == 7

    @pytest.mark.parametrize("bad", [0, -1, "x", True])
    def test_bad_report_max_pages_falls_back_with_a_warning(self, monkeypatch, caplog, bad):
        with caplog.at_level(logging.WARNING, logger=daemon_main.logger.name):
            client = self._built_client(monkeypatch, {"salesforce": {"report_max_pages": bad}})
        assert client.report_max_pages == 50
        assert "salesforce.report_max_pages must be a whole number of at least 1; using 50" in caplog.text


# ---------------------------------------------------------------------------- #
# build_connectors: Jira / Confluence share one Atlassian OAuth grant
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsAtlassian:
    def _org_config(self):
        return {"atlassian": {"client_id": "ac", "client_secret": "as"}}

    def _patch_token(self, monkeypatch, token=None, error=None):
        def loader(path):
            if error is not None:
                raise error
            return token
        monkeypatch.setattr(daemon_main, "load_atlassian_token", loader)

    def test_both_built_when_configured_and_authenticated(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        confluence_fake = fake_client_class(result="https://x.atlassian.net/wiki")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        names = {c.name for c in connectors}
        assert names == {"jira", "confluence"}
        for c in connectors:
            assert c.my_email == "me@x.com"

    def test_both_clients_share_one_user_directory(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "cloud_id": "cloud-1", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        confluence_fake = fake_client_class(result="https://x.atlassian.net/wiki")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        daemon_main.build_connectors({}, self._org_config())

        directory = jira_fake.captured_kwargs["user_directory"]
        assert directory is confluence_fake.captured_kwargs["user_directory"]
        assert directory._cache_file == str(data_dir() / "atlassian_user_cache.json")
        assert directory._background_reports is True
        assert directory._cloud_id == "cloud-1"

    def test_config_passed_to_clients_merges_org_registration_and_token(self, monkeypatch):
        # Regression coverage for the reauth-on-restart fix: JiraClient/
        # ConfluenceClient need client_id/client_secret (from org config) *and*
        # the per-user access/refresh token merged into one dict so they can
        # refresh an expired token instead of forcing re-authentication.
        self._patch_token(monkeypatch, token={"access_token": "at", "refresh_token": "rt", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", fake_client_class(result="url"))

        daemon_main.build_connectors({}, self._org_config())

        assert jira_fake.captured_kwargs["config"] == {
            "client_id": "ac", "client_secret": "as",
            "access_token": "at", "refresh_token": "rt", "account_email": "me@x.com",
        }

    def test_both_skipped_when_atlassian_org_config_absent(self, monkeypatch):
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors, _failures = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert jira_fake.instantiated is False
        assert confluence_fake.instantiated is False

    def test_both_skipped_when_not_authenticated(self, monkeypatch):
        self._patch_token(monkeypatch, error=daemon_main.AtlassianOAuthError("no token file"))
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []
        assert jira_fake.instantiated is False
        assert confluence_fake.instantiated is False

    def test_jira_and_confluence_connectors_get_the_clients_account_id(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        jira_fake.account_id = "acc-me"
        confluence_fake = fake_client_class(result="https://x.atlassian.net/wiki")
        confluence_fake.account_id = "acc-me"
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert {c.name: c.my_account_id for c in connectors} == {"jira": "acc-me", "confluence": "acc-me"}
        # The id Jira learned reaches the clients built after it.
        assert confluence_fake.captured_kwargs["config"]["account_id"] == "acc-me"

    def test_jira_disabled_does_not_affect_confluence(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)
        config = {"connectors": {"jira": {"enabled": False}}}

        connectors, _failures = daemon_main.build_connectors(config, self._org_config())

        assert [c.name for c in connectors] == ["confluence"]
        assert jira_fake.instantiated is False

    def test_jira_skipped_when_check_connection_raises_confluence_unaffected(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(connection_error=daemon_main.JiraClientError("401"))
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors, _failures = daemon_main.build_connectors({}, self._org_config())

        assert [c.name for c in connectors] == ["confluence"]


class TestBuildConnectorsAssets:
    """Jira Assets is off by default and guarded by atlassian_oauth.assets_enabled()."""

    _TOKEN = {"access_token": "at", "cloud_id": "c1", "account_email": "me@x.com",
              "scopes": ["read:cmdb-object:jira"]}

    def _setup(self, monkeypatch, token=None):
        monkeypatch.setattr(daemon_main, "load_atlassian_token", lambda path: token or self._TOKEN)
        monkeypatch.setattr(daemon_main, "JiraClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "ConfluenceClient", fake_client_class(result="ok"))
        fake = fake_client_class(result=None)
        monkeypatch.setattr(daemon_main, "AssetsClient", fake)
        return fake

    def _org(self, **extra):
        return {"atlassian": {"client_id": "ac", "client_secret": "as", **extra}}

    def test_not_built_by_default(self, monkeypatch):
        fake = self._setup(monkeypatch)

        connectors, failures = daemon_main.build_connectors({}, self._org())

        assert "assets" not in {c.name for c in connectors}
        assert fake.instantiated is False
        assert "assets" not in failures

    def test_built_when_explicitly_enabled(self, monkeypatch):
        fake = self._setup(monkeypatch)

        connectors, _ = daemon_main.build_connectors({"connectors": {"assets": {"enabled": True}}}, self._org())

        assets = next(c for c in connectors if c.name == "assets")
        assert assets.my_email == "me@x.com"
        assert fake.captured_kwargs["config"]["scopes"] == ["read:cmdb-object:jira"]

    def test_bundle_workspace_id_reaches_the_client(self, monkeypatch):
        fake = self._setup(monkeypatch)

        daemon_main.build_connectors({}, self._org(assets=True, assets_workspace_id="ws-bundle"))

        assert fake.captured_kwargs["config"]["assets_workspace_id"] == "ws-bundle"

    def test_settings_workspace_id_wins_over_the_bundle(self, monkeypatch):
        fake = self._setup(monkeypatch)
        config = {"connectors": {"assets": {"enabled": True, "workspace_id": " ws-settings "}}}

        daemon_main.build_connectors(config, self._org(assets_workspace_id="ws-bundle"))

        assert fake.captured_kwargs["config"]["assets_workspace_id"] == "ws-settings"

    def test_built_when_the_bundle_flag_is_set(self, monkeypatch):
        self._setup(monkeypatch)

        connectors, _ = daemon_main.build_connectors({}, self._org(assets=True))

        assert "assets" in {c.name for c in connectors}

    def test_explicit_false_beats_the_bundle_flag(self, monkeypatch):
        fake = self._setup(monkeypatch)
        config = {"connectors": {"assets": {"enabled": False}}}

        connectors, _ = daemon_main.build_connectors(config, self._org(assets=True))

        assert "assets" not in {c.name for c in connectors}
        assert fake.instantiated is False

    def test_token_without_the_assets_scope_is_not_authenticated(self, monkeypatch):
        self._setup(monkeypatch, token={"access_token": "at", "cloud_id": "c1", "scopes": ["read:jira-work"]})
        from privacyfence.assets_client import AssetsClient as RealAssetsClient
        monkeypatch.setattr(daemon_main, "AssetsClient", RealAssetsClient)

        connectors, failures = daemon_main.build_connectors(
            {"connectors": {"assets": {"enabled": True}}}, self._org(),
        )

        assert "assets" not in {c.name for c in connectors}
        assert failures["assets"] == "not_authenticated"

    def test_missing_org_config_is_recorded(self, monkeypatch):
        self._setup(monkeypatch)

        _, failures = daemon_main.build_connectors({"connectors": {"assets": {"enabled": True}}}, {})

        assert failures["assets"] == "no_org_config"


# ---------------------------------------------------------------------------- #
# build_connectors: Telegram
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsTelegram:
    def _make_session(self, tmp_path, monkeypatch, exists=True):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        os.makedirs(tmp_path / "credentials", exist_ok=True)
        if exists:
            (tmp_path / "credentials" / "telegram.session").write_bytes(b"")

    def test_built_when_creds_and_session_present(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, {"telegram": {"enabled": True}})

        assert len(connectors) == 1
        assert connectors[0].name == "telegram"
        assert fake.captured_kwargs == {
            "api_id": 123, "api_hash": "hash",
            "session_file": os.path.join(str(tmp_path), "credentials/telegram.session"),
            "chat_cache_file": str(data_dir() / "telegram_chat_cache.json"),
        }
        # Same as Slack (see TestBuildConnectorsSlack): directory-cache
        # warming is not inline in build_connectors() for either
        # connector -- it's kicked off separately, in the background, by
        # _warm_connector_caches() (see run_app()).
        assert fake.directories_refreshed is False

    def test_skipped_without_the_org_config_section(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors, failures = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert failures["telegram"] == "no_org_config"
        assert fake.instantiated is False

    def test_skipped_when_no_app_credentials(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, {"telegram": {"enabled": True}})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_session_file_absent(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=False)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors, _failures = daemon_main.build_connectors({}, {"telegram": {"enabled": True}})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_disabled_via_config(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors, _failures = daemon_main.build_connectors({"connectors": {"telegram": {"enabled": False}}}, {"telegram": {"enabled": True}})

        assert connectors == []
        assert fake.instantiated is False

    def test_unexpected_construction_error_is_caught_not_fatal(self, monkeypatch, tmp_path):
        # build_connectors deliberately catches bare Exception for Telegram
        # (MTProto client construction can fail in more ways than a typed
        # error) -- a bug here would crash daemon startup entirely.
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        monkeypatch.setattr(
            daemon_main, "TelegramPrivacyFenceClient",
            fake_client_class(init_error=RuntimeError("unexpected MTProto failure")),
        )

        connectors, _failures = daemon_main.build_connectors({}, {"telegram": {"enabled": True}})

        assert connectors == []


# ---------------------------------------------------------------------------- #
# build_connectors: cross-cutting
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsCrossCutting:
    def test_no_connectors_configured_returns_empty_list_not_fatal(self):
        connectors, _failures = daemon_main.build_connectors({}, {"telegram": {"enabled": True}})
        assert connectors == []

    def test_all_ten_connectors_built_together(self, monkeypatch, tmp_path):
        for attr in (
            "GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient",
            "AppsScriptClient",
        ):
            monkeypatch.setattr(daemon_main, attr, fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "SlackClient", fake_client_class(result="ws"))
        monkeypatch.setattr(daemon_main, "load_salesforce_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "load_atlassian_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "JiraClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "ConfluenceClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        os.makedirs(tmp_path / "credentials", exist_ok=True)
        (tmp_path / "credentials" / "telegram.session").write_bytes(b"")
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (1, "h"))
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake_client_class())

        org_config = {
            **GOOGLE_ORG_CONFIG,
            "slack": {"client_id": "x"},
            "salesforce": {"consumer_key": "x"},
            "atlassian": {"client_id": "x"},
            "telegram": {"enabled": True},
        }
        connectors, _failures = daemon_main.build_connectors({}, org_config)

        assert {c.name for c in connectors} == {
            "gmail", "drive", "calendar", "contacts", "tasks", "apps_script",
            "slack", "salesforce", "jira", "confluence", "telegram",
        }


# ---------------------------------------------------------------------------- #
# build_connectors: Grist
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsGrist:
    API_KEY = {"auth": "api_key", "server_url": "https://docs.getgrist.com", "api_key": "k"}
    OAUTH = {
        "auth": "oauth", "server_url": "https://docs.getgrist.com",
        "access_token": "at", "refresh_token": "rt", "expires_at": 4102444800,
    }
    OAUTH_APP = {"client_id": "cid", "client_secret": "csecret"}

    def _patch(self, monkeypatch, record, *, check_error=None):
        from privacyfence.grist_client import GristClient

        def load(path):
            if record is None:
                raise daemon_main.GristClientError(
                    "Grist is not authenticated. Use Authenticate… in PrivacyFence Settings."
                )
            return record

        def check(self):
            if check_error is not None:
                raise check_error
            return "docs.getgrist.com"

        monkeypatch.setattr(daemon_main, "load_grist_token", load)
        monkeypatch.setattr(GristClient, "check_connection", check)

    def test_api_key_file_builds_connector_with_only_the_bare_section(self, monkeypatch):
        self._patch(monkeypatch, self.API_KEY)
        connectors, failures = daemon_main.build_connectors({}, {"grist": {"enabled": True}})
        assert [c.name for c in connectors] == ["grist"]
        assert connectors[0].download_mode == "local"
        assert "grist" not in failures

    def test_oauth_file_builds_through_token_provider(self, monkeypatch):
        from privacyfence.grist_auth import GristTokenProvider

        self._patch(monkeypatch, self.OAUTH)
        connectors, failures = daemon_main.build_connectors({}, {"grist": self.OAUTH_APP})
        assert [c.name for c in connectors] == ["grist"]
        assert isinstance(connectors[0]._client._credential, GristTokenProvider)
        assert "grist" not in failures

    def test_org_mode_without_grist_section_is_no_org_config(self, monkeypatch):
        self._patch(monkeypatch, self.API_KEY)
        org = {"mode": "org", "server": {"issuer_url": "https://pf.example.com"}}
        _connectors, failures = daemon_main.build_connectors({}, org)
        assert failures["grist"] == "no_org_config"

    def test_no_file_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, None)
        connectors, failures = daemon_main.build_connectors({}, {"grist": {"enabled": True}})
        assert connectors == []
        assert failures["grist"] == "not_authenticated"

    def test_api_key_file_while_bundle_has_oauth_app_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, self.API_KEY)
        connectors, failures = daemon_main.build_connectors({}, {"grist": self.OAUTH_APP})
        assert connectors == []
        assert failures["grist"] == "not_authenticated"

    def test_credential_for_another_server_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, self.OAUTH)
        org = {"grist": {**self.OAUTH_APP, "server_url": "https://grist.example.com"}}
        connectors, failures = daemon_main.build_connectors({}, org)
        assert connectors == []
        assert failures["grist"] == "not_authenticated"

    def test_without_the_section_is_no_org_config_in_local_mode_too(self, monkeypatch):
        self._patch(monkeypatch, self.API_KEY)
        connectors, failures = daemon_main.build_connectors({}, {})
        assert connectors == []
        assert failures["grist"] == "no_org_config"

    def test_disabled_has_no_entry(self, monkeypatch):
        self._patch(monkeypatch, self.API_KEY)
        config = {"connectors": {"grist": {"enabled": False}}}
        connectors, failures = daemon_main.build_connectors(config, {"grist": {"enabled": True}})
        assert connectors == []
        assert "grist" not in failures


class TestBuildConnectorsPeek:
    RECORD = {"base_url": "https://peek.example.com", "token": "tok"}

    def _patch(self, monkeypatch, record, *, check_error=None):
        from privacyfence.peek_client import PeekClient, PeekClientError

        def load(path):
            if record is None:
                raise PeekClientError("Peek is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            return record

        def check(self):
            if check_error is not None:
                raise check_error
            return "peek.example.com"

        monkeypatch.setattr(daemon_main, "load_peek_token", load)
        monkeypatch.setattr(PeekClient, "check_connection", check)

    def test_token_file_builds_connector_with_only_the_bare_section(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        connectors, failures = daemon_main.build_connectors({}, {"peek": {"enabled": True}})
        assert [c.name for c in connectors] == ["peek"]
        assert connectors[0].download_mode == "local"
        assert "peek" not in failures

    def test_pinned_url_equal_to_the_file_builds(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        org = {"peek": {"base_url": "https://peek.example.com/"}}
        connectors, failures = daemon_main.build_connectors({}, org)
        assert [c.name for c in connectors] == ["peek"]
        assert "peek" not in failures

    def test_pinned_url_differing_from_the_file_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        org = {"peek": {"base_url": "https://other.example.com"}}
        connectors, failures = daemon_main.build_connectors({}, org)
        assert connectors == []
        assert failures["peek"] == "not_authenticated"

    def test_org_mode_without_peek_section_is_no_org_config(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        org = {"mode": "org", "server": {"issuer_url": "https://pf.example.com"}}
        _connectors, failures = daemon_main.build_connectors({}, org)
        assert failures["peek"] == "no_org_config"

    def test_no_file_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, None)
        connectors, failures = daemon_main.build_connectors({}, {"peek": {"enabled": True}})
        assert connectors == []
        assert failures["peek"] == "not_authenticated"

    def test_check_connection_401_is_not_authenticated(self, monkeypatch):
        from privacyfence.peek_client import PeekClientError

        err = PeekClientError(
            "Peek refused the saved sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings "
            "to connect again.",
            status=401,
        )
        self._patch(monkeypatch, self.RECORD, check_error=err)
        connectors, failures = daemon_main.build_connectors({}, {"peek": {"enabled": True}})
        assert connectors == []
        assert failures["peek"] == "not_authenticated"

    def test_without_the_section_is_no_org_config_in_local_mode_too(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        connectors, failures = daemon_main.build_connectors({}, {})
        assert connectors == []
        assert failures["peek"] == "no_org_config"

    def test_disabled_has_no_entry(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        config = {"connectors": {"peek": {"enabled": False}}}
        connectors, failures = daemon_main.build_connectors(config, {"peek": {"enabled": True}})
        assert connectors == []
        assert "peek" not in failures


# ---------------------------------------------------------------------------- #
# build_connectors: per-connector failure reasons -- the data model the
# privacyfence_status meta-tool needs to tell "never set up" /
# "auth expired" / an actual runtime error apart, instead of every un-built
# connector looking identical.
# ---------------------------------------------------------------------------- #

class TestClassifyConnectorFailure:
    def test_file_not_found_is_not_authenticated_regardless_of_message(self):
        assert daemon_main._classify_connector_failure(FileNotFoundError("nope")) == "not_authenticated"

    def test_use_authenticate_call_to_action_is_not_authenticated(self):
        exc = daemon_main.SlackClientError(
            "No Slack token found at '/x'. Use Authenticate… in the PrivacyFence Settings to sign in."
        )
        assert daemon_main._classify_connector_failure(exc) == "not_authenticated"

    def test_organization_config_not_installed_is_no_org_config(self):
        exc = daemon_main.GmailClientError("Google organization config not installed")
        assert daemon_main._classify_connector_failure(exc) == "no_org_config"

    def test_app_credentials_not_available_is_no_org_config(self):
        exc = daemon_main.TelegramClientError("Telegram app credentials not available in this build")
        assert daemon_main._classify_connector_failure(exc) == "no_org_config"

    def test_anything_else_falls_back_to_the_redacted_public_message(self):
        # GmailClientError isn't on safe_errors.PUBLIC_SAFE_EXCEPTION_TYPES
        # (it routinely wraps a third-party HTTP body), so a real
        # check_connection()-style failure redacts down to the generic
        # message rather than leaking whatever text it wrapped.
        exc = daemon_main.GmailClientError("token expired: Bearer ya29.abcdefgh12345678")
        assert daemon_main._classify_connector_failure(exc) == GENERIC_PUBLIC_MESSAGE


class TestBuildConnectorsFailureReasons:
    def test_no_failure_entry_for_a_successfully_built_connector(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(result="user@example.com"))

        connectors, failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert len(connectors) == 1
        assert "gmail" not in failures

    def test_no_failure_entry_for_a_deliberately_disabled_connector(self, monkeypatch):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, "GmailClient", fake)
        config = {"connectors": {"gmail": {"enabled": False}}}

        connectors, failures = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors == []
        assert "gmail" not in failures
        assert fake.instantiated is False

    def test_no_org_config_reason_when_google_org_config_absent(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(result="user@example.com"))

        _connectors, failures = daemon_main.build_connectors({}, {})

        assert failures["gmail"] == "no_org_config"

    def test_not_authenticated_reason_when_token_file_missing(self, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "GmailClient", fake_client_class(init_error=FileNotFoundError("no token file"))
        )

        _connectors, failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert failures["gmail"] == "not_authenticated"

    def test_not_authenticated_reason_for_slacks_real_no_token_message(self, monkeypatch):
        # Exercises slack_client.load_token_file's real message text (rather
        # than a test-only stand-in), since _classify_connector_failure's
        # "not_authenticated" bucket depends on its exact "Use Authenticate…"
        # phrasing matching across every connector's own token loader.
        monkeypatch.setattr(daemon_main, "SlackClient", fake_client_class(result="ws"))

        _connectors, failures = daemon_main.build_connectors({}, {"slack": {"client_id": "abc"}})

        assert failures["slack"] == "not_authenticated"

    def test_redacted_reason_when_check_connection_raises(self, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "GmailClient",
            fake_client_class(connection_error=daemon_main.GmailClientError("token expired")),
        )

        _connectors, failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert failures["gmail"] == GENERIC_PUBLIC_MESSAGE

    def test_only_the_failing_connector_gets_a_failure_entry(self, monkeypatch):
        # Every other Google-family client also needs faking out (not just
        # Gmail/Drive) -- GOOGLE_ORG_CONFIG makes them all attempt a real
        # connection otherwise, same as TestBuildConnectorsGoogleFamily's own
        # test_only_this_connector_is_skipped_when_others_succeed above.
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(
            connection_error=daemon_main.GmailClientError("boom")
        ))
        for attr in ("DriveClient", "CalendarClient", "ContactsClient", "TasksClient", "AppsScriptClient"):
            monkeypatch.setattr(daemon_main, attr, fake_client_class(result="user@example.com"))

        _connectors, failures = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        # Slack/Salesforce/Atlassian/Telegram also have no org config of
        # their own in GOOGLE_ORG_CONFIG, so they fail too (each with its
        # own "no_org_config") -- this test only cares that Gmail's sibling
        # Google-family connectors, which all *do* have org config here,
        # stay unaffected by Gmail's own failure.
        google_family = {"gmail", "drive", "calendar", "contacts", "tasks", "apps_script"}
        assert {name for name in failures if name in google_family} == {"gmail"}


# ---------------------------------------------------------------------------- #
# setup_logging
# ---------------------------------------------------------------------------- #

class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _restore_root_logger(self):
        # setup_logging() clears and replaces the *real* root logger's
        # handlers/level as a side effect -- restore it so this doesn't leak
        # into other tests' log capture or leave a FileHandler pointing at a
        # deleted tmp_path.
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        yield
        for h in root.handlers:
            h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    def test_creates_log_file_at_configured_path(self, tmp_path):
        log_file = tmp_path / "sub" / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"file": str(log_file)}})
        assert log_file.exists()

    def test_defaults_to_info_level(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"file": str(log_file)}})
        assert logging.getLogger().level == logging.INFO

    def test_honors_configured_level(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"level": "DEBUG", "file": str(log_file)}})
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_name_falls_back_to_info(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"level": "NOT_A_REAL_LEVEL", "file": str(log_file)}})
        assert logging.getLogger().level == logging.INFO

    def test_missing_logging_section_uses_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        daemon_main.setup_logging({})
        assert (tmp_path / "logs" / "privacyfence.log").exists()

    def test_a_secret_logged_anywhere_is_redacted_in_the_log_file(self, tmp_path):
        # The root logger's formatter is safe_errors.SecretRedactingFormatter, so
        # this holds for every logger in the process, not just routes_mcp.py's
        # own tool-call-failure log line.
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"file": str(log_file)}})
        logging.getLogger("privacyfence.some_module").warning(
            "Upstream call failed: refresh_token=abcdefgh12345678"
        )
        contents = log_file.read_text()
        assert "abcdefgh12345678" not in contents
        assert "[REDACTED]" in contents


# ---------------------------------------------------------------------------- #
# _maybe_start_web_server -- the web approval UI is unconditionally installed
# and the embedded server unconditionally started in local mode -- there is
# no native alternative to select (ADR 0001), and no config key that turns
# the server off entirely. web.
# mcp.enabled/web.settings.enabled remain independent levers for those two
# surfaces specifically; either can be on or off without affecting whether
# the server itself (and /approvals) runs.
# ---------------------------------------------------------------------------- #

class TestMaybeStartWebServer:
    def _no_bind(self, monkeypatch, tmp_path):
        # Never actually binds a real socket -- this suite proves the
        # wiring (which ApprovalUI gets installed, whether a server object
        # comes back, whether /mcp is mounted), not uvicorn's own serve
        # loop. mcp_token also has to land under an isolated tmp_path, not
        # paths.data_dir()'s real value (the repo root itself in dev mode)
        # -- see web/mcp_auth.py's load_or_create_mcp_token().
        from privacyfence import paths
        from privacyfence.web.server import WebServer
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        started = {}
        monkeypatch.setattr(WebServer, "start", lambda self: started.update(called=True))
        return started

    @staticmethod
    def _connector_host():
        from privacyfence.connector_host import ConnectorHost
        return ConnectorHost([])

    def test_bare_config_still_installs_the_web_approval_ui_and_starts_a_server(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import WebApprovalUI, get_web_approval_ui
        from privacyfence.approval_ui import get_approval_ui
        started = self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server({}, self._connector_host(), unattended_sessions_enabled=False)

        assert result is not None
        assert started.get("called") is True
        assert get_approval_ui() is get_web_approval_ui()
        assert isinstance(get_approval_ui(), WebApprovalUI)

    def test_web_mode_installs_the_web_approval_ui_and_starts_a_server(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import WebApprovalUI, get_web_approval_ui
        from privacyfence.approval_ui import get_approval_ui
        started = self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"port": 18765}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        assert result is not None
        assert result.port == 18765
        assert started.get("called") is True
        assert get_approval_ui() is get_web_approval_ui()
        assert isinstance(get_approval_ui(), WebApprovalUI)
        assert result.mcp_url is None  # web.mcp.enabled wasn't set

    def test_web_mode_defaults_to_the_standard_port(self, monkeypatch, tmp_path):
        from privacyfence.web.server import DEFAULT_PORT
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {}, self._connector_host(), unattended_sessions_enabled=False,
        )
        assert result.port == DEFAULT_PORT

    def test_mcp_enabled_starts_a_server_with_mcp_mounted(self, monkeypatch, tmp_path):
        started = self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert result is not None
        assert started.get("called") is True
        assert result.mcp_url == f"{result.base_url}/mcp"

    def test_web_mode_registry_gets_the_real_base_url_once_started(self, monkeypatch, tmp_path):
        # gate.py's pending-result URL needs the registry to know the server's real
        # base_url, not just exist -- set once the server actually starts,
        # not at construction time.
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"port": 18765}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.approval_url("abc") == f"{result.base_url}/approvals/abc"

    def test_mcp_dispatcher_shares_the_same_registry_as_the_approval_ui(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher._registry is get_web_approval_ui().deferred_registry

    def test_the_admin_api_gets_the_startup_admin_config_and_the_shared_org_config(self, monkeypatch, tmp_path):
        from privacyfence.web import server as web_server
        self._no_bind(monkeypatch, tmp_path)
        seen = {}

        def fake_build_app(web_ui, **kwargs):
            seen.update(kwargs)
            return lambda scope, receive, send: None

        monkeypatch.setattr(web_server, "build_app", fake_build_app)
        org_config = {"slack": {"client_id": "s"}}
        startup_admin = {"services": {}}

        daemon_main._maybe_start_web_server(
            {}, self._connector_host(), unattended_sessions_enabled=False,
            org_config=org_config, startup_admin=startup_admin,
        )

        assert seen["org_config"] is org_config
        assert seen["startup_admin"] is startup_admin

    def test_approvals_config_overrides_the_registrys_defaults(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        daemon_main._maybe_start_web_server(
            {
                "web": {
                    "approvals": {
                        "hold_window_seconds": 5, "pending_ttl_seconds": 60,
                        "ledger_ttl_seconds": 30, "max_pending": 3,
                        "max_pending_per_principal": 2,
                    },
                },
            },
            self._connector_host(), unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.hold_window == 5
        assert registry.pending_ttl == 60
        assert registry.ledger_ttl == 30
        assert registry.max_pending == 3
        assert registry.max_pending_per_principal == 2

    def test_max_pending_per_principal_defaults_when_not_configured(self, monkeypatch, tmp_path):
        # An install that never sets this key still gets the lower per-
        # principal cap, not an unbounded one.
        from privacyfence.approvals import DEFAULT_MAX_PENDING_PER_PRINCIPAL
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.max_pending_per_principal == DEFAULT_MAX_PENDING_PER_PRINCIPAL

    def test_adaptive_hold_defaults_on_when_not_configured(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.adaptive_hold is True

    def test_adaptive_hold_can_be_configured_off(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        daemon_main._maybe_start_web_server(
            {"web": {"approvals": {"adaptive_hold": False}}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.adaptive_hold is False

    def test_require_passkey_with_nothing_enrolled_logs_a_warning_but_still_starts(
        self, monkeypatch, tmp_path, caplog,
    ):
        # With a passkey required and none enrolled, the daemon starts,
        # releases nothing, and shows a loud persistent banner rather than
        # refusing to boot. This is the "loud" half aimed at the daemon's own
        # log; web_shell.py's TestBanner/
        # test_routes_approvals.py's TestRequirePasskeyBanner cover the
        # human-facing half.
        #
        # require_passkey also needs a separated install or the ADR 0003
        # developer override (step_up_config.py's own gate) -- this test is
        # about the enrollment banner, not that gate, so set the override.
        monkeypatch.setenv(daemon_main.privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        self._no_bind(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING):
            result = daemon_main._maybe_start_web_server(
                {"step_up": {"enabled": True, "require_passkey": True}},
                self._connector_host(), unattended_sessions_enabled=False,
            )
        assert result is not None  # never refuses to boot over this
        assert "require_passkey" in caplog.text
        assert "/security" in caplog.text

    def test_require_passkey_with_a_credential_enrolled_logs_nothing(self, monkeypatch, tmp_path, caplog):
        from privacyfence import webauthn_stepup as wa
        from privacyfence.principal import LOCAL_PRINCIPAL
        monkeypatch.setenv(daemon_main.privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        self._no_bind(monkeypatch, tmp_path)
        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        with caplog.at_level(logging.WARNING):
            daemon_main._maybe_start_web_server(
                {"step_up": {"enabled": True, "require_passkey": True}},
                self._connector_host(), unattended_sessions_enabled=False,
            )
        assert "require_passkey" not in caplog.text

    def test_require_passkey_off_logs_nothing(self, monkeypatch, tmp_path, caplog):
        self._no_bind(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING):
            daemon_main._maybe_start_web_server(
                {"step_up": {"enabled": True, "require_passkey": False}},
                self._connector_host(), unattended_sessions_enabled=False,
            )
        assert "require_passkey" not in caplog.text

    def test_mcp_dispatcher_sees_the_connector_hosts_live_connector_set(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        connector_host = self._connector_host()

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, connector_host, unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher.connectors == {}
        from privacyfence.connectors.gmail import GmailConnector  # any real Connector subclass
        fake_connector = object.__new__(GmailConnector)
        connector_host.set_connectors([fake_connector])
        # No second push into the dispatcher -- it polls connector_host.connectors.
        assert list(result.mcp_dispatcher.connectors) == [fake_connector.name]

    def test_mcp_dispatcher_gives_a_different_principal_their_own_fresh_connector_set(
        self, monkeypatch, tmp_path,
    ):
        # ADR 0008: a non-owner principal (an os-<uid>/os-<sid> OS user)
        # never sees the owner's connector_host at all -- ConnectorRegistry
        # builds them their own, empty set instead ("a new user starts with
        # the packaged default policy and no connectors").
        from privacyfence.principal import Principal, principal_scope

        self._no_bind(monkeypatch, tmp_path)
        connector_host = self._connector_host()
        from privacyfence.connectors.gmail import GmailConnector

        fake_connector = object.__new__(GmailConnector)
        connector_host.set_connectors([fake_connector])

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, connector_host, unattended_sessions_enabled=False,
        )

        with principal_scope(Principal(id="os-1002")):
            other_connectors = result.mcp_dispatcher.connectors
        assert other_connectors == {}
        # The owner's own connectors are unaffected by resolving another
        # principal in between.
        assert list(result.mcp_dispatcher.connectors) == [fake_connector.name]

    def test_the_dispatcher_has_no_way_to_mint_a_sign_in_link(self, monkeypatch, tmp_path):
        """No MCP tool may mint a sign-in credential (ADR 0013), so this
        call site hands the /mcp dispatcher no ``set_bootstrap_link_provider``
        seam, no way to mint a live session. Asserted here, at the call site
        that would wire it, so reintroducing it by accident fails rather than
        passes quietly."""
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert not hasattr(result.mcp_dispatcher, "get_sign_in_link")
        assert not hasattr(result.mcp_dispatcher, "set_bootstrap_link_provider")

    def test_mcp_dispatcher_defaults_to_local_mode(self, monkeypatch, tmp_path):
        # privacyfence_status's own mode field -- every call site in this
        # class passes no org_config, so this must be "local", the default.
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher.status("checking")["mode"] == "local"

    def test_no_mcp_dispatcher_means_nothing_to_wire(self, monkeypatch, tmp_path):
        # web.mcp.enabled defaults False -- must not raise reaching for
        # mcp_dispatcher.set_bootstrap_link_provider on a None dispatcher.
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server({}, self._connector_host(), unattended_sessions_enabled=False)

        assert result.mcp_dispatcher is None

    # ------------------------------------------------------------------ #
    # web.settings.enabled -- the settings UI's own off switch, independent
    # of the approval surface (which is always on).
    # ------------------------------------------------------------------ #

    def _controller(self, tmp_path, monkeypatch):
        from privacyfence import resource_names, settings_controller as sc, update_checker

        monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "rn.json")
        monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "uc.json")
        monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
        org_dir_path = tmp_path / "org"
        org_dir_path.mkdir()
        monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
        settings_dir = tmp_path / "settings_data"
        settings_dir.mkdir()
        monkeypatch.setattr(sc, "data_dir", lambda: settings_dir)
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept: {}\nconnectors: {}\n", encoding="utf-8")
        connector_host = self._connector_host()
        return sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)

    def test_settings_not_enabled_still_starts_the_server_but_leaves_it_unwired(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {}, self._connector_host(), unattended_sessions_enabled=False, controller=controller,
        )

        assert result is not None
        assert result.controller is None

    def test_settings_enabled_without_a_controller_leaves_it_unwired(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert result is not None
        assert result.controller is None

    def test_settings_enabled_wires_the_controller_into_the_server(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}, "port": 18765}}, self._connector_host(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert result is not None
        assert result.controller is controller

    def test_wires_a_live_step_up_config_into_the_controller(self, monkeypatch, tmp_path):
        # SettingsController.enable_step_up is a no-op without this --
        # _maybe_start_web_server must hand the controller the same
        # LiveStepUpConfig it hands the server itself (see that method's
        # own docstring on why it needs to be the *same* object).
        from privacyfence.step_up_config import LiveStepUpConfig
        monkeypatch.setenv(daemon_main.privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}}, "step_up": {"require_passkey": True}},
            self._connector_host(), unattended_sessions_enabled=False, controller=controller,
        )

        assert result is not None
        assert isinstance(controller._step_up, LiveStepUpConfig)
        # The config this daemon actually booted with, not a fresh default.
        assert controller._step_up.require_passkey is True

    def test_settings_not_enabled_still_wires_step_up_into_the_controller(self, monkeypatch, tmp_path):
        # Unlike controller wiring into the server itself (result.controller
        # above), this is unconditional on web.settings.enabled -- wiring an
        # attribute costs nothing, and nothing about /settings being
        # unmounted should make a later enable_step_up() call silently
        # look wired but do nothing.
        from privacyfence.step_up_config import LiveStepUpConfig
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        daemon_main._maybe_start_web_server(
            {}, self._connector_host(), unattended_sessions_enabled=False, controller=controller,
        )

        assert isinstance(controller._step_up, LiveStepUpConfig)

    def test_mcp_dispatcher_gets_the_controllers_connector_status_provider(self, monkeypatch, tmp_path):
        # privacyfence_status's own connector view --
        # wired to SettingsController.status_connectors alongside the
        # unattended-session listener above, so the tool reports the same
        # enabled/authenticated/blocked_by state the settings page does
        # rather than re-deriving it from the built connectors alone.
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)
        controller._connectors = ["gmail"]
        controller._connector_failures = {"slack": "not_authenticated"}

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, controller=controller,
        )

        status = result.mcp_dispatcher.status("checking")
        assert status["setup_complete"] is True
        assert status["connectors"] == controller.status_connectors()
        rows = {row["name"]: row for row in status["connectors"]}
        assert rows["slack"]["blocked_by"] == "not_authenticated"

    def test_mcp_dispatcher_gets_wired_to_notify_the_controller_of_connector_changes(self, monkeypatch, tmp_path):
        # SettingsController.refresh_connectors() needs a
        # way to reach McpDispatcher.notify_tools_changed once both objects
        # exist -- wired here alongside status_connectors above.
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert controller._connectors_changed_listener == result.mcp_dispatcher.notify_tools_changed

    def test_no_controller_means_status_falls_back_to_built_connectors_only(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher.status("checking")["connectors"] == []

    def test_allow_quit_defaults_true_and_is_configurable(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True, "allow_quit": False}}}, self._connector_host(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert result.allow_quit is False

    def test_notifications_enabled_defaults_true_and_is_configurable(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        default_result = daemon_main._maybe_start_web_server(
            {}, self._connector_host(), unattended_sessions_enabled=False,
        )
        assert default_result.notifications_enabled is True

        off_result = daemon_main._maybe_start_web_server(
            {"web": {"notifications": {"enabled": False}}}, self._connector_host(),
            unattended_sessions_enabled=False,
        )
        assert off_result.notifications_enabled is False


# ---------------------------------------------------------------------------- #
# _maybe_start_web_server -- org mode. org_config.json's "mode" selects this branch; every
# TestMaybeStartWebServer test above passes no org_config at all (or {}),
# so mode always defaults to "local" there -- this class is additive.
# ---------------------------------------------------------------------------- #

class TestMaybeStartWebServerOrgMode:
    def _no_bind(self, monkeypatch, tmp_path):
        from privacyfence import paths
        from privacyfence.web.server import WebServer
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        started = {}
        monkeypatch.setattr(WebServer, "start", lambda self: started.update(called=True))
        monkeypatch.setattr(
            "privacyfence.org_identity.discover_idp",
            lambda issuer: {
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/jwks",
            },
        )
        return started

    @staticmethod
    def _connector_host():
        from privacyfence.connector_host import ConnectorHost
        return ConnectorHost([])

    @staticmethod
    def _org_config(**overrides):
        base = {
            "mode": "org",
            "server": {"issuer_url": "https://pf.example.com", "bind_host": "0.0.0.0", "port": 8765},
            "idp": {"issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec"},
        }
        base.update(overrides)
        return base

    def test_mcp_disabled_starts_nothing_even_in_org_mode(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": False}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert result is None

    def test_org_mode_starts_a_server_with_mcp_enabled(self, monkeypatch, tmp_path):
        started = self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert result is not None
        assert started.get("called") is True
        assert result.org is not None

    def test_org_mode_uses_the_configured_bind_host_and_port(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False,
            org_config=self._org_config(server={"issuer_url": "https://pf.example.com", "bind_host": "0.0.0.0", "port": 9999}),
        )
        assert result.host == "0.0.0.0"
        assert result.port == 9999

    def test_org_mode_base_url_is_the_issuer_url(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert result.base_url == "https://pf.example.com"

    def test_org_mode_installs_the_web_approval_ui_unconditionally(self, monkeypatch, tmp_path):
        from privacyfence.approval_ui import get_approval_ui
        from privacyfence.web_approval_ui import WebApprovalUI

        self._no_bind(monkeypatch, tmp_path)
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert isinstance(get_approval_ui(), WebApprovalUI)

    def test_org_mode_registry_gets_the_real_base_url_once_started(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui

        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        registry = get_web_approval_ui().deferred_registry
        assert registry.approval_url("abc") == f"{result.base_url}/approvals/abc"

    def test_org_mode_registry_gets_the_per_principal_approval_cap(self, monkeypatch, tmp_path):
        # This is the mode the cap actually matters in -- one registry
        # shared by every principal -- so it must be wired through org
        # mode's own registry construction, not just local mode's.
        from privacyfence.web_approval_ui import get_web_approval_ui

        self._no_bind(monkeypatch, tmp_path)
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}, "approvals": {"max_pending_per_principal": 7}}},
            self._connector_host(), unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        registry = get_web_approval_ui().deferred_registry
        assert registry.max_pending_per_principal == 7

    def test_org_mode_registry_gets_adaptive_hold_too(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui

        self._no_bind(monkeypatch, tmp_path)
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}, "approvals": {"adaptive_hold": False}}},
            self._connector_host(), unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        registry = get_web_approval_ui().deferred_registry
        assert registry.adaptive_hold is False

    def test_org_mode_without_idp_section_raises(self, monkeypatch, tmp_path):
        # An org config with an incomplete IdP or server section.
        self._no_bind(monkeypatch, tmp_path)
        with pytest.raises(org_mode.ConfigurationError):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False,
                org_config={"mode": "org", "server": {"issuer_url": "https://pf.example.com"}},
            )

    @staticmethod
    def _single_user_config(issuer_url="http://localhost:8765", **server):
        return {"mode": "org", "server": {"issuer_url": issuer_url, "port": 8765, **server}}

    def test_single_user_mode_on_localhost_builds_the_org_app_without_an_idp(self, monkeypatch, tmp_path):
        started = self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._single_user_config(),
        )
        assert started.get("called") is True
        assert result.org.idp is None
        assert result.base_url == "http://localhost:8765"

    def test_single_user_mode_serves_no_identity_provider_routes(self, monkeypatch, tmp_path):
        from starlette.testclient import TestClient

        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._single_user_config(),
        )
        client = TestClient(result._server.config.app, base_url="http://localhost:8765")
        assert client.get("/login", follow_redirects=False).status_code == 404
        assert client.get("/oauth/idp/login-callback", follow_redirects=False).status_code == 404

    def test_single_user_mode_logs_single_user_in_place_of_the_idp(self, monkeypatch, tmp_path, caplog):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        with caplog.at_level(logging.INFO, logger=daemon_main.logger.name):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=self._single_user_config(),
            )
        assert "IdP single-user," in caplog.text

    def test_single_user_mode_defaults_the_issuer_to_localhost_from_the_environment(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setenv("PRIVACYFENCE_MODE", "org")
        monkeypatch.setenv("PRIVACYFENCE_PORT", "9100")
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config={},
        )
        assert result.base_url == "http://localhost:9100"

    @pytest.mark.parametrize("issuer_url", ["https://pf.example.com", "http://pf.example.com", "https://localhost"])
    def test_single_user_mode_refuses_an_address_that_is_not_plain_http_localhost(
        self, monkeypatch, tmp_path, issuer_url,
    ):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        with pytest.raises(org_mode.ConfigurationError) as excinfo:
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=self._single_user_config(issuer_url),
            )
        assert str(excinfo.value) == (
            "Organization mode without an identity provider is single-user mode and runs only at "
            "http://localhost or http://127.0.0.1. Add an \"idp\" section to org_bundle.json to "
            "serve any other address."
        )

    @pytest.mark.parametrize("pin", ["org_bundle_signing_pubkey.txt", "org_config_signing_pubkey.txt"])
    def test_single_user_mode_refuses_a_data_directory_with_a_pinned_signing_key(
        self, monkeypatch, tmp_path, pin,
    ):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / pin).write_text("a2V5\n")
        with pytest.raises(org_mode.ConfigurationError, match="belonged to an organization server"):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=self._single_user_config(),
            )

    def test_single_user_mode_refuses_a_data_directory_with_an_imported_admin_config(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setattr(admin_config, "load", lambda: {"imported_from_bundle_sha256": "ab" * 32})
        with pytest.raises(org_mode.ConfigurationError) as excinfo:
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=self._single_user_config(),
            )
        assert str(excinfo.value) == (
            "This data directory belonged to an organization server; restore org_bundle.json, or "
            "use a new data directory for single-user mode."
        )

    def test_single_user_mode_with_an_empty_import_hash_is_a_fresh_install(self, monkeypatch, tmp_path):
        started = self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setattr(admin_config, "load", lambda: {"imported_from_bundle_sha256": ""})
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._single_user_config(),
        )
        assert started.get("called") is True

    def test_a_multi_user_server_ignores_a_pinned_signing_key(self, monkeypatch, tmp_path):
        started = self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle_signing_pubkey.txt").write_text("a2V5\n")
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert started.get("called") is True

    def test_local_mcp_dispatcher_gets_the_local_scoped_token_policy(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher._scoped_token_policy.allow_ai_proposals is True
        assert result.mcp_dispatcher.scoped_token_proposals_enabled() is True

    def test_org_mode_without_server_section_raises(self, monkeypatch, tmp_path):
        # An org config with an incomplete IdP or server section.
        self._no_bind(monkeypatch, tmp_path)
        with pytest.raises(org_mode.ConfigurationError):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False,
                org_config={"mode": "org", "idp": {"issuer": "https://idp.example.com", "client_id": "c"}},
            )

    def test_org_mode_reports_org_in_the_status_tool(self, monkeypatch, tmp_path):
        # privacyfence_status's own mode field -- this is the one branch that
        # must not default to "local".
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        assert result.mcp_dispatcher.status("checking")["mode"] == "org"

    def test_org_mcp_dispatcher_gets_the_policy_from_org_config(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        off = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        on = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(), unattended_sessions_enabled=False,
            org_config={**self._org_config(), "scoped_tokens": {"enabled": True, "allow_ai_proposals": True}},
        )

        assert off.mcp_dispatcher.scoped_token_proposals_enabled() is False
        assert on.mcp_dispatcher.scoped_token_proposals_enabled() is True

    def test_no_org_config_defaults_to_local_mode(self, monkeypatch, tmp_path):
        # Omitting org_config entirely (every local-mode call site, and
        # every install's real invocation until it opts into "mode": "org")
        # must behave exactly like local mode.
        started = self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web"}}, self._connector_host(), unattended_sessions_enabled=False,
        )
        assert result is not None
        assert result.org is None
        assert started.get("called") is True


# ---------------------------------------------------------------------------- #
# _start_org_web_server -- per-principal ConnectorRegistry wiring.
# connector_registry.py's own ConnectorRegistry, as plugged into org mode's
# actual /mcp dispatch -- see that module's own docstring.
# ---------------------------------------------------------------------------- #

class TestOrgModeConnectorRegistry:
    # Deliberately not a subclass of TestMaybeStartWebServerOrgMode -- pytest
    # would collect and re-run every inherited test method a second time
    # under this class's own name too. Same three helpers, copied instead.
    _no_bind = TestMaybeStartWebServerOrgMode._no_bind
    _connector_host = staticmethod(TestMaybeStartWebServerOrgMode._connector_host)
    _org_config = staticmethod(TestMaybeStartWebServerOrgMode._org_config)

    def test_org_auth_carries_a_real_connector_registry(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=self._org_config(),
        )
        from privacyfence.connector_registry import ConnectorRegistry
        assert isinstance(result.org.connector_registry, ConnectorRegistry)

    _GRIST_AUTO = {
        "server_url": "https://grist.example.com",
        "auto_provision": {"enabled": True, "provisioner_url": "https://prov.example.com"},
    }

    def test_org_server_starts_grist_provisioner_with_registry_evict(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        seen: dict = {}
        monkeypatch.setattr(
            daemon_main.grist_provisioning, "start",
            lambda org_config, *, on_change, stop_event: seen.update(
                org_config=org_config, on_change=on_change, stop_event=stop_event,
            ),
        )
        org_config = self._org_config(grist=self._GRIST_AUTO)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )

        assert seen["org_config"] is org_config
        assert seen["stop_event"] is daemon_main._shutdown_event
        # The registry's own evict, so a new key reaches the person's next call.
        assert seen["on_change"].__self__ is result.org.connector_registry
        assert seen["on_change"].__func__.__name__ == "evict"

    def test_org_server_refuses_broken_grist_auto_provision(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(
            daemon_main.grist_provisioning, "start", lambda *a, **kw: pytest.fail("started"),
        )
        org_config = self._org_config(grist={"auto_provision": {"enabled": "yes"}})

        with pytest.raises(daemon_main.org_mode.ConfigurationError, match="auto_provision"):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=org_config,
            )

    def test_org_server_starts_the_relay_registration_loop_on_the_shared_config(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        events: list = []
        monkeypatch.setattr(
            daemon_main.relay_client, "start_registration_loop",
            lambda issuer_url, org_config, stop: events.append(("loop", issuer_url, org_config, stop)),
        )
        monkeypatch.setattr(daemon_main.relay_client, "request_check", lambda: events.append(("check",)))
        org_config = self._org_config()

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )

        assert [e[0] for e in events] == ["loop", "check"]
        _, issuer_url, shared, stop = events[0]
        assert shared is org_config and shared is result.org.org_config
        assert stop is daemon_main._shutdown_event
        assert issuer_url == result.org.issuer_url

    def test_org_auth_carries_the_org_config_bundle(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        org_config = self._org_config()
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )
        assert result.org.org_config == org_config

    def test_registry_builds_a_principals_own_connectors_from_their_own_settings(self, monkeypatch, tmp_path):
        # The real end-to-end plumbing the exit criterion needs: once a
        # principal has a token file under their own users/<id>/credentials/
        # (what web/routes_connect.py's callback route writes), the very
        # next /mcp call for that principal must see a connector built from
        # it -- not the local principal's own set, and not nothing.
        self._no_bind(monkeypatch, tmp_path)
        org_config = self._org_config(slack={"client_id": "cid", "client_secret": "sec"})
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )
        from privacyfence.principal import Principal
        from privacyfence.slack_client import SlackClient

        alice = Principal(id="alice")
        alice_dir = tmp_path / "users" / "alice"
        (alice_dir / "credentials").mkdir(parents=True)
        (alice_dir / "credentials" / "slack_token.json").write_text('{"access_token": "xoxp-alice"}')
        # build_connectors' own Slack branch calls check_connection() on a
        # real SlackClient -- stub it so this test exercises the registry's
        # own per-principal wiring, not slack_sdk's HTTP layer.
        monkeypatch.setattr(SlackClient, "check_connection", lambda self: "alice-workspace")

        host = result.org.connector_registry.get(alice)

        assert "slack" in host.connectors
        # bootstrapped on first use, per-principal -- under authority/
        assert (alice_dir / "authority" / "config" / "settings.yaml").exists()

    def test_two_principals_get_independent_connector_sets(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        org_config = self._org_config(slack={"client_id": "cid", "client_secret": "sec"})
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )
        from privacyfence.principal import Principal
        from privacyfence.slack_client import SlackClient

        monkeypatch.setattr(SlackClient, "check_connection", lambda self: "workspace")
        alice, bob = Principal(id="alice"), Principal(id="bob")
        for pid in ("alice", "bob"):
            creds = tmp_path / "users" / pid / "credentials"
            creds.mkdir(parents=True)
        (tmp_path / "users" / "alice" / "credentials" / "slack_token.json").write_text('{"access_token": "xoxp-a"}')

        alice_host = result.org.connector_registry.get(alice)
        bob_host = result.org.connector_registry.get(bob)

        assert "slack" in alice_host.connectors
        assert "slack" not in bob_host.connectors


    def test_org_server_starts_peek_provisioner_with_registry_evict(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(
            daemon_main.peek_provisioning, "start", lambda org_config, **kw: calls.append((org_config, kw)),
        )
        org_config = self._org_config()
        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
            unattended_sessions_enabled=False, org_config=org_config,
        )

        assert len(calls) == 1
        passed_config, kwargs = calls[0]
        assert passed_config is org_config
        assert kwargs["on_change"] == result.org.connector_registry.evict
        assert kwargs["stop_event"] is daemon_main._shutdown_event

    def test_org_server_refuses_broken_peek_auto_provision(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        monkeypatch.setattr(
            daemon_main.peek_provisioning, "start", lambda *a, **kw: pytest.fail("started the provisioner"),
        )
        org_config = self._org_config(peek={"auto_provision": {"enabled": "yes"}})

        with pytest.raises(org_mode.ConfigurationError, match="must be true or false"):
            daemon_main._maybe_start_web_server(
                {"web": {"mcp": {"enabled": True}}}, self._connector_host(),
                unattended_sessions_enabled=False, org_config=org_config,
            )


class TestPeekRelease:
    """``privacyfence-app --peek-release PRINCIPAL_ID``: take back the Peek token PrivacyFence minted."""

    KEY_ENV = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"

    class _FakeClient:
        instances: list = []
        answer: Any = True

        def __init__(self, base_url, key):
            self.base_url, self.key, self.released = base_url, key, []
            type(self).instances.append(self)

        def release_account(self, principal_id):
            self.released.append(principal_id)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        fake = type("FakeClient", (self._FakeClient,), {"instances": [], "answer": True})
        monkeypatch.setattr(daemon_main, "PeekProvisioningClient", fake)
        org_config = {
            "mode": "org",
            # Without an idp this would be single-user mode, where auto-provisioning is off.
            "idp": {"issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec"},
            "peek": {"base_url": "https://peek.example.com", "auto_provision": {"enabled": True}},
        }
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: org_config)
        monkeypatch.setenv(self.KEY_ENV, "peekprov_secret")
        released = []
        monkeypatch.setattr(daemon_main.peek_provisioning, "release", released.append)
        return SimpleNamespace(fake=fake, org_config=org_config, released=released, root=tmp_path)

    def test_unknown_account(self, env, capsys):
        for principal_id in ("nobody", "../escape", ""):
            assert daemon_main.run_peek_release(principal_id) == 1
        captured = capsys.readouterr()
        assert captured.err == "No such account.\n" * 3
        assert captured.out == ""
        assert env.fake.instances == []

    def test_local_is_refused(self, env, capsys):
        (env.root / "users" / "local").mkdir()
        assert daemon_main.run_peek_release("local") == 1
        assert capsys.readouterr().err == "No such account.\n"

    def test_configuration_error_is_printed(self, env, monkeypatch, capsys):
        # The switch now lives in the admin config, whose key_env is validated when the
        # provisioning settings are parsed.
        admin = admin_config.import_from_bundle(env.org_config, mode="org", single_user=False)
        admin["services"]["peek"]["auto_provision"]["key_env"] = "not a name"
        admin_config.save(admin)
        assert daemon_main.run_peek_release("alice") == 1
        assert "environment variable name" in capsys.readouterr().err
        assert env.released == []

    def test_not_enabled(self, env, capsys):
        del env.org_config["peek"]["auto_provision"]
        assert daemon_main.run_peek_release("alice") == 1
        assert capsys.readouterr().err == "Peek auto-provisioning is not enabled in the organization config.\n"

    def test_missing_key(self, env, monkeypatch, capsys):
        monkeypatch.setenv(self.KEY_ENV, "  ")
        assert daemon_main.run_peek_release("alice") == 1
        assert capsys.readouterr().err == f"Set {self.KEY_ENV} to the Peek provisioning key first.\n"
        assert env.fake.instances == []

    def test_success_revoked_message(self, env, capsys):
        assert daemon_main.run_peek_release("alice") == 0
        captured = capsys.readouterr()
        assert captured.out == (
            "Released the Peek connection of alice. The Peek account stays. "
            "PrivacyFence's token for it is revoked.\n"
        )
        assert captured.err == ""
        client = env.fake.instances[0]
        assert (client.base_url, client.key) == ("https://peek.example.com", "peekprov_secret")

    def test_no_link_message(self, env, capsys):
        env.fake.answer = False
        assert daemon_main.run_peek_release("alice") == 0
        assert capsys.readouterr().out == (
            "Released the Peek connection of alice. The Peek account stays. "
            "Peek had no provisioned token for it.\n"
        )

    def test_client_error_exits_1(self, env, capsys):
        from privacyfence.peek_client import PeekClientError

        env.fake.answer = PeekClientError("Peek refused the provisioning key (HTTP 401).")
        assert daemon_main.run_peek_release("alice") == 1
        captured = capsys.readouterr()
        assert captured.err == "Peek refused the provisioning key (HTTP 401).\n"
        assert captured.out == ""
        assert env.released == ["alice"]

    def test_release_called(self, env):
        assert daemon_main.run_peek_release("alice") == 0
        assert env.fake.instances[0].released == ["alice"]
        assert env.released == ["alice"]

    def test_runs_after_the_runtime_identity_check(self, monkeypatch):
        from privacyfence import privilege_separation

        calls: list[str] = []
        monkeypatch.setattr(privilege_separation, "check_runtime_identity", lambda: calls.append("identity"))
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: pytest.fail("read config"))
        monkeypatch.setattr(daemon_main, "run_peek_release", lambda principal_id: calls.append(principal_id) or 0)

        assert daemon_main.main(["--peek-release", "alice"]) == 0
        assert calls == ["identity", "alice"]


class TestScopeRowsUpgrade:
    """The v2 -> v3 ``auto_accept:`` upgrade at load and at org startup (ADR 0172)."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        from privacyfence import paths, scoped_tokens

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(scoped_tokens, "_STORE", None)
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))  # the local principal's settings.yaml
        daemon_main._deferred_warnings.clear()
        yield
        daemon_main._deferred_warnings.clear()

    @staticmethod
    def _v2_rules():
        return [
            PolicyRule(id="r-a", predicate="approved_folder", value=["V1"],
                       operations=frozenset({"drive.download_file"})),
            PolicyRule(id="r-b", predicate="approved_folder", value=["V1"],
                       operations=frozenset({"drive.read_file_contents"})),
        ]

    def _write_v2(self, path, rules=None):
        section = policy_store.rules_to_config(rules or self._v2_rules())
        section["version"] = 2
        path.write_text(yaml.safe_dump({"auto_accept": section}))

    def test_load_config_upgrades_with_principal(self, tmp_path):
        from privacyfence.principal import LOCAL_PRINCIPAL

        path = tmp_path / "settings.yaml"
        self._write_v2(path)

        config = daemon_main.load_config(str(path), principal=LOCAL_PRINCIPAL)

        assert config["auto_accept"]["version"] == 3
        assert yaml.safe_load(path.read_text())["auto_accept"]["version"] == 3
        assert (tmp_path / "settings.yaml.pre-scope-rules.bak").exists()
        assert any("converted to one rule per scope" in w for w in daemon_main._deferred_warnings)

    def test_load_config_without_principal_does_not_upgrade(self, tmp_path):
        path = tmp_path / "settings.yaml"
        self._write_v2(path)
        before = path.read_text()

        config = daemon_main.load_config(str(path))

        assert config["auto_accept"]["version"] == 2
        assert path.read_text() == before
        assert not (tmp_path / "settings.yaml.pre-scope-rules.bak").exists()
        assert daemon_main._deferred_warnings == []

    def test_upgrade_all_principals_runs_before_the_token_store_loads(self, monkeypatch, tmp_path):
        from privacyfence import org_identity, scoped_tokens
        from privacyfence.web.server import WebServer

        order: list[str] = []
        real_upgrade = daemon_main._upgrade_all_principals
        real_init = WebServer.__init__

        def upgrade():
            order.append("upgrade")
            real_upgrade()

        def server_init(self, *args, **kwargs):
            order.append("server")
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(daemon_main, "_upgrade_all_principals", upgrade)
        monkeypatch.setattr(WebServer, "__init__", server_init)
        monkeypatch.setattr(WebServer, "start", lambda self: None)
        monkeypatch.setattr(
            org_identity, "discover_idp",
            lambda issuer: {
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/jwks",
            },
        )
        org_config = {
            "mode": "org",
            "server": {"issuer_url": "https://pf.example.com", "bind_host": "0.0.0.0", "port": 8765},
            "idp": {"issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec"},
        }
        alice_cfg = tmp_path / "users" / "alice" / "authority" / "config" / "settings.yaml"
        alice_cfg.parent.mkdir(parents=True)
        self._write_v2(alice_cfg)

        from privacyfence.connector_host import ConnectorHost
        daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, ConnectorHost([]),
            unattended_sessions_enabled=False, org_config=org_config,
        )

        assert order == ["upgrade", "server"]
        assert yaml.safe_load(alice_cfg.read_text())["auto_accept"]["version"] == 3
        assert scoped_tokens.current_store() is not None

    def test_upgrade_all_principals_skips_a_principal_without_a_settings_file(self, tmp_path):
        (tmp_path / "users" / "bob").mkdir(parents=True)

        daemon_main._upgrade_all_principals()

        assert not (tmp_path / "users" / "bob" / "authority" / "config" / "settings.yaml").exists()

    @staticmethod
    def _legacy_token(authority, rule_ids):
        """A token entry as a version before ADR 0236 wrote it: rule ids, no rules of its own."""
        from privacyfence.scoped_tokens import STORE_FILE_NAME

        authority.mkdir(parents=True, exist_ok=True)
        (authority / STORE_FILE_NAME).write_text(json.dumps({"version": 1, "tokens": [{
            "id": "st-old", "label": "old", "digest": "d", "principal_email": "", "principal_display_name": "",
            "created_at": 0.0, "expires_at": 1e12, "rule_ids": list(rule_ids), "tools": ["gmail_list_messages"],
        }]}))
        return authority / STORE_FILE_NAME

    def test_load_config_gives_a_legacy_token_its_own_rules(self, tmp_path):
        from privacyfence.policy import scope_rows
        from privacyfence.policy.registry import Verb
        from privacyfence.principal import LOCAL_PRINCIPAL

        row = scope_rows.ScopeRow(
            id="r-folder", scope="drive.folder", value=["V1"], verbs=frozenset({Verb.READ}),
            operations=scope_rows.implied_operations("drive.folder", [Verb.READ]),
        )
        path = tmp_path / "settings.yaml"
        path.write_text(yaml.safe_dump({"auto_accept": scope_rows.rows_to_config([row])}))
        token_file = self._legacy_token(tmp_path / "authority", ["r-folder"])

        daemon_main.load_config(str(path), principal=LOCAL_PRINCIPAL)

        (entry,) = json.loads(token_file.read_text())["tokens"]
        assert entry["rules"] == [scope_rows.row_to_dict(row)]
        assert daemon_main._deferred_warnings == [
            "Scoped token 'old' now holds its own copy of 1 rule(s); 0 rule(s) it named no longer existed",
        ]

    def test_upgrade_all_principals_converts_a_token_of_a_principal_without_a_settings_file(self, tmp_path):
        token_file = self._legacy_token(tmp_path / "users" / "bob" / "authority", ["r-gone"])

        daemon_main._upgrade_all_principals()

        (entry,) = json.loads(token_file.read_text())["tokens"]
        assert entry["rules"] == [] and "rule_ids" not in entry
        assert "Scoped token 'old' now holds its own copy of 0 rule(s); 1 rule(s) it named no longer existed" in (
            daemon_main._deferred_warnings
        )

    def test_upgrade_all_principals_converts_a_principal_with_v3_rules(self, tmp_path):
        from privacyfence.policy import scope_rows

        alice_cfg = tmp_path / "users" / "alice" / "authority" / "config" / "settings.yaml"
        alice_cfg.parent.mkdir(parents=True)
        alice_cfg.write_text(yaml.safe_dump({"auto_accept": scope_rows.rows_to_config([])}))
        token_file = self._legacy_token(tmp_path / "users" / "alice" / "authority", ["r-gone"])

        daemon_main._upgrade_all_principals()

        assert json.loads(token_file.read_text())["tokens"][0]["rules"] == []

    def test_fresh_install_seed_is_already_version_3(self, tmp_path, caplog):
        from privacyfence.principal import LOCAL_PRINCIPAL

        path = tmp_path / "settings.yaml"

        config = daemon_main.load_config(str(path), principal=LOCAL_PRINCIPAL)

        assert config["auto_accept"]["version"] == 3
        assert not (tmp_path / "settings.yaml.pre-scope-rules.bak").exists()
        assert daemon_main._deferred_warnings == []

    def test_unscoped_decisions_unchanged_by_upgrade(self, tmp_path):
        from privacyfence.auto_accept import ReviewContext
        from privacyfence.policy import catalogue, propose
        from privacyfence.policy.engine import evaluate
        from privacyfence.policy.registry import Verb
        from privacyfence.policy.value_format import VALUE_FORMATS
        from privacyfence.principal import LOCAL_PRINCIPAL

        v2: list[PolicyRule] = []
        for group in [*propose.SCOPES_BY_GROUP, *catalogue.EXTRA_SCOPES]:
            extra = catalogue.EXTRA_SCOPES.get(group)
            needs_value = extra.needs_value if extra else propose.scope_needs_value(propose.SCOPES_BY_GROUP[group][0])
            from privacyfence.policy import scope_rows
            verbs = list(scope_rows.scope_verbs(group))
            predicate = extra.predicate if extra else propose.SCOPES_BY_GROUP[group][0].predicate
            value = ([VALUE_FORMATS[predicate].example if predicate in VALUE_FORMATS else "V1"]) if needs_value else None
            v2.extend(catalogue.rules_for_catalogue_entry(group, value, verbs))
        # the custom cases the scope_rows round-trip test covers
        v2 += [
            PolicyRule(id="r-d76028ec0b", predicate="always_allow", value=None,
                       operations=frozenset({"contacts.edit"}), conditions=(("no_contact_info_change", None),)),
            PolicyRule(id="my-handwritten", predicate="approved_folder", value=["V1"],
                       operations=frozenset({"drive.write_file"})),
            PolicyRule(id="r-unknown", predicate="some_predicate", value=["V1"],
                       operations=frozenset({"gmail.read_message"})),
            PolicyRule(id="r-drivefile", predicate="drive.file", value=["V1"],
                       operations=frozenset({"drive.read_file_contents"})),
            PolicyRule(id="r-partial", predicate="approved_sandbox_folder", value=["V1"],
                       operations=frozenset({"sheets.format_range"})),
        ]
        assert Verb.READ in {v for g in propose.SCOPES_BY_GROUP for v in scope_rows.scope_verbs(g)}
        path = tmp_path / "settings.yaml"
        section = policy_store.rules_to_config(v2)
        section["version"] = 2
        path.write_text(yaml.safe_dump({"auto_accept": section}))
        before = policy_store.compile_rules_from_config(yaml.safe_load(path.read_text()))

        config = daemon_main.load_config(str(path), principal=LOCAL_PRINCIPAL)
        after = policy_store.compile_rules_from_config(yaml.safe_load(path.read_text()))

        assert config["auto_accept"]["version"] == 3
        file_ = SimpleNamespace(id="V1", parent_ids=["V1"], mime_type="V1", owners=["me@example.com"])
        channel_msg = SimpleNamespace(channel_id="V1", is_private=False)
        keys = [
            "channel_id", "channel", "calendar_id", "space_key", "destination_folder_id", "parent_folder_id",
            "label_name", "to", "project_key", "issue_key", "object_type", "report_id", "task_list_id",
            "source_list_id", "destination_list_id", "chat_id", "script_id", "doc_id", "slug",
        ]
        contexts = [
            ReviewContext(connector="x", tool="t", args={**{k: "V1" for k in keys}, "is_self_dm": True},
                          raw_data=[channel_msg], my_email="me@example.com"),
            ReviewContext(connector="x", tool="t", args={k: ["V1"] for k in keys}, raw_data={"file": file_},
                          my_email="me@example.com"),
            ReviewContext(connector="x", tool="t", args={k: "OTHER" for k in keys}, raw_data=file_),
            ReviewContext(connector="x", tool="t", args={}, raw_data=None),
        ]
        operations = sorted({op for rule in v2 for op in rule.operations})
        decisions = []
        for op in operations:
            for ctx in contexts:
                was = evaluate(before, op, ctx)[0]
                assert evaluate(after, op, ctx)[0] is was, (op, ctx.args.get("channel_id"))
                decisions.append(was)
        assert True in decisions and False in decisions  # the comparison is not vacuous


# ---------------------------------------------------------------------------- #
# build_connectors: Miro
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsMiro:
    ORG = {"miro": {"client_id": "cid", "client_secret": "csecret"}}
    RECORD = {
        "access_token": "at", "refresh_token": "rt", "expires_at": 4102444800,
        "team_id": "3458764549483493025",
    }

    def _patch(self, monkeypatch, record):
        from privacyfence.miro_client import MiroClient

        def load(path):
            if record is None:
                raise daemon_main.MiroClientError(
                    "Miro is not authenticated. Use Authenticate… in PrivacyFence Settings."
                )
            return record

        monkeypatch.setattr(daemon_main, "load_miro_token", load)
        monkeypatch.setattr(MiroClient, "check_connection", lambda self: "Team")

    def test_section_and_token_file_build_the_connector(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        connectors, failures = daemon_main.build_connectors({}, self.ORG)
        assert [c.name for c in connectors] == ["miro"]
        assert "miro" not in failures

    def test_no_section_is_no_org_config(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        connectors, failures = daemon_main.build_connectors({}, {})
        assert connectors == []
        assert failures["miro"] == "no_org_config"

    def test_no_token_file_is_not_authenticated(self, monkeypatch):
        self._patch(monkeypatch, None)
        connectors, failures = daemon_main.build_connectors({}, self.ORG)
        assert connectors == []
        assert failures["miro"] == "not_authenticated"

    def test_incomplete_section_is_logged_with_its_message_and_not_built(self, monkeypatch, caplog):
        self._patch(monkeypatch, self.RECORD)
        with caplog.at_level("WARNING"):
            connectors, failures = daemon_main.build_connectors({}, {"miro": {"client_id": "cid"}})
        assert connectors == []
        assert failures["miro"] not in ("no_org_config", "not_authenticated")
        assert "client_id and client_secret go together" in caplog.text

    def test_disabled_has_no_entry(self, monkeypatch):
        self._patch(monkeypatch, self.RECORD)
        config = {"connectors": {"miro": {"enabled": False}}}
        connectors, failures = daemon_main.build_connectors(config, self.ORG)
        assert connectors == []
        assert "miro" not in failures


# parse_args
# ---------------------------------------------------------------------------- #

class TestParseArgs:
    def test_defaults_have_no_oauth_flags_set(self):
        args = daemon_main.parse_args([])
        assert not any([
            args.gmail_oauth, args.drive_oauth, args.contacts_oauth, args.calendar_oauth,
            args.tasks_oauth, args.apps_script_oauth, args.slack_oauth, args.salesforce_oauth,
            args.atlassian_oauth, args.grist_oauth, args.miro_oauth, args.telegram_setup,
        ])

    def test_config_flag_overrides_default(self):
        args = daemon_main.parse_args(["--config", "/tmp/custom.yaml"])
        assert args.config == "/tmp/custom.yaml"

    @pytest.mark.parametrize("flag,attr", [
        ("--gmail-oauth", "gmail_oauth"),
        ("--drive-oauth", "drive_oauth"),
        ("--contacts-oauth", "contacts_oauth"),
        ("--calendar-oauth", "calendar_oauth"),
        ("--tasks-oauth", "tasks_oauth"),
        ("--apps-script-oauth", "apps_script_oauth"),
        ("--slack-oauth", "slack_oauth"),
        ("--salesforce-oauth", "salesforce_oauth"),
        ("--atlassian-oauth", "atlassian_oauth"),
        ("--grist-oauth", "grist_oauth"),
        ("--miro-oauth", "miro_oauth"),
        ("--telegram-setup", "telegram_setup"),
    ])
    def test_each_oauth_flag_sets_only_its_own_attribute(self, flag, attr):
        args = daemon_main.parse_args([flag])
        assert getattr(args, attr) is True
        other_attrs = {
            "gmail_oauth", "drive_oauth", "contacts_oauth", "calendar_oauth", "tasks_oauth",
            "apps_script_oauth", "slack_oauth", "salesforce_oauth", "atlassian_oauth", "grist_oauth",
            "miro_oauth", "telegram_setup",
        } - {attr}
        assert not any(getattr(args, other) for other in other_attrs)


# ---------------------------------------------------------------------------- #
# Instance lock
# ---------------------------------------------------------------------------- #

class TestInstanceLock:
    @pytest.fixture(autouse=True)
    def _reset_lock_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "LOCK_FILE", str(tmp_path / "privacyfence.lock"))
        daemon_main._lock_fd = None
        yield
        daemon_main._release_instance_lock()

    def test_first_acquire_succeeds(self):
        assert daemon_main._acquire_instance_lock() is True

    def test_second_acquire_fails_while_first_is_held(self):
        assert daemon_main._acquire_instance_lock() is True
        assert daemon_main._acquire_instance_lock() is False

    def test_acquire_succeeds_again_after_release(self):
        assert daemon_main._acquire_instance_lock() is True
        daemon_main._release_instance_lock()
        assert daemon_main._acquire_instance_lock() is True

    def test_release_without_acquire_is_a_no_op(self):
        daemon_main._release_instance_lock()  # must not raise

    @pytest.mark.skipif(
        sys.platform == "win32", reason="portalocker's Windows backend takes a mandatory lock (LockFileEx) rather than POSIX advisory locking -- a second same-process open for reading while the lock is held raises PermissionError there, unlike fcntl.flock. The instance-lock feature itself (a second daemon cannot start) is unaffected; only this test's own read-back of the held file needs a Windows-specific rewrite",
    )
    def test_lock_file_records_holder_pid(self):
        # portalocker.lock() is handed the raw fd _acquire_instance_lock()
        # already opened, same as the fcntl.flock() call it replaced -- the
        # os.ftruncate()/os.write() calls right after it must still see a
        # live, writable fd rather than one portalocker has consumed or
        # wrapped.
        assert daemon_main._acquire_instance_lock() is True
        assert Path(daemon_main.LOCK_FILE).read_text() == str(os.getpid())

    def test_second_acquire_is_rejected_at_the_os_level_not_just_in_process(self):
        # Confirms the portalocker swap still takes a real OS-level
        # exclusive lock (LOCK_EX | LOCK_NB), not just something this
        # process's own _lock_fd bookkeeping enforces -- an independent fd
        # on the same file, opened the way a second daemon instance would,
        # must be rejected by portalocker itself.
        assert daemon_main._acquire_instance_lock() is True
        other_fd = os.open(daemon_main.LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            with pytest.raises(portalocker.exceptions.LockException):
                portalocker.lock(other_fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
        finally:
            os.close(other_fd)


# ---------------------------------------------------------------------------- #
# run_*_oauth: headless/dev CLI setup commands
# ---------------------------------------------------------------------------- #

GOOGLE_OAUTH_RUNNERS = [
    pytest.param("run_gmail_oauth", "GmailClient", "GmailClientError", id="gmail"),
    pytest.param("run_drive_oauth", "DriveClient", "DriveClientError", id="drive"),
    pytest.param("run_contacts_oauth", "ContactsClient", "ContactsClientError", id="contacts"),
    pytest.param("run_calendar_oauth", "CalendarClient", "CalendarClientError", id="calendar"),
    pytest.param("run_tasks_oauth", "TasksClient", "TasksClientError", id="tasks"),
    pytest.param("run_apps_script_oauth", "AppsScriptClient", "AppsScriptClientError", id="apps_script"),
]


class TestGoogleOauthRunners:
    @pytest.mark.parametrize("runner_name,client_attr,error_attr", GOOGLE_OAUTH_RUNNERS)
    def test_success_authorizes_and_prints_email(self, monkeypatch, capsys, runner_name, client_attr, error_attr):
        fake = fake_client_class(result="me@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)
        runner = getattr(daemon_main, runner_name)

        code = runner({"google": {"client_id": "id", "client_secret": "secret"}})

        assert code == 0
        assert fake.authorize_called is True
        assert "me@example.com" in capsys.readouterr().out

    @pytest.mark.parametrize("runner_name,client_attr,error_attr", GOOGLE_OAUTH_RUNNERS)
    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys, runner_name, client_attr, error_attr):
        error_cls = getattr(daemon_main, error_attr)
        fake = fake_client_class(authorize_error=error_cls("no browser available"))
        monkeypatch.setattr(daemon_main, client_attr, fake)
        runner = getattr(daemon_main, runner_name)

        code = runner({})

        assert code == 1
        assert "no browser available" in capsys.readouterr().err


class TestSlackOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_slack_oauth({}) == 1
        assert "No Slack organization config" in capsys.readouterr().err

    def test_success_prints_team_name_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "slack_authorize_interactive",
            lambda **kw: {"team_name": "Acme"},
        )
        code = daemon_main.run_slack_oauth({"slack": {"client_id": "id", "client_secret": "s"}})
        assert code == 0
        assert "Acme" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.SlackClientError("invalid redirect")
        monkeypatch.setattr(daemon_main, "slack_authorize_interactive", raiser)
        code = daemon_main.run_slack_oauth({"slack": {"client_id": "id", "client_secret": "s"}})
        assert code == 1
        assert "invalid redirect" in capsys.readouterr().err


class TestSalesforceOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_salesforce_oauth({}) == 1
        assert "No Salesforce organization config" in capsys.readouterr().err

    def test_success_prints_instance_url_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "salesforce_authorize_interactive",
            lambda **kw: {"instance_url": "https://x.salesforce.com"},
        )
        code = daemon_main.run_salesforce_oauth({"salesforce": {"consumer_key": "ck", "consumer_secret": "cs"}})
        assert code == 0
        assert "x.salesforce.com" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.SalesforceClientError("bad login url")
        monkeypatch.setattr(daemon_main, "salesforce_authorize_interactive", raiser)
        code = daemon_main.run_salesforce_oauth({"salesforce": {"consumer_key": "ck", "consumer_secret": "cs"}})
        assert code == 1
        assert "bad login url" in capsys.readouterr().err


class TestGristOauthRunner:
    APP = {"grist": {"client_id": "cid", "client_secret": "cs"}}

    def test_no_oauth_app_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_grist_oauth({}) == 1
        assert "No Grist OAuth app in the organization config." in capsys.readouterr().err

    def test_success_prints_host_and_returns_0(self, monkeypatch, capsys):
        seen = {}

        def fake(config, token_file):
            seen["config"], seen["token_file"] = config, token_file
            return {"server_url": "https://docs.getgrist.com"}

        monkeypatch.setattr(daemon_main, "grist_authorize_interactive", fake)
        assert daemon_main.run_grist_oauth(self.APP) == 0
        assert "Grist OAuth complete. Signed in to docs.getgrist.com." in capsys.readouterr().out
        assert seen["config"].client_id == "cid"
        assert seen["token_file"].endswith("grist_token.json")

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(config, token_file):
            raise daemon_main.GristClientError("consent denied")
        monkeypatch.setattr(daemon_main, "grist_authorize_interactive", raiser)
        assert daemon_main.run_grist_oauth(self.APP) == 1
        assert "Grist OAuth setup failed: consent denied" in capsys.readouterr().err


class TestMiroOauthRunner:
    APP = {"miro": {"client_id": "cid", "client_secret": "cs"}}

    def test_no_section_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_miro_oauth({}) == 1
        assert "Miro OAuth setup failed: Miro organization config not installed" in capsys.readouterr().err

    def test_incomplete_section_prints_its_message_and_returns_1(self, capsys):
        assert daemon_main.run_miro_oauth({"miro": {"client_id": "cid"}}) == 1
        assert "client_id and client_secret go together" in capsys.readouterr().err

    def test_success_prints_team_and_returns_0(self, monkeypatch, capsys):
        seen = {}

        def fake(config, token_file):
            seen["config"], seen["token_file"] = config, token_file
            return {"team_id": "3458764549483493025"}

        monkeypatch.setattr(daemon_main, "miro_authorize_interactive", fake)
        assert daemon_main.run_miro_oauth(self.APP) == 0
        assert "Miro OAuth complete. Signed in to the Miro team 3458764549483493025." in capsys.readouterr().out
        assert seen["config"].client_id == "cid"
        assert seen["token_file"].endswith("miro_token.json")

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(config, token_file):
            raise daemon_main.MiroClientError("consent denied")
        monkeypatch.setattr(daemon_main, "miro_authorize_interactive", raiser)
        assert daemon_main.run_miro_oauth(self.APP) == 1
        assert "Miro OAuth setup failed: consent denied" in capsys.readouterr().err


class TestAtlassianOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_atlassian_oauth({}) == 1
        assert "No Atlassian organization config" in capsys.readouterr().err

    @pytest.mark.parametrize(("config", "org_extra", "expect_assets"), [
        (None, {}, False),
        ({"connectors": {"assets": {"enabled": True}}}, {}, True),
        (None, {"assets": True}, True),
        ({"connectors": {"assets": {"enabled": False}}}, {"assets": True}, False),
    ])
    def test_authorize_scopes_include_assets_only_when_enabled(self, monkeypatch, config, org_extra, expect_assets):
        captured = {}

        def fake_authorize(**kw):
            captured.update(kw)
            return {"site_url": "https://acme.atlassian.net"}

        monkeypatch.setattr(daemon_main, "atlassian_authorize_interactive", fake_authorize)
        org = {"atlassian": {"client_id": "ci", "client_secret": "cs", **org_extra}}

        assert daemon_main.run_atlassian_oauth(org, config) == 0
        assert ("read:cmdb-object:jira" in captured["scopes"]) is expect_assets
        assert "read:jira-work" in captured["scopes"]

    def test_success_prints_site_url_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "atlassian_authorize_interactive",
            lambda **kw: {"site_url": "https://acme.atlassian.net"},
        )
        code = daemon_main.run_atlassian_oauth({"atlassian": {"client_id": "ci", "client_secret": "cs"}})
        assert code == 0
        assert "acme.atlassian.net" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.AtlassianOAuthError("consent denied")
        monkeypatch.setattr(daemon_main, "atlassian_authorize_interactive", raiser)
        code = daemon_main.run_atlassian_oauth({"atlassian": {"client_id": "ci", "client_secret": "cs"}})
        assert code == 1
        assert "consent denied" in capsys.readouterr().err

    def test_passes_a_pick_resource_callback(self, monkeypatch):
        # Without one, resolve_resource_and_save hard-fails the moment
        # accessible-resources returns more than one entry -- see
        # _cli_pick_atlassian_resource's own docstring for why that happens
        # even for a single-site QA account.
        captured = {}

        def fake_authorize(**kw):
            captured.update(kw)
            return {"site_url": "https://acme.atlassian.net"}

        monkeypatch.setattr(daemon_main, "atlassian_authorize_interactive", fake_authorize)
        daemon_main.run_atlassian_oauth({"atlassian": {"client_id": "ci", "client_secret": "cs"}})
        assert captured["pick_resource"] is daemon_main._cli_pick_atlassian_resource


class TestCliPickAtlassianResource:
    def test_same_url_duplicates_auto_picked_without_prompting(self, monkeypatch):
        # The classic Jira + granular Confluence scope split (atlassian_oauth.py's
        # DEFAULT_SCOPES) can make one site come back as two resource entries.
        resources = [
            {"id": "cloud1", "url": "https://acme.atlassian.net", "scopes": ["read:jira-work"]},
            {"id": "cloud1", "url": "https://acme.atlassian.net", "scopes": ["read:space:confluence"]},
        ]
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
        assert daemon_main._cli_pick_atlassian_resource(resources) is resources[0]

    def test_distinct_sites_prompts_and_returns_chosen_index(self, monkeypatch, capsys):
        resources = [
            {"id": "cloud1", "url": "https://acme.atlassian.net"},
            {"id": "cloud2", "url": "https://other.atlassian.net"},
        ]
        monkeypatch.setattr("builtins.input", lambda *a: "1")
        assert daemon_main._cli_pick_atlassian_resource(resources) is resources[1]
        out = capsys.readouterr().out
        assert "acme.atlassian.net" in out and "other.atlassian.net" in out

    def test_distinct_sites_reprompts_on_invalid_input(self, monkeypatch):
        resources = [
            {"id": "cloud1", "url": "https://acme.atlassian.net"},
            {"id": "cloud2", "url": "https://other.atlassian.net"},
        ]
        answers = iter(["nope", "99", "0"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        assert daemon_main._cli_pick_atlassian_resource(resources) is resources[0]


class TestPeekLoginRunner:
    BASE = "https://peek.example.com"

    def _login(self, *, interval=1, expires_at=None):
        from privacyfence.peek_client import PeekDeviceLogin

        return PeekDeviceLogin(
            device_code="dc", user_code="ABCD-1234",
            verification_url=f"{self.BASE}/cli/verify",
            interval=interval,
            expires_at=time.time() + 600 if expires_at is None else expires_at,
        )

    def _patch(self, monkeypatch, polls, *, login=None, prompt=None, start_error=None):
        from privacyfence.peek_client import PeekLoginPoll

        seen = {"sleeps": [], "saved": None, "prompts": 0, "polled": []}
        statuses = iter(polls)

        def fake_input(message):
            seen["prompts"] += 1
            return prompt

        def start(base):
            seen["started"] = base
            if start_error is not None:
                raise start_error
            return login or self._login()

        def poll(base, device_code):
            seen["polled"].append((base, device_code))
            status = next(statuses)
            if isinstance(status, Exception):
                raise status
            return PeekLoginPoll(status=status, token="newtok" if status == "approved" else "")

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(daemon_main.time, "sleep", lambda n: seen["sleeps"].append(n))
        monkeypatch.setattr(daemon_main, "peek_start_device_login", start)
        monkeypatch.setattr(daemon_main, "peek_poll_device_login", poll)
        monkeypatch.setattr(daemon_main, "save_peek_token", lambda path, base, token: seen.update(saved=(path, base, token)))
        return seen

    def test_success_after_a_pending_poll_saves_the_token(self, monkeypatch, capsys):
        seen = self._patch(monkeypatch, ["pending", "approved"], prompt=self.BASE + "/")
        assert daemon_main.run_peek_login({}) == 0
        out = capsys.readouterr().out
        assert "Open https://peek.example.com/cli/verify and approve the code ABCD-1234." in out
        assert "Peek sign-in complete. Connected to peek.example.com." in out
        path, base, token = seen["saved"]
        assert path.endswith("peek_token.json")
        assert (base, token) == (self.BASE, "newtok")
        assert seen["sleeps"] == [1, 1]
        assert seen["prompts"] == 1

    def test_pinned_url_skips_the_prompt(self, monkeypatch):
        seen = self._patch(monkeypatch, ["approved"])
        assert daemon_main.run_peek_login({"peek": {"base_url": self.BASE}}) == 0
        assert seen["prompts"] == 0
        assert seen["started"] == self.BASE

    def test_bad_pinned_url_returns_1(self, monkeypatch, capsys):
        self._patch(monkeypatch, [])
        assert daemon_main.run_peek_login({"peek": {"base_url": "ftp://x"}}) == 1
        assert "Peek organization config is not usable" in capsys.readouterr().err

    def test_denied_returns_1(self, monkeypatch, capsys):
        self._patch(monkeypatch, ["denied"], prompt=self.BASE)
        assert daemon_main.run_peek_login({}) == 1
        assert "The Peek sign-in was denied." in capsys.readouterr().err

    @pytest.mark.parametrize("status", ["expired", "consumed"])
    def test_expired_and_consumed_return_1(self, monkeypatch, capsys, status):
        self._patch(monkeypatch, [status], prompt=self.BASE)
        assert daemon_main.run_peek_login({}) == 1
        assert "The Peek sign-in code expired. Run --peek-login again." in capsys.readouterr().err

    def test_time_past_expires_at_returns_1_without_polling(self, monkeypatch, capsys):
        seen = self._patch(
            monkeypatch, [], prompt=self.BASE, login=self._login(expires_at=time.time() - 1)
        )
        assert daemon_main.run_peek_login({}) == 1
        assert "The Peek sign-in code expired. Run --peek-login again." in capsys.readouterr().err
        assert seen["polled"] == []

    def test_client_error_returns_1(self, monkeypatch, capsys):
        from privacyfence.peek_client import PeekClientError

        self._patch(monkeypatch, [PeekClientError("boom")], prompt=self.BASE)
        assert daemon_main.run_peek_login({}) == 1
        assert "Peek sign-in failed: boom" in capsys.readouterr().err

    def test_bad_typed_address_returns_1(self, monkeypatch, capsys):
        self._patch(monkeypatch, [], prompt="")
        assert daemon_main.run_peek_login({}) == 1
        assert "Peek sign-in failed: Enter the Peek server address" in capsys.readouterr().err


class TestTelegramSetupRunner:
    def test_missing_app_credentials_prints_error_and_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)
        code = daemon_main.run_telegram_setup()
        assert code == 1
        assert "No Telegram app credentials" in capsys.readouterr().err

    def test_success_authorizes_and_prints_session_path(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))

        captured = {}
        class FakeTelegramClient:
            def __init__(self, api_id, api_hash, session_file):
                captured["api_id"] = api_id
                captured["session_file"] = session_file
            async def authorize_interactive(self):
                captured["authorized"] = True
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", FakeTelegramClient)

        code = daemon_main.run_telegram_setup()

        assert code == 0
        assert captured["authorized"] is True
        assert captured["session_file"] in capsys.readouterr().out


# ---------------------------------------------------------------------------- #
# _warm_connector_caches
# ---------------------------------------------------------------------------- #

class TestWarmConnectorCaches:
    """_warm_connector_caches() is what run_app() calls right after the web
    server's own ASGI event loop is known to be up -- Slack's client is
    synchronous, so it gets its own background thread; Telegram's is
    asyncio-native and has to run on that same loop (see the function's
    docstring). Both are fire-and-forget from the caller's point of view,
    so these tests poll briefly for the background work to land rather than
    joining a handle _warm_connector_caches doesn't expose."""

    def _running_loop(self):
        """A bare event loop on its own thread -- stands in for the web
        server's own loop without needing a real WebServer/socket."""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        return loop, thread

    def _stop(self, loop, thread):
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_slack_connector_warmed_on_its_own_background_thread(self):
        client = MagicMock()
        connector = SlackConnector(client)
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([connector], loop)
            assert self._wait_until(lambda: client.ensure_directories_fresh.called)
        finally:
            self._stop(loop, thread)

    def test_telegram_connector_warmed_on_the_given_web_loop(self):
        calls: list[threading.Thread] = []

        class FakeTelegramClient:
            async def ensure_chat_directory_fresh(self):
                calls.append(threading.current_thread())

        connector = TelegramConnector(FakeTelegramClient())
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([connector], loop)
            assert self._wait_until(lambda: bool(calls))
            assert calls[0] is thread
        finally:
            self._stop(loop, thread)

    def test_telegram_warm_failure_is_logged_not_raised(self, caplog):
        class FailingTelegramClient:
            async def ensure_chat_directory_fresh(self):
                raise RuntimeError("boom")

        connector = TelegramConnector(FailingTelegramClient())
        loop, thread = self._running_loop()
        try:
            with caplog.at_level(logging.WARNING, logger="privacyfence.daemon"):
                daemon_main._warm_connector_caches([connector], loop)
                assert self._wait_until(lambda: "Background Telegram cache warm failed" in caplog.text)
            assert "boom" in caplog.text
        finally:
            self._stop(loop, thread)

    def test_other_connector_types_are_left_untouched(self):
        other = MagicMock()
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([other], loop)
            # Nothing to poll for -- this must be a synchronous no-op for a
            # connector that's neither Slack nor Telegram.
            other.client.ensure_directories_fresh.assert_not_called()
        finally:
            self._stop(loop, thread)

    def test_empty_connector_list_is_a_no_op(self):
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([], loop)  # must not raise
        finally:
            self._stop(loop, thread)


# ---------------------------------------------------------------------------- #
# run_app
# ---------------------------------------------------------------------------- #

class _FakeWebServer:
    """Stand-in for whatever web/server.py's WebServer._maybe_start_web_
    server() would return -- run_app() only ever calls wait_until_ready()
    on it (see that function's own construction logic, covered separately
    by TestMaybeStartWebServer). Distinct per instance so a test can assert
    identity against the specific loop it hands back."""
    instances: list["_FakeWebServer"] = []

    def __init__(self, loop: Any) -> None:
        self._loop = loop
        type(self).instances.append(self)

    def wait_until_ready(self, timeout: float = 5.0) -> Any:
        return self._loop


class TestRunApp:
    """run_app() ends by blocking on _wait_for_shutdown() (a plain
    threading.Event), not inside a native UI run loop, so this whole class
    is platform-independent and runs on the Linux CI leg like every other
    class in this file."""

    def _patch_common(self, monkeypatch, connectors=None, *, web_server="__default__"):
        """``web_server`` controls what the (mocked) _maybe_start_web_server
        returns: the default builds a _FakeWebServer with a harmless
        placeholder loop (the common case -- most of these tests don't care
        about cache-warming specifically); ``None`` simulates the org-mode
        "no server built" case (see TestMaybeStartWebServerOrgMode for the
        real short-circuit this stands in for -- local mode always builds
        one); any other value is used as the loop a real, given
        _FakeWebServer.wait_until_ready() should return (itself ``None`` to
        simulate a loop that never became ready in time).

        Also stubs _wait_for_shutdown() to return immediately and
        _run_update_check_timer() to a no-op (real SettingsController
        instances aren't otherwise configured with a mocked check_for_update
        in this class -- see test_settings_controller.py's own controller
        fixture for that -- so the real timer thread must not run here).

        Every call run_app() makes into
        _maybe_start_web_server is recorded on
        ``self._web_server_calls`` -- what
        TestUnattendedSessionsConfig below reads instead of the old
        fake-IPCServer's own ``.unattended_sessions_enabled`` attribute.
        """
        connectors = [] if connectors is None else connectors
        monkeypatch.setattr(daemon_main, "init_config_path", lambda path: None)
        fake_audit_logger = MagicMock()
        monkeypatch.setattr(daemon_main, "init_audit_logger", lambda path, **kwargs: fake_audit_logger)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: (connectors, {}))
        monkeypatch.setattr(daemon_main, "_wait_for_shutdown", lambda: None)
        monkeypatch.setattr(daemon_main, "_run_update_check_timer", lambda controller: None)
        monkeypatch.setattr(daemon_main, "_run_personal_data_sweep", lambda: None)

        _FakeWebServer.instances = []
        self._web_server_calls: list[dict[str, Any]] = []

        def fake_maybe_start_web_server(config, connector_host, **kwargs):
            self._web_server_calls.append({"config": config, "connector_host": connector_host, **kwargs})
            if web_server is None:
                return None
            loop = SimpleNamespace() if web_server == "__default__" else web_server
            return _FakeWebServer(loop)

        monkeypatch.setattr(daemon_main, "_maybe_start_web_server", fake_maybe_start_web_server)
        return fake_audit_logger

    def test_lock_already_held_returns_0_without_building_connectors(self, monkeypatch, capsys, caplog):
        # Exit 0, not 1: whatever re-launches this daemon (a service
        # manager, an autostart entry, a human running it again) finds an
        # instance already running as an expected outcome, not a failure
        # the service manager should log as one. Logged at INFO rather
        # than ERROR for the same reason; the stderr message stays for a
        # human running the CLI a second time.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: False)
        build_calls = []
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: build_calls.append(1))

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert build_calls == []
        assert "already running" in capsys.readouterr().err
        assert "already running" in caplog.text
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)

    def test_successful_startup_waits_for_shutdown_and_releases_lock(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        connector = SimpleNamespace(name="gmail")
        self._patch_common(monkeypatch, connectors=[connector])

        wait_calls = []
        monkeypatch.setattr(daemon_main, "_wait_for_shutdown", lambda: wait_calls.append(1))

        result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert wait_calls == [1]
        assert release_calls == [1]
        # The connector_host built for _maybe_start_web_server is a real
        # ConnectorHost (see connector_host.py), not a stub.
        host = self._web_server_calls[0]["connector_host"]
        assert host.connectors == {"gmail": connector}

    def test_update_check_timer_thread_started_with_the_real_controller(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        timer_calls = []
        monkeypatch.setattr(daemon_main, "_run_update_check_timer", lambda controller: timer_calls.append(controller))

        daemon_main.run_app({}, "config.yaml")

        assert wait_until(lambda: timer_calls)
        from privacyfence.settings_controller import SettingsController
        assert isinstance(timer_calls[0], SettingsController)

    def test_personal_data_sweep_thread_started(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        sweeps = []
        monkeypatch.setattr(daemon_main, "_run_personal_data_sweep", lambda: sweeps.append(1))

        daemon_main.run_app({}, "config.yaml")

        assert wait_until(lambda: sweeps)

    def test_background_cache_warm_kicked_off_with_connectors_and_web_loop(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        connector = SimpleNamespace(name="slack")
        loop = SimpleNamespace()
        self._patch_common(monkeypatch, connectors=[connector], web_server=loop)
        warm_calls = []
        monkeypatch.setattr(daemon_main, "_warm_connector_caches", lambda conns, loop: warm_calls.append((conns, loop)))

        daemon_main.run_app({}, "config.yaml")

        assert len(warm_calls) == 1
        assert warm_calls[0][0] == [connector]
        assert warm_calls[0][1] is loop

    def test_org_mode_refuses_to_start_over_insecure_storage_permissions(self, tmp_path, monkeypatch):
        """The storage-permissions check is wired into run_app() right after
        org_config is loaded -- raises before build_connectors()/the web
        server ever get a chance to run, same "fail fast, before anything
        else stands up" posture the ConfigurationError for a broken org
        config already has."""
        from privacyfence.secure_files import InsecurePermissionsError

        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mode": "org"})
        # Writing admin_config.json would tighten the directory this test deliberately loosens,
        # and the import is not what is under test here.
        monkeypatch.setattr(daemon_main, "load_admin_config", lambda bundle: ({}, False))
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "user_dir", lambda: tmp_path)
        tmp_path.chmod(0o755)
        build_calls = []
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: build_calls.append(1))

        with pytest.raises(InsecurePermissionsError):
            daemon_main.run_app({}, "config.yaml")

        assert build_calls == []

    def test_single_user_mode_logs_its_warning_at_every_start(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mode": "org"})
        monkeypatch.setattr(daemon_main, "load_admin_config", lambda bundle: ({}, False))

        with caplog.at_level(logging.WARNING):
            daemon_main.run_app({}, "config.yaml")

        assert "Single-user mode: one administrator, no identity provider, on this computer only." in caplog.text

    def test_a_server_with_an_idp_does_not_log_the_single_user_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mode": "org", "idp": {"issuer": "https://i"}})
        monkeypatch.setattr(daemon_main, "load_admin_config", lambda bundle: ({}, False))

        with caplog.at_level(logging.WARNING):
            daemon_main.run_app({}, "config.yaml")

        assert "Single-user mode" not in caplog.text

    def test_background_cache_warm_skipped_silently_when_no_web_server_at_all(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        # Simulates org mode's own "mcp.enabled off" short circuit (see
        # TestMaybeStartWebServerOrgMode) -- local mode always builds a
        # server, but run_app() itself doesn't care which mode
        # produced None here.
        self._patch_common(monkeypatch, web_server=None)
        warm_calls = []
        monkeypatch.setattr(daemon_main, "_warm_connector_caches", lambda conns, loop: warm_calls.append((conns, loop)))

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert warm_calls == []
        assert "skipping background cache warm" not in caplog.text

    def test_background_cache_warm_skipped_and_logged_if_web_loop_never_became_ready(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        warm_calls = []
        monkeypatch.setattr(daemon_main, "_warm_connector_caches", lambda conns, loop: warm_calls.append((conns, loop)))
        # A server was built but its loop never got captured before
        # WebServer.wait_until_ready()'s own timeout -- distinct from "no
        # server at all" above.
        server = _FakeWebServer(loop=None)
        monkeypatch.setattr(daemon_main, "_maybe_start_web_server", lambda *a, **kw: server)

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert warm_calls == []
        assert "skipping background cache warm" in caplog.text

    def test_no_connectors_built_still_completes_startup(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert self._web_server_calls[0]["connector_host"].connectors == {}
        assert "No connectors could be initialized" in caplog.text

    def test_inconsistent_drive_privacy_categories_log_a_warning(self, monkeypatch, caplog):
        # check_consistency_warnings() runs right after init_privacy_filter()
        # -- see privacy_filter.py. Advisory only, never changes what
        # actually gets filtered.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])
        config = {"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "block"}}}

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app(config, "config.yaml")

        assert result == 0
        assert "file_metadata" in caplog.text
        assert "file_list" in caplog.text

    def test_consistent_drive_privacy_categories_log_no_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])
        config = {"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "allow"}}}

        with caplog.at_level(logging.WARNING):
            daemon_main.run_app(config, "config.yaml")

        assert "file_metadata" not in caplog.text

    def test_malformed_privacy_filter_config_refuses_to_start(self, monkeypatch):
        # A typo'd default_policy must fail closed, not silently
        # downgrade to "allow" -- run_app() propagates
        # PrivacyFilterConfigError (a ValueError) all the way out, same
        # "print and refuse to start" path org_mode.ConfigurationError
        # already takes via main()'s top-level catch.
        from privacyfence.privacy_filter import PrivacyFilterConfigError

        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        config = {"privacy": {"default_policy": "delete_everything"}}

        with pytest.raises(PrivacyFilterConfigError):
            daemon_main.run_app(config, "config.yaml")

    @pytest.mark.skipif(
        sys.platform == "win32", reason="secure_files.audit_directory_permissions() flags every directory as insecure here because chmod does not restrict access on Windows -- same known, accepted permission-bits gap as test_secure_files.py, just surfacing through the org-mode startup check instead of a direct stat() assertion",
    )
    def test_org_mode_passes_org_managed_through_to_privacy_filter(self, monkeypatch):
        # An org-managed install's genuinely-absent privacy groups
        # should fail closed to "block", not inherit local mode's "allow".
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(
            daemon_main, "load_org_config",
            lambda: {"mode": "org", "server": {"issuer_url": "https://pf.example.com"},
                      "idp": {"issuer": "https://idp.example.com", "client_id": "c"}},
        )

        result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        from privacyfence.privacy_filter import category_policy
        assert category_policy("privacy", "body") == "block"

    def test_local_mode_privacy_filter_still_defaults_to_allow(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        from privacyfence.privacy_filter import category_policy
        assert category_policy("privacy", "body") == "allow"

    def test_keyboard_interrupt_is_caught_lock_released_returns_0(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        self._patch_common(monkeypatch)

        def raise_interrupt():
            raise KeyboardInterrupt()
        monkeypatch.setattr(daemon_main, "_wait_for_shutdown", raise_interrupt)

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert release_calls == [1]
        assert "Interrupted; shutting down" in caplog.text

    def test_unexpected_exception_still_releases_lock_then_propagates(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        self._patch_common(monkeypatch)

        def raise_other():
            raise RuntimeError("shutdown wait crashed")
        monkeypatch.setattr(daemon_main, "_wait_for_shutdown", raise_other)

        with pytest.raises(RuntimeError, match="shutdown wait crashed"):
            daemon_main.run_app({}, "config.yaml")

        assert release_calls == [1]

    def test_installs_the_on_disk_rules_without_rewriting_the_file(self, monkeypatch, tmp_path, caplog):
        # run_app reads the auto_accept: section straight into the hot rule store. Nothing converts
        # or rewrites the file on the way (ADR 0041), so its bytes are exactly what was there.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        rule = PolicyRule(
            id="", predicate="approved_folder", value=["F1"],
            operations=frozenset({"drive.read_file_contents", "sheets.read_values"}),
        )
        config = {policy_store.AUTO_ACCEPT_CONFIG_KEY: policy_store.rules_to_config(policy_store.merge_rules([rule]))}
        config_path = tmp_path / "settings.yaml"
        original_text = yaml.safe_dump(config)
        config_path.write_text(original_text, encoding="utf-8")

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app(dict(config), str(config_path))

        assert result == 0
        assert config_path.read_text(encoding="utf-8") == original_text
        assert not (tmp_path / "settings.yaml.bak").exists()
        from privacyfence import auto_accept
        assert auto_accept.get_policy_v2_store_rules() == policy_store.compile_rules_from_config(config)

    def test_stale_rule_suggestion_priority_key_is_silently_ignored(self, monkeypatch, tmp_path, caplog):
        # rule_suggestion_priority is not a setting (every matching
        # auto-accept rule gets its own "Always allow" button, so there's
        # nothing to prioritize or exclude). A rule_suggestion_priority
        # block in a user's settings.yaml must still load without error,
        # through the same silent "unknown key is inert" handling as any
        # other unrecognized key, not a dedicated call-out.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        config = {"rule_suggestion_priority": {"drive_read": ["approved_folder", "i_am_owner"]}}
        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app(config, str(tmp_path / "settings.yaml"))

        assert result == 0
        assert "rule_suggestion_priority" not in caplog.text

    def test_unattended_sessions_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        daemon_main.run_app({}, "config.yaml")

        assert self._web_server_calls[0]["unattended_sessions_enabled"] is False

    def test_unattended_sessions_enabled_flag_passed_through_from_org_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"unattended_sessions": {"enabled": True}})

        daemon_main.run_app({}, "config.yaml")

        assert self._web_server_calls[0]["unattended_sessions_enabled"] is True

    @pytest.mark.parametrize("value", ["truthful", "all_read_only"])
    def test_a_bundle_carrying_the_removed_tool_annotations_key_refuses_to_start(self, monkeypatch, value):
        # ADR 0089: only a 5.0.0a1 bundle can carry mcp.tool_annotations. The daemon refuses to
        # start rather than quietly advertise something other than what the administrator asked for.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mcp": {"tool_annotations": value}})

        with pytest.raises(org_mode.ConfigurationError, match=r"mcp\.tool_annotations.*removed"):
            daemon_main.run_app({}, "config.yaml")

        assert self._web_server_calls == []

    def test_unattended_sessions_enabled_in_settings_yaml_is_ignored(self, monkeypatch):
        """unattended_sessions.enabled lives in org_config.json, not settings.yaml -- a
        stray copy in settings.yaml (e.g. left over pre-migration) must not enable it."""
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)

        daemon_main.run_app({"unattended_sessions": {"enabled": True}}, "config.yaml")

        assert self._web_server_calls[0]["unattended_sessions_enabled"] is False

    def test_exports_pending_audit_entries_on_startup(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        fake_audit_logger = self._patch_common(monkeypatch)

        daemon_main.run_app({}, "config.yaml")

        fake_audit_logger.export_all_pending.assert_called_once()

    def test_audit_logger_is_closed_on_shutdown(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        fake_audit_logger = self._patch_common(monkeypatch)

        daemon_main.run_app({}, "config.yaml")

        fake_audit_logger.close.assert_called_once()


class TestAuditForwardingWiring:
    """run_app() only builds an AuditForwarder when org mode's
    audit_forwarding config is both enabled and actually running in org
    mode -- local mode (or an org_config with mode: local) never forwards,
    whatever the section says."""

    def _patch_common(self, *args, **kwargs):
        # Reuses TestRunApp's own setup rather than duplicating it --
        # instantiated directly (not inherited) so pytest doesn't also
        # re-collect and re-run TestRunApp's own test_* methods under this
        # class.
        return TestRunApp()._patch_common(*args, **kwargs)

    def test_disabled_by_default_no_forwarder_built(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        self._patch_common(monkeypatch)
        stub = daemon_main.init_audit_logger
        captured = {}

        def wrapped(path, **kwargs):
            captured.update(kwargs)
            return stub(path, **kwargs)
        monkeypatch.setattr(daemon_main, "init_audit_logger", wrapped)

        daemon_main.run_app({}, "config.yaml")

        assert captured["forwarder"] is None

    def test_enabled_but_local_mode_no_forwarder_built(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {
            "audit_forwarding": {"enabled": True, "kind": "syslog", "syslog": {"host": "siem.example.com"}},
        })
        stub = daemon_main.init_audit_logger
        captured = {}

        def wrapped(path, **kwargs):
            captured.update(kwargs)
            return stub(path, **kwargs)
        monkeypatch.setattr(daemon_main, "init_audit_logger", wrapped)

        daemon_main.run_app({}, "config.yaml")

        assert captured["forwarder"] is None

    @pytest.mark.skipif(
        sys.platform == "win32", reason="secure_files.audit_directory_permissions() flags every directory as insecure here because chmod does not restrict access on Windows -- same known, accepted permission-bits gap as test_secure_files.py, just surfacing through the org-mode startup check instead of a direct stat() assertion",
    )
    def test_enabled_org_mode_builds_a_forwarder(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {
            "mode": "org",
            "audit_forwarding": {"enabled": True, "kind": "syslog", "syslog": {"host": "siem.example.com"}},
        })
        stub = daemon_main.init_audit_logger
        captured = {}

        def wrapped(path, **kwargs):
            captured.update(kwargs)
            return stub(path, **kwargs)
        monkeypatch.setattr(daemon_main, "init_audit_logger", wrapped)

        try:
            daemon_main.run_app({}, "config.yaml")
        finally:
            forwarder = captured.get("forwarder")
            if forwarder is not None:
                forwarder.stop()

        assert captured["forwarder"] is not None

    @pytest.mark.skipif(
        sys.platform == "win32", reason="secure_files.audit_directory_permissions() flags every directory as insecure here because chmod does not restrict access on Windows -- same known, accepted permission-bits gap as test_secure_files.py, just surfacing through the org-mode startup check instead of a direct stat() assertion",
    )
    def test_enabled_org_mode_with_invalid_forwarding_config_does_not_crash_startup(self, monkeypatch, caplog, tmp_path):
        # kind="syslog" with no host at all -- audit_forwarding.build_sender()
        # raises ValueError; run_app() must log and keep starting without a
        # forwarder rather than taking the whole daemon down over a bad
        # forwarding config.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {
            "mode": "org", "audit_forwarding": {"enabled": True, "kind": "syslog"},
        })
        stub = daemon_main.init_audit_logger
        captured = {}

        def wrapped(path, **kwargs):
            captured.update(kwargs)
            return stub(path, **kwargs)
        monkeypatch.setattr(daemon_main, "init_audit_logger", wrapped)

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert captured["forwarder"] is None
        assert "Could not start audit-log forwarding" in caplog.text


# ---------------------------------------------------------------------------- #
# main(): CLI dispatch
# ---------------------------------------------------------------------------- #

class TestMain:
    def _patch_config(self, monkeypatch, config=None):
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: config or {})
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: None)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})

    def test_config_load_failure_prints_error_and_returns_1(self, monkeypatch, capsys):
        def raiser(path, **kwargs):
            raise ValueError("bad yaml")
        monkeypatch.setattr(daemon_main, "load_config", raiser)

        result = daemon_main.main([])

        assert result == 1
        assert "Configuration error" in capsys.readouterr().err

    @pytest.mark.parametrize("flag,runner_name", [
        ("--gmail-oauth", "run_gmail_oauth"),
        ("--drive-oauth", "run_drive_oauth"),
        ("--contacts-oauth", "run_contacts_oauth"),
        ("--calendar-oauth", "run_calendar_oauth"),
        ("--tasks-oauth", "run_tasks_oauth"),
        ("--apps-script-oauth", "run_apps_script_oauth"),
        ("--slack-oauth", "run_slack_oauth"),
        ("--salesforce-oauth", "run_salesforce_oauth"),
        ("--atlassian-oauth", "run_atlassian_oauth"),
        ("--grist-oauth", "run_grist_oauth"),
        ("--miro-oauth", "run_miro_oauth"),
    ])
    def test_oauth_flag_dispatches_to_the_right_runner(self, monkeypatch, flag, runner_name):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, runner_name, lambda org_config, *a: calls.append(1) or 0)

        result = daemon_main.main([flag])

        assert result == 0
        assert calls == [1]

    def test_telegram_setup_flag_dispatches_with_no_org_config_arg(self, monkeypatch):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, "run_telegram_setup", lambda: calls.append(1) or 0)

        result = daemon_main.main(["--telegram-setup"])

        assert result == 0
        assert calls == [1]

    def test_peek_login_flag_dispatches_with_the_org_config(self, monkeypatch):
        self._patch_config(monkeypatch)
        seen = []
        monkeypatch.setattr(daemon_main, "run_peek_login", lambda org_config: seen.append(org_config) or 0)

        assert daemon_main.main(["--peek-login"]) == 0
        assert len(seen) == 1

    def test_no_oauth_flag_calls_run_app(self, monkeypatch):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: calls.append((config, path)) or 0)

        result = daemon_main.main([])

        assert result == 0
        assert len(calls) == 1

    def test_no_oauth_flag_triggers_the_separation_gate(self, monkeypatch):
        # ADR 0003 decision 6: fired only on the path that actually starts
        # the persistent daemon -- see the next test for why the one-shot
        # CLI flags below must not trigger it.
        self._patch_config(monkeypatch)
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: 0)
        calls = []
        monkeypatch.setattr(
            daemon_main.privilege_separation, "enforce_separation", lambda: calls.append(1)
        )

        result = daemon_main.main([])

        assert result == 0
        assert calls == [1]

    @pytest.mark.parametrize("flag", ["--gmail-oauth", "--telegram-setup", "--peek-login"])
    def test_oauth_and_telegram_flags_do_not_trigger_the_separation_gate(self, monkeypatch, flag):
        # A password-prompting admin/UAC/polkit dialog popping up during a
        # scripted, headless `--gmail-oauth` invocation would be a surprising
        # side effect of an unrelated flag, and there is nothing to refuse
        # yet on a one-shot command.
        self._patch_config(monkeypatch)
        monkeypatch.setattr(daemon_main, "run_gmail_oauth", lambda org_config: 0)
        monkeypatch.setattr(daemon_main, "run_telegram_setup", lambda: 0)
        monkeypatch.setattr(daemon_main, "run_peek_login", lambda org_config: 0)
        calls = []
        monkeypatch.setattr(
            daemon_main.privilege_separation, "enforce_separation", lambda: calls.append(1)
        )

        result = daemon_main.main([flag])

        assert result == 0
        assert calls == []

    def test_refuses_to_start_when_the_separation_gate_refuses(self, monkeypatch, capsys):
        # ADR 0003 decision 6: a packaged, unseparated install must not reach
        # run_app() at all -- see privilege_separation.enforce_separation()
        # for what triggers this in practice.
        self._patch_config(monkeypatch)
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: 0)

        def refuse() -> None:
            raise daemon_main.privilege_separation.PrivilegeSeparationError("not privilege-separated")

        monkeypatch.setattr(daemon_main.privilege_separation, "enforce_separation", refuse)

        result = daemon_main.main([])

        assert result == 1
        assert "not privilege-separated" in capsys.readouterr().err

    def test_hands_over_without_serving_when_the_gate_separates_the_install(self, monkeypatch, capsys):
        # The other way out of enforce_separation(), and the one that is not
        # a failure: decision 6's automatic `enable` took, so a service now
        # runs the daemon under an account this process is not. It must not
        # reach run_app() -- that is the silent policy reset
        # check_runtime_identity() exists to prevent, arriving after that
        # check has already run and passed -- and it must not exit non-zero
        # either, or Task Scheduler/launchd/systemd report a failed start for
        # the one outcome decision 6 is trying to reach.
        self._patch_config(monkeypatch)
        served = []
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: served.append(1) or 0)

        def hand_over() -> None:
            raise daemon_main.privilege_separation.SeparationHandover(
                "PrivacyFence has just separated this install"
            )

        monkeypatch.setattr(daemon_main.privilege_separation, "enforce_separation", hand_over)

        result = daemon_main.main([])

        assert result == 0
        assert served == []
        captured = capsys.readouterr()
        assert "has just separated this install" in captured.out
        assert "Configuration error" not in captured.err

    def test_logs_the_dev_unseparated_notice_when_present(self, monkeypatch, caplog):
        # ADR 0003 decision 7's startup-log half -- see privilege_separation.
        # dev_unseparated_notice()'s own docstring for when this is non-None.
        self._patch_config(monkeypatch)
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: 0)
        monkeypatch.setattr(daemon_main.privilege_separation, "enforce_separation", lambda: None)
        monkeypatch.setattr(
            daemon_main.privilege_separation, "dev_unseparated_notice",
            lambda: "PRIVACYFENCE_DEV_ALLOW_UNSEPARATED is set -- not protected",
        )

        with caplog.at_level(logging.WARNING):
            result = daemon_main.main([])

        assert result == 0
        assert "not protected" in caplog.text

    def test_no_log_line_when_the_dev_notice_is_none(self, monkeypatch, caplog):
        self._patch_config(monkeypatch)
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: 0)
        monkeypatch.setattr(daemon_main.privilege_separation, "enforce_separation", lambda: None)
        monkeypatch.setattr(daemon_main.privilege_separation, "dev_unseparated_notice", lambda: None)

        with caplog.at_level(logging.WARNING):
            daemon_main.main([])

        assert "not protected" not in caplog.text

    def test_fatal_exception_is_caught_prints_error_and_returns_1(self, monkeypatch, capsys):
        self._patch_config(monkeypatch)
        def raiser(config, path):
            raise RuntimeError("unexpected crash")
        monkeypatch.setattr(daemon_main, "run_app", raiser)

        result = daemon_main.main([])

        assert result == 1
        assert "Fatal error" in capsys.readouterr().err

    def test_refuses_to_start_as_the_wrong_account_on_a_separated_install(self, monkeypatch, capsys):
        # This runs before load_config(), and has to: on a
        # separated install started as the logged-in user, settings.yaml is
        # unreadable under the service-owned authority directory and
        # load_config()'s own first-run path would seed a fresh default over
        # it -- a silent policy reset rather than a visible failure.
        self._patch_config(monkeypatch)
        loaded = []
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: loaded.append(path) or {})
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: 0)

        def refuse() -> None:
            raise daemon_main.privilege_separation.PrivilegeSeparationError("wrong account")

        monkeypatch.setattr(daemon_main.privilege_separation, "check_runtime_identity", refuse)

        result = daemon_main.main([])

        assert result == 1
        assert "wrong account" in capsys.readouterr().err
        assert loaded == []


# ---------------------------------------------------------------------------- #
# _load_principal_settings
# ---------------------------------------------------------------------------- #

class TestLoadPrincipalSettings:
    """Org mode's per-principal ConnectorRegistry factory is the only thing
    that ever loads a non-local principal's settings.yaml, so it is also the
    only place that can make those settings *live* for that principal.

    Both assertions below are regressions: a non-local principal used to end
    up with config_path=None (fixed earlier, covered by the first test), and
    with a permanently empty AutoAcceptEvaluator regardless of what its own
    settings.yaml said (fixed here, covered by the rest).
    """

    @staticmethod
    def _seed(tmp_path, monkeypatch, principal_id: str, rules: dict, *, extra: dict | None = None) -> None:
        """``rules`` is a ``tests.helpers.policy_rules`` table, written as the ``auto_accept:``
        section; ``extra`` adds other top-level keys."""
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        config_dir = tmp_path / "users" / principal_id / "authority" / "config"
        config_dir.mkdir(parents=True)
        cfg = {policy_store.AUTO_ACCEPT_CONFIG_KEY: policy_store.rules_to_config(policy_rules(rules)), **(extra or {})}
        (config_dir / "settings.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def test_registers_the_principals_own_config_path(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept
        from privacyfence.policy.engine import PolicyRule
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings()
            # Would raise "auto_accept config path not initialized" without it.
            rule = PolicyRule(
                id="ignored-merge-recomputes-it", predicate="always_allow", value=None,
                operations=frozenset({"gmail.send_message"}),
            )
            assert auto_accept.add_policy_v2_rules([rule]) is True
            assert any(
                r.predicate == "always_allow" and "gmail.send_message" in r.operations
                for r in auto_accept.get_policy_v2_rules()
            )

    def test_seeds_the_evaluator_so_configured_rules_actually_apply(self, tmp_path, monkeypatch):
        """The real bug: settings.yaml on disk said contacts.edit had a rule,
        privacyfence_list_policy (get_policy_v2_rules, read from disk) agreed, but the hot-reloaded
        store gate.py and privacyfence_check_policy both consult (get_policy_v2_store_rules) had an
        empty rule set -- so the rule was silently inert and every call went to a human.
        """
        from privacyfence import auto_accept
        from privacyfence.policy.engine import preflight
        from privacyfence.principal import Principal, principal_scope

        self._seed(
            tmp_path, monkeypatch, "alice",
            {"contacts.edit": [{"predicate": "always_allow", "conditions": [("no_contact_info_change", None)]}]},
        )

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings()
            rules = auto_accept.get_policy_v2_store_rules()

            # A name-only edit matches the configured rule...
            verdict, matched_rule, _ = preflight(rules, "contacts.edit", {"display_name": "QA Contact"})
            assert verdict == "auto_accept"
            assert matched_rule

            # ...and the rule still discriminates: changing contact info does not.
            verdict, _, _ = preflight(rules, "contacts.edit", {"emails": ["qa@example.invalid"]})
            assert verdict == "requires_review"

    def test_one_principals_rules_do_not_leak_into_anothers_evaluator(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept
        from privacyfence.policy.engine import preflight
        from privacyfence.principal import Principal, principal_scope

        self._seed(
            tmp_path, monkeypatch, "alice",
            {"contacts.edit": [{"predicate": "always_allow", "conditions": [("no_contact_info_change", None)]}]},
        )
        self._seed(tmp_path, monkeypatch, "bob", {})

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings()
            alice_verdict, _, _ = preflight(
                auto_accept.get_policy_v2_store_rules(), "contacts.edit", {"display_name": "QA Contact"},
            )

        with principal_scope(Principal(id="bob")):
            daemon_main._load_principal_settings()
            bob_verdict, _, _ = preflight(
                auto_accept.get_policy_v2_store_rules(), "contacts.edit", {"display_name": "QA Contact"},
            )

        assert alice_verdict == "auto_accept"
        assert bob_verdict == "requires_review"

    def test_a_principals_v1_policy_section_is_refused_not_converted(self, tmp_path, monkeypatch):
        """An org principal's settings.yaml still carrying ``auto_accept_rules`` is refused with a
        message naming the section, and the file is left exactly as it was (ADR 0041)."""
        from privacyfence.principal import Principal, principal_scope

        self._seed(
            tmp_path, monkeypatch, "alice", {},
            extra={"auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]}},
        )

        with principal_scope(Principal(id="alice")):
            with pytest.raises(policy_store.V1PolicyConfigError, match="'auto_accept_rules'"):
                daemon_main._load_principal_settings()
            settings_path = daemon_main._resolve_authority_path("config/settings.yaml")

        with open(settings_path, encoding="utf-8") as fh:
            on_disk = yaml.safe_load(fh)
        assert "auto_accept_rules" in on_disk
        assert "migrated_to_policy_v2" not in on_disk

    def test_populates_the_privacy_filter_registry_for_the_principal(self, tmp_path, monkeypatch):
        """Without this, privacy_filter._REGISTRY would keep its default
        empty-dict entry for every principal but whichever one a local-mode
        run_app() happened to call init_privacy_filter() for --
        category_policy()'s own "group absent from the registry" fallback is
        a hardcoded "allow", the opposite of org mode's intended fail-closed
        "block" default. Every org principal must get a real registry entry,
        the same way _load_principal_settings() already makes auto-accept
        rules live for them."""
        from privacyfence import privacy_filter
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings()
            # No "privacy" section anywhere -- absent-group fail-safe default
            # must be "block" (org_managed=True), never "allow".
            assert privacy_filter.category_policy("privacy", "body") == "block"

    def test_uses_the_install_wide_config_when_given_not_the_principals_own_file(self, tmp_path, monkeypatch):
        """The install-wide PII/privacy policy is meant to be one server-
        controlled file, not each principal's own unconfigured per-user
        settings.yaml (docs/org-mode-setup-guide.md §9: "there is no
        per-user override") -- an admin's explicit override in the real
        server config must reach every principal, not just whichever one's
        own file happens to (never) carry a privacy section."""
        from privacyfence import privacy_filter
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})
        install_wide = {"privacy": {"default_policy": "allow"}}

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings(install_wide_config=install_wide)
            assert privacy_filter.category_policy("privacy", "body") == "allow"

    def test_two_principals_get_independent_privacy_filter_entries(self, tmp_path, monkeypatch):
        from privacyfence import privacy_filter
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})
        self._seed(tmp_path, monkeypatch, "bob", {})

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings(install_wide_config={"privacy": {"default_policy": "allow"}})
            alice_policy = privacy_filter.category_policy("privacy", "body")

        with principal_scope(Principal(id="bob")):
            daemon_main._load_principal_settings(install_wide_config={"privacy": {"default_policy": "block"}})
            bob_policy = privacy_filter.category_policy("privacy", "body")

        assert alice_policy == "allow"
        assert bob_policy == "block"

    def test_seeds_the_pii_gate_from_the_install_wide_config(self, tmp_path, monkeypatch):
        """The same hazard as the privacy-filter one above, in pii_detector.
        Its _REGISTRY is a PrincipalRegistry too and run_app() is the only
        caller of init_pii_detection(), so without seeding an org principal
        would get a default-constructed _PiiState -- detection on, both
        optional categories on -- regardless of what the install's
        settings.yaml said. Harmless on its own (that default detects more,
        not less), but not something a page that *edits* this value can
        ship on top of."""
        from privacyfence import pii_detector
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})
        install_wide = {"pii_detection": {"enabled": False}}

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings(install_wide_config=install_wide)
            assert pii_detector.is_pii_detection_enabled() is False

    def test_an_optional_pii_category_disabled_install_wide_reaches_a_principal(self, tmp_path, monkeypatch):
        from privacyfence import pii_detector
        from privacyfence.principal import Principal, principal_scope

        self._seed(tmp_path, monkeypatch, "alice", {})
        install_wide = {"pii_detection": {"enabled": True, "detect_ip_addresses": False}}

        with principal_scope(Principal(id="alice")):
            daemon_main._load_principal_settings(install_wide_config=install_wide)
            assert "IP address" not in pii_detector.detect_pii_categories("ping 10.1.2.3 please")


class TestPrintSignInLink:
    """The break-glass path: a sign-in link printed in the human's own
    terminal, never handed to the agent -- no MCP tool may mint this
    credential for the party it governs (ADR 0013).

    The confirmation dialog itself belongs to the companion process and is
    tested there (tests/unit/web/test_control_channel.py); what is exercised
    here is the command's own behavior around each of the three answers it
    can get.
    """

    @pytest.fixture
    def channel(self, monkeypatch):
        from privacyfence.web import control_channel

        monkeypatch.setattr(control_channel, "read_base_url", lambda: "http://127.0.0.1:8765")
        return control_channel

    def test_a_confirmed_link_is_printed_on_stdout_alone(self, channel, monkeypatch, capsys):
        monkeypatch.setattr(channel, "mint_console_bootstrap_code", lambda: "attested-code")

        assert daemon_main.run_print_sign_in_link() == 0

        captured = capsys.readouterr()
        # stdout is the URL and nothing else, so this can be piped straight
        # into a browser command.
        assert captured.out.strip() == "http://127.0.0.1:8765/approvals?bootstrap=attested-code"
        assert "consumed by the first visit" in captured.err

    def test_an_unconfirmed_link_still_prints_but_says_what_it_is(self, channel, monkeypatch, capsys):
        def _refused():
            raise channel.ControlChannelError("the sign-in link was denied")

        monkeypatch.setattr(channel, "mint_console_bootstrap_code", _refused)
        monkeypatch.setattr(channel, "mint_bootstrap_code", lambda: "unattested-code")

        assert daemon_main.run_print_sign_in_link() == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == "http://127.0.0.1:8765/approvals?bootstrap=unattested-code"
        # The reader is told at the terminal, not at the Approve button.
        assert "the sign-in link was denied" in captured.err
        assert "approving from it will be refused" in captured.err.lower()

    def test_no_daemon_running_prints_nothing_to_stdout(self, monkeypatch, capsys):
        from privacyfence.web import control_channel

        monkeypatch.setattr(control_channel, "read_base_url", lambda: None)

        assert daemon_main.run_print_sign_in_link() == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "does not appear to be running" in captured.err

    def test_an_unreachable_control_channel_is_an_error_not_a_traceback(self, channel, monkeypatch, capsys):
        def _boom():
            raise OSError("no such socket")

        monkeypatch.setattr(channel, "mint_console_bootstrap_code", _boom)

        assert daemon_main.run_print_sign_in_link() == 1
        assert capsys.readouterr().out == ""

    def test_a_fallback_that_also_fails_prints_nothing_to_stdout(self, channel, monkeypatch, capsys):
        def _refused():
            raise channel.ControlChannelError("no companion answered")

        def _also_refused():
            raise OSError("no such socket")

        monkeypatch.setattr(channel, "mint_console_bootstrap_code", _refused)
        monkeypatch.setattr(channel, "mint_bootstrap_code", _also_refused)

        assert daemon_main.run_print_sign_in_link() == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Could not mint a sign-in link at all" in captured.err

    def test_main_dispatches_before_the_runtime_identity_check(self, monkeypatch):
        """On a separated install ``check_runtime_identity()`` refuses the
        very account this command is meant to be run from -- it exists to
        stop the *daemon* starting as the wrong user. So the flag is handled
        ahead of it, and ahead of reading any config."""
        from privacyfence import privilege_separation

        def _refuse():
            raise privilege_separation.PrivilegeSeparationError("wrong account")

        monkeypatch.setattr(privilege_separation, "check_runtime_identity", _refuse)
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: pytest.fail("read config"))
        monkeypatch.setattr(daemon_main, "run_print_sign_in_link", lambda: 0)

        assert daemon_main.main(["--print-sign-in-link"]) == 0


class TestPrintMcpToken:
    """ADR 0008's own break-glass-style path for ``/mcp``: a direct HTTP
    client with no shim mints its own token the same way the ``.mcpb``
    shim does, as this OS account."""

    @pytest.fixture
    def channel(self, monkeypatch):
        from privacyfence.web import control_channel

        monkeypatch.setattr(control_channel, "read_base_url", lambda: "http://127.0.0.1:8765")
        return control_channel

    def test_a_minted_token_is_printed_on_stdout_alone(self, channel, monkeypatch, capsys):
        monkeypatch.setattr(channel, "mint_mcp_token", lambda: "sometoken")

        assert daemon_main.run_print_mcp_token() == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == "sometoken"

    def test_no_daemon_running_prints_nothing_to_stdout(self, monkeypatch, capsys):
        from privacyfence.web import control_channel

        monkeypatch.setattr(control_channel, "read_base_url", lambda: None)

        assert daemon_main.run_print_mcp_token() == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "does not appear to be running" in captured.err

    def test_an_unreachable_control_channel_is_an_error_not_a_traceback(self, channel, monkeypatch, capsys):
        def _boom():
            raise OSError("no such socket")

        monkeypatch.setattr(channel, "mint_mcp_token", _boom)

        assert daemon_main.run_print_mcp_token() == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Could not reach PrivacyFence's control channel" in captured.err

    def test_a_refused_mint_is_an_error_not_a_traceback(self, channel, monkeypatch, capsys):
        def _refused():
            raise channel.ControlChannelError("MCP tokens are not available on this install")

        monkeypatch.setattr(channel, "mint_mcp_token", _refused)

        assert daemon_main.run_print_mcp_token() == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Could not mint an MCP token" in captured.err

    def test_main_dispatches_before_the_runtime_identity_check(self, monkeypatch):
        from privacyfence import privilege_separation

        def _refuse():
            raise privilege_separation.PrivilegeSeparationError("wrong account")

        monkeypatch.setattr(privilege_separation, "check_runtime_identity", _refuse)
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: pytest.fail("read config"))
        monkeypatch.setattr(daemon_main, "run_print_mcp_token", lambda: 0)

        assert daemon_main.main(["--print-mcp-token"]) == 0



class TestRevokeScopedTokens:
    """``privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID`` (ADR 0165): the administrator takes
    back everything one person holds, from the server's command line."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        from privacyfence import paths, scoped_tokens
        from privacyfence.principal import Principal

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        alice = Principal(id="alice", email="alice@example.com")
        (tmp_path / "users" / "alice").mkdir(parents=True)
        made = scoped_tokens.ScopedTokenStore(lambda: [alice])
        policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=30)
        monkeypatch.setattr("privacyfence.auto_accept.get_policy_v2_store_rules", lambda: [])
        for label in ("one", "two"):
            made.create(alice, label=label, days=5, rules=[], tools=["gmail_get_message"], policy=policy)
        return made, alice

    def test_revoke_scoped_tokens_revokes_and_reports_the_count(self, store, tmp_path, capsys):
        from privacyfence import scoped_tokens

        made, alice = store
        assert daemon_main.run_revoke_scoped_tokens("alice") == 0
        assert capsys.readouterr().out == "Revoked 2 scoped token(s) for alice.\n"
        # The file is what a running daemon re-reads on the token's next use.
        fresh = scoped_tokens.ScopedTokenStore(lambda: [alice])
        fresh.load()
        assert fresh.tokens_for(alice) == []
        assert made.tokens_for(alice) == []
        assert daemon_main.run_revoke_scoped_tokens("alice") == 0
        assert capsys.readouterr().out == "Revoked 0 scoped token(s) for alice.\n"

    @pytest.mark.parametrize("principal_id", ["nobody", "../escape", "", "users"])
    def test_revoke_scoped_tokens_for_an_unknown_account_exits_1_and_creates_no_folder(
        self, store, tmp_path, capsys, principal_id,
    ):
        before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

        assert daemon_main.run_revoke_scoped_tokens(principal_id) == 1

        captured = capsys.readouterr()
        assert "No such account." in captured.err
        assert captured.out == ""
        assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before

    def test_revoke_scoped_tokens_runs_after_the_runtime_identity_check(self, monkeypatch):
        from privacyfence import privilege_separation

        calls: list[str] = []

        def _check():
            calls.append("identity")

        monkeypatch.setattr(privilege_separation, "check_runtime_identity", _check)
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: pytest.fail("read config"))
        monkeypatch.setattr(
            daemon_main, "run_revoke_scoped_tokens", lambda principal_id: calls.append(principal_id) or 0,
        )

        assert daemon_main.main(["--revoke-scoped-tokens", "alice"]) == 0
        assert calls == ["identity", "alice"]

        # On a separated install the check refuses the wrong account first, and nothing is revoked.
        def _refuse():
            raise privilege_separation.PrivilegeSeparationError("wrong account")

        calls.clear()
        monkeypatch.setattr(privilege_separation, "check_runtime_identity", _refuse)
        assert daemon_main.main(["--revoke-scoped-tokens", "alice"]) != 0
        assert calls == []

    def test_local_is_always_a_known_account(self, tmp_path, monkeypatch, capsys):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        assert daemon_main.run_revoke_scoped_tokens("local") == 0
        assert capsys.readouterr().out == "Revoked 0 scoped token(s) for local.\n"


class TestGristRelease:
    """``privacyfence-app --grist-release PRINCIPAL_ID``."""

    KEY_ENV = "PRIVACYFENCE_GRIST_PROVISIONING_KEY"

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from privacyfence import grist_provisioner_api, grist_provisioning, paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        monkeypatch.setenv(self.KEY_ENV, "prov-key")
        org_config = {
            "mode": "org",
            # Without an idp this would be single-user mode, where auto-provisioning is off.
            "idp": {"issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec"},
            "grist": {
                "server_url": "https://grist.example.com",
                "auto_provision": {"enabled": True, "provisioner_url": "https://prov.example.com"},
            },
        }
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: org_config)

        class FakeClient:
            result = grist_provisioner_api.GristRelease(released=True, revoked=True)
            error: Exception | None = None
            built_with: tuple = ()
            released: list[str] = []

            def __init__(self, base_url, key):
                FakeClient.built_with = (base_url, key)

            def release_account(self, external_id):
                FakeClient.released.append(external_id)
                if FakeClient.error is not None:
                    raise FakeClient.error
                return FakeClient.result

        FakeClient.released = []
        FakeClient.error = None
        monkeypatch.setattr(grist_provisioner_api, "GristProvisionerClient", FakeClient)
        monkeypatch.setattr(grist_provisioning, "release", lambda pid: None)
        return FakeClient, org_config

    def test_unknown_account(self, env, capsys):
        client, _ = env
        for principal_id in ("nobody", "../escape", ""):
            assert daemon_main.run_grist_release(principal_id) == 1
        captured = capsys.readouterr()
        assert captured.err.count("No such account.") == 3
        assert captured.out == ""
        assert client.released == []

    def test_local_is_refused(self, env, tmp_path, capsys):
        (tmp_path / "users" / "local").mkdir(parents=True)
        assert daemon_main.run_grist_release("local") == 1
        assert "No such account." in capsys.readouterr().err

    def test_configuration_error_is_printed(self, env, monkeypatch, capsys):
        def _broken():
            raise daemon_main.org_mode.ConfigurationError("org_config.json is broken")

        monkeypatch.setattr(daemon_main, "load_org_config", _broken)
        assert daemon_main.run_grist_release("alice") == 1
        assert "org_config.json is broken" in capsys.readouterr().err

    def test_not_enabled(self, env, monkeypatch, capsys):
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mode": "org"})
        assert daemon_main.run_grist_release("alice") == 1
        assert capsys.readouterr().err == "Grist auto-provisioning is not enabled in the organization config.\n"

    def test_missing_key(self, env, monkeypatch, capsys):
        monkeypatch.setenv(self.KEY_ENV, "  ")
        assert daemon_main.run_grist_release("alice") == 1
        assert capsys.readouterr().err == f"Set {self.KEY_ENV} to the Grist provisioning key first.\n"
        assert env[0].released == []

    def test_revoked_message(self, env, capsys):
        client, _ = env
        assert daemon_main.run_grist_release("alice") == 0
        captured = capsys.readouterr()
        assert captured.out == (
            "Released the Grist connection of alice. The Grist API key PrivacyFence was given is deleted in Grist.\n"
        )
        assert captured.err == ""
        assert client.built_with == ("https://prov.example.com", "prov-key")
        assert client.released == ["alice"]

    def test_own_key_message(self, env, capsys):
        from privacyfence.grist_provisioner_api import GristRelease

        env[0].result = GristRelease(released=True, revoked=False)
        assert daemon_main.run_grist_release("alice") == 0
        assert capsys.readouterr().out == (
            "Released the Grist connection of alice. Its Grist API key was the person's own, or was already "
            "replaced, and stays; delete it in Grist or disable the account there.\n"
        )

    def test_no_link_message(self, env, capsys):
        from privacyfence.grist_provisioner_api import GristRelease

        env[0].result = GristRelease(released=False, revoked=False)
        assert daemon_main.run_grist_release("alice") == 0
        assert capsys.readouterr().out == (
            "Released the Grist connection of alice. The Grist provisioner had no link for it.\n"
        )

    def test_client_error_exits_1(self, env, capsys):
        from privacyfence.grist_provisioner_api import GristProvisionerClientError

        env[0].error = GristProvisionerClientError("The Grist provisioner is unreachable.")
        assert daemon_main.run_grist_release("alice") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("The Grist provisioner is unreachable.")
        assert captured.err.rstrip().endswith("run --grist-release again to delete it in Grist.")

    def test_release_runs_before_the_provisioner_call(self, env, monkeypatch):
        from privacyfence import grist_provisioning

        client, _ = env
        order: list[str] = []
        monkeypatch.setattr(grist_provisioning, "release", lambda pid: order.append(f"release {pid}"))
        original = client.release_account
        monkeypatch.setattr(
            client, "release_account", lambda self, external_id: order.append("provisioner") or original(self, external_id),
        )

        assert daemon_main.run_grist_release("alice") == 0
        assert order == ["release alice", "provisioner"]

    def test_runs_after_the_runtime_identity_check(self, monkeypatch):
        from privacyfence import privilege_separation

        calls: list[str] = []
        monkeypatch.setattr(privilege_separation, "check_runtime_identity", lambda: calls.append("identity"))
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: pytest.fail("read config"))
        monkeypatch.setattr(daemon_main, "run_grist_release", lambda pid: calls.append(pid) or 0)

        assert daemon_main.main(["--grist-release", "alice"]) == 0
        assert calls == ["identity", "alice"]


class TestLoadOrgConfigSerial:
    """SEC-A7: the serial pin is wired into every load_org_config() call."""

    def test_load_org_config_rejects_older_signed_bundle(self, tmp_path, monkeypatch):
        from privacyfence import org_bundle_signing

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        key = _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}, "serial": 200})
        daemon_main.load_org_config()

        _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}, "serial": 100}, key)

        with pytest.raises(org_mode.ConfigurationError) as excinfo:
            daemon_main.load_org_config()
        assert "rollback refused" in str(excinfo.value)
        assert str(org_bundle_signing.pinned_serial_path(tmp_path)) in str(excinfo.value)

    def test_load_org_config_records_the_highest_serial(self, tmp_path, monkeypatch):
        from privacyfence import org_bundle_signing

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        key = _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}, "serial": 100})
        daemon_main.load_org_config()
        assert org_bundle_signing.load_pinned_serial(tmp_path) == 100

        _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}, "serial": 300}, key)
        daemon_main.load_org_config()

        assert org_bundle_signing.load_pinned_serial(tmp_path) == 300

    def test_load_org_config_refuses_an_invalid_serial_without_a_remedy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        _write_signed_bundle(tmp_path, {"slack": {"client_id": "abc"}, "serial": 0})

        with pytest.raises(org_mode.ConfigurationError) as excinfo:
            daemon_main.load_org_config()
        assert str(excinfo.value).endswith("(serial is not a positive integer) -- refusing to start.")


class TestPersonalDataSweep:
    """The sweep erases Atlassian user-cache entries that missed their report (ADR 0202) for every
    principal under data_dir(), at startup and then on each interval, until shutdown."""

    def test_sweeps_data_dir_until_shutdown(self, monkeypatch):
        roots = []

        def sweep(root):
            roots.append(root)
            if len(roots) == 2:
                daemon_main._shutdown_event.set()
            return 0

        monkeypatch.setattr(daemon_main, "erase_overdue_caches", sweep)
        monkeypatch.setattr(daemon_main, "PERSONAL_DATA_SWEEP_INTERVAL_SECONDS", 0)
        daemon_main._run_personal_data_sweep()
        assert roots == [data_dir(), data_dir()]

    def test_failure_is_logged_and_retried(self, monkeypatch, caplog):
        def sweep(root):
            daemon_main._shutdown_event.set()
            raise OSError("permission denied")

        monkeypatch.setattr(daemon_main, "erase_overdue_caches", sweep)
        with caplog.at_level(logging.WARNING):
            daemon_main._run_personal_data_sweep()
        assert "personal data sweep failed" in caplog.text


class TestLoadOrgConfigAdoption:
    """7.x reads org/org_bundle.json and adopts a 6.x org_config.json by copying it,
    leaving every 6.x file untouched so a downgrade keeps working."""

    @staticmethod
    def _legacy_files(tmp_path):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir() if p.name.startswith("org_config")}

    def test_a_data_dir_with_only_org_config_json_loads_through_adoption(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text(json.dumps({"slack": {"client_id": "abc"}}))

        assert daemon_main.load_org_config() == {"slack": {"client_id": "abc"}}
        assert (tmp_path / "org_bundle.json").exists()
        assert daemon_main._bundle_adopted_this_start is True

    def test_the_flag_stays_true_across_later_loads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text("{}")
        daemon_main.load_org_config()
        daemon_main.load_org_config()
        assert daemon_main._bundle_adopted_this_start is True

    def test_adoption_is_audited_once(self, tmp_path, monkeypatch):
        from privacyfence.audit_log import init_audit_logger

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text("{}")
        init_audit_logger(str(tmp_path / "audit"))
        daemon_main.load_org_config()
        daemon_main._audit_org_bundle_adopted()

        entries = [
            json.loads(line)
            for f in (tmp_path / "audit").glob("*.jsonl")
            for line in f.read_text().splitlines()
        ]
        assert [e["decision"] for e in entries] == ["org_bundle_adopted"]

    def test_run_app_audits_adoption_right_after_the_bundle_hash(self):
        import inspect

        source = inspect.getsource(daemon_main.run_app)
        assert source.index("log_org_config_bundle_hash(bundle)") < source.index("_audit_org_bundle_adopted()")
        assert "if _bundle_adopted_this_start:" in source

    def test_nothing_adopted_leaves_the_flag_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_bundle.json").write_text("{}")
        daemon_main.load_org_config()
        assert daemon_main._bundle_adopted_this_start is False

    def test_a_changed_org_config_json_after_adoption_warns_and_is_not_read(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text(json.dumps({"slack": {"client_id": "old"}}))
        daemon_main.load_org_config()
        caplog.clear()

        (tmp_path / "org_config.json").write_text(json.dumps({"slack": {"client_id": "new"}}))
        with caplog.at_level(logging.WARNING):
            assert daemon_main.load_org_config() == {"slack": {"client_id": "old"}}
        assert "org/org_config.json changed after the upgrade and is ignored" in caplog.text

    def test_an_unchanged_org_config_json_does_not_warn(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text("{}")
        daemon_main.load_org_config()
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            daemon_main.load_org_config()
        assert "changed after the upgrade" not in caplog.text

    def test_a_7x_bundle_install_leaves_the_6x_files_for_a_downgrade(self, tmp_path, monkeypatch):
        from privacyfence import org_bundle_signing

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        private_key, _ = org_bundle_signing.generate_keypair()
        old = org_bundle_signing.sign_bundle({"serial": 1, "slack": {"client_id": "a"}}, private_key)
        (tmp_path / "org_config.json").write_text(json.dumps(old))
        (tmp_path / "org_config_signing_pubkey.txt").write_text(old["signing_public_key"] + "\n")
        (tmp_path / "org_config_serial.txt").write_text("1\n")
        before = self._legacy_files(tmp_path)

        daemon_main.load_org_config()
        # What the Admin page / Settings upload does: install a newer bundle and advance the serial.
        newer = org_bundle_signing.sign_bundle({"serial": 5, "slack": {"client_id": "b"}}, private_key)
        (tmp_path / "org_bundle.json").write_text(json.dumps(newer))
        assert daemon_main.load_org_config()["serial"] == 5

        assert (tmp_path / "org_bundle_serial.txt").read_text().strip() == "5"
        assert self._legacy_files(tmp_path) == before
        assert (tmp_path / "org_config_serial.txt").read_text() == "1\n"


# ---------------------------------------------------------------------------- #
# run_app: the admin config import and the effective org config
# ---------------------------------------------------------------------------- #

def _patch_run_app(monkeypatch, bundle, *, audit=None):
    """Stub everything run_app() touches except the admin config and the effective config."""
    monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
    monkeypatch.setattr(daemon_main, "init_config_path", lambda path: None)
    monkeypatch.setattr(daemon_main, "init_audit_logger", lambda path, **kwargs: MagicMock())
    monkeypatch.setattr(daemon_main, "load_org_config", lambda: bundle)
    monkeypatch.setattr(daemon_main, "check_storage_permissions", lambda org_active: None)
    seen: dict[str, Any] = {}

    def fake_build_connectors(config, org_config):
        seen["org_config"] = org_config
        return [], {}

    monkeypatch.setattr(daemon_main, "build_connectors", fake_build_connectors)
    monkeypatch.setattr(daemon_main, "_wait_for_shutdown", lambda: None)
    monkeypatch.setattr(daemon_main, "_run_update_check_timer", lambda controller: None)
    monkeypatch.setattr(daemon_main, "_run_personal_data_sweep", lambda: None)
    monkeypatch.setattr(
        daemon_main, "_maybe_start_web_server", lambda config, host, **kwargs: seen.update(web=kwargs)
    )
    if audit is not None:
        monkeypatch.setattr(daemon_main, "get_audit_logger", lambda: audit)
    return seen


class _RecordingAudit:
    def __init__(self, events: list):
        self.events = events

    def record(self, entry):
        self.events.append(entry.decision)


class TestAdminConfigStartup:
    BUNDLE = {
        "slack": {"client_id": "s", "client_secret": "x"},
        "authz": {"allowed_domains": ["example.com"]},
    }

    def test_first_start_imports_and_audits_after_the_bundle_hash(self, monkeypatch, tmp_path, caplog):
        events: list = []
        _patch_run_app(monkeypatch, self.BUNDLE, audit=_RecordingAudit(events))
        monkeypatch.setattr(daemon_main, "log_org_config_bundle_hash", lambda b: events.append("bundle_hash"))

        with caplog.at_level(logging.WARNING):
            assert daemon_main.run_app({}, "config.yaml") == 0

        stored = json.loads((tmp_path / "admin_config.json").read_text())
        assert {name for name, row in stored["services"].items() if row["offered"]} == {"slack"}
        assert stored["authz"] == {"allowed_domains": ["example.com"]}
        assert "Imported 1 settings from org_config.json" in caplog.text
        assert events == ["bundle_hash", "admin_config_imported"]

    def test_second_start_does_not_import_again_and_says_the_settings_are_ignored(
        self, monkeypatch, tmp_path, caplog
    ):
        events: list = []
        _patch_run_app(monkeypatch, self.BUNDLE, audit=_RecordingAudit(events))
        daemon_main.run_app({}, "config.yaml")
        stored = (tmp_path / "admin_config.json").read_text()
        events.clear()
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            daemon_main.run_app({}, "config.yaml")

        assert (tmp_path / "admin_config.json").read_text() == stored
        assert "Imported" not in caplog.text
        assert "are now managed on the Admin page and are ignored: authz" in caplog.text
        assert "admin_config_imported" not in events

    def test_a_bundle_without_legacy_settings_logs_neither_notice(self, monkeypatch, caplog):
        _patch_run_app(monkeypatch, {})
        daemon_main.run_app({}, "config.yaml")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            daemon_main.run_app({}, "config.yaml")
        assert "are ignored" not in caplog.text

    def test_a_malformed_admin_config_refuses_to_start(self, monkeypatch, tmp_path):
        _patch_run_app(monkeypatch, {})
        (tmp_path / "admin_config.json").write_text("{not json")

        with pytest.raises(org_mode.ConfigurationError, match="admin_config.json"):
            daemon_main.run_app({}, "config.yaml")

    def test_record_admin_config_import_hashes_the_bundle(self, monkeypatch):
        import hashlib

        recorded: list = []
        monkeypatch.setattr(daemon_main, "get_audit_logger", lambda: SimpleNamespace(record=recorded.append))
        daemon_main.record_admin_config_import(self.BUNDLE, False)
        assert recorded == []

        daemon_main.record_admin_config_import(self.BUNDLE, True)

        digest = hashlib.sha256(json.dumps(self.BUNDLE, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert [e.decision for e in recorded] == ["admin_config_imported"]
        assert digest in recorded[0].summary

    def test_record_admin_config_import_survives_an_audit_failure(self, monkeypatch, caplog):
        def broken():
            raise RuntimeError("disk full")

        monkeypatch.setattr(daemon_main, "get_audit_logger", broken)
        with caplog.at_level(logging.WARNING):
            daemon_main.record_admin_config_import({}, True)
        assert "Audit log write failed for the admin config import" in caplog.text


class TestLoadAdminConfigSingleUser:
    def test_a_single_user_install_gets_the_single_user_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(admin_config, "admin_config_path", lambda: tmp_path / "admin_config.json")
        admin, imported = daemon_main.load_admin_config({"mode": "org"})
        assert admin["step_up"] == {"enabled": True, "require_passkey": True, "scope": "writes_and_pii_reads"}
        assert imported is True

    def test_an_organization_server_does_not(self, tmp_path, monkeypatch):
        monkeypatch.setattr(admin_config, "admin_config_path", lambda: tmp_path / "admin_config.json")
        idp = {"issuer": "https://idp.example.com", "client_id": "c", "client_secret": "s"}
        admin, _ = daemon_main.load_admin_config({"mode": "org", "idp": idp})
        assert admin["step_up"] != {"enabled": True, "require_passkey": True, "scope": "writes_and_pii_reads"}


class TestEffectiveOrgConfigWiring:
    # An organization bundle with no idp is single-user mode, where auto-provisioning is off.
    IDP = {"issuer": "https://idp.example.com", "client_id": "c", "client_secret": "s"}
    ORG_BUNDLE = {
        "mode": "org",
        "server": {"issuer_url": "https://pf.example.com"},
        "idp": {"issuer": "https://idp.example.com", "client_id": "c", "client_secret": "s"},
    }

    @staticmethod
    def _builtin_slack(monkeypatch):
        monkeypatch.setattr(
            app_credentials, "builtin_app",
            lambda key: {"client_id": "builtin", "client_secret": "secret"} if key == "slack" else None,
        )

    @staticmethod
    def _offering_slack(bundle, mode):
        admin = admin_config.import_from_bundle({}, mode=mode, single_user=False)
        admin["services"]["slack"]["offered"] = True
        return admin

    def test_the_bundle_hash_is_logged_from_the_raw_bundle(self, monkeypatch):
        bundle = {"slack": {"client_id": "s"}, "authz": {}}
        seen = _patch_run_app(monkeypatch, bundle)
        hashed: list = []
        monkeypatch.setattr(daemon_main, "log_org_config_bundle_hash", hashed.append)

        daemon_main.run_app({}, "config.yaml")

        assert hashed == [bundle]
        assert hashed[0] is bundle
        # The effective config is what every other reader gets, and it is a different object.
        assert seen["org_config"] is not bundle
        assert "unattended_sessions" in seen["org_config"] and "unattended_sessions" not in bundle

    def test_run_app_hands_the_bundle_and_startup_admin_to_the_web_server(self, monkeypatch):
        bundle = {"slack": {"client_id": "s"}}
        seen = _patch_run_app(monkeypatch, bundle)

        daemon_main.run_app({}, "config.yaml")

        assert seen["web"]["bundle"] is bundle
        assert seen["web"]["startup_admin"]["services"]["slack"]["offered"] is True
        assert seen["web"]["org_config"] is seen["org_config"]

    def test_org_mode_reaches_the_slack_branch_through_a_builtin_app(self, monkeypatch):
        self._builtin_slack(monkeypatch)
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        org_config = daemon_main.effective_org_config(self.ORG_BUNDLE, self._offering_slack(self.ORG_BUNDLE, "org"))
        assert org_config["slack"]["relay"] is True

        connectors, _failures = daemon_main.build_connectors({}, org_config)

        assert [c.name for c in connectors] == ["slack"]

    def test_local_mode_does_not_use_a_builtin_app(self, monkeypatch):
        self._builtin_slack(monkeypatch)
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        org_config = daemon_main.effective_org_config({}, self._offering_slack({}, "local"))
        assert "slack" not in org_config

        connectors, _failures = daemon_main.build_connectors({}, org_config)

        assert connectors == []
        assert fake.instantiated is False

    def test_the_cli_form_never_uses_a_builtin_app_even_in_an_org_bundle(self, monkeypatch):
        self._builtin_slack(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: self.ORG_BUNDLE)
        admin_config.save(self._offering_slack(self.ORG_BUNDLE, "org"))
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: {})
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: None)
        seen: list = []
        monkeypatch.setattr(daemon_main, "run_slack_oauth", lambda org_config: seen.append(org_config) or 0)

        assert daemon_main.main(["--slack-oauth"]) == 0

        assert "slack" not in seen[0]

    def test_a_malformed_admin_config_makes_the_cli_exit_non_zero(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
        (tmp_path / "admin_config.json").write_text("[]")
        monkeypatch.setattr(daemon_main, "load_config", lambda path, **kwargs: {})
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: None)

        assert daemon_main.main(["--slack-oauth"]) == 1
        assert "admin_config.json" in capsys.readouterr().err

    def test_the_org_web_server_keeps_the_bundle_and_startup_admin_on_org_auth(self, monkeypatch, tmp_path):
        from privacyfence import paths
        from privacyfence.connector_host import ConnectorHost
        from privacyfence.web.server import WebServer

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(WebServer, "start", lambda self: None)
        monkeypatch.setattr(
            "privacyfence.org_identity.discover_idp",
            lambda issuer: {
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/jwks",
            },
        )
        bundle = dict(self.ORG_BUNDLE, authz={})
        admin = admin_config.import_from_bundle(bundle, mode="org", single_user=False)
        org_config = daemon_main.effective_org_config(bundle, admin)

        server = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, ConnectorHost([]), unattended_sessions_enabled=False,
            org_config=org_config, bundle=bundle, startup_admin=admin,
        )

        assert server.org.bundle is bundle
        assert server.org.startup_admin is admin
        assert server.org.org_config is org_config

    def test_a_withdrawn_peek_starts_no_provisioner(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://peek.example.com")
        monkeypatch.setenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", "key")
        bundle = {"mode": "org", "idp": self.IDP}
        admin = admin_config.import_from_bundle(bundle, mode="org", single_user=False)
        admin["services"]["peek"]["offered"] = True
        admin["services"]["peek"]["auto_provision"]["enabled"] = True
        started = []
        monkeypatch.setattr(
            daemon_main.peek_provisioning, "PeekProvisioner", lambda **kw: SimpleNamespace(start=lambda: started.append(kw))
        )

        assert daemon_main.peek_provisioning.start(
            daemon_main.effective_org_config(bundle, admin), on_change=lambda p: None, stop_event=threading.Event()
        ) is not None
        assert len(started) == 1

        started.clear()
        admin["services"]["peek"]["offered"] = False
        withdrawn = daemon_main.effective_org_config(bundle, admin)
        assert "peek" not in withdrawn
        assert daemon_main.peek_provisioning.start(
            withdrawn, on_change=lambda p: None, stop_event=threading.Event()
        ) is None
        assert started == []

    def test_a_malformed_admin_config_makes_the_release_command_print_it_and_fail(
        self, monkeypatch, tmp_path, capsys
    ):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"mode": "org"})
        (tmp_path / "admin_config.json").write_text("{nope")

        assert daemon_main.run_peek_release("alice") == 1
        assert "admin_config.json" in capsys.readouterr().err

    def test_peek_release_reads_the_switch_from_the_admin_config_and_the_address_from_the_environment(
        self, monkeypatch, tmp_path, capsys
    ):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(daemon_main, "data_dir", lambda: tmp_path)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://peek.example.com")
        monkeypatch.setenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", "peekprov_secret")
        bundle = {"mode": "org", "idp": self.IDP}
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: bundle)
        admin = admin_config.import_from_bundle(bundle, mode="org", single_user=False)
        admin["services"]["peek"]["offered"] = True
        admin["services"]["peek"]["auto_provision"]["enabled"] = True
        admin_config.save(admin)
        clients: list = []

        class Client:
            def __init__(self, base_url, key):
                clients.append((base_url, key))

            def release_account(self, principal_id):
                return True

        monkeypatch.setattr(daemon_main, "PeekProvisioningClient", Client)
        monkeypatch.setattr(daemon_main.peek_provisioning, "release", lambda principal_id: None)

        assert daemon_main.run_peek_release("alice") == 0
        assert clients == [("https://peek.example.com", "peekprov_secret")]
        assert "Released the Peek connection of alice" in capsys.readouterr().out
