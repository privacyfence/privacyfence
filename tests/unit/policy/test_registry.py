"""Unit tests for privacyfence.policy.registry.

The registry's whole job is to be a *superset* of the existing tables it joins -- these tests
assert exact agreement with `TOOL_TO_GATE`/`TOOL_TO_OPERATION` (so the registry provably changes nothing for
every consumer that still reads those tables directly) and then assert the registry adds a verb
and scope subject everywhere those tables don't reach, including the six operation keys the grant
resource-type manifest does not cover.
"""
from __future__ import annotations

import pytest

from privacyfence.auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION
from privacyfence.policy.resource_registry import GRANT_RESOURCE_TYPES
from privacyfence.policy.registry import (
    GATES,
    LEGACY_VERB_ALIASES,
    TOOL_REGISTRY,
    TOOL_TO_VERB,
    VERB_FAMILY,
    VERB_SCOPE_SUBJECT,
    ScopeSubject,
    Verb,
    VerbFamily,
    operation_verbs,
)

# The thirteen operation keys no grant capability in the resource-type manifest (GRANT_RESOURCE_TYPES)
# reaches. They are governable all the same: policy/catalogue.py's EXTRA_SCOPES (apps_script.project,
# gmail.filters, slack.group_chats, grist.document) configures them from Settings and
# privacyfence_propose_policy_change, and the approval popup never proposes a rule for them (ADR 0077).
_GRANT_MANIFEST_UNREACHABLE_OPERATIONS = frozenset({
    "apps_script.read_content",
    "apps_script.write_content",
    "apps_script.read_execution_log",
    "gmail.create_filter",
    "gmail.update_filter",
    "slack.create_group_chat",
    "grist.read_records",
    "grist.add_records",
    "grist.update_records",
    "grist.create_table",
    "grist.add_columns",
    "grist.import_csv",
    "grist.update_csv",
})

# Operation keys shared by tools that perform two different verbs. Migrating a v1 rule keyed on
# one of these must expand to every verb listed, not just the first, or the migration silently
# narrows what the rule allows.
_DUAL_VERB_OPERATIONS: dict[str, frozenset[Verb]] = {
    "calendar.create_modify_event": frozenset({Verb.ADD, Verb.UPDATE}),
    "slack.read_messages": frozenset({Verb.READ, Verb.SEARCH}),
    "telegram.read_chat_messages": frozenset({Verb.READ, Verb.SEARCH}),
}


def _capability_operation_keys() -> set[str]:
    keys: set[str] = set()
    for resource_type in GRANT_RESOURCE_TYPES:
        for capability in resource_type.capabilities.values():
            keys.update(op_key for op_key, _rule_name in capability.targets)
    return keys


class TestReproducesToolToGate:
    def test_same_tool_names(self):
        assert set(TOOL_REGISTRY) == set(TOOL_TO_GATE)

    @pytest.mark.parametrize("tool", sorted(TOOL_TO_GATE))
    def test_gate_matches_for_every_tool(self, tool):
        assert TOOL_REGISTRY[tool].gate == TOOL_TO_GATE[tool]

    def test_every_gate_value_is_a_known_gate(self):
        assert GATES == frozenset({"auto", "review", "popup"})
        assert {entry.gate for entry in TOOL_REGISTRY.values()} <= GATES


class TestReproducesToolToOperation:
    @pytest.mark.parametrize("tool", sorted(TOOL_TO_OPERATION))
    def test_operation_matches_for_every_mapped_tool(self, tool):
        assert TOOL_REGISTRY[tool].operation == TOOL_TO_OPERATION[tool]

    def test_tools_with_no_operation_key_have_none(self):
        unmapped = set(TOOL_TO_GATE) - set(TOOL_TO_OPERATION)
        assert unmapped, "fixture assumption: some tools have no operation key"
        for tool in unmapped:
            assert TOOL_REGISTRY[tool].operation is None


class TestAutoGatedToolsCarryNoVerb:
    def test_auto_gated_tools_have_no_verb_scope_or_family(self):
        auto_tools = [t for t, gate in TOOL_TO_GATE.items() if gate == "auto"]
        assert auto_tools, "fixture assumption: at least one auto-gated tool exists"
        for tool in auto_tools:
            entry = TOOL_REGISTRY[tool]
            assert entry.operation is None
            assert entry.verb is None
            assert entry.scope_subject is None
            assert entry.verb_family is None


