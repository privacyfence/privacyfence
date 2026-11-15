"""A reference upstream MCP server for PrivacyFence (ADR 0286).

This is a reference for upstream implementers and for tests, not a production server. It shows what
an upstream has to do with the signed identity claim PrivacyFence sends on every HTTP request, in the
``X-PrivacyFence-Claim`` header (``docs/identity-claim.md`` is the contract): verify it (EdDSA only,
``typ`` ``pf-identity-claim+jwt``, ``aud``, ``iss``, ``exp``, a ``jti`` never seen before, and for a
tool call the ``req`` hash of the exact call) and filter what it returns by the claim's ``audiences``.
Only ``sub``, ``audiences`` and ``principal_type`` may change what a query returns; a service claim
(the startup ``tools/list``) lists the tools and gets no documents. The verifier is the vendorable
``contrib/verifier/pf_claim_verify.py``.

Run it::

    python3 scripts/upstream_mcp_reference_server.py --audience https://kh.example/mcp \\
        --issuer https://pf.example --jwks-url https://pf.example/upstream-mcp/jwks
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import importlib.util
import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

CLAIM_HEADER = "x-privacyfence-claim"
MAX_BODY_BYTES = 1024 * 1024


def _load_pf_claim_verify() -> Any:
    """Load the vendorable verifier by path, as an upstream that vendors the file would."""
    path = Path(__file__).resolve().parents[1] / "contrib" / "verifier" / "pf_claim_verify.py"
    spec = importlib.util.spec_from_file_location("pf_claim_verify", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


pf_claim_verify = _load_pf_claim_verify()

DEFAULT_DOCUMENTS: list[dict[str, str]] = [
    {"id": "1", "audience": "general", "title": "Policy overview", "body": "The company policy in brief."},
    {"id": "2", "audience": "general", "title": "Office policy", "body": "Office hours policy."},
    {"id": "3", "audience": "hr", "title": "HR policy", "body": "Leave and conduct policy."},
    {"id": "4", "audience": "finance", "title": "Finance policy", "body": "Expense policy."},
]

# The verified claim of the request being handled.
_claims: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("reference_claims")


def _text(payload: object, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], is_error=error)


def _build_server(documents: list[dict]) -> MCPServer:
    def allowed() -> list[dict]:
        claims = _claims.get()
        audiences = set(claims.get("audiences") or [])
        if claims.get("principal_type") == "service":
            return []
        return [d for d in documents if d["audience"] in audiences]

    async def list_tools(ctx: ServerRequestContext, _params: Any) -> types.ListToolsResult:
        def tool(name: str, description: str, props: list[str]) -> types.Tool:
            return types.Tool(
                name=name, description=description,
                input_schema={
                    "type": "object", "properties": {p: {"type": "string"} for p in props},
                    "required": props,
                },
            )

        return types.ListToolsResult(tools=[
            tool("search", "Search documents.", ["query"]),
            tool("get_doc", "Fetch one document.", ["id"]),
            tool("propose_change", "Propose a change to a document.", ["id", "text"]),
        ])

    async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        args = params.arguments or {}
        if params.name == "search":
            query = str(args.get("query", "")).lower()
            hits = [d for d in allowed() if query in d["title"].lower() or query in d["body"].lower()]
            return _text(hits)
        if params.name == "get_doc":
            for doc in allowed():
                if doc["id"] == str(args.get("id", "")):
                    return _text(doc)
            return types.CallToolResult(content=[types.TextContent(type="text", text="not found")], is_error=True)
        if params.name == "propose_change":
            return _text({"proposal": "accepted", "by": _claims.get()["sub"]})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool {params.name}")], is_error=True,
        )

    return MCPServer("upstream-reference", version="1", on_list_tools=list_tools, on_call_tool=call_tool)


def build_app(
    *, audience: str, issuer: str, jwks: dict | None = None, jwks_url: str | None = None,
    documents: list[dict] | None = None, clock: Callable[[], float] | None = None,
) -> Starlette:
    # The issuer is PrivacyFence's issuer URL, with no suffix.
    verifier = pf_claim_verify.ClaimVerifier(
        issuer=issuer, audience=audience, jwks=jwks, jwks_url=jwks_url, **({"clock": clock} if clock else {}),
    )
    server = _build_server(list(DEFAULT_DOCUMENTS if documents is None else documents))
    # Stateless: every request is verified and handled on its own, so its claim is the one in force.
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    class _Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive)
            token = request.headers.get(CLAIM_HEADER)
            if token is None:
                await JSONResponse({"error": "invalid_claim"}, status_code=401)(scope, receive, send)
                return
            await self._handle(scope, receive, send, token)

        async def _handle(self, scope: Scope, receive: Receive, send: Send, token: str) -> None:
            body = b""
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                body += message.get("body", b"")
                if len(body) > MAX_BODY_BYTES:
                    await PlainTextResponse("request too large", status_code=413)(scope, receive, send)
                    return
                more = message.get("more_body", False)
            try:
                principal = verifier.verify(token.strip(), body=body)
            except Exception:  # noqa: BLE001 -- every failure is the same 401, with no detail
                await JSONResponse({"error": "invalid_claim"}, status_code=401)(scope, receive, send)
                return
            claims = {
                "sub": principal.sub,
                "audiences": list(principal.audiences),
                "principal_type": principal.principal_type,
            }
            sent = False

            async def replay() -> Any:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            reset = _claims.set(claims)
            try:
                await manager.handle_request(scope, replay, send)
            finally:
                _claims.reset(reset)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", _Endpoint())], lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reference upstream MCP server (not for production).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--issuer", required=True, help="The PrivacyFence issuer URL, with no suffix.")
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument("--jwks-url", help="The JWKS URL (Ed25519 keys), <issuer>/upstream-mcp/jwks.")
    key.add_argument("--jwks-file", help="A JWKS file (Ed25519 keys).")
    args = parser.parse_args()

    jwks = None
    if args.jwks_file:
        with open(args.jwks_file, encoding="utf-8") as handle:
            jwks = json.load(handle)
    app = build_app(audience=args.audience, issuer=args.issuer, jwks=jwks, jwks_url=args.jwks_url)
    import uvicorn

    print(f"listening on {args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
