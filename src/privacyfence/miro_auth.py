"""How PrivacyFence signs in to Miro.

- The authorization code flow of a confidential client, with no PKCE (Miro
  offers none); the ``state`` is checked by the loopback.
- Expiring tokens only, refreshed and rotated: a refresh returns a new refresh
  token and voids the old one.
- One token per person, for the one team chosen at sign-in.
- Redirects are never followed, and nothing logs a token, a code or the client
  secret.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

from . import oauth_loopback
from .oauth_loopback import OAuthLoopbackError
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

MIRO_OAUTH_PORT = 53686
MIRO_REDIRECT_PATH = "/callback"
AUTHORIZE_URL = "https://miro.com/oauth/authorize"
TOKEN_URL = "https://api.miro.com/v1/oauth/token"  # nosec B105  # an endpoint URL, not a credential
REQUIRED_SCOPES = ("boards:read", "boards:write")
NOT_AUTHENTICATED = "Miro is not authenticated. Use Authenticate… in PrivacyFence Settings."

_REFRESH_MARGIN_SECONDS = 60
_DETAIL_LIMIT = 200
_UNREADABLE = (
    "Miro's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again."
)
_EXPIRED = (
    "Your Miro sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again."
)
_NEVER_EXPIRES = (
    "Miro returned a token that never expires. Create the PrivacyFence app in Miro with "
    "\"Expire user authorization token\" turned on; see the Miro setup guide."
)


class MiroClientError(Exception):
    """Any Miro failure the caller can show a person."""


# --------------------------------------------------------------------------- #
# Bundle section
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MiroOAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)


def _text(section: dict[str, Any], key: str) -> str:
    return str(section.get(key) or "").strip()


def bundle_settings(section: dict[str, Any]) -> MiroOAuthConfig:
    """Read the organization bundle's ``miro`` section."""
    client_id = _text(section, "client_id")
    client_secret = _text(section, "client_secret")
    if not client_id and not client_secret:
        raise MiroClientError("Miro organization config not installed")
    if not client_id or not client_secret:
        raise MiroClientError(
            "Miro organization config is incomplete: client_id and client_secret go together."
        )
    return MiroOAuthConfig(client_id=client_id, client_secret=client_secret)


# --------------------------------------------------------------------------- #
# Credential file
# --------------------------------------------------------------------------- #


def save_token_file(path: str, record: dict[str, Any]) -> None:
    """The only place a Miro credential is written."""
    atomic_write_json(path, record)


