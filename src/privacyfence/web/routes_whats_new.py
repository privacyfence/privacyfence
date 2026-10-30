"""Org mode's "What's new" page and its seen endpoint (ADR 0238). Local mode mounts neither.

- ``GET /whats-new``: the release notes since the version the signed-in principal last saw
  (``whats_new.notes_since``), newest first. Opening it records the running version as seen.
- ``POST /api/whats-new/seen``: the strip's close button. Same auth as every other org-mode write:
  the ``pf_org_session`` cookie, the double-submit CSRF token in the body and the Origin check. It
  records the *running* version, whatever the client sends.

**Every route here is classified, or the app refuses to start** -- ADR 0014's rule, applied to
this module (see ``routes_push.py``).
"""
from __future__ import annotations

import asyncio
import json
from html import escape
from typing import Sequence

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import update_checker, web_shell, whats_new
from ..markdown_to_html import markdown_to_html
from ..principal import Principal
from . import org_session
from .csp import nonce_for as _csp_nonce_for
from .org_session import OrgSessionStore


PAGE_PATH = "/whats-new"
SEEN_PATH = "/api/whats-new/seen"
SESSION = "session"
SESSION_AND_CSRF = "session+csrf"
CHANGELOG_URL = "https://github.com/privacyfence/privacyfence/blob/main/CHANGELOG.md"
# The body is {"csrf": "<session id>"}; anything much larger is not one.
_MAX_BODY_BYTES = 1024

# ADR 0014: path -> (auth, reason). build_routes() refuses to return a route missing from here.
ROUTE_CLASSIFICATION: dict[str, tuple[str, str]] = {
    PAGE_PATH: (
        SESSION,
        "Shows the bundled release notes and records, for the signed-in principal only, that they were seen.",
    ),
    SEEN_PATH: (
        SESSION_AND_CSRF,
        "Records, for the signed-in principal only, that the running version's notes were seen.",
    ),
}


def _page_notes(principal: Principal) -> tuple[list[whats_new.ReleaseNotes], str]:
    """The sections to show and the running version, marking the running version seen. File I/O,
    so the handler runs it in a thread."""
    running = whats_new.running_version()
    last = whats_new.read_last_seen(principal)
    sections = whats_new.load_changelog()
    notes: list[whats_new.ReleaseNotes] = []
    if last is not None and update_checker.is_newer(running, last):
        notes = whats_new.notes_since(sections, last, running)
    if not notes:
        notes = whats_new.notes_since(sections, None, running)[:1]
    whats_new.mark_seen(principal, running)
    return notes, running


def _section_html(note: whats_new.ReleaseNotes) -> str:
    title = "In this pre-release" if note.version == whats_new.UNRELEASED else escape(note.version)
    date = f' <span class="pf-whats-new-date">{escape(note.date)}</span>' if note.date else ""
    return (
        f'<section class="pf-whats-new-release"><h2>{title}{date}</h2>'
        f"{markdown_to_html(note.body)}</section>"
    )


def render_page(
    notes: Sequence[whats_new.ReleaseNotes], *, running: str, truncated: bool, principal: Principal, nonce: str,
) -> str:
    sections = "\n".join(_section_html(note) for note in notes)
    truncated_html = "<p>Older changes are in the full changelog.</p>" if truncated else ""
    empty_html = "" if notes else "<p>No release notes are available for this version.</p>"
    body = f"""<style nonce="{nonce}">.pf-whats-new {{ max-width: 760px; margin: 0 auto; padding: 24px 20px; }}
.pf-whats-new-release h2 {{ margin-bottom: 4px; }} .pf-whats-new-date {{ color: var(--muted); font-weight: 400; font-size: 14px; }}</style>
<div class="pf-whats-new stack">
<h1>What's new in PrivacyFence</h1>
<p class="lead">You are using version {escape(running)}.</p>
{sections}
{truncated_html}{empty_html}
<p><a href="{CHANGELOG_URL}">Full changelog on GitHub</a></p>
</div>"""
    return web_shell.wrap(
        body, title="PrivacyFence — What's new", active="whats-new", nonce=nonce,
        nav_items=web_shell.org_nav_items(principal.is_admin),
        principal_label=principal.email or principal.display_name or principal.id,
        live_updates=False, notifications_enabled=False,
    )


def build_routes(*, sessions: OrgSessionStore) -> list[Route]:
    async def page(request: Request) -> Response:
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return RedirectResponse(
                f"/login?next={PAGE_PATH}", status_code=302, headers={"Cache-Control": "no-store"},
            )
        notes, running = await asyncio.to_thread(_page_notes, principal)
        html = render_page(
            notes[:whats_new.MAX_SECTIONS], running=running, truncated=len(notes) > whats_new.MAX_SECTIONS,
            principal=principal, nonce=_csp_nonce_for(request),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def seen(request: Request) -> Response:
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
        csrf = body.get("csrf") if isinstance(body, dict) else None
        if not isinstance(csrf, str) or not org_session.check_csrf(request, csrf):
            return JSONResponse({"error": "csrf"}, status_code=403)
        running = whats_new.running_version()
        await asyncio.to_thread(whats_new.mark_seen, principal, running)
        return JSONResponse({"seen": running}, headers={"Cache-Control": "no-store"})

    routes = [
        Route(PAGE_PATH, page, methods=["GET"]),
        Route(SEEN_PATH, seen, methods=["POST"]),
    ]
    unclassified = [route.path for route in routes if route.path not in ROUTE_CLASSIFICATION]
    if unclassified:
        # Not an assert: ADR 0014's consequences note that an assert is skipped under -O.
        raise RuntimeError(f"routes_whats_new: unclassified route(s) {unclassified} (ADR 0014)")
    return routes


__all__ = [
    "CHANGELOG_URL", "PAGE_PATH", "ROUTE_CLASSIFICATION", "SEEN_PATH", "SESSION", "SESSION_AND_CSRF",
    "build_routes", "render_page",
]
