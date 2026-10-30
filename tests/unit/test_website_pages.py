"""Static checks on every hand-written privacyfence.eu page, as scripts/build_site.py builds it.

No browser: these read the built HTML (tests/website_site.py), so the shared header and footer
partials are checked where visitors get them. The browser-level guardrails are
tests/integration/test_website_layout.py (responsive layout) and
tests/integration/test_website_consent.py (nothing third-party before consent). This module
checks what the HTML itself must carry:

- a canonical link on the apex host, and a GA4 content group (`pf-content-group`);
- the shared footer: privacy policy, imprint, relay terms, cookie settings, GitHub, license, contact;
- `site.js` (the consent banner) on every page, and no Google script tag anywhere: analytics is
  injected by site.js after consent, never written into a page;
- OpenGraph/Twitter tags and parseable JSON-LD on the homepage and /download/;
- the generated robots.txt allows every crawler and names the sitemap; the generated sitemap
  lists every page, /docs/ included when it was built.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import pytest

from tests.website_site import WEBSITE, build_site, built_site, read_page

pytestmark = pytest.mark.unit

APEX = "https://privacyfence.eu"
CONTENT_GROUPS = {"marketing", "download", "connector", "platform", "ai-agent", "docs"}
MEASUREMENT_ID = "G-7Z3PFP4XPT"


PAGES = {path: read_page(path) for path in build_site.PAGES}


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
        'href="/relay-terms/"',
        '<a href="/privacy/#your-choice" data-cookie-settings>Cookie settings</a>',
        'href="https://github.com/privacyfence/privacyfence"',
        "/blob/main/LICENSE",
        'href="mailto:info@privacyfence.eu"',
    ):
        assert needle in footer, f"{path}'s footer is missing {needle}"


@pytest.mark.parametrize("path", PAGES)
def test_consent_code_on_every_page_and_no_google_tag(path):
    page = PAGES[path]
    assert re.search(r'<script src="/site\.js\?v=[0-9a-f]{12}" defer></script>', page)
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
    built = [
        p.relative_to(built_site()).as_posix()
        for p in built_site().rglob("*")
        if p.is_file() and p.suffix in {".html", ".js"} and MEASUREMENT_ID in p.read_text(encoding="utf-8")
    ]
    assert built == ["site.js"]


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
    assert "<title>Open-source MCP gateway with human approval — PrivacyFence</title>" in page
    h1 = re.sub(r"<[^>]+>", "", re.search(r"<h1>(.*?)</h1>", page, flags=re.DOTALL).group(1))
    assert re.sub(r"\s+", " ", h1).strip() == "AI access without giving AI the keys."
    assert "Approve the sensitive. Automate the routine." in page
    assert "Not authority." not in page


def test_robots_allows_every_crawler_and_names_the_sitemap():
    robots = (built_site() / "robots.txt").read_text(encoding="utf-8")
    rules = [line.strip() for line in robots.splitlines() if line.strip() and not line.startswith("#")]
    assert rules == ["User-agent: *", "Allow: /", f"Sitemap: {APEX}/sitemap.xml"]


def test_sitemap_lists_every_page():
    tree = ET.parse(built_site() / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {loc.text for loc in tree.getroot().findall("s:url/s:loc", ns)}
    pages = set()
    for index in built_site().rglob("index.html"):
        rel = index.parent.relative_to(built_site()).as_posix()
        pages.add("/" if rel == "." else f"/{rel}/")
    # Unlisted pages are published but deliberately kept out of the sitemap.
    assert locs == {f"{APEX}{path}" for path in pages - set(build_site.UNLISTED_PAGES)}


def test_robots_and_sitemap_are_generated_not_hand_written():
    # Both are written by scripts/build_site.py; a hand-written copy under website/ would be
    # silently overwritten, or worse, published instead.
    assert not (WEBSITE / "robots.txt").exists()
    assert not (WEBSITE / "sitemap.xml").exists()


# ---- Second-level pages: BreadcrumbList, cross-links --------------------------------------------

SECOND_LEVEL = [
    path for path in build_site.PAGES if re.fullmatch(r"/(?:ai-agents|compare|connectors)/[a-z0-9-]+/", path)
]
CONNECTOR_PAGES = [path for path in SECOND_LEVEL if path.startswith("/connectors/")]


def _json_ld(page: str) -> list[dict]:
    nodes = []
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, flags=re.S):
        data = json.loads(raw)
        nodes.extend(data.get("@graph", [data]))
    return nodes


def _canonical(page: str) -> str:
    return re.search(r'<link rel="canonical" href="([^"]+)"', page)[1]


def test_there_are_fifteen_second_level_pages():
    assert len(SECOND_LEVEL) == 15


@pytest.mark.parametrize("path", SECOND_LEVEL)
def test_every_second_level_page_has_a_breadcrumb_ending_at_its_canonical_url(path):
    page = PAGES[path]
    crumbs = [n for n in _json_ld(page) if n.get("@type") == "BreadcrumbList"]
    assert len(crumbs) == 1
    items = crumbs[0]["itemListElement"]
    assert [i["position"] for i in items] == [1, 2, 3]
    assert items[0]["item"] == f"{APEX}/"
    assert items[1]["item"] == f"{APEX}/{path.split('/')[1]}/"
    assert items[-1]["item"] == _canonical(page)
    assert all(i["name"] for i in items)


@pytest.mark.parametrize("path", [p for p in build_site.PAGES if p not in SECOND_LEVEL])
def test_no_other_hand_written_page_gets_a_generated_breadcrumb(path):
    assert not [n for n in _json_ld(PAGES[path]) if n.get("@type") == "BreadcrumbList"]


@pytest.mark.parametrize("path", ["/", "/download/"])
def test_same_as_lists_the_glama_listing(path):
    app = next(n for n in _json_ld(PAGES[path]) if n.get("@type") == "SoftwareApplication")
    assert "https://glama.ai/mcp/servers/privacyfence/privacyfence" in app["sameAs"]


def _main_links(page: str) -> set[str]:
    main = re.search(r"<main.*?</main>", page, flags=re.S)[0]
    return {re.sub(r"#.*", "", h) for h in re.findall(r'href="(/[^"]*)"', main)}


@pytest.mark.parametrize("path", CONNECTOR_PAGES)
def test_a_connector_page_links_the_other_connectors_from_its_content(path):
    links = _main_links(PAGES[path])
    assert (set(CONNECTOR_PAGES) - {path}) | {"/connectors/"} <= links
    assert path not in links, "the block leaves out the page it is on"


def test_content_links_into_the_weakly_linked_pages():
    """Header and footer excluded: every connector page and the FAQ get at least three links from
    the content of other pages."""
    for target in [*CONNECTOR_PAGES, "/faq/"]:
        linking = [p for p, page in PAGES.items() if p != target and target in _main_links(page)]
        assert len(linking) >= 3, f"{target} is linked from the content of only {linking}"


# ---- Unlisted pages -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", build_site.UNLISTED_PAGES)
def test_an_unlisted_page_is_built_unchanged_and_is_not_linked_or_indexed(path):
    source = build_site.UNLISTED_PAGES[path]
    # Byte for byte: the page's CSP hashes cover its inline script and style, so nothing may be
    # injected into it.
    assert read_page(path).encode("utf-8") == (WEBSITE / source).read_bytes()
    assert _meta(read_page(path), "name", "robots") == "noindex, nofollow"
    assert path not in (built_site() / "sitemap.xml").read_text(encoding="utf-8")
    assert path not in (built_site() / "llms.txt").read_text(encoding="utf-8")
    for page_path, page in PAGES.items():
        assert f'href="{path}"' not in page, f"{page_path} links to the unlisted page {path}"
    assert path not in build_site.PAGES


LIMITED_USE_SENTENCE = (
    "PrivacyFence's use and transfer to any other app of information received from Google APIs "
    "will adhere to the Google API Services User Data Policy, including the Limited Use requirements."
)


def _text(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", page))


def test_relay_terms_page_is_wired():
    assert "/relay-terms/" in build_site.PAGES
    assert '<link rel="canonical" href="https://privacyfence.eu/relay-terms/">' in PAGES["/relay-terms/"]
    assert "oauth.privacyfence.eu" in PAGES["/relay-terms/"]
    assert "[Relay terms]" in (built_site() / "llms.txt").read_text(encoding="utf-8")


def test_privacy_page_describes_the_relay_and_google_user_data():
    page = PAGES["/privacy/"]
    assert 'id="relay"' in page
    assert "oauth.privacyfence.eu" in page
    assert 'id="google-user-data"' in page
    assert LIMITED_USE_SENTENCE in _text(page)
    assert "https://developers.google.com/terms/api-services-user-data-policy" in page


@pytest.mark.parametrize("path", PAGES)
def test_no_page_claims_there_is_no_privacyfence_server(path):
    text = _text(PAGES[path])
    assert "there are none" not in text
    assert "no PrivacyFence-operated server is involved" not in text


@pytest.mark.parametrize("path", ["/download/", "/faq/"])
def test_desktop_mode_deprecation_notice(path):
    assert "PrivacyFence 8 removes it" in _text(PAGES[path])
