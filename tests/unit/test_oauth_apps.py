"""The effective org config: bundle credentials, admin config and built-in apps composed."""
from __future__ import annotations

import copy
import threading

import pytest

from privacyfence import admin_config, app_credentials, oauth_apps
from privacyfence.org_mode import ConfigurationError, GristProvisioningConfig, PeekProvisioningConfig

BUILTIN = {"client_id": "builtin-id", "client_secret": "builtin-secret"}
PEEK_VAR = "PRIVACYFENCE_PEEK_BASE_URL"
GRIST_VAR = "PRIVACYFENCE_GRIST_SERVER_URL"
PROVISIONER_VAR = "PRIVACYFENCE_GRIST_PROVISIONER_URL"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for variable in (PEEK_VAR, GRIST_VAR, PROVISIONER_VAR):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def builtins(monkeypatch):
    """Install built-in apps for the keys given; the rest are absent."""

    def install(*keys):
        monkeypatch.setattr(
            app_credentials, "builtin_app", lambda key: dict(BUILTIN) if key in keys else None
        )

    return install


def make_admin(*offered, **services):
    admin = admin_config.import_from_bundle({}, mode="local", single_user=False)
    admin["services"]["telegram"]["offered"] = False
    for name in offered:
        admin["services"][name]["offered"] = True
    for name, fields in services.items():
        admin["services"][name].update(fields)
    return admin


def compose(bundle, admin, *, mode="org", single_user=False):
    return oauth_apps.effective_org_config(bundle, admin, mode=mode, single_user=single_user)


def compose_core(bundle, admin, **kwargs):
    """``compose`` without the operational sections, which every admin config contributes."""
    result = compose(bundle, admin, **kwargs)
    return {k: v for k, v in result.items() if k not in admin_config.OPERATIONAL_SECTIONS}


class TestLegacyKeys:
    def test_every_legacy_key_is_dropped_from_the_bundle(self):
        bundle = {key: {"x": 1} for key in admin_config.LEGACY_BUNDLE_KEYS}
        bundle["org_name"] = "Acme"
        result = compose_core(bundle, make_admin())
        assert result == {"org_name": "Acme"}

    def test_sub_keys_are_dropped(self):
        bundle = {
            "atlassian": {"client_id": "a", "client_secret": "s", "assets": True, "assets_workspace_id": "w"},
            "grist": {"server_url": "https://grist.example"},
        }
        result = compose(bundle, make_admin("atlassian"))
        assert result["atlassian"] == {"client_id": "a", "client_secret": "s", "assets": False}
        assert "grist" not in result

    def test_oauth_grist_keeps_its_own_server_url_and_loses_auto_provision(self):
        grist = {"client_id": "g", "client_secret": "s", "server_url": "https://g.example", "auto_provision": {"enabled": True}}
        result = compose({"grist": grist}, make_admin("grist"))
        assert result["grist"] == {"client_id": "g", "client_secret": "s", "server_url": "https://g.example"}

    def test_bundle_relay_marker_is_stripped(self):
        bundle = {"slack": {"client_id": "i", "client_secret": "s", "relay": True}, "mode": "org"}
        result = compose(bundle, make_admin("slack"))
        assert result["slack"] == {"client_id": "i", "client_secret": "s"}
        assert not oauth_apps.uses_relay(result["slack"])

    def test_non_object_values_pass_through(self):
        assert compose_core({"serial": 3, "org_name": "x"}, make_admin()) == {"serial": 3, "org_name": "x"}


class TestOffered:
    def test_withdrawn_service_loses_its_section(self, builtins):
        builtins("slack")
        bundle = {"slack": {"client_id": "i", "client_secret": "s"}}
        assert "slack" not in compose(bundle, make_admin())

    def test_missing_service_counts_as_not_offered(self):
        admin = make_admin("slack")
        del admin["services"]["slack"]
        assert "slack" not in compose({"slack": {"client_id": "i", "client_secret": "s"}}, admin)

    def test_malformed_service_entry_counts_as_not_offered(self):
        admin = make_admin()
        admin["services"]["slack"] = "nonsense"
        assert "slack" not in compose({"slack": {"client_id": "i"}}, admin)

    def test_admin_without_services(self):
        assert compose({"slack": {"client_id": "i"}}, {}) == {}


