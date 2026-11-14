"""A reference upstream MCP server for PrivacyFence (ADR 0279).

This is a reference for upstream implementers and for tests, not a production server. It shows what
an upstream has to do with the signed caller claim PrivacyFence sends on every HTTP request:
verify it (RS256 only, ``typ`` ``pf-upstream-claim+jwt``, ``aud``, ``iss``, ``exp``/``nbf`` with no
leeway, a ``jti`` never seen before) and filter what it returns by the claim's ``domains``. Only
``sub``, ``domains`` and ``act.type`` may change what a query returns; a service claim (the startup
``tools/list``) lists the tools and gets no documents.

Run it::

    python3 scripts/upstream_mcp_reference_server.py --audience https://kh.example/mcp \\
        --issuer https://pf.example/upstream-mcp --jwks-url https://pf.example/upstream-mcp/jwks
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import jwt
from jwt.algorithms import RSAAlgorithm
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

CLAIM_TYPE = "pf-upstream-claim+jwt"

DEFAULT_DOCUMENTS: list[dict[str, str]] = [
    {"id": "1", "domain": "general", "title": "Policy overview", "body": "The company policy in brief."},
    {"id": "2", "domain": "general", "title": "Office policy", "body": "Office hours policy."},
    {"id": "3", "domain": "hr", "title": "HR policy", "body": "Leave and conduct policy."},
    {"id": "4", "domain": "finance", "title": "Finance policy", "body": "Expense policy."},
]

# The verified claim of the request being handled.
_claims: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("upstream_claims")


class _Verifier:
    def __init__(
        self, *, audience: str, issuer: str | None, jwks: dict | None, jwks_url: str | None,
        public_key_pem: bytes | None,
    ) -> None:
        if sum(x is not None for x in (jwks, jwks_url, public_key_pem)) != 1:
            raise ValueError("give exactly one of jwks, jwks_url, public_key_pem")
        self._audience = audience
        self._issuer = issuer
        self._jwks = jwks
        self._public_key_pem = public_key_pem
        self._jwk_client = jwt.PyJWKClient(jwks_url) if jwks_url else None
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _key(self, token: str) -> Any:
        if self._public_key_pem is not None:
            return self._public_key_pem
        if self._jwk_client is not None:
            return self._jwk_client.get_signing_key_from_jwt(token).key
        assert self._jwks is not None
        kid = jwt.get_unverified_header(token).get("kid")
        for jwk in self._jwks["keys"]:
            if jwk.get("kid") == kid:
                return RSAAlgorithm.from_jwk(json.dumps(jwk))
        raise jwt.InvalidTokenError("unknown kid")

    def verify(self, token: str) -> dict[str, Any]:
        if jwt.get_unverified_header(token).get("typ") != CLAIM_TYPE:
            raise jwt.InvalidTokenError("wrong typ")
        options: dict[str, Any] = {"require": ["exp", "nbf", "aud", "sub", "jti"]}
        claims = jwt.decode(
            token, self._key(token), algorithms=["RS256"], audience=self._audience,
            issuer=self._issuer, leeway=0, options=options,
        )
        now = time.time()
        with self._lock:
            self._seen = {j: exp for j, exp in self._seen.items() if exp > now}
            if claims["jti"] in self._seen:
                raise jwt.InvalidTokenError("replayed jti")
            self._seen[claims["jti"]] = float(claims["exp"])
        return claims


def _text(payload: object, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], is_error=error)


def _build_server(documents: list[dict]) -> MCPServer:
    def allowed() -> list[dict]:
        claims = _claims.get()
        domains = set(claims.get("domains") or [])
        if (claims.get("act") or {}).get("type") == "service":
            return []
        return [d for d in documents if d["domain"] in domains]

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
    *, audience: str, issuer: str | None, jwks: dict | None = None, jwks_url: str | None = None,
    public_key_pem: bytes | None = None, documents: list[dict] | None = None,
) -> Starlette:
    verifier = _Verifier(
        audience=audience, issuer=issuer, jwks=jwks, jwks_url=jwks_url, public_key_pem=public_key_pem,
    )
    server = _build_server(list(DEFAULT_DOCUMENTS if documents is None else documents))
    # Stateless: every request is verified and handled on its own, so its claim is the one in force.
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    class _Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive)
            header = request.headers.get("authorization", "")
            try:
                scheme, _, token = header.partition(" ")
                if scheme.lower() != "bearer" or not token:
                    raise jwt.InvalidTokenError("no bearer token")
                claims = verifier.verify(token.strip())
            except Exception:  # noqa: BLE001 -- every failure is the same 401, with no detail
                await JSONResponse({"error": "invalid_claim"}, status_code=401)(scope, receive, send)
                return
            reset = _claims.set(claims)
            try:
                await manager.handle_request(scope, receive, send)
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
    parser.add_argument("--issuer")
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument("--jwks-url")
    key.add_argument("--jwks-file")
    key.add_argument("--public-key-file")
    args = parser.parse_args()

    jwks = public_key_pem = None
    if args.jwks_file:
        with open(args.jwks_file, encoding="utf-8") as handle:
            jwks = json.load(handle)
    if args.public_key_file:
        with open(args.public_key_file, "rb") as handle:
            public_key_pem = handle.read()
    app = build_app(
        audience=args.audience, issuer=args.issuer, jwks=jwks, jwks_url=args.jwks_url,
        public_key_pem=public_key_pem,
    )
    import uvicorn

    print(f"listening on {args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
