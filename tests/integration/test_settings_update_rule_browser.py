"""The Auto-accept page's "Update rule" notice in a real browser (settings_window_html.py).

Whether the notice and button appear, what the confirm says and what is posted are only observable
with a live DOM. Skipped when Playwright or a Chromium build is missing, the same posture
test_browser_smoke.py takes."""
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

NOTICE = ".pf-aa-missing"
BUTTON = '[data-action="update_policy_rule"]'


def _state(missing_tools):
    state = _make_state()
    state["auto_accept"]["rules"][0]["missing_tools"] = list(missing_tools)
    return state


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
    pg.set_content(build_html(_state([]), initial_section="auto_accept"))
    pg.evaluate("window.__posts = []; window.__confirms = []; window.__answer = true;"
                "window.confirm = function (t) { window.__confirms.push(t); return window.__answer; };"
                "window.webkit = {messageHandlers: {pf: {postMessage: function (m) { window.__posts.push(m); }}}};")
    yield pg
    pg.close()


def _render(page, state):
    page.evaluate("s => window.__pfRender(s)", copy.deepcopy(state))


def test_no_notice_or_button_for_a_rule_with_nothing_missing(page):
    _render(page, _state([]))
    assert page.query_selector(NOTICE) is None
    assert page.query_selector(BUTTON) is None


def test_the_notice_names_the_tools_and_the_button_carries_the_rule_id(page):
    _render(page, _state(["grist_import_csv", "grist_update_csv"]))
    text = page.inner_text(NOTICE)
    assert "Newer tools aren't covered yet: grist_import_csv, grist_update_csv." in text
    assert page.get_attribute(BUTTON, "data-payload") == '{"rule_id":"r-abc123"}'


def test_the_confirm_names_the_tools_and_a_yes_posts_the_update(page):
    _render(page, _state(["grist_import_csv"]))
    page.click(BUTTON)
    (message,) = page.evaluate("window.__confirms")
    assert "grist_import_csv" in message
    assert page.evaluate("window.__posts") == [{"action": "update_policy_rule", "rule_id": "r-abc123"}]


def test_declining_the_confirm_posts_nothing(page):
    _render(page, _state(["grist_import_csv"]))
    page.evaluate("window.__answer = false")
    page.click(BUTTON)
    assert page.evaluate("window.__confirms.length") == 1
    assert page.evaluate("window.__posts") == []
