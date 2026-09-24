"""AGT-3: which AI system a tool call is attributed to, as routes_mcp.py's handle_call_tool
captures it (ADR 0006, ADR 0035).

Every capture path here is a *claim* -- the handshake ``clientInfo``, a 2026-07-28 request's own
``_meta`` envelope, and an org-mode DCR ``client_name`` -- so the two properties pinned below are
ADR 0006's Verification 1 (a claimed identity never changes an outcome) and Verification 2 (no
caller-supplied signal ever yields an attested ``agent_source``).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from privacyfence import agent_overrides, card_builder, gate, org_identity
from privacyfence.agent_identity import UNKNOWN_AGENT, AgentSource, current_agent
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.policy.engine import PolicyRule
from privacyfence.web import oauth_provider as op
from privacyfence.web import routes_mcp as rm
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan

TOKEN = "mcp-test-token"

HOSTILE = "‮evil‬\x1b[31m<script>alert(1)</script>\n" + "x" * 200
CLAIMED_NAMES = ["Claude", "ChatGPT", "", HOSTILE]

ATTESTED = {AgentSource.OVERRIDE.value, AgentSource.OAUTH_CLIENT.value}


class AgentEchoConnector(Connector):
    """Returns the identity the call ran under -- what every capture test reads back."""

    @property
    def name(self) -> str:
        return "echo"

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="echo_say", description="Echoes the caller's agent.",
            params=[ToolParam("message", "str", required=True)], read_only=True,
        )]

    async def call(self, tool: str, args: dict) -> object:
        agent = current_agent()
        return {"agent": [agent.id, agent.name, agent.version, agent.source.value]}


class GatedConnector(AgentEchoConnector):
    """A real gated read: everything below it (rule match, popup, audit) is gate.py's own."""

    async def call(self, tool: str, args: dict) -> object:
        return await gate.gated_call(
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="from alice@example.com", sender="alice@example.com",
            raw_data={"body": "raw"}, filtered_data={"body": "released"},
            gate="review", preview={"from": "alice@example.com"}, details_text="full body here",
            my_email="me@example.com",
        )


class _OrgVerifier(TokenVerifier):
    """Stands in for OrgOAuthProvider.verify_token: a signed-in human, via DCR client ``client_id``."""

    def __init__(self, client_id: str) -> None:
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != TOKEN:
            return None
        return AccessToken(token=token, client_id=self._client_id, scopes=[], subject="alice@example.com")


def _dispatcher(connector: Connector) -> McpDispatcher:
    store = {connector.name: connector}
    return McpDispatcher(lambda: store)


@contextlib.asynccontextmanager
async def _session(dispatcher, *, client_name: str | None, verifier=None, client_names=None, overrides=None):
    """A live, initialized ClientSession whose handshake claims ``client_name`` (None: the SDK's
    own default clientInfo)."""
    app, session_manager = build_mcp_asgi_app(
        dispatcher, token=None if verifier else TOKEN, verifier=verifier, client_names=client_names,
        overrides=overrides,
    )
    info = types.Implementation(name=client_name, version="9.9") if client_name is not None else None
    async with mcp_lifespan(session_manager):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client:
            async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (read, write):
                async with ClientSession(read, write, client_info=info) as session:
                    await session.initialize()
                    yield session


async def _agent_of_call(**session_kwargs) -> list[str]:
    async with _session(_dispatcher(AgentEchoConnector()), **session_kwargs) as session:
        result = await session.call_tool("echo_say", {"message": "hi"})
    return result.structured_content["agent"]


# --------------------------------------------------------------------------- #
# Local mode: the handshake clientInfo
# --------------------------------------------------------------------------- #

