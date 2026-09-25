"""Browser tests for website/download/ (docs/downloads-and-release-kpi.md).

The download page is built entirely at runtime from the Cloudflare Worker's release manifests, so
nothing meaningful about it can be asserted by reading the HTML: the platform cards, filenames,
sizes and checksums only exist after `download.js` has fetched and rendered them. These tests run
the real page in headless Chromium with the API responses stubbed at the network layer
(`page.route`), which is what lets them assert the behaviours the page is meant to have --
"OS detection only highlights, never hides", "stats gracefully disappear if the stats API is
unavailable" -- rather than merely that some markup exists.

Stubbing rather than calling the live Worker is deliberate: a test that hit
downloads.privacyfence.eu would need the network, would be flaky on a Worker deploy, and -- worse
-- would increment the very production download counter it is meant to keep honest.

Same skip posture as test_browser_smoke.py: skipped when playwright or its Chromium build is
missing rather than failing, since CI always has both.
"""

from __future__ import annotations

import json

import pytest

from tests.website_site import build_site, built_site, chromium_launch_kwargs, serve

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]

API_ORIGIN = "https://downloads.privacyfence.eu"

STABLE_MANIFEST = {
    "schema": 1,
    "version": "4.3.0",
    "channel": "stable",
    "published_at": "2026-09-01T12:00:00Z",
    "artifacts": [
        {
            "id": "macos-arm64",
            "kind": "installer",
            "platform": "macos",
            "architecture": "arm64",
            "filename": "PrivacyFence-4.3.0.dmg",
            "key": "releases/stable/4.3.0/PrivacyFence-4.3.0.dmg",
            "size": 104857600,
            "sha256": "a" * 64,
        },
        {
            "id": "windows-x64",
            "kind": "installer",
            "platform": "windows",
            "architecture": "x64",
            "filename": "PrivacyFence-4.3.0-setup.exe",
            "key": "releases/stable/4.3.0/PrivacyFence-4.3.0-setup.exe",
            "size": 89128960,
            "sha256": "b" * 64,
        },
        {
            "id": "linux-x64",
            "kind": "installer",
            "platform": "linux",
            "architecture": "x64",
            "filename": "privacyfence_4.3.0_amd64.deb",
            "key": "releases/stable/4.3.0/privacyfence_4.3.0_amd64.deb",
            "size": 47028460,
            "sha256": "c" * 64,
        },
    ],
}

# An artifact id download.js's own PLATFORMS map has never heard of -- STABLE_MANIFEST's three
# plus this one, used by TestUnknownArtifactId below. Deliberately a plausible *future* platform
# rather than a made-up one: the point of the map's fallback is that adding a platform to a
# release ships it on the website with no website deploy at all.
STABLE_MANIFEST_WITH_UNKNOWN_ARTIFACT = {
    **STABLE_MANIFEST,
    "artifacts": [
        *STABLE_MANIFEST["artifacts"],
        {
            "id": "linux-arm64",
            "kind": "installer",
            "platform": "linux",
            "architecture": "arm64",
            "filename": "privacyfence_4.3.0_arm64.deb",
            "key": "releases/stable/4.3.0/privacyfence_4.3.0_arm64.deb",
            "size": 44000000,
            "sha256": "e" * 64,
        },
    ],
}

BETA_MANIFEST = {
    "schema": 1,
    "version": "4.4.0b1",
    "channel": "beta",
    "published_at": "2026-09-10T12:00:00Z",
    "artifacts": [
        {
            "id": "macos-arm64",
            "kind": "installer",
            "platform": "macos",
            "architecture": "arm64",
            "filename": "PrivacyFence-4.4.0b1.dmg",
            "key": "releases/beta/4.4.0b1/PrivacyFence-4.4.0b1.dmg",
            "size": 105906176,
            "sha256": "d" * 64,
        }
    ],
}


@pytest.fixture(scope="module")
def website_server():
    """Serves the built site (tests/website_site.py) over a real socket -- `file://` would make the
    page's relative fetches and its own origin behave differently from production. Built offline,
    so /download/ has no pre-rendered cards and everything on it comes from the stubbed API."""
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


