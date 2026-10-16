"""Pure checks the Salesforce write tools run before their approval card: parsing the JSON the
caller passed, checking field names and values against the object's describe, finding the key of
a batch update, and comparing and rendering values for the card.

Nothing here does I/O. Every error is a ``ValueError`` whose message names the tool parameter,
the record position and the field API name; no error message carries a value read from
Salesforce."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from .salesforce_client import SalesforceField

MAX_BATCH_RECORDS = 200
MAX_PAYLOAD_CHARS = 1_000_000
MAX_KEY_CHARS = 255
MAX_ERRORS_SHOWN = 10
MAX_CELL_PREVIEW_CHARS = 200

# describe type -> kind of JSON value accepted
WRITABLE_TYPES: dict[str, str] = {
    "string": "text", "textarea": "text", "picklist": "text", "multipicklist": "text",
    "combobox": "text", "email": "text", "phone": "text", "url": "text", "reference": "text",
    "time": "text",
    "boolean": "bool", "int": "int", "double": "number", "currency": "number",
    "percent": "number", "date": "date", "datetime": "datetime",
}
KEY_TYPES = frozenset({"id", "string", "email"})

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?(Z|[+-]\d{2}:?\d{2})"
)
_ID_RE = re.compile(r"[A-Za-z0-9]{15}([A-Za-z0-9]{3})?")
_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")

_EXPECTED = {
    "text": "text",
    "bool": "true or false",
    "int": "a whole number",
    "number": "a number",
    "date": "a date as YYYY-MM-DD",
    "datetime": "a date-time in ISO 8601",
}


class _JsonObject(dict[str, Any]):
    """A parsed JSON object that remembers the names it repeated."""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__()
        self.repeated: list[str] = []
        for name, value in pairs:
            if name in self:
                self.repeated.append(name)
            self[name] = value


def _load(raw: str, param: str) -> Any:
    if len(raw) > MAX_PAYLOAD_CHARS:
        raise ValueError(f"{param}: larger than {MAX_PAYLOAD_CHARS:,} characters")
    try:
        return json.loads(raw, object_pairs_hook=_JsonObject)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{param}: invalid JSON: {exc.msg}") from None


def _check_object(obj: Any, param: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"{param}: must be a JSON object of field names to values")
    if not obj:
        raise ValueError(f"{param}: names no field")
    if isinstance(obj, _JsonObject) and obj.repeated:
        raise ValueError(f"{param}: {obj.repeated[0]} is named twice")
    for name, value in obj.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{param}: {name} must be text, a number, true/false or null")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{param}: {name} must be a finite number")
    return dict(obj)


def parse_fields_object(raw: str, param: str) -> dict[str, Any]:
    return _check_object(_load(raw, param), param)


def parse_records(raw: str) -> list[dict[str, Any]]:
    data = _load(raw, "records")
    if not isinstance(data, list):
        raise ValueError("records: must be a JSON array of objects")
    if not 1 <= len(data) <= MAX_BATCH_RECORDS:
        raise ValueError(f"records: must hold 1 to {MAX_BATCH_RECORDS} records, got {len(data)}")
    return [_check_object(item, f"records[{i}]") for i, item in enumerate(data)]


def field_index(fields: list[SalesforceField]) -> dict[str, SalesforceField]:
    return {f.name.lower(): f for f in fields}


def _matches_kind(value: Any, kind: str) -> bool:
    if kind == "text":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "date":
        return isinstance(value, str) and _DATE_RE.fullmatch(value) is not None
    return isinstance(value, str) and _DATETIME_RE.fullmatch(value) is not None


def resolve_fields(
    values: dict[str, Any],
    index: dict[str, SalesforceField],
    object_type: str,
    mode: str,
    where: str,
) -> dict[str, Any]:
    creating = mode == "create"
    resolved: dict[str, Any] = {}
    for name, value in values.items():
        field = index.get(name.lower())
        if field is None:
            raise ValueError(f"{where}: {object_type} has no field {name!r}")
        if field.name in resolved:
            raise ValueError(f"{where}: field {field.name} is named twice")
        if not (field.createable if creating else field.updateable):
            raise ValueError(
                f"{where}: {object_type}.{field.name} cannot be set when "
                f"{'creating' if creating else 'updating'} a record"
            )
        kind = WRITABLE_TYPES.get(field.type)
        if kind is None:
            raise ValueError(
                f"{where}: {object_type}.{field.name} has type {field.type}, "
                "which PrivacyFence does not write"
            )
        if value is None:
            if creating:
                raise ValueError(f"{where}: {field.name} is null; leave it out instead")
            if not field.nillable:
                raise ValueError(f"{where}: {field.name} cannot be cleared")
        elif not _matches_kind(value, kind):
            raise ValueError(f"{where}: {field.name} must be {_EXPECTED[kind]}")
        resolved[field.name] = value
    return resolved


def resolve_key_field(
    key_field: str, index: dict[str, SalesforceField], object_type: str
) -> SalesforceField:
    field = index.get(key_field.lower())
    if field is None:
        raise ValueError(f"key_field: {object_type} has no field {key_field!r}")
    if field.type != "id" and not (field.external_id and field.type in KEY_TYPES):
        raise ValueError(
            f"key_field: {object_type}.{field.name} is not an External ID field; "
            "use Id or a field marked External ID"
        )
    return field


def key_text(value: Any, where: str, key_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_KEY_CHARS
        or any(ch < " " for ch in value)
    ):
        raise ValueError(
            f"{where}: {key_name} must be non-empty text without control characters, "
            f"at most {MAX_KEY_CHARS} characters"
        )
    return value


def key_norm(value: str, is_id: bool) -> str:
    # A 15- and an 18-character id name the same record, and Salesforce compares External ID text without case.
    return value[:15] if is_id else value.casefold()


def _empty(value: Any) -> bool:
    return value is None or value == ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _instant(value: Any) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _OFFSET_RE.sub(r"\1:\2", text)
    return datetime.fromisoformat(text)


def values_differ(new: Any, current: Any, field_type: str) -> bool:
    if _empty(new) or _empty(current):
        return not (_empty(new) and _empty(current))
    if field_type == "boolean":
        return _as_bool(new) != _as_bool(current)
    if field_type in ("int", "double", "currency", "percent"):
        try:
            return float(new) != float(current)
        except (TypeError, ValueError):
            return str(new) != str(current)
    if field_type == "datetime":
        try:
            return _instant(new) != _instant(current)
        except (TypeError, ValueError):
            return str(new) != str(current)
    if field_type == "multipicklist":
        return set(str(new).split(";")) != set(str(current).split(";"))
    if (
        field_type == "reference"
        and isinstance(new, str)
        and isinstance(current, str)
        and _ID_RE.fullmatch(new)
        and _ID_RE.fullmatch(current)
    ):
        return new[:15] != current[:15]
    return str(new) != str(current)


def cell_text(value: Any, limit: int | None = MAX_CELL_PREVIEW_CHARS) -> str:
    if _empty(value):
        return "(empty)"
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text
