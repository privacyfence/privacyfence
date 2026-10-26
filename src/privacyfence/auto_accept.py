"""Auto-accept: the v2 policy store's hot-reloaded per-principal cache, plus the tool/gate/
temp-accept infrastructure every gated call needs regardless of engine.

``gate.py`` evaluates against the ``auto_accept:`` section exclusively (``get_policy_v2_store_rules``
below), and every surface -- Settings, the MCP propose tool, the popup's "Always allow" -- writes
through ``add_policy_v2_rules``. A config still carrying the earlier ``auto_accept_rules``/
``auto_accept_grants`` sections is refused at startup (``policy.store.reject_v1_sections``).

What's left here is genuinely engine-agnostic:

- ``TOOL_TO_OPERATION``/``TOOL_TO_GATE`` -- which operation key and which gate (``auto``/``review``/
  ``popup``) a tool maps to. Every rule engine v1 or v2 has ever had keys off these.
- The same-file temp-accept grace window (``TEMP_ACCEPT_ELIGIBLE_OPERATIONS``,
  ``register_temp_accept``/``is_temp_accepted``) -- an in-memory, non-persisted acceptance that both
  ``policy.engine.evaluate``/``preflight`` and ``gate.py`` consult via callback, independent of
  where the rule list they're otherwise checking comes from.
- ``ReviewContext`` and the small parsing helpers (``_file_from``, ``_domain_of``, ``_address_of``,
  ``_attendee_email``) the scope/condition selectors (``policy/scopes.py``, ``policy/conditions.py``)
  and the proposal builder (``policy/propose.py``) import directly.
- ``_AutoAcceptState``/the ``PrincipalRegistry`` it lives in -- per-principal config path, the
  temp-accept store, the hot-reloaded v2 rule cache, and the rules-changed listener broadcast that
  lets a pending approval re-resolve the moment a rule changes.
"""
from __future__ import annotations
import logging
import threading
import time
import yaml
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Callable

from .principal import PrincipalRegistry, current_principal
from .policy.resource_registry import DRIVE_SANDBOX_WRITE_TARGETS
from .secure_files import atomic_write_text

if TYPE_CHECKING:
    # Only for the type hint on `_AutoAcceptState.policy_v2_store_rules`/`set_policy_v2_store_rules`
    # below -- a real (non-TYPE_CHECKING) import would be circular: `policy.engine` itself imports
    # `ReviewContext`/`temp_accept_key` from this module.
    from .policy.engine import PolicyRule
    from .policy.registry import Verb
    from .policy.scope_rows import ScopeRow

logger = logging.getLogger(__name__)

_TEMP_ACCEPT_EXCLUDED_OPERATIONS: frozenset[str] = frozenset({
    # Destructive with no undo path through PrivacyFence, unlike
    # insert/format -- gets the standing-rule treatment only (like
    # sheets.add_sheet/sheets.rename_sheet below), never the lighter-weight
    # temp accept.
    "sheets.delete_dimensions",
    # One-shot per file, not called in a burst the way write_range/
    # format_range/insert_dimensions are.
    "sheets.add_sheet",
    "sheets.rename_sheet",
    # Each clears/replaces a file's whole content in one call rather than
    # being repeated cell-by-cell or range-by-range against the same file.
    "drive.write_file",
    "drive.write_doc",
    # One-shot actions on a file, not a burst of repeated calls against it.
    "drive.upload_file",
    "drive.move_file",
    "drive.copy_file",
})

# Write operations expected to be called repeatedly against the same file in
# quick succession (e.g. an agent filling in a sheet cell-by-cell, or building
# up formatting one range at a time). Allow once on one of these also arms a
# lightweight, in-memory grace window scoped to one file (gate.py) -- there's
# no separate button for it -- and never persisted to settings.yaml, unlike
# Always allow, so it disappears with the daemon and with wall-clock time,
# a much smaller commitment than a standing rule. Maps operation key -> the
# args field that identifies "the same file" for that operation -- every
# sheets.* operation addresses its spreadsheet by spreadsheet_id, every
# drive.*/docs.* operation by file_id (see DRIVE_SANDBOX_WRITE_TARGETS).
TEMP_ACCEPT_ELIGIBLE_OPERATIONS: dict[str, str] = {
    op_key: ("spreadsheet_id" if op_key.startswith("sheets.") else "file_id")
    for op_key, _rule_name in DRIVE_SANDBOX_WRITE_TARGETS
    if op_key not in _TEMP_ACCEPT_EXCLUDED_OPERATIONS
}

