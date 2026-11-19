"""The OAuth upstream MCP client against the in-process OAuth reference app, BearerAuth and the catalog."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp import ClientSession, types

from privacyfence.upstream_mcp_client import (
    BearerAuth,
    CallerNotAllowedError,
    CallResult,
    UpstreamMcpClient,
    UpstreamMcpClientError,
    UpstreamToolCatalog,
    person_gate_of,
)
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_oauth_tokens import UpstreamOAuthError
from privacyfence.upstream_tools import ToolEntry, SavedTool, classify, tool_digest

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_oauth_reference_server_for_client",
    Path(__file__).parents[2] / "scripts" / "upstream_mcp_oauth_reference_server.py",
)
assert _SPEC and _SPEC.loader
ref = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ref
_SPEC.loader.exec_module(ref)

BASE = "http://ref.test"
URL = f"{BASE}/mcp"
REDIRECT = "http://pf.test/upstream-mcp/ref/callback"
VERIFIER = "v" * 50


def _upstream(url: str = URL) -> UpstreamServer:
    return UpstreamServer(id="ref", label="Ref", url=url, oauth=UpstreamOAuthSettings())


async def _access_token(app) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        client_id = (await http.post("/register", json={"redirect_uris": [REDIRECT]})).json()["client_id"]
        resp = await http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
        tokens = (await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
        })).json()
    return tokens["access_token"]


def _method(request: httpx2.Request) -> str:
    try:
        return str(json.loads(request.content or b"{}").get("method", ""))
    except ValueError:
        return ""


class _Spy(httpx2.AsyncBaseTransport):
    """Passes every request to the reference app, records it, and lets a test answer some itself."""

    def __init__(self, app, intercept=None):
        self._inner = httpx2.ASGITransport(app=app)
        self._intercept = intercept
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        if self._intercept is not None:
            answer = self._intercept(request, self)
            if answer is not None:
                return answer
        return await self._inner.handle_async_request(request)

    def methods(self) -> list[str]:
        return [_method(r) for r in self.requests]


class _Bearer:
    def __init__(self, *tokens: str):
        self.tokens = list(tokens)
        self.calls: list[bool] = []

    async def __call__(self, force_refresh: bool) -> str:
        self.calls.append(force_refresh)
        return self.tokens[min(len(self.calls), len(self.tokens)) - 1]


@contextlib.asynccontextmanager
async def _served(intercept=None, **app_kwargs):
    app = ref.build_app(base_url=BASE, **app_kwargs)
    async with app.router.lifespan_context(app):
        token = await _access_token(app)
        spy = _Spy(app, intercept)
        yield UpstreamMcpClient(_upstream(), transport=spy), spy, token, app


def _answer(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, json={"error": "x"})


# ----- #
# ----- BearerAuth
# ----- #


def _drive(auth: BearerAuth, url: str, response: httpx2.Response) -> httpx2.Request:
    request = httpx2.Request("POST", url)
    flow = auth.auth_flow(request)
    sent = next(flow)
    with pytest.raises(StopIteration):
        flow.send(response)
    return sent


def test_bearer_auth_adds_the_header_only_for_its_origin():
    auth = BearerAuth("tok", "https://up.example")
    assert _drive(auth, "https://up.example/mcp", httpx2.Response(200)).headers["authorization"] == "Bearer tok"
    assert _drive(auth, "https://UP.example:443/x", httpx2.Response(200)).headers["authorization"] == "Bearer tok"
    assert "authorization" not in _drive(auth, "https://other.example/mcp", httpx2.Response(200)).headers
    assert "authorization" not in _drive(auth, "http://up.example/mcp", httpx2.Response(200)).headers


def test_bearer_auth_matches_an_origin_with_a_port_and_a_different_case():
    auth = BearerAuth("tok", "http://localhost:8080")
    assert _drive(auth, "http://LOCALHOST:8080/mcp", httpx2.Response(200)).headers["authorization"] == "Bearer tok"
    assert "authorization" not in _drive(auth, "http://localhost:9090/mcp", httpx2.Response(200)).headers


@pytest.mark.parametrize("header", ["WWW-Authenticate", "www-authenticate"])
def test_bearer_auth_records_a_403_that_asks_for_scope(header):
    auth = BearerAuth("tok", "https://up.example")
    _drive(auth, "https://up.example/mcp", httpx2.Response(403, headers={header: 'Bearer error="insufficient_scope"'}))
    assert auth.status == 403 and auth.insufficient_scope is True


def test_bearer_auth_keeps_the_first_error_status_and_ignores_a_plain_403():
    auth = BearerAuth("tok", "https://up.example")
    assert (auth.status, auth.insufficient_scope) == (None, False)
    _drive(auth, "https://up.example/mcp", httpx2.Response(200))
    assert auth.status == 200
    _drive(auth, "https://up.example/mcp", httpx2.Response(401))
    _drive(auth, "https://up.example/mcp", httpx2.Response(405))
    assert auth.status == 401
    _drive(auth, "https://up.example/mcp", httpx2.Response(403))
    assert auth.insufficient_scope is False


def test_the_error_types_carry_their_data():
    assert CallerNotAllowedError("n8n_workflow").principal_type == "n8n_workflow"
    error = UpstreamMcpClientError("tool_error", "text")
    assert (error.kind, error.detail, str(error)) == ("tool_error", "text", "tool_error")


# ----- #
# ----- The client
# ----- #


async def test_list_tools_and_call_send_the_bearer_on_every_request_and_no_claim():
    async with _served() as (client, spy, token, _app):
        bearer = _Bearer(token)
        tools = await client.list_tools(bearer=bearer)
        result = await client.call_tool("whoami", {}, bearer=bearer)
    assert {t.name for t in tools} >= {"whoami", "echo", "rename_thing", "wipe_thing"}
    assert isinstance(result, CallResult)
    assert json.loads(result.text)["account"] == "alice"
    assert result.structured is None and result.dropped_blocks == 0
    assert bearer.calls == [False, False]
    methods = spy.methods()
    assert "initialize" in methods and "tools/call" in methods and "tools/list" in methods
    assert all(r.headers["authorization"] == f"Bearer {token}" for r in spy.requests)
    assert all("x-privacyfence-claim" not in r.headers for r in spy.requests)
    assert all(not any("claim" in name.lower() for name in r.headers) for r in spy.requests)


async def test_the_bearer_is_also_sent_on_the_session_close():
    # A server that issues a session id makes the SDK send a DELETE when the session closes.
    class Sessioned(_Spy):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            if _method(request) == "initialize":
                response.headers["mcp-session-id"] = "abc"
            return response

    app = ref.build_app(base_url=BASE)
    async with app.router.lifespan_context(app):
        token = await _access_token(app)
        spy = Sessioned(app)
        await UpstreamMcpClient(_upstream(), transport=spy).list_tools(bearer=_Bearer(token))
    assert "DELETE" in [r.method for r in spy.requests]
    assert all(r.headers["authorization"] == f"Bearer {token}" for r in spy.requests)


async def test_a_call_with_arguments_returns_the_text():
    async with _served() as (client, _spy, token, _app):
        result = await client.call_tool("echo", {"text": "hello"}, bearer=_Bearer(token))
    assert "hello" in result.text


async def test_structured_content_and_other_blocks_are_counted(monkeypatch):
    async def fake(self, name, arguments=None, **kwargs):
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text="one"),
                types.ImageContent(type="image", data="aGk=", mime_type="image/png"),
                types.TextContent(type="text", text="two"),
            ],
            structured_content={"a": 1},
        )

    monkeypatch.setattr(ClientSession, "call_tool", fake)
    async with _served() as (client, _spy, token, _app):
        result = await client.call_tool("whoami", {}, bearer=_Bearer(token))
    assert (result.text, result.structured, result.dropped_blocks) == ("one\ntwo", {"a": 1}, 1)


async def test_a_non_object_structured_content_is_dropped(monkeypatch):
    async def fake(self, name, arguments=None, **kwargs):
        return types.CallToolResult(content=[types.TextContent(type="text", text="t")])

    monkeypatch.setattr(ClientSession, "call_tool", fake)
    async with _served() as (client, _spy, token, _app):
        result = await client.call_tool("whoami", {}, bearer=_Bearer(token))
    assert result.structured is None


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        ("not_connected", "not_connected"),
        ("auth_expired", "auth_expired"),
        ("insufficient_scope", "insufficient_scope"),
        ("unreachable", "unreachable"),
        ("invalid_metadata", "unreachable"),
        ("registration_failed", "unreachable"),
        ("not_configured", "unreachable"),
    ],
)
async def test_each_token_error_kind_maps_to_a_client_error_and_nothing_is_sent(raised, kind):
    async def bearer(_force: bool) -> str:
        raise UpstreamOAuthError(raised)

    async with _served() as (client, spy, _token, _app):
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=bearer)
    assert caught.value.kind == kind
    assert spy.requests == []


async def test_a_401_on_initialize_is_retried_once_with_a_refreshed_token():
    def intercept(request, spy):
        if _method(request) == "initialize" and spy.methods().count("initialize") == 1:
            return _answer(401, {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return None

    async with _served(intercept) as (client, spy, token, _app):
        bearer = _Bearer("stale", token)
        result = await client.call_tool("whoami", {}, bearer=bearer)
    assert json.loads(result.text)["account"] == "alice"
    assert bearer.calls == [False, True]
    assert spy.methods().count("initialize") == 2
    assert spy.methods().count("tools/call") == 1
    assert spy.requests[0].headers["authorization"] == "Bearer stale"
    assert spy.requests[-1].headers["authorization"] == f"Bearer {token}"


async def test_a_second_401_on_initialize_is_auth_expired():
    def intercept(request, spy):
        return _answer(401) if _method(request) == "initialize" else None

    async with _served(intercept) as (client, spy, _token, _app):
        bearer = _Bearer("a", "b")
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=bearer)
    assert caught.value.kind == "auth_expired"
    assert bearer.calls == [False, True]
    assert spy.methods().count("initialize") == 2
    assert "tools/call" not in spy.methods()


async def test_a_401_on_the_call_is_auth_expired_and_the_call_is_sent_once():
    def intercept(request, spy):
        return _answer(401) if _method(request) == "tools/call" else None

    async with _served(intercept) as (client, spy, token, _app):
        bearer = _Bearer(token)
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=bearer)
    assert caught.value.kind == "auth_expired"
    assert bearer.calls == [False]
    assert spy.methods().count("tools/call") == 1
    assert spy.methods().count("initialize") == 1


async def test_a_403_insufficient_scope_is_reported_and_not_retried():
    def intercept(request, spy):
        if _method(request) == "initialize":
            return _answer(403, {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="mcp:write"'})
        return None

    async with _served(intercept) as (client, spy, token, _app):
        bearer = _Bearer(token)
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=bearer)
    assert caught.value.kind == "insufficient_scope"
    assert bearer.calls == [False]
    assert spy.methods().count("initialize") == 1


async def test_a_revoked_token_is_auth_expired():
    async with _served() as (client, _spy, token, app):
        app.state.revoke_all()
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=_Bearer(token, token))
    assert caught.value.kind == "auth_expired"


async def test_a_server_error_is_unreachable_and_logs_no_body(caplog):
    def intercept(request, spy):
        return httpx2.Response(500, text="SECRET-BODY-TEXT") if _method(request) == "initialize" else None

    async with _served(intercept) as (client, _spy, token, _app):
        caplog.set_level(logging.DEBUG)
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.list_tools(bearer=_Bearer(token))
    assert caught.value.kind == "unreachable" and caught.value.detail == ""
    assert "SECRET-BODY-TEXT" not in "\n".join(r.getMessage() for r in caplog.records)
    assert token not in "\n".join(r.getMessage() for r in caplog.records)


async def test_an_unreachable_address_is_unreachable():
    class Refuse(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx2.ConnectError("no route")

    client = UpstreamMcpClient(_upstream(), transport=Refuse())
    with pytest.raises(UpstreamMcpClientError) as caught:
        await client.list_tools(bearer=_Bearer("t"))
    assert caught.value.kind == "unreachable"


async def test_a_result_over_the_cap_is_too_large():
    async def fake(self, name, arguments=None, **kwargs):
        return types.CallToolResult(content=[types.TextContent(type="text", text="x" * 1_000_001)])

    async with _served() as (client, _spy, token, _app):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ClientSession, "call_tool", fake)
            with pytest.raises(UpstreamMcpClientError) as caught:
                await client.call_tool("whoami", {}, bearer=_Bearer(token))
    assert caught.value.kind == "too_large" and caught.value.detail == ""


async def test_an_error_result_is_a_tool_error_with_detail(monkeypatch):
    async def fake(self, name, arguments=None, **kwargs):
        return types.CallToolResult(content=[types.TextContent(type="text", text="no such page")], is_error=True)

    monkeypatch.setattr(ClientSession, "call_tool", fake)
    async with _served() as (client, _spy, token, _app):
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("whoami", {}, bearer=_Bearer(token))
    assert (caught.value.kind, caught.value.detail) == ("tool_error", "no such page")


async def test_the_bearer_is_not_sent_to_another_origin():
    # A redirect to another host is not followed by the SDK, and the auth would not add the header there.
    def intercept(request, spy):
        return httpx2.Response(307, headers={"Location": "http://evil.test/mcp"}) if _method(request) == "initialize" else None

    async with _served(intercept) as (client, spy, token, _app):
        with pytest.raises(UpstreamMcpClientError):
            await client.list_tools(bearer=_Bearer(token))
    assert all(r.url.host == "ref.test" for r in spy.requests)


# ----- #
# ----- The catalog
# ----- #


def _saved(name: str, *, gate: str | None = None, read_only=None, destructive=None, description: str = "Does it.",
           schema=None) -> SavedTool:
    if schema is None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    entry = ToolEntry(name, description, schema, read_only, destructive)
    default, verb = classify(read_only, destructive)
    return SavedTool(name, description, schema, read_only, destructive, gate or default, verb, tool_digest(entry))


def test_an_empty_catalog_has_no_specs_and_knows_no_tool():
    catalog = UpstreamToolCatalog(_upstream())
    assert catalog.specs() == []
    assert catalog.saved_tool("ref_a") is None


def test_saved_tool_is_found_by_exposed_name_and_update_replaces_the_list():
    catalog = UpstreamToolCatalog(_upstream(), (_saved("a"),))
    assert catalog.saved_tool("ref_a").name == "a"
    assert catalog.saved_tool("a") is None
    catalog.update((_saved("b"),))
    assert catalog.saved_tool("ref_a") is None
    assert [s.name for s in catalog.specs()] == ["ref_b"]


def test_a_read_tool_spec():
    catalog = UpstreamToolCatalog(_upstream(), (_saved("look", read_only=True),))
    (spec,) = catalog.specs()
    assert spec.name == "ref_look"
    assert spec.read_only is True and spec.destructive is False
    assert spec.description == (
        "Does it. Returns what the Ref server answers."
        " Requires user approval: PrivacyFence shows the result to the person before it is returned."
    )
    assert [(p.name, p.annotation, p.required) for p in spec.params] == [("reason", "str", True)]
    assert set(spec.input_schema["properties"]) == {"q", "reason"}
    assert spec.input_schema["required"] == ["q", "reason"]


def test_a_write_tool_spec_and_a_destructive_one():
    catalog = UpstreamToolCatalog(_upstream(), (_saved("put", destructive=False), _saved("drop")))
    put, drop = catalog.specs()
    assert (put.read_only, put.destructive) == (False, False)
    assert (drop.read_only, drop.destructive) == (False, True)
    assert put.description.endswith(" Requires user approval: PrivacyFence asks the person before the call is sent.")


def test_the_gate_sentence_never_mentions_auto_and_follows_the_person_gate():
    tools = (_saved("r", gate="auto", read_only=True), _saved("w", gate="auto"), _saved("x", gate="review"))
    catalog = UpstreamToolCatalog(_upstream(), tools)
    r, w, x = catalog.specs()
    assert "shows the result" in r.description and "asks the person" in w.description
    assert "shows the result" in x.description
    assert not any("auto" in s.description.lower() for s in (r, w, x))
    assert [person_gate_of(t) for t in tools] == ["review", "popup", "review"]


def test_the_description_is_cleaned_and_cut():
    catalog = UpstreamToolCatalog(_upstream(), (_saved("t", description="Line one\nline\x00 two" + "x" * 800),))
    (spec,) = catalog.specs()
    head, sep, _tail = spec.description.partition(" Returns what the Ref server answers.")
    assert sep and head.startswith("Line oneline two") and len(head) == 700
    assert "\n" not in spec.description and "\x00" not in spec.description


def test_the_schema_is_copied_and_a_missing_properties_or_required_gets_both():
    original = {"type": "object"}
    catalog = UpstreamToolCatalog(_upstream(), (_saved("t", schema=original),))
    (spec,) = catalog.specs()
    assert list(spec.input_schema["properties"]) == ["reason"]
    assert spec.input_schema["required"] == ["reason"]
    assert original == {"type": "object"}