class TestLocalModeCapture:
    async def test_a_registry_client_is_named_and_recorded_as_a_claim(self):
        assert await _agent_of_call(client_name="claude-code") == ["claude-code", "Claude Code", "9.9", "client_info"]

    async def test_an_unrecognised_client_keeps_its_claimed_name(self):
        assert await _agent_of_call(client_name="Claude") == ["unknown:Claude", "Claude", "9.9", "client_info"]

    async def test_an_empty_name_is_unknown_not_a_default(self):
        assert await _agent_of_call(client_name="") == ["", "", "", ""]

    async def test_a_hostile_name_is_sanitized_before_it_is_recorded(self):
        agent_id, name, _version, source = await _agent_of_call(client_name=HOSTILE)
        assert source == "client_info"
        assert len(name) <= 64
        assert not any(ch in name for ch in ("‮", "‬", "\x1b", "\n"))
        assert agent_id == "unknown:" + name

    async def test_meta_tools_are_not_attributed(self, monkeypatch):
        # Plan Invariant 5: privacyfence_status and the other meta-tools are not gated calls.
        seen = []
        monkeypatch.setattr(McpDispatcher, "status", lambda self, reason: seen.append(current_agent()) or {})
        async with _session(_dispatcher(AgentEchoConnector()), client_name="claude-code") as session:
            await session.call_tool("privacyfence_status", {})
        assert seen == [UNKNOWN_AGENT]


class TestModernProtocolCapture:
    async def test_a_sessionless_request_attributes_from_its_own_envelope(self):
        # 2026-07-28: no initialize, no Mcp-Session-Id -- the client info rides the request's
        # own _meta, which the SDK surfaces through the same ctx.session.client_params.
        app, session_manager = build_mcp_asgi_app(_dispatcher(AgentEchoConnector()), token=TOKEN)
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "echo_say", "arguments": {"message": "hi"},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "openai-mcp", "version": "1.0"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "Mcp-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call", "Mcp-Name": "echo_say",
        }
        async with mcp_lifespan(session_manager):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
                response = await c.post("/mcp", json=body, headers=headers)
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers
        agent = response.json()["result"]["structuredContent"]["agent"]
        assert agent == ["chatgpt", "ChatGPT", "1.0", "client_info"]


class TestDegradedClientInfo:
    """Same posture as _connection_of: a missing or moved clientInfo costs attribution, never the call."""

    @pytest.mark.parametrize("session", [
        SimpleNamespace(),                                         # the attribute moved
        SimpleNamespace(client_params=None),                       # no client info sent
        SimpleNamespace(client_params=SimpleNamespace()),          # params without client_info
        SimpleNamespace(client_params=SimpleNamespace(client_info=SimpleNamespace(name=42, version=None))),
    ])
    def test_degrades_to_unknown(self, session):
        assert rm._resolve_agent(SimpleNamespace(session=session), None, None) == UNKNOWN_AGENT


class TestClientInfoIconsAreNeverRead:
    """ADR 0006 Invariant 4 / ADR 0035 decision 2: ``clientInfo.icons`` and ``website_url`` are
    never read, so a caller-supplied URL can never reach the approval card -- neither as a
    tracking beacon nor as a borrowed brand mark. Pinned end to end, from the handshake read to
    the rendered card."""

    class _ClientInfo:
        name = "openai-mcp"
        version = "1.0"

        @property
        def icons(self):
            raise AssertionError("clientInfo.icons was read")

        @property
        def website_url(self):
            raise AssertionError("clientInfo.website_url was read")

    def test_neither_capture_nor_the_card_touches_them(self):
        session = SimpleNamespace(client_params=SimpleNamespace(client_info=self._ClientInfo()))
        agent = rm._resolve_agent(SimpleNamespace(session=session), None, None)
        assert agent.id == "chatgpt"
        doc = card_builder.build_card_html(
            title="Read Message", preview={"From": "a@example.com"}, details_text="Body.",
            is_read=True, layout="narrow", agent=agent,
        )
        # Claimed tier, so no mark at all -- and every image the card does draw is bundled.
        assert 'data-agent-tier="claimed"' in doc
        assert re.findall(r'<img[^>]*src="(?!data:image/png;base64,)', doc) == []


