"""The OpenID Connect provider's HTTP routes under ``/oidc``.

Discovery and the key set need no sign-in: the web apps fetch them server-side. ``sessions`` is
the browser-session store the authorization route will read; the discovery and key routes do not
use it.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..oidc_provider import OidcProvider
from .org_session import OrgSessionStore

DISCOVERY_PATH = "/oidc/.well-known/openid-configuration"
JWKS_PATH = "/oidc/jwks"


def build_routes(provider: OidcProvider, *, sessions: OrgSessionStore) -> list[Route]:
    async def discovery(request: Request) -> Response:
        return JSONResponse(provider.discovery())

    async def jwks(request: Request) -> Response:
        return JSONResponse(provider.jwks())

    return [
        Route(DISCOVERY_PATH, discovery, methods=["GET"]),
        Route(JWKS_PATH, jwks, methods=["GET"]),
    ]
