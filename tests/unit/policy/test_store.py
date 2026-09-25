"""Tests for privacyfence.policy.store: the on-disk v2 schema.

Covers the serialization round trip (``rule_to_dict``/``rule_from_dict``/``compile_rules_from_config``),
``merge_rules``'s union-by-meaning behaviour and its stable, content-derived ids, and
``reject_v1_sections``' refusal of a config written for the earlier format (ADR 0041).
"""
from __future__ import annotations

import pytest

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
        b = store.rule_id_for("always_allow", None, (("not_shared_drive", None),))
        assert a != b

    def test_id_is_a_stable_prefixed_string(self):
        rule_id = store.rule_id_for("approved_folder", ["f1"], ())
        assert rule_id.startswith("r-")
        assert len(rule_id) == len("r-") + 10


class TestRuleIdForRule:
    """Rule attribution: the canonical id for an already-compiled rule,
    independent of whatever its own ``.id`` happens to be -- gate.py's ``_evaluate_auto_accept``
    needs this to attribute a decision made against a rule whose ``.id`` is not canonical (one
    built in memory) to the same row Settings' Auto-accept page lists."""

    def test_matches_rule_id_for_of_the_same_fields(self):
        rule = _rule(id_="approved_sandbox_folder", predicate="approved_sandbox_folder", value=["F1"])
        assert store.rule_id_for_rule(rule) == store.rule_id_for("approved_sandbox_folder", ["F1"], ())

    def test_ignores_the_rules_own_id(self):
        # Same (predicate, value, conditions) as above, but a completely different, made-up `.id`
        # -- exactly the shape a v1-compiled rule has (compat.compile_rule_entry sets id=rule_name)
        # and exactly why this function recomputes rather than trusting `.id`.
        rule = _rule(id_="some ambiguous v1 name", predicate="approved_sandbox_folder", value=["F1"])
        assert store.rule_id_for_rule(rule) == store.rule_id_for("approved_sandbox_folder", ["F1"], ())

    def test_two_rules_with_the_same_id_but_different_values_get_different_canonical_ids(self):
        # Two v1-compiled rules sharing one ambiguous name (`.id`) because
        # they came from the same predicate, but naming two different resources.
        a = _rule(id_="approved_sandbox_folder", predicate="approved_sandbox_folder", value=["F1"])
        b = _rule(id_="approved_sandbox_folder", predicate="approved_sandbox_folder", value=["F2"])
        assert store.rule_id_for_rule(a) != store.rule_id_for_rule(b)

    def test_agrees_with_merge_rules_own_id_for_the_same_meaning(self):
        rules = [
            _rule(id_="a", predicate="approved_sandbox_folder", value=["F1"], operations=("drive.write_file",)),
            _rule(id_="b", predicate="approved_sandbox_folder", value=["F1"], operations=("sheets.write_range",)),
        ]
        merged = store.merge_rules(rules)
        assert store.rule_id_for_rule(merged[0]) == merged[0].id


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


class TestDropConvertedV1Sections:
    """ADR 0047: 4.1-4.4 converted the v1 sections and left them on disk under a marker."""

    def test_removes_both_sections_and_the_marker(self):
        current = store.rules_to_config([_rule()])
        cfg = {
            store.AUTO_ACCEPT_CONFIG_KEY: current,
            "auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]},
            "auto_accept_grants": {},
            store.CONVERTED_V1_MARKER: True,
            "logging": {},
        }
        removed = store.drop_converted_v1_sections(cfg)
        assert removed == ["auto_accept_rules", "auto_accept_grants", store.CONVERTED_V1_MARKER]
        assert cfg == {store.AUTO_ACCEPT_CONFIG_KEY: current, "logging": {}}
        store.reject_v1_sections(cfg, "cfg")

    def test_a_marker_alone_is_removed(self):
        cfg = {store.CONVERTED_V1_MARKER: True}
        assert store.drop_converted_v1_sections(cfg) == [store.CONVERTED_V1_MARKER]
        assert cfg == {}

    @pytest.mark.parametrize("marker", [None, False])
    def test_an_unconverted_v1_section_is_left_for_the_refusal(self, marker):
        cfg = {"auto_accept_rules": {}}
        if marker is not None:
            cfg[store.CONVERTED_V1_MARKER] = marker
        before = dict(cfg)
        assert store.drop_converted_v1_sections(cfg) == []
        assert cfg == before
        with pytest.raises(store.V1PolicyConfigError):
            store.reject_v1_sections(cfg, "cfg")


class TestRejectV1Sections:
    @pytest.mark.parametrize("key", store.V1_SECTION_KEYS)
    def test_each_v1_section_is_refused_and_named(self, key):
        with pytest.raises(store.V1PolicyConfigError) as excinfo:
            store.reject_v1_sections({key: {"contacts.edit": [{"rule": "no_contact_info_change"}]}}, "/cfg.yaml")
        message = str(excinfo.value)
        assert "/cfg.yaml" in message
        assert f"'{key}'" in message
        assert "that section" in message
        assert "Auto-accept page" in message
        assert "not converted automatically" in message

    def test_both_sections_are_named_together(self):
        with pytest.raises(store.V1PolicyConfigError) as excinfo:
            store.reject_v1_sections({"auto_accept_rules": {}, "auto_accept_grants": {}}, "cfg")
        message = str(excinfo.value)
        assert "'auto_accept_rules' and 'auto_accept_grants'" in message
        assert "those sections" in message

    @pytest.mark.parametrize("value", [None, {}, []])
    def test_an_empty_v1_section_is_still_refused(self, value):
        with pytest.raises(store.V1PolicyConfigError):
            store.reject_v1_sections({"auto_accept_grants": value}, "cfg")

    def test_is_a_value_error(self):
        # daemon_main.main reports ValueError from load_config as "Configuration error" and exits 1.
        assert issubclass(store.V1PolicyConfigError, ValueError)

    def test_current_format_passes(self):
        cfg = {store.AUTO_ACCEPT_CONFIG_KEY: store.rules_to_config([_rule()]), "logging": {}}
        assert store.reject_v1_sections(cfg, "cfg") is None

    def test_empty_config_passes(self):
        assert store.reject_v1_sections({}, "cfg") is None
