"""The Grist API-key modal in a real browser (settings_window_html.py).

The modal is rebuilt on every render, and its result arrives by snapshot push, so what is typed
into it, where focus sits and when it closes are only observable with a live DOM. Skipped when
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

KEY = '[data-grist-field="api_key"]'
URL = '[data-grist-field="server_url"]'


def _state(*, busy=False, error=""):
    state = _make_state(grist_signin="api_key", grist_auth={"error": error})
    state["connectors"].append({
        "key": "grist", "label": "Grist", "icon": "grist", "icon_data_uri": "", "authed": False,
        "enabled": True, "busy": busy, "has_org": True, "auth_label": "Authenticate…",
    })
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
    pg.set_content(build_html(_state(), initial_section="connectors"))
    pg.evaluate("window.__posts = []; window.webkit = {messageHandlers: {pf: "
                "{postMessage: function (m) { window.__posts.push(m); }}}};")
    yield pg
    pg.close()


def _render(page, state):
    page.evaluate("s => window.__pfRender(s)", copy.deepcopy(state))


def _open(page):
    page.click("[data-grist-auth]")
    page.wait_for_selector(KEY)


def _modal_open(page):
    return page.query_selector(KEY) is not None


def test_typed_values_and_focus_survive_an_unrelated_snapshot(page):
    _open(page)
    assert page.evaluate("document.activeElement.getAttribute('data-grist-field')") == "api_key"
    page.fill(URL, "https://grist.example.org")
    page.fill(KEY, "typed-key")
    page.focus(URL)
    _render(page, _state())
    assert page.input_value(KEY) == "typed-key"
    assert page.input_value(URL) == "https://grist.example.org"
    assert page.evaluate("document.activeElement.getAttribute('data-grist-field')") == "server_url"


def test_early_snapshot_does_not_close_and_a_failure_stays_visible(page):
    _open(page)
    page.fill(KEY, "wrong-key")
    page.click("[data-grist-submit]")
    assert page.evaluate("window.__posts.filter(m => m.action === 'grist_connect').length") == 1
    assert page.input_value(KEY) == ""  # the key is not kept after submit
    _render(page, _state())  # a snapshot before the worker marked the row busy
    assert _modal_open(page)
    _render(page, _state(busy=True))
    assert _modal_open(page)
    _render(page, _state(error="Grist rejected the API key."))
    assert _modal_open(page)
    assert "Grist rejected the API key." in page.inner_text(".pf-modal-error")


def test_success_closes_once_the_row_was_seen_busy(page):
    _open(page)
    page.fill(KEY, "good-key")
    page.click("[data-grist-submit]")
    _render(page, _state(busy=True))
    _render(page, _state())
    assert not _modal_open(page)
