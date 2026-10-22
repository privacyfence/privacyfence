"""Replays each AI client's recorded handshake (``tests/fixtures/ai_clients/<client>/``) through the
real organization-mode OAuth routes and ``/mcp``, in-process: DCR ``/register`` with the recorded
body, ``/authorize`` with the IdP leg faked, ``/token``, ``initialize`` with the recorded
``clientInfo``, ``tools/list`` and one read ``tools/call``.

It catches a server-side change that breaks a client CI cannot run itself (claude.ai, for one), and
pins what the call is attributed to: the AI system its redirect URIs attest (ADR 0224),
``oauth_client``, or else the client's own claims, where the DCR ``client_name`` wins over
``clientInfo`` and the source is ``client_info``. Each fixture's ``README.md`` names the expected
``agent_id`` and ``agent_source``, so a fixture carries its own expectation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from mcp import types
from starlette.applications import Starlette

from privacyfence.agent_identity import current_agent
from privacyfence.connector import Connector, ToolSpec
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.routes_mcp import mcp_lifespan, mount_mcp, mount_org_oauth
from tests.unit.web.conftest import (
    ISSUER,
    authorize_and_get_code,
    exchange_for_tokens,
    isolate_org_oauth_stores,
    mcp_session,
    org_idp,
    register_client,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "ai_clients"
EXPECTED_RE = re.compile(r"^- Expected (agent_id|agent_source): `([^`]+)`$", re.MULTILINE)


@dataclass(frozen=True)
class RecordedClient:
    slug: str
    register_body: dict
    initialize_params: dict
    expected_agent_id: str
    expected_agent_source: str

    @property
    def redirect_uri(self) -> str:
        return self.register_body["redirect_uris"][0]

    @property
    def client_info(self) -> types.Implementation:
        info = self.initialize_params["clientInfo"]
        return types.Implementation(name=info["name"], version=info["version"])


def _load(directory: Path) -> RecordedClient:
    expected = dict(EXPECTED_RE.findall((directory / "README.md").read_text(encoding="utf-8")))
    assert set(expected) == {"agent_id", "agent_source"}, (
        f"{directory.name}/README.md must name the expected agent_id and agent_source (see ai_clients/README.md)"
    )
    return RecordedClient(
        slug=directory.name,
        register_body=json.loads((directory / "register.json").read_text(encoding="utf-8")),
        initialize_params=json.loads((directory / "initialize.json").read_text(encoding="utf-8")),
        expected_agent_id=expected["agent_id"], expected_agent_source=expected["agent_source"],
    )


CLIENT_DIRS = sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())


class AgentEchoConnector(Connector):
    """One read tool that returns the identity its call ran under."""

    @property
    def name(self) -> str:
        return "echo"

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name="echo_whoami", description="Returns the calling agent.", params=[], read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        agent = current_agent()
        return {"agent_id": agent.id, "agent_source": agent.source.value}


def _build_app(tmp_path, monkeypatch):
    """The org-mode ``/mcp`` and OAuth routes, wired to the provider's DCR names and redirect-URI
    attestation the way server.py's org app wires them."""
    isolate_org_oauth_stores(tmp_path, monkeypatch)
    provider = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    dispatcher = McpDispatcher(lambda: {"echo": AgentEchoConnector()})
    mcp_route, session_manager = mount_mcp(
        dispatcher, verifier=provider,
        client_names=provider.client_name, attested_agents=provider.attested_agent_id,
    )
    app = Starlette(routes=[mcp_route, *mount_org_oauth(provider, issuer_url=ISSUER)])
    return app, provider, session_manager


async def _replay(recorded: RecordedClient, tmp_path, monkeypatch) -> dict:
    """Runs the recorded handshake end to end and returns the read call's structured result."""
    app, provider, session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await register_client(client, recorded.register_body)
        assert registration["client_name"] == recorded.register_body.get("client_name")
        assert registration["redirect_uris"] == recorded.register_body["redirect_uris"]
        auth_method = recorded.register_body.get("token_endpoint_auth_method", "client_secret_basic")
        assert registration["token_endpoint_auth_method"] == auth_method
        # A confidential client (claude.ai) needs a secret issued, and presents it at /token.
        client_secret = registration.get("client_secret") if auth_method != "none" else None
        assert (client_secret is not None) == (auth_method != "none"), registration
        client_id = registration["client_id"]
        code, verifier = await authorize_and_get_code(
            client, client_id=client_id, monkeypatch=monkeypatch, redirect_uri=recorded.redirect_uri,
            claims={"sub": "alice", "email": "alice@example.com"},
        )
        tokens = await exchange_for_tokens(
            client, client_id=client_id, code=code, code_verifier=verifier, redirect_uri=recorded.redirect_uri,
            client_secret=client_secret,
        )

    async with mcp_lifespan(session_manager):
        async with mcp_session(app, access_token=tokens["access_token"], client_info=recorded.client_info) as session:
            listed = await session.list_tools()
            assert "echo_whoami" in {tool.name for tool in listed.tools}
            result = await session.call_tool("echo_whoami", {"reason": "replay"})
    assert not result.is_error, result.content
    return result.structured_content


def test_every_fixture_directory_is_complete():
    assert CLIENT_DIRS, f"no fixtures under {FIXTURES_DIR}"
    for directory in CLIENT_DIRS:
        assert {"register.json", "initialize.json", "README.md"} <= {p.name for p in directory.iterdir()}, directory


@pytest.mark.parametrize("client_dir", CLIENT_DIRS, ids=lambda p: p.name)
async def test_handshake_succeeds_and_is_attributed(client_dir, tmp_path, monkeypatch):
    recorded = _load(client_dir)
    assert await _replay(recorded, tmp_path, monkeypatch) == {
        "agent_id": recorded.expected_agent_id, "agent_source": recorded.expected_agent_source,
    }
