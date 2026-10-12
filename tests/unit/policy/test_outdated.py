"""Tests for privacyfence.policy.outdated.

A stored rule keeps the operations it was saved with, so a tool added later is denied under a rule
whose verbs already name it. ``missing_operations`` is the pure answer to "what would re-adding this
rule's verbs add?"; the invariant that matters most is that it never offers more than the rule's own
verbs, connector and conditions imply (a read-only rule is never offered a write tool, a Gmail label
rule is never offered Contacts operations) and never guesses for a rule the catalogues do not know.
"""
from __future__ import annotations

import dataclasses

from privacyfence.policy import catalogue, outdated
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb


def _rule(group: str, value: list[str] | None, verbs: list[Verb]) -> PolicyRule:
    (rule,) = catalogue.rules_for_catalogue_entry(group, value, verbs)
    return rule


def _without(rule: PolicyRule, *operations: str) -> PolicyRule:
    return dataclasses.replace(rule, operations=rule.operations - frozenset(operations))


class TestMissingOperations:

    def test_grist_rule_saved_before_the_csv_tools_reports_the_import(self):
        full = _rule("grist.document", ["DOC1"], [Verb.READ, Verb.CREATE, Verb.UPDATE])
        stale = _without(full, "grist.import_csv")
        assert outdated.missing_operations(stale) == {"grist.import_csv"}
        assert "grist_import_csv" in outdated.missing_tools(stale)
        assert outdated.missing_tools(stale) == ("grist_import_csv",)

    def test_a_complete_rule_reports_nothing(self):
        full = _rule("grist.document", ["DOC1"], [Verb.READ, Verb.CREATE, Verb.UPDATE])
        assert outdated.missing_operations(full) == frozenset()
        assert outdated.missing_tools(full) == ()

    def test_a_proposable_scope_rule_reports_its_missing_operation(self):
        full = _rule("drive.folder", ["F1"], [Verb.READ, Verb.DOWNLOAD])
        stale = _without(full, "sheets.read_values")
        assert outdated.missing_operations(stale) == {"sheets.read_values"}

    def test_an_extra_scope_rule_is_supported(self):
        full = _rule("apps_script.project", ["SCRIPT1"], [Verb.READ, Verb.UPDATE])
        stale = _without(full, "apps_script.read_execution_log")
        assert outdated.missing_operations(stale) == {"apps_script.read_execution_log"}
        assert outdated.missing_tools(stale)

    def test_a_predicate_no_catalogue_knows_reports_nothing(self):
        rule = PolicyRule(id="r", predicate="hand_written", value=["x"], operations=frozenset({"grist.add_records"}))
        assert outdated.missing_operations(rule) == frozenset()
        assert outdated.missing_tools(rule) == ()

    def test_a_move_rule_is_not_widened_by_the_copy_verb(self):
        rule = PolicyRule(
            id="r", predicate="move_within_approved_folders", value=["F1", "F2"],
            operations=frozenset({"drive.move_file"}),
        )
        assert outdated.missing_operations(rule) == frozenset()

    def test_a_verb_the_rule_does_not_have_contributes_nothing(self):
        read_only = _rule("grist.document", ["DOC1"], [Verb.READ])
        assert outdated.missing_operations(read_only) == frozenset()
        stale = _without(read_only, "grist.read_records")
        assert outdated.missing_operations(stale) == frozenset()  # no verb left to imply anything

    def test_a_read_only_rule_is_never_offered_write_tools(self):
        full = _rule("drive.folder", ["F1"], [Verb.READ])
        stale = _without(full, "drive.read_file_contents")
        stale = dataclasses.replace(stale, operations=stale.operations | {"drive.download_file"})
        missing = outdated.missing_operations(stale)
        assert all(op.split(".")[1].startswith(("read", "download")) for op in missing)

    def test_a_shared_predicate_is_limited_to_the_rules_own_connector(self):
        gmail = _rule("gmail.label", ["Newsletters"], [Verb.LABEL])
        assert outdated.missing_operations(gmail) == frozenset()
        stale = _without(gmail, "gmail.remove_label")
        assert outdated.missing_operations(stale) == {"gmail.remove_label"}

    def test_a_conditioned_entry_whose_condition_does_not_match_is_skipped(self):
        operations = frozenset({"calendar.read_event_details"})
        matching = PolicyRule(
            id="a", predicate="always_allow", value=None, operations=frozenset(),
            conditions=(("not_private", None),),
        )
        assert outdated.missing_operations(dataclasses.replace(matching, operations=operations)) == frozenset()
        unknown = dataclasses.replace(
            matching, operations=frozenset({"calendar.read_event_details"}), conditions=(("something_else", 1),),
        )
        assert outdated.missing_operations(unknown) == frozenset()
        two_conditions = dataclasses.replace(
            matching, operations=operations, conditions=(("not_private", None), ("no_external_attendees", None)),
        )
        assert outdated.missing_operations(two_conditions) == frozenset()

    def test_an_unconditional_entry_is_skipped_for_a_rule_carrying_conditions(self):
        rule = PolicyRule(
            id="a", predicate="always_allow", value=None, operations=frozenset({"gmail.create_draft"}),
            conditions=(("not_private", None),),
        )
        assert outdated.missing_operations(rule) == frozenset()
