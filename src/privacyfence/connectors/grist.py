"""Grist connector: lists the documents a Grist account can open and each document's tables
and columns, with no approval card. The client is built by the daemon from a personal API key or
an OAuth sign-in (see ``grist_auth``)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason
from ..grist_client import (
    GristAccessDenied,
    GristClient,
    GristClientError,
    validate_doc_id,
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
                    "is_formula}]}]}. Use grist_list_documents first to find the doc_id. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("doc_id", "str", description=_DOC_ID_DESCRIPTION),
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
    # Helpers
    # ------------------------------------------------------------------ #

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
