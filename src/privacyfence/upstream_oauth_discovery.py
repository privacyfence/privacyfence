"""OAuth discovery and client registration for hosted upstream MCP servers.

ADR 0330 says why. ``discover`` follows the MCP authorization spec (RFC 9728 protected-resource
metadata, then RFC 8414 authorization-server metadata) for one declared upstream, and
``RegistrationStore`` yields the OAuth client PrivacyFence uses at that server: a declared
``client_id``, else a dynamic registration (RFC 7591) cached on disk.

This module makes server-side requests to addresses a remote server names, so every URL it is told
to fetch passes ``_checked_url`` first (https, no user name or fragment, no private address, the
issuer's own host), and the issuer's host must resolve to public addresses. The connection resolves
the name again later, so a name that answers differently the second time is a residual risk that
ADR 0330 records.

Nothing here logs or quotes a response body, a token or a secret, and there is no module-level
mutable state: the per-upstream locks are attributes of the store.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    validate_metadata_issuer,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from pydantic import ValidationError

from .secure_files import atomic_write_json, secure_mkdir
from .upstream_mcp_config import UpstreamServer
from .upstream_oauth_tokens import OAuthClient, UpstreamOAuthError, http_json

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_REGISTRATION_FILE_VERSION = 1
_SECRET_EXPIRY_MARGIN_SECONDS = 60
_PRIVATE_SUFFIXES = (".local", ".internal", ".home.arpa", ".lan")
_LOOPBACK_NAMES = ("localhost",)


@dataclass(frozen=True)
class OAuthEndpoints:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    scope: str | None
    token_auth_methods: tuple[str, ...]
    iss_supported: bool
    loopback: bool


# --------------------------------------------------------------------------- #
# The URL and address rules
# --------------------------------------------------------------------------- #


def _split(url: str) -> tuple[str, str, str]:
    """``(scheme, host, host[:port])`` in lower case; any parse problem is ``invalid_metadata``."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
        userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        raise UpstreamOAuthError("invalid_metadata") from None
    if not host or userinfo or parts.fragment:
        raise UpstreamOAuthError("invalid_metadata")
    shown = f"[{host}]" if ":" in host else host
    return parts.scheme.lower(), host, shown if port is None else f"{shown}:{port}"


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Global and neither multicast nor reserved (``is_global`` alone admits some multicast ranges)."""
    return address.is_global and not address.is_multicast and not address.is_reserved


def _is_loopback_host(host: str) -> bool:
    literal = _ip_literal(host)
    return host in _LOOPBACK_NAMES or (literal is not None and literal.is_loopback)


def _checked_url(url: str, *, host: str | None, loopback: bool) -> None:
    """Refuse a metadata URL a server must not be sent to (``invalid_metadata``).

    ``loopback`` (the upstream itself is an ``http://`` loopback address) relaxes the rule only for
    a URL whose own host is ``localhost`` or a loopback address; every other host must still be
    https and public.
    """
    scheme, url_host, netloc = _split(url)
    relaxed = loopback and _is_loopback_host(url_host)
    if scheme != "https" and not (relaxed and scheme == "http"):
        raise UpstreamOAuthError("invalid_metadata")
    if not relaxed:
        literal = _ip_literal(url_host)
        if literal is not None:
            if not _is_public(literal):
                raise UpstreamOAuthError("invalid_metadata")
        elif url_host.count(".") < 1 or url_host.endswith(_PRIVATE_SUFFIXES) or url_host.startswith("."):
            raise UpstreamOAuthError("invalid_metadata")
    if host is not None and netloc != host:
        raise UpstreamOAuthError("invalid_metadata")


async def _resolve(host: str) -> list[str]:
    """The addresses the name resolves to (tests patch this)."""
    found = await asyncio.to_thread(socket.getaddrinfo, host, None)
    return [str(entry[4][0]) for entry in found]


async def _require_public(host: str, loopback: bool) -> None:
    """The issuer's host must resolve to public addresses only."""
    if loopback and _is_loopback_host(host):
        return
    try:
        addresses = await _resolve(host)
    except OSError:
        raise UpstreamOAuthError("unreachable") from None
    for address in addresses:
        literal = _ip_literal(address)
        if literal is None or not _is_public(literal):
            raise UpstreamOAuthError("invalid_metadata")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "PrivacyFence", "version": "0"},
    },
}


async def _resource_metadata_hint(upstream: UpstreamServer, transport: httpx2.AsyncBaseTransport | None) -> str | None:
    status, _, headers = await http_json(
        "POST",
        upstream.url,
        transport=transport,
        headers={"Accept": "application/json, text/event-stream"},
        json_body=_INITIALIZE,
    )
    if status != 401:
        return None
    return extract_resource_metadata_from_www_auth(httpx2.Response(status, headers=headers))


