"""Chunked Drive downloads for ``source.call`` (ADR 0123).

Binary files are read with ranged requests and are never stored. Google-native files cannot be
ranged, so the first call for one exports it once, writes it under a spool directory the plugin
cannot see (owner-only, never named after the Drive file) and serves 8 MiB chunks from there. Each
later call re-reads the file's metadata and compares its ``modified_time`` with the one the spool
holds, so a file edited between chunks is reported rather than served as a Frankenstein of two
versions. Spool files idle for ten minutes are swept on every
call, and everything is removed when the host shuts down.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privacyfence.drive_client import _GOOGLE_DOC_EXPORTS
from privacyfence.plugins.constants import DRIVE_SPOOL_IDLE_SECONDS
from privacyfence.plugins.protocol import RpcError
from privacyfence.secure_files import atomic_write_bytes, secure_mkdir

logger = logging.getLogger(__name__)


def _revision_changed() -> RpcError:
    return RpcError(
        "upstream_error",
        "the file changed while it was being downloaded",
        extra={"reason": "revision_changed"},
    )


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

    def read_chunk_at(
        self,
        client: Any,
        plugin: str,
        file_id: str,
        *,
        offset: int,
        length: int,
        expected_revision: str | None,
    ) -> tuple[dict, int | None, str]:
        """Serve one chunk at ``offset``. Returns ``(data, next_offset or None at eof, revision)``.

        Binary files are read with a ranged request and never spooled, so there is no size
        limit. Google-native files cannot be ranged: they are exported once into the spool and
        served from there.
        """
        self.sweep()
        metadata = client.get_file_metadata(file_id)
        revision = metadata.modified_time
        key = (plugin, file_id)
        if expected_revision is not None and expected_revision != revision:
            self._drop(key)
            raise _revision_changed()

        if metadata.mime_type not in _GOOGLE_DOC_EXPORTS:
            total = metadata.size
            mime_type = metadata.mime_type or "application/octet-stream"
            if offset > total:
                raise RpcError("invalid_params", "offset is past the end of the file")
            wanted = min(length, total - offset)
            chunk = client.download_range(file_id, offset, wanted) if wanted > 0 else b""
            if len(chunk) != wanted:
                raise _revision_changed()
        else:
            with self._lock:
                entry = self._entries.get(key)
            if entry is None or entry.revision != revision or not entry.path.is_file():
                entry = self._fetch(client, plugin, file_id, metadata)
            entry.last_used = self._clock()
            total = entry.size
            mime_type = entry.mime_type
            if offset > total:
                raise RpcError("invalid_params", "offset is past the end of the file")
            with entry.path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(min(length, total - offset))

        end = offset + len(chunk)
        eof = end >= total
        data = {
            "file_id": file_id,
            "mime_type": mime_type,
            "revision": revision,
            "total_size_bytes": total,
            "offset": offset,
            "length": len(chunk),
            "eof": eof,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
        }
        return data, None if eof else end, revision

    def _fetch(self, client: Any, plugin: str, file_id: str, metadata: Any) -> _Entry:
        revision = metadata.modified_time
        fetched = client.download_file_bytes(file_id)
        data = fetched["data"]
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
