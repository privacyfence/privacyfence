"""Unit tests for privacyfence.plugins.trust."""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

import pytest
import yaml

from privacyfence import privilege_separation
from privacyfence.plugins import trust
from privacyfence.plugins.manifest import MANIFEST_FILENAME

MANIFEST = {
    "name": "today", "display_name": "Today", "version": "1.2.0", "protocol": "1",
    "command": ["today-plugin"], "tools": "dynamic",
}
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def _no_problem(plugin_dir: Path, executable: Path) -> None:
    return None


def _install(root: Path, name: str = "today", **over) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    data = {**MANIFEST, "name": name, **over}
    (plugin_dir / MANIFEST_FILENAME).write_text(yaml.safe_dump(data), encoding="utf-8")
    (plugin_dir / f"today-plugin{_EXE_SUFFIX}").write_bytes(b"binary " + name.encode())
    return plugin_dir


class TestLocation:
    def test_linux(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")

        assert trust.plugins_dir() == Path("/usr/local/lib/privacyfence/plugins")

    def test_macos(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")

        assert trust.plugins_dir() == Path("/Library/PrivacyFence/plugins")

    def test_windows_uses_program_files(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setenv("ProgramFiles", "D:\\Programs")

        assert trust.plugins_dir() == Path("D:\\Programs") / "PrivacyFence Plugins"

    @pytest.mark.parametrize("value", [None, ""])
    def test_windows_falls_back_without_program_files(self, monkeypatch, value):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        if value is None:
            monkeypatch.delenv("ProgramFiles", raising=False)
        else:
            monkeypatch.setenv("ProgramFiles", value)

        assert trust.plugins_dir() == Path("C:\\Program Files") / "PrivacyFence Plugins"

    def test_no_environment_override(self, monkeypatch, tmp_path):
        # A configurable directory could point somewhere the user can write (ADR 0121).
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setenv("PRIVACYFENCE_PLUGINS_DIR", str(tmp_path))

        assert trust.plugins_dir() == Path("/usr/local/lib/privacyfence/plugins")

    def test_user_writable_executable_refused(self, monkeypatch, tmp_path, caplog):
        plugin_dir = _install(tmp_path / "plugins")
        exe = plugin_dir / f"today-plugin{_EXE_SUFFIX}"
        # tmp_path and its ancestors are not administrator-only, so the check is faked: only the
        # executable is user-writable here.
        monkeypatch.setattr(
            privilege_separation, "admin_only_write_problem",
            lambda path: f"{path} is owned by uid 501, not root" if path == exe else None,
        )

        assert trust.admin_only_problem(plugin_dir, exe) == f"{exe} is owned by uid 501, not root"
        with caplog.at_level(logging.WARNING, logger="privacyfence.plugins.trust"):
            [found] = trust.discover(tmp_path / "plugins")

        assert found.problem == "executable is writable by non-administrators"
        assert found.manifest is not None
        assert "owned by uid 501" in caplog.text

    def test_checks_executable_then_directories_up_to_the_root(self, monkeypatch, tmp_path):
        plugin_dir = tmp_path / "plugins" / "today"
        (plugin_dir / "bin").mkdir(parents=True)
        exe = plugin_dir / "bin" / "today-plugin"
        exe.write_bytes(b"x")
        seen: list[Path] = []
        monkeypatch.setattr(
            privilege_separation, "admin_only_write_problem", lambda path: seen.append(path),
        )

        assert trust.admin_only_problem(plugin_dir, exe) is None

        real_dir = plugin_dir.resolve()
        assert seen[:3] == [exe, real_dir / "bin", real_dir]
        assert seen[3:] == list(real_dir.parents)
        assert seen[-1] == Path(real_dir.anchor)

    def test_the_first_problem_wins(self, monkeypatch, tmp_path):
        plugin_dir = _install(tmp_path)
        exe = plugin_dir / f"today-plugin{_EXE_SUFFIX}"
        bad = {plugin_dir.resolve().parent, plugin_dir.resolve().parent.parent}
        monkeypatch.setattr(
            privilege_separation, "admin_only_write_problem",
            lambda path: f"bad {path}" if path in bad else None,
        )

        assert trust.admin_only_problem(plugin_dir, exe) == f"bad {plugin_dir.resolve().parent}"

    def test_a_writable_plugin_directory_is_refused(self, monkeypatch, tmp_path):
        plugin_dir = _install(tmp_path)
        exe = plugin_dir / f"today-plugin{_EXE_SUFFIX}"
        monkeypatch.setattr(
            privilege_separation, "admin_only_write_problem",
            lambda path: "writable dir" if path == plugin_dir.resolve() else None,
        )

        assert trust.admin_only_problem(plugin_dir, exe) == "writable dir"

    @pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
    def test_a_symlinked_plugin_directory_is_checked_where_it_really_is(self, monkeypatch, tmp_path):
        real = _install(tmp_path / "elsewhere")
        (tmp_path / "plugins").mkdir()
        link = tmp_path / "plugins" / "today"
        link.symlink_to(real, target_is_directory=True)
        seen: list[Path] = []
        monkeypatch.setattr(
            privilege_separation, "admin_only_write_problem", lambda path: seen.append(path),
        )

        trust.admin_only_problem(link, link / "today-plugin")

        assert real.resolve() in seen
        assert real.resolve().parent in seen


class TestDiscovery:
    def test_sorted_and_skips_hidden_entries_and_files(self, tmp_path):
        _install(tmp_path, "zeta")
        _install(tmp_path, "alpha")
        (tmp_path / ".staging").mkdir()
        (tmp_path / "README.txt").write_text("not a plugin", encoding="utf-8")

        found = trust.discover(tmp_path, trust_check=_no_problem)

        assert [p.dir_name for p in found] == ["alpha", "zeta"]
        alpha = found[0]
        assert alpha.path == tmp_path / "alpha"
        assert alpha.problem is None
        assert alpha.manifest is not None and alpha.manifest.name == "alpha"
        exe = tmp_path / "alpha" / f"today-plugin{_EXE_SUFFIX}"
        assert alpha.executable_sha256 == hashlib.sha256(exe.read_bytes()).hexdigest()
        assert alpha.manifest_sha256 == trust.sha256_file(tmp_path / "alpha" / MANIFEST_FILENAME)

    def test_empty_directory(self, tmp_path):
        assert trust.discover(tmp_path, trust_check=_no_problem) == []

    def test_unlistable_directory_raises(self, tmp_path):
        # The host must be able to tell "no plugins" from "could not look", so it never deletes
        # a plugin's data after a failed listing.
        with pytest.raises(OSError):
            trust.discover(tmp_path / "missing", trust_check=_no_problem)

    def test_invalid_manifest(self, tmp_path):
        plugin_dir = _install(tmp_path, display_name="")

        [found] = trust.discover(tmp_path, trust_check=_no_problem)

        assert found.problem == "manifest invalid: display_name must be 1 to 60 characters"
        assert found.manifest is None
        assert found.executable_sha256 == ""
        assert found.manifest_sha256 == trust.sha256_file(plugin_dir / MANIFEST_FILENAME)

    def test_missing_manifest(self, tmp_path):
        (tmp_path / "today").mkdir()

        [found] = trust.discover(tmp_path, trust_check=_no_problem)

        assert found.problem == f"manifest invalid: {MANIFEST_FILENAME} not found"
        assert found.manifest_sha256 == ""

    def test_command_outside_the_plugin_directory(self, tmp_path):
        _install(tmp_path, command=["../escape"])

        [found] = trust.discover(tmp_path, trust_check=_no_problem)

        assert found.problem == "manifest invalid: command[0] resolves outside the plugin directory"

    def test_protocol_major_mismatch(self, tmp_path):
        _install(tmp_path, protocol="2")

        [found] = trust.discover(tmp_path, trust_check=lambda *_: pytest.fail("not reached"))

        assert found.problem == "protocol major mismatch"
        assert found.manifest is not None

    def test_missing_executable_hashes_empty(self, tmp_path, caplog):
        plugin_dir = _install(tmp_path)
        os.remove(plugin_dir / f"today-plugin{_EXE_SUFFIX}")

        with caplog.at_level(logging.WARNING, logger="privacyfence.plugins.trust"):
            [found] = trust.discover(tmp_path, trust_check=_no_problem)

        assert found.executable_sha256 == ""
        assert "Could not hash" in caplog.text

    def test_trust_check_gets_the_plugin_directory_and_resolved_executable(self, tmp_path):
        plugin_dir = _install(tmp_path)
        calls = []

        trust.discover(tmp_path, trust_check=lambda d, e: calls.append((d, e)))

        assert calls == [(plugin_dir, (plugin_dir / f"today-plugin{_EXE_SUFFIX}").resolve())]


class TestHashes:
    def test_matches_hashlib_across_blocks(self, tmp_path):
        data = os.urandom(trust._HASH_BLOCK_BYTES * 2 + 17)
        path = tmp_path / "big"
        path.write_bytes(data)

        assert trust.sha256_file(path) == hashlib.sha256(data).hexdigest()

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty"
        path.write_bytes(b"")

        assert trust.sha256_file(path) == hashlib.sha256(b"").hexdigest()

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            trust.sha256_file(tmp_path / "missing")
