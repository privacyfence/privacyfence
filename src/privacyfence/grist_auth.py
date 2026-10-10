"""How PrivacyFence authenticates to Grist.

With an OAuth app in the organization bundle, people sign in through the
authorization code flow with PKCE (a confidential client, with refresh tokens).
Without one, they enter a personal API key. Either way, a credential is only
ever used with the server it was entered or issued for. Redirects are never
followed, and nothing logs a token, a key, the client secret or a code; only a
host is logged.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

import requests

from . import oauth_loopback
from .oauth_loopback import OAuthLoopbackError
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

GRIST_OAUTH_PORT = 53685
GRIST_REDIRECT_PATH = "/callback"
GRIST_SCOPES = "doc:read doc:write doc.schema:write offline_access"
DEFAULT_SERVER_URL = "https://docs.getgrist.com"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DISCOVERY_PATH = "/.well-known/oauth-authorization-server"
_REFRESH_MARGIN_SECONDS = 60
_DEFAULT_EXPIRES_IN = 3600
_DETAIL_LIMIT = 200

_NOT_AUTHENTICATED = "Grist is not authenticated. Use Authenticate… in PrivacyFence Settings."
_UNREADABLE = (
    "Grist's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again."
)
_OAUTH_REQUIRED = (
    "Your organization connects to Grist with OAuth. Use Authenticate… in PrivacyFence Settings."
)
_DIFFERENT_SERVER = (
    "Grist was connected to a different server than your organization uses. "
    "Use Authenticate… in PrivacyFence Settings to connect again."
)
_EXPIRED = (
    "Your Grist sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again."
)
_SCHEME_RULE = "The Grist server address must start with https:// (http:// is allowed only for localhost)."


class GristClientError(Exception):
    """Any Grist failure the caller can show a person."""


class GristAccessDenied(GristClientError):
    """Grist answered HTTP 403."""


# --------------------------------------------------------------------------- #
# Server address
# --------------------------------------------------------------------------- #


def _scheme_allowed(scheme: str, host: str) -> bool:
    return scheme == "https" or (scheme == "http" and host in _LOOPBACK_HOSTS)


def normalize_server_url(url: str) -> str:
    """The server address as ``scheme://host[:port][/prefix]``, or GristClientError."""
    text = (url or "").strip().rstrip("/")
    if not text:
        raise GristClientError("Enter the Grist server address, such as https://docs.getgrist.com.")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        raise GristClientError(_SCHEME_RULE) from None
    scheme = parts.scheme.lower()
    if not _scheme_allowed(scheme, host):
        raise GristClientError(_SCHEME_RULE)
    if not host:
        raise GristClientError("Enter the Grist server address, such as https://docs.getgrist.com.")
    if "@" in parts.netloc or "?" in text or "#" in text:
        raise GristClientError(
            "The Grist server address must not contain a user name, password, query or fragment."
        )
    if "api" in (segment.lower() for segment in parts.path.split("/")):
        raise GristClientError("Enter the server address without /api.")
    return f"{scheme}://{parts.netloc.lower()}{parts.path}"


# --------------------------------------------------------------------------- #
# Bundle section
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GristOAuthConfig:
    server_url: str
    client_id: str
    client_secret: str = field(repr=False)
    auth_server_url: str


@dataclass(frozen=True)
class GristBundle:
    server_url: str
    oauth: GristOAuthConfig | None


def _text(section: dict[str, Any], key: str) -> str:
    return str(section.get(key) or "").strip()


