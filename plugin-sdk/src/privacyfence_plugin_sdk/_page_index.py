"""The page index rules, as the daemon applies them to a ``pages.list`` result.

The SDK never imports PrivacyFence, so these rules are a copy of the daemon's; a test keeps the
limits equal.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from .blocks import clean_line

_MAX_PAGE_INDEX_ENTRIES = 500
_MAX_PAGE_PATH_CHARS = 512
_MAX_TITLE_CHARS = 120
_MAX_PAGE_VERSION_CHARS = 40
_MAX_PAGE_DESCRIPTION_CHARS = 200
_PAGE_ENTRY_PATH_RE = re.compile(r"/[\x21\x22\x24-\x5b\x5d-\x7e]*")  # always .fullmatch(); no space, # or backslash
_KEYS = frozenset({"path", "title", "version", "created_at", "updated_at", "description"})
_ONE_LINE = "must not contain line breaks, tabs, control or bidirectional characters"


def normalized_page_path(raw: str) -> str | None:
    """URL-decode ``raw`` once and apply the daemon's path rules; ``None`` means the path is rejected."""
    if len(raw) > 3 * _MAX_PAGE_PATH_CHARS:
        return None
    path = unquote(raw)
    if "\x00" in path or "\\" in path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > _MAX_PAGE_PATH_CHARS or "//" in path or ".." in path.split("/"):
        return None
    return path


def _text(entry: dict, key: str, where: str, *, required: bool, max_len: int) -> str | None:
    value = entry.get(key)
    if value is None:
        if required:
            raise ValueError(f"{where}.{key} is required" if key not in entry else f"{where}.{key} must be a string")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} must be a string")
    if not value:
        raise ValueError(f"{where}.{key} must not be empty")
    if len(value) > max_len:
        raise ValueError(f"{where}.{key} is longer than {max_len} characters")
    return value


def _one_line(entry: dict, key: str, where: str, *, max_len: int) -> None:
    value = _text(entry, key, where, required=False, max_len=max_len)
    if value is not None and clean_line(value) != value:
        raise ValueError(f"{where}.{key} {_ONE_LINE}")


def _timestamp(entry: dict, key: str, where: str) -> None:
    value = _text(entry, key, where, required=False, max_len=1 << 16)
    if value is None:
        return
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{where}.{key} must be an RFC 3339 timestamp with a time zone") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{where}.{key} must be an RFC 3339 timestamp with a time zone")
    try:
        parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError(f"{where}.{key} must be an RFC 3339 timestamp with a time zone") from None


def _validate_entry(entry: Any, where: str) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"{where} must be an object")
    path = _text(entry, "path", where, required=True, max_len=_MAX_PAGE_PATH_CHARS)
    assert path is not None
    if not _PAGE_ENTRY_PATH_RE.fullmatch(path):
        raise ValueError(f"{where}.path must start with \"/\" and hold only printable ASCII without space, # or \\")
    bare = path.partition("?")[0]
    if normalized_page_path(bare) != bare or any(seg in (".", "..") for seg in bare.split("/")):
        raise ValueError(f"{where}.path must be decoded, without empty or dot segments")
    title = _text(entry, "title", where, required=True, max_len=_MAX_TITLE_CHARS)
    assert title is not None
    if not title.strip():
        raise ValueError(f"{where}.title must not be only whitespace")
    if clean_line(title) != title:
        raise ValueError(f"{where}.title {_ONE_LINE}")
    _one_line(entry, "version", where, max_len=_MAX_PAGE_VERSION_CHARS)
    _timestamp(entry, "created_at", where)
    _timestamp(entry, "updated_at", where)
    _one_line(entry, "description", where, max_len=_MAX_PAGE_DESCRIPTION_CHARS)
    return {k: v for k, v in entry.items() if k in _KEYS and v is not None}


def validate_page_entries(entries: list[dict]) -> list[dict]:
    """The entries a ``pages.list`` result may carry, or ``ValueError`` with a ``pages[<i>].<field>`` detail."""
    if not isinstance(entries, list):
        raise ValueError("pages must be a list")
    if len(entries) > _MAX_PAGE_INDEX_ENTRIES:
        raise ValueError(f"pages has more than {_MAX_PAGE_INDEX_ENTRIES} entries")
    return [_validate_entry(e, f"pages[{i}]") for i, e in enumerate(entries)]
