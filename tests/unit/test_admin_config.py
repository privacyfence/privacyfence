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

from privacyfence import admin_config, paths

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
