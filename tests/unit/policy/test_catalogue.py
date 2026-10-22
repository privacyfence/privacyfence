"""Tests for privacyfence.policy.catalogue.

This module is the Auto-accept Settings page's own scope catalogue
(``_policy_scope_catalogue``/``_POLICY_EXTRA_SCOPES``/``_rules_for_catalogue_entry``, all originally
private to ``settings_controller.py``), promoted here so the bridge writer
(``gate.propose_policy_change``) can share it instead of re-deriving it a second time -- see this
module's own docstring. ``tests/unit/test_settings_controller.py``'s ``TestPolicyScopeCatalogue``/
``TestAddPolicyRule`` already prove the re-exported names behave identically; these tests exercise
the real definitions directly.
"""
from __future__ import annotations

import pytest

from privacyfence.policy import catalogue, propose, store
from privacyfence.policy.registry import Verb
from privacyfence.policy.value_format import RuleValueError


class TestScopeCatalogue:
    def test_covers_every_propose_group_and_every_extra(self):
        ids = {entry["id"] for entry in catalogue.scope_catalogue()}
        assert set(propose.SCOPES_BY_GROUP) <= ids
        assert set(catalogue.EXTRA_SCOPES) <= ids

    def test_apps_script_entry_needs_a_value_and_offers_read_and_update(self):
        entry = next(e for e in catalogue.scope_catalogue() if e["id"] == "apps_script.project")
        assert entry["needs_value"] is True
        assert set(entry["verbs"]) == {"read", "update"}
        assert entry["connector"] == "apps_script"

    def test_grist_document_entry_needs_a_value_and_offers_four_verbs(self):
        entry = next(e for e in catalogue.scope_catalogue() if e["id"] == "grist.document")
        assert entry["needs_value"] is True
        assert set(entry["verbs"]) == {"read", "add", "update", "restructure"}
        assert entry["connector"] == "grist"

    def test_gmail_and_slack_unconditional_extras_need_no_value(self):
        by_id = {e["id"]: e for e in catalogue.scope_catalogue()}
        assert by_id["gmail.filters"]["needs_value"] is False
        assert by_id["gmail.filters"]["verbs"] == ["restructure"]
        assert by_id["gmail.filters"]["label"] == "Gmail — filters (unconditional)"
        assert by_id["slack.group_chats"]["needs_value"] is False
        assert by_id["slack.group_chats"]["verbs"] == ["send"]
        assert by_id["slack.group_chats"]["label"] == "Slack — group chats (unconditional)"
        assert "gmail.configure" not in by_id and "slack.share_anything" not in by_id


class TestParseVerbs:
    def test_drops_unknown_verb_names_rather_than_raising(self):
        assert catalogue.parse_verbs(["read", "not_a_verb", "update"]) == [Verb.READ, Verb.UPDATE]

    def test_non_list_input_yields_no_verbs(self):
        assert catalogue.parse_verbs("read") == []
        assert catalogue.parse_verbs(None) == []


