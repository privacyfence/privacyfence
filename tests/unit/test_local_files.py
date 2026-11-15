"""Tests for local_files.py (ADR 0299): the single place an agent-named file turns into bytes
(an ``upload:<id>`` reference only) and a produced file turns into a one-time link."""
from __future__ import annotations

import pytest

from privacyfence import local_files
from privacyfence.org_mode import LINK_RESULT_NOTE, DownloadDeliveryConfig
from privacyfence.principal import Principal, principal_scope
from privacyfence.upload_staging import get_upload_staging_store

ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    from privacyfence import paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


class TestCanAccessUserFiles:
    @pytest.mark.parametrize("mode", ["local", "org"])
    def test_always_false(self, mode):
        assert local_files.can_access_user_files(mode) is False


class TestRequireLocalFilesPaths:
    @pytest.mark.parametrize("mode", ["local", "org"])
    def test_a_path_that_is_not_an_upload_reference_is_refused_with_the_upload_slot_message(self, mode, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"hello")
        with local_files.call_context(), pytest.raises(local_files.LocalFileAccessError) as excinfo:
            local_files.require_local_files([str(f)], max_total_bytes=500, download_mode=mode)
        assert str(excinfo.value) == local_files.NO_BRIDGE_UPLOAD_MESSAGE
        assert "privacyfence_create_upload_slot" in str(excinfo.value)

    def test_the_refusal_does_not_need_a_call_context(self):
        with pytest.raises(local_files.LocalFileAccessError, match="upload slot"):
            local_files.require_local_files(["~/a.pdf"], max_total_bytes=500, download_mode="local")

    def test_read_and_size_of_an_unclaimed_path_never_touch_the_disk(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"hello")
        with pytest.raises(local_files.LocalFileAccessError, match="upload slot"):
            local_files.read_local_file(str(f), download_mode="local")
        with pytest.raises(local_files.LocalFileAccessError, match="upload slot"):
            local_files.local_file_size(str(f), download_mode="local")

    def test_the_first_bad_path_in_a_list_stops_the_call(self):
        with local_files.call_context(), pytest.raises(local_files.LocalFileAccessError, match="upload slot"):
            local_files.require_local_files(
                ["~/a.pdf", "~/b.pdf"], max_total_bytes=500, download_mode="local",
            )


class TestRequireLocalFilesUploadRef:
    """Capability uploads (ADR 0028): an ``upload:<id>`` reference is claimed in every mode --
    see require_local_files' own docstring."""

    def test_claims_in_every_mode(self, monkeypatch):
        # Org mode: can_access_user_files() is always False, and there is
        # no call_context/bridge at all -- the least favorable case for
        # every other path shape, and the exact case this exists to fix.
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "report.pdf", max_bytes=1000)
            store.fill(token, ALICE.id, [b"file content"])
            ref = f"{local_files.UPLOAD_REF_PREFIX}{local_files._encode_token(token)}"
            with local_files.call_context():
                local_files.require_local_files([ref], max_total_bytes=1000, download_mode="org")
                assert local_files.read_local_file(ref, download_mode="org") == b"file content"

    def test_works_with_no_call_context_bound_by_the_call_but_needs_one_to_claim_into(self, monkeypatch):
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "report.pdf", max_bytes=1000)
        ref = f"{local_files.UPLOAD_REF_PREFIX}{local_files._encode_token(token)}"
        with pytest.raises(local_files.LocalFileAccessError, match="no active call"):
            local_files.require_local_files([ref], max_total_bytes=1000, download_mode="local")

    def test_wrong_principal_gets_the_same_error_as_expired_or_already_claimed(self, monkeypatch):
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "report.pdf", max_bytes=1000)
            store.fill(token, ALICE.id, [b"file content"])
        ref = f"{local_files.UPLOAD_REF_PREFIX}{local_files._encode_token(token)}"
        with principal_scope(BOB), local_files.call_context(), pytest.raises(
            local_files.LocalFileAccessError,
        ):
            local_files.require_local_files([ref], max_total_bytes=1000, download_mode="local")

    @pytest.mark.parametrize("ref", ["upload:a", "upload:!!!!", "upload:"])
    def test_a_malformed_upload_reference_is_refused(self, ref):
        with principal_scope(ALICE), local_files.call_context(), pytest.raises(
            local_files.LocalFileAccessError, match="invalid upload reference",
        ):
            local_files.require_local_files([ref], max_total_bytes=1000, download_mode="org")

    def test_an_expired_upload_slot_is_refused(self):
        with principal_scope(ALICE):
            token = get_upload_staging_store().create_slot(ALICE, "a.pdf", max_bytes=1000, ttl_seconds=-1.0)
            ref = f"{local_files.UPLOAD_REF_PREFIX}{local_files._encode_token(token)}"
            with local_files.call_context(), pytest.raises(local_files.LocalFileAccessError, match="expired"):
                local_files.require_local_files([ref], max_total_bytes=1000, download_mode="org")

    def test_second_require_for_the_same_ref_in_one_call_reuses_the_cache(self, monkeypatch):
        with principal_scope(ALICE):
            store = get_upload_staging_store()
            token = store.create_slot(ALICE, "report.pdf", max_bytes=1000)
            store.fill(token, ALICE.id, [b"file content"])
            ref = f"{local_files.UPLOAD_REF_PREFIX}{local_files._encode_token(token)}"
            with local_files.call_context():
                local_files.require_local_files([ref], max_total_bytes=1000, download_mode="local")
                # A second call must not try to re-claim the single-use slot.
                local_files.require_local_files([ref], max_total_bytes=1000, download_mode="local")
                assert local_files.read_local_file(ref, download_mode="local") == b"file content"


