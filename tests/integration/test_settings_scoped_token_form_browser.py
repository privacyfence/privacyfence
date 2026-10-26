"""The Scoped tokens form's Duplicate button and rule-filtered tool list in a real browser
(settings_window_html.py).

What the form holds after a click, and which tools are drawn, are only observable with a live DOM.
Skipped when Playwright or a Chromium build is missing, the same posture test_browser_smoke.py takes."""
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

DAY = 86400
TOOLS = {
    "gmail": ["gmail_get_message", "gmail_send_message", "gmail_list_messages"],
    "drive": ["drive_get_file_content", "drive_list_files"],
    "tasks": ["tasks_list_tasks"],
}
DATA = {
    "delivery": "companion", "max_days": 365, "default_days": 90, "warn_days": 14,
    "tokens": [
        {"id": "st-1", "label": "Daily sync", "created_at": 0, "expires_at": 30 * DAY, "days_left": 20,
         "expiring_soon": False, "expired": False,
         "rules": [{"id": "r-gmail", "sentence": "Gmail: allow read"},
                   {"id": "r-drive", "sentence": "Drive: allow read"}],
         "tools": ["gmail_get_message", "tasks_list_tasks"], "allow_personal_data": True},
        {"id": "st-2", "label": "Daily sync 2", "created_at": 0, "expires_at": 500 * DAY, "days_left": 0,
         "expiring_soon": False, "expired": True,
         "rules": [{"id": "r-gmail", "sentence": "Gmail: allow read"}],
         "tools": ["gmail_get_message"], "allow_personal_data": False},
    ],
    "rules": [
        {"id": "r-gmail", "title": "Gmail - anything", "verbs": ["read"], "sentence": "Gmail: allow read",
         "tools": ["gmail_get_message", "gmail_send_message"]},
        {"id": "r-drive", "title": "Drive - anything", "verbs": ["read"], "sentence": "Drive: allow read",
         "tools": ["drive_get_file_content"]},
    ],
    "tools": [{"connector": c, "tools": t} for c, t in TOOLS.items()],
    "auto_tools": ["gmail_list_messages", "drive_list_files", "tasks_list_tasks"],
}


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


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    pg.set_content(build_html(_make_state(), initial_section="scoped_tokens", scoped_tokens=copy.deepcopy(DATA)))
    yield pg
    pg.close()


def _duplicate(page, label):
    page.click(f'[aria-label="Duplicate {label}"]')


def _ticked(page):
    return sorted(page.eval_on_selector_all(
        '[data-st-tool][aria-checked="true"]', "els => els.map(e => e.getAttribute('data-st-tool'))"))


def _shown(page):
    return sorted(page.eval_on_selector_all(
        '.pf-chips:not([data-st-other-tools] *) > [data-st-tool]', "els => els.map(e => e.getAttribute('data-st-tool'))"))


def _select_rule(page, rule_id):
    page.click(f'[data-st-rule="{rule_id}"]')


def test_duplicate_prefills_the_form(page):
    _duplicate(page, "Daily sync")
    assert page.input_value("[data-st-label]") == "Daily sync 3"
    assert page.input_value("[data-st-days]") == "30"
    rules = page.eval_on_selector_all('[data-st-rule][aria-checked="true"]', "els => els.map(e => e.getAttribute('data-st-rule'))")
    assert sorted(rules) == ["r-drive", "r-gmail"]
    assert _ticked(page) == ["gmail_get_message", "tasks_list_tasks"]
    assert page.get_attribute("[data-st-personal-data]", "aria-checked") == "true"
    assert page.evaluate("document.activeElement.hasAttribute('data-st-label')")


def test_duplicate_of_expired_token_is_offered(page):
    _duplicate(page, "Daily sync 2")
    assert page.input_value("[data-st-label]") == "Daily sync 3"
    # 500 days is cut to the policy's maximum
    assert page.input_value("[data-st-days]") == "365"
    assert _ticked(page) == ["gmail_get_message"]
    assert page.query_selector("[data-st-dup-missing]") is None


def test_tools_follow_the_selected_rules(page):
    assert page.query_selector("[data-st-no-rule-hint]") is not None
    assert _shown(page) == []
    _select_rule(page, "r-gmail")
    assert page.query_selector("[data-st-no-rule-hint]") is None
    # covered tools and the same connector's no-rule tool; drive and tasks stay out of the open list
    assert _shown(page) == ["gmail_get_message", "gmail_list_messages", "gmail_send_message"]


