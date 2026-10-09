"""Every number, name and pattern the plugin protocol shares between the daemon and a plugin.

One module so the wire limits, the timeouts and the naming rules cannot drift apart between the
supervisor, the validators and the published JSON schema (ADR 0120, ADR 0122). Names that end up in
an MCP tool name or a policy key are built only by the helpers at the bottom, so the first
underscore of an MCP tool name always splits the plugin from the tool: plugin names hold no
underscore. The model-facing consequences of the gate limits are in ADR 0121.
"""
from __future__ import annotations

import re

PROTOCOL_VERSION = "1.2.0"
PROTOCOL_MAJOR = 1

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_IN_FLIGHT = 16
INVALID_LINES_LIMIT = 3
INLINE_RESULT_BYTES = 100_000              # ADR 0092
WRITE_RESULT_MAX_BYTES = 2048              # ADR 0133
MAX_PREVIEW_BYTES = 64 * 1024
MAX_PREVIEW_BLOCKS = 50
MAX_CELL_CHARS = 4096
MAX_TITLE_CHARS = 120
MAX_EFFECT_CHARS = 200
MAX_DESCRIPTION_CHARS = 1024               # tests/unit/web/test_tool_schema_portability.py's limit
MAX_SOURCE_RESULT_BYTES = 12 * 1024 * 1024
DRIVE_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_SPOOL_IDLE_SECONDS = 600
MAX_PAGE_BODY_BYTES = 8 * 1024 * 1024
MAX_PAGE_PATH_CHARS = 512
MAX_PAGE_INDEX_ENTRIES = 500
MAX_PAGE_VERSION_CHARS = 40
MAX_PAGE_DESCRIPTION_CHARS = 200
MAX_TOOLS = 64
MAX_SCOPE_VALUES = 100
MAX_SCOPE_VALUE_CHARS = 200
MAX_SCOPE_TYPE_DESCRIPTION_CHARS = 500
MCP_TOOL_NAME_MAX = 64
SOURCE_PAGE_BUDGET_BYTES = MAX_SOURCE_RESULT_BYTES - 64 * 1024   # room for the envelope
CURSOR_MAX_CHARS = 4096
JIRA_PAGE_SIZE_MAX = 100
CALENDAR_PAGE_SIZE_MAX = 250
SUBJECT_ID_MAX_CHARS = 200
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")      # always .fullmatch()
APPROVAL_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,40}")   # always .fullmatch()
PAGE_ENTRY_PATH_RE = re.compile(r"/[\x21\x22\x24-\x5b\x5d-\x7e]*")   # always .fullmatch(); no space, # or backslash
OUTPUT_TYPES: dict[str, tuple[str, ...]] = {
    "application/json": (".json",),
    "text/csv": (".csv",),
    "text/html": (".html", ".htm"),
    "text/plain": (".txt",),
    "text/markdown": (".md",),
}
DEFAULT_OUTPUT_TYPES = ("application/json", "text/csv")
OUTPUT_READ_PAGE_BYTES = 90_000                     # under INLINE_RESULT_BYTES with the envelope
OUTPUT_LIST_PAGE = 200
OUTPUT_MAX_DEPTH = 8

TIMEOUT_SECONDS: dict[str, float] = {
    "initialize": 10.0,
    "tool.prepare": 30.0,
    "tool.execute": 60.0,
    "web.request": 10.0,
    "pages.list": 10.0,
    "storage.purge": 30.0,
    "source.call": 120.0,
    "confirm.request": 5.0,
    "approval.request": 5.0,
}
CONFIRM_AWAIT_MAX_MS = 300_000
MAX_PENDING_CONFIRMS = 64                  # all plugins together; the host's finalizer threads
MAX_PENDING_CONFIRMS_PER_PLUGIN = 8
SHUTDOWN_GRACE_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 2.0
SEND_TIMEOUT_SECONDS = 10.0                # ADR 0132: a message the plugin does not read in time is a crash
SHUTDOWN_NOTIFY_TIMEOUT_SECONDS = 1.0
CLOSE_WAIT_SECONDS = 1.0
RESTART_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
CRASH_LIMIT = 5
CRASH_WINDOW_SECONDS = 600.0
PENDING_CARD_SECONDS = 900.0               # = approvals.DEFAULT_PENDING_TTL_SECONDS
DECISION_REPLAY_SECONDS = 300.0            # = approvals.DEFAULT_LEDGER_TTL_SECONDS
# A deferred card can still release, and the daemon can still replay a decision, within this window.
PREPARED_CALL_LIFETIME_SECONDS = PENDING_CARD_SECONDS + DECISION_REPLAY_SECONDS   # 1200.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_PUMP_CHUNK_BYTES = 64 * 1024
LOG_DRAIN_SECONDS = 2.0

PLUGIN_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")     # always .fullmatch()
TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")       # always .fullmatch()
SCOPE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")      # always .fullmatch()
BLOCK_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")       # always .fullmatch(); table column keys
RESERVED_PLUGIN_NAMES = frozenset({
    "privacyfence", "plugin", "plugins", "settings", "mcp",
    "gmail", "drive", "contacts", "calendar", "tasks", "apps_script",
    "slack", "jira", "confluence", "salesforce", "telegram",
    "apps", "sheets", "docs",
})

SOURCE_OPERATIONS: tuple[str, ...] = (
    "salesforce.report_run", "jira.search", "drive.download",
    "sheets.get_values", "confluence.get_page", "calendar.list_events",
)

GATES = ("auto", "review", "popup")
BLOCK_TYPES = ("heading", "fields", "table", "text", "code", "diff")

AUDIT_PLUGIN_SOURCE = "plugin_source"
AUDIT_PLUGIN_CONFIRM = "plugin_confirm"
AUDIT_PLUGIN_LIFECYCLE = "plugin_lifecycle"
AUDIT_PLUGIN_APPROVAL = "plugin_approval"
AUDIT_PLUGIN_OUTPUT = "plugin_output"

ERROR_CODES: dict[str, int] = {
    "parse_error": -32700, "invalid_request": -32600, "method_not_found": -32601,
    "invalid_params": -32602, "internal_error": -32603,
    "operation_not_allowed": -32001, "connector_unavailable": -32002, "unknown_principal": -32003,
    "payload_too_large": -32004, "upstream_error": -32005, "org_only_field": -32006,
    "confirmation_refused": -32007, "unknown_tool": -32008, "invalid_blocks": -32009,
    "version_mismatch": -32010, "unknown_call": -32011, "digest_mismatch": -32012,
    "timeout": -32013, "introspection_only": -32014,
}


def mcp_tool_name(plugin: str, tool: str) -> str:
    return f"{plugin}_{tool}"


def operation_key(plugin: str, tool: str) -> str:
    return f"plugin.{plugin}.{tool}"


def scope_predicate(plugin: str, scope_type: str) -> str:
    return f"plugin:{plugin}:{scope_type}"