def expected_builtin(service):
    if service == "salesforce":
        return {"consumer_key": "builtin-id", "consumer_secret": "builtin-secret", "relay": True}
    section = {"client_id": "builtin-id", "client_secret": "builtin-secret", "relay": True}
    if service == "google":
        section["auth_uri"] = oauth_apps.GOOGLE_AUTH_URI
        section["token_uri"] = oauth_apps.GOOGLE_TOKEN_URI
    return section


def bundle_section(service):
    if service == "salesforce":
        return {"consumer_key": "own-key", "consumer_secret": "own-secret"}
    return {"client_id": "own-id", "client_secret": "own-secret"}


@pytest.mark.parametrize("service", ["google", "slack", "salesforce", "miro"])
@pytest.mark.parametrize("mode", ["local", "org"])
@pytest.mark.parametrize("case", ["bundle", "builtin", "absent"])
class TestCredentialServices:
    def test_section(self, builtins, service, mode, case):
        builtins(*([oauth_apps.BUILTIN_KEY[service]] if case in ("builtin",) or case == "bundle" else []))
        bundle = {service: bundle_section(service)} if case == "bundle" else {}
        admin = make_admin(service)
        result = compose(bundle, admin, mode=mode)
        source = oauth_apps.credentials_source(bundle, service, mode=mode)
        if case == "bundle":
            assert result[service] == bundle_section(service)
            assert source == "bundle"
        elif case == "builtin" and mode == "org":
            assert result[service] == expected_builtin(service)
            assert oauth_apps.uses_relay(result[service])
            assert source == "builtin"
        else:
            assert service not in result
            assert source == "none"


def test_absent_builtin_gives_no_section(builtins):
    builtins()
    assert compose_core({}, make_admin("google", "slack", "salesforce", "miro", "atlassian")) == {}


class TestAtlassian:
    def test_credentials_with_admin_assets(self):
        bundle = {"atlassian": {"client_id": "a", "client_secret": "s"}}
        admin = make_admin("atlassian", atlassian={"assets": True, "assets_workspace_id": "ws-1"})
        assert compose(bundle, admin)["atlassian"] == {
            "client_id": "a",
            "client_secret": "s",
            "assets": True,
            "assets_workspace_id": "ws-1",
        }

    def test_builtin_relay_credentials(self, builtins):
        builtins("atlassian_relay")
        result = compose({}, make_admin("atlassian"))
        assert result["atlassian"] == {**expected_builtin("atlassian"), "assets": False}
        assert compose_core({}, make_admin("atlassian"), mode="local") == {}

    def test_no_credentials_no_section(self, builtins):
        builtins()
        bundle = {"atlassian": {"assets": True}}
        admin = make_admin("atlassian", atlassian={"assets": True})
        assert "atlassian" not in compose(bundle, admin)


class TestTelegram:
    def test_enabled_with_credentials(self, monkeypatch):
        monkeypatch.setattr(app_credentials, "telegram_app_credentials", lambda: (1, "hash"))
        assert compose({}, make_admin("telegram"))["telegram"] == {"enabled": True}

    def test_no_credentials_no_section(self, monkeypatch):
        monkeypatch.setattr(app_credentials, "telegram_app_credentials", lambda: None)
        assert "telegram" not in compose({"telegram": {"enabled": True}}, make_admin("telegram"))

    def test_withdrawn(self, monkeypatch):
        monkeypatch.setattr(app_credentials, "telegram_app_credentials", lambda: (1, "hash"))
        assert "telegram" not in compose({}, make_admin())