TEMP_ACCEPT_TTL_SECONDS = 300

# Maps tool name → operation key used in settings.yaml
TOOL_TO_OPERATION: dict[str, str] = {
    "gmail_get_message":              "gmail.read_message",
    "gmail_get_thread":               "gmail.read_thread",
    "gmail_download_attachment":      "gmail.download_attachment",
    "gmail_create_draft":             "gmail.create_draft",
    "gmail_reply_draft":              "gmail.create_draft",
    "gmail_reply_all_draft":          "gmail.create_draft",
    "gmail_create_draft_with_attachments":    "gmail.create_draft",
    "gmail_reply_draft_with_attachments":     "gmail.create_draft",
    "gmail_reply_all_draft_with_attachments": "gmail.create_draft",
    "gmail_add_label":                "gmail.add_label",
    "gmail_remove_label":             "gmail.remove_label",
    "gmail_archive_message":          "gmail.archive_message",
    "gmail_create_filter":            "gmail.create_filter",
    "gmail_update_filter":            "gmail.update_filter",
    "gmail_create_label":             "gmail.create_label",
    "drive_get_file_content":         "drive.read_file_contents",
    "drive_get_revision_content":     "drive.read_file_contents",
    "drive_download_file":           "drive.download_file",
    "drive_write_file_content":       "drive.write_file",
    "drive_write_doc_content":        "drive.write_doc",
    "drive_upload_file":              "drive.upload_file",
    "drive_move_file":                "drive.move_file",
    "drive_copy_file":                "drive.copy_file",
    "drive_add_comment":              "drive.comment_file",
    "drive_sheets_get_values":        "sheets.read_values",
    "drive_sheets_write_range":       "sheets.write_range",
    "drive_sheets_add_sheet":         "sheets.add_sheet",
    "drive_sheets_rename_sheet":      "sheets.rename_sheet",
    "drive_sheets_format_range":      "sheets.format_range",
    "drive_sheets_insert_dimensions": "sheets.insert_dimensions",
    "drive_sheets_delete_dimensions": "sheets.delete_dimensions",
    "drive_docs_edit_content":        "docs.edit_content",
    "drive_docs_format_content":      "docs.format_content",
    "slack_get_channel_history":      "slack.read_messages",
    "slack_get_thread_replies":       "slack.read_messages",
    "slack_search_messages":          "slack.read_messages",
    "slack_create_group_chat":        "slack.create_group_chat",
    "slack_send_message":             "slack.send_message",
    "calendar_get_event_details":     "calendar.read_event_details",
    "calendar_create_event":          "calendar.create_modify_event",
    "calendar_update_event":          "calendar.create_modify_event",
    "calendar_delete_event":          "calendar.delete_event",
    "calendar_create_out_of_office":  "calendar.out_of_office",
    "calendar_set_working_location":  "calendar.working_location",
    "calendar_set_event_visibility":  "calendar.set_visibility",
    "calendar_set_event_color":       "calendar.set_color",
    "salesforce_get_record":          "salesforce.read_record",
    "salesforce_run_report":          "salesforce.run_report",
    "salesforce_export_report":       "salesforce.run_report",
    "salesforce_search":              "salesforce.search",
    "salesforce_create_record":       "salesforce.create_record",
    "salesforce_update_record":       "salesforce.update_record",
    "salesforce_create_records":      "salesforce.create_records",
    "salesforce_update_records":      "salesforce.update_records",
    "grist_get_records":              "grist.read_records",
    "grist_export_records":           "grist.read_records",
    "grist_export_sql":               "grist.read_records",
    "grist_add_records":              "grist.add_records",
    "grist_update_records":           "grist.update_records",
    "grist_create_table":             "grist.create_table",
    "grist_add_columns":              "grist.add_columns",
    "grist_import_csv":               "grist.import_csv",
    "grist_update_csv":               "grist.update_csv",
    "peek_get_comments":              "peek.read_comments",
    "peek_get_stats":                 "peek.read_stats",
    "peek_upload_page":               "peek.upload_page",
    "peek_set_visibility":            "peek.set_visibility",
    "peek_add_comment":               "peek.add_comment",
    "peek_rename_page":               "peek.rename_page",
    "peek_delete_page":               "peek.delete_page",
    "miro_list_items":                "miro.list_items",
    "miro_get_items":                 "miro.get_items",
    "miro_list_connectors":           "miro.list_connectors",
    "miro_create_items":              "miro.create_items",
    "miro_create_connectors":         "miro.create_connectors",
    "miro_update_items":              "miro.update_items",
    "miro_update_board":              "miro.update_board",
    "contacts_update":                "contacts.edit",
    "contacts_create":                "contacts.create",
    "contacts_add_label":             "contacts.add_label",
    "contacts_remove_label":          "contacts.remove_label",
    "jira_get_issue":                 "jira.read_issue",
    "jira_search_issues_with_fields": "jira.read_issue",
    "jira_export_issues":             "jira.read_issue",
    "jira_create_issue":              "jira.create_issue",
    "jira_add_comment":               "jira.add_comment",
    "jira_update_issue":              "jira.update_issue",
    "jira_transition_issue":          "jira.transition_issue",
    "assets_get_object":              "assets.read_object",
    "assets_search_objects":          "assets.read_object",
    "assets_get_related_objects":     "assets.read_object",
    "assets_get_connected_tickets":   "assets.read_tickets",
    "assets_export_objects":          "assets.read_object",
    "confluence_get_page":            "confluence.read_page",
    "confluence_get_page_by_title":   "confluence.read_page",
    "confluence_download_attachment": "confluence.download_attachment",
    "confluence_create_page":         "confluence.create_page",
    "confluence_update_page":         "confluence.update_page",
    "telegram_get_messages":          "telegram.read_chat_messages",
    # Shares telegram.read_chat_messages with telegram_get_messages (rather
    # than its own telegram.search_messages key) so a "trusted chats" rule
    # only needs configuring once per chat, not once per Telegram read tool
    # -- matching slack.read_messages, which slack_search_messages already
    # shares with slack_get_channel_history/slack_get_thread_replies.
    "telegram_search_messages":       "telegram.read_chat_messages",
    "telegram_send_message":          "telegram.send_message",
    "tasks_create_task":              "tasks.create_task",
    "tasks_update_task":              "tasks.update_task",
    "tasks_complete_task":            "tasks.complete_task",
    "tasks_uncomplete_task":          "tasks.uncomplete_task",
    "tasks_move_task":                "tasks.move_task",
    "apps_script_get_content":        "apps_script.read_content",
    "apps_script_write_content":      "apps_script.write_content",
    "apps_script_get_execution_log":  "apps_script.read_execution_log",
}

