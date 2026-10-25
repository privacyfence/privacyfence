"""Tests for privacyfence.policy.value_format."""
from __future__ import annotations

import pytest

from privacyfence import assets_client, grist_client, peek_client
from privacyfence.policy import catalogue, propose
from privacyfence.policy.value_format import (
    UNVALIDATED_VALUE_PREDICATES,
    VALUE_FORMATS,
    RuleValueError,
    normalise_rule_value,
)

_FOLDER_URL = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

VALID = [
    ("approved_folder", "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"),
    ("approved_folder", "root"),
    ("approved_sandbox_folder", "folder1"),
    ("parent_folder_allowlist", "folder_1-x"),
    ("move_within_approved_folders", "F1"),
    ("approved_channel", "C0123456789"),
    ("approved_channel_all_results", "G0123"),
    ("slack.my_conversations", "C0123456789"),
    ("slack.my_conversations_all_results", "G0123456789"),
    ("approved_recipient", "U0123ABCD"),
    ("approved_recipient", "W0123ABCD"),
    ("approved_chats", "-100987654321"),
    ("approved_chats_all_results", "42"),
    ("approved_project_keys", "MYPROJ"),
    ("approved_project_keys_all_results", "My_Proj2"),
    ("approved_schema_ids", "12"),
    ("approved_schema_ids_all_results", "7"),
    ("approved_space_keys", "TEAM"),
    ("approved_space_keys", "~alice.b"),
    ("personal_calendar", "primary"),
    ("personal_calendar", "team@group.calendar.google.com"),
    ("approved_report_ids", "00O000000000001"),
    ("approved_report_ids", "00O000000000001AAA"),
    ("approved_object_types", "Account"),
    ("approved_object_types", "MyThing__c"),
    ("approved_task_list", "@default"),
    ("approved_task_list", "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow"),
    ("approved_task_list", "abc=="),
    ("apps_script.project", "1A2b3C4d5E6f7G8h"),
    ("grist.document", "8CAN8gKdxY7zq3FvPXbHdM"),
    ("peek.page", "team-dashboard"),
    ("trusted_sender_domain", "example.com"),
    ("approved_recipient_domain", "mail.example.co.uk"),
]

INVALID = [
    ("approved_folder", "Q3 Reports"),
    ("approved_folder", _FOLDER_URL),
    ("approved_sandbox_folder", "My Folder"),
    ("parent_folder_allowlist", "a/b"),
    ("move_within_approved_folders", "Q3 plans"),
    ("approved_channel", "#general"),
    ("approved_channel", "general"),
    ("approved_channel_all_results", "c0123"),
    ("slack.my_conversations", "#hr"),
    ("slack.my_conversations_all_results", "#hr"),
    ("approved_recipient", "alice"),
    ("approved_chats", "My Chat"),
    ("approved_chats_all_results", "@somebody"),
    ("approved_project_keys", "My Project"),
    ("approved_project_keys_all_results", "1ABC"),
    ("approved_schema_ids", "Inventory"),
    ("approved_schema_ids_all_results", "-3"),
    ("approved_space_keys", "Team Space"),
    ("approved_space_keys", "https://x.atlassian.net/wiki/spaces/TEAM"),
    ("personal_calendar", "Work calendar"),
    ("approved_report_ids", "Quarterly pipeline"),
    ("approved_report_ids", "00X000000000001"),
    ("approved_object_types", "My Object"),
    ("approved_task_list", "My tasks"),
    ("apps_script.project", "My script"),
    ("grist.document", "Budget 2026"),
    ("grist.document", "x" * 129),
    ("peek.page", "Team Dashboard"),
    ("trusted_sender_domain", "alice@example.com"),
    ("trusted_sender_domain", "https://example.com"),
    ("approved_recipient_domain", "example"),
]


@pytest.mark.parametrize(("predicate", "value"), VALID)
def test_valid_value_is_accepted(predicate, value):
    assert normalise_rule_value([predicate], value) == [value]


@pytest.mark.parametrize(("predicate", "value"), INVALID)
def test_title_name_or_url_is_refused_with_guidance(predicate, value):
    fmt = VALUE_FORMATS[predicate]
    with pytest.raises(RuleValueError) as excinfo:
        normalise_rule_value([predicate], value)
    message = str(excinfo.value)
    assert f"is not {fmt.what}" in message
    assert fmt.how in message
    assert fmt.example in message


