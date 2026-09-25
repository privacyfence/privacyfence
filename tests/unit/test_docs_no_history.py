"""The published docs describe the current version only -- no project history.

A documentation audit found roughly 150 passages across the user docs that described upgrades,
retired features or implementation phases: "as of 4.2", "P9 of the policy redesign", "(#428)",
"SEC-09". Each was accurate when written and misleading to a new reader, who cannot tell a phase
name from a feature name or a fixed bug from an open one. History has exactly two homes,
`CHANGELOG.md` (what changed, per release) and `docs/adr/` (why, and what was rejected);
`docs/README.md`'s documentation principles say so, and this test is what keeps it true.

The patterns are narrow on purpose. They catch the shapes history actually took in these docs,
not every sentence that could be read as history, so a false positive is rare and gets an entry in
`_ALLOWED` with the reason rather than a looser pattern.

Scope is the published set (`PUBLISHED_DOCS`: what `privacyfence.eu/docs/` renders, plus the two
root files readers land on first) and the contributor docs (`CONTRIBUTOR_DOCS`: the GitHub-only
half of `docs/README.md`'s index), which are held to the same rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

PUBLISHED_DOCS = (
    "README.md",
    "SECURITY.md",
    "docs/getting-started.md",
    "docs/install-macos.md",
    "docs/install-windows.md",
    "docs/install-linux.md",
    "docs/platform-support.md",
    "docs/how-it-works.md",
    "docs/approvals-and-policy.md",
    "docs/tools-reference.md",
    "docs/always-allow-rules-reference.md",
    "docs/pii-detection-keywords.md",
    "docs/security-and-compliance.md",
    "docs/configuration-reference.md",
    "docs/org-mode-setup-guide.md",
    "docs/connecting-a-service.md",
    "docs/google-cloud-setup.md",
    "docs/slack-setup.md",
    "docs/salesforce-setup.md",
    "docs/atlassian-setup.md",
    "docs/telegram-setup.md",
)

CONTRIBUTOR_DOCS = (
    "CONTRIBUTING.md",
    "CLAUDE.md",
    "docs/README.md",
    "docs/coding-and-testing-guidelines.md",
    "docs/dev-vs-live-setup.md",
    "docs/testing-policy.md",
    "docs/release-testing.md",
    "docs/packaging.md",
    "docs/connector-qa.md",
    "docs/downloads-and-release-kpi.md",
    "docs/images/screenshots/README.md",
)

_PATTERNS = {
    "phase name": re.compile(r"\bPhase \d"),
    "phase ID": re.compile(r"\bP\d{1,2}\b"),
    "issue/PR number": re.compile(r"#\d{3}"),
    "finding ID": re.compile(r"\b(?:SEC|TST)-\d+"),
    "version-qualified history": re.compile(r"\b(?:as of|since|through|until) v?\d+\.\d", re.IGNORECASE),
    "migration guide": re.compile(r"\bmigration guide\b", re.IGNORECASE),
}

# (doc, exact matched text) pairs that are not history, each with its reason. Keep it short.
_ALLOWED: dict[tuple[str, str], str] = {
    ("docs/README.md", "as of 4.2"): "the documentation principles quote the phrase as the example of what not to write",
}


def _hits(rel: str) -> list[str]:
    hits = []
    for lineno, line in enumerate((REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
        for label, pattern in _PATTERNS.items():
            for match in pattern.finditer(line):
                if (rel, match.group(0)) not in _ALLOWED:
                    hits.append(f"{rel}:{lineno}: {label} {match.group(0)!r}: {line.strip()}")
    return hits


@pytest.mark.parametrize("rel", PUBLISHED_DOCS + CONTRIBUTOR_DOCS)
def test_doc_carries_no_history(rel):
    hits = _hits(rel)
    assert not hits, (
        "Docs describe the current version only; history belongs in CHANGELOG.md or an "
        "ADR (see docs/README.md's documentation principles):\n" + "\n".join(hits)
    )


def test_every_listed_doc_exists():
    missing = [rel for rel in PUBLISHED_DOCS + CONTRIBUTOR_DOCS if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"PUBLISHED_DOCS/CONTRIBUTOR_DOCS name files that do not exist: {missing}"


def test_the_patterns_catch_the_shapes_they_exist_for():
    samples = {
        "phase name": "added in Phase 4 of the plan",
        "phase ID": "retired by P9",
        "issue/PR number": "fixed in #428",
        "finding ID": "see SEC-09",
        "version-qualified history": "as of 4.2 the popup is gone",
        "migration guide": "read the migration guide first",
    }
    for label, text in samples.items():
        assert _PATTERNS[label].search(text), label
    assert not any(p.search("Python 3.11 or later, macOS 13, port 8443") for p in _PATTERNS.values())