# Maps tool name -> the gate it goes through: "auto" (never reaches gated_call
# at all), "review" (gated_call gate="review", the read direction), or
# "popup" (gated_call gate="popup", the write direction). Unlike
# TOOL_TO_OPERATION, this covers every tool, including the unconditionally
# auto-accepted ones, since a preflight check needs a definitive answer for
# those too.
#
# This is the single source of truth docs/tools-reference.md's per-tool
# gate column is checked against (see
# tests/unit/connectors/test_readme_manifest_alignment.py) -- keep it in sync
# with the gate= argument each connectors/*.py call site actually passes to
# gated_call(), not just with the docs.
TOOL_TO_GATE: dict[str, str] = {
    # Gmail
    "gmail_list_messages":             "auto",
    "gmail_list_threads":              "auto",
    "gmail_get_message":               "review",
    "gmail_get_thread":                "review",
    "gmail_list_message_attachments":  "auto",
    "gmail_download_attachment":       "review",
    "gmail_create_draft":              "popup",
    "gmail_reply_draft":               "popup",
    "gmail_reply_all_draft":           "popup",
    "gmail_create_draft_with_attachments":    "popup",
    "gmail_reply_draft_with_attachments":     "popup",
    "gmail_reply_all_draft_with_attachments": "popup",
    "gmail_add_label":                 "popup",
    "gmail_remove_label":              "popup",
    "gmail_archive_message":           "popup",
    "gmail_list_filters":              "auto",
    "gmail_list_labels":               "auto",
    "gmail_create_filter":             "popup",
    "gmail_update_filter":             "popup",
    "gmail_create_label":              "popup",
    # Google Drive (incl. Sheets)
    "drive_list_files":                "auto",
    "drive_get_file_metadata":         "auto",
    "drive_list_revisions":            "auto",
    "drive_list_folder":               "auto",
    "drive_list_shared_drives":        "auto",
    "drive_create_blank_file":         "auto",
    "drive_get_file_content":          "review",
    "drive_get_revision_content":      "review",
    "drive_download_file":             "review",
    "drive_write_file_content":        "popup",
    "drive_upload_file":               "popup",
    "drive_write_doc_content":         "popup",
    "drive_move_file":                 "popup",
    "drive_copy_file":                 "popup",
    "drive_add_comment":               "popup",
    "drive_sheets_create":             "auto",
    "drive_sheets_get_metadata":       "auto",
    "drive_sheets_get_values":         "review",
    "drive_sheets_write_range":        "popup",
    "drive_sheets_add_sheet":          "popup",
    "drive_sheets_rename_sheet":       "popup",
    "drive_sheets_format_range":       "popup",
    "drive_sheets_insert_dimensions":  "popup",
    "drive_sheets_delete_dimensions":  "popup",
    "drive_docs_edit_content":         "popup",
    "drive_docs_format_content":       "popup",
    # Slack
    "slack_list_channels":             "auto",
    "slack_list_dms":                  "auto",
    "slack_list_group_chats":          "auto",
    "slack_resolve_permalink":         "auto",
    "slack_refresh_user_cache":        "auto",
    "slack_refresh_channel_cache":     "auto",
    "slack_get_channel_history":       "review",
    "slack_get_thread_replies":        "review",
    "slack_search_messages":           "review",
    "slack_create_group_chat":         "popup",
    "slack_send_message":              "popup",
    # Google Calendar
    "calendar_list_calendars":         "auto",
    "calendar_list_events":            "auto",
    "calendar_get_free_busy":          "auto",
    "calendar_list_rooms":             "auto",
    "calendar_get_event_visibility":   "auto",
    "calendar_list_colors":            "auto",
    "calendar_get_event_details":      "review",
    "calendar_create_event":           "popup",
    "calendar_update_event":           "popup",
    "calendar_delete_event":           "popup",
    "calendar_create_out_of_office":   "popup",
    "calendar_set_working_location":   "popup",
    "calendar_set_event_visibility":   "popup",
    "calendar_set_event_color":        "popup",
    # Google Contacts
    "contacts_list":                   "auto",
    "contacts_search":                 "auto",
    "contacts_get":                    "auto",
    "contacts_update":                 "popup",
    "contacts_create":                 "popup",
    "contacts_add_label":              "popup",
    "contacts_remove_label":           "popup",
    # Telegram
    "telegram_list_chats":             "auto",
    "telegram_refresh_chat_cache":     "auto",
    "telegram_get_messages":           "review",
    "telegram_search_messages":        "review",
    "telegram_send_message":           "popup",
    # Salesforce
    "salesforce_list_reports":         "auto",
    "salesforce_get_record":           "review",
    "salesforce_run_report":           "review",
    "salesforce_export_report":        "review",
    "salesforce_search":               "review",
    "salesforce_create_record":        "popup",
    "salesforce_update_record":        "popup",
    "salesforce_create_records":       "popup",
    "salesforce_update_records":       "popup",
    # Jira
    "jira_list_projects":              "auto",
    "jira_search_issues":              "auto",
    "jira_get_transitions":            "auto",
    "jira_list_fields":                "auto",
    "jira_find_users":                 "auto",
    "jira_refresh_user_cache":         "auto",
    "jira_get_issue":                  "review",
    "jira_search_issues_with_fields":  "review",
    "jira_export_issues":              "review",
    "jira_create_issue":               "popup",
    "jira_add_comment":                "popup",
    "jira_update_issue":               "popup",
    "jira_transition_issue":           "popup",
    # Confluence
    "confluence_list_spaces":          "auto",
    "confluence_search":               "auto",
    "confluence_cql_search":           "auto",
    "confluence_list_pages":           "auto",
    "confluence_get_page":             "review",
    "confluence_get_page_by_title":    "review",
    "confluence_list_attachments":     "auto",
    "confluence_find_users":           "auto",
    "confluence_refresh_user_cache":   "auto",
    "confluence_download_attachment":  "review",
    "confluence_create_page":          "popup",
    "confluence_update_page":          "popup",
    # Google Tasks
    "tasks_list_task_lists":           "auto",
    "tasks_list_tasks":                "auto",
    "tasks_get_task":                  "auto",
    "tasks_create_task":               "popup",
    "tasks_update_task":               "popup",
    "tasks_complete_task":             "popup",
    "tasks_uncomplete_task":           "popup",
    "tasks_move_task":                 "popup",
    # Apps Script
    "apps_script_list_projects":       "auto",
    "apps_script_get_content":         "review",
    "apps_script_write_content":       "popup",
    "apps_script_get_execution_log":   "review",
    # Grist
    "grist_list_documents":            "auto",
    "grist_list_tables":               "auto",
    "grist_get_records":               "review",
    "grist_export_records":            "review",
    "grist_export_sql":                "review",
    "grist_add_records":               "popup",
    "grist_update_records":            "popup",
    "grist_create_table":              "popup",
    "grist_add_columns":               "popup",
    "grist_import_csv":                "popup",
    "grist_update_csv":                "popup",
    # Jira Assets
    "assets_list_schemas":             "auto",
    "assets_list_object_types":        "auto",
    "assets_list_attributes":          "auto",
    "assets_search_objects":           "review",
    "assets_get_object":               "review",
    "assets_get_related_objects":      "review",
    "assets_get_connected_tickets":    "review",
    "assets_export_objects":           "review",
    # Peek
    "peek_list_pages":                 "auto",
    "peek_get_comments":               "review",
    "peek_get_stats":                  "review",
    "peek_upload_page":                "popup",
    "peek_set_visibility":             "popup",
    "peek_add_comment":                "popup",
    "peek_rename_page":                "popup",
    "peek_delete_page":                "popup",
    # Miro
    "miro_list_boards":                "auto",
    "miro_get_board":                  "auto",
    "miro_list_tags":                  "auto",
    "miro_list_items":                 "review",
    "miro_get_items":                  "review",
    "miro_list_connectors":            "review",
    "miro_create_items":               "popup",
    "miro_create_connectors":          "popup",
    "miro_update_items":               "popup",
    "miro_update_board":               "popup",
}

