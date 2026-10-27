"""whats_new: changelog parsing, version selection and the per-person seen state (ADR 0238).

The root conftest's autouse ``_isolate_whats_new_state`` sends ``state_path`` into ``tmp_path``;
``REAL_STATE_PATH`` is captured at collection, before that fixture patches anything.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from privacyfence import paths, secure_files, update_checker, whats_new
from privacyfence.principal import Principal
from privacyfence.whats_new import ReleaseNotes
from privacyfence.whats_new import state_path as REAL_STATE_PATH

pytestmark = pytest.mark.unit

ALICE = Principal(id="alice", email="alice@example.com")

CHANGELOG = """\
# Changelog

<!-- Rules for this file.
## [9.9.9] — 2099-01-01
- hidden inside a comment
-->

## [Unreleased]

### Added

- **Pending thing.** Not released yet.

## [6.10.0] — 2026-10-30

### Fixed

- **Ten.** The tenth fix
  wraps onto a second line.

## [6.9.1] — 2026-10-26

### Fixed

- **Nine.** The ninth fix
  wraps onto a second line.

## [6.8.2] — 2026-10-25

### Fixed

- **Eight.** The eighth fix
  wraps onto a second line.

## [foo]

- not a version

## [6.7.0]

- **Seven.** No date on this one.

[Unreleased]: https://example.com/compare/v6.10.0...HEAD
[6.10.0]: https://example.com/releases/v6.10.0

## [6.6.0] — 2026-01-01

