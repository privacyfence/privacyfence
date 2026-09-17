"""Tests for scripts/changelog_section.py, and for the real CHANGELOG.md it reads.

Imported by file path (importlib) rather than as a package, since scripts/ isn't part of the
installed ``privacyfence`` distribution -- same pattern as tests/unit/test_r2_release.py and
tests/unit/test_build_org_bundle.py.

Half of this file tests the parser against synthetic changelogs; the other half asserts things
about the repo's own CHANGELOG.md that would otherwise only be discovered on tag day, when the
release build is the thing that fails.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "changelog_section.py"
_spec = importlib.util.spec_from_file_location("changelog_section", _SCRIPT_PATH)
changelog_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_section)

REAL_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
REAL_TEXT = REAL_CHANGELOG.read_text(encoding="utf-8")

SAMPLE = """# Changelog

<!--
A comment that quotes the convention it documents:
## [Unreleased] is permanent; only the tagging PR renames it.
-->

## [Unreleased]

### Added

- Something not yet released.

## [2.1.0] — 2026-05-01

Intro line.

### Added

- A thing.

### Fixed

- Another thing.

## [2.0.0] — 2026-04-01

### Removed

- The old thing.

[2.1.0]: https://example.invalid/compare/v2.0.0...v2.1.0
"""


class TestSection:
    def test_returns_body_without_its_own_heading(self):
        body = changelog_section.section(SAMPLE, "2.1.0")
        assert body.startswith("Intro line.")
        assert "## [2.1.0]" not in body

    def test_stops_at_the_next_version_heading(self):
        body = changelog_section.section(SAMPLE, "2.1.0")
        assert "Another thing" in body
        assert "The old thing" not in body

    def test_keeps_subheadings_inside_the_section(self):
        # "### Added" is a section boundary only if the boundary pattern is sloppy about level.
        body = changelog_section.section(SAMPLE, "2.1.0")
        assert "### Added" in body
        assert "### Fixed" in body

    def test_last_section_runs_to_the_link_definitions(self):
        body = changelog_section.section(SAMPLE, "2.0.0")
        assert "The old thing" in body
        # Reference-link definitions are part of the trailing block, not a section boundary; they're
        # harmless in a release body and not worth a second parser rule to strip.
        assert body.endswith("v2.1.0")

    def test_tolerates_a_leading_v(self):
        assert changelog_section.section(SAMPLE, "v2.1.0") == changelog_section.section(SAMPLE, "2.1.0")

    def test_ignores_a_heading_quoted_inside_an_html_comment(self):
        # The real CHANGELOG.md's top-of-file comment quotes "## [Unreleased]" while explaining the
        # convention. Matching it there would make the Unreleased section start in the wrong place.
        body = changelog_section.section(SAMPLE, "Unreleased")
        assert "Something not yet released" in body
        assert "A comment that quotes" not in body

    def test_ignores_a_heading_inside_a_fenced_code_block(self):
        fenced = "## [1.0.0] — 2026-01-01\n\n```\n## [0.9.0] — 2025-01-01\nnot a section\n```\n\nreal content\n"
        body = changelog_section.section(fenced, "1.0.0")
        assert "real content" in body
        assert "0.9.0" not in changelog_section.known_versions(fenced)

    def test_missing_version_raises(self):
        with pytest.raises(LookupError, match="no section for 9.9.9"):
            changelog_section.section(SAMPLE, "9.9.9")

    def test_duplicate_headings_for_the_same_version_raise(self):
        # The B9 trap: 4.0.0's section was opened early, so following CLAUDE.md's "rename
        # [Unreleased]" step literally produces a *second* ## [4.0.0]. Without this guard the
        # parser matched the first heading, stopped at the next ##, emitted whichever half came
        # first and exited 0 -- a green build shipping half the notes.
        duplicated = (
            "# Changelog\n\n"
            "## [4.0.0] — 2026-09-16\n\n- the entries the release PR just renamed\n\n"
            "## [4.0.0] — 2026-09-14\n\n- the entries written early, including Upgrading from 3.x\n"
        )
        with pytest.raises(LookupError, match="2 headings for 4.0.0"):
            changelog_section.section(duplicated, "4.0.0")

    def test_duplicate_detection_tolerates_a_leading_v(self):
        duplicated = "# Changelog\n\n## [1.0.0]\n\n- a\n\n## [1.0.0]\n\n- b\n"
        with pytest.raises(LookupError, match="2 headings for v1.0.0"):
            changelog_section.section(duplicated, "v1.0.0")

    def test_empty_section_raises(self):
        # The permanent, currently-empty "## [Unreleased]" heading: an empty release body would be
        # published as a silently blank release, so it's an error, not a valid answer.
        with pytest.raises(LookupError, match="is empty"):
            changelog_section.section("# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n\n- x\n", "Unreleased")


class TestKnownVersions:
    def test_lists_every_section_in_file_order(self):
        assert changelog_section.known_versions(SAMPLE) == ["Unreleased", "2.1.0", "2.0.0"]


class TestUnreleasedBody:
    def test_returns_pending_entries(self):
        assert "Something not yet released" in changelog_section.unreleased_body(SAMPLE)

    def test_stops_at_the_next_version_heading(self):
        assert "A thing." not in changelog_section.unreleased_body(SAMPLE)

    def test_empty_when_the_heading_has_nothing_under_it(self):
        assert changelog_section.unreleased_body("# C\n\n## [Unreleased]\n\n## [1.0.0]\n\n- x\n") == ""

    def test_empty_when_there_is_no_unreleased_heading(self):
        assert changelog_section.unreleased_body("# C\n\n## [1.0.0]\n\n- x\n") == ""

    def test_ignores_the_heading_quoted_in_the_top_of_file_comment(self):
        # The real CHANGELOG.md explains the convention by quoting "## [Unreleased]" in a comment.
        # Reading that as the heading would make this report pending content on an empty cycle.
        commented = "# C\n\n<!--\n## [Unreleased] is permanent.\n-->\n\n## [Unreleased]\n\n## [1.0.0]\n\n- x\n"
        assert changelog_section.unreleased_body(commented) == ""

    def test_does_not_raise_on_two_unreleased_headings(self):
        # section() raises on duplicates; this must still answer "yes, pending" rather than blow up.
        assert changelog_section.unreleased_body("# C\n\n## [Unreleased]\n\n- a\n\n## [Unreleased]\n\n- b\n") == "- a"


class TestMain:
    def test_prints_the_section_and_exits_zero(self, capsys, tmp_path):
        # SAMPLE's [Unreleased] is populated, which is the normal mid-cycle state and is what
        # --allow-unreleased is for; the guard itself is exercised below.
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE, encoding="utf-8")
        assert changelog_section.main(["2.1.0", "--changelog", str(path), "--allow-unreleased"]) == 0
        assert "A thing." in capsys.readouterr().out

    def test_missing_version_exits_non_zero_and_lists_what_is_available(self, capsys, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE, encoding="utf-8")
        assert changelog_section.main(["3.0.0", "--changelog", str(path)]) == 1
        err = capsys.readouterr().err
        assert "no section for 3.0.0" in err
        assert "2.1.0" in err

    def test_duplicate_version_exits_non_zero_rather_than_shipping_half_the_notes(self, capsys, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [2.1.0]\n\n- first\n\n## [2.1.0]\n\n- second\n", encoding="utf-8")
        assert changelog_section.main(["2.1.0", "--changelog", str(path)]) == 1
        assert "2 headings for 2.1.0" in capsys.readouterr().err

    def test_unreadable_changelog_exits_non_zero(self, capsys, tmp_path):
        assert changelog_section.main(["1.0.0", "--changelog", str(tmp_path / "nope.md")]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_defaults_to_the_repo_changelog(self, capsys):
        # --allow-unreleased so this keeps testing the default --changelog path rather than
        # doubling as an assertion about whether the release PR has merged [Unreleased] yet.
        assert changelog_section.main(["4.0.0", "--allow-unreleased"]) == 0
        assert "Upgrading from 3.x" in capsys.readouterr().out

    def test_populated_unreleased_exits_non_zero_rather_than_dropping_the_cycle(self, capsys, tmp_path):
        # The other half of the B9/B10 trap. Doing only the "correct its date" half of CLAUDE.md's
        # release step leaves one correct [2.1.0] heading with the whole cycle stranded above it:
        # the duplicate guard sees nothing wrong, and the release ships without any of it.
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE, encoding="utf-8")
        assert changelog_section.main(["2.1.0", "--changelog", str(path)]) == 1
        err = capsys.readouterr().err
        assert "[Unreleased] section still has entries" in err
        assert "merge [Unreleased] into [2.1.0]" in err

    def test_empty_unreleased_renders_without_the_flag(self, capsys, tmp_path):
        # The state the release PR is supposed to leave behind: a fresh empty [Unreleased].
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE.replace("### Added\n\n- Something not yet released.\n\n", ""), encoding="utf-8")
        assert changelog_section.main(["2.1.0", "--changelog", str(path)]) == 0
        assert "A thing." in capsys.readouterr().out

    def test_asking_for_unreleased_itself_is_not_blocked_by_its_own_content(self, capsys, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE, encoding="utf-8")
        assert changelog_section.main(["Unreleased", "--changelog", str(path)]) == 0
        assert "Something not yet released" in capsys.readouterr().out

    def test_missing_version_is_reported_before_the_unreleased_guard(self, capsys, tmp_path):
        # Both are true at once for a mistyped version; the more basic answer is the useful one.
        path = tmp_path / "CHANGELOG.md"
        path.write_text(SAMPLE, encoding="utf-8")
        assert changelog_section.main(["3.0.0", "--changelog", str(path)]) == 1
        assert "no section for 3.0.0" in capsys.readouterr().err


class TestRealChangelog:
    """Properties of this repo's own CHANGELOG.md. Each one fails a release build if violated."""

    def test_the_release_being_shipped_has_a_section(self):
        # build.yml hands this exact string to action-gh-release as the 4.0.0 release body.
        assert "Upgrading from 3.x" in changelog_section.section(REAL_TEXT, "4.0.0")

    def test_unreleased_heading_is_present_and_first(self):
        # CLAUDE.md's d929510 failure mode: without a permanent Unreleased heading, two branches in
        # flight both open a concrete version heading and both claim the same next version.
        versions = changelog_section.known_versions(REAL_TEXT)
        assert versions[0] == "Unreleased"

    def test_no_pre_release_has_its_own_section(self):
        # Keep a Changelog folds a/b/rc tags into the version they lead to. Sixteen alpha sections
        # is exactly what this file exists to avoid.
        pre_release = [v for v in changelog_section.known_versions(REAL_TEXT) if re.search(r"(a|b|rc|alpha|beta)\d*$", v)]
        assert pre_release == []

    def test_sections_are_ordered_newest_version_first(self):
        # Not newest *date* first: the 3.4.x maintenance line and the 4.0 line ran in parallel, so
        # v3.4.5-v3.4.7 were tagged after v4.0.0-alpha1..alpha4. Sorting by date would be wrong.
        released = [v for v in changelog_section.known_versions(REAL_TEXT) if v != "Unreleased"]
        as_tuples = [tuple(int(part) for part in v.split(".")) for v in released]
        assert as_tuples == sorted(as_tuples, reverse=True)

    def test_every_section_names_a_real_tag(self):
        # A section for a version that was never tagged would produce a release body for a release
        # that doesn't exist -- a mistyped heading is otherwise invisible until tag day. 4.0.0 is
        # the one legitimate exception: this file documents it ahead of its own tag, which is the
        # whole point of writing the notes before the release is cut.
        try:
            listed = subprocess.run(
                ["git", "tag", "--list"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout
        except (OSError, subprocess.CalledProcessError):  # pragma: no cover -- no git, or not a repo
            pytest.skip("git tags unavailable in this checkout")
        tags = set(listed.split())
        if not tags:  # pragma: no cover -- a tagless checkout (shallow clone, source tarball)
            pytest.skip("no tags in this checkout")
        missing = [
            version
            for version in changelog_section.known_versions(REAL_TEXT)
            if version not in ("Unreleased", "4.0.0") and f"v{version}" not in tags
        ]
        assert missing == []

    def test_no_version_has_two_sections(self):
        # The state the 4.0.0 release PR must not land in. Caught here, in under a second, rather
        # than on tag day by a release whose body is half the notes and whose build is green.
        versions = changelog_section.known_versions(REAL_TEXT)
        duplicated = sorted({v for v in versions if versions.count(v) > 1})
        assert duplicated == [], f"CHANGELOG.md has more than one section for: {duplicated}"

    def test_every_section_has_content(self):
        for version in changelog_section.known_versions(REAL_TEXT):
            if version == "Unreleased":
                continue  # permanently empty between releases, by design
            assert changelog_section.section(REAL_TEXT, version)
