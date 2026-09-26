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
from privacyfence.dialog_window_html import build_choice_html, build_confirmation_html
from privacyfence.principal import Principal, principal_scope

from ..unit.test_pdf_render import text_pdf
from . import test_browser_smoke as _smoke

# The browser tests' own fixtures, bound here by name so pytest finds them for this module.
browser = _smoke.browser
pf_home = _smoke.pf_home
org_server = _smoke.org_server
org_server_and_ui = _smoke.org_server_and_ui
local_server_with_settings = _smoke.local_server_with_settings
local_server = _smoke.local_server

_MARKDOWN_PREVIEW = _smoke._MARKDOWN_PREVIEW
_MOBILE_EMULATION = _smoke._MOBILE_EMULATION
_TEN_COLUMN_TABLE = _smoke._TEN_COLUMN_TABLE
_pdf_bytes = _smoke._pdf_bytes
_sign_in_org = _smoke._sign_in_org
_sign_in_local = _smoke._sign_in_local

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


@pytest.mark.parametrize("step", ["phone", "code", "password"])
def test_connect_telegram(shot, monkeypatch, step):
    """/connect's Telegram sign-in at each step, with an error showing. The org fixture's bundle
    has no Telegram app credentials, so the page is rendered directly."""
    from privacyfence.web import routes_connect

    page, save = shot
    monkeypatch.setattr(routes_connect, "telegram_app_credentials", lambda: (123, "apihash"))
    page.set_content(routes_connect._render_connect_page(
        principal=_ADMIN, org_config={}, flash_connected="", flash_error="", csrf="c", nonce="n",
        telegram_state=routes_connect._TelegramState(
            step=None if step == "phone" else step, error="That did not work. Try again.",
        ),
    ))
    save(f"connect-telegram-{step}")


def test_local_security(shot, monkeypatch):
    """Local mode's /security, a web_shell.plain_page document, with two passkeys enrolled. The
    in-process local server mounts no /security, so the page is rendered directly."""
    from types import SimpleNamespace

    from privacyfence.step_up_config import StepUpConfig
    from privacyfence.web import routes_security

    page, save = shot
    monkeypatch.setattr(routes_security, "_recent_mints", lambda: [
        ("2026-09-26 09:14:02 UTC", "Issued a sign-in code that can approve (confirmed in the companion)"),
        ("2026-09-25 17:40:51 UTC", "Refused a sign-in code that can approve: the companion did not confirm it"),
    ])
    creds = [
        SimpleNamespace(credential_id="c1", label="MacBook Touch ID", created_at=1_780_000_000, backed_up=True),
        SimpleNamespace(credential_id="c2", label="YubiKey", created_at=1_785_000_000, backed_up=False),
    ]
    page.set_content(routes_security._render_security_page(
        principal=Principal(id="local", display_name="You"), creds=creds, csrf="c", step_up=StepUpConfig(),
        nonce="n", back_link=("/settings", "Back to settings"),
    ))
    save("fallback-local-security")


@pytest.mark.parametrize("case", ["no-longer-pending", "preparing", "not-authorized"])
def test_fallback_page(shot, local_server, case):
    """The documents with no shell (web_shell.plain_page): the two stand-ins for a card and the
    unauthenticated page."""
    page, save = shot
    server, web_ui = local_server
    approval = None
    if case != "not-authorized":
        _sign_in_local(page, server)
    try:
        if case == "not-authorized":
            page.goto(f"{server.base_url}/approvals")
        elif case == "preparing":
            approval, _ = web_ui.deferred_registry.register_or_coalesce(
                dedupe_key="review-preparing", connector="gmail", tool="gmail_get_message",
                gate_kind="review", request_id="review-preparing",
            )
            page.goto(f"{server.base_url}/approvals/{approval.id}")
        else:
            page.goto(f"{server.base_url}/approvals/no-such-approval")
        page.wait_for_load_state("load")
        save(f"fallback-{case}")
    finally:
        if approval is not None:
            web_ui.resolve(approval.id, "deny")


