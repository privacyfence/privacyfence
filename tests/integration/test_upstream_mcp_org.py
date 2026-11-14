"""An organization server in front of the reference upstream, over real loopback sockets (ADR 0283-0285).

The server is a real ``WebServer`` in organization mode; the upstream is the p6 reference server on
another port, verifying PrivacyFence's claims against the server's own ``/upstream-mcp/jwks``. The
AI client is the official ``mcp`` client with a real access token for a verified person whose
admin-config grants give them the ``general`` domain only.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence import gate as gate_module
from privacyfence import org_identity as oi
from privacyfence import upstream_claims, upstream_mcp_registry, upstream_mcp_runtime
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.connectors import upstream_mcp as connector_module
from privacyfence.principal import Principal, current_principal
from privacyfence.upstream_mcp_config import GrantStore, parse_upstreams
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import OrgSessionStore
from privacyfence.web.server import OrgAuth, WebServer
from privacyfence.web_approval_ui import WebApprovalUI

pytestmark = pytest.mark.timeout(60)

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_reference_server", Path(__file__).parents[2] / "scripts" / "upstream_mcp_reference_server.py",
)
assert _SPEC and _SPEC.loader
reference = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reference
_SPEC.loader.exec_module(reference)

_AUDIENCE = "ref-upstream"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"localhost:{port} never became connectable")


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


class _CountingApp:
    """Counts the MCP POSTs that reach the reference server."""

    def __init__(self, app) -> None:
        self._app = app
        self.posts = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            self.posts += 1
        await self._app(scope, receive, send)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    # Loopback calls go direct, whatever proxy the environment carries.
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setattr("privacyfence.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    org_port, ref_port = _free_port(), _free_port()
    issuer_url = f"http://localhost:{org_port}"

    (tmp_path / "admin_config.json").write_text(json.dumps({
        "upstream_mcp": {"ref": {"grants": [{"match": "*", "domains": ["general"]}]}},
    }), encoding="utf-8")
    upstreams = parse_upstreams({
        "version": 1,
        "upstreams": [{
            "id": "ref", "label": "Reference upstream", "url": f"http://localhost:{ref_port}/mcp",
            "audience": _AUDIENCE,
            "tools": {
                "search": {"gate": "auto", "verb": "search"},
                "get_doc": {"gate": "auto", "verb": "read"},
                "propose_change": {
                    "gate": "popup", "verb": "add", "effect": "Adds a change proposal to the reference upstream.",
                },
            },
        }],
    }, source="test")
    upstream_mcp_registry.register(upstreams)
    upstream_mcp_runtime.configure(
        upstreams=upstreams, signer=upstream_claims.load_signer(issuer_url),
        grants=GrantStore(tmp_path / "admin_config.json", frozenset({"ref"})),
    )

    # The upstream verifies against the organization server's own key set.
    counting = _CountingApp(reference.build_app(
        audience=_AUDIENCE, issuer=f"{issuer_url}/upstream-mcp", jwks_url=f"{issuer_url}/upstream-mcp/jwks",
    ))
    upstream_server = uvicorn.Server(uvicorn.Config(counting, host="127.0.0.1", port=ref_port, log_level="warning"))
    upstream_thread = threading.Thread(target=upstream_server.run, daemon=True)
    upstream_thread.start()
    _wait_until_connectable(ref_port)

    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{issuer_url}/oauth/idp/callback")
    registry = ConnectorRegistry(factory=lambda principal: upstream_mcp_runtime.connectors_for_principal())
    dispatcher = McpDispatcher(lambda: registry.get(current_principal()).connectors, mode="org")
    org = OrgAuth(
        provider=provider, sessions=OrgSessionStore(), idp=_idp(), issuer_url=issuer_url, connector_registry=registry,
    )
    server = WebServer(WebApprovalUI(), host="localhost", port=org_port, org=org, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable(org_port)
        deadline = time.monotonic() + 15
        while upstream_mcp_runtime.status()[0]["state"] != "ready":
            assert time.monotonic() < deadline, "the upstream's tool list never arrived"
            time.sleep(0.05)
        token = provider._mint_tokens(
            client_id="claude", scopes=[], resource=None,
            principal=Principal(id="u1", email="ann@example.com", email_verified=True),
        ).access_token
        yield issuer_url, token, counting
    finally:
        server.stop()
        upstream_server.should_exit = True
        upstream_thread.join(timeout=10)


async def _session(issuer_url: str, token: str):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    return http, streamable_http_client(f"{issuer_url}/mcp", http_client=http)


async def test_a_person_with_the_general_grant_reads_only_general_documents(stack):
    issuer_url, token, _counting = stack
    http, transport = await _session(issuer_url, token)
    async with http, transport as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"ref_search", "ref_get_doc", "ref_propose_change"} <= names

        found = await session.call_tool("ref_search", {"query": "policy", "reason": "integration test"})
        assert found.is_error is not True
        titles = {doc["title"] for doc in json.loads(found.content[0].text)}
        assert titles == {"Policy overview", "Office policy"}

        hidden = await session.call_tool("ref_get_doc", {"id": "3", "reason": "integration test"})
        assert hidden.is_error is True  # the HR document is not in the person's domains

        status = await session.call_tool("privacyfence_status", {})
        upstreams = status.structured_content["upstreams"]
        assert [(row["id"], row["state"], row["tools_exposed"], row["missing_tools"]) for row in upstreams] == [
            ("ref", "ready", 3, []),
        ]


async def test_a_denied_popup_write_never_reaches_the_upstream(stack, monkeypatch):
    issuer_url, token, counting = stack

    async def deny(**_kwargs):
        raise gate_module.GateDeniedError("Request denied: a person said no.")

    monkeypatch.setattr(connector_module, "gated_call", deny)
    http, transport = await _session(issuer_url, token)
    async with http, transport as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        before = counting.posts
        result = await session.call_tool(
            "ref_propose_change", {"id": "1", "text": "Change it", "reason": "integration test"},
        )
        assert result.is_error is True
        assert "denied" in result.content[0].text.lower()
        assert counting.posts == before
