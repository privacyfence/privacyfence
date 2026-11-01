"""Static checks on /install/, the install picker page, as scripts/build_site.py builds it.

The picker's behaviour is in tests/integration/test_install_page.py. What the page's own HTML must
carry is checked here: it is deployed and listed, it has the five radio groups and the eight
results the script switches between, and, inside <main>, it never mentions the deprecated desktop
installers (the header still carries the Download CTA until the header is switched to Install).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tests.website_site import build_site, built_site, read_page

pytestmark = pytest.mark.unit

RADIOS = {
    "who": ["single", "org"],
    "bundle": ["suite", "standalone"],
    "signin": ["own", "builtin"],
    "apps": ["builtin", "own"],
    "proxy": ["caddy", "nginx", "other"],
}
RESULT_KEYS = [
    "suite-single",
    "standalone-single",
    "suite-caddy-idp",
    "suite-caddy-google",
    "suite-nginx-google",
    "suite-no-config",
    "standalone-caddy",
    "standalone-proxy",
]


class _MainText(HTMLParser):
    """Collects the text and the link targets inside <main>, nothing else."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.depth += 1
        elif self.depth:
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag):
        if tag == "main" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def test_the_page_and_its_script_are_deployed():
    assert build_site.PAGES["/install/"] == "install/index.html"
    assert build_site.STATIC["install/install.js"] == "website/install/install.js"
    assert re.search(r'<script src="/install/install\.js\?v=[0-9a-f]{12}" defer></script>', read_page("/install/"))
    assert (built_site() / "install" / "install.js").is_file()


def test_the_page_is_in_the_sitemap_and_llms_txt():
    assert "<loc>https://privacyfence.eu/install/</loc>" in (built_site() / "sitemap.xml").read_text(encoding="utf-8")
    assert "[Install](https://privacyfence.eu/install/)" in (built_site() / "llms.txt").read_text(encoding="utf-8")


def test_the_picker_has_the_five_radio_groups():
    page = read_page("/install/")
    for name, values in RADIOS.items():
        found = re.findall(rf'<input type="radio" name="{name}" value="([^"]+)"', page)
        assert found == values, name


def test_there_is_one_result_per_combination():
    page = read_page("/install/")
    assert re.findall(r'<section class="install-result[^"]*" data-result="([^"]+)"', page) == RESULT_KEYS


def test_main_never_mentions_the_desktop_installers():
    parser = _MainText()
    parser.feed(read_page("/install/"))
    # The suite's own install URL ends in .../releases/latest/download/install.sh; that is GitHub's
    # path, not a link to this site's /download/ page, so it is taken out before the text check.
    text = " ".join(parser.parts).lower().replace("/releases/latest/download/", "/")
    assert text.strip()
    for forbidden in ("desktop mode", "/download/", ".dmg", ".deb", ".exe"):
        assert forbidden not in text, forbidden
    assert not [h for h in parser.hrefs if h.startswith("/download/")]
