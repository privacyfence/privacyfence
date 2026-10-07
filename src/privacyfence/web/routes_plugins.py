"""``GET /plugins/<name>/...``: a running plugin's read-only pages (ADR 0124). Local mode only.

The routes are owner-only twice over: web/server.py's ``build_app`` wraps them with
``_owner_only_routes`` exactly as it wraps the Settings routes, so a principal other than the owner
gets that wrapper's 404, and the endpoint itself asks ``is_owner_session`` (the local auth adapter,
ADR 0033: a live session a human asked for, from the same session store as Settings) and answers
the same 404 when it says no. An MCP bearer token and a request with no cookie therefore get
exactly what a non-owner gets, and none of them reaches the plugin.

GET and HEAD are served; HEAD is forwarded as GET and its body dropped. Every other method is a
405 with ``Allow: GET, HEAD``, answered before any auth check or plugin call. The sandbox CSP and
the other fixed headers on every response under ``/plugins/`` are ``_SecurityHeadersMiddleware``'s,
not this module's: a route cannot set its own CSP there.
"""
from __future__ import annotations

from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import Receive, Scope, Send

from ..plugins.constants import PLUGIN_NAME_RE
from ..plugins.pages import PageHost, parse_query, render_plugin_page
from ..principal import current_principal

_ALLOWED_METHODS = "GET, HEAD"


class _GetHeadRoute(Route):
    """A GET route whose 405 names exactly ``GET, HEAD``. Starlette builds the ``Allow`` header by
    joining a set, so its order would change from one process to the next."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] not in ("GET", "HEAD"):
            response = PlainTextResponse(
                "Method Not Allowed", status_code=405, headers={"Allow": _ALLOWED_METHODS},
            )
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)


def _not_found() -> Response:
    # The same answer as web/server.py's _owner_only_endpoint, so a request without the owner's
    # session cannot tell the two refusals apart.
    return PlainTextResponse("Not Found", status_code=404)


def _raw_remainder(request: Request, name: str) -> str | None:
    """The request path after ``/plugins/<name>``, still URL-encoded, so that it is decoded once,
    by ``normalize_path``. ``None`` when the raw path does not carry the name as matched (an encoded
    letter in the name), which is answered like an unknown plugin."""
    raw = request.scope.get("raw_path")
    raw_path = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else request.url.path
    prefix = f"/plugins/{name}"
    if not raw_path.startswith(prefix + "/"):
        return None
    return raw_path[len(prefix):]


def build_routes(plugin_host: PageHost, *, is_owner_session: Callable[[Request], bool]) -> list[BaseRoute]:
    """The page routes, for ``build_app`` to wrap with ``_owner_only_routes`` and mount.

    ``is_owner_session`` is the local auth adapter: true for a live session a human asked for.
    """

    async def plugin_page(request: Request) -> Response:
        if not is_owner_session(request):
            return _not_found()
        name = request.path_params["name"]
        if not PLUGIN_NAME_RE.fullmatch(name):
            return _not_found()
        raw = _raw_remainder(request, name)
        if raw is None:
            return _not_found()
        query_string = request.scope.get("query_string", b"").decode("latin-1")
        status, headers, body = await render_plugin_page(
            plugin_host, name, raw, parse_query(query_string), current_principal(),
        )
        if request.method == "HEAD":
            body = b""
        return Response(body, status_code=status, headers=headers)

    async def plugin_root(request: Request) -> Response:
        if not is_owner_session(request):
            return _not_found()
        name = request.path_params["name"]
        if not PLUGIN_NAME_RE.fullmatch(name):
            return _not_found()
        return RedirectResponse(f"/plugins/{name}/", status_code=307)

    return [
        _GetHeadRoute("/plugins/{name}", plugin_root, methods=["GET"], name="plugin_root"),
        _GetHeadRoute("/plugins/{name}/{path:path}", plugin_page, methods=["GET"], name="plugin_page"),
    ]


__all__ = ["build_routes"]