# The tools a scoped token's allow_personal_data flag applies to (ADR 0173): each one's result carries no
# row (the data goes to a file or to a page the user's own rule names), so the PII scan, which
# guards the model's context, is not what protects their data.
PERSONAL_DATA_RULE_TOOLS: frozenset[str] = frozenset({
    "grist_export_records",
    "grist_export_sql",
    "salesforce_export_report",
    "jira_export_issues",
    "assets_export_objects",
    "peek_upload_page",
    "drive_download_file",
})

# Reads whose content reaches the AI system by design, which a token's allow_personal_data_reads flag
# may let through when one of the token's rules covers the call.
PERSONAL_DATA_READ_TOOLS: frozenset[str] = frozenset({
    "gmail_get_message",
    "gmail_get_thread",
    "slack_get_channel_history",
    "slack_get_thread_replies",
    "slack_search_messages",
})

# Popup-gate tools whose payload is scanned too (the gate's upload scan): drive_upload_file's can be
# external content the model never read, peek_upload_page's is the page body.
UPLOAD_PII_SCAN_TOOLS: frozenset[str] = frozenset({"drive_upload_file", "peek_upload_page"})

@dataclass
class ReviewContext:
    connector: str
    tool: str
    args: dict
    raw_data: Any
    my_email: str = ""
    my_domain: str = field(init=False)
    session_created_ids: set = field(default_factory=set)
    my_account_id: str = ""

    def __post_init__(self):
        self.my_domain = self.my_email.split("@", 1)[-1] if "@" in self.my_email else ""



