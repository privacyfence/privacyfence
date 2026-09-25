#!/usr/bin/env python3
"""Build privacyfence.eu into one directory: the hand-written pages and the published docs.

This is the whole website build. `.github/workflows/pages.yml` runs it to produce what GitHub
Pages deploys, `.github/workflows/website-build.yml` runs it on every pull request, and the
website tests run it to get the pages they check. Nothing reaches the public site except what this
script writes.

    python3 scripts/build_site.py --out _site

What it does, in order:

1. **Hand-written pages.** Every page in `PAGES` is read from `website/`, its
   `<!-- include: NAME key="value" -->` lines are replaced with `website/_partials/NAME.html`, and
   it is written to the same path under the output directory. An `include: clients` line is the
   "Works with" strip, rendered from `website/_data/clients.json`, which also fills the JSON-LD
   `softwareRequirements` (guardrail 10). Every file in `STATIC` is copied
   as is. Nothing else under `website/` is published: `REPOSITORY_ONLY` files stay in the
   repository, and `BUILD_INPUTS` are read by this script and never copied.
   tests/unit/test_website_download_cta.py holds every file under `website/` to exactly one of
   those four lists (guardrail 7).
2. **`/download/` pre-rendered.** The current stable release manifest is fetched from the
   download Worker and written into the page as static cards, so the page shows a version and
   working download links without JavaScript; `download.js` still refreshes it in the browser.
   If the Worker cannot be reached the build warns and the page keeps its loading row, exactly as
   before. The version also goes into the JSON-LD `softwareVersion` of `/` and `/download/`.
3. **`/docs/` from the latest stable release.** The docs are the ones at the newest stable tag
   (`vX.Y.Z`, the same channel rule as `scripts/r2_release.py`), not `main`: the site documents
   the version people can download. The published set, its order and its sections come from
   `docs/README.md`'s "User and operator docs" half at that tag; `CONTRIBUTOR_DOCS` is the other
   half, which stays on GitHub (tests/unit/test_website_docs_allowlist.py, guardrail 5). Each doc
   is exported, links that leave the published set are rewritten to GitHub at the same tag, and the
   result is rendered by the docs generator (Zensical, in strict mode) with the site's own header,
   footer, tokens and consent code (`website/_docs/`). See
   docs/adr/0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md.
4. **Stale-tag guard.** If that tag predates the published doc set (it has no
   `docs/how-it-works.md`), `/docs/` and `llms-full.txt` are skipped with a warning and
   `llms.txt` points at the docs on GitHub instead. The first stable release that carries the
   published set turns `/docs/` on by itself. Whenever `/docs/` is not built, the hand-written
   pages' links into it (the header's Docs link among them) are pointed at the docs on GitHub.
5. **Generated files.** `sitemap.xml`, `robots.txt`, `llms.txt` and (with docs) `llms-full.txt`.
6. **Link check.** Every internal link and fragment in every HTML page of the output must resolve
   (guardrail 6). A broken one fails the build.

Options for previews and tests: `--docs-ref REF` renders the docs from another git ref, and
`--docs-ref worktree` from the checkout as it is; `--no-docs` skips `/docs/`; `--offline` skips
the Worker fetch; `--release-manifest FILE` pre-renders `/download/` from a saved manifest.
`--report FILE` writes a JSON summary of what was built.

Requires the `docs` extra (`pip install --require-hashes -r requirements/docs.lock.txt`) only when
`/docs/` is rendered. Imports nothing from the `privacyfence` package.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r2_release import channel_for_version  # noqa: E402 -- a sibling script, not a package

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"

SITE_URL = "https://privacyfence.eu"
GITHUB_URL = "https://github.com/privacyfence/privacyfence"
RELEASES_API = "https://downloads.privacyfence.eu/api/releases/stable"
DOWNLOAD_ORIGIN = "https://downloads.privacyfence.eu"

# ---- The build manifest ----------------------------------------------------------------------

# URL path -> page source under website/. Each is assembled from the shared partials.
PAGES: dict[str, str] = {
    "/": "index.html",
    "/how-it-works/": "how-it-works/index.html",
    "/security/": "security/index.html",
    "/enterprise/": "enterprise/index.html",
    "/connectors/": "connectors/index.html",
    "/download/": "download/index.html",
    "/privacy/": "privacy/index.html",
    "/imprint/": "imprint/index.html",
}

# Output path -> source path relative to the repository root, copied byte for byte.
STATIC: dict[str, str] = {
    "tokens.css": "website/tokens.css",
    "chrome.css": "website/chrome.css",
    "styles.css": "website/styles.css",
    "docs-theme.css": "website/_docs/docs-theme.css",
    "site.js": "website/site.js",
    "stats.js": "website/stats.js",
    "download/download.js": "website/download/download.js",
    "assets/og.png": "website/assets/og.png",
    "assets/architecture.svg": "website/assets/architecture.svg",
    "assets/architecture-narrow.svg": "website/assets/architecture-narrow.svg",
    "assets/icon.png": "src/privacyfence/resources/icon_512.png",
    "assets/gmail-read-thread.png": "docs/images/screenshots/gmail-read-thread.png",
    "assets/sheets-write.png": "docs/images/screenshots/sheets-write.png",
}

# Files under website/ this script reads to build other files, and never publishes themselves.
BUILD_INPUTS: frozenset[str] = frozenset(
    {
        "_partials/header.html",
        "_partials/footer.html",
        "_docs/overrides/main.html",
        "_data/clients.json",
    }
)

# Files under website/ that deliberately stay in the repository: the source of the canonical
# description (tests/unit/test_website_canonical_description.py) and the source the OpenGraph
# image is rendered from.
REPOSITORY_ONLY: frozenset[str] = frozenset({"canonical-description.md", "assets/og-source.html"})

# The docs/*.md files that are not published: the contributor half of docs/README.md, and the
# index itself. Every other docs/*.md must be in the published half (guardrail 5). docs/adr/** and
# docs/images/screenshots/README.md are below docs/, so they are never candidates.
CONTRIBUTOR_DOCS: frozenset[str] = frozenset(
    {
        "README.md",
        "coding-and-testing-guidelines.md",
        "dev-vs-live-setup.md",
        "testing-policy.md",
        "release-testing.md",
        "packaging.md",
        "connector-qa.md",
        "downloads-and-release-kpi.md",
    }
)

# Published docs the browser layout test (guardrail 12) loads in addition to every hand-written
# page: the entry point, the widest tables, and the longest code blocks.
DOCS_LAYOUT_SAMPLE = ("/docs/getting-started/", "/docs/tools-reference/", "/docs/org-mode-setup-guide/")

# The file whose presence marks a tag as carrying the published doc set (the stale-tag guard).
PUBLISHED_SET_MARKER = "docs/how-it-works.md"

# GA4 content group per published doc (decision I1): platform install pages and connector setup
# guides are counted apart from the rest of the docs. Everything else is "docs".
CONNECTOR_GUIDES = frozenset(
    {"google-cloud-setup", "slack-setup", "salesforce-setup", "atlassian-setup", "telegram-setup"}
)

# Display names per release-manifest artifact id. Mirrors the PLATFORMS map in
# website/download/download.js, which renders the same cards in the browser;
# tests/unit/test_build_site.py keeps the two in step.
PLATFORMS: dict[str, tuple[str, str]] = {
    "macos-arm64": ("macOS", "Apple silicon · installer + Claude extension"),
    "windows-x64": ("Windows", "64-bit"),
    "linux-x64": ("Linux", "Debian / Ubuntu, 64-bit"),
}

ROBOTS_TXT = f"""\
# privacyfence.eu welcomes every crawler: search engines, AI search and AI training alike.
# See docs/adr/0048-every-crawler-is-allowed-including-ai-training.md.
User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""


