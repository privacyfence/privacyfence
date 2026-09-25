"""Unit tests for scripts/build_site.py, the privacyfence.eu build.

The website guardrails (tests/unit/test_website_*.py, tests/integration/test_website_*.py) check
the built output; these check the build's own pieces: which tag the docs come from, the stale-tag
guard, partials, the /download/ pre-render, how doc links are rewritten, and the generated files.
"""

from __future__ import annotations

import io
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
    # The header's Docs link and every hand-written page's links into /docs/ go to GitHub instead.
    for source in build_site.PAGES.values():
        assert 'href="/docs/' not in (site / source).read_text(encoding="utf-8"), source
    home = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="https://github.com/privacyfence/privacyfence/tree/main/docs"' in home
    assert 'href="https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md"' in home


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


def test_header_nav_lists_the_target_site_in_both_places():
    header = build_site.render_partial("header")
    inline = re.findall(r'<a href="([^"]+)">', header.split('<div class="nav-links">', 1)[1].split("</div>", 1)[0])
    menu = re.findall(r'<a href="([^"]+)">', header.split('<div class="nav-menu-panel">', 1)[1].split("</div>", 1)[0])
    assert inline == menu == ["/how-it-works/", "/security/", "/enterprise/", "/connectors/", "/docs/", "/faq/"]


def test_clients_include_is_rendered_from_the_data_file():
    page = build_site.assemble_page("<main>\n    <!-- include: clients -->\n</main>\n")
    assert '    <ul class="works-with cluster" aria-label="Tested AI clients">' in page
    for client in build_site.load_clients()["clients"]:
        assert f">{client['name']}" in page
    with pytest.raises(build_site.BuildError):
        build_site.assemble_page('<main>\n<!-- include: clients colour="red" -->\n</main>\n')


def test_clients_requirement_sentence():
    assert build_site.clients_requirement() == (
        "An MCP-compatible AI client, such as Claude Desktop, Claude Code or claude.ai (organization deployment)"
    )


def test_every_page_gets_the_same_header_and_footer():
    # The partials carried over Wave 0's header, menu and footer unchanged: the four pages must
    # still be identical there, the download page's CTA aside.
    def chrome(path):
        page = read_page(path)
        header = page[page.index('<a class="skip-link"') : page.index("</header>")]
        footer = page[page.index('<footer class="site-footer') : page.index("</footer>")]
        return header.replace('href="/releases/">All releases', 'href="/download/">Download'), footer

    assert len({chrome(path) for path in build_site.PAGES}) == 1
    header, footer = chrome("/")
    assert '<details class="nav-menu">' in header and "nav-menu-panel" in header
    assert '<li><a href="/privacy/" data-privacy-link>Privacy</a></li>' in footer
    assert '<li><a href="/privacy/#your-choice" data-cookie-settings>Cookie settings</a></li>' in footer
    assert '<li><a href="/releases/">Releases</a></li>' in footer
    assert '<a class="nav-cta" href="/releases/">All releases</a>' in read_page("/download/")


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


# ---- /releases/ pre-render -----------------------------------------------------------------------


def _installer(artifact_id, filename, **extra):
    return {"id": artifact_id, "kind": "installer", "filename": filename, **extra}


RELEASES = {
    "channels": {
        "stable": {
            "version": "4.6.1",
            "channel": "stable",
            "published_at": "2026-09-25T10:00:00Z",
            "artifacts": [
                _installer("macos-arm64", "PrivacyFence-4.6.1.dmg", size=104857600, sha256="a" * 64),
                _installer("linux-arm64", "privacyfence_4.6.1_arm64.deb"),
            ],
        },
        "alpha": {
            "version": "4.7.0a2",
            "channel": "alpha",
            "published_at": "2026-09-26T08:00:00Z",
            "artifacts": [_installer("windows-x64", "PrivacyFence-4.7.0a2-setup.exe", size=900)],
        },
        # A leftover from an older cycle: listed, and marked as older than stable.
        "rc": {
            "version": "4.0.0rc3",
            "channel": "rc",
            "published_at": "2026-08-30T08:00:00Z",
            "artifacts": [
                _installer("linux-x64", "privacyfence_4.0.0rc3_amd64.deb"),
                {"id": "sbom", "kind": "sbom", "filename": "privacyfence-4.0.0rc3.cdx.json"},
            ],
        },
        "beta": None,
    }
}


