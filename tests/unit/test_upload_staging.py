"""Tests for upload_staging.py: the local file bridge's own encrypted-at-
rest staging store for the shim -> daemon upload direction (ADR 0007).
Mirrors test_download_staging.py's own structure and conventions closely --
this is the same design, run in the opposite direction."""
from __future__ import annotations

import pytest

from privacyfence import paths, upload_staging
from privacyfence.principal import Principal
from privacyfence.upload_staging import (
    UploadAlreadyFilledError,
    UploadStagingStore,
    UploadTooLargeError,
)

ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _chunks(data: bytes) -> list[bytes]:
    # Split into two chunks to exercise the streaming accumulation path,
    # not just a single-shot fill.
    if len(data) < 2:
        return [data]
    mid = len(data) // 2
    return [data[:mid], data[mid:]]


class TestCreateFillClaim:
    def test_round_trip_returns_the_right_bytes(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/report.pdf", max_bytes=1000)
        written = store.fill(token, ALICE.id, _chunks(b"hello world"))
        assert written == len(b"hello world")
        assert store.claim(token, ALICE.id) == b"hello world"

    def test_ciphertext_file_is_gone_after_claim(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/a.bin", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"secret payload"))
        assert len(list(paths.uploads_dir(ALICE).iterdir())) == 1
        store.claim(token, ALICE.id)
        assert list(paths.uploads_dir(ALICE).iterdir()) == []

    def test_second_claim_of_the_same_token_returns_none(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"data"))
        assert store.claim(token, ALICE.id) == b"data"
        assert store.claim(token, ALICE.id) is None

    def test_the_file_on_disk_is_never_plaintext(self):
        store = UploadStagingStore()
        plaintext = b"This is a strictly confidential upload. " * 50
        token = store.create_slot(ALICE, "~/confidential.txt", max_bytes=len(plaintext) + 10)
        store.fill(token, ALICE.id, _chunks(plaintext))
        [on_disk] = list(paths.uploads_dir(ALICE).iterdir())
        raw = on_disk.read_bytes()
        assert raw != plaintext
        assert plaintext not in raw

    def test_claim_before_fill_returns_none(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        assert store.claim(token, ALICE.id) is None


class TestFillFailureCases:
    def test_fill_of_unknown_token_raises_lookup_error(self):
        store = UploadStagingStore()
        forged_token = b"\x00" * 32
        with pytest.raises(LookupError):
            store.fill(forged_token, ALICE.id, _chunks(b"data"))

    def test_fill_by_the_wrong_principal_raises_lookup_error(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        with pytest.raises(LookupError):
            store.fill(token, BOB.id, _chunks(b"data"))
        # Still fillable by the right principal afterward -- a rejected
        # cross-principal fill must not consume the slot.
        assert store.fill(token, ALICE.id, _chunks(b"data")) == 4

    def test_second_fill_raises_already_filled(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"first"))
        with pytest.raises(UploadAlreadyFilledError):
            store.fill(token, ALICE.id, _chunks(b"second"))
        # The first fill's bytes are still what gets claimed.
        assert store.claim(token, ALICE.id) == b"first"

    def test_oversized_stream_raises_too_large(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/big.bin", max_bytes=5)
        with pytest.raises(UploadTooLargeError):
            store.fill(token, ALICE.id, _chunks(b"way too many bytes"))

    def test_oversized_fill_leaves_the_slot_unfilled(self):
        """A rejected over-cap stream must not leave a half-written slot
        that a later, correctly-sized fill can no longer use."""
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/big.bin", max_bytes=5)
        with pytest.raises(UploadTooLargeError):
            store.fill(token, ALICE.id, _chunks(b"way too many bytes"))
        assert store.claim(token, ALICE.id) is None


class TestExpiry:
    def test_expired_slot_fill_raises_lookup_error(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000, ttl_seconds=-1.0)
        with pytest.raises(LookupError):
            store.fill(token, ALICE.id, _chunks(b"data"))

    def test_expired_slot_sweep_removes_orphaned_ciphertext(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000, ttl_seconds=-1.0)
        store.create_slot(ALICE, "~/g.txt", max_bytes=1000, ttl_seconds=300.0)
        # claim() never raises -- a swept (expired) slot returns None, the
        # same no-oracle posture as an unknown/wrong-principal slot.
        assert store.claim(token, ALICE.id) is None


class TestStartupSweep:
    """UploadStagingStore.__init__ must not let a daemon restart turn a
    lost registry entry into ciphertext that lingers forever -- mirrors
    DownloadStagingStore's own equivalent test."""

    def test_orphaned_ciphertext_from_a_previous_process_is_removed_on_startup(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"data"))
        assert len(list(paths.uploads_dir(ALICE).iterdir())) == 1

        # A fresh store -- as if the daemon restarted -- has no in-memory
        # record of that slot, so the file on disk is unclaimable dead
        # weight and must be swept.
        UploadStagingStore()
        assert list(paths.uploads_dir(ALICE).iterdir()) == []

    def test_sweeps_every_principals_uploads_dir_not_just_one(self):
        alice_leftover = paths.uploads_dir(ALICE) / "a"
        alice_leftover.write_bytes(b"x")
        bob_leftover = paths.uploads_dir(BOB) / "b"
        bob_leftover.write_bytes(b"y")

        UploadStagingStore()

        assert not alice_leftover.exists()
        assert not bob_leftover.exists()

    def test_a_fresh_store_with_nothing_on_disk_constructs_cleanly(self):
        UploadStagingStore()

    def test_skips_a_non_file_entry_in_the_uploads_dir(self):
        subdir = paths.uploads_dir(ALICE) / "not-a-file"
        subdir.mkdir()

        # Must not raise, and must leave the subdirectory alone -- only
        # plain files are ever written here by fill(), so a directory
        # entry is not this sweep's business.
        UploadStagingStore()
        assert subdir.exists()

    def test_tolerates_a_scan_failure_during_startup_sweep(self, monkeypatch):
        paths.uploads_dir(ALICE)  # ensure the directory exists to scan

        from pathlib import Path
        real_iterdir = Path.iterdir

        def failing_iterdir(self, *a, **k):
            if self == paths.uploads_dir(ALICE):
                raise OSError("permission denied")
            return real_iterdir(self, *a, **k)

        monkeypatch.setattr(Path, "iterdir", failing_iterdir)
        try:
            UploadStagingStore()  # must not raise
        finally:
            monkeypatch.setattr(Path, "iterdir", real_iterdir)

    def test_tolerates_an_unlink_failure_during_startup_sweep(self, monkeypatch):
        leftover = paths.uploads_dir(ALICE) / "orphaned-slot-id"
        leftover.write_bytes(b"some old ciphertext")

        from pathlib import Path
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        try:
            UploadStagingStore()  # must not raise
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)


class TestClaimDiskFailures:
    def test_ciphertext_missing_on_disk_returns_none(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"data"))
        [on_disk] = list(paths.uploads_dir(ALICE).iterdir())
        on_disk.unlink()

        assert store.claim(token, ALICE.id) is None

    def test_corrupted_ciphertext_fails_decrypt_and_returns_none(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"data"))
        [on_disk] = list(paths.uploads_dir(ALICE).iterdir())
        on_disk.write_bytes(b"\x00" * 40)

        assert store.claim(token, ALICE.id) is None

    def test_claim_tolerates_a_cleanup_unlink_failure_and_still_returns_the_bytes(self, monkeypatch):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"data"))

        from pathlib import Path
        real_unlink = Path.unlink
        monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")))
        try:
            result = store.claim(token, ALICE.id)
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)
        assert result == b"data"

    def test_sweep_tolerates_an_unlink_failure_on_an_expired_entry(self, monkeypatch):
        store = UploadStagingStore()
        store.create_slot(ALICE, "~/f.txt", max_bytes=1000, ttl_seconds=-1.0)

        from pathlib import Path
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        try:
            # Must not raise -- the sweep logs and moves on.
            store.create_slot(ALICE, "~/g.txt", max_bytes=1000, ttl_seconds=300.0)
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)


