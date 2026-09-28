"""Replays each AI client's recorded handshake (``tests/fixtures/ai_clients/<client>/``) through the
real organization-mode OAuth routes and ``/mcp``, in-process: DCR ``/register`` with the recorded
body, ``/authorize`` with the IdP leg faked, ``/token``, ``initialize`` with the recorded
``clientInfo``, ``tools/list`` and one read ``tools/call``.

It catches a server-side change that breaks a client CI cannot run itself (claude.ai, for one), and
pins what the call is attributed to (ADR 0035): unpinned, from the client's own claims, where the
DCR ``client_name`` wins over ``clientInfo`` and the source is ``client_info``; and pinned by an
admin on Settings -> AI systems, the one attested source, ``oauth_client``. Each fixture's
``README.md`` names both expected ``agent_id`` values, so a fixture carries its own expectation.
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

from privacyfence.agent_identity import AgentSource, current_agent
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
EXPECTED_RE = re.compile(r"^- Expected agent_id, (unpinned|pinned): `([^`]+)`$", re.MULTILINE)
ADMIN = "admin@example.com"


@dataclass(frozen=True)
class RecordedClient:
    slug: str
    register_body: dict
    initialize_params: dict
    expected_unpinned: str
    expected_pinned: str

    @property
    def redirect_uri(self) -> str:
        return self.register_body["redirect_uris"][0]

    @property
    def client_info(self) -> types.Implementation:
        info = self.initialize_params["clientInfo"]
        return types.Implementation(name=info["name"], version=info["version"])


def _load(directory: Path) -> RecordedClient:
    expected = dict(EXPECTED_RE.findall((directory / "README.md").read_text(encoding="utf-8")))
    assert set(expected) == {"unpinned", "pinned"}, (
        f"{directory.name}/README.md must name both expected agent_ids (see ai_clients/README.md)"
    )
    return RecordedClient(
        slug=directory.name,
        register_body=json.loads((directory / "register.json").read_text(encoding="utf-8")),
        initialize_params=json.loads((directory / "initialize.json").read_text(encoding="utf-8")),
        expected_unpinned=expected["unpinned"], expected_pinned=expected["pinned"],
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
    """The org-mode ``/mcp`` and OAuth routes, wired to the provider's DCR names and admin pins the
    way server.py's org app wires them."""
    isolate_org_oauth_stores(tmp_path, monkeypatch)
    provider = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    dispatcher = McpDispatcher(lambda: {"echo": AgentEchoConnector()})
    mcp_route, session_manager = mount_mcp(
        dispatcher, verifier=provider,
        client_names=provider.client_name, pinned_agents=provider.pinned_agent_id,
    )
    app = Starlette(routes=[mcp_route, *mount_org_oauth(provider, issuer_url=ISSUER)])
    return app, provider, session_manager


async def _replay(recorded: RecordedClient, tmp_path, monkeypatch, *, pin: bool) -> dict:
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

    if pin:
        provider.agent_pins.pin(client_id, recorded.expected_pinned, pinned_by=ADMIN)

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
async def test_unpinned_handshake_succeeds_and_is_attributed_to_the_clients_claim(client_dir, tmp_path, monkeypatch):
    recorded = _load(client_dir)
    assert await _replay(recorded, tmp_path, monkeypatch, pin=False) == {
        "agent_id": recorded.expected_unpinned, "agent_source": AgentSource.CLIENT_INFO.value,
    }


@pytest.mark.parametrize("client_dir", CLIENT_DIRS, ids=lambda p: p.name)
async def test_pinned_handshake_is_attributed_to_the_admins_pin(client_dir, tmp_path, monkeypatch):
    recorded = _load(client_dir)
    assert await _replay(recorded, tmp_path, monkeypatch, pin=True) == {
        "agent_id": recorded.expected_pinned, "agent_source": AgentSource.OAUTH_CLIENT.value,
    }
