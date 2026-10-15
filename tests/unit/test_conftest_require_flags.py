"""The switches that turn a release-gating skip into a failure.

``PRIVACYFENCE_REQUIRE_PACKAGED=1`` and ``PRIVACYFENCE_REQUIRE_BROWSER=1`` are set in CI so
that a packaged or browser test that silently skips (no installer built, no Chromium) fails
the job instead of reporting green. These tests run the two hooks in ``tests/conftest.py``
against throwaway test modules in a pytester directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFTEST = f"""
import sys

sys.path.insert(0, {str(REPO_ROOT)!r})

from tests.conftest import pytest_collectreport, pytest_runtest_makereport  # noqa: E402,F401
"""


@pytest.fixture
def project(pytester, monkeypatch):
    for name in ("PRIVACYFENCE_REQUIRE_PACKAGED", "PRIVACYFENCE_REQUIRE_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    pytester.makeconftest(_CONFTEST)
    return pytester


def _run(project):
    return project.runpytest_inprocess("-p", "no:cacheprovider")


def test_packaged_skip_fails_when_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.packaged
        def test_it():
            pytest.skip("no dist/x built yet")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_PACKAGED", "1")
    result = _run(project)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*skipped while PRIVACYFENCE_REQUIRE_PACKAGED=1 requires it to run: no dist/x built yet*"])


def test_packaged_skip_fails_when_the_artifact_is_missing(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.packaged
        def test_it():
            pytest.skip("no dist/PrivacyFence-*.pkg built yet")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_PACKAGED", "1")
    _run(project).assert_outcomes(failed=1)


def test_packaged_skip_for_an_unsigned_build_stays_a_skip_when_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.packaged
        def test_it():
            pytest.skip("x.dmg is signed but not notarized for this build (NOTARIZE_PROFILE unset)")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_PACKAGED", "1")
    _run(project).assert_outcomes(skipped=1)


def test_packaged_skip_stays_a_skip_without_the_switch(project):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.packaged
        def test_it():
            pytest.skip("no dist/x built yet")
        """
    )
    _run(project).assert_outcomes(skipped=1)


def test_browser_chromium_skip_fails_when_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.browser
        def test_it():
            pytest.skip("Chromium not available for Playwright (boom)")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_BROWSER", "1")
    _run(project).assert_outcomes(failed=1)


def test_browser_unrelated_skip_stays_a_skip_when_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        @pytest.mark.browser
        def test_it():
            pytest.skip("/docs/ was not built")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_BROWSER", "1")
    _run(project).assert_outcomes(skipped=1)


def test_module_level_skip_fails_collection_when_packaged_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        pytest.skip("no artifact", allow_module_level=True)

        def test_it():
            pass
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_PACKAGED", "1")
    result = _run(project)
    assert result.ret != 0
    result.assert_outcomes(errors=1)


def test_module_level_playwright_skip_fails_collection_when_browser_required(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        pytest.importorskip("no_such_playwright_module", reason="playwright not installed")

        def test_it():
            pass
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_BROWSER", "1")
    result = _run(project)
    assert result.ret != 0
    result.assert_outcomes(errors=1)


def test_unmarked_skip_stays_a_skip_with_both_switches(project, monkeypatch):
    project.makepyfile(
        """
        import pytest

        def test_it():
            pytest.skip("Chromium not available")
        """
    )
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_PACKAGED", "1")
    monkeypatch.setenv("PRIVACYFENCE_REQUIRE_BROWSER", "1")
    _run(project).assert_outcomes(skipped=1)
