"""Screenshots of the app's pages for a human design review, driven by scripts/render_ui_review.py.

Not a test module: its name does not match ``test_*.py``, so the suite never collects it, and it
asserts nothing beyond "the page rendered". scripts/render_ui_review.py passes this file to pytest
explicitly, with ``PF_UI_REVIEW_OUT`` naming the directory to write into, because that is how it
gets the servers the browser tests already build (``org_server_and_ui``, ``local_server`` and the
rest, imported from test_browser_smoke.py) instead of a second way of starting them.

Every surface is captured at 393 px (the phone emulation the phone-layout tests use) and 1280 px
(a desktop window), in the light and the dark theme. Synthetic data only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from privacyfence import paths as paths_module
from privacyfence.card_builder import build_card_html
from privacyfence.principal import Principal, principal_scope

from . import test_browser_smoke as _smoke

# The browser tests' own fixtures, bound here by name so pytest finds them for this module.
browser = _smoke.browser
pf_home = _smoke.pf_home
org_server = _smoke.org_server
org_server_and_ui = _smoke.org_server_and_ui

_MARKDOWN_PREVIEW = _smoke._MARKDOWN_PREVIEW
_MOBILE_EMULATION = _smoke._MOBILE_EMULATION
_TEN_COLUMN_TABLE = _smoke._TEN_COLUMN_TABLE
_pdf_bytes = _smoke._pdf_bytes
_sign_in_org = _smoke._sign_in_org

OUT = Path(os.environ.get("PF_UI_REVIEW_OUT", "test-results/ui-review"))

WIDTHS = {
    "393": _MOBILE_EMULATION,
    "1280": {"viewport": {"width": 1280, "height": 900}},
}
SCHEMES = ("light", "dark")
_ADMIN = Principal(id="carol", email="carol@example.com", display_name="Carol", is_admin=True)


@pytest.fixture(params=[(w, s) for w in WIDTHS for s in SCHEMES], ids=lambda p: f"{p[0]}-{p[1]}")
def shot(browser, request):
    """A page in the context for one width and theme, and a function that saves it."""
    width, scheme = request.param
    ctx = browser.new_context(ignore_https_errors=True, color_scheme=scheme, **WIDTHS[width])
    page = ctx.new_page()

    def save(name: str) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(150)  # let the SSE indicator and fonts settle
        page.screenshot(path=str(OUT / f"{name}-{width}-{scheme}.png"), full_page=True)

    yield page, save
    ctx.close()


@pytest.mark.parametrize("path", ["approvals", "connect", "security"])
def test_shell_page(shot, org_server_and_ui, path):
    page, save = shot
    server, sessions, web_ui = org_server_and_ui
    _sign_in_org(page.context, server, sessions, principal=_ADMIN)
    pending = []
    if path == "approvals":
        with principal_scope(_ADMIN):
            for i, (connector, tool, gate, summary) in enumerate([
                ("gmail", "Get message", "review", "Quarterly numbers from Alice"),
                ("drive", "Upload file", "popup", "Board deck v3.pptx"),
            ]):
                approval, _ = web_ui.deferred_registry.register_or_coalesce(
                    dedupe_key=f"review-{i}", connector=connector, tool=f"{connector}_{i}", gate_kind=gate,
                    request_id=f"review-{i}", summary=summary, tool_name=tool,
                )
                pending.append(approval)
    try:
        response = page.goto(f"{server.base_url}/{path}")
        assert response.status == 200
        page.wait_for_load_state("load")
        for approval in pending:
            page.wait_for_selector(f'[data-approval-id="{approval.id}"]')
        save(path)
    finally:
        for approval in pending:
            web_ui.resolve(approval.id, "deny")


@pytest.mark.parametrize("section", ["general", "privacy"])
def test_settings(shot, org_server, section):
    page, save = shot
    server, sessions = org_server
    _sign_in_org(page.context, server, sessions, principal=_ADMIN)
    page.goto(f"{server.base_url}/settings")
    page.wait_for_selector(".pf-navitem")
    page.locator(f'.pf-navitem[data-nav="{section}"]').evaluate("(el) => el.click()")
    page.wait_for_selector(".pf-page, .pf-detail-page")
    save(f"settings-{section}")


_WRITE = {"title": "Send email", "preview": {"To": "alice@example.com", "Subject": "Q3 numbers"},
          "details_text": "Hi Alice, the numbers are attached.", "is_read": False}
_READ = {"title": "Get file content", "preview": {"File": "Quarterly report", "Owner": "alice@example.com"},
         "details_text": "", "is_read": True, "claude_reason": "To summarise the quarter for you."}


def _card_kinds() -> dict[str, dict]:
    icon = (Path(paths_module.__file__).parent / "resources" / "icon_512.png").read_bytes()
    return {
        "write": {**_WRITE, "layout": "narrow", "claude_reason": "You asked me to send Alice the numbers.",
                  "accept_all_choices": [("rule", "Always allow — this recipient")]},
        "write-flagged": {**_WRITE, "layout": "narrow", "write_content_flags": ["Phone number", "IBAN"]},
        "read": {**_READ, "layout": "narrow", "accept_all_choices": [("a", "Always allow — this folder"),
                                                                     ("b", "Always allow — this owner")]},
        "read-pii": {**_READ, "layout": "wide", "pii_categories": ["Email address", "National ID"],
                     "preview_blocks": [{"type": "markdown", "text": _MARKDOWN_PREVIEW}]},
        "read-pdf": {**_READ, "layout": "wide", "pdf_bytes": _pdf_bytes()},
        "read-image": {**_READ, "layout": "wide", "preview_bytes": icon, "preview_mime_type": "image/png"},
        "read-markdown": {**_READ, "layout": "wide", "preview_blocks": [{"type": "markdown", "text": _MARKDOWN_PREVIEW}]},
        "read-table": {**_READ, "layout": "wide", "preview_tables": [_TEN_COLUMN_TABLE], "table_only": True},
    }


@pytest.mark.parametrize("kind", list(_card_kinds()))
def test_card(shot, kind):
    """The card document as GET /approvals/{id} serves it (the route returns it verbatim)."""
    page, save = shot
    page.set_content(build_card_html(**_card_kinds()[kind]))
    # The buttons are enabled on DOMContentLoaded; wait for it so they are shown as a reviewer
    # sees them, not in their pre-load disabled state.
    page.wait_for_function("() => !document.querySelector('.pf-btn-primary[aria-disabled=\"true\"]')")
    save(f"card-{kind}")
