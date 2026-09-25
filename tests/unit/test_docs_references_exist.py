"""Every `docs/...md` path named anywhere in the repository exists.

`tests/unit/test_docs_links.py` checks Markdown links. It cannot see the other place docs get
cited: code comments, test skip reasons, workflow comments, packaging scripts. A documentation
audit found those citing documents that had been deleted months earlier, so a reader following a
comment to its explanation found nothing. Deleting or renaming a doc now fails here until every
mention is updated in the same pull request.

A deliberate citation of a deleted document stays possible, in the one form that can still be
followed: a git revision spec, `<commit>^:docs/<name>.md`, which `git show` resolves. When the
checkout has its git history, the test also checks that each such citation resolves.

Two places are exempt. `CHANGELOG.md` records what each release shipped, including the docs it
had then. `docs/adr/` records are frozen once accepted; their Markdown links are still checked by
`test_docs_links.py`, and fixing one is a permitted edit. This file is skipped too: its
examples name files that do not exist on purpose.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404  # fixed argv, no shell: lists and reads this repo's own git objects
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# A path under docs/ ending in .md, optionally prefixed by a git revision (`be78e7ee^:`).
_REF = re.compile(r"(?:\b([0-9a-f]{7,40}\^?):)?(?<![\w-])(docs/[A-Za-z0-9_./-]+?\.md)\b")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "htmlcov", "_site"}
_EXEMPT = ("CHANGELOG.md", "docs/adr/", "tests/unit/test_docs_references_exist.py")
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".pdf", ".woff", ".woff2", ".zip", ".db"}


def _git() -> str | None:
    if not (REPO_ROOT / ".git").exists():
        return None
    return shutil.which("git")


def _files() -> list[str]:
    git = _git()
    if git:
        out = subprocess.run(  # nosec B603  # fixed argv, see the import
            [git, "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
        ).stdout.decode("utf-8")
        return [p for p in out.split("\0") if p]
    return [
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    ]


def _references() -> list[tuple[str, int, str | None, str]]:
    refs = []
    for rel in _files():
        if rel.startswith(_EXEMPT) or Path(rel).suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _REF.finditer(line):
                refs.append((rel, lineno, match.group(1), match.group(2)))
    return refs


def test_every_current_docs_reference_exists():
    dangling = [
        f"{rel}:{lineno}: {path}"
        for rel, lineno, rev, path in _references()
        if rev is None and not (REPO_ROOT / path).is_file()
    ]
    assert not dangling, (
        "These name a docs/ file that does not exist. Point them at the doc that holds the content "
        "now, or cite the old file as `<commit>^:docs/<name>.md`:\n" + "\n".join(dangling)
    )


def test_every_historical_docs_citation_resolves():
    git = _git()
    if git is None:
        pytest.skip("no git history in this checkout")
    unresolved = []
    for rel, lineno, rev, path in _references():
        if rev is None:
            continue
        result = subprocess.run(  # nosec B603  # fixed argv, see the import
            [git, "cat-file", "-e", f"{rev}:{path}"], cwd=REPO_ROOT, capture_output=True
        )
        if result.returncode != 0:
            unresolved.append(f"{rel}:{lineno}: {rev}:{path}")
    if unresolved and (REPO_ROOT / ".git" / "shallow").exists():
        pytest.skip("shallow clone: historical commits are not available")
    assert not unresolved, "These `git show` citations do not resolve:\n" + "\n".join(unresolved)


def test_the_pattern_reads_both_forms():
    current = _REF.search("see docs/getting-started.md for the install")
    assert current is not None and current.group(1) is None
    assert current.group(2) == "docs/getting-started.md"
    historical = _REF.search("`git show be78e7ee^:docs/windows-support-plan.md` Phase 6.3")
    assert historical is not None
    assert historical.groups() == ("be78e7ee^", "docs/windows-support-plan.md")
    assert _REF.search("mydocs/x.md") is None
    linked = _REF.search("(https://github.com/privacyfence/privacyfence/blob/main/docs/how-it-works.md)")
    assert linked is not None and linked.group(2) == "docs/how-it-works.md"
