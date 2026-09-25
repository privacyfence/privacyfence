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
  ``_attendee_email``) P2's scope/condition selectors (``policy/scopes.py``, ``policy/conditions.py``)
  and P5's proposal builder (``policy/propose.py``) import directly.
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

from .principal import PrincipalRegistry
from .policy.resource_registry import DRIVE_SANDBOX_WRITE_TARGETS
from .secure_files import atomic_write_text

if TYPE_CHECKING:
    # Only for the type hint on `_AutoAcceptState.policy_v2_store_rules`/`set_policy_v2_store_rules`
    # below -- a real (non-TYPE_CHECKING) import would be circular: `policy.engine` itself imports
    # `ReviewContext`/`temp_accept_key` from this module.
    from .policy.engine import PolicyRule

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
    "drive_download_file":           "drive.download_file",
    "drive_write_file_content":       "drive.write_file",
    "drive_write_doc_content":        "drive.write_doc",
    "drive_upload_file":              "drive.upload_file",
    "drive_move_file":                "drive.move_file",
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
    "salesforce_search":              "salesforce.search",
    "contacts_update":                "contacts.edit",
    "contacts_create":                "contacts.create",
    "contacts_add_label":             "contacts.add_label",
    "contacts_remove_label":          "contacts.remove_label",
    "jira_get_issue":                 "jira.read_issue",
    "jira_create_issue":              "jira.create_issue",
    "jira_add_comment":               "jira.add_comment",
    "jira_update_issue":              "jira.update_issue",
    "jira_transition_issue":          "jira.transition_issue",
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
# This is the single source of truth the connector tables in
# 5deef1d8:docs/TECHNICAL_REFERENCE.md are checked against (see
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
    "drive_list_folder":               "auto",
    "drive_list_shared_drives":        "auto",
    "drive_create_blank_file":         "auto",
    "drive_get_file_content":          "review",
    "drive_download_file":             "review",
    "drive_write_file_content":        "popup",
    "drive_upload_file":               "popup",
    "drive_write_doc_content":         "popup",
    "drive_move_file":                 "popup",
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
    "salesforce_search":               "review",
    # Jira
    "jira_list_projects":              "auto",
    "jira_search_issues":              "auto",
    "jira_get_transitions":            "auto",
    "jira_get_issue":                  "review",
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
}

@dataclass
class ReviewContext:
    connector: str
    tool: str
    args: dict
    raw_data: Any
    my_email: str = ""
    my_domain: str = field(init=False)
    session_created_ids: set = field(default_factory=set)

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
    email_part = sender
    if "<" in sender and ">" in sender:
        email_part = sender[sender.index("<") + 1 : sender.index(">")]
    return email_part.split("@", 1)[-1].lower().strip()


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


def get_policy_v2_rules() -> "list[PolicyRule]":
    """Read-only snapshot of the on-disk v2 ``auto_accept:`` section, straight from disk -- backs
    ``privacyfence_list_policy``, which needs each rule's real, stable id
    (``policy.store.rule_id_for``) to give a model something ``privacyfence_propose_policy_change``'s
    ``update``/``remove`` can actually target -- unlike ``get_policy_v2_store_rules()`` above, which
    is the in-memory hot-reloaded cache ``gate.py`` evaluates against, this always re-reads the file.

    Imports ``policy.store`` locally rather than at module level: ``policy.engine`` (imported by
    ``policy.store``) itself imports ``ReviewContext``/``temp_accept_key`` from this module, so a
    module-level import here would be circular.
    """
    from .policy import store as policy_store

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        with open(state.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    return policy_store.compile_rules_from_config(cfg)


def add_policy_v2_rules(rules: "list[PolicyRule]") -> bool:
    """Merge ``rules`` into the on-disk v2 ``auto_accept:`` section and hot-reload the store,
    notifying every rules-changed listener -- the one writer every surface (Settings, the MCP
    bridge, the popup's own "Always allow" flow) now shares. Returns whether the on-disk rule set
    actually changed -- merging a rule that adds no new ``(predicate, value, conditions)`` row and
    no new operation to an existing one is a no-op.
    """
    from .policy import store as policy_store

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        with open(state.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        existing = policy_store.compile_rules_from_config(cfg)
        merged = policy_store.merge_rules(existing + list(rules))
        if merged == existing:
            return False
        cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = policy_store.rules_to_config(merged)
        atomic_write_text(state.config_path, yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True))
        set_policy_v2_store_rules(merged)
    notify_rules_changed()
    return True


def remove_policy_v2_rule(rule_id: str) -> bool:
    """Remove the rule with this stable id from the on-disk v2 ``auto_accept:`` section entirely.
    Returns whether anything was actually removed."""
    from .policy import store as policy_store

    state = _REGISTRY.get()
    if state.config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with state.write_lock:
        with open(state.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        existing = policy_store.compile_rules_from_config(cfg)
        remaining = [rule for rule in existing if rule.id != rule_id]
        if len(remaining) == len(existing):
            return False
        cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = policy_store.rules_to_config(remaining)
        atomic_write_text(state.config_path, yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True))
        set_policy_v2_store_rules(remaining)
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
