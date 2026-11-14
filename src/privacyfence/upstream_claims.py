"""The signed caller claim PrivacyFence sends to an upstream MCP server (ADR 0279).

Every HTTP request to an upstream carries ``Authorization: Bearer <jwt>``: an RS256 token, signed
with its own key (not the OIDC one), naming who the call is for (``sub``, ``email``, ``domains``)
and what made it (``act``). It lives sixty seconds and each request gets a fresh one, so a revoked
grant stops working within a minute. The key set is published at ``/upstream-mcp/jwks``.

Only ``sub``, ``domains`` and ``act.type`` may change what an upstream returns; ``name``,
``claimed_name`` and ``label`` are for its audit only (ADR 0006 decision 3).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import httpx2
import jwt

from . import agent_identity, oidc_provider, paths, scoped_tokens
from .upstream_mcp_config import UpstreamServer

CLAIM_TYPE = "pf-upstream-claim+jwt"
CLAIM_LIFETIME_SECONDS = 60
KEY_FILENAME = "upstream_claim_signing_key.pem"
SERVICE_SUB = "privacyfence"


@dataclass(frozen=True)
class CallerClaim:
    sub: str
    email: str
    email_verified: bool
    domains: tuple[str, ...]
    act: dict[str, object]


class ClaimSigner:
    def __init__(self, *, issuer: str, key: oidc_provider.SigningKey) -> None:
        self._issuer = issuer
        self._key = key

    def jwks(self) -> dict[str, object]:
        return oidc_provider.jwks(self._key)

    def mint(self, *, upstream: UpstreamServer, caller: CallerClaim, tool: str | None, now: float | None = None) -> str:
        iat = int(time.time() if now is None else now)
        payload: dict[str, object] = {
            "iss": self._issuer,
            "aud": upstream.audience,
            "sub": caller.sub,
            "email": caller.email,
            "email_verified": caller.email_verified,
            "domains": sorted(caller.domains),
            "act": caller.act,
            "pf_upstream": upstream.id,
            "pf_tool": tool,
            "iat": iat,
            "nbf": iat,
            "exp": iat + CLAIM_LIFETIME_SECONDS,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(
            payload, self._key.private_key, algorithm="RS256", headers={"kid": self._key.kid, "typ": CLAIM_TYPE}
        )


def load_signer(issuer_url: str) -> ClaimSigner:
    key = oidc_provider.load_or_create_signing_key(paths.org_dir() / KEY_FILENAME)
    return ClaimSigner(issuer=issuer_url.rstrip("/") + "/upstream-mcp", key=key)


def actor_from_context() -> dict[str, object]:
    """The ``act`` claim for the call being dispatched: a scoped token first, else the AI client."""
    scope = scoped_tokens.current_token_scope()
    if scope is not None:
        return {"type": "workflow", "token_id": scope.id, "label": scope.label}
    agent = agent_identity.current_agent()
    attested = agent.is_attested()
    return {
        "type": "ai_client",
        "attested": attested,
        "name": agent.name if attested else "",
        "claimed_name": "" if attested else agent.name,
    }


def service_caller() -> CallerClaim:
    return CallerClaim(sub=SERVICE_SUB, email="", email_verified=False, domains=(), act={"type": "service"})


class ClaimAuth(httpx2.Auth):
    """Puts a freshly minted claim on every request (its own ``jti`` each time)."""

    def __init__(self, signer: ClaimSigner, upstream: UpstreamServer, caller: CallerClaim, tool: str | None) -> None:
        self._signer = signer
        self._upstream = upstream
        self._caller = caller
        self._tool = tool

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        token = self._signer.mint(upstream=self._upstream, caller=self._caller, tool=self._tool)
        request.headers["Authorization"] = "Bearer " + token
        yield request
