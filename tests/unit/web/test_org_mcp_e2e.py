"""End-to-end proof that Claude adds the org-mode connector by DCR and
that audience separation holds (ADR 0011) -- drives the real ``/mcp`` Streamable HTTP
endpoint and the real OAuth 2.1 authorization-server routes
(``mount_org_oauth``) together, over an in-process ASGI transport, exactly
the way a real Claude client would: register via DCR, complete
``/authorize`` (with the IdP leg faked -- that dance has its own thorough
coverage in test_oauth_provider.py and test_org_identity.py), exchange the
code at ``/token``, then actually call a tool over ``/mcp`` with the
resulting bearer token and confirm it resolves to the signed-in human's own
Principal -- not the OAuth client's id, not the local principal.

Also covers audience separation for org mode specifically: an
access token minted by this same authorization server must never be
accepted as an org-mode browser session (web/org_session.py), and a
browser session cookie must never be accepted as a bearer token on
``/mcp``. (Local mode's own audience-separation test predates this file --
see web/test_routes_mcp.py.)
"""
from __future__ import annotations

import httpx
from starlette.applications import Starlette

from privacyfence.connector import Connector, ToolSpec
from privacyfence.principal import current_principal
from privacyfence.web import org_session
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.routes_mcp import mcp_lifespan, mount_mcp, mount_org_oauth
from tests.unit.web.conftest import (
    CLAUDE_REDIRECT_URI,
    ISSUER,
    authorize_and_get_code,
    exchange_for_tokens,
    isolate_org_oauth_stores,
    mcp_session,
    org_idp,
    register_client,
)


class WhoAmIConnector(Connector):
    @property
    def name(self) -> str:
        return "whoami"

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name="whoami", description="Returns the current principal.", params=[], read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        p = current_principal()
        return {"id": p.id, "email": p.email, "display_name": p.display_name, "is_admin": p.is_admin}


def _build_app(tmp_path, monkeypatch):
    isolate_org_oauth_stores(tmp_path, monkeypatch)
    provider = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    dispatcher = McpDispatcher(lambda: {"whoami": WhoAmIConnector()})
    mcp_route, session_manager = mount_mcp(dispatcher, verifier=provider)
    oauth_routes = mount_org_oauth(provider, issuer_url=ISSUER)
    app = Starlette(routes=[mcp_route, *oauth_routes])
    return app, provider, session_manager


async def test_dcr_authorize_token_and_a_real_tool_call_resolve_to_the_signed_in_principal(tmp_path, monkeypatch):
    app, _provider, session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await register_client(client)
        code, verifier = await authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch,
            claims={"sub": "alice", "email": "alice@example.com", "name": "Alice A.", "groups": ["admins"]},
        )
        tokens = await exchange_for_tokens(
            client, client_id=registration["client_id"], code=code, code_verifier=verifier,
        )

    async with mcp_lifespan(session_manager):
        async with mcp_session(app, access_token=tokens["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structured_content == {
                "id": "alice", "email": "alice@example.com", "display_name": "Alice A.", "is_admin": True,
            }


async def test_two_different_humans_authorizing_the_same_claude_client_get_isolated_principals(tmp_path, monkeypatch):
    app, _provider, session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await register_client(client)

        code_a, verifier_a = await authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "alice"},
        )
        tokens_a = await exchange_for_tokens(
            client, client_id=registration["client_id"], code=code_a, code_verifier=verifier_a,
        )

        code_b, verifier_b = await authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "bob"},
        )
        tokens_b = await exchange_for_tokens(
            client, client_id=registration["client_id"], code=code_b, code_verifier=verifier_b,
        )

    async with mcp_lifespan(session_manager):
        async with mcp_session(app, access_token=tokens_a["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structured_content["id"] == "alice"

        async with mcp_session(app, access_token=tokens_b["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structured_content["id"] == "bob"


async def test_mcp_access_token_is_rejected_as_an_org_session_cookie(tmp_path, monkeypatch):
    """Audience separation, org-mode side: an MCP bearer token must
    never double as a browser session."""
    app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await register_client(client)
        code, verifier = await authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "alice"},
        )
        tokens = await exchange_for_tokens(
            client, client_id=registration["client_id"], code=code, code_verifier=verifier,
        )

    sessions = org_session.OrgSessionStore()
    # The MCP access token was never handed to OrgSessionStore.create() --
    # presenting it as a session cookie value must not resolve to anything.
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"cookie", f"{org_session.SESSION_COOKIE}={tokens['access_token']}".encode())],
    }
    assert org_session.authenticated(Request(scope), sessions) is None


async def test_org_session_cookie_is_rejected_as_an_mcp_bearer_token(tmp_path, monkeypatch):
    """Audience separation, the other direction: a browser session id
    must never verify as an MCP access token."""
    _app, provider, _session_manager = _build_app(tmp_path, monkeypatch)
    from privacyfence.principal import Principal

    sessions = org_session.OrgSessionStore()
    session_id = sessions.create(Principal(id="alice"))

    assert await provider.verify_token(session_id) is None


async def test_dcr_registration_is_visible_to_a_fresh_provider_instance(tmp_path, monkeypatch):
    """The DCR client store survives a daemon restart -- a fresh
    OrgOAuthProvider pointed at the same clients file sees clients an
    earlier one registered, so Claude never has to re-run DCR after a
    restart."""
    isolate_org_oauth_stores(tmp_path, monkeypatch)
    first = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    await first.register_client(OAuthClientInformationFull(
        client_id="claude-1", redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URI)], token_endpoint_auth_method="none",
    ))

    second = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    assert await second.get_client("claude-1") is not None


class TestIdpCallbackRouteErrorHandling:
    """The /oauth/idp/callback route's own error branches (routes_mcp.py's
    mount_org_oauth) -- OrgOAuthProvider.handle_idp_callback's own logic is
    covered directly in test_oauth_provider.py; this covers the HTTP-level
    wrapping around it."""

    async def test_idp_error_param_returns_400_not_500(self, tmp_path, monkeypatch):
        app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            r = await client.get("/oauth/idp/callback", params={"error": "access_denied", "state": "whatever"})
        assert r.status_code == 400

    async def test_missing_state_or_code_returns_400(self, tmp_path, monkeypatch):
        app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            missing_code = await client.get("/oauth/idp/callback", params={"state": "s"})
            missing_state = await client.get("/oauth/idp/callback", params={"code": "c"})
        assert missing_code.status_code == 400
        assert missing_state.status_code == 400

    async def test_provider_failure_returns_400_not_500(self, tmp_path, monkeypatch):
        app, provider, _session_manager = _build_app(tmp_path, monkeypatch)

        async def failing_callback(*, state, code):
            raise ValueError("invalid or expired authorization attempt")

        monkeypatch.setattr(provider, "handle_idp_callback", failing_callback)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            r = await client.get("/oauth/idp/callback", params={"state": "s", "code": "c"})
        assert r.status_code == 400