class TestBuildUploadSlot:
    """The privacyfence_create_upload_slot meta-tool's own handler."""

    def test_returns_a_capability_url_and_id(self):
        with principal_scope(ALICE):
            result = local_files.build_upload_slot(
                ALICE, filename="report.pdf", size_bytes=None, base_url="http://127.0.0.1:8765",
            )
        assert result["method"] == "PUT"
        assert result["upload_url"] == f"http://127.0.0.1:8765/mcp-files/slots/{result['upload_id']}"
        assert result["max_bytes"] == local_files.DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES
        assert result["upload_id"] in result["example"]

    def test_upload_id_can_be_claimed_as_an_upload_ref(self):
        with principal_scope(ALICE):
            slot = local_files.build_upload_slot(
                ALICE, filename="report.pdf", size_bytes=None, base_url="http://127.0.0.1:8765",
            )
            token = local_files._decode_token(slot["upload_id"])
            get_upload_staging_store().fill(token, ALICE.id, [b"file content"])
            ref = f"{local_files.UPLOAD_REF_PREFIX}{slot['upload_id']}"
            with local_files.call_context():
                local_files.require_local_files([ref], max_total_bytes=1000, download_mode="local")
                assert local_files.read_local_file(ref, download_mode="local") == b"file content"

    def test_rejects_a_size_over_the_cap_before_minting_a_slot(self):
        with principal_scope(ALICE), pytest.raises(local_files.LocalFileAccessError, match="over PrivacyFence's"):
            local_files.build_upload_slot(
                ALICE, filename="huge.bin",
                size_bytes=local_files.DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES + 1,
                base_url="http://127.0.0.1:8765",
            )
        assert get_upload_staging_store().pending_count == 0

    def test_url_strips_a_trailing_slash_on_base_url(self):
        with principal_scope(ALICE):
            result = local_files.build_upload_slot(
                ALICE, filename="report.pdf", size_bytes=None, base_url="http://127.0.0.1:8765/",
            )
        assert "//mcp-files" not in result["upload_url"]


class TestDeliverFile:
    def test_returns_a_one_time_capability_link_and_writes_nothing_to_disk(self, tmp_path):
        dest_dir = tmp_path / "downloads"
        with principal_scope(ALICE), local_files.call_context(base_url="http://127.0.0.1:8765") as state:
            result = local_files.deliver_file(
                str(dest_dir), "report.pdf", b"content", "application/pdf", download_mode="local",
            )
        assert result["delivery"] == "link"
        assert result["path"] is None
        assert result["download_url"].startswith("http://127.0.0.1:8765/mcp-files/fetch/")
        assert "note" in result
        assert state.staged_download is True
        assert not dest_dir.exists()

    def test_with_no_call_context_the_link_is_relative(self):
        with principal_scope(ALICE):
            result = local_files.deliver_file(
                "~/Downloads", "report.pdf", b"content", "application/pdf", download_mode="local",
            )
        assert result["delivery"] == "link"
        assert result["download_url"].startswith("/mcp-files/fetch/")

    def test_oversized_file_raises_local_file_access_error(self):
        local_files.configure_file_bridge(max_download_bytes=5)
        try:
            with pytest.raises(local_files.LocalFileAccessError):
                local_files.deliver_file(
                    "/tmp", "big.bin", b"way too many bytes", "application/octet-stream", download_mode="local",
                )
        finally:
            local_files.configure_file_bridge()


    def test_check_delivery_size_raises_over_the_limit(self):
        local_files.configure_file_bridge(max_download_bytes=5)
        try:
            with pytest.raises(local_files.LocalFileAccessError, match=r"This file is 6 bytes, over .* 5-byte"):
                local_files.check_delivery_size(6)
        finally:
            local_files.configure_file_bridge()

    def test_check_delivery_size_allows_the_limit(self):
        local_files.configure_file_bridge(max_download_bytes=5)
        try:
            local_files.check_delivery_size(5)
        finally:
            local_files.configure_file_bridge()

