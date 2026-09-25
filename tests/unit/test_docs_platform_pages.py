"""Every supported platform has exactly one install guide, and every way in reaches it.

The user docs describe installing PrivacyFence in three places that can drift apart: the support
matrix in ``docs/platform-support.md`` (what is supported), one ``docs/install-<platform>.md`` page
per platform (how to install it), and ``docs/getting-started.md`` (where a new reader picks a
platform). The download page on the website is a fourth: it is what a reader actually downloads
from. A platform added to the matrix without a guide, a guide getting-started.md forgets to link,
or an installer the download page stops offering each leaves a reader at a dead end, and none of
them fails anything else.

Deliberately a static test, like ``test_website_download_cta.py``: it reads the Markdown and the
download page's source, needs no browser and no network, and so runs on every machine in every
suite. The matrix is parsed the same way ``test_minimum_os_versions.py`` parses it, so the two
tests agree on what the table is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
DOWNLOAD_PAGE = REPO_ROOT / "website" / "download" / "index.html"
DOWNLOAD_SCRIPT = REPO_ROOT / "website" / "download" / "download.js"

# Which install guide a matrix row belongs to, keyed by a word its first column must contain.
PLATFORM_KEYWORDS = {"macos": ("macOS",), "windows": ("Windows",), "linux": ("Linux", "Debian", "Ubuntu")}

# The artifact id download.js renders a card for, per installer platform.
DOWNLOAD_ARTIFACT_IDS = {"macos": "macos-arm64", "windows": "windows-x64", "linux": "linux-x64"}

_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)")


def _matrix_rows() -> list[dict[str, str]]:
    text = (DOCS / "platform-support.md").read_text(encoding="utf-8")
    lines = text.split("## Support matrix", 1)[1].split("\n\n", 2)[1].splitlines()
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [
        dict(zip(header, [cell.strip() for cell in line.strip("|").split("|")], strict=True))
        for line in lines[2:]
    ]


def _platform_of(label: str) -> str:
    matches = [key for key, words in PLATFORM_KEYWORDS.items() if any(w in label for w in words)]
    assert len(matches) == 1, f"matrix row {label!r} names {len(matches)} platforms, not one: {matches}"
    return matches[0]


def _is_org_mode(row: dict[str, str]) -> bool:
    return "org mode" in row["Platform"]


def test_the_matrix_has_a_guide_column_and_rows():
    rows = _matrix_rows()
    assert rows, "no rows in platform-support.md's support matrix"
    assert all("Guide" in row for row in rows), "the support matrix has no Guide column"


@pytest.mark.parametrize("row", _matrix_rows(), ids=lambda row: row["Platform"])
def test_each_matrix_row_links_its_platforms_one_install_page(row):
    links = _LINK.findall(row["Guide"])
    if _is_org_mode(row):
        # Organization mode is a server deployment, not a desktop install: its guide is the one
        # deployment guide, never an install page.
        assert links == ["org-mode-setup-guide.md"], row["Guide"]
        return
    platform = _platform_of(row["Platform"])
    install_pages = [link for link in links if link.startswith("install-")]
    assert install_pages == [f"install-{platform}.md"], (
        f"{row['Platform']!r} should link exactly install-{platform}.md, links {install_pages}"
    )
    assert (DOCS / install_pages[0]).is_file()


def test_every_installer_platform_is_in_the_matrix():
    platforms = {_platform_of(row["Platform"]) for row in _matrix_rows() if not _is_org_mode(row)}
    assert platforms == set(PLATFORM_KEYWORDS)


def test_every_install_page_is_a_matrix_platform():
    pages = {path.name for path in DOCS.glob("install-*.md")}
    assert pages == {f"install-{platform}.md" for platform in PLATFORM_KEYWORDS}


def test_getting_started_links_every_install_page():
    links = set(_LINK.findall((DOCS / "getting-started.md").read_text(encoding="utf-8")))
    missing = [f"install-{p}.md" for p in PLATFORM_KEYWORDS if f"install-{p}.md" not in links]
    assert not missing, f"getting-started.md does not link {missing}"


def test_the_download_page_offers_every_installer_platform():
    # The cards are rendered by download.js from its PLATFORMS table (the page itself holds only
    # the grid they go into), so both halves have to be there.
    page = DOWNLOAD_PAGE.read_text(encoding="utf-8")
    assert 'id="download-grid"' in page
    assert '<script src="download.js"' in page
    script = DOWNLOAD_SCRIPT.read_text(encoding="utf-8")
    table = script.split("const PLATFORMS = {", 1)[1].split("};", 1)[0]
    offered = set(re.findall(r"^\s*'([a-z0-9-]+)':\s*\{", table, flags=re.MULTILINE))
    missing = [aid for aid in DOWNLOAD_ARTIFACT_IDS.values() if aid not in offered]
    assert not missing, f"download.js renders no card for {missing}; it offers {sorted(offered)}"
