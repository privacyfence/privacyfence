"""Block builders and local validation, with the same rules the daemon applies.

A block is data, never markup. The builders return sanitized dicts and raise ``ValueError`` on a
wrong type or field, so a mistake shows up in the plugin's own tests instead of as an
``invalid_blocks`` error from PrivacyFence.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

# Limits copied from the protocol; a test compares them with the daemon's constants.
_MAX_PREVIEW_BLOCKS = 50
_MAX_PREVIEW_BYTES = 64 * 1024
_MAX_CELL_CHARS = 4096
_MAX_FIELD_ITEMS = 50
_MAX_TABLE_COLUMNS = 20
_BLOCK_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

_STRIP = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩‎‏؜]")
_LANGUAGE = re.compile(r"[a-z0-9+#-]{1,20}")

__all__ = [
    "code", "diff", "fields", "heading", "table", "text", "validate_blocks",
]


def _clean(value: str) -> str:
    return _STRIP.sub("", value)


def _only(block: dict, allowed: set[str], where: str) -> None:
    extra = set(block) - allowed - {"type"}
    if extra:
        raise ValueError(f"{where}: unexpected field {sorted(extra)[0]}")


def _string(block: dict, key: str, where: str) -> str:
    value = block.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    return _clean(value)


def _validate_one(block: Any, index: int) -> dict:
    where = f"block {index}"
    if not isinstance(block, dict):
        raise ValueError(f"{where}: must be an object")
    kind = block.get("type")
    if kind == "heading":
        _only(block, {"text", "level"}, where)
        level = block.get("level", 2)
        if level not in (2, 3) or isinstance(level, bool):
            raise ValueError(f"{where}: level must be 2 or 3")
        return {"type": "heading", "text": _string(block, "text", where), "level": level}
    if kind == "fields":
        _only(block, {"items"}, where)
        items = block.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= _MAX_FIELD_ITEMS:
            raise ValueError(f"{where}: items must be a list of 1 to {_MAX_FIELD_ITEMS} entries")
        out = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"label", "value"}:
                raise ValueError(f"{where}: each item needs exactly a label and a value")
            out.append({"label": _string(item, "label", where), "value": _string(item, "value", where)})
        return {"type": "fields", "items": out}
    if kind == "table":
        _only(block, {"columns", "rows"}, where)
        return _validate_table(block, where)
    if kind == "text":
        _only(block, {"text"}, where)
        return {"type": "text", "text": _string(block, "text", where)}
    if kind == "code":
        _only(block, {"text", "language"}, where)
        out = {"type": "code", "text": _string(block, "text", where)}
        language = block.get("language")
        if language is not None:
            if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
                raise ValueError(f"{where}: language must match [a-z0-9+#-]{{1,20}}")
            out["language"] = language
        return out
    if kind == "diff":
        _only(block, {"format", "text"}, where)
        if block.get("format") != "unified":
            raise ValueError(f"{where}: format must be 'unified'")
        return {"type": "diff", "format": "unified", "text": _string(block, "text", where)}
    raise ValueError(f"{where}: unknown block type")


def _validate_table(block: dict, where: str) -> dict:
    columns = block.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= _MAX_TABLE_COLUMNS:
        raise ValueError(f"{where}: columns must be a list of 1 to {_MAX_TABLE_COLUMNS} entries")
    cleaned_columns = []
    keys: set[str] = set()
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"key", "label"}:
            raise ValueError(f"{where}: each column needs exactly a key and a label")
        key = column["key"]
        if not isinstance(key, str) or not _BLOCK_KEY_RE.fullmatch(key):
            raise ValueError(f"{where}: column keys must match {_BLOCK_KEY_RE.pattern}")
        if key in keys:
            raise ValueError(f"{where}: column keys must be unique")
        keys.add(key)
        cleaned_columns.append({"key": key, "label": _string(column, "label", where)})
    rows = block.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{where}: rows must be a list")
    cleaned_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{where}: each row must be an object")
        extra = set(row) - keys
        if extra:
            raise ValueError(f"{where}: row uses undeclared key {sorted(extra)[0]}")
        cleaned_row: dict[str, Any] = {}
        for key, cell in row.items():
            if isinstance(cell, str):
                cell = _clean(cell)
                if len(cell) > _MAX_CELL_CHARS:
                    cell = cell[: _MAX_CELL_CHARS - 1] + "…"
            elif cell is not None and not isinstance(cell, (int, float, bool)):
                raise ValueError(f"{where}: a cell must be a string, number, boolean or null")
            elif isinstance(cell, float) and not math.isfinite(cell):
                raise ValueError(f"{where}: must be a finite number")
            cleaned_row[key] = cell
        cleaned_rows.append(cleaned_row)
    return {"type": "table", "columns": cleaned_columns, "rows": cleaned_rows}


def validate_blocks(
    blocks: Any, *, max_blocks: int = _MAX_PREVIEW_BLOCKS, max_bytes: int | None = _MAX_PREVIEW_BYTES
) -> list[dict]:
    """Sanitized copies of ``blocks``; raises ``ValueError`` when any block or the list is invalid.

    Control characters and bidirectional-override characters are stripped from every string, and
    table cells longer than the cell limit are cut with a trailing ellipsis. ``max_bytes=None``
    turns the serialized-size cap off, which is what payloads use.
    """
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")
    if len(blocks) > max_blocks:
        raise ValueError(f"at most {max_blocks} blocks are allowed")
    out = [_validate_one(block, index) for index, block in enumerate(blocks)]
    if max_bytes is not None:
        size = len(json.dumps(out, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size > max_bytes:
            raise ValueError(f"blocks serialize to {size} bytes; the limit is {max_bytes}")
    return out


def heading(text: str, level: int = 2) -> dict:
    return _validate_one({"type": "heading", "text": text, "level": level}, 0)


def fields(mapping: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> dict:
    """A label/value list. Values are converted with ``str``."""
    pairs = list(mapping.items()) if isinstance(mapping, Mapping) else list(mapping)
    items = [{"label": str(label), "value": str(value)} for label, value in pairs]
    return _validate_one({"type": "fields", "items": items}, 0)


def table(columns: Sequence[tuple[str, str]], rows: Sequence[Mapping[str, Any]]) -> dict:
    """A table. ``columns`` is ``[(key, label), ...]`` and each row maps a declared key to a cell."""
    return _validate_one(
        {
            "type": "table",
            "columns": [{"key": key, "label": label} for key, label in columns],
            "rows": [dict(row) for row in rows],
        },
        0,
    )


def text(s: str) -> dict:
    return _validate_one({"type": "text", "text": s}, 0)


def code(s: str, language: str | None = None) -> dict:
    block: dict[str, Any] = {"type": "code", "text": s}
    if language is not None:
        block["language"] = language
    return _validate_one(block, 0)


def diff(s: str) -> dict:
    return _validate_one({"type": "diff", "format": "unified", "text": s}, 0)
