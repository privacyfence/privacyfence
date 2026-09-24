"""AGT-5: the attested org pin (ADR 0006 option C, ADR 0035 decision 3), the local relabel (option D,
ADR 0037) and the audit viewer's agent column.

- Org mode: an admin pins a DCR ``client_id`` to a registry AI system -> ``oauth_client``. The pin
  is admin-only, step-up gated and audited; the DCR ``client_name`` never overrides it; unpinned
  stays ``client_info``.
- Local mode: ``settings.yaml``'s ``agent_overrides:`` relabels the name and records
  ``client_info`` on every install -- separated or not, whatever file it was read from. Its
  selector is the caller's own claimed name, and every local AI system shares one MCP token.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import agent_overrides, org_identity, paths, privilege_separation
from privacyfence.agent_identity import (
    REGISTRY,
    UNKNOWN_AGENT,
    AgentIdentity,
    AgentSource,
    entry_for_id,
    identify_registry_id,
)
from privacyfence.audit_log import AuditEntry, get_audit_logger
from privacyfence.principal import Principal, principal_scope
from privacyfence.settings_controller import audit_rows
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import agent_pins, org_session
from privacyfence.web import oauth_provider as op
from privacyfence.web import routes_settings as ros
from privacyfence.web.routes_mcp import _resolve_agent

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
    monkeypatch.setattr(op, "pins_file_path", lambda: tmp_path / "agent_pins.json")
    return op.OrgOAuthProvider(_idp(), idp_callback_url=f"{BASE_URL}/oauth/idp-callback")


async def _register(provider: op.OrgOAuthProvider, client_id: str, client_name: str | None) -> None:
    await provider.register_client(OAuthClientInformationFull(
        client_id=client_id, client_name=client_name, redirect_uris=[AnyUrl("https://c.example.com/cb")],
        token_endpoint_auth_method="none",
    ))


def _resolve_org(provider: op.OrgOAuthProvider, client_id: str, *, handshake: str | None = "claude-code"):
    return _resolve_agent(
        _ctx(handshake), _token(client_id), provider.client_name, pinned_agents=provider.pinned_agent_id,
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


# --------------------------------------------------------------------------- #
# The pin store
# --------------------------------------------------------------------------- #

class TestAgentPinStore:
    def test_pin_persists_and_reloads(self, tmp_path):
        path = tmp_path / "agent_pins.json"
        store = agent_pins.AgentPinStore(path)
        store.pin("c1", "chatgpt", pinned_by="carol")
        assert agent_pins.AgentPinStore(path).pinned_agent_id("c1") == "chatgpt"
        assert json.loads(path.read_text())["c1"]["pinned_by"] == "carol"

    def test_unpin_reports_whether_there_was_a_pin(self, tmp_path):
        store = agent_pins.AgentPinStore(tmp_path / "agent_pins.json")
        store.pin("c1", "cursor", pinned_by="carol")
        assert store.unpin("c1") is True
        assert store.unpin("c1") is False
        assert store.pinned_agent_id("c1") is None
        assert agent_pins.AgentPinStore(tmp_path / "agent_pins.json").pins() == {}

    def test_an_unknown_agent_id_is_refused(self, tmp_path):
        store = agent_pins.AgentPinStore(tmp_path / "agent_pins.json")
        with pytest.raises(ValueError):
            store.pin("c1", "evil", pinned_by="carol")
        assert store.pins() == {}

    def test_a_pin_to_an_id_the_registry_no_longer_knows_is_dropped_on_load(self, tmp_path):
        path = tmp_path / "agent_pins.json"
        path.write_text(json.dumps({
            "c1": {"agent_id": "retired", "pinned_at": 1.0, "pinned_by": "carol"},
            "c2": {"agent_id": "gemini-cli", "pinned_at": 1.0, "pinned_by": "carol"},
        }))
        assert set(agent_pins.AgentPinStore(path).pins()) == {"c2"}

    def test_a_corrupt_file_fails_closed_to_no_pins(self, tmp_path):
        path = tmp_path / "agent_pins.json"
        path.write_text("{not json")
        assert agent_pins.AgentPinStore(path).pins() == {}

    def test_the_default_path_is_under_org_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        assert agent_pins.pins_file_path() == paths.org_dir() / "agent_pins.json"


# --------------------------------------------------------------------------- #
# Capture: org pin -> oauth_client
# --------------------------------------------------------------------------- #

class TestOrgPinCapture:
    async def test_a_pinned_client_attributes_as_oauth_client(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        provider.agent_pins.pin("c1", "chatgpt", pinned_by="carol")

        agent = _resolve_org(provider, "c1")

        assert agent == AgentIdentity(id="chatgpt", name="ChatGPT", version="1.0", source=AgentSource.OAUTH_CLIENT)

    async def test_unpinning_reverts_to_client_info(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        provider.agent_pins.pin("c1", "chatgpt", pinned_by="carol")
        provider.agent_pins.unpin("c1")

        agent = _resolve_org(provider, "c1")

        assert agent.source is AgentSource.CLIENT_INFO
        assert agent.id == "chatgpt"

    async def test_the_dcr_client_name_never_overrides_a_pin(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "claude-code")
        provider.agent_pins.pin("c1", "cursor", pinned_by="carol")

        agent = _resolve_org(provider, "c1", handshake="claude-code")

        assert (agent.id, agent.source) == ("cursor", AgentSource.OAUTH_CLIENT)

    async def test_an_unpinned_registration_stays_claimed(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "openai-mcp")

        agent = _resolve_org(provider, "c1")

        assert (agent.id, agent.source) == ("chatgpt", AgentSource.CLIENT_INFO)

    async def test_a_pin_never_transfers_to_another_registration_with_the_same_name(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        await _register(provider, "c2", "openai-mcp")
        provider.agent_pins.pin("c1", "chatgpt", pinned_by="carol")

        assert _resolve_org(provider, "c2").source is AgentSource.CLIENT_INFO

    async def test_a_pin_left_by_a_pruned_registration_is_inert(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        provider.agent_pins.pin("gone", "chatgpt", pinned_by="carol")

        assert provider.pinned_agent_id("gone") is None
        agent = _resolve_org(provider, "gone", handshake=None)
        assert agent is UNKNOWN_AGENT

    async def test_the_prune_keeps_the_pin_on_disk_as_stale(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        fake_now = [time.time()]
        monkeypatch.setattr(op.time, "time", lambda: fake_now[0])
        await _register(provider, "c1", "openai-mcp")
        provider.agent_pins.pin("c1", "chatgpt", pinned_by="carol")
        fake_now[0] += op._STALE_CLIENT_TTL_SECONDS + 1
        await _register(provider, "c2", "cursor-vscode")

        assert not provider.has_client("c1")
        assert provider.pinned_agent_id("c1") is None
        assert "c1" in provider.agent_pins.pins()

    async def test_listing_clients_does_not_count_as_using_them(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        before = (tmp_path / "oauth_clients.json").read_text()

        listed = provider.list_clients()

        assert [(c.client_id, c.client_name) for c in listed] == [("c1", "openai-mcp")]
        assert (tmp_path / "oauth_clients.json").read_text() == before

    def test_a_pin_needs_an_access_token(self):
        agent = _resolve_agent(_ctx("claude-code"), None, None, pinned_agents=lambda _cid: "chatgpt")
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

        agent = _resolve_agent(_ctx("openai-mcp"), None, None, overrides=overrides)

        assert (agent.id, agent.source) == ("chatgpt", AgentSource.CLIENT_INFO)

    def test_a_pin_outranks_an_override(self):
        overrides = agent_overrides.AgentOverrides(mapping={"x": "cursor"})
        agent = _resolve_agent(_ctx("x"), _token(), None, pinned_agents=lambda _c: "chatgpt", overrides=overrides)
        assert (agent.id, agent.source) == ("chatgpt", AgentSource.OAUTH_CLIENT)

    def test_a_relabel_outranks_the_dcr_name(self):
        overrides = agent_overrides.AgentOverrides(mapping={"x": "cursor"})
        agent = _resolve_agent(_ctx("x"), _token(), lambda _c: "openai-mcp", pinned_agents=lambda _c: None, overrides=overrides)
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
# The admin's pin page and actions
# --------------------------------------------------------------------------- #

def _step_up() -> StepUpConfig:
    return StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)


def _settings_client(provider, *, step_up: StepUpConfig | None = None):
    sessions = org_session.OrgSessionStore()
    routes = ros.build_org_routes(
        sessions=sessions, install_wide_settings={}, step_up=step_up or StepUpConfig(),
        step_up_origin=BASE_URL, oauth_provider=provider,
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


class TestPinActions:
    async def test_an_admin_pins_and_the_pin_is_audited(self, org_home, monkeypatch):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        client, sessions = _settings_client(provider)
        csrf = _sign_in(client, sessions, ADMIN)

        r = client.post("/api/settings/pin_agent_client", json={"client_id": "c1", "agent_id": "chatgpt", "csrf": csrf})

        assert r.status_code == 200
        assert provider.pinned_agent_id("c1") == "chatgpt"
        row = r.json()["agents"]["clients"][0]
        assert (row["client_id"], row["pinned_agent_id"], row["pinned_agent_name"]) == ("c1", "chatgpt", "ChatGPT")
        with principal_scope(ADMIN):
            summaries = [e.summary for e in get_audit_logger().recent_entries(10)]
        assert any("Pinned OAuth client 'c1'" in s and "'chatgpt'" in s and "admin=carol" in s for s in summaries)

        r = client.post("/api/settings/unpin_agent_client", json={"client_id": "c1", "csrf": csrf})
        assert r.status_code == 200
        assert provider.pinned_agent_id("c1") is None
        with principal_scope(ADMIN):
            summaries = [e.summary for e in get_audit_logger().recent_entries(10)]
        assert any("Unpinned OAuth client 'c1'" in s for s in summaries)

    async def test_a_repeat_pin_and_an_absent_unpin_change_nothing_and_audit_nothing(self, org_home, monkeypatch):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        provider.agent_pins.pin("c1", "chatgpt", pinned_by="carol")
        client, sessions = _settings_client(provider)
        csrf = _sign_in(client, sessions, ADMIN)

        assert client.post("/api/settings/pin_agent_client", json={"client_id": "c1", "agent_id": "chatgpt", "csrf": csrf}).status_code == 200
        assert client.post("/api/settings/unpin_agent_client", json={"client_id": "zz", "csrf": csrf}).status_code == 200
        with principal_scope(ADMIN):
            assert [e for e in get_audit_logger().recent_entries(10) if e.decision == "settings_change"] == []

    async def test_a_non_admin_cannot_pin(self, org_home, monkeypatch):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        client, sessions = _settings_client(provider)
        csrf = _sign_in(client, sessions, ALICE)

        r = client.post("/api/settings/pin_agent_client", json={"client_id": "c1", "agent_id": "chatgpt", "csrf": csrf})

        assert r.status_code == 403
        assert provider.agent_pins.pins() == {}

    async def test_pinning_without_step_up_gets_a_428(self, org_home, monkeypatch):
        from privacyfence import webauthn_stepup as wa

        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        wa.add_credential(ADMIN, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client, sessions = _settings_client(provider, step_up=_step_up())
        csrf = _sign_in(client, sessions, ADMIN)

        for action, payload in (("pin_agent_client", {"client_id": "c1", "agent_id": "chatgpt"}),
                                ("unpin_agent_client", {"client_id": "c1"})):
            r = client.post(f"/api/settings/{action}", json={**payload, "csrf": csrf})
            assert r.status_code == 428, action
            assert r.json()["error"] == "step_up_required"
        assert provider.agent_pins.pins() == {}

    @pytest.mark.parametrize("payload", [
        {"client_id": "not-registered", "agent_id": "chatgpt"},
        {"client_id": "c1", "agent_id": "not-an-ai-system"},
    ])
    async def test_a_pin_must_name_a_live_registration_and_a_registry_entry(self, org_home, monkeypatch, payload):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        client, sessions = _settings_client(provider)
        csrf = _sign_in(client, sessions, ADMIN)

        r = client.post("/api/settings/pin_agent_client", json={**payload, "csrf": csrf})

        assert r.status_code == 400
        assert provider.agent_pins.pins() == {}

    def test_without_a_provider_pinning_is_refused_and_unpinning_is_a_no_op(self, org_home):
        client, sessions = _settings_client(None)
        csrf = _sign_in(client, sessions, ADMIN)

        assert client.post("/api/settings/pin_agent_client", json={"client_id": "c1", "agent_id": "chatgpt", "csrf": csrf}).status_code == 400
        r = client.post("/api/settings/unpin_agent_client", json={"client_id": "c1", "csrf": csrf})
        assert r.status_code == 200
        assert r.json()["agents"] == {
            "clients": [], "stale_pins": [],
            "registry": [{"id": e.agent_id, "name": e.display_name} for e in REGISTRY],
        }


class TestAgentsPageState:
    async def test_admin_state_lists_clients_sanitized_and_stale_pins(self, org_home, monkeypatch):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "‮evil\n<b>")
        provider.agent_pins.pin("gone", "cursor", pinned_by="carol")
        client, sessions = _settings_client(provider)
        csrf = _sign_in(client, sessions, ADMIN)

        state = client.post("/api/settings/unpin_agent_client", json={"client_id": "nope", "csrf": csrf}).json()["agents"]

        assert state["clients"][0]["client_name"] == "evil<b>"
        assert state["clients"][0]["pinned_agent_id"] == ""
        assert state["stale_pins"] == [{"client_id": "gone", "agent_id": "cursor", "agent_name": "Cursor"}]

    async def test_a_non_admin_gets_no_client_list(self, org_home, monkeypatch):
        # A client id no other part of the page can contain by chance: the embedded state also
        # carries the setuptools_scm version, whose "+g<sha>" suffix contained "c1" often enough
        # to fail this test on unrelated commits.
        client_id = "registered-client-not-for-alice"
        provider = _provider(org_home, monkeypatch)
        await _register(provider, client_id, "openai-mcp")
        client, sessions = _settings_client(provider)
        _sign_in(client, sessions, ALICE)

        r = client.get("/settings")

        assert r.status_code == 200
        assert client_id not in r.text.split("window.__pfInitialState = ", 1)[1].split("</script>", 1)[0]

    async def test_the_admin_page_embeds_the_ai_systems_section(self, org_home, monkeypatch):
        provider = _provider(org_home, monkeypatch)
        await _register(provider, "c1", "openai-mcp")
        client, sessions = _settings_client(provider)
        _sign_in(client, sessions, ADMIN)

        r = client.get("/settings")

        assert '"agents": true' in r.text
        assert "renderAgents" in r.text


# --------------------------------------------------------------------------- #
# The audit viewer's agent column, both modes
# --------------------------------------------------------------------------- #

def _entry(**agent) -> AuditEntry:
    return AuditEntry(
        timestamp="2026-09-24T10:00:00+00:00", week="2026-W39", request_id="r1",
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
        client, sessions = _settings_client(None)
        csrf = _sign_in(client, sessions, ALICE)

        state = client.post("/api/settings/unpin_agent_client", json={"csrf": csrf})
        assert state.status_code == 403  # a non-admin cannot even no-op an admin action

        page = client.get("/settings").text
        initial = json.loads(page.split("window.__pfInitialState = ", 1)[1].split(";</script>", 1)[0])
        recent = initial["audit"]["recent"]
        assert recent[0]["agent"] == {"tier": "attested", "headline": "Cursor", "claim": "", "icon_id": "cursor"}

    def test_a_log_that_cannot_be_read_costs_only_the_list(self, org_home, monkeypatch):
        client, sessions = _settings_client(None)
        _sign_in(client, sessions, ALICE)

        def _boom():
            raise RuntimeError("audit log is on fire")

        monkeypatch.setattr(ros, "get_audit_logger", _boom)
        page = client.get("/settings")
        assert page.status_code == 200
        initial = json.loads(page.text.split("window.__pfInitialState = ", 1)[1].split(";</script>", 1)[0])
        assert initial["audit"]["recent"] == []
