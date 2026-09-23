"""Tests for local_files.py: the local file bridge (ADR 0007) -- the single
place a connector's local-path read/write goes through, whether that means
a direct filesystem call (unseparated install), a shim handshake, or a
clear fallback error."""
from __future__ import annotations

import pytest

from privacyfence import local_files
from privacyfence.principal import LOCAL_PRINCIPAL, Principal, principal_scope
from privacyfence.upload_staging import get_upload_staging_store

ALICE = Principal(id="alice", email="alice@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    from privacyfence import paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _unseparated(monkeypatch) -> None:
    monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: False)


def _separated(monkeypatch) -> None:
    monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: True)


class TestCanAccessUserFiles:
    def test_true_in_local_mode_when_unseparated(self, monkeypatch):
        _unseparated(monkeypatch)
        assert local_files.can_access_user_files("local") is True

    def test_force_bridge_for_tests_overrides_even_an_unseparated_install(self, monkeypatch):
        _unseparated(monkeypatch)
        local_files.force_bridge_for_tests(True)
        try:
            assert local_files.can_access_user_files("local") is False
        finally:
            local_files.force_bridge_for_tests(False)

    def test_false_in_local_mode_when_separated(self, monkeypatch):
        _separated(monkeypatch)
        assert local_files.can_access_user_files("local") is False

    def test_false_in_org_mode_regardless_of_separation(self, monkeypatch):
        _unseparated(monkeypatch)
        assert local_files.can_access_user_files("org") is False
        _separated(monkeypatch)
        assert local_files.can_access_user_files("org") is False


