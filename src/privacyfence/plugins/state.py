"""The enabled-plugin state file, ``plugins-state.json`` in the data root.

One record per plugin a human has enabled: the version, the sha256 hashes of the executable and
the manifest as reviewed, the gate floor the human approved, and the tool signatures they saw
(ADR 0121). A plugin whose hashes no longer match is disabled until someone enables it again.
The file is written atomically with mode 0600, and a file that cannot be parsed reads as empty,
so every plugin stays disabled rather than starting on a guess (fail closed).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from privacyfence.plugins.trust import DiscoveredPlugin
from privacyfence.secure_files import atomic_write_json

logger = logging.getLogger(__name__)

STATE_FILENAME = "plugins-state.json"
STATE_VERSION = 1
HASH_DRIFT_REASON = "executable or manifest changed, enable again"

ToolSignature = tuple[str, str, bool, bool, tuple[str, ...]]


class _CorruptStateError(ValueError):
    pass


@dataclass(frozen=True)
class PluginRecord:
    enabled: bool
    version: str
    executable_sha256: str
    manifest_sha256: str
    max_gate_floor: str
    enabled_at: str
    disabled_reason: str | None
    reviewed_tools: tuple[ToolSignature, ...]

    @property
    def reviewed(self) -> frozenset[ToolSignature]:
        """The reviewed signatures, in the shape ``tools.validate_tool_defs(reviewed=...)`` takes."""
        return frozenset(self.reviewed_tools)

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "version": self.version,
            "executable_sha256": self.executable_sha256,
            "manifest_sha256": self.manifest_sha256,
            "max_gate_floor": self.max_gate_floor,
            "enabled_at": self.enabled_at,
            "disabled_reason": self.disabled_reason,
            "reviewed_tools": [[n, g, r, d, list(s)] for n, g, r, d, s in self.reviewed_tools],
        }

    @classmethod
    def from_json(cls, raw: Any) -> PluginRecord:
        if not isinstance(raw, dict):
            raise _CorruptStateError("a plugin record is not an object")
        try:
            record = cls(
                enabled=raw["enabled"],
                version=raw["version"],
                executable_sha256=raw["executable_sha256"],
                manifest_sha256=raw["manifest_sha256"],
                max_gate_floor=raw["max_gate_floor"],
                enabled_at=raw["enabled_at"],
                disabled_reason=raw["disabled_reason"],
                reviewed_tools=tuple(_signature(s) for s in raw["reviewed_tools"]),
            )
        except (KeyError, TypeError) as exc:
            raise _CorruptStateError(f"a plugin record is malformed: {exc!r}") from None
        strings = (record.version, record.executable_sha256, record.manifest_sha256,
                   record.max_gate_floor, record.enabled_at)
        if (not isinstance(record.enabled, bool) or not all(isinstance(s, str) for s in strings)
                or not (record.disabled_reason is None or isinstance(record.disabled_reason, str))):
            raise _CorruptStateError("a plugin record has a field of the wrong type")
        return record


def _signature(raw: Any) -> ToolSignature:
    if not isinstance(raw, (list, tuple)) or len(raw) != 5:
        raise TypeError("a reviewed tool signature must have five entries")
    name, gate, read_only, destructive, scopes = raw
    if (not isinstance(name, str) or not isinstance(gate, str) or not isinstance(read_only, bool)
            or not isinstance(destructive, bool) or not isinstance(scopes, (list, tuple))
            or not all(isinstance(s, str) for s in scopes)):
        raise TypeError("a reviewed tool signature has a field of the wrong type")
    return (name, gate, read_only, destructive, tuple(scopes))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PluginStateStore:
    """Reads and writes ``plugins-state.json``. Every write rereads the file first, under a lock."""

    def __init__(self, path: Path, *, clock: Callable[[], str] = _now) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()

    def load(self) -> dict[str, PluginRecord]:
        """Every record. A missing file is empty; a corrupt one is logged and read as empty."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read %s, so every plugin stays disabled: %s", self.path, exc)
            return {}
        try:
            return self._parse(text)
        except ValueError as exc:
            logger.warning("%s is corrupt, so every plugin stays disabled: %s", self.path, exc)
            return {}

    @staticmethod
    def _parse(text: str) -> dict[str, PluginRecord]:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise _CorruptStateError(f"expected an object with version {STATE_VERSION}")
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            raise _CorruptStateError("plugins must be an object")
        return {str(name): PluginRecord.from_json(raw) for name, raw in plugins.items()}

    def _save(self, records: dict[str, PluginRecord]) -> None:
        payload = {
            "version": STATE_VERSION,
            "plugins": {name: records[name].to_json() for name in sorted(records)},
        }
        atomic_write_json(self.path, payload, mode=0o600, indent=2)

    def enable(
        self,
        name: str,
        *,
        version: str,
        executable_sha256: str,
        manifest_sha256: str,
        max_gate_floor: str,
        reviewed_tools: Iterable[tuple],
    ) -> PluginRecord:
        """Record ``name`` as enabled with what the human reviewed, replacing any earlier record."""
        record = PluginRecord(
            enabled=True,
            version=version,
            executable_sha256=executable_sha256,
            manifest_sha256=manifest_sha256,
            max_gate_floor=max_gate_floor,
            enabled_at=self._clock(),
            disabled_reason=None,
            reviewed_tools=tuple(sorted(_signature(s) for s in reviewed_tools)),
        )
        with self._lock:
            records = self.load()
            records[name] = record
            self._save(records)
        return record

    def disable(self, name: str, reason: str) -> None:
        """Mark ``name`` disabled with ``reason``. A plugin with no record has nothing to disable."""
        with self._lock:
            records = self.load()
            current = records.get(name)
            if current is None:
                return
            records[name] = PluginRecord(
                enabled=False,
                version=current.version,
                executable_sha256=current.executable_sha256,
                manifest_sha256=current.manifest_sha256,
                max_gate_floor=current.max_gate_floor,
                enabled_at=current.enabled_at,
                disabled_reason=reason,
                reviewed_tools=current.reviewed_tools,
            )
            self._save(records)

    def forget(self, name: str) -> None:
        """Drop ``name``'s record entirely (the plugin was uninstalled)."""
        with self._lock:
            records = self.load()
            if records.pop(name, None) is not None:
                self._save(records)

    def check_hashes(self, discovered: DiscoveredPlugin) -> str | None:
        """Disable an enabled plugin whose executable or manifest no longer hashes to what was
        reviewed, and return the reason; ``None`` when nothing changed or nothing is enabled.

        An unreadable file hashes to ``""``, which never matches a recorded hash.
        """
        record = self.load().get(discovered.dir_name)
        if record is None or not record.enabled:
            return None
        if (discovered.executable_sha256 == record.executable_sha256 != ""
                and discovered.manifest_sha256 == record.manifest_sha256 != ""):
            return None
        logger.warning("Plugin %s changed since it was enabled; disabling it", discovered.dir_name)
        self.disable(discovered.dir_name, HASH_DRIFT_REASON)
        return HASH_DRIFT_REASON
