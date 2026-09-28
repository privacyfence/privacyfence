"""Shared org-mode OAuth + ``/mcp`` helpers for driving the real authorization-server routes and the
real Streamable HTTP endpoint in-process, the way a real AI client does: register via DCR, complete
``/authorize`` with the IdP leg faked, redeem the code at ``/token``, then open an MCP session with
the bearer token.

test_org_mcp_e2e.py uses them with one fixed Claude registration; test_ai_client_replay.py replays
each recorded client under ``tests/fixtures/ai_clients/`` through them. The IdP dance itself has its
own coverage in test_oauth_provider.py and test_org_identity.py, which is why it is faked here.
"""
from __future__ import annotations

import contextlib
import urllib.parse as up

import httpx
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from privacyfence import org_identity as oi

ISSUER = "https://pf.example.com"
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
CLIENT_STATE = "clients-own-state"


def org_idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
        admin_group_claim="groups", admin_group_values=("admins",),
    )


def isolate_org_oauth_stores(tmp_path, monkeypatch) -> None:
    """Points the DCR client store, the refresh-token store and the AI-systems pin store at
    ``tmp_path``, so a provider built afterwards touches nothing outside the test."""
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider.pins_file_path", lambda: tmp_path / "agent_pins.json")


async def register_client(client: httpx.AsyncClient, body: dict | None = None) -> dict:
    """POSTs a DCR ``/register`` request -- ``body`` as a client sent it, or a minimal Claude
    registration -- and returns the issued client information."""
    if body is None:
        body = {
            "redirect_uris": [CLAUDE_REDIRECT_URI], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        }
    r = await client.post("/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def authorize_and_get_code(
    client: httpx.AsyncClient, *, client_id: str, monkeypatch, claims: dict,
    redirect_uri: str = CLAUDE_REDIRECT_URI,
) -> tuple[str, str]:
    """Drives /authorize -> (faked IdP) -> /oauth/idp/callback and returns
    ``(code, code_verifier)`` -- the code the client would receive at its ``redirect_uri``, and
    the PKCE verifier it needs to redeem it."""
    verifier, challenge = oi.generate_pkce_pair()
    r = await client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": CLIENT_STATE,
    })
    assert r.status_code == 302, r.text
    own_state = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))["state"]

    monkeypatch.setattr("privacyfence.org_identity.exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
    monkeypatch.setattr(
        "privacyfence.org_identity.verify_id_token", lambda idp, token, *, nonce: {**claims, "nonce": nonce},
    )
    cb = await client.get("/oauth/idp/callback", params={"code": "idp-code", "state": own_state})
    assert cb.status_code == 302, cb.text
    parsed = up.urlparse(cb.headers["location"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == redirect_uri
    qs = dict(up.parse_qsl(parsed.query))
    assert qs["state"] == CLIENT_STATE
    return qs["code"], verifier


async def exchange_for_tokens(
    client: httpx.AsyncClient, *, client_id: str, code: str, code_verifier: str,
    redirect_uri: str = CLAUDE_REDIRECT_URI, client_secret: str | None = None,
) -> dict:
    """Redeems ``code`` at /token. ``client_secret`` is sent in the body (``client_secret_post``)
    when given, as a confidential client such as claude.ai does."""
    data = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": code_verifier,
    }
    if client_secret is not None:
        data["client_secret"] = client_secret
    r = await client.post("/token", data=data)
    assert r.status_code == 200, r.text
    return r.json()


@contextlib.asynccontextmanager
async def mcp_session(app, *, access_token: str, client_info: types.Implementation | None = None):
    """One initialized MCP ClientSession, connected with ``access_token`` as its bearer token and
    claiming ``client_info`` in its handshake (None: the SDK's own default). Does *not* itself
    enter ``mcp_lifespan`` -- ``StreamableHTTPSessionManager.run()`` may only be entered once per
    instance (it raises on a second ``async with``), so a test that opens more than one MCP session
    against the same app wraps all of them in one shared ``async with mcp_lifespan(...)``."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url=ISSUER, headers={"Authorization": f"Bearer {access_token}"},
    ) as http_client:
        async with streamable_http_client(f"{ISSUER}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write, client_info=client_info) as session:
                await session.initialize()
                yield session
