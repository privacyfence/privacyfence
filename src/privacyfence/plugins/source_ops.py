"""``source.call``: the read-only window a plugin has onto PrivacyFence's connectors (ADR 0120, 0123).

A plugin never holds a connector token. It names one of six operations and PrivacyFence runs it
with the connector's own client, under the local principal, and hands the result back. The call is
not gated by a card (the user approved the operation list when they enabled the plugin), so
everything it does is audited: one ``plugin_source`` entry per call, with the target names and the
byte count and never the data. Nothing in this module logs or records what a client returned, or
what a client's exception said.

Each adapter reaches into a connector's private client attribute (``_sf``, ``_jira`` and so on).
That is deliberate and confined to this table: the clients already return what a plugin needs, and
no connector changes for it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from privacyfence.audit_log import AuditEntry, current_week, get_audit_logger
from privacyfence.calendar_client import CalendarClientError
from privacyfence.confluence_client import ConfluenceClientError
from privacyfence.connectors.salesforce import _parse_report_filters
from privacyfence.drive_client import DriveClientError
from privacyfence.jira_client import JiraClientError
from privacyfence.plugins.constants import (
    AUDIT_PLUGIN_SOURCE,
    DRIVE_CHUNK_BYTES,
    MAX_SOURCE_RESULT_BYTES,
    SOURCE_OPERATIONS,
)
from privacyfence.plugins.protocol import RpcError, SourceCallParams
from privacyfence.plugins.spool import DownloadSpool
from privacyfence.principal import LOCAL_PRINCIPAL, LOCAL_PRINCIPAL_ID, principal_scope
from privacyfence.salesforce_client import REPORT_FILTER_OPERATORS, SalesforceClientError

if TYPE_CHECKING:
    from privacyfence.connector import Connector
    from privacyfence.plugins.manifest import Manifest

logger = logging.getLogger(__name__)

# ``(enabled, blocked_by)`` as SettingsController._connectors_state reports them.
ConnectorState = tuple[bool, str | None]

_CLIENT_ERRORS = (
    SalesforceClientError,
    JiraClientError,
    DriveClientError,
    ConfluenceClientError,
    CalendarClientError,
    RuntimeError,
)

_UPSTREAM_DETAIL = "the service returned an error"
_VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_MAX_ID_CHARS = 256
_MAX_RANGE_CHARS = 512
_MAX_TIME_CHARS = 64
_MAX_JQL_CHARS = 8192


@dataclass(frozen=True)
class SourceAdapter:
    connector: str
    client_attr: str
    # (client, validated params + "plugin", spool) -> (data, next_cursor)
    run: Callable[[Any, dict, DownloadSpool], tuple[Any, str | None]]
    targets: Callable[[dict], str]
    validate: Callable[[dict], dict]


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


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]



# --- salesforce.report_run ---------------------------------------------------------------------


def _validate_salesforce(params: dict) -> dict:
    out: dict[str, Any] = {"report_id": _str(params, "report_id", max_len=_MAX_ID_CHARS)}
    filters = params.get("filters")
    if filters is not None:
        if not isinstance(filters, list):
            raise _bad("params.filters must be a list")
        for flt in filters:
            # The saved report is fetched before the client looks at a filter's operator, so a bad
            # one is refused here with the client's own list of operators.
            if isinstance(flt, dict) and isinstance(flt.get("operator"), str):
                if flt["operator"] not in REPORT_FILTER_OPERATORS:
                    raise _bad("params.filters has an unknown operator")
        try:
            _parse_report_filters(json.dumps(filters))
        except (ValueError, TypeError) as exc:
            raise _bad(str(exc)) from None
    out["filters"] = filters or []
    return out


def _run_salesforce(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    filters = _parse_report_filters(json.dumps(params["filters"])) if params["filters"] else None
    return client.run_report(params["report_id"], filters=filters), None


# --- jira.search -------------------------------------------------------------------------------


def _validate_jira(params: dict) -> dict:
    return {
        "jql": _str(params, "jql", max_len=_MAX_JQL_CHARS),
        "max_results": _int(params, "max_results", low=1, high=500, default=100),
    }


def _run_jira(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    issues = client.search_issues(params["jql"], params["max_results"])
    return [dataclasses.asdict(issue) for issue in issues], None


# --- drive.download ----------------------------------------------------------------------------


def _validate_drive_download(params: dict) -> dict:
    out = {
        "file_id": _str(params, "file_id", max_len=_MAX_ID_CHARS),
        "length": _int(params, "length", low=1, high=DRIVE_CHUNK_BYTES, default=DRIVE_CHUNK_BYTES),
        "offset": _int(params, "offset", low=0, high=None),
        "cursor": _str(params, "cursor", max_len=_MAX_RANGE_CHARS, required=False),
    }
    if out["offset"] is not None and out["cursor"] is not None:
        raise _bad("offset and cursor cannot both be given")
    return out


def _run_drive_download(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    return spool.read_chunk(
        client,
        params["plugin"],
        params["file_id"],
        offset=params["offset"],
        cursor=params["cursor"],
        length=params["length"],
    )


# --- sheets.get_values -------------------------------------------------------------------------


def _validate_sheets(params: dict) -> dict:
    option = _str(params, "value_render_option", max_len=32, required=False, default="FORMATTED_VALUE")
    if option not in _VALUE_RENDER_OPTIONS:
        raise _bad(f"params.value_render_option must be one of {', '.join(_VALUE_RENDER_OPTIONS)}")
    return {
        "spreadsheet_id": _str(params, "spreadsheet_id", max_len=_MAX_ID_CHARS),
        "range": _str(params, "range", max_len=_MAX_RANGE_CHARS),
        "value_render_option": option,
    }


def _run_sheets(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    values = client.get_sheet_values(params["spreadsheet_id"], params["range"], params["value_render_option"])
    return {"values": values}, None


# --- confluence.get_page -----------------------------------------------------------------------


def _validate_confluence(params: dict) -> dict:
    return {"page_id": _str(params, "page_id", max_len=_MAX_ID_CHARS)}


def _run_confluence(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    return dataclasses.asdict(client.get_page(params["page_id"])), None


# --- calendar.list_events ----------------------------------------------------------------------


def _validate_calendar(params: dict) -> dict:
    return {
        "calendar_id": _str(params, "calendar_id", max_len=_MAX_ID_CHARS, required=False, default="primary"),
        "time_min": _str(params, "time_min", max_len=_MAX_TIME_CHARS),
        "time_max": _str(params, "time_max", max_len=_MAX_TIME_CHARS),
        "max_results": _int(params, "max_results", low=1, high=250, default=250),
    }


def _run_calendar(client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    events = client.list_events(params["calendar_id"], params["max_results"], params["time_min"], params["time_max"])
    return [dataclasses.asdict(event) for event in events], None


SOURCE_ADAPTERS: dict[str, SourceAdapter] = {
    "salesforce.report_run": SourceAdapter(
        "salesforce",
        "_sf",
        _run_salesforce,
        lambda p: f"{p['report_id']}; filters={len(p['filters'])}",
        _validate_salesforce,
    ),
    "jira.search": SourceAdapter(
        "jira",
        "_jira",
        _run_jira,
        lambda p: f"jql:{_hash(p['jql'])}; max_results={p['max_results']}",
        _validate_jira,
    ),
    "drive.download": SourceAdapter(
        "drive",
        "_drive",
        _run_drive_download,
        lambda p: f"{p['file_id']}; offset={'cursor' if p['cursor'] else p['offset'] or 0}; length={p['length']}",
        _validate_drive_download,
    ),
    "sheets.get_values": SourceAdapter(
        "drive",
        "_drive",
        _run_sheets,
        lambda p: f"{p['spreadsheet_id']}; {p['range']}",
        _validate_sheets,
    ),
    "confluence.get_page": SourceAdapter(
        "confluence",
        "_confluence",
        _run_confluence,
        lambda p: p["page_id"],
        _validate_confluence,
    ),
    "calendar.list_events": SourceAdapter(
        "calendar",
        "_calendar",
        _run_calendar,
        lambda p: f"{p['calendar_id']}; {p['time_min']}; {p['time_max']}",
        _validate_calendar,
    ),
}


def _unavailable(reason: str) -> RpcError:
    return RpcError("connector_unavailable", f"the connector is {reason.replace('_', ' ')}", extra={"reason": reason})


def _audit(plugin: str, operation: str, summary: str, started: float) -> None:
    try:
        get_audit_logger().record(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=f"plugin:{plugin}",
                tool=operation,
                tool_name=f"{plugin} source read",
                summary=summary,
                sender="",
                decision=AUDIT_PLUGIN_SOURCE,
                auto_accept_rule="",
                latency_seconds=time.monotonic() - started,
                claude_reason="",
            )
        )
    except Exception:
        logger.warning("could not write the audit entry for a source call by plugin %s", plugin, exc_info=True)


def _run_in_scope(adapter: SourceAdapter, client: Any, params: dict, spool: DownloadSpool) -> tuple[Any, str | None]:
    with principal_scope(LOCAL_PRINCIPAL):
        return adapter.run(client, params, spool)


async def _serve(
    raw: dict,
    *,
    plugin: str,
    manifest: Manifest,
    introspecting: bool,
    connectors_provider: Callable[[], dict[str, Connector]],
    connector_state: Callable[[str], ConnectorState],
    spool: DownloadSpool,
    seen: dict[str, str],
) -> dict:
    if introspecting:
        raise RpcError("introspection_only", "source.call is not available while the plugin is inspected")
    parsed = SourceCallParams.from_wire(raw, mode="local")
    if parsed.principal != LOCAL_PRINCIPAL_ID:
        raise RpcError("unknown_principal", "only the local principal exists")
    operation = parsed.operation
    if operation not in SOURCE_OPERATIONS or operation not in manifest.source_operations:
        raise RpcError("operation_not_allowed", "the plugin may not use this operation")
    seen["operation"] = operation

    adapter = SOURCE_ADAPTERS[operation]
    enabled, blocked_by = connector_state(adapter.connector)
    connector = connectors_provider().get(adapter.connector)
    if not enabled:
        raise _unavailable("disabled")
    if connector is None or blocked_by == "not_authenticated":
        raise _unavailable("not_authenticated")
    if blocked_by:
        raise _unavailable("unavailable")
    client = getattr(connector, adapter.client_attr, None)
    if client is None:
        raise _unavailable("unavailable")

    params = adapter.validate(parsed.params)
    seen["targets"] = adapter.targets(params)
    try:
        data, cursor = await asyncio.to_thread(
            _run_in_scope, adapter, client, {**params, "plugin": plugin}, spool
        )
    except RpcError:
        raise
    except _CLIENT_ERRORS as exc:
        logger.warning("source call %s by plugin %s failed upstream: %s", operation, plugin, type(exc).__name__)
        raise RpcError("upstream_error", _UPSTREAM_DETAIL) from None

    payload = json.dumps(data, default=str)
    if len(payload.encode()) > MAX_SOURCE_RESULT_BYTES:
        raise RpcError("payload_too_large", "the result is larger than the source result limit")
    seen["bytes"] = str(len(payload))
    return {"operation": operation, "data": json.loads(payload), "bytes": len(payload), "next_cursor": cursor}


async def handle_source_call(
    params: dict,
    *,
    plugin: str,
    manifest: Manifest,
    introspecting: bool,
    connectors_provider: Callable[[], dict[str, Connector]],
    connector_state: Callable[[str], ConnectorState],
    spool: DownloadSpool,
) -> dict:
    started = time.monotonic()
    spool.sweep()
    seen: dict[str, str] = {}
    try:
        result = await _serve(
            params,
            plugin=plugin,
            manifest=manifest,
            introspecting=introspecting,
            connectors_provider=connectors_provider,
            connector_state=connector_state,
            spool=spool,
            seen=seen,
        )
    except RpcError as exc:
        _audit(plugin, seen.get("operation", "unknown"), f"{seen.get('targets', '-')}; error={exc.code}", started)
        raise
    except Exception as exc:
        logger.error("source call by plugin %s failed: %s", plugin, type(exc).__name__)
        _audit(plugin, seen.get("operation", "unknown"), f"{seen.get('targets', '-')}; error=internal_error", started)
        raise RpcError("internal_error", "the source call failed") from None
    _audit(plugin, result["operation"], f"{seen['targets']}; bytes={result['bytes']}", started)
    return result
