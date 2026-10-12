"""Jira Assets connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..assets_client import AssetsClient, AssetsClientError
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason
from ..org_mode import DownloadDeliveryConfig

logger = logging.getLogger(__name__)

_REASON = "One sentence: why are you calling this tool right now?"


class AssetsConnector(Connector):
    def __init__(self, client: AssetsClient) -> None:
        self._assets = client
        self.my_email: str = ""
        self.download_mode: str = "local"
        self.download_config: DownloadDeliveryConfig | None = None
        self.download_base_url: str = ""

    @property
    def client(self) -> AssetsClient:
        return self._assets

    @property
    def name(self) -> str:
        return "assets"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="assets_list_schemas",
                description=(
                    "List the Jira Assets object schemas on the connected Atlassian site. "
                    "Returns a list of {id, name, key, description, object_count, object_type_count}, "
                    "at most max_results entries. Use assets_list_object_types to see what a schema "
                    "contains. Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50,
                              description="Maximum number of schemas to return, 1 to 100. Default 50."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_list_object_types",
                description=(
                    "List the object types in one Jira Assets schema. "
                    "Returns a list of {id, name, parent_id, description, object_count}. "
                    "Use assets_list_schemas to find the schema id, and assets_list_attributes to see "
                    "the attributes of one type. Auto-approved."
                ),
                params=[
                    ToolParam("schema_id", "str",
                              description="Numeric id of the schema, as assets_list_schemas returns it "
                                          "in id."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_list_attributes",
                description=(
                    "List the attribute definitions of one Jira Assets object type, including which "
                    "attributes reference other object types. Returns a list of {id, name, kind, "
                    "value_type, reference_type_id, reference_type_name, reference_kind, "
                    "min_cardinality, max_cardinality, is_label}; system attributes are left out. "
                    "Use assets_list_object_types to find the type id. Auto-approved."
                ),
                params=[
                    ToolParam("object_type_id", "str",
                              description="Numeric id of the object type, as assets_list_object_types "
                                          "returns it in id."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "assets_list_schemas":
            return await self._list_schemas(**args)
        if tool == "assets_list_object_types":
            return await self._list_object_types(**args)
        if tool == "assets_list_attributes":
            return await self._list_attributes(**args)
        raise ValueError(f"Unknown Assets tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_schemas(self, max_results: int = 50) -> Any:
        t0 = time.time()
        limit = max(1, min(int(max_results), 100))
        schemas = await self._fetch(self._assets.list_schemas, limit)
        data = [
            {
                "id": s.id, "name": s.name, "key": s.key, "description": s.description,
                "object_count": s.object_count, "object_type_count": s.object_type_count,
            }
            for s in schemas
        ]
        self._auto_audit("assets_list_schemas", "List Jira Assets Schemas",
                         f"List schemas (max {limit})", f"{len(data)} schema(s)", t0)
        return data

    async def _list_object_types(self, schema_id: str) -> Any:
        t0 = time.time()
        types = await self._fetch(self._assets.list_object_types, schema_id)
        data = [
            {
                "id": t.id, "name": t.name, "parent_id": t.parent_id,
                "description": t.description, "object_count": t.object_count,
            }
            for t in types
        ]
        self._auto_audit("assets_list_object_types", "List Jira Assets Object Types",
                         f"List object types: schema {schema_id}", f"{len(data)} object type(s)", t0)
        return data

    async def _list_attributes(self, object_type_id: str) -> Any:
        t0 = time.time()
        attributes = await self._fetch(self._assets.list_attributes, object_type_id)
        data = [
            {
                "id": a.id, "name": a.name, "kind": a.kind, "value_type": a.value_type,
                "reference_type_id": a.reference_type_id,
                "reference_type_name": a.reference_type_name,
                "reference_kind": a.reference_kind,
                "min_cardinality": a.min_cardinality, "max_cardinality": a.max_cardinality,
                "is_label": a.is_label,
            }
            for a in attributes
            if not a.is_system
        ]
        self._auto_audit("assets_list_attributes", "List Jira Assets Attributes",
                         f"List attributes: object type {object_type_id}",
                         f"{len(data)} attribute(s)", t0)
        return data

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except AssetsClientError as exc:
            logger.error("Assets fetch failed: %s", exc)
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
