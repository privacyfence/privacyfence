"""The admin config store: schema, load/save and the one-time import from a bundle."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import stat
import sys

import pytest

from privacyfence import admin_config, connector_configs, paths

ALL_SERVICES = list(admin_config.SERVICES)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    # This module is exempt from the conftest isolation fixture so the real path functions run.
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _write(tmp_path, content):
    path = tmp_path / "org" / "admin_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def _offered(config):
    return sorted(name for name, service in config["services"].items() if service["offered"])


def _import(bundle, *, mode="org", single_user=False):
    return admin_config.import_from_bundle(bundle, mode=mode, single_user=single_user)


class TestLoadAndSave:
    def test_path_is_in_the_org_dir(self, tmp_path):
        assert admin_config.admin_config_path() == tmp_path / "org" / "admin_config.json"

    def test_missing_file_loads_as_none(self):
        assert admin_config.load() is None

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[1, 2]", '"text"', "{}", '{"version": 2}', '{"version": "1"}', '{"version": true}'],
    )
    def test_bad_file_raises_without_its_content(self, tmp_path, content):
        path = _write(tmp_path, content)
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            admin_config.load()
        assert str(path) in str(excinfo.value)
        assert content not in str(excinfo.value)

    def test_secret_looking_content_never_reaches_the_message(self, tmp_path):
        _write(tmp_path, '{"version": 1, "secret": SUPERSECRET')
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            admin_config.load()
        assert "SUPERSECRET" not in str(excinfo.value)

    def test_unreadable_path_raises(self, tmp_path):
        (tmp_path / "org" / "admin_config.json").mkdir(parents=True)
        with pytest.raises(admin_config.AdminConfigError, match="could not be read"):
            admin_config.load()

    def test_normalization_fills_missing_services_and_keys(self, tmp_path):
        _write(
            tmp_path,
            {
                "version": 1,
                "services": {"slack": {"offered": True}, "atlassian": {"offered": True}, "miro": "oops"},
                "unattended_sessions": "oops",
                "org_server": True,
            },
        )
        loaded = admin_config.load()
        assert loaded is not None
        assert set(loaded["services"]) == set(admin_config.SERVICES)
        assert loaded["services"]["slack"] == {"offered": True}
        assert loaded["services"]["atlassian"] == {"offered": True, "assets": False, "assets_workspace_id": ""}
        assert loaded["services"]["miro"] == {"offered": False}
        assert loaded["services"]["peek"]["base_url"] == ""
        assert loaded["services"]["peek"]["auto_provision"]["key_env"] == "PRIVACYFENCE_PEEK_PROVISIONING_KEY"
        assert loaded["services"]["grist"]["auto_provision"]["provisioner_url"] == ""
        assert loaded["unattended_sessions"] == {"enabled": False}
        assert loaded["imported_from_bundle_sha256"] == ""
        assert loaded["rooms"] == [] and loaded["authz"] == {}
        assert loaded["org_server"] is True

    def test_normalization_fills_nested_keys(self, tmp_path):
        _write(tmp_path, {"version": 1, "services": {"grist": {"auto_provision": {"enabled": True}}}})
        loaded = admin_config.load()
        assert loaded["services"]["grist"]["auto_provision"] == {
            "enabled": True,
            "trust_unverified_email": False,
            "key_env": "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
            "provisioner_url": "",
        }

    def test_a_non_object_services_value_is_replaced(self, tmp_path):
        _write(tmp_path, {"version": 1, "services": [1]})
        loaded = admin_config.load()
        assert all(not service["offered"] for service in loaded["services"].values())

    def test_save_round_trips(self, tmp_path):
        config = _import({"slack": {"client_id": "x"}})
        admin_config.save(config)
        assert admin_config.load() == config

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)"
    )
    def test_saved_file_is_private(self, tmp_path):
        admin_config.save(_import({}))
        mode = stat.S_IMODE(os.stat(tmp_path / "org" / "admin_config.json").st_mode)
        assert mode == 0o600


class TestImportOffered:
    def test_empty_bundle_single_user_org_offers_the_builtin_services(self):
        config = _import({}, single_user=True)
        assert _offered(config) == sorted(["google", "slack", "salesforce", "atlassian", "miro", "telegram"])

    def test_empty_bundle_local_mode_offers_only_telegram(self):
        assert _offered(_import({}, mode="local")) == ["telegram"]
        assert _offered(_import({}, mode="local", single_user=True)) == ["telegram"]

    def test_empty_bundle_multi_user_org_offers_nothing(self):
        assert _offered(_import({})) == []

    @pytest.mark.parametrize("single_user", [False, True])
    def test_grist_and_peek_are_never_offered_from_an_empty_bundle(self, single_user):
        config = _import({}, single_user=single_user)
        assert not config["services"]["grist"]["offered"]
        assert not config["services"]["peek"]["offered"]

    @pytest.mark.parametrize("mode", ["local", "org"])
    def test_bundle_sections_decide_what_is_offered(self, mode):
        config = _import(
            {"slack": {"client_id": "a", "client_secret": "b"}, "peek": {"base_url": "https://peek.example.com"}},
            mode=mode,
            single_user=True,
        )
        assert _offered(config) == ["peek", "slack"]
        assert config["services"]["peek"]["base_url"] == "https://peek.example.com"

    def test_empty_and_non_dict_sections_do_not_count(self):
        config = _import({"google": {}, "slack": "oops", "telegram": {"enabled": True}}, mode="local")
        assert _offered(config) == ["telegram"]


class TestImportCopies:
    def test_atlassian_assets_are_copied(self):
        config = _import({"atlassian": {"client_id": "a", "assets": True, "assets_workspace_id": "ws-1"}})
        assert config["services"]["atlassian"] == {"offered": True, "assets": True, "assets_workspace_id": "ws-1"}

    def test_atlassian_without_assets_keeps_defaults(self):
        config = _import({"atlassian": {"client_id": "a"}})
        assert config["services"]["atlassian"] == {"offered": True, "assets": False, "assets_workspace_id": ""}

    def test_grist_server_url_is_copied_without_a_client_id(self):
        config = _import({"grist": {"server_url": "https://grist.example.com"}})
        assert config["services"]["grist"]["server_url"] == "https://grist.example.com"
        assert config["services"]["grist"]["offered"]

    def test_grist_server_url_is_not_copied_with_a_client_id(self):
        config = _import({"grist": {"client_id": "c", "server_url": "https://grist.example.com"}})
        assert config["services"]["grist"]["server_url"] == ""
        assert config["services"]["grist"]["offered"]

    def test_every_operational_section_is_copied_verbatim(self):
        bundle = {
            "rooms": [{"id": "r1"}],
            "rooms_synced_at": "2026-10-01T00:00:00Z",
            "unattended_sessions": {"enabled": True},
            "download_delivery": {"mode": "x"},
            "web_push": {"enabled": False},
            "authz": {"allowed_domains": ["example.com"]},
            "step_up": {"enabled": True, "scope": "writes"},
            "scoped_tokens": {"max_ttl": 5},
            "audit_forwarding": {"http": {"bearer_token_env": "TOKEN_VAR"}},
        }
        config = _import(copy.deepcopy(bundle))
        for key in admin_config.OPERATIONAL_SECTIONS:
            assert config[key] == bundle[key]

    def test_the_copy_is_independent_of_the_bundle(self):
        bundle = {"rooms": [{"id": "r1"}]}
        config = _import(bundle)
        config["rooms"][0]["id"] = "changed"
        assert bundle["rooms"][0]["id"] == "r1"

    def test_hash_is_the_documented_sha256(self):
        bundle = {"slack": {"client_id": "a"}, "authz": {"x": 1}}
        expected = hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert _import(bundle)["imported_from_bundle_sha256"] == expected

    def test_hash_of_an_empty_bundle_is_empty(self):
        assert _import({})["imported_from_bundle_sha256"] == ""


class TestSingleUserStepUp:
    def test_single_user_defaults_to_the_packaged_local_step_up(self):
        assert _import({}, single_user=True)["step_up"] == {
            "enabled": True,
            "require_passkey": True,
            "scope": "writes_and_pii_reads",
        }

    def test_a_bundle_step_up_wins_over_the_default(self):
        assert _import({"step_up": {"enabled": False}}, single_user=True)["step_up"] == {"enabled": False}

    def test_other_installs_have_no_step_up_default(self):
        assert _import({})["step_up"] == {}
        assert _import({}, mode="local")["step_up"] == {}

    def test_the_default_is_not_shared_between_configs(self):
        first = _import({}, single_user=True)
        first["step_up"]["scope"] = "writes"
        assert _import({}, single_user=True)["step_up"]["scope"] == "writes_and_pii_reads"


class TestAutoProvisionImport:
    def test_peek_settings_are_copied_and_the_address_locked_when_enabled(self):
        config = _import(
            {
                "peek": {
                    "base_url": "https://peek.example.com",
                    "auto_provision": {"enabled": True, "trust_unverified_email": True, "key_env": "MY_KEY"},
                }
            }
        )
        peek = config["services"]["peek"]
        assert peek["base_url"] == "https://peek.example.com"
        assert peek["address_locked"] is True
        assert peek["auto_provision"] == {"enabled": True, "trust_unverified_email": True, "key_env": "MY_KEY"}

    def test_a_default_or_blank_key_env_keeps_the_default(self):
        for raw in ({"enabled": True}, {"enabled": True, "key_env": ""}, {"enabled": True, "key_env": 5}):
            config = _import({"peek": {"base_url": "https://p.example.com", "auto_provision": raw}})
            assert config["services"]["peek"]["auto_provision"]["key_env"] == "PRIVACYFENCE_PEEK_PROVISIONING_KEY"

    def test_disabled_provisioning_copies_the_flags_but_does_not_lock(self):
        config = _import(
            {"peek": {"base_url": "https://p.example.com", "auto_provision": {"enabled": False, "trust_unverified_email": True}}}
        )
        peek = config["services"]["peek"]
        assert peek["address_locked"] is False
        assert peek["auto_provision"]["trust_unverified_email"] is True
        assert peek["auto_provision"]["enabled"] is False

    def test_enabled_without_an_address_does_not_lock(self):
        config = _import({"peek": {"auto_provision": {"enabled": True}}})
        assert config["services"]["peek"]["address_locked"] is False
        assert config["services"]["peek"]["auto_provision"]["enabled"] is True

    def test_a_non_object_auto_provision_is_ignored(self):
        config = _import({"peek": {"base_url": "https://p.example.com", "auto_provision": "yes"}})
        assert config["services"]["peek"]["auto_provision"]["enabled"] is False

    def test_grist_keeps_the_provisioner_url_and_locks_the_address(self):
        config = _import(
            {
                "grist": {
                    "server_url": "https://grist.example.com",
                    "auto_provision": {"enabled": True, "provisioner_url": "https://prov.example.com"},
                }
            }
        )
        grist = config["services"]["grist"]
        assert grist["address_locked"] is True
        assert grist["auto_provision"]["enabled"] is True
        assert grist["auto_provision"]["provisioner_url"] == "https://prov.example.com"
        assert grist["auto_provision"]["key_env"] == "PRIVACYFENCE_GRIST_PROVISIONING_KEY"

    def test_grist_non_string_provisioner_url_is_ignored(self):
        config = _import(
            {"grist": {"server_url": "https://g.example.com", "auto_provision": {"enabled": True, "provisioner_url": 5}}}
        )
        assert config["services"]["grist"]["auto_provision"]["provisioner_url"] == ""

    def test_grist_oauth_app_imports_no_provisioning(self):
        config = _import(
            {"grist": {"client_id": "c", "server_url": "https://g.example.com", "auto_provision": {"enabled": True}}}
        )
        grist = config["services"]["grist"]
        assert grist["address_locked"] is False
        assert grist["auto_provision"]["enabled"] is False


class TestLegacyKeysIn:
    def test_credentials_only_bundle_has_none(self):
        bundle = {
            "mode": "org",
            "google": {"client_id": "a"},
            "atlassian": {"client_id": "a"},
            "grist": {"client_id": "c", "server_url": "https://g.example.com"},
            "serial": 3,
        }
        assert admin_config.legacy_keys_in(bundle) == []
        assert admin_config.legacy_keys_in({}) == []

    def test_dotted_names(self):
        bundle = {
            "authz": {},
            "peek": {"base_url": "https://p.example.com"},
            "atlassian": {"assets": True, "assets_workspace_id": "w"},
            "grist": {"server_url": "https://g.example.com", "auto_provision": {}},
        }
        assert admin_config.legacy_keys_in(bundle) == [
            "authz",
            "peek",
            "atlassian.assets",
            "atlassian.assets_workspace_id",
            "grist.server_url",
            "grist.auto_provision",
        ]

    def test_non_dict_sections_are_ignored(self):
        assert admin_config.legacy_keys_in({"atlassian": "oops", "grist": 5}) == []


class TestEnsure:
    def test_missing_file_imports_and_saves(self, tmp_path):
        config, imported = admin_config.ensure({"slack": {"client_id": "a"}}, mode="org", single_user=False)
        assert imported is True
        assert _offered(config) == ["slack"]
        assert admin_config.load() == config

    def test_existing_file_is_returned_without_writing(self, tmp_path):
        admin_config.save(_import({"slack": {"client_id": "a"}}))
        path = tmp_path / "org" / "admin_config.json"
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        config, imported = admin_config.ensure(
            {"slack": {"client_id": "a"}, "authz": {"x": 1}}, mode="org", single_user=False
        )
        assert imported is False
        assert config == admin_config.load()
        assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime

    def test_an_empty_hash_with_a_credentials_only_bundle_is_left_alone(self):
        admin_config.save(_import({}))
        _, imported = admin_config.ensure({"slack": {"client_id": "a"}}, mode="org", single_user=False)
        assert imported is False

    def test_a_later_bundle_with_legacy_keys_reimports_exactly_once(self):
        admin_config.save(_import({}))
        bundle = {"slack": {"client_id": "a"}, "authz": {"allowed_domains": ["example.com"]}}
        config, imported = admin_config.ensure(bundle, mode="org", single_user=False)
        assert imported is True
        assert config["authz"] == {"allowed_domains": ["example.com"]}
        assert config["imported_from_bundle_sha256"] == _import(bundle)["imported_from_bundle_sha256"]
        again, imported_again = admin_config.ensure(bundle, mode="org", single_user=False)
        assert imported_again is False
        assert again == config


class TestReimportMerge:
    def _start(self, current, **kwargs):
        config = _import({}, **kwargs)
        config.update(current)
        admin_config.save(config)

    def test_decisions_stay_and_defaults_are_filled(self, caplog):
        config = _import({})
        config["authz"] = {"allowed_domains": ["mine.example.com"]}
        config["services"]["slack"]["offered"] = True
        admin_config.save(config)
        bundle = {
            "authz": {"allowed_domains": ["theirs.example.com"]},
            "web_push": {"enabled": False},
            "rooms": [{"id": "r"}],
        }
        with caplog.at_level(logging.WARNING, logger="privacyfence.admin_config"):
            merged, imported = admin_config.ensure(bundle, mode="org", single_user=False)
        assert imported is True
        assert merged["authz"] == {"allowed_domains": ["mine.example.com"]}
        assert merged["web_push"] == {"enabled": False}
        assert merged["rooms"] == [{"id": "r"}]
        text = caplog.text
        assert "web_push" in text and "rooms" in text and "authz" not in text

    def test_offered_is_never_turned_off(self):
        config = _import({})
        config["services"]["google"]["offered"] = True
        admin_config.save(config)
        merged, _ = admin_config.ensure(
            {"slack": {"client_id": "a"}, "authz": {"x": 1}}, mode="org", single_user=False
        )
        assert merged["services"]["google"]["offered"] is True
        assert merged["services"]["slack"]["offered"] is True

    def test_step_up_is_never_lowered(self):
        config = _import({}, single_user=True)
        admin_config.save(config)
        merged, _ = admin_config.ensure(
            {"step_up": {"enabled": True, "require_passkey": False, "scope": "writes"}}, mode="org", single_user=True
        )
        assert merged["step_up"] == {"enabled": True, "require_passkey": True, "scope": "writes_and_pii_reads"}

    def test_step_up_may_be_raised_from_its_default(self):
        admin_config.save(_import({}))
        merged, _ = admin_config.ensure(
            {"step_up": {"enabled": True, "require_passkey": True, "scope": "writes_and_reads"}},
            mode="org",
            single_user=False,
        )
        assert merged["step_up"]["scope"] == "writes_and_reads"

    def test_step_up_with_an_unknown_scope_counts_as_the_narrowest(self):
        admin_config.save(_import({}, single_user=True))
        merged, _ = admin_config.ensure(
            {"step_up": {"enabled": True, "require_passkey": True, "scope": "bogus"}}, mode="org", single_user=True
        )
        assert merged["step_up"]["scope"] == "writes_and_pii_reads"

    def test_org_server_and_unknown_keys_are_kept(self):
        config = _import({})
        config["org_server"] = True
        admin_config.save(config)
        merged, _ = admin_config.ensure({"authz": {"x": 1}}, mode="org", single_user=False)
        assert merged["org_server"] is True
        assert admin_config.load()["org_server"] is True

    def test_addresses_and_assets_are_copied_only_over_defaults(self, caplog):
        config = _import({})
        config["services"]["peek"]["base_url"] = "https://mine.example.com"
        admin_config.save(config)
        bundle = {
            "peek": {"base_url": "https://theirs.example.com"},
            "grist": {"server_url": "https://grist.example.com"},
            "atlassian": {"assets": True, "assets_workspace_id": "w"},
        }
        with caplog.at_level(logging.WARNING, logger="privacyfence.admin_config"):
            merged, _ = admin_config.ensure(bundle, mode="org", single_user=False)
        services = merged["services"]
        assert services["peek"]["base_url"] == "https://mine.example.com"
        assert services["grist"]["server_url"] == "https://grist.example.com"
        assert services["atlassian"]["assets"] is True
        assert services["atlassian"]["assets_workspace_id"] == "w"
        assert "services.grist.server_url" in caplog.text
        assert "services.atlassian.assets" in caplog.text
        assert "services.peek.base_url" not in caplog.text

    def test_auto_provision_enabled_is_never_changed(self):
        config = _import({})
        config["services"]["peek"]["auto_provision"]["enabled"] = False
        config["services"]["grist"]["auto_provision"]["enabled"] = False
        admin_config.save(config)
        bundle = {
            "peek": {
                "base_url": "https://p.example.com",
                "auto_provision": {"enabled": True, "trust_unverified_email": True, "key_env": "K"},
            },
            "grist": {
                "server_url": "https://g.example.com",
                "auto_provision": {"enabled": True, "provisioner_url": "https://prov.example.com"},
            },
        }
        merged, _ = admin_config.ensure(bundle, mode="org", single_user=False)
        peek, grist = merged["services"]["peek"], merged["services"]["grist"]
        assert peek["auto_provision"]["enabled"] is False
        assert grist["auto_provision"]["enabled"] is False
        assert peek["auto_provision"]["trust_unverified_email"] is True
        assert peek["auto_provision"]["key_env"] == "K"
        assert peek["address_locked"] is True
        assert grist["auto_provision"]["provisioner_url"] == "https://prov.example.com"

    def test_auto_provision_fields_a_decision_already_set_are_kept(self):
        config = _import({})
        config["services"]["peek"]["auto_provision"]["key_env"] = "MINE"
        admin_config.save(config)
        merged, _ = admin_config.ensure(
            {"peek": {"auto_provision": {"enabled": True, "key_env": "THEIRS"}}}, mode="org", single_user=False
        )
        assert merged["services"]["peek"]["auto_provision"]["key_env"] == "MINE"


class TestConfigFor:
    def test_without_a_file_it_imports_without_writing(self, tmp_path):
        config = admin_config.config_for({"slack": {"client_id": "a"}}, mode="org", single_user=False)
        assert _offered(config) == ["slack"]
        assert not (tmp_path / "org" / "admin_config.json").exists()

    def test_with_a_file_it_returns_the_stored_config(self, tmp_path):
        stored = _import({"google": {"client_id": "g"}})
        admin_config.save(stored)
        before = (tmp_path / "org" / "admin_config.json").read_bytes()
        config = admin_config.config_for({"slack": {"client_id": "a"}}, mode="org", single_user=False)
        assert config == stored
        assert (tmp_path / "org" / "admin_config.json").read_bytes() == before


def test_service_labels_cover_every_service():
    assert set(admin_config.SERVICE_LABELS) == set(admin_config.SERVICES)
    assert set(admin_config.OPERATIONAL_SECTIONS) <= set(admin_config.LEGACY_BUNDLE_KEYS)


# --------------------------------------------------------------------------- #
# apply_change, replace_rooms, service_rows
# --------------------------------------------------------------------------- #

SENTINEL = "SENTINEL-bearer-value-9f3a"
BUNDLE: dict = {}


@pytest.fixture
def clean_env(monkeypatch):
    for variable in (
        "PRIVACYFENCE_PEEK_BASE_URL",
        "PRIVACYFENCE_GRIST_SERVER_URL",
        "PRIVACYFENCE_GRIST_PROVISIONER_URL",
        "PRIVACYFENCE_PEEK_PROVISIONING_KEY",
        "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def public_dns(monkeypatch):
    """Every host name resolves to a public address."""
    monkeypatch.setattr(
        admin_config.socket, "getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )


def _change(action, params=None, *, bundle=None, mode="org", single_user=False):
    return admin_config.apply_change(
        action, params or {}, bundle=BUNDLE if bundle is None else bundle, mode=mode, single_user=single_user
    )


def _stored():
    config = admin_config.load()
    assert config is not None
    return config


def _delivery(**overrides):
    params = {
        "inline_max_bytes": 1000,
        "link_ttl_seconds": 600,
        "allow_disk_staging": True,
        "agent_links": True,
        "allow_exports": True,
    }
    return {**params, **overrides}


def _audit(**overrides):
    params = {
        "enabled": True,
        "kind": "http",
        "syslog_host": "",
        "syslog_port": 6514,
        "syslog_protocol": "tcp",
        "http_url": "https://siem.example.com/ingest",
    }
    return {**params, **overrides}


class TestServiceToggles:
    def test_offer_and_withdraw(self, clean_env):
        assert _change("offer_service", {"service": "slack"}) == ("Offered Slack", ["services"])
        assert _stored()["services"]["slack"]["offered"] is True
        assert _change("withdraw_service", {"service": "slack"}) == ("Stopped offering Slack", ["services"])
        assert _stored()["services"]["slack"]["offered"] is False

    def test_no_op_saves_nothing(self, tmp_path, clean_env):
        assert _change("withdraw_service", {"service": "slack"}) == ("", [])
        assert not (tmp_path / "org" / "admin_config.json").exists()

    def test_unknown_service(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match=r"^Unknown service\.$"):
            _change("offer_service", {"service": "nope"})

    def test_unknown_action(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="Unknown action"):
            _change("toggle_pii_detection", {})

    def test_org_only_action_in_local_mode(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="organization servers only"):
            _change("set_web_push", {"enabled": True}, mode="local")

    @pytest.mark.parametrize("params", [{}, {"service": 3}])
    def test_bad_parameters(self, clean_env, params):
        with pytest.raises(admin_config.AdminConfigError):
            _change("offer_service", params)

    def test_a_bool_is_not_an_int(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="wrong type"):
            _change("set_scoped_tokens", {"enabled": True, "max_days": True, "allow_ai_proposals": False})

    def test_applies_on_top_of_the_bundle_import_when_there_is_no_file(self, clean_env):
        summary, _ = _change("offer_service", {"service": "slack"}, bundle={"telegram": {"x": 1}})
        assert summary == "Offered Slack"
        assert _stored()["services"]["telegram"]["offered"] is True


class TestAtlassianAssets:
    def test_stores_assets_and_workspace(self, clean_env):
        params = {"enabled": True, "workspace_id": " ws-1 "}
        assert _change("set_atlassian_assets", params) == ("Set Jira Assets to on", ["services"])
        service = _stored()["services"]["atlassian"]
        assert (service["assets"], service["assets_workspace_id"]) == (True, "ws-1")
        assert _change("set_atlassian_assets", params) == ("", [])
        assert _change("set_atlassian_assets", {"enabled": False}) == ("Set Jira Assets to off", ["services"])
        assert _stored()["services"]["atlassian"]["assets_workspace_id"] == ""

    def test_refuses_control_characters_and_long_ids(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="control characters"):
            _change("set_atlassian_assets", {"enabled": True, "workspace_id": "a\u0000b"})
        with pytest.raises(admin_config.AdminConfigError, match="too long"):
            _change("set_atlassian_assets", {"enabled": True, "workspace_id": "a" * 201})


class TestServerAddress:
    def test_normalizes_and_clears(self, clean_env, public_dns):
        summary, sections = _change("set_server_address", {"service": "peek", "value": " https://Peek.Example.com/ "})
        assert (summary, sections) == ("Set the Peek server to https://peek.example.com", ["services"])
        assert _stored()["services"]["peek"]["base_url"] == "https://peek.example.com"
        assert _change("set_server_address", {"service": "peek", "value": "https://peek.example.com"}) == ("", [])
        assert _change("set_server_address", {"service": "peek", "value": ""}) == ("Cleared the Peek server", ["services"])
        assert _stored()["services"]["peek"]["base_url"] == ""

    def test_grist_uses_its_own_field(self, clean_env, public_dns):
        _change("set_server_address", {"service": "grist", "value": "https://docs.example.com"})
        assert _stored()["services"]["grist"]["server_url"] == "https://docs.example.com"

    def test_client_errors_are_mapped(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="Peek server address must start with https"):
            _change("set_server_address", {"service": "peek", "value": "ftp://x"})
        with pytest.raises(admin_config.AdminConfigError, match="without /api"):
            _change("set_server_address", {"service": "grist", "value": "https://g.example.com/api"})

    def test_only_peek_and_grist_have_an_address(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match=r"^Unknown service\.$"):
            _change("set_server_address", {"service": "slack", "value": "https://x.example.com"})

    @pytest.mark.parametrize(
        "value", ["http://localhost:8080", "https://10.0.0.5", "https://[::1]"]
    )
    def test_org_mode_refuses_http_and_ip_literals(self, clean_env, value):
        with pytest.raises(admin_config.AdminConfigError, match="public host name"):
            _change("set_server_address", {"service": "peek", "value": value})

    @pytest.mark.parametrize("resolved", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"])
    def test_org_mode_refuses_hosts_resolving_to_private_addresses(self, clean_env, monkeypatch, resolved):
        monkeypatch.setattr(admin_config.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (resolved, 0))])
        with pytest.raises(admin_config.AdminConfigError, match="public host name"):
            _change("set_server_address", {"service": "grist", "value": "https://internal.example.com"})

    def test_org_mode_refuses_unresolvable_hosts(self, clean_env, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("no such host")

        monkeypatch.setattr(admin_config.socket, "getaddrinfo", fail)
        with pytest.raises(admin_config.AdminConfigError, match="public host name"):
            _change("set_server_address", {"service": "peek", "value": "https://nowhere.example.com"})

    def test_local_mode_accepts_loopback_http_without_resolving(self, clean_env, monkeypatch):
        monkeypatch.setattr(admin_config.socket, "getaddrinfo", lambda *a, **k: pytest.fail("resolved"))
        summary, _ = _change("set_server_address", {"service": "peek", "value": "http://localhost:8080"}, mode="local")
        assert summary == "Set the Peek server to http://localhost:8080"

    def test_refused_while_the_environment_sets_it(self, clean_env, monkeypatch, public_dns):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://env.example.com")
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_server_address", {"service": "peek", "value": "https://other.example.com"})
        assert str(excinfo.value) == "This address is set by PRIVACYFENCE_PEEK_BASE_URL on the server."

    def test_refused_while_the_address_is_imported_and_locked(self, clean_env, public_dns):
        config = _import({"peek": {"base_url": "https://peek.example.com", "auto_provision": {"enabled": True}}})
        assert config["services"]["peek"]["address_locked"] is True
        admin_config.save(config)
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_server_address", {"service": "peek", "value": ""})
        assert str(excinfo.value) == (
            "This address came from the signed configuration file; "
            "set PRIVACYFENCE_PEEK_BASE_URL on the server to change it."
        )

    def test_refused_while_a_bundle_grist_app_owns_it(self, clean_env, public_dns):
        bundle = {"grist": {"client_id": "id", "client_secret": "s", "server_url": "https://g.example.com"}}
        with pytest.raises(admin_config.AdminConfigError, match="Grist app in the configuration file"):
            _change("set_server_address", {"service": "grist", "value": "https://x.example.com"}, bundle=bundle)


class TestAutoProvisioning:
    PEEK = {"service": "peek", "enabled": True, "trust_unverified_email": False}

    def _with_env_address(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://peek.example.com")

    def test_turns_on_with_an_environment_address(self, clean_env, monkeypatch):
        self._with_env_address(monkeypatch)
        assert _change("set_auto_provisioning", self.PEEK) == ("Turned automatic Peek accounts on", ["auto_provision"])
        assert _stored()["services"]["peek"]["auto_provision"]["enabled"] is True
        assert _change("set_auto_provisioning", self.PEEK) == ("", [])
        changed = {**self.PEEK, "trust_unverified_email": True}
        assert _change("set_auto_provisioning", changed) == ("Changed automatic Peek accounts", ["auto_provision"])
        off = {**self.PEEK, "enabled": False}
        assert _change("set_auto_provisioning", off) == ("Turned automatic Peek accounts off", ["auto_provision"])

    def test_turning_off_needs_no_address(self, clean_env):
        assert _change("set_auto_provisioning", {**self.PEEK, "enabled": False}) == ("", [])

    def test_refused_without_an_environment_address(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_auto_provisioning", self.PEEK)
        assert str(excinfo.value) == (
            "Automatic accounts need the Peek address from PRIVACYFENCE_PEEK_BASE_URL on the server, "
            "so the provisioning key only goes where the server's operator points it."
        )

    def test_refused_for_an_address_typed_on_the_page(self, clean_env, public_dns):
        _change("set_server_address", {"service": "peek", "value": "https://peek.example.com"})
        with pytest.raises(admin_config.AdminConfigError, match="PRIVACYFENCE_PEEK_BASE_URL"):
            _change("set_auto_provisioning", self.PEEK)

    def test_grist_needs_both_environment_addresses(self, clean_env, monkeypatch):
        params = {"service": "grist", "enabled": True, "trust_unverified_email": False}
        monkeypatch.setenv("PRIVACYFENCE_GRIST_SERVER_URL", "https://docs.example.com")
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_auto_provisioning", params)
        assert str(excinfo.value) == (
            "Automatic accounts need PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL on the server."
        )
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONER_URL", "https://prov.example.com")
        assert _change("set_auto_provisioning", params)[0] == "Turned automatic Grist accounts on"

    def test_grist_with_a_bundle_oauth_app(self, clean_env, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_GRIST_SERVER_URL", "https://docs.example.com")
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONER_URL", "https://prov.example.com")
        params = {"service": "grist", "enabled": True, "trust_unverified_email": False}
        with pytest.raises(admin_config.AdminConfigError, match="work only with API keys"):
            _change("set_auto_provisioning", params, bundle={"grist": {"client_id": "x", "client_secret": "y"}})

    def test_imported_locked_addresses_count_as_trusted(self, clean_env):
        grist = {
            "server_url": "https://docs.example.com",
            "auto_provision": {"enabled": True, "provisioner_url": "https://prov.example.com"},
        }
        admin_config.save(_import({"grist": grist}))
        params = {"service": "grist", "enabled": True, "trust_unverified_email": True}
        assert _change("set_auto_provisioning", params)[0] == "Changed automatic Grist accounts"

    def test_refused_in_single_user_mode_and_for_other_services(self, clean_env, monkeypatch):
        self._with_env_address(monkeypatch)
        with pytest.raises(admin_config.AdminConfigError, match="identity provider"):
            _change("set_auto_provisioning", self.PEEK, single_user=True)
        with pytest.raises(admin_config.AdminConfigError, match=r"^Unknown service\.$"):
            _change("set_auto_provisioning", {**self.PEEK, "service": "slack"})
        with pytest.raises(admin_config.AdminConfigError, match="organization servers only"):
            _change("set_auto_provisioning", self.PEEK, mode="local")


class TestPlainSections:
    def test_unattended_sessions(self, clean_env):
        assert _change("set_unattended_sessions", {"enabled": True}) == (
            "Turned unattended sessions on",
            ["unattended_sessions"],
        )
        assert _stored()["unattended_sessions"] == {"enabled": True}
        assert _change("set_unattended_sessions", {"enabled": True}) == ("", [])
        assert _change("set_unattended_sessions", {"enabled": False})[0] == "Turned unattended sessions off"

    def test_unattended_sessions_work_in_local_mode(self, clean_env):
        assert _change("set_unattended_sessions", {"enabled": True}, mode="local")[1] == ["unattended_sessions"]

    def test_clear_rooms(self, clean_env):
        assert _change("clear_rooms") == ("", [])
        replace = {"rooms": [{"resource_email": "a@x.com"}], "rooms_synced_at": "2026-01-01T00:00:00Z"}
        admin_config.replace_rooms(replace, bundle=BUNDLE, mode="org", single_user=False)
        assert _change("clear_rooms") == ("Cleared the room directory", ["rooms"])
        assert _stored()["rooms"] == [] and _stored()["rooms_synced_at"] == ""

    def test_web_push(self, clean_env):
        assert _change("set_web_push", {"enabled": False}) == ("Turned web push off", ["web_push"])
        assert _stored()["web_push"] == {"enabled": False}
        assert _change("set_web_push", {"enabled": False}) == ("", [])
        assert _change("set_web_push", {"enabled": True})[0] == "Turned web push on"

    def test_download_delivery(self, clean_env):
        assert _change("set_download_delivery", _delivery()) == ("Changed file delivery", ["download_delivery"])
        assert _stored()["download_delivery"] == _delivery()
        assert _change("set_download_delivery", _delivery()) == ("", [])

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"inline_max_bytes": -1}, 'must be >= 0'),
            ({"link_ttl_seconds": 0}, "must be > 0"),
        ],
    )
    def test_download_delivery_parser_errors_lose_the_bundle_prefix(self, clean_env, overrides, message):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_download_delivery", _delivery(**overrides))
        assert message in str(excinfo.value)
        assert "org_bundle.json's " not in str(excinfo.value)
        assert not (admin_config.admin_config_path()).exists()

    def test_authz_splits_lists(self, clean_env):
        params = {
            "allowed_domains": "acme.com, @Beta.org\n\n gamma.net ,",
            "groups_claim": " groups ",
            "required_groups": "staff\nadmins",
        }
        assert _change("set_authz", params) == ("Changed sign-in restrictions", ["authz"])
        assert _stored()["authz"] == {
            "allowed_domains": ["acme.com", "@Beta.org", "gamma.net"],
            "groups_claim": "groups",
            "required_groups": ["staff", "admins"],
        }
        assert _change("set_authz", params) == ("", [])

    def test_authz_required_groups_need_a_claim(self, clean_env):
        params = {"allowed_domains": "", "groups_claim": "", "required_groups": "staff"}
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_authz", params)
        assert "required_groups" in str(excinfo.value) and "org_bundle.json's " not in str(excinfo.value)

    def test_scoped_tokens_store_proposals(self, clean_env):
        from privacyfence import scoped_tokens

        params = {"enabled": True, "max_days": 30, "allow_ai_proposals": True}
        assert _change("set_scoped_tokens", params) == ("Changed scoped tokens", ["scoped_tokens"])
        stored = _stored()["scoped_tokens"]
        assert stored == {"enabled": True, "max_days": 30, "allow_ai_proposals": True}
        assert _change("set_scoped_tokens", params) == ("", [])
        assert scoped_tokens.policy_from_org_config({"scoped_tokens": stored}, org_mode=True).allow_ai_proposals is True
        off = _change("set_scoped_tokens", {**params, "enabled": False})
        assert off == ("Changed scoped tokens", ["scoped_tokens"])
        policy = scoped_tokens.policy_from_org_config({"scoped_tokens": _stored()["scoped_tokens"]}, org_mode=True)
        assert policy.allow_ai_proposals is False

    def test_imported_proposals_survive_the_import(self, clean_env):
        bundle = {"scoped_tokens": {"enabled": True, "max_days": 7, "allow_ai_proposals": True}}
        config = _import(bundle)
        assert config["scoped_tokens"]["allow_ai_proposals"] is True
        admin_config.save(config)
        _change("set_scoped_tokens", {"enabled": True, "max_days": 8, "allow_ai_proposals": True}, bundle=bundle)
        assert _stored()["scoped_tokens"]["allow_ai_proposals"] is True

    @pytest.mark.parametrize("days", [0, 366, -3])
    def test_scoped_token_days_out_of_range(self, clean_env, days):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_scoped_tokens", {"enabled": True, "max_days": days, "allow_ai_proposals": False})
        assert str(excinfo.value) == "Scoped tokens can last from 1 to 365 days."

    @pytest.mark.parametrize("days", [1, 365])
    def test_scoped_token_days_at_the_edges(self, clean_env, days):
        assert _change("set_scoped_tokens", {"enabled": True, "max_days": days, "allow_ai_proposals": False})[0]


class TestTightenStepUp:
    def _set(self, **section):
        admin_config.save({**_import({}), "step_up": section})

    def _tighten(self, enabled, require_passkey, scope):
        return _change("tighten_step_up", {"enabled": enabled, "require_passkey": require_passkey, "scope": scope})

    def test_enabling_is_accepted(self, clean_env):
        self._set(rp_id="example.com", batch="per_item")
        assert self._tighten(True, False, "writes_and_pii_reads") == ("Made step-up stricter", ["step_up"])
        assert _stored()["step_up"] == {
            "rp_id": "example.com",
            "batch": "per_item",
            "enabled": True,
            "require_passkey": False,
            "scope": "writes_and_pii_reads",
        }

    def test_requiring_a_passkey_and_a_wider_scope_are_accepted(self, clean_env):
        self._set(enabled=True, scope="writes")
        assert self._tighten(True, True, "writes")[0] == "Made step-up stricter"
        assert self._tighten(True, True, "writes_and_reads")[0] == "Made step-up stricter"
        assert _stored()["step_up"]["scope"] == "writes_and_reads"

    @pytest.mark.parametrize(
        ("current", "submitted"),
        [
            ({"enabled": True}, (False, False, "writes_and_pii_reads")),
            ({"enabled": True, "scope": "writes_and_reads"}, (True, False, "writes")),
            ({"enabled": True, "require_passkey": True}, (True, False, "writes_and_pii_reads")),
            ({"enabled": True, "scope": "writes_and_reads"}, (True, False, "writes_and_pii_reads")),
        ],
    )
    def test_relaxing_is_refused(self, clean_env, current, submitted):
        self._set(**current)
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            self._tighten(*submitted)
        assert str(excinfo.value) == (
            "Step-up can only be made stricter here. To relax it, edit org/admin_config.json "
            "and restart PrivacyFence (ADR 0068)."
        )
        assert _stored()["step_up"] == current

    def test_unchanged_submission_is_a_no_op_even_against_defaults(self, clean_env):
        self._set(enabled=True, require_passkey=True, scope="writes_and_reads")
        assert self._tighten(True, True, "writes_and_reads") == ("", [])
        self._set()
        assert self._tighten(False, False, "writes_and_pii_reads") == ("", [])
        assert _stored()["step_up"] == {}

    def test_unknown_scope_is_a_parser_error(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            self._tighten(True, False, "everything")
        assert "scope" in str(excinfo.value) and "org_bundle.json's " not in str(excinfo.value)


class TestAuditForwarding:
    @pytest.fixture(autouse=True)
    def _bearer(self, monkeypatch):
        monkeypatch.setenv("SIEM_TOKEN", SENTINEL)

    def _start(self, bearer="SIEM_TOKEN", url="https://siem.example.com/ingest", kind="http"):
        config = _import({})
        config["audit_forwarding"] = {
            "enabled": True,
            "kind": kind,
            "http": {"url": url, "bearer_token_env": bearer},
            "syslog": {"host": "log.example.com", "port": 6514, "protocol": "tcp"},
        }
        admin_config.save(config)

    def test_stores_the_section(self, clean_env):
        assert _change("set_audit_forwarding", _audit()) == ("Changed audit forwarding", ["audit_forwarding"])
        assert _stored()["audit_forwarding"] == {
            "enabled": True,
            "kind": "http",
            "syslog": {"host": "", "port": 6514, "protocol": "tcp"},
            "http": {"url": "https://siem.example.com/ingest", "bearer_token_env": ""},
        }
        assert _change("set_audit_forwarding", _audit()) == ("", [])

    def test_the_bearer_name_is_never_taken_from_the_request(self, clean_env):
        self._start()
        summary, _ = _change("set_audit_forwarding", {**_audit(syslog_host="log.example.com"), "bearer_token_env": "HOME", "http": {"bearer_token_env": "HOME"}})
        assert summary == ""
        assert _stored()["audit_forwarding"]["http"]["bearer_token_env"] == "SIEM_TOKEN"

    def test_a_changed_syslog_target_keeps_the_bearer_name(self, clean_env):
        self._start()
        summary, _ = _change("set_audit_forwarding", _audit(syslog_host="other.example.com"))
        assert summary == "Changed audit forwarding"
        assert _stored()["audit_forwarding"]["http"]["bearer_token_env"] == "SIEM_TOKEN"

    @pytest.mark.parametrize(
        "url",
        [
            "http://siem.example.com/ingest",
            "https://evil.example.org/ingest",
            "https://siem.example.com:8443/ingest",
        ],
    )
    def test_a_moved_url_drops_the_bearer(self, clean_env, caplog, url):
        self._start()
        caplog.set_level("DEBUG")
        summary, sections = _change("set_audit_forwarding", _audit(http_url=url, enabled=False))
        assert summary == (
            "Changed audit forwarding; the bearer token was removed. Set it again in org/admin_config.json."
        )
        assert sections == ["audit_forwarding"]
        assert _stored()["audit_forwarding"]["http"]["bearer_token_env"] == ""
        assert SENTINEL not in summary and SENTINEL not in caplog.text
        assert SENTINEL not in admin_config.admin_config_path().read_text()

    def test_a_changed_path_or_credentials_in_the_same_endpoint_keeps_the_bearer(self, clean_env):
        self._start()
        _change("set_audit_forwarding", _audit(http_url="https://SIEM.example.com/other/path"))
        assert _stored()["audit_forwarding"]["http"]["bearer_token_env"] == "SIEM_TOKEN"

    def test_a_changed_kind_drops_the_bearer(self, clean_env):
        self._start()
        summary, _ = _change("set_audit_forwarding", _audit(kind="syslog", syslog_host="log.example.com"))
        assert "bearer token was removed" in summary

    def test_no_bearer_no_mention(self, clean_env):
        self._start(bearer="")
        summary, _ = _change("set_audit_forwarding", _audit(http_url="https://elsewhere.example.com"))
        assert summary == "Changed audit forwarding"

    def test_an_unparseable_url_counts_as_moved(self, clean_env):
        self._start()
        summary, _ = _change("set_audit_forwarding", _audit(http_url="https://siem.example.com:notaport/", enabled=False))
        assert "bearer token was removed" in summary

    def test_http_url_must_be_https(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            _change("set_audit_forwarding", _audit(http_url="http://siem.example.com"))
        assert "must use https://" in str(excinfo.value) and "org_bundle.json's " not in str(excinfo.value)

    def test_bad_protocol_and_port(self, clean_env):
        with pytest.raises(admin_config.AdminConfigError, match="protocol"):
            _change("set_audit_forwarding", _audit(syslog_protocol="icmp"))
        with pytest.raises(admin_config.AdminConfigError, match="port from 1 to 65535"):
            _change("set_audit_forwarding", _audit(syslog_port=0))
        with pytest.raises(admin_config.AdminConfigError, match="must be"):
            _change("set_audit_forwarding", _audit(kind="smoke"))


class TestReplaceRooms:
    def _replace(self, data):
        return admin_config.replace_rooms(data, bundle=BUNDLE, mode="org", single_user=False)

    def test_happy_path(self, clean_env):
        rooms = [
            {"resource_email": "a@x.com", "name": "Room A", "capacity": 8, "ratio": 0.5, "av": True, "notes": None},
            {"resource_email": "b@x.com"},
        ]
        assert self._replace({"rooms": rooms, "rooms_synced_at": "2026-01-01T00:00:00Z"}) == (
            "Replaced the room directory (2 rooms)"
        )
        stored = _stored()
        assert stored["rooms"] == rooms and stored["rooms_synced_at"] == "2026-01-01T00:00:00Z"

    def test_missing_sync_time_is_empty(self, clean_env):
        self._replace({"rooms": []})
        assert _stored()["rooms_synced_at"] == ""

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {"rooms": "x"},
            {"rooms": ["x"]},
            {"rooms": [{"name": "no email"}]},
            {"rooms": [{"resource_email": ""}]},
            {"rooms": [{"resource_email": 3}]},
            {"rooms": [{"resource_email": "a@x.com", "name": "x" * 501}]},
            {"rooms": [{"resource_email": "a@x.com", "meta": {"nested": 1}}]},
            {"rooms": [{"resource_email": "a@x.com", "meta": ["a"]}]},
            {"rooms": [{"resource_email": "a@x.com", "n": float("nan")}]},
            {"rooms": [{"resource_email": "a@x.com", "name": "line\nbreak"}]},
            {"rooms": [{"resource_email": "a@x.com", "name": "tab\there"}]},
            {"rooms": [{"resource_email": "a@x.com", "name": "esc\u001b[0m"}]},
            {"rooms": [{"resource_email": "a@x.com", "name": "sep x"}]},
            {"rooms": [{"resource_email": "a@x\r.com"}]},
            {"rooms": [{"resource_email": "a@x.com", "bad\nkey": 1}]},
            {"rooms": [], "rooms_synced_at": 5},
            {"rooms": [], "rooms_synced_at": "x\ny"},
        ],
    )
    def test_rule_violations(self, clean_env, data):
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            self._replace(data)
        assert str(excinfo.value) == "The file is not a room directory written by sync_room_directory.py."
        assert not admin_config.admin_config_path().exists()

    def test_a_500_character_name_is_fine(self, clean_env):
        self._replace({"rooms": [{"resource_email": "a@x.com", "name": "x" * 500}]})

    def test_too_many_rooms(self, clean_env):
        rooms = [{"resource_email": f"r{i}@x.com"} for i in range(5001)]
        with pytest.raises(admin_config.AdminConfigError) as excinfo:
            self._replace({"rooms": rooms})
        assert str(excinfo.value) == "A room directory can hold at most 5000 rooms."
        assert self._replace({"rooms": rooms[:5000]}).endswith("(5000 rooms)")


class TestServiceRows:
    @pytest.fixture(autouse=True)
    def _apps(self, monkeypatch, clean_env):
        apps = {
            "google_web": {"client_id": "g-id", "client_secret": "g-secret-VALUE"},
            "slack": {"client_id": "s-id", "client_secret": "s-secret-VALUE"},
            "salesforce": {"client_id": "sf-id", "client_secret": "sf-secret-VALUE"},
        }
        monkeypatch.setattr(admin_config.app_credentials, "builtin_app", lambda key: apps.get(key))
        monkeypatch.setattr(admin_config.app_credentials, "telegram_app_credentials", lambda: (1, "hash-VALUE"))

    @pytest.fixture(autouse=True)
    def _no_connector_configs(self, monkeypatch, tmp_path):
        # This module is exempt from the shared path isolation, and an organization-mode row
        # reads the stored connector configs.
        monkeypatch.setattr(connector_configs, "default_org_dir", lambda: tmp_path)

    def _rows(self, bundle=None, admin=None, *, mode="org", single_user=False):
        rows = admin_config.service_rows(
            bundle or {}, admin or _import({}, mode=mode), mode=mode, single_user=single_user
        )
        return {row["key"]: row for row in rows}

    def test_one_row_per_service_with_exact_fields(self):
        rows = admin_config.service_rows({}, _import({}), mode="org")
        assert [row["key"] for row in rows] == list(admin_config.SERVICES)
        assert all(set(row) == {"key", "label", "offered", "app_source", "client_id", "relay", "options"} for row in rows)
        assert rows[0]["label"] == "Google" and rows[0]["offered"] is False

    def test_builtin_apps_use_the_relay_in_org_mode(self):
        rows = self._rows()
        assert (rows["google"]["app_source"], rows["google"]["client_id"], rows["google"]["relay"]) == ("builtin", "g-id", True)
        assert rows["telegram"]["app_source"] == "builtin" and rows["telegram"]["relay"] is False
        assert rows["miro"]["app_source"] == "none" and rows["miro"]["relay"] is False
        assert rows["salesforce"]["client_id"] == "sf-id"

    def test_local_mode_has_no_builtin_apps_and_no_relay(self):
        rows = self._rows(mode="local")
        assert rows["google"]["app_source"] == "none" and rows["google"]["relay"] is False
        assert rows["telegram"]["app_source"] == "builtin" and rows["telegram"]["relay"] is False

    def test_bundle_apps_win_and_are_not_relayed(self):
        bundle = {"slack": {"client_id": "own-slack", "client_secret": "x"}, "salesforce": {"consumer_key": "own-sf"}}
        rows = self._rows(bundle, mode="local")
        assert (rows["slack"]["app_source"], rows["slack"]["client_id"], rows["slack"]["relay"]) == ("bundle", "own-slack", False)
        assert (rows["salesforce"]["app_source"], rows["salesforce"]["client_id"]) == ("bundle", "own-sf")

    def test_telegram_without_credentials(self, monkeypatch):
        monkeypatch.setattr(admin_config.app_credentials, "telegram_app_credentials", lambda: None)
        assert self._rows()["telegram"]["app_source"] == "none"

    def test_offered_follows_the_admin_config(self):
        admin = _import({})
        admin["services"]["slack"]["offered"] = True
        rows = self._rows(admin=admin)
        assert rows["slack"]["offered"] is True and rows["google"]["offered"] is False

    def test_a_missing_service_in_the_admin_config(self):
        admin = _import({})
        admin["services"] = {}
        assert self._rows(admin=admin)["slack"]["offered"] is False

    def test_atlassian_options(self):
        admin = _import({})
        admin["services"]["atlassian"].update(assets=True, assets_workspace_id="ws")
        assert self._rows(admin=admin)["atlassian"]["options"] == {"assets": True, "assets_workspace_id": "ws"}

    def test_grist_and_peek_are_server_rows(self):
        rows = self._rows()
        for key, field in (("grist", "server_url"), ("peek", "base_url")):
            assert rows[key]["app_source"] == "server" and rows[key]["relay"] is False
            assert rows[key]["client_id"] == ""
            options = rows[key]["options"]
            assert options[field] == "" and options["address_source"] == "admin" and options["locked"] is False

    def test_grist_bundle_oauth_app_locks_the_row(self):
        bundle = {"grist": {"client_id": "grist-id", "client_secret": "s", "server_url": "https://g.example.com"}}
        row = self._rows(bundle)["grist"]
        assert row["client_id"] == "grist-id"
        assert row["options"]["address_source"] == "bundle" and row["options"]["locked"] is True
        assert row["options"]["server_url"] == "https://g.example.com"
        assert row["options"]["auto_provision"]["available"] is False
        assert "API keys" in row["options"]["auto_provision"]["reason"]

    def test_environment_addresses_win_and_lock(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://env.example.com/")
        admin = _import({})
        admin["services"]["peek"]["base_url"] = "https://admin.example.com"
        options = self._rows(admin=admin)["peek"]["options"]
        assert options["base_url"] == "https://env.example.com"
        assert (options["address_source"], options["locked"]) == ("environment", True)
        assert options["auto_provision"]["available"] is True and options["auto_provision"]["reason"] == ""

    def test_an_unusable_environment_address_is_ignored_by_the_page(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "ftp://nope")
        assert self._rows()["peek"]["options"]["address_source"] == "admin"

    def test_imported_locked_address(self):
        admin = _import({"peek": {"base_url": "https://peek.example.com", "auto_provision": {"enabled": True}}})
        options = self._rows(admin=admin)["peek"]["options"]
        assert (options["address_source"], options["locked"]) == ("imported", True)
        assert options["auto_provision"]["enabled"] is True and options["auto_provision"]["available"] is True

    def test_grist_auto_provisioning_availability(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_GRIST_SERVER_URL", "https://docs.example.com")
        auto = self._rows()["grist"]["options"]["auto_provision"]
        assert auto["available"] is False and "PRIVACYFENCE_GRIST_PROVISIONER_URL" in auto["reason"]
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONER_URL", "https://prov.example.com")
        assert self._rows()["grist"]["options"]["auto_provision"]["available"] is True

    def test_auto_provisioning_is_unavailable_in_local_and_single_user_mode(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_BASE_URL", "https://env.example.com")
        local = self._rows(mode="local")["peek"]["options"]["auto_provision"]
        assert local["available"] is False and "organization server" in local["reason"]
        single = self._rows(single_user=True)["peek"]["options"]["auto_provision"]
        assert single["available"] is False and "identity provider" in single["reason"]

    def test_key_set_reports_presence_never_the_key(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", SENTINEL)
        monkeypatch.setenv("CUSTOM_GRIST_KEY", " ")
        admin = _import({})
        admin["services"]["grist"]["auto_provision"]["key_env"] = "CUSTOM_GRIST_KEY"
        rows = self._rows(admin=admin)
        assert rows["peek"]["options"]["auto_provision"]["key_set"] is True
        assert rows["grist"]["options"]["auto_provision"]["key_set"] is False
        assert SENTINEL not in json.dumps(rows)

    def test_key_set_follows_a_key_file_and_an_unreadable_file_is_not_set(self, monkeypatch, tmp_path):
        key_file = tmp_path / "peek_key"
        key_file.write_text(SENTINEL + "\n")
        monkeypatch.delenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE", str(key_file))
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE", str(tmp_path / "absent"))
        rows = self._rows()
        assert rows["peek"]["options"]["auto_provision"]["key_set"] is True
        assert rows["grist"]["options"]["auto_provision"]["key_set"] is False
        assert SENTINEL not in json.dumps(rows)

    def test_rows_name_the_key_variable_never_its_value(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_GRIST_KEY", SENTINEL)
        admin = _import({})
        admin["services"]["grist"]["auto_provision"]["key_env"] = "CUSTOM_GRIST_KEY"
        rows = self._rows(admin=admin)
        assert rows["peek"]["options"]["key_env"] == "PRIVACYFENCE_PEEK_PROVISIONING_KEY"
        assert rows["grist"]["options"]["key_env"] == "CUSTOM_GRIST_KEY"
        assert SENTINEL not in json.dumps(rows)

    def test_no_row_carries_a_secret(self):
        bundle = {
            "slack": {"client_id": "own", "client_secret": "BUNDLE-SECRET-VALUE"},
            "grist": {"client_id": "gid", "client_secret": "GRIST-SECRET-VALUE", "server_url": "https://g.example.com"},
        }
        text = json.dumps(self._rows(bundle))
        for secret in ("BUNDLE-SECRET-VALUE", "GRIST-SECRET-VALUE", "g-secret-VALUE", "s-secret-VALUE", "sf-secret-VALUE", "hash-VALUE"):
            assert secret not in text


class TestNothingSecretLeaks:
    def test_summaries_and_logs_never_carry_secrets(self, clean_env, monkeypatch, caplog, public_dns):
        monkeypatch.setenv("SIEM_TOKEN", SENTINEL)
        caplog.set_level("DEBUG")
        bundle = {"slack": {"client_id": "x", "client_secret": "CLIENT-SECRET-VALUE"}}
        config = _import(bundle)
        config["audit_forwarding"] = {"http": {"url": "https://siem.example.com", "bearer_token_env": "SIEM_TOKEN"}}
        admin_config.save(config)
        summaries = [
            _change("offer_service", {"service": "slack"}, bundle=bundle)[0],
            _change("set_server_address", {"service": "peek", "value": "https://peek.example.com"}, bundle=bundle)[0],
            _change("set_audit_forwarding", _audit(http_url="https://new.example.com"), bundle=bundle)[0],
            _change("set_authz", {"allowed_domains": "a.com", "groups_claim": "", "required_groups": ""}, bundle=bundle)[0],
        ]
        everything = "\n".join(summaries) + caplog.text + admin_config.admin_config_path().read_text()
        assert SENTINEL not in everything and "CLIENT-SECRET-VALUE" not in everything
        assert summaries[3] == "Changed sign-in restrictions"


class TestOrgServerFlag:
    def test_default_is_false(self):
        assert _import({})["org_server"] is False

    @pytest.mark.parametrize("stored, expected", [(True, True), (False, False), ("yes", False), (1, False)])
    def test_load_normalizes_the_flag(self, tmp_path, stored, expected):
        config = _import({})
        config["org_server"] = stored
        _write(tmp_path, config)
        assert admin_config.load()["org_server"] is expected

    def test_a_file_written_before_the_flag_loads_as_false(self, tmp_path):
        config = _import({})
        del config["org_server"]
        _write(tmp_path, config)
        assert admin_config.load()["org_server"] is False

    def test_mark_org_server_sets_and_keeps_the_flag(self):
        admin_config.save(_import({}))
        assert admin_config.mark_org_server()["org_server"] is True
        assert _stored()["org_server"] is True
        assert admin_config.mark_org_server()["org_server"] is True

    def test_mark_org_server_without_a_file_creates_one(self):
        admin_config.mark_org_server()
        assert _stored()["org_server"] is True

    @pytest.mark.parametrize("flag", [True, False])
    def test_apply_change_cannot_change_it(self, flag, clean_env):
        config = _import({})
        config["org_server"] = flag
        admin_config.save(config)
        _change("offer_service", {"service": "slack", "org_server": not flag})
        _change("set_unattended_sessions", {"enabled": True, "org_server": not flag})
        assert _stored()["org_server"] is flag
        assert _stored()["services"]["slack"]["offered"] is True

    def test_a_reimport_keeps_it(self):
        config = _import({})
        config["org_server"] = True
        admin_config.save(config)
        merged, imported = admin_config.ensure(
            {"authz": {"allowed_domains": ["x.example.com"]}}, mode="org", single_user=False
        )
        assert imported is True
        assert merged["org_server"] is True
        assert _stored()["org_server"] is True


class TestAllowedDomainsSeed:
    """``PRIVACYFENCE_ALLOWED_DOMAINS`` seeds Admin → Sign-in on the first import only."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch):
        monkeypatch.delenv("PRIVACYFENCE_ALLOWED_DOMAINS", raising=False)

    def test_the_first_import_seeds_lower_cased_and_de_duplicated(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", " Acme.com, acme.org ,ACME.COM,")
        config, imported = admin_config.ensure({}, mode="org", single_user=False)
        assert imported is True
        assert config["authz"] == {"allowed_domains": ["acme.com", "acme.org"]}
        assert admin_config.load()["authz"] == {"allowed_domains": ["acme.com", "acme.org"]}

    def test_the_seed_happens_once_and_later_starts_log_that_it_is_ignored(self, monkeypatch, caplog):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com")
        admin_config.ensure({}, mode="org", single_user=False)
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com,evil.com")
        with caplog.at_level(logging.INFO, logger="privacyfence.admin_config"):
            config, imported = admin_config.ensure({}, mode="org", single_user=False)
        assert imported is False
        assert config["authz"] == {"allowed_domains": ["acme.com"]}
        ignored = [r for r in caplog.records if "PRIVACYFENCE_ALLOWED_DOMAINS is ignored" in r.getMessage()]
        assert len(ignored) == 1

    def test_an_admin_edit_survives_a_restart_with_the_variable_set(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com")
        admin_config.ensure({}, mode="org", single_user=False)
        _change("set_authz", {"allowed_domains": "", "groups_claim": "", "required_groups": ""})
        config, _ = admin_config.ensure({}, mode="org", single_user=False)
        assert config["authz"]["allowed_domains"] == []

    def test_an_existing_config_without_the_seed_is_not_seeded(self, monkeypatch):
        admin_config.ensure({}, mode="org", single_user=False)
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com")
        config, _ = admin_config.ensure({}, mode="org", single_user=False)
        assert config["authz"] == {}

    def test_a_later_legacy_bundle_is_merged_without_the_variable(self, monkeypatch):
        admin_config.ensure({}, mode="org", single_user=False)
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "env.example.com")
        bundle = {"authz": {"allowed_domains": ["bundle.example.com"]}}
        config, imported = admin_config.ensure(bundle, mode="org", single_user=False)
        assert imported is True
        assert config["authz"] == {"allowed_domains": ["bundle.example.com"]}

    def test_a_bundle_s_own_allowed_domains_win(self, monkeypatch, caplog):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "env.example.com")
        with caplog.at_level(logging.WARNING, logger="privacyfence.admin_config"):
            config = _import({"authz": {"allowed_domains": ["bundle.example.com"]}})
        assert config["authz"] == {"allowed_domains": ["bundle.example.com"]}
        assert any("PRIVACYFENCE_ALLOWED_DOMAINS is ignored" in r.getMessage() for r in caplog.records)

    def test_local_mode_is_not_seeded(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com")
        assert _import({}, mode="local")["authz"] == {}

    @pytest.mark.parametrize("value", [
        "*", "*.acme.com", "@acme.com", "alice@acme.com", "acme", "com", "acme..com", "-acme.com", "acme-.com",
        "acme.com/", "https://acme.com", "acme com", "1.2.3.4", "acme.cöm", "acKme.com", "acme.com,bad_domain.com",
    ])
    def test_a_value_that_is_not_a_domain_refuses_the_start(self, monkeypatch, value):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", value)
        with pytest.raises(admin_config.ConfigurationError, match="PRIVACYFENCE_ALLOWED_DOMAINS"):
            admin_config.ensure({}, mode="org", single_user=False)
        assert admin_config.load() is None

    def test_config_for_shows_the_seed_without_writing(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_ALLOWED_DOMAINS", "acme.com")
        assert admin_config.config_for({}, mode="org", single_user=False)["authz"] == {"allowed_domains": ["acme.com"]}
        assert admin_config.load() is None


class TestSigninState:
    def test_not_builtin_without_the_variable(self, monkeypatch):
        monkeypatch.delenv("PRIVACYFENCE_IDP", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_ADMIN_EMAILS", "alice@acme.com")
        assert admin_config.signin_state({}) == {"builtin": False, "admin_emails": []}

    def test_builtin_lists_the_admins_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_IDP", "builtin-google")
        monkeypatch.setenv("PRIVACYFENCE_ADMIN_EMAILS", "alice@acme.com, bob@acme.com")
        assert admin_config.signin_state({}) == {"builtin": True, "admin_emails": ["alice@acme.com", "bob@acme.com"]}

    def test_a_bundle_idp_is_not_builtin(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_IDP", "builtin-google")
        assert admin_config.signin_state({"idp": {"issuer": "https://i", "client_id": "c"}})["builtin"] is False
