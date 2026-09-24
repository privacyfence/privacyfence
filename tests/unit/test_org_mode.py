"""Tests for org_mode.py: the mode toggle and §10.2 server config (P7)."""
from __future__ import annotations

import pytest

from privacyfence import org_mode


class TestResolveMode:
    def test_absent_key_defaults_to_local(self):
        assert org_mode.resolve_mode({}) == "local"

    def test_explicit_local(self):
        assert org_mode.resolve_mode({"mode": "local"}) == "local"

    def test_explicit_org(self):
        assert org_mode.resolve_mode({"mode": "org"}) == "org"

    def test_invalid_value_raises(self):
        # SEC-04: ConfigurationError, not a bare ValueError -- so callers
        # (daemon_main.py's main()) can't mistake this for some other
        # ValueError-raising failure further down the same startup path.
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.resolve_mode({"mode": "something-else"})


class TestServerConfigFromOrgConfig:
    def test_requires_issuer_url(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.ServerConfig.from_org_config({"server": {}})

    def test_requires_a_server_section_at_all(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.ServerConfig.from_org_config({})

    @pytest.mark.parametrize("issuer_url", [
        "pf.example.com",           # no scheme at all
        "pf.example.com:8765",      # "pf.example.com" reads as the scheme
        "ftp://pf.example.com",     # a scheme, but not one a browser follows
        "https://",                 # parses, but there is no host in it
    ])
    def test_rejects_an_issuer_url_that_is_not_an_absolute_http_url(self, issuer_url):
        # Every one of these used to start the daemon. The scheme-less
        # spellings then died several frames later inside mount_org_oauth's
        # AnyHttpUrl(); "https://" died nowhere at all -- it left the Host
        # allowlist without the issuer host, so every request answered
        # "Invalid Host header" and the config value at fault was named
        # nowhere.
        with pytest.raises(org_mode.ConfigurationError, match="issuer_url"):
            org_mode.ServerConfig.from_org_config({"server": {"issuer_url": issuer_url}})

    def test_issuer_url_is_stripped_of_surrounding_whitespace(self):
        # The nastiest spelling of this bug, because nothing rejects it:
        # pydantic's AnyHttpUrl normalizes the trailing space away, while
        # urlsplit(...).hostname keeps it *in the hostname*
        # ("pf.example.com "), so server.py's allowlist ends up holding a
        # host no Host header can ever match.
        config = org_mode.ServerConfig.from_org_config({"server": {"issuer_url": " https://pf.example.com "}})
        assert config.issuer_url == "https://pf.example.com"

    def test_builds_config_with_defaults(self):
        config = org_mode.ServerConfig.from_org_config({"server": {"issuer_url": "https://pf.example.com"}})
        assert config.issuer_url == "https://pf.example.com"
        assert config.bind_host == org_mode.DEFAULT_BIND_HOST
        assert config.port == org_mode.DEFAULT_PORT
        assert config.trusted_proxies == ()
        assert config.tls_configured is False

    def test_builds_config_with_every_field_set(self):
        config = org_mode.ServerConfig.from_org_config({
            "server": {
                "bind_host": "0.0.0.0", "port": 443, "issuer_url": "https://pf.example.com",
                "tls": {"cert_file": "/etc/pf/cert.pem", "key_file": "/etc/pf/key.pem"},
                "trusted_proxies": ["10.0.0.5", "10.0.0.6"],
            },
        })
        assert config.bind_host == "0.0.0.0"
        assert config.port == 443
        assert config.cert_file == "/etc/pf/cert.pem"
        assert config.key_file == "/etc/pf/key.pem"
        assert config.trusted_proxies == ("10.0.0.5", "10.0.0.6")
        assert config.tls_configured is True

    def test_tls_configured_is_false_when_only_one_half_is_set(self):
        config = org_mode.ServerConfig.from_org_config({
            "server": {"issuer_url": "https://pf.example.com", "tls": {"cert_file": "/etc/pf/cert.pem"}},
        })
        assert config.tls_configured is False


class TestDefaults:
    def test_default_bind_host_is_loopback(self):
        # An org bundle with no "bind_host" must never listen on the
        # network: the same 127.0.0.1 scripts/build_org_bundle.py writes
        # by default (test_build_org_bundle.py pins the two together).
        assert org_mode.ServerConfig().bind_host == "127.0.0.1"

    def test_default_has_no_tls(self):
        assert org_mode.ServerConfig().tls_configured is False


class TestDownloadDeliveryConfigFromOrgConfig:
    """An existing org install with no "download_delivery" section keeps
    working exactly as before (inline-first, 8MB cap, staging allowed)."""

    def test_absent_section_uses_defaults(self):
        config = org_mode.DownloadDeliveryConfig.from_org_config({})
        assert config.inline_max_bytes == org_mode.DEFAULT_INLINE_MAX_BYTES
        assert config.link_ttl_seconds == org_mode.DEFAULT_LINK_TTL_SECONDS
        assert config.allow_disk_staging is True
        # Phase 4: defaults to the capability link -- see agent_links' own
        # docstring.
        assert config.agent_links is True

    def test_every_field_set(self):
        config = org_mode.DownloadDeliveryConfig.from_org_config({
            "download_delivery": {
                "inline_max_bytes": 1_000_000, "link_ttl_seconds": 60.0, "allow_disk_staging": False,
                "agent_links": False,
            },
        })
        assert config.inline_max_bytes == 1_000_000
        assert config.link_ttl_seconds == 60.0
        assert config.allow_disk_staging is False
        assert config.agent_links is False

    def test_zero_inline_max_bytes_is_allowed(self):
        # The knob an org picks for "no file content ever reaches Claude's
        # context, unconditionally" -- forces every download through a
        # staged link.
        config = org_mode.DownloadDeliveryConfig.from_org_config({
            "download_delivery": {"inline_max_bytes": 0},
        })
        assert config.inline_max_bytes == 0

    def test_negative_inline_max_bytes_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.DownloadDeliveryConfig.from_org_config({"download_delivery": {"inline_max_bytes": -1}})

    def test_non_positive_link_ttl_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.DownloadDeliveryConfig.from_org_config({"download_delivery": {"link_ttl_seconds": 0}})


class TestDownloadDeliveryConfigStagedLinkPath:
    """Phase 4 ("Clients without the bridge"): which URL path a staged
    download's link should use, given agent_links -- see that field's own
    docstring for the reasoning."""

    def test_agent_links_true_uses_the_capability_route(self):
        config = org_mode.DownloadDeliveryConfig(agent_links=True)
        path = config.staged_link_path(b"\x01" * 32)
        assert path.startswith("/mcp-files/fetch/")

    def test_agent_links_false_uses_the_browser_route(self):
        config = org_mode.DownloadDeliveryConfig(agent_links=False)
        path = config.staged_link_path(b"\x01" * 32)
        assert path.startswith("/downloads/")

    def test_both_routes_encode_the_same_token(self):
        import base64
        token = b"\x02" * 32
        expected = base64.urlsafe_b64encode(token).decode("ascii")
        assert org_mode.DownloadDeliveryConfig(agent_links=True).staged_link_path(token) == f"/mcp-files/fetch/{expected}"
        assert org_mode.DownloadDeliveryConfig(agent_links=False).staged_link_path(token) == f"/downloads/{expected}"


class TestAuditForwardingConfigFromOrgConfig:
    """Org mode's centralized audit-log forwarding destination. An existing org
    install with no "audit_forwarding" section keeps working exactly as
    before this phase (forwarding off)."""

    def test_absent_section_uses_defaults(self):
        config = org_mode.AuditForwardingConfig.from_org_config({})
        assert config.enabled is False
        assert config.kind == org_mode.DEFAULT_AUDIT_FORWARDING_KIND
        assert config.syslog_host == ""
        assert config.syslog_port == org_mode.DEFAULT_SYSLOG_PORT
        assert config.syslog_protocol == org_mode.DEFAULT_SYSLOG_PROTOCOL
        assert config.http_url == ""
        assert config.http_bearer_token_env == ""

    def test_every_syslog_field_set(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {
                "enabled": True, "kind": "syslog",
                "syslog": {"host": "siem.example.com", "port": 601, "protocol": "udp"},
            },
        })
        assert config.enabled is True
        assert config.kind == "syslog"
        assert config.syslog_host == "siem.example.com"
        assert config.syslog_port == 601
        assert config.syslog_protocol == "udp"

    def test_every_http_field_set(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {
                "enabled": True, "kind": "http",
                "http": {"url": "https://siem.example.com/ingest", "bearer_token_env": "SIEM_TOKEN"},
            },
        })
        assert config.kind == "http"
        assert config.http_url == "https://siem.example.com/ingest"
        assert config.http_bearer_token_env == "SIEM_TOKEN"

    def test_non_dict_section_is_ignored(self):
        config = org_mode.AuditForwardingConfig.from_org_config({"audit_forwarding": "not a dict"})
        assert config.enabled is False

    def test_non_dict_syslog_and_http_subsections_are_ignored(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"syslog": "nope", "http": ["nope"]},
        })
        assert config.syslog_host == ""
        assert config.http_url == ""

    def test_invalid_kind_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({"audit_forwarding": {"kind": "carrier_pigeon"}})

    def test_invalid_syslog_protocol_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({
                "audit_forwarding": {"syslog": {"protocol": "quic"}},
            })

    def test_enabled_http_kind_with_non_https_url_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({
                "audit_forwarding": {"enabled": True, "kind": "http", "http": {"url": "http://siem.example.com"}},
            })

    def test_disabled_http_kind_with_non_https_url_does_not_raise(self):
        # A mid-rollout config -- enabled=False, section not fully filled
        # in yet -- must not be rejected just because the URL isn't
        # https:// yet; the check only applies once it's actually live.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": False, "kind": "http", "http": {"url": "http://siem.example.com"}},
        })
        assert config.http_url == "http://siem.example.com"

    def test_syslog_kind_with_non_https_http_url_present_does_not_raise(self):
        # The https:// check only applies when kind is actually "http" --
        # an org that's set up both sections while trying out syslog first
        # shouldn't be blocked by an unrelated http.url value.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "syslog", "http": {"url": "http://siem.example.com"}},
        })
        assert config.kind == "syslog"

    def test_enabled_http_kind_with_https_url_does_not_raise(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "http", "http": {"url": "https://siem.example.com"}},
        })
        assert config.http_url == "https://siem.example.com"

    def test_enabled_http_kind_with_empty_url_does_not_raise(self):
        # An incomplete config (enabled flipped on before the URL is filled
        # in) -- audit_forwarding.build_sender() is where "http_url is
        # required" is actually enforced, not here.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "http"},
        })
        assert config.http_url == ""


