"""Builds the real wheel and checks it carries CHANGELOG.md; guards ADR 0237's build hook.

The root setup.py copies CHANGELOG.md into ``privacyfence/resources/`` during ``build_py``. If
``setuptools.build_meta`` ever stopped running it, the organization image would lose the release
notes its What's new page reads, and nothing else would notice until an operator upgraded.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_built_wheel_contains_the_changelog(tmp_path):
    pytest.importorskip("build")
    pytest.importorskip("setuptools_scm")
    pytest.importorskip("wheel")

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=540,
    )

    (wheel,) = tmp_path.glob("privacyfence-*.whl")
    with zipfile.ZipFile(wheel) as zf:
        assert "privacyfence/resources/CHANGELOG.md" in zf.namelist()
        bundled = zf.read("privacyfence/resources/CHANGELOG.md")
    assert bundled == (REPO_ROOT / "CHANGELOG.md").read_bytes()
