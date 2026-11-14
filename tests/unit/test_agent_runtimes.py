"""Tests for the agent-runtime configuration and the deliver_to request."""

from __future__ import annotations

import logging

import pytest

from privacyfence import agent_runtimes as ar
from privacyfence.n8n_api import N8nApi
from privacyfence.org_mode import ConfigurationError

URL = "https://n8n.internal.example"


def config(**kw):
    values = {"enabled": True, "base_url": URL, "project_id": "", "mcp_url": ""}
    values.update(kw)
    return ar.N8nRuntimeConfig(**values)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("PRIVACYFENCE_N8N_API_KEY", "PRIVACYFENCE_N8N_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)


class TestFromOrgConfig:
    def test_defaults(self):
        cfg = ar.N8nRuntimeConfig.from_org_config({}, environ={})
        assert cfg == ar.N8nRuntimeConfig(False, "", "", "")

    def test_values_and_injected_environ(self):
        org = {
            "agent_runtimes": {"n8n": {"enabled": True, "project_id": "abc123", "mcp_url": "https://pf.example/mcp"}}
        }
        cfg = ar.N8nRuntimeConfig.from_org_config(org, environ={"PRIVACYFENCE_N8N_URL": URL + "/"})
        assert cfg == ar.N8nRuntimeConfig(True, URL, "abc123", "https://pf.example/mcp")

    def test_reads_os_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_N8N_URL", URL)
        assert ar.N8nRuntimeConfig.from_org_config({}).base_url == URL

    @pytest.mark.parametrize(
        "org",
        [
            {"agent_runtimes": []},
            {"agent_runtimes": {"n8n": []}},
            {"agent_runtimes": {"n8n": {"enabled": "yes"}}},
            {"agent_runtimes": {"n8n": {"project_id": "a b"}}},
            {"agent_runtimes": {"n8n": {"project_id": 5}}},
            {"agent_runtimes": {"n8n": {"project_id": "a" * 37}}},
            {"agent_runtimes": {"n8n": {"mcp_url": "http://evil.example/mcp"}}},
            {"agent_runtimes": {"n8n": {"mcp_url": "ftp://x"}}},
            {"agent_runtimes": {"n8n": {"mcp_url": 5}}},
            {"agent_runtimes": {"n8n": {"mcp_url": "https://u:p@x.example/mcp"}}},
            {"agent_runtimes": {"n8n": {"mcp_url": "https://x.example:99999/mcp"}}},
            {"agent_runtimes": {"n8n": {"mcp_url": "https:///mcp"}}},
        ],
    )
    def test_bad_section_raises(self, org):
        with pytest.raises(ConfigurationError):
            ar.N8nRuntimeConfig.from_org_config(org, environ={})

    def test_malformed_env_url_raises(self):
        with pytest.raises(ConfigurationError):
            ar.N8nRuntimeConfig.from_org_config({}, environ={"PRIVACYFENCE_N8N_URL": "http://n8n.example"})

    @pytest.mark.parametrize("mcp", ["http://localhost:8080/mcp", "http://127.0.0.1/mcp", "http://[::1]:1/mcp"])
    def test_loopback_http_mcp_url_is_accepted(self, mcp):
        org = {"agent_runtimes": {"n8n": {"mcp_url": mcp}}}
        assert ar.N8nRuntimeConfig.from_org_config(org, environ={}).mcp_url == mcp


class TestCredentialType:
    def test_default_is_bearer(self):
        cfg = ar.N8nRuntimeConfig.from_org_config({}, environ={})
        assert cfg.credential_type == "httpBearerAuth" == ar.DEFAULT_CREDENTIAL_TYPE
        assert ar.CREDENTIAL_TYPES == ("httpBearerAuth", "privacyFenceApi")

    def test_privacyfence_api_with_mcp_url(self):
        org = {"agent_runtimes": {"n8n": {"credential_type": "privacyFenceApi", "mcp_url": "https://pf.example/mcp"}}}
        assert ar.N8nRuntimeConfig.from_org_config(org, environ={}).credential_type == "privacyFenceApi"

    @pytest.mark.parametrize("value", ["", "other", 5, None])
    def test_other_value_is_refused(self, value):
        org = {"agent_runtimes": {"n8n": {"credential_type": value}}}
        with pytest.raises(ConfigurationError) as err:
            ar.N8nRuntimeConfig.from_org_config(org, environ={})
        assert str(err.value) == "agent_runtimes.n8n.credential_type must be httpBearerAuth or privacyFenceApi."

    def test_privacyfence_api_needs_an_mcp_url(self):
        org = {"agent_runtimes": {"n8n": {"credential_type": "privacyFenceApi"}}}
        with pytest.raises(ConfigurationError) as err:
            ar.N8nRuntimeConfig.from_org_config(org, environ={})
        assert str(err.value) == (
            "agent_runtimes.n8n.mcp_url is required for the privacyFenceApi credential type: "
            "the credential keeps this server's address."
        )

    def test_delivery_message_follows_the_type(self):
        assert ar.delivery_message("httpBearerAuth") == ar.DELIVERY_MESSAGE
        text = ar.delivery_message("privacyFenceApi")
        assert text == ar.DELIVERY_MESSAGE_PRIVACYFENCE_NODE
        assert "n8n-nodes-privacyfence" in text and "privacyFenceApi" in text

    def test_constants(self):
        assert ar.N8N_NODE_PACKAGE == "n8n-nodes-privacyfence"
        assert ar.N8N_NODE_TYPE == "n8n-nodes-privacyfence.privacyFence"


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://n8n.example.com", "https://n8n.example.com"),
            ("https://n8n.example.com/", "https://n8n.example.com"),
            (" https://n8n.example.com/sub/ ", "https://n8n.example.com/sub"),
            ("https://10.0.0.5:5678", "https://10.0.0.5:5678"),
            ("https://192.168.1.2", "https://192.168.1.2"),
            ("https://203.0.113.5:5678", "https://203.0.113.5:5678"),
            ("https://n8n1.example.com", "https://n8n1.example.com"),
            ("https://1.example.com", "https://1.example.com"),
            ("https://127.0.0.1:5678", "https://127.0.0.1:5678"),
            ("https://[::1]:5678", "https://[::1]:5678"),
        ],
    )
    def test_accepted(self, raw, expected):
        assert ar.normalize_n8n_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "http://n8n.example.com",
            "https://169.254.169.254",
            "https://[fe80::1]",
            "https://0.0.0.0",
            "https://[::]",
            "https://224.0.0.1",
            "https://[ff02::1]",
            "https://metadata.google.internal",
            "https://Metadata.Google.Internal./x",
            "https://2852039166",
            "https://0",
            "https://0x7f.1",
            "https://0177.0.0.1",
            "https://1.2.3",
            "https://2852039166./x",
            "https://user:pw@n8n.example.com",
            "https://user@n8n.example.com",
            "https://n8n.example.com?x=1",
            "https://n8n.example.com/?",
            "https://n8n.example.com#frag",
            "https://n8n.example.com:99999",
            "https://",
            "",
            "n8n.example.com",
        ],
    )
    def test_refused(self, raw):
        with pytest.raises(ConfigurationError) as err:
            ar.normalize_n8n_url(raw)
        assert "PRIVACYFENCE_N8N_URL" in str(err.value)


