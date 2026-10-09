"""Copied from
privacyfence.plugins.source_ops; tests/unit/plugin_sdk/test_testhost_params.py compares them.

The daemon refuses a malformed ``source.call`` before it touches a connector. ``validate`` raises
the same ``invalid_params`` error with the same detail, checking in the same order.
"""
from __future__ import annotations

import json
from typing import Any

from .._rpc import RpcError

_MAX_ID_CHARS = 256
_MAX_RANGE_CHARS = 512
_MAX_TIME_CHARS = 64
_MAX_JQL_CHARS = 8192
_MAX_REPORT_COLUMNS = 100
CURSOR_MAX_CHARS = 4096
JIRA_PAGE_SIZE_MAX = 100
CALENDAR_PAGE_SIZE_MAX = 250
_LEGACY_JIRA_MAX_RESULTS = 500
DRIVE_CHUNK_BYTES = 8 * 1024 * 1024
_VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
REPORT_FILTER_OPERATORS = frozenset({
    "equals", "notEqual", "lessThan", "greaterThan", "lessOrEqual", "greaterOrEqual",
    "contains", "notContain", "startsWith", "includes", "excludes", "within",
})
_FILTERS_SHAPE_ERROR = (
    'filters: must be a JSON array of {"column", "operator", "value"} objects, '
    "value a string or a list of strings"
)


def _bad(detail: str) -> RpcError:
    return RpcError("invalid_params", detail)


def _str(params: dict, key: str, *, max_len: int, required: bool = True, default: str | None = None) -> str | None:
    value = params.get(key)
    if value is None:
        if required and default is None:
            raise _bad(f"params.{key} is required")
        return default
    if not isinstance(value, str):
        raise _bad(f"params.{key} must be a string")
    if not value:
        raise _bad(f"params.{key} must not be empty")
    if len(value) > max_len:
        raise _bad(f"params.{key} is longer than {max_len} characters")
    return value


def _int(params: dict, key: str, *, low: int, high: int | None, default: int | None = None) -> int | None:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"params.{key} must be an integer")
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise _bad(f"params.{key} must be {bound}")
    return value


def _cursor(params: dict) -> None:
    _str(params, "cursor", max_len=CURSOR_MAX_CHARS, required=False)


def _page_size(params: dict, *, high: int, legacy_high: int) -> None:
    if _int(params, "page_size", low=1, high=high) is None:
        _int(params, "max_results", low=1, high=legacy_high)


def _filters(params: dict) -> None:
    filters = params.get("filters")
    if filters is None:
        return
    if not isinstance(filters, list):
        raise _bad("params.filters must be a list")
    for flt in filters:
        if isinstance(flt, dict) and isinstance(flt.get("operator"), str):
            if flt["operator"] not in REPORT_FILTER_OPERATORS:
                raise _bad("params.filters has an unknown operator")
    try:
        _parse_report_filters(json.dumps(filters))
    except (ValueError, TypeError) as exc:
        raise _bad(str(exc)) from None


def _parse_report_filters(raw: str) -> None:
    """The shape rules of the daemon's ``_parse_report_filters``, without building the filters."""
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(_FILTERS_SHAPE_ERROR)
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(_FILTERS_SHAPE_ERROR)
        column, operator, value = item.get("column"), item.get("operator"), item.get("value")
        if not isinstance(column, str) or not column.strip() or not isinstance(operator, str):
            raise ValueError(_FILTERS_SHAPE_ERROR)
        if not isinstance(value, str) and not (
            isinstance(value, list) and value and all(isinstance(v, str) for v in value)
        ):
            raise ValueError(_FILTERS_SHAPE_ERROR)


def _salesforce(params: dict) -> None:
    _str(params, "report_id", max_len=_MAX_ID_CHARS)
    _filters(params)
    columns = params.get("columns")
    if columns is not None:
        if (
            not isinstance(columns, list)
            or not 1 <= len(columns) <= _MAX_REPORT_COLUMNS
            or not all(isinstance(c, str) and 0 < len(c) <= _MAX_ID_CHARS for c in columns)
        ):
            raise _bad("params.columns must be a list of column names")
    page_by = _str(params, "page_by", max_len=_MAX_ID_CHARS, required=False)
    cursor = _str(params, "cursor", max_len=CURSOR_MAX_CHARS, required=False)
    if cursor is not None and page_by is None:
        raise _bad("cursor needs page_by")


def _jira(params: dict) -> None:
    _str(params, "jql", max_len=_MAX_JQL_CHARS)
    _page_size(params, high=JIRA_PAGE_SIZE_MAX, legacy_high=_LEGACY_JIRA_MAX_RESULTS)
    _cursor(params)


def _drive_download(params: dict) -> None:
    _str(params, "file_id", max_len=_MAX_ID_CHARS)
    _int(params, "length", low=1, high=DRIVE_CHUNK_BYTES, default=DRIVE_CHUNK_BYTES)
    offset = _int(params, "offset", low=0, high=None)
    cursor = _str(params, "cursor", max_len=CURSOR_MAX_CHARS, required=False)
    if offset is not None and cursor is not None:
        raise _bad("offset and cursor cannot both be given")


def _sheets(params: dict) -> None:
    option = _str(params, "value_render_option", max_len=32, required=False, default="FORMATTED_VALUE")
    if option not in _VALUE_RENDER_OPTIONS:
        raise _bad(f"params.value_render_option must be one of {', '.join(_VALUE_RENDER_OPTIONS)}")
    _str(params, "spreadsheet_id", max_len=_MAX_ID_CHARS)
    _str(params, "range", max_len=_MAX_RANGE_CHARS)
    _cursor(params)


def _confluence(params: dict) -> None:
    _str(params, "page_id", max_len=_MAX_ID_CHARS)
    _cursor(params)


def _calendar(params: dict) -> None:
    _str(params, "calendar_id", max_len=_MAX_ID_CHARS, required=False, default="primary")
    _str(params, "time_min", max_len=_MAX_TIME_CHARS)
    _str(params, "time_max", max_len=_MAX_TIME_CHARS)
    _page_size(params, high=CALENDAR_PAGE_SIZE_MAX, legacy_high=CALENDAR_PAGE_SIZE_MAX)
    _cursor(params)


_VALIDATORS = {
    "salesforce.report_run": _salesforce,
    "jira.search": _jira,
    "drive.download": _drive_download,
    "sheets.get_values": _sheets,
    "confluence.get_page": _confluence,
    "calendar.list_events": _calendar,
}


def validate(operation: str, params: dict[str, Any]) -> None:
    """Raise ``RpcError("invalid_params", detail)`` as the daemon does for a malformed call."""
    _VALIDATORS[operation](params)