# What /api/releases/history returns: every published version on every channel, newest first.
HISTORY = {
    "releases": [
        RELEASES["channels"]["alpha"],
        RELEASES["channels"]["stable"],
        {
            "version": "4.6.1rc1",
            "channel": "rc",
            "published_at": "2026-09-22T08:00:00Z",
            "artifacts": [_installer("macos-arm64", "PrivacyFence-4.6.1rc1.dmg")],
        },
        {
            "version": "4.6.0",
            "channel": "stable",
            "published_at": "2026-09-20T08:00:00Z",
            "artifacts": [_installer("linux-x64", "privacyfence_4.6.0_amd64.deb")],
        },
        RELEASES["channels"]["rc"],
    ]
}


def test_release_version_order():
    order = ["4.10.0", "4.7.0a2", "4.6.1", "4.6.1rc1", "4.6.1b2", "4.6.1b1", "4.6.1a9", "4.0.0rc3"]
    assert sorted(order, key=build_site.release_version_key, reverse=True) == order
    assert build_site.release_version_key("4.6.1.dev3+gabc") is None
    assert build_site.release_version_key("latest") is None


def test_published_releases_are_newest_first_installers_only():
    releases = build_site.published_releases(RELEASES)
    assert [m["version"] for m in releases] == ["4.7.0a2", "4.6.1", "4.0.0rc3"]
    rc = releases[-1]
    assert [a["id"] for a in rc["artifacts"]] == ["linux-x64"]


def test_a_channel_without_installers_or_a_parseable_version_is_left_out():
    data = {
        "channels": {
            "stable": {"version": "4.6.1", "artifacts": [{"id": "sbom", "kind": "sbom", "filename": "x.json"}]},
            "beta": {"version": "4.7.0.dev1", "artifacts": [_installer("linux-x64", "x.deb")]},
            "alpha": {"version": "4.7.0a1", "artifacts": [_installer("linux-x64", "x.deb")]},
        }
    }
    assert [m["version"] for m in build_site.published_releases(data)] == ["4.7.0a1"]
    assert build_site.published_releases({"channels": {}}) == []


def test_published_releases_take_the_whole_history():
    releases = build_site.published_releases(HISTORY)
    assert [m["version"] for m in releases] == ["4.7.0a2", "4.6.1", "4.6.1rc1", "4.6.0", "4.0.0rc3"]
    assert [a["id"] for a in releases[-1]["artifacts"]] == ["linux-x64"]  # the sbom is dropped
    # Order comes from the versions, not from the route; unknown channels and junk are left out.
    shuffled = {
        "releases": [
            *reversed(HISTORY["releases"]),
            {"version": "4.9.0", "channel": "nightly", "artifacts": [_installer("linux-x64", "x.deb")]},
            {"version": "4.8.0.dev1", "channel": "stable", "artifacts": [_installer("linux-x64", "x.deb")]},
            "not a manifest",
        ]
    }
    assert build_site.published_releases(shuffled) == releases
    assert build_site.published_releases({"releases": []}) == []


def test_history_rows_mark_only_the_newest_stable_current_and_older_prereleases_superseded():
    rows = build_site.render_release_rows(build_site.published_releases(HISTORY)).split("\n")
    by_version = {re.search(r'data-version="([^"]+)"', row)[1]: row for row in rows}
    assert list(by_version) == ["4.7.0a2", "4.6.1", "4.6.1rc1", "4.6.0", "4.0.0rc3"]
    assert [v for v, row in by_version.items() if "download-badge" in row] == ["4.6.1"]
    assert [v for v, row in by_version.items() if "Superseded by 4.6.1" in row] == ["4.6.1rc1", "4.0.0rc3"]
    assert 'href="https://downloads.privacyfence.eu/download/version/4.6.0/linux-x64"' in by_version["4.6.0"]