def test_no_rule_selected_lists_the_tools_that_need_no_rule_open(page):
    assert page.inner_text("[data-st-no-rule-hint]") == (
        "Select a rule to see the tools it covers, or pick a tool that needs no rule.")
    assert page.inner_text("[data-st-other-tools] summary") == "Tools that need no rule (3)"
    assert page.get_attribute("[data-st-other-tools]", "open") is not None
    assert page.is_visible('[data-st-other-tools] [data-st-tool="gmail_list_messages"]')
    page.click('[data-st-tool="gmail_list_messages"]')
    assert _ticked(page) == ["gmail_list_messages"]


def test_ticked_tool_stays_visible_when_its_rule_is_deselected(page):
    _select_rule(page, "r-gmail")
    page.click('[data-st-tool="gmail_send_message"]')
    _select_rule(page, "r-gmail")
    assert "gmail_send_message" in _shown(page)
    assert page.query_selector("[data-st-uncovered]") is not None


def test_other_no_rule_tools_are_collapsed(page):
    _select_rule(page, "r-gmail")
    summary = page.inner_text("[data-st-other-tools] summary")
    assert summary == "Tools that need no rule, from other connectors (2)"
    assert page.get_attribute("[data-st-other-tools]", "open") is None
    assert not page.is_visible("[data-st-other-tools] [data-st-tool]")
    page.click("[data-st-other-tools] summary")
    assert page.is_visible('[data-st-other-tools] [data-st-tool="tasks_list_tasks"]')


def test_reads_checkbox_is_sent_with_the_create_request(page):
    _capture_posts(page)
    page.fill("[data-st-label]", "triage")
    assert page.get_attribute("[data-st-personal-data-reads]", "aria-checked") == "false"
    page.click("[data-st-personal-data-reads]")
    assert page.get_attribute("[data-st-personal-data-reads]", "aria-checked") == "true"
    assert page.get_attribute("[data-st-personal-data]", "aria-checked") == "false"
    page.click("[data-st-create]")
    [posted] = page.evaluate("window.__posted")
    assert posted["action"] == "create_scoped_token"
    assert posted["allow_personal_data_reads"] is True
    assert posted["allow_personal_data"] is False


def test_clear_form_resets(page):
    _duplicate(page, "Daily sync")
    page.click("[data-st-clear]")
    assert page.input_value("[data-st-label]") == ""
    assert page.input_value("[data-st-days]") == "90"
    assert page.query_selector('[data-st-rule][aria-checked="true"]') is None
    assert _ticked(page) == []
    assert page.get_attribute("[data-st-personal-data]", "aria-checked") == "false"
    assert page.query_selector("[data-st-clear]") is None
    assert page.query_selector("[data-st-dup-missing]") is None


def _capture_posts(page):
    page.evaluate(
        "window.__posted = []; window.webkit = {messageHandlers: {pf: "
        "{postMessage: function (m) { window.__posted.push(m); }}}};"
    )


def test_a_token_is_renamed_from_the_modal(page):
    _capture_posts(page)
    page.click('[aria-label="Rename Daily sync"]')
    field = page.locator('.pf-modal input[aria-label="Token name"]')
    assert field.input_value() == "Daily sync"
    field.fill("Nightly sync")
    page.click('.pf-modal [aria-label="Save name"]')
    assert page.evaluate("window.__posted") == [
        {"action": "rename_scoped_token", "token_id": "st-1", "label": "Nightly sync"}
    ]
    assert page.locator(".pf-modal").count() == 0


def test_cancel_escape_and_an_unchanged_name_post_nothing(page):
    _capture_posts(page)
    for close in (
        lambda: page.click('.pf-modal [aria-label="Cancel rename"]'),
        lambda: page.keyboard.press("Escape"),
        lambda: page.click('.pf-modal [aria-label="Save name"]'),
    ):
        page.click('[aria-label="Rename Daily sync"]')
        assert page.locator(".pf-modal").count() == 1
        close()
        assert page.locator(".pf-modal").count() == 0
        assert page.evaluate("window.__posted") == []
