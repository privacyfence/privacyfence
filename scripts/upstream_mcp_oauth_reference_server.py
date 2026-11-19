"""A reference upstream MCP server with OAuth, for tests and a development setup.

This is a test aid, not a production server: it approves every authorization request at once, keeps
everything in memory and has no real accounts. It plays the vendor's side of the OAuth flow
PrivacyFence uses to sign each person in to a hosted MCP server: protected resource metadata (RFC
9728), authorization server metadata (RFC 8414) with ``authorization_response_iss_parameter_supported``
(RFC 9207), dynamic client registration (RFC 7591), an authorization endpoint that checks S256 PKCE,
a token endpoint with refresh token rotation, and a Streamable HTTP ``/mcp`` endpoint behind a bearer
check. The account a sign-in belongs to comes from the optional ``login_hint`` query parameter of
``/authorize`` (a key of ``accounts`` maps to its name, any other hint is the account, no hint is
``alice``), so a test can sign in several people.

Run it::

    python3 scripts/upstream_mcp_oauth_reference_server.py --base-url http://127.0.0.1:8098 --port 8098
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import contextvars
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

DEFAULT_ACCOUNT = "alice"
SCOPE = "mcp:read"

# The account of the bearer token of the request being handled.
_account: contextvars.ContextVar[str] = contextvars.ContextVar("reference_account")


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    account: str


@dataclass
class _Access:
    client_id: str
    account: str
    expires_at: float


@dataclass
class _Refresh:
    client_id: str
    account: str


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii", "replace")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _text(payload: object) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))])


def _tool(
    name: str, description: str, props: list[str], annotations: types.ToolAnnotations | None = None,
) -> types.Tool:
    return types.Tool(
        name=name, description=description,
        input_schema={"type": "object", "properties": {p: {"type": "string"} for p in props}, "required": props},
        annotations=annotations,
    )


def _build_server(get_extra_tools: Callable[[], list[Any]]) -> MCPServer:
    base = [
        _tool("whoami", "The account this token belongs to.", [], types.ToolAnnotations(read_only_hint=True)),
        _tool("echo", "Return the text argument.", ["text"], types.ToolAnnotations(read_only_hint=True)),
        _tool("rename_thing", "Rename a thing.", []),
        _tool("wipe_thing", "Wipe a thing.", [], types.ToolAnnotations(destructive_hint=True)),
    ]

    async def list_tools(ctx: ServerRequestContext, _params: Any) -> types.ListToolsResult:
        # app.state.extra_tools is read on every call, so a test can change it between refreshes.
        return types.ListToolsResult(tools=[*base, *get_extra_tools()])

    async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        args = params.arguments or {}
        if params.name == "whoami":
            return _text({"account": _account.get("")})
        if params.name == "echo":
            return _text(str(args.get("text", "")))
        if params.name == "rename_thing":
            return _text("renamed")
        if params.name == "wipe_thing":
            return _text("wiped")
        if any(getattr(t, "name", None) == params.name for t in get_extra_tools()):
            return _text("ok")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool {params.name}")], is_error=True,
        )

    return MCPServer("oauth-reference", version="1", on_list_tools=list_tools, on_call_tool=call_tool)


def _oauth_error(error: str, description: str = "", status: int = 400) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def build_app(
    *, base_url: str, accounts: dict[str, str] | None = None, access_token_seconds: int = 3600,
) -> Starlette:
    base = base_url.rstrip("/")
    resource_metadata = f"{base}/.well-known/oauth-protected-resource"
    mapping = dict(accounts or {})

    # State lives in the app, never at module level.
    clients: dict[str, list[str]] = {}
    codes: dict[str, _Code] = {}
    access_tokens: dict[str, _Access] = {}
    refresh_tokens: dict[str, _Refresh] = {}

    def _issue(client_id: str, account: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_tokens[access] = _Access(client_id, account, time.time() + access_token_seconds)
        refresh_tokens[refresh] = _Refresh(client_id, account)
        return {
            "access_token": access, "token_type": "Bearer", "expires_in": access_token_seconds,
            "refresh_token": refresh, "scope": SCOPE,
        }

    async def protected_resource(_request: Request) -> Response:
        return JSONResponse({
            "resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": [SCOPE],
        })

    async def server_metadata(_request: Request) -> Response:
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        })

    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        uris = body.get("redirect_uris") if isinstance(body, dict) else None
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
            return _oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty list")
        client_id = f"client-{secrets.token_hex(8)}"
        clients[client_id] = list(uris)
        return JSONResponse(
            {
                "client_id": client_id, "redirect_uris": list(uris), "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            },
            status_code=201,
        )

    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        # Until the client and redirect URI are known good, an error is shown here, never redirected.
        if client_id not in clients or redirect_uri not in clients[client_id]:
            return _oauth_error("invalid_request", "unknown client or redirect_uri")
        if q.get("response_type") != "code":
            return _oauth_error("unsupported_response_type")
        challenge = q.get("code_challenge", "")
        if not challenge or q.get("code_challenge_method") != "S256":
            return _oauth_error("invalid_request", "S256 PKCE is required")
        hint = q.get("login_hint") or ""
        account = mapping.get(hint, hint) if hint else DEFAULT_ACCOUNT
        code = secrets.token_urlsafe(24)
        codes[code] = _Code(client_id, redirect_uri, challenge, account)
        params = {"code": code, "iss": base}
        if q.get("state") is not None:
            params["state"] = q["state"]
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    async def token(request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        client_id = str(form.get("client_id") or "")
        if client_id not in clients:
            return _oauth_error("invalid_client", status=401)
        if grant == "authorization_code":
            # Single use: the code is gone whether or not the rest checks out.
            record = codes.pop(str(form.get("code") or ""), None)
            verifier = str(form.get("code_verifier") or "")
            if (
                record is None or record.client_id != client_id
                or record.redirect_uri != str(form.get("redirect_uri") or "")
                or not verifier or not secrets.compare_digest(_s256(verifier), record.challenge)
            ):
                return _oauth_error("invalid_grant")
            return JSONResponse(_issue(client_id, record.account), headers={"Cache-Control": "no-store"})
        if grant == "refresh_token":
            old = refresh_tokens.pop(str(form.get("refresh_token") or ""), None)
            if old is None or old.client_id != client_id:
                return _oauth_error("invalid_grant")
            return JSONResponse(_issue(client_id, old.account), headers={"Cache-Control": "no-store"})
        return _oauth_error("unsupported_grant_type")

    server = _build_server(lambda: list(app.state.extra_tools))
    # Stateless: every request is authorized and handled on its own.
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    class _Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive)
            header = request.headers.get("authorization", "")
            scheme, _, value = header.partition(" ")
            record = access_tokens.get(value.strip()) if scheme.lower() == "bearer" else None
            if record is None or record.expires_at <= time.time():
                await Response(
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata}"'},
                )(scope, receive, send)
                return
            reset = _account.set(record.account)
            try:
                await manager.handle_request(scope, receive, send)
            finally:
                _account.reset(reset)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource", protected_resource),
            Route("/.well-known/oauth-authorization-server", server_metadata),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
            Route("/mcp", _Endpoint()),
        ],
        lifespan=lifespan,
    )

    def revoke_all() -> None:
        access_tokens.clear()
        refresh_tokens.clear()

    # A test sets or appends types.Tool entries here; the server reads it on every list.
    app.state.extra_tools = []
    app.state.revoke_all = revoke_all
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Reference upstream MCP server with OAuth (not for production).")
    parser.add_argument("--base-url", required=True, help="The public URL of this server, e.g. http://127.0.0.1:8098.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    import uvicorn

    app = build_app(base_url=args.base_url)
    print(f"listening on {args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