class TestRulesForCatalogueEntry:
    def test_a_title_instead_of_an_id_raises(self):
        with pytest.raises(RuleValueError, match="not a Slack ID"):
            catalogue.rules_for_catalogue_entry("slack.channel", ["#general"], [Verb.READ])

    def test_the_value_is_checked_before_the_verb(self):
        with pytest.raises(RuleValueError):
            catalogue.rules_for_catalogue_entry("slack.channel", "general", [Verb.RESTRUCTURE])

    def test_an_extra_scope_value_is_checked_too(self):
        with pytest.raises(RuleValueError, match="Peek page slug"):
            catalogue.rules_for_catalogue_entry("peek.page", "Team Dashboard", [Verb.READ])

    def test_a_valid_id_still_compiles(self):
        assert catalogue.rules_for_catalogue_entry("slack.channel", ["C0123456789"], [Verb.READ])
        assert catalogue.rules_for_catalogue_entry("peek.page", "team-dashboard", [Verb.READ])

    def test_unknown_group_yields_no_rules(self):
        assert catalogue.rules_for_catalogue_entry("not.a.real.group", None, [Verb.READ]) == []

    def test_a_verb_the_group_cannot_govern_yields_no_rules(self):
        # drive.folder's "download"/"read"/... verbs never include "send" -- this is the bridge's write-time
        # validation surfacing as an empty result, which gate.propose_policy_change turns into a
        # clear ValueError before any popup.
        assert catalogue.rules_for_catalogue_entry("drive.folder", ["folder1"], [Verb.SEND]) == []

    def test_a_value_needing_extra_scope_with_no_value_yields_no_rules(self):
        assert catalogue.rules_for_catalogue_entry("apps_script.project", None, [Verb.READ]) == []

    def test_apps_script_project_compiles_to_the_expected_operations(self):
        rules = catalogue.rules_for_catalogue_entry("apps_script.project", ["script1"], [Verb.READ, Verb.UPDATE])
        assert len(rules) == 1
        rule = rules[0]
        assert rule.predicate == "apps_script.project"
        assert rule.value == ["script1"]
        assert rule.operations == frozenset({
            "apps_script.read_content", "apps_script.write_content", "apps_script.read_execution_log",
        })

    def test_grist_document_compiles_read_and_create_to_their_operations(self):
        rules = catalogue.rules_for_catalogue_entry("grist.document", ["DOC1"], [Verb.READ, Verb.ADD])
        assert len(rules) == 1
        assert rules[0].predicate == "grist.document"
        assert rules[0].value == ["DOC1"]
        assert rules[0].operations == frozenset({"grist.read_records", "grist.add_records", "grist.import_csv"})

    def test_grist_document_with_no_value_yields_no_rules(self):
        assert catalogue.rules_for_catalogue_entry("grist.document", None, [Verb.READ]) == []

    def test_unconditional_extra_ignores_a_submitted_value(self):
        rules = catalogue.rules_for_catalogue_entry("gmail.filters", None, [Verb.RESTRUCTURE])
        assert len(rules) == 1
        assert rules[0].predicate == "gmail.anything"
        assert rules[0].operations == frozenset({"gmail.create_filter", "gmail.update_filter"})

    def test_drive_folder_group_delegates_to_propose_rules_for_scope_group(self):
        via_catalogue = catalogue.rules_for_catalogue_entry("drive.folder", ["folder1"], [Verb.READ])
        via_propose = propose.rules_for_scope_group("drive.folder", ["folder1"], [Verb.READ])
        assert via_catalogue == via_propose

    def test_result_is_a_real_merged_policy_rule_store_can_persist(self):
        rules = catalogue.rules_for_catalogue_entry("slack.group_chats", None, [Verb.SEND])
        # merge_rules mints the same content-derived id store.compile_rules_from_config would read
        # back -- proving this is a real, persistable PolicyRule, not just a plain construction.
        assert rules == store.merge_rules(rules)

    def test_no_catalogue_entry_carries_a_personal_data_label(self):
        assert not [e["id"] for e in catalogue.scope_catalogue() if "personal_data_label" in e]

    def test_peek_page_read_compiles_comments_and_stats(self):
        rules = catalogue.rules_for_catalogue_entry("peek.page", ["dash"], [Verb.READ])
        assert len(rules) == 1
        assert rules[0].predicate == "peek.page"
        assert rules[0].operations == frozenset({"peek.read_comments", "peek.read_stats"})

    def test_peek_page_update_compiles_upload_only(self):
        rules = catalogue.rules_for_catalogue_entry("peek.page", ["dash"], [Verb.ADD])
        assert len(rules) == 1
        assert rules[0].operations == frozenset({"peek.upload_page"})


class TestSlackExtras:
    @staticmethod
    def _entry(group):
        return next(e for e in catalogue.scope_catalogue() if e["id"] == group)

    def test_mentions_scope_offers_read_and_search(self):
        entry = self._entry("slack.mentions_of_me")
        assert entry["verbs"] == ["read", "search"]
        assert entry["connector"] == "slack"
        assert entry["label"] == "Slack — mentions of me"

    def test_direct_messages_scope_offers_read(self):
        entry = self._entry("slack.direct_messages_to_me")
        assert entry["verbs"] == ["read"]
        assert entry["label"] == "Slack — direct messages to me"

    def test_rules_compile_to_the_read_messages_operation(self):
        for group, verbs in (
            ("slack.mentions_of_me", [Verb.READ, Verb.SEARCH]),
            ("slack.direct_messages_to_me", [Verb.READ]),
        ):
            rules = catalogue.rules_for_catalogue_entry(group, None, verbs)
            assert rules
            assert {r.predicate for r in rules} == {group}
            assert all(r.operations == frozenset({"slack.read_messages"}) for r in rules)

    def test_neither_scope_takes_a_value(self):
        for group in ("slack.mentions_of_me", "slack.direct_messages_to_me"):
            assert self._entry(group)["needs_value"] is False


