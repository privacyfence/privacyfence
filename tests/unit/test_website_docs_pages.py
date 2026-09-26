"""Static checks on the rendered /docs/ pages of privacyfence.eu.

The docs are rendered by the docs generator inside the site's own theme (website/_docs/). These
checks read the built HTML and hold every docs page to what the hand-written pages already carry
(tests/unit/test_website_pages.py): the shared header and footer, the consent code and no Google
tag, a canonical link on the apex, a content group, a description. On top of that, docs pages
carry TechArticle and BreadcrumbList JSON-LD and the version note, and nothing loads from a third
party (no web fonts, no repository API calls). Skipped when /docs/ was not built, i.e. when the
`docs` extra is not installed; .github/workflows/website-build.yml always builds it.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.website_site import build_site, built_site, docs_built, read_page

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not docs_built(), reason="/docs/ not built: pip install --require-hashes -r requirements/docs.lock.txt"
    ),
]

APEX = "https://privacyfence.eu"


def _docs_paths() -> list[str]:
    if not docs_built():
        return []
    paths = []
    for index in sorted((built_site() / "docs").rglob("index.html")):
        rel = index.parent.relative_to(built_site()).as_posix()
        paths.append(f"/{rel}/")
    return paths


DOCS_PAGES = _docs_paths()


def _meta(page: str, name: str) -> str | None:
    match = re.search(rf'<meta name="{re.escape(name)}" content="([^"]*)"', page)
    return match.group(1) if match else None


def test_every_published_doc_is_rendered():
    readme = (build_site.REPO / "docs" / "README.md").read_text(encoding="utf-8")
    stems = {stem for section in build_site.published_nav(readme) for stem in section.docs}
    assert set(DOCS_PAGES) == {"/docs/"} | {f"/docs/{stem}/" for stem in stems}


@pytest.mark.parametrize("path", DOCS_PAGES)
def test_site_chrome_consent_and_canonical(path):
    page = read_page(path)
    assert f'<link rel="canonical" href="{APEX}{path}">' in page
    assert '<header class="site-header">' in page and '<details class="nav-menu">' in page
    footer = page.split('<footer class="site-footer', 1)[1]
    assert '<a href="/privacy/" data-privacy-link>Privacy</a>' in footer
    assert '<a href="/privacy/#your-choice" data-cookie-settings>Cookie settings</a>' in footer
    assert re.search(r'<script src="/site\.js\?v=[0-9a-f]{12}" defer></script>', page)
    for stylesheet in ("/tokens.css", "/base.css", "/chrome.css", "/docs-theme.css"):
        assert re.search(rf'<link rel="stylesheet" href="{re.escape(stylesheet)}\?v=[0-9a-f]{{12}}">', page)
    for third_party in (
        "googletagmanager",
        "gtag(",
        "G-7Z3PFP4XPT",
        "fonts.googleapis",
        "fonts.gstatic",
        "api.github.com",
    ):
        assert third_party not in page, f"{path} carries {third_party}"


@pytest.mark.parametrize("path", DOCS_PAGES)
def test_description_and_content_group(path):
    page = read_page(path)
    description = _meta(page, "description")
    assert description and 30 <= len(description) <= 250, description
    stem = path.removeprefix("/docs/").strip("/")
    assert _meta(page, "pf-content-group") == (build_site.content_group(stem) if stem else "docs")


def test_content_groups_follow_the_file():
    assert build_site.content_group("install-macos") == "platform"
    assert build_site.content_group("slack-setup") == "connector"
    assert build_site.content_group("tools-reference") == "docs"
    assert _meta(read_page("/docs/install-linux/"), "pf-content-group") == "platform"
    assert _meta(read_page("/docs/google-cloud-setup/"), "pf-content-group") == "connector"


@pytest.mark.parametrize("path", DOCS_PAGES)
def test_json_ld(path):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', read_page(path), flags=re.S)
    assert len(blocks) == 1
    graph = json.loads(blocks[0])["@graph"]
    article = next(node for node in graph if node["@type"] == "TechArticle")
    assert article["url"] == f"{APEX}{path}"
    assert article["headline"] and article["description"]
    assert article["publisher"]["name"] == "Andras Takacs"
    crumbs = next(node for node in graph if node["@type"] == "BreadcrumbList")["itemListElement"]
    assert [c["item"] for c in crumbs][:2] == [f"{APEX}/", f"{APEX}/docs/"]
    assert crumbs[-1]["item"] == f"{APEX}{path}"


@pytest.mark.parametrize("path", DOCS_PAGES)
def test_version_note(path):
    assert '<p class="pf-version-note">These pages describe PrivacyFence' in read_page(path)


def test_llms_full_carries_every_doc_with_absolute_links():
    full = (built_site() / "llms-full.txt").read_text(encoding="utf-8")
    for path in (p for p in DOCS_PAGES if p != "/docs/"):
        assert f"Source: {APEX}{path}\n" in full
    assert "](getting-started.md" not in full and "](../" not in full


def test_llms_txt_links_the_rendered_docs():
    llms = (built_site() / "llms.txt").read_text(encoding="utf-8")
    assert f"({APEX}/docs/getting-started/)" in llms
    assert f"({APEX}/llms-full.txt)" in llms
