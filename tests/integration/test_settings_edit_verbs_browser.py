"""The Auto-accept page's "Edit verbs" panel in a real browser (settings_window_html.py).

Opening the panel, toggling chips, the disabled `move` toggle and what Save posts are only
observable with a live DOM. Skipped when Playwright or a Chromium build is missing, the same posture
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

DRIVE_VERBS = ["read", "download", "add", "update", "restructure", "move", "delete"]


def _row(rule_id, scope, scope_verbs, verbs, value_ids, scope_type=None):
    return {
        "id": rule_id, "sentence": f"{scope} rule", "connector": scope.split(".")[0],
        "connector_label": scope.split(".")[0].title(), "scope_type": scope_type or scope, "scope": scope,
        "scope_verbs": scope_verbs, "value": ", ".join(value_ids), "value_ids": value_ids,
        "verbs": [{"verb": v, "family": "read"} for v in verbs], "covered_tools": ["drive_get_file_content"],
        "match_count": 0, "last_matched": "", "never_matched": True,
    }


def _state():
    state = _make_state()
    state["auto_accept"]["rules"] = [
        _row("r-drive", "drive.folder", DRIVE_VERBS, ["read"], ["FOLDER1"]),
        _row("r-domain", "gmail.sender_domain", ["read", "download", "move"], ["read"], ["example.com"]),
        _row("r-two", "drive.folder", DRIVE_VERBS, ["read"], ["FOLDER1", "FOLDER2"]),
        {**_row("r-custom", "gmail.sender", [], ["read"], []), "scope": None, "scope_verbs": []},
    ]
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
    pg.evaluate("window.__posts = []; window.__confirms = []; window.__answer = true;"
                "window.confirm = function (t) { window.__confirms.push(t); return window.__answer; };"
                "window.webkit = {messageHandlers: {pf: {postMessage: function (m) { window.__posts.push(m); }}}};")
    yield pg
    pg.close()


def _row_el(page, rule_id):
    return f'.pf-aa-row:has([data-aa-edit="{rule_id}"])'


def _open(page, rule_id):
    page.click(f'[data-aa-edit="{rule_id}"]')


def _chip(rule_id, verb):
    return f'{_row_el(None, rule_id)} [data-aa-edit-verb="{verb}"]'


def test_a_custom_row_has_no_edit_link(page):
    assert page.query_selector('[data-aa-edit="r-custom"]') is None
    assert page.query_selector('[data-aa-edit="r-drive"]') is not None


def test_adding_update_and_confirming_posts_and_the_chips_change(page):
    _open(page, "r-drive")
    assert page.get_attribute(_chip("r-drive", "read"), "aria-checked") == "true"
    assert page.get_attribute(_chip("r-drive", "update"), "aria-checked") == "false"

    page.click(_chip("r-drive", "update"))
    save = f'{_row_el(page, "r-drive")} [data-aa-edit-save]'
    page.click(save)

    (message,) = page.evaluate("window.__confirms")
    assert message.startswith("Change this rule to allow read, update? Added verbs cover all of their tools.")
    assert page.evaluate("window.__posts") == [
        {"action": "set_policy_rule_verbs", "rule_id": "r-drive", "verbs": ["read", "update"]},
    ]
    # The server answers with a fresh snapshot; the panel closed and the row shows the new chips.
    state = _state()
    state["auto_accept"]["rules"][0]["verbs"] = [
        {"verb": "read", "family": "read"}, {"verb": "update", "family": "write"},
    ]
    page.evaluate("s => window.__pfRender(s)", copy.deepcopy(state))
    assert page.query_selector("[data-aa-edit-verb]") is None
    chips = page.inner_text(f'{_row_el(page, "r-drive")} .pf-aa-verbs')
    assert "read" in chips and "update" in chips


def test_declining_the_confirm_posts_nothing(page):
    _open(page, "r-drive")
    page.click(_chip("r-drive", "update"))
    page.evaluate("window.__answer = false")
    page.click(f'{_row_el(page, "r-drive")} [data-aa-edit-save]')
    assert page.evaluate("window.__posts") == []


def test_save_is_disabled_while_no_verb_is_selected(page):
    _open(page, "r-drive")
    page.click(_chip("r-drive", "read"))
    save = f'{_row_el(page, "r-drive")} [data-aa-edit-save]'
    assert page.get_attribute(save, "aria-disabled") == "true"
    page.click(save, force=True)  # Playwright treats aria-disabled as not actionable
    assert page.evaluate("window.__confirms") == []
    assert page.evaluate("window.__posts") == []


def test_cancel_closes_the_panel_without_posting(page):
    _open(page, "r-drive")
    page.click(f'{_row_el(page, "r-drive")} [data-aa-edit-cancel]')
    assert page.query_selector("[data-aa-edit-verb]") is None
    assert page.evaluate("window.__posts") == []


def test_a_gmail_sender_domain_row_with_one_value_can_toggle_move(page):
    _open(page, "r-domain")
    assert page.get_attribute(_chip("r-domain", "move"), "aria-disabled") is None
    page.click(_chip("r-domain", "move"))
    assert page.get_attribute(_chip("r-domain", "move"), "aria-checked") == "true"
    assert page.query_selector(f'{_row_el(page, "r-domain")} .pf-aa-pair-hint') is None
    page.click(f'{_row_el(page, "r-domain")} [data-aa-edit-save]')
    assert page.evaluate("window.__posts") == [
        {"action": "set_policy_rule_verbs", "rule_id": "r-domain", "verbs": ["read", "move"]},
    ]


def test_a_drive_folder_row_with_one_id_cannot_toggle_move(page):
    _open(page, "r-drive")
    assert page.get_attribute(_chip("r-drive", "move"), "aria-disabled") == "true"
    page.click(_chip("r-drive", "move"), force=True)
    assert page.get_attribute(_chip("r-drive", "move"), "aria-checked") == "false"
    assert "source folder and its destination folder" in page.inner_text(_row_el(page, "r-drive"))


def test_a_drive_folder_row_with_two_ids_can_toggle_move(page):
    _open(page, "r-two")
    assert page.get_attribute(_chip("r-two", "move"), "aria-disabled") is None
    page.click(_chip("r-two", "move"))
    assert page.get_attribute(_chip("r-two", "move"), "aria-checked") == "true"
