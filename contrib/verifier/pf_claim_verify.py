"""Verify the PrivacyFence identity claim in one vendorable file.

Copy this file into an upstream MCP server. It needs only the standard library, ``PyJWT`` and
``cryptography``, and its syntax is valid on Python 3.9. ``docs/identity-claim.md`` in the
PrivacyFence repository is the contract this file implements (claim version 1).

PrivacyFence sends the claim in the ``X-PrivacyFence-Claim`` header of every HTTP request it
forwards. A claim for a ``tools/call`` request is bound to that exact call by ``req``; a claim for
any other request (initialize, initialized, delete) carries no audiences. Pass the raw request body
so both rules are enforced::

    from pf_claim_verify import CLAIM_HEADER, ClaimError, ClaimVerifier

    verifier = ClaimVerifier(
        issuer="https://pf.example.org",
        audience="https://hub.example.org/mcp",
        jwks_url="https://pf.example.org/upstream-mcp/jwks",
    )

    def authenticate(headers, body: bytes):
        try:
            principal = verifier.verify(headers[CLAIM_HEADER], body=body)
        except ClaimError as exc:
            raise PermissionError(exc.code)  # answer 401 and log the code, never the claim
        return principal  # principal.audiences is () unless body is a tools/call

Dispatch only the tool name and arguments the claim was verified against. The claim does not cover
``params._meta``. A replayed ``jti`` is refused by default; plug a shared store in through
``replay_cache`` (an object with ``add_if_new(jti, expires_at) -> bool``) when you run several
processes, or pass ``replay_cache=None`` to switch the check off.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CLAIM_VERSION = 1
CLAIM_TYPE = "pf-identity-claim+jwt"
CLAIM_HEADER = "X-PrivacyFence-Claim"
MAX_LIFETIME_SECONDS = 60
MAX_LEEWAY_SECONDS = 5
MIN_REFETCH_SECONDS = 30
_PRINCIPAL_TYPES = frozenset({"claude_client", "n8n_workflow", "service", "human"})
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "jti"]


class ClaimError(Exception):
    """The claim is not acceptable. ``code`` is one of the stable strings below."""

    CODES = (
        "malformed", "bad_algorithm", "bad_type", "unknown_key", "bad_signature", "bad_issuer",
        "bad_audience", "expired", "not_yet_valid", "bad_lifetime", "bad_version",
        "bad_principal_type", "bad_body", "batch_not_allowed", "request_required",
        "request_mismatch", "request_unexpected", "replayed",
    )

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def canonical_request(tool: str, arguments: Mapping[str, Any]) -> bytes:
    """The bytes ``req`` hashes: sorted keys, no whitespace, UTF-8 (not escaped), no NaN."""
    return json.dumps(
        {"tool": tool, "arguments": arguments},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def request_hash(tool: str, arguments: Mapping[str, Any]) -> str:
    """``req``: base64url without padding of SHA-256 over :func:`canonical_request`."""
    digest = hashlib.sha256(canonical_request(tool, arguments)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Principal:
    sub: str
    principal_type: str
    email: Optional[str]
    audiences: Tuple[str, ...]
    tools: Tuple[str, ...]
    approved_by: Optional[str]
    token_id: Optional[str]
    act: Optional[Dict[str, str]]
    jti: str
    expires_at: float
    claim_version: int


class InMemoryReplayCache:
    """A thread-safe, per-process ``jti`` cache. Use a shared store across processes."""

    def __init__(self, *, clock: Callable[[], float] = time.time, max_entries: int = 100000) -> None:
        self._clock = clock
        self._max = max_entries
        self._seen = {}  # type: Dict[str, float]
        self._lock = threading.Lock()

    def add_if_new(self, jti: str, expires_at: float) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._seen) >= self._max:
                self._seen = {k: v for k, v in self._seen.items() if v > now}
                if len(self._seen) >= self._max:
                    # Full of live entries: fail closed rather than forget one.
                    return False
            known = self._seen.get(jti)
            if known is not None and known > now:
                return False
            self._seen[jti] = expires_at
            return True


_DEFAULT_REPLAY = object()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _default_fetch(url: str) -> bytes:
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("jwks_url must be an http(s) URL")
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310  # nosec B310 - scheme checked above
        return response.read()  # type: ignore[no-any-return]


def _reject_duplicates(pairs: Any) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ClaimError("bad_body", "duplicate key in request body")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ClaimError("bad_body", "non-finite number in request body")


class _Call:
    """What the request is: a tool call (``name``, ``arguments``) or not."""

    def __init__(self, name: Optional[str] = None, arguments: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.arguments = arguments or {}

    @property
    def is_call(self) -> bool:
        return self.name is not None


def _parse_body(body: bytes) -> _Call:
    if not body or not body.strip():
        return _Call()
    try:
        parsed = json.loads(
            body.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
        )
    except ClaimError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ClaimError("bad_body", "request body is not valid JSON") from exc
    if isinstance(parsed, list):
        raise ClaimError("batch_not_allowed", "JSON-RPC batches are not accepted")
    if not isinstance(parsed, dict):
        raise ClaimError("bad_body", "request body is not a JSON-RPC object")
    if parsed.get("method") != "tools/call":
        return _Call()
    params = parsed.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise ClaimError("bad_body", "tools/call without a tool name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ClaimError("bad_body", "tools/call arguments are not an object")
    return _Call(params["name"], arguments)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_tuple(value: Any, name: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ClaimError("malformed", name + " is not a list of strings")
    return tuple(value)


class ClaimVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: Optional[str] = None,
        jwks: Optional[Mapping[str, Any]] = None,
        fetch: Optional[Callable[[str], Any]] = None,
        leeway: float = 5,
        jwks_ttl: float = 300,
        replay_cache: Any = _DEFAULT_REPLAY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (jwks_url is None) == (jwks is None):
            raise ValueError("exactly one of jwks_url and jwks is required")
        if not 0 <= leeway <= MAX_LEEWAY_SECONDS:
            raise ValueError("leeway must be 0 to %d seconds" % MAX_LEEWAY_SECONDS)
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._fetch = fetch or _default_fetch
        self._leeway = leeway
        self._ttl = jwks_ttl
        self._clock = clock
        if replay_cache is _DEFAULT_REPLAY:
            replay_cache = InMemoryReplayCache(clock=clock)
        self._replay = replay_cache
        self._lock = threading.Lock()
        self._keys = {}  # type: Dict[str, Ed25519PublicKey]
        self._fetched_at = None  # type: Optional[float]
        if jwks is not None:
            self._keys = self._load_keys(jwks)

    # -- public API ---------------------------------------------------------------------------

    def verify(self, token: str, *, body: bytes = b"") -> Principal:
        """Verify ``token`` for the HTTP request whose raw body is ``body``."""
        return self._verify(token, _parse_body(body))

    def verify_tool_call(self, token: str, tool: str, arguments: Optional[Mapping[str, Any]]) -> Principal:
        """Verify ``token`` for a tool call the server already parsed."""
        return self._verify(token, _Call(tool, dict(arguments) if arguments else {}))

    # -- internals ----------------------------------------------------------------------------

    @staticmethod
    def _load_keys(document: Mapping[str, Any]) -> Dict[str, Ed25519PublicKey]:
        keys = {}  # type: Dict[str, Ed25519PublicKey]
        entries = document.get("keys") if isinstance(document, Mapping) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519":
                continue
            kid, x = entry.get("kid"), entry.get("x")
            if not isinstance(kid, str) or not isinstance(x, str):
                continue
            try:
                keys[kid] = Ed25519PublicKey.from_public_bytes(_b64url_decode(x))
            except (ValueError, TypeError):
                continue
        return keys

    def _refresh(self, now: float) -> None:
        # Record the attempt first so a failing endpoint is not hammered either.
        self._fetched_at = now
        try:
            raw = self._fetch(self._jwks_url or "")
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            document = json.loads(raw) if isinstance(raw, str) else raw
            self._keys = self._load_keys(document)
        except Exception:  # noqa: BLE001 - keep the last good key set when the fetch fails
            return

    def _key_for(self, kid: Any) -> Ed25519PublicKey:
        if not isinstance(kid, str) or not kid:
            raise ClaimError("unknown_key", "token has no kid")
        if self._jwks_url is not None:
            now = self._clock()
            with self._lock:
                if self._fetched_at is None or now - self._fetched_at >= self._ttl:
                    self._refresh(now)
                elif kid not in self._keys and now - self._fetched_at >= MIN_REFETCH_SECONDS:
                    self._refresh(now)
                key = self._keys.get(kid)
        else:
            key = self._keys.get(kid)
        if key is None:
            raise ClaimError("unknown_key", "no published key matches the token's kid")
        return key

    def _verify(self, token: str, call: _Call) -> Principal:
        if not isinstance(token, str) or not token:
            raise ClaimError("malformed", "no claim")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ClaimError("malformed", "claim is not a JWT") from exc
        if header.get("alg") != "EdDSA":
            raise ClaimError("bad_algorithm", "alg must be EdDSA")
        if header.get("typ") != CLAIM_TYPE:
            raise ClaimError("bad_type", "typ must be " + CLAIM_TYPE)
        key = self._key_for(header.get("kid"))
        try:
            claims = jwt.decode(
                token, key, algorithms=["EdDSA"], audience=self._audience, issuer=self._issuer,
                options={
                    "require": _REQUIRED_CLAIMS, "verify_exp": False, "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidSignatureError as exc:
            raise ClaimError("bad_signature", "signature does not verify") from exc
        except jwt.InvalidAudienceError as exc:
            raise ClaimError("bad_audience", "aud does not match") from exc
        except jwt.InvalidIssuerError as exc:
            raise ClaimError("bad_issuer", "iss does not match") from exc
        except jwt.MissingRequiredClaimError as exc:
            code = {"aud": "bad_audience", "iss": "bad_issuer"}.get(getattr(exc, "claim", ""), "malformed")
            raise ClaimError(code, "required claim missing") from exc
        except jwt.PyJWTError as exc:
            raise ClaimError("malformed", "claim could not be decoded") from exc

        exp, iat = claims["exp"], claims["iat"]
        if not _is_number(exp) or not _is_number(iat):
            raise ClaimError("malformed", "exp and iat must be numbers")
        if not isinstance(claims["sub"], str) or not isinstance(claims["jti"], str) or not claims["jti"]:
            raise ClaimError("malformed", "sub and jti must be strings")
        now = self._clock()
        if exp + self._leeway < now:
            raise ClaimError("expired", "claim has expired")
        if iat - self._leeway > now:
            raise ClaimError("not_yet_valid", "claim is issued in the future")
        if exp - iat <= 0 or exp - iat > MAX_LIFETIME_SECONDS:
            raise ClaimError("bad_lifetime", "lifetime must be 1 to %d seconds" % MAX_LIFETIME_SECONDS)
        version = claims.get("claim_version")
        if version != CLAIM_VERSION or isinstance(version, bool):
            raise ClaimError("bad_version", "claim_version must be %d" % CLAIM_VERSION)
        principal_type = claims.get("principal_type")
        if principal_type not in _PRINCIPAL_TYPES:
            raise ClaimError("bad_principal_type", "unknown principal_type")
        audiences = _string_tuple(claims.get("audiences", []), "audiences")
        tools = _string_tuple(claims.get("tools", []), "tools")

        req = claims.get("req")
        if call.is_call:
            if req is None:
                raise ClaimError("request_required", "a tools/call needs a claim bound to it")
            if not isinstance(req, str):
                raise ClaimError("malformed", "req must be a string")
            try:
                expected = request_hash(call.name or "", call.arguments)
            except (TypeError, ValueError) as exc:
                raise ClaimError("bad_body", "arguments cannot be canonicalised") from exc
            if not hmac.compare_digest(expected.encode("ascii"), req.encode("utf-8")):
                raise ClaimError("request_mismatch", "the claim was issued for a different call")
        else:
            if req is not None:
                raise ClaimError("request_unexpected", "req on a request that is not a tools/call")
            audiences, tools = (), ()

        act = claims.get("act")
        if act is not None and not (
            isinstance(act, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in act.items())
        ):
            raise ClaimError("malformed", "act must be an object of strings")
        for name in ("email", "approved_by", "token_id"):
            if claims.get(name) is not None and not isinstance(claims[name], str):
                raise ClaimError("malformed", name + " must be a string")

        if self._replay is not None and not self._replay.add_if_new(claims["jti"], float(exp) + self._leeway):
            raise ClaimError("replayed", "this claim was already used")

        return Principal(
            sub=claims["sub"],
            principal_type=principal_type,
            email=claims.get("email"),
            audiences=audiences,
            tools=tools,
            approved_by=claims.get("approved_by"),
            token_id=claims.get("token_id"),
            act=dict(act) if act is not None else None,
            jti=claims["jti"],
            expires_at=float(exp),
            claim_version=version,
        )
