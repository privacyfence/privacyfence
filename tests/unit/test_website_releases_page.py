"""Static checks on /releases/, the release history page (#365), as scripts/build_site.py builds it.

The page's rows exist only once the build (render_release_rows, tests/unit/test_build_site.py) or
releases.js (tests/integration/test_releases_page.py) has read the Worker's
/api/releases/history, or /api/releases when that route is unavailable. What the page's own HTML
and script must carry is checked here: it is deployed and in the sitemap, /download/ and the
footer link it while the header nav does not, it reads the history with the per-channel list as
its fallback, it offers no download that bypasses the download Worker, and it presents installers
only.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import WEBSITE, build_site, built_site, read_page

pytestmark = pytest.mark.unit

SCRIPT = (WEBSITE / "releases" / "releases.js").read_text(encoding="utf-8")


def test_the_page_and_its_script_are_deployed():
    assert build_site.PAGES["/releases/"] == "releases/index.html"
    assert build_site.STATIC["releases/releases.js"] == "website/releases/releases.js"
    assert re.search(r'<script src="/releases/releases\.js\?v=[0-9a-f]{12}" defer></script>', read_page("/releases/"))


def test_the_page_is_in_the_sitemap_and_llms_txt():
    assert "<loc>https://privacyfence.eu/releases/</loc>" in (built_site() / "sitemap.xml").read_text(encoding="utf-8")
    assert "(https://privacyfence.eu/releases/)" in (built_site() / "llms.txt").read_text(encoding="utf-8")


def test_download_page_links_the_history_from_its_header_and_the_pre_release_section():
    page = read_page("/download/")
    assert '<a class="nav-cta" href="/releases/">All releases</a>' in page
    prerelease = page.split('id="prerelease-block"', 1)[1].split("</section>", 1)[0]
    assert 'href="/releases/"' in prerelease


def test_the_header_nav_does_not_list_the_page():
    for path in build_site.PAGES:
        header = read_page(path).split('<header class="site-header">', 1)[1].split("</header>", 1)[0]
        nav = header.replace('<a class="nav-cta" href="/releases/">All releases</a>', "")
        assert 'href="/releases/"' not in nav, path


def test_the_script_downloads_only_through_the_worker():
    # Every download link is built from API + /download/version/, pinned to the row's version;
    # GitHub is linked only for release notes, and R2 is never named.
    assert "const API = 'https://downloads.privacyfence.eu';" in SCRIPT
    assert "`${API}/download/version/" in SCRIPT
    assert "r2.dev" not in SCRIPT and "r2.cloudflarestorage" not in SCRIPT
    assert re.findall(r"`\$\{REPO\}([^`$]*)", SCRIPT) == ["/releases/tag/v"]


def test_the_script_reads_the_history_and_falls_back_to_the_newest_per_channel():
    # The Worker and the site deploy independently: the history route may not exist yet.
    assert re.findall(r"load\('([^']+)'\)", SCRIPT) == ["/api/releases/history", "/api/releases"]


def test_the_script_presents_installers_only():
    assert "artifact.kind === 'installer'" in SCRIPT


def test_the_page_hardcodes_no_release():
    source = (WEBSITE / "releases" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\.\d+\.\d+", source), "versions come from /api/releases, not the page"
    assert not re.search(r"\.(dmg|exe|deb|pkg|whl)\b", source), "filenames come from /api/releases, not the page"
