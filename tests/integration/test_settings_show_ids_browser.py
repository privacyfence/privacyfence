"""The ID/name toggle and the invalid-value styling on the Auto-accept page, in a real browser
(settings_window_html.py). Skipped when Playwright or a Chromium build is missing, the same posture
test_browser_smoke.py takes."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright (test-only) not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from privacyfence.settings_window_html import build_html  # noqa: E402
from tests.unit.test_settings_window_html import _make_state  # noqa: E402
from tests.website_site import chromium_launch_kwargs  # noqa: E402

pytestmark = pytest.mark.browser


def _item(item_id, name, status):
    return {"id": item_id, "short_id": item_id, "name": name, "status": status}


def _state():
    state = _make_state()
    state["auto_accept"]["rules"] = [{
        "id": "r-drive", "sentence": "Drive - folder My Drive: Reports, Finance: Old: allow read",
        "sentence_parts": ["Drive - folder ", ": allow read"],
        "value_items": [_item("fid-a", "My Drive: Reports", "resolved"), _item("fid-b", "Finance: Old", "invalid"),
                        _item("fid-c", None, "pending")],
        "connector": "drive", "connector_label": "Drive", "scope_type": "drive.folder", "scope": "drive.folder",
        "scope_verbs": ["read"], "value": "x", "value_ids": ["fid-a", "fid-b", "fid-c"],
        "verbs": [{"verb": "read", "family": "read"}], "covered_tools": ["drive_get_file_content"],
        "match_count": 0, "last_matched": "", "never_matched": True,
    }]
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
    pg.set_content(build_html(_state(), initial_section="auto_accept"))
    yield pg
    pg.close()


def test_values_show_by_name_and_the_invalid_one_is_marked(page):
    assert page.inner_text(".pf-aa-sentence").replace("⚠︎ ", "") == (
        "Drive - folder My Drive: Reports, Finance: Old, fid-c: allow read"
    )
    assert page.locator(".pf-aa-sentence .pf-val-invalid").inner_text().endswith("Finance: Old")
    assert page.locator(".pf-aa-sentence .pf-val-pending").inner_text() == "fid-c"
    assert "not found" in page.inner_text(".pf-val-legend")


def test_toggle_switches_to_ids_and_back(page):
    page.click("[data-show-ids-toggle]")
    assert "folder fid-a, fid-b, fid-c: allow read" in page.inner_text(".pf-aa-sentence").replace("⚠︎ ", "")
    assert page.locator(".pf-aa-sentence .pf-val-invalid").count() == 1
    assert page.get_attribute("[data-show-ids-toggle]", "aria-pressed") == "true"
    page.click("[data-show-ids-toggle]")
    assert "My Drive: Reports" in page.inner_text(".pf-aa-sentence")