async def _first_document(
    urls: list[str], *, host: str, loopback: bool, transport: httpx2.AsyncBaseTransport | None, parse: Any,
) -> Any:
    """The first URL (that passes the URL rule) whose answer is 200 and parses; else ``None``."""
    for url in urls:
        try:
            _checked_url(url, host=host, loopback=loopback)
        except UpstreamOAuthError:
            continue
        status, body, _ = await http_json("GET", url, transport=transport, headers={"Accept": "application/json"})
        if status != 200 or body is None:
            continue
        try:
            return parse(body)
        except (ValidationError, OAuthFlowError):
            continue
    return None


def _same(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


async def discover(upstream: UpstreamServer, *, transport: httpx2.AsyncBaseTransport | None = None) -> OAuthEndpoints:
    scheme, upstream_host, upstream_netloc = _split(upstream.url)
    loopback = scheme == "http"

    hint = await _resource_metadata_hint(upstream, transport)
    prm: ProtectedResourceMetadata | None = await _first_document(
        build_protected_resource_metadata_discovery_urls(hint, upstream.url),
        host=upstream_netloc,
        loopback=loopback,
        transport=transport,
        parse=ProtectedResourceMetadata.model_validate,
    )
    if prm is None:
        raise UpstreamOAuthError("invalid_metadata")
    if not check_resource_allowed(resource_url_from_server_url(upstream.url), str(prm.resource)):
        raise UpstreamOAuthError("invalid_metadata")

    listed = [str(server) for server in prm.authorization_servers]
    if upstream.oauth.issuer is not None:
        pinned = [value for value in listed if _same(value, upstream.oauth.issuer)]
        if not pinned:
            raise UpstreamOAuthError("invalid_metadata")
        issuer = pinned[0]
    else:
        issuer = listed[0]
    _checked_url(issuer, host=None, loopback=loopback)
    _, issuer_host, issuer_netloc = _split(issuer)
    await _require_public(issuer_host, loopback)

    metadata: OAuthMetadata | None = await _first_document(
        build_oauth_authorization_server_metadata_discovery_urls(issuer, upstream.url),
        host=issuer_netloc,
        loopback=loopback,
        transport=transport,
        parse=_parse_server_metadata(issuer),
    )
    if metadata is None:
        raise UpstreamOAuthError("invalid_metadata")

    authorization_endpoint = str(metadata.authorization_endpoint)
    token_endpoint = str(metadata.token_endpoint)
    registration_endpoint = None if metadata.registration_endpoint is None else str(metadata.registration_endpoint)
    for endpoint in (authorization_endpoint, token_endpoint, registration_endpoint):
        if endpoint is not None:
            _checked_url(endpoint, host=issuer_netloc, loopback=loopback)
    if "S256" not in (metadata.code_challenge_methods_supported or ()):
        raise UpstreamOAuthError("invalid_metadata")

    if upstream.oauth.scopes:
        scope: str | None = " ".join(upstream.oauth.scopes)
    elif prm.scopes_supported:
        scope = " ".join(prm.scopes_supported)
    else:
        scope = None

    return OAuthEndpoints(
        issuer=str(metadata.issuer),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        resource=str(prm.resource),
        scope=scope,
        token_auth_methods=tuple(metadata.token_endpoint_auth_methods_supported or ("client_secret_basic",)),
        iss_supported=bool(metadata.authorization_response_iss_parameter_supported),
        loopback=loopback,
    )


def _parse_server_metadata(issuer: str) -> Any:
    def parse(body: dict[str, Any]) -> OAuthMetadata:
        metadata = OAuthMetadata.model_validate(body)
        validate_metadata_issuer(metadata, issuer)
        return metadata

    return parse


# --------------------------------------------------------------------------- #
# Client registration
# --------------------------------------------------------------------------- #


class RegistrationStore:
    """The OAuth client PrivacyFence uses at each upstream; dynamic registrations are cached on disk."""

    def __init__(
        self,
        directory: Path,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        upstreams: Iterable[UpstreamServer] = (),
    ) -> None:
        self._directory = Path(directory)
        self._transport = transport
        self._locks: dict[str, asyncio.Lock] = {}
        # What ``client_for`` may answer without the network: the declared upstreams, and the token
        # endpoint methods last seen by ``ensure_client`` (a declared secret's auth method).
        self._upstreams = {upstream.id: upstream for upstream in upstreams}
        self._methods: dict[str, tuple[str, ...]] = {}

    def _path(self, upstream_id: str) -> Path:
        return self._directory / f"{upstream_id}.json"

    def _declared(self, upstream: UpstreamServer, token_auth_methods: tuple[str, ...] | None) -> OAuthClient | None:
        settings = upstream.oauth
        if settings.client_id is None:
            return None
        secret: str | None = None
        if settings.client_secret_env is not None:
            secret = os.environ.get(settings.client_secret_env) or None
            if secret is None:
                raise UpstreamOAuthError("not_configured")
        if secret is None:
            method = "none"
        elif token_auth_methods is not None and "client_secret_post" in token_auth_methods:
            method = "client_secret_post"
        else:
            method = "client_secret_basic"
        return OAuthClient(client_id=settings.client_id, client_secret=secret, auth_method=method)

    async def ensure_client(
        self, upstream: UpstreamServer, endpoints: OAuthEndpoints, redirect_uri: str,
    ) -> OAuthClient:
        self._upstreams[upstream.id] = upstream
        self._methods[upstream.id] = endpoints.token_auth_methods
        declared = self._declared(upstream, endpoints.token_auth_methods)
        if declared is not None:
            return declared
        lock = self._locks.setdefault(upstream.id, asyncio.Lock())
        async with lock:
            cached = self._cached(upstream.id, endpoints.issuer, redirect_uri)
            if cached is not None:
                return cached
            return await self._register(upstream, endpoints, redirect_uri)

    def client_for(self, upstream_id: str) -> OAuthClient:
        """The declared or cached client, with no network; ``not_configured`` when there is none."""
        upstream = self._upstreams.get(upstream_id)
        if upstream is not None:
            declared = self._declared(upstream, self._methods.get(upstream_id))
            if declared is not None:
                return declared
        record = self._read(upstream_id)
        if record is None:
            raise UpstreamOAuthError("not_configured")
        return OAuthClient(
            client_id=record["client_id"], client_secret=record["client_secret"], auth_method=record["auth_method"],
        )

    def forget(self, upstream_id: str) -> None:
        self._path(upstream_id).unlink(missing_ok=True)

    # -- the cache ----------------------------------------------------------- #

    def _read(self, upstream_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(upstream_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != _REGISTRATION_FILE_VERSION:
            return None
        if not isinstance(data.get("client_id"), str) or not isinstance(data.get("auth_method"), str):
            return None
        secret = data.get("client_secret")
        if secret is not None and not isinstance(secret, str):
            return None
        return data

    def _cached(self, upstream_id: str, issuer: str, redirect_uri: str) -> OAuthClient | None:
        record = self._read(upstream_id)
        if record is None or record.get("issuer") != issuer or record.get("redirect_uri") != redirect_uri:
            return None
        expires = record.get("client_secret_expires_at")
        if isinstance(expires, (int, float)) and expires != 0 and expires <= time.time() + _SECRET_EXPIRY_MARGIN_SECONDS:
            return None
        return OAuthClient(
            client_id=record["client_id"], client_secret=record.get("client_secret"), auth_method=record["auth_method"],
        )

    def _write(self, record: dict[str, Any]) -> None:
        secure_mkdir(self._directory)
        atomic_write_json(self._path(record["upstream_id"]), record)

    async def _register(self, upstream: UpstreamServer, endpoints: OAuthEndpoints, redirect_uri: str) -> OAuthClient:
        if endpoints.registration_endpoint is None:
            raise UpstreamOAuthError("registration_failed")
        methods = endpoints.token_auth_methods
        method = "none" if "none" in methods else "client_secret_post" if "client_secret_post" in methods else "client_secret_basic"
        body: dict[str, Any] = {
            "client_name": "PrivacyFence",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if endpoints.scope is not None:
            body["scope"] = endpoints.scope
        status, answer, _ = await http_json(
            "POST", endpoints.registration_endpoint, transport=self._transport,
            headers={"Accept": "application/json"}, json_body=body,
        )
        try:
            if status not in (200, 201):
                raise ValueError
            info = OAuthClientInformationFull.model_validate(answer)  # a missing or empty client_id fails here
            if info.redirect_uris and redirect_uri not in {str(uri) for uri in info.redirect_uris}:
                raise ValueError
        except (ValueError, ValidationError):
            logger.warning("Upstream %s registration failed: registration_failed", upstream.id)
            raise UpstreamOAuthError("registration_failed") from None
        # The method actually in force: what the server echoed, else what was asked for.
        granted = info.token_endpoint_auth_method or method
        if info.client_secret is None:
            granted = "none"
        elif granted == "none":
            granted = "client_secret_basic"
        self._write(
            {
                "version": _REGISTRATION_FILE_VERSION,
                "upstream_id": upstream.id,
                "issuer": endpoints.issuer,
                "redirect_uri": redirect_uri,
                "client_id": info.client_id,
                "client_secret": info.client_secret,
                "client_secret_expires_at": info.client_secret_expires_at,
                "auth_method": granted,
                "registered_at": time.time(),
            }
        )
        return OAuthClient(client_id=info.client_id, client_secret=info.client_secret, auth_method=granted)
