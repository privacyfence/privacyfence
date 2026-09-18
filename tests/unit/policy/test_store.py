"""Tests for privacyfence.policy.store (P4 of the policy v2 redesign): the on-disk v2 schema.

Covers the serialization round trip (``rule_to_dict``/``rule_from_dict``/``compile_rules_from_config``),
``merge_rules``'s union-by-meaning behaviour and its stable, content-derived ids, and the
destructive/send verb classification the Settings migration banner (``settings_controller.
SettingsController.policy_v2_migration_notice_html``) depends on.
"""
from __future__ import annotations

from privacyfence.policy import store
from privacyfence.policy.engine import PolicyRule


def _rule(id_="r1", predicate="approved_folder", value=None, operations=("op.a",), conditions=()):
    return PolicyRule(id=id_, predicate=predicate, value=value, operations=frozenset(operations),
                       conditions=tuple(conditions))


class TestRuleIdFor:
    def test_same_predicate_value_conditions_gives_same_id(self):
        a = store.rule_id_for("approved_folder", ["f1"], ())
        b = store.rule_id_for("approved_folder", ["f1"], ())
        assert a == b

    def test_id_does_not_depend_on_list_value_order(self):
        a = store.rule_id_for("approved_folder", ["f1", "f2"], ())
        b = store.rule_id_for("approved_folder", ["f2", "f1"], ())
        assert a == b

    def test_different_value_gives_different_id(self):
        a = store.rule_id_for("approved_folder", ["f1"], ())
        b = store.rule_id_for("approved_folder", ["f2"], ())
        assert a != b

    def test_different_predicate_gives_different_id(self):
        a = store.rule_id_for("approved_folder", ["f1"], ())
        b = store.rule_id_for("i_am_owner", ["f1"], ())
        assert a != b

    def test_different_conditions_gives_different_id(self):
        a = store.rule_id_for("always_allow", None, ())
        b = store.rule_id_for("always_allow", None, (("shared_drive_exclusion", None),))
        assert a != b

    def test_id_is_a_stable_prefixed_string(self):
        rule_id = store.rule_id_for("approved_folder", ["f1"], ())
        assert rule_id.startswith("r-")
        assert len(rule_id) == len("r-") + 10


class TestMergeRules:
    def test_same_meaning_across_operations_merges_into_one_rule(self):
        rules = [
            _rule(id_="a", predicate="approved_sandbox_folder", value=["F1"], operations=("drive.write_file",)),
            _rule(id_="b", predicate="approved_sandbox_folder", value=["F1"], operations=("sheets.write_range",)),
        ]
        merged = store.merge_rules(rules)
        assert len(merged) == 1
        assert merged[0].operations == frozenset({"drive.write_file", "sheets.write_range"})

    def test_merged_rule_gets_the_content_derived_id_not_either_original_id(self):
        rules = [
            _rule(id_="a", predicate="approved_sandbox_folder", value=["F1"], operations=("drive.write_file",)),
            _rule(id_="b", predicate="approved_sandbox_folder", value=["F1"], operations=("sheets.write_range",)),
        ]
        merged = store.merge_rules(rules)
        assert merged[0].id == store.rule_id_for("approved_sandbox_folder", ["F1"], ())
        assert merged[0].id not in ("a", "b")

    def test_different_value_stays_separate(self):
        rules = [
            _rule(id_="a", predicate="approved_folder", value=["F1"], operations=("op.a",)),
            _rule(id_="b", predicate="approved_folder", value=["F2"], operations=("op.b",)),
        ]
        merged = store.merge_rules(rules)
        assert len(merged) == 2
        assert {r.operations for r in merged} == {frozenset({"op.a"}), frozenset({"op.b"})}

    def test_different_conditions_stays_separate(self):
        rules = [
            _rule(id_="a", predicate="always_allow", value=None, operations=("op.a",), conditions=()),
            _rule(id_="b", predicate="always_allow", value=None, operations=("op.b",),
                  conditions=(("shared_drive_exclusion", None),)),
        ]
        merged = store.merge_rules(rules)
        assert len(merged) == 2

    def test_preserves_first_seen_order(self):
        rules = [
            _rule(id_="a", predicate="i_am_owner", value=None, operations=("op.a",)),
            _rule(id_="b", predicate="always_allow", value=None, operations=("op.b",)),
        ]
        merged = store.merge_rules(rules)
        assert [r.predicate for r in merged] == ["i_am_owner", "always_allow"]

    def test_empty_list(self):
        assert store.merge_rules([]) == []


class TestRuleDictRoundTrip:
    def test_rule_to_dict_then_from_dict_is_lossless(self):
        rule = _rule(id_="r-abc123", predicate="always_allow", value=None, operations=("op.a", "op.b"),
                      conditions=(("shared_drive_exclusion", None), ("older_than_days", 7)))
        restored = store.rule_from_dict(store.rule_to_dict(rule))
        assert restored == rule

    def test_operations_serialize_sorted(self):
        rule = _rule(operations=("z.op", "a.op"))
        data = store.rule_to_dict(rule)
        assert data["operations"] == ["a.op", "z.op"]


