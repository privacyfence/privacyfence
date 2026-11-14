"""Guards that the homepage's primary Install CTAs point at the install page, and that
page exists (ADR 0277, ADR 0300).

Deliberately a static test rather than a browser one: the failure this module exists to catch is
the one where the CTAs survive a revert of the page (or vice versa), leaving every "Install"
button on the homepage pointing at nothing. That is worth an assertion that runs on every machine,
in every suite, with no browser.

It also holds guardrail 7: every file under website/ is either published by the build manifest in
scripts/build_site.py or explicitly kept back from the public site.
"""

from __future__ import annotations

import re

import pytest

from tests.website_site import REPO, WEBSITE, build_site, built_site, read_page

pytestmark = pytest.mark.unit

# The three primary CTAs: header nav, hero, and the closing call to action.
CTA_PATTERN = re.compile(r'<a class="(?:nav-cta|button primary)" href="([^"]+)">([^<]*Install[^<]*)</a>')


def _ctas() -> list[tuple[str, str]]:
    # The built page: the header CTA comes from the shared header partial.
    return CTA_PATTERN.findall(read_page("/"))


def test_the_install_page_the_ctas_point_at_exists():
    assert (WEBSITE / "install" / "index.html").is_file()
    assert (WEBSITE / "install" / "install.js").is_file()


def test_homepage_install_ctas_point_at_the_install_page():
    ctas = _ctas()
    assert len(ctas) == 3, f"expected 3 Install CTAs, found {len(ctas)}: {ctas}"
    for href, label in ctas:
        assert href == "/install/", f"{label.strip()!r} points at {href!r}, not the install page"


def test_homepage_install_ctas_do_not_point_at_github():
    # The specific regression: a CTA left on GitHub Releases although the install page
    # is the primary path. GitHub stays linked for source and docs -- just not as the
    # install button.
    for href, label in _ctas():
        assert "github.com" not in href, f"{label.strip()!r} still points at GitHub: {href}"


def test_the_header_cta_is_install_on_every_page():
    # The shared header's call to action is Install everywhere, the /download/ stub included.
    for path in build_site.PAGES:
        assert '<a class="nav-cta" href="/install/">Install</a>' in read_page(path), path


def test_github_remains_linked_for_source_and_docs():
    # The install buttons point at the install page; that must not quietly delist the project's own
    # source, which stays a documented secondary source.
    assert "https://github.com/privacyfence/privacyfence" in read_page("/")


def _website_files() -> set[str]:
    return {path.relative_to(WEBSITE).as_posix() for path in WEBSITE.rglob("*") if path.is_file()}


def test_every_website_file_is_actually_deployed():
    """Guardrail 7: every file under website/ is published by scripts/build_site.py's manifest,
    or is explicitly one of the build's inputs or repository-only.

    The build publishes a named list rather than the directory, so nothing lands on the public
    site by accident; this is what keeps that list complete. It checks the whole tree, so a file
    added under website/ and left out of the manifest is caught here instead of 404ing in
    production.
    """
    accounted = (
        set(build_site.PAGES.values())
        | set(build_site.UNLISTED_PAGES.values())
        | {source.removeprefix("website/") for source in build_site.STATIC.values() if source.startswith("website/")}
        | set(build_site.BUILD_INPUTS)
        | set(build_site.REPOSITORY_ONLY)
    )
    missing = sorted(_website_files() - accounted)
    assert not missing, (
        f"these website files are in no list of scripts/build_site.py (PAGES, STATIC, BUILD_INPUTS or "
        f"REPOSITORY_ONLY), so they 404 in production: {missing}"
    )


def test_the_manifest_names_only_files_that_exist():
    for source in build_site.PAGES.values():
        assert (WEBSITE / source).is_file(), f"PAGES names website/{source}, which does not exist"
    for dest, source in build_site.STATIC.items():
        assert (REPO / source).is_file(), f"STATIC copies {source} to {dest}, but {source} does not exist"
    for name in build_site.BUILD_INPUTS:
        assert (WEBSITE / name).is_file(), f"BUILD_INPUTS names website/{name}, which does not exist"


def test_every_manifest_entry_is_in_the_built_site():
    site = built_site()
    for source in build_site.PAGES.values():
        assert (site / source).is_file(), f"{source} is in PAGES but not in the built site"
    for dest in build_site.STATIC:
        assert (site / dest).is_file(), f"{dest} is in STATIC but not in the built site"


def test_repository_only_files_and_build_inputs_are_not_deployed():
    published = set(build_site.PAGES.values()) | {s.removeprefix("website/") for s in build_site.STATIC.values()}
    built_names = {path.name for path in built_site().rglob("*") if path.is_file()}
    for name in build_site.REPOSITORY_ONLY | build_site.BUILD_INPUTS:
        assert (WEBSITE / name).is_file(), f"{name} is listed as not deployed but does not exist"
        assert name not in published, f"{name} is not to be deployed, but the build manifest publishes it"
    for name in build_site.REPOSITORY_ONLY:
        assert name.rsplit("/", 1)[-1] not in built_names, f"{name} is repository-only but is in the built site"
    assert not any(built_site().glob("_*")), "a build-only directory (website/_partials, website/_docs) was published"
