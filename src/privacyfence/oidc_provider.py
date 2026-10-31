"""PrivacyFence as an OpenID Connect provider for web apps the operator names.

The operator lists each app (its ``client_id``, redirect addresses and secret) in the server's
environment; nothing is registered dynamically. This module holds the parts that need no HTTP:
the client list read from the environment, the RS256 signing key kept in the organization
directory, and the provider object behind the routes in ``web/routes_oidc.py``: discovery, the key
set, single-use authorization codes bound to a PKCE S256 challenge, ID tokens and short-lived access
tokens for userinfo.

Codes and access tokens live only in this process's memory and are capped, so a restart signs
nobody out of the apps (they keep their own sessions) and a flood of requests cannot grow memory
without bound.

Deleting the key file rotates the key at the next start. A key file that exists but cannot be used
refuses the start rather than being replaced, so a damaged file is never silently swapped for a
new identity.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .org_mode import ConfigurationError
from .principal import Principal

CLIENTS_ENV = "PRIVACYFENCE_OIDC_CLIENTS"
_CLIENT_STEM = "PRIVACYFENCE_OIDC_CLIENT_"
_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_MIN_SECRET_LENGTH = 32
_MIN_KEY_BITS = 2048
_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

CODE_LIFETIME_SECONDS = 60
TOKEN_LIFETIME_SECONDS = 300
# The most live codes, and live access tokens, held at once. Past it a new code is refused and the
# oldest access token is dropped.
MAX_LIVE_ENTRIES = 10_000


@dataclass(frozen=True)
class OidcClient:
    """A web app that signs in through PrivacyFence. The secret never appears in ``repr``."""

    client_id: str
    redirect_uris: tuple[str, ...]
    secret: str = field(repr=False)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _redirect_uri_ok(uri: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(uri)
        host = parts.hostname
        # Reading the port validates it as well.
        _ = parts.port
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None or parts.fragment or "#" in uri:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in _LOCALHOST_HOSTS


def _client_secret(stem: str) -> str:
    inline_env = f"{stem}SECRET"
    file_env = f"{stem}SECRET_FILE"
    inline = _env(inline_env)
    path = _env(file_env)
    if inline and path:
        raise ConfigurationError(f"Set {inline_env} or {file_env}, not both.")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                secret = handle.read().rstrip("\r\n").strip()
        except (OSError, UnicodeDecodeError):
            # The reason is dropped on purpose: a decode error quotes the bytes it choked on.
            raise ConfigurationError(f"{file_env} could not be read.") from None
    else:
        secret = inline
    if len(secret) < _MIN_SECRET_LENGTH:
        raise ConfigurationError(f"{inline_env} must be at least {_MIN_SECRET_LENGTH} characters")
    return secret


def clients_from_env() -> dict[str, OidcClient]:
    """The clients named in ``PRIVACYFENCE_OIDC_CLIENTS``, keyed by ``client_id``.

    Empty when the variable is unset or empty. Every problem raises ``ConfigurationError`` with a
    message that names the variable and never quotes a secret."""
    names = [part.strip().lower() for part in _env(CLIENTS_ENV).split(",") if part.strip()]
    clients: dict[str, OidcClient] = {}
    for name in names:
        if not _NAME_RE.fullmatch(name):
            raise ConfigurationError(f"{CLIENTS_ENV} has an invalid name: {name}")
        if name in clients:
            continue
        stem = f"{_CLIENT_STEM}{name.upper().replace('-', '_')}_"
        uris = tuple(part.strip() for part in _env(f"{stem}REDIRECT_URIS").split(",") if part.strip())
        if not uris:
            raise ConfigurationError(f"{stem}REDIRECT_URIS must list at least one address")
        for uri in uris:
            if not _redirect_uri_ok(uri):
                raise ConfigurationError(
                    f"{stem}REDIRECT_URIS has an address that is not https:// (http:// only for localhost) "
                    f"or has a user name or fragment: {uri}"
                )
        clients[name] = OidcClient(client_id=name, redirect_uris=uris, secret=_client_secret(stem))
    return clients


# ----- #
# ----- Signing key
# ----- #


@dataclass(frozen=True)
class SigningKey:
    """The RS256 key ID tokens are signed with, and its ``kid``."""

    private_key: rsa.RSAPrivateKey = field(repr=False)
    kid: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64url(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _kid(private_key: rsa.RSAPrivateKey) -> str:
    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _b64url(hashlib.sha256(der).digest())[:16]


def _write_new_key(path: Path, private_key: rsa.RSAPrivateKey) -> None:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def load_or_create_signing_key(path: Path) -> SigningKey:
    """Load the RSA key at ``path``, or create one there on first start.

    A file that exists but is not a usable RSA key of at least 2048 bits raises
    ``ConfigurationError``; it is never overwritten."""
    if path.exists():
        try:
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError):
            loaded = None
        if not isinstance(loaded, rsa.RSAPrivateKey) or loaded.key_size < _MIN_KEY_BITS:
            raise ConfigurationError(
                f"{path} is not a usable RSA signing key of at least {_MIN_KEY_BITS} bits; "
                "delete it to make a new one."
            )
        return SigningKey(private_key=loaded, kid=_kid(loaded))
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=_MIN_KEY_BITS)
    _write_new_key(path, private_key)
    return SigningKey(private_key=private_key, kid=_kid(private_key))


def jwks(key: SigningKey) -> dict[str, object]:
    """The JSON Web Key Set that verifies tokens signed with ``key``."""
    numbers = key.private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": key.kid,
                "n": _int_b64url(numbers.n),
                "e": _int_b64url(numbers.e),
            }
        ]
    }


# ----- #
# ----- Provider
# ----- #


@dataclass(frozen=True)
class CodeGrant:
    """What an authorization code stands for, from the authorization request to its redemption."""

    client_id: str
    redirect_uri: str
    principal: Principal
    email_verified: bool
    nonce: str | None
    code_challenge: str
    scope: str  # the requested scopes, space-separated, "openid" always included
    auth_time: int
    expires_at: float


@dataclass(frozen=True)
class _IssuedToken:
    claims: dict[str, object]
    expires_at: float


def _s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


class OidcProvider:
    """The OpenID Connect provider: issuer, clients, signing key, and the live codes and tokens."""

    def __init__(
        self,
        *,
        issuer: str,
        clients: dict[str, OidcClient],
        key: SigningKey,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer = issuer
        self._clients = clients
        self._key = key
        self._clock = clock
        self._lock = threading.Lock()
        self._codes: dict[str, CodeGrant] = {}
        # Insertion-ordered, so the first entry is always the oldest token.
        self._tokens: dict[str, _IssuedToken] = {}

    def discovery(self) -> dict[str, object]:
        iss = self.issuer
        return {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/authorize",
            "token_endpoint": f"{iss}/token",
            "userinfo_endpoint": f"{iss}/userinfo",
            "jwks_uri": f"{iss}/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "email", "profile"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
            "claims_supported": ["sub", "email", "email_verified", "name"],
        }

    def jwks(self) -> dict[str, object]:
        return jwks(self._key)

    def client(self, client_id: str) -> OidcClient | None:
        return self._clients.get(client_id)

    def authenticate(self, client_id: str, secret: str) -> OidcClient | None:
        """The client when ``secret`` is its secret. Compared as bytes, so non-ASCII input is just
        a mismatch, never an exception."""
        client = self._clients.get(client_id)
        if client is None:
            return None
        if not hmac.compare_digest(secret.encode(), client.secret.encode()):
            return None
        return client

    def _prune_locked(self, now: float) -> None:
        for code in [c for c, grant in self._codes.items() if grant.expires_at <= now]:
            del self._codes[code]
        for token in [t for t, issued in self._tokens.items() if issued.expires_at <= now]:
            del self._tokens[token]

    def issue_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        principal: Principal,
        email_verified: bool,
        nonce: str | None,
        code_challenge: str,
        scope: str,
        auth_time: int,
    ) -> str | None:
        """A new single-use code, valid for 60 seconds, or None when too many codes are live."""
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            if len(self._codes) >= MAX_LIVE_ENTRIES:
                return None
            self._codes[code] = CodeGrant(
                client_id=client_id, redirect_uri=redirect_uri, principal=principal,
                email_verified=email_verified, nonce=nonce, code_challenge=code_challenge, scope=scope,
                auth_time=auth_time, expires_at=now + CODE_LIFETIME_SECONDS,
            )
        return code

    def redeem_code(self, code: str, *, client_id: str, redirect_uri: str, code_verifier: str) -> CodeGrant | None:
        """The code's grant, consuming the code whatever the outcome. None unless the code is live,
        was issued to ``client_id`` for ``redirect_uri``, and ``code_verifier`` matches its S256
        challenge. An empty verifier never matches."""
        with self._lock:
            grant = self._codes.pop(code, None)
            now = self._clock()
        if grant is None or grant.expires_at <= now:
            return None
        if grant.client_id != client_id or grant.redirect_uri != redirect_uri:
            return None
        if not code_verifier:
            return None
        if not hmac.compare_digest(_s256(code_verifier).encode(), grant.code_challenge.encode()):
            return None
        return grant

    def issue_tokens(self, grant: CodeGrant) -> dict[str, object]:
        """The token response for a redeemed grant: an access token for userinfo and a signed ID
        token, both valid for five minutes."""
        access_token = secrets.token_urlsafe(32)
        principal = grant.principal
        user_claims: dict[str, object] = {
            "sub": principal.id,
            "email": principal.email,
            "email_verified": grant.email_verified,
            "name": principal.display_name,
        }
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            while len(self._tokens) >= MAX_LIVE_ENTRIES:
                del self._tokens[next(iter(self._tokens))]
            self._tokens[access_token] = _IssuedToken(claims=user_claims, expires_at=now + TOKEN_LIFETIME_SECONDS)
        iat = int(now)
        claims: dict[str, object] = {
            "iss": self.issuer,
            "sub": principal.id,
            "aud": grant.client_id,
            "iat": iat,
            "exp": iat + TOKEN_LIFETIME_SECONDS,
            "auth_time": grant.auth_time,
            "at_hash": _b64url(hashlib.sha256(access_token.encode("ascii")).digest()[:16]),
            "email": principal.email,
            "email_verified": grant.email_verified,
            "name": principal.display_name,
        }
        if grant.nonce is not None:
            claims["nonce"] = grant.nonce
        id_token = jwt.encode(claims, self._key.private_key, algorithm="RS256", headers={"kid": self._key.kid})
        return {
            "access_token": access_token,
            "token_type": "Bearer",  # nosec B105  # the OAuth token_type, not a credential
            "expires_in": TOKEN_LIFETIME_SECONDS,
            "scope": grant.scope,
            "id_token": id_token,
        }

    def userinfo(self, access_token: str) -> dict[str, object] | None:
        """The claims for a live access token, else None."""
        with self._lock:
            issued = self._tokens.get(access_token)
            now = self._clock()
            if issued is None:
                return None
            if issued.expires_at <= now:
                del self._tokens[access_token]
                return None
            return dict(issued.claims)
