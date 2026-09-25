"""Guardrail 10: the AI clients the site names as tested are data, kept in one file.

`website/_data/clients.json` is the one list. scripts/build_site.py renders the "Works with" strip
from it wherever a page has an `include: clients` line, and writes it into the JSON-LD
`softwareRequirements` of every page that describes the software. So adding a client when a
release supports it is a one-line change to the data file, and no page can list a different set.

Checked here, on the pages as built (tests/website_site.py):

- the data file is well formed, and claude.ai is listed for organization deployments only (local
  mode listens on localhost, which claude.ai's servers cannot reach);
- the homepage carries the strip exactly as rendered from the data, and the strip markup is
  written nowhere by hand under website/;
- the JSON-LD of `/` and `/download/` names the same clients;
- no page names an AI client the data file does not list as tested (ChatGPT and Gemini are
  expected in a later release, and are added to the data file then, not before).
"""

from __future__ import annotations

import json
import re

import pytest

from tests.website_site import WEBSITE, build_site, read_page

pytestmark = pytest.mark.unit

DATA = json.loads((WEBSITE / "_data" / "clients.json").read_text(encoding="utf-8"))
NAMES = [client["name"] for client in DATA["clients"]]
PAGES = {path: read_page(path) for path in build_site.PAGES}
# AI clients the site must not name until the data file lists them.
NOT_YET_SUPPORTED = ("ChatGPT", "Gemini", "Copilot", "Cursor")


def test_the_data_file_lists_the_tested_clients():
    assert NAMES == ["Claude Desktop", "Claude Code", "claude.ai"]
    for client in DATA["clients"]:
        assert client["deployments"], client
        assert set(client["deployments"]) <= {"local", "organization"}, client
        assert client["connects"], client


def test_claude_ai_is_organization_only():
    claude_ai = next(c for c in DATA["clients"] if c["name"] == "claude.ai")
    assert claude_ai["deployments"] == ["organization"]


def test_the_homepage_renders_the_strip_from_the_data():
    strip = re.search(r'<ul class="works-with.*?</ul>', PAGES["/"], flags=re.DOTALL)
    assert strip, "the homepage has no Works with strip"
    expected = re.search(r'<ul class="works-with.*?</ul>', build_site.render_clients(), flags=re.DOTALL)
    assert re.sub(r"\s+", " ", strip.group(0)) == re.sub(r"\s+", " ", expected.group(0))


def test_the_strip_is_never_written_by_hand():
    by_hand = [
        p.relative_to(WEBSITE).as_posix()
        for p in WEBSITE.rglob("*.html")
        if 'class="works-with' in p.read_text(encoding="utf-8")
    ]
    assert by_hand == [], f"write `<!-- include: clients -->` instead of the strip itself: {by_hand}"


@pytest.mark.parametrize("path", ["/", "/download/"])
def test_json_ld_names_the_same_clients(path):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', PAGES[path], flags=re.DOTALL)
    data = [json.loads(block) for block in blocks]
    nodes = [node for item in data for node in item.get("@graph", [item])]
    app = next(n for n in nodes if n.get("@type") == "SoftwareApplication")
    assert app["softwareRequirements"] == build_site.clients_requirement()
    for name in NAMES:
        assert name in app["softwareRequirements"]


@pytest.mark.parametrize("path", PAGES)
def test_no_page_names_a_client_that_is_not_listed(path):
    text = re.sub(r"<[^>]+>", " ", PAGES[path])
    for name in NOT_YET_SUPPORTED:
        assert not re.search(rf"\b{name}\b", text), f"{path} names {name}, which clients.json does not list"
