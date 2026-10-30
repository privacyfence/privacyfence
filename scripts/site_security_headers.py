#!/usr/bin/env python3
"""The security headers privacyfence.eu is expected to send.

GitHub Pages cannot set response headers, so Cloudflare does (three Modify Response Header
Transform Rules, docs/adr/0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md). This
module holds the *expected* headers: tests/website_site.py serves the built site under them so the
browser tests prove the pages work under the policy, scripts/check_site_headers.py compares the
live site with them after every deploy, and running this file prints what to enter in the
Cloudflare dashboard.

Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

HSTS = "max-age=31536000; includeSubDomains"
NOSNIFF = "nosniff"
REFERRER_POLICY = "strict-origin-when-cross-origin"

_GTM = "https://www.googletagmanager.com"
_GA = "https://*.google-analytics.com"

_SCRIPT_SRC_SITE = f"script-src 'self' {_GTM}"
_STYLE_SRC_SITE = "style-src 'self'"

_DIRECTIVES_BEFORE_SCRIPT = ["default-src 'self'"]
_DIRECTIVES_AFTER_STYLE = [
    f"img-src 'self' data: {_GA} {_GTM}",
    f"connect-src 'self' https://downloads.privacyfence.eu {_GA} https://*.analytics.google.com {_GTM}",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
]


def _csp(script_src: str, style_src: str) -> str:
    return "; ".join([*_DIRECTIVES_BEFORE_SCRIPT, script_src, style_src, *_DIRECTIVES_AFTER_STYLE])


CSP_SITE = _csp(_SCRIPT_SRC_SITE, _STYLE_SRC_SITE)
# The docs generator's inline bootstrap scripts and table-alignment styles (docs/adr/0201).
CSP_DOCS = _csp(f"script-src 'self' 'unsafe-inline' {_GTM}", "style-src 'self' 'unsafe-inline'")


# Pages that carry their own inline script and style instead of loading them from 'self'. Each
# gets the site policy plus the sha256 of exactly its inline blocks, read from the page, so no
# other inline code is allowed and the hashes cannot drift from the file.
_WEBSITE = Path(__file__).resolve().parent.parent / "website"
HASHED_INLINE_PAGES: dict[str, str] = {
    "/tools/connector-config/": "tools/connector-config/index.html",
}


def inline_block_hashes(html: str, tag: str) -> list[str]:
    """CSP `'sha256-...'` sources for the attribute-less inline `<tag>` blocks of `html`."""
    return [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii") + "'"
        for body in re.findall(rf"<{tag}>(.*?)</{tag}>", html, flags=re.DOTALL)
    ]


def csp_for_hashed_page(path: str) -> str:
    """The site CSP with the hashes of `path`'s inline script and style added."""
    html = (_WEBSITE / HASHED_INLINE_PAGES[path]).read_text(encoding="utf-8")
    scripts, styles = inline_block_hashes(html, "script"), inline_block_hashes(html, "style")
    return _csp(" ".join([_SCRIPT_SRC_SITE, *scripts]), " ".join([_STYLE_SRC_SITE, *styles]))


def headers_for_path(path: str) -> dict[str, str]:
    """The four headers the site sends for URL path `path` (no query string)."""
    docs = path == "/docs" or path.startswith("/docs/")
    if path in HASHED_INLINE_PAGES:
        csp = csp_for_hashed_page(path)
    else:
        csp = CSP_DOCS if docs else CSP_SITE
    return {
        "Strict-Transport-Security": HSTS,
        "X-Content-Type-Options": NOSNIFF,
        "Referrer-Policy": REFERRER_POLICY,
        "Content-Security-Policy": csp,
    }


def main() -> int:
    """Prints the Cloudflare rules for the dashboard."""
    site = headers_for_path("/")
    hashed = " AND ".join(f"URI Path does not equal {p}" for p in HASHED_INLINE_PAGES)
    rules = [
        (
            "pf-security-headers-all",
            "Hostname equals privacyfence.eu",
            [
                f"{name}: {site[name]}"
                for name in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy")
            ],
        ),
        (
            "pf-csp-site",
            f"Hostname equals privacyfence.eu AND URI Path does not equal /docs AND URI Path does not start with /docs/ AND {hashed}",
            [f"Content-Security-Policy: {CSP_SITE}"],
        ),
        (
            "pf-csp-docs",
            "Hostname equals privacyfence.eu AND (URI Path equals /docs OR URI Path starts with /docs/)",
            [f"Content-Security-Policy: {CSP_DOCS}"],
        ),
        *(
            (
                f"pf-csp-{path.strip('/').replace('/', '-')}",
                f"Hostname equals privacyfence.eu AND URI Path equals {path}",
                [f"Content-Security-Policy: {csp_for_hashed_page(path)}"],
            )
            for path in HASHED_INLINE_PAGES
        ),
    ]
    for name, match, lines in rules:
        print(f"Rule: {name}")
        print(f"  When: {match}")
        print("  Set response headers:")
        for line in lines:
            print(f"    {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
