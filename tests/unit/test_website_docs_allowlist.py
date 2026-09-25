"""Guardrail 5: the docs allowlist is exhaustive.

privacyfence.eu/docs/ publishes exactly the docs in the "User and operator docs" half of
docs/README.md (scripts/build_site.py reads its sections and links as the site navigation). Every
other top-level docs/*.md must be named in build_site.CONTRIBUTOR_DOCS, the contributor half,
which stays on GitHub. A new doc that is in neither list fails here, so it is never published by
accident and never left out by accident. docs/adr/** and docs/images/** are below docs/ and are
never published. See docs/adr/0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import REPO, build_site

pytestmark = pytest.mark.unit

DOCS = REPO / "docs"
README = (DOCS / "README.md").read_text(encoding="utf-8")
NAV = build_site.published_nav(README)
PUBLISHED = {f"{stem}.md" for section in NAV for stem in section.docs}
ALL_DOCS = {path.name for path in DOCS.glob("*.md")}


def test_every_doc_is_published_or_contributor_only():
    unclassified = sorted(ALL_DOCS - PUBLISHED - build_site.CONTRIBUTOR_DOCS)
    assert not unclassified, (
        f"{unclassified} are neither in docs/README.md's 'User and operator docs' half (published on "
        "privacyfence.eu) nor in build_site.CONTRIBUTOR_DOCS (GitHub only); add each to one of them"
    )


def test_no_doc_is_both():
    both = sorted(PUBLISHED & build_site.CONTRIBUTOR_DOCS)
    assert not both, f"{both} are published and contributor-only at once"


def test_every_listed_doc_exists():
    assert PUBLISHED <= ALL_DOCS, f"docs/README.md publishes missing docs: {sorted(PUBLISHED - ALL_DOCS)}"
    missing = sorted(build_site.CONTRIBUTOR_DOCS - ALL_DOCS)
    assert not missing, f"build_site.CONTRIBUTOR_DOCS names docs that do not exist: {missing}"


def test_the_contributor_half_of_the_readme_matches_the_contributor_set():
    # The other half of docs/README.md lists the contributor docs; the set in build_site.py must
    # say the same thing, so the index and the build cannot disagree about what stays on GitHub.
    contributor_half = re.search(r"^## Contributor docs\n(.*?)^## ", README, flags=re.M | re.S)[1]
    listed = set(re.findall(r"\]\(([\w.-]+\.md)\)", contributor_half))
    assert listed == build_site.CONTRIBUTOR_DOCS - {"README.md"}


def test_every_nav_section_has_docs():
    assert NAV, "docs/README.md's published half has no sections"
    for section in NAV:
        assert section.docs, f"docs/README.md section {section.title!r} publishes no doc"