def load_token_file(path: str) -> dict[str, Any]:
    """The saved credential record, or MiroClientError."""
    if not os.path.exists(path):
        raise MiroClientError(NOT_AUTHENTICATED)
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        raise MiroClientError(_UNREADABLE) from None
    if not isinstance(record, dict):
        raise MiroClientError(_UNREADABLE)
    for key in ("access_token", "refresh_token", "team_id"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise MiroClientError(NOT_AUTHENTICATED)
    if not _is_number(record.get("expires_at")):
        raise MiroClientError(NOT_AUTHENTICATED)
    return {**record, "user_id": record.get("user_id") or "", "scope": record.get("scope") or ""}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_number(value: Any) -> bool:
    return _is_number(value) and math.isfinite(value) and value > 0


# --------------------------------------------------------------------------- #
# Token requests
# --------------------------------------------------------------------------- #


def _token_request(form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """POST the form to Miro's token endpoint; the status and the JSON object it answered with."""
    try:
        response = requests.post(TOKEN_URL, data=form, timeout=30, allow_redirects=False)
    except requests.RequestException as exc:
        raise MiroClientError(f"Could not reach Miro's sign-in server: {type(exc).__name__}") from None
    status = response.status_code
    if 300 <= status < 400:
        raise MiroClientError(f"Miro's sign-in server answered with a redirect (HTTP {status}).")
    if status >= 500:
        raise MiroClientError(f"Miro's sign-in server answered with an error (HTTP {status}).")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise MiroClientError(
            f"Miro's sign-in server answered with something other than JSON (HTTP {status})."
        )
    return status, body


def _message(body: dict[str, Any]) -> str:
    for key in ("message", "error_description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:_DETAIL_LIMIT]
    return "no detail"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def _incomplete(key: str) -> MiroClientError:
    return MiroClientError(f"Miro returned an incomplete sign-in answer (no {key}).")


def exchange_code(config: MiroOAuthConfig, code: str, redirect_uri: str) -> dict[str, Any]:
    """Trade an authorization code for the credential record (not yet saved)."""
    status, body = _token_request({
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    })
    if status >= 400:
        raise MiroClientError(f"Miro sign-in failed: {_message(body)}")
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise _incomplete("access_token")
    team_id = body.get("team_id")
    if not isinstance(team_id, (str, int)) or isinstance(team_id, bool) or not str(team_id):
        raise _incomplete("team_id")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(refresh_token, str) or not refresh_token or expires_in is None:
        raise MiroClientError(_NEVER_EXPIRES)
    if not _positive_number(expires_in):
        raise _incomplete("expires_in")
    scope = body.get("scope")
    scope = scope if isinstance(scope, str) else ""
    if scope.strip():
        granted = scope.split()
        missing = [name for name in REQUIRED_SCOPES if name not in granted]
        if missing:
            raise MiroClientError(
                f"The Miro app is missing the permission(s) {', '.join(missing)}. "
                "Turn on boards:read and boards:write in the app's settings in Miro, then sign in again."
            )
    logger.info("Miro sign-in complete")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + float(expires_in),
        "team_id": str(team_id),
        "user_id": str(body.get("user_id") or ""),
        "scope": scope,
    }


def refresh(config: MiroOAuthConfig, record: dict[str, Any]) -> dict[str, Any]:
    """The record with a fresh access token (and the rotated refresh token, if one came back)."""
    status, body = _token_request({
        "grant_type": "refresh_token",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "refresh_token": record["refresh_token"],
    })
    if status in (400, 401):
        raise MiroClientError(_EXPIRED)
    if status >= 400:
        raise MiroClientError(f"Miro sign-in refresh failed: {_message(body)}")
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise _incomplete("access_token")
    expires_in = body.get("expires_in")
    if not isinstance(expires_in, (int, float)) or not _positive_number(expires_in):
        raise _incomplete("expires_in")
    new_refresh = body.get("refresh_token")
    return {
        **record,
        "access_token": access_token,
        "refresh_token": new_refresh if isinstance(new_refresh, str) and new_refresh else record["refresh_token"],
        "expires_at": time.time() + float(expires_in),
    }


# --------------------------------------------------------------------------- #
# Token provider
# --------------------------------------------------------------------------- #


class MiroTokenProvider:
    """Supplies a valid access token, refreshing and saving it as needed."""

    def __init__(self, config: MiroOAuthConfig, token_file: str, record: dict[str, Any]) -> None:
        self._config = config
        self._token_file = token_file
        self._record = dict(record)
        self._lock = threading.Lock()

    @property
    def team_id(self) -> str:
        return str(self._record["team_id"])

    def _valid(self) -> bool:
        return float(self._record["expires_at"]) - _REFRESH_MARGIN_SECONDS > time.time()

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._valid():
                return self._record["access_token"]
            # A refresh voids the old refresh token. Another provider may have refreshed
            # since this one last did, so take the newest record before using the token.
            try:
                latest = load_token_file(self._token_file)
            except MiroClientError:
                latest = None
            if latest is not None and latest["refresh_token"] != self._record["refresh_token"]:
                self._record = latest
                if not force_refresh and self._valid():
                    return self._record["access_token"]
            self._record = refresh(self._config, self._record)
            save_token_file(self._token_file, self._record)
            return self._record["access_token"]

    def __repr__(self) -> str:
        return "MiroTokenProvider()"


# --------------------------------------------------------------------------- #
# Interactive sign-in
# --------------------------------------------------------------------------- #


def authorize_interactive(config: MiroOAuthConfig, token_file: str) -> dict[str, Any]:
    """Sign in through the browser (loopback redirect), save and return the record."""

    def _build(redirect_uri: str, state: str, _challenge: str) -> str:
        return build_authorize_url(config.client_id, redirect_uri, state)

    def _exchange(code: str, redirect_uri: str, _verifier: str) -> dict[str, Any]:
        return exchange_code(config, code, redirect_uri)

    try:
        record = oauth_loopback.run_browser_oauth(
            _build, _exchange, port=MIRO_OAUTH_PORT, path=MIRO_REDIRECT_PATH, redirect_host="localhost",
        )
    except OAuthLoopbackError as exc:
        raise MiroClientError(f"Miro sign-in failed: {exc}") from exc
    save_token_file(token_file, record)
    return record
