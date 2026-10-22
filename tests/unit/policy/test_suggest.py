"""Tests for privacyfence.policy.suggest.

``suggested_rules`` names the catalogue rules that would cover a call, from its arguments alone.
"""
from __future__ import annotations

import dataclasses

import pytest

from privacyfence.policy import propose, scopes
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