class TestDeliverExport:
    @staticmethod
    def _fake_store(monkeypatch):
        calls = []

        class FakeStore:
            def stage(self, principal, data, name, mime_type, **kwargs):
                calls.append({"data": data, "name": name, "mime_type": mime_type, **kwargs})
                return b"\x01" * 32

        monkeypatch.setattr(local_files, "get_download_staging_store", lambda: FakeStore())
        return calls

    def test_local_mode_calls_deliver_file(self, monkeypatch):
        seen = {}

        def fake(dest_dir, name, data, mime_type, *, download_mode):
            seen.update(dest_dir=dest_dir, name=name, download_mode=download_mode)
            return {"path": "/x"}

        monkeypatch.setattr(local_files, "deliver_file", fake)
        result = local_files.deliver_export(
            "~/out", "a.csv", b"x", "text/csv",
            download_mode="local", download_config=None, download_base_url="",
        )
        assert result == {"path": "/x"}
        assert seen == {"dest_dir": "~/out", "name": "a.csv", "download_mode": "local"}

    def test_org_mode_stages_and_returns_a_link(self, monkeypatch):
        calls = self._fake_store(monkeypatch)
        result = local_files.deliver_export(
            "~/out", "a.csv", b"abc", "text/csv",
            download_mode="org", download_config=None, download_base_url="https://pf.example",
        )
        assert result["delivery"] == "link"
        assert result["download_url"].startswith("https://pf.example/mcp-files/fetch/")
        assert isinstance(result["expires_at"], str)
        assert result["note"] == LINK_RESULT_NOTE
        assert result["size_bytes"] == 3
        assert calls[0]["data"] == b"abc"

    def test_org_mode_uses_the_config_ttl(self, monkeypatch):
        calls = self._fake_store(monkeypatch)
        local_files.deliver_export(
            "~/out", "a.csv", b"abc", "text/csv", download_mode="org",
            download_config=DownloadDeliveryConfig(link_ttl_seconds=77),
            download_base_url="https://pf.example",
        )
        assert calls[0]["ttl_seconds"] == 77

    def test_org_mode_checks_the_delivery_size(self, monkeypatch):
        calls = self._fake_store(monkeypatch)
        local_files.configure_file_bridge(max_download_bytes=2)
        try:
            with pytest.raises(local_files.LocalFileAccessError):
                local_files.deliver_export(
                    "~/out", "a.csv", b"abc", "text/csv",
                    download_mode="org", download_config=None, download_base_url="",
                )
        finally:
            local_files.configure_file_bridge()
        assert calls == []

    def test_org_mode_marks_the_call_as_having_staged_a_download(self, monkeypatch):
        self._fake_store(monkeypatch)
        with local_files.call_context():
            local_files.deliver_export(
                "~/out", "a.csv", b"abc", "text/csv",
                download_mode="org", download_config=None, download_base_url="https://pf.example",
            )
            assert local_files.call_produced_deliveries() is True


class TestCallProducedDeliveries:
    def test_false_with_no_call_context(self):
        assert local_files.call_produced_deliveries() is False

    def test_false_when_nothing_was_staged(self):
        with local_files.call_context():
            assert local_files.call_produced_deliveries() is False

    def test_true_after_a_link_delivery(self):
        with principal_scope(ALICE), local_files.call_context():
            local_files.deliver_file("~/Downloads", "f.pdf", b"x", "application/pdf", download_mode="local")
            assert local_files.call_produced_deliveries() is True

    def test_true_after_marking_a_staged_download(self):
        with local_files.call_context():
            local_files.mark_staged_download()
            assert local_files.call_produced_deliveries() is True

    def test_marking_with_no_call_context_is_a_no_op(self):
        local_files.mark_staged_download()
        assert local_files.call_produced_deliveries() is False
