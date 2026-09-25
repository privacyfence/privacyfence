"""Guardrail 11: no page claims what ADR 0025 rules out.

PrivacyFence has no certification, business-continuity plan or SLA
(docs/adr/0025-no-certified-security-framework.md), and does not by itself make anyone compliant.
The one place the site may use those words is where it says so: the "What PrivacyFence does not
claim" section of /security/ (`id="what-privacyfence-does-not-claim"`). Every other hand-written
page, as built (tests/website_site.py), is held to none of "certified", "compliant with", "SLA"
or "guarantee". The /docs/ pages are the published docs, which carry the same limitations in
their own words and are reviewed as docs. `ALLOWED` lists the few phrases that use one of the
words about someone else.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import build_site, read_page

pytestmark = pytest.mark.unit

FORBIDDEN = re.compile(r"\b(certified|compliant with|SLAs?|guarantee[sd]?)\b", re.IGNORECASE)
LIMITATIONS_ID = "what-privacyfence-does-not-claim"
PAGES = {path: read_page(path) for path in build_site.PAGES}
# Uses that are about someone else, not a claim PrivacyFence makes: the privacy policy names the
# certification Google's data transfer relies on.
ALLOWED = {"/privacy/": [r"certified\s+under\s+the\s+EU–US\s+Data\s+Privacy\s+Framework"]}


def _without_limitations(page: str) -> str:
    return re.sub(
        rf'<section [^>]*id="{LIMITATIONS_ID}".*?</section>', "", page, count=1, flags=re.DOTALL
    )


def test_security_has_the_limitations_section():
    page = PAGES["/security/"]
    section = re.search(rf'<section [^>]*id="{LIMITATIONS_ID}".*?</section>', page, flags=re.DOTALL)
    assert section, "/security/ has no limitations section"
    text = section.group(0)
    for needle in ("certification", "SLA", "root", "compliant", "local mode"):
        assert needle in text, f"the limitations section no longer mentions {needle!r}"


@pytest.mark.parametrize("path", ["/enterprise/", "/"])
def test_limitations_are_linked(path):
    assert f'href="/security/#{LIMITATIONS_ID}"' in PAGES[path]


@pytest.mark.parametrize("path", PAGES)
def test_no_ruled_out_claims(path):
    page = _without_limitations(PAGES[path])
    for allowed in ALLOWED.get(path, []):
        page = re.sub(allowed, "", page)
    hits = sorted({m.group(0) for m in FORBIDDEN.finditer(page)})
    assert not hits, f"{path} says {hits}; only /security/'s limitations section may (ADR 0025)"


def test_the_pattern_catches_what_it_should():
    for phrase in ("ISO 27001 certified", "compliant with GDPR", "a 99.9% SLA", "we guarantee"):
        assert FORBIDDEN.search(phrase), phrase
