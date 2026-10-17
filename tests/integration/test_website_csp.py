"""Guardrail: every page works under the Content-Security-Policy Cloudflare sends (WEB-9).

tests/website_site.py serves the built site with the headers from scripts/site_security_headers.py,
so Chromium enforces the real policy here. Every page is loaded with consent accepted (so the
Google Tag Manager loader is exercised) and must raise no `securitypolicyviolation` event and no
console message about the policy. Google is never contacted: gtag.js is answered with an empty
script. Same skip posture as test_website_consent.py.
"""

from __future__ import annotations

import json

import pytest

from tests.website_site import (
    API_ORIGIN,
    GA_ORIGIN,
    STABLE_MANIFEST,
    chromium_launch_kwargs,
    page_paths,
    serve,
    built_site,
)

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]

PAGES = page_paths()
BANNER = ".consent-banner"

_LISTENER = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__cspViolations.push(e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline'));
});
"""


@pytest.fixture(scope="module")
def site_url():
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


class Visit:
    def __init__(self, browser, site_url):
        self.site_url = site_url
        self.context = browser.new_context()
        self.context.add_init_script(_LISTENER)
        self.requests: list[str] = []
        self.console: list[str] = []
        self.context.on("request", lambda request: self.requests.append(request.url))
        self.context.route("**/*", self._handle)
        self.page = self.context.new_page()
        self.page.on("console", lambda m: self.console.append(m.text) if "Content Security Policy" in m.text else None)

    def _handle(self, route):
        url = route.request.url
        if url.startswith(self.site_url):
            route.continue_()
        elif url == f"{API_ORIGIN}/api/releases/stable":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(STABLE_MANIFEST))
        elif url.startswith(f"{API_ORIGIN}/download/"):
            route.fulfill(status=204, body="")
        elif url.startswith(API_ORIGIN):
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')
        elif url.startswith(GA_ORIGIN):
            route.fulfill(status=200, content_type="application/javascript", body="")
        else:
            route.abort()

    def goto(self, path):
        self.page.goto(f"{self.site_url}{path}", wait_until="networkidle")

    def accept(self):
        with self.page.expect_request(lambda request: request.url.startswith(GA_ORIGIN), timeout=10_000):
            self.page.click(f"{BANNER} [data-consent=granted]")

    def violations(self) -> list[str]:
        return self.page.evaluate("() => window.__cspViolations || []")

    def close(self):
        self.context.close()


@pytest.fixture
def visit(browser, site_url):
    visits = []

    def open_visit():
        v = Visit(browser, site_url)
        visits.append(v)
        return v

    yield open_visit
    for v in visits:
        v.close()


@pytest.mark.parametrize("path", PAGES)
def test_no_page_violates_its_csp(visit, path):
    v = visit()
    v.goto(path)
    assert v.page.locator(BANNER).is_visible(), f"{path} shows no consent banner"
    v.accept()
    v.page.reload(wait_until="networkidle")
    assert not v.violations(), f"{path} violated its CSP: {v.violations()}"
    assert not v.console, f"{path} logged CSP errors: {v.console}"


def test_the_consent_flow_loads_google_tag_manager_under_the_csp(visit):
    v = visit()
    v.goto("/")
    v.accept()
    assert any(url.startswith(GA_ORIGIN) for url in v.requests), "accepting did not request Google Tag Manager"
    assert not v.violations(), f"accepting violated the CSP: {v.violations()}"
    assert not v.console, f"accepting logged CSP errors: {v.console}"
    assert not v.page.locator(BANNER).is_visible()
