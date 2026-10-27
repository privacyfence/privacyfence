"""Tests for the root setup.py build hook that bundles CHANGELOG.md into the wheel (ADR 0237).

Imported by file path (importlib) rather than run: the ``__main__`` guard keeps ``setup()`` from
being called on import, so the hook can be exercised without building anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# setup.py imports setuptools at module level, and a bare Python 3.14 venv does not ship it. The
# wheel integration test builds with the real toolchain, so skipping here loses no coverage.
setuptools_dist = pytest.importorskip("setuptools.dist")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("pf_setup", REPO_ROOT / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundled_changelog_path_is_inside_the_package_resources():
    assert _load().BUNDLED_CHANGELOG == Path("privacyfence/resources/CHANGELOG.md")


def test_changelog_source_is_the_repository_root_file():
    module = _load()
    assert module.CHANGELOG == REPO_ROOT / "CHANGELOG.md"
    assert module.CHANGELOG.is_file()


def test_build_py_copies_the_changelog_into_build_lib(tmp_path):
    module = _load()
    dist = setuptools_dist.Distribution(
        {"name": "privacyfence", "packages": [], "package_dir": {"": "src"}}
    )
    cmd = module.BuildPyWithChangelog(dist)
    cmd.build_lib = str(tmp_path)
    cmd.ensure_finalized()
    cmd.run()
    copied = tmp_path / "privacyfence" / "resources" / "CHANGELOG.md"
    assert copied.read_bytes() == (REPO_ROOT / "CHANGELOG.md").read_bytes()