# --------------------------------------------------------------------------- #
# Org mode: the DCR client_name, falling back to the handshake
# --------------------------------------------------------------------------- #

def _provider(tmp_path, monkeypatch) -> op.OrgOAuthProvider:
    monkeypatch.setattr(op, "_clients_file_path", lambda: str(tmp_path / "oauth_clients.json"))
    monkeypatch.setattr(op, "_refresh_store_path", lambda: str(tmp_path / "oauth_refresh.json"))
    idp = org_identity.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )
    return op.OrgOAuthProvider(idp, idp_callback_url="https://pf.example.com/oauth/idp/callback")


def _dcr_client(client_id: str, client_name: str | None) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id, client_name=client_name,
        redirect_uris=[AnyUrl("https://client.example.com/callback")], token_endpoint_auth_method="none",
    )


class TestOrgModeCapture:
    async def test_a_registered_client_name_is_a_claim_not_an_attestation(self, tmp_path, monkeypatch):
        # Gate G1: an unpinned DCR client_name is claimed -- recorded as client_info.
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(_dcr_client("dcr-1", "claude-ai"))
        agent = await _agent_of_call(
            client_name="cursor-vscode", verifier=_OrgVerifier("dcr-1"), client_names=provider.client_name,
        )
        assert agent == ["claude", "Claude", "", "client_info"]

    @pytest.mark.parametrize("dcr_name", [None, "", "‮\n"])
    async def test_no_usable_dcr_name_falls_back_to_the_handshake(self, tmp_path, monkeypatch, dcr_name):
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(_dcr_client("dcr-1", dcr_name))
        agent = await _agent_of_call(
            client_name="cursor-vscode", verifier=_OrgVerifier("dcr-1"), client_names=provider.client_name,
        )
        assert agent == ["cursor", "Cursor", "9.9", "client_info"]

    async def test_an_unregistered_client_id_falls_back_to_the_handshake(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        agent = await _agent_of_call(
            client_name="gemini-cli-mcp-client", verifier=_OrgVerifier("never-registered"),
            client_names=provider.client_name,
        )
        assert agent == ["gemini-cli", "Gemini CLI", "9.9", "client_info"]

    async def test_a_per_call_lookup_never_writes_the_clients_file(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(_dcr_client("dcr-1", "claude-ai"))
        clients_file = tmp_path / "oauth_clients.json"
        # Backdate the file so a rewrite within the same clock tick still shows up as a change.
        os.utime(clients_file, (1_000_000, 1_000_000))
        before = (clients_file.stat().st_mtime_ns, clients_file.read_bytes())

        async with _session(
            _dispatcher(AgentEchoConnector()), client_name="x",
            verifier=_OrgVerifier("dcr-1"), client_names=provider.client_name,
        ) as session:
            for _ in range(3):
                await session.call_tool("echo_say", {"message": "hi"})

        assert (clients_file.stat().st_mtime_ns, clients_file.read_bytes()) == before

    def test_client_name_does_not_bump_last_used_at(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        provider._clients["dcr-1"] = op._StoredClient(info=_dcr_client("dcr-1", "claude-ai"), last_used_at=123.0)
        assert provider.client_name("dcr-1") == "claude-ai"
        assert provider.client_name("missing") is None
        assert provider._clients["dcr-1"].last_used_at == 123.0


# --------------------------------------------------------------------------- #
# Verification 2: no caller-supplied signal yields an attested agent_source
# --------------------------------------------------------------------------- #

# Names that try to *look* attested, or to impersonate a registry entry, on top of the usual set.
_SPOOFS = CLAIMED_NAMES + ["override", "oauth_client", "claude-code", "Claude Code", "CLAUDE-AI"]


class TestNoCallerSignalIsAttested:
    @pytest.mark.parametrize("claimed", _SPOOFS)
    async def test_handshake(self, claimed):
        assert (await _agent_of_call(client_name=claimed))[3] not in ATTESTED

    @pytest.mark.parametrize("claimed", _SPOOFS)
    async def test_dcr_client_name(self, tmp_path, monkeypatch, claimed):
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(_dcr_client("dcr-1", claimed))
        agent = await _agent_of_call(
            client_name=claimed, verifier=_OrgVerifier("dcr-1"), client_names=provider.client_name,
        )
        assert agent[3] not in ATTESTED

    @pytest.mark.parametrize("claimed", _SPOOFS)
    async def test_local_override(self, claimed):
        # ADR 0037: settings.yaml maps the caller's own name, so a match is still that caller's
        # claim -- every spoof is mapped to a registry entry here and none may come back attested.
        overrides = agent_overrides.from_config({"agent_overrides": {
            name: "claude-code" for name in _SPOOFS if name.strip()
        }})
        agent_id, _name, _version, source = await _agent_of_call(client_name=claimed, overrides=overrides)
        assert source not in ATTESTED
        if claimed.strip():
            assert (agent_id, source) == ("claude-code", "client_info")

    @pytest.mark.parametrize("claimed", _SPOOFS)
    def test_modern_envelope(self, claimed):
        # Same read the session-less path takes (see TestModernProtocolCapture), without the wire.
        info = SimpleNamespace(name=claimed, version="1")
        ctx = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(client_info=info)))
        assert rm._resolve_agent(ctx, None, None).source.value not in ATTESTED
        assert not rm._resolve_agent(ctx, None, None).is_attested()


# --------------------------------------------------------------------------- #
# Verification 1: a claimed identity never changes an outcome
# --------------------------------------------------------------------------- #

@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def _audit_entries(audit_dir) -> list[dict]:
    week_file = audit_dir / f"{current_week()}.jsonl"
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


def _install_rule(monkeypatch) -> None:
    monkeypatch.setattr(gate, "get_policy_v2_store_rules", lambda: [PolicyRule(
        id="r-gmail", predicate="gmail.anything", value=None, operations=frozenset({"gmail.read_message"}),
    )])


def _no_rules(monkeypatch, popup_answer: str) -> None:
    monkeypatch.setattr(gate, "get_policy_v2_store_rules", lambda: [])
    monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
    monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_answer, None))


