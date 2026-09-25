"""Guardrail 8: connector pages match connectors.

Every module in src/privacyfence/connectors/ maps to exactly one connector page on
privacyfence.eu (`/connectors/<name>/`), one setup guide under docs/, and one row of README.md's
Connectors table. The Google connectors share a page and a guide because they share one OAuth
setup; Jira and Confluence share one because they share one Atlassian app.

A new connector module fails `test_every_connector_module_is_mapped` until it is added to
`CONNECTORS` below, with its page, guide and README row in place. Checked on the pages as built
(tests/website_site.py): each page is in the build manifest, is counted as a connector page in
GA4 (`pf-content-group`), and has a "Set it up" button to its guide, which is published as a
connector guide. /connectors/ links every connector page.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import REPO, build_site, read_page

pytestmark = pytest.mark.unit

GITHUB_DOCS = "https://github.com/privacyfence/privacyfence/blob/[^/]+/docs"

# module -> (connector page, setup guide stem, README Connectors row)
CONNECTORS: dict[str, tuple[str, str, str]] = {
    "gmail": ("/connectors/google-workspace/", "google-cloud-setup", "Gmail"),
    "drive": ("/connectors/google-workspace/", "google-cloud-setup", "Google Drive, Docs & Sheets"),
    "calendar": ("/connectors/google-workspace/", "google-cloud-setup", "Google Calendar"),
    "contacts": ("/connectors/google-workspace/", "google-cloud-setup", "Google Contacts, Tasks"),
    "tasks": ("/connectors/google-workspace/", "google-cloud-setup", "Google Contacts, Tasks"),
    "apps_script": ("/connectors/google-workspace/", "google-cloud-setup", "Google Apps Script"),
    "slack": ("/connectors/slack/", "slack-setup", "Slack"),
    "telegram": ("/connectors/telegram/", "telegram-setup", "Telegram"),
    "salesforce": ("/connectors/salesforce/", "salesforce-setup", "Salesforce"),
    "jira": ("/connectors/jira-confluence/", "atlassian-setup", "Jira"),
    "confluence": ("/connectors/jira-confluence/", "atlassian-setup", "Confluence"),
}
PAGE_GUIDES = {page: guide for page, guide, _ in CONNECTORS.values()}
CONNECTOR_PAGE = re.compile(r"^/connectors/[a-z0-9-]+/$")


def _readme_rows() -> set[str]:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Connectors", 1)[1].split("\n## ", 1)[0]
    return {m.group(1).strip() for m in re.finditer(r"^\| ([^|]+) \|", table, flags=re.M)} - {"Connector", "---"}


def test_every_connector_module_is_mapped():
    modules = {p.stem for p in (REPO / "src" / "privacyfence" / "connectors").glob("*.py")} - {"__init__"}
    assert modules == set(CONNECTORS), "map each connector module to its page, setup guide and README row"


def test_every_connector_page_is_mapped_and_built():
    built = {path for path in build_site.PAGES if CONNECTOR_PAGE.match(path)}
    assert built == set(PAGE_GUIDES), "every /connectors/<name>/ page belongs to a connector module, and each is in PAGES"


def test_every_readme_row_belongs_to_a_connector():
    assert _readme_rows() == {row for _, _, row in CONNECTORS.values()}


def test_setup_guides_are_the_published_connector_guides():
    guides = set(PAGE_GUIDES.values())
    assert guides == build_site.CONNECTOR_GUIDES
    for guide in guides:
        assert (REPO / "docs" / f"{guide}.md").is_file(), guide


@pytest.mark.parametrize("path", sorted(PAGE_GUIDES))
def test_connector_page_links_its_setup_guide(path):
    page = read_page(path)
    guide = PAGE_GUIDES[path]
    assert re.search(
        rf'<a class="button primary" href="(/docs/{guide}/|{GITHUB_DOCS}/{guide}\.md)">Set it up</a>', page
    ), f"{path} has no 'Set it up' button to {guide}"
    assert re.search(r'<meta name="pf-content-group" content="connector">', page)


@pytest.mark.parametrize("path", sorted(PAGE_GUIDES))
def test_the_overview_links_every_connector_page(path):
    assert f'href="{path}"' in read_page("/connectors/")
