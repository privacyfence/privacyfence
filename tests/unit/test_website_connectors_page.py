"""/connectors/ summarizes docs/tools-reference.md, and must not drift from it.

The page has one card per connector (`data-connector`), carrying that connector's tool count and
how many of its tools are `auto`, `review` and `popup` (`data-tools`, `data-auto`, `data-review`,
`data-popup`), and printing the same numbers. docs/tools-reference.md is generated from the code
(guardrail 3), so its summary table is the truth: when a tool is added or its gate changes, this
fails until the card is updated. Each card also links its connector's section of the tools
reference; the build's link check (guardrail 6) proves the anchor exists when /docs/ is built.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import REPO, read_page

pytestmark = pytest.mark.unit

PAGE = read_page("/connectors/")
SUMMARY_ROW = re.compile(
    r"^\| \[(?P<name>[^\]]+)\]\(#(?P<anchor>[a-z0-9-]+)\) \| (?P<tools>\d+) \| (?P<auto>\d+) \| (?P<review>\d+) \| (?P<popup>\d+) \|$",
    re.M,
)
CARD = re.compile(
    r'<article class="card stack connector-card" id="(?P<anchor>[a-z0-9-]+)" data-connector="(?P<name>[^"]+)"'
    r' data-tools="(?P<tools>\d+)" data-auto="(?P<auto>\d+)" data-review="(?P<review>\d+)" data-popup="(?P<popup>\d+)">'
    r"(?P<body>.*?)</article>",
    re.S,
)
REFERENCE = {m["anchor"]: m.groupdict() for m in SUMMARY_ROW.finditer((REPO / "docs" / "tools-reference.md").read_text(encoding="utf-8"))}
CARDS = {m["anchor"]: m.groupdict() for m in CARD.finditer(PAGE)}


def test_both_sides_parsed():
    assert len(REFERENCE) >= 10, "the tools reference's summary table no longer parses"
    assert CARDS, "the connector cards no longer parse"


def test_one_card_per_connector():
    assert list(CARDS) == list(REFERENCE)


@pytest.mark.parametrize("anchor", sorted(REFERENCE))
def test_card_counts_match_the_tools_reference(anchor):
    card, ref = CARDS[anchor], REFERENCE[anchor]
    for key in ("tools", "auto", "review", "popup"):
        assert card[key] == ref[key], f"/connectors/ {card['name']}: {key} is {card[key]}, the tools reference says {ref[key]}"
    printed = f"{ref['tools']} tools: {ref['auto']} without a card · {ref['review']} reviewed · {ref['popup']} need approval"
    assert printed in card["body"]
    # /docs/tools-reference/, or the same doc on GitHub when the build has no /docs/.
    assert re.search(rf'href="(/docs/tools-reference/|https://github\.com/[^"]+/tools-reference\.md)#{anchor}"', card["body"])


def test_the_totals_in_the_copy_match():
    total = sum(int(ref["tools"]) for ref in REFERENCE.values())
    assert f"{total} tools" in PAGE
    assert f"{total} connector tools" in read_page("/how-it-works/")
    assert len(REFERENCE) == 11 and "Eleven connectors" in PAGE
