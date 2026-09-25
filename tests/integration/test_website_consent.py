"""Guardrail 13: nothing third-party happens on privacyfence.eu before the visitor consents.

Google Analytics sets cookies, so on an EU site it needs prior opt-in consent, and the site
promises more than the minimum: *no* request to Google at all before "Accept" (Consent Mode's
basic implementation; see docs/adr/0050-website-analytics-is-ga4-behind-consent.md). This
drives every page in headless Chromium and checks both halves:

- with no stored choice, a page requests nothing from any origin but the site itself and the
  project's own download Worker, and sets no cookie;
- "Accept" loads googletagmanager.com, now and on the next page; "Decline" loads nothing, and
  that choice persists across pages; "Cookie settings" in the footer reopens the choice;
- the GA4 config carries the page's content group, and a download button click sends
  `download_click` only after consent.

The download Worker (downloads.privacyfence.eu) is first-party: the project runs it and it
stores counters only (see /privacy/). The homepage asks it for the download total and
/download/ for the release list, so it is allowed on every page.

Google is never contacted: gtag.js is answered with an empty script, so what is tested is that
the page *asks* for it, and when. Same skip posture as test_download_page.py.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from tests.website_site import (
    API_ORIGIN,
    GA_ORIGIN,
    STABLE_MANIFEST,
    chromium_launch_kwargs,
    page_paths,
    serve,
    stage_site,
)

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]

PAGES = page_paths()
FIRST_PARTY_HOSTS = {urlsplit(API_ORIGIN).netloc}
BANNER = ".consent-banner"


@pytest.fixture(scope="module")
def site_url(tmp_path_factory):
    with serve(stage_site(tmp_path_factory.mktemp("site"))) as url:
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
    """One browser context with every outgoing request recorded and nothing leaving the machine."""

    def __init__(self, browser, site_url):
        self.site_url = site_url
        self.context = browser.new_context()
        self.requests: list[str] = []
        self.context.on("request", lambda request: self.requests.append(request.url))
        self.context.route("**/*", self._handle)
        self.page = self.context.new_page()

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

    def third_party(self) -> list[str]:
        site_host = urlsplit(self.site_url).netloc
        return [url for url in self.requests if urlsplit(url).netloc not in {site_host, *FIRST_PARTY_HOSTS}]

    def ga_requests(self) -> list[str]:
        return [url for url in self.requests if url.startswith(GA_ORIGIN)]

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
def test_no_third_party_request_and_no_cookie_before_a_choice(visit, path):
    v = visit()
    v.goto(path)
    assert v.page.locator(BANNER).is_visible(), f"{path} shows no consent banner to a first-time visitor"
    assert not v.third_party(), f"{path} contacted a third party before consent: {v.third_party()}"
    assert v.context.cookies() == [], f"{path} set cookies before consent: {v.context.cookies()}"


@pytest.mark.parametrize("path", PAGES)
def test_accept_loads_google_analytics_with_the_pages_content_group(visit, path):
    v = visit()
    v.goto(path)
    v.page.click(f"{BANNER} [data-consent=granted]")
    v.page.wait_for_load_state("networkidle")
    assert v.ga_requests(), f"{path}: accepting did not load Google Analytics"
    assert not v.page.locator(BANNER).is_visible()

    group = v.page.get_attribute('meta[name="pf-content-group"]', "content")
    config = v.page.evaluate("() => (window.dataLayer || []).map((e) => Array.from(e)).find((e) => e[0] === 'config')")
    assert config is not None, "gtag was never configured"
    assert config[1] == "G-7Z3PFP4XPT"
    assert config[2]["content_group"] == group
    assert config[2]["allow_google_signals"] is False
    assert config[2]["allow_ad_personalization_signals"] is False


def test_accepting_carries_over_to_the_next_page(visit):
    v = visit()
    v.goto("/")
    v.page.click(f"{BANNER} [data-consent=granted]")
    v.requests.clear()
    v.goto("/download/")
    assert not v.page.locator(BANNER).is_visible(), "the banner came back after accepting"
    assert v.ga_requests(), "Google Analytics did not load on the next page after accepting"


def test_declining_loads_nothing_and_persists_across_pages(visit):
    v = visit()
    v.goto("/")
    v.page.click(f"{BANNER} [data-consent=denied]")
    v.page.wait_for_load_state("networkidle")
    assert not v.page.locator(BANNER).is_visible()
    for path in PAGES:
        v.goto(path)
        assert not v.page.locator(BANNER).is_visible(), f"{path} asked again after declining"
    assert not v.third_party(), f"declining still contacted a third party: {v.third_party()}"
    assert v.context.cookies() == []


def test_cookie_settings_reopens_the_choice_and_can_withdraw_consent(visit):
    v = visit()
    v.goto("/privacy/")
    v.page.click(f"{BANNER} [data-consent=granted]")
    v.page.click("[data-cookie-settings]")
    assert v.page.locator(BANNER).is_visible()
    v.page.click(f"{BANNER} [data-consent=denied]")
    stored = v.page.evaluate("() => localStorage.getItem('pf-analytics-consent')")
    assert stored == "denied"
    assert v.page.evaluate("() => window['ga-disable-G-7Z3PFP4XPT']") is True

    v.requests.clear()
    v.goto("/")
    assert not v.ga_requests(), "Google Analytics loaded again after consent was withdrawn"


def test_accept_and_decline_carry_equal_weight(visit):
    v = visit()
    v.goto("/")
    buttons = v.page.locator(f"{BANNER} button")
    assert buttons.count() == 2
    accept, decline = (buttons.nth(i) for i in range(2))
    assert accept.get_attribute("class") == decline.get_attribute("class")
    a, d = accept.bounding_box(), decline.bounding_box()
    assert abs(a["width"] - d["width"]) < 2 and abs(a["height"] - d["height"]) < 2


def _download_click_events(page):
    return page.evaluate(
        "() => (window.dataLayer || []).map((e) => Array.from(e))"
        ".filter((e) => e[0] === 'event' && e[1] === 'download_click').map((e) => e[2])"
    )


def _click_first_download(page):
    # Stop the navigation so the page (and its dataLayer) survives the click.
    page.evaluate("() => document.addEventListener('click', (e) => e.preventDefault())")
    page.locator(".download-grid:not(.compact) .download-button").first.click()


def test_download_click_is_sent_only_after_consent(visit):
    declined = visit()
    declined.goto("/download/")
    declined.page.click(f"{BANNER} [data-consent=denied]")
    _click_first_download(declined.page)
    assert _download_click_events(declined.page) == []
    assert not declined.ga_requests()

    accepted = visit()
    accepted.goto("/download/")
    accepted.page.click(f"{BANNER} [data-consent=granted]")
    _click_first_download(accepted.page)
    events = _download_click_events(accepted.page)
    assert len(events) == 1
    first = STABLE_MANIFEST["artifacts"][0]
    assert events[0] == {"platform": first["platform"], "architecture": first["architecture"], "channel": "stable"}
