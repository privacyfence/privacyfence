"""Unit tests for privacyfence.plugins.storage."""
from __future__ import annotations

import stat
import sys

import pytest

from privacyfence import paths
from privacyfence.plugins import storage
from privacyfence.principal import LOCAL_PRINCIPAL, Principal

pytestmark = pytest.mark.unit

ALICE = Principal(id="alice")


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    return data


class TestPaths:
    def test_install_dir_is_under_the_data_root(self, root):
        assert storage.install_dir("today") == root / "plugin-data" / "today" / "shared"

    def test_local_principal_dir_is_distinct_from_the_install_dir(self, root):
        local = storage.principal_dir("today", LOCAL_PRINCIPAL)

        assert local == root / "plugin-data" / "today" / "user"
        assert local != storage.install_dir("today")

    def test_other_principal_has_its_own_root(self, root):
        assert storage.principal_dir("today", ALICE) == root / "users" / "alice" / "plugin-data" / "today" / "user"


class TestOutputDir:
    def test_output_dir_is_under_the_plugins_data_dir(self, root):
        assert storage.output_dir("today", LOCAL_PRINCIPAL) == root / "plugin-data" / "today" / "outputs"

    def test_other_principal_has_its_own_output_dir(self, root):
        assert storage.output_dir("today", ALICE) == root / "users" / "alice" / "plugin-data" / "today" / "outputs"

    def test_output_dir_is_distinct_from_the_other_two(self, root):
        dirs = {storage.install_dir("today"), storage.principal_dir("today", LOCAL_PRINCIPAL), storage.output_dir("today", LOCAL_PRINCIPAL)}
        assert len(dirs) == 3

    def test_remove_all_deletes_the_output_dir(self, root):
        out = storage.output_dir("today", ALICE)
        out.mkdir(parents=True)
        (out / "a.csv").write_text("x")

        storage.remove_all("today")

        assert not out.exists()


class TestEnsureDirs:
    def test_creates_the_install_dir_and_one_per_principal(self, root):
        shared, per_principal = storage.ensure_dirs("today", [LOCAL_PRINCIPAL, ALICE])

        assert shared.is_dir()
        assert set(per_principal) == {"local", "alice"}
        assert all(p.is_dir() for p in per_principal.values())
        assert per_principal["alice"] == storage.principal_dir("today", ALICE)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_directories_are_private(self, root):
        shared, per_principal = storage.ensure_dirs("today", [LOCAL_PRINCIPAL])

        assert stat.S_IMODE(shared.stat().st_mode) == 0o700
        assert stat.S_IMODE(per_principal["local"].stat().st_mode) == 0o700

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_loosened_directory_is_tightened(self, root):
        shared, _ = storage.ensure_dirs("today", [])
        shared.chmod(0o755)

        storage.ensure_dirs("today", [])

        assert stat.S_IMODE(shared.stat().st_mode) == 0o700

    def test_no_principals(self, root):
        shared, per_principal = storage.ensure_dirs("today", [])

        assert shared.is_dir() and per_principal == {}


class TestRemoveAll:
    def test_removes_install_local_and_every_principal(self, root):
        storage.ensure_dirs("today", [LOCAL_PRINCIPAL, ALICE])
        (storage.install_dir("today") / "f.txt").write_text("x")
        (storage.principal_dir("today", ALICE) / "g.txt").write_text("y")

        storage.remove_all("today")

        assert not (root / "plugin-data" / "today").exists()
        assert not (root / "users" / "alice" / "plugin-data" / "today").exists()
        assert (root / "users" / "alice").is_dir()

    def test_leaves_other_plugins_alone(self, root):
        storage.ensure_dirs("today", [LOCAL_PRINCIPAL, ALICE])
        storage.ensure_dirs("other", [LOCAL_PRINCIPAL, ALICE])

        storage.remove_all("today")

        assert storage.install_dir("other").is_dir()
        assert storage.principal_dir("other", ALICE).is_dir()

    def test_missing_is_fine(self, root):
        storage.remove_all("today")

        assert list(root.iterdir()) == []

    def test_a_plain_file_in_users_is_ignored(self, root):
        (root / "users").mkdir()
        (root / "users" / "stray.txt").write_text("x")
        storage.ensure_dirs("today", [LOCAL_PRINCIPAL])

        storage.remove_all("today")

        assert (root / "users" / "stray.txt").exists()
        assert not (root / "plugin-data" / "today").exists()
