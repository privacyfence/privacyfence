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

import pytest

from privacyfence.auto_accept import AutoAcceptEvaluator
from privacyfence.policy import compat
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
