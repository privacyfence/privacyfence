"""Guardrail 12: every privacyfence.eu page is responsive, from a 320 px phone to a desktop.

Layout can only be measured in a rendering engine, so this is the website's one guardrail that
needs a browser. It loads every hand-written page (`website/**/index.html`, staged exactly as
pages.yml deploys them, see tests/website_site.py) at each tested viewport and checks the
measurable rules from the header comment of `website/styles.css`:

- no page-level horizontal scroll;
- no element's box extends past the viewport, except inside a scroll container (wide code and
  tables scroll in their own box, which is intended);
- every header destination is reachable: visible, or visible once the menu is opened;
- tap targets are at least 44 x 44 px below 1024 px, for header links, the menu toggle,
  buttons and footer links.

It also saves a full-page screenshot of every page at every width under
test-results/website-layout/, which tests.yml uploads for review.

External requests are stubbed: the download Worker answers with a fixed manifest (so
/download/'s cards render), and nothing else leaves the machine. Same skip posture as
test_download_page.py: skipped when playwright or its Chromium build is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.website_site import (
    API_ORIGIN,
    REPO,
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

WIDTHS = [320, 360, 390, 768, 1024, 1440]
PAGES = page_paths()
TAP_TARGET_BELOW = 1024
MIN_TAP = 44
SCREENSHOTS = REPO / "test-results" / "website-layout"


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


def _stub_network(page, site_url):
    def handle(route):
        url = route.request.url
        if url.startswith(site_url):
            route.continue_()
        elif url == f"{API_ORIGIN}/api/releases/stable":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(STABLE_MANIFEST))
        elif url.startswith(f"{API_ORIGIN}/api/"):
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')
        else:
            route.abort()

    page.route("**/*", handle)


# Every element whose box ends outside [0, innerWidth], skipping what is legitimately allowed to:
# content inside a scroll/clip container, content of a closed <details>, and the 1 px
# visually-hidden helpers.
OVERFLOW_JS = """
() => {
  const width = window.innerWidth;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.width <= 1 && rect.height <= 1) continue;
    if (el.closest('details:not([open]) > :not(summary)')) continue;
    let clipped = false;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const overflow = getComputedStyle(a).overflowX;
      if (overflow === 'auto' || overflow === 'scroll' || overflow === 'hidden' || overflow === 'clip') {
        clipped = true;
        break;
      }
    }
    if (clipped) continue;
    if (rect.right > width + 0.5 || rect.left < -0.5) {
      const name = el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
      offenders.push(`${name} [${Math.round(rect.left)}, ${Math.round(rect.right)}]`);
    }
  }
  return offenders;
}
"""

VISIBLE_HEADER_HREFS_JS = """
() => [...document.querySelectorAll('.site-header nav a')]
  .filter((a) => { const r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0
    && getComputedStyle(a).visibility !== 'hidden'; })
  .map((a) => a.getAttribute('href'))
"""

ALL_HEADER_HREFS_JS = (
    "() => [...new Set([...document.querySelectorAll('.site-header nav a')].map((a) => a.getAttribute('href')))]"
)

SMALL_TARGETS_JS = """
(min) => {
  const selector = '.site-header a, .site-header summary, .button, button, .footer-links a';
  const small = [];
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;  // not rendered at this width
    if (rect.width < min - 0.5 || rect.height < min - 0.5) {
      small.push(`${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 30)}" `
        + `${Math.round(rect.width)}x${Math.round(rect.height)}`);
    }
  }
  return small;
}
"""


def _open(browser, site_url, path, width):
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    _stub_network(page, site_url)
    page.goto(f"{site_url}{path}", wait_until="networkidle")
    return context, page


def _slug(path: str) -> str:
    return path.strip("/").replace("/", "_") or "home"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("path", PAGES)
def test_page_layout(browser, site_url, path, width):
    context, page = _open(browser, site_url, path, width)
    try:
        # The review screenshot first, so a failing page still leaves one behind. Lazy images
        # are made eager so a full-page capture shows them.
        page.evaluate("() => document.querySelectorAll('img[loading=lazy]').forEach((i) => { i.loading = 'eager'; })")
        page.wait_for_load_state("networkidle")
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS / f"{_slug(path)}-{width}.png"), full_page=True)

        scroll_width, inner_width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        assert scroll_width <= inner_width, f"{path} scrolls sideways at {width}px ({scroll_width} > {inner_width})"

        offenders = page.evaluate(OVERFLOW_JS)
        assert not offenders, f"{path} at {width}px: elements extend past the viewport: {offenders}"

        destinations = set(page.evaluate(ALL_HEADER_HREFS_JS))
        visible = set(page.evaluate(VISIBLE_HEADER_HREFS_JS))
        if destinations - visible:
            toggle = page.locator(".site-header .nav-menu summary")
            assert toggle.is_visible(), (
                f"{path} at {width}px hides {sorted(destinations - visible)} with no menu to reach them"
            )
            toggle.click()
            visible = set(page.evaluate(VISIBLE_HEADER_HREFS_JS))
            offenders = page.evaluate(OVERFLOW_JS)
            assert not offenders, f"{path} at {width}px: the open menu extends past the viewport: {offenders}"
        assert destinations <= visible, (
            f"{path} at {width}px: header destinations unreachable even from the menu: {sorted(destinations - visible)}"
        )

        if width < TAP_TARGET_BELOW:
            small = page.evaluate(SMALL_TARGETS_JS, MIN_TAP)
            assert not small, f"{path} at {width}px: tap targets under {MIN_TAP}px: {small}"
    finally:
        context.close()


def test_every_page_is_covered():
    # page_paths() globs website/; this pins that the glob still finds the pages the site has,
    # so a moved directory can't silently shrink the test to nothing.
    assert {"/", "/download/", "/privacy/", "/imprint/"} <= set(PAGES)


def test_desktop_shows_inline_links_and_no_menu(browser, site_url):
    context, page = _open(browser, site_url, "/", 1440)
    try:
        assert page.locator(".site-header .nav-links").is_visible()
        assert not page.locator(".site-header .nav-menu").is_visible()
    finally:
        context.close()


def test_the_menu_works_by_keyboard(browser, site_url):
    context, page = _open(browser, site_url, "/", 390)
    try:
        page.locator(".site-header .nav-menu summary").focus()
        page.keyboard.press("Enter")
        assert page.locator(".nav-menu-panel a").first.is_visible()
        page.keyboard.press("Enter")  # focus is still on the summary: toggles it closed again
        assert not page.locator(".nav-menu-panel a").first.is_visible()
    finally:
        context.close()


def test_skip_link_is_not_rendered_until_focused(browser, site_url):
    # It used to sit at top: -100px, which macOS/iOS elastic overscroll revealed at the top of
    # the page. Unfocused it must have no visible box at all; focused it must be on screen.
    context, page = _open(browser, site_url, "/", 1440)
    try:
        page.mouse.wheel(0, -400)
        box = page.locator(".skip-link").bounding_box()
        assert box is None or (box["width"] <= 1 and box["height"] <= 1)
        page.keyboard.press("Tab")
        box = page.locator(".skip-link").bounding_box()
        assert box and box["width"] > 40 and box["y"] >= 0
    finally:
        context.close()


def test_screenshots_directory_is_under_test_results():
    # tests.yml uploads test-results/website-layout/ as the review artifact; keep the two in step.
    workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "test-results/website-layout/" in workflow
    assert Path(SCREENSHOTS).relative_to(REPO).as_posix() == "test-results/website-layout"
