"""PrivacyFence as an OpenID Connect provider for web apps the operator names.

The operator lists each app (its ``client_id``, redirect addresses and secret) in the server's
environment; nothing is registered dynamically. This module holds the parts that need no HTTP:
the client list read from the environment, the RS256 signing key kept in the organization
directory, and the provider object the routes in ``web/routes_oidc.py`` serve discovery and the
key set from.

Deleting the key file rotates the key at the next start. A key file that exists but cannot be used
refuses the start rather than being replaced, so a damaged file is never silently swapped for a
new identity.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .org_mode import ConfigurationError

CLIENTS_ENV = "PRIVACYFENCE_OIDC_CLIENTS"
_CLIENT_STEM = "PRIVACYFENCE_OIDC_CLIENT_"
_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_MIN_SECRET_LENGTH = 32
_MIN_KEY_BITS = 2048
_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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
            os.fchmod(handle.fileno(), 0o600)
            handle.write(pem)
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


class OidcProvider:
    """The OpenID Connect provider's configuration: issuer, clients and signing key."""

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
