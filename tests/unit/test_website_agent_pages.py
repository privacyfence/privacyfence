"""Guardrail 14: AI agent pages match the tested clients.

Every client in `website/_data/clients.json` (guardrail 10's one list of tested AI clients) has
exactly one page on privacyfence.eu, `/ai-agents/<slug>/`, and one published setup doc,
`docs/connect-<slug>.md`, listed in docs/README.md's "AI agent setup" section. Modelled on
guardrail 8 (tests/unit/test_website_connector_pages.py), which holds connector pages to connector
modules the same way.

A client added to the data file fails `test_every_client_has_one_page_and_every_page_a_client`
until its page is in `PAGES`, and a page added without a client fails the same test. Checked on
the pages as built (tests/website_site.py): each page is counted in GA4's `ai-agent` content group
(`pf-content-group`, the same group `build_site.content_group()` gives every `connect-*` doc),
names its client, and has a "Set it up" button to its setup doc. /ai-agents/ links every page, the
"Works with" strip links each client to its page, the header links /ai-agents/ in both of its nav
lists, and llms.txt lists every page.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.website_site import REPO, WEBSITE, build_site, built_site, read_page

pytestmark = pytest.mark.unit

GITHUB_DOCS = "https://github.com/privacyfence/privacyfence/blob/[^/]+/docs"
CLIENTS = json.loads((WEBSITE / "_data" / "clients.json").read_text(encoding="utf-8"))["clients"]
AGENT_PAGES = {f"/ai-agents/{client['slug']}/": client for client in CLIENTS}
AGENT_PAGE = re.compile(r"^/ai-agents/[a-z0-9-]+/$")


def _agent_setup_docs() -> list[str]:
    readme = (REPO / "docs" / "README.md").read_text(encoding="utf-8")
    section = next(s for s in build_site.published_nav(readme) if s.title == "AI agent setup")
    return section.docs


def _content_group(page: str) -> str | None:
    match = re.search(r'<meta name="pf-content-group" content="([^"]*)">', page)
    return match.group(1) if match else None


def test_every_client_has_one_page_and_every_page_a_client():
    built = [path for path in build_site.PAGES if AGENT_PAGE.match(path)]
    assert len(built) == len(set(built))
    assert set(built) == set(AGENT_PAGES), "every /ai-agents/<slug>/ page belongs to a clients.json entry, and each is in PAGES"
    assert "/ai-agents/" in build_site.PAGES


def test_every_setup_doc_is_a_published_ai_agent_doc():
    docs = _agent_setup_docs()
    for client in CLIENTS:
        stem = f"connect-{client['slug']}"
        assert stem in docs, f"docs/README.md's 'AI agent setup' section does not list {stem}.md"
        assert (REPO / "docs" / f"{stem}.md").is_file(), stem
        assert build_site.content_group(stem) == "ai-agent"


@pytest.mark.parametrize("path", sorted(AGENT_PAGES))
def test_agent_page_names_its_client_and_links_its_setup_doc(path):
    page = read_page(path)
    client = AGENT_PAGES[path]
    stem = f"connect-{client['slug']}"
    assert _content_group(page) == "ai-agent"
    assert f'<p class="kicker">AI agent · {client["name"]}</p>' in page, f"{path} does not name {client['name']}"
    assert re.search(
        rf'<a class="button primary" href="(/docs/{stem}/|{GITHUB_DOCS}/{stem}\.md)">Set it up</a>', page
    ), f"{path} has no 'Set it up' button to {stem}"


def test_the_overview_is_an_ai_agent_page():
    assert _content_group(read_page("/ai-agents/")) == "ai-agent"


@pytest.mark.parametrize("path", sorted(AGENT_PAGES))
def test_the_overview_links_every_agent_page(path):
    assert f'href="{path}"' in read_page("/ai-agents/")


@pytest.mark.parametrize("path", sorted(AGENT_PAGES))
def test_the_works_with_strip_links_every_agent_page(path):
    strip = re.search(r'<ul class="works-with.*?</ul>', read_page("/"), flags=re.DOTALL).group(0)
    assert f'<a href="{path}"' in strip


def test_the_header_links_the_overview_in_both_nav_lists():
    header = build_site.render_partial("header")
    inline = header.split('<div class="nav-links">', 1)[1].split("</div>", 1)[0]
    menu = header.split('<div class="nav-menu-panel">', 1)[1].split("</div>", 1)[0]
    for nav in (inline, menu):
        assert '<a href="/ai-agents/">AI agents</a>' in nav


@pytest.mark.parametrize("path", ["/ai-agents/", *sorted(AGENT_PAGES)])
def test_llms_txt_lists_every_agent_page(path):
    assert f"({build_site.SITE_URL}{path})" in (built_site() / "llms.txt").read_text(encoding="utf-8")
