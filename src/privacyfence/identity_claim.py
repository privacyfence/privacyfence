"""The v1 identity claim PrivacyFence sends to an upstream MCP server (plan 3.1 to 3.3).

The claim is an EdDSA JWT in its own header, ``X-PrivacyFence-Claim``; ``Authorization`` is never set
or read. It names the person (or the workflow token), the audiences they hold and, for a
``tools/call`` request, the exact call: ``req`` is the base64url SHA-256 of the canonical JSON of
``{"tool", "arguments"}``. A request that is not a tool call carries no audiences and no tools.

``docs/identity-claim.md`` is the contract; ``contrib/verifier/req_test_vectors.json`` holds the
vectors for ``req``. This module is not wired into the connector yet and leaves the shipped
``upstream_claims`` module alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2

from . import paths, upstream_claim_keys
from .upstream_claim_keys import KeySet, KeySetError

CLAIM_VERSION = 1
CLAIM_TYPE = "pf-identity-claim+jwt"
CLAIM_HEADER = "X-PrivacyFence-Claim"
MIN_LIFETIME_SECONDS = 5
MAX_LIFETIME_SECONDS = 60


class CallerNotAllowedError(Exception):
    """The caller's principal type is not allowed to use this upstream."""


class ClaimSigningError(Exception):
    """The claim could not be signed; nothing may be sent."""


def canonical_request(tool: str, arguments: Mapping[str, Any]) -> bytes:
    """The bytes ``req`` hashes: sorted keys, no whitespace, UTF-8 (not escaped), no NaN."""
    return json.dumps(
        {"tool": tool, "arguments": arguments},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def request_hash(tool: str, arguments: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_request(tool, arguments)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class IdentityCaller:
    sub: str
    principal_type: str
    email: str | None = None
    audiences: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    approved_by: str | None = None
    token_id: str | None = None
    act: Mapping[str, str] | None = None


@dataclass(frozen=True)
class MintedClaim:
    token: str = field(repr=False)
    jti: str
    req: str | None


class ClaimSigner:
    def __init__(self, *, issuer: str, keys: KeySet, clock: Callable[[], float] = time.time) -> None:
        self._issuer = issuer
        self._keys = keys
        self._clock = clock

    def jwks(self) -> dict[str, Any]:
        return self._keys.jwks()

    def mint(
        self, *, audience: str, caller: IdentityCaller, req: str | None, lifetime: int, now: float | None = None,
    ) -> MintedClaim:
        if not MIN_LIFETIME_SECONDS <= lifetime <= MAX_LIFETIME_SECONDS:
            raise ValueError(f"lifetime must be {MIN_LIFETIME_SECONDS} to {MAX_LIFETIME_SECONDS} seconds")
        moment = self._clock() if now is None else now
        iat = int(moment)
        jti = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "claim_version": CLAIM_VERSION,
            "iss": self._issuer,
            "aud": audience,
            "sub": caller.sub,
            "principal_type": caller.principal_type,
            "audiences": sorted(caller.audiences),
            "tools": sorted(caller.tools),
            "iat": iat,
            "exp": iat + lifetime,
            "jti": jti,
        }
        if caller.email:
            payload["email"] = caller.email
        if caller.approved_by:
            payload["approved_by"] = caller.approved_by
        if caller.token_id:
            payload["token_id"] = caller.token_id
        if caller.act:
            payload["act"] = dict(caller.act)
        if req:
            payload["req"] = req
        try:
            token = self._keys.sign(payload, moment)
        except KeySetError as exc:
            raise ClaimSigningError("The caller claim could not be signed.") from exc
        except Exception as exc:  # any failure inside the signing library fails closed
            raise ClaimSigningError("The caller claim could not be signed.") from exc
        return MintedClaim(token=token, jti=jti, req=req or None)


class IdentityClaimAuth(httpx2.Auth):
    """Puts a freshly minted claim on every request, bound to the call it carries."""

    requires_request_body = True

    def __init__(self, signer: ClaimSigner, audience: str, caller: IdentityCaller, lifetime: int) -> None:
        self._signer = signer
        self._audience = audience
        self._caller = caller
        self._lifetime = lifetime
        self.last_call_jti: str | None = None
        self.last_call_req: str | None = None
        self.minted_count = 0

    @staticmethod
    def _call_of(content: bytes) -> tuple[str, dict[str, Any]] | None:
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(body, dict) or body.get("method") != "tools/call":
            return None
        params = body.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return None
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return None
        return params["name"], arguments

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        call = self._call_of(request.content)
        if call is not None:
            req: str | None = request_hash(call[0], call[1])
            caller = self._caller
        else:
            req = None
            caller = IdentityCaller(
                sub=self._caller.sub, principal_type=self._caller.principal_type, email=self._caller.email,
                audiences=(), tools=(), approved_by=self._caller.approved_by,
                token_id=self._caller.token_id, act=self._caller.act,
            )
        minted = self._signer.mint(audience=self._audience, caller=caller, req=req, lifetime=self._lifetime)
        self.minted_count += 1
        if call is not None:
            self.last_call_jti = minted.jti
            self.last_call_req = minted.req
        request.headers[CLAIM_HEADER] = minted.token
        yield request


def load_signer(issuer_url: str, *, overlap_seconds: int) -> ClaimSigner:
    keys = KeySet(paths.org_dir() / upstream_claim_keys.FILENAME, overlap_seconds=overlap_seconds)
    keys.ensure()
    return ClaimSigner(issuer=issuer_url.rstrip("/"), keys=keys)
