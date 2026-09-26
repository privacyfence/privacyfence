"""Org mode's installable app and web push routes (ADR 0081). Local mode mounts none of them.

- ``GET /manifest.webmanifest`` and ``GET /icons/icon-{192,512}.png``: the Web App Manifest and
  its icons, which make the org app installable to a phone's Home Screen. iOS delivers web push
  only to an installed site, so without these there is no push on an iPhone at all. They carry
  no user data, and a browser fetches a manifest without credentials, so they are public.
- ``POST``/``DELETE /api/push/subscription``: store or remove the signed-in principal's push
  subscription. Same auth as every other org-mode write: the ``pf_org_session`` cookie, the
  double-submit CSRF token in the body, and the Origin check. Mounted only when push is on for the
  org (``org_config.json``'s ``web_push.enabled``, default on). A stored subscription records a
  hash of the session that posted it, which is how ``POST /logout`` stops pushes to the browser
  that signs out.

**Every route here is classified, or the app refuses to start** -- ADR 0014's rule, applied to
this module: ``ROUTE_CLASSIFICATION`` names each path's auth and why, and ``build_routes`` raises
if it builds a route that is not in it. A public route has to say why it can be public.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..design_css import token_value
from ..web_push import (
    InvalidSubscription, PushSubscriptionStore, parse_subscription, session_tag, validate_endpoint,
)
from . import org_session
from .org_session import OrgSessionStore

logger = logging.getLogger(__name__)

MANIFEST_PATH = "/manifest.webmanifest"
SUBSCRIPTION_PATH = "/api/push/subscription"
# path -> (file under resources/, square size in px)
ICON_PATHS = {"/icons/icon-192.png": ("icon_192.png", 192), "/icons/icon-512.png": ("icon_512.png", 512)}

_RESOURCES = Path(__file__).resolve().parent.parent / "resources"
# A browser's PushSubscription.toJSON() is a few hundred bytes; anything much larger is not one.
_MAX_BODY_BYTES = 4096

# ADR 0014: path -> (auth, reason). build_routes() refuses to return a route missing from here.
PUBLIC = "public"
SESSION_AND_CSRF = "session+csrf"
ROUTE_CLASSIFICATION: dict[str, tuple[str, str]] = {
    MANIFEST_PATH: (
        PUBLIC,
        "Static app metadata (name, colours, icon URLs) with no user data. Browsers fetch a "
        "manifest without cookies unless it is linked with crossorigin=use-credentials.",
    ),
    "/icons/icon-192.png": (PUBLIC, "The app icon the manifest names; the same image the website shows."),
    "/icons/icon-512.png": (PUBLIC, "The app icon the manifest names; the same image the website shows."),
    SUBSCRIPTION_PATH: (
        SESSION_AND_CSRF,
        "Writes the signed-in principal's own push subscriptions, and only theirs.",
    ),
}


def manifest() -> dict:
    """The Web App Manifest. ``start_url`` is the approvals list, which is what a notification
    opens too; ``display: standalone`` is what iOS needs to treat the Home Screen icon as an app
    that may receive push."""
    return {
        "name": "PrivacyFence",
        "short_name": "PrivacyFence",
        "description": "Review and decide what your AI assistant may read and do.",
        "id": "/approvals",
        "start_url": "/approvals",
        "scope": "/",
        "display": "standalone",
        "theme_color": token_value("--bg"),
        "background_color": token_value("--bg"),
        "icons": [
            {"src": path, "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any"}
            for path, (_name, size) in ICON_PATHS.items()
        ],
    }


def build_routes(*, sessions: OrgSessionStore, store: PushSubscriptionStore | None) -> list[Route]:
    """The manifest and icon routes, plus the subscription routes when ``store`` is given (push
    on for the org). ``store`` is ``None`` when ``web_push.enabled`` is false: the routes then do
    not exist, so nothing can be stored for a push that will never be sent."""
    manifest_body = json.dumps(manifest(), indent=2).encode("utf-8")
    icons = {path: (_RESOURCES / name).read_bytes() for path, (name, _size) in ICON_PATHS.items()}

    async def serve_manifest(request: Request) -> Response:
        del request
        return Response(
            manifest_body, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"},
        )

    def icon_route(path: str) -> Route:
        async def serve_icon(request: Request) -> Response:
            del request
            return Response(icons[path], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

        return Route(path, serve_icon, methods=["GET"])

    routes: list[Route] = [Route(MANIFEST_PATH, serve_manifest, methods=["GET"])]
    routes.extend(icon_route(path) for path in ICON_PATHS)

    if store is not None:
        async def subscription(request: Request) -> Response:
            principal = org_session.authenticated(request, sessions)
            if principal is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if not org_session.check_origin(request):
                return JSONResponse({"error": "origin mismatch"}, status_code=403)
            raw = await request.body()
            if len(raw) > _MAX_BODY_BYTES:
                return JSONResponse({"error": "request too large"}, status_code=413)
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return JSONResponse({"error": "invalid JSON"}, status_code=400)
            if not isinstance(body, dict) or not org_session.check_csrf(request, body.get("csrf")):
                return JSONResponse({"error": "csrf"}, status_code=403)
            try:
                if request.method == "DELETE":
                    endpoint = validate_endpoint(body.get("endpoint"))
                    removed = store.remove(principal.id, endpoint)
                    return JSONResponse({"removed": removed}, headers={"Cache-Control": "no-store"})
                # Tagged with this sign-in session, so /logout can drop this browser's copy
                # (routes_org_identity.py) and leave the principal's other devices alone.
                tag = session_tag(request.cookies.get(org_session.SESSION_COOKIE, ""))
                store.add(principal.id, replace(parse_subscription(body.get("subscription")), session=tag))
            except InvalidSubscription as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse({"subscribed": True}, headers={"Cache-Control": "no-store"})

        routes.append(Route(SUBSCRIPTION_PATH, subscription, methods=["POST", "DELETE"]))

    unclassified = [route.path for route in routes if route.path not in ROUTE_CLASSIFICATION]
    if unclassified:
        # Not an assert: ADR 0014's consequences note that an assert is skipped under -O.
        raise RuntimeError(f"routes_push: unclassified route(s) {unclassified} (ADR 0014)")
    return routes


__all__ = [
    "ICON_PATHS", "MANIFEST_PATH", "PUBLIC", "ROUTE_CLASSIFICATION", "SESSION_AND_CSRF",
    "SUBSCRIPTION_PATH", "build_routes", "manifest",
]