def test_release_rows_link_the_worker_by_exact_version():
    rows = build_site.render_release_rows(build_site.published_releases(RELEASES))
    assert rows.count("<tr ") == 3
    assert 'href="https://downloads.privacyfence.eu/download/version/4.6.1/macos-arm64"' in rows
    assert 'href="https://downloads.privacyfence.eu/download/version/4.7.0a2/windows-x64"' in rows
    assert 'href="https://github.com/privacyfence/privacyfence/releases/tag/v4.6.1">Release notes</a>' in rows
    assert '<th scope="row">4.6.1 <span class="download-badge">Current</span></th>' in rows
    assert '<time datetime="2026-09-25">2026-09-25</time>' in rows
    assert '<td>Release candidate<span class="release-superseded">Superseded by 4.6.1</span></td>' in rows
    assert "<td>Alpha</td>" in rows  # newer than stable: not superseded
    assert ">linux-arm64</a>" in rows  # an id the display map lacks still gets a link
    assert '<span class="release-size">100.0 MB</span>' in rows
    assert f"<code>{'a' * 64}</code>" in rows
    assert "sbom" not in rows and "cdx.json" not in rows
    for href in re.findall(r'href="([^"]+)"', rows):
        assert href.startswith(
            (
                "https://downloads.privacyfence.eu/download/version/",
                "https://github.com/privacyfence/privacyfence/releases/tag/v",
            )
        )


def test_releases_prerender_replaces_the_loading_row():
    source = build_site.assemble_page((WEBSITE / "releases" / "index.html").read_text(encoding="utf-8"))
    page = build_site.prerender_releases(source, RELEASES)
    assert build_site.RELEASES_LOADING_ROW not in page
    assert page.count('<tr data-channel="') == 3
    # Nothing published: the loading row stays, and releases.js shows the fallback.
    assert build_site.prerender_releases(source, {"channels": {"stable": None}}) == source
    with pytest.raises(build_site.BuildError):
        build_site.prerender_releases("<html></html>", RELEASES)


def test_the_build_prerenders_the_history_when_given_it(tmp_path):
    report = build_site.build(tmp_path / "_site", docs_ref=None, manifest=MANIFEST, releases=HISTORY)
    assert report.releases == ["4.7.0a2", "4.6.1", "4.6.1rc1", "4.6.0", "4.0.0rc3"]
    page = (tmp_path / "_site" / "releases" / "index.html").read_text(encoding="utf-8")
    assert "download/version/4.6.0/linux-x64" in page


def test_the_build_prerenders_releases_when_given_the_list(tmp_path):
    report = build_site.build(tmp_path / "_site", docs_ref=None, manifest=MANIFEST, releases=RELEASES)
    assert report.releases == ["4.7.0a2", "4.6.1", "4.0.0rc3"]
    page = (tmp_path / "_site" / "releases" / "index.html").read_text(encoding="utf-8")
    assert "download/version/4.7.0a2/windows-x64" in page


