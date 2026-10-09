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
import json
import logging
import secrets
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

# Sheets snapshots one plugin may hold at once; the least recently used is evicted past this.
_SNAPSHOTS_PER_PLUGIN = 4


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


@dataclass
class _RowsEntry:
    path: Path
    count: int
    last_used: float


def _row_line(row: Any) -> bytes:
    """One row as a JSON line, with the default separators so the size matches ``source_ops``."""
    try:
        return json.dumps(row, default=str, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        # A lone surrogate cannot be UTF-8 encoded; escaping it can.
        return json.dumps(row, default=str).encode()


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
        self._rows: dict[tuple[str, str], _RowsEntry] = {}
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

        if metadata.mime_type.startswith("application/vnd.google-apps.") and metadata.mime_type not in _GOOGLE_DOC_EXPORTS:
            raise RpcError(
                "invalid_params",
                f"files of type {metadata.mime_type} cannot be downloaded",
                extra={"reason": "not_downloadable"},
            )

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

    def put_rows(self, plugin: str, rows: list) -> str:
        """Hold a Sheets read so later pages serve the same version. Returns the snapshot id."""
        self.sweep()
        snapshot = secrets.token_hex(8)
        data = b"\n".join(_row_line(row) for row in rows)
        path = self._root / plugin / f"sheets-{snapshot}.jsonl"
        secure_mkdir(path.parent, 0o700)
        atomic_write_bytes(path, data, mode=0o600)
        evicted: list[_RowsEntry] = []
        with self._lock:
            self._rows[(plugin, snapshot)] = _RowsEntry(path=path, count=len(rows), last_used=self._clock())
            held = sorted((e.last_used, k) for k, e in self._rows.items() if k[0] == plugin)
            for _, key in held[: max(0, len(held) - _SNAPSHOTS_PER_PLUGIN)]:
                evicted.append(self._rows.pop(key))
        for entry in evicted:
            _unlink(entry.path)
        return snapshot

    def rows_page(self, plugin: str, snapshot: str, first: int, budget: int) -> tuple[list, int, int]:
        """Rows of a held snapshot from ``first`` that fit ``budget`` bytes as a JSON list.

        Returns ``(rows, end, total)``. ``KeyError`` when the snapshot is unknown or gone.
        """
        self.sweep()
        key = (plugin, snapshot)
        with self._lock:
            entry = self._rows.get(key)
            if entry is not None:
                entry.last_used = self._clock()
        if entry is None:
            raise KeyError(snapshot)
        try:
            raw = entry.path.read_bytes()
        except OSError:
            raise KeyError(snapshot) from None
        # Split on b"\n" only: str.splitlines also splits on U+2028, U+2029 and U+0085, which stay
        # raw inside a JSON string.
        lines = raw.split(b"\n") if entry.count else []
        total = entry.count
        if first >= total:
            return [], first, total
        size = 2
        end = first
        while end < total:
            added = len(lines[end]) + (2 if end > first else 0)
            if size + added > budget:
                break
            size += added
            end += 1
        if end == first:
            raise RpcError("payload_too_large", "a single record is larger than the page limit")
        return [json.loads(line) for line in lines[first:end]], end, total

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
        with self._lock:
            stale_rows = [k for k, e in self._rows.items() if now - e.last_used > DRIVE_SPOOL_IDLE_SECONDS]
            rows = [self._rows.pop(k) for k in stale_rows]
        for row in rows:
            _unlink(row.path)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._rows.clear()
        self._remove_all_files()

    def _remove_all_files(self) -> None:
        for child in self._root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                _unlink(child)
