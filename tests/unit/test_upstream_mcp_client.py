"""The upstream MCP client against the in-process reference app, and the tool catalog."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import socket
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession, types

from privacyfence.identity_claim import CLAIM_HEADER, ClaimSigner, IdentityCaller
from privacyfence.upstream_claim_keys import KeySet
from privacyfence.upstream_mcp_client import (
    CallResult,
    UpstreamMcpClient,
    UpstreamMcpClientError,
    UpstreamToolCatalog,
)
from privacyfence.upstream_mcp_config import DeclaredTool, UpstreamServer

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_reference_server_for_client",
    Path(__file__).parents[2] / "scripts" / "upstream_mcp_reference_server.py",
)
assert _SPEC and _SPEC.loader
ref = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ref
_SPEC.loader.exec_module(ref)

AUD = "https://kh.example/mcp"
ISSUER = "https://pf.example"
URL = "http://testserver/mcp"


def _declared(name: str, gate: str = "auto", verb: str = "read") -> DeclaredTool:
    return DeclaredTool(
        name=name, exposed_name=f"kh_{name}", operation=f"kh.{name}", gate=gate, verb=verb,
        effect="changes a document" if gate == "popup" else "",
    )


def _upstream(*tools: DeclaredTool, url: str = URL) -> UpstreamServer:
    return UpstreamServer(id="kh", label="KH", url=url, audience=AUD, tools=tuple(tools))


@pytest.fixture
def signer(tmp_path):
    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    return ClaimSigner(issuer=ISSUER, keys=keys)


def _caller(*audiences: str) -> IdentityCaller:
    return IdentityCaller(
        sub="u1", principal_type="claude_client", email="a@b.example", audiences=tuple(audiences),
        tools=("search",), act={"sub": "client-1"},
    )


@contextlib.asynccontextmanager
async def _client(signer, **app_kwargs):
    app = ref.build_app(audience=AUD, issuer=ISSUER, jwks=signer.jwks(), **app_kwargs)
    async with app.router.lifespan_context(app):
        yield UpstreamMcpClient(_upstream(), signer, transport=httpx2.ASGITransport(app=app))


async def test_list_tools_uses_the_service_claim(signer):
    async with _client(signer) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["search", "get_doc", "propose_change"]


async def test_call_tool_returns_text(signer):
    async with _client(signer) as client:
        result = await client.call_tool("search", {"query": "policy"}, _caller("general"))
    assert isinstance(result, CallResult)
    assert [d["title"] for d in json.loads(result.text)] == ["Policy overview", "Office policy"]
    assert result.structured is None
    assert result.dropped_blocks == 0


async def test_call_tool_returns_structured_content_and_counts_other_blocks(signer, monkeypatch):
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
    async with _client(signer) as client:
        result = await client.call_tool("search", {}, _caller())
    assert (result.text, result.structured, result.dropped_blocks) == ("one\ntwo", {"a": 1}, 1)


class _Spy(httpx2.AsyncBaseTransport):
    """Passes every request to the reference app and keeps the headers of each."""

    def __init__(self, app):
        self._inner = httpx2.ASGITransport(app=app)
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


@contextlib.asynccontextmanager
async def _spied(signer, upstream=None):
    app = ref.build_app(audience=AUD, issuer=ISSUER, jwks=signer.jwks())
    async with app.router.lifespan_context(app):
        spy = _Spy(app)
        yield UpstreamMcpClient(upstream or _upstream(), signer, transport=spy), spy


def _claim_of(request: httpx2.Request) -> dict:
    import jwt

    return jwt.decode(request.headers[CLAIM_HEADER], options={"verify_signature": False})


async def test_a_call_sends_the_claim_header_and_never_authorization(signer):
    async with _spied(signer) as (client, spy):
        result = await client.call_tool("search", {"query": "policy"}, _caller("general"))
    assert spy.requests
    assert all(CLAIM_HEADER in r.headers and "authorization" not in r.headers for r in spy.requests)
    call = [r for r in spy.requests if json.loads(r.content or b"{}").get("method") == "tools/call"]
    assert len(call) == 1
    claim = _claim_of(call[0])
    assert claim["audiences"] == ["general"] and claim["tools"] == ["search"] and claim["req"]
    assert claim["jti"] == result.jti and claim["req"] == result.req


async def test_requests_that_are_not_the_call_carry_no_audiences(signer):
    async with _spied(signer) as (client, spy):
        await client.call_tool("search", {"query": "policy"}, _caller("general"))
    others = [r for r in spy.requests if json.loads(r.content or b"{}").get("method") != "tools/call" and r.content]
    assert others
    for request in others:
        claim = _claim_of(request)
        assert claim["audiences"] == [] and claim["tools"] == [] and "req" not in claim


async def test_identity_claim_off_sends_no_header(signer):
    upstream = UpstreamServer(
        id="kh", label="KH", url=URL, audience=AUD, tools=(_declared("search"),), identity_claim="off",
    )
    async with _spied(signer, upstream) as (client, spy):
        # The reference server refuses unsigned calls, so only the transport is observed.
        with pytest.raises(UpstreamMcpClientError):
            await client.call_tool("search", {"query": "policy"}, None)
    assert spy.requests
    assert all(CLAIM_HEADER not in r.headers and "authorization" not in r.headers for r in spy.requests)


async def test_a_signing_failure_sends_nothing_and_is_claim_failed(signer, monkeypatch):
    def broken(*args, **kwargs):
        from privacyfence.upstream_claim_keys import KeySetError

        raise KeySetError("No upstream claim signing key is usable.")

    monkeypatch.setattr(KeySet, "sign", broken)
    async with _spied(signer) as (client, spy):
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("search", {"query": "policy"}, _caller("general"))
    assert caught.value.kind == "claim_failed"
    assert spy.requests == []


async def test_a_result_over_the_cap_is_too_large(signer):
    big = [{"id": "9", "domain": "general", "title": "Big", "body": "x" * 1_000_001}]
    async with _client(signer, documents=big) as client:
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("search", {"query": "big"}, _caller("general"))
    assert caught.value.kind == "too_large"
    assert caught.value.detail == ""


async def test_an_error_result_is_a_tool_error_with_detail(signer):
    async with _client(signer) as client:
        with pytest.raises(UpstreamMcpClientError) as caught:
            await client.call_tool("get_doc", {"id": "nope"}, _caller("general"))
    assert caught.value.kind == "tool_error"
    assert caught.value.detail == "not found"


async def test_an_unreachable_url_is_unreachable(signer):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = UpstreamMcpClient(_upstream(url=f"http://127.0.0.1:{port}/mcp"), signer)
    with pytest.raises(UpstreamMcpClientError) as caught:
        await client.call_tool("search", {}, _caller())
    assert caught.value.kind == "unreachable"
    assert caught.value.detail == ""


# ----- #
# ----- The catalog
# ----- #


def _tool(name: str, schema=None, description: str = "Does it.") -> types.Tool:
    if schema is None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    return types.Tool(name=name, description=description, input_schema=schema)


def _catalog(*declared: DeclaredTool) -> UpstreamToolCatalog:
    return UpstreamToolCatalog(_upstream(*declared))


def test_empty_catalog_has_no_specs_and_everything_missing():
    catalog = _catalog(_declared("search"))
    assert catalog.specs() == []
    assert catalog.missing() == ["kh_search"]


def test_spec_description_is_cleaned_cut_and_gets_the_returns_sentence():
    catalog = _catalog(_declared("search"))
    catalog.update([_tool("search", description="Line one\nline\x00 two" + "x" * 800)])
    (spec,) = catalog.specs()
    head, sep, tail = spec.description.partition(" Returns what the KH server answers.")
    assert sep
    assert head.startswith("Line oneline two")
    assert len(head) == 700
    assert "\n" not in spec.description and "\x00" not in spec.description
    assert tail == " Auto-approved: the KH server decides which data the caller may see."


@pytest.mark.parametrize(
    ("gate", "verb", "sentence"),
    [
        ("auto", "read", " Auto-approved: the KH server decides which data the caller may see."),
        ("review", "search", " Requires user approval: PrivacyFence shows the result to the person before it is returned."),
        ("popup", "update", " Requires user approval: PrivacyFence asks the person before the call is sent."),
    ],
)
def test_gate_sentence_per_gate(gate, verb, sentence):
    catalog = _catalog(_declared("t", gate, verb))
    catalog.update([_tool("t", description="")])
    (spec,) = catalog.specs()
    assert spec.description == " Returns what the KH server answers." + sentence


def test_reason_is_added_to_params_and_a_copy_of_the_schema():
    original = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    catalog = _catalog(_declared("search"))
    catalog.update([_tool("search", original)])
    (spec,) = catalog.specs()
    assert [(p.name, p.annotation, p.required) for p in spec.params] == [("reason", "str", True)]
    assert spec.params[0].description == "One sentence: why are you calling this tool right now?"
    assert set(spec.input_schema["properties"]) == {"q", "reason"}
    assert spec.input_schema["required"] == ["q", "reason"]
    assert original == {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def test_a_schema_without_properties_or_required_gets_both():
    catalog = _catalog(_declared("t"))
    catalog.update([_tool("t", {"type": "object"})])
    (spec,) = catalog.specs()
    assert list(spec.input_schema["properties"]) == ["reason"]
    assert spec.input_schema["required"] == ["reason"]


def test_read_only_and_destructive_follow_the_verb():
    catalog = _catalog(_declared("a", "auto", "read"), _declared("b", "popup", "delete"), _declared("c", "popup", "update"))
    catalog.update([_tool("a"), _tool("b"), _tool("c")])
    flags = {s.name: (s.read_only, s.destructive) for s in catalog.specs()}
    assert flags == {"kh_a": (True, False), "kh_b": (False, True), "kh_c": (False, False)}


def test_specs_keep_file_order_and_ignore_undeclared_tools():
    catalog = _catalog(_declared("b"), _declared("a"))
    catalog.update([_tool("a"), _tool("extra"), _tool("b")])
    assert [s.name for s in catalog.specs()] == ["kh_b", "kh_a"]
    assert catalog.missing() == []


def test_update_keeps_only_the_last_snapshot():
    catalog = _catalog(_declared("a"))
    catalog.update([_tool("a")])
    catalog.update([])
    assert catalog.specs() == []


NESTED_DEFS = {"type": "object", "properties": {"p": {"type": "object", "properties": {"q": {"$defs": {"x": {}}}}}}}


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"type": "string"}, "not an object schema"),
        ({"type": "object", "properties": []}, "not an object schema"),
        ({"type": "object", "required": "q"}, "not an object schema"),
        ({"type": "object", "properties": {"reason": {"type": "string"}}}, "already has a reason"),
        ({"type": "object", "properties": {"q": {"description": "x" * 40_000}}}, "larger than 32 KB"),
        ({"type": "object", "properties": {"q": {"$ref": "#/x"}}}, "$ref, $defs or definitions"),
        ({"type": "object", "definitions": {}}, "$ref, $defs or definitions"),
        ({"type": "object", "$defs": {}}, "$ref, $defs or definitions"),
        (NESTED_DEFS, "$ref, $defs or definitions"),
        ({"type": "object", "anyOf": [{"properties": {"a": {"$ref": "#"}}}]}, "$ref, $defs or definitions"),
    ],
)
def test_each_skip_reason_is_logged_and_the_tool_reported_missing(schema, reason, caplog):
    catalog = _catalog(_declared("t"), _declared("ok"))
    catalog.update([_tool("t", schema), _tool("ok")])
    with caplog.at_level(logging.WARNING, logger="privacyfence.upstream_mcp_client"):
        assert [s.name for s in catalog.specs()] == ["kh_ok"]
    assert catalog.missing() == ["kh_t"]
    assert any("kh_t" in r.getMessage() and reason in r.getMessage() for r in caplog.records)


def test_a_non_object_schema_value_is_skipped():
    tool = _tool("t")
    object.__setattr__(tool, "input_schema", None)
    catalog = _catalog(_declared("t"))
    catalog.update([tool])
    assert catalog.specs() == []
