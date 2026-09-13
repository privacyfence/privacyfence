"""Guards the Phase 6 cutover: the homepage's primary CTAs point at the download page, and that
page exists (docs/release-publishing-kpi-plan.md Phase 6).

Deliberately a static test rather than a browser one. tests/integration/test_download_page.py
covers what the page *does*, but it needs Chromium and skips without it -- and the failure this
module exists to catch is the one where the CTAs survive a revert of the page (or vice versa),
leaving every "Download" button on the homepage pointing at nothing. That is worth an assertion
that runs on every machine, in every suite, with no browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEBSITE = Path(__file__).resolve().parents[2] / "website"
INDEX = WEBSITE / "index.html"

# The three CTAs Phase 6 repoints: header nav, hero, and the closing call to action.
CTA_PATTERN = re.compile(r'<a class="(?:nav-cta|button primary)" href="([^"]+)">([^<]*Download[^<]*)</a>')


def _ctas() -> list[tuple[str, str]]:
    return CTA_PATTERN.findall(INDEX.read_text(encoding="utf-8"))


def test_the_download_page_the_ctas_point_at_exists():
    assert (WEBSITE / "download" / "index.html").is_file()
    assert (WEBSITE / "download" / "download.js").is_file()


def test_all_three_primary_ctas_point_at_the_download_page():
    ctas = _ctas()
    assert len(ctas) == 3, f"expected 3 primary download CTAs, found {len(ctas)}: {ctas}"
    for href, label in ctas:
        assert href == "download/", f"{label.strip()!r} points at {href!r}, not the download page"


def test_no_primary_cta_still_points_at_github_releases():
    # The specific regression Phase 6 is: a CTA left on GitHub Releases after the download page
    # became the primary path. GitHub stays linked for source and docs -- just not as the
    # download button.
    for href, label in _ctas():
        assert "github.com" not in href, f"{label.strip()!r} still points at GitHub: {href}"


def test_github_remains_linked_for_source_and_docs():
    # The cutover repoints the download buttons; it must not quietly delist the project's own
    # source, which the plan keeps as a documented secondary source.
    assert "https://github.com/privacyfence/privacyfence" in INDEX.read_text(encoding="utf-8")


PAGES_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pages.yml"


def test_every_website_file_is_actually_deployed():
    """Every file under website/ is copied by pages.yml's build step.

    This is the test that was missing when the download page shipped: the page existed in the
    repo, the homepage CTAs pointed at it, every other test passed -- and `/download/` was a 404
    in production, because pages.yml copies a hand-written list of files rather than the
    directory. Asserting the files exist locally proves nothing about what the public site
    serves.

    Deliberately checks the whole tree rather than the two download files, so the next file
    added under website/ is caught the same way instead of repeating this exact outage.
    """
    workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")
    missing = [
        path.relative_to(WEBSITE).as_posix()
        for path in sorted(WEBSITE.rglob("*"))
        if path.is_file() and f"website/{path.relative_to(WEBSITE).as_posix()}" not in workflow
    ]
    assert not missing, (
        f"these website files are never copied into _site by pages.yml, so they 404 in production: {missing}"
    )
