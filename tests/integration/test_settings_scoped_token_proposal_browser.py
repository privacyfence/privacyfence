"""The AI-proposed scoped token's review panel and delegation dialog in a real browser
(settings_window_html.py).

Focus, `disabled` and the posted payloads are only observable with a live DOM. Skipped when
Playwright or a Chromium build is missing, the same posture test_browser_smoke.py takes."""
from __future__ import annotations

import copy

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright (test-only) not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from privacyfence.settings_window_html import build_html  # noqa: E402
from tests.unit.test_settings_window_html import _make_state  # noqa: E402
from tests.website_site import chromium_launch_kwargs  # noqa: E402

pytestmark = pytest.mark.browser

HOSTILE = "<img src=x onerror=\"window.__pwned = true\">"
DATA = {
    "delivery": "companion", "max_days": 365, "default_days": 90, "warn_days": 14,
    "tokens": [], "rules": [], "tools": [{"connector": "gmail", "tools": ["gmail_get_message"]}],
    "auto_tools": [], "scope_groups": [],
    "proposals": [{"id": "p1", "label": "Nightly digest", "agent_label": "Claude", "expires_at": 4_000_000_000}],
    "proposals_enabled": True,
}
PROPOSAL = {
    "id": "p1", "label": "Nightly digest", "days": 30,
    "tools": ["gmail_get_message", "gmail_send_message"],
    "allow_personal_data": True, "allow_personal_data_reads": False,
    "rules": [
        {"request": {"kind": "new", "group": "gmail.anything", "value": "", "verbs": ["read", "send"]},
         "sentence": "Gmail: allow read, send"},
        {"request": {"kind": "new", "group": "drive.folder", "value": "abc", "verbs": ["read"]},
         "sentence": "Drive folder abc: allow read"},
    ],
    "agent_label": "Claude", "reason": "Run the digest every night", "source": "explicit",
    "uncovered_tools": [], "expires_at": 4_000_000_000,
}

CAPTURE = "window.__posts = []; window.webkit = {messageHandlers: {pf: {postMessage: m => window.__posts.push(m)}}};"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(**chromium_launch_kwargs())
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available for Playwright ({exc})")
            return
        yield b
        b.close()


def _open(browser, proposal):
    pg = browser.new_page()
    pg.set_content(build_html(_make_state(), initial_section="scoped_tokens",
                              scoped_tokens=copy.deepcopy(DATA), scoped_proposal=copy.deepcopy(proposal)))
    pg.evaluate(CAPTURE)
    return pg


@pytest.fixture
def page(browser):
    pg = _open(browser, PROPOSAL)
    yield pg
    pg.close()


def _posts(page):
    return page.evaluate("window.__posts")


def _continue(page):
    page.click("[data-sp-continue]")
    page.wait_for_selector("[data-sp-dialog]")


def test_review_panel_replaces_the_new_token_form(page):
    assert page.locator("[data-sp-panel]").count() == 1
    assert page.locator(".pf-st-form [data-st-create]").count() == 0
    assert page.locator("h2", has_text="New token").count() == 0
    assert page.locator("h2", has_text="Review proposed token").count() == 1
    assert page.locator("[data-sp-waiting]").inner_text().startswith("Nightly digest — proposed by Claude")
    assert page.get_attribute("[data-sp-days]", "max") == "30"
    assert page.locator("[data-sp-rule][aria-checked=true]").count() == 2
    assert page.locator("[data-sp-tool][aria-checked=true]").count() == 2


def test_delegate_is_disabled_until_the_name_is_typed_exactly(page):
    _continue(page)
    go = page.get_by_role("button", name="Delegate access")
    assert go.is_disabled()
    for wrong in ("nightly digest", "Nightly", "Nightly digest "):
        page.fill("[data-sp-typed]", wrong)
        assert go.is_disabled()
    page.fill("[data-sp-typed]", "Nightly digest")
    assert go.is_enabled()


def test_typing_keeps_focus_in_the_input(page):
    _continue(page)
    page.click("[data-sp-typed]")
    page.keyboard.type("Nightly")
    assert page.evaluate("document.activeElement.hasAttribute('data-sp-typed')")
    assert page.input_value("[data-sp-typed]") == "Nightly"


def test_cancel_is_focused_and_escape_cancels(page):
    _continue(page)
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Cancel"
    page.keyboard.press("Escape")
    assert page.locator("[data-sp-dialog]").count() == 0
    assert _posts(page) == []


def test_personal_data_boxes_start_unticked(page):
    pd = page.locator("[data-sp-pd]")
    assert pd.get_attribute("aria-checked") == "false"
    assert page.locator("[data-sp-pdr]").count() == 0
    assert "The AI client asked for this." in page.locator("[data-sp-panel]").inner_text()
    _continue(page)
    assert "personal data" not in page.locator("[data-sp-dialog]").inner_text()


def test_unticking_a_rule_a_verb_and_a_tool_narrows_the_request(page):
    page.click('[data-sp-rule="1"]')
    page.click('[data-sp-verb="0:send"]')
    page.click('[data-sp-tool="gmail_send_message"]')
    page.click("[data-sp-pd]")
    _continue(page)
    dialog = page.locator("[data-sp-dialog]").inner_text()
    assert "personal data" in dialog
    assert "Drive folder abc" not in dialog
    page.fill("[data-sp-typed]", "Nightly digest")
    page.get_by_role("button", name="Delegate access").click()
    (post,) = _posts(page)
    assert post["rules"] == [{"kind": "new", "group": "gmail.anything", "value": "", "verbs": ["read"]}]
    assert post["tools"] == ["gmail_get_message"]
    assert post["allow_personal_data"] is True
    assert post["allow_personal_data_reads"] is False


def test_delegate_posts_the_proposal_id_and_typed_label(page):
    _continue(page)
    page.fill("[data-sp-typed]", "Nightly digest")
    page.get_by_role("button", name="Delegate access").click()
    (post,) = _posts(page)
    assert post["action"] == "create_proposed_scoped_token"
    assert post["proposal_id"] == "p1"
    assert post["label"] == "Nightly digest"
    assert post["typed_label"] == "Nightly digest"
    assert post["days"] == 30
    assert len(post["rules"]) == 2
    assert post["allow_personal_data"] is False


def test_decline_posts_decline(page):
    page.click("[data-sp-decline]")
    assert _posts(page) == [{"action": "decline_scoped_token_proposal", "proposal_id": "p1"}]


def test_missing_proposal_shows_the_notice(browser):
    pg = _open(browser, {"missing": True})
    try:
        assert pg.locator("[data-sp-missing]").inner_text() == (
            "This token proposal has expired, was declined or was already used.")
        assert pg.locator("[data-sp-continue]").count() == 0
        assert pg.locator("[data-st-create]").count() == 0
    finally:
        pg.close()


def test_untrusted_reason_is_rendered_as_text(browser):
    hostile = {**PROPOSAL, "reason": HOSTILE, "agent_label": HOSTILE,
               "rules": [{**PROPOSAL["rules"][0], "sentence": HOSTILE}]}
    pg = _open(browser, hostile)
    try:
        assert HOSTILE in pg.locator("[data-sp-reason]").inner_text()
        assert pg.locator("[data-sp-panel] img").count() == 0
        _continue(pg)
        assert pg.locator("[data-sp-dialog] img").count() == 0
        assert pg.evaluate("window.__pwned") is None
    finally:
        pg.close()
