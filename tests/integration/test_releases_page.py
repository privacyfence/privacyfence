"""Browser tests for /releases/, the release history page (website/releases/).

The page lists every published version from the download Worker's `GET /api/releases/history`,
and falls back to `GET /api/releases` (the newest release per channel) when a Worker without that
route, or a failing one, answers. These run the built page in headless Chromium with both
responses stubbed at the network layer, as tests/integration/test_download_page.py does for
/download/ -- never the live Worker, whose counters a test must not touch. They check what only a
browser shows: releases.js renders the live list newest first, installers only, every download
through the Worker at its exact version; it keeps the rows the build pre-rendered when the API
fails, and shows the GitHub fallback only when there is nothing else.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.website_site import API_ORIGIN, build_site, built_site, chromium_launch_kwargs, serve

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]


def _installer(artifact_id, platform, architecture, filename, sha):
    return {
        "id": artifact_id,
        "kind": "installer",
        "platform": platform,
        "architecture": architecture,
        "filename": filename,
        "key": f"releases/x/{filename}",
        "size": 47028460,
        "sha256": sha * 64,
    }


RELEASES = {
    "channels": {
        "stable": {
            "schema": 1,
            "version": "4.6.1",
            "channel": "stable",
            "published_at": "2026-09-25T10:00:00Z",
            "artifacts": [
                _installer("linux-x64", "linux", "x64", "privacyfence_4.6.1_amd64.deb", "c"),
                _installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.6.1.dmg", "a"),
                _installer("windows-x64", "windows", "x64", "PrivacyFence-4.6.1-setup.exe", "b"),
            ],
        },
        "alpha": {
            "schema": 1,
            "version": "4.7.0a2",
            "channel": "alpha",
            "published_at": "2026-09-26T08:00:00Z",
            "artifacts": [_installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.7.0a2.dmg", "d")],
        },
        "beta": None,
        "rc": {
            "schema": 1,
            "version": "4.0.0rc3",
            "channel": "rc",
            "published_at": "2026-08-30T08:00:00Z",
            "artifacts": [
                _installer("linux-x64", "linux", "x64", "privacyfence_4.0.0rc3_amd64.deb", "e"),
                # Not an installer: must never be offered, whatever a manifest carries.
                {"id": "sbom-cyclonedx", "kind": "sbom", "filename": "privacyfence-4.0.0rc3.cdx.json"},
            ],
        },
    }
}


def _manifest(version, channel, published, *artifacts):
    return {
        "schema": 1,
        "version": version,
        "channel": channel,
        "published_at": published,
        "artifacts": list(artifacts),
    }


# What /api/releases/history returns: every published version, newest first, installers only and
# without R2 keys. The non-installer entry proves the page filters too, whatever the route sends.
HISTORY = {
    "releases": [
        _manifest(
            "4.7.0a2",
            "alpha",
            "2026-09-26T08:00:00Z",
            _installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.7.0a2.dmg", "d"),
        ),
        RELEASES["channels"]["stable"],
        _manifest(
            "4.6.1rc1",
            "rc",
            "2026-09-22T08:00:00Z",
            _installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.6.1rc1.dmg", "f"),
        ),
        _manifest(
            "4.6.0",
            "stable",
            "2026-09-20T08:00:00Z",
            _installer("linux-x64", "linux", "x64", "privacyfence_4.6.0_amd64.deb", "1"),
        ),
        _manifest(
            "4.6.0b1",
            "beta",
            "2026-09-15T08:00:00Z",
            _installer("windows-x64", "windows", "x64", "PrivacyFence-4.6.0b1-setup.exe", "2"),
            {"id": "sbom-cyclonedx", "kind": "sbom", "filename": "privacyfence-4.6.0b1.cdx.json"},
        ),
        _manifest(
            "4.5.0",
            "stable",
            "2026-09-10T08:00:00Z",
            _installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.5.0.dmg", "3"),
        ),
        RELEASES["channels"]["rc"],
    ]
}
HISTORY_VERSIONS = ["4.7.0a2", "4.6.1", "4.6.1rc1", "4.6.0", "4.6.0b1", "4.5.0", "4.0.0rc3"]
LATEST_VERSIONS = ["4.7.0a2", "4.6.1", "4.0.0rc3"]


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


@pytest.fixture(scope="module")
def offline_site():
    """The shared offline build: the table holds only its loading row until releases.js runs."""
    with serve(built_site()) as url:
        yield url


@pytest.fixture(scope="module")
def prerendered_site():
    """A build that pre-rendered the table from HISTORY, as a deploy with the Worker up does."""
    tmp = Path(tempfile.mkdtemp(prefix="pf-releases-"))
    try:
        build_site.build(tmp / "_site", docs_ref=None, fetch_manifest=False, releases=HISTORY)
        with serve(tmp / "_site") as url:
            yield url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _open(browser, site_url, releases, history=None, history_status=404):
    """Opens /releases/ with the Worker stubbed. `history` answers /api/releases/history; without
    it that route returns `history_status` (404: a Worker deployed before the route existed).
    `releases=None` makes /api/releases fail."""
    context = browser.new_context()
    page = context.new_page()

    def handle(route):
        url = route.request.url
        if url.startswith(site_url):
            route.continue_()
        elif url == f"{API_ORIGIN}/api/releases/history":
            if history is not None:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(history))
            else:
                route.fulfill(status=history_status, content_type="application/json", body='{"error":"no"}')
        elif url == f"{API_ORIGIN}/api/releases" and releases is not None:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(releases))
        elif url.startswith(API_ORIGIN):
            route.fulfill(status=503, content_type="application/json", body='{"error":"down"}')
        else:
            route.abort()

    page.route("**/*", handle)
    page.goto(f"{site_url}/releases/", wait_until="networkidle")
    return context, page


def _versions(page):
    return page.eval_on_selector_all(
        "#releases-body tr th[scope=row]", "ths => ths.map(t => t.firstChild.textContent.trim())"
    )


def test_the_history_lists_every_version_newest_first(browser, offline_site):
    context, page = _open(browser, offline_site, RELEASES, history=HISTORY)
    try:
        assert _versions(page) == HISTORY_VERSIONS
        assert page.is_hidden("#releases-fallback")
        # "Current" marks the newest stable only; older stables are plain rows.
        assert page.eval_on_selector_all(
            "#releases-body tr:has(.download-badge)", "rows => rows.map(r => r.dataset.version)"
        ) == ["4.6.1"]
        # Every pre-release older than the current stable is superseded; the newer alpha is not.
        superseded = page.eval_on_selector_all(
            "#releases-body tr:has(.release-superseded)", "rows => rows.map(r => r.dataset.version)"
        )
        assert superseded == ["4.6.1rc1", "4.6.0b1", "4.0.0rc3"]
        assert "Superseded by 4.6.1" in page.inner_text("#releases-body tr[data-version='4.6.0b1']")
        assert "2026-09-10" in page.inner_text("#releases-body tr[data-version='4.5.0']")
    finally:
        context.close()


def test_history_downloads_go_through_the_worker_at_the_exact_version(browser, offline_site):
    context, page = _open(browser, offline_site, RELEASES, history=HISTORY)
    try:
        hrefs = page.eval_on_selector_all(".release-download", "links => links.map(a => a.href)")
        assert len(hrefs) == 9
        # A crawler following a link would be counted as a download.
        rels = page.eval_on_selector_all(".release-download", "links => links.map(a => a.rel)")
        assert rels == ["nofollow"] * 9
        assert all(href.startswith(f"{API_ORIGIN}/download/version/") for href in hrefs)
        assert f"{API_ORIGIN}/download/version/4.5.0/macos-arm64" in hrefs
        assert f"{API_ORIGIN}/download/version/4.6.0b1/windows-x64" in hrefs
        notes = page.eval_on_selector_all("#releases-body td:last-child a", "links => links.map(a => a.href)")
        assert "https://github.com/privacyfence/privacyfence/releases/tag/v4.6.0" in notes
        assert "cdx.json" not in page.content() and "sbom" not in page.inner_text("#releases-body").lower()
    finally:
        context.close()


@pytest.mark.parametrize("history_status", [404, 503])
def test_a_worker_without_the_history_falls_back_to_the_newest_per_channel(browser, offline_site, history_status):
    context, page = _open(browser, offline_site, RELEASES, history_status=history_status)
    try:
        assert _versions(page) == LATEST_VERSIONS
        assert page.is_hidden("#releases-fallback")
        body = page.inner_text("#releases-body")
        assert "Alpha" in body and "Stable" in body and "Release candidate" in body
        assert "Superseded by 4.6.1" in body  # the leftover rc is older than stable
        assert "Current" in page.inner_text("#releases-body tr[data-channel=stable] th")
    finally:
        context.close()


def test_an_empty_history_falls_back_to_the_newest_per_channel(browser, offline_site):
    context, page = _open(browser, offline_site, RELEASES, history={"releases": []})
    try:
        assert _versions(page) == LATEST_VERSIONS
    finally:
        context.close()


def test_fallback_downloads_go_through_the_worker_at_the_exact_version(browser, offline_site):
    context, page = _open(browser, offline_site, RELEASES)
    try:
        hrefs = page.eval_on_selector_all(".release-download", "links => links.map(a => a.href)")
        assert sorted(hrefs) == [
            f"{API_ORIGIN}/download/version/4.0.0rc3/linux-x64",
            f"{API_ORIGIN}/download/version/4.6.1/linux-x64",
            f"{API_ORIGIN}/download/version/4.6.1/macos-arm64",
            f"{API_ORIGIN}/download/version/4.6.1/windows-x64",
            f"{API_ORIGIN}/download/version/4.7.0a2/macos-arm64",
        ]
        rels = page.eval_on_selector_all(".release-download", "links => links.map(a => a.rel)")
        assert rels == ["nofollow"] * 5
        notes = page.eval_on_selector_all("#releases-body td:last-child a", "links => links.map(a => a.href)")
        assert "https://github.com/privacyfence/privacyfence/releases/tag/v4.7.0a2" in notes
        assert "cdx.json" not in page.content() and "sbom" not in page.inner_text("#releases-body").lower()
        assert "e" * 64 in page.eval_on_selector_all(".download-checksum code", "els => els.map(e => e.textContent)")
    finally:
        context.close()


def test_an_api_outage_with_nothing_prerendered_shows_the_github_fallback(browser, offline_site):
    context, page = _open(browser, offline_site, None, history_status=503)
    try:
        assert page.is_visible("#releases-fallback")
        assert page.is_hidden("#releases-table")
        assert (
            page.get_attribute("#releases-fallback a", "href")
            == "https://github.com/privacyfence/privacyfence/releases"
        )
    finally:
        context.close()


def test_an_api_with_nothing_published_shows_the_fallback(browser, offline_site):
    context, page = _open(
        browser,
        offline_site,
        {"channels": {"stable": None, "alpha": None, "beta": None, "rc": None}},
        history={"releases": []},
    )
    try:
        assert page.is_visible("#releases-fallback")
    finally:
        context.close()


def test_an_api_outage_keeps_the_prerendered_rows(browser, prerendered_site):
    context, page = _open(browser, prerendered_site, None, history_status=503)
    try:
        assert _versions(page) == HISTORY_VERSIONS
        assert page.is_hidden("#releases-fallback")
    finally:
        context.close()


def test_a_history_outage_keeps_the_prerendered_history_when_nothing_is_newer(browser, prerendered_site):
    context, page = _open(browser, prerendered_site, RELEASES, history_status=503)
    try:
        assert _versions(page) == HISTORY_VERSIONS
    finally:
        context.close()


def test_a_history_outage_shows_a_release_published_since_the_build(browser, prerendered_site):
    newer = json.loads(json.dumps(RELEASES))
    newer["channels"]["stable"]["version"] = "4.6.2"
    context, page = _open(browser, prerendered_site, newer, history_status=503)
    try:
        assert _versions(page) == ["4.7.0a2", "4.6.2", "4.0.0rc3"]
    finally:
        context.close()


def test_the_live_history_replaces_the_prerendered_rows(browser, prerendered_site):
    newer = json.loads(json.dumps(HISTORY))
    newer["releases"].insert(
        0,
        _manifest(
            "4.7.0a3",
            "alpha",
            "2026-09-27T08:00:00Z",
            _installer("macos-arm64", "macos", "arm64", "PrivacyFence-4.7.0a3.dmg", "4"),
        ),
    )
    context, page = _open(browser, prerendered_site, RELEASES, history=newer)
    try:
        assert _versions(page) == ["4.7.0a3", *HISTORY_VERSIONS]
        assert page.locator("#releases-body tr").count() == len(HISTORY_VERSIONS) + 1
    finally:
        context.close()


def test_the_page_works_without_javascript(browser, prerendered_site):
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.route(
            "**/*", lambda route: route.continue_() if route.request.url.startswith(prerendered_site) else route.abort()
        )
        page.goto(f"{prerendered_site}/releases/")
        assert _versions(page) == HISTORY_VERSIONS
    finally:
        context.close()
