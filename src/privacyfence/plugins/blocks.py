"""Block validation and conversion for plugin previews and payloads.

A block is data, never markup. Everything a plugin sends is validated on arrival, sanitized (control
and bidirectional-override characters removed, so a plugin cannot reorder or hide text on an
approval card) and then only ever handed on as plain strings: ``to_card_blocks`` keeps ``"<b>"``
as literal text and the card renderer escapes it. Anything invalid fails the whole list.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from privacyfence.plugins.constants import MAX_CELL_CHARS, MAX_PREVIEW_BLOCKS, MAX_PREVIEW_BYTES

_LANGUAGE_RE = re.compile(r"[a-z0-9+#-]{1,20}")
_MAX_FIELD_ITEMS = 50
_MAX_COLUMNS = 20

# C0 and C1 controls except \n and \t, DEL, and the bidi controls U+202A-U+202E, U+2066-U+2069,
# U+200E, U+200F and U+061C.
_STRIP_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")


class BlockError(ValueError):
    """The block list is not valid."""


def _clean(value: str) -> str:
    return _STRIP_RE.sub("", value)


def _obj(value: Any, where: str, required: frozenset[str] | set[str], optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise BlockError(f"{where} must be an object")
    missing = required - value.keys()
    if missing:
        raise BlockError(f"{where} is missing {sorted(missing)[0]!r}")
    extra = value.keys() - required - optional
    if extra:
        raise BlockError(f"{where} has unknown field {sorted(extra, key=str)[0]!r}")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise BlockError(f"{where} must be a string")
    return _clean(value)


def _key(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BlockError(f"{where} must be a non-empty string")
    return value


def _cell(value: Any, where: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BlockError(f"{where} must be a finite number")
        return value
    if isinstance(value, str):
        text = _clean(value)
        if len(text) > MAX_CELL_CHARS:
            text = text[: MAX_CELL_CHARS - 1] + "…"
        return text
    raise BlockError(f"{where} must be a string, number, boolean or null")


def _validate_heading(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "text"}, frozenset({"level"}))
    out = {"type": "heading", "text": _str(block["text"], f"{where}.text")}
    if "level" in block:
        level = block["level"]
        if isinstance(level, bool) or level not in (2, 3):
            raise BlockError(f"{where}.level must be 2 or 3")
        out["level"] = level
    return out


def _validate_fields(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "items"})
    items = block["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= _MAX_FIELD_ITEMS:
        raise BlockError(f"{where}.items must be a list of 1 to {_MAX_FIELD_ITEMS} items")
    clean = []
    for i, item in enumerate(items):
        w = f"{where}.items[{i}]"
        _obj(item, w, {"label", "value"})
        clean.append({"label": _str(item["label"], f"{w}.label"),
                      "value": _str(item["value"], f"{w}.value")})
    return {"type": "fields", "items": clean}


def _validate_table(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "columns", "rows"})
    columns = block["columns"]
    if not isinstance(columns, list) or not 1 <= len(columns) <= _MAX_COLUMNS:
        raise BlockError(f"{where}.columns must be a list of 1 to {_MAX_COLUMNS} columns")
    clean_cols = []
    keys: set[str] = set()
    for i, col in enumerate(columns):
        w = f"{where}.columns[{i}]"
        _obj(col, w, {"key", "label"})
        key = _key(col["key"], f"{w}.key")
        if key in keys:
            raise BlockError(f"{w}.key {key!r} is declared twice")
        keys.add(key)
        clean_cols.append({"key": key, "label": _str(col["label"], f"{w}.label")})
    rows = block["rows"]
    if not isinstance(rows, list):
        raise BlockError(f"{where}.rows must be a list")
    clean_rows = []
    for r, row in enumerate(rows):
        w = f"{where}.rows[{r}]"
        if not isinstance(row, dict):
            raise BlockError(f"{w} must be an object")
        undeclared = row.keys() - keys
        if undeclared:
            raise BlockError(f"{w} uses undeclared key {sorted(undeclared, key=str)[0]!r}")
        clean_rows.append({k: _cell(v, f"{w}.{k}") for k, v in row.items()})
    return {"type": "table", "columns": clean_cols, "rows": clean_rows}


def _validate_text(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "text"})
    return {"type": "text", "text": _str(block["text"], f"{where}.text")}


def _validate_code(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "text"}, frozenset({"language"}))
    out = {"type": "code", "text": _str(block["text"], f"{where}.text")}
    if "language" in block:
        language = block["language"]
        if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
            raise BlockError(f"{where}.language must match [a-z0-9+#-]{{1,20}}")
        out["language"] = language
    return out


def _validate_diff(block: dict, where: str) -> dict:
    _obj(block, where, {"type", "format", "text"})
    if block["format"] != "unified":
        raise BlockError(f"{where}.format must be 'unified'")
    return {"type": "diff", "format": "unified", "text": _str(block["text"], f"{where}.text")}


_VALIDATORS = {
    "heading": _validate_heading,
    "fields": _validate_fields,
    "table": _validate_table,
    "text": _validate_text,
    "code": _validate_code,
    "diff": _validate_diff,
}


def validate_blocks(
    blocks: Any,
    *,
    max_blocks: int = MAX_PREVIEW_BLOCKS,
    max_bytes: int | None = MAX_PREVIEW_BYTES,
) -> list[dict]:
    """Return sanitized copies of ``blocks`` or raise ``BlockError``.

    ``max_bytes=None`` means no byte cap; payloads use the inline-result check instead.
    """
    if not isinstance(blocks, list):
        raise BlockError("blocks must be a list")
    if len(blocks) > max_blocks:
        raise BlockError(f"more than {max_blocks} blocks")
    out = []
    for i, block in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(block, dict):
            raise BlockError(f"{where} must be an object")
        kind = block.get("type")
        validator = _VALIDATORS.get(kind) if isinstance(kind, str) else None
        if validator is None:
            raise BlockError(f"{where} has an unknown type")
        out.append(validator(block, where))
    if max_bytes is not None:
        size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode())
        if size > max_bytes:
            raise BlockError(f"blocks are larger than {max_bytes} bytes")
    return out


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value)


def to_card_blocks(blocks: list[dict]) -> list[dict]:
    """Map validated blocks to the approval card's block vocabulary (plain strings only)."""
    out: list[dict] = []
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            out.append({"type": "heading", "label": block["text"]})
        elif kind == "fields":
            out.extend({"type": "field", "label": i["label"], "value": i["value"]}
                       for i in block["items"])
        elif kind == "table":
            keys = [c["key"] for c in block["columns"]]
            out.append({
                "type": "table",
                "headers": [c["label"] for c in block["columns"]],
                "rows": [[_cell_text(row.get(k)) for k in keys] for row in block["rows"]],
            })
        elif kind == "text":
            out.append({"type": "text", "text": block["text"]})
        elif kind == "code":
            out.append({"type": "code", "text": block["text"],
                        "language": block.get("language") or ""})
        elif kind == "diff":
            out.append({"type": "diff", "text": block["text"]})
    return out


def fields_dict(blocks: list[dict]) -> dict[str, str]:
    """Every ``fields`` item in order as ``{label: value}``; repeats become "label (2)", ..."""
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for block in blocks:
        if block["type"] != "fields":
            continue
        for item in block["items"]:
            label = item["label"]
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} ({seen[label]})"
            out[label] = item["value"]
    return out


def flatten_text(blocks: list[dict]) -> str:
    """Every string in the blocks, one per line, for the PII scan."""
    lines: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "fields":
            for item in block["items"]:
                lines += [item["label"], item["value"]]
        elif kind == "table":
            keys = [c["key"] for c in block["columns"]]
            lines += [c["label"] for c in block["columns"]]
            for row in block["rows"]:
                lines += [str(row[k]) for k in keys if row.get(k) is not None]
        else:
            lines.append(block["text"])
    return "\n".join(lines)