def _file_from(raw: Any) -> Any:
    """Unwrap a Drive file object out of whatever shape a call's raw_data
    carries it in -- a dict with a "file" key (e.g. {"file": drive_file,
    "values": ...}) or an object with a .file attribute (e.g. get_file_
    content's response), or the raw value itself if it's already the file.
    """
    if isinstance(raw, dict):
        return raw.get("file", raw)
    return raw.file if hasattr(raw, "file") else raw


def _domain_of(sender: str) -> str:
    """The lower-cased domain of the real address in a header-shaped string, or "" when it has none.

    Parsed with `_address_of`, so a display name that merely contains an address cannot supply the
    domain. An address without exactly one "@" has no domain: some Python 3.11 releases return
    `x@evil.com@trusted.com` from parseaddr (CVE-2023-27043), and an empty domain matches no rule.
    """
    addr = _address_of(sender)
    return addr.rsplit("@", 1)[1].strip() if addr.count("@") == 1 else ""


def _address_of(raw: str) -> str:
    """Extract just the address out of a raw RFC 5322 header-shaped string
    ("Display Name <addr@example.com>" or a bare "addr@example.com"),
    lower-cased, discarding the display name entirely.

    Used everywhere an identity rule needs an exact-address comparison
    against ctx.my_email rather than a substring check -- a naive
    `my_email in raw.lower()` matches a spoofed display name that simply
    contains the victim's address as text (e.g. a "From" header whose real
    address is attacker-controlled but whose display name reads
    "victim@example.com") or a lookalike address the real one happens to be
    a substring of (e.g. "victim@example.com.attacker.net"). parseaddr()
    understands the header syntax, so it isolates the actual address either
    way; comparing that with `==` closes both holes. parseaddr() on a
    malformed string degrades to ("", "") rather than raising, so this
    never throws.
    """
    return parseaddr(raw or "")[1].strip().lower()


