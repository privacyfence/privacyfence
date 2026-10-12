"""Jira Assets connector."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..assets_client import AssetsClient, AssetsClientError, AssetsObject
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..org_mode import DownloadDeliveryConfig
from .. import export_files, local_files

logger = logging.getLogger(__name__)

_REASON = "One sentence: why are you calling this tool right now?"

_SENDER = "Jira Assets"
_MAX_CARD_ATTRIBUTES = 8
_MAX_CELL_CHARS = 120

_DESTINATION_DIR = (
    "Folder on the user's computer to write the file to, absolute or starting with ~/."
)

_EXPORT_COLUMNS = ["id", "key", "label", "type"]


# The schema-scope matcher reads .schema_id from raw_data; an export's raw_data carries ids and
# no attribute value (ADR 0159).
@dataclass(frozen=True)
class _ExportedObject:
    id: str
    key: str
    schema_id: str


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
                    "Use assets_get_object to read one object by id, assets_export_objects to write "
                    "a large result to a file, and assets_list_attributes to see which attributes a "
                    "type has. Requires user approval."
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
                    "object id, and assets_get_related_objects to follow its links. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("object_id", "str",
                              description="Numeric id of the object, as assets_search_objects "
                                          "returns it in id."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_get_related_objects",
                description=(
                    "Read the Jira Assets objects linked to one object, following references "
                    "outbound, inbound or both, up to three hops. Returns {root_id, objects, "
                    "edges: [{from, to, attribute}], truncated}, with depth 1 to 3 and at most 200 "
                    "objects; truncated true means that limit was hit. Use assets_get_object to read "
                    "only the object itself. Requires user approval."
                ),
                params=[
                    ToolParam("object_id", "str",
                              description="Numeric id of the object to start from, as "
                                          "assets_search_objects returns it in id."),
                    ToolParam("direction", "str", required=False, default="both",
                              description="Which links to follow: outbound (references the object "
                                          "holds), inbound (objects referencing it) or both."),
                    ToolParam("depth", "int", required=False, default=1,
                              description="How many hops to follow, 1 to 3. Default 1."),
                    ToolParam("max_objects", "int", required=False, default=50,
                              description="Most objects to return, root included, 1 to 200. "
                                          "Default 50."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_get_connected_tickets",
                description=(
                    "List the Jira tickets connected to one Jira Assets object. "
                    "Returns {object: {id, key, label, type}, tickets: [{key, summary, status, "
                    "issue_type, priority}]}. Use assets_get_object to read the object itself. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("object_id", "str",
                              description="Numeric id of the object, as assets_search_objects "
                                          "returns it in id."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="assets_export_objects",
                description=(
                    "Write every Jira Assets object an AQL query matches, up to 10,000, to a CSV or "
                    "JSON file on the user's computer, in one approval. Columns: id, key, label, type, "
                    "then every attribute name; a multi-value cell joins its values with \"; \". "
                    "Returns {rows, columns, sha256, path, name, size_bytes, delivery}, never the "
                    "objects. A query matching no object writes no file and returns rows 0. "
                    "Use assets_search_objects to read a few objects in the reply. "
                    + export_files.ORG_DELIVERY_SENTENCE + " Requires user approval."
                ),
                params=[
                    ToolParam("aql", "str",
                              description="AQL query selecting the objects to export, for example: "
                                          "objectType = \"Contract\". At most 4000 characters."),
                    ToolParam("destination_dir", "str", description=_DESTINATION_DIR),
                    ToolParam("file_name", "str", required=False, default="",
                              description="File name ending in .csv or .json, which picks the format. "
                                          "Empty means assets-objects.csv."),
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
        if tool == "assets_get_related_objects":
            return await self._get_related_objects(**args)
        if tool == "assets_get_connected_tickets":
            return await self._get_connected_tickets(**args)
        if tool == "assets_export_objects":
            return await self._export_objects(**args)
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

    async def _get_related_objects(
        self, object_id: str, direction: str = "both", depth: int = 1, max_objects: int = 50,
    ) -> Any:
        depth = max(1, min(int(depth), 3))
        max_objects = max(1, min(int(max_objects), 200))
        graph = await self._fetch(
            self._assets.related_objects, object_id, direction, depth, max_objects)
        keys = {o.id: o.key for o in graph.objects}
        root_key = keys.get(graph.root_id, graph.root_id)
        linked = max(len(graph.objects) - 1, 0)
        by_type: dict[str, list[AssetsObject]] = {}
        for o in graph.objects:
            by_type.setdefault(o.type_name, []).append(o)
        tables: list[dict[str, Any]] = []
        for objs in by_type.values():
            names: list[str] = []
            for o in objs:
                for name in o.attributes:
                    if name not in names and len(names) < _MAX_CARD_ATTRIBUTES:
                        names.append(name)
            tables.append({
                "headers": ["Key", "Label", *names],
                "rows": [
                    [_cell(o.key), _cell(o.label),
                     *(_cell(_joined(o.attributes.get(n, []))) for n in names)]
                    for o in objs
                ],
            })
        if graph.edges:
            tables.append({
                "headers": ["From", "Attribute", "To"],
                "rows": [
                    [_cell(keys.get(e.source_id, e.source_id)), _cell(e.attribute),
                     _cell(keys.get(e.target_id, e.target_id))]
                    for e in graph.edges
                ],
            })
        return await gated_call(
            connector=self.name,
            tool="assets_get_related_objects",
            tool_name="Read Linked Jira Assets Objects",
            summary=f"{root_key}: {linked} linked objects",
            sender=_SENDER,
            raw_data=graph.objects,
            filtered_data={
                "root_id": graph.root_id,
                "objects": [o.to_dict() for o in graph.objects],
                "edges": [
                    {"from": e.source_id, "to": e.target_id, "attribute": e.attribute}
                    for e in graph.edges
                ],
                "truncated": graph.truncated,
            },
            gate="review",
            preview={"Root": root_key, "Direction": direction, "Depth": str(depth),
                     "Objects": str(linked)},
            details_text="\n".join(_details_line(o) for o in graph.objects),
            pii_scan_text=_scan_text(graph.objects),
            preview_tables=tables,
            table_only=True,
            my_email=self.my_email,
            args={"object_id": object_id, "direction": direction, "depth": depth,
                  "max_objects": max_objects},
        )

    async def _get_connected_tickets(self, object_id: str) -> Any:
        obj = await self._fetch(self._assets.get_object, object_id)
        tickets = await self._fetch(self._assets.connected_tickets, object_id)
        return await gated_call(
            connector=self.name,
            tool="assets_get_connected_tickets",
            tool_name="Read Jira Assets Object Tickets",
            summary=f"{obj.key}: {len(tickets)} tickets",
            sender=_SENDER,
            raw_data=[obj],
            filtered_data={
                "object": {"id": obj.id, "key": obj.key, "label": obj.label, "type": obj.type_name},
                "tickets": [
                    {"key": t.key, "summary": t.summary, "status": t.status,
                     "issue_type": t.issue_type, "priority": t.priority}
                    for t in tickets
                ],
            },
            gate="review",
            preview={"Object": obj.key, "Tickets": str(len(tickets))},
            details_text="\n".join(
                f"{t.key} — {t.summary} ({t.status}, {t.issue_type}, {t.priority})"
                for t in tickets
            ) or "(no connected tickets)",
            pii_scan_text="\n".join(t.summary for t in tickets),
            preview_tables=[{
                "headers": ["Key", "Summary", "Status", "Type", "Priority"],
                "rows": [[_cell(t.key), _cell(t.summary), _cell(t.status), _cell(t.issue_type),
                          _cell(t.priority)] for t in tickets],
            }] if tickets else [],
            my_email=self.my_email,
            args={"object_id": object_id},
        )

    async def _export_objects(
        self, aql: str, destination_dir: str, file_name: str = "",
    ) -> Any:
        t0 = time.time()
        destination_dir = export_files.check_destination(destination_dir)
        file_name, fmt = export_files.export_file_name(file_name, "assets-objects")
        export_files.refuse_org_without_staging(
            "assets_export_objects", self.download_mode,
            (self.download_config or DownloadDeliveryConfig()).allow_disk_staging,
            "assets_search_objects",
        )
        objects = await self._fetch(self._assets.export_objects, aql)
        names: list[str] = []
        for o in objects:
            for name in o.attributes:
                if name not in names:
                    names.append(name)
        columns = [*_EXPORT_COLUMNS, *names]
        if not objects:
            self._auto_audit("assets_export_objects", "Export Jira Assets Objects",
                             f"Export: {aql[:80]}", "0 object(s)", t0)
            return {
                "rows": 0, "columns": columns, "sha256": None, "delivery": "none",
                "note": "No object matches this AQL, so no file was written.",
            }
        rows = [
            [o.id, o.key, o.label, o.type_name, *(_joined(o.attributes.get(n, [])) for n in names)]
            for o in objects
        ]
        data = await asyncio.to_thread(export_files.render, columns, rows, fmt)
        sha256 = hashlib.sha256(data).hexdigest()
        local_files.check_delivery_size(len(data))
        saved_to = export_files.saved_to(destination_dir, file_name, self.download_mode)
        preview = {"AQL": aql[:200], "Attributes": ", ".join(names) or "none"}
        await gated_call(
            connector=self.name,
            tool="assets_export_objects",
            tool_name="Export Jira Assets Objects",
            summary=f"Export: {aql[:80]}",
            sender=_SENDER,
            raw_data=[_ExportedObject(o.id, o.key, o.schema_id) for o in objects],
            filtered_data=None,
            gate="review",
            preview=preview,
            preview_tables=[export_files.sample_table(columns, rows)],
            pii_scan_text=export_files.scan_text(data),
            new_info={
                "Rows": f"{len(rows):,}",
                "Columns": str(len(columns)),
                "Content returned to {agent}": (
                    "None in the tool result: the rows are written to the file below, where "
                    "{agent} can open it"
                ),
                "Saved to": saved_to,
            },
            details_text=export_files.card_details(preview, len(rows), columns, saved_to, sha256),
            delivery="staged_link" if self.download_mode == "org"
            else "local_disk" if local_files.can_access_user_files(self.download_mode)
            else "client_bridge",
            my_email=self.my_email,
            args={
                "aql": aql, "destination_dir": destination_dir,
                "file_name": file_name, "sha256": sha256,
            },
        )
        delivered = local_files.deliver_export(
            destination_dir, file_name, data, export_files.MIME_TYPES[fmt],
            download_mode=self.download_mode, download_config=self.download_config,
            download_base_url=self.download_base_url,
        )
        return export_files.export_summary(columns, rows, sha256, delivered)

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