class BuildError(RuntimeError):
    """The build cannot produce a correct site. Raised, never swallowed."""


def warn(message: str) -> None:
    print(f"::warning::{message}" if _in_actions() else f"warning: {message}", file=sys.stderr)


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


# ---- Git -------------------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed git subcommands, no shell
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def _version_key(tag: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in tag.lstrip("v").split("."))
    return major, minor, patch


def latest_stable_tag(tags: list[str] | None = None) -> str | None:
    """The newest `vX.Y.Z` tag whose channel is stable, or None if there is none."""
    if tags is None:
        tags = _git("tag", "--list", "v*").split()
    stable = []
    for tag in tags:
        try:
            if channel_for_version(tag.lstrip("v")) == "stable" and re.fullmatch(r"v\d+\.\d+\.\d+", tag):
                stable.append(tag)
        except ValueError:
            continue  # a spelling r2_release does not treat as a release version
    return max(stable, key=_version_key) if stable else None


@dataclass
class DocsSource:
    """Where the docs are read from: a git ref, or the working tree (`ref=None`)."""

    ref: str | None

    @property
    def blob_ref(self) -> str:
        """The ref GitHub links point at: the tag being published, or `main` for a preview."""
        return self.ref or "main"

    def read(self, path: str) -> str | None:
        if self.ref is None:
            file = REPO / path
            return file.read_text(encoding="utf-8") if file.is_file() else None
        try:
            return _git("show", f"{self.ref}:{path}")
        except subprocess.CalledProcessError:
            return None

    def kind(self, path: str) -> str | None:
        """ "blob" for a file, "tree" for a directory, None if the path does not exist."""
        path = path.rstrip("/")
        if self.ref is None:
            target = REPO / path
            return "tree" if target.is_dir() else "blob" if target.is_file() else None
        try:
            return _git("cat-file", "-t", f"{self.ref}:{path}").strip()
        except subprocess.CalledProcessError:
            return None

    def list_docs(self) -> list[str]:
        """File names of every docs/*.md (top level only)."""
        if self.ref is None:
            return sorted(p.name for p in (REPO / "docs").glob("*.md"))
        names = _git("ls-tree", "--name-only", f"{self.ref}:docs").split()
        return sorted(name for name in names if name.endswith(".md"))


# ---- Partials --------------------------------------------------------------------------------

INCLUDE_RE = re.compile(r"^(?P<indent>[ \t]*)<!-- include: (?P<name>[a-z-]+)(?P<args>[^>]*?) -->\n", re.M)
PARTIAL_DEFAULTS = {
    "header": {"skip_target": "#top", "cta_href": "/download/", "cta_label": "Download"},
    "footer": {},
}


def render_partial(name: str, indent: str = "", **overrides: str) -> str:
    """`website/_partials/<name>.html` with its `{{key}}` placeholders filled, each line indented
    by `indent`. Lines starting with `<!--#` are notes for whoever edits the partial and are
    dropped."""
    if name not in PARTIAL_DEFAULTS:
        raise BuildError(f"unknown partial {name!r}")
    unknown = set(overrides) - set(PARTIAL_DEFAULTS[name])
    if unknown:
        raise BuildError(f"partial {name!r} takes no argument(s) {sorted(unknown)}")
    values = {**PARTIAL_DEFAULTS[name], **overrides}
    text = (WEBSITE / "_partials" / f"{name}.html").read_text(encoding="utf-8")
    text = re.sub(r"^<!--#.*?-->\n", "", text, flags=re.M | re.S)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", html.escape(value, quote=True))
    leftover = re.findall(r"\{\{\w+\}\}", text)
    if leftover:
        raise BuildError(f"partial {name!r} has unfilled placeholders {leftover}")
    return "".join(f"{indent}{line}" if line.strip() else line for line in text.splitlines(keepends=True))


