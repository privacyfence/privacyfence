"""Guardrail 1: one canonical product description, word for word, on the homepage and in README.

`website/canonical-description.md` is the single source. README.md and website/index.html must
each contain it verbatim, compared after stripping HTML tags and comments, decoding entities and
collapsing whitespace, so line wrapping and markup don't matter but every word does. Changing the
description means changing that file first, then both copies, in the same PR.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "website" / "canonical-description.md"


def _normalize(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical() -> str:
    return _normalize(SOURCE.read_text(encoding="utf-8"))


def test_the_description_is_substantial():
    # Guards against an emptied or truncated source file making every check below vacuous.
    assert canonical().startswith("PrivacyFence is an open-source privacy and approval gateway")
    assert len(canonical()) > 500


@pytest.mark.parametrize("target", ["README.md", "website/index.html"])
def test_description_appears_verbatim(target):
    text = _normalize((REPO / target).read_text(encoding="utf-8"))
    assert canonical() in text, (
        f"{target} does not contain website/canonical-description.md verbatim; "
        "update it from that file (or change the file first, then every copy)"
    )


def test_readme_opens_with_it():
    # The description is what a reader of the repository (and of PyPI's project page, which
    # renders README.md) sees first, before any other section.
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    first_section = readme.split("\n## ", 1)[0]
    assert canonical() in _normalize(first_section)