class TestGrist:
    def test_bundle_oauth_app_beats_admin_address(self):
        grist = {"client_id": "g", "client_secret": "s", "auth_server_url": "https://a", "server_url": "https://own"}
        admin = make_admin("grist", grist={"server_url": "https://admin.example"})
        assert compose({"grist": grist}, admin)["grist"] == grist

    def test_admin_address_alone(self):
        admin = make_admin("grist", grist={"server_url": "https://admin.example"})
        assert compose({}, admin)["grist"] == {"server_url": "https://admin.example"}

    def test_local_with_neither(self):
        assert compose({}, make_admin("grist"), mode="local")["grist"] == {"enabled": True}

    def test_org_with_neither(self):
        assert "grist" not in compose({}, make_admin("grist"))

    def test_environment_address_wins(self, monkeypatch):
        monkeypatch.setenv(GRIST_VAR, "https://env.example/")
        admin = make_admin("grist", grist={"server_url": "https://admin.example"})
        assert compose({}, admin)["grist"] == {"server_url": "https://env.example"}

    def test_withdrawn(self, monkeypatch):
        monkeypatch.setenv(GRIST_VAR, "https://env.example")
        assert "grist" not in compose({}, make_admin())


class TestPeek:
    def test_admin_address(self):
        admin = make_admin("peek", peek={"base_url": "https://peek.example"})
        assert compose({}, admin)["peek"] == {"base_url": "https://peek.example"}

    def test_local_with_neither(self):
        assert compose({}, make_admin("peek"), mode="local")["peek"] == {"enabled": True}

    def test_org_with_neither(self):
        assert "peek" not in compose({}, make_admin("peek"))

    def test_bundle_peek_is_ignored(self):
        assert "peek" not in compose({"peek": {"base_url": "https://peek.example"}}, make_admin("peek"))

    def test_environment_address_wins(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://env.example/")
        admin = make_admin("peek", peek={"base_url": "https://admin.example"})
        assert compose({}, admin)["peek"] == {"base_url": "https://env.example"}


def enable_provisioning(admin, name, **fields):
    admin["services"][name]["auto_provision"]["enabled"] = True
    admin["services"][name]["auto_provision"].update(fields)
    return admin


class TestPeekAutoProvision:
    def test_environment_address_enables_it(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        admin = enable_provisioning(make_admin("peek"), "peek", trust_unverified_email=True)
        peek = compose({}, admin)["peek"]
        assert peek["auto_provision"] == {
            "enabled": True,
            "key_env": "PRIVACYFENCE_PEEK_PROVISIONING_KEY",
            "trust_unverified_email": True,
        }
        config = PeekProvisioningConfig.from_org_config({"mode": "org", "peek": peek})
        assert config.enabled and config.base_url == "https://peek.example"
        assert config.trust_unverified_email

    def test_locked_address_enables_it(self):
        admin = enable_provisioning(
            make_admin("peek", peek={"base_url": "https://peek.example", "address_locked": True}), "peek"
        )
        assert compose({}, admin)["peek"]["auto_provision"]["enabled"] is True

    def test_admin_address_never_enables_it(self):
        admin = enable_provisioning(make_admin("peek", peek={"base_url": "https://peek.example"}), "peek")
        assert "auto_provision" not in compose({}, admin)["peek"]

    def test_switch_off(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        assert "auto_provision" not in compose({}, make_admin("peek"))["peek"]

    def test_local_mode(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        admin = enable_provisioning(make_admin("peek"), "peek")
        assert "auto_provision" not in compose({}, admin, mode="local")["peek"]

    def test_single_user(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        admin = enable_provisioning(make_admin("peek"), "peek")
        assert "auto_provision" not in compose({}, admin, single_user=True)["peek"]

    def test_no_address(self):
        admin = enable_provisioning(make_admin("peek"), "peek")
        assert "peek" not in compose({}, admin)


class TestGristAutoProvision:
    def environment(self, monkeypatch):
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")

    def test_environment_addresses_enable_it(self, monkeypatch):
        self.environment(monkeypatch)
        admin = enable_provisioning(make_admin("grist"), "grist")
        grist = compose({}, admin)["grist"]
        assert grist["auto_provision"] == {
            "enabled": True,
            "key_env": "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
            "trust_unverified_email": False,
            "provisioner_url": "https://provisioner.example",
        }
        config = GristProvisioningConfig.from_org_config({"mode": "org", "grist": grist})
        assert config.enabled
        assert config.server_url == "https://grist.example"
        assert config.provisioner_url == "https://provisioner.example"

    def test_imported_locked_addresses_enable_it(self):
        admin = enable_provisioning(
            make_admin("grist", grist={"server_url": "https://grist.example", "address_locked": True}),
            "grist",
            provisioner_url="https://provisioner.example",
        )
        grist = compose({}, admin)["grist"]
        assert GristProvisioningConfig.from_org_config({"mode": "org", "grist": grist}).enabled

    def test_locked_address_without_provisioner(self):
        admin = enable_provisioning(
            make_admin("grist", grist={"server_url": "https://grist.example", "address_locked": True}), "grist"
        )
        assert "auto_provision" not in compose({}, admin)["grist"]

    def test_environment_server_with_locked_provisioner(self, monkeypatch):
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        admin = enable_provisioning(
            make_admin("grist", grist={"address_locked": True}), "grist", provisioner_url="https://p.example"
        )
        assert compose({}, admin)["grist"]["auto_provision"]["provisioner_url"] == "https://p.example"

    def test_admin_address_never_enables_it(self, monkeypatch):
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")
        admin = enable_provisioning(make_admin("grist", grist={"server_url": "https://admin.example"}), "grist")
        assert "auto_provision" not in compose({}, admin)["grist"]

    def test_unlocked_stored_provisioner_is_not_used(self):
        admin = enable_provisioning(
            make_admin("grist", grist={"server_url": "https://admin.example"}),
            "grist",
            provisioner_url="https://provisioner.example",
        )
        assert "auto_provision" not in compose({}, admin)["grist"]

    def test_switch_off(self, monkeypatch):
        self.environment(monkeypatch)
        assert "auto_provision" not in compose({}, make_admin("grist"))["grist"]

    def test_local_mode(self, monkeypatch):
        self.environment(monkeypatch)
        admin = enable_provisioning(make_admin("grist"), "grist")
        assert "auto_provision" not in compose({}, admin, mode="local")["grist"]

    def test_single_user(self, monkeypatch):
        self.environment(monkeypatch)
        admin = enable_provisioning(make_admin("grist"), "grist")
        assert "auto_provision" not in compose({}, admin, single_user=True)["grist"]

    def test_oauth_app_means_no_auto_provision(self, monkeypatch):
        self.environment(monkeypatch)
        admin = enable_provisioning(make_admin("grist"), "grist")
        bundle = {"grist": {"client_id": "g", "client_secret": "s", "auth_server_url": "https://a"}}
        assert "auto_provision" not in compose(bundle, admin)["grist"]

    def test_missing_provisioner(self, monkeypatch):
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        admin = enable_provisioning(make_admin("grist"), "grist")
        assert "auto_provision" not in compose({}, admin)["grist"]


class TestEnvironmentAddresses:
    def test_empty(self):
        assert oauth_apps.environment_addresses() == {}

    def test_blank_values_are_left_out(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "  ")
        assert oauth_apps.environment_addresses() == {}

    def test_all_three_are_normalised(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "https://peek.example/")
        monkeypatch.setenv(GRIST_VAR, " https://grist.example/ ")
        monkeypatch.setenv(PROVISIONER_VAR, "https://p.example/")
        assert oauth_apps.environment_addresses() == {
            "peek": "https://peek.example",
            "grist": "https://grist.example",
            "grist_provisioner": "https://p.example",
        }

    @pytest.mark.parametrize("variable", [PEEK_VAR, GRIST_VAR, PROVISIONER_VAR])
    def test_bad_value_names_the_variable(self, monkeypatch, variable):
        monkeypatch.setenv(variable, "ftp://nope")
        with pytest.raises(ConfigurationError, match=variable):
            oauth_apps.environment_addresses()


class TestOperationalSections:
    def test_every_section_is_copied_from_admin(self):
        admin = make_admin()
        for key in admin_config.OPERATIONAL_SECTIONS:
            admin[key] = {"marker": key} if key not in ("rooms", "rooms_synced_at") else [key]
        result = compose({}, admin)
        for key in admin_config.OPERATIONAL_SECTIONS:
            assert result[key] == admin[key]

    def test_admin_wins_over_bundle(self):
        admin = make_admin()
        admin["authz"] = {"allowed_domains": ["a.example"]}
        result = compose({"authz": {"allowed_domains": ["b.example"]}}, admin)
        assert result["authz"] == {"allowed_domains": ["a.example"]}

    def test_absent_in_admin_stays_absent(self):
        admin = make_admin()
        del admin["authz"]
        assert "authz" not in compose({"authz": {"x": 1}}, admin)

    def test_result_does_not_alias_admin(self):
        admin = make_admin()
        admin["authz"] = {"allowed_domains": ["a.example"]}
        compose({}, admin)["authz"]["allowed_domains"].append("evil.example")
        assert admin["authz"] == {"allowed_domains": ["a.example"]}


def test_neither_input_is_mutated(builtins, monkeypatch):
    builtins("google_web", "atlassian_relay")
    monkeypatch.setenv(PEEK_VAR, "https://peek.example")
    bundle = {
        "slack": {"client_id": "i", "client_secret": "s", "relay": True},
        "atlassian": {"client_id": "a", "client_secret": "s", "assets": True},
        "grist": {"client_id": "g", "auto_provision": {"enabled": True}},
        "authz": {"x": 1},
        "mode": "org",
    }
    admin = enable_provisioning(
        make_admin("google", "slack", "atlassian", "grist", "peek"), "peek"
    )
    bundle_before, admin_before = copy.deepcopy(bundle), copy.deepcopy(admin)
    result = compose(bundle, admin)
    result["slack"]["client_id"] = "changed"
    result["atlassian"]["client_id"] = "changed"
    result["grist"]["client_id"] = "changed"
    assert bundle == bundle_before
    assert admin == admin_before


class TestCredentialsSource:
    def test_salesforce_bundle_needs_consumer_key(self, builtins):
        builtins()
        assert oauth_apps.credentials_source({"salesforce": {"client_id": "x"}}, "salesforce", mode="org") == "none"
        assert oauth_apps.credentials_source({"salesforce": {"consumer_key": "k"}}, "salesforce", mode="org") == "bundle"

    def test_bundle_beats_builtin(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({"slack": {"client_id": "i"}}, "slack", mode="org") == "bundle"

    def test_local_mode_never_builtin(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({}, "slack", mode="local") == "none"

    def test_malformed_section(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({"slack": "x"}, "slack", mode="org") == "builtin"


class TestRelayHelpers:
    def test_redirect_uri(self):
        assert oauth_apps.relay_redirect_uri("slack") == "https://oauth.privacyfence.eu/callback/slack"

    def test_uses_relay_needs_exactly_true(self):
        assert oauth_apps.uses_relay({"relay": True})
        assert not oauth_apps.uses_relay({"relay": "yes"})
        assert not oauth_apps.uses_relay({})


class TestSharedConfig:
    def test_refresh_keeps_the_same_object(self):
        target = {"a": 1, "stale": 2}
        same = target
        oauth_apps.refresh_in_place(target, {"a": 3, "b": 4})
        assert target is same
        assert target == {"a": 3, "b": 4}

    def test_snapshot_is_an_independent_copy(self):
        shared = {"a": 1}
        copy_ = oauth_apps.snapshot(shared)
        assert copy_ == shared
        assert copy_ is not shared
        shared["b"] = 2
        copy_["c"] = 3
        assert "b" not in copy_
        assert "c" not in shared

    def test_snapshot_waits_for_a_refresh(self):
        shared = {"a": 1}
        taken: list[dict] = []
        with oauth_apps._refresh_lock:
            thread = threading.Thread(target=lambda: taken.append(oauth_apps.snapshot(shared)))
            thread.start()
            thread.join(0.05)
            assert thread.is_alive()
        thread.join(5)
        assert taken == [{"a": 1}]
