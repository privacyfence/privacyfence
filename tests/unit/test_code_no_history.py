"""The code carries no project history: no phase names, plan item IDs, finding IDs or bare issue numbers.

`test_docs_no_history.py` keeps the docs free of history. This is the same rule for everything else
in the tree: source, tests, scripts, workflows, installers and packaging. A comment tagged
`#428 Phase 4`, `P9`, `SEC-23`, `F5` or `§10.6` points at a plan or review that has been deleted,
and a new reader cannot tell a phase name from a feature name or a closed issue from an open one.
A comment says why in its own words. When the why is a decision, it names the ADR. When it points
at work that is still open, it gives the issue's full URL and describes the limitation in words.

The patterns match the shapes these tags actually took. A match is exempt when it cites something
that still exists: a section or decision of a named ADR (`ADR 0003 §5a`, `ADR 0008 D3`), an RFC, the
Debian policy, or a named Markdown document. Anything else that is not history goes in `_ALLOWED`
with its reason, rather than making a pattern looser.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404  # runs a fixed `git ls-files` argv only
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PENDING_DIR = Path(__file__).resolve().parent / "code_history_pending"

# docs/ has its own guard, and ADRs are history by design. The changelog and recorded fixtures are
# history and third-party data respectively. The two guards quote the shapes as their own samples.
_EXEMPT = (
    "docs/",
    "CHANGELOG.md",
    "tests/fixtures/",
    "tests/unit/test_code_no_history.py",
    "tests/unit/test_docs_no_history.py",
)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "htmlcov", "_site"}
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".pdf", ".woff", ".woff2", ".zip", ".db"}
_LOCKFILES = ("package-lock.json",)

_PATTERNS = {
    "phase name": re.compile(r"\b[Pp]hase \d"),
    "phase ID": re.compile(r"\bP\d{1,2}(?:\.\d+)?\b"),
    "issue/PR number": re.compile(r"(?:\b[\w.-]+/[\w.-]+#|(?<![\w&#])#)\d{2,4}\b(?!-)"),
    "finding ID": re.compile(r"\b(?:SEC|TST)-\d+"),
    "section reference": re.compile(r"§\s?\d+(?:\.\d+)*"),
    # Not inside a short string literal, so data such as a Slack file ID "F1" does not count.
    "plan item ID": re.compile(r"(?<![\"'\w])[BDFW]\d{1,2}\b(?![\"'])"),
    "wave": re.compile(r"\bWave \d"),
    "version-qualified history": re.compile(r"\b(?:as of|since|through|until) v?\d+\.\d", re.IGNORECASE),
}

# What may precede a section reference or plan item ID when it cites something that still exists.
_CITATION = re.compile(
    r"(?:ADR \d{4}(?:'s)?(?: decision)?|RFC ?\d+|WebAuthn(?: L\d)?|Debian policy|[\w./-]+\.md`?(?:'s)?)[,:]?\s*$"
)
_CITABLE = {"section reference", "plan item ID"}

# (path prefix, exact matched text) pairs that are not history, each with its reason. Keep it short.
_ALLOWED: dict[tuple[str, str], str] = {
    ("cloudflare/", "D1"): "Cloudflare D1 is the Worker's database product, not a plan item",
    (".github/workflows/deploy-download-worker.yml", "D1"): "Cloudflare D1, the Worker's database",
    ("website/", "D1"): "Cloudflare D1, the Worker's database",
    ("CLAUDE.md", "D1"): "Cloudflare D1, the Worker's database",
    ("privilege_separation.py", "since 4.3"): "4.3BSD, the Unix release, not a PrivacyFence version",
    ("web/routes_connect.py", "#555"): "a CSS hex colour in the connect page's stylesheet",
    ("web/routes_connect.py", "#888"): "a CSS hex colour in the connect page's stylesheet",
    ("", "§2.7"): "the definition of done in docs/coding-and-testing-guidelines.md, cited by number everywhere",
}

# Embedded base64 (fonts in the approval window's stylesheet) is data that spells anything.
_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")


def _files() -> list[str]:
    git = shutil.which("git") if (REPO_ROOT / ".git").exists() else None
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


def _allowed(rel: str, text: str) -> bool:
    return any(
        text == allowed_text and (rel.startswith(prefix) or rel.endswith(prefix)) for prefix, allowed_text in _ALLOWED
    )


def line_hits(rel: str, line: str) -> list[tuple[str, str]]:
    line = _DATA_URI.sub("data:", line)
    hits = []
    for label, pattern in _PATTERNS.items():
        for match in pattern.finditer(line):
            if label in _CITABLE and _CITATION.search(line[: match.start()]):
                continue
            if not _allowed(rel, match.group(0)):
                hits.append((label, match.group(0)))
    return hits


def _file_hits(rel: str) -> list[str]:
    try:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return []
    return [
        f"{rel}:{lineno}: {label} {found!r}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), 1)
        for label, found in line_hits(rel, line)
    ]


def _in_scope(rel: str) -> bool:
    return not (
        rel.startswith(_EXEMPT)
        or Path(rel).suffix.lower() in _BINARY_SUFFIXES
        or Path(rel).name in _LOCKFILES
        or rel.startswith("tests/unit/code_history_pending/")
    )


def _pending() -> dict[str, str]:
    """Paths still waiting for their tags to be rewritten, mapped to the list that names them."""
    pending: dict[str, str] = {}
    if PENDING_DIR.is_dir():
        for listing in sorted(PENDING_DIR.glob("*.txt")):
            for line in listing.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    pending.setdefault(line.strip(), listing.name)
    return pending


def test_code_carries_no_history():
    pending = _pending()
    hits = [hit for rel in _files() if _in_scope(rel) and rel not in pending for hit in _file_hits(rel)]
    assert not hits, (
        "Code comments, docstrings and strings say why in their own words; history belongs in "
        "CHANGELOG.md or an ADR, and an open issue is cited by its full URL (see "
        "tests/unit/test_code_no_history.py's docstring):\n" + "\n".join(hits)
    )


def test_pending_lists_name_each_existing_file_once():
    seen: dict[str, str] = {}
    problems = []
    if PENDING_DIR.is_dir():
        for listing in sorted(PENDING_DIR.glob("*.txt")):
            for line in listing.read_text(encoding="utf-8").splitlines():
                rel = line.strip()
                if not rel:
                    continue
                if rel in seen:
                    problems.append(f"{rel} is in both {seen[rel]} and {listing.name}")
                seen[rel] = listing.name
                if not (REPO_ROOT / rel).is_file():
                    problems.append(f"{listing.name} names {rel}, which does not exist")
    assert not problems, "\n".join(problems)


def test_the_patterns_catch_the_shapes_they_exist_for():
    samples = {
        "phase name": "added in Phase 4 of the plan",
        "phase ID": "retired by P9",
        "issue/PR number": "fixed in #428",
        "finding ID": "see SEC-09",
        "section reference": "per §10.6",
        "plan item ID": "# B9: turn step-up on",
        "wave": "found during Wave 1",
        "version-qualified history": "as of 4.2 the popup is gone",
    }
    for label, text in samples.items():
        assert (label, _PATTERNS[label].search(text).group(0)) in line_hits("src/x.py", text), label
    assert line_hits("src/x.py", "owner/repo#374") == [("issue/PR number", "owner/repo#374")]


def test_citations_and_data_are_not_history():
    for text in (
        "ADR 0008 D3 keeps the token under authority/",
        "ADR 0002 §5a",
        "RFC 6749 §3.3 requires it",
        "Debian policy §9.1.2",
        "coding-and-testing-guidelines.md §2.7",
        '"files": [{"id": "F1"}]',
        "Python 3.11 or later, macOS 13, port 8443",
        "https://github.com/privacyfence/privacyfence/issues/121",
        "color: #fff; background: #1a1a1a",
        "[the checklist](docs/coding-and-testing-guidelines.md#27-definition-of-done)",
        "authData flag bits (WebAuthn L2 §6.1)",
        "run the full §2.7 gate",
        "src: url(data:font/woff2;base64,d09GMgABAAAAP1W35B7)",
    ):
        assert line_hits("src/x.py", text) == [], text
