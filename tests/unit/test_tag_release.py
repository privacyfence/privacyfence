"""Tests for scripts/tag_release.py.

Imported by file path (importlib) rather than as a package, same pattern as
tests/unit/test_r2_release.py -- scripts/ isn't part of the installed ``privacyfence`` distribution.

The git-plumbing checks (check_top_of_main, check_no_tag_at_head, known_identities, main()'s
end-to-end path) are exercised against real temporary git repositories rather than mocked
subprocess calls -- these checks are all about actual git state (branch, cleanliness, remote
tracking, existing tags), and a real repo is both the most faithful way to test that and simpler
than mocking a sequence of `git` invocations. The pure parsing/sequencing logic
(parse_version, check_sequential) needs no git at all and is tested directly against fabricated
histories.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "tag_release.py"
_spec = importlib.util.spec_from_file_location("tag_release", _SCRIPT_PATH)
tag_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tag_release)

Identity = tag_release.Identity


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' plus a clone of it, both with user.email/name set (a clean CI environment
    has neither configured) and one commit already pushed to main. Returns (clone, bare_origin)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git(["init", "--bare", "-b", "main"], cwd=origin)

    clone = tmp_path / "clone"
    _run_git(["clone", str(origin), str(clone)], cwd=tmp_path)
    _run_git(["config", "user.email", "test@example.com"], cwd=clone)
    _run_git(["config", "user.name", "Test"], cwd=clone)
    (clone / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=clone)
    _run_git(["commit", "-m", "Initial commit"], cwd=clone)
    _run_git(["push", "-u", "origin", "main"], cwd=clone)
    return clone, origin


class TestParseVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("4.2.0", Identity(4, 2, 0, "", 0)),
            ("v4.2.0", Identity(4, 2, 0, "", 0)),
            ("4.2.0a1", Identity(4, 2, 0, "a", 1)),
            ("4.2.0b12", Identity(4, 2, 0, "b", 12)),
            ("4.2.0rc3", Identity(4, 2, 0, "rc", 3)),
        ],
    )
    def test_parses_valid_version(self, version, expected):
        assert tag_release.parse_version(version) == expected

    @pytest.mark.parametrize(
        "version",
        [
            "not-a-version",
            "4.2",
            "4.2.0-beta1",  # old spelling -- refused for a *new* tag
            "4.2.0-alpha1",
            "4.2.0.dev3+gabc1234",
            "4.2.0+local",
        ],
    )
    def test_rejects_invalid_version(self, version):
        with pytest.raises(tag_release.TagError, match="isn't a valid release version"):
            tag_release.parse_version(version)


class TestTagName:
    def test_stable(self):
        assert tag_release.tag_name(Identity(4, 2, 0, "", 0)) == "v4.2.0"

    def test_prerelease(self):
        assert tag_release.tag_name(Identity(4, 2, 0, "a", 1)) == "v4.2.0a1"


class TestNextVersionOptions:
    def test_patch_minor_major_bumps(self):
        assert tag_release.next_version_options((4, 2, 0)) == {(4, 2, 1), (4, 3, 0), (5, 0, 0)}


class TestCheckSequential:
    def test_first_prerelease_of_fresh_line_is_a1(self):
        known = [Identity(4, 1, 0, "", 0)]
        tag_release.check_sequential(Identity(4, 2, 0, "a", 1), known)  # no raise

    def test_prerelease_gap_is_rejected(self):
        known = [Identity(4, 1, 0, "", 0)]
        with pytest.raises(tag_release.TagError, match="must be a1"):
            tag_release.check_sequential(Identity(4, 2, 0, "a", 3), known)

    def test_next_prerelease_number_after_existing_ones(self):
        known = [Identity(4, 1, 0, "", 0), Identity(4, 2, 0, "a", 1), Identity(4, 2, 0, "a", 2)]
        tag_release.check_sequential(Identity(4, 2, 0, "a", 3), known)  # no raise

    def test_prerelease_number_gap_among_existing_ones_is_rejected(self):
        known = [Identity(4, 1, 0, "", 0), Identity(4, 2, 0, "a", 1), Identity(4, 2, 0, "a", 2)]
        with pytest.raises(tag_release.TagError, match="must be a3"):
            tag_release.check_sequential(Identity(4, 2, 0, "a", 4), known)

    def test_new_prerelease_stage_starts_at_1_regardless_of_other_stage(self):
        known = [Identity(4, 1, 0, "", 0), Identity(4, 2, 0, "a", 1), Identity(4, 2, 0, "a", 2)]
        tag_release.check_sequential(Identity(4, 2, 0, "b", 1), known)  # no raise

    def test_prerelease_after_line_already_stable_is_rejected(self):
        known = [Identity(4, 2, 0, "", 0)]
        with pytest.raises(tag_release.TagError, match="already released as a stable version"):
            tag_release.check_sequential(Identity(4, 2, 0, "a", 1), known)

    def test_prerelease_version_that_skips_stable_is_rejected(self):
        known = [Identity(4, 1, 0, "", 0)]
        with pytest.raises(tag_release.TagError, match="isn't a valid next version"):
            tag_release.check_sequential(Identity(4, 3, 0, "a", 1), known)

    def test_stable_patch_bump_accepted(self):
        known = [Identity(4, 1, 0, "", 0)]
        tag_release.check_sequential(Identity(4, 1, 1, "", 0), known)  # no raise

    def test_stable_version_skip_is_rejected(self):
        known = [Identity(4, 1, 0, "", 0)]
        with pytest.raises(tag_release.TagError, match="isn't a valid next version"):
            tag_release.check_sequential(Identity(4, 3, 0, "", 0), known)

    def test_stable_finalizing_existing_prerelease_cycle_is_accepted(self):
        # 4.2.0 is reached via a1/b1 first, so it's already "next" by the time it's stable --
        # nothing here re-checks it against the version-skip rule.
        known = [Identity(4, 1, 0, "", 0), Identity(4, 2, 0, "a", 1), Identity(4, 2, 0, "b", 1)]
        tag_release.check_sequential(Identity(4, 2, 0, "", 0), known)  # no raise

    def test_duplicate_stable_is_rejected(self):
        known = [Identity(4, 2, 0, "", 0)]
        with pytest.raises(tag_release.TagError, match="already tagged as a stable release"):
            tag_release.check_sequential(Identity(4, 2, 0, "", 0), known)

    def test_no_prior_stable_release_skips_the_skip_check(self):
        # Bootstrap case: nothing to be "next" after.
        tag_release.check_sequential(Identity(0, 1, 0, "", 0), [])  # no raise
        tag_release.check_sequential(Identity(0, 1, 0, "a", 1), [])  # no raise


class TestKnownIdentities:
    def test_parses_canonical_and_legacy_spellings(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        for name in ("v4.1.0", "v4.2.0a1", "v3.2.0-beta1", "not-a-release"):
            _run_git(["tag", name], cwd=clone)

        identities = tag_release.known_identities(clone)

        assert Identity(4, 1, 0, "", 0) in identities
        assert Identity(4, 2, 0, "a", 1) in identities
        assert Identity(3, 2, 0, "b", 1) in identities
        assert len(identities) == 3  # "not-a-release" contributes nothing


class TestCheckTopOfMain:
    def test_passes_when_on_main_clean_and_up_to_date(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        tag_release.check_top_of_main(clone, fetch=False)  # no raise

    def test_rejects_wrong_branch(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        _run_git(["checkout", "-b", "feature/x"], cwd=clone)
        with pytest.raises(tag_release.TagError, match="not 'main'"):
            tag_release.check_top_of_main(clone, fetch=False)

    def test_rejects_dirty_working_tree(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        (clone / "README.md").write_text("changed\n", encoding="utf-8")
        with pytest.raises(tag_release.TagError, match="isn't clean"):
            tag_release.check_top_of_main(clone, fetch=False)

    def test_rejects_local_main_behind_remote(self, tmp_path):
        clone, origin = _init_repo_with_remote(tmp_path)
        # A second clone pushes a commit origin/main now has that `clone` was never fetched.
        other = tmp_path / "other"
        _run_git(["clone", str(origin), str(other)], cwd=tmp_path)
        _run_git(["config", "user.email", "test@example.com"], cwd=other)
        _run_git(["config", "user.name", "Test"], cwd=other)
        (other / "README.md").write_text("more\n", encoding="utf-8")
        _run_git(["commit", "-am", "Second commit"], cwd=other)
        _run_git(["push", "origin", "main"], cwd=other)

        with pytest.raises(tag_release.TagError, match="is not origin/main"):
            tag_release.check_top_of_main(clone, fetch=True)

    def test_rejects_local_main_ahead_of_remote(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        (clone / "README.md").write_text("ahead\n", encoding="utf-8")
        _run_git(["commit", "-am", "Unpushed commit"], cwd=clone)
        with pytest.raises(tag_release.TagError, match="is not origin/main"):
            tag_release.check_top_of_main(clone, fetch=False)


class TestCheckNoTagAtHead:
    def test_passes_when_head_is_untagged(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        tag_release.check_no_tag_at_head(clone)  # no raise

    def test_rejects_when_head_already_tagged(self, tmp_path):
        clone, _ = _init_repo_with_remote(tmp_path)
        _run_git(["tag", "v4.1.0a6"], cwd=clone)
        with pytest.raises(tag_release.TagError, match="already carries tag"):
            tag_release.check_no_tag_at_head(clone)


class TestMain:
    def test_creates_local_tag_without_pushing(self, tmp_path, capsys):
        clone, origin = _init_repo_with_remote(tmp_path)
        exit_code = tag_release.main(["0.1.0", "--repo", str(clone), "--no-fetch"])

        assert exit_code == 0
        assert _run_git_output(["tag", "--list", "v0.1.0"], cwd=clone) == "v0.1.0"
        # not pushed: origin's bare repo has no tags
        assert _run_git_output(["tag", "--list"], cwd=origin) == ""
        assert "not pushed" in capsys.readouterr().out

    def test_pushes_with_flag(self, tmp_path, capsys):
        clone, origin = _init_repo_with_remote(tmp_path)
        exit_code = tag_release.main(["0.1.0", "--repo", str(clone), "--no-fetch", "--push"])

        assert exit_code == 0
        assert _run_git_output(["tag", "--list", "v0.1.0"], cwd=origin) == "v0.1.0"
        assert "pushed v0.1.0" in capsys.readouterr().out

    def test_exits_nonzero_and_creates_no_tag_on_bad_version(self, tmp_path, capsys):
        clone, _ = _init_repo_with_remote(tmp_path)
        exit_code = tag_release.main(["not-a-version", "--repo", str(clone), "--no-fetch"])

        assert exit_code == 1
        assert "isn't a valid release version" in capsys.readouterr().err
        assert _run_git_output(["tag", "--list"], cwd=clone) == ""

    def test_exits_nonzero_on_sequential_gap(self, tmp_path, capsys):
        clone, _ = _init_repo_with_remote(tmp_path)
        _run_git(["tag", "v4.1.0"], cwd=clone)
        # A second, untagged commit -- otherwise HEAD already carrying v4.1.0 trips the
        # no-tag-at-head check before the sequential check gets a chance to run.
        (clone / "README.md").write_text("more\n", encoding="utf-8")
        _run_git(["commit", "-am", "Second commit"], cwd=clone)
        _run_git(["push", "origin", "main"], cwd=clone)
        exit_code = tag_release.main(["4.3.0", "--repo", str(clone), "--no-fetch"])

        assert exit_code == 1
        assert "isn't a valid next version" in capsys.readouterr().err
        assert _run_git_output(["tag", "--list", "v4.3.0"], cwd=clone) == ""

    def test_exits_nonzero_when_not_on_main(self, tmp_path, capsys):
        clone, _ = _init_repo_with_remote(tmp_path)
        _run_git(["checkout", "-b", "feature/x"], cwd=clone)
        exit_code = tag_release.main(["0.1.0", "--repo", str(clone), "--no-fetch"])

        assert exit_code == 1
        assert "not 'main'" in capsys.readouterr().err


def _run_git_output(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()