class TestGmailQueryExtra:
    @staticmethod
    def _entry():
        return next(e for e in catalogue.scope_catalogue() if e["id"] == "gmail.query")

    def test_offers_read_and_download(self):
        entry = self._entry()
        assert entry["verbs"] == ["read", "download"]
        assert entry["connector"] == "gmail"
        assert entry["label"] == "Gmail — query"
        assert entry["needs_value"] is True

    def test_operations_are_the_three_read_family_keys(self):
        extra = catalogue.EXTRA_SCOPES["gmail.query"]
        operations = (
            catalogue.extra_operations_for(extra, Verb.READ) | catalogue.extra_operations_for(extra, Verb.DOWNLOAD)
        )
        assert operations == {"gmail.read_message", "gmail.read_thread", "gmail.download_attachment"}

    def test_value_is_kept_whole_in_the_rule(self):
        query = 'in:inbox -category:promotions -label:"Proposed SPAM", from:(a OR b)'
        rules = catalogue.rules_for_catalogue_entry("gmail.query", [query], [Verb.READ])
        assert rules
        assert all(r.predicate == "gmail.query" and list(r.value) == [query] for r in rules)

    def test_single_value_groups_names_gmail_query(self):
        assert catalogue.SINGLE_VALUE_GROUPS == frozenset({"gmail.query"})


def test_scope_catalogue_has_no_verb_labels():
    assert all("verb_labels" not in entry for entry in catalogue.scope_catalogue())


def test_parse_verbs_maps_legacy_names():
    assert catalogue.parse_verbs(["create", "draft", "format", "comment", "label", "complete",
                                  "transition", "copy", "archive", "configure", "share"]) == [
        Verb.ADD, Verb.UPDATE, Verb.MOVE, Verb.RESTRUCTURE, Verb.SEND,
    ]
    # Current names win their own spelling, duplicates collapse keeping first-seen order, and an
    # unknown name or a non-string is still dropped.
    assert catalogue.parse_verbs(["update", "read", "label", "read", "bogus", 3]) == [Verb.UPDATE, Verb.READ]
    assert catalogue.parse_verbs("read") == []


def test_group_aliases_resolve():
    assert catalogue.GROUP_ALIASES == {
        "gmail.configure": "gmail.filters", "slack.share_anything": "slack.group_chats",
    }
    assert "gmail.configure" not in catalogue.EXTRA_SCOPES
    assert catalogue.rules_for_catalogue_entry("gmail.configure", None, [Verb.RESTRUCTURE]) == \
        catalogue.rules_for_catalogue_entry("gmail.filters", None, [Verb.RESTRUCTURE])
    assert catalogue.rules_for_catalogue_entry("slack.share_anything", None, [Verb.SEND]) == \
        catalogue.rules_for_catalogue_entry("slack.group_chats", None, [Verb.SEND])


def test_slack_group_chats_never_reaches_send_message():
    (rule,) = catalogue.rules_for_catalogue_entry("slack.group_chats", None, [Verb.SEND])
    assert rule.operations == frozenset({"slack.create_group_chat"})
    extra = catalogue.EXTRA_SCOPES["slack.group_chats"]
    assert catalogue.extra_operations_for(extra, Verb.SEND) == frozenset({"slack.create_group_chat"})


@pytest.mark.parametrize("verb, tools", [
    (Verb.READ, {"grist.read_records"}),
    (Verb.ADD, {"grist.add_records", "grist.import_csv"}),
    (Verb.UPDATE, {"grist.update_records", "grist.update_csv"}),
    (Verb.RESTRUCTURE, {"grist.create_table", "grist.add_columns"}),
])
def test_grist_document_rule_governs_every_tool_of_its_verb(verb, tools):
    rules = catalogue.rules_for_catalogue_entry("grist.document", ["DOC1"], [verb])
    assert rules[0].operations == frozenset(tools)
