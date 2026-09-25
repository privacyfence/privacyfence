"""Guards the Phase 6 cutover: the homepage's primary CTAs point at the download page, and that
page exists (docs/downloads-and-release-kpi.md).

Deliberately a static test rather than a browser one. tests/integration/test_download_page.py
covers what the page *does*, but it needs Chromium and skips without it -- and the failure this
module exists to catch is the one where the CTAs survive a revert of the page (or vice versa),
leaving every "Download" button on the homepage pointing at nothing. That is worth an assertion
that runs on every machine, in every suite, with no browser.

It also holds guardrail 7: every file under website/ is either published by the build manifest in
scripts/build_site.py or explicitly kept back from the public site.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import REPO, WEBSITE, build_site, built_site, read_page

pytestmark = pytest.mark.unit

# The three CTAs Phase 6 repoints: header nav, hero, and the closing call to action.
CTA_PATTERN = re.compile(r'<a class="(?:nav-cta|button primary)" href="([^"]+)">([^<]*Download[^<]*)</a>')


def _ctas() -> list[tuple[str, str]]:
    # The built page: the header CTA comes from the shared header partial.
    return CTA_PATTERN.findall(read_page("/"))


def test_the_download_page_the_ctas_point_at_exists():
    assert (WEBSITE / "download" / "index.html").is_file()
    assert (WEBSITE / "download" / "download.js").is_file()


def test_all_three_primary_ctas_point_at_the_download_page():
    ctas = _ctas()
    assert len(ctas) == 3, f"expected 3 primary download CTAs, found {len(ctas)}: {ctas}"
    for href, label in ctas:
        assert href == "/download/", f"{label.strip()!r} points at {href!r}, not the download page"


def test_no_primary_cta_still_points_at_github_releases():
    # The specific regression Phase 6 is: a CTA left on GitHub Releases after the download page
    # became the primary path. GitHub stays linked for source and docs -- just not as the
    # download button.
    for href, label in _ctas():
        assert "github.com" not in href, f"{label.strip()!r} still points at GitHub: {href}"


def test_github_remains_linked_for_source_and_docs():
    # The cutover repoints the download buttons; it must not quietly delist the project's own
    # source, which the plan keeps as a documented secondary source.
    assert "https://github.com/privacyfence/privacyfence" in read_page("/")


def _website_files() -> set[str]:
    return {path.relative_to(WEBSITE).as_posix() for path in WEBSITE.rglob("*") if path.is_file()}


def test_every_website_file_is_actually_deployed():
    """Guardrail 7: every file under website/ is published by scripts/build_site.py's manifest,
    or is explicitly one of the build's inputs or repository-only.

    This is the test that was missing when the download page shipped: the page existed in the
    repo, the homepage CTAs pointed at it, every other test passed -- and `/download/` was a 404
    in production, because the deploy step copied a hand-written list of files rather than the
    directory. The build still publishes a named list rather than the directory, so nothing
    lands on the public site by accident; this is what keeps that list complete.

    Deliberately checks the whole tree rather than the two download files, so the next file
    added under website/ is caught the same way instead of repeating this exact outage.
    """
    accounted = (
        set(build_site.PAGES.values())
        | {source.removeprefix("website/") for source in build_site.STATIC.values() if source.startswith("website/")}
        | set(build_site.BUILD_INPUTS)
        | set(build_site.REPOSITORY_ONLY)
    )
    missing = sorted(_website_files() - accounted)
    assert not missing, (
        f"these website files are in no list of scripts/build_site.py (PAGES, STATIC, BUILD_INPUTS or "
        f"REPOSITORY_ONLY), so they 404 in production: {missing}"
    )


def test_the_manifest_names_only_files_that_exist():
    for source in build_site.PAGES.values():
        assert (WEBSITE / source).is_file(), f"PAGES names website/{source}, which does not exist"
    for dest, source in build_site.STATIC.items():
        assert (REPO / source).is_file(), f"STATIC copies {source} to {dest}, but {source} does not exist"
    for name in build_site.BUILD_INPUTS:
        assert (WEBSITE / name).is_file(), f"BUILD_INPUTS names website/{name}, which does not exist"


def test_every_manifest_entry_is_in_the_built_site():
    site = built_site()
    for source in build_site.PAGES.values():
        assert (site / source).is_file(), f"{source} is in PAGES but not in the built site"
    for dest in build_site.STATIC:
        assert (site / dest).is_file(), f"{dest} is in STATIC but not in the built site"


def test_repository_only_files_and_build_inputs_are_not_deployed():
    published = set(build_site.PAGES.values()) | {s.removeprefix("website/") for s in build_site.STATIC.values()}
    built_names = {path.name for path in built_site().rglob("*") if path.is_file()}
    for name in build_site.REPOSITORY_ONLY | build_site.BUILD_INPUTS:
        assert (WEBSITE / name).is_file(), f"{name} is listed as not deployed but does not exist"
        assert name not in published, f"{name} is not to be deployed, but the build manifest publishes it"
    for name in build_site.REPOSITORY_ONLY:
        assert name.rsplit("/", 1)[-1] not in built_names, f"{name} is repository-only but is in the built site"
    assert not any(built_site().glob("_*")), "a build-only directory (website/_partials, website/_docs) was published"
