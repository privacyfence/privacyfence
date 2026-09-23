"""Local file bridge: an in-memory registry plus per-principal, encrypted-at-
rest disk storage for file bytes the ``.mcpb`` shim reads off the user's own
disk and hands to the daemon, because the daemon itself cannot (see ADR
0007). Mirrors ``download_staging.py``'s ``DownloadStagingStore`` closely --
same TTL-sweep pattern, same startup-orphan sweep, same "no oracle" claim
semantics, same "plaintext never touches this server's disk unencrypted"
property -- but runs in the opposite direction: the shim *fills* a slot the
daemon created, and the daemon (not a browser) *claims* it exactly once, for
the same tool call that asked for it.

Differences from ``DownloadStagingStore`` worth being explicit about:

- A slot is created (`create_slot`) before any bytes exist, so streamed
  upload can enforce ``max_bytes`` as it arrives rather than after the fact
  (`fill` writes to disk incrementally under a ``max_bytes`` cap, and a
  second `fill` on an already-filled slot is rejected -- see `fill`'s own
  docstring for why this can't just be "overwrite").
- ``claim()`` here is called by the daemon's own tool-dispatch code (see
  local_files.py's ``call_context``), not by an HTTP route -- there is no
  browser download URL on this side of the bridge.
- The slot is bound to a specific request's declared path from the moment
  it's created (guard G1 is enforced on the shim side; this store only
  needs to remember which path a slot was minted for, so a filled slot can
  be looked up by path when a tool resumes after the upload handshake).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, BinaryIO, Iterable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import paths
from .secure_files import atomic_write_bytes

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

# 10 minutes: long enough to cover a slow upload over a loopback connection
# plus whatever gate/approval delay follows before the tool call that
# requested it resumes and claims the bytes (see local_files.py -- a slot
# is claimed once, synchronously, inside the same tools/call round trip
# that created it, so this is a ceiling on "handshake took too long", not a
# UX-facing wait).
DEFAULT_TTL_SECONDS = 600.0

_NONCE_LEN = 12
_HKDF_INFO = b"privacyfence-upload-staging-v1"
_KEY_LEN = 32  # AES-256


def _derive_key(token: bytes) -> bytes:
    """HKDF-SHA256 over the raw token -- see download_staging._derive_key,
    same construction, distinct ``info`` so a token from one store can never
    be replayed against the other."""
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(token)


def _lookup_id(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


class UploadTooLargeError(ValueError):
    """Raised by ``fill()`` when the stream exceeds the slot's ``max_bytes``.
    A ``ValueError`` subclass so ``safe_errors.public_message()`` shows it
    verbatim -- see local_files.py's own error-message convention."""


class UploadAlreadyFilledError(ValueError):
    """Raised by ``fill()`` on a slot that already has data. ``ValueError``
    subclass for the same reason as ``UploadTooLargeError``."""


@dataclass
class _PendingSlot:
    """Carries no key material -- see module docstring. ``path`` is set
    once ``create_slot`` reserves the on-disk name; ``filled`` flips to True
    only after a `fill()` call completes successfully, which is what makes a
    second `fill()` on the same slot an error rather than a silent
    overwrite."""

    lookup_id: str
    principal_id: str
    declared_path: str
    disk_path: Path
    max_bytes: int
    created_at: float
    expires_at: float
    filled: bool = False
    size_bytes: int = 0


