"""Equivalence tests for privacyfence.policy.compat (P3 of the policy v2 redesign).

The differential harness the redesign proposal's Safety net asks for, before anything else: for
every fixture P2 already built for `policy.scopes`/`policy.conditions` -- one `(value, ctx)` per
predicate, including every absence case with `raw_data=None` -- compile a one-rule v1 config
around that predicate, and assert `AutoAcceptEvaluator.should_auto_accept()` and
`policy.engine.evaluate()` (fed `policy.compat.compile_rules()`'s output) return the *exact* same
`(bool, matched_rule)` pair, not just the same boolean. `compat.compile_rule_entry` keeps the
original v1 rule name as the compiled `PolicyRule.id` for exactly this reason -- a v1 rule name and
its v2 compilation must be indistinguishable to a caller that only looks at the result. Reusing
test_scopes.py's/test_conditions.py's own `FIXTURES` tables here (rather than a separate corpus)
is deliberate: P0's fixture corpus never shipped as its own module (see the redesign proposal's own
P0 status), so this is that harness, built from the fixtures P2 already proved individually
faithful to their old counterparts.
"""
from __future__ import annotations

import copy

import pytest

from privacyfence.auto_accept import AutoAcceptEvaluator
from privacyfence.policy import compat, store
from privacyfence.policy.conditions import condition_for_predicate
from privacyfence.policy.engine import PolicyRule, evaluate
from privacyfence.policy.scopes import SCOPE_SELECTORS

from .test_conditions import FIXTURES as CONDITION_FIXTURES
from .test_scopes import FIXTURES as SCOPE_FIXTURES

_OP = "test.operation"


def _compare(rule_name, value, ctx):
    rules_config = {_OP: [{"rule": rule_name, "value": value}]}
    v1_ok, v1_rule = AutoAcceptEvaluator(rules_config).should_auto_accept(_OP, ctx)
    v2_ok, v2_rule = evaluate(compat.compile_rules(rules_config), _OP, ctx)
    return (v1_ok, v1_rule), (v2_ok, v2_rule)


class TestCompiledScopeRulesAgreeWithV1:
    @pytest.mark.parametrize("predicate", sorted(SCOPE_FIXTURES))
    def test_agrees_on_every_fixture(self, predicate):
        for value, ctx in SCOPE_FIXTURES[predicate]:
            v1_result, v2_result = _compare(predicate, value, ctx)
            assert v2_result == v1_result, (predicate, value, ctx, v1_result, v2_result)


class TestCompiledConditionRulesAgreeWithV1:
    @pytest.mark.parametrize("predicate", sorted(CONDITION_FIXTURES))
    def test_agrees_on_every_fixture(self, predicate):
        for value, ctx in CONDITION_FIXTURES[predicate]:
            v1_result, v2_result = _compare(predicate, value, ctx)
            assert v2_result == v1_result, (predicate, value, ctx, v1_result, v2_result)


class TestCompileRuleEntry:
    def test_scope_predicate_compiles_with_original_name_as_id(self):
        rule = compat.compile_rule_entry(_OP, "approved_folder", ["f1"])
        assert rule == PolicyRule(id="approved_folder", predicate="approved_folder", value=["f1"],
                                   operations=frozenset({_OP}))

    def test_condition_predicate_compiles_to_always_allow_plus_condition(self):
        rule = compat.compile_rule_entry(_OP, "shared_drive_exclusion", None)
        condition = condition_for_predicate("shared_drive_exclusion")
        assert rule == PolicyRule(
            id="shared_drive_exclusion", predicate="always_allow", value=None,
            operations=frozenset({_OP}), conditions=((condition.name, None),),
        )

    def test_session_temp_accept_pseudo_rule_compiles_to_nothing(self):
        assert compat.compile_rule_entry(_OP, "session_temp_accept", None) is None

    def test_unknown_rule_name_compiles_to_nothing(self):
        assert compat.compile_rule_entry(_OP, "not_a_real_rule", None) is None

    def test_every_scope_selector_key_is_reachable(self):
        for predicate in SCOPE_SELECTORS:
            assert compat.compile_rule_entry(_OP, predicate, None) is not None


class TestCompileRules:
    def test_flattens_across_operation_keys_preserving_order(self):
        rules_config = {
            "op.a": [{"rule": "approved_folder", "value": ["f1"]}, {"rule": "i_am_owner", "value": None}],
            "op.b": [{"rule": "always_allow", "value": None}],
        }
        rules = compat.compile_rules(rules_config)
        assert [r.id for r in rules] == ["approved_folder", "i_am_owner", "always_allow"]
        assert rules[0].operations == frozenset({"op.a"})
        assert rules[2].operations == frozenset({"op.b"})

    def test_drops_unknown_entries_without_raising(self):
        rules_config = {_OP: [{"rule": "not_a_real_rule", "value": None}, {"rule": "always_allow", "value": None}]}
        rules = compat.compile_rules(rules_config)
        assert [r.id for r in rules] == ["always_allow"]

    def test_empty_config_compiles_to_no_rules(self):
        assert compat.compile_rules({}) == []
        assert compat.compile_rules(None) == []