- after the link definitions, so not a section
"""


@pytest.fixture
def sections() -> tuple[ReleaseNotes, ...]:
    return tuple(whats_new.parse_changelog(CHANGELOG))


def versions(notes: list[ReleaseNotes]) -> list[str]:
    return [n.version for n in notes]


def test_parse_ignores_html_comments():
    assert "9.9.9" not in versions(whats_new.parse_changelog(CHANGELOG))


def test_parse_reads_versions_dates_and_order(sections):
    assert [(s.version, s.date) for s in sections] == [
        ("Unreleased", ""),
        ("6.10.0", "2026-10-30"),
        ("6.9.1", "2026-10-26"),
        ("6.8.2", "2026-10-25"),
        ("6.7.0", ""),
    ]


def test_parse_joins_continuation_lines(sections):
    body = sections[2].body
    assert "- **Nine.** The ninth fix wraps onto a second line." in body
    assert body.startswith("### Fixed")
    assert not body.endswith("\n")


def test_parse_excludes_link_definitions_and_what_follows(sections):
    assert all("example.com" not in s.body for s in sections)
    assert "6.6.0" not in versions(list(sections))


def test_parse_drops_unreleased_holding_only_empty_headings():
    text = "## [Unreleased]\n\n### Added\n\n### Fixed\n\n## [1.0.0] — 2026-01-01\n\n- **One.** Thing.\n"
    assert versions(whats_new.parse_changelog(text)) == ["1.0.0"]


def test_parse_drops_sections_that_are_not_versions(sections):
    assert "foo" not in versions(list(sections))


def test_parse_gives_an_undated_heading_an_empty_date(sections):
    assert sections[-1] == ReleaseNotes("6.7.0", "", "- **Seven.** No date on this one.")


def test_parse_ends_a_section_at_a_plain_level_two_heading():
    text = "## [1.0.0] — 2026-01-01\n\n- **One.** Thing.\n\n## Not versioned\n\n- stray\n"
    notes = whats_new.parse_changelog(text)
    assert [(n.version, n.body) for n in notes] == [("1.0.0", "- **One.** Thing.")]


def test_notes_since_returns_the_sections_after_last_seen(sections):
    assert versions(whats_new.notes_since(sections, "6.8.2", "6.10.0")) == ["6.10.0", "6.9.1"]


def test_notes_since_stops_at_the_running_version(sections):
    assert versions(whats_new.notes_since(sections, "6.8.2", "6.9.1")) == ["6.9.1"]


def test_notes_since_without_last_seen_gives_every_stable_section_up_to_running(sections):
    assert versions(whats_new.notes_since(sections, None, "6.9.1")) == ["6.9.1", "6.8.2", "6.7.0"]


def test_notes_since_puts_unreleased_first_for_a_prerelease(sections):
    assert versions(whats_new.notes_since(sections, "6.8.2", "6.11.0rc1")) == ["Unreleased", "6.10.0", "6.9.1"]


def test_notes_since_prerelease_without_an_unreleased_section(sections):
    no_unreleased = [s for s in sections if s.version != "Unreleased"]
    assert versions(whats_new.notes_since(no_unreleased, "6.9.1", "6.11.0rc1")) == ["6.10.0"]


def test_notes_since_never_includes_unreleased_for_a_stable_running_version(sections):
    assert "Unreleased" not in versions(whats_new.notes_since(sections, None, "6.10.0"))


def test_notes_since_an_unparseable_running_version_gives_nothing(sections):
    assert whats_new.notes_since(sections, "6.8.2", "garbage") == []


@pytest.mark.parametrize("version", ["6.10.0rc1", "6.10.0b2", "6.10.1.dev3+gabc"])
def test_is_prerelease_true(version):
    assert whats_new.is_prerelease(version) is True


@pytest.mark.parametrize("version", ["6.10.0", "garbage"])
def test_is_prerelease_false(version):
    assert whats_new.is_prerelease(version) is False


def write_state(tmp_path: Path, text: str) -> None:
    path = whats_new.state_path(ALICE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_read_last_seen_missing_file_is_none():
    assert whats_new.read_last_seen(ALICE) is None


def test_read_last_seen_returns_the_stored_version(tmp_path):
    write_state(tmp_path, json.dumps({"last_seen_version": "6.9.1"}))
    assert whats_new.read_last_seen(ALICE) == "6.9.1"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"last_seen_version": 3}'])
def test_read_last_seen_unusable_state_is_none_and_warns(tmp_path, caplog, content):
    write_state(tmp_path, content)
    with caplog.at_level(logging.WARNING, logger="privacyfence.whats_new"):
        assert whats_new.read_last_seen(ALICE) is None
    assert len(caplog.records) == 1
    assert content not in caplog.text


def test_read_last_seen_unreadable_file_is_none_and_warns(tmp_path, caplog):
    path = whats_new.state_path(ALICE)
    path.mkdir(parents=True)  # reading a directory raises OSError
    with caplog.at_level(logging.WARNING, logger="privacyfence.whats_new"):
        assert whats_new.read_last_seen(ALICE) is None
    assert "OSError" in caplog.text or "Error" in caplog.text


def test_read_last_seen_unsafe_principal_is_none_and_warns(monkeypatch, caplog):
    def boom(principal):
        raise ValueError("Unsafe principal id")

    monkeypatch.setattr(whats_new, "state_path", boom)
    with caplog.at_level(logging.WARNING, logger="privacyfence.whats_new"):
        assert whats_new.read_last_seen(ALICE) is None
    assert "ValueError" in caplog.text


def test_mark_seen_writes_the_file_and_creates_its_directory():
    path = whats_new.state_path(ALICE)
    assert not path.parent.exists()
    whats_new.mark_seen(ALICE, "6.10.0")
    assert json.loads(path.read_text(encoding="utf-8")) == {"last_seen_version": "6.10.0"}


def test_mark_seen_never_moves_backwards():
    whats_new.mark_seen(ALICE, "6.10.0")
    whats_new.mark_seen(ALICE, "6.9.1")
    assert whats_new.read_last_seen(ALICE) == "6.10.0"


def test_mark_seen_moves_forwards():
    whats_new.mark_seen(ALICE, "6.9.1")
    whats_new.mark_seen(ALICE, "6.10.0")
    assert whats_new.read_last_seen(ALICE) == "6.10.0"


def test_mark_seen_ignores_an_unparseable_version():
    whats_new.mark_seen(ALICE, "garbage")
    assert not whats_new.state_path(ALICE).exists()


def test_mark_seen_logs_and_swallows_a_write_failure(monkeypatch, caplog):
    def boom(path, data, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(secure_files, "atomic_write_json", boom)
    with caplog.at_level(logging.WARNING, logger="privacyfence.whats_new"):
        whats_new.mark_seen(ALICE, "6.10.0")
    assert "alice" in caplog.text and "OSError" in caplog.text
    assert "disk full" not in caplog.text


def test_state_path_is_under_the_principals_user_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(whats_new, "state_path", REAL_STATE_PATH)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    assert whats_new.state_path(ALICE) == tmp_path / "users" / "alice" / "whats_new.json"


def stub_changelog(monkeypatch, notes: tuple[ReleaseNotes, ...]) -> None:
    """Replace load_changelog. conftest's _reset() calls .cache_clear() on teardown, while the
    monkeypatch is still active, so the stub carries a no-op one."""

    def load() -> tuple[ReleaseNotes, ...]:
        return notes

    load.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(whats_new, "load_changelog", load)


@pytest.fixture
def notes_for_6_10(monkeypatch):
    monkeypatch.setattr(whats_new, "running_version", lambda: "6.10.0")
    stub_changelog(monkeypatch, (ReleaseNotes("6.10.0", "2026-10-30", "- **Ten.** x"),))


def test_pending_version_first_use_records_running_and_announces_nothing(notes_for_6_10):
    assert whats_new.pending_version(ALICE) == ""
    assert whats_new.read_last_seen(ALICE) == "6.10.0"


def test_pending_version_same_version_announces_nothing(notes_for_6_10):
    whats_new.mark_seen(ALICE, "6.10.0")
    assert whats_new.pending_version(ALICE) == ""


def test_pending_version_after_an_upgrade_announces_running_and_does_not_write(notes_for_6_10):
    whats_new.mark_seen(ALICE, "6.9.1")
    assert whats_new.pending_version(ALICE) == "6.10.0"
    assert whats_new.read_last_seen(ALICE) == "6.9.1"


def test_pending_version_without_a_changelog_announces_nothing_and_leaves_the_state(monkeypatch):
    monkeypatch.setattr(whats_new, "running_version", lambda: "6.10.0")
    stub_changelog(monkeypatch, ())
    whats_new.mark_seen(ALICE, "6.9.1")
    assert whats_new.pending_version(ALICE) == ""
    assert whats_new.read_last_seen(ALICE) == "6.9.1"


def test_changelog_path_prefers_the_bundled_copy(monkeypatch, tmp_path):
    package, repo = tmp_path / "pkg", tmp_path / "repo"
    (package / "resources").mkdir(parents=True)
    (package / "resources" / "CHANGELOG.md").write_text("bundled", encoding="utf-8")
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text("root", encoding="utf-8")
    monkeypatch.setattr(whats_new, "_PACKAGE_DIR", package)
    monkeypatch.setattr(whats_new, "_REPO_ROOT", repo)
    assert whats_new.changelog_path() == package / "resources" / "CHANGELOG.md"


def test_changelog_path_falls_back_to_the_repository_root(monkeypatch, tmp_path):
    package, repo = tmp_path / "pkg", tmp_path / "repo"
    package.mkdir()
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text("## [1.0.0] — 2026-01-01\n\n- **One.** x\n", encoding="utf-8")
    monkeypatch.setattr(whats_new, "_PACKAGE_DIR", package)
    monkeypatch.setattr(whats_new, "_REPO_ROOT", repo)
    assert whats_new.changelog_path() == repo / "CHANGELOG.md"
    assert versions(list(whats_new.load_changelog())) == ["1.0.0"]


def test_without_any_changelog_the_path_is_none_and_the_notes_are_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(whats_new, "_PACKAGE_DIR", tmp_path / "pkg")
    monkeypatch.setattr(whats_new, "_REPO_ROOT", tmp_path / "repo")
    assert whats_new.changelog_path() is None
    assert whats_new.load_changelog() == ()


def test_load_changelog_logs_and_returns_nothing_when_the_read_fails(monkeypatch, tmp_path, caplog):
    target = tmp_path / "CHANGELOG.md"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(whats_new, "changelog_path", lambda: target)
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == target:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with caplog.at_level(logging.WARNING, logger="privacyfence.whats_new"):
        assert whats_new.load_changelog() == ()
    assert "PermissionError" in caplog.text


def test_load_changelog_parses_the_repositorys_changelog():
    parsed = whats_new.load_changelog()
    first_released = next(s for s in parsed if s.version != whats_new.UNRELEASED)
    assert update_checker.parse_version(first_released.version) is not None


def test_running_version_is_the_package_version():
    import privacyfence

    assert whats_new.running_version() == privacyfence.__version__
