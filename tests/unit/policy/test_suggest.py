"""Tests for privacyfence.policy.suggest.

``suggested_rules`` names the catalogue rules that would cover a call, from its arguments alone.
"""
from __future__ import annotations

import dataclasses

import pytest

from privacyfence.policy import catalogue, propose, scopes, suggest
from privacyfence.policy.registry import TOOL_REGISTRY, Verb
from privacyfence.policy.suggest import suggested_rules

pytestmark = pytest.mark.unit


def _groups(suggestions):
    return [s["group"] for s in suggestions]


class TestArgumentDerivedSuggestions:
    def test_salesforce_report_is_named_by_its_id(self):
        entry = TOOL_REGISTRY["salesforce_export_report"]
        [suggestion] = suggested_rules("salesforce_export_report", {"report_id": "00O1"})
        assert suggestion["group"] == "salesforce.report"
        assert suggestion["value"] == ["00O1"]
        assert suggestion["value_known"] is True
        assert suggestion["verb"] == entry.verb.value == "read"
        assert suggestion["operation"] == entry.operation
        assert suggestion["label"]

    def test_grist_document_is_named_by_its_doc_id(self):
        [suggestion] = suggested_rules("grist_update_csv", {"doc_id": "docA"})
        assert suggestion["group"] == "grist.document"
        assert suggestion["value"] == ["docA"]
        assert suggestion["verb"] == TOOL_REGISTRY["grist_update_csv"].verb.value == "update"

    def test_peek_page_is_named_by_its_slug(self):
        [suggestion] = suggested_rules("peek_upload_page", {"slug": "pipeline"})
        assert suggestion["group"] == "peek.page"
        assert suggestion["value"] == ["pipeline"]

    def test_peek_page_without_a_slug_is_not_suggested(self):
        assert "peek.page" not in _groups(suggested_rules("peek_upload_page", {}))

    def test_apps_script_project_is_named_by_its_script_id(self):
        tool = next(
            name for name, entry in TOOL_REGISTRY.items()
            if entry.operation and entry.operation.startswith("apps_script.") and entry.verb == Verb.READ
        )
        [suggestion] = suggested_rules(tool, {"script_id": "S1"})
        assert suggestion["group"] == "apps_script.project"
        assert suggestion["value"] == ["S1"]


class TestRunTimeResources:
    def test_a_fetched_item_scope_is_suggested_with_no_value(self):
        suggestions = suggested_rules("drive_download_file", {"file_id": "F1"})
        folder = [s for s in suggestions if s["group"] == "drive.folder"]
        assert folder
        assert all(s["value"] is None and s["value_known"] is False for s in folder)
        assert folder[0]["verb"] == "download"


class TestNeverSuggested:
    def test_unknown_tool_has_no_suggestions(self):
        assert suggested_rules("no_such_tool", {}) == []

    def test_ungoverned_tool_has_no_suggestions(self, monkeypatch):
        entry = dataclasses.replace(TOOL_REGISTRY["salesforce_export_report"], operation=None)
        monkeypatch.setitem(TOOL_REGISTRY, "salesforce_export_report", entry)
        assert suggested_rules("salesforce_export_report", {"report_id": "00O1"}) == []

    def test_unconditional_extra_scope_is_not_suggested(self):
        assert suggested_rules("gmail_create_filter", {}) == []

    def test_every_valueless_extra_scope_is_decided(self):
        undecided = [
            group for group, extra in catalogue.EXTRA_SCOPES.items()
            if not extra.needs_value
            and group not in suggest._VALUELESS_EXTRAS
            and extra.predicate not in {"gmail.anything", "slack.anything"}
        ]
        assert undecided == []

    def test_unwidenable_always_allow_scope_is_not_suggested(self):
        assert "gmail.anything" not in _groups(suggested_rules("gmail_create_draft", {"to": "a@b.c"}))