class TestGovernedToolsCarryAVerb:
    @pytest.mark.parametrize("tool", sorted(TOOL_TO_OPERATION))
    def test_every_operation_bearing_tool_has_a_verb_and_scope_subject(self, tool):
        entry = TOOL_REGISTRY[tool]
        assert entry.gate in ("review", "popup")
        assert isinstance(entry.verb, Verb)
        assert isinstance(entry.scope_subject, ScopeSubject)
        assert isinstance(entry.verb_family, VerbFamily)

    def test_tool_to_verb_covers_exactly_the_operation_bearing_tools(self):
        assert set(TOOL_TO_VERB) == set(TOOL_TO_OPERATION)


class TestVerbVocabulary:
    def test_every_verb_has_a_family(self):
        assert set(VERB_FAMILY) == set(Verb)

    def test_every_verb_has_a_scope_subject(self):
        assert set(VERB_SCOPE_SUBJECT) == set(Verb)

    def test_families_partition_the_verbs_by_risk(self):
        assert VERB_FAMILY[Verb.READ] == VerbFamily.READ
        assert VERB_FAMILY[Verb.DOWNLOAD] == VerbFamily.READ
        assert VERB_FAMILY[Verb.SEARCH] == VerbFamily.READ
        assert VERB_FAMILY[Verb.DELETE] == VerbFamily.DESTRUCTIVE
        assert VERB_FAMILY[Verb.SEND] == VerbFamily.SEND
        for verb in (Verb.ADD, Verb.UPDATE, Verb.RESTRUCTURE, Verb.MOVE):
            assert VERB_FAMILY[verb] == VerbFamily.WRITE

    def test_quantified_scope_subjects_match_the_verbs_that_need_more_than_one_object(self):
        # These two are exactly the verbs that measure more than a single item -- getting one
        # of these wrong describes a rule as allowing more (or other) objects than it can match.
        assert VERB_SCOPE_SUBJECT[Verb.SEARCH] == ScopeSubject.EVERY_RESULT
        assert VERB_SCOPE_SUBJECT[Verb.MOVE] == ScopeSubject.SOURCE_AND_DESTINATION
        assert VERB_SCOPE_SUBJECT[Verb.SEND] == ScopeSubject.DESTINATION
        assert VERB_SCOPE_SUBJECT[Verb.ADD] == ScopeSubject.CONTAINER


class TestGrantManifestUnreachableOperationsAreCovered:
    def test_they_are_absent_from_the_grant_resource_type_manifest(self):
        # The registry covers what the manifest does not. If a grant capability starts reaching
        # one of these, this set and its comment are out of date.
        capability_keys = _capability_operation_keys()
        for op_key in _GRANT_MANIFEST_UNREACHABLE_OPERATIONS:
            assert op_key not in capability_keys

    @pytest.mark.parametrize("operation", sorted(_GRANT_MANIFEST_UNREACHABLE_OPERATIONS))
    def test_registry_assigns_a_verb_and_scope_subject_anyway(self, operation):
        verbs = operation_verbs(operation)
        assert verbs, f"{operation} should resolve to at least one verb"
        for verb in verbs:
            assert isinstance(verb, Verb)


class TestOperationVerbs:
    @pytest.mark.parametrize("operation,expected", sorted(_DUAL_VERB_OPERATIONS.items()))
    def test_dual_verb_operations_report_every_verb(self, operation, expected):
        assert operation_verbs(operation) == expected

    def test_single_verb_operation_reports_one_verb(self):
        assert operation_verbs("jira.create_issue") == frozenset({Verb.ADD})

    def test_unknown_operation_reports_no_verbs(self):
        assert operation_verbs("no.such.operation") == frozenset()

    def test_return_type_is_a_frozenset(self):
        assert isinstance(operation_verbs("jira.create_issue"), frozenset)


def test_there_are_nine_verbs():
    assert [v.value for v in Verb] == [
        "read", "download", "search", "add", "update", "restructure", "move", "send", "delete",
    ]
    assert set(ScopeSubject) == {
        ScopeSubject.ITEM, ScopeSubject.CONTAINER, ScopeSubject.EVERY_RESULT,
        ScopeSubject.SOURCE_AND_DESTINATION, ScopeSubject.DESTINATION,
    }
    assert LEGACY_VERB_ALIASES == {
        "create": Verb.ADD, "draft": Verb.ADD,
        "format": Verb.UPDATE, "comment": Verb.UPDATE, "label": Verb.UPDATE,
        "complete": Verb.UPDATE, "transition": Verb.UPDATE,
        "copy": Verb.MOVE, "archive": Verb.MOVE,
        "configure": Verb.RESTRUCTURE, "share": Verb.SEND,
    }


_VERB_MATRIX: dict[str, Verb] = {
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


def test_tool_to_verb_matches_the_verb_matrix():
    assert len(_VERB_MATRIX) == 109
    assert TOOL_TO_VERB == _VERB_MATRIX
