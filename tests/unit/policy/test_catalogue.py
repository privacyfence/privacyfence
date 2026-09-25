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

from privacyfence.policy import catalogue, propose, store
from privacyfence.policy.registry import Verb


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

    def test_gmail_and_slack_unconditional_extras_need_no_value(self):
        by_id = {e["id"]: e for e in catalogue.scope_catalogue()}
        assert by_id["gmail.configure"]["needs_value"] is False
        assert by_id["gmail.configure"]["verbs"] == ["configure"]
        assert by_id["slack.share_anything"]["needs_value"] is False
        assert by_id["slack.share_anything"]["verbs"] == ["share"]


class TestParseVerbs:
    def test_drops_unknown_verb_names_rather_than_raising(self):
        assert catalogue.parse_verbs(["read", "not_a_verb", "update"]) == [Verb.READ, Verb.UPDATE]

    def test_non_list_input_yields_no_verbs(self):
        assert catalogue.parse_verbs("read") == []
        assert catalogue.parse_verbs(None) == []


class TestRulesForCatalogueEntry:
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

    def test_unconditional_extra_ignores_a_submitted_value(self):
        rules = catalogue.rules_for_catalogue_entry("gmail.configure", None, [Verb.CONFIGURE])
        assert len(rules) == 1
        assert rules[0].predicate == "gmail.anything"
        assert rules[0].operations == frozenset({"gmail.create_filter", "gmail.update_filter"})

    def test_drive_folder_group_delegates_to_propose_rules_for_scope_group(self):
        via_catalogue = catalogue.rules_for_catalogue_entry("drive.folder", ["folder1"], [Verb.READ])
        via_propose = propose.rules_for_scope_group("drive.folder", ["folder1"], [Verb.READ])
        assert via_catalogue == via_propose

    def test_result_is_a_real_merged_policy_rule_store_can_persist(self):
        rules = catalogue.rules_for_catalogue_entry("slack.share_anything", None, [Verb.SHARE])
        # merge_rules mints the same content-derived id store.compile_rules_from_config would read
        # back -- proving this is a real, persistable PolicyRule, not just a plain construction.
        assert rules == store.merge_rules(rules)