# ---- The tested AI clients (guardrail 10) --------------------------------------------------------

CLIENTS_FILE = WEBSITE / "_data" / "clients.json"
DEPLOYMENT_NOTES = {("organization",): "organization deployment"}


def load_clients() -> dict:
    """website/_data/clients.json: the one list of AI clients the site names as tested."""
    data = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    for client in data["clients"]:
        if not client.get("name") or not set(client.get("deployments", [])) <= {"local", "organization"}:
            raise BuildError(f"{CLIENTS_FILE.name}: a client needs a name and deployments from local/organization: {client}")
    return data


def render_clients(indent: str = "") -> str:
    """The "Works with" strip, from the clients data file."""
    data = load_clients()
    e = html.escape
    items = []
    for client in data["clients"]:
        note = DEPLOYMENT_NOTES.get(tuple(client["deployments"]))
        suffix = f' <small>{e(note)}</small>' if note else ""
        items.append(f'  <li class="client" title="{e(client["name"])}, {e(client["connects"])}">{e(client["name"])}{suffix}</li>')
    lines = [
        '<ul class="works-with cluster" aria-label="Tested AI clients">',
        '  <li class="works-with-label">Works with</li>',
        *items,
        f'  <li class="works-with-more">and {e(data["others"])}</li>',
        "</ul>",
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


def clients_requirement() -> str:
    """The same list as one sentence, for the JSON-LD `softwareRequirements`."""
    data = load_clients()
    names = []
    for client in data["clients"]:
        note = DEPLOYMENT_NOTES.get(tuple(client["deployments"]))
        names.append(f'{client["name"]} ({note})' if note else client["name"])
    listed = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else names[0]
    return f"An MCP-compatible AI client, such as {listed}"


def set_clients(page: str) -> str:
    """Adds `softwareRequirements` from the clients data file to the page's JSON-LD
    SoftwareApplication node."""
    return _update_software_node(page, softwareRequirements=clients_requirement())


GENERATED_PARTIALS: dict[str, Callable[[str], str]] = {"clients": render_clients}


def assemble_page(source: str) -> str:
    """A page from website/ with every include line replaced by its partial."""

    def include(match: re.Match[str]) -> str:
        args = dict((key, value) for key, value in (arg.split("=", 1) for arg in shlex.split(match["args"])))
        if match["name"] in GENERATED_PARTIALS:
            if args:
                raise BuildError(f"partial {match['name']!r} takes no arguments")
            return GENERATED_PARTIALS[match["name"]](match["indent"])
        return render_partial(match["name"], match["indent"], **args)

    page = INCLUDE_RE.sub(include, source)
    if "<!-- include:" in page:
        raise BuildError('an include line did not match `<!-- include: NAME key="value" -->` on a line of its own')
    return page


# ---- /download/ pre-render --------------------------------------------------------------------


def fetch_stable_manifest(url: str = RELEASES_API, timeout: float = 15) -> dict | None:
    """The stable release manifest from the download Worker, or None (with a warning) if it
    cannot be read. The site still builds without it: the page then loads it in the browser."""
    request = urllib.request.Request(url, headers={"User-Agent": "privacyfence-site-build"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 -- fixed https URL
            manifest = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        warn(f"could not read the stable release manifest ({exc}); /download/ is built without pre-rendered cards")
        return None
    if not isinstance(manifest, dict) or not manifest.get("version") or not manifest.get("artifacts"):
        warn("the stable release manifest has no version or artifacts; /download/ is built without pre-rendered cards")
        return None
    return manifest


def _format_size(size: object) -> str:
    if not isinstance(size, (int, float)) or size <= 0:
        return ""
    mb = size / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 1 else f"{round(size / 1024)} KB"


def render_download_cards(manifest: dict) -> str:
    """The same cards download.js builds, as static HTML (no platform highlight: that needs the
    visitor's browser)."""
    channel = manifest.get("channel", "stable")
    cards = []
    for artifact in manifest.get("artifacts", []):
        artifact_id = str(artifact.get("id", ""))
        name, detail = PLATFORMS.get(artifact_id, (artifact_id, ""))
        filename = str(artifact.get("filename", ""))
        size = _format_size(artifact.get("size"))
        e = html.escape
        parts = [
            f'<article class="download-card" data-artifact-id="{e(artifact_id)}">',
            f"<h3>{e(name)}</h3>",
        ]
        if detail:
            parts.append(f'<p class="download-detail">{e(detail)}</p>')
        parts.append(
            f'<a class="button primary download-button" href="{DOWNLOAD_ORIGIN}/download/{e(channel)}/{e(artifact_id)}"'
            f' aria-label="Download {e(name)}: {e(filename)}">Download</a>'
        )
        parts.append(f'<p class="download-meta">{e(f"{filename} · {size}" if size else filename)}</p>')
        if artifact.get("sha256"):
            parts.append(
                '<details class="download-checksum"><summary>SHA-256</summary>'
                f"<code>{e(str(artifact['sha256']))}</code></details>"
            )
        parts.append("</article>")
        cards.append("".join(parts))
    return "\n        ".join(cards)


LOADING_ROW = '<p class="download-loading" id="download-loading">Loading the latest release…</p>'
RELEASE_META = '<div class="release-meta cluster" id="release-meta" aria-live="polite"></div>'


def prerender_download(page: str, manifest: dict) -> str:
    version = html.escape(str(manifest["version"]))
    if LOADING_ROW not in page or RELEASE_META not in page:
        raise BuildError(
            "website/download/index.html no longer has the loading row and release-meta the pre-render fills"
        )
    page = page.replace(LOADING_ROW, render_download_cards(manifest))
    meta = (
        '<div class="release-meta cluster" id="release-meta" aria-live="polite">'
        f"<span>Version {version}</span>"
        f'<span><a class="text-link" href="{GITHUB_URL}/releases/tag/v{version}">Release notes</a></span></div>'
    )
    return page.replace(RELEASE_META, meta)


JSON_LD_RE = re.compile(r'(<script type="application/ld\+json">)(.*?)(</script>)', re.S)


def set_software_version(page: str, version: str) -> str:
    """Adds `softwareVersion` to the page's JSON-LD SoftwareApplication node."""
    return _update_software_node(page, softwareVersion=version)


def _update_software_node(page: str, **fields: str) -> str:
    def fill(match: re.Match[str]) -> str:
        data = json.loads(match[2])
        nodes = data.get("@graph", [data])
        for node in nodes:
            if node.get("@type") == "SoftwareApplication":
                node.update(fields)
        body = json.dumps(data, indent=2, ensure_ascii=False).replace("</", "<\\/")
        return f"{match[1]}\n{body}\n  {match[3]}"

    return JSON_LD_RE.sub(fill, page)


# ---- Docs export ------------------------------------------------------------------------------

MD_LINK_RE = re.compile(r"(?<!!)\[(?P<text>(?:[^\[\]]|\[[^\]]*\])*)\]\((?P<target>[^)\s]+)\)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class NavSection:
    title: str
    docs: list[str] = field(default_factory=list)  # stems, e.g. "getting-started"


def published_half(readme: str) -> str:
    """The "User and operator docs" half of docs/README.md, without its own heading."""
    match = re.search(r"^## User and operator docs\n(?P<body>.*?)^## ", readme, flags=re.M | re.S)
    if not match:
        raise BuildError("docs/README.md has no '## User and operator docs' section followed by another '## ' section")
    return match["body"]


def published_nav(readme: str) -> list[NavSection]:
    """Sections (`### ` headings) and the docs each links, in order, from the published half."""
    sections: list[NavSection] = []
    seen: set[str] = set()
    for line in published_half(readme).splitlines():
        if line.startswith("### "):
            sections.append(NavSection(line[4:].strip()))
            continue
        for match in MD_LINK_RE.finditer(line):
            target = match["target"].split("#", 1)[0]
            if "/" in target or not target.endswith(".md") or target in seen:
                continue
            if not sections:
                raise BuildError(f"docs/README.md links {target} before the first '### ' section")
            seen.add(target)
            sections[-1].docs.append(target[:-3])
    if not seen:
        raise BuildError("docs/README.md's published half links no docs")
    return sections


def doc_title(markdown: str, fallback: str) -> str:
    match = re.search(r"^# (.+)$", markdown, flags=re.M)
    return _plain(match[1]) if match else fallback


def _plain(markdown: str) -> str:
    text = MD_LINK_RE.sub(lambda m: m["text"], markdown)
    text = re.sub(r"`|\*\*|__", "", text)
    return re.sub(r"\s+", " ", text).strip()


def readme_descriptions(readme: str) -> dict[str, str]:
    """The one-line description docs/README.md's published half gives each doc (the text after
    the dash in its bullet), keyed by stem. A bullet listing several docs describes each of them;
    a doc whose bullet has no description is left out."""
    bullets = re.split(r"^- ", published_half(readme), flags=re.M)[1:]
    descriptions = {}
    for bullet in bullets:
        bullet = bullet.split("\n\n", 1)[0]
        _, dash, description = re.sub(r"\s+", " ", bullet).partition(" — ")
        if not dash:
            continue
        description = _plain(description)
        for match in MD_LINK_RE.finditer(bullet):
            target = match["target"].split("#", 1)[0]
            if "/" not in target and target.endswith(".md"):
                descriptions[target[:-3]] = description
    return descriptions


def _curly_quotes(text: str) -> str:
    """Straight double quotes as typographic ones. A description ends up in a `content="..."`
    attribute the generator writes without escaping, where a straight quote would end it."""
    parts = text.split('"')
    return "".join(part + ("“" if i % 2 == 0 else "”") for i, part in enumerate(parts[:-1])) + parts[-1]


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}…"


def doc_description(markdown: str, limit: int = 200) -> str:
    """The first prose paragraph after the title, as plain text, cut at a word near `limit`."""
    body = re.split(r"^# .+$", markdown, maxsplit=1, flags=re.M)[-1]
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block or block[0] in "#|>-*`<!" or re.match(r"\d+\. ", block):
            continue
        return _shorten(_plain(block), limit)
    return ""


def content_group(stem: str) -> str:
    if stem.startswith("install-"):
        return "platform"
    if stem in CONNECTOR_GUIDES:
        return "connector"
    return "docs"


def rewrite_links(markdown: str, resolve: Callable[[str], str]) -> str:
    """Applies `resolve` to every inline Markdown link target outside fenced code blocks."""
    out = []
    in_fence = False
    for line in markdown.splitlines(keepends=True):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence:
            line = MD_LINK_RE.sub(lambda m: f"[{m['text']}]({resolve(m['target'])})", line)
        out.append(line)
    return "".join(out)


class LinkResolver:
    """Resolves one doc's link targets: a link into the published set stays a doc link (relative
    for the generator, absolute for llms-full.txt); a link to anything else in the repository
    becomes a GitHub URL at the source's ref, and must exist there."""

    def __init__(self, source: DocsSource, published: set[str], doc_dir: str = "docs"):
        self.source = source
        self.published = published
        self.doc_dir = doc_dir

    def __call__(self, target: str, *, absolute: bool = False) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.I) or target.startswith("#"):
            return target  # an external URL, mailto:, or an anchor on the same page
        path, _, fragment = target.partition("#")
        anchor = f"#{fragment}" if fragment else ""
        resolved = posixpath.normpath(posixpath.join(self.doc_dir, unquote(path)))
        if resolved.startswith("../") or resolved == "..":
            raise BuildError(f"link {target!r} leaves the repository")
        if resolved.startswith("docs/") and "/" not in resolved[5:] and resolved[5:-3] in self.published:
            stem = resolved[5:-3]
            if absolute:
                return f"{SITE_URL}/docs/{stem}/{anchor}"
            return f"{stem}.md{anchor}"
        kind = self.source.kind(resolved)
        if kind is None:
            raise BuildError(f"link {target!r} points at {resolved}, which does not exist at {self.source.blob_ref}")
        return f"{GITHUB_URL}/{kind}/{self.source.blob_ref}/{resolved}{anchor}"


@dataclass
class DocsExport:
    nav: list[NavSection]
    titles: dict[str, str]
    descriptions: dict[str, str]
    markdown: dict[str, str]  # stem -> exported Markdown, links rewritten for the generator
    absolute: dict[str, str]  # stem -> Markdown with absolute links, for llms-full.txt
    index: str

    @property
    def stems(self) -> list[str]:
        return [stem for section in self.nav for stem in section.docs]


def export_docs(source: DocsSource, version_label: str) -> DocsExport:
    readme = source.read("docs/README.md")
    if readme is None:
        raise BuildError(f"docs/README.md does not exist at {source.blob_ref}")
    nav = published_nav(readme)
    stems = [stem for section in nav for stem in section.docs]
    published = set(stems)
    raw: dict[str, str] = {}
    for stem in stems:
        text = source.read(f"docs/{stem}.md")
        if text is None:
            raise BuildError(f"docs/README.md publishes {stem}.md, which does not exist at {source.blob_ref}")
        raw[stem] = text
    # Guardrail 5 fails a pull request that leaves a doc unclassified; at a tag, say so rather than
    # fail the deploy, since the doc is simply not published.
    unclassified = sorted(set(source.list_docs()) - {f"{stem}.md" for stem in stems} - CONTRIBUTOR_DOCS)
    if unclassified:
        warn(f"docs at {source.blob_ref} in neither half of docs/README.md are not published: {unclassified}")
    resolver = LinkResolver(source, published)
    titles = {stem: doc_title(text, stem) for stem, text in raw.items()}
    # A page's description is its index line ("Configuration reference: every settings.yaml
    # key ..."), or its opening paragraph where the index gives it none or only repeats the title.
    from_readme = readme_descriptions(readme)
    descriptions = {}
    for stem, text in raw.items():
        line = from_readme.get(stem, "")
        words = re.findall(r"\w+", line.lower())
        repeats_title = sum(word in titles[stem].lower() for word in words) >= 0.6 * len(words)
        if line and not repeats_title:
            descriptions[stem] = _shorten(f"{titles[stem]}: {line}", 200)
        else:
            descriptions[stem] = doc_description(text)
        descriptions[stem] = _curly_quotes(descriptions[stem])
    markdown = {stem: rewrite_links(text, resolver) for stem, text in raw.items()}
    absolute = {stem: rewrite_links(text, lambda t: resolver(t, absolute=True)) for stem, text in raw.items()}

    # The /docs/ landing page is docs/README.md's published half: its sections and one-line
    # descriptions, with file names shown as the docs' titles.
    def titled(match: re.Match[str]) -> str:
        target = resolver(match["target"])
        stem = target.split("#", 1)[0][:-3] if target.endswith(".md") or ".md#" in target else None
        text = titles[stem] if stem in titles and re.fullmatch(r"`[\w.-]+\.md`", match["text"]) else match["text"]
        return f"[{text}]({target})"

    body = MD_LINK_RE.sub(titled, published_half(readme))
    body = re.sub(r"^### ", "## ", body, flags=re.M)
    index = (
        "# PrivacyFence documentation\n\n"
        f"How to install, use, deploy and secure PrivacyFence. These pages describe {version_label}.\n"
        f"{body}"
    )
    return DocsExport(nav, titles, descriptions, markdown, absolute, index)


INDEX_DESCRIPTION = (
    "PrivacyFence documentation: install on macOS, Windows or Linux, connect AI assistants and "
    "services, approvals and policy, organization deployment, security, and reference."
)


def _front_matter(**values: str) -> str:
    return (
        "---\n"
        + "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items())
        + "---\n\n"
    )


def write_docs_project(
    export: DocsExport, workdir: Path, version: str, version_label: str, changelog_ref: str = "main"
) -> Path:
    """Writes the generator project (Markdown, theme, config) into `workdir`; returns its config."""
    docs_dir = workdir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "index.md").write_text(
        _front_matter(description=INDEX_DESCRIPTION, pf_content_group="docs") + export.index, encoding="utf-8"
    )
    for stem in export.stems:
        front = _front_matter(
            description=export.descriptions[stem] or export.titles[stem], pf_content_group=content_group(stem)
        )
        (docs_dir / f"{stem}.md").write_text(front + export.markdown[stem], encoding="utf-8")

    # The project icon in the docs bar, in place of the generator's default book icon.
    (docs_dir / "assets").mkdir()
    shutil.copyfile(REPO / "src" / "privacyfence" / "resources" / "icon_512.png", docs_dir / "assets" / "logo.png")

    overrides = workdir / "overrides"
    shutil.copytree(WEBSITE / "_docs" / "overrides", overrides)
    partials = overrides / "partials"
    partials.mkdir(exist_ok=True)
    (partials / "pf-site-header.html").write_text(render_partial("header", skip_target="#pf-content"), encoding="utf-8")
    (partials / "pf-site-footer.html").write_text(render_partial("footer"), encoding="utf-8")

    config = {
        "site_name": "PrivacyFence documentation",
        "site_url": f"{SITE_URL}/docs/",
        "site_description": INDEX_DESCRIPTION,
        "docs_dir": "docs",
        "site_dir": "site",
        "strict": True,
        "use_directory_urls": True,
        "nav": [{"Overview": "index.md"}]
        + [{section.title: [f"{stem}.md" for stem in section.docs]} for section in export.nav if section.docs],
        "theme": {
            "name": "material",
            "custom_dir": "overrides",
            # Self-hosted only (guardrail 13): no Google Fonts, no GitHub API calls for repo stats.
            "font": False,
            "favicon": "/assets/icon.png",
            "logo": "assets/logo.png",
            "language": "en",
            "features": ["navigation.sections", "navigation.footer", "toc.follow", "search.highlight"],
        },
        "extra_css": ["/tokens.css", "/chrome.css", "/docs-theme.css"],
        "extra": {
            "pf_version": version,
            "pf_version_label": version_label,
            "pf_changelog_url": f"{GITHUB_URL}/blob/{changelog_ref}/CHANGELOG.md",
            "generator": False,
        },
        "markdown_extensions": [
            "abbr",
            "admonition",
            "attr_list",
            "def_list",
            "footnotes",
            "md_in_html",
            "tables",
            {"toc": {"permalink": True}},
            "pymdownx.details",
            "pymdownx.superfences",
            "pymdownx.inlinehilite",
            {"pymdownx.highlight": {"anchor_linenums": False}},
        ],
    }
    config_file = workdir / "mkdocs.yml"
    # JSON is YAML, so the config needs no YAML library here.
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_file


def render_docs(config_file: Path, out_docs: Path) -> None:
    """Runs the generator in strict mode and moves its output to `out_docs`."""
    try:
        import zensical  # noqa: F401 -- only checking it is installed
    except ImportError as exc:
        raise BuildError(
            "rendering /docs/ needs the docs extra: pip install --require-hashes -r requirements/docs.lock.txt "
            "(or pass --no-docs)"
        ) from exc
    result = subprocess.run(  # nosec B603 -- our own interpreter, fixed arguments
        [sys.executable, "-m", "zensical", "build", "--config-file", str(config_file), "--clean", "--strict"],
        cwd=config_file.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
        raise BuildError(f"the docs generator failed in strict mode:\n{output}")
    built = config_file.parent / "site"
    # The site-wide sitemap.xml is generated below; the generator's own copy and 404 page would
    # only duplicate or contradict it.
    for name in ("sitemap.xml", "sitemap.xml.gz", "404.html"):
        (built / name).unlink(missing_ok=True)
    shutil.copytree(built, out_docs)


# ---- Generated files ---------------------------------------------------------------------------


def sitemap_xml(paths: list[str]) -> str:
    urls = "".join(f"  <url><loc>{html.escape(SITE_URL + path)}</loc></url>\n" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )


def canonical_description() -> list[str]:
    text = (WEBSITE / "canonical-description.md").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return [re.sub(r"\s+", " ", para).strip() for para in re.split(r"\n\s*\n", text) if para.strip()]


def llms_txt(export: DocsExport | None, version: str | None) -> str:
    first, *rest = canonical_description()
    lines = ["# PrivacyFence", "", f"> {first}", ""]
    lines += [para for para in rest for para in (para, "")]
    if version:
        lines += [f"The current stable release is {version}.", ""]
    lines += ["## Docs", ""]
    if export is not None:
        lines.append(f"- [Documentation overview]({SITE_URL}/docs/): {INDEX_DESCRIPTION}")
        for stem in export.stems:
            description = f": {export.descriptions[stem]}" if export.descriptions[stem] else ""
            lines.append(f"- [{export.titles[stem]}]({SITE_URL}/docs/{stem}/){description}")
    else:
        lines.append(
            f"- [Documentation on GitHub]({GITHUB_URL}/tree/main/docs): install, configuration, organization deployment and security"
        )
    lines += [
        "",
        "## Website",
        "",
        f"- [How it works]({SITE_URL}/how-it-works/): the request flow from AI client to connected service, with one read and one write approved",
        f"- [Security]({SITE_URL}/security/): the security model in brief, and what PrivacyFence does not claim",
        f"- [Enterprise]({SITE_URL}/enterprise/): local mode and organization deployment side by side, with prerequisites",
        f"- [Connectors]({SITE_URL}/connectors/): per connector, what an AI client can read, what is reviewed and which writes need approval",
        f"- [Download]({SITE_URL}/download/): installers for macOS, Windows and Linux, each with its SHA-256 checksum",
        f"- [Privacy policy]({SITE_URL}/privacy/): what this website and the download service do with visitor data",
        f"- [Imprint]({SITE_URL}/imprint/): who publishes privacyfence.eu",
        "",
        "## Source",
        "",
        f"- [GitHub repository]({GITHUB_URL}): source code, issues and releases, Apache License 2.0",
        "- [PyPI package](https://pypi.org/project/privacyfence/): `pip install privacyfence`",
        f"- [Security policy]({GITHUB_URL}/blob/main/SECURITY.md): how to report a vulnerability",
    ]
    if export is not None:
        lines += ["", "## Optional", "", f"- [All documentation in one file]({SITE_URL}/llms-full.txt)"]
    return "\n".join(lines) + "\n"


def llms_full_txt(export: DocsExport, version_label: str) -> str:
    parts = [f"# PrivacyFence documentation\n\nEvery published PrivacyFence document, for {version_label}.\n"]
    for stem in export.stems:
        parts.append(f"\n---\n\nSource: {SITE_URL}/docs/{stem}/\n\n{export.absolute[stem].strip()}\n")
    return "".join(parts)


# ---- Link check (guardrail 6) ------------------------------------------------------------------


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name in ("id", "name") if tag == "a" else ("id",):
            if values.get(name):
                self.ids.add(values[name] or "")
        for name in ("href", "src"):
            if values.get(name) and not (tag == "link" and values.get("rel") in {"canonical", "alternate"}):
                self.links.append(values[name] or "")


def _page_file(site: Path, url_path: str) -> Path | None:
    candidate = site / url_path.lstrip("/")
    if url_path.endswith("/") or candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


def check_links(site: Path) -> list[str]:
    """Every internal href/src in every page of `site` resolves to a file, and every fragment to
    an id on the target page. Returns the broken ones, as "page -> link"."""
    parsed: dict[Path, _LinkCollector] = {}

    def parse(page: Path) -> _LinkCollector:
        if page not in parsed:
            collector = _LinkCollector()
            collector.feed(page.read_text(encoding="utf-8"))
            parsed[page] = collector
        return parsed[page]

    broken = []
    for page in sorted(site.rglob("*.html")):
        page_url = "/" + page.relative_to(site).as_posix()
        for link in parse(page).links:
            parts = urlsplit(link)
            if parts.scheme in {"mailto", "tel", "javascript", "data"}:
                continue
            if parts.scheme or parts.netloc:
                if parts.netloc != urlsplit(SITE_URL).netloc:
                    continue  # external: not ours to check offline
                path = parts.path or "/"
            else:
                path = posixpath.join(posixpath.dirname(page_url), parts.path) if parts.path else page_url
                path = posixpath.normpath(path) + ("/" if parts.path.endswith("/") and path != "/" else "")
            target = _page_file(site, unquote(path)) if path != page_url else page
            if target is None:
                broken.append(f"{page_url} -> {link}")
                continue
            fragment = unquote(parts.fragment)
            if (
                fragment
                and target.suffix == ".html"
                and not fragment.startswith("__")
                and fragment not in parse(target).ids
            ):
                broken.append(f"{page_url} -> {link} (no id {fragment!r})")
    return broken


# ---- The build ---------------------------------------------------------------------------------

_ASSET_REF = re.compile(r'(?P<attr>\b(?:src|href)=")(?P<path>/[^"?#]+\.(?:css|js))(?=")')
_DOCS_LINK = re.compile(r'(?P<attr>\bhref=")/docs/(?P<stem>[^"#?/]*)/?(?P<frag>#[^"]*)?"')


def fingerprint_assets(site: Path) -> None:
    """Appends `?v=<content hash>` to every root-relative .css/.js reference in the built HTML.

    GitHub Pages and the Cloudflare proxy cache these files for a while under a fixed URL, so a
    deploy that changes a page and its script together could otherwise serve the new HTML with
    the old script: /download/ once showed every card twice, because a cached pre-Wave-3
    download.js appended the live cards to the build's pre-rendered ones instead of replacing
    them. A content hash changes the URL exactly when the file changes."""
    hashes: dict[str, str] = {}

    def versioned(match: re.Match[str]) -> str:
        path = match["path"]
        if path not in hashes:
            file = site / path.lstrip("/")
            if not file.is_file():
                return match[0]
            hashes[path] = hashlib.sha256(file.read_bytes()).hexdigest()[:12]
        return f"{match['attr']}{path}?v={hashes[path]}"

    for page in site.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        updated = _ASSET_REF.sub(versioned, text)
        if updated != text:
            page.write_text(updated, encoding="utf-8")


def point_docs_links_at_github(site: Path, ref: str) -> None:
    """When /docs/ is not built (stale tag, --no-docs), links to it from the marketing pages go to
    the docs on GitHub at `ref` instead, so the site has no dead link and the link walker passes.
    The caller passes `main`, as llms.txt does: a stale tag may not have the page linked at all."""

    def github(match: re.Match[str]) -> str:
        stem, frag = match["stem"], match["frag"] or ""
        target = f"{GITHUB_URL}/blob/{ref}/docs/{stem}.md{frag}" if stem else f"{GITHUB_URL}/tree/{ref}/docs"
        return f'{match["attr"]}{target}"'

    for page in site.rglob("*.html"):
        if page.is_relative_to(site / "docs"):
            continue
        text = page.read_text(encoding="utf-8")
        updated = _DOCS_LINK.sub(github, text)
        if updated != text:
            page.write_text(updated, encoding="utf-8")


@dataclass
class BuildReport:
    out: str
    pages: list[str]
    docs: str  # "published", "skipped: stale tag", "skipped: --no-docs", "skipped: no stable tag"
    docs_ref: str | None
    docs_pages: list[str]
    download_version: str | None
    layout_sample: list[str]


def check_manifest() -> None:
    """Refuses a manifest that would publish a file it also says stays private."""
    published = set(PAGES.values()) | {s.removeprefix("website/") for s in STATIC.values() if s.startswith("website/")}
    leaked = sorted(published & (REPOSITORY_ONLY | BUILD_INPUTS))
    if leaked:
        raise BuildError(f"the build manifest publishes files it lists as repository-only or build inputs: {leaked}")


def build(
    out: Path,
    *,
    docs_ref: str | None = "latest",
    render: bool = True,
    manifest: dict | None = None,
    fetch_manifest: bool = True,
) -> BuildReport:
    """Builds the whole site into `out` (replacing it). `docs_ref` is a git ref, "latest" (the
    newest stable tag), "worktree", or None for no docs."""
    check_manifest()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for dest, source in STATIC.items():
        target = out / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / source, target)
    (out / ".nojekyll").touch()

    if manifest is None and fetch_manifest:
        manifest = fetch_stable_manifest()
    version = str(manifest["version"]) if manifest else None

    for url_path, source in PAGES.items():
        page = set_clients(assemble_page((WEBSITE / source).read_text(encoding="utf-8")))
        if manifest:
            if url_path == "/download/":
                page = prerender_download(page, manifest)
            page = set_software_version(page, str(manifest["version"]))
        target = out / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")

    export: DocsExport | None = None
    docs_state = "skipped: --no-docs"
    source: DocsSource | None = None
    docs_version = None
    if docs_ref is not None:
        if docs_ref == "latest":
            tag = latest_stable_tag()
            source = DocsSource(tag) if tag else None
            if source is None:
                docs_state = "skipped: no stable tag"
                warn("no stable release tag found; /docs/ is not built")
        elif docs_ref == "worktree":
            source = DocsSource(None)
        else:
            try:
                _git("rev-parse", "--verify", "--quiet", f"{docs_ref}^{{commit}}")
            except subprocess.CalledProcessError as exc:
                raise BuildError(f"--docs-ref {docs_ref!r} is not a commit, tag or branch in this checkout") from exc
            source = DocsSource(docs_ref)
    if source is not None:
        if source.read(PUBLISHED_SET_MARKER) is None:
            docs_state = "skipped: stale tag"
            warn(
                f"{source.blob_ref} predates the published doc set (it has no {PUBLISHED_SET_MARKER}); "
                "/docs/ and llms-full.txt are not built, and llms.txt links the docs on GitHub. "
                "The first stable release that carries the published set turns /docs/ on."
            )
        else:
            docs_version = (
                source.ref.lstrip("v") if source.ref and re.fullmatch(r"v\d+\.\d+\.\d+", source.ref) else None
            )
            label = f"PrivacyFence {docs_version}" if docs_version else f"PrivacyFence as of {source.blob_ref}"
            export = export_docs(source, label)
            if render:
                with tempfile.TemporaryDirectory(prefix="pf-docs-") as tmp:
                    config = write_docs_project(
                        export, Path(tmp), docs_version or source.blob_ref, label, source.blob_ref
                    )
                    render_docs(config, out / "docs")
            (out / "llms-full.txt").write_text(llms_full_txt(export, label), encoding="utf-8")
            docs_state = "published"

    docs_pages = ["/docs/", *(f"/docs/{stem}/" for stem in export.stems)] if export and render else []
    if not docs_pages:
        point_docs_links_at_github(out, "main")
    fingerprint_assets(out)
    pages = list(PAGES)
    (out / "sitemap.xml").write_text(sitemap_xml(pages + docs_pages), encoding="utf-8")
    (out / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8")
    (out / "llms.txt").write_text(llms_txt(export, version or docs_version), encoding="utf-8")

    broken = check_links(out)
    if broken:
        raise BuildError("broken internal links in the built site:\n  " + "\n  ".join(broken))

    return BuildReport(
        out=str(out),
        pages=pages,
        docs=docs_state,
        docs_ref=source.blob_ref if source else None,
        docs_pages=docs_pages,
        download_version=version,
        layout_sample=pages + [p for p in DOCS_LAYOUT_SAMPLE if p in docs_pages],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--out", type=Path, default=REPO / "_site", help="output directory (replaced)")
    docs = parser.add_mutually_exclusive_group()
    docs.add_argument(
        "--docs-ref",
        default="latest",
        help='git ref to publish the docs from, or "worktree" for the checkout as it is (default: the newest stable tag)',
    )
    docs.add_argument("--no-docs", action="store_true", help="do not build /docs/")
    release = parser.add_mutually_exclusive_group()
    release.add_argument("--offline", action="store_true", help="do not fetch the release manifest")
    release.add_argument("--release-manifest", type=Path, help="pre-render /download/ from this manifest file")
    parser.add_argument("--report", type=Path, help="write a JSON summary of the build here")
    args = parser.parse_args(argv)

    manifest = json.loads(args.release_manifest.read_text(encoding="utf-8")) if args.release_manifest else None
    try:
        report = build(
            args.out.resolve(),
            docs_ref=None if args.no_docs else args.docs_ref,
            manifest=manifest,
            fetch_manifest=not args.offline,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    summary = json.dumps(report.__dict__, indent=2)
    if args.report:
        args.report.write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
