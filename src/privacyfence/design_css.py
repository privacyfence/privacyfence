"""The design system the app shares with the website (ADR 0078), and the app's own layer on it
(ADR 0079).

``resources/design/tokens.css`` (colour, type scale, spacing and shape tokens) and
``resources/design/base.css`` (layout primitives, buttons, focus style, labels, and the shared
rules in its header comment) are the same files ``scripts/build_site.py`` publishes as
privacyfence.eu's ``/tokens.css`` and ``/base.css``. Every document the app renders inlines
:data:`DOCUMENT_CSS` at the start of its one nonce'd ``<style>``, before its own rules, so a
page-specific rule of equal specificity still wins over a shared one. That is the shared files
followed by ``resources/design/app.css`` (dark mode, status tokens, application components),
which only the app loads. Inlined rather than linked:
the documents are self-contained (CSP ``default-src 'none'``, and a card is served again,
unchanged, until it is decided).
"""
from __future__ import annotations

from pathlib import Path

DESIGN_DIR = Path(__file__).parent / "resources" / "design"

TOKENS_CSS = (DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
BASE_CSS = (DESIGN_DIR / "base.css").read_text(encoding="utf-8")

APP_CSS = (DESIGN_DIR / "app.css").read_text(encoding="utf-8")

# Tokens first: base.css reads them. The app layer last: it overrides token values (dark mode)
# and adds components.
SHARED_CSS = TOKENS_CSS + BASE_CSS
DOCUMENT_CSS = SHARED_CSS + APP_CSS


def token_value(name: str) -> str:
    """A light-theme token's value from ``tokens.css``'s ``:root`` block (``"--bg"`` ->
    ``"#f6f8fb"``), for the few places that need a colour outside CSS: the web app manifest's
    ``theme_color``/``background_color`` (web/routes_push.py), which the website's own
    ``<meta name="theme-color">`` also takes from ``--bg``."""
    import re

    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*:\s*([^;]+);", TOKENS_CSS)
    if match is None:
        raise KeyError(name)
    return match.group(1).strip()
