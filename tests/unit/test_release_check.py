"""Tests for scripts/release_check.py.

Imported by file path (importlib), same pattern as tests/unit/test_tag_release.py. The three
step scripts are faked by replacing ``subprocess.run`` on the imported module and recording argv;
only the invalid-version test runs the real scripts.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_check.py"
_spec = importlib.util.spec_from_file_location("release_check", _SCRIPT_PATH)
release_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_check)


class FakeRun:
    """Records each argv and answers by script name; `fail` maps a script name to an exit code."""

    def __init__(self, *, channel: str = "stable", notes: str = "- a change\n", fail: dict[str, int] | None = None):
        self.channel = channel
        self.notes = notes
        self.fail = fail or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        script = Path(argv[1]).name
        code = self.fail.get(script, 0)
        stdout = {"r2_release.py": f"{self.channel}\n", "changelog_section.py": self.notes}.get(script, "")
        stderr = f"{script} broke\n" if code else ""
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    @property
    def scripts(self) -> list[str]:
        return [Path(call[1]).name for call in self.calls]


@pytest.fixture(autouse=True)
def release_root(tmp_path_factory, monkeypatch):
    # Not under `tmp_path`: test_no_summary_is_written_when_the_variable_is_unset asserts it stays empty.
    root = tmp_path_factory.mktemp("release-root") / "repo"
    (root / ".tld").mkdir(parents=True)
    (root / ".tld" / "release-version").write_text("7.1.0\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "ROOT", root)
    return root


@pytest.fixture
def fake(monkeypatch):
    runner = FakeRun()
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return runner


def test_stable_runs_channel_notes_and_tag_checks_in_order(fake):
    assert release_check.main(["7.1.0"]) == 0
    assert fake.scripts == ["r2_release.py", "changelog_section.py", "tag_release.py"]
    assert fake.calls[0][0] == sys.executable


def test_prerelease_skips_the_release_notes_step(monkeypatch):
    runner = FakeRun(channel="prerelease")
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    assert release_check.main(["7.1.0a1"]) == 0
    assert runner.scripts == ["r2_release.py", "tag_release.py"]


def test_tag_checks_run_with_no_fetch_and_check_only(fake):
    release_check.main(["7.1.0"])
    assert fake.calls[-1][2:] == ["7.1.0", "--no-fetch", "--check-only"]


def test_a_failing_channel_step_stops_everything(monkeypatch, capsys):
    runner = FakeRun(fail={"r2_release.py": 3})
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    assert release_check.main(["7.1.0"]) == 3
    assert runner.scripts == ["r2_release.py"]
    assert "release check failed: release channel (exit 3)" in capsys.readouterr().err


def test_a_failing_notes_step_stops_before_the_tag_checks(monkeypatch, capsys):
    runner = FakeRun(fail={"changelog_section.py": 2})
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    assert release_check.main(["7.1.0"]) == 2
    assert runner.scripts == ["r2_release.py", "changelog_section.py"]
    assert "release check failed: release notes (exit 2)" in capsys.readouterr().err


def test_a_failing_tag_check_returns_its_exit_code(monkeypatch, capsys):
    runner = FakeRun(fail={"tag_release.py": 1})
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    assert release_check.main(["7.1.0"]) == 1
    assert "release check failed: tag checks (exit 1)" in capsys.readouterr().err


def test_notes_are_appended_to_the_step_summary(fake, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert release_check.main(["7.1.0"]) == 0
    text = summary.read_text(encoding="utf-8")
    assert "### Release notes that will ship for v7.1.0" in text
    assert "- a change" in text


def test_no_summary_is_written_when_the_variable_is_unset(fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert release_check.main(["7.1.0"]) == 0
    assert list(tmp_path.iterdir()) == []


def test_never_passes_allow_unreleased(fake):
    release_check.main(["7.1.0"])
    assert all("--allow-unreleased" not in call for call in fake.calls)


def test_an_invalid_version_fails_at_the_channel_step(capsys):
    assert release_check.main(["nope"]) == 1
    assert "release check failed: release channel (exit 1)" in capsys.readouterr().err


def test_a_stable_version_must_match_the_version_file(fake, release_root, capsys):
    (release_root / ".tld" / "release-version").write_text("7.0.2\n", encoding="utf-8")
    assert release_check.main(["7.1.0"]) == 1
    assert "release check failed: release version file" in capsys.readouterr().err
    assert fake.scripts == ["r2_release.py"]


def test_a_missing_version_file_fails_a_stable_release(fake, release_root, capsys):
    (release_root / ".tld" / "release-version").unlink()
    assert release_check.main(["7.1.0"]) == 1
    assert "release check failed: release version file" in capsys.readouterr().err
    assert fake.scripts == ["r2_release.py"]


def test_a_prerelease_ignores_the_version_file(release_root, monkeypatch):
    (release_root / ".tld" / "release-version").write_text("7.0.2\n", encoding="utf-8")
    runner = FakeRun(channel="beta")
    monkeypatch.setattr(release_check.subprocess, "run", runner)
    assert release_check.main(["7.1.0a1"]) == 0
    assert runner.scripts == ["r2_release.py", "tag_release.py"]


def test_the_repository_version_file_is_a_stable_version():
    text = (_SCRIPT_PATH.parents[1] / ".tld" / "release-version").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", text)
