"""The Drive download spool: ranged chunks, native exports, revisions and the idle sweep."""
from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest

from privacyfence.plugins.constants import DRIVE_CHUNK_BYTES, DRIVE_SPOOL_IDLE_SECONDS
from privacyfence.plugins.protocol import RpcError
from privacyfence.plugins.spool import DownloadSpool

MIB = 1024 * 1024
NATIVE = "application/vnd.google-apps.document"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def spool(tmp_path, clock):
    return DownloadSpool(tmp_path / "spool", clock=clock)


def _files(root):
    return [p for p in root.rglob("*") if p.is_file()]


class RangeDrive:
    """A fake Drive that serves binary files by Range only and Google-native files as exports."""

    def __init__(self, data: bytes = b"", *, size: int | None = None, mime_type: str = "application/pdf",
                 revision: str = "2026-01-01T00:00:00Z", short: int = 0):
        self.data = data
        self.size = len(data) if size is None else size
        self.mime_type = mime_type
        self.revision = revision
        self.short = short
        self.ranges: list[tuple[int, int]] = []
        self.exports = 0

    def get_file_metadata(self, file_id):
        return SimpleNamespace(id=file_id, size=self.size, mime_type=self.mime_type, modified_time=self.revision)

    def download_range(self, file_id, offset, length):
        self.ranges.append((offset, length))
        return self.data[offset:offset + length - self.short]

    def download_file_bytes(self, file_id):
        self.exports += 1
        return {"data": self.data, "name": "x", "mime_type": "text/plain", "size_bytes": len(self.data)}


def _read_at(spool, drive, offset, length=DRIVE_CHUNK_BYTES, expected_revision=None):
    return spool.read_chunk_at(drive, "p", "f1", offset=offset, length=length, expected_revision=expected_revision)


class TestReadChunkAt:
    def test_binary_chunk_uses_a_range_call_and_writes_no_spool_file(self, spool, tmp_path):
        drive = RangeDrive(b"0123456789")

        data, next_offset, revision = _read_at(spool, drive, 2, 4)

        assert base64.b64decode(data["content_base64"]) == b"2345"
        assert (data["offset"], data["length"], data["total_size_bytes"], data["eof"]) == (2, 4, 10, False)
        assert data["mime_type"] == "application/pdf"
        assert (next_offset, revision) == (6, "2026-01-01T00:00:00Z")
        assert drive.ranges == [(2, 4)]
        assert drive.exports == 0
        assert _files(tmp_path / "spool") == []

    def test_last_chunk_is_eof_with_no_next_offset(self, spool):
        data, next_offset, _ = _read_at(spool, RangeDrive(b"0123456789"), 8, 100)
        assert base64.b64decode(data["content_base64"]) == b"89"
        assert data["eof"] is True
        assert next_offset is None

    def test_offset_at_the_end_is_an_empty_eof_chunk(self, spool):
        drive = RangeDrive(b"0123456789")
        data, next_offset, _ = _read_at(spool, drive, 10)
        assert (data["length"], data["eof"], next_offset) == (0, True, None)
        assert drive.ranges == []

    def test_a_huge_file_returns_its_last_chunk_with_only_range_calls(self, spool):
        size = 100 * MIB
        tail = b"x" * 1000
        drive = RangeDrive(size=size)
        drive.data = None

        def download_range(file_id, offset, length):
            drive.ranges.append((offset, length))
            return tail[:length]

        drive.download_range = download_range

        data, next_offset, _ = _read_at(spool, drive, size - 1000)

        assert data["length"] == 1000
        assert data["total_size_bytes"] == size
        assert next_offset is None
        assert drive.ranges == [(size - 1000, 1000)]
        assert drive.exports == 0

    def test_short_read_is_revision_changed(self, spool):
        with pytest.raises(RpcError) as excinfo:
            _read_at(spool, RangeDrive(b"0123456789", short=1), 0, 5)
        assert excinfo.value.code == "upstream_error"
        assert excinfo.value.extra == {"reason": "revision_changed"}

    def test_offset_past_the_end_is_invalid_params(self, spool):
        with pytest.raises(RpcError) as excinfo:
            _read_at(spool, RangeDrive(b"0123456789"), 11)
        assert excinfo.value.code == "invalid_params"

    def test_expected_revision_mismatch_is_revision_changed(self, spool):
        with pytest.raises(RpcError) as excinfo:
            _read_at(spool, RangeDrive(b"0123456789"), 0, expected_revision="2025-12-31T00:00:00Z")
        assert excinfo.value.extra == {"reason": "revision_changed"}

    def test_matching_expected_revision_is_served(self, spool):
        data, _, _ = _read_at(spool, RangeDrive(b"0123456789"), 0, 3, expected_revision="2026-01-01T00:00:00Z")
        assert data["length"] == 3

    def test_native_file_is_exported_once_spooled_and_swept(self, spool, clock, tmp_path):
        drive = RangeDrive(b"a" * 20, mime_type="application/vnd.google-apps.document")

        first, next_offset, _ = _read_at(spool, drive, 0, 8)
        second, _, _ = _read_at(spool, drive, next_offset, 8)

        assert base64.b64decode(second["content_base64"]) == b"a" * 8
        assert first["mime_type"] == "text/plain"
        assert first["total_size_bytes"] == 20
        assert drive.exports == 1
        assert drive.ranges == []
        assert len(_files(tmp_path / "spool")) == 1

        clock.now += DRIVE_SPOOL_IDLE_SECONDS + 1
        spool.sweep()
        assert _files(tmp_path / "spool") == []

    def test_native_export_has_no_size_cap(self, spool):
        drive = RangeDrive(b"a" * (64 * MIB + 1), mime_type="application/vnd.google-apps.spreadsheet")
        data, _, _ = _read_at(spool, drive, 0, 4)
        assert data["total_size_bytes"] == 64 * MIB + 1

    def test_native_offset_past_the_end_is_invalid_params(self, spool):
        drive = RangeDrive(b"abc", mime_type="application/vnd.google-apps.document")
        with pytest.raises(RpcError) as excinfo:
            _read_at(spool, drive, 4)
        assert excinfo.value.code == "invalid_params"

    def test_native_revision_mismatch_drops_the_spool(self, spool, tmp_path):
        drive = RangeDrive(b"abcdef", mime_type="application/vnd.google-apps.document")
        _read_at(spool, drive, 0, 2)
        drive.revision = "2026-02-02T00:00:00Z"
        with pytest.raises(RpcError) as excinfo:
            _read_at(spool, drive, 2, 2, expected_revision="2026-01-01T00:00:00Z")
        assert excinfo.value.extra == {"reason": "revision_changed"}
        assert _files(tmp_path / "spool") == []


