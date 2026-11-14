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


# -- the v1 claim (X-PrivacyFence-Claim, EdDSA) ---------------------------------------------------------

V1_ISSUER = "https://pf.example"


@pytest.fixture
def ed_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


def _ed_jwks(key) -> dict:
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": x, "use": "sig", "alg": "EdDSA", "kid": "k1"}]}


def _v1_token(key, *, audiences=(), body: dict | None = None, principal_type="claude_client") -> str:
    import jwt

    now = int(time.time())
    payload = {
        "claim_version": 1, "iss": V1_ISSUER, "aud": AUD, "sub": "u1", "principal_type": principal_type,
        "audiences": sorted(audiences), "tools": [], "iat": now, "exp": now + 60,
        "jti": f"j{time.monotonic_ns()}",
    }
    if body is not None and body.get("method") == "tools/call":
        from privacyfence.identity_claim import request_hash

        params = body["params"]
        payload["req"] = request_hash(params["name"], params.get("arguments") or {})
    return jwt.encode(payload, key, algorithm="EdDSA", headers={"kid": "k1", "typ": "pf-identity-claim+jwt"})


def _v1_app(key, **kw):
    return ref.build_app(audience=AUD, issuer=ISSUER, jwks=_ed_jwks(key), **kw)


async def _v1_post(app, token: str, body, *, session_id: str | None = None):
    headers = {
        "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
        "X-PrivacyFence-Claim": token,
    }
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver") as http:
        return await http.post("/mcp", headers=headers, json=body)


def _call(tool: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


def _result_titles(response) -> list[str]:
    text = response.text
    # Streamable HTTP answers with an SSE "data:" line carrying the JSON-RPC result.
    data = next(line[5:] for line in text.splitlines() if line.startswith("data:")) if "data:" in text else text
    payload = json.loads(data)["result"]["content"][0]["text"]
    return [d["title"] for d in json.loads(payload)]


async def test_v1_claim_returns_only_documents_of_its_audiences(ed_key):
    app = _v1_app(ed_key)
    body = _call("search", {"query": "policy"})
    async with app.router.lifespan_context(app):
        response = await _v1_post(app, _v1_token(ed_key, audiences=("general", "hr"), body=body), body)
    assert response.status_code == 200
    assert sorted(_result_titles(response)) == ["HR policy", "Office policy", "Policy overview"]


async def test_v1_claim_for_other_arguments_is_401(ed_key):
    app = _v1_app(ed_key)
    token = _v1_token(ed_key, audiences=("hr",), body=_call("search", {"query": "policy"}))
    async with app.router.lifespan_context(app):
        response = await _v1_post(app, token, _call("search", {"query": "salary"}))
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_claim"}


async def test_v1_initialize_claim_carries_no_audiences_and_lists_no_documents(ed_key):
    app = _v1_app(ed_key)
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    async with app.router.lifespan_context(app):
        # Even a claim that tries to carry audiences for a non-call request grants none: the verifier
        # refuses a req on it, and a claim without req yields no audiences.
        ok = await _v1_post(app, _v1_token(ed_key, audiences=()), init)
        assert ok.status_code == 200
        search = _call("search", {"query": "policy"})
        listed = await _v1_post(app, _v1_token(ed_key, audiences=(), body=search), search)
    assert _result_titles(listed) == []


async def test_v1_list_body_is_401(ed_key):
    app = _v1_app(ed_key)
    body = [_call("search", {"query": "policy"})]
    async with app.router.lifespan_context(app):
        response = await _v1_post(app, _v1_token(ed_key, audiences=("hr",)), body)
    assert response.status_code == 401


async def test_v1_oversized_body_is_413(ed_key):
    app = _v1_app(ed_key)
    big = _call("search", {"query": "x" * (ref.MAX_BODY_BYTES + 10)})
    async with app.router.lifespan_context(app):
        response = await _v1_post(app, _v1_token(ed_key), big)
    assert response.status_code == 413
    assert response.text == "request too large"


async def test_v1_issuer_has_no_upstream_mcp_suffix(ed_key):
    app = _v1_app(ed_key)
    body = _call("search", {"query": "policy"})
    async with app.router.lifespan_context(app):
        response = await _v1_post(app, _v1_token(ed_key, audiences=("hr",), body=body), body)
    assert response.status_code == 200