class TestMigrateToPolicyV2:
    """P4: the one-time on-disk migration. The load-bearing assertion is
    ``test_migrated_v2_config_compiles_to_the_same_rule_set_v1_would`` -- everything else here is
    the idempotency/marker/never-touches-v1 bookkeeping ``resource_grants.TestMigrateRulesToGrants``
    already established the shape for."""

    def _rules_config(self):
        return {
            "drive.write_file": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "sheets.write_range": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "sheets.delete_dimensions": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "gmail.read_message": [{"rule": "shared_drive_exclusion", "value": None}],
        }

    def test_marker_is_set(self):
        cfg, migrated = compat.migrate_to_policy_v2({}, self._rules_config())
        assert migrated is True
        assert cfg[store.MIGRATED_TO_POLICY_V2_MARKER] is True

    def test_writes_an_auto_accept_section(self):
        cfg, _ = compat.migrate_to_policy_v2({}, self._rules_config())
        assert store.AUTO_ACCEPT_CONFIG_KEY in cfg
        assert cfg[store.AUTO_ACCEPT_CONFIG_KEY]["version"] == store.SCHEMA_VERSION
        assert cfg[store.AUTO_ACCEPT_CONFIG_KEY]["rules"]

    def test_does_not_mutate_the_caller_s_dict(self):
        original = {"auto_accept_rules": self._rules_config()}
        snapshot = copy.deepcopy(original)
        compat.migrate_to_policy_v2(original, self._rules_config())
        assert original == snapshot

    def test_never_touches_v1_sections(self):
        original = {"auto_accept_rules": self._rules_config(), "auto_accept_grants": {"drive": {}}}
        cfg, _ = compat.migrate_to_policy_v2(original, self._rules_config())
        assert cfg["auto_accept_rules"] == original["auto_accept_rules"]
        assert cfg["auto_accept_grants"] == original["auto_accept_grants"]

    def test_already_migrated_config_is_returned_unchanged(self):
        cfg = {store.MIGRATED_TO_POLICY_V2_MARKER: True}
        result, migrated = compat.migrate_to_policy_v2(cfg, self._rules_config())
        assert result is cfg
        assert migrated is False

    def test_idempotent_second_run_is_a_no_op(self):
        once, _ = compat.migrate_to_policy_v2({}, self._rules_config())
        twice, migrated_again = compat.migrate_to_policy_v2(once, self._rules_config())
        assert migrated_again is False
        assert twice == once

    def test_empty_rules_config_sets_the_marker_in_memory_but_signals_nothing_to_persist(self):
        # A fresh/empty v1 config compiles to no rules -- the returned copy still carries the
        # marker (so a later run with the same empty config doesn't redo the work), but the second
        # element is False: there's nothing worth a real disk write, a .bak, or a log line for.
        # This is what keeps daemon_main.run_app() from performing a real write on every single
        # startup of an install with zero configured auto-accept rules.
        cfg, migrated = compat.migrate_to_policy_v2({}, {})
        assert migrated is False
        assert cfg[store.MIGRATED_TO_POLICY_V2_MARKER] is True
        assert cfg[store.AUTO_ACCEPT_CONFIG_KEY]["rules"] == []

    def test_migrated_v2_config_compiles_to_the_same_rule_set_v1_would(self):
        """The round-trip test the redesign proposal's P4 exit criteria names: v1 -> migrate -> v2
        -> compile must equal compiling v1 directly (merged, since store.merge_rules unions same-
        meaning entries the way compile_rules alone wouldn't -- v2.evaluate()'s matching is
        unaffected by that merge, since it only ever checks operation-set membership)."""
        rules_config = self._rules_config()
        direct = store.merge_rules(compat.compile_rules(rules_config))

        cfg, _ = compat.migrate_to_policy_v2({}, rules_config)
        via_disk = store.compile_rules_from_config(cfg)

        assert sorted(via_disk, key=lambda r: r.id) == sorted(direct, key=lambda r: r.id)

    def test_merges_same_scope_across_operations_into_one_stored_rule(self):
        cfg, _ = compat.migrate_to_policy_v2({}, self._rules_config())
        rules = store.compile_rules_from_config(cfg)
        sandbox_rules = [r for r in rules if r.predicate == "approved_sandbox_folder"]
        assert len(sandbox_rules) == 1
        assert sandbox_rules[0].operations == frozenset(
            {"drive.write_file", "sheets.write_range", "sheets.delete_dimensions"}
        )
