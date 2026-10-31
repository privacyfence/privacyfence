"""The effective org config: bundle credentials, admin config and built-in apps composed."""
from __future__ import annotations

import copy
import threading

import pytest

from privacyfence import admin_config, app_credentials, connector_configs, oauth_apps
from privacyfence.org_mode import ConfigurationError, GristProvisioningConfig, PeekProvisioningConfig

BUILTIN = {"client_id": "builtin-id", "client_secret": "builtin-secret"}
PEEK_VAR = "PRIVACYFENCE_PEEK_BASE_URL"
GRIST_VAR = "PRIVACYFENCE_GRIST_SERVER_URL"
PROVISIONER_VAR = "PRIVACYFENCE_GRIST_PROVISIONER_URL"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for variable in (PEEK_VAR, GRIST_VAR, PROVISIONER_VAR, "PRIVACYFENCE_PEEK_PROVISIONING_KEY", "PRIVACYFENCE_GRIST_PROVISIONING_KEY"):
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


def compose(bundle, admin, *, mode="org", single_user=False, connectors=None):
    return oauth_apps.effective_org_config(
        bundle, admin, mode=mode, single_user=single_user, connectors={} if connectors is None else connectors
    )


def connector(provider, **fields):
    """A stored connector config for ``provider`` as ``load_all`` returns it."""
    if not fields:
        fields = bundle_section(provider)
    return {provider: connector_configs.LoadedConfig(provider, "connector-config", 1, "", "", dict(fields))}


def compose_core(bundle, admin, **kwargs):
    """``compose`` without the operational sections, which every admin config contributes."""
    result = compose(bundle, admin, **kwargs)
    return {k: v for k, v in result.items() if k not in admin_config.OPERATIONAL_SECTIONS}