def temp_accept_key(operation_key: str, ctx: "ReviewContext") -> str | None:
    """The file identity a temp accept for this operation would be scoped to.

    Returns None when the operation isn't in TEMP_ACCEPT_ELIGIBLE_OPERATIONS
    or the expected arg is missing — either way, gate.py takes that as
    "don't show the disclosure caption, and don't arm a grace window on
    Allow once."
    """
    arg_name = TEMP_ACCEPT_ELIGIBLE_OPERATIONS.get(operation_key)
    if not arg_name:
        return None
    value = ctx.args.get(arg_name)
    return str(value) if value else None


def _attendee_email(attendee: Any) -> str:
    """Extract an email address from an attendee, whichever shape it's in.

    calendar_get_event_details passes real Attendee dicts/objects with an
    "email" field; calendar_create_event/update_event pass plain email
    strings (parsed from a comma-separated arg) since the event doesn't
    exist yet.
    """
    if isinstance(attendee, dict):
        return attendee.get("email", "") or ""
    if isinstance(attendee, str):
        return attendee
    return getattr(attendee, "email", "") or ""



# ── Per-principal state ──────────────────────────────────────────────────────
#
# One PrincipalRegistry entry per principal (local mode has exactly one; org mode one per signed-in
# user) -- config path, write lock, the temp-accept grace window, the hot-reloaded v2 rule cache, and
# the rules-changed listener broadcast that lets a pending approval re-resolve the moment a rule
# changes. Every accessor function below is what gate.py/settings_controller.py/daemon_main.py
# actually call; nothing outside this module reaches into _AutoAcceptState directly.


class _AutoAcceptState:
    def __init__(self) -> None:
        self.config_path: str | None = None
        self.write_lock = threading.Lock()
        # (operation_key, file_key) -> monotonic expiry. In-memory only, by design: it lives and
        # dies with this principal's registry entry (i.e. with the daemon process), unlike the
        # YAML-backed rules below.
        self.temp_accepts: dict[tuple[str, str], float] = {}
        self.temp_accepts_lock = threading.Lock()
        # A list, not a single slot -- more than one subscriber exists (the menu bar's own refresh,
        # via set_rules_changed_listener below, and approvals.PendingApprovalRegistry's own
        # re-evaluation broadcast, via add_rules_changed_listener), and one must not clobber what
        # the other already registered.
        self.rules_changed_listeners: list[Callable[[], None]] = []
        self.rules_changed_listener: Callable[[], None] | None = None  # see
        # set_rules_changed_listener's docstring
        # Rules that exist in the on-disk v2 `auto_accept:` section -- added through the
        # Auto-accept Settings page, the MCP bridge, the popup's own "Always allow" flow, or by
        # hand -- refreshed by set_policy_v2_store_rules() below. This is the only rule source
        # gate.py evaluates against.
        self.policy_v2_store_rules: list["PolicyRule"] = []


_REGISTRY: PrincipalRegistry[_AutoAcceptState] = PrincipalRegistry(_AutoAcceptState)


def init_config_path(path: str) -> None:
    """Register the on-disk config path so add_policy_v2_rules()/remove_policy_v2_rule() can
    persist."""
    _REGISTRY.get().config_path = path


def register_temp_accept(
    operation_key: str, file_key: str, ttl_seconds: float = TEMP_ACCEPT_TTL_SECONDS
) -> None:
    """Grant a temporary, in-memory auto-accept for one file, for ``ttl_seconds``."""
    state = _REGISTRY.get()
    with state.temp_accepts_lock:
        state.temp_accepts[(operation_key, file_key)] = time.monotonic() + ttl_seconds


def is_temp_accepted(operation_key: str, file_key: str | None) -> bool:
    """Whether ``operation_key``/``file_key`` currently has an active grace-window acceptance --
    the callback ``policy.engine.evaluate``/``preflight`` take as ``is_temp_accepted``, and what
    gate.py itself checks directly for a real call."""
    if file_key is None:
        return False
    state = _REGISTRY.get()
    key = (operation_key, file_key)
    with state.temp_accepts_lock:
        expiry = state.temp_accepts.get(key)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            del state.temp_accepts[key]
            return False
        return True


