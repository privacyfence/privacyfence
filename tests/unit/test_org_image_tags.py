"""Tests for scripts/org_image_tags.py.

Imported by file path (importlib) rather than as a package, same pattern as
tests/unit/test_tag_release.py. ``image_tags`` is pure and is tested against fabricated tag lists;
``main`` is exercised against a real temporary git repository.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "org_image_tags.py"
_spec = importlib.util.spec_from_file_location("org_image_tags", _SCRIPT_PATH)
org_image_tags = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(org_image_tags)


@pytest.mark.parametrize(
    ("known", "version", "expected"),
    [
        ([], "1.0.0", ["1.0.0", "1.0", "1", "latest"]),
        (["v6.4.0", "v6.5.0", "v6.5.1"], "6.5.1", ["6.5.1", "6.5", "6", "latest"]),
        (["v6.4.2", "v6.5.0"], "6.4.3", ["6.4.3", "6.4"]),
        (["v5.9.0", "v6.0.0"], "5.9.1", ["5.9.1", "5.9", "5"]),
        (
            ["v7.0.0a1", "v7.0.0rc2", "vX", "6.9.9"],
            "6.5.0",
            ["6.5.0", "6.5", "6", "latest"],
        ),
    ],
)
def test_image_tags(known: list[str], version: str, expected: list[str]) -> None:
    assert org_image_tags.image_tags(version, known) == expected


@pytest.mark.parametrize("version", ["6.6.0a1", "6.6.0rc1", "6.6", "v6.6.0"])
def test_non_stable_version_is_refused(version: str) -> None:
    with pytest.raises(ValueError, match="stable versions only"):
        org_image_tags.image_tags(version, [])


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_main_reads_tags_from_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(["init"], tmp_path)
    _git(
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-m", "init"],
        tmp_path,
    )
    _git(["tag", "v1.0.0"], tmp_path)
    _git(["tag", "v1.1.0"], tmp_path)
    monkeypatch.setattr(org_image_tags, "REPO_ROOT", tmp_path)

    assert org_image_tags.main(["1.0.1"]) == 0
    assert capsys.readouterr().out == "1.0.1\n1.0\n"


def test_main_refuses_a_pre_release(capsys: pytest.CaptureFixture[str]) -> None:
    assert org_image_tags.main(["6.6.0a1"]) == 2
    assert "error: org images are published for stable versions only" in capsys.readouterr().err