def _open_download_page(
    browser,
    website_server,
    *,
    stable=STABLE_MANIFEST,
    prereleases=None,
    stats=None,
    user_agent=None,
):
    """Opens the download page with every Worker API call stubbed.

    `prereleases` maps a channel name to its manifest; every channel not named answers 404, which
    is what an unpublished channel really does. Passing None for stable/stats makes that endpoint
    fail, which is how the failure-path tests are written.
    """
    if prereleases is None:
        prereleases = {"beta": BETA_MANIFEST}
    context = browser.new_context(**({"user_agent": user_agent} if user_agent else {}))
    page = context.new_page()

    def route_json(payload):
        def handler(route):
            if payload is None:
                route.fulfill(status=503, content_type="application/json", body='{"error":"down"}')
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        return handler

    page.route(f"{API_ORIGIN}/api/releases/stable", route_json(stable))
    for channel in ("rc", "beta", "alpha"):
        page.route(f"{API_ORIGIN}/api/releases/{channel}", route_json(prereleases.get(channel)))
    page.route(f"{API_ORIGIN}/api/stats/downloads", route_json(stats))

    page.goto(f"{website_server}/download/", wait_until="networkidle")
    return context, page


class TestStableDownloads:
    def test_every_published_platform_is_offered(self, browser, website_server):
        context, page = _open_download_page(browser, website_server)
        try:
            ids = page.eval_on_selector_all(
                ".download-grid:not(.compact) .download-card", "cards => cards.map(c => c.dataset.artifactId)"
            )
            assert sorted(ids) == ["linux-x64", "macos-arm64", "windows-x64"]
        finally:
            context.close()

    def test_buttons_point_at_the_worker_not_github_or_r2(self, browser, website_server):
        # Every installer download must go through the Worker: it is the only public path to the
        # private bucket, and the only thing that counts the download.
        context, page = _open_download_page(browser, website_server)
        try:
            hrefs = page.eval_on_selector_all(".download-button", "links => links.map(a => a.href)")
            assert hrefs, "no download buttons rendered"
            for href in hrefs:
                assert href.startswith(f"{API_ORIGIN}/download/"), href

            # The stable grid specifically serves the stable channel (the pre-release block
            # below it serves whichever pre-release channel has a build, which is why the
            # assertion above is channel-agnostic).
            stable_hrefs = page.eval_on_selector_all(
                ".download-grid:not(.compact) .download-button", "links => links.map(a => a.href)"
            )
            assert sorted(stable_hrefs) == [
                f"{API_ORIGIN}/download/stable/linux-x64",
                f"{API_ORIGIN}/download/stable/macos-arm64",
                f"{API_ORIGIN}/download/stable/windows-x64",
            ]
        finally:
            context.close()

    def test_filenames_and_checksums_come_from_the_manifest(self, browser, website_server):
        # Nothing about a release may be hardcoded in the page -- a new build must need no
        # website change.
        context, page = _open_download_page(browser, website_server)
        try:
            body = page.inner_text("body")
            assert "privacyfence_4.3.0_amd64.deb" in body
            assert "Version 4.3.0" in body
            checksums = page.eval_on_selector_all(".download-checksum code", "els => els.map(e => e.textContent)")
            assert "c" * 64 in checksums
        finally:
            context.close()

    def test_release_notes_link_targets_the_published_version(self, browser, website_server):
        context, page = _open_download_page(browser, website_server)
        try:
            href = page.get_attribute("#release-meta a", "href")
            assert href == "https://github.com/privacyfence/privacyfence/releases/tag/v4.3.0"
        finally:
            context.close()


class TestOsDetection:
    MAC_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    WINDOWS_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    @pytest.mark.parametrize(
        ("user_agent", "expected_id"),
        [(MAC_UA, "macos-arm64"), (WINDOWS_UA, "windows-x64")],
    )
    def test_highlights_the_likely_platform(self, browser, website_server, user_agent, expected_id):
        context, page = _open_download_page(browser, website_server, user_agent=user_agent)
        try:
            highlighted = page.eval_on_selector_all(
                ".download-card.recommended", "cards => cards.map(c => c.dataset.artifactId)"
            )
            assert highlighted == [expected_id]
        finally:
            context.close()

    def test_never_hides_the_platforms_it_did_not_pick(self, browser, website_server):
        # The rule: detection may highlight, never hide. Downloading an
        # installer for a different machine than the one you are browsing on is ordinary.
        context, page = _open_download_page(browser, website_server, user_agent=self.MAC_UA)
        try:
            visible = page.eval_on_selector_all(
                ".download-grid:not(.compact) .download-card",
                "cards => cards.filter(c => c.offsetParent !== null).map(c => c.dataset.artifactId)",
            )
            assert sorted(visible) == ["linux-x64", "macos-arm64", "windows-x64"]
        finally:
            context.close()


ALPHA_MANIFEST = {
    "schema": 1,
    "version": "4.0.0a14",
    "channel": "alpha",
    "published_at": "2026-09-13T16:37:00Z",
    "artifacts": [
        {
            "id": "linux-x64",
            "kind": "installer",
            "platform": "linux",
            "architecture": "x64",
            "filename": "privacyfence_4.0.0a14_amd64.deb",
            "key": "releases/alpha/4.0.0a14/privacyfence_4.0.0a14_amd64.deb",
            "size": 47028460,
            "sha256": "e" * 64,
        }
    ],
}

