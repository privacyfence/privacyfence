"""``GET /plugin-pages`` and ``GET /plugin-pages/<name>``: the owner's browser of the pages the
running plugins list. Local mode only.

Owner-only twice over, like ``routes_plugins.py``: ``build_app`` wraps the routes with
``_owner_only_routes``, and each endpoint asks ``is_owner_session`` and answers the same 404
before the plugin host is called. The pages render PrivacyFence's own escaped strings and the
plugin's validated entry fields (``plugin_browser_html``); the response carries the app's nonce
CSP, not the sandbox of ``/plugins/``, and sets no cookie.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Route

from .. import plugin_browser_html, web_shell
from ..plugins.constants import PLUGIN_NAME_RE
from ..principal import current_principal
from . import csp

if TYPE_CHECKING:
    from ..plugins.host import PluginHost


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404)


def build_routes(
    plugin_host: PluginHost, *, is_owner_session: Callable[[Request], bool],
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
) -> list[BaseRoute]:
    """The browser routes, for ``build_app`` to wrap with ``_owner_only_routes`` and mount."""

    def _page(request: Request, indexes: list, *, single: bool) -> Response:
        nonce = csp.nonce_for(request)
        body = plugin_browser_html.render(indexes, single=single, nonce=nonce)
        html = web_shell.wrap(
            body, title="PrivacyFence — Plugin pages", active="plugin-pages", nonce=nonce,
            notifications_enabled=notifications_enabled, notifications_detail=notifications_detail,
            plugin_pages=tuple(plugin_host.page_links()),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def all_pages(request: Request) -> Response:
        if not is_owner_session(request):
            return _not_found()
        indexes = await plugin_host.list_all_pages(current_principal())
        return _page(request, indexes, single=False)

    async def one_plugin(request: Request) -> Response:
        if not is_owner_session(request):
            return _not_found()
        name = request.path_params["name"]
        if not PLUGIN_NAME_RE.fullmatch(name):
            return _not_found()
        try:
            index = await plugin_host.list_pages(name, current_principal())
        except LookupError:
            return _not_found()
        return _page(request, [index], single=True)

    return [
        Route("/plugin-pages", all_pages, methods=["GET"], name="plugin_pages"),
        Route("/plugin-pages/{name}", one_plugin, methods=["GET"], name="plugin_pages_one"),
    ]


__all__ = ["build_routes"]
