"""Static checks on every hand-written privacyfence.eu page (website/**/index.html).

No browser: these read the HTML. The browser-level guardrails are
tests/integration/test_website_layout.py (responsive layout) and
tests/integration/test_website_consent.py (nothing third-party before consent). This module
checks what the HTML itself must carry:

- a canonical link on the apex host, and a GA4 content group (`pf-content-group`);
- the shared footer: privacy policy, imprint, cookie settings, GitHub, license, contact;
- `site.js` (the consent banner) on every page, and no Google script tag anywhere: analytics is
  injected by site.js after consent, never written into a page;
- OpenGraph/Twitter tags and parseable JSON-LD on the homepage and /download/;
- robots.txt allows every crawler and names the sitemap; the sitemap lists every page.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
WEBSITE = REPO / "website"
APEX = "https://privacyfence.eu"
CONTENT_GROUPS = {"marketing", "download", "connector", "platform", "docs"}
MEASUREMENT_ID = "G-7Z3PFP4XPT"


def _pages() -> dict[str, str]:
    pages = {}
    for index in sorted(WEBSITE.rglob("index.html")):
        rel = index.parent.relative_to(WEBSITE).as_posix()
        pages["/" if rel == "." else f"/{rel}/"] = index.read_text(encoding="utf-8")
    return pages


PAGES = _pages()


def _meta(page: str, attr: str, name: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', page)
    return match.group(1) if match else None


@pytest.mark.parametrize("path", PAGES)
def test_canonical_link_is_on_the_apex(path):
    assert f'<link rel="canonical" href="{APEX}{path}">' in PAGES[path]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_declares_a_content_group(path):
    assert _meta(PAGES[path], "name", "pf-content-group") in CONTENT_GROUPS


@pytest.mark.parametrize("path", PAGES)
def test_every_page_has_title_and_description(path):
    assert re.search(r"<title>[^<]{10,}</title>", PAGES[path])
    description = _meta(PAGES[path], "name", "description")
    assert description and 50 <= len(description) <= 250


@pytest.mark.parametrize("path", PAGES)
def test_footer_links(path):
    footer = PAGES[path].split('<footer class="site-footer', 1)[1]
    for needle in (
        'href="/privacy/"',
        'href="/imprint/"',
        '<a href="/privacy/#your-choice" data-cookie-settings>Cookie settings</a>',
        'href="https://github.com/privacyfence/privacyfence"',
        "/blob/main/LICENSE",
        'href="mailto:info@privacyfence.eu"',
    ):
        assert needle in footer, f"{path}'s footer is missing {needle}"


@pytest.mark.parametrize("path", PAGES)
def test_consent_code_on_every_page_and_no_google_tag(path):
    page = PAGES[path]
    assert '<script src="/site.js" defer></script>' in page
    assert "googletagmanager" not in page
    assert "gtag(" not in page
    assert MEASUREMENT_ID not in page


def test_the_measurement_id_lives_only_in_site_js():
    holders = [
        p.relative_to(WEBSITE).as_posix()
        for p in WEBSITE.rglob("*")
        if p.is_file() and p.suffix in {".html", ".js"} and MEASUREMENT_ID in p.read_text(encoding="utf-8")
    ]
    assert holders == ["site.js"]


@pytest.mark.parametrize("path", ["/", "/download/"])
def test_social_card_tags(path):
    page = PAGES[path]
    assert _meta(page, "property", "og:url") == f"{APEX}{path}"
    assert _meta(page, "property", "og:image") == f"{APEX}/assets/og.png"
    assert _meta(page, "property", "og:image:width") == "1200"
    assert _meta(page, "property", "og:image:height") == "630"
    assert _meta(page, "property", "og:title")
    assert _meta(page, "property", "og:description")
    assert _meta(page, "name", "twitter:card") == "summary_large_image"
    assert (WEBSITE / "assets" / "og.png").is_file()


def test_og_image_is_1200_by_630():
    data = (WEBSITE / "assets" / "og.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (1200, 630)


def _json_ld(page: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, flags=re.DOTALL)
    nodes = []
    for block in blocks:
        data = json.loads(block)
        nodes.extend(data.get("@graph", [data]))
    return nodes


@pytest.mark.parametrize("path", ["/", "/download/"])
def test_json_ld_describes_the_software(path):
    nodes = _json_ld(PAGES[path])
    app = next(n for n in nodes if n.get("@type") == "SoftwareApplication")
    assert app["applicationCategory"] == "SecurityApplication"
    assert set(app["operatingSystem"]) == {"macOS", "Windows", "Linux"}
    assert app["offers"]["price"] == "0"
    assert "apache.org/licenses/LICENSE-2.0" in app["license"]
    assert "https://github.com/privacyfence/privacyfence" in app["sameAs"]
    assert "https://pypi.org/project/privacyfence/" in app["sameAs"]
    publisher = app["publisher"]
    if set(publisher) == {"@id"}:
        publisher = next(n for n in nodes if n.get("@id") == publisher["@id"])
    assert publisher["@type"] == "Person"
    assert publisher["name"] == "Andras Takacs"


def test_homepage_json_ld_declares_the_website():
    assert any(n.get("@type") == "WebSite" for n in _json_ld(PAGES["/"]))


def test_homepage_title_and_heading():
    page = PAGES["/"]
    assert "<title>PrivacyFence — privacy and approval gateway for AI assistants (MCP)</title>" in page
    h1 = re.sub(r"<[^>]+>", "", re.search(r"<h1>(.*?)</h1>", page, flags=re.DOTALL).group(1))
    assert re.sub(r"\s+", " ", h1).strip() == "AI access without giving AI the keys."
    assert "Approve the sensitive. Automate the routine." in page
    assert "Not authority." not in page


def test_robots_allows_every_crawler_and_names_the_sitemap():
    robots = (WEBSITE / "robots.txt").read_text(encoding="utf-8")
    rules = [line.strip() for line in robots.splitlines() if line.strip() and not line.startswith("#")]
    assert rules == ["User-agent: *", "Allow: /", f"Sitemap: {APEX}/sitemap.xml"]


def test_sitemap_lists_every_page():
    tree = ET.parse(WEBSITE / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {loc.text for loc in tree.getroot().findall("s:url/s:loc", ns)}
    assert locs == {f"{APEX}{path}" for path in PAGES}