class UploadStagingStore:
    """See module docstring. Every public method is safe to call from any
    thread; internal state is protected by one ``threading.Lock`` (cheap,
    dict-sized critical sections only -- streaming I/O and AES-GCM work
    happen outside the lock, exactly like ``DownloadStagingStore``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingSlot] = {}
        self._sweep_orphaned_from_previous_process()

    def _sweep_orphaned_from_previous_process(self) -> None:
        """Mirrors ``DownloadStagingStore``'s own startup sweep exactly,
        substituting ``paths.all_uploads_dirs()`` -- a fresh registry has no
        ``self._pending`` entries yet, so anything already on disk under any
        principal's ``uploads_dir()`` is unclaimable dead weight from a
        previous process life."""
        for directory in paths.all_uploads_dirs():
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                logger.warning("upload_staging: could not scan %s for startup cleanup: %s", directory, exc)
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                try:
                    entry.unlink()
                except OSError as exc:
                    logger.warning("upload_staging: could not remove orphaned ciphertext %s: %s", entry, exc)
                else:
                    logger.info("upload_staging: removed ciphertext orphaned by a previous process: %s", entry)

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #

    def create_slot(
        self,
        principal: "Principal",
        declared_path: str,
        max_bytes: int,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> bytes:
        """Reserves a slot for a file the shim is about to upload for
        ``declared_path`` (the path exactly as the agent passed it -- see
        ADR 0007 guard G1; this store trusts its caller to have already
        decided that this path needs a slot, it does no path validation of
        its own). Returns the raw 256-bit token that names the slot in the
        ``upload_path`` handed to the shim; like ``DownloadStagingStore.
        stage()``, that return value is the only copy of the token this
        process ever holds outside the caller's own use of it.
        """
        token = secrets.token_bytes(32)
        lookup_id = _lookup_id(token)
        now = time.time()
        slot = _PendingSlot(
            lookup_id=lookup_id,
            principal_id=principal.id,
            declared_path=declared_path,
            disk_path=paths.uploads_dir(principal) / lookup_id,
            max_bytes=max_bytes,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        with self._lock:
            self._sweep_expired_locked()
            self._pending[lookup_id] = slot
        logger.info(
            "upload_staging: slot created for principal=%s, max_bytes=%d, expires in %.0fs",
            principal.id, max_bytes, ttl_seconds,
        )
        return token

    # ------------------------------------------------------------------ #
    # Fill -- the shim's PUT
    # ------------------------------------------------------------------ #

    def fill(self, token: bytes, principal_id: str, stream: BinaryIO | Iterable[bytes]) -> int:
        """Consumes ``stream`` (a sync file-like object in tests, an async
        byte iterator from Starlette's ``request.stream()`` in production --
        callers pass whichever matches their context; see
        ``routes_file_bridge.py`` for the async path) into the slot's
        on-disk ciphertext, enforcing ``max_bytes`` as bytes arrive rather
        than after the fact -- a streamed upload larger than the slot's cap
        is rejected mid-stream instead of being fully buffered first.
        Returns the plaintext size written.

        Raises ``LookupError`` for an unknown/expired/wrong-principal slot
        (the route layer turns that into 404 -- see routes_file_bridge.py),
        ``UploadAlreadyFilledError`` for a second fill (409), and
        ``UploadTooLargeError`` once the cap is exceeded (413). All three
        are deliberately typed so the HTTP layer can map them without
        inspecting message text.
        """
        lookup_id = _lookup_id(token)
        with self._lock:
            self._sweep_expired_locked()
            slot = self._pending.get(lookup_id)
            if slot is None or slot.principal_id != principal_id:
                raise LookupError("unknown, expired, or wrong-principal upload slot")
            if slot.filled:
                raise UploadAlreadyFilledError("this upload slot has already been filled")

        plaintext = bytearray()
        max_bytes = slot.max_bytes
        for chunk in stream:
            plaintext.extend(chunk)
            if len(plaintext) > max_bytes:
                raise UploadTooLargeError(f"upload exceeds the {max_bytes}-byte limit for this call")
        return self._finish_fill(slot, token, bytes(plaintext))

    async def afill(self, token: bytes, principal_id: str, stream: AsyncIterator[bytes]) -> int:
        """Async counterpart of ``fill()`` for Starlette's ``request.
        stream()``. Same semantics, same exceptions -- see ``fill()``'s
        docstring."""
        lookup_id = _lookup_id(token)
        with self._lock:
            self._sweep_expired_locked()
            slot = self._pending.get(lookup_id)
            if slot is None or slot.principal_id != principal_id:
                raise LookupError("unknown, expired, or wrong-principal upload slot")
            if slot.filled:
                raise UploadAlreadyFilledError("this upload slot has already been filled")

        plaintext = bytearray()
        max_bytes = slot.max_bytes
        async for chunk in stream:
            plaintext.extend(chunk)
            if len(plaintext) > max_bytes:
                raise UploadTooLargeError(f"upload exceeds the {max_bytes}-byte limit for this call")
        return self._finish_fill(slot, token, bytes(plaintext))

    def _finish_fill(self, slot: "_PendingSlot", token: bytes, data: bytes) -> int:
        """Claims ``slot`` for this fill -- under the lock, before any I/O
        -- then encrypts and writes ``data`` outside it. Ordered this way
        specifically so two fills racing the same slot can never both write
        to ``slot.disk_path``: without the claim happening first, both
        could pass an outer "not filled" check, both write (the second
        silently clobbering the first's ciphertext on disk, since both
        target the same final path, not a per-attempt temp file), and
        whichever loses a *second*, later race would then unlink whatever
        ciphertext happens to be on disk at that point -- not necessarily
        its own. Claiming first means only the winner ever calls
        ``atomic_write_bytes`` at all, so there is nothing to unwind. The
        key is derived from the raw ``token`` the caller already holds --
        ``_PendingSlot`` itself never stores anything but the token's hash
        (``lookup_id``), exactly like ``StagedDownload`` never stores its
        own download token (see module docstring)."""
        with self._lock:
            if slot.filled:
                raise UploadAlreadyFilledError("this upload slot has already been filled")
            slot.filled = True
            slot.size_bytes = len(data)
        try:
            key = _derive_key(token)
            nonce = secrets.token_bytes(_NONCE_LEN)
            ciphertext = AESGCM(key).encrypt(nonce, data, None)
            atomic_write_bytes(slot.disk_path, nonce + ciphertext)
        except Exception:
            # The claim above must not permanently burn the slot on a
            # transient write failure -- unclaim it so a retried fill()
            # for the same slot can still succeed.
            with self._lock:
                slot.filled = False
                slot.size_bytes = 0
            raise
        logger.info("upload_staging: filled slot (%d bytes) for principal=%s", len(data), slot.principal_id)
        return len(data)

    # ------------------------------------------------------------------ #
    # Claim -- the daemon's own read, once, per call
    # ------------------------------------------------------------------ #

    def claim(self, token: bytes, principal_id: str) -> bytes | None:
        """Returns the plaintext on a successful, single-use claim, or
        ``None`` for a missing, expired, wrong-principal, unfilled, or
        already-claimed token -- same "no oracle" posture as
        ``DownloadStagingStore.claim()``. Deletes both the ciphertext file
        and the registry entry before returning, so a slot can never be
        claimed twice."""
        lookup_id = _lookup_id(token)
        with self._lock:
            self._sweep_expired_locked()
            slot = self._pending.get(lookup_id)
            if slot is None or slot.principal_id != principal_id or not slot.filled:
                return None
            del self._pending[lookup_id]

        try:
            raw = slot.disk_path.read_bytes()
        except OSError as exc:
            logger.warning("upload_staging: ciphertext missing for a live slot (%s): %s", lookup_id, exc)
            return None
        finally:
            try:
                slot.disk_path.unlink(missing_ok=True)
            except OSError:
                pass

        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        key = _derive_key(token)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except Exception:
            logger.warning("upload_staging: AES-GCM decrypt failed for %s", lookup_id)
            return None
        return plaintext

    # ------------------------------------------------------------------ #
    # Expiry
    # ------------------------------------------------------------------ #

    def _sweep_expired_locked(self) -> None:
        """Must be called with ``self._lock`` held. Deletes both the
        registry entry and any orphaned ciphertext for anything past
        ``expires_at`` that was never claimed -- an unfilled expired slot
        has no ciphertext to remove, ``unlink(missing_ok=True)`` handles
        both cases identically."""
        now = time.time()
        expired = [s for s in self._pending.values() if now > s.expires_at]
        for slot in expired:
            del self._pending[slot.lookup_id]
            try:
                slot.disk_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("upload_staging: could not remove expired ciphertext %s: %s", slot.disk_path, exc)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


_INSTANCE: UploadStagingStore | None = None


def get_upload_staging_store() -> UploadStagingStore:
    """Lazily-constructed process-wide singleton -- mirrors
    ``download_staging.get_download_staging_store()`` exactly, including
    the reset-between-tests contract (see tests/conftest.py)."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = UploadStagingStore()
    return _INSTANCE


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "UploadAlreadyFilledError",
    "UploadStagingStore",
    "UploadTooLargeError",
    "get_upload_staging_store",
]
