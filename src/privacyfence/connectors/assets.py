"""Jira Assets connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..assets_client import AssetsClient, AssetsClientError, AssetsObject
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..org_mode import DownloadDeliveryConfig

logger = logging.getLogger(__name__)

_REASON = "One sentence: why are you calling this tool right now?"

_SENDER = "Jira Assets"
_MAX_CARD_ATTRIBUTES = 8
_MAX_CELL_CHARS = 120


def _cell(text: str) -> str:
    return text if len(text) <= _MAX_CELL_CHARS else text[:_MAX_CELL_CHARS - 1] + "…"


def _joined(values: list[str]) -> str:
    return "; ".join(values)


def _scan_text(objects: list[AssetsObject]) -> str:
    return "\n".join(v for o in objects for values in o.attributes.values() for v in values)


def _details_line(obj: AssetsObject) -> str:
    return f"{obj.key} — {obj.label}: " + "; ".join(
        f"{name}: {_joined(values)}" for name, values in obj.attributes.items()
    )


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
                    "Use assets_list_object_types to find the type id, and assets_search_objects to read "
                    "objects of that type. Auto-approved."
                ),
                params=[
                    ToolParam("object_type_id", "str",
                              description="Numeric id of the object type, as assets_list_object_types "
                                          "returns it in id."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_search_objects",
                description=(
                    "Search Jira Assets objects with an AQL query and return them with their "
                    "attribute values. Returns a list of {id, key, label, type, type_id, schema_id, "
                    "created, updated, attributes, references}, at most max_results entries. "
                    "Use assets_get_object to read one object by id, and assets_list_attributes "
                    "to see which attributes a type has. Requires user approval."
                ),
                params=[
                    ToolParam("aql", "str",
                              description="AQL query, for example: objectType = \"Contract\" AND "
                                          "Name LIKE \"ACME\". At most 4000 characters."),
                    ToolParam("max_results", "int", required=False, default=25,
                              description="Maximum number of objects to return, 1 to 100. Default 25."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_get_object",
                description=(
                    "Read one Jira Assets object with every attribute value and its outbound "
                    "references. Returns {id, key, label, type, type_id, schema_id, created, "
                    "updated, attributes, references}. Use assets_search_objects to find the "
                    "object id. Requires user approval."
                ),
                params=[
                    ToolParam("object_id", "str",
                              description="Numeric id of the object, as assets_search_objects "
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
        if tool == "assets_search_objects":
            return await self._search_objects(**args)
        if tool == "assets_get_object":
            return await self._get_object(**args)
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
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _search_objects(self, aql: str, max_results: int = 25) -> Any:
        limit = max(1, min(int(max_results), 100))
        objects = await self._fetch(self._assets.search_objects, aql, limit)
        names: list[str] = []
        for o in objects:
            for name in o.attributes:
                if name not in names and len(names) < _MAX_CARD_ATTRIBUTES:
                    names.append(name)
        rows = [
            [_cell(o.key), _cell(o.label), _cell(o.type_name),
             *(_cell(_joined(o.attributes.get(n, []))) for n in names)]
            for o in objects
        ]
        return await gated_call(
            connector=self.name,
            tool="assets_search_objects",
            tool_name="Search Jira Assets",
            summary=f"AQL: {aql[:120]}",
            sender=_SENDER,
            raw_data=objects,
            filtered_data=[o.to_dict() for o in objects],
            gate="review",
            preview={"Query": aql[:200], "Objects": str(len(objects))},
            details_text="\n".join(_details_line(o) for o in objects) or "(no matches)",
            pii_scan_text=_scan_text(objects),
            preview_tables=[{"headers": ["Key", "Label", "Type", *names], "rows": rows}] if objects else [],
            table_only=True,
            my_email=self.my_email,
            args={"aql": aql, "max_results": limit},
        )

    async def _get_object(self, object_id: str) -> Any:
        obj = await self._fetch(self._assets.get_object, object_id)
        return await gated_call(
            connector=self.name,
            tool="assets_get_object",
            tool_name="Read Jira Assets Object",
            summary=f"{obj.key}: {obj.label}",
            sender=_SENDER,
            raw_data=obj,
            filtered_data=obj.to_dict(),
            gate="review",
            preview={"Key": obj.key, "Label": obj.label, "Type": obj.type_name,
                     "Schema": obj.schema_id},
            details_text=_details_line(obj),
            pii_scan_text=_scan_text([obj]),
            preview_tables=[{
                "headers": ["Attribute", "Value"],
                "rows": [[name, _cell(_joined(values))] for name, values in obj.attributes.items()],
            }] if obj.attributes else [],
            my_email=self.my_email,
            args={"object_id": object_id},
        )

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
