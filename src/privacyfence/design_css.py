"""The design system the app shares with the website (ADR 0078).

``resources/design/tokens.css`` (colour, type scale, spacing and shape tokens) and
``resources/design/base.css`` (layout primitives, buttons, focus style, labels, and the shared
rules in its header comment) are the same files ``scripts/build_site.py`` publishes as
privacyfence.eu's ``/tokens.css`` and ``/base.css``. Every document the app renders inlines
:data:`SHARED_CSS` at the start of its one nonce'd ``<style>``, before its own rules, so a
page-specific rule of equal specificity still wins over a shared one. Inlined rather than linked:
the documents are self-contained (CSP ``default-src 'none'``, and a card is served again,
unchanged, until it is decided).
"""
from __future__ import annotations

from pathlib import Path

DESIGN_DIR = Path(__file__).parent / "resources" / "design"

TOKENS_CSS = (DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
BASE_CSS = (DESIGN_DIR / "base.css").read_text(encoding="utf-8")

# Tokens first: base.css reads them.
SHARED_CSS = TOKENS_CSS + BASE_CSS