class TestAuthzPolicyConfigFromOrgConfig:
    """An existing org install with no "authz" section keeps admitting every
    IdP-authenticated principal exactly as before this landed."""

    def test_absent_section_is_disabled_with_defaults(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({})
        assert config.enabled is False
        assert config.allowed_domains == ()
        assert config.groups_claim == ""
        assert config.required_groups == ()

    def test_allowed_domains_are_lowercased_and_at_stripped(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({
            "authz": {"allowed_domains": ["Acme.com", "@sub.Acme.com"]},
        })
        assert config.allowed_domains == ("acme.com", "sub.acme.com")
        assert config.enabled is True

    def test_required_groups_with_groups_claim(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({
            "authz": {"groups_claim": "groups", "required_groups": ["privacyfence-users"]},
        })
        assert config.groups_claim == "groups"
        assert config.required_groups == ("privacyfence-users",)
        assert config.enabled is True

    def test_required_groups_without_groups_claim_raises(self):
        with pytest.raises(org_mode.ConfigurationError, match="groups_claim"):
            org_mode.AuthzPolicyConfig.from_org_config({"authz": {"required_groups": ["admins"]}})

    def test_blank_domain_entries_are_dropped(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({"authz": {"allowed_domains": ["", "  ", "acme.com"]}})
        assert config.allowed_domains == ("acme.com",)

    def test_non_dict_authz_section_is_treated_as_absent(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({"authz": "not-a-dict"})
        assert config.enabled is False


class TestConfigurationError:
    def test_is_a_value_error_subclass(self):
        # So every existing `except ValueError`/`pytest.raises(ValueError)`
        # around org-config parsing keeps working unchanged -- this is a
        # narrowing of the exception type raised, not a new one call sites
        # must learn to catch.
        assert issubclass(org_mode.ConfigurationError, ValueError)