class TestFailClosed:
    def test_a_selector_that_raises_is_skipped(self, monkeypatch):
        def boom(_value, _ctx):
            raise RuntimeError("selector broke")

        selector = dataclasses.replace(scopes.SCOPE_SELECTORS["approved_report_ids"], matches=boom)
        monkeypatch.setitem(scopes.SCOPE_SELECTORS, "approved_report_ids", selector)
        assert suggested_rules("salesforce_export_report", {"report_id": "00O1"}) == []

    def test_a_value_builder_that_raises_is_skipped(self, monkeypatch):
        def boom(_ctx):
            raise RuntimeError("builder broke")

        scope = next(s for s in propose.PROPOSABLE_SCOPES if s.predicate == "approved_report_ids")
        monkeypatch.setattr(propose, "PROPOSABLE_SCOPES", (dataclasses.replace(scope, value_of=boom),))
        assert suggested_rules("salesforce_export_report", {"report_id": "00O1"}) == []

    def test_a_predicate_in_neither_selector_table_is_skipped(self, monkeypatch):
        scope = next(s for s in propose.PROPOSABLE_SCOPES if s.predicate == "approved_report_ids")
        monkeypatch.setattr(
            propose, "PROPOSABLE_SCOPES", (dataclasses.replace(scope, predicate="no_such_predicate"),),
        )
        assert suggested_rules("salesforce_export_report", {"report_id": "00O1"}) == []

    def test_a_value_the_selector_rejects_is_skipped(self, monkeypatch):
        selector = dataclasses.replace(scopes.SCOPE_SELECTORS["approved_report_ids"], matches=lambda v, c: False)
        monkeypatch.setitem(scopes.SCOPE_SELECTORS, "approved_report_ids", selector)
        assert suggested_rules("salesforce_export_report", {"report_id": "00O1"}) == []


    def test_a_valueless_scope_whose_predicate_is_missing_is_skipped(self, monkeypatch):
        monkeypatch.delitem(scopes.NEW_SCOPE_SELECTORS, "slack.my_conversations")
        groups = _groups(suggested_rules("slack_get_channel_history", {"channel_id": "C1"}))
        assert "slack.my_conversations" not in groups

    def test_a_valueless_applicability_check_that_raises_is_skipped(self, monkeypatch):
        def boom(_tool, _args):
            raise RuntimeError("applicability broke")

        monkeypatch.setitem(suggest._VALUELESS_EXTRAS, "slack.my_conversations", boom)
        groups = _groups(suggested_rules("slack_get_channel_history", {"channel_id": "C1"}))
        assert "slack.my_conversations" not in groups


class TestSlackValuelessSuggestions:
    @staticmethod
    def _shape(tool, args):
        return [
            (s["group"], s["verb"], s["value"], s["value_known"], s.get("requires_args", "absent"))
            for s in suggested_rules(tool, args)
        ]

    def test_search(self):
        assert self._shape("slack_search_messages", {"query": "x"}) == [
            ("slack.channel", "search", None, False, "absent"),
            ("slack.mentions_of_me", "search", [], True, "absent"),
            ("slack.my_conversations", "search", [], True, {"member_only": True}),
        ]

    def test_search_member_only(self):
        assert self._shape("slack_search_messages", {"query": "x", "member_only": True}) == [
            ("slack.channel", "search", None, False, "absent"),
            ("slack.mentions_of_me", "search", [], True, "absent"),
            ("slack.my_conversations", "search", [], True, "absent"),
        ]

    def test_channel_history(self):
        assert self._shape("slack_get_channel_history", {"channel_id": "C1"}) == [
            ("slack.channel", "read", ["C1"], True, "absent"),
            ("slack.my_conversations", "read", [], True, "absent"),
        ]

    def test_channel_history_of_a_direct_message(self):
        assert self._shape("slack_get_channel_history", {"channel_id": "D1"}) == [
            ("slack.channel", "read", ["D1"], True, "absent"),
            ("slack.direct_messages_to_me", "read", [], True, "absent"),
            ("slack.my_conversations", "read", [], True, "absent"),
        ]

    def test_thread_replies_in_a_group(self):
        assert self._shape("slack_get_thread_replies", {"channel_id": "G1", "thread_ts": "1.2"}) == [
            ("slack.channel", "read", ["G1"], True, "absent"),
            ("slack.mentions_of_me", "read", [], True, "absent"),
            ("slack.my_conversations", "read", [], True, "absent"),
        ]

    def test_thread_replies_in_a_direct_message(self):
        assert _groups(suggested_rules("slack_get_thread_replies", {"channel_id": "D1", "thread_ts": "1.2"})) == [
            "slack.channel", "slack.mentions_of_me", "slack.direct_messages_to_me", "slack.my_conversations",
        ]

    def test_no_channel_gets_no_suggestion(self):
        assert suggested_rules("slack_get_channel_history", {}) == []

    def test_an_unconditional_slack_scope_is_not_suggested(self):
        assert suggested_rules("slack_create_group_chat", {"participants": "U1,U2"}) == []


class TestGmailArchiveSuggestion:
    def test_archive_suggests_a_label_rule_with_value_known_at_run_time(self):
        suggestions = suggested_rules("gmail_archive_message", {"message_id": "m1"})
        [suggestion] = [s for s in suggestions if s["group"] == "gmail.label"]
        assert suggestion["verb"] == "move"
        assert suggestion["value"] is None
        assert suggestion["value_known"] is False


class TestGmailQuerySuggestion:
    def test_an_uncovered_gmail_read_suggests_the_query_scope_with_no_known_value(self):
        suggestions = suggested_rules("gmail_get_message", {"message_id": "m1"})
        [suggestion] = [s for s in suggestions if s["group"] == "gmail.query"]
        assert suggestion["verb"] == "read"
        assert suggestion["value"] is None
        assert suggestion["value_known"] is False

    def test_a_gmail_read_no_longer_raises_for_the_query_scope(self):
        for tool in ("gmail_get_message", "gmail_get_thread", "gmail_download_attachment"):
            assert any(s["group"] == "gmail.query" for s in suggested_rules(tool, {}))
