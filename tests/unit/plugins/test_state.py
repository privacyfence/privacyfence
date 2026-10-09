"""Unit tests for privacyfence.plugins.state."""
from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from privacyfence.plugins.state import (
    HASH_DRIFT_REASON,
    STATE_FILENAME,
    PluginRecord,
    PluginStateStore,
)
from privacyfence.plugins.trust import DiscoveredPlugin

SIGS = [("today_list", "review", True, False, ("calendar",)), ("today_note", "popup", False, False, ())]


def _store(tmp_path: Path) -> PluginStateStore:
    return PluginStateStore(tmp_path / STATE_FILENAME, clock=lambda: "2026-10-07T10:00:00+00:00")


def _enable(store: PluginStateStore, name: str = "today", **over) -> PluginRecord:
    kwargs = {
        "version": "1.2.0", "executable_sha256": "e" * 64, "manifest_sha256": "m" * 64,
        "max_gate_floor": "auto", "reviewed_tools": SIGS, **over,
    }
    return store.enable(name, **kwargs)


def _found(name: str = "today", exe: str = "e" * 64, man: str = "m" * 64) -> DiscoveredPlugin:
    return DiscoveredPlugin(name, Path("/x") / name, None, None, exe, man)


class TestRoundTrip:
    def test_missing_file_is_empty(self, tmp_path):
        assert _store(tmp_path).load() == {}

    def test_enable_then_load(self, tmp_path):
        store = _store(tmp_path)
        record = _enable(store)

        loaded = _store(tmp_path).load()

        assert loaded == {"today": record}
        assert record.enabled is True
        assert record.enabled_at == "2026-10-07T10:00:00+00:00"
        assert record.disabled_reason is None
        assert record.reviewed == frozenset(SIGS)

    def test_file_shape(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        data = json.loads(store.path.read_text(encoding="utf-8"))

        assert data == {"version": 1, "plugins": {"today": {
            "enabled": True, "version": "1.2.0", "executable_sha256": "e" * 64,
            "manifest_sha256": "m" * 64, "max_gate_floor": "auto",
            "enabled_at": "2026-10-07T10:00:00+00:00", "disabled_reason": None,
            "reviewed_tools": [["today_list", "review", True, False, ["calendar"]],
                               ["today_note", "popup", False, False, []]],
        }}}

    def test_reviewed_tools_accept_lists(self, tmp_path):
        store = _store(tmp_path)

        record = _enable(store, reviewed_tools=[["a_b", "auto", True, False, ["x"]]])

        assert record.reviewed_tools == (("a_b", "auto", True, False, ("x",)),)

    def test_default_clock_is_utc_iso(self, tmp_path):
        store = PluginStateStore(tmp_path / STATE_FILENAME)

        record = _enable(store)

        assert record.enabled_at.endswith("+00:00")

    def test_disable_keeps_the_record(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        store.disable("today", "disabled by you")

        record = store.load()["today"]
        assert record.enabled is False
        assert record.disabled_reason == "disabled by you"
        assert record.executable_sha256 == "e" * 64
        assert record.reviewed == frozenset(SIGS)

    def test_disable_unknown_is_a_no_op(self, tmp_path):
        store = _store(tmp_path)

        store.disable("today", "disabled by you")

        assert not store.path.exists()

    def test_enable_again_clears_the_reason(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)
        store.disable("today", "disabled by you")

        _enable(store, version="1.3.0")

        record = store.load()["today"]
        assert record.enabled is True and record.disabled_reason is None and record.version == "1.3.0"

    def test_forget(self, tmp_path):
        store = _store(tmp_path)
        _enable(store, "today")
        _enable(store, "other")

        store.forget("today")
        store.forget("never-there")

        assert set(store.load()) == {"other"}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_mode_0600(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


class TestCorruptFile:
    @pytest.mark.parametrize("text", [
        "{not json",
        "[]",
        '{"version": 2, "plugins": {}}',
        '{"version": 1}',
        '{"version": 1, "plugins": {"today": []}}',
        '{"version": 1, "plugins": {"today": {"enabled": true}}}',
        json.dumps({"version": 1, "plugins": {"today": {
            "enabled": "yes", "version": "1", "executable_sha256": "", "manifest_sha256": "",
            "max_gate_floor": "review", "enabled_at": "", "disabled_reason": None,
            "reviewed_tools": []}}}),
        json.dumps({"version": 1, "plugins": {"today": {
            "enabled": True, "version": "1", "executable_sha256": "", "manifest_sha256": "",
            "max_gate_floor": "review", "enabled_at": "", "disabled_reason": 3,
            "reviewed_tools": []}}}),
        json.dumps({"version": 1, "plugins": {"today": {
            "enabled": True, "version": "1", "executable_sha256": "", "manifest_sha256": "",
            "max_gate_floor": "review", "enabled_at": "", "disabled_reason": None,
            "reviewed_tools": [["a", "auto", True]]}}}),
        json.dumps({"version": 1, "plugins": {"today": {
            "enabled": True, "version": "1", "executable_sha256": "", "manifest_sha256": "",
            "max_gate_floor": "review", "enabled_at": "", "disabled_reason": None,
            "reviewed_tools": [["a", "auto", "yes", False, []]]}}}),
    ])
    def test_corrupt_file_reads_as_empty(self, tmp_path, caplog, text):
        store = _store(tmp_path)
        store.path.write_text(text, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="privacyfence.plugins.state"):
            assert store.load() == {}

        assert "corrupt" in caplog.text

    def test_unreadable_file_reads_as_empty(self, tmp_path, caplog):
        store = _store(tmp_path)
        store.path.write_bytes(b"\xff\xfe\x00")

        with caplog.at_level(logging.WARNING, logger="privacyfence.plugins.state"):
            assert store.load() == {}

        assert "Could not read" in caplog.text

    def test_enable_replaces_a_corrupt_file(self, tmp_path):
        store = _store(tmp_path)
        store.path.write_text("{not json", encoding="utf-8")

        _enable(store)

        assert set(store.load()) == {"today"}


class TestHashDrift:
    def test_changed_hash_disables(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        reason = store.check_hashes(_found(exe="f" * 64))

        assert reason == "executable or manifest changed, enable again" == HASH_DRIFT_REASON
        record = store.load()["today"]
        assert record.enabled is False
        assert record.disabled_reason == HASH_DRIFT_REASON

    def test_changed_manifest_disables(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        assert store.check_hashes(_found(man="n" * 64)) == HASH_DRIFT_REASON
        assert store.load()["today"].enabled is False

    def test_unreadable_file_counts_as_changed(self, tmp_path):
        store = _store(tmp_path)
        _enable(store, executable_sha256="")

        assert store.check_hashes(_found(exe="")) == HASH_DRIFT_REASON

    def test_unchanged_is_fine(self, tmp_path):
        store = _store(tmp_path)
        _enable(store)

        assert store.check_hashes(_found()) is None
        assert store.load()["today"].enabled is True

    def test_unknown_or_disabled_plugin_is_left_alone(self, tmp_path):
        store = _store(tmp_path)

        assert store.check_hashes(_found()) is None

        _enable(store)
        store.disable("today", "disabled by you")

        assert store.check_hashes(_found(exe="f" * 64)) is None
        assert store.load()["today"].disabled_reason == "disabled by you"
