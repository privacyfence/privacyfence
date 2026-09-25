"""Guardrail 12: every privacyfence.eu page is responsive, from a 320 px phone to a desktop.

Layout can only be measured in a rendering engine, so this is the website's one guardrail that
needs a browser. It loads every page in the build manifest -- each hand-written page, and a sample
of /docs/ pages when the docs were built (getting-started, tools-reference for its wide tables,
the organization guide for its long code blocks) -- from the site exactly as
scripts/build_site.py builds it (see tests/website_site.py), at each tested viewport, and checks
the measurable rules from the header comment of `website/styles.css`:

- no page-level horizontal scroll;
- no element's box extends past the viewport, except inside a scroll container (wide code and
  tables scroll in their own box, which is intended);
- every header destination is reachable: visible, or visible once the menu is opened;
- tap targets are at least 44 x 44 px below 1024 px, for header links, the menu toggle,
  buttons and footer links.

It also saves a full-page screenshot of every page at every width under
test-results/website-layout/, which tests.yml uploads for review. The screenshot is a review
artifact, not an assertion: when Chromium refuses the capture it is retried once, then taken of the
viewport only, then skipped with a warning -- it never fails the layout test (see _save_screenshot).

External requests are stubbed: the download Worker answers with a fixed manifest (so
/download/'s cards render), and nothing else leaves the machine. Same skip posture as
test_download_page.py: skipped when playwright or its Chromium build is missing.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from tests.website_site import (
    build_site,
    docs_built,
    API_ORIGIN,
    REPO,
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

WIDTHS = [320, 360, 390, 768, 1024, 1440]
PAGES = page_paths()
TAP_TARGET_BELOW = 1024
MIN_TAP = 44
SCREENSHOTS = REPO / "test-results" / "website-layout"


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


def _stub_network(page, site_url):
    def handle(route):
        url = route.request.url
        if url.startswith(site_url):
            route.continue_()
        elif url == f"{API_ORIGIN}/api/releases/stable":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(STABLE_MANIFEST))
        elif url == f"{API_ORIGIN}/api/releases":
            # /releases/'s table, filled so its scroll container is what gets measured.
            body = {"channels": {"stable": STABLE_MANIFEST, "alpha": None, "beta": None, "rc": None}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        elif url.startswith(f"{API_ORIGIN}/api/"):
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')
        else:
            route.abort()

    page.route("**/*", handle)


# Every element whose box ends outside [0, innerWidth], skipping what is legitimately allowed to:
# content inside a scroll/clip container, content of a closed <details>, the 1 px
# visually-hidden helpers, and a closed off-canvas drawer (the /docs/ navigation on a phone), which
# sits entirely left of the viewport until opened -- test_docs_navigation_drawer_opens_on_screen
# checks it once open.
OVERFLOW_JS = """
() => {
  const width = window.innerWidth;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.width <= 1 && rect.height <= 1) continue;
    if (el.closest('details:not([open]) > :not(summary)')) continue;
    if (rect.right <= 0.5) continue;
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


def _save_screenshot(page, target: Path) -> str | None:
    """Save the review screenshot; return "full", "viewport", or None if none could be taken.

    Chromium intermittently answers a full-page capture with "Protocol error
    (Page.captureScreenshot): Unable to capture screenshot" on a page it captured fine at every
    other width (PR #732's website-build run failed on /security/ at 1024 px this way, with every
    layout assertion unreached). The screenshot only feeds the uploaded review artifact, so a
    refused capture must not fail the layout checks that follow it: retry once, fall back to the
    viewport, and warn rather than raise if even that is refused.
    """
    for attempt in range(2):
        try:
            page.screenshot(path=str(target), full_page=True)
            return "full"
        except PlaywrightError:
            if attempt == 0:
                page.wait_for_timeout(250)
    try:
        page.screenshot(path=str(target))
    except PlaywrightError as exc:
        warnings.warn(f"no layout screenshot for {target.name}: {exc}", stacklevel=2)
        return None
    warnings.warn(f"{target.name} is a viewport-only screenshot: the full-page capture was refused twice", stacklevel=2)
    return "viewport"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("path", PAGES)
def test_page_layout(browser, site_url, path, width):
    context, page = _open(browser, site_url, path, width)
    try:
        # The review screenshot first, so a failing page still leaves one behind. Lazy images
        # are made eager so a full-page capture shows them. Best effort: see _save_screenshot.
        page.evaluate("() => document.querySelectorAll('img[loading=lazy]').forEach((i) => { i.loading = 'eager'; })")
        page.wait_for_load_state("networkidle")
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        _save_screenshot(page, SCREENSHOTS / f"{_slug(path)}-{width}.png")

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
    # page_paths() reads the build manifest; this pins that it still lists the pages the site
    # has, so a manifest change can't silently shrink the test to nothing.
    assert {"/", "/download/", "/privacy/", "/imprint/"} <= set(PAGES)
    if docs_built():
        assert set(build_site.DOCS_LAYOUT_SAMPLE) <= set(PAGES)


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


@pytest.mark.parametrize("width", [320, 768])
def test_docs_navigation_drawer_opens_on_screen(browser, site_url, width):
    # Below the docs generator's own breakpoint the docs navigation is a drawer behind the menu
    # button in the docs bar. Opened, it must sit fully inside the viewport with every link in it.
    if not docs_built():
        pytest.skip("/docs/ was not built (the docs extra is not installed)")
    context, page = _open(browser, site_url, "/docs/getting-started/", width)
    try:
        page.locator('label.md-header__button[for="__drawer"]').click()
        page.wait_for_timeout(400)  # the drawer slides in
        drawer = page.locator(".md-sidebar--primary")
        box = drawer.bounding_box()
        assert box and box["x"] >= -0.5 and box["x"] + box["width"] <= width + 0.5, box
        assert page.evaluate(OVERFLOW_JS) == []
        # Sections open as sub-panels: the last one's docs are one tap away.
        drawer.get_by_text("Reference appendices", exact=True).first.click()
        page.wait_for_timeout(400)
        assert drawer.locator('a[href$="tools-reference/"]').first.is_visible()
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


class _RefusingPage:
    """Stands in for a Playwright page whose first `refusals` screenshot calls are refused."""

    def __init__(self, refusals: int):
        self.refusals = refusals
        self.calls: list[bool] = []  # full_page flag of each screenshot call

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self.calls.append(full_page)
        if len(self.calls) <= self.refusals:
            raise PlaywrightError("Protocol error (Page.captureScreenshot): Unable to capture screenshot")
        Path(path).write_bytes(b"png")

    def wait_for_timeout(self, timeout: float) -> None:
        pass


@pytest.mark.parametrize(
    ("refusals", "result", "calls"),
    [
        (0, "full", [True]),
        (1, "full", [True, True]),
        (2, "viewport", [True, True, False]),
        (3, None, [True, True, False]),
    ],
)
def test_a_refused_screenshot_never_fails_the_layout_test(tmp_path, refusals, result, calls):
    page = _RefusingPage(refusals)
    target = tmp_path / "security-1024.png"
    if result == "full":
        assert _save_screenshot(page, target) == result
    else:
        with pytest.warns(UserWarning, match="security-1024.png"):
            assert _save_screenshot(page, target) == result
    assert page.calls == calls
    assert target.exists() == (result is not None)
