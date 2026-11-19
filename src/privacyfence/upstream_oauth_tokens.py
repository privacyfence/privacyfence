"""Tokens for hosted upstream MCP servers that sign each person in with OAuth.

ADR 0329 (the OAuth-only upstream feature) and ADR 0330 (discovery, registration and token
storage) say why. This module holds what the discovery, registration, connect and call code share:

- ``UpstreamOAuthError``, whose text is its kind and never a response body;
- ``http_json``, the one capped, redirect-free request helper every OAuth request goes through;
- the per-person token file (plaintext JSON, mode 0600, like every other third-party token);
- the authorize URL, the authorization-code exchange and the refreshing token provider.

Nothing here logs or quotes a response body, a token, a code or a secret, and there is no
module-level mutable state: locks live on the provider instance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from mcp.shared.auth import OAuthToken
from mcp.shared.auth_utils import calculate_token_expiry
from pydantic import ValidationError

from . import paths
from .secure_files import atomic_write_json, secure_mkdir

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
_TIMEOUT_SECONDS = 10.0
_REFRESH_MARGIN_SECONDS = 60
_TOKEN_FILE_VERSION = 1
_BAD_CLIENT_STATUSES = (400, 401)


class UpstreamOAuthError(Exception):
    """An upstream OAuth failure; ``str(exc)`` is the kind and nothing else."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None = field(default=None, repr=False)
    auth_method: str = "none"


@dataclass(frozen=True)
class TokenRecord:
    upstream_id: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float | None
    scope: str
    issuer: str
    client_id: str
    auth_method: str
    token_endpoint: str
    resource: str | None
    connected_at: float


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


