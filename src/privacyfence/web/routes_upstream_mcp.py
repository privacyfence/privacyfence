"""The key set for the claims PrivacyFence signs onto upstream MCP requests (ADR 0279).

``/upstream-mcp/jwks`` needs no sign-in: an upstream fetches it server-side to verify a claim. The
key is separate from the OIDC provider's, so a claim can never be confused with an ID token.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..upstream_claims import ClaimSigner

JWKS_PATH = "/upstream-mcp/jwks"


def build_routes(signer: ClaimSigner) -> list[Route]:
    async def jwks(_request: Request) -> Response:
        return JSONResponse(signer.jwks(), headers={"Cache-Control": "max-age=300"})

    return [Route(JWKS_PATH, jwks, methods=["GET"])]