def test_an_unreachable_worker_leaves_releases_to_the_browser(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(build_site.urllib.request, "urlopen", fail)
    assert build_site.fetch_all_releases() is None
    assert "could not read the release list" in capsys.readouterr().err


def _stub_worker(monkeypatch, responses):
    """urlopen answering each URL from `responses`: a dict/list is the JSON body, an exception is
    raised. Returns the URLs asked for, in order."""
    asked = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        asked.append(request.full_url)
        answer = responses[request.full_url]
        if isinstance(answer, Exception):
            raise answer
        return Response(json.dumps(answer).encode())

    monkeypatch.setattr(build_site.urllib.request, "urlopen", urlopen)
    return asked


def test_the_build_reads_the_history_first(monkeypatch):
    asked = _stub_worker(monkeypatch, {build_site.HISTORY_API: HISTORY, build_site.ALL_RELEASES_API: RELEASES})
    assert build_site.fetch_all_releases() == HISTORY
    assert asked == [build_site.HISTORY_API]


@pytest.mark.parametrize(
    "history",
    [
        urllib.error.HTTPError(build_site.HISTORY_API, 404, "Not Found", {}, None),
        urllib.error.URLError("reset"),
        {"error": "no such API route"},
    ],
    ids=["route-missing", "unreachable", "unexpected-body"],
)
def test_a_worker_without_the_history_falls_back_to_the_newest_per_channel(monkeypatch, capsys, history):
    asked = _stub_worker(monkeypatch, {build_site.HISTORY_API: history, build_site.ALL_RELEASES_API: RELEASES})
    assert build_site.fetch_all_releases() == RELEASES
    assert asked == [build_site.HISTORY_API, build_site.ALL_RELEASES_API]
    assert "falls back to the newest release per channel" in capsys.readouterr().err


def test_releases_js_mirrors_the_build():
    script = (WEBSITE / "releases" / "releases.js").read_text(encoding="utf-8")
    platforms = dict(
        (match[1], (match[2], match[3]))
        for match in re.finditer(r"'([\w-]+)': \{ name: '([^']*)', detail: '([^']*)'", script)
    )
    assert platforms == build_site.PLATFORMS
    channels = re.search(r"const CHANNEL_NAMES = \{([^}]*)\}", script)[1]
    assert dict(re.findall(r"(\w+): '([^']*)'", channels)) == build_site.CHANNEL_NAMES


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


def test_assets_are_versioned_by_content(tmp_path):
    # A deploy must never pair new HTML with a cached old script: /download/ once showed every
    # card twice when a cached download.js appended to the pre-rendered cards.
    site = tmp_path / "_site"
    (site / "sub").mkdir(parents=True)
    (site / "a.js").write_text("one", encoding="utf-8")
    (site / "b.css").write_text("two", encoding="utf-8")
    (site / "sub" / "index.html").write_text(
        '<link rel="stylesheet" href="/b.css"><script src="/a.js"></script>'
        '<script src="/missing.js"></script><img src="/a.js.png"><a href="https://x.test/c.js">x</a>',
        encoding="utf-8",
    )
    build_site.fingerprint_assets(site)
    page = (site / "sub" / "index.html").read_text(encoding="utf-8")
    one, two = (build_site.hashlib.sha256(v).hexdigest()[:12] for v in (b"one", b"two"))
    assert f'href="/b.css?v={two}"' in page
    assert f'src="/a.js?v={one}"' in page
    assert 'src="/missing.js"' in page
    assert 'src="/a.js.png"' in page
    assert 'href="https://x.test/c.js"' in page

    (site / "a.js").write_text("changed", encoding="utf-8")
    (site / "sub" / "index.html").write_text('<script src="/a.js"></script>', encoding="utf-8")
    build_site.fingerprint_assets(site)
    assert one not in (site / "sub" / "index.html").read_text(encoding="utf-8")


def test_docs_links_fall_back_to_github_when_docs_are_not_built(tmp_path):
    site = tmp_path / "_site"
    (site / "docs").mkdir(parents=True)
    (site / "index.html").write_text(
        '<a href="/docs/">d</a><a href="/docs/getting-started/#first">g</a><a href="/download/">x</a>',
        encoding="utf-8",
    )
    (site / "docs" / "index.html").write_text('<a href="/docs/">self</a>', encoding="utf-8")
    build_site.point_docs_links_at_github(site, "main")
    home = (site / "index.html").read_text(encoding="utf-8")
    base = "https://github.com/privacyfence/privacyfence"
    assert f'href="{base}/tree/main/docs"' in home
    assert f'href="{base}/blob/main/docs/getting-started.md#first"' in home
    assert 'href="/download/"' in home
    assert (site / "docs" / "index.html").read_text(encoding="utf-8") == '<a href="/docs/">self</a>'


def test_the_header_links_the_docs():
    header = (WEBSITE / "_partials" / "header.html").read_text(encoding="utf-8")
    assert header.count('<a href="/docs/">Docs</a>') == 2  # .nav-links and the <details> menu
