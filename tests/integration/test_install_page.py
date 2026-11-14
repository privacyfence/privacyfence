"""Browser tests for website/install/ (the install picker).

Runs the real page in headless Chromium against the built site: with JavaScript the picker shows
exactly the result that fits the chosen setup, and without it every path is listed.

Same skip posture as test_browser_smoke.py: skipped when playwright or its Chromium build is
missing rather than failing, since CI always has both.
"""

from __future__ import annotations

import pytest

from tests.website_site import built_site, chromium_launch_kwargs, serve

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]

DEFAULTS = {"who": "single", "bundle": "suite", "signin": "own", "apps": "builtin", "proxy": "caddy"}

# (choices that differ from the defaults, the one visible result)
CASES = [
    ({}, "suite-single"),
    ({"bundle": "standalone"}, "standalone-single"),
    ({"who": "org"}, "suite-caddy-idp"),
    ({"who": "org", "signin": "builtin"}, "suite-caddy-google"),
    ({"who": "org", "signin": "builtin", "proxy": "nginx"}, "suite-nginx-google"),
    ({"who": "org", "proxy": "nginx"}, "suite-no-config"),
    ({"who": "org", "signin": "builtin", "proxy": "other"}, "suite-no-config"),
    ({"who": "org", "bundle": "standalone"}, "standalone-caddy"),
    ({"who": "org", "bundle": "standalone", "proxy": "nginx"}, "standalone-proxy"),
]


@pytest.fixture(scope="module")
def website_server():
    with serve(built_site()) as url:
        yield url


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            instance = playwright.chromium.launch(**chromium_launch_kwargs())
            try:
                yield instance
            finally:
                instance.close()
    except PlaywrightError as exc:  # pragma: no cover -- environment-dependent
        pytest.skip(f"chromium unavailable: {exc}")


@pytest.fixture
def page(browser, website_server):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{website_server}/install/", wait_until="networkidle")
    try:
        yield page
    finally:
        context.close()


def _choose(page, **choices):
    for name, value in {**DEFAULTS, **choices}.items():
        page.check(f'input[name="{name}"][value="{value}"]')


def _visible_results(page):
    return page.eval_on_selector_all("[data-result]", "els => els.filter(e => !e.hidden).map(e => e.dataset.result)")


def test_the_picker_shows_the_default_result(page):
    assert page.is_visible("#install-picker")
    assert _visible_results(page) == ["suite-single"]


def test_choosing_an_organization_swaps_the_single_note_for_the_org_fieldsets(page):
    assert page.is_visible(".install-note[data-when='single']")
    assert not page.is_visible("fieldset[data-when='org']")
    page.check('input[name="who"][value="org"]')
    assert not page.is_visible(".install-note[data-when='single']")
    assert page.locator("fieldset[data-when='org']:visible").count() == 3


@pytest.mark.parametrize(("choices", "expected"), CASES)
def test_every_combination_shows_one_result(page, choices, expected):
    _choose(page, **choices)
    assert _visible_results(page) == [expected]


def test_the_builtin_sign_in_snippet_is_only_for_an_organization_with_it(page):
    snippet = "[data-result='standalone-caddy'] [data-when='signin-builtin']"
    _choose(page, who="org", bundle="standalone")
    assert not page.is_visible(snippet)
    _choose(page, who="org", bundle="standalone", signin="builtin")
    assert page.is_visible(snippet)
    assert not page.is_visible("[data-result='standalone-caddy'] [data-when='signin-own']")
    _choose(page, who="single", bundle="standalone", signin="builtin")
    assert not page.is_visible(snippet)


def test_without_javascript_every_path_is_listed(browser, website_server):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(f"{website_server}/install/")
        assert not page.is_visible("#install-picker")
        hidden = page.eval_on_selector_all("[data-result]", "els => els.filter(e => !e.checkVisibility()).length")
        assert hidden == 0
        assert page.locator("[data-result]").count() == 8
    finally:
        context.close()