RC_MANIFEST = {**BETA_MANIFEST, "version": "4.4.0rc1", "channel": "rc"}


class TestPreReleaseSection:
    def test_uses_the_manifest_metadata_when_a_pre_release_exists(self, browser, website_server):
        context, page = _open_download_page(browser, website_server)
        try:
            assert page.is_visible("#prerelease-block")
            assert "4.4.0b1" in page.inner_text("#prerelease-summary")
            hrefs = page.eval_on_selector_all("#prerelease-grid .download-button", "links => links.map(a => a.href)")
            assert hrefs == [f"{API_ORIGIN}/download/beta/macos-arm64"]
        finally:
            context.close()

    def test_falls_back_to_alpha_when_no_beta_or_rc_exists(self, browser, website_server):
        # The state this project was actually in when the page first shipped: only alphas had ever
        # been released, so hardcoding beta left the section hidden with a perfectly good build
        # published one channel over.
        context, page = _open_download_page(browser, website_server, prereleases={"alpha": ALPHA_MANIFEST})
        try:
            assert page.is_visible("#prerelease-block")
            summary = page.inner_text("#prerelease-summary")
            assert "4.0.0a14" in summary
            assert "alpha" in summary, "the section must name the channel it actually found"
            hrefs = page.eval_on_selector_all("#prerelease-grid .download-button", "links => links.map(a => a.href)")
            assert hrefs == [f"{API_ORIGIN}/download/alpha/linux-x64"]
        finally:
            context.close()

    def test_prefers_the_most_production_ready_channel_of_the_same_cycle(self, browser, website_server):
        # Within one release cycle, rc beats beta beats alpha: handing a tester a release
        # candidate over an earlier-stage build of the *same* upcoming version is the safer
        # default when more than one of its pre-releases is published at once.
        context, page = _open_download_page(
            browser,
            website_server,
            prereleases={"rc": RC_MANIFEST, "beta": BETA_MANIFEST, "alpha": ALPHA_MANIFEST},
        )
        try:
            summary = page.inner_text("#prerelease-summary")
            assert "4.4.0rc1" in summary
            assert "rc" in summary
        finally:
            context.close()

    def test_a_newer_alpha_beats_a_stale_rc_from_an_already_shipped_cycle(self, browser, website_server):
        # The bug this module used to have no coverage for: nothing clears a channel's latest.json
        # once its own cycle ships stable, so an already-superseded rc can sit there indefinitely.
        # A fixed "rc always wins" priority kept surfacing that stale rc over a genuinely newer
        # alpha published for the *next* cycle -- the page must pick by version instead.
        stale_rc = {**RC_MANIFEST, "version": "4.0.0rc1"}
        fresh_alpha = {**ALPHA_MANIFEST, "version": "4.1.0a1"}
        context, page = _open_download_page(
            browser, website_server, prereleases={"rc": stale_rc, "alpha": fresh_alpha}
        )
        try:
            summary = page.inner_text("#prerelease-summary")
            assert "4.1.0a1" in summary
            assert "alpha" in summary
        finally:
            context.close()

    def test_stays_hidden_when_no_pre_release_is_published(self, browser, website_server):
        # An empty channel answers 404 in production; showing an invitation to download nothing
        # would be worse than showing no section at all.
        context, page = _open_download_page(browser, website_server, prereleases={})
        try:
            assert page.is_hidden("#prerelease-block")
        finally:
            context.close()


class TestDegradedApi:
    def test_stats_disappear_rather_than_break_the_page(self, browser, website_server):
        context, page = _open_download_page(browser, website_server, stats=None)
        try:
            assert page.is_hidden("#download-stats")
            # The downloads themselves are unaffected: stats are enhancement-only.
            assert len(page.query_selector_all(".download-button")) == 4
        finally:
            context.close()

    def test_stats_render_when_available(self, browser, website_server):
        context, page = _open_download_page(
            browser, website_server, stats={"total": 1234, "by_channel": {}, "by_platform": []}
        )
        try:
            assert "1,234" in page.inner_text("#download-stats")
        finally:
            context.close()

    def test_release_metadata_failure_falls_back_to_github(self, browser, website_server):
        # The page must never be a dead end -- GitHub Releases remains the documented secondary
        # source, so an outage degrades to it rather than to nothing.
        context, page = _open_download_page(browser, website_server, stable=None)
        try:
            assert page.is_visible("#download-fallback")
            href = page.get_attribute("#download-fallback a", "href")
            assert href == "https://github.com/privacyfence/privacyfence/releases/latest"
            assert page.query_selector("#download-loading") is None
        finally:
            context.close()