def bundle_settings(section: dict[str, Any], *, org_mode: bool) -> GristBundle:
    """Read the organization bundle's ``grist`` section."""
    client_id = _text(section, "client_id")
    client_secret = _text(section, "client_secret")
    if bool(client_id) != bool(client_secret):
        raise GristClientError(
            "Grist organization config is incomplete: client_id and client_secret go together."
        )
    raw_server = _text(section, "server_url")
    server_url = normalize_server_url(raw_server) if raw_server else ""
    oauth: GristOAuthConfig | None = None
    if client_id:
        server_url = server_url or DEFAULT_SERVER_URL
        raw_auth = _text(section, "auth_server_url")
        oauth = GristOAuthConfig(
            server_url=server_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_server_url=normalize_server_url(raw_auth) if raw_auth else server_url,
        )
    if org_mode and not server_url:
        raise GristClientError("Grist organization config not installed")
    return GristBundle(server_url=server_url, oauth=oauth)


# --------------------------------------------------------------------------- #
# Credential file
# --------------------------------------------------------------------------- #

_REQUIRED_KEYS: dict[str, dict[str, Any]] = {
    "oauth": {"access_token": str, "refresh_token": str, "expires_at": (int, float)},
    "api_key": {"api_key": str},
}


def save_token_file(token_file: str, record: dict[str, Any]) -> None:
    """The only place a Grist credential is written."""
    atomic_write_json(token_file, record)


def save_api_key(token_file: str, server_url: str, api_key: str) -> None:
    save_token_file(
        token_file,
        {"auth": "api_key", "server_url": normalize_server_url(server_url), "api_key": api_key},
    )


