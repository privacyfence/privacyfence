"""Grist connector: lists the documents a Grist account can open and each document's tables
and columns, with no approval card. The client is built by the daemon from a personal API key or
an OAuth sign-in (see ``grist_auth``)."""
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

_LIST_DENIED = (
    "This Grist server does not let PrivacyFence list your documents. Open the document in "
    "Grist, then Settings (the gear icon) → Document ID, and use that id."
)

_FILTER_ERROR = (
    'filter must be a JSON object mapping column ids to lists of values, such as {"Status": ["Open"]}.'
)
_SORT_ERROR = "sort must be column ids separated by commas, each optionally prefixed with -."
_LIMIT_ERROR = "limit must be between 1 and 500."
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
                        description="The table id from grist_list_tables, such as Contacts "
                                    "(not the label shown in Grist).",
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
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "grist_list_documents":
            return await self._list_documents()
        if tool == "grist_list_tables":
            return await self._list_tables(**args)
        if tool == "grist_get_records":
            return await self._get_records(**args)
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