class TestUnknownArtifactId:
    """download.js hardcodes nothing about a release: an artifact id its own PLATFORMS map has no
    display name for still gets a card (falling back to the id itself) rather than being silently
    dropped while it waits for a website deploy. No real artifact exercises this -- the `.pkg` is
    not a download of its own, since the DMG carries it (see scripts/build_dmg.sh) -- so it is
    asserted directly here."""

    MAC_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    def test_renders_alongside_the_known_platforms(self, browser, website_server):
        context, page = _open_download_page(
            browser, website_server, stable=STABLE_MANIFEST_WITH_UNKNOWN_ARTIFACT,
        )
        try:
            ids = page.eval_on_selector_all(
                ".download-grid:not(.compact) .download-card", "cards => cards.map(c => c.dataset.artifactId)"
            )
            assert sorted(ids) == ["linux-arm64", "linux-x64", "macos-arm64", "windows-x64"]

            href = page.get_attribute('.download-card[data-artifact-id="linux-arm64"] .download-button', "href")
            assert href == f"{API_ORIGIN}/download/stable/linux-arm64"

            body = page.inner_text("body")
            assert "privacyfence_4.3.0_arm64.deb" in body
        finally:
            context.close()

    def test_it_never_becomes_the_recommendation(self, browser, website_server):
        # detectPlatformId() only ever returns an id PLATFORMS knows, so an unmapped artifact can
        # render without ever being the one a visitor is steered to.
        context, page = _open_download_page(
            browser, website_server, stable=STABLE_MANIFEST_WITH_UNKNOWN_ARTIFACT, user_agent=self.MAC_UA,
        )
        try:
            highlighted = page.eval_on_selector_all(
                ".download-card.recommended", "cards => cards.map(c => c.dataset.artifactId)"
            )
            assert highlighted == ["macos-arm64"]
        finally:
            context.close()


# What the build pre-renders when it can read the Worker: an older release than the one the
# stubbed API answers with, so a test can tell which of the two the page is showing.
PRERENDERED_MANIFEST = {
    **STABLE_MANIFEST,
    "version": "4.2.0",
    "artifacts": [
        {**artifact, "filename": artifact["filename"].replace("4.3.0", "4.2.0")} for artifact in STABLE_MANIFEST["artifacts"]
    ],
}


@pytest.fixture(scope="module")
def prerendered_server(tmp_path_factory):
    out = tmp_path_factory.mktemp("prerendered") / "_site"
    build_site.build(out, docs_ref=None, manifest=PRERENDERED_MANIFEST)
    with serve(out) as url:
        yield url


class TestPrerenderedPage:
    """scripts/build_site.py writes the stable release it saw at deploy time into the page, so
    /download/ offers working downloads and names a version without JavaScript (and to crawlers).
    download.js then replaces that with the live manifest."""

    def test_works_without_javascript(self, browser, prerendered_server):
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(prerendered_server) else route.abort())
        try:
            page.goto(f"{prerendered_server}/download/")
            ids = page.eval_on_selector_all(".download-grid .download-card", "cards => cards.map(c => c.dataset.artifactId)")
            assert ids == ["macos-arm64", "windows-x64", "linux-x64"]
            assert "Version 4.2.0" in page.inner_text("#release-meta")
            href = page.get_attribute('.download-card[data-artifact-id="linux-x64"] .download-button', "href")
            assert href == f"{API_ORIGIN}/download/stable/linux-x64"
            assert "privacyfence_4.2.0_amd64.deb" in page.inner_text("#download-grid")
            assert page.query_selector("#download-loading") is None
        finally:
            context.close()

    def test_the_live_manifest_replaces_the_prerendered_cards(self, browser, prerendered_server):
        context, page = _open_download_page(browser, prerendered_server)
        try:
            filenames = page.eval_on_selector_all(
                ".download-grid:not(.compact) .download-meta", "nodes => nodes.map(n => n.textContent)"
            )
            assert len(filenames) == 3, filenames
            assert all("4.3.0" in name for name in filenames), filenames
            assert page.inner_text("#release-meta").count("Version") == 1
            assert "Version 4.3.0" in page.inner_text("#release-meta")
        finally:
            context.close()

    def test_an_api_outage_keeps_the_prerendered_cards(self, browser, prerendered_server):
        context, page = _open_download_page(browser, prerendered_server, stable=None)
        try:
            assert page.locator(".download-grid:not(.compact) .download-card").count() == 3
            assert not page.is_visible("#download-fallback")
        finally:
            context.close()
