"""The tool list model of a hosted upstream MCP server (ADR 0331).

Pure functions and frozen dataclasses for classifying a server's tools, choosing the gate a person
meets, digesting what an approval is pinned to, diffing a fetched list against the saved one, plus
``ToolStore``, the one file per server that holds the saved list. Nothing here logs, reaches the
network or keeps module-level mutable state; the runtime passes in everything it needs.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import secure_files

GATES = ("review", "popup", "auto")
MAX_TOOLS = 50
MAX_SCHEMA_BYTES = 32 * 1024
MAX_EXPOSED_NAME_CHARS = 64

_STORE_VERSION = 1
_NAME = re.compile(r"[A-Za-z0-9_-]{1,48}")
_ID = re.compile(r"[A-Za-z0-9_-]+")
_REF_KEYS = frozenset({"$ref", "$defs", "definitions"})
_TOO_MANY = f"more than {MAX_TOOLS} tools"


@dataclass(frozen=True)
class ToolEntry:
    name: str
    description: str
    input_schema: dict
    read_only: bool | None
    destructive: bool | None


@dataclass(frozen=True)
class SavedTool:
    name: str
    description: str
    input_schema: dict
    read_only: bool | None
    destructive: bool | None
    gate: str
    verb: str
    digest: str


@dataclass(frozen=True)
class SavedList:
    upstream_id: str
    url: str
    saved_at: str
    tools: tuple[SavedTool, ...]


@dataclass(frozen=True)
class ToolDiff:
    """``new``, ``changed`` and ``unchanged`` hold the fetched entries; ``removed`` the names of
    saved tools the server no longer lists as supported; ``unsupported`` each fetched entry that
    cannot be exposed, with the reason."""

    new: tuple[ToolEntry, ...]
    changed: tuple[ToolEntry, ...]
    removed: tuple[str, ...]
    unchanged: tuple[ToolEntry, ...]
    unsupported: tuple[tuple[ToolEntry, str], ...]


def classify(read_only: bool | None, destructive: bool | None) -> tuple[str, str]:
    """The class default gate and the policy verb. An absent destructive hint is destructive."""
    if read_only is True and destructive is not True:
        return "review", "read"
    if destructive is False:
        return "popup", "update"
    return "popup", "delete"


def person_gate(configured: str, class_default: str) -> str:
    """``auto`` is never enforced: it only lets a person make an Always allow rule."""
    return configured if configured in ("review", "popup") else class_default


def _has_ref(node: object) -> bool:
    if isinstance(node, dict):
        return any(key in _REF_KEYS or _has_ref(value) for key, value in node.items())
    if isinstance(node, list):
        return any(_has_ref(item) for item in node)
    return False


def unsupported_reason(upstream_id: str, entry: ToolEntry) -> str | None:
    if not _NAME.fullmatch(entry.name):
        return "its name is not 1-48 letters, digits, underscores or hyphens"
    if len(f"{upstream_id}_{entry.name}") > MAX_EXPOSED_NAME_CHARS:
        return "its exposed name is longer than 64 characters"
    schema = entry.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return "its input schema is not an object schema"
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not isinstance(schema.get("required", []), list):
        return "its input schema is not an object schema"
    if "reason" in properties:
        return "its input schema already has a reason property"
    if len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES:
        return "its input schema is larger than 32 KB"
    if _has_ref(schema):
        return "its input schema uses $ref, $defs or definitions"
    return None


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def tool_digest(entry: ToolEntry) -> str:
    return _digest({
        "name": entry.name,
        "description": entry.description,
        "input_schema": entry.input_schema,
        "read_only": entry.read_only,
        "destructive": entry.destructive,
    })


def declaration_digest(upstream_url: str, operation: str, verb: str, configured_gate: str, digest: str) -> str:
    """What an ADR 0300 approval is pinned to: a gate, verb, URL or tool change voids it."""
    return _digest({
        "gate": configured_gate, "operation": operation, "tool": digest, "url": upstream_url, "verb": verb,
    })


def diff(saved: tuple[SavedTool, ...], fetched: tuple[tuple[ToolEntry, str | None], ...]) -> ToolDiff:
    saved_digests = {tool.name: tool.digest for tool in saved}
    new: list[ToolEntry] = []
    changed: list[ToolEntry] = []
    unchanged: list[ToolEntry] = []
    unsupported: list[tuple[ToolEntry, str]] = []
    kept: set[str] = set()
    for entry, reason in fetched:
        if reason is None and len(kept) >= MAX_TOOLS:
            reason = _TOO_MANY
        if reason is not None:
            unsupported.append((entry, reason))
            continue
        kept.add(entry.name)
        known = saved_digests.get(entry.name)
        if known is None:
            new.append(entry)
        elif known == tool_digest(entry):
            unchanged.append(entry)
        else:
            changed.append(entry)
    removed = tuple(name for name in saved_digests if name not in kept)
    return ToolDiff(tuple(new), tuple(changed), removed, tuple(unchanged), tuple(unsupported))


def _tool_to_json(tool: SavedTool) -> dict:
    return {
        "name": tool.name, "description": tool.description, "input_schema": tool.input_schema,
        "read_only": tool.read_only, "destructive": tool.destructive,
        "gate": tool.gate, "verb": tool.verb, "digest": tool.digest,
    }


def _tool_from_json(raw: object) -> SavedTool:
    if not isinstance(raw, dict):
        raise ValueError("tool is not an object")
    tool = SavedTool(
        name=raw["name"], description=raw["description"], input_schema=raw["input_schema"],
        read_only=raw["read_only"], destructive=raw["destructive"],
        gate=raw["gate"], verb=raw["verb"], digest=raw["digest"],
    )
    texts = (tool.name, tool.description, tool.gate, tool.verb, tool.digest)
    if not all(isinstance(text, str) for text in texts) or not isinstance(tool.input_schema, dict):
        raise ValueError("tool field has the wrong type")
    if any(hint is not None and not isinstance(hint, bool) for hint in (tool.read_only, tool.destructive)):
        raise ValueError("tool hint is not a boolean")
    return tool


class ToolStore:
    """``<directory>/<id>.json``, written atomically with mode 0600."""

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    def _path(self, upstream_id: str) -> Path:
        if not _ID.fullmatch(upstream_id):
            raise ValueError("upstream id is not a plain identifier")
        return self._directory / f"{upstream_id}.json"

    def load(self, upstream_id: str) -> SavedList | None:
        try:
            raw = json.loads(self._path(upstream_id).read_text(encoding="utf-8"))
            if raw["version"] != _STORE_VERSION or raw["upstream_id"] != upstream_id:
                return None
            url, saved_at = raw["url"], raw["saved_at"]
            if not isinstance(url, str) or not isinstance(saved_at, str):
                return None
            return SavedList(upstream_id, url, saved_at, tuple(_tool_from_json(item) for item in raw["tools"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, upstream_id: str, url: str, tools: tuple[SavedTool, ...], now: datetime) -> None:
        secure_files.atomic_write_json(
            self._path(upstream_id),
            {
                "version": _STORE_VERSION, "upstream_id": upstream_id, "url": url,
                "saved_at": now.isoformat(), "tools": [_tool_to_json(tool) for tool in tools],
            },
            indent=2, sort_keys=True,
        )
