"""Guardrail 6: no broken links on privacyfence.eu.

Two halves, both checked on every run:

- **The built site.** Every internal `href`/`src` in every HTML page scripts/build_site.py
  writes resolves to a file in the output, and every `#fragment` to an id on the target page. The
  build itself refuses to finish with a broken one; this holds it to that. With the `docs` extra
  installed the check covers /docs/ as well (the docs generator also runs in strict mode, which
  fails on a link to a page that does not exist).
- **The docs export.** Every relative link in every published doc either stays inside the
  published set or is rewritten to a GitHub URL for a path that exists. This half needs no docs
  generator, so it runs everywhere.
"""

from __future__ import annotations

import pytest

from tests.website_site import build_site, built_site

pytestmark = pytest.mark.unit


def test_the_built_site_has_no_broken_internal_links():
    assert build_site.check_links(built_site()) == []


def test_every_published_doc_link_resolves():
    # Raises BuildError naming the doc link that points at nothing.
    export = build_site.export_docs(build_site.DocsSource(None), "PrivacyFence (working tree)")
    assert export.stems, "the export published no docs"
    for stem, text in export.markdown.items():
        for match in build_site.MD_LINK_RE.finditer(text):
            target = match["target"]
            assert (
                target.startswith(("https://", "http://", "mailto:", "#"))
                or target.split("#", 1)[0][:-3] in export.stems
            ), f"{stem}.md keeps a relative link {target!r} outside the published set"


def test_the_link_checker_finds_a_broken_link(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "index.html").write_text(
        '<h2 id="here">x</h2><a href="/b/">b</a><a href="#here">ok</a><a href="#gone">anchor</a>'
        '<a href="https://privacyfence.eu/a/">self</a><a href="https://example.com/x">external</a>'
        '<img src="/missing.png">',
        encoding="utf-8",
    )
    assert build_site.check_links(tmp_path) == [
        "/a/index.html -> /b/",
        "/a/index.html -> #gone (no id 'gone')",
        "/a/index.html -> /missing.png",
    ]