def set_policy_v2_store_rules(rules: "list[PolicyRule]") -> None:
    """Hot-reload the current principal's rule set -- called by settings_controller.py after any
    Auto-accept Settings page mutation, by this module's own add_policy_v2_rules/
    remove_policy_v2_rule after a bridge or popup write, and by daemon_main.py whenever it loads a
    principal's settings (at startup, and for each organization principal)."""
    _REGISTRY.get().policy_v2_store_rules = list(rules)


def get_policy_v2_store_rules() -> "list[PolicyRule]":
    return _REGISTRY.get().policy_v2_store_rules


def _read_rows_locked(state: Any) -> "tuple[dict[str, Any], list[ScopeRow]] | None":
    """The config and its rows, read under ``state.write_lock`` (the caller holds it), after the
    token-aware upgrade (``policy_upgrade.ensure_scope_rows``). ``None`` when the upgrade had to
    leave the section at version 2, in which case a writer must write nothing.

    Imports locally: ``scope_rows`` imports ``catalogue``, which imports ``store``, whose
    ``engine`` imports this module, and ``policy_upgrade`` imports ``scoped_tokens``, which
    imports this module."""
    from . import policy_upgrade
    from .policy import scope_rows

    with open(state.config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not policy_upgrade.ensure_scope_rows(cfg, state.config_path, current_principal()):
        return None
    return cfg, scope_rows.rows_from_config(cfg)


def _write_rows_locked(state: Any, cfg: dict[str, Any], rows: "list[ScopeRow]") -> None:
    """Persist ``rows`` as the ``auto_accept:`` section and hot-reload the store (lock held)."""
    from .policy import scope_rows
    from .policy import store as policy_store

    cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = scope_rows.rows_to_config(rows)
    atomic_write_text(state.config_path, yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True))
    set_policy_v2_store_rules(scope_rows.compile_rows(rows))


