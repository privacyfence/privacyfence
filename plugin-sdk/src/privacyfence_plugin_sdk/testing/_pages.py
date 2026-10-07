"""Plugin pages as the daemon serves them: fixed security headers, path rules and response filtering."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

from .._rpc import Peer, RpcError

MAX_PAGE_PATH_CHARS = 512
MAX_PAGE_BODY_BYTES = 8 * 1024 * 1024
_WEB_REQUEST_TIMEOUT = 30.0

CSP = (
    "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)
# Header names are lower case, as an ASGI server hands them on.
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "private, no-store",
    "x-frame-options": "DENY",
}
_ALLOWED_STATUSES = frozenset({200, 204, 400, 404, 500})
_CONTENT_TYPES = frozenset({
    "text/html", "text/plain", "text/css", "application/javascript", "application/json",
    "image/png", "image/svg+xml", "image/jpeg",
})
_PLAIN = "text/plain; charset=utf-8"
_NO_ANSWER = b"The plugin did not answer."
_BAD_RESPONSE = b"The plugin returned an invalid response."


@dataclass
class PageResponse:
    """What a browser would get for one request to a plugin page."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def normalize_path(raw: str) -> str | None:
    """URL-decode ``raw`` once and apply the daemon's rules; ``None`` means the path is rejected."""
    if len(raw) > 3 * MAX_PAGE_PATH_CHARS:
        return None
    path = unquote(raw)
    if "\x00" in path or "\\" in path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > MAX_PAGE_PATH_CHARS or "//" in path or ".." in path.split("/"):
        return None
    return path


def _content_type(value: Any) -> str:
    if not isinstance(value, str):
        return "application/octet-stream"
    base, _, params = value.partition(";")
    base, params = base.strip().lower(), params.strip().lower().replace(" ", "")
    if base in _CONTENT_TYPES and params in ("", "charset=utf-8"):
        return f"{base}; {params}" if params else base
    return "application/octet-stream"


def filter_response(result: Any) -> tuple[int, dict[str, str], bytes]:
    """The daemon's filter on a plugin's ``web.request`` result: ``(status, headers, body)``."""
    headers = {"cache-control": SECURITY_HEADERS["cache-control"]}
    if not isinstance(result, dict) or not isinstance(result.get("status"), int) or isinstance(result["status"], bool):
        return 502, {**headers, "content-type": _PLAIN}, _BAD_RESPONSE
    status = result["status"]
    plugin_headers = result.get("headers")
    plugin_headers = plugin_headers if isinstance(plugin_headers, dict) else {}
    content_type = next((v for k, v in plugin_headers.items() if str(k).lower() == "content-type"), None)
    body = result.get("body")
    encoding = result.get("body_encoding", "utf8")
    try:
        if not isinstance(body, str):
            raise ValueError("body")
        if encoding == "utf8":
            data = body.encode("utf-8")
        elif encoding == "base64":
            data = base64.b64decode(body, validate=True)
        else:
            raise ValueError("body_encoding")
    except (ValueError, binascii.Error, UnicodeError):
        return 502, {**headers, "content-type": _PLAIN}, _BAD_RESPONSE
    if status not in _ALLOWED_STATUSES or len(data) > MAX_PAGE_BODY_BYTES:
        return 502, {**headers, "content-type": _PLAIN}, _BAD_RESPONSE
    return status, {**headers, "content-type": _content_type(content_type)}, data


def split_target(target: str) -> tuple[str, dict[str, str]]:
    """Split ``/path?a=1&a=2`` into the path and the query, where the last value wins."""
    target = target.partition("#")[0]
    path, _, query = target.partition("?")
    return path, {k: v for k, v in parse_qsl(query, keep_blank_values=True)}


def _response(status: int, body: bytes, *, extra: dict[str, str] | None = None) -> PageResponse:
    headers = {**SECURITY_HEADERS, "content-type": _PLAIN, **(extra or {})}
    return PageResponse(status, headers, body)


async def serve(
    peer: Peer, principal: dict, method: str, target: str, query: dict[str, str] | None = None
) -> PageResponse:
    """One page request, start to finish: method, path, ``web.request``, then the response filter."""
    method = method.upper()
    if method not in ("GET", "HEAD"):
        return _response(405, b"Method not allowed.", extra={"allow": "GET, HEAD"})
    raw_path, parsed = split_target(target)
    path = normalize_path(raw_path)
    if path is None:
        return _response(400, b"Bad path.")
    try:
        result = await peer.request("web.request", {
            "principal": principal, "method": "GET", "path": path, "query": {**parsed, **(query or {})},
        }, timeout=_WEB_REQUEST_TIMEOUT)
    except RpcError:
        return _response(502, _NO_ANSWER)
    status, headers, body = filter_response(result)
    return PageResponse(status, {**SECURITY_HEADERS, **headers}, b"" if method == "HEAD" else body)
