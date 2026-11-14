"""The reference upstream MCP server verifies the v1 identity claim and filters by audience."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence.identity_claim import CLAIM_HEADER, ClaimSigner, IdentityCaller, request_hash
from privacyfence.upstream_claim_keys import KeySet

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_reference_server", Path(__file__).parents[2] / "scripts" / "upstream_mcp_reference_server.py",
)
assert _SPEC and _SPEC.loader
ref = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ref
_SPEC.loader.exec_module(ref)

AUD = "https://kh.example/mcp"
ISSUER = "https://pf.example"


@pytest.fixture
def signer(tmp_path):
    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    return ClaimSigner(issuer=ISSUER, keys=keys)


def _caller(*audiences: str, principal_type: str = "claude_client") -> IdentityCaller:
    return IdentityCaller(sub="u1", principal_type=principal_type, email="a@b.example", audiences=tuple(audiences))


def _app(signer, **kw):
    return ref.build_app(audience=AUD, issuer=ISSUER, jwks=signer.jwks(), **kw)


def _mint(signer, caller, body=None, *, audience: str = AUD, now: float | None = None) -> str:
    req = None
    if isinstance(body, dict) and body.get("method") == "tools/call":
        params = body["params"]
        req = request_hash(params["name"], params.get("arguments") or {})
    return signer.mint(audience=audience, caller=caller, req=req, lifetime=30, now=now).token


@contextlib.asynccontextmanager
async def _session(app, signer, caller):
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            # Each request gets its own claim, bound to its call, like IdentityClaimAuth does.
            class _Auth(httpx2.Auth):
                requires_request_body = True

                def auth_flow(self, request):
                    try:
                        body = json.loads(request.content)
                    except ValueError:
                        body = None
                    who = caller if isinstance(body, dict) and body.get("method") == "tools/call" else _caller(
                        principal_type=caller.principal_type,
                    )
                    request.headers[CLAIM_HEADER] = _mint(signer, who, body)
                    yield request

            http.auth = _Auth()
            async with streamable_http_client("http://testserver/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _titles(result) -> list[str]:
    return [d["title"] for d in json.loads(result.content[0].text)]


async def _post(app, token: str | None, body=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers[CLAIM_HEADER] = token
    body = body if body is not None else {"jsonrpc": "2.0", "id": 1, "method": "ping"}
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


async def test_search_filters_by_audiences(signer):
    async with _session(_app(signer), signer, _caller("general", "hr")) as session:
        result = await session.call_tool("search", {"query": "POLICY"})
    assert sorted(_titles(result)) == ["HR policy", "Office policy", "Policy overview"]


async def test_get_doc_of_an_audience_the_claim_lacks_is_not_found(signer):
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
    async with _session(_app(signer), signer, _caller("hr", principal_type="service")) as session:
        tools = await session.list_tools()
        result = await session.call_tool("search", {"query": "policy"})
    assert {t.name for t in tools.tools} == {"search", "get_doc", "propose_change"}
    assert _titles(result) == []


async def test_missing_claim_is_401(signer):
    response = await _post(_app(signer), None)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_claim"}


async def test_authorization_header_alone_is_401(signer):
    token = _mint(signer, _caller("hr"))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=_app(signer)), base_url="http://t") as http:
        response = await http.post(
            "/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert response.status_code == 401


async def test_expired_claim_is_401(signer):
    token = _mint(signer, _caller("hr"))
    later = _app(signer, clock=lambda: time.time() + 3600)
    assert (await _post(later, token)).status_code == 401


async def test_wrong_audience_is_401(signer):
    token = _mint(signer, _caller("hr"), audience="https://other.example/mcp")
    assert (await _post(_app(signer), token)).status_code == 401


async def test_wrong_typ_is_401(signer):
    token = _mint(signer, _caller("hr"))
    payload = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(payload, Ed25519PrivateKey.generate(), algorithm="EdDSA", headers={"kid": "k", "typ": "JWT"})
    assert (await _post(_app(signer), forged)).status_code == 401


async def test_replayed_claim_is_401(signer):
    app = _app(signer)
    token = _mint(signer, _caller("hr"))
    async with app.router.lifespan_context(app):
        first = await _post(app, token)
        assert first.status_code != 401
        assert (await _post(app, token)).status_code == 401


def test_exactly_one_key_source_is_required(signer):
    with pytest.raises(ValueError):
        ref.build_app(audience=AUD, issuer=ISSUER)
    with pytest.raises(ValueError):
        ref.build_app(audience=AUD, issuer=ISSUER, jwks=signer.jwks(), jwks_url="https://x/jwks")


async def test_claim_for_other_arguments_is_401(signer):
    app = _app(signer)
    token = _mint(signer, _caller("hr"), _call("search", {"query": "policy"}))
    async with app.router.lifespan_context(app):
        response = await _post(app, token, _call("search", {"query": "salary"}))
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_claim"}


async def test_initialize_claim_carries_no_audiences_and_lists_no_documents(signer):
    app = _app(signer)
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    async with app.router.lifespan_context(app):
        ok = await _post(app, _mint(signer, _caller(), init), init)
        assert ok.status_code == 200
        search = _call("search", {"query": "policy"})
        listed = await _post(app, _mint(signer, _caller(), search), search)
    assert _result_titles(listed) == []


async def test_list_body_is_401(signer):
    app = _app(signer)
    body = [_call("search", {"query": "policy"})]
    async with app.router.lifespan_context(app):
        response = await _post(app, _mint(signer, _caller("hr")), body)
    assert response.status_code == 401


async def test_oversized_body_is_413(signer):
    app = _app(signer)
    big = _call("search", {"query": "x" * (ref.MAX_BODY_BYTES + 10)})
    async with app.router.lifespan_context(app):
        response = await _post(app, _mint(signer, _caller()), big)
    assert response.status_code == 413
    assert response.text == "request too large"