class TestRuleFromDictFailsClosed:
    def test_not_a_dict_returns_none(self):
        assert store.rule_from_dict("not a dict") is None
        assert store.rule_from_dict(None) is None
        assert store.rule_from_dict([1, 2]) is None

    def test_missing_id_returns_none(self):
        assert store.rule_from_dict({"predicate": "always_allow", "operations": ["op.a"]}) is None

    def test_missing_predicate_returns_none(self):
        assert store.rule_from_dict({"id": "r1", "operations": ["op.a"]}) is None

    def test_missing_operations_returns_none(self):
        assert store.rule_from_dict({"id": "r1", "predicate": "always_allow"}) is None

    def test_empty_operations_returns_none(self):
        assert store.rule_from_dict({"id": "r1", "predicate": "always_allow", "operations": []}) is None

    def test_non_string_operation_returns_none(self):
        assert store.rule_from_dict({"id": "r1", "predicate": "always_allow", "operations": [1]}) is None

    def test_malformed_conditions_returns_none(self):
        base = {"id": "r1", "predicate": "always_allow", "operations": ["op.a"]}
        assert store.rule_from_dict({**base, "conditions": "not a list"}) is None
        assert store.rule_from_dict({**base, "conditions": [["only_one_element"]]}) is None
        assert store.rule_from_dict({**base, "conditions": [[1, "value"]]}) is None

    def test_absent_conditions_defaults_to_empty_tuple(self):
        rule = store.rule_from_dict({"id": "r1", "predicate": "always_allow", "operations": ["op.a"]})
        assert rule.conditions == ()


class TestCompileRulesFromConfig:
    def test_no_auto_accept_section_compiles_to_no_rules(self):
        assert store.compile_rules_from_config({}) == []

    def test_wrong_type_section_compiles_to_no_rules(self):
        assert store.compile_rules_from_config({"auto_accept": "not a dict"}) == []

    def test_missing_rules_key_compiles_to_no_rules(self):
        assert store.compile_rules_from_config({"auto_accept": {"version": 2}}) == []

    def test_wrong_type_rules_value_compiles_to_no_rules(self):
        assert store.compile_rules_from_config({"auto_accept": {"rules": "not a list"}}) == []

    def test_one_malformed_entry_is_dropped_others_still_compile(self):
        cfg = {
            "auto_accept": {
                "version": 2,
                "rules": [
                    {"id": "r1", "predicate": "always_allow", "operations": ["op.a"]},
                    {"id": "bad", "predicate": "always_allow"},  # missing operations
                ],
            }
        }
        rules = store.compile_rules_from_config(cfg)
        assert [r.id for r in rules] == ["r1"]

    def test_valid_config_compiles_every_rule(self):
        cfg = {
            "auto_accept": {
                "version": 2,
                "rules": [
                    {"id": "r1", "predicate": "always_allow", "operations": ["op.a"]},
                    {"id": "r2", "predicate": "i_am_owner", "operations": ["op.b"]},
                ],
            }
        }
        assert [r.id for r in store.compile_rules_from_config(cfg)] == ["r1", "r2"]


class TestRulesToConfig:
    def test_round_trips_through_compile_rules_from_config(self):
        rules = [_rule(id_="r-x", predicate="always_allow", value=None, operations=("op.a",))]
        cfg = {"auto_accept": store.rules_to_config(rules)}
        assert store.compile_rules_from_config(cfg) == rules

    def test_carries_schema_version(self):
        assert store.rules_to_config([])["version"] == store.SCHEMA_VERSION


class TestVerbFamiliesAndDestructiveOrSend:
    def test_delete_operation_is_destructive(self):
        rule = _rule(predicate="always_allow", value=None, operations=("sheets.delete_dimensions",))
        from privacyfence.policy.registry import VerbFamily
        assert VerbFamily.DESTRUCTIVE in store.verb_families(rule)
        assert store.is_destructive_or_send(rule) is True

    def test_send_operation_is_send(self):
        rule = _rule(predicate="always_allow", value=None, operations=("slack.send_message",))
        from privacyfence.policy.registry import VerbFamily
        assert VerbFamily.SEND in store.verb_families(rule)
        assert store.is_destructive_or_send(rule) is True

    def test_read_only_operation_is_neither(self):
        rule = _rule(predicate="approved_folder", value=["F1"], operations=("drive.read_file_contents",))
        assert store.is_destructive_or_send(rule) is False

    def test_write_only_operation_is_not_flagged(self):
        rule = _rule(predicate="always_allow", value=None, operations=("jira.update_issue",))
        assert store.is_destructive_or_send(rule) is False

    def test_unknown_operation_has_no_verb_families(self):
        rule = _rule(predicate="always_allow", value=None, operations=("not.a.real.operation",))
        assert store.verb_families(rule) == frozenset()
        assert store.is_destructive_or_send(rule) is False


class TestDestructiveOrSendRules:
    def test_filters_configured_rules_to_only_destructive_or_send(self):
        cfg = {
            "auto_accept": {
                "version": 2,
                "rules": [
                    {"id": "r-read", "predicate": "approved_folder", "value": ["F1"],
                     "operations": ["drive.read_file_contents"]},
                    {"id": "r-delete", "predicate": "always_allow", "operations": ["sheets.delete_dimensions"]},
                ],
            }
        }
        flagged = store.destructive_or_send_rules(cfg)
        assert [r.id for r in flagged] == ["r-delete"]

    def test_empty_config_flags_nothing(self):
        assert store.destructive_or_send_rules({}) == []