def load_token_file(token_file: str) -> dict[str, Any]:
    """The saved credential record, or GristClientError."""
    if not os.path.exists(token_file):
        raise GristClientError(_NOT_AUTHENTICATED)
    try:
        with open(token_file, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        raise GristClientError(_UNREADABLE) from None
    if not isinstance(record, dict):
        raise GristClientError(_UNREADABLE)
    auth = record.get("auth")
    required = _REQUIRED_KEYS.get(auth) if isinstance(auth, str) else None
    if required is None:
        raise GristClientError(_NOT_AUTHENTICATED)
    for key, kind in {"server_url": str, **required}.items():
        value = record.get(key)
        if not isinstance(value, kind) or isinstance(value, bool) or value == "":
            raise GristClientError(_NOT_AUTHENTICATED)
    return {**record, "server_url": normalize_server_url(record["server_url"])}


class GristCredential(Protocol):
    can_refresh: bool

    def access_token(self, *, force_refresh: bool = False) -> str: ...


class GristApiKey:
    """A personal API key; it cannot be refreshed."""

    can_refresh = False

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def access_token(self, *, force_refresh: bool = False) -> str:
        return self._api_key

    def __repr__(self) -> str:
        return "GristApiKey(<hidden>)"


def resolve_credential(
    bundle: GristBundle, record: dict[str, Any], token_file: str,
) -> tuple[str, GristCredential]:
    """The ``(server_url, credential)`` pair for a saved record.

    A credential is never paired with a server it was not entered or issued for.
    """
    kind = record.get("auth")
    if bundle.oauth is not None:
        if kind != "oauth":
            raise GristClientError(_OAUTH_REQUIRED)
        if record["server_url"] != bundle.oauth.server_url:
            raise GristClientError(_DIFFERENT_SERVER)
        return bundle.oauth.server_url, GristTokenProvider(bundle.oauth, token_file, record)
    if kind != "api_key":
        raise GristClientError(_NOT_AUTHENTICATED)
    if bundle.server_url and record["server_url"] != bundle.server_url:
        raise GristClientError(_DIFFERENT_SERVER)
    return record["server_url"], GristApiKey(record["api_key"])


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GristOAuthEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    client_secret_basic: bool


_DISCOVERY_CACHE: dict[str, GristOAuthEndpoints] = {}


def _host(url: str) -> str:
    return urlsplit(url).netloc


def _clip(value: Any) -> str:
    return str(value or "")[:_DETAIL_LIMIT]


def _send(method: Any, url: str, **kwargs: Any) -> tuple[int, dict[str, Any] | None]:
    """One sign-in request: ``(status, JSON object or None)``.

    Redirects, server errors, transport failures and a non-JSON success become
    GristClientError; a 4xx comes back for the caller to read.
    """
    host = _host(url)
    try:
        resp = method(url, timeout=30, allow_redirects=False, **kwargs)
    except requests.RequestException as exc:
        raise GristClientError(
            f"Could not reach Grist's sign-in server at {host}: {type(exc).__name__}"
        ) from None
    status = resp.status_code
    if 300 <= status < 400:
        raise GristClientError(
            f"Grist's sign-in server answered with a redirect (HTTP {status}). "
            "Check the sign-in server in the organization config."
        )
    if status >= 500:
        raise GristClientError(f"Grist's sign-in server answered with an error (HTTP {status}).")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None
    if status < 300 and body is None:
        raise GristClientError(
            f"Grist's sign-in server answered with something other than JSON (HTTP {status})."
        )
    return status, body


def _unusable(host: str) -> GristClientError:
    return GristClientError(
        f"Grist's sign-in settings at {host} are not usable. Check the Grist server address "
        "(and the sign-in server, if set) in the organization config."
    )


def _discovery_document(base: str) -> dict[str, Any]:
    status, body = _send(requests.get, base + _DISCOVERY_PATH)
    if status >= 400 or body is None:
        raise _unusable(_host(base))
    return body


def _issuer_of(document: dict[str, Any], host: str) -> str:
    issuer = document.get("issuer")
    if not isinstance(issuer, str):
        raise _unusable(host)
    try:
        return normalize_server_url(issuer)
    except GristClientError:
        raise _unusable(host) from None


def _endpoint(document: dict[str, Any], key: str, issuer: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise _unusable(_host(issuer))
    parts = urlsplit(value)
    if not _scheme_allowed(parts.scheme.lower(), parts.hostname or "") or parts.netloc.lower() != _host(issuer):
        raise _unusable(_host(issuer))
    return value


def discover(auth_server_url: str, server_url: str) -> GristOAuthEndpoints:
    """The OAuth endpoints for a Grist server (RFC 8414), cached per sign-in server.

    The configured server vouches for the issuer its document names; that
    issuer's own document is fetched once and nothing is followed further.
    """
    cached = _DISCOVERY_CACHE.get(auth_server_url)
    if cached is not None:
        return cached
    host = _host(auth_server_url)
    document = _discovery_document(auth_server_url)
    issuer = _issuer_of(document, host)
    if issuer != auth_server_url:
        document = _discovery_document(issuer)
        if _issuer_of(document, host) != issuer:
            raise _unusable(host)
    methods = document.get("token_endpoint_auth_methods_supported")
    endpoints = GristOAuthEndpoints(
        authorization_endpoint=_endpoint(document, "authorization_endpoint", issuer),
        token_endpoint=_endpoint(document, "token_endpoint", issuer),
        client_secret_basic=methods is None or (isinstance(methods, list) and "client_secret_basic" in methods),
    )
    logger.info("Grist sign-in for %s uses the sign-in server %s", _host(server_url), _host(issuer))
    _DISCOVERY_CACHE[auth_server_url] = endpoints
    return endpoints


def build_authorize_url(
    endpoints: GristOAuthEndpoints, client_id: str, redirect_uri: str, state: str, code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": GRIST_SCOPES,
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorization_endpoint}?{urlencode(params)}"


def _post_token(
    config: GristOAuthConfig, endpoints: GristOAuthEndpoints, form: dict[str, str],
) -> tuple[int, dict[str, Any] | None]:
    data = dict(form)
    headers = {"Accept": "application/json"}
    if endpoints.client_secret_basic:
        pair = f"{quote(config.client_id, safe='')}:{quote(config.client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    else:
        data["client_id"] = config.client_id
        data["client_secret"] = config.client_secret
    return _send(requests.post, endpoints.token_endpoint, data=data, headers=headers)


def _error_detail(body: dict[str, Any]) -> str:
    error = _clip(body.get("error"))
    description = _clip(body.get("error_description"))
    return f"{error}: {description}" if description else error


def _token_fields(body: dict[str, Any], host: str) -> tuple[str, float]:
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GristClientError(f"Grist's sign-in server at {host} did not return an access token.")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        expires_in = _DEFAULT_EXPIRES_IN
    return access_token, time.time() + expires_in


def exchange_code(
    config: GristOAuthConfig, endpoints: GristOAuthEndpoints, code: str, redirect_uri: str, code_verifier: str,
) -> dict[str, Any]:
    """Trade an authorization code for the OAuth credential record (not yet saved)."""
    status, body = _post_token(config, endpoints, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })
    if status >= 400:
        detail = _error_detail(body) if body else f"HTTP {status}"
        raise GristClientError(f"Grist sign-in failed: {detail}")
    body = body or {}  # _send raises for a 2xx without a JSON object
    access_token, expires_at = _token_fields(body, _host(endpoints.token_endpoint))
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GristClientError(
            "Grist did not return a refresh token. Check that the PrivacyFence app in Grist allows offline_access."
        )
    logger.info("Grist sign-in complete for %s", _host(config.server_url))
    return {
        "auth": "oauth",
        "server_url": config.server_url,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


def refresh(config: GristOAuthConfig, endpoints: GristOAuthEndpoints, record: dict[str, Any]) -> dict[str, Any]:
    """The record with a fresh access token (and the rotated refresh token, if one came back)."""
    status, body = _post_token(config, endpoints, {
        "grant_type": "refresh_token",
        "refresh_token": record["refresh_token"],
    })
    if status >= 400:
        if body is None:
            if status in (400, 401):
                raise GristClientError(_EXPIRED)
            raise GristClientError(f"Grist sign-in refresh failed: HTTP {status}")
        if body.get("error") == "invalid_grant":
            raise GristClientError(_EXPIRED)
        raise GristClientError(f"Grist sign-in refresh failed: {_error_detail(body)}")
    body = body or {}  # _send raises for a 2xx without a JSON object
    access_token, expires_at = _token_fields(body, _host(endpoints.token_endpoint))
    updated = {**record, "access_token": access_token, "expires_at": expires_at}
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        updated["refresh_token"] = new_refresh
    return updated


class GristTokenProvider:
    """Supplies a valid OAuth access token, refreshing and saving it as needed."""

    can_refresh = True

    def __init__(self, config: GristOAuthConfig, token_file: str, record: dict[str, Any]) -> None:
        self._config = config
        self._token_file = token_file
        self._record = dict(record)
        self._lock = threading.Lock()

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or float(self._record["expires_at"]) - _REFRESH_MARGIN_SECONDS <= time.time():
                endpoints = discover(self._config.auth_server_url, self._config.server_url)
                self._record = refresh(self._config, endpoints, self._record)
                save_token_file(self._token_file, self._record)
            return self._record["access_token"]

    def __repr__(self) -> str:
        return f"GristTokenProvider({_host(self._config.server_url)})"


def authorize_interactive(config: GristOAuthConfig, token_file: str) -> dict[str, Any]:
    """Sign in through the browser (loopback redirect), save and return the record."""
    endpoints = discover(config.auth_server_url, config.server_url)

    def _build(redirect_uri: str, state: str, code_challenge: str) -> str:
        return build_authorize_url(endpoints, config.client_id, redirect_uri, state, code_challenge)

    def _exchange(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        return exchange_code(config, endpoints, code, redirect_uri, code_verifier)

    try:
        record = oauth_loopback.run_browser_oauth(
            _build, _exchange, port=GRIST_OAUTH_PORT, path=GRIST_REDIRECT_PATH, redirect_host="localhost",
        )
    except OAuthLoopbackError as exc:
        raise GristClientError(f"Grist sign-in failed: {exc}") from exc
    save_token_file(token_file, record)
    return record