class TestEndpoint:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://N8N.Example.com/x", ("https", "n8n.example.com", 443)),
            ("http://localhost/mcp", ("http", "localhost", 80)),
            ("https://h:8443", ("https", "h", 8443)),
        ],
    )
    def test_endpoint(self, url, expected):
        assert ar.endpoint(url) == expected


class TestParseDeliverTo:
    def test_valid(self):
        assert ar.parse_deliver_to({"runtime": "n8n"}) == ar.DeliveryTarget("n8n", "")
        assert ar.parse_deliver_to({"runtime": "n8n", "workflow_id": ""}).workflow_id == ""
        assert ar.parse_deliver_to({"runtime": "n8n", "workflow_id": "Ab12"}).workflow_id == "Ab12"

    @pytest.mark.parametrize("raw", [None, "n8n", ["n8n"], 5])
    def test_not_a_dict(self, raw):
        with pytest.raises(ValueError, match=r"^deliver_to is \{runtime: 'n8n', workflow_id\}\.$"):
            ar.parse_deliver_to(raw)

    def test_unknown_key(self):
        with pytest.raises(ValueError, match=r"^deliver_to takes only runtime and workflow_id\.$"):
            ar.parse_deliver_to({"runtime": "n8n", "extra": 1})

    @pytest.mark.parametrize("raw", [{}, {"runtime": "zapier"}, {"workflow_id": "ab"}])
    def test_other_runtime(self, raw):
        with pytest.raises(ValueError, match=r"^The only runtime PrivacyFence delivers to is n8n\.$"):
            ar.parse_deliver_to(raw)

    @pytest.mark.parametrize("wf", ["a b", "a" * 37, 5, None, "../x"])
    def test_bad_workflow_id(self, wf):
        with pytest.raises(ValueError, match=r"^workflow_id is the n8n workflow's id: 1 to 36 letters and digits\.$"):
            ar.parse_deliver_to({"runtime": "n8n", "workflow_id": wf})


