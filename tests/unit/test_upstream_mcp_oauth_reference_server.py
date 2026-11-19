"""The OAuth reference upstream: metadata, registration, PKCE, refresh rotation and the bearer check."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_oauth_reference_server",
    Path(__file__).parents[2] / "scripts" / "upstream_mcp_oauth_reference_server.py",
)
assert _SPEC and _SPEC.loader
ref = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ref
_SPEC.loader.exec_module(ref)

BASE = "http://ref.test"
REDIRECT = "http://pf.test/upstream-mcp/ref/callback"
VERIFIER = "v" * 50


def _challenge(verifier: str = VERIFIER) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, follow_redirects=False)


async def _register(http) -> str:
    resp = await http.post("/register", json={"redirect_uris": [REDIRECT]})
    assert resp.status_code == 201
    return resp.json()["client_id"]


async def _authorize(http, client_id: str, hint: str | None = None, state: str = "st1"):
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": _challenge(), "code_challenge_method": "S256",
    }
    if hint is not None:
        params["login_hint"] = hint
    return await http.get("/authorize", params=params)


def _code_of(resp) -> str:
    return parse_qs(urlparse(resp.headers["location"]).query)["code"][0]


async def _tokens(http, client_id: str, hint: str | None = None) -> dict:
    code = _code_of(await _authorize(http, client_id, hint))
    resp = await http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
    })
    assert resp.status_code == 200
    return resp.json()


@contextlib.asynccontextmanager
async def _mcp(app, access_token: str):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as http:
            async with streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _who(result) -> str:
    return json.loads(result.content[0].text)["account"]


async def test_both_documents_are_served():
    async with _client(ref.build_app(base_url=BASE)) as http:
        resource = (await http.get("/.well-known/oauth-protected-resource")).json()
        meta = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert resource == {"resource": f"{BASE}/mcp", "authorization_servers": [BASE], "scopes_supported": ["mcp:read"]}
    assert meta["issuer"] == BASE
    assert meta["authorization_endpoint"] == f"{BASE}/authorize"
    assert meta["token_endpoint"] == f"{BASE}/token"
    assert meta["registration_endpoint"] == f"{BASE}/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["authorization_response_iss_parameter_supported"] is True


async def test_registration_authorize_and_exchange():
    async with _client(ref.build_app(base_url=BASE)) as http:
        client_id = await _register(http)
        resp = await _authorize(http, client_id)
        assert resp.status_code == 302
        location = urlparse(resp.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
        query = parse_qs(location.query)
        assert query["state"] == ["st1"]
        assert query["iss"] == [BASE]
        tokens = (await http.post("/token", data={
            "grant_type": "authorization_code", "code": query["code"][0], "client_id": client_id,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
        })).json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"] and tokens["refresh_token"]


async def test_registration_and_authorize_reject_bad_requests():
    async with _client(ref.build_app(base_url=BASE)) as http:
        assert (await http.post("/register", json={})).status_code == 400
        client_id = await _register(http)
        good = {
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": _challenge(), "code_challenge_method": "S256",
        }
        assert (await http.get("/authorize", params={**good, "redirect_uri": "http://evil.test/cb"})).status_code == 400
        assert (await http.get("/authorize", params={**good, "client_id": "nope"})).status_code == 400
        assert (await http.get("/authorize", params={**good, "code_challenge_method": "plain"})).status_code == 400
        assert (await http.get("/authorize", params={**good, "response_type": "token"})).status_code == 400


async def test_wrong_verifier_is_refused_and_burns_the_code():
    async with _client(ref.build_app(base_url=BASE)) as http:
        client_id = await _register(http)
        code = _code_of(await _authorize(http, client_id))
        data = {
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": REDIRECT, "code_verifier": "w" * 50,
        }
        bad = await http.post("/token", data=data)
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        again = await http.post("/token", data={**data, "code_verifier": VERIFIER})
        assert again.status_code == 400


async def test_a_reused_code_is_refused():
    async with _client(ref.build_app(base_url=BASE)) as http:
        client_id = await _register(http)
        code = _code_of(await _authorize(http, client_id))
        data = {
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
        }
        assert (await http.post("/token", data=data)).status_code == 200
        second = await http.post("/token", data=data)
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"


async def test_code_is_bound_to_client_and_redirect_uri():
    async with _client(ref.build_app(base_url=BASE)) as http:
        first, second = await _register(http), await _register(http)
        code = _code_of(await _authorize(http, first))
        other_client = await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": second,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
        })
        assert other_client.status_code == 400
        code = _code_of(await _authorize(http, first))
        other_uri = await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": first,
            "redirect_uri": "http://pf.test/other", "code_verifier": VERIFIER,
        })
        assert other_uri.status_code == 400


async def test_refresh_rotates():
    async with _client(ref.build_app(base_url=BASE)) as http:
        client_id = await _register(http)
        tokens = await _tokens(http, client_id)
        data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
        fresh = await http.post("/token", data=data)
        assert fresh.status_code == 200
        body = fresh.json()
        assert body["refresh_token"] != tokens["refresh_token"]
        assert body["access_token"] != tokens["access_token"]
        old = await http.post("/token", data=data)
        assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
        assert (await http.post("/token", data={**data, "refresh_token": body["refresh_token"]})).status_code == 200


async def test_missing_unknown_and_expired_tokens_get_401_with_the_header():
    expected = f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource"'
    app = ref.build_app(base_url=BASE, access_token_seconds=0)
    async with _client(app) as http:
        client_id = await _register(http)
        expired = (await _tokens(http, client_id))["access_token"]
        for headers in ({}, {"Authorization": "Bearer unknown"}, {"Authorization": f"Bearer {expired}"}):
            resp = await http.post("/mcp", headers=headers, json={})
            assert resp.status_code == 401
            assert resp.headers["www-authenticate"] == expected


async def test_valid_token_lists_the_four_tools_with_annotations():
    app = ref.build_app(base_url=BASE)
    async with _client(app) as http:
        access = (await _tokens(http, await _register(http)))["access_token"]
    async with _mcp(app, access) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert set(tools) == {"whoami", "echo", "rename_thing", "wipe_thing"}
        assert tools["whoami"].annotations.read_only_hint is True
        assert tools["echo"].annotations.read_only_hint is True
        assert tools["rename_thing"].annotations is None
        assert tools["wipe_thing"].annotations.destructive_hint is True
        assert (await session.call_tool("echo", {"text": "hi"})).content[0].text == '"hi"'
        assert (await session.call_tool("rename_thing", {})).content[0].text == '"renamed"'
        assert (await session.call_tool("wipe_thing", {})).content[0].text == '"wiped"'


@pytest.mark.parametrize(
    ("hint", "expected"), [(None, "alice"), ("u-1", "Bob Mapped"), ("someone", "someone")],
)
async def test_whoami_follows_the_login_hint(hint, expected):
    app = ref.build_app(base_url=BASE, accounts={"u-1": "Bob Mapped"})
    async with _client(app) as http:
        access = (await _tokens(http, await _register(http), hint))["access_token"]
    async with _mcp(app, access) as session:
        assert _who(await session.call_tool("whoami", {})) == expected


async def test_the_account_survives_a_refresh():
    app = ref.build_app(base_url=BASE)
    async with _client(app) as http:
        client_id = await _register(http)
        tokens = await _tokens(http, client_id, "carol")
        fresh = (await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id,
        })).json()
    async with _mcp(app, fresh["access_token"]) as session:
        assert _who(await session.call_tool("whoami", {})) == "carol"


async def test_extra_tools_appear_in_the_next_list():
    app = ref.build_app(base_url=BASE)
    async with _client(app) as http:
        access = (await _tokens(http, await _register(http)))["access_token"]
    async with _mcp(app, access) as session:
        assert len((await session.list_tools()).tools) == 4
        app.state.extra_tools.append(types.Tool(
            name="extra", description="Extra.", input_schema={"type": "object", "properties": {}},
        ))
        names = [t.name for t in (await session.list_tools()).tools]
        assert names[-1] == "extra" and len(names) == 5
        app.state.extra_tools = []
        assert len((await session.list_tools()).tools) == 4


async def test_revoke_all_makes_a_valid_token_answer_401():
    app = ref.build_app(base_url=BASE)
    async with _client(app) as http:
        client_id = await _register(http)
        tokens = await _tokens(http, client_id)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        async with app.router.lifespan_context(app):
            assert (await http.post("/mcp", headers=headers, json={})).status_code != 401
            app.state.revoke_all()
            assert (await http.post("/mcp", headers=headers, json={})).status_code == 401
        refresh = await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id,
        })
        assert refresh.status_code == 400
        # Registered clients survive: a new sign-in works.
        assert (await _tokens(http, client_id))["access_token"]
