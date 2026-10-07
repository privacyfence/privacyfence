"""The Drive download spool: chunks, cursors, revisions, idle sweep and the size cap."""
from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest

from privacyfence.plugins.constants import DRIVE_CHUNK_BYTES, DRIVE_MAX_FILE_BYTES, DRIVE_SPOOL_IDLE_SECONDS
from privacyfence.plugins.protocol import RpcError
from privacyfence.plugins.spool import DownloadSpool, decode_cursor, encode_cursor

MIB = 1024 * 1024


class FakeDrive:
    def __init__(self, data: bytes = b"", *, size: int | None = None, revision: str = "2026-01-01T00:00:00Z"):
        self.data = data
        self.size = len(data) if size is None else size
        self.revision = revision
        self.downloads = 0

    def get_file_metadata(self, file_id):
        return SimpleNamespace(id=file_id, size=self.size, modified_time=self.revision)

    def download_file_bytes(self, file_id):
        self.downloads += 1
        return {"data": self.data, "name": "x", "mime_type": "application/octet-stream", "size_bytes": len(self.data)}


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


class TestDriveChunks:
    def test_reassembles_ten_mb_file(self, spool):
        data = os.urandom(10 * MIB)
        drive = FakeDrive(data)
        first, cursor = spool.read_chunk(drive, "p", "f1", length=8 * MIB)
        assert first["eof"] is False and first["offset"] == 0 and first["length"] == 8 * MIB
        assert first["total_size_bytes"] == 10 * MIB and cursor
        second, cursor2 = spool.read_chunk(drive, "p", "f1", cursor=cursor, length=8 * MIB)
        assert second["eof"] is True and second["offset"] == 8 * MIB and second["length"] == 2 * MIB
        assert cursor2 is None
        joined = base64.b64decode(first["content_base64"]) + base64.b64decode(second["content_base64"])
        assert joined == data
        assert drive.downloads == 1

    def test_default_length_is_the_chunk_size(self, spool):
        drive = FakeDrive(b"a" * (DRIVE_CHUNK_BYTES + 5))
        first, cursor = spool.read_chunk(drive, "p", "f1")
        assert first["length"] == DRIVE_CHUNK_BYTES and cursor

    def test_offset_reads_the_tail(self, spool):
        drive = FakeDrive(b"0123456789")
        data, cursor = spool.read_chunk(drive, "p", "f1", offset=4, length=3)
        assert base64.b64decode(data["content_base64"]) == b"456"
        assert cursor is not None and data["mime_type"] == "application/octet-stream"
        assert decode_cursor(cursor) == ("f1", drive.revision, 7)

    def test_offset_at_the_end_is_an_empty_eof_chunk(self, spool):
        drive = FakeDrive(b"abc")
        data, cursor = spool.read_chunk(drive, "p", "f1", offset=3)
        assert data["eof"] is True and data["length"] == 0 and data["content_base64"] == ""
        assert cursor is None

    def test_offset_past_the_end_is_invalid(self, spool):
        with pytest.raises(RpcError) as err:
            spool.read_chunk(FakeDrive(b"abc"), "p", "f1", offset=4)
        assert err.value.code == "invalid_params"

    def test_offset_and_cursor_together_rejected(self, spool):
        with pytest.raises(RpcError) as err:
            spool.read_chunk(FakeDrive(b"abc"), "p", "f1", offset=1, cursor=encode_cursor("f1", "r", 1))
        assert err.value.code == "invalid_params"

    @pytest.mark.parametrize("cursor", ["!!!", base64.urlsafe_b64encode(b"[1]").decode(),
                                        base64.urlsafe_b64encode(b'{"f":"f1","r":"r","o":-1}').decode(),
                                        base64.urlsafe_b64encode(b'{"f":1,"r":"r","o":0}').decode()])
    def test_bad_cursor_is_invalid_params(self, spool, cursor):
        with pytest.raises(RpcError) as err:
            spool.read_chunk(FakeDrive(b"abc"), "p", "f1", cursor=cursor)
        assert err.value.code == "invalid_params"

    def test_cursor_for_another_file_is_invalid_params(self, spool):
        with pytest.raises(RpcError) as err:
            spool.read_chunk(FakeDrive(b"abc"), "p", "f1", cursor=encode_cursor("other", "r", 0))
        assert err.value.code == "invalid_params"

    def test_a_fresh_read_of_the_same_revision_reuses_the_spool(self, spool):
        drive = FakeDrive(b"abc")
        spool.read_chunk(drive, "p", "f1")
        spool.read_chunk(drive, "p", "f1")
        assert drive.downloads == 1

    def test_spool_files_are_private_and_not_named_after_the_drive_file(self, spool, tmp_path):
        spool.read_chunk(FakeDrive(b"abc"), "p", "secret-name", length=1)
        (path,) = _files(tmp_path / "spool")
        assert "secret-name" not in str(path)
        assert path.parent.name == "p"
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0

    def test_a_vanished_spool_file_is_fetched_again(self, spool, tmp_path):
        drive = FakeDrive(b"abc")
        spool.read_chunk(drive, "p", "f1", length=1)
        (path,) = _files(tmp_path / "spool")
        path.unlink()
        data, _ = spool.read_chunk(drive, "p", "f1", offset=1)
        assert base64.b64decode(data["content_base64"]) == b"bc"
        assert drive.downloads == 2

    def test_a_removal_failure_is_not_fatal(self, spool, clock, monkeypatch):
        spool.read_chunk(FakeDrive(b"abc"), "p", "f1")
        clock.now += DRIVE_SPOOL_IDLE_SECONDS + 1

        def refuse(self, missing_ok=False):
            raise OSError("busy")

        monkeypatch.setattr("pathlib.Path.unlink", refuse)
        spool.sweep()