class TestRequireLocalFilesDirectAccess:
    def test_returns_normally_when_the_daemon_can_read_directly(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"hello")
        # No error, no LocalFilesNeeded -- direct reads never need a call_context at all.
        local_files.require_local_files([str(f)], max_total_bytes=1000, download_mode="local")

    def test_read_local_file_reads_directly(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"hello world")
        local_files.require_local_files([str(f)], max_total_bytes=1000, download_mode="local")
        assert local_files.read_local_file(str(f), download_mode="local") == b"hello world"

    def test_local_file_size_reads_directly(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"hello world")
        assert local_files.local_file_size(str(f), download_mode="local") == len(b"hello world")

    def test_local_file_size_missing_file_raises_local_file_access_error(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        missing = tmp_path / "nope.pdf"
        with pytest.raises(local_files.LocalFileAccessError):
            local_files.local_file_size(str(missing), download_mode="local")

    def test_tilde_is_expanded(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "report.pdf").write_bytes(b"data")
        assert local_files.local_file_size("~/report.pdf", download_mode="local") == 4

    def test_read_local_file_raises_when_separated_and_no_bridge(self, monkeypatch):
        _separated(monkeypatch)
        with pytest.raises(local_files.LocalFileAccessError):
            local_files.read_local_file("~/report.pdf", download_mode="local")

    def test_local_file_size_raises_when_separated_and_no_bridge(self, monkeypatch):
        _separated(monkeypatch)
        with pytest.raises(local_files.LocalFileAccessError):
            local_files.local_file_size("~/report.pdf", download_mode="local")

    def test_read_local_file_wraps_an_os_error(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"data")

        real_open = open

        def flaky_open(path, *args, **kwargs):
            if str(path) == str(f):
                raise OSError("permission denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", flaky_open)
        with pytest.raises(local_files.LocalFileAccessError, match="permission denied"):
            local_files.read_local_file(str(f), download_mode="local")


class TestRequireLocalFilesBridge:
    def test_raises_local_files_needed_when_bridge_capable_and_no_direct_access(self, monkeypatch):
        _separated(monkeypatch)
        with local_files.call_context(bridge_available=True, uploads={}), pytest.raises(
            local_files.LocalFilesNeeded,
        ) as excinfo:
            local_files.require_local_files(["~/a.pdf", "~/b.pdf"], max_total_bytes=500, download_mode="local")
        # Every missing path is listed at once, not one at a time.
        assert excinfo.value.paths == ["~/a.pdf", "~/b.pdf"]
        assert excinfo.value.max_bytes == 500

    def test_raises_local_file_access_error_when_no_bridge_available(self, monkeypatch):
        _separated(monkeypatch)
        with local_files.call_context(bridge_available=False, uploads={}), pytest.raises(
            local_files.LocalFileAccessError,
        ) as excinfo:
            local_files.require_local_files(["~/a.pdf"], max_total_bytes=500, download_mode="local")
        assert "extension" in str(excinfo.value) or "content_base64" in str(excinfo.value)

    def test_raises_local_file_access_error_with_no_call_context_at_all(self, monkeypatch):
        # A separated daemon, no call_context entered (e.g. a direct HTTP
        # client that skipped the file-bridge protocol entirely) -- same
        # fallback error as "bridge_available=False".
        _separated(monkeypatch)
        with pytest.raises(local_files.LocalFileAccessError):
            local_files.require_local_files(["~/a.pdf"], max_total_bytes=500, download_mode="local")

    def test_already_uploaded_path_needs_no_handshake(self, monkeypatch):
        _separated(monkeypatch)
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "~/a.pdf", max_bytes=1000)
            store.fill(token, ALICE.id, [b"file content"])
            slot_b64 = local_files._encode_token(token)
            with local_files.call_context(bridge_available=True, uploads={"~/a.pdf": slot_b64}):
                local_files.require_local_files(["~/a.pdf"], max_total_bytes=1000, download_mode="local")
                assert local_files.read_local_file("~/a.pdf", download_mode="local") == b"file content"

    def test_claimed_upload_bytes_are_cached_for_the_rest_of_the_call(self, monkeypatch):
        """The slot is single-use in upload_staging -- a second claim would
        return None -- so require_local_files must never claim it twice
        within the same call. Reading it more than once via read_local_file/
        local_file_size must come from the per-call cache."""
        _separated(monkeypatch)
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "~/a.pdf", max_bytes=1000)
            store.fill(token, ALICE.id, [b"file content"])
            slot_b64 = local_files._encode_token(token)
            with local_files.call_context(bridge_available=True, uploads={"~/a.pdf": slot_b64}):
                local_files.require_local_files(["~/a.pdf"], max_total_bytes=1000, download_mode="local")
                # A second require_local_files() call for the same path in
                # the same call_context must not try to re-claim the
                # already-consumed slot.
                local_files.require_local_files(["~/a.pdf"], max_total_bytes=1000, download_mode="local")
                assert local_files.read_local_file("~/a.pdf", download_mode="local") == b"file content"
                assert local_files.local_file_size("~/a.pdf", download_mode="local") == len(b"file content")

    def test_malformed_upload_slot_reference_raises_local_file_access_error(self, monkeypatch):
        _separated(monkeypatch)
        with local_files.call_context(bridge_available=True, uploads={"~/a.pdf": "a"}), pytest.raises(
            local_files.LocalFileAccessError, match="invalid upload reference",
        ):
            local_files.require_local_files(["~/a.pdf"], max_total_bytes=1000, download_mode="local")

    def test_expired_upload_slot_raises_local_file_access_error(self, monkeypatch):
        _separated(monkeypatch)
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "~/a.pdf", max_bytes=1000, ttl_seconds=-1.0)
            slot_b64 = local_files._encode_token(token)
            with local_files.call_context(bridge_available=True, uploads={"~/a.pdf": slot_b64}), pytest.raises(
                local_files.LocalFileAccessError,
            ):
                local_files.require_local_files(["~/a.pdf"], max_total_bytes=1000, download_mode="local")


