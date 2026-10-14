"""The page an org member sees before an MCP client they have not used before gets a code for
them (ADR 0177).

Without it, anyone who can start ``/authorize`` for a registered (or freshly self-registered, DCR
is open) client and get a member's browser to follow the link gets that member's silent IdP
re-authentication turned into a code for the attacker's client. This module holds the two pages --
the consent form and the "returning to your AI client" page that follows it -- and
``ConsentStore``, the per-person record of which (client, redirect URI) pairs they already allowed.
``OrgOAuthProvider`` holds the pending consents themselves.

Every value put into either page goes through ``html.escape``: the client name is whatever the
client registered with, and the redirect host is whatever it asked for.
"""
from __future__ import annotations

import html
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .. import paths, web_shell
from ..principal import Principal
from ..secure_files import atomic_write_json

logger = logging.getLogger(__name__)

CONSENTS_FILE_NAME = "oauth_consents.json"
UNNAMED_CLIENT_LABEL = "An unnamed AI client"


@dataclass(frozen=True)
class ConsentView:
    """What the consent page shows, and the CSRF token its form must send back."""

    client_name: str
    redirect_host: str
    principal_label: str
    csrf: str


class ConsentStore:
    """Which (client id, redirect URI) pairs each person has already allowed, on disk beside
    the rest of that person's data (``oauth_consents.json`` under ``paths.user_dir``).

    Keyed by redirect URI as well as client id: a client may register several, and the person
    allowed the one the page showed. A missing or malformed file reads as "no consent" -- the
    page is shown again -- so a damaged file can never grant anything."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @staticmethod
    def _path(principal: Principal):
        return paths.user_dir(principal) / CONSENTS_FILE_NAME

    def _read(self, principal: Principal) -> dict[str, Any]:
        path = self._path(principal)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            clients = raw["clients"]
            if not isinstance(clients, dict):
                raise TypeError("'clients' is not an object")
            return clients
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unreadable %s (%s): treating it as no consent given", path, exc)
            return {}

    def has(self, principal: Principal, client_id: str, redirect_uri: str) -> bool:
        with self._lock:
            redirects = self._read(principal).get(client_id)
        return isinstance(redirects, dict) and redirect_uri in redirects

    def record(self, principal: Principal, client_id: str, redirect_uri: str) -> None:
        with self._lock:
            clients = self._read(principal)
            redirects = clients.get(client_id)
            if not isinstance(redirects, dict):
                redirects = {}
            redirects[redirect_uri] = time.time()
            clients[client_id] = redirects
            atomic_write_json(self._path(principal), {"clients": clients}, indent=2, sort_keys=True)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def render_consent_page(view: ConsentView, *, consent_id: str, nonce: str) -> str:
    """The consent form. ``consent_id`` is not in the markup: the form posts back to the URL it
    was served from."""
    body = (
        f"<h1>Allow {_esc(view.client_name)} to use PrivacyFence as {_esc(view.principal_label)}?</h1>"
        "<p>It will be able to call your PrivacyFence tools. Anything your rules don't allow "
        "still needs your approval on a card.</p>"
        f"<p>You'll return to {_esc(view.redirect_host)}.</p>"
        '<form method="post">'
        f'<input type="hidden" name="csrf" value="{_esc(view.csrf)}">'
        '<button type="submit" name="decision" value="allow">Allow</button> '
        '<button type="submit" name="decision" value="deny">Don\'t allow</button>'
        "</form>"
    )
    return web_shell.plain_page(body, title="Allow access?", nonce=nonce)


def render_continue_page(redirect_url: str, *, redirect_host: str, nonce: str) -> str:
    """The page that follows the decision. A 200 page rather than a redirect: org mode's
    ``form-action 'self'`` would block a form post that redirects to the client's origin."""
    url = _esc(redirect_url)
    body = (
        f"<h1>Returning to {_esc(redirect_host)}…</h1>"
        f'<p><a href="{url}">Continue</a></p>'
    )
    return web_shell.plain_page(
        body, title="Returning to your AI client", nonce=nonce,
        head_html=f'<meta http-equiv="refresh" content="0;url={url}">',
    )
