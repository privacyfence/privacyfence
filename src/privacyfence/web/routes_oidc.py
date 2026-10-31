"""The OpenID Connect provider's HTTP routes under ``/oidc``.

Discovery and the key set need no sign-in: the web apps fetch them server-side. ``/oidc/authorize``
runs in the person's browser and reads the PrivacyFence browser session from ``sessions``; it never
shows a consent page, since only apps the operator named can use it, and PKCE S256 is mandatory.
``/oidc/token`` authenticates the app with its secret and swaps a code for an ID token and a
five-minute access token, which only ``/oidc/userinfo`` accepts. These tokens and the MCP
authorization server's tokens live in separate stores, so neither is accepted where the other is.

An unknown client or an unregistered redirect address is answered here, never redirected, so a
request can only ever send a browser back to an address the operator registered.
"""
from __future__ import annotations

import base64
import logging
import re
from urllib.parse import quote, unquote_plus, urlencode

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from ..oidc_provider import OidcProvider
from .org_session import SESSION_COOKIE, OrgSessionStore

logger = logging.getLogger(__name__)

DISCOVERY_PATH = "/oidc/.well-known/openid-configuration"
JWKS_PATH = "/oidc/jwks"
AUTHORIZE_PATH = "/oidc/authorize"
TOKEN_PATH = "/oidc/token"  # nosec B105  # a route path, not a credential
USERINFO_PATH = "/oidc/userinfo"

_CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_NO_STORE = {"Cache-Control": "no-store"}


def _redirect_with(redirect_uri: str, params: dict[str, str]) -> Response:
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302, headers=_NO_STORE)


def _error_redirect(redirect_uri: str, error: str, description: str, state: str | None) -> Response:
    params = {"error": error, "error_description": description}
    if state is not None:
        params["state"] = state
    return _redirect_with(redirect_uri, params)


def _json(body: dict[str, object], status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(body, status_code=status_code, headers={**_NO_STORE, **(headers or {})})


def _invalid_client() -> Response:
    return _json({"error": "invalid_client"}, 401, {"WWW-Authenticate": "Basic"})


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    """``(client_id, secret)`` from ``Authorization: Basic``, each part form-decoded the way
    openid-client encodes it; ``("", "")`` for a Basic header that cannot be read; None when there
    is no Basic header."""
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "")
    client_id, sep, secret = decoded.partition(":")
    if not sep:
        return ("", "")
    return unquote_plus(client_id), unquote_plus(secret)


def _form_text(value: str | UploadFile | None) -> str:
    return value if isinstance(value, str) else ""


def build_routes(provider: OidcProvider, *, sessions: OrgSessionStore) -> list[Route]:
    async def discovery(request: Request) -> Response:
        return JSONResponse(provider.discovery())

    async def jwks(request: Request) -> Response:
        return JSONResponse(provider.jwks())

    async def authorize(request: Request) -> Response:
        query = request.query_params
        client = provider.client(query.get("client_id", ""))
        if client is None:
            return PlainTextResponse("Unknown client.", status_code=400)
        redirect_uri = query.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:
            return PlainTextResponse("This redirect address is not registered for this client.", status_code=400)
        state = query.get("state")

        if query.get("response_type") != "code":
            return _error_redirect(
                redirect_uri, "unsupported_response_type", "Only response_type=code is supported.", state
            )
        scopes = list(dict.fromkeys(query.get("scope", "").split()))
        if "openid" not in scopes:
            return _error_redirect(redirect_uri, "invalid_scope", "The openid scope is required.", state)
        code_challenge = query.get("code_challenge", "")
        if query.get("code_challenge_method") != "S256" or not _CODE_CHALLENGE_RE.fullmatch(code_challenge):
            return _error_redirect(redirect_uri, "invalid_request", "PKCE with S256 is required.", state)

        session_id = request.cookies.get(SESSION_COOKIE, "")
        session = sessions.get_session(session_id) if session_id else None
        if session is None:
            target = f"{request.url.path}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=302, headers=_NO_STORE)
        principal = session.principal
        if not principal.email:
            return _error_redirect(redirect_uri, "access_denied", "This account has no email address.", state)

        code = provider.issue_code(
            client_id=client.client_id, redirect_uri=redirect_uri, principal=principal,
            email_verified=session.email_verified, nonce=query.get("nonce") or None,
            code_challenge=code_challenge, scope=" ".join(scopes), auth_time=int(session.created_at),
        )
        if code is None:
            return _error_redirect(
                redirect_uri, "temporarily_unavailable", "Too many sign-ins are in progress; try again.", state
            )
        params = {"code": code}
        if state is not None:
            params["state"] = state
        return _redirect_with(redirect_uri, params)

    async def token(request: Request) -> Response:
        form = await request.form()
        basic = _basic_credentials(request)
        if basic is not None:
            if "client_secret" in form:
                return _json({"error": "invalid_request"}, 400)
            client_id, secret = basic
        else:
            client_id = _form_text(form.get("client_id"))
            secret = _form_text(form.get("client_secret"))
        client = provider.authenticate(client_id, secret) if client_id and secret else None
        if client is None:
            return _invalid_client()

        if form.get("grant_type") != "authorization_code":
            return _json({"error": "unsupported_grant_type"}, 400)
        grant = provider.redeem_code(
            _form_text(form.get("code")),
            client_id=client.client_id,
            redirect_uri=_form_text(form.get("redirect_uri")),
            code_verifier=_form_text(form.get("code_verifier")),
        )
        if grant is None:
            return _json({"error": "invalid_grant"}, 400)
        body = provider.issue_tokens(grant)
        logger.info("OIDC sign-in to %s for principal %s", client.client_id, grant.principal.id)
        return _json(body)

    async def userinfo(request: Request) -> Response:
        scheme, _, access_token = request.headers.get("authorization", "").partition(" ")
        claims = provider.userinfo(access_token.strip()) if scheme.lower() == "bearer" else None
        if claims is None:
            return _json({"error": "invalid_token"}, 401, {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return _json(claims)

    return [
        Route(DISCOVERY_PATH, discovery, methods=["GET"]),
        Route(JWKS_PATH, jwks, methods=["GET"]),
        Route(AUTHORIZE_PATH, authorize, methods=["GET"]),
        Route(TOKEN_PATH, token, methods=["POST"]),
        Route(USERINFO_PATH, userinfo, methods=["GET", "POST"]),
    ]