async def http_json(
    method: str,
    url: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    auth: httpx2.Auth | None = None,
) -> tuple[int, dict[str, Any] | None, httpx2.Headers]:
    """One request, redirects off, the body read through a 64 KiB cap.

    Returns the status, the body when it is a JSON object (else ``None``) and the headers. A
    transport error, a timeout or an oversize body is ``unreachable``.
    """
    try:
        async with httpx2.AsyncClient(
            transport=transport, timeout=httpx2.Timeout(_TIMEOUT_SECONDS), follow_redirects=False,
        ) as client:
            request = client.build_request(method, url, headers=headers, json=json_body, data=form)
            response = await client.send(request, auth=auth if auth is not None else httpx2.USE_CLIENT_DEFAULT, stream=True)
            try:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise UpstreamOAuthError("unreachable")
            finally:
                await response.aclose()
    except httpx2.HTTPError:
        raise UpstreamOAuthError("unreachable") from None
    try:
        parsed = json.loads(bytes(body))
    except ValueError:
        parsed = None
    return response.status_code, parsed if isinstance(parsed, dict) else None, response.headers


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` in lower case."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


# --------------------------------------------------------------------------- #
# Token files
# --------------------------------------------------------------------------- #


def token_path(principal: Principal, upstream_id: str) -> Path:
    return paths.user_dir(principal) / "credentials" / "upstream_mcp" / f"{upstream_id}.json"


def token_paths(upstream_id: str) -> list[Path]:
    """Every person's token file for this upstream, newest modification first."""
    found = list((paths.data_dir() / "users").glob(f"*/credentials/upstream_mcp/{upstream_id}.json"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def load_token(path: Path) -> TokenRecord | None:
    """The saved record, or ``None`` when the file is absent, unreadable or not a version 1 record."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _TOKEN_FILE_VERSION:
        return None
    try:
        return TokenRecord(
            upstream_id=_text(data["upstream_id"]),
            access_token=_text(data["access_token"]),
            refresh_token=_optional_text(data.get("refresh_token")),
            expires_at=_optional_number(data.get("expires_at")),
            scope=_text(data.get("scope", "")),
            issuer=_text(data["issuer"]),
            client_id=_text(data["client_id"]),
            auth_method=_text(data["auth_method"]),
            token_endpoint=_text(data["token_endpoint"]),
            resource=_optional_text(data.get("resource")),
            connected_at=_number(data["connected_at"]),
        )
    except (KeyError, TypeError):
        return None


def save_token(path: Path, record: TokenRecord) -> None:
    """The only place an upstream credential is written."""
    path = Path(path)
    secure_mkdir(path.parent)
    atomic_write_json(path, {"version": _TOKEN_FILE_VERSION, **asdict(record)})


def delete_token(path: Path) -> None:
    Path(path).unlink(missing_ok=True)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("not text")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("not a number")
    return float(value)


def _optional_number(value: Any) -> float | None:
    return None if value is None else _number(value)


# --------------------------------------------------------------------------- #
# Authorization code flow
# --------------------------------------------------------------------------- #


def build_authorize_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None,
    resource: str | None,
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope is not None:
        query["scope"] = scope
    if resource is not None:
        query["resource"] = resource
    separator = "&" if urlsplit(authorization_endpoint).query else "?"
    return f"{authorization_endpoint}{separator}{urlencode(query)}"


async def _token_request(
    token_endpoint: str,
    client: OAuthClient,
    form: dict[str, str],
    transport: httpx2.AsyncBaseTransport | None,
) -> tuple[int, dict[str, Any] | None]:
    form = {**form, "client_id": client.client_id}
    auth: httpx2.Auth | None = None
    if client.client_secret is not None and client.auth_method == "client_secret_post":
        form["client_secret"] = client.client_secret
    elif client.client_secret is not None and client.auth_method == "client_secret_basic":
        auth = httpx2.BasicAuth(quote(client.client_id, safe=""), quote(client.client_secret, safe=""))
    status, body, _ = await http_json(
        "POST", token_endpoint, transport=transport, form=form, auth=auth,
        headers={"Accept": "application/json"},
    )
    return status, body


def _parse_token(body: dict[str, Any] | None) -> OAuthToken:
    try:
        token = OAuthToken.model_validate(body)
    except ValidationError:
        raise UpstreamOAuthError("unreachable") from None
    if not token.access_token:
        raise UpstreamOAuthError("unreachable")
    return token


def _expiry(token: OAuthToken) -> float | None:
    return None if token.expires_in is None else calculate_token_expiry(token.expires_in)


async def exchange_code(
    *,
    token_endpoint: str,
    client: OAuthClient,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
    upstream_id: str,
    issuer: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> TokenRecord:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if resource is not None:
        form["resource"] = resource
    status, body = await _token_request(token_endpoint, client, form, transport)
    if status in _BAD_CLIENT_STATUSES:
        raise UpstreamOAuthError("auth_expired")
    if status != 200:
        raise UpstreamOAuthError("unreachable")
    token = _parse_token(body)
    return TokenRecord(
        upstream_id=upstream_id,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=_expiry(token),
        scope=token.scope or "",
        issuer=issuer,
        client_id=client.client_id,
        auth_method=client.auth_method,
        token_endpoint=token_endpoint,
        resource=resource,
        connected_at=time.time(),
    )


class UpstreamTokenProvider:
    """Hands out a fresh bearer for a saved token file, refreshing it when it is about to expire."""

    def __init__(
        self,
        client_source: Callable[[str], OAuthClient],
        *,
        on_invalid_client: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_source = client_source
        self._on_invalid_client = on_invalid_client
        self._clock = clock
        self._transport = transport
        self._locks: dict[str, asyncio.Lock] = {}

    def _stale(self, record: TokenRecord) -> bool:
        return record.expires_at is not None and record.expires_at - self._clock() <= _REFRESH_MARGIN_SECONDS

    async def bearer(self, path: Path, force_refresh: bool = False) -> str:
        record = load_token(path)
        if record is None:
            raise UpstreamOAuthError("not_connected")
        if not force_refresh and not self._stale(record):
            return record.access_token
        seen = record.access_token
        async with self._locks.setdefault(str(path), asyncio.Lock()):
            record = load_token(path)
            if record is None:
                raise UpstreamOAuthError("not_connected")
            # Another call may have refreshed while this one waited for the lock.
            if record.access_token != seen or (not force_refresh and not self._stale(record)):
                return record.access_token
            return (await self._refresh(path, record)).access_token

    async def _refresh(self, path: Path, record: TokenRecord) -> TokenRecord:
        if not record.refresh_token:
            delete_token(path)
            raise UpstreamOAuthError("auth_expired")
        client = self._client_source(record.upstream_id)
        if client.client_id != record.client_id:
            delete_token(path)
            raise UpstreamOAuthError("auth_expired")
        form = {"grant_type": "refresh_token", "refresh_token": record.refresh_token}
        if record.resource is not None:
            form["resource"] = record.resource
        status, body = await _token_request(record.token_endpoint, client, form, self._transport)
        if status in _BAD_CLIENT_STATUSES:
            delete_token(path)
            if body is not None and body.get("error") == "invalid_client" and self._on_invalid_client is not None:
                self._on_invalid_client(record.upstream_id)
            raise UpstreamOAuthError("auth_expired")
        if status != 200:
            raise UpstreamOAuthError("unreachable")
        token = _parse_token(body)
        refreshed = TokenRecord(
            upstream_id=record.upstream_id,
            access_token=token.access_token,
            refresh_token=token.refresh_token or record.refresh_token,
            expires_at=_expiry(token),
            scope=token.scope or record.scope,
            issuer=record.issuer,
            client_id=record.client_id,
            auth_method=record.auth_method,
            token_endpoint=record.token_endpoint,
            resource=record.resource,
            connected_at=record.connected_at,
        )
        save_token(path, refreshed)
        return refreshed