class TestDeliveryAvailable:
    def test_needs_everything(self, monkeypatch):
        assert ar.delivery_available(None) is False
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY", "k")
        assert ar.delivery_available(config()) is True
        assert ar.delivery_available(config(enabled=False)) is False
        assert ar.delivery_available(config(base_url="")) is False

    def test_no_key(self):
        assert ar.delivery_available(config()) is False

    def test_conflicting_key_variables_are_unavailable_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY", "k")
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY_FILE", "/nope")
        with caplog.at_level(logging.WARNING):
            assert ar.delivery_available(config()) is False
        assert "not both" in caplog.text


class TestApiKey:
    def test_raises_on_both(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY", "k")
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY_FILE", "/nope")
        with pytest.raises(ConfigurationError):
            ar.api_key()

    def test_unset_is_none(self):
        assert ar.api_key() is None


class TestNames:
    def test_credential_name(self):
        assert ar.credential_name("Daily digest") == "PrivacyFence Daily digest"


class TestMcpUrlFor:
    def test_override_wins(self):
        assert ar.mcp_url_for(config(mcp_url="https://pf.example/mcp"), "https://x/") == "https://pf.example/mcp"

    def test_default_appends_mcp(self):
        assert ar.mcp_url_for(config(), "https://pf.example/") == "https://pf.example/mcp"


class TestApiFor:
    def test_none_without_key(self):
        assert ar.api_for(config()) is None

    def test_none_without_url(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY", "k")
        assert ar.api_for(config(base_url="")) is None

    def test_key_from_env(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY", "k")
        api = ar.api_for(config())
        assert isinstance(api, N8nApi)
        assert repr(api) == "N8nApi(n8n.internal.example)"

    def test_key_from_file(self, monkeypatch, tmp_path):
        path = tmp_path / "key"
        path.write_text("filekey\n")
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY_FILE", str(path))
        assert isinstance(ar.api_for(config()), N8nApi)

    def test_broken_key_config_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRIVACYFENCE_N8N_API_KEY_FILE", str(tmp_path / "missing"))
        assert ar.api_for(config()) is None


class TestEnvironmentLocks:
    def test_unset_and_empty_lock_nothing(self):
        assert ar.environment_locks({}) == {"agent_runtimes": {}, "scoped_tokens": {}}
        env = {"PRIVACYFENCE_N8N_DELIVERY": " ", "PRIVACYFENCE_N8N_PROJECT_ID": "", "PRIVACYFENCE_N8N_MCP_URL": "  "}
        assert ar.environment_locks(env) == {"agent_runtimes": {}, "scoped_tokens": {}}

    def test_delivery_gives_three_locks(self):
        locks = ar.environment_locks({"PRIVACYFENCE_N8N_DELIVERY": "true"})
        var = "PRIVACYFENCE_N8N_DELIVERY"
        assert locks["agent_runtimes"] == {"enabled": ar.EnvLock(True, var)}
        assert locks["scoped_tokens"] == {
            "enabled": ar.EnvLock(True, var), "allow_ai_proposals": ar.EnvLock(True, var),
        }

    def test_project_id_alone_locks_only_the_project_id(self):
        locks = ar.environment_locks({"PRIVACYFENCE_N8N_PROJECT_ID": "  abc123 "})
        assert locks == {
            "agent_runtimes": {"project_id": ar.EnvLock("abc123", "PRIVACYFENCE_N8N_PROJECT_ID")},
            "scoped_tokens": {},
        }

    def test_mcp_url_alone_locks_only_the_mcp_url(self):
        locks = ar.environment_locks({"PRIVACYFENCE_N8N_MCP_URL": " https://pf.example.com/mcp "})
        assert locks["agent_runtimes"] == {"mcp_url": ar.EnvLock("https://pf.example.com/mcp", "PRIVACYFENCE_N8N_MCP_URL")}
        assert locks["scoped_tokens"] == {}

    @pytest.mark.parametrize("value", ["false", "1", "yes"])
    def test_delivery_other_than_true_is_refused(self, value):
        with pytest.raises(ConfigurationError, match="PRIVACYFENCE_N8N_DELIVERY must be true, or unset."):
            ar.environment_locks({"PRIVACYFENCE_N8N_DELIVERY": value})

    @pytest.mark.parametrize("value", ["has space", "a" * 37, "x/y"])
    def test_a_bad_project_id_is_refused(self, value):
        with pytest.raises(ConfigurationError, match="PRIVACYFENCE_N8N_PROJECT_ID must be 1 to 36 letters and digits."):
            ar.environment_locks({"PRIVACYFENCE_N8N_PROJECT_ID": value})

    @pytest.mark.parametrize("value", ["http://pf.example.com/mcp", "ftp://x", "nonsense"])
    def test_a_bad_mcp_url_is_refused(self, value):
        with pytest.raises(ConfigurationError, match="PRIVACYFENCE_N8N_MCP_URL must be an https:// URL"):
            ar.environment_locks({"PRIVACYFENCE_N8N_MCP_URL": value})

    def test_it_reads_the_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_N8N_DELIVERY", "true")
        assert "enabled" in ar.environment_locks()["agent_runtimes"]


class TestApplyEnvironment:
    def test_locks_overwrite_stored_values(self):
        config_ = {
            "agent_runtimes": {"n8n": {"enabled": False, "project_id": "old", "credential_type": "httpBearerAuth"}},
            "scoped_tokens": {"enabled": False, "max_days": 7, "allow_ai_proposals": False},
        }
        env = {"PRIVACYFENCE_N8N_DELIVERY": "true", "PRIVACYFENCE_N8N_PROJECT_ID": "new"}
        ar.apply_environment(config_, env)
        assert config_["agent_runtimes"]["n8n"] == {
            "enabled": True, "project_id": "new", "credential_type": "httpBearerAuth",
        }
        assert config_["scoped_tokens"] == {"enabled": True, "max_days": 7, "allow_ai_proposals": True}

    def test_missing_sections_are_created(self):
        config_: dict = {"agent_runtimes": "not a dict"}
        ar.apply_environment(config_, {"PRIVACYFENCE_N8N_DELIVERY": "true"})
        assert config_ == {
            "agent_runtimes": {"n8n": {"enabled": True}},
            "scoped_tokens": {"enabled": True, "allow_ai_proposals": True},
        }

    def test_no_locks_leaves_the_config_unchanged(self):
        config_ = {"agent_runtimes": {"n8n": {"enabled": False}}}
        ar.apply_environment(config_, {})
        assert config_ == {"agent_runtimes": {"n8n": {"enabled": False}}}

    def test_the_environment_mapping_is_never_mutated(self):
        env = {"PRIVACYFENCE_N8N_DELIVERY": "true"}
        ar.apply_environment({}, env)
        assert env == {"PRIVACYFENCE_N8N_DELIVERY": "true"}


class TestCheckEnvironment:
    DELIVERY = {"PRIVACYFENCE_N8N_DELIVERY": "true"}

    def test_it_refuses_without_the_address(self):
        with pytest.raises(ConfigurationError, match="PRIVACYFENCE_N8N_DELIVERY=true needs PRIVACYFENCE_N8N_URL"):
            ar.check_environment(config(base_url=""), "key", self.DELIVERY)

    def test_it_refuses_without_the_key(self):
        with pytest.raises(ConfigurationError, match="PRIVACYFENCE_N8N_API_KEY_FILE"):
            ar.check_environment(config(), None, self.DELIVERY)

    def test_it_passes_with_both(self):
        ar.check_environment(config(), "key", self.DELIVERY)

    def test_it_passes_when_delivery_is_unset(self):
        ar.check_environment(config(base_url=""), None, {})

    def test_a_bad_value_is_let_through(self):
        with pytest.raises(ConfigurationError, match="must be true, or unset"):
            ar.check_environment(config(), "key", {"PRIVACYFENCE_N8N_DELIVERY": "false"})


class TestSetEnvironmentVariables:
    def test_it_lists_the_set_ones_sorted(self):
        env = {"PRIVACYFENCE_N8N_PROJECT_ID": "x", "PRIVACYFENCE_N8N_DELIVERY": "false", "PRIVACYFENCE_N8N_MCP_URL": " "}
        assert ar.set_environment_variables(env) == ["PRIVACYFENCE_N8N_DELIVERY", "PRIVACYFENCE_N8N_PROJECT_ID"]

    def test_it_never_raises_on_a_bad_value(self):
        assert ar.set_environment_variables({"PRIVACYFENCE_N8N_MCP_URL": "junk"}) == ["PRIVACYFENCE_N8N_MCP_URL"]
        assert ar.set_environment_variables({}) == []
