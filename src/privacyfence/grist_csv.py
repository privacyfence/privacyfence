"""Parsing and checking of a CSV file for ``grist_import_csv`` and ``grist_update_csv`` (ADR 0148).

Pure functions only: no I/O and no network. Every failure raises ``GristCsvError`` before anything
is written, and no message ever carries a cell's value, because ``routes_mcp`` logs the text of
every failed call.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .grist_client import GristColumn

MAX_ROWS = 20_000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 10_000
MAX_KEY_CHARS = 200
MAX_ERRORS_SHOWN = 10
MAX_ABS_INT = 2**53
CSV_TYPES = frozenset({"Text", "Numeric", "Int", "Bool", "Date", "Choice", "Any"})
KEY_TYPES = frozenset({"Text", "Numeric", "Int", "Choice"})

_DELIMITERS = (",", ";", "\t")
_INT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0"})
# int() refuses very long digit strings; nothing that long is within MAX_ABS_INT anyway.
_MAX_INT_TEXT = 400


class GristCsvError(ValueError):
    """Shown to the model verbatim. Never carries a cell value."""


@dataclass(frozen=True)
class CsvRow:
    line: int  # physical file line where the record starts; the header is line 1
    values: dict[str, Any]  # column id -> coerced value; empty cells are absent


@dataclass(frozen=True)
class ParsedCsv:
    column_ids: list[str]  # Grist column ids, in header order
    column_types: dict[str, str]  # column id -> base type (text before the first ":")
    text: str  # the decoded file
    rows: list[CsvRow]  # blank lines dropped


def base_type(grist_type: str) -> str:
    return grist_type.split(":", 1)[0]


def _split_header(text: str) -> str:
    """The delimiter that splits the first line into the most fields; comma wins a tie."""
    first_line = text.splitlines()[0] if text.strip() else ""
    if not first_line.strip():
        raise GristCsvError("The CSV file is empty.")
    best, best_count = ",", -1
    for delimiter in _DELIMITERS:
        try:
            count = len(next(csv.reader([first_line], delimiter=delimiter)))
        except (csv.Error, StopIteration):
            continue
        if count > best_count:
            best, best_count = delimiter, count
    return best


def _resolve_header(names: list[str], columns: list[GristColumn], table_id: str) -> list[GristColumn]:
    names = [name.strip() for name in names]
    for k, name in enumerate(names, start=1):
        if not name:
            raise GristCsvError(f"Header column {k} is empty.")
    by_id = {c.id: c for c in columns}
    resolved: list[GristColumn] = []
    unknown: list[str] = []
    for name in names:
        if name in by_id:
            resolved.append(by_id[name])
            continue
        matches = [c for c in columns if c.label == name]
        if len(matches) > 1:
            raise GristCsvError(
                f"Header {name} matches more than one column label; use the column id."
            )
        if matches:
            resolved.append(matches[0])
        else:
            unknown.append(name)
    if unknown:
        raise GristCsvError(
            f"Unknown column(s) in table {table_id}: {', '.join(sorted(unknown))}. "
            "The header row must use column ids or labels; call grist_list_tables to see them."
        )
    seen: set[str] = set()
    for column in resolved:
        if column.id in seen:
            raise GristCsvError(f"Column {column.id} appears more than once in the header row.")
        seen.add(column.id)
    if len(resolved) > MAX_COLUMNS:
        raise GristCsvError(f"The CSV file has more than {MAX_COLUMNS} columns.")
    return resolved


def _check_columns(resolved: list[GristColumn], table_id: str) -> None:
    formulas = sorted(c.id for c in resolved if c.is_formula)
    if formulas:
        raise GristCsvError(
            f"Column(s) {', '.join(formulas)} in table {table_id} are formula columns "
            "and cannot be written."
        )
    for column in resolved:
        if base_type(column.type) not in CSV_TYPES:
            raise GristCsvError(
                f"Column {column.id} has type {column.type}, which CSV import does not support. "
                f"Supported: {', '.join(sorted(CSV_TYPES))}."
            )


def _as_int(text: str) -> int | None:
    if not _INT_RE.fullmatch(text) or len(text) > _MAX_INT_TEXT:
        return None
    value = int(text)
    return value if abs(value) <= MAX_ABS_INT else None


def _coerce(text: str, kind: str) -> tuple[Any, str]:
    """``(value, "")`` for a good cell, or ``(None, reason)`` for one that does not fit its column."""
    if kind in ("Text", "Choice", "Any"):
        return text, ""
    if kind == "Int":
        value = _as_int(text)
        return (value, "") if value is not None else (None, "not a whole number.")
    if kind == "Numeric":
        value = _as_int(text)
        if value is not None:
            return value, ""
        if _FLOAT_RE.fullmatch(text):
            number = float(text)
            if math.isfinite(number):
                return number, ""
        return None, "not a number."
    if kind == "Bool":
        lowered = text.lower()
        if lowered in _TRUE:
            return True, ""
        if lowered in _FALSE:
            return False, ""
        return None, "not true or false."
    # Date
    if _DATE_RE.fullmatch(text):
        try:
            return calendar.timegm(date.fromisoformat(text).timetuple()), ""
        except ValueError:
            pass
    return None, "not a date in YYYY-MM-DD form."


def parse_csv(data: bytes, columns: list[GristColumn], table_id: str) -> ParsedCsv:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GristCsvError("The CSV file is not UTF-8 text.") from exc
    delimiter = _split_header(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    try:
        header = next(reader)
    except csv.Error as exc:
        raise GristCsvError("Line 1: the CSV file is malformed.") from exc
    resolved = _resolve_header(header, columns, table_id)
    _check_columns(resolved, table_id)
    ids = [c.id for c in resolved]
    types = {c.id: base_type(c.type) for c in resolved}

    errors: list[str] = []
    rows: list[CsvRow] = []
    count = 0
    while True:
        line = reader.line_num + 1
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise GristCsvError(f"Line {line}: the CSV file is malformed.") from exc
        cells = [cell.strip() for cell in record]
        if not any(cells):
            continue
        count += 1
        if count > MAX_ROWS:
            raise GristCsvError(f"The CSV file has more than {MAX_ROWS} rows; split it.")
        if len(cells) != len(ids):
            errors.append(f"Line {line}: expected {len(ids)} cells, found {len(cells)}.")
            continue
        values: dict[str, Any] = {}
        for column_id, cell in zip(ids, cells, strict=True):
            if not cell:
                continue
            if len(cell) > MAX_CELL_CHARS:
                errors.append(
                    f"Line {line}, column {column_id}: the value is longer than "
                    f"{MAX_CELL_CHARS} characters."
                )
                continue
            value, problem = _coerce(cell, types[column_id])
            if problem:
                errors.append(f"Line {line}, column {column_id}: {problem}")
                continue
            values[column_id] = value
        rows.append(CsvRow(line=line, values=values))
    if not count:
        raise GristCsvError("The CSV file has no data rows.")
    if errors:
        message = "\n".join(errors[:MAX_ERRORS_SHOWN])
        if len(errors) > MAX_ERRORS_SHOWN:
            message += f"\n… and {len(errors) - MAX_ERRORS_SHOWN} more."
        raise GristCsvError(message)
    return ParsedCsv(column_ids=ids, column_types=types, text=text, rows=rows)


def same_key(value: Any) -> Any:
    """The value used to compare keys: ``3`` and ``3.0`` are the same key, strings compare exactly."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (list, dict)):
        # Grist returns an error or list cell as a list; keep it hashable and apart from text.
        return ("\0json", json.dumps(value, sort_keys=True, default=str))
    return value


def key_index(rows: list[CsvRow], key_column: str) -> dict[Any, CsvRow]:
    for row in rows:
        if key_column not in row.values:
            raise GristCsvError(f"Line {row.line}: the key column {key_column} is empty.")
    for row in rows:
        if len(str(row.values[key_column])) > MAX_KEY_CHARS:
            raise GristCsvError(
                f"Line {row.line}: the key in column {key_column} is longer than "
                f"{MAX_KEY_CHARS} characters."
            )
    index: dict[Any, CsvRow] = {}
    for row in rows:
        key = same_key(row.values[key_column])
        if key in index:
            raise GristCsvError(
                f"Lines {index[key].line}, {row.line} have the same key in column {key_column}."
            )
        index[key] = row
    return index
