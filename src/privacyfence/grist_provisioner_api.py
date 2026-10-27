"""Client for the Grist provisioner, the service next to a self-hosted Grist
that sets or reads a person's Grist API key.

PrivacyFence calls it with a scoped provisioning key; it never calls back.
Redirects are never followed, and nothing logs an email, an issued key or the
provisioning key; only a host and a principal id are logged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
_EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9._@-]{1,200}")
_UNEXPECTED = "The Grist provisioner answered with something unexpected."


class GristProvisionerClientError(Exception):
    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class GristProvisionedAccount:
    external_id: str
    user_id: int
    disabled: bool
    has_key: bool
    managed: bool
    key_id: str
    expires_at: int


@dataclass(frozen=True)
class GristIssuedKey:
    key: str = field(repr=False)
    managed: bool
    key_id: str
    expires_at: int


@dataclass(frozen=True)
class GristRelease:
    released: bool
    revoked: bool


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no detail"
    error = body.get("error") if isinstance(body, dict) else None
    return error[:200] if isinstance(error, str) and error else "no detail"


def _unexpected() -> GristProvisionerClientError:
    return GristProvisionerClientError(_UNEXPECTED)


def _bool(data: dict[str, Any], key: str) -> bool:
    """True only for the JSON value ``true``; a missing field is unexpected."""
    if key not in data:
        raise _unexpected()
    return data[key] is True


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _unexpected()
    return value


def _str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise _unexpected()
    return value


def _object(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise _unexpected() from None
    if not isinstance(body, dict):
        raise _unexpected()
    return body


class GristProvisionerClient:
    def __init__(self, base_url: str, key: str) -> None:
        self._base_url = base_url
        self._session = requests.Session()
        self._headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    @property
    def host(self) -> str:
        return urlsplit(self._base_url).netloc

    def __repr__(self) -> str:
        return f"GristProvisionerClient({self.host})"

    def _send(self, method: str, path: str, *, not_found: str | None, **kwargs: Any) -> requests.Response:
        """One request and the error mapping. ``not_found`` is the message for a JSON 404."""
        try:
            response = self._session.request(
                method,
                self._base_url + path,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise GristProvisionerClientError(
                f"Could not reach the Grist provisioner at {self.host}: {type(exc).__name__}"
            ) from exc
        status = response.status_code
        if 200 <= status < 300:
            return response
        detail = _detail(response)

        def fail(message: str) -> GristProvisionerClientError:
            return GristProvisionerClientError(message, status=status, detail=detail)

        if 300 <= status < 400:
            raise fail(
                f"The Grist provisioner answered with a redirect (HTTP {status}). Check --grist-provisioner-url."
            )
        if status == 401:
            raise fail(
                "The Grist provisioner refused the provisioning key (HTTP 401). "
                "Check the key in the daemon's environment."
            )
        if status == 404:
            if detail == "no detail":
                raise fail("This server is not a Grist provisioner (HTTP 404). Check --grist-provisioner-url.")
            if not_found is not None:
                raise fail(not_found)
            raise fail(f"The Grist provisioner refused the request (HTTP 404): {detail}")
        if status in (403, 409):
            raise fail(f"The Grist provisioner refused this account (HTTP {status}): {detail}")
        if status >= 500:
            raise fail(f"The Grist provisioner answered with an error (HTTP {status}).")
        raise fail(f"The Grist provisioner refused the request (HTTP {status}): {detail}")

    @staticmethod
    def _check_id(external_id: str) -> str:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise GristProvisionerClientError(f"Not a Grist provisioning id: {external_id!r}")
        return external_id

    def check(self) -> int:
        logger.info("Grist provisioning at %s: %s %s", self.host, "check", "-")
        response = self._send("GET", "/api/provisioning/self", not_found=None)
        return _int(_object(response), "accounts")

    def upsert_account(self, external_id: str, email: str) -> GristProvisionedAccount:
        self._check_id(external_id)
        logger.info("Grist provisioning at %s: %s %s", self.host, "upsert_account", external_id)
        response = self._send(
            "PUT",
            f"/api/provisioning/accounts/{external_id}",
            not_found="Grist has no account for this person yet (HTTP 404).",
            json={"email": email},
        )
        data = _object(response)
        return GristProvisionedAccount(
            external_id=_str(data, "external_id"),
            user_id=_int(data, "user_id"),
            disabled=_bool(data, "disabled"),
            has_key=_bool(data, "has_key"),
            managed=_bool(data, "managed"),
            key_id=_str(data, "key_id"),
            expires_at=_int(data, "expires_at"),
        )

    def issue_key(self, external_id: str) -> GristIssuedKey:
        self._check_id(external_id)
        logger.info("Grist provisioning at %s: %s %s", self.host, "issue_key", external_id)
        response = self._send(
            "POST",
            f"/api/provisioning/accounts/{external_id}/key",
            not_found="The Grist provisioner has no link for this person (HTTP 404).",
            json={},
        )
        data = _object(response)
        key = _str(data, "key")
        if not key:
            raise _unexpected()
        return GristIssuedKey(
            key=key,
            managed=_bool(data, "managed"),
            key_id=_str(data, "key_id"),
            expires_at=_int(data, "expires_at"),
        )

    def release_account(self, external_id: str) -> GristRelease:
        self._check_id(external_id)
        logger.info("Grist provisioning at %s: %s %s", self.host, "release_account", external_id)
        try:
            response = self._send(
                "DELETE", f"/api/provisioning/accounts/{external_id}", not_found="no link"
            )
        except GristProvisionerClientError as exc:
            if exc.status == 404 and exc.detail != "no detail":
                return GristRelease(False, False)
            raise
        data = _object(response)
        return GristRelease(released=_bool(data, "released"), revoked=_bool(data, "revoked"))