def _open_settings_section(page, base_url: str, section: str) -> None:
    page.goto(f"{base_url}/settings")
    page.wait_for_selector(".pf-navitem")
    page.locator(f'.pf-navitem[data-nav="{section}"]').evaluate("(el) => el.click()")
    page.wait_for_selector(".pf-page, .pf-detail-page, .pf-about-page")


# Every section an org admin sees; Connectors is local mode's alone (below).
@pytest.mark.parametrize("section", ["general", "auto_accept", "privacy", "audit", "agents", "about"])
def test_settings(shot, org_server, section):
    page, save = shot
    server, sessions = org_server
    _sign_in_org(page.context, server, sessions, principal=_ADMIN)
    _open_settings_section(page, server.base_url, section)
    save(f"settings-{section}")


def test_settings_local_connectors(shot, local_server_with_settings):
    """Local mode's Connectors section, the one section org mode never draws: every connector
    row, none of them authenticated."""
    page, save = shot
    server, _web_ui = local_server_with_settings
    _sign_in_local(page, server)
    _open_settings_section(page, server.base_url, "connectors")
    save("settings-local-connectors")


_WRITE = {"title": "Send email", "preview": {"To": "alice@example.com", "Subject": "Q3 numbers"},
          "details_text": "Hi Alice, the numbers are attached.", "is_read": False}
_READ = {"title": "Get file content", "preview": {"File": "Quarterly report", "Owner": "alice@example.com"},
         "details_text": "", "is_read": True, "claude_reason": "To summarise the quarter for you."}


# A three-page document with a line of text on each page, so the review shows the page images
# doing their job (_pdf_bytes() is one blank page, which renders as a white box).
_REPORT_PDF = text_pdf([
    ["Quarterly report, Q3", "Revenue up 12% on the quarter.", "Two new regions opened."],
    ["Regional results", "EMEA 1.2M (+8%)", "AMER 2.4M (+15%)"],
    ["Open risks", "One supplier contract renews in November."],
])


def _card_kinds() -> dict[str, dict]:
    icon = (Path(paths_module.__file__).parent / "resources" / "icon_512.png").read_bytes()
    return {
        "write": {**_WRITE, "layout": "narrow", "claude_reason": "You asked me to send Alice the numbers.",
                  "accept_all_choices": [("rule", "this recipient")]},
        "write-flagged": {**_WRITE, "layout": "narrow", "write_content_flags": ["Phone number", "IBAN"]},
        "read": {**_READ, "layout": "narrow", "accept_all_choices": [("a", "this folder"), ("b", "this owner")]},
        "read-pii": {**_READ, "layout": "wide", "pii_categories": ["Email address", "National ID"],
                     "preview_blocks": [{"type": "markdown", "text": _MARKDOWN_PREVIEW}]},
        "read-pdf": {**_READ, "layout": "wide", "pdf_bytes": _REPORT_PDF},
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


_DIALOGS = {
    # web_approval_ui.py's show_pii_confirmation_popup, and a choice picker (show_rule_choice_popup).
    "pii": lambda: build_confirmation_html(
        title="PrivacyFence — Possible PII Detected",
        message_lines=["PrivacyFence detected possible personal data in this content: Email address, National ID.",
                       "Are you sure you want to proceed?"],
        cancel_label="Cancel", confirm_label="Proceed",
    ),
    "choice": lambda: build_choice_html(
        title="PrivacyFence — Choose Auto-Accept Rule", prompt="Which rule should always allow this?",
        options=["i_am_owner", "approved_folder: Finance", "approved_sender: alice@example.com"],
    ),
}


@pytest.mark.parametrize("kind", list(_DIALOGS))
def test_dialog(shot, kind):
    """The PII confirmation and the choice dialog, as GET /approvals/{id} serves them."""
    page, save = shot
    page.set_content(_DIALOGS[kind]())
    page.wait_for_function("() => !document.querySelector('[data-pf-action][aria-disabled=\"true\"]')")
    save(f"dialog-{kind}")