class TestFillRaceRecheck:
    def test_a_slot_filled_between_the_outer_check_and_finish_raises_already_filled(self):
        """_finish_fill claims the slot (sets ``filled``) under the lock
        before any I/O, specifically so two fills racing the same slot can
        never both reach the disk write -- see its own docstring. Exercised
        here by calling the private finisher directly on an already-filled
        slot, since provoking the real race deterministically in a sync
        test isn't practical."""
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"first"))
        # The slot is only popped from _pending on claim(), not fill() --
        # fetch it back out to simulate the race directly.
        lookup_id = upload_staging._lookup_id(token)
        slot = store._pending[lookup_id]
        assert slot.filled is True

        with pytest.raises(UploadAlreadyFilledError):
            store._finish_fill(slot, token, b"second")
        # The loser never touched disk -- the first fill's bytes are
        # unaffected.
        assert store.claim(token, ALICE.id) == b"first"

    def test_a_failed_write_unclaims_the_slot_so_a_retry_can_still_succeed(self, monkeypatch):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)

        monkeypatch.setattr(
            upload_staging, "atomic_write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            store.fill(token, ALICE.id, _chunks(b"data"))
        monkeypatch.undo()

        # Unclaimed -- a retried fill for the same slot still works.
        assert store.fill(token, ALICE.id, _chunks(b"retry")) == len(b"retry")
        assert store.claim(token, ALICE.id) == b"retry"


class TestPendingCount:
    def test_reflects_live_slots(self):
        store = UploadStagingStore()
        assert store.pending_count == 0
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        assert store.pending_count == 1
        store.fill(token, ALICE.id, _chunks(b"data"))
        store.claim(token, ALICE.id)
        assert store.pending_count == 0


class TestSingleton:
    def test_get_upload_staging_store_returns_the_same_instance(self):
        first = upload_staging.get_upload_staging_store()
        second = upload_staging.get_upload_staging_store()
        assert first is second


async def _achunks(data: bytes):
    for chunk in _chunks(data):
        yield chunk


class TestAfillCapability:
    """The unauthenticated capability fill (ADR 0007's "Clients without the
    bridge" section, ADR 0028) -- same semantics as fill()/afill() minus the
    principal check, since the capability route it backs
    (routes_file_bridge.py's PUT /mcp-files/slots/<token>) carries no
    bearer header/principal to check the slot against at all."""

    async def test_round_trip_needs_no_principal(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/report.pdf", max_bytes=1000)
        written = await store.afill_capability(token, _achunks(b"hello world"))
        assert written == len(b"hello world")
        # The claim side still checks the principal it was created for --
        # a capability upload is not a capability *claim*.
        assert store.claim(token, ALICE.id) == b"hello world"
        assert store.claim(token, BOB.id) is None

    async def test_unknown_token_raises_lookup_error(self):
        store = UploadStagingStore()
        with pytest.raises(LookupError):
            await store.afill_capability(b"\x00" * 32, _achunks(b"data"))

    async def test_second_fill_raises_already_filled(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        await store.afill_capability(token, _achunks(b"first"))
        with pytest.raises(UploadAlreadyFilledError):
            await store.afill_capability(token, _achunks(b"second"))

    async def test_oversized_stream_raises_too_large(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/big.bin", max_bytes=5)
        with pytest.raises(UploadTooLargeError):
            await store.afill_capability(token, _achunks(b"way too many bytes"))

    async def test_expired_slot_raises_lookup_error(self):
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000, ttl_seconds=-1.0)
        with pytest.raises(LookupError):
            await store.afill_capability(token, _achunks(b"data"))

    async def test_a_slot_already_filled_via_the_authenticated_route_rejects_a_capability_fill(self):
        """Whichever route fills a slot first wins -- the two fill paths
        share the same ``filled`` flag, so a slot can't be double-filled
        by mixing the authenticated and capability routes either."""
        store = UploadStagingStore()
        token = store.create_slot(ALICE, "~/f.txt", max_bytes=1000)
        store.fill(token, ALICE.id, _chunks(b"first"))
        with pytest.raises(UploadAlreadyFilledError):
            await store.afill_capability(token, _achunks(b"second"))
