"""Chunked Drive downloads for ``source.call`` (ADR 0123).

Drive has no range reads, so the first call for a file fetches the whole thing once, writes it
under a spool directory the plugin cannot see (owner-only, never named after the Drive file) and
serves 8 MiB chunks from there. Each later call re-reads the file's metadata and compares its
``modified_time`` with the one the spool holds, so a file edited between chunks is reported rather
than served as a Frankenstein of two versions. Spool files idle for ten minutes are swept on every
call, and everything is removed when the host shuts down.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privacyfence.plugins.constants import DRIVE_CHUNK_BYTES, DRIVE_MAX_FILE_BYTES, DRIVE_SPOOL_IDLE_SECONDS
from privacyfence.plugins.protocol import RpcError
from privacyfence.secure_files import atomic_write_bytes, secure_mkdir

logger = logging.getLogger(__name__)


def encode_cursor(file_id: str, revision: str, offset: int) -> str:
    raw = json.dumps({"f": file_id, "r": revision, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str, int]:
    """Return ``(file_id, revision, offset)``; anything malformed is ``invalid_params``."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        file_id, revision, offset = data["f"], data["r"], data["o"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise RpcError("invalid_params", "cursor is not valid") from None
    if (
        not isinstance(file_id, str)
        or not isinstance(revision, str)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise RpcError("invalid_params", "cursor is not valid")
    return file_id, revision, offset


@dataclass
class _Entry:
    path: Path
    revision: str
    mime_type: str
    size: int
    last_used: float


def _digest(value: str, length: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove spool file %s", path.name)


class DownloadSpool:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._root = Path(root)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        secure_mkdir(self._root, 0o700)
        # Whatever a previous run left behind is unreachable (the index is in memory) and holds
        # file content, so it goes first.
        self._remove_all_files()

    def read_chunk(
        self,
        client: Any,
        plugin: str,
        file_id: str,
        *,
        offset: int | None = None,
        cursor: str | None = None,
        length: int = DRIVE_CHUNK_BYTES,
    ) -> tuple[dict, str | None]:
        """Serve one chunk of ``file_id``. Returns ``(data, next_cursor)``."""
        if cursor is not None and offset is not None:
            raise RpcError("invalid_params", "offset and cursor cannot both be given")
        cursor_revision: str | None = None
        if cursor is not None:
            cursor_file, cursor_revision, offset = decode_cursor(cursor)
            if cursor_file != file_id:
                raise RpcError("invalid_params", "cursor belongs to a different file")
        continuing = cursor is not None or offset is not None
        start = offset or 0
        self.sweep()

        metadata = client.get_file_metadata(file_id)
        revision = metadata.modified_time
        key = (plugin, file_id)
        with self._lock:
            entry = self._entries.get(key)
        if continuing and (
            (entry is not None and entry.revision != revision)
            or (cursor_revision is not None and cursor_revision != revision)
        ):
            self._drop(key)
            raise RpcError(
                "upstream_error",
                "the file changed while it was being downloaded",
                extra={"reason": "revision_changed"},
            )
        if entry is None or entry.revision != revision or not entry.path.is_file():
            entry = self._fetch(client, plugin, file_id, metadata)
        entry.last_used = self._clock()

        if start > entry.size:
            raise RpcError("invalid_params", "offset is past the end of the file")
        end = min(start + length, entry.size)
        with entry.path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(end - start)
        eof = end >= entry.size
        data = {
            "file_id": file_id,
            "mime_type": entry.mime_type,
            "revision": revision,
            "total_size_bytes": entry.size,
            "offset": start,
            "length": len(chunk),
            "eof": eof,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
        }
        return data, None if eof else encode_cursor(file_id, revision, end)

    def _fetch(self, client: Any, plugin: str, file_id: str, metadata: Any) -> _Entry:
        revision = metadata.modified_time
        if metadata.size > DRIVE_MAX_FILE_BYTES:
            raise RpcError("payload_too_large", "the file is larger than the 64 MiB limit")
        fetched = client.download_file_bytes(file_id)
        data = fetched["data"]
        # Google-native files report size 0 in metadata, so the export is measured too.
        if len(data) > DRIVE_MAX_FILE_BYTES:
            raise RpcError("payload_too_large", "the file is larger than the 64 MiB limit")
        path = self._root / plugin / f"{_digest(file_id, 16)}-{_digest(revision, 12)}"
        atomic_write_bytes(path, data, mode=0o600)
        entry = _Entry(
            path=path,
            revision=revision,
            mime_type=fetched.get("mime_type") or "application/octet-stream",
            size=len(data),
            last_used=self._clock(),
        )
        with self._lock:
            previous = self._entries.get((plugin, file_id))
            self._entries[(plugin, file_id)] = entry
        if previous is not None and previous.path != path:
            _unlink(previous.path)
        return entry

    def _drop(self, key: tuple[str, str]) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            _unlink(entry.path)

    def sweep(self) -> None:
        """Delete spool files nobody has read for ``DRIVE_SPOOL_IDLE_SECONDS``."""
        now = self._clock()
        with self._lock:
            stale = [k for k, e in self._entries.items() if now - e.last_used > DRIVE_SPOOL_IDLE_SECONDS]
            entries = [self._entries.pop(k) for k in stale]
        for entry in entries:
            _unlink(entry.path)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
        self._remove_all_files()

    def _remove_all_files(self) -> None:
        for child in self._root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                _unlink(child)
