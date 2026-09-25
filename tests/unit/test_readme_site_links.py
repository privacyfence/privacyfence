"""README.md's links to privacyfence.eu point at pages the site actually builds.

README.md is also the PyPI long description, so it links its documentation on privacyfence.eu
rather than in the repository. Nothing fetches those URLs here (no network in this tier), so this
checks them against what scripts/build_site.py publishes instead: a `/docs/<name>/` link names a
doc in the published half of docs/README.md, and its `#anchor` is one of that doc's headings; any
other path is a hand-written page in the build manifest. tests/unit/test_docs_links.py covers the
README's links back into this repository.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_docs_links import _anchors
from tests.website_site import REPO, build_site

pytestmark = pytest.mark.unit

README = (REPO / "README.md").read_text(encoding="utf-8")
SITE_LINK = re.compile(r"\]\((https://privacyfence\.eu(/[^)\s]*))\)")
LINKS = sorted({(match[1], match[2]) for match in SITE_LINK.finditer(README)})
PUBLISHED = {
    stem
    for section in build_site.published_nav((REPO / "docs" / "README.md").read_text(encoding="utf-8"))
    for stem in section.docs
}


def test_the_readme_links_its_docs_on_the_site():
    assert sum(1 for _, path in LINKS if path.startswith("/docs/")) >= 10, LINKS


@pytest.mark.parametrize("url, path", LINKS, ids=[url for url, _ in LINKS])
def test_site_link_resolves(url: str, path: str):
    page, _, anchor = path.partition("#")
    docs = re.fullmatch(r"/docs/(?P<stem>[a-z0-9-]+)/", page)
    if docs:
        stem = docs["stem"]
        assert stem in PUBLISHED, f"README.md links {url}, but {stem}.md is not a published doc"
        if anchor:
            assert anchor in _anchors(REPO / "docs" / f"{stem}.md"), f"README.md links {url}: no such heading"
        return
    assert not anchor, f"README.md links {url}: only /docs/ anchors are checked; link the page"
    assert page in {*build_site.PAGES, "/docs/"}, f"README.md links {url}, which the site build does not publish"
