"""Grist connector: lists the documents a Grist account can open and each document's tables
and columns, reads records behind a review card, and adds or changes records behind an approval
popup. The client is built by the daemon from a personal API key or an OAuth sign-in (see
``grist_auth``)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..grist_client import (
    GristAccessDenied,
    GristClient,
    GristClientError,
    GristColumn,
    GristDocument,
    validate_doc_id,
    validate_identifier,
)

logger = logging.getLogger(__name__)

_REASON = ToolParam(
    "reason", "str", required=True,
    description="One sentence: why are you calling this tool right now?",
)

_DOC_ID_DESCRIPTION = (
    "The document's id from grist_list_documents, or the Document ID under the document's "
    "Settings (gear icon) in Grist. Not the shorter id in the document's address."
)

_TABLE_ID_DESCRIPTION = (
    "The table id from grist_list_tables, such as Contacts (not the label shown in Grist)."
)

_LIST_DENIED = (
    "This Grist server does not let PrivacyFence list your documents. Open the document in "
    "Grist, then Settings (the gear icon) → Document ID, and use that id."
)

_FILTER_ERROR = (
    'filter must be a JSON object mapping column ids to lists of values, such as {"Status": ["Open"]}.'
)
_SORT_ERROR = "sort must be column ids separated by commas, each optionally prefixed with -."
_LIMIT_ERROR = "limit must be between 1 and 500."
_ADD_RECORDS_ERROR = (
    "records must be a JSON array of 1 to 100 objects mapping column ids to text, numbers, "
    "true/false or null."
)
_UPDATE_RECORDS_ERROR = (
    'records must be a JSON array of 1 to 100 {"id": <record id>, "fields": {...}} objects '
    "with distinct ids."
)
_COLUMNS_ERROR = (
    'columns must be a JSON array of 1 to 50 {"id": ..., "label": ..., "type": ...} objects '
    "with distinct ids."
)
_SIMPLE_TYPES = frozenset(
    {"Text", "Numeric", "Int", "Bool", "Date", "Choice", "ChoiceList", "Any"}
)
_COLUMNS_DESCRIPTION = (
    'A JSON array of {"id": <column id>, "label": <optional label>, "type": <optional type>} '
    "objects. Types: Text (default), Numeric, Int, Bool, Date, Choice, ChoiceList, Any, "
    "Ref:<TableId>, RefList:<TableId>. 1 to 50 columns."
)
_NO_TEAM = "(not shown by Grist)"
_CELL_LIMIT = 200
_SCALARS = (str, int, float, bool, type(None))


def _is_column_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_identifier(value, "column")
    except GristClientError:
        return False
    return True


def _parse_filter(raw: str) -> dict[str, list[Any]]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(_FILTER_ERROR) from exc
    if not isinstance(parsed, dict) or len(parsed) > 20:
        raise ValueError(_FILTER_ERROR)
    for key, values in parsed.items():
        if (
            not _is_column_id(key)
            or not isinstance(values, list)
            or not values
            or not all(isinstance(v, _SCALARS) for v in values)
        ):
            raise ValueError(_FILTER_ERROR)
    return parsed


def _validate_sort(raw: str) -> str:
    sort = raw.strip()
    if not sort:
        return ""
    for item in sort.split(","):
        if not _is_column_id(item.strip().removeprefix("-")):
            raise ValueError(_SORT_ERROR)
    return ",".join(item.strip() for item in sort.split(","))


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError(_LIMIT_ERROR)
    return limit


def _is_fields(value: Any) -> bool:
    return isinstance(value, dict) and all(
        _is_column_id(k) and isinstance(v, _SCALARS) for k, v in value.items()
    )


def _parse_add_records(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(_ADD_RECORDS_ERROR) from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 100 or not all(
        _is_fields(row) for row in parsed
    ):
        raise ValueError(_ADD_RECORDS_ERROR)
    return parsed


def _parse_update_records(raw: str) -> list[tuple[int, dict[str, Any]]]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(_UPDATE_RECORDS_ERROR) from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 100:
        raise ValueError(_UPDATE_RECORDS_ERROR)
    rows: list[tuple[int, dict[str, Any]]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(_UPDATE_RECORDS_ERROR)
        rec_id, fields = item.get("id"), item.get("fields")
        if (
            isinstance(rec_id, bool) or not isinstance(rec_id, int) or rec_id <= 0
            or not fields or not _is_fields(fields)
        ):
            raise ValueError(_UPDATE_RECORDS_ERROR)
        rows.append((rec_id, fields))
    if len({rec_id for rec_id, _ in rows}) != len(rows):
        raise ValueError(_UPDATE_RECORDS_ERROR)
    return rows


def _validate_column_type(value: Any) -> str:
    if isinstance(value, str):
        if value in _SIMPLE_TYPES:
            return value
        kind, sep, target = value.partition(":")
        if sep and kind in ("Ref", "RefList") and _is_table_id(target):
            return value
    raise ValueError(
        f"Unsupported column type {value!r}. Use one of Text, Numeric, Int, Bool, Date, "
        "Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>."
    )


def _is_table_id(value: str) -> bool:
    try:
        validate_identifier(value, "table")
    except GristClientError:
        return False
    return True


def _parse_columns(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(_COLUMNS_ERROR) from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 50:
        raise ValueError(_COLUMNS_ERROR)
    columns: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict) or not _is_column_id(item.get("id")):
            raise ValueError(_COLUMNS_ERROR)
        label = item.get("label", item["id"])
        if not isinstance(label, str) or len(label) > 100:
            raise ValueError(_COLUMNS_ERROR)
        col_type = _validate_column_type(item.get("type", "Text"))
        columns.append({"id": item["id"], "label": label or item["id"], "type": col_type})
    if len({c["id"] for c in columns}) != len(columns):
        raise ValueError(_COLUMNS_ERROR)
    return columns


def _column_preview(columns: list[dict[str, str]]) -> tuple[list[dict[str, Any]], str]:
    table = [{
        "headers": ["Column", "Label", "Type"],
        "rows": [[c["id"], c["label"], c["type"]] for c in columns],
    }]
    details = "\n".join(f"{c['id']} ({c['type']}) {c['label']}" for c in columns)
    return table, details


def _check_writable_columns(table_id: str, written: set[str], columns: list[GristColumn]) -> None:
    by_id = {c.id: c for c in columns}
    unknown = written - by_id.keys()
    if unknown:
        raise ValueError(
            f"Unknown column(s) in table {table_id}: {', '.join(sorted(unknown))}. "
            "Call grist_list_tables to see the column ids."
        )
    formulas = {c for c in written if by_id[c].is_formula}
    if formulas:
        raise ValueError(
            f"Column(s) {', '.join(sorted(formulas))} in table {table_id} are formula columns "
            "and cannot be written."
        )


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > _CELL_LIMIT:
        return text[:_CELL_LIMIT] + "…"
    return text


class GristConnector(Connector):
    def __init__(self, client: GristClient) -> None:
        self._client = client

    @property
    def client(self) -> GristClient:
        return self._client

    @property
    def name(self) -> str:
        return "grist"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="grist_list_documents",
                description=(
                    "List the Grist documents this account can open, with their workspace and "
                    "team. Returns {documents: [{id, name, workspace, team}]}. Some Grist "
                    "servers do not allow apps to list documents; then ask the user for the "
                    "Document ID shown under the document's Settings. Pass an id as doc_id to "
                    "grist_list_tables. Auto-approved."
                ),
                params=[_REASON],
                read_only=True,
            ),
            ToolSpec(
                name="grist_list_tables",
                description=(
                    "List the tables of one Grist document and each table's columns (id, label "
                    "and type). Returns {doc_id, tables: [{id, columns: [{id, label, type, "
                    "is_formula}]}]}. Use grist_list_documents first to find the doc_id, and "
                    "grist_get_records to read a table's records. Auto-approved."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="grist_get_records",
                description=(
                    "Read records from one table of a Grist document, optionally filtered and "
                    "sorted. Returns {doc_id, table_id, records: [{id, fields}], truncated}. "
                    "Use grist_list_tables first to find the table and column ids. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    ToolParam(
                        "table_id", "str",
                        description=_TABLE_ID_DESCRIPTION,
                    ),
                    ToolParam(
                        "filter", "str", required=False, default="",
                        description='A JSON object mapping column ids to lists of allowed values, '
                                    'such as {"Status": ["Open", "Waiting"]}. Rows match when every '
                                    "listed column has one of its values. Empty means no filter.",
                    ),
                    ToolParam(
                        "sort", "str", required=False, default="",
                        description="Column ids separated by commas, each optionally prefixed "
                                    "with - for descending, such as -Date,Name. Empty means "
                                    "Grist's own order.",
                    ),
                    ToolParam(
                        "limit", "int", required=False, default=100,
                        description="Most records to return, 1 to 500. Default 100; the result "
                                    "says truncated when there were more.",
                    ),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="grist_add_records",
                description=(
                    "Add new records to one table of a Grist document. Returns {doc_id, "
                    "table_id, added_ids}. Use grist_update_records to change existing records "
                    "and grist_list_tables to find the column ids. Requires user approval."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    ToolParam("table_id", "str", description=_TABLE_ID_DESCRIPTION),
                    ToolParam(
                        "records", "str",
                        description="A JSON array of objects, each mapping column ids to the new "
                                    'record\'s values, such as [{"Name": "Ada", "Score": 3}]. 1 to '
                                    "100 records; values are text, numbers, true/false or null.",
                    ),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="grist_update_records",
                description=(
                    "Change cells of existing records in one table of a Grist document. Returns "
                    "{doc_id, table_id, updated_ids}. Use grist_add_records to add new records "
                    "and grist_get_records to find record ids. Requires user approval."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    ToolParam("table_id", "str", description=_TABLE_ID_DESCRIPTION),
                    ToolParam(
                        "records", "str",
                        description='A JSON array of {"id": <record id>, "fields": {<column id>: '
                                    '<new value>}} objects, such as [{"id": 5, "fields": '
                                    '{"Status": "Done"}}]. 1 to 100 records; only the listed '
                                    "columns change.",
                    ),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="grist_create_table",
                description=(
                    "Create a new table, with its columns, in a Grist document. Returns "
                    "{doc_id, table_id, column_ids}; table_id is the id Grist assigned. Use "
                    "grist_add_columns to add columns to a table that already exists and "
                    "grist_list_tables to see the existing tables. Requires user approval."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    ToolParam(
                        "table_id", "str",
                        description="The id for the new table, such as Contacts.",
                    ),
                    ToolParam("columns", "str", description=_COLUMNS_DESCRIPTION),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="grist_add_columns",
                description=(
                    "Add new columns to an existing table of a Grist document. Returns "
                    "{doc_id, table_id, column_ids}. Use grist_create_table to create a new "
                    "table and grist_list_tables to see the existing columns. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
                    ToolParam("table_id", "str", description=_TABLE_ID_DESCRIPTION),
                    ToolParam("columns", "str", description=_COLUMNS_DESCRIPTION),
                    _REASON,
                ],
                read_only=False,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "grist_list_documents":
            return await self._list_documents()
        if tool == "grist_list_tables":
            return await self._list_tables(**args)
        if tool == "grist_get_records":
            return await self._get_records(**args)
        if tool == "grist_add_records":
            return await self._add_records(**args)
        if tool == "grist_update_records":
            return await self._update_records(**args)
        if tool == "grist_create_table":
            return await self._create_table(**args)
        if tool == "grist_add_columns":
            return await self._add_columns(**args)
        raise ValueError(f"Unknown Grist tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_documents(self) -> Any:
        t0 = time.time()
        try:
            documents = await self._fetch(self._client.list_documents)
        except RuntimeError as exc:
            if isinstance(exc.__cause__, GristAccessDenied):
                raise RuntimeError(_LIST_DENIED) from exc.__cause__
            raise
        self._auto_audit("grist_list_documents", "List Grist Documents", "List documents",
                         f"{len(documents)} document(s)", t0)
        return {"documents": [asdict(d) for d in documents]}

    async def _list_tables(self, doc_id: str) -> Any:
        t0 = time.time()
        try:
            validate_doc_id(doc_id)
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        tables = await self._fetch(self._client.list_tables, doc_id)
        self._auto_audit("grist_list_tables", "List Grist Tables", f"List tables of {doc_id}",
                         f"{len(tables)} table(s)", t0)
        return {"doc_id": doc_id, "tables": [asdict(t) for t in tables]}

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_records(
        self, doc_id: str, table_id: str, filter: str = "", sort: str = "", limit: int = 100,
    ) -> Any:
        try:
            validate_doc_id(doc_id)
            validate_identifier(table_id, "table")
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        filters = _parse_filter(filter)
        sort = _validate_sort(sort)
        limit = _validate_limit(limit)

        doc = await self._doc_info(doc_id)
        page = await self._fetch(
            self._client.get_records, doc_id, table_id, filters=filters, sort=sort, limit=limit,
        )
        records = page.records
        columns: list[str] = []
        for record in records:
            for column in record.fields:
                if column not in columns:
                    columns.append(column)
        if records:
            details = "\n\n".join(
                f"#{r.id}\n" + "\n".join(f"{c}: {_cell_text(r.fields.get(c))}" for c in r.fields)
                for r in records
            )
        else:
            details = "(no records)"
        if page.truncated:
            details = f"Showing the first {limit} records; more match.\n\n{details}"
        tables = [{
            "headers": ["id", *columns],
            "rows": [[str(r.id), *[_cell_text(r.fields.get(c)) for c in columns]] for r in records],
        }] if records else None
        return await gated_call(
            connector=self.name,
            tool="grist_get_records",
            tool_name="Read Grist Records",
            summary=f"Read {len(records)} record(s) from {doc.name} / {table_id}",
            sender=doc.name,
            raw_data=page,
            filtered_data={
                "doc_id": doc_id,
                "table_id": table_id,
                "records": [asdict(r) for r in records],
                "truncated": page.truncated,
            },
            gate="review",
            preview={
                "Server": self._client.host,
                "Document": doc.name,
                "Team": doc.team or _NO_TEAM,
                "Table": table_id,
                "Filter": filter or "(none)",
                "Sort": sort or "(none)",
            },
            new_info={"Records": str(len(records)), "Record content": "values of every visible column"},
            details_text=details,
            pii_scan_text=details,
            preview_tables=tables,
            table_only=True,
            my_email="",
            args={
                "doc_id": doc_id, "table_id": table_id,
                "filter": filter, "sort": sort, "limit": limit,
            },
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _add_records(self, doc_id: str, table_id: str, records: str) -> Any:
        try:
            validate_doc_id(doc_id)
            validate_identifier(table_id, "table")
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        rows = _parse_add_records(records)

        doc = await self._doc_info(doc_id)
        columns = await self._fetch(self._client.list_columns, doc_id, table_id)
        headers: list[str] = []
        for row in rows:
            for column in row:
                if column not in headers:
                    headers.append(column)
        _check_writable_columns(table_id, set(headers), columns)

        await gated_call(
            connector=self.name,
            tool="grist_add_records",
            tool_name="Add Grist Records",
            summary=f"Add {len(rows)} record(s) to {doc.name} / {table_id}",
            sender=doc.name,
            raw_data={"doc_id": doc_id, "table_id": table_id, "records": rows},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Document": doc.name,
                "Table": table_id,
                "Records": str(len(rows)),
            },
            preview_tables=[{
                "headers": headers,
                "rows": [[_cell_text(row.get(c)) for c in headers] for row in rows],
            }],
            details_text=json.dumps(rows, ensure_ascii=False, indent=2),
            my_email="",
            args={"doc_id": doc_id, "table_id": table_id},
        )
        added = await self._fetch(self._client.add_records, doc_id, table_id, rows)
        return {"doc_id": doc_id, "table_id": table_id, "added_ids": added}

    async def _update_records(self, doc_id: str, table_id: str, records: str) -> Any:
        try:
            validate_doc_id(doc_id)
            validate_identifier(table_id, "table")
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        rows = _parse_update_records(records)

        doc = await self._doc_info(doc_id)
        columns = await self._fetch(self._client.list_columns, doc_id, table_id)
        _check_writable_columns(table_id, {c for _, fields in rows for c in fields}, columns)
        existing = await self._fetch(
            self._client.get_records_by_id, doc_id, table_id, [rec_id for rec_id, _ in rows],
        )
        current = {r.id: r.fields for r in existing}
        missing = {rec_id for rec_id, _ in rows} - current.keys()
        if missing:
            raise ValueError(
                f"Record id(s) not found in table {table_id}: "
                f"{', '.join(map(str, sorted(missing)))}."
            )

        changes = [
            (rec_id, column, current[rec_id].get(column), value)
            for rec_id, fields in rows
            for column, value in fields.items()
        ]
        await gated_call(
            connector=self.name,
            tool="grist_update_records",
            tool_name="Update Grist Records",
            summary=f"Update {len(rows)} record(s) in {doc.name} / {table_id}",
            sender=doc.name,
            raw_data={
                "doc_id": doc_id,
                "table_id": table_id,
                "records": [{"id": rec_id, "fields": fields} for rec_id, fields in rows],
            },
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Document": doc.name,
                "Table": table_id,
                "Records": str(len(rows)),
                "Cells changed": str(len(changes)),
            },
            preview_tables=[{
                "headers": ["Record", "Column", "Current", "New"],
                "rows": [
                    [f"#{rec_id}", column, _cell_text(old), _cell_text(new)]
                    for rec_id, column, old, new in changes
                ],
            }],
            details_text="\n".join(
                f"#{rec_id} {column}: {_cell_text(old)} → {_cell_text(new)}"
                for rec_id, column, old, new in changes
            ),
            my_email="",
            args={"doc_id": doc_id, "table_id": table_id},
        )
        await self._fetch(self._client.update_records, doc_id, table_id, rows)
        return {"doc_id": doc_id, "table_id": table_id, "updated_ids": [rec_id for rec_id, _ in rows]}

    async def _create_table(self, doc_id: str, table_id: str, columns: str) -> Any:
        try:
            validate_doc_id(doc_id)
            validate_identifier(table_id, "table")
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        cols = _parse_columns(columns)

        doc = await self._doc_info(doc_id)
        tables = await self._fetch(self._client.list_tables, doc_id)
        if any(t.id == table_id for t in tables):
            raise ValueError(
                f"Table {table_id} already exists in this document. "
                "Use grist_add_columns to add columns to it."
            )
        table, details = _column_preview(cols)
        await gated_call(
            connector=self.name,
            tool="grist_create_table",
            tool_name="Create Grist Table",
            summary=f"Create table {table_id} in {doc.name}",
            sender=doc.name,
            raw_data={"doc_id": doc_id, "table_id": table_id, "columns": cols},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Document": doc.name,
                "New table": table_id,
                "Columns": str(len(cols)),
            },
            preview_tables=table,
            details_text=details,
            my_email="",
            args={"doc_id": doc_id, "table_id": table_id},
        )
        created = await self._fetch(self._client.add_table, doc_id, table_id, cols)
        return {"doc_id": doc_id, "table_id": created, "column_ids": [c["id"] for c in cols]}

    async def _add_columns(self, doc_id: str, table_id: str, columns: str) -> Any:
        try:
            validate_doc_id(doc_id)
            validate_identifier(table_id, "table")
        except GristClientError as exc:
            raise ValueError(str(exc)) from exc
        cols = _parse_columns(columns)

        doc = await self._doc_info(doc_id)
        tables = await self._fetch(self._client.list_tables, doc_id)
        existing = next((t for t in tables if t.id == table_id), None)
        if existing is None:
            raise ValueError(
                f"Table {table_id} does not exist in this document. "
                "Use grist_create_table to create it."
            )
        dupes = {c["id"] for c in cols} & {c.id for c in existing.columns}
        if dupes:
            raise ValueError(
                f"Column(s) {', '.join(sorted(dupes))} already exist in table {table_id}."
            )
        table, details = _column_preview(cols)
        await gated_call(
            connector=self.name,
            tool="grist_add_columns",
            tool_name="Add Grist Columns",
            summary=f"Add {len(cols)} column(s) to {doc.name} / {table_id}",
            sender=doc.name,
            raw_data={"doc_id": doc_id, "table_id": table_id, "columns": cols},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Document": doc.name,
                "Table": table_id,
                "New columns": str(len(cols)),
            },
            preview_tables=table,
            details_text=details,
            my_email="",
            args={"doc_id": doc_id, "table_id": table_id},
        )
        created = await self._fetch(self._client.add_columns, doc_id, table_id, cols)
        return {"doc_id": doc_id, "table_id": table_id, "column_ids": created}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _doc_info(self, doc_id: str) -> GristDocument:
        try:
            return await self._fetch(self._client.get_document, doc_id)
        except RuntimeError as exc:
            if isinstance(exc.__cause__, GristAccessDenied):
                return GristDocument(id=doc_id, name=doc_id, workspace="", team="")
            raise

    async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except GristClientError as exc:
            logger.warning("Grist request failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