class TestPreviewServices:
    VAR = "PRIVACYFENCE_PREVIEW_SERVICES"

    @pytest.fixture(autouse=True)
    def _slack_is_preview(self, monkeypatch, builtins):
        builtins("slack", "miro")
        monkeypatch.setattr(oauth_apps, "PREVIEW_SERVICES", frozenset({"slack"}))

    def test_a_preview_service_section_is_dropped(self):
        result = compose({}, make_admin("slack", "miro"))
        assert "slack" not in result and "miro" in result

    @pytest.mark.parametrize("value", ["slack", "miro,slack", " slack , miro", "*"])
    def test_a_named_preview_service_is_kept(self, monkeypatch, value):
        monkeypatch.setenv(self.VAR, value)
        assert "slack" in compose({}, make_admin("slack"))

    def test_naming_another_service_keeps_it_hidden(self, monkeypatch):
        monkeypatch.setenv(self.VAR, "miro")
        assert "slack" not in compose({}, make_admin("slack"))

    def test_an_empty_set_changes_nothing(self, monkeypatch):
        admin = make_admin("slack", "miro")
        monkeypatch.setattr(oauth_apps, "PREVIEW_SERVICES", frozenset())
        with_empty = compose({}, admin)
        monkeypatch.setenv(self.VAR, "*")
        assert compose({}, admin) == with_empty
        assert "slack" in with_empty


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
        result = compose(bundle, make_admin("atlassian"), mode="local")
        assert result["atlassian"] == {"client_id": "a", "client_secret": "s", "assets": False}
        assert "grist" not in result

    def test_oauth_grist_keeps_its_own_server_url_and_loses_auto_provision(self):
        grist = {"client_id": "g", "client_secret": "s", "server_url": "https://g.example", "auto_provision": {"enabled": True}}
        result = compose({"grist": grist}, make_admin("grist"), mode="local")
        assert result["grist"] == {"client_id": "g", "client_secret": "s", "server_url": "https://g.example"}

    def test_bundle_relay_marker_is_stripped(self):
        bundle = {"slack": {"client_id": "i", "client_secret": "s", "relay": True}, "mode": "org"}
        result = compose(bundle, make_admin("slack"), mode="local")
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
        source = oauth_apps.credentials_source(bundle, service, mode=mode, connectors={})
        if case == "bundle" and mode == "local":
            assert result[service] == bundle_section(service)
            assert source == "bundle"
        elif case in ("builtin", "bundle") and mode == "org":
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
        connectors = connector("atlassian", client_id="a", client_secret="s")
        admin = make_admin("atlassian", atlassian={"assets": True, "assets_workspace_id": "ws-1"})
        assert compose({}, admin, connectors=connectors)["atlassian"] == {
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


class TestSalesforceLoginUrl:
    URL = "https://acme.my.salesforce.com"

    def test_the_admin_login_url_reaches_the_builtin_section(self, builtins):
        builtins("salesforce")
        result = compose({}, make_admin("salesforce", salesforce={"login_url": self.URL}))
        assert result["salesforce"] == {**expected_builtin("salesforce"), "login_url": self.URL}

    def test_no_login_url_leaves_the_section_without_one(self, builtins):
        builtins("salesforce")
        assert "login_url" not in compose({}, make_admin("salesforce"))["salesforce"]

    def test_a_connector_config_without_one_gets_the_admin_url(self):
        connectors = connector("salesforce", consumer_key="k", consumer_secret="s")
        result = compose({}, make_admin("salesforce", salesforce={"login_url": self.URL}), connectors=connectors)
        assert result["salesforce"]["login_url"] == self.URL

    def test_a_connector_configs_own_login_url_wins(self):
        connectors = connector(
            "salesforce", consumer_key="k", consumer_secret="s", login_url="https://test.salesforce.com"
        )
        result = compose({}, make_admin("salesforce", salesforce={"login_url": self.URL}), connectors=connectors)
        assert result["salesforce"]["login_url"] == "https://test.salesforce.com"

    def test_a_local_bundles_own_login_url_wins(self):
        bundle = {"salesforce": {"consumer_key": "k", "consumer_secret": "s", "login_url": "https://test.salesforce.com"}}
        result = compose(bundle, make_admin("salesforce", salesforce={"login_url": self.URL}), mode="local")
        assert result["salesforce"]["login_url"] == "https://test.salesforce.com"

    @pytest.mark.parametrize("stored", ["https://evil.com", "http://login.salesforce.com", "https://login.salesforce.com@evil.com"])
    def test_a_stored_url_that_is_not_salesforces_is_ignored(self, builtins, stored):
        builtins("salesforce")
        assert "login_url" not in compose({}, make_admin("salesforce", salesforce={"login_url": stored}))["salesforce"]

    def test_a_withdrawn_service_has_no_section(self, builtins):
        builtins("salesforce")
        assert "salesforce" not in compose({}, make_admin(salesforce={"login_url": self.URL}))


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
        assert compose({"grist": grist}, admin, mode="local")["grist"] == grist
        assert compose({}, admin, connectors=connector("grist", **grist))["grist"] == grist

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
        own = {"client_id": "g", "client_secret": "s", "auth_server_url": "https://a", "server_url": "https://g"}
        assert "auto_provision" not in compose({}, admin, connectors=connector("grist", **own))["grist"]

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


PEEK_AUTO = "PRIVACYFENCE_PEEK_AUTO_PROVISION"
GRIST_AUTO = "PRIVACYFENCE_GRIST_AUTO_PROVISION"
PEEK_KEY = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"
GRIST_KEY = "PRIVACYFENCE_GRIST_PROVISIONING_KEY"


class TestEnvironmentAutoProvision:
    def test_empty(self):
        assert oauth_apps.environment_auto_provision() == frozenset()

    @pytest.mark.parametrize("variable", [PEEK_AUTO, GRIST_AUTO])
    def test_env_auto_provision_bad_value(self, monkeypatch, variable):
        monkeypatch.setenv(variable, "yes")
        with pytest.raises(ConfigurationError, match=f"{variable} must be true, or unset."):
            oauth_apps.environment_auto_provision()

    def test_env_auto_provision_case_and_space(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "  TrUe ")
        monkeypatch.setenv(GRIST_AUTO, "   ")
        assert oauth_apps.environment_auto_provision() == frozenset({"peek"})

    def test_peek_section_from_env_ignores_stored_off(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        admin = make_admin("peek")
        admin["services"]["peek"]["auto_provision"]["enabled"] = False
        admin["services"]["peek"]["auto_provision"]["trust_unverified_email"] = True
        peek = compose({}, admin)["peek"]
        assert peek["auto_provision"] == {
            "enabled": True,
            "key_env": "PRIVACYFENCE_PEEK_PROVISIONING_KEY",
            "trust_unverified_email": True,
        }
        assert admin["services"]["peek"]["auto_provision"]["enabled"] is False

    def test_grist_section_from_env(self, monkeypatch):
        monkeypatch.setenv(GRIST_AUTO, "true")
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")
        grist = compose({}, make_admin("grist"))["grist"]
        assert grist["auto_provision"] == {
            "enabled": True,
            "key_env": "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
            "trust_unverified_email": False,
            "provisioner_url": "https://provisioner.example",
        }

    def test_env_offers_service(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        admin = make_admin()
        assert admin["services"]["peek"]["offered"] is False
        assert "peek" in compose({}, admin)
        assert admin["services"]["peek"]["offered"] is False

    def test_single_user_section_with_env(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "http://localhost:7700")
        peek = compose({}, make_admin(), single_user=True)["peek"]
        assert peek["auto_provision"]["enabled"] is True

    def test_single_user_section_without_env(self, monkeypatch):
        monkeypatch.setenv(PEEK_VAR, "http://localhost:7700")
        admin = enable_provisioning(make_admin("peek"), "peek")
        assert "auto_provision" not in compose({}, admin, single_user=True)["peek"]


class TestCheckEnvironmentAutoProvision:
    @staticmethod
    def check(*, single_user=False, grist_oauth=False):
        oauth_apps.check_environment_auto_provision(single_user=single_user, grist_oauth=grist_oauth)

    def test_nothing_set_passes(self):
        self.check()

    def test_peek_missing_address(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_KEY, "k")
        with pytest.raises(ConfigurationError) as raised:
            self.check()
        assert str(raised.value) == f"{PEEK_AUTO}=true needs {PEEK_VAR}."

    def test_grist_missing_address(self, monkeypatch):
        monkeypatch.setenv(GRIST_AUTO, "true")
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        monkeypatch.setenv(GRIST_KEY, "k")
        with pytest.raises(ConfigurationError) as raised:
            self.check()
        assert str(raised.value) == f"{GRIST_AUTO}=true needs {GRIST_VAR} and {PROVISIONER_VAR}."

    def test_peek_missing_key(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        monkeypatch.delenv(PEEK_KEY, raising=False)
        monkeypatch.delenv(f"{PEEK_KEY}_FILE", raising=False)
        with pytest.raises(ConfigurationError) as raised:
            self.check()
        assert str(raised.value) == f"{PEEK_AUTO}=true needs {PEEK_KEY} or {PEEK_KEY}_FILE."

    def test_grist_missing_key(self, monkeypatch):
        monkeypatch.setenv(GRIST_AUTO, "true")
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")
        monkeypatch.delenv(GRIST_KEY, raising=False)
        monkeypatch.delenv(f"{GRIST_KEY}_FILE", raising=False)
        with pytest.raises(ConfigurationError) as raised:
            self.check()
        assert str(raised.value) == f"{GRIST_AUTO}=true needs {GRIST_KEY} or {GRIST_KEY}_FILE."

    def test_custom_key_env_is_named(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        monkeypatch.setenv(PEEK_KEY, "default-key-is-not-the-one")
        stored = make_admin("peek")
        stored["services"]["peek"]["auto_provision"]["key_env"] = "MY_PEEK_KEY"
        monkeypatch.delenv("MY_PEEK_KEY", raising=False)
        monkeypatch.delenv("MY_PEEK_KEY_FILE", raising=False)
        monkeypatch.setattr(admin_config, "config_for", lambda *a, **kw: stored)
        with pytest.raises(ConfigurationError) as raised:
            self.check()
        assert str(raised.value) == f"{PEEK_AUTO}=true needs MY_PEEK_KEY or MY_PEEK_KEY_FILE."

    def test_grist_oauth_app(self, monkeypatch):
        monkeypatch.setenv(GRIST_AUTO, "true")
        monkeypatch.setenv(GRIST_VAR, "https://grist.example")
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")
        monkeypatch.setenv(GRIST_KEY, "k")
        with pytest.raises(ConfigurationError) as raised:
            self.check(grist_oauth=True)
        assert str(raised.value) == f"{GRIST_AUTO}=true cannot be used with a Grist OAuth app."

    def test_single_user_non_loopback_peek(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "https://peek.example")
        monkeypatch.setenv(PEEK_KEY, "k")
        with pytest.raises(ConfigurationError) as raised:
            self.check(single_user=True)
        assert str(raised.value) == (
            f"On a single-user install, {PEEK_AUTO}=true needs a localhost address in {PEEK_VAR}."
        )

    def test_single_user_non_loopback_grist(self, monkeypatch):
        monkeypatch.setenv(GRIST_AUTO, "true")
        monkeypatch.setenv(GRIST_VAR, "http://localhost:8484")
        monkeypatch.setenv(PROVISIONER_VAR, "https://provisioner.example")
        monkeypatch.setenv(GRIST_KEY, "k")
        with pytest.raises(ConfigurationError) as raised:
            self.check(single_user=True)
        assert str(raised.value) == (
            f"On a single-user install, {GRIST_AUTO}=true needs localhost addresses in {GRIST_VAR} and {PROVISIONER_VAR}."
        )

    def test_org_server_with_both_services_passes(self, monkeypatch):
        for variable, value in (
            (PEEK_AUTO, "true"), (GRIST_AUTO, "true"), (PEEK_VAR, "https://peek.example"),
            (GRIST_VAR, "https://grist.example"), (PROVISIONER_VAR, "https://provisioner.example"),
            (PEEK_KEY, "k"), (GRIST_KEY, "k"),
        ):
            monkeypatch.setenv(variable, value)
        self.check()

    def test_single_user_loopback_passes(self, monkeypatch):
        monkeypatch.setenv(PEEK_AUTO, "true")
        monkeypatch.setenv(PEEK_VAR, "http://[::1]:7700")
        monkeypatch.setenv(PEEK_KEY, "k")
        self.check(single_user=True)


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
    result = compose(bundle, admin, mode="local")
    result["slack"]["client_id"] = "changed"
    result["atlassian"]["client_id"] = "changed"
    result["grist"]["client_id"] = "changed"
    assert bundle == bundle_before
    assert admin == admin_before


class TestCredentialsSource:
    def test_salesforce_bundle_needs_consumer_key(self, builtins):
        builtins()
        source = oauth_apps.credentials_source
        assert source({"salesforce": {"client_id": "x"}}, "salesforce", mode="local") == "none"
        assert source({"salesforce": {"consumer_key": "k"}}, "salesforce", mode="local") == "bundle"

    def test_local_mode_bundle_beats_builtin(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({"slack": {"client_id": "i"}}, "slack", mode="local") == "bundle"

    def test_organization_mode_never_reads_the_bundle(self, builtins):
        builtins()
        bundle = {"slack": {"client_id": "i", "client_secret": "s"}}
        assert oauth_apps.credentials_source(bundle, "slack", mode="org", connectors={}) == "none"
        builtins("slack")
        assert oauth_apps.credentials_source(bundle, "slack", mode="org", connectors={}) == "builtin"

    def test_local_mode_never_builtin(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({}, "slack", mode="local") == "none"

    def test_malformed_section(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({"slack": "x"}, "slack", mode="org", connectors={}) == "builtin"
        assert oauth_apps.credentials_source({"slack": "x"}, "slack", mode="local") == "none"

    def test_connector_beats_builtin_in_organization_mode(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({}, "slack", mode="org", connectors=connector("slack")) == "connector"

    def test_local_mode_ignores_connector_configs(self, builtins):
        builtins("slack")
        assert oauth_apps.credentials_source({}, "slack", mode="local", connectors=connector("slack")) == "none"


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


class TestConnectorConfigs:
    """In organization mode a provider's app is its connector config, else the built-in app, else none."""

    @pytest.mark.parametrize("service", ["google", "slack", "salesforce", "miro", "atlassian"])
    def test_connector_beats_builtin_and_is_not_relayed(self, builtins, service):
        builtins(oauth_apps.BUILTIN_KEY[service])
        section = compose({}, make_admin(service), connectors=connector(service))[service]
        assert not oauth_apps.uses_relay(section)
        assert {k: v for k, v in section.items() if k not in ("auth_uri", "token_uri", "assets")} == bundle_section(service)

    @pytest.mark.parametrize("service", ["google", "slack", "salesforce", "miro"])
    def test_builtin_beats_nothing_and_the_bundle_is_ignored(self, builtins, service):
        builtins(oauth_apps.BUILTIN_KEY[service])
        bundle = {service: bundle_section(service)}
        assert compose(bundle, make_admin(service))[service] == expected_builtin(service)
        builtins()
        assert service not in compose(bundle, make_admin(service))

    def test_google_gets_its_endpoints(self):
        section = compose({}, make_admin("google"), connectors=connector("google"))["google"]
        assert section["auth_uri"] == oauth_apps.GOOGLE_AUTH_URI
        assert section["token_uri"] == oauth_apps.GOOGLE_TOKEN_URI

    def test_an_imported_google_config_keeps_its_endpoints(self):
        fields = {**bundle_section("google"), "auth_uri": "https://auth.example", "token_uri": "https://token.example"}
        section = compose({}, make_admin("google"), connectors=connector("google", **fields))["google"]
        assert section == fields

    def test_a_withdrawn_service_loses_its_connector_config(self):
        assert "slack" not in compose({}, make_admin(), connectors=connector("slack"))

    def test_grist_connector_beats_the_admin_address(self):
        own = {"client_id": "g", "client_secret": "s", "auth_server_url": "https://a", "server_url": "https://own"}
        admin = make_admin("grist", grist={"server_url": "https://admin.example"})
        assert compose({}, admin, connectors=connector("grist", **own))["grist"] == own

    def test_the_bundles_credential_sections_are_dropped_even_for_unoffered_services(self):
        bundle = {"mode": "org", **{p: {"client_id": "x", "client_secret": "y"} for p in connector_configs.PROVIDERS}}
        assert compose_core(bundle, make_admin()) == {"mode": "org"}

    def test_the_bundle_is_still_read_for_mode_server_and_idp(self):
        bundle = {"mode": "org", "server": {"issuer_url": "https://pf.example"}, "idp": {"issuer": "https://i"}}
        assert compose_core(bundle, make_admin()) == bundle

    def test_local_mode_is_unchanged_and_ignores_connectors(self):
        bundle = {"slack": bundle_section("slack")}
        result = compose(bundle, make_admin("slack"), mode="local", connectors=connector("slack", client_id="c", client_secret="d"))
        assert result["slack"] == bundle_section("slack")

    def test_stored_configs_are_read_from_disk_when_none_are_given(self, tmp_path):
        # The autouse fixture points the default directory at tmp_path.
        from tests.connector_config_support import SENTINEL, fixture_key, public_bytes, sign

        org = connector_configs.default_org_dir()
        connector_configs.accept(
            org, sign("slack"), confirmed_fingerprint=connector_configs.fingerprint(public_bytes(fixture_key()))
        )
        admin = make_admin("slack")
        result = oauth_apps.effective_org_config({}, admin, mode="org")
        assert result["slack"] == {"client_id": "slack-id", "client_secret": SENTINEL}
        assert "slack" not in oauth_apps.effective_org_config({}, admin, mode="local")

    def test_a_stored_config_that_no_longer_verifies_refuses(self, tmp_path):
        from tests.connector_config_support import fixture_key, public_bytes, sign

        org = connector_configs.default_org_dir()
        connector_configs.accept(
            org, sign("slack"), confirmed_fingerprint=connector_configs.fingerprint(public_bytes(fixture_key()))
        )
        path = connector_configs.stored_path(org, "slack")
        path.write_bytes(path.read_bytes().replace(b"slack-id", b"evil-id"))
        with pytest.raises(ConfigurationError, match="slack"):
            oauth_apps.effective_org_config({}, make_admin("slack"), mode="org")


class TestSlackLimited:
    def test_the_built_in_app_is_limited_until_it_is_listed(self, monkeypatch):
        assert oauth_apps.slack_limited({"client_id": "x", "relay": True})
        monkeypatch.setattr(oauth_apps, "SLACK_MARKETPLACE_LISTED", True)
        assert not oauth_apps.slack_limited({"client_id": "x", "relay": True})

    def test_an_own_app_and_no_section_are_not_limited(self):
        assert not oauth_apps.slack_limited({"client_id": "x", "client_secret": "y"})
        assert not oauth_apps.slack_limited({})
        assert not oauth_apps.slack_limited(None)

    def test_the_effective_section_decides(self, builtins):
        builtins("slack")
        admin = make_admin("slack")
        assert oauth_apps.slack_limited(compose({}, admin)["slack"])
        own = connector("slack", client_id="own", client_secret="s")
        assert not oauth_apps.slack_limited(compose({}, admin, connectors=own)["slack"])


class TestSlackManifest:
    def test_redirect_url_scopes_and_settings(self):
        from privacyfence.slack_client import DEFAULT_USER_SCOPES

        manifest = oauth_apps.slack_manifest("https://pf.example.org/")
        assert manifest["display_information"]["name"] == "PrivacyFence"
        assert manifest["oauth_config"]["redirect_urls"] == ["https://pf.example.org/oauth/callback/slack"]
        assert manifest["oauth_config"]["scopes"] == {"user": DEFAULT_USER_SCOPES}
        assert "bot" not in manifest["oauth_config"]["scopes"]
        assert manifest["settings"] == {
            "org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False,
        }

    def test_the_create_url_decodes_to_the_manifest(self):
        import json
        from urllib.parse import parse_qs, urlparse

        url = oauth_apps.slack_create_app_url("https://pf.example.org")
        parsed = urlparse(url)
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "api.slack.com", "/apps")
        query = parse_qs(parsed.query)
        assert query["new_app"] == ["1"]
        assert json.loads(query["manifest_json"][0]) == oauth_apps.slack_manifest("https://pf.example.org")

    def test_the_builtin_slack_row_carries_the_link_only_while_limited(self, builtins, monkeypatch):
        builtins("slack")
        admin = make_admin("slack")

        def slack_row():
            rows = admin_config.service_rows({}, admin, mode="org", issuer_url="https://pf.example.org")
            return next(row for row in rows if row["key"] == "slack")

        options = slack_row()["options"]
        assert options["slack_limited"] is True
        assert options["create_app_url"] == oauth_apps.slack_create_app_url("https://pf.example.org")
        monkeypatch.setattr(oauth_apps, "SLACK_MARKETPLACE_LISTED", True)
        assert "slack_limited" not in slack_row()["options"]
