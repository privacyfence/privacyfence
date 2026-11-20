"""Organization auto-accept rules in a real browser (settings_window_html.py, plan 1167 section 3.5).

The Admin page's Organization rules section shares the Auto-accept page's add-rule form and Edit
verbs panel (``RULE_LISTS``); what those controls post from the Admin page, and the read-only
"Set by your organization" block on a person's Auto-accept page, are only observable with a live
DOM. The page's bridge (``window.webkit.messageHandlers.pf``), which on the Admin page runs the
passkey step before it sends an action, is replaced by a recorder, as in
test_settings_edit_verbs_browser.py. Skipped when Playwright or a Chromium build is missing, the
same posture test_browser_smoke.py takes."""
from __future__ import annotations

import copy

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright (test-only) not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from privacyfence.settings_window_html import build_html  # noqa: E402
from tests.unit.test_settings_window_html import _admin_state, _make_state  # noqa: E402
from tests.website_site import chromium_launch_kwargs  # noqa: E402

pytestmark = pytest.mark.browser

_PHONE = {"width": 375, "height": 812}
_RECORD_POSTS = (
    "window.__posts = []; window.__confirms = [];"
    "window.confirm = function (t) { window.__confirms.push(t); return true; };"
    "window.webkit = {messageHandlers: {pf: {postMessage: function (m) { window.__posts.push(m); }}}};"
)


def _org_rule(rule_id="org-r1", value_id="FOLDER9", verbs=("read",)):
    return {
        "id": rule_id, "sentence": f"Drive - in folder {value_id}: allow {', '.join(verbs)}",
        "connector": "drive", "connector_label": "Drive", "scope_type": "drive.folder", "scope": "drive.folder",
        "scope_verbs": ["read", "download"], "value": value_id, "value_ids": [value_id],
        "verbs": [{"verb": v, "family": "read"} for v in verbs], "covered_tools": ["drive_get_file_content"],
        "missing_tools": [],
    }


def _admin(*rules):
    aa = _make_state()["auto_accept"]
    return _admin_state(org_rules={"rules": list(rules), "scope_groups": aa["scope_groups"], "connectors": aa["connectors"]})


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


def _open(browser, html, viewport=None):
    pg = browser.new_page(viewport=viewport) if viewport else browser.new_page()
    pg.set_content(html)
    pg.evaluate(_RECORD_POSTS)
    return pg


@pytest.fixture
def admin_page(browser):
    pg = _open(browser, build_html(_admin(), surface="admin", is_admin=True, initial_section="org_rules"))
    yield pg
    pg.close()


def _no_horizontal_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_an_admin_adds_a_drive_folder_read_rule_and_sees_it_listed(admin_page):
    assert "Organization rules" in admin_page.inner_text(".pf-page-title")
    assert "prefer read verbs" in admin_page.inner_text(".pf-org-rules-warning")
    admin_page.select_option("[data-aa-group-select]", "drive.folder")
    admin_page.fill("[data-aa-value]", "FOLDER9")
    admin_page.click('[data-aa-verb-toggle="read"]')
    admin_page.click("[data-aa-add]")
    assert admin_page.evaluate("window.__posts") == [
        {"action": "add_org_rule", "group": "drive.folder", "value": "FOLDER9", "verbs": "read"},
    ]
    # The server answers with a fresh snapshot that lists the new rule.
    admin_page.evaluate("s => window.__pfRender(s)", copy.deepcopy(_admin(_org_rule())))
    row = admin_page.locator('.pf-aa-row:has([data-aa-edit="org-r1"])')
    assert "Drive - in folder FOLDER9: allow read" in row.inner_text()
    assert "Matched" not in row.inner_text()


def test_edit_verbs_posts_one_comma_separated_string(admin_page):
    admin_page.evaluate("s => window.__pfRender(s)", copy.deepcopy(_admin(_org_rule())))
    admin_page.click('[data-aa-edit="org-r1"]')
    admin_page.click('[data-aa-edit-verb="download"]')
    admin_page.click("[data-aa-edit-save]")
    assert admin_page.evaluate("window.__posts") == [
        {"action": "set_org_rule_verbs", "rule_id": "org-r1", "verbs": "read,download"},
    ]


def test_remove_posts_the_org_action(admin_page):
    admin_page.evaluate("s => window.__pfRender(s)", copy.deepcopy(_admin(_org_rule())))
    admin_page.click('.pf-aa-row [aria-label="Remove rule"]')
    assert admin_page.evaluate("window.__posts") == [{"action": "remove_org_rule", "rule_id": "org-r1"}]


def test_a_person_sees_org_rules_read_only(browser):
    state = _make_state(org_rules=[_org_rule()])
    pg = _open(browser, build_html(state, initial_section="auto_accept"))
    try:
        block = pg.locator("[data-org-rules-readonly]")
        assert "Set by your organization" in block.inner_text()
        assert "Only an administrator can change these." in block.inner_text()
        assert "Drive - in folder FOLDER9: allow read" in block.inner_text()
        assert block.locator('[aria-label="Remove rule"]').count() == 0
        assert block.locator("[data-action], [data-aa-edit], [role=button]").count() == 0
        # The person's own add-rule form still posts the person's action, with a list of verbs.
        pg.select_option("[data-aa-group-select]", "gmail.sender")
        pg.click('[data-aa-verb-toggle="read"]')
        pg.click("[data-aa-add]")
        assert pg.evaluate("window.__posts") == [
            {"action": "add_policy_rule", "group": "gmail.sender", "value": "", "verbs": ["read"]},
        ]
    finally:
        pg.close()


@pytest.mark.parametrize("surface", ["admin", "person"])
def test_both_sections_fit_a_phone(browser, surface):
    long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
    if surface == "admin":
        html = build_html(_admin(_org_rule(value_id=long_id)), surface="admin", is_admin=True, initial_section="org_rules")
    else:
        html = build_html(_make_state(org_rules=[_org_rule(value_id=long_id)]), initial_section="auto_accept")
    pg = _open(browser, html, viewport=_PHONE)
    try:
        pg.wait_for_selector(".pf-aa-row")
        _no_horizontal_overflow(pg)
    finally:
        pg.close()
