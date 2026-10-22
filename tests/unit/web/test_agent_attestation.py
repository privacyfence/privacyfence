"""The redirect-URI attestation (ADR 0006 option C, ADR 0224), the local relabel (option D,
ADR 0037) and the audit viewer's agent column.

- Org mode: a registration whose redirect URIs are all one AI system's own callbacks is attested
  as that system (ADR 0224), ``oauth_client``; any other stays ``client_info``, whatever its
  ``client_name``.
- Local mode: ``settings.yaml``'s ``agent_overrides:`` relabels the name and records
  ``client_info`` on every install -- separated or not, whatever file it was read from. Its
  selector is the caller's own claimed name, and every local AI system shares one MCP token.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import agent_label, agent_overrides, org_identity, paths, privilege_separation
from privacyfence.agent_identity import (
    UNKNOWN_AGENT,
    AgentIdentity,
    AgentSource,
    entry_for_id,
    identify_registry_id,
)
from privacyfence.audit_log import AuditEntry, current_week, get_audit_logger
from privacyfence.principal import Principal, principal_scope
from privacyfence.settings_controller import audit_rows
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session
from privacyfence.web import oauth_provider as op
from privacyfence.web import routes_settings as ros
from privacyfence.web.routes_mcp import _resolve_agent


@pytest.fixture(autouse=True)
def _org_mode_labels(monkeypatch):
    """The tiered labels here are org mode's; local mode's "Undetected" is ADR 0088."""
    monkeypatch.setattr(agent_label, "_local_mode", False)

BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com")
ADMIN = Principal(id="carol", email="carol@example.com", is_admin=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ctx(name: str | None, version: str | None = "1.0") -> SimpleNamespace:
    info = SimpleNamespace(name=name, version=version) if name is not None else None
    return SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(client_info=info)))


def _token(client_id: str = "client-1") -> AccessToken:
    return AccessToken(token="t", client_id=client_id, scopes=[], subject="alice@example.com")


def _idp() -> org_identity.IdpConfig:
    return org_identity.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _provider(tmp_path, monkeypatch) -> op.OrgOAuthProvider:
    monkeypatch.setattr(op, "_clients_file_path", lambda: str(tmp_path / "oauth_clients.json"))
    monkeypatch.setattr(op, "_refresh_store_path", lambda: str(tmp_path / "oauth_refresh.json"))
    return op.OrgOAuthProvider(_idp(), idp_callback_url=f"{BASE_URL}/oauth/idp-callback")


async def _register(provider: op.OrgOAuthProvider, client_id: str, client_name: str | None) -> None:
    await provider.register_client(OAuthClientInformationFull(
        client_id=client_id, client_name=client_name, redirect_uris=[AnyUrl("https://c.example.com/cb")],
        token_endpoint_auth_method="none",
    ))


def _resolve_org(provider: op.OrgOAuthProvider, client_id: str, *, handshake: str | None = "claude-code"):
    return _resolve_agent(
        _ctx(handshake), _token(client_id), provider.client_name, attested_agents=provider.attested_agent_id,
    )


# --------------------------------------------------------------------------- #
# agent_identity: registry-id resolution
# --------------------------------------------------------------------------- #

class TestIdentifyRegistryId:
    def test_a_registry_id_gets_its_display_name_and_the_given_source(self):
        agent = identify_registry_id("chatgpt", "2.0\n", AgentSource.OAUTH_CLIENT)
        assert agent == AgentIdentity(id="chatgpt", name="ChatGPT", version="2.0", source=AgentSource.OAUTH_CLIENT)

    @pytest.mark.parametrize("agent_id", ["not-a-thing", "", "unknown:claude-code"])
    def test_an_unknown_id_resolves_to_nothing(self, agent_id):
        assert identify_registry_id(agent_id, "", AgentSource.OAUTH_CLIENT) is None

    def test_no_source_resolves_to_nothing(self):
        assert identify_registry_id("chatgpt", "", AgentSource.NONE) is None

    def test_entry_for_id_rejects_a_non_string(self):
        assert entry_for_id(None) is None
        assert entry_for_id("cursor") is not None


class TestRedirectAttestation:
    async def _register_with(self, provider, client_id, client_name, redirect_uris) -> None:
        await provider.register_client(OAuthClientInformationFull(
            client_id=client_id, client_name=client_name, redirect_uris=[AnyUrl(u) for u in redirect_uris],
            token_endpoint_auth_method="none",
        ))

    @pytest.mark.parametrize("uri, agent_id", [
        ("https://claude.ai/api/mcp/auth_callback", "claude"),
        ("https://chatgpt.com/connector/oauth/abc123", "chatgpt"),
        ("https://vertexaisearch.cloud.google.com/oauth-redirect", "gemini-enterprise"),
    ])
    async def test_a_vendors_own_callback_attests_whatever_the_client_calls_itself(
        self, tmp_path, monkeypatch, uri, agent_id,
    ):
        provider = _provider(tmp_path, monkeypatch)
        await self._register_with(provider, "c1", "Totally Not Claude", [uri])

        agent = _resolve_org(provider, "c1", handshake="evil")

        assert (agent.id, agent.source) == (agent_id, AgentSource.OAUTH_CLIENT)

    async def test_a_loopback_redirect_stays_claimed(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await self._register_with(provider, "c1", "Claude Code (privacyfence)", ["http://localhost:50520/callback"])

        agent = _resolve_org(provider, "c1")

        assert (agent.id, agent.source) == ("claude-code", AgentSource.CLIENT_INFO)

    async def test_one_foreign_redirect_uri_keeps_the_registration_claimed(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await self._register_with(
            provider, "c1", "Claude", ["https://claude.ai/api/mcp/auth_callback", "http://localhost:1/cb"],
        )

        assert _resolve_org(provider, "c1").source is AgentSource.CLIENT_INFO

    def test_an_unknown_client_is_not_attested(self, tmp_path, monkeypatch):
        assert _provider(tmp_path, monkeypatch).attested_agent_id("nobody") is None

    async def test_asking_does_not_count_as_using_the_client(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await self._register_with(provider, "c1", "Claude", ["https://claude.ai/api/mcp/auth_callback"])
        before = (tmp_path / "oauth_clients.json").read_text()

        assert provider.attested_agent_id("c1") == "claude"

        assert (tmp_path / "oauth_clients.json").read_text() == before

    async def test_a_registration_with_a_foreign_redirect_stays_claimed(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "ChatGPT")

        agent = _resolve_org(provider, "c1")

        assert (agent.id, agent.source) == ("chatgpt", AgentSource.CLIENT_INFO)

    async def test_a_gemini_enterprise_name_with_a_foreign_redirect_is_claimed(self, tmp_path, monkeypatch):
        # The admin registers Gemini Enterprise with the prescribed client_name; its handshake name
        # is not recognised, so the registered name decides (ADR 0109).
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "Gemini Enterprise")

        agent = _resolve_org(provider, "c1", handshake="not-a-registry-name")

        assert (agent.id, agent.name, agent.source) == ("gemini-enterprise", "Gemini Enterprise", AgentSource.CLIENT_INFO)

    def test_an_attestation_needs_an_access_token(self):
        agent = _resolve_agent(_ctx("claude-code"), None, None, attested_agents=lambda _cid: "chatgpt")
        assert agent.source is AgentSource.CLIENT_INFO


# --------------------------------------------------------------------------- #
# Local override -> a relabel, recorded as client_info on every install (ADR 0037)
# --------------------------------------------------------------------------- #

_OVERRIDE_CONFIG = {"agent_overrides": {"My-Wrapper": "claude-code", "python-mcp-client": "claude-code"}}


def _started_overrides(tmp_path, monkeypatch, *, separated: bool, config_path: str):
    """The ``AgentOverrides`` daemon_main's own startup hands the web server, for a daemon whose
    settings came from ``config_path`` on a separated (or unseparated) install."""
    from privacyfence import daemon_main
    from privacyfence.connector_host import ConnectorHost
    from privacyfence.web.server import WebServer

    monkeypatch.setattr(privilege_separation, "is_enabled", lambda: separated)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(WebServer, "start", lambda self: None)
    captured: dict[str, object] = {}
    real_init = WebServer.__init__

    def _init(self, *args, **kwargs):
        captured["overrides"] = kwargs.get("agent_overrides")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(WebServer, "__init__", _init)
    daemon_main._maybe_start_web_server(
        dict(_OVERRIDE_CONFIG), ConnectorHost([]), unattended_sessions_enabled=False, config_path=config_path,
    )
    return captured["overrides"]


class TestLocalOverrideNeverAttests:
    """The review finding this replaces: on a separated install an override was recorded as
    ``override`` (attested), so any local AI system sending ``python-mcp-client`` was recorded as
    attested Claude Code. The override now relabels only, wherever it was read from."""

    @pytest.mark.parametrize("separated", [True, False], ids=["separated", "unseparated"])
    @pytest.mark.parametrize("where", ["authority", "elsewhere", "none"])
    def test_a_match_is_claimed_on_every_install(self, tmp_path, monkeypatch, separated, where):
        config_path = {
            "authority": str(paths.authority_root(tmp_path) / "config" / "settings.yaml"),
            "elsewhere": str(tmp_path / "home" / "settings.yaml"),
            "none": "",
        }[where]
        overrides = _started_overrides(tmp_path, monkeypatch, separated=separated, config_path=config_path)

        agent = _resolve_agent(_ctx("my-wrapper", "3.1"), None, None, overrides=overrides)

        assert agent == AgentIdentity(id="claude-code", name="Claude Code", version="3.1", source=AgentSource.CLIENT_INFO)
        assert not agent.is_attested()

    def test_a_spoofed_mapped_name_is_recorded_as_a_claim(self, tmp_path, monkeypatch):
        # The finding's own scenario: any AI system holding the shared local token sends a mapped name.
        overrides = _started_overrides(
            tmp_path, monkeypatch, separated=True,
            config_path=str(paths.authority_root(tmp_path) / "config" / "settings.yaml"),
        )
        agent = _resolve_agent(_ctx("python-mcp-client"), None, None, overrides=overrides)
        assert (agent.id, agent.source) == ("claude-code", AgentSource.CLIENT_INFO)

    def test_an_unmatched_name_falls_through_to_the_claim(self):
        overrides = agent_overrides.AgentOverrides(mapping={"my-wrapper": "claude-code"})

        agent = _resolve_agent(_ctx("ChatGPT"), None, None, overrides=overrides)

        assert (agent.id, agent.source) == ("chatgpt", AgentSource.CLIENT_INFO)

    async def test_a_redirect_attestation_outranks_an_override(self, tmp_path, monkeypatch):
        overrides = agent_overrides.AgentOverrides(mapping={"x": "cursor"})
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(OAuthClientInformationFull(
            client_id="client-1", client_name="x", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
            token_endpoint_auth_method="none",
        ))
        agent = _resolve_agent(
            _ctx("x"), _token("client-1"), provider.client_name, attested_agents=provider.attested_agent_id,
            overrides=overrides,
        )
        assert (agent.id, agent.source) == ("claude", AgentSource.OAUTH_CLIENT)

    def test_a_relabel_outranks_the_dcr_name(self):
        overrides = agent_overrides.AgentOverrides(mapping={"x": "cursor"})
        agent = _resolve_agent(_ctx("x"), _token(), lambda _c: "ChatGPT", attested_agents=lambda _c: None, overrides=overrides)
        assert (agent.id, agent.source) == ("cursor", AgentSource.CLIENT_INFO)

    def test_no_claimed_name_matches_no_override(self):
        overrides = agent_overrides.AgentOverrides(mapping={"x": "cursor"})
        assert overrides.resolve(None, "") is None
        assert _resolve_agent(_ctx(None), None, None, overrides=overrides) is UNKNOWN_AGENT


class TestFromConfig:
    def test_absent_section_is_none(self):
        assert agent_overrides.from_config({}) is None

    def test_a_non_mapping_section_is_ignored(self):
        assert agent_overrides.from_config({"agent_overrides": ["claude-code"]}) is None

    def test_bad_entries_are_skipped_and_good_ones_kept(self):
        overrides = agent_overrides.from_config({"agent_overrides": {
            "ok": "gemini-cli", "unknown-target": "nope", "": "cursor", "not-str": 3,
            "\u202eSpoof\n": "cursor",
        }})
        assert overrides is not None
        assert dict(overrides.mapping) == {"ok": "gemini-cli", "spoof": "cursor"}

    def test_nothing_usable_is_none(self):
        assert agent_overrides.from_config({"agent_overrides": {"a": "nope"}}) is None


# --------------------------------------------------------------------------- #
# Org settings helpers
# --------------------------------------------------------------------------- #

def _settings_client(*, step_up: StepUpConfig | None = None):
    sessions = org_session.OrgSessionStore()
    routes = ros.build_org_routes(
        sessions=sessions, install_wide_settings={}, step_up=step_up or StepUpConfig(),
        step_up_origin=BASE_URL,
    )
    return TestClient(Starlette(routes=routes), base_url=BASE_URL, follow_redirects=False), sessions


def _sign_in(client, sessions, principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


@pytest.fixture
def org_home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# The audit viewer's agent column, both modes
# --------------------------------------------------------------------------- #

def _entry(**agent) -> AuditEntry:
    return AuditEntry(
        # Now, not a fixed date: the settings page lists only the current and previous ISO week.
        timestamp=datetime.now(timezone.utc).isoformat(), week=current_week(), request_id="r1",
        connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
        summary="s", sender="a@example.com", decision="approved", auto_accept_rule="",
        latency_seconds=0.0, pii_detected=False, **agent,
    )


class TestAuditRows:
    def test_each_tier(self):
        rows = audit_rows([
            _entry(agent_id="chatgpt", agent_name="ChatGPT", agent_source="oauth_client"),
            _entry(agent_id="chatgpt", agent_name="ChatGPT", agent_source="client_info"),
            _entry(agent_id="unknown:foo", agent_name="foo", agent_source="client_info"),
            _entry(),
        ])
        assert [r["agent"]["tier"] for r in rows] == ["attested", "claimed", "unknown", "unknown"]
        assert rows[0]["agent"]["headline"] == "ChatGPT"
        assert rows[1]["agent"]["headline"] == "Says it is ChatGPT"
        assert rows[2]["agent"]["claim"] == "foo"
        assert rows[3]["agent"]["headline"] == "Unrecognised AI system"
        assert rows[0]["connector"] == "Gmail"

    def test_an_unknown_source_value_is_never_promoted(self):
        row = audit_rows([_entry(agent_id="chatgpt", agent_name="ChatGPT", agent_source="admin_says_so")])[0]
        assert row["agent"]["tier"] == "unknown"

    def test_org_mode_audit_page_shows_the_principals_own_rows_with_agents(self, org_home):
        with principal_scope(ALICE):
            get_audit_logger().record(_entry(agent_id="cursor", agent_name="Cursor", agent_source="oauth_client"))
        client, sessions = _settings_client()
        _sign_in(client, sessions, ALICE)

        page = client.get("/settings").text
        initial = json.loads(page.split("window.__pfInitialState = ", 1)[1].split(";</script>", 1)[0])
        recent = initial["audit"]["recent"]
        assert recent[0]["agent"] == {"tier": "attested", "headline": "Cursor", "claim": "", "icon_id": "cursor"}

    def test_a_log_that_cannot_be_read_costs_only_the_list(self, org_home, monkeypatch):
        client, sessions = _settings_client()
        _sign_in(client, sessions, ALICE)

        def _boom():
            raise RuntimeError("audit log is on fire")

        monkeypatch.setattr(ros, "get_audit_logger", _boom)
        page = client.get("/settings")
        assert page.status_code == 200
        initial = json.loads(page.text.split("window.__pfInitialState = ", 1)[1].split(";</script>", 1)[0])
        assert initial["audit"]["recent"] == []
