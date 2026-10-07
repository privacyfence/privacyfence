"""Plugin pages: request normalization and response filtering for ``GET /plugins/<name>/...``
(ADR 0124).

A plugin page is whatever the plugin answers to ``web.request``, after this module has cut it down
to a status from a short list, one content type from an allowlist and a body under the size cap.
Every other header the plugin sends (``set-cookie``, its own CSP, ``cache-control``) is dropped:
the fixed security headers are web/server.py's ``_SecurityHeadersMiddleware``'s, set for every
path under ``/plugins/``. The SDK's test host applies the same rules, so a plugin author sees in a
test what the daemon serves.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote

from privacyfence.plugins.constants import MAX_PAGE_BODY_BYTES, MAX_PAGE_PATH_CHARS
from privacyfence.plugins.protocol import RpcError
from privacyfence.principal import Principal

CSP = (
    "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)
CACHE_CONTROL = "private, no-store"

_ALLOWED_STATUSES = frozenset({200, 204, 400, 404, 500})
_CONTENT_TYPES = frozenset({
    "text/html", "text/plain", "text/css", "application/javascript", "application/json",
    "image/png", "image/svg+xml", "image/jpeg",
})
_OCTET_STREAM = "application/octet-stream"
_PLAIN = "text/plain; charset=utf-8"
BAD_PATH = b"Bad path."
NOT_FOUND = b"Not Found"
NO_ANSWER = b"The plugin did not answer."
BAD_RESPONSE = b"The plugin returned an invalid response."


class PageHost(Protocol):
    """The one ``PluginHost`` method a page needs."""

    async def web_request(self, name: str, path: str, query: dict[str, str], principal: Principal) -> dict: ...


def normalize_path(raw: str) -> str | None:
    """URL-decode ``raw`` once and check it; ``None`` means the path is rejected.

    Rejected: a ``..`` segment, NUL, a backslash, an empty segment (``//``), and a decoded path
    longer than ``MAX_PAGE_PATH_CHARS``. The result always starts with ``/``.
    """
    if len(raw) > 3 * MAX_PAGE_PATH_CHARS:  # every decoded character is at most three encoded ones
        return None
    path = unquote(raw)
    if "\x00" in path or "\\" in path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > MAX_PAGE_PATH_CHARS or "//" in path or ".." in path.split("/"):
        return None
    return path


def parse_query(raw: str) -> dict[str, str]:
    """The query string as ``{name: value}``: strings only, and the last value of a repeated name
    wins."""
    return dict(parse_qsl(raw, keep_blank_values=True))


def _content_type(value: Any) -> str:
    if not isinstance(value, str):
        return _OCTET_STREAM
    base, _, params = value.partition(";")
    base, params = base.strip().lower(), params.strip().lower().replace(" ", "")
    if base in _CONTENT_TYPES and params in ("", "charset=utf-8"):
        return f"{base}; {params}" if params else base
    return _OCTET_STREAM


def _plain(status: int, body: bytes) -> tuple[int, dict[str, str], bytes]:
    return status, {"cache-control": CACHE_CONTROL, "content-type": _PLAIN}, body


def filter_response(result: Any) -> tuple[int, dict[str, str], bytes]:
    """Cut a plugin's ``web.request`` result down to ``(status, headers, body)``.

    The status must be one of 200, 204, 400, 404 or 500, the body must decode per
    ``body_encoding`` and fit in ``MAX_PAGE_BODY_BYTES``; anything else is a 502. The only headers
    kept are ``content-type`` (from the allowlist, else ``application/octet-stream``) and this
    module's own ``cache-control``.
    """
    if not isinstance(result, dict) or not isinstance(result.get("status"), int) or isinstance(result["status"], bool):
        return _plain(502, BAD_RESPONSE)
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
        return _plain(502, BAD_RESPONSE)
    if status not in _ALLOWED_STATUSES or len(data) > MAX_PAGE_BODY_BYTES:
        return _plain(502, BAD_RESPONSE)
    return status, {"cache-control": CACHE_CONTROL, "content-type": _content_type(content_type)}, data


async def render_plugin_page(
    host: PageHost, name: str, path: str, query: dict[str, str], principal: Principal,
) -> tuple[int, dict[str, str], bytes]:
    """One page request: normalize ``path`` (still URL-encoded), ask the plugin, filter the answer.

    A rejected path is a 400 and a plugin that is not serving pages a 404, neither of which reaches
    the plugin; a plugin that times out or answers with an error is a 502.
    """
    normalized = normalize_path(path)
    if normalized is None:
        return _plain(400, BAD_PATH)
    try:
        result = await host.web_request(name, normalized, query, principal)
    except LookupError:
        return _plain(404, NOT_FOUND)
    except RpcError:
        return _plain(502, NO_ANSWER)
    return filter_response(result)


__all__ = [
    "CACHE_CONTROL", "CSP", "PageHost", "filter_response", "normalize_path", "parse_query",
    "render_plugin_page",
]
