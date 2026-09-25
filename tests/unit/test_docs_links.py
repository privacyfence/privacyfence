"""Every relative Markdown link in this repo resolves -- file *and* heading anchor.

Three separate code-review rounds each found a different set of dead cross-references that a hand
audit had missed: ~300 citations to four deleted plan documents, nine README/`pii-detection-
keywords.md` anchors pointing at headings that had been renamed, and a single anchor that was one
hyphen short of the real slug. Each was fixed by hand, and the next round found another. This test
is the mechanism that was missing -- a broken link now fails a PR in under a second instead of
waiting for someone to read the doc and click.

Scope: relative links, plus the absolute GitHub URLs that point back into *this* repo. External
`http(s)://` URLs are deliberately not fetched (no network in this tier, and a third party's uptime
is not this suite's business), and `mailto:` is skipped for the same reason -- but a
`github.com/privacyfence/privacyfence/blob/main/<path>` or
`raw.githubusercontent.com/privacyfence/privacyfence/main/<path>` URL names a file in the working
tree, so it is resolved against the checkout exactly like a relative link would be. README.md is
the reason that matters: it is also the PyPI long description, so its links and images have to be
absolute to render there (privacyfence/privacyfence#370), and without this they would have dropped
out of the check above the moment they stopped being relative.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `[text](target)`, with an optional `"title"` after the target. Deliberately not a Markdown
# parser: this only needs to find link targets, and the pattern that matters (a path, optionally
# followed by `#anchor`) is unambiguous enough that a parser would buy nothing.
_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "htmlcov"}

# GitHub renders `../../releases` and friends from a repo-root README as links to the repository's
# own tabs (releases, issues, ...) -- they are not filesystem paths and resolve above the repo
# root, so they are correct-as-written rather than broken.
_GITHUB_REPO_TABS = {"releases", "issues", "pulls", "wiki", "actions", "security", "tags"}

# The two absolute forms a link into this repo's own tree can take: GitHub's rendered-file view and
# the raw-content host images have to use. Everything after one of these prefixes is a repo-root-
# relative path (plus, for `blob/`, an optional `#anchor`).
_SELF_URL_PREFIXES = (
    "https://github.com/privacyfence/privacyfence/blob/main/",
    "https://raw.githubusercontent.com/privacyfence/privacyfence/main/",
)

# `<img src="...">`, the HTML form README.md uses for the screenshots (Markdown's own `![]()` has
# no way to set `width`). Only the absolute-URL check below looks at these.
_IMG = re.compile(r"<img\s[^>]*?src=\"([^\"]+)\"")


def _markdown_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.md")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


def _slug(heading: str) -> str:
    """GitHub's heading-anchor slug.

    Lowercase, drop everything that is not a word character, space or hyphen, then turn each
    remaining space into a hyphen. The subtlety that produced a real bug: a character GitHub drops
    (`/`, `.`) leaves its *surrounding spaces* behind, so `--check / --record` collapses to four
    hyphens between the words, not three.
    """
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # inline code renders as its contents
    text = text.replace("*", "")  # emphasis markers; `_` is NOT stripped -- GitHub keeps
    text = re.sub(r"[^\w\s-]", "", text)  # underscores in identifiers (qa_fixture_recorder.py)
    return text.strip().lower().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def _relative_links() -> list[tuple[Path, int, str]]:
    found = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in _LINK.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            found.append((path, text[: match.start()].count("\n") + 1, target))
    return found


def _self_links() -> list[tuple[Path, int, str]]:
    """Absolute URLs that point back into this repo's own tree -- Markdown links and `<img>` tags
    alike, since README.md's screenshots are the latter."""
    found = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for pattern in (_LINK, _IMG):
            for match in pattern.finditer(text):
                url = match.group(1)
                if url.startswith(_SELF_URL_PREFIXES):
                    found.append((path, text[: match.start()].count("\n") + 1, url))
    return found


_LINKS = _relative_links()
_SELF_LINKS = _self_links()


def test_the_scan_actually_found_links():
    """Guards the test itself: a regex or walk that silently matches nothing would make every
    assertion below vacuously pass, which is the failure mode a link checker can least afford."""
    assert len(_LINKS) > 50, f"only {len(_LINKS)} relative links found -- the scan is probably broken"


@pytest.mark.parametrize("path, line, target", _LINKS, ids=lambda v: str(v) if isinstance(v, str) else "")
def test_relative_link_resolves(path: Path, line: int, target: str):
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    file_part, _, anchor = target.partition("#")
    if not file_part:
        return

    resolved = (path.parent / file_part).resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        # A GitHub repo-tab shortcut (`../../releases`), not a path into the tree.
        assert resolved.name in _GITHUB_REPO_TABS, f"{where}: link escapes the repo and is not a GitHub tab: {target}"
        return

    _assert_resolves(where, resolved, anchor, target)


def _assert_resolves(where: str, resolved: Path, anchor: str, target: str) -> None:
    assert resolved.exists(), f"{where}: link target does not exist: {target}"

    if anchor and resolved.suffix == ".md":
        available = _anchors(resolved)
        assert anchor.lower() in available, (
            f"{where}: no heading in {resolved.relative_to(REPO_ROOT)} produces the anchor "
            f"#{anchor} (that file's headings slugify to, among others: "
            f"{sorted(a for a in available if a[:3] == anchor.lower()[:3]) or sorted(available)[:5]})"
        )


def test_the_self_url_scan_actually_found_links():
    """Same guard as above, for the absolute-URL scan. README.md links its docs on privacyfence.eu
    (tests/unit/test_readme_site_links.py checks those), so what is left here is its images and its
    links to root files (LICENSE, NOTICE, SECURITY.md, ...): none at all would mean the prefixes or
    the `<img>` pattern stopped matching."""
    assert len(_SELF_LINKS) >= 5, f"only {len(_SELF_LINKS)} self-referencing absolute URLs found -- scan is probably broken"


@pytest.mark.parametrize("path, line, url", _SELF_LINKS, ids=lambda v: str(v) if isinstance(v, str) else "")
def test_absolute_self_url_resolves(path: Path, line: int, url: str):
    """A `blob/main/...` or `raw.githubusercontent.com/.../main/...` URL is a path into this
    checkout wearing a hostname. Resolve it as one -- a doc renamed without updating README.md
    would otherwise only 404 for whoever clicked it on PyPI."""
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    prefix = next(p for p in _SELF_URL_PREFIXES if url.startswith(p))
    file_part, _, anchor = url[len(prefix) :].partition("#")

    assert file_part, f"{where}: self-referencing URL names no path: {url}"
    resolved = (REPO_ROOT / file_part).resolve()
    assert resolved.is_relative_to(REPO_ROOT), f"{where}: self-referencing URL escapes the repo: {url}"
    _assert_resolves(where, resolved, anchor, url)