class TestRevision:
    def test_revision_change_is_reported(self, spool, tmp_path):
        drive = FakeDrive(b"0123456789")
        _, cursor = spool.read_chunk(drive, "p", "f1", length=4)
        drive.revision = "2026-02-02T00:00:00Z"
        with pytest.raises(RpcError) as err:
            spool.read_chunk(drive, "p", "f1", cursor=cursor)
        assert err.value.code == "upstream_error"
        assert err.value.to_error()["data"]["reason"] == "revision_changed"
        assert _files(tmp_path / "spool") == []

    def test_revision_change_with_offset_is_reported(self, spool):
        drive = FakeDrive(b"0123456789")
        spool.read_chunk(drive, "p", "f1", length=4)
        drive.revision = "other"
        with pytest.raises(RpcError) as err:
            spool.read_chunk(drive, "p", "f1", offset=4)
        assert err.value.to_error()["data"]["reason"] == "revision_changed"

    def test_cursor_from_an_older_revision_after_a_sweep_is_reported(self, spool, clock):
        drive = FakeDrive(b"0123456789")
        _, cursor = spool.read_chunk(drive, "p", "f1", length=4)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS + 1
        drive.revision = "newer"
        with pytest.raises(RpcError) as err:
            spool.read_chunk(drive, "p", "f1", cursor=cursor)
        assert err.value.to_error()["data"]["reason"] == "revision_changed"

    def test_a_fresh_read_after_an_edit_replaces_the_old_spool(self, spool, tmp_path):
        drive = FakeDrive(b"old")
        spool.read_chunk(drive, "p", "f1")
        drive.data, drive.revision = b"new!", "later"
        data, _ = spool.read_chunk(drive, "p", "f1")
        assert base64.b64decode(data["content_base64"]) == b"new!"
        assert len(_files(tmp_path / "spool")) == 1


class TestIdleSweep:
    def test_spool_removed_after_idle(self, spool, clock, tmp_path):
        spool.read_chunk(FakeDrive(b"abc"), "p", "f1", length=1)
        assert len(_files(tmp_path / "spool")) == 1
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        spool.sweep()
        assert len(_files(tmp_path / "spool")) == 1
        clock.now += 2
        spool.sweep()
        assert _files(tmp_path / "spool") == []

    def test_reading_keeps_the_file_alive(self, spool, clock, tmp_path):
        drive = FakeDrive(b"abc")
        spool.read_chunk(drive, "p", "f1", length=1)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        spool.read_chunk(drive, "p", "f1", offset=1, length=1)
        clock.now += DRIVE_SPOOL_IDLE_SECONDS - 1
        spool.sweep()
        assert len(_files(tmp_path / "spool")) == 1

    def test_clear_removes_everything(self, spool, tmp_path):
        spool.read_chunk(FakeDrive(b"abc"), "p", "f1")
        spool.read_chunk(FakeDrive(b"abc"), "q", "f2")
        spool.clear()
        assert _files(tmp_path / "spool") == []

    def test_leftovers_of_a_previous_run_are_removed(self, tmp_path, clock):
        root = tmp_path / "spool"
        (root / "p").mkdir(parents=True)
        (root / "p" / "left").write_bytes(b"x")
        (root / "stray").write_bytes(b"x")
        DownloadSpool(root, clock=clock)
        assert _files(root) == []


class TestSizeCap:
    def test_file_over_64_mib_refused(self, spool):
        drive = FakeDrive(b"", size=DRIVE_MAX_FILE_BYTES + 1)
        with pytest.raises(RpcError) as err:
            spool.read_chunk(drive, "p", "f1")
        assert err.value.code == "payload_too_large"
        assert drive.downloads == 0

    def test_native_file_size_checked_after_download(self, spool, tmp_path):
        drive = FakeDrive(b"x" * (DRIVE_MAX_FILE_BYTES + 1), size=0)
        with pytest.raises(RpcError) as err:
            spool.read_chunk(drive, "p", "f1")
        assert err.value.code == "payload_too_large"
        assert drive.downloads == 1
        assert _files(tmp_path / "spool") == []

    def test_a_file_at_the_limit_is_served(self, spool):
        drive = FakeDrive(b"x" * DRIVE_MAX_FILE_BYTES)
        data, cursor = spool.read_chunk(drive, "p", "f1")
        assert data["total_size_bytes"] == DRIVE_MAX_FILE_BYTES and cursor
