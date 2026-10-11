"""Peek REST client and Peek's device sign-in.

A Peek token is only ever sent to the server it was issued for (ADR 0147):
the credential file records that server, every request goes to it, and
redirects are never followed. Nothing here logs a token, a device code, a
page password, page content or a comment body; log lines carry the host and
counts only.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from . import secure_files

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
VISIBILITIES = ("public", "password", "private")
PASSWORD_MAX_BYTES = 72
# PrivacyFence's own ceiling on a page it reads; the server's own max_upload
# (2 MiB by default) still applies.
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_RENAMED_PREFIX = "page was renamed; use slug "
_LOGIN_STATUSES = ("pending", "approved", "denied", "expired", "consumed")
_REAUTH = "Use Authenticate… in PrivacyFence Settings"
_UNEXPECTED_SHAPE = "Peek answered with something unexpected. Check the Peek server address."


class PeekClientError(Exception):
    """A Peek problem: bad input, a refused request, or an unusable answer.

    ``status`` is the HTTP status when the error came from an answer, else
    ``None``.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- addresses and slugs ----------------------------------------------------


def normalize_base_url(raw: str) -> str:
    """Return ``scheme://host[:port][/path]`` for a Peek server address.

    Only ``https`` is accepted, plus ``http`` to a loopback host.
    """
    text = raw.strip().rstrip("/")
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        hostname, port, parts = None, None, urlsplit("")
    if not hostname:
        raise PeekClientError("Enter the Peek server address, such as https://peek.example.com.")
    if parts.scheme != "https" and not (parts.scheme == "http" and hostname in _LOOPBACK_HOSTS):
        raise PeekClientError("The Peek server address must start with https:// (http:// is allowed only for localhost).")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise PeekClientError("The Peek server address must not contain a user name, password, query or fragment.")
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"


def validate_slug(slug: str) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise PeekClientError(f"Not a Peek page slug: {slug!r}")
    return slug


def bundle_base_url(section: dict[str, Any], *, org_mode: bool) -> str:
    """The Peek server an organization bundle pins, or ``""`` when it pins none."""
    raw = section.get("base_url") or ""
    url = ""
    if raw:
        try:
            url = normalize_base_url(str(raw))
        except PeekClientError as exc:
            raise PeekClientError(f"Peek organization config is not usable: {exc}") from exc
    if org_mode and not url:
        raise PeekClientError("Peek organization config not installed")
    return url


# --- HTTP -------------------------------------------------------------------


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no detail"
    error = body.get("error") if isinstance(body, dict) else None
    return error[:200] if isinstance(error, str) and error else "no detail"


def _send(session: requests.Session, method: str, url: str, host: str, **kwargs: Any) -> requests.Response:
    """One request and the error mapping shared by sign-in and the client."""
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False, **kwargs)
    except requests.RequestException as exc:
        raise PeekClientError(f"Could not reach Peek at {host}: {type(exc).__name__}") from exc
    status = response.status_code
    if 300 <= status < 400:
        raise PeekClientError(
            f"Peek answered with a redirect (HTTP {status}). Check the Peek server address.", status=status
        )
    if status == 401:
        raise PeekClientError(
            f"Peek refused the saved sign-in (HTTP 401). {_REAUTH} to connect again.", status=status
        )
    if status == 403:
        raise PeekClientError(f"Peek refused the request (HTTP 403): {_detail(response)}", status=status)
    if status == 404:
        detail = _detail(response)
        if detail.startswith(_RENAMED_PREFIX):
            new_slug = detail[len(_RENAMED_PREFIX):]
            try:
                validate_slug(new_slug)
            except PeekClientError:
                pass
            else:
                raise PeekClientError(
                    f"Peek says this page was renamed; its slug is now {new_slug}. Use that slug.", status=status
                )
        raise PeekClientError(
            "Peek found no such page (HTTP 404). It may have been renamed or deleted: "
            "call peek_list_pages to see the current slugs.",
            status=status,
        )
    if not 200 <= status < 300:
        raise PeekClientError(f"Peek API error (HTTP {status}): {_detail(response)}", status=status)
    try:
        response.json()
    except ValueError as exc:
        raise PeekClientError(
            f"Peek answered with something other than JSON (HTTP {status}). Check the Peek server address.",
            status=status,
        ) from exc
    return response


def _host_of(url: str) -> str:
    return urlsplit(url).netloc


