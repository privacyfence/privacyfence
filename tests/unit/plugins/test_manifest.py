"""Unit tests for privacyfence.plugins.manifest."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from privacyfence.plugins import manifest as manifest_mod
from privacyfence.plugins.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    resolve_command,
)

VALID = {
    "name": "today", "display_name": "Today", "version": "1.2.0", "protocol": "1",
    "command": ["today-plugin"], "source_operations": ["calendar.list_events"], "tools": "dynamic",
    "max_gate_floor": "auto", "pages": True, "service_credentials": False,
}


def _write(tmp_path: Path, data, name: str = "today") -> Path:
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(exist_ok=True)
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    (plugin_dir / MANIFEST_FILENAME).write_text(text, encoding="utf-8")
    return plugin_dir


def _with(**over):
    data = dict(VALID)
    for k, v in over.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return data


class TestLoad:
    def test_valid(self, tmp_path):
        m = load_manifest(_write(tmp_path, VALID))
        assert m == Manifest(
            name="today", display_name="Today", version="1.2.0", protocol="1",
            command=("today-plugin",), source_operations=frozenset({"calendar.list_events"}),
            max_gate_floor="auto", pages=True, service_credentials=False)

    def test_defaults(self, tmp_path):
        data = {k: VALID[k] for k in ("name", "display_name", "version", "protocol", "command", "tools")}
        m = load_manifest(_write(tmp_path, data))
        assert (m.source_operations, m.max_gate_floor, m.pages, m.service_credentials) == (
            frozenset(), "review", False, False)

    def test_prerelease_version(self, tmp_path):
        assert load_manifest(_write(tmp_path, _with(version="1.0.0-rc.1"))).version == "1.0.0-rc.1"

    def test_missing_file(self, tmp_path):
        (tmp_path / "today").mkdir()
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path / "today")

    def test_unreadable(self, tmp_path):
        d = _write(tmp_path, VALID)
        (d / MANIFEST_FILENAME).write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(ManifestError, match="cannot be read"):
            load_manifest(d)

    def test_too_large(self, tmp_path):
        with pytest.raises(ManifestError, match="larger than"):
            load_manifest(_write(tmp_path, "# " + "x" * 70_000))

    def test_invalid_yaml(self, tmp_path):
        with pytest.raises(ManifestError, match="not valid YAML"):
            load_manifest(_write(tmp_path, "a: [unclosed"))

    def test_python_object_tag_refused(self, tmp_path):
        with pytest.raises(ManifestError):
            load_manifest(_write(tmp_path, "name: !!python/object/apply:os.system ['true']"))

    @pytest.mark.parametrize("text", ["", "- a\n- b", "just a string"])
    def test_not_a_mapping(self, tmp_path, text):
        with pytest.raises(ManifestError, match="mapping"):
            load_manifest(_write(tmp_path, text))

    def test_unknown_key(self, tmp_path):
        with pytest.raises(ManifestError, match="unknown key 'extra'"):
            load_manifest(_write(tmp_path, _with(extra=1)))

    @pytest.mark.parametrize("key", ["name", "display_name", "version", "protocol", "command", "tools"])
    def test_required_key_missing(self, tmp_path, key):
        with pytest.raises(ManifestError, match=f"{key} is required"):
            load_manifest(_write(tmp_path, _with(**{key: None})))

    @pytest.mark.parametrize("over, match", [
        ({"name": 5}, "name must be a string"),
        ({"name": "Today"}, "does not match"),
        ({"name": "t"}, "does not match"),
        ({"name": "a_b"}, "does not match"),
        ({"name": "slack"}, "reserved"),
        ({"name": "gmail"}, "reserved"),
        ({"display_name": ""}, "1 to 60"),
        ({"display_name": "x" * 61}, "1 to 60"),
        ({"display_name": 3}, "display_name must be a string"),
        ({"display_name": "To\u202eday"}, "control or bidirectional"),
        ({"display_name": "To\x07day"}, "control or bidirectional"),
        ({"version": "1.2"}, "MAJOR.MINOR.PATCH"),
        ({"version": "v1.2.3"}, "MAJOR.MINOR.PATCH"),
        ({"version": "01.2.3"}, "MAJOR.MINOR.PATCH"),
        ({"version": 1.2}, "version must be a string"),
        ({"protocol": 1}, "protocol must be a string"),
        ({"protocol": "1.0"}, "major version"),
        ({"protocol": "x"}, "major version"),
        ({"command": []}, "non-empty list"),
        ({"command": "today-plugin"}, "non-empty list"),
        ({"command": ["a", 3]}, "non-empty list"),
        ({"command": [""]}, "non-empty list"),
        ({"source_operations": "calendar.list_events"}, "list of strings"),
        ({"source_operations": [3]}, "list of strings"),
        ({"source_operations": ["gmail.send"]}, "not supported"),
        ({"tools": ["a"]}, "dynamic"),
        ({"tools": "static"}, "dynamic"),
        ({"max_gate_floor": "popup"}, "max_gate_floor"),
        ({"pages": "yes"}, "pages must be true or false"),
        ({"service_credentials": 1}, "service_credentials must be true or false"),
    ])
    def test_invalid_field(self, tmp_path, over, match):
        with pytest.raises(ManifestError, match=match):
            load_manifest(_write(tmp_path, _with(**over)))

    def test_name_must_equal_directory(self, tmp_path):
        with pytest.raises(ManifestError, match="directory name 'other'"):
            load_manifest(_write(tmp_path, VALID, name="other"))

    def test_service_credentials_rejected_in_local_mode(self, tmp_path):
        d = _write(tmp_path, _with(service_credentials=True))
        with pytest.raises(ManifestError, match="local mode"):
            load_manifest(d)
        with pytest.raises(ManifestError, match="local mode"):
            load_manifest(d, mode="local")

    def test_service_credentials_allowed_in_org_mode(self, tmp_path):
        d = _write(tmp_path, _with(service_credentials=True))
        assert load_manifest(d, mode="org").service_credentials is True


class TestResolveCommand:
    def _manifest(self, *command):
        return Manifest("today", "Today", "1.0.0", "1", tuple(command), frozenset(), "review",
                        False, False)

    def test_inside_dir(self, tmp_path):
        exe = tmp_path / "bin" / "run"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        argv = resolve_command(self._manifest("bin/run", "--flag", "x"), tmp_path)
        assert argv == [str(exe.resolve()), "--flag", "x"]

    def test_dotdot_escape_refused(self, tmp_path):
        d = tmp_path / "today"
        d.mkdir()
        (tmp_path / "evil").write_text("x")
        with pytest.raises(ManifestError, match="outside"):
            resolve_command(self._manifest("../evil"), d)

    def test_sibling_with_shared_prefix_refused(self, tmp_path):
        d = tmp_path / "today"
        d.mkdir()
        (tmp_path / "today-evil").mkdir()
        with pytest.raises(ManifestError, match="outside"):
            resolve_command(self._manifest("../today-evil/run"), d)

    def test_absolute_path_outside_refused(self, tmp_path):
        d = tmp_path / "today"
        d.mkdir()
        with pytest.raises(ManifestError, match="outside"):
            resolve_command(self._manifest(str(tmp_path / "x")), d)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="os.symlink unavailable")
    def test_symlink_out_refused(self, tmp_path):
        d = tmp_path / "today"
        d.mkdir()
        target = tmp_path / "outside"
        target.write_text("x")
        try:
            (d / "run").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks here")
        with pytest.raises(ManifestError, match="outside"):
            resolve_command(self._manifest("run"), d)

    def test_command_equal_to_directory_is_accepted_by_the_check(self, tmp_path):
        assert resolve_command(self._manifest("."), tmp_path)[0] == str(tmp_path.resolve())

    def test_windows_appends_exe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "win32")
        argv = resolve_command(self._manifest("today-plugin"), tmp_path)
        assert Path(argv[0]).name == "today-plugin.exe"

    def test_windows_keeps_an_existing_name_without_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "win32")
        (tmp_path / "run").write_text("x")
        (tmp_path / "run.exe").write_text("x")
        assert resolve_command(self._manifest("run"), tmp_path)[0] == str((tmp_path / "run").resolve())

    def test_windows_never_suffixes_the_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "win32")
        assert resolve_command(self._manifest("."), tmp_path)[0] == str(tmp_path.resolve())

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="os.symlink unavailable")
    def test_windows_symlink_out_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "win32")
        d = tmp_path / "today"
        d.mkdir()
        target = tmp_path / "outside"
        target.write_text("x")
        try:
            (d / "run").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks here")
        with pytest.raises(ManifestError, match="outside"):
            resolve_command(self._manifest("run"), d)

    def test_windows_keeps_existing_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "win32")
        argv = resolve_command(self._manifest("run.cmd"), tmp_path)
        assert Path(argv[0]).name == "run.cmd"

    def test_posix_never_appends_exe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod.sys, "platform", "linux")
        assert Path(resolve_command(self._manifest("run"), tmp_path)[0]).name == "run"