class TestDeliverFile:
    def test_direct_write(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        dest_dir = tmp_path / "downloads"
        result = local_files.deliver_file(str(dest_dir), "report.pdf", b"content", "application/pdf", download_mode="local")
        assert result["delivery"] == "local_disk"
        assert result["path"] == str(dest_dir / "report.pdf")
        assert (dest_dir / "report.pdf").read_bytes() == b"content"

    def test_bridge_delivery_stages_and_records_a_pending_delivery(self, monkeypatch):
        _separated(monkeypatch)
        with principal_scope(ALICE), local_files.call_context(bridge_available=True, uploads={}) as state:
            result = local_files.deliver_file(
                "~/Downloads", "report.pdf", b"content", "application/pdf", download_mode="local",
            )
        assert result["delivery"] == "client_bridge"
        assert result["path"] is None
        assert len(state.pending_deliveries) == 1
        entry = state.pending_deliveries[0]
        assert entry["dest_dir"] == "~/Downloads"
        assert entry["name"] == "report.pdf"
        assert entry["download_path"].startswith("/mcp-files/downloads/")
        assert entry["size_bytes"] == len(b"content")
        assert state.staged_download is True

    def test_no_bridge_fallback_returns_a_download_link(self, monkeypatch):
        _separated(monkeypatch)
        with principal_scope(ALICE), local_files.call_context(
            bridge_available=False, uploads={}, base_url="http://127.0.0.1:8765",
        ) as state:
            result = local_files.deliver_file(
                "~/Downloads", "report.pdf", b"content", "application/pdf", download_mode="local",
            )
        assert result["delivery"] == "link"
        assert result["download_url"].startswith("http://127.0.0.1:8765/mcp-files/downloads/")
        assert "note" in result
        assert state.staged_download is True

    def test_direct_write_wraps_an_os_error(self, monkeypatch, tmp_path):
        _unseparated(monkeypatch)
        dest_dir = tmp_path / "downloads"

        def flaky_open(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", flaky_open)
        with pytest.raises(local_files.LocalFileAccessError, match="disk full"):
            local_files.deliver_file(str(dest_dir), "report.pdf", b"content", "application/pdf", download_mode="local")

    def test_no_bridge_fallback_with_no_call_context_at_all(self, monkeypatch):
        # A separated daemon serving a client that never entered
        # call_context() (e.g. a bare HTTP client bypassing the file-bridge
        # protocol entirely) -- deliver_file must still degrade to the link
        # fallback rather than raising AttributeError on a None state.
        _separated(monkeypatch)
        result = local_files.deliver_file(
            "~/Downloads", "report.pdf", b"content", "application/pdf", download_mode="local",
        )
        assert result["delivery"] == "link"
        assert result["download_url"].startswith("/mcp-files/downloads/")

    def test_oversized_file_raises_local_file_access_error(self, monkeypatch):
        _unseparated(monkeypatch)
        local_files.configure_file_bridge(max_download_bytes=5)
        try:
            with pytest.raises(local_files.LocalFileAccessError):
                local_files.deliver_file(
                    "/tmp", "big.bin", b"way too many bytes", "application/octet-stream", download_mode="local",
                )
        finally:
            local_files.configure_file_bridge()


class TestCallProducedDeliveries:
    def test_false_with_no_call_context(self):
        assert local_files.call_produced_deliveries() is False

    def test_false_when_nothing_was_staged(self):
        with local_files.call_context(bridge_available=True, uploads={}):
            assert local_files.call_produced_deliveries() is False

    def test_true_after_a_bridge_delivery(self, monkeypatch):
        _separated(monkeypatch)
        with principal_scope(ALICE), local_files.call_context(bridge_available=True, uploads={}):
            local_files.deliver_file("~/Downloads", "f.pdf", b"x", "application/pdf", download_mode="local")
            assert local_files.call_produced_deliveries() is True


class TestBuildNeedUploadsFiles:
    def test_creates_one_slot_per_path_with_the_wire_shape(self, monkeypatch):
        _separated(monkeypatch)
        needed = local_files.LocalFilesNeeded(["~/a.pdf", "~/b.pdf"], max_bytes=1000)
        files = local_files.build_need_uploads_files(LOCAL_PRINCIPAL, needed)
        assert [f["path"] for f in files] == ["~/a.pdf", "~/b.pdf"]
        for f in files:
            assert f["max_bytes"] == 1000
            assert f["upload_path"] == f"/mcp-files/uploads/{f['slot']}"
            assert f["slot"]
