"""The reference upstream MCP server verifies claims and filters by domain."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence import oidc_provider
from privacyfence.upstream_claims import CLAIM_TYPE, CallerClaim, ClaimSigner, service_caller
from privacyfence.upstream_mcp_config import UpstreamServer

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_reference_server", Path(__file__).parents[2] / "scripts" / "upstream_mcp_reference_server.py",
)
assert _SPEC and _SPEC.loader
ref = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ref
_SPEC.loader.exec_module(ref)

AUD = "https://kh.example/mcp"
ISSUER = "https://pf.example/upstream-mcp"
UPSTREAM = UpstreamServer(id="kh", label="KH", url=AUD, audience=AUD, tools=())


@pytest.fixture
def signer(tmp_path):
    return ClaimSigner(issuer=ISSUER, key=oidc_provider.load_or_create_signing_key(tmp_path / "k.pem"))


def _caller(*domains: str) -> CallerClaim:
    return CallerClaim(
        sub="u1", email="a@b.example", email_verified=True, domains=tuple(domains),
        act={"type": "ai_client", "attested": False, "name": "", "claimed_name": ""},
    )


def _app(signer, **kw):
    return ref.build_app(audience=AUD, issuer=ISSUER, jwks=signer.jwks(), **kw)


@contextlib.asynccontextmanager
async def _session(app, signer, caller):
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            # Each request gets its own claim, like ClaimAuth does.
            class _Auth(httpx2.Auth):
                def auth_flow(self, request):
                    request.headers["Authorization"] = "Bearer " + signer.mint(upstream=UPSTREAM, caller=caller, tool=None)
                    yield request

            http.auth = _Auth()
            async with streamable_http_client("http://testserver/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _titles(result) -> list[str]:
    return [d["title"] for d in json.loads(result.content[0].text)]


async def _raw_post(app, token: str | None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver") as http:
        return await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})


async def test_search_filters_by_domains(signer):
    async with _session(_app(signer), signer, _caller("general", "hr")) as session:
        result = await session.call_tool("search", {"query": "POLICY"})
    assert sorted(_titles(result)) == ["HR policy", "Office policy", "Policy overview"]


async def test_get_doc_of_a_domain_the_claim_lacks_is_not_found(signer):
    async with _session(_app(signer), signer, _caller("general")) as session:
        hr = await session.call_tool("get_doc", {"id": "3"})
        ok = await session.call_tool("get_doc", {"id": "1"})
    assert hr.is_error and hr.content[0].text == "not found"
    assert json.loads(ok.content[0].text)["title"] == "Policy overview"


async def test_propose_change_names_the_sub(signer):
    async with _session(_app(signer), signer, _caller()) as session:
        result = await session.call_tool("propose_change", {"id": "1", "text": "x"})
    assert json.loads(result.content[0].text) == {"proposal": "accepted", "by": "u1"}


async def test_a_service_claim_lists_tools_and_gets_no_documents(signer):
    async with _session(_app(signer), signer, service_caller()) as session:
        tools = await session.list_tools()
        result = await session.call_tool("search", {"query": "policy"})
    assert {t.name for t in tools.tools} == {"search", "get_doc", "propose_change"}
    assert _titles(result) == []


async def test_missing_claim_is_401(signer):
    response = await _raw_post(_app(signer), None)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_claim"}


async def test_expired_claim_is_401(signer):
    token = signer.mint(upstream=UPSTREAM, caller=_caller("hr"), tool=None, now=time.time() - 3600)
    assert (await _raw_post(_app(signer), token)).status_code == 401


async def test_wrong_audience_is_401(signer):
    other = UpstreamServer(id="x", label="X", url=AUD, audience="https://other.example/mcp", tools=())
    token = signer.mint(upstream=other, caller=_caller("hr"), tool=None)
    assert (await _raw_post(_app(signer), token)).status_code == 401


async def test_wrong_typ_is_401(signer):
    import jwt

    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUD, "sub": "u1", "domains": ["hr"], "nbf": now, "exp": now + 60, "jti": "j1"},
        signer._key.private_key, algorithm="RS256", headers={"kid": signer._key.kid, "typ": "JWT"},
    )
    assert CLAIM_TYPE != "JWT"
    assert (await _raw_post(_app(signer), token)).status_code == 401


async def test_replayed_claim_is_401(signer):
    app = _app(signer)
    token = signer.mint(upstream=UPSTREAM, caller=_caller("hr"), tool=None)
    async with app.router.lifespan_context(app):
        first = await _raw_post(app, token)
        assert first.status_code != 401
        assert (await _raw_post(app, token)).status_code == 401


def test_exactly_one_key_source_is_required(signer):
    with pytest.raises(ValueError):
        ref.build_app(audience=AUD, issuer=None)
    with pytest.raises(ValueError):
        ref.build_app(audience=AUD, issuer=None, jwks=signer.jwks(), jwks_url="https://x/jwks")
