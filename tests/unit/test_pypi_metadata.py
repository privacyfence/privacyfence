"""The metadata a `pip install privacyfence` visitor actually sees on pypi.org.

`pyproject.toml`'s `[project]` table and `README.md` are, together, the entire PyPI project page:
the sidebar comes from `[project.urls]` and `classifiers`, and the page body *is* `README.md`,
rendered by a machine that has never heard of this repo's directory layout. Both halves can go
wrong at once -- no links or facets in the sidebar, and a body whose every doc link 404s and
whose screenshots are broken images.

Nothing about that failure is visible from a source checkout: `README.md` renders perfectly on
GitHub either way. These tests assert
the two properties a checkout can't show you -- that the sidebar metadata is present and
self-consistent, and that the long description contains no path only GitHub could resolve.

Link *targets* are checked in test_docs_links.py, which resolves the absolute
`github.com/.../blob/main/...` URLs this file requires back against the working tree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

# The sidebar rows worth guaranteeing. PyPI gives Homepage/Download their own well-known slots and
# lists the rest verbatim, so these names are the labels a visitor reads, not internal keys.
_REQUIRED_URL_LABELS = {"Homepage", "Download", "Documentation", "Source", "Changelog", "Issues", "Security"}

# `[text](target)` and `<img src="target">` -- the two ways README.md points at anything.
_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_IMG = re.compile(r"<img\s[^>]*?src=\"([^\"]+)\"")


def test_project_urls_cover_the_sidebar():
    urls = PROJECT.get("urls", {})
    assert _REQUIRED_URL_LABELS <= set(urls), f"missing [project.urls] entries: {sorted(_REQUIRED_URL_LABELS - set(urls))}"


@pytest.mark.parametrize("label", sorted(_REQUIRED_URL_LABELS))
def test_project_url_is_absolute_https(label: str):
    """A relative or `http://` URL in the sidebar is either dead or a downgrade on click."""
    url = PROJECT["urls"][label]
    assert url.startswith("https://"), f"[project.urls] {label} is not an https URL: {url}"


def test_classifiers_are_well_formed():
    """Structure only -- the authoritative check that each string is a *registered* trove
    classifier is PyPI's own upload validation, which rejects the whole distribution. This catches
    the typo (a lone `::`, stray whitespace) before a release day does."""
    classifiers = PROJECT["classifiers"]
    assert classifiers, "no classifiers: the project appears in no PyPI browse facet at all"
    for classifier in classifiers:
        assert " :: " in classifier, f"not a trove classifier: {classifier!r}"
        parts = classifier.split(" :: ")
        assert all(part and part == part.strip() for part in parts), f"malformed classifier: {classifier!r}"


@pytest.mark.parametrize(
    "prefix",
    ["Development Status ::", "License :: OSI Approved ::", "Intended Audience ::", "Operating System ::", "Topic ::"],
)
def test_classifier_facet_is_populated(prefix: str):
    assert any(c.startswith(prefix) for c in PROJECT["classifiers"]), f"no `{prefix}` classifier"


def test_license_classifier_matches_the_declared_license():
    license_text = PROJECT["license"]["text"]
    assert license_text == "Apache-2.0", f"unexpected license, update this test deliberately: {license_text}"
    assert "License :: OSI Approved :: Apache Software License" in PROJECT["classifiers"]


def test_python_version_classifiers_match_requires_python():
    """The classifiers claim support to a browsing user; `requires-python` enforces it at install
    time. A version listed in one and not the other is a promise the other half breaks."""
    spec = SpecifierSet(PROJECT["requires-python"])
    claimed = {c.rsplit(" :: ", 1)[1] for c in PROJECT["classifiers"] if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)}
    assert claimed, "no `Programming Language :: Python :: 3.N` classifiers"
    for version in sorted(claimed):
        assert spec.contains(version), f"classifier claims Python {version}, which requires-python ({spec}) excludes"


def test_readme_is_the_long_description():
    """The rest of this module is only true of PyPI's page because the page is this file."""
    assert PROJECT["readme"] == "README.md"


def _readme_targets() -> list[tuple[int, str]]:
    found = []
    for pattern in (_LINK, _IMG):
        for match in pattern.finditer(README):
            found.append((README[: match.start()].count("\n") + 1, match.group(1)))
    return sorted(found)


_README_TARGETS = _readme_targets()


def test_the_readme_scan_actually_found_targets():
    assert len(_README_TARGETS) > 20, f"only {len(_README_TARGETS)} README links/images found -- the scan is probably broken"


@pytest.mark.parametrize("line, target", _README_TARGETS, ids=lambda v: v if isinstance(v, str) else "")
def test_readme_target_renders_off_github(line: int, target: str):
    """PyPI resolves nothing relative to this repo: `docs/how-it-works.md` 404s there and
    `docs/images/screenshots/*.png` renders as a broken image. In-page `#anchor` links are fine --
    PyPI slugs headings the same way GitHub does -- and so is `mailto:`."""
    assert target.startswith(("https://", "#", "mailto:")), (
        f"README.md:{line}: `{target}` only resolves on GitHub. README.md is also the PyPI long "
        f"description, so links need the absolute https:// form (blob/main/... for files, "
        f"raw.githubusercontent.com/.../main/... for images)."
    )
