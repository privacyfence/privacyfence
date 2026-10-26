"""The tool registry.

One row per tool, joining `auto_accept.TOOL_TO_GATE` and `auto_accept.TOOL_TO_OPERATION` with the
two columns neither table carries: which *verb* a tool performs, and what kind of object a rule's
scope is measured against when it governs that verb (`ScopeSubject`). Both tables stay the source
of truth for gate and operation key -- this module is provably a superset of them (`test_registry.py`
asserts the join is exact). In particular:

- `gmail.create_filter`, `gmail.update_filter`, `slack.create_group_chat`,
  `apps_script.read_content`, `apps_script.write_content` and `apps_script.read_execution_log`
  are the operation groups the old per-operation rule tables and the grant model had no way to
  configure at all. They get a verb and a scope subject here like every other governed tool, which
  is what makes them configurable: `policy/catalogue.py` offers `apps_script.project` (read),
  `gmail.filters` (restructure), `slack.group_chats` (send), `grist.document` (read, add,
  update, restructure) and `miro.board` (read, add, update), and the catalogue's write-time
  validation keeps a rule naming a verb its scope cannot govern from being stored under any of them.
- Three operation keys are shared by tools that perform two different verbs:
  `calendar.create_modify_event` (add vs. update), `slack.read_messages` and
  `telegram.read_chat_messages` (read vs. search, depending on whether the call returns one
  conversation or a search's worth of results). `operation_verbs()` reports every verb any tool
  registers under a given operation key, so anything that expands a rule keyed on one of these
  expands to *every* verb here -- expanding to only the first would silently narrow what the rule
  allows.

`policy/propose.py` derives which operation keys a (scope, verb) pair reaches from this registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION

GATES: frozenset[str] = frozenset({"auto", "review", "popup"})


class Verb(str, Enum):
    """The nine verbs a rule can allow, grouped by family below."""

    READ = "read"
    DOWNLOAD = "download"
    SEARCH = "search"
    ADD = "add"
    UPDATE = "update"
    RESTRUCTURE = "restructure"
    MOVE = "move"
    SEND = "send"
    DELETE = "delete"


class VerbFamily(str, Enum):
    """A UI grouping, not a permission -- a rule is always stored as its expanded verb list, never
    as "every verb in this family, including ones added later" (see the proposal's "Families are a
    UI affordance, not a permission" note)."""

    READ = "read"
    WRITE = "write"
    SEND = "send"
    DESTRUCTIVE = "destructive"


class ScopeSubject(str, Enum):
    """What object(s) a verb's scope is measured against, and with what quantifier.

    Most verbs measure a single item, but a handful quantify over more than one -- `search`
    requires every result to be in scope, and `move` requires both the source and the
    destination. These are the cases that go wrong when a selector checks only
    one of the objects -- a folder grant approving a move out of the folder because only the source
    was checked, or a channel rule needing a twin predicate to cover a search. This column records
    the quantifier; it is metadata, and each scope selector is what actually enforces it.
    """

    ITEM = "item"
    CONTAINER = "container"
    EVERY_RESULT = "every_result"
    SOURCE_AND_DESTINATION = "source_and_destination"
    DESTINATION = "destination"


VERB_FAMILY: dict[Verb, VerbFamily] = {
    Verb.READ: VerbFamily.READ,
    Verb.DOWNLOAD: VerbFamily.READ,
    Verb.SEARCH: VerbFamily.READ,
    Verb.ADD: VerbFamily.WRITE,
    Verb.UPDATE: VerbFamily.WRITE,
    Verb.RESTRUCTURE: VerbFamily.WRITE,
    Verb.MOVE: VerbFamily.WRITE,
    Verb.SEND: VerbFamily.SEND,
    Verb.DELETE: VerbFamily.DESTRUCTIVE,
}

VERB_SCOPE_SUBJECT: dict[Verb, ScopeSubject] = {
    Verb.READ: ScopeSubject.ITEM,
    Verb.DOWNLOAD: ScopeSubject.ITEM,
    Verb.SEARCH: ScopeSubject.EVERY_RESULT,
    Verb.ADD: ScopeSubject.CONTAINER,
    Verb.UPDATE: ScopeSubject.ITEM,
    Verb.RESTRUCTURE: ScopeSubject.ITEM,
    Verb.MOVE: ScopeSubject.SOURCE_AND_DESTINATION,
    Verb.SEND: ScopeSubject.DESTINATION,
    Verb.DELETE: ScopeSubject.ITEM,
}

# Verb names retired when 19 verbs became nine. `catalogue.parse_verbs` maps them, so bridge calls
# and sync recipes written with the old names keep working.
LEGACY_VERB_ALIASES: dict[str, Verb] = {
    "create": Verb.ADD,
    "draft": Verb.ADD,
    "format": Verb.UPDATE,
    "comment": Verb.UPDATE,
    "label": Verb.UPDATE,
    "complete": Verb.UPDATE,
    "transition": Verb.UPDATE,
    "copy": Verb.MOVE,
    "archive": Verb.MOVE,
    "configure": Verb.RESTRUCTURE,
    "share": Verb.SEND,
}

# Every tool with an operation key (TOOL_TO_OPERATION) gets exactly one verb here -- the verb is a
# property of what the *tool* does, not of the operation key it shares with other tools. Where two
# tools share an operation key but perform different verbs (calendar.create_modify_event,
# slack.read_messages, telegram.read_chat_messages -- see module docstring), each tool still gets
# its own accurate verb; operation_verbs() is what recombines them per operation key.
TOOL_TO_VERB: dict[str, Verb] = {
    "gmail_get_message": Verb.READ,
    "gmail_get_thread": Verb.READ,
    "gmail_download_attachment": Verb.DOWNLOAD,
    "gmail_create_draft": Verb.ADD,
    "gmail_reply_draft": Verb.ADD,
    "gmail_reply_all_draft": Verb.ADD,
    "gmail_create_draft_with_attachments": Verb.ADD,
    "gmail_reply_draft_with_attachments": Verb.ADD,
    "gmail_reply_all_draft_with_attachments": Verb.ADD,
    "gmail_add_label": Verb.UPDATE,
    "gmail_remove_label": Verb.UPDATE,
    "gmail_archive_message": Verb.MOVE,
    "gmail_create_filter": Verb.RESTRUCTURE,
    "gmail_update_filter": Verb.RESTRUCTURE,
    "gmail_create_label": Verb.ADD,
    "drive_get_file_content": Verb.READ,
    "drive_get_revision_content": Verb.READ,
    "drive_download_file": Verb.DOWNLOAD,
    "drive_write_file_content": Verb.UPDATE,
    "drive_write_doc_content": Verb.UPDATE,
    "drive_upload_file": Verb.ADD,
    "drive_move_file": Verb.MOVE,
    "drive_copy_file": Verb.MOVE,
    "drive_add_comment": Verb.UPDATE,
    "drive_sheets_get_values": Verb.READ,
    "drive_sheets_write_range": Verb.UPDATE,
    "drive_sheets_add_sheet": Verb.RESTRUCTURE,
    "drive_sheets_rename_sheet": Verb.RESTRUCTURE,
    "drive_sheets_format_range": Verb.UPDATE,
    "drive_sheets_insert_dimensions": Verb.RESTRUCTURE,
    "drive_sheets_delete_dimensions": Verb.DELETE,
    "drive_docs_edit_content": Verb.UPDATE,
    "drive_docs_format_content": Verb.UPDATE,
    "slack_get_channel_history": Verb.READ,
    "slack_get_thread_replies": Verb.READ,
    "slack_search_messages": Verb.SEARCH,
    "slack_create_group_chat": Verb.SEND,
    "slack_send_message": Verb.SEND,
    "calendar_get_event_details": Verb.READ,
    "calendar_create_event": Verb.ADD,
    "calendar_update_event": Verb.UPDATE,
    "calendar_create_out_of_office": Verb.ADD,
    "calendar_set_working_location": Verb.UPDATE,
    "calendar_set_event_visibility": Verb.UPDATE,
    # calendar.set_color changes one attribute of an existing event, same shape as
    # calendar.working_location, so it gets the same verb.
    "calendar_set_event_color": Verb.UPDATE,
    "calendar_delete_event": Verb.DELETE,
    "salesforce_get_record": Verb.READ,
    "salesforce_run_report": Verb.READ,
    "salesforce_export_report": Verb.READ,
    "salesforce_search": Verb.SEARCH,
    "salesforce_create_record": Verb.ADD,
    "salesforce_update_record": Verb.UPDATE,
    "salesforce_create_records": Verb.ADD,
    "salesforce_update_records": Verb.UPDATE,
    "grist_get_records": Verb.READ,
    "grist_export_records": Verb.READ,
    "grist_export_sql": Verb.READ,
    "grist_add_records": Verb.ADD,
    "grist_update_records": Verb.UPDATE,
    "grist_create_table": Verb.RESTRUCTURE,
    "grist_add_columns": Verb.RESTRUCTURE,
    "grist_import_csv": Verb.ADD,
    "grist_update_csv": Verb.UPDATE,
    "contacts_update": Verb.UPDATE,
    "contacts_create": Verb.ADD,
    "contacts_add_label": Verb.UPDATE,
    "contacts_remove_label": Verb.UPDATE,
    "assets_get_object": Verb.READ,
    "assets_search_objects": Verb.SEARCH,
    "assets_get_related_objects": Verb.SEARCH,
    "assets_get_connected_tickets": Verb.READ,
    "assets_export_objects": Verb.SEARCH,
    "jira_get_issue": Verb.READ,
    "jira_search_issues_with_fields": Verb.SEARCH,
    "jira_export_issues": Verb.SEARCH,
    "jira_create_issue": Verb.ADD,
    "jira_add_comment": Verb.UPDATE,
    "jira_update_issue": Verb.UPDATE,
    "jira_transition_issue": Verb.UPDATE,
    "confluence_get_page": Verb.READ,
    "confluence_get_page_by_title": Verb.READ,
    "confluence_download_attachment": Verb.DOWNLOAD,
    "confluence_create_page": Verb.ADD,
    "confluence_update_page": Verb.UPDATE,
    "telegram_get_messages": Verb.READ,
    "peek_get_comments": Verb.READ,
    "peek_get_stats": Verb.READ,
    "peek_upload_page": Verb.ADD,
    "peek_set_visibility": Verb.UPDATE,
    "peek_add_comment": Verb.UPDATE,
    "peek_rename_page": Verb.UPDATE,
    "peek_delete_page": Verb.DELETE,
    "miro_list_items": Verb.READ,
    "miro_get_items": Verb.READ,
    "miro_list_connectors": Verb.READ,
    "miro_create_items": Verb.ADD,
    "miro_create_connectors": Verb.ADD,
    "miro_update_items": Verb.UPDATE,
    "miro_update_board": Verb.UPDATE,
    "telegram_search_messages": Verb.SEARCH,
    "telegram_send_message": Verb.SEND,
    "tasks_create_task": Verb.ADD,
    "tasks_update_task": Verb.UPDATE,
    "tasks_complete_task": Verb.UPDATE,
    "tasks_uncomplete_task": Verb.UPDATE,
    "tasks_move_task": Verb.MOVE,
    "apps_script_get_content": Verb.READ,
    "apps_script_write_content": Verb.UPDATE,
    "apps_script_get_execution_log": Verb.READ,
}


@dataclass(frozen=True)
class ToolRegistryEntry:
    """One row of the registry: what a tool is gated as, and (if governed) what it does."""

    tool: str
    gate: str
    operation: str | None
    verb: Verb | None
    scope_subject: ScopeSubject | None

    @property
    def verb_family(self) -> VerbFamily | None:
        return VERB_FAMILY[self.verb] if self.verb is not None else None


def _build_registry() -> dict[str, ToolRegistryEntry]:
    entries: dict[str, ToolRegistryEntry] = {}
    for tool, gate in TOOL_TO_GATE.items():
        operation = TOOL_TO_OPERATION.get(tool)
        verb = TOOL_TO_VERB.get(tool)
        subject = VERB_SCOPE_SUBJECT[verb] if verb is not None else None
        entries[tool] = ToolRegistryEntry(
            tool=tool, gate=gate, operation=operation, verb=verb, scope_subject=subject
        )
    return entries


TOOL_REGISTRY: dict[str, ToolRegistryEntry] = _build_registry()


def operation_verbs(operation: str) -> frozenset[Verb]:
    """Every verb any tool performs under `operation` key.

    More than one member means a rule keyed on `operation` must expand to every verb here -- see
    the module docstring's note on `calendar.create_modify_event`,
    `slack.read_messages` and `telegram.read_chat_messages`.
    """
    return frozenset(
        entry.verb
        for entry in TOOL_REGISTRY.values()
        if entry.operation == operation and entry.verb is not None
    )
