"""Unit tests for scripts/build_site.py, the privacyfence.eu build.

The website guardrails (tests/unit/test_website_*.py, tests/integration/test_website_*.py) check
the built output; these check the build's own pieces: which tag the docs come from, the stale-tag
guard, partials, the /download/ pre-render, how doc links are rewritten, and the generated files.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error

import pytest

from tests.website_site import REPO, WEBSITE, build_site, read_page

pytestmark = pytest.mark.unit

MANIFEST = {
    "schema": 1,
    "version": "4.5.0",
    "channel": "stable",
    "artifacts": [
        {"id": "macos-arm64", "filename": "PrivacyFence-4.5.0.dmg", "size": 104857600, "sha256": "a" * 64},
        {"id": "linux-x64", "filename": "privacyfence_4.5.0_amd64.deb", "size": 900, "sha256": "c" * 64},
        {"id": "linux-arm64", "filename": "privacyfence_4.5.0_arm64.deb"},
    ],
}


def _has_tag(tag: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=REPO, capture_output=True
        ).returncode
        == 0
    )


# ---- Which tag the docs come from ---------------------------------------------------------------


def test_latest_stable_tag_ignores_pre_releases_and_sorts_by_version():
    tags = ["v4.4.0", "v4.5.0", "v4.5.0a3", "v4.10.0", "v4.1.0-a1", "v5.0.0rc1", "v4.9.9b2", "not-a-tag"]
    assert build_site.latest_stable_tag(tags) == "v4.10.0"


def test_no_stable_tag():
    assert build_site.latest_stable_tag(["v4.5.0a1", "v5.0.0rc1"]) is None
    assert build_site.latest_stable_tag([]) is None


@pytest.mark.skipif(not _has_tag("v4.5.0"), reason="needs the repository's tags (fetch-depth: 0)")
def test_latest_stable_tag_from_the_repository():
    tag = build_site.latest_stable_tag()
    assert tag is not None and build_site._version_key(tag) >= (4, 5, 0)


@pytest.mark.skipif(not _has_tag("v4.5.0"), reason="needs the repository's tags (fetch-depth: 0)")
def test_the_stale_tag_guard_skips_docs_for_a_tag_without_the_published_set(tmp_path):
    # v4.5.0 predates the published doc set: publishing it would put the old docs online.
    report = build_site.build(tmp_path / "_site", docs_ref="v4.5.0", fetch_manifest=False)
    assert report.docs == "skipped: stale tag"
    assert report.docs_pages == []
    site = tmp_path / "_site"
    assert not (site / "docs").exists()
    assert not (site / "llms-full.txt").exists()
    llms = (site / "llms.txt").read_text(encoding="utf-8")
    assert "https://github.com/privacyfence/privacyfence/tree/main/docs" in llms
    assert "/docs/" not in (site / "sitemap.xml").read_text(encoding="utf-8")


def test_docs_source_at_a_ref_reads_that_ref():
    head = build_site.DocsSource("HEAD")
    assert (
        head.read("docs/README.md")
        == subprocess.run(
            ["git", "show", "HEAD:docs/README.md"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
    )
    assert head.read("no/such/file.txt") is None
    assert head.kind("docs/adr") == "tree"
    assert head.kind("docs/README.md") == "blob"
    assert head.kind("nope") is None
    assert "README.md" in head.list_docs()
    assert build_site.DocsSource(None).list_docs() == sorted(p.name for p in (REPO / "docs").glob("*.md"))


def test_export_without_rendering_writes_llms_full(tmp_path):
    report = build_site.build(tmp_path / "_site", docs_ref="worktree", render=False, fetch_manifest=False)
    assert report.docs == "published"
    assert (tmp_path / "_site" / "llms-full.txt").is_file()
    assert report.docs_pages == []  # nothing rendered, so nothing to list or lay out


# ---- Partials -----------------------------------------------------------------------------------


def test_header_partial_defaults_and_overrides():
    default = build_site.render_partial("header")
    assert '<a class="skip-link" href="#top">Skip to content</a>' in default
    assert '<a class="nav-cta" href="/download/">Download</a>' in default
    custom = build_site.render_partial("header", "  ", cta_href="/x/?a=1&b=2", cta_label="All <releases>")
    assert '<a class="nav-cta" href="/x/?a=1&amp;b=2">All &lt;releases&gt;</a>' in custom
    assert all(line.startswith("  ") for line in custom.splitlines() if line.strip())


def test_partial_errors():
    with pytest.raises(build_site.BuildError):
        build_site.render_partial("nope")
    with pytest.raises(build_site.BuildError):
        build_site.render_partial("header", colour="red")
    with pytest.raises(build_site.BuildError):
        build_site.assemble_page("<body>\n  <p><!-- include: header --></p>\n</body>\n")


def test_every_page_gets_the_same_header_and_footer():
    # The partials carried over Wave 0's header, menu and footer unchanged: the four pages must
    # still be identical there, the download page's CTA aside.
    def chrome(path):
        page = read_page(path)
        header = page[page.index('<a class="skip-link"') : page.index("</header>")]
        footer = page[page.index('<footer class="site-footer') : page.index("</footer>")]
        return header.replace(
            'href="https://github.com/privacyfence/privacyfence/releases">All releases', 'href="/download/">Download'
        ), footer

    assert len({chrome(path) for path in build_site.PAGES}) == 1
    header, footer = chrome("/")
    assert '<details class="nav-menu">' in header and "nav-menu-panel" in header
    assert '<li><a href="/privacy/" data-privacy-link>Privacy</a></li>' in footer
    assert '<li><a href="/privacy/#your-choice" data-cookie-settings>Cookie settings</a></li>' in footer
    assert (
        '<a class="nav-cta" href="https://github.com/privacyfence/privacyfence/releases">All releases</a>'
        in read_page("/download/")
    )


def test_no_include_line_survives_the_build():
    for path in build_site.PAGES:
        assert "<!-- include:" not in read_page(path)
        assert "{{" not in read_page(path)


# ---- /download/ pre-render -----------------------------------------------------------------------


def test_prerender_writes_cards_version_and_release_notes():
    source = (WEBSITE / "download" / "index.html").read_text(encoding="utf-8")
    page = build_site.prerender_download(build_site.assemble_page(source), MANIFEST)
    assert build_site.LOADING_ROW not in page
    assert "<span>Version 4.5.0</span>" in page
    assert 'href="https://github.com/privacyfence/privacyfence/releases/tag/v4.5.0">Release notes</a>' in page
    assert page.count('class="download-card"') == 3
    assert 'href="https://downloads.privacyfence.eu/download/stable/macos-arm64"' in page
    assert "PrivacyFence-4.5.0.dmg · 100.0 MB" in page
    assert "privacyfence_4.5.0_amd64.deb · 1 KB" in page
    assert "<h3>linux-arm64</h3>" in page  # an id the display map lacks still gets a card
    assert f"<code>{'a' * 64}</code>" in page


def test_prerender_needs_its_placeholders():
    with pytest.raises(build_site.BuildError):
        build_site.prerender_download("<html></html>", MANIFEST)


def test_software_version_goes_into_json_ld():
    for source in ("index.html", "download/index.html"):
        page = build_site.set_software_version((WEBSITE / source).read_text(encoding="utf-8"), "4.5.0")
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, flags=re.S)
        nodes = [n for b in blocks for d in [json.loads(b)] for n in d.get("@graph", [d])]
        assert next(n for n in nodes if n["@type"] == "SoftwareApplication")["softwareVersion"] == "4.5.0"


def test_platform_names_match_download_js():
    script = (WEBSITE / "download" / "download.js").read_text(encoding="utf-8")
    in_js = dict(
        (match[1], (match[2], match[3]))
        for match in re.finditer(r"'([\w-]+)': \{ name: '([^']*)', detail: '([^']*)'", script)
    )
    assert in_js == build_site.PLATFORMS


def test_an_unreachable_worker_is_a_warning_not_a_failure(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(build_site.urllib.request, "urlopen", fail)
    assert build_site.fetch_stable_manifest() is None
    assert "could not read the stable release manifest" in capsys.readouterr().err


def test_a_manifest_without_artifacts_is_ignored(monkeypatch, capsys):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b'{"version": "4.5.0", "artifacts": []}'

    monkeypatch.setattr(build_site.urllib.request, "urlopen", lambda *a, **k: Response())
    assert build_site.fetch_stable_manifest() is None
    assert "no version or artifacts" in capsys.readouterr().err


def test_the_build_prerenders_when_given_a_manifest(tmp_path):
    report = build_site.build(tmp_path / "_site", docs_ref=None, manifest=MANIFEST)
    assert report.download_version == "4.5.0"
    page = (tmp_path / "_site" / "download" / "index.html").read_text(encoding="utf-8")
    assert "<span>Version 4.5.0</span>" in page
    assert '"softwareVersion": "4.5.0"' in (tmp_path / "_site" / "index.html").read_text(encoding="utf-8")


# ---- Docs export ---------------------------------------------------------------------------------


README = (REPO / "docs" / "README.md").read_text(encoding="utf-8")


def test_published_nav_follows_the_readme():
    nav = build_site.published_nav(README)
    assert nav[0].title == "Install and first steps"
    assert nav[0].docs[0] == "getting-started"
    stems = [stem for section in nav for stem in section.docs]
    assert len(stems) == len(set(stems))
    assert "testing-policy" not in stems and "README" not in stems


def test_published_nav_errors():
    with pytest.raises(build_site.BuildError):
        build_site.published_nav("# Docs\n\n## Something else\n")
    with pytest.raises(build_site.BuildError):
        build_site.published_nav("## User and operator docs\n\n- [a](a.md)\n\n## Contributor docs\n")
    with pytest.raises(build_site.BuildError):
        build_site.published_nav("## User and operator docs\n\n### Empty\n\n## Contributor docs\n")


@pytest.fixture
def resolver():
    return build_site.LinkResolver(build_site.DocsSource("HEAD"), {"getting-started", "how-it-works"})


def test_links_inside_the_published_set_stay_doc_links(resolver):
    assert resolver("getting-started.md") == "getting-started.md"
    assert resolver("how-it-works.md#tokens") == "how-it-works.md#tokens"
    assert resolver("how-it-works.md#tokens", absolute=True) == "https://privacyfence.eu/docs/how-it-works/#tokens"


def test_links_leaving_the_published_set_go_to_github_at_the_ref(resolver):
    blob = "https://github.com/privacyfence/privacyfence/blob/HEAD"
    assert resolver("testing-policy.md") == f"{blob}/docs/testing-policy.md"
    assert resolver("adr/0025-no-certified-security-framework.md#decision") == (
        f"{blob}/docs/adr/0025-no-certified-security-framework.md#decision"
    )
    assert resolver("../CHANGELOG.md") == f"{blob}/CHANGELOG.md"
    assert resolver("adr/") == "https://github.com/privacyfence/privacyfence/tree/HEAD/docs/adr"


def test_external_and_same_page_links_are_untouched(resolver):
    for target in ("https://example.com/a.md", "mailto:info@privacyfence.eu", "#section"):
        assert resolver(target) == target


def test_a_link_to_nothing_fails_the_build(resolver):
    with pytest.raises(build_site.BuildError, match="does not exist"):
        resolver("no-such-doc.md")
    with pytest.raises(build_site.BuildError, match="leaves the repository"):
        resolver("../../etc/passwd")


def test_rewrite_skips_code_blocks_and_images(resolver):
    text = "See [it](how-it-works.md) and [x](testing-policy.md).\n```\n[a](testing-policy.md)\n```\n![img](a.png)\n"
    out = build_site.rewrite_links(text, lambda target: f"<{target}>")
    assert "[it](<how-it-works.md>)" in out and "[x](<testing-policy.md>)" in out
    assert "[a](testing-policy.md)" in out
    assert "![img](a.png)" in out


def test_doc_description_is_the_first_paragraph():
    text = "# Title\n\n| a | b |\n\nPrivacyFence [does](x.md) `things` well.\n\n## Next\n"
    assert build_site.doc_description(text) == "PrivacyFence does things well."
    long = "# T\n\n" + "word " * 100
    described = build_site.doc_description(long, limit=50)
    assert described.endswith("…") and len(described) <= 51
    assert build_site.doc_description("# Only a title\n") == ""


def test_the_docs_index_names_docs_by_title():
    export = build_site.export_docs(build_site.DocsSource(None), "PrivacyFence (test)")
    assert export.index.startswith("# PrivacyFence documentation\n")
    assert "[Getting started](getting-started.md)" in export.index
    assert "`getting-started.md`" not in export.index
    assert "\n## Install and first steps\n" in export.index


# ---- Generated files ------------------------------------------------------------------------------


def test_sitemap_and_robots():
    xml = build_site.sitemap_xml(["/", "/docs/a/"])
    assert "<loc>https://privacyfence.eu/</loc>" in xml and "<loc>https://privacyfence.eu/docs/a/</loc>" in xml
    assert "Sitemap: https://privacyfence.eu/sitemap.xml" in build_site.ROBOTS_TXT


def test_llms_txt_starts_with_the_canonical_description():
    llms = build_site.llms_txt(None, "4.5.0")
    first = build_site.canonical_description()[0]
    assert llms.startswith(f"# PrivacyFence\n\n> {first}\n")
    assert "The current stable release is 4.5.0." in llms
    assert "llms-full.txt" not in llms


def test_main_writes_a_report(tmp_path, capsys):
    report_file = tmp_path / "report.json"
    code = build_site.main(["--out", str(tmp_path / "_site"), "--no-docs", "--offline", "--report", str(report_file)])
    assert code == 0
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["docs"] == "skipped: --no-docs"
    assert report["layout_sample"] == list(build_site.PAGES)
    assert (tmp_path / "_site" / ".nojekyll").is_file()


def test_main_reports_a_build_error(tmp_path, capsys):
    code = build_site.main(["--out", str(tmp_path / "_site"), "--docs-ref", "no-such-ref", "--offline"])
    assert code == 1
    assert "'no-such-ref' is not a commit" in capsys.readouterr().err


def test_descriptions_come_from_the_index_line_or_the_opening_paragraph():
    readme = (
        "## User and operator docs\n\n### S\n\n"
        "- [`a.md`](a.md) — every `settings.yaml` key, and the\n  `privacyfence_*` tools.\n"
        "- [`b.md`](b.md), [`c.md`](c.md) — one page per platform.\n"
        "- [`d.md`](d.md)\n\n## Contributor docs\n"
    )
    assert build_site.readme_descriptions(readme) == {
        "a": "every settings.yaml key, and the privacyfence_* tools.",
        "b": "one page per platform.",
        "c": "one page per platform.",
    }
    export = build_site.export_docs(build_site.DocsSource(None), "PrivacyFence (test)")
    assert export.descriptions["configuration-reference"].startswith("Configuration reference: every settings.yaml key")
    # The index line for the Atlassian guide only repeats its title, so its opening paragraph is used.
    assert export.descriptions["atlassian-setup"].startswith("PrivacyFence connects to Jira Cloud")


def test_descriptions_carry_no_straight_double_quote():
    # They go into an HTML attribute the docs generator writes without escaping.
    assert build_site._curly_quotes('what "Always allow" proposes') == "what “Always allow” proposes"
    export = build_site.export_docs(build_site.DocsSource(None), "PrivacyFence (test)")
    assert not any('"' in description for description in export.descriptions.values())


def test_the_build_refuses_a_manifest_that_publishes_a_private_file(monkeypatch):
    monkeypatch.setitem(build_site.STATIC, "og-source.html", "website/assets/og-source.html")
    monkeypatch.setattr(build_site, "REPOSITORY_ONLY", frozenset({"assets/og-source.html"}))
    with pytest.raises(build_site.BuildError, match="repository-only"):
        build_site.check_manifest()