@pytest.mark.parametrize("predicate", sorted(VALUE_FORMATS))
def test_every_format_accepts_its_own_example(predicate):
    assert normalise_rule_value([predicate], VALUE_FORMATS[predicate].example)


def test_client_regexes_are_reused():
    assert VALUE_FORMATS["grist.document"].pattern is grist_client.DOC_ID_RE
    assert VALUE_FORMATS["peek.page"].pattern is peek_client._SLUG_RE
    assert VALUE_FORMATS["approved_schema_ids"].pattern is assets_client._ID_RE


def test_no_value_is_none():
    assert normalise_rule_value(["approved_folder"], None) is None
    assert normalise_rule_value(["approved_folder"], []) is None


def test_int_is_accepted_for_telegram():
    assert normalise_rule_value(["approved_chats"], -100123) == ["-100123"]
    assert normalise_rule_value(["approved_chats"], [1, "2"]) == ["1", "2"]


@pytest.mark.parametrize("value", [True, {"a": 1}, 1.5, [True], [["x"]], [None]])
def test_unsupported_types_are_refused(value):
    with pytest.raises(RuleValueError, match="text id or a list of ids"):
        normalise_rule_value(["approved_folder"], value)


@pytest.mark.parametrize("value", [["", "x"], "   ", ["x", " "]])
def test_empty_entry_is_refused(value):
    with pytest.raises(RuleValueError, match="empty entry"):
        normalise_rule_value(["approved_folder"], value)


def test_items_are_stripped_and_deduplicated_keeping_order():
    assert normalise_rule_value(["approved_folder"], [" b ", "a", "b", "a"]) == ["b", "a"]


def test_long_value_is_cut_to_sixty_characters_in_the_message():
    with pytest.raises(RuleValueError) as excinfo:
        normalise_rule_value(["approved_channel"], "x" * 100)
    assert repr("x" * 60 + "…") in str(excinfo.value)
    assert "x" * 61 not in str(excinfo.value)


def test_every_predicate_of_a_group_is_checked():
    with pytest.raises(RuleValueError, match="Slack ID"):
        normalise_rule_value(["approved_folder", "approved_channel"], "folder1")


def test_unvalidated_predicates_accept_any_text():
    assert normalise_rule_value(sorted(UNVALIDATED_VALUE_PREDICATES), "Any Name!") == ["Any Name!"]


def _value_needing_predicates() -> set[str]:
    found = {
        scope.predicate
        for scopes in propose.SCOPES_BY_GROUP.values()
        for scope in scopes
        if propose.scope_needs_value(scope)
    }
    found |= {extra.predicate for extra in catalogue.EXTRA_SCOPES.values() if extra.needs_value}
    return found


def test_every_value_needing_predicate_has_a_format_or_is_listed_as_unvalidated():
    unknown = _value_needing_predicates() - set(VALUE_FORMATS) - UNVALIDATED_VALUE_PREDICATES
    assert not unknown


def test_validated_and_unvalidated_predicates_are_disjoint():
    assert not set(VALUE_FORMATS) & UNVALIDATED_VALUE_PREDICATES


class TestGmailQueryValue:
    def test_accepts_a_normal_query_with_commas_and_quotes(self):
        query = 'in:inbox -category:promotions -label:"Proposed SPAM", (from:a OR from:b)'
        assert normalise_rule_value(["gmail.query"], query) == [query]

    def test_refuses_control_characters(self):
        with pytest.raises(RuleValueError):
            normalise_rule_value(["gmail.query"], "in:inbox\x07")
        with pytest.raises(RuleValueError):
            normalise_rule_value(["gmail.query"], "in:inbox\nOR in:anywhere")

    def test_refuses_a_query_over_500_characters(self):
        assert normalise_rule_value(["gmail.query"], "a" * 500)
        with pytest.raises(RuleValueError):
            normalise_rule_value(["gmail.query"], "a" * 501)

    @pytest.mark.parametrize("query", ["in:inbox) OR (in:anywhere", "(in:inbox", "in:inbox)", "((a)"])
    def test_refuses_unbalanced_parentheses(self, query):
        with pytest.raises(RuleValueError, match="unbalanced parentheses or quotes"):
            normalise_rule_value(["gmail.query"], query)

    def test_refuses_an_odd_number_of_quotes(self):
        with pytest.raises(RuleValueError, match="unbalanced parentheses or quotes"):
            normalise_rule_value(["gmail.query"], 'label:"Proposed SPAM')