_EXPECTED = {
    "rule_match": (False, {"body": "released"}, "auto_accepted", "r-gmail", "r-gmail"),
    "popup_accept": (False, {"body": "released"}, "approved", "", ""),
    "popup_deny": (True, None, "rejected", "", ""),
}


class TestClaimedIdentityNeverChangesAnOutcome:
    """ADR 0006 Verification 1: the same gated call, differing only in clientInfo.name, gives the
    same decision, the same rule match and the same released data."""

    @pytest.mark.parametrize("scenario", ["rule_match", "popup_accept", "popup_deny"])
    async def test_same_decision_rule_and_data_whatever_the_claimed_name(self, monkeypatch, audit_dir, scenario):
        if scenario == "rule_match":
            _install_rule(monkeypatch)
        else:
            _no_rules(monkeypatch, "accept" if scenario == "popup_accept" else "deny")

        outcomes = {}
        agents = {}
        for claimed in CLAIMED_NAMES + ["claude-code"]:
            async with _session(_dispatcher(GatedConnector()), client_name=claimed) as session:
                result = await session.call_tool("echo_say", {"message": "hi"})
            entry = _audit_entries(audit_dir)[-1]
            released = result.structured_content if not result.is_error else None
            outcomes[claimed] = (
                result.is_error, released, entry["decision"], entry["auto_accept_rule"], entry["rule_id"],
            )
            agents[claimed] = (entry["agent_id"], entry["agent_source"])

        assert len(set(map(repr, outcomes.values()))) == 1, outcomes
        # Not vacuous: the identity really was captured, and differed between the runs.
        assert len(set(agents.values())) == len(agents)
        assert agents["claude-code"] == ("claude-code", "client_info")
        assert agents[""] == ("", "")
        assert outcomes["Claude"] == _EXPECTED[scenario]