class TestSpoolFiles:
    def test_spool_files_are_private_and_not_named_after_the_drive_file(self, spool, tmp_path):
        spool.read_chunk_at(
            RangeDrive(b"abc", mime_type=NATIVE), "p", "secret-name", offset=0, length=1, expected_revision=None
        )
        (path,) = _files(tmp_path / "spool")
        assert "secret-name" not in str(path)
        assert path.parent.name == "p"
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0

    def test_a_vanished_spool_file_is_fetched_again(self, spool, tmp_path):
        drive = RangeDrive(b"abc", mime_type=NATIVE)
        _read_at(spool, drive, 0, 1)
        (path,) = _files(tmp_path / "spool")
        path.unlink()
        data, _, _ = _read_at(spool, drive, 1)
        assert base64.b64decode(data["content_base64"]) == b"bc"
        assert drive.exports == 2

    def test_a_fresh_read_after_an_edit_replaces_the_old_spool(self, spool, tmp_path):
        drive = RangeDrive(b"old", mime_type=NATIVE)
        _read_at(spool, drive, 0)
        drive.data, drive.revision = b"new!", "later"
        data, _, _ = _read_at(spool, drive, 0)
        assert base64.b64decode(data["content_base64"]) == b"new!"
        assert len(_files(tmp_path / "spool")) == 1

    def test_a_removal_failure_is_not_fatal(self, spool, clock, monkeypatch):
        _read_at(spool, RangeDrive(b"abc", mime_type=NATIVE), 0)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS + 1

        def refuse(self, missing_ok=False):
            raise OSError("busy")

        monkeypatch.setattr("pathlib.Path.unlink", refuse)
        spool.sweep()


class TestIdleSweep:
    def test_spool_removed_after_idle(self, spool, clock, tmp_path):
        _read_at(spool, RangeDrive(b"abc", mime_type=NATIVE), 0, 1)
        assert len(_files(tmp_path / "spool")) == 1
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        spool.sweep()
        assert len(_files(tmp_path / "spool")) == 1
        clock.now += 2
        spool.sweep()
        assert _files(tmp_path / "spool") == []

    def test_reading_keeps_the_file_alive(self, spool, clock, tmp_path):
        drive = RangeDrive(b"abc", mime_type=NATIVE)
        _read_at(spool, drive, 0, 1)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        _read_at(spool, drive, 1, 1)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        spool.sweep()
        assert len(_files(tmp_path / "spool")) == 1

    def test_clear_removes_everything(self, spool, tmp_path):
        spool.read_chunk_at(
            RangeDrive(b"abc", mime_type=NATIVE), "p", "f1", offset=0, length=3, expected_revision=None
        )
        spool.read_chunk_at(
            RangeDrive(b"abc", mime_type=NATIVE), "q", "f2", offset=0, length=3, expected_revision=None
        )
        spool.clear()
        assert _files(tmp_path / "spool") == []

    def test_leftovers_of_a_previous_run_are_removed(self, tmp_path, clock):
        root = tmp_path / "spool"
        (root / "p").mkdir(parents=True)
        (root / "p" / "left").write_bytes(b"x")
        (root / "stray").write_bytes(b"x")
        DownloadSpool(root, clock=clock)
        assert _files(root) == []