def _post_unauthenticated(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = base_url + path
    with requests.Session() as session:
        try:
            response = _send(session, "POST", url, _host_of(url), json=body)
        except PeekClientError as exc:
            if exc.status in (404, 405):
                raise PeekClientError(
                    f"Peek has no device sign-in at this address (HTTP {exc.status}). Check the Peek server address.",
                    status=exc.status,
                ) from exc
            raise
    return _as_dict(response.json())


# --- parsing helpers ----------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise PeekClientError(_UNEXPECTED_SHAPE)
    return value


def _str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _iso(item: dict[str, Any], key: str) -> str:
    """Peek's Unix seconds as ``2026-10-10T11:07:00Z`` (a text value passes through)."""
    value = item.get(key)
    if isinstance(value, str):
        return value
    seconds = _int(item, key)
    if not seconds:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- device sign-in -------------------------------------------------------------


@dataclass(frozen=True)
class PeekDeviceLogin:
    device_code: str = field(repr=False)
    user_code: str
    verification_url: str
    interval: int
    expires_at: float


@dataclass(frozen=True)
class PeekLoginPoll:
    status: str
    token: str = field(default="", repr=False)


def start_device_login(base_url: str) -> PeekDeviceLogin:
    data = _post_unauthenticated(base_url, "/api/cli/login/start", {})
    device_code = _str(data, "device_code")
    user_code = _str(data, "user_code")
    verification_url = _str(data, "verification_url")
    expires_in = _int(data, "expires_in")
    if not (device_code and user_code and verification_url and expires_in):
        raise PeekClientError("Peek answered the sign-in request without a sign-in code.")
    if not verification_url.startswith(base_url + "/"):
        raise PeekClientError("Peek answered with a sign-in address on another server. Check the Peek server address.")
    interval = _int(data, "interval")
    return PeekDeviceLogin(
        device_code=device_code,
        user_code=user_code,
        verification_url=verification_url,
        interval=interval if interval >= 1 else 2,
        expires_at=time.time() + expires_in,
    )


def poll_device_login(base_url: str, device_code: str) -> PeekLoginPoll:
    data = _post_unauthenticated(base_url, "/api/cli/login/poll", {"device_code": device_code})
    status = _str(data, "status")
    token = _str(data, "token")
    if status not in _LOGIN_STATUSES or (status == "approved" and not token):
        raise PeekClientError("Peek answered the sign-in check with something unexpected.")
    return PeekLoginPoll(status=status, token=token if status == "approved" else "")


# --- credential file --------------------------------------------------------------


def save_token_file(path: str, base_url: str, token: str) -> None:
    secure_files.atomic_write_json(path, {"base_url": base_url, "token": token})


def load_token_file(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError as exc:
        raise PeekClientError("Peek is not authenticated. Use Authenticate… in PrivacyFence Settings.") from exc
    except OSError as exc:
        raise _unreadable() from exc
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise _unreadable() from exc
    base_url = record.get("base_url") if isinstance(record, dict) else None
    token = record.get("token") if isinstance(record, dict) else None
    if not (isinstance(base_url, str) and base_url and isinstance(token, str) and token):
        raise _unreadable()
    try:
        return {"base_url": normalize_base_url(base_url), "token": token}
    except PeekClientError as exc:
        raise _unreadable() from exc


def _unreadable() -> PeekClientError:
    return PeekClientError(f"Peek's saved sign-in could not be read. {_REAUTH} to connect again.")


def resolve_server(pinned_base_url: str, record: dict[str, str]) -> str:
    if pinned_base_url and pinned_base_url != record["base_url"]:
        raise PeekClientError(
            "Peek was connected to a different server than your organization uses. "
            f"{_REAUTH} to connect again."
        )
    return record["base_url"]


# --- results ------------------------------------------------------------------------


@dataclass(frozen=True)
class PeekPage:
    slug: str
    name: str
    owner: str
    size_bytes: int
    visibility: str  # may also be "restricted"
    url: str
    created_at: str
    version: int  # the latest version


@dataclass(frozen=True)
class PeekComment:
    id: int
    author: str
    body: str
    anchor_kind: str
    selector: str
    element_text: str
    created_at: str
    version: int


@dataclass(frozen=True)
class PeekVisit:
    """One visit. Peek also returns each visit's IP address and user agent;
    they are deliberately never carried (ADR 0149)."""

    visitor_name: str
    visited_at: str


@dataclass(frozen=True)
class PeekStats:
    slug: str
    name: str
    total_visits: int
    unique_visitors: int
    recent: list[PeekVisit] = field(default_factory=list)


@dataclass(frozen=True)
class PeekUploadResult:
    slug: str
    url: str
    visibility: str
    version: int
    version_url: str


@dataclass(frozen=True)
class PeekRenameResult:
    slug: str
    url: str
    name: str


def _page(item: Any) -> PeekPage:
    d = _as_dict(item)
    return PeekPage(
        slug=_str(d, "slug"),
        name=_str(d, "name"),
        owner=_str(d, "owner"),
        size_bytes=_int(d, "size"),
        visibility=_str(d, "visibility"),
        url=_str(d, "url"),
        created_at=_iso(d, "created_at"),
        version=_int(d, "version"),
    )


def _comment(item: Any) -> PeekComment:
    d = _as_dict(item)
    return PeekComment(
        id=_int(d, "id"),
        author=_str(d, "author"),
        body=_str(d, "body"),
        anchor_kind=_str(d, "anchor_kind"),
        selector=_str(d, "selector"),
        element_text=_str(d, "element_text"),
        created_at=_iso(d, "created_at"),
        version=_int(d, "version"),
    )


def _check_visibility(visibility: str, password: str) -> None:
    if visibility not in VISIBILITIES:
        raise PeekClientError("visibility must be public, password or private.")
    if (visibility == "password") != bool(password) or len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise PeekClientError("A password of 1 to 72 bytes goes with password visibility, and only with it.")
    if password != password.strip():
        raise PeekClientError("A Peek password cannot start or end with a space.")


# --- client ----------------------------------------------------------------------------


class PeekClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def host(self) -> str:
        return _host_of(self._base_url)

    def __repr__(self) -> str:
        return f"PeekClient({self.host})"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """The single choke point: adds the bearer token, returns parsed JSON."""
        headers = {"Authorization": f"Bearer {self._token}"}
        response = _send(self._session, method, self._base_url + path, self.host, headers=headers, **kwargs)
        return response.json()

    def check_connection(self) -> str:
        self._request("GET", "/api/uploads")
        return self.host

    def list_pages(self) -> list[PeekPage]:
        pages = [_page(item) for item in _as_list(self._request("GET", "/api/uploads"))]
        logger.debug("Peek at %s listed %d page(s)", self.host, len(pages))
        return sorted(pages, key=lambda page: page.created_at, reverse=True)

    def get_page(self, slug: str) -> PeekPage:
        validate_slug(slug)
        for page in self.list_pages():
            if page.slug == slug:
                return page
        raise PeekClientError(
            f"No Peek page {slug!r} among the pages this account can manage. "
            "It may have been renamed or deleted: call peek_list_pages to see the current slugs."
        )

    def get_comments(self, slug: str, version: int = 0) -> list[PeekComment]:
        validate_slug(slug)
        kwargs: dict[str, Any] = {"params": {"v": str(version)}} if version > 0 else {}
        try:
            data = self._request("GET", f"/api/uploads/{slug}/comments", **kwargs)
        except PeekClientError as exc:
            if version > 0 and exc.status == 404:
                raise PeekClientError(
                    f"Peek has no version {version} of this page (HTTP 404). Retention may have removed it.",
                    status=404,
                ) from exc
            raise
        comments = sorted((_comment(item) for item in _as_list(data)), key=lambda c: (c.created_at, c.id))
        logger.debug("Peek at %s returned %d comment(s)", self.host, len(comments))
        return comments

    def get_stats(self, slug: str) -> PeekStats:
        validate_slug(slug)
        data = _as_dict(self._request("GET", f"/api/uploads/{slug}/stats"))
        recent = data.get("recent")
        visits = [
            PeekVisit(visitor_name=_str(_as_dict(v), "name"), visited_at=_iso(_as_dict(v), "visited_at"))
            for v in (recent if isinstance(recent, list) else [])
        ]
        return PeekStats(
            slug=_str(data, "slug"),
            name=_str(data, "name"),
            total_visits=_int(data, "total_visits"),
            unique_visitors=_int(data, "unique_visitors"),
            recent=visits,
        )

    def upload_page(
        self, data: bytes, name: str, visibility: str, password: str, slug: str = ""
    ) -> PeekUploadResult:
        if slug:
            validate_slug(slug)
            if visibility or password:
                raise PeekClientError("A new version keeps the page's visibility; change it with set_visibility.")
            form = {"slug": slug}
        else:
            _check_visibility(visibility, password)
            form = {"visibility": visibility}
            if visibility == "password":
                form["password"] = password
        result = _as_dict(
            self._request("POST", "/api/upload", files={"file": (name, data, "text/html")}, data=form)
        )
        return PeekUploadResult(
            slug=_str(result, "slug"),
            url=_str(result, "url"),
            visibility=_str(result, "visibility"),
            version=_int(result, "version"),
            version_url=_str(result, "version_url"),
        )

    def set_visibility(self, slug: str, visibility: str, password: str) -> str:
        validate_slug(slug)
        _check_visibility(visibility, password)
        result = _as_dict(
            self._request(
                "POST", f"/api/uploads/{slug}/visibility", json={"visibility": visibility, "password": password}
            )
        )
        return _str(result, "visibility")

    def delete_page(self, slug: str) -> None:
        validate_slug(slug)
        self._request("DELETE", f"/api/uploads/{slug}")

    def rename_page(self, slug: str, name: str) -> PeekRenameResult:
        validate_slug(slug)
        if not 1 <= len(name.strip()) <= 200:
            raise PeekClientError("A Peek page name is 1 to 200 characters.")
        result = _as_dict(self._request("POST", f"/api/uploads/{slug}/name", json={"name": name}))
        return PeekRenameResult(slug=_str(result, "slug"), url=_str(result, "url"), name=_str(result, "name"))

    def add_comment(self, slug: str, body: str, selector: str, element_text: str) -> PeekComment:
        validate_slug(slug)
        payload = {"body": body, "selector": selector, "element_text": element_text}
        try:
            result = self._request("POST", f"/api/uploads/{slug}/account-comments", json=payload)
        except PeekClientError as exc:
            if exc.status in (404, 405):
                raise PeekClientError(
                    "This Peek server does not accept comments from PrivacyFence: "
                    "it has no account comment endpoint. See the Peek setup guide.",
                    status=exc.status,
                ) from exc
            raise
        return _comment(result)