def get_policy_rows() -> "list[ScopeRow]":
    """Read-only snapshot of the on-disk ``auto_accept:`` section as one row per scope, straight
    from disk -- backs ``privacyfence_list_policy``, which needs each row's real, stable id to
    give a model something ``privacyfence_propose_policy_change``'s ``update``/``remove`` can
    target. Unlike ``get_policy_v2_store_rules()`` above, which is the in-memory hot-reloaded
    cache ``gate.py`` evaluates against, this always re-reads the file, and it never writes: a
    version-2 section is converted in memory only.
    """
    from .policy import scope_rows

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        with open(state.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    return scope_rows.rows_from_config(cfg)


def get_policy_v2_rules() -> "list[PolicyRule]":
    """The on-disk rows compiled to engine rules (``get_policy_rows()``, compiled)."""
    from .policy import scope_rows

    return scope_rows.compile_rows(get_policy_rows())


def policy_row_ids_for(rules: "list[PolicyRule]") -> list[str]:
    """The ids of the rows ``add_policy_v2_rules(rules)`` would extend or create, computed against
    the current rows without writing."""
    from .policy import scope_rows

    return scope_rows.add_rules(get_policy_rows(), list(rules))[1]


def add_policy_v2_rules(rules: "list[PolicyRule]") -> bool:
    """Merge ``rules`` into the on-disk ``auto_accept:`` section's rows and hot-reload the store,
    notifying every rules-changed listener -- the one writer every surface (Settings, the MCP
    bridge, the popup's own "Always allow" flow) shares. Returns whether the on-disk rows
    actually changed -- merging a rule that adds no new scope and no new operation to an existing
    row is a no-op.
    """
    from .policy import scope_rows

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        loaded = _read_rows_locked(state)
        if loaded is None:
            return False
        cfg, existing = loaded
        merged, _ = scope_rows.add_rules(existing, list(rules))
        if merged == existing:
            return False
        _write_rows_locked(state, cfg, merged)
    notify_rules_changed()
    return True


def remove_policy_v2_rule(rule_id: str) -> bool:
    """Remove the row with this stable id from the on-disk ``auto_accept:`` section entirely.
    Returns whether anything was actually removed."""
    from .policy import scope_rows

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        loaded = _read_rows_locked(state)
        if loaded is None:
            return False
        cfg, existing = loaded
        remaining = scope_rows.remove_row(existing, rule_id)
        if len(remaining) == len(existing):
            return False
        _write_rows_locked(state, cfg, remaining)
    notify_rules_changed()
    return True


def update_policy_v2_rule(rule_id: str) -> frozenset[str]:
    """Add to the row with this stable id the operations its own verbs imply today and it lacks
    (``scope_rows.fill_missing``, ADR 0166), and return the keys added -- empty when the id is
    unknown or nothing is missing, in which case nothing is written. The id, value and conditions
    are kept; what is added is computed here, never taken from a caller."""
    from .policy import scope_rows

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        loaded = _read_rows_locked(state)
        if loaded is None:
            return frozenset()
        cfg, existing = loaded
        target = next((row for row in existing if row.id == rule_id), None)
        missing = scope_rows.missing_operations(target) if target is not None else frozenset()
        if target is None or not missing:
            return frozenset()
        _write_rows_locked(state, cfg, scope_rows.fill_missing(existing, rule_id))
    notify_rules_changed()
    return missing


def set_policy_rule_verbs(rule_id: str, verbs: "list[Verb]") -> bool:
    """Change which verbs the row with this stable id allows (``scope_rows.set_verbs``): a kept verb
    keeps the operations it had, an added verb gets all of its tools, a removed verb's operations
    go. Returns whether anything was written -- ``False`` when ``set_verbs`` refuses (unknown or
    custom row, no verbs, a verb outside the scope) or the result equals the current rows."""
    from .policy import scope_rows

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        loaded = _read_rows_locked(state)
        if loaded is None:
            return False
        cfg, existing = loaded
        updated = scope_rows.set_verbs(existing, rule_id, verbs)
        if updated is None or updated == existing:
            return False
        _write_rows_locked(state, cfg, updated)
    notify_rules_changed()
    return True


def set_rules_changed_listener(callback: Callable[[], None] | None) -> None:
    """Register (or, with ``None``, clear) *the* single-slot listener -- kept for the one caller
    that predates the multi-listener support below (settings_controller.py's own menu-bar refresh)
    and for tests that patch it directly. Implemented as "replace whatever this call previously
    added to add_rules_changed_listener's list", so calling this twice doesn't leave two menu-bar
    callbacks both firing.
    """
    state = _REGISTRY.get()
    if state.rules_changed_listener is not None:
        remove_rules_changed_listener(state.rules_changed_listener)
    state.rules_changed_listener = callback
    if callback is not None:
        add_rules_changed_listener(callback)


def add_rules_changed_listener(callback: Callable[[], None]) -> None:
    """Register an additional callback fired whenever the live rule set changes -- unlike
    set_rules_changed_listener, doesn't replace any listener already registered this way.
    approvals.py's rules-changed re-evaluation broadcast is one such subscriber; more than one can
    coexist (e.g. the menu bar and the web approval registry, both active in the same process)."""
    listeners = _REGISTRY.get().rules_changed_listeners
    if callback not in listeners:
        listeners.append(callback)


def remove_rules_changed_listener(callback: Callable[[], None]) -> None:
    listeners = _REGISTRY.get().rules_changed_listeners
    if callback in listeners:
        listeners.remove(callback)


def notify_rules_changed() -> None:
    """Fire every rules-changed listener for the current principal -- e.g. so an already-pending
    approval that a rule change now covers is resolved immediately (approvals.PendingApprovalRegistry.
    reevaluate_all) and so a live Settings tab gets a fresh snapshot pushed. Called by this module's
    own add_policy_v2_rules/remove_policy_v2_rule after a bridge/popup write, and by
    settings_controller.py after a Settings page mutation."""
    state = _REGISTRY.get()
    for listener in list(state.rules_changed_listeners):
        try:
            listener()
        except Exception:
            logger.exception("Rules-changed listener raised")


__all__ = [
    "PERSONAL_DATA_READ_TOOLS",
    "PERSONAL_DATA_RULE_TOOLS",
    "UPLOAD_PII_SCAN_TOOLS",
    "ReviewContext",
    "TEMP_ACCEPT_ELIGIBLE_OPERATIONS",
    "TEMP_ACCEPT_TTL_SECONDS",
    "TOOL_TO_GATE",
    "TOOL_TO_OPERATION",
    "add_policy_v2_rules",
    "add_rules_changed_listener",
    "get_policy_v2_rules",
    "get_policy_v2_store_rules",
    "init_config_path",
    "is_temp_accepted",
    "notify_rules_changed",
    "register_temp_accept",
    "remove_policy_v2_rule",
    "remove_rules_changed_listener",
    "set_policy_v2_store_rules",
    "set_rules_changed_listener",
    "temp_accept_key",
]
