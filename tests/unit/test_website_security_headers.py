"""Guardrail: the security headers privacyfence.eu is expected to send (WEB-9).

Cloudflare sends the headers; scripts/site_security_headers.py holds the expected set (see
docs/adr/0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md). These tests pin that set
and check that the hand-written pages, which get the strict policy, need none of the inline script
and style the docs policy allows. The browser half is tests/integration/test_website_csp.py.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tests.website_site import build_site, built_site  # first: puts scripts/ on sys.path
from site_security_headers import (  # noqa: E402
    CSP_DOCS,
    CSP_SITE,
    HASHED_INLINE_PAGES,
    HSTS,
    NOSNIFF,
    REFERRER_POLICY,
    headers_for_path,
    inline_block_hashes,
)

SCRIPT_ORIGINS = ("https://www.googletagmanager.com",)


def _directives(csp: str) -> dict[str, str]:
    return {name: value for name, _, value in (part.partition(" ") for part in csp.split("; "))}


def test_the_csp_has_the_agreed_directives():
    assert HSTS == "max-age=31536000; includeSubDomains"
    assert NOSNIFF == "nosniff"
    assert REFERRER_POLICY == "strict-origin-when-cross-origin"
    assert CSP_SITE == "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' https://www.googletagmanager.com",
            "style-src 'self'",
            "img-src 'self' data: https://*.google-analytics.com https://www.googletagmanager.com",
            (
                "connect-src 'self' https://downloads.privacyfence.eu https://*.google-analytics.com"
                + " https://*.analytics.google.com https://www.googletagmanager.com"
            ),
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def test_the_docs_csp_is_the_site_csp_plus_inline_script_and_style():
    site, docs = _directives(CSP_SITE), _directives(CSP_DOCS)
    assert list(site) == list(docs)
    assert docs["script-src"] == "'self' 'unsafe-inline' https://www.googletagmanager.com"
    assert docs["style-src"] == "'self' 'unsafe-inline'"
    assert {k: v for k, v in docs.items() if k not in ("script-src", "style-src")} == {
        k: v for k, v in site.items() if k not in ("script-src", "style-src")
    }
    assert "unsafe-inline" not in CSP_SITE


@pytest.mark.parametrize(
    "path, csp",
    [
        ("/", CSP_SITE),
        ("/download/", CSP_SITE),
        ("/documents/", CSP_SITE),
        ("/docs", CSP_DOCS),
        ("/docs/", CSP_DOCS),
        ("/docs/getting-started/", CSP_DOCS),
    ],
)
def test_headers_for_path_picks_the_docs_policy_only_under_docs(path, csp):
    headers = headers_for_path(path)
    assert headers["Content-Security-Policy"] == csp
    assert headers["Strict-Transport-Security"] == HSTS
    assert headers["X-Content-Type-Options"] == NOSNIFF
    assert headers["Referrer-Policy"] == REFERRER_POLICY


class _InlineFinder(HTMLParser):
    """Collects what the strict policy would block: inline scripts and styles, on* handlers, and
    scripts from an origin the policy does not name."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self._script: dict[str, str | None] | None = None
        self._script_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name in a:
            if name == "style" or name.startswith("on"):
                self.problems.append(f"<{tag} {name}=...>")
        if tag == "style":
            self.problems.append("<style> element")
        if tag == "script":
            self._script, self._script_text = a, ""
            src = a.get("src")
            if src and src.startswith(("http://", "https://", "//")) and not src.startswith(SCRIPT_ORIGINS):
                self.problems.append(f"<script src={src}> from an origin the CSP does not allow")

    def handle_data(self, data):
        if self._script is not None:
            self._script_text += data

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            if (
                not self._script.get("src")
                and self._script.get("type") != "application/ld+json"
                and self._script_text.strip()
            ):
                self.problems.append("inline executable <script>")
            self._script = None


def _hand_written_pages():
    site = built_site()
    unlisted = set(build_site.UNLISTED_PAGES.values())
    return sorted(
        p.relative_to(site).as_posix()
        for p in site.rglob("*.html")
        # The unlisted pages carry their own hash-based policy instead.
        if p.relative_to(site).parts[0] != "docs" and p.relative_to(site).as_posix() not in unlisted
    )


def test_hand_written_pages_need_no_unsafe_inline():
    pages = _hand_written_pages()
    assert pages, "the built site has no hand-written pages"
    offenders = {}
    for rel in pages:
        finder = _InlineFinder()
        finder.feed((built_site() / rel).read_text(encoding="utf-8"))
        if finder.problems:
            offenders[rel] = finder.problems
    assert not offenders, f"pages the strict CSP would break: {offenders}"
    assert re.search(r"unsafe-inline", CSP_SITE) is None


# ---- Pages with their own inline script and style ---------------------------------------------

CONNECTOR_CONFIG = "/tools/connector-config/"


def _page_source(path: str) -> str:
    return (build_site.WEBSITE / HASHED_INLINE_PAGES[path]).read_text(encoding="utf-8")


def _meta_csp_directives(html: str) -> dict[str, str]:
    match = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    assert match, "the page has no meta CSP"
    return _directives(match.group(1))


def test_the_connector_config_page_is_a_hashed_inline_page():
    assert CONNECTOR_CONFIG in HASHED_INLINE_PAGES
    assert HASHED_INLINE_PAGES[CONNECTOR_CONFIG] == build_site.UNLISTED_PAGES[CONNECTOR_CONFIG]


def test_the_connector_config_page_gets_the_hashes_of_its_inline_blocks():
    html = _page_source(CONNECTOR_CONFIG)
    scripts, styles = inline_block_hashes(html, "script"), inline_block_hashes(html, "style")
    assert len(scripts) == 1 and len(styles) == 1
    csp = _directives(headers_for_path(CONNECTOR_CONFIG)["Content-Security-Policy"])
    assert csp["script-src"] == f"'self' https://www.googletagmanager.com {scripts[0]}"
    assert csp["style-src"] == f"'self' {styles[0]}"
    assert "unsafe-inline" not in headers_for_path(CONNECTOR_CONFIG)["Content-Security-Policy"]
    assert {k: v for k, v in csp.items() if k not in ("script-src", "style-src")} == {
        k: v for k, v in _directives(CSP_SITE).items() if k not in ("script-src", "style-src")
    }


def test_the_header_hashes_are_the_ones_the_page_declares_in_its_meta_csp():
    meta = _meta_csp_directives(_page_source(CONNECTOR_CONFIG))
    header = _directives(headers_for_path(CONNECTOR_CONFIG)["Content-Security-Policy"])
    assert meta["script-src"] in header["script-src"].split()
    assert meta["style-src"] in header["style-src"].split()


def test_the_built_page_has_the_hashed_blocks_unchanged():
    built = (built_site() / CONNECTOR_CONFIG.strip("/") / "index.html").read_text(encoding="utf-8")
    header = _directives(headers_for_path(CONNECTOR_CONFIG)["Content-Security-Policy"])
    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        assert set(inline_block_hashes(built, tag)) <= set(header[directive].split())


@pytest.mark.parametrize(
    "path",
    ["/", "/docs/", "/tools/", "/tools/connector-config", "/tools/connector-config/x", "/tools/other/"],
)
def test_other_paths_do_not_get_the_connector_config_hashes(path):
    csp = headers_for_path(path)["Content-Security-Policy"]
    assert csp == (CSP_DOCS if path.startswith("/docs") else CSP_SITE)
    assert "sha256-" not in csp
