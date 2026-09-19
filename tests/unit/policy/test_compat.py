"""Equivalence tests for privacyfence.policy.compat.

The differential harness the redesign proposal's Safety net asks for, before anything else: for
every fixture P2 already built for `policy.scopes`/`policy.conditions` -- one `(value, ctx)` per
predicate, including every absence case with `raw_data=None` -- compile a one-rule v1 config
around that predicate, and assert the frozen pre-P9 `V1Reference` (tests/unit/policy/
_v1_reference.py -- a verbatim copy of what `AutoAcceptEvaluator.should_auto_accept()` did before
P9 deleted it) and `policy.engine.evaluate()` (fed `policy.compat.compile_rules()`'s output) return
the *exact* same `(bool, matched_rule)` pair, not just the same boolean. `compat.compile_rule_entry`
keeps the original v1 rule name as the compiled `PolicyRule.id` for exactly this reason -- a v1 rule
name and its v2 compilation must be indistinguishable to a caller that only looks at the result.
Reusing test_scopes.py's/test_conditions.py's own `FIXTURES` tables here (rather than a separate
corpus) is deliberate: P0's fixture corpus never shipped as its own module (see the redesign
proposal's own P0 status), so this is that harness, built from the fixtures P2 already proved
individually faithful to their old counterparts.

As of P9, `policy.compat.compile_rules`/`compile_rule_entry` have exactly one production caller
(`migrate_to_policy_v2`, the one-time on-disk migration) -- the shadow-mode comparison this harness
originally protected is gone from gate.py, but the compiler itself is still load-bearing (a
hand-edited install's v1 config must still migrate to the exact rule set v1 would have evaluated),
so this equivalence coverage still matters and stays.
"""
from __future__ import annotations

import copy

import pytest

from privacyfence.policy import compat, store
from privacyfence.policy.conditions import condition_for_predicate
from privacyfence.policy.engine import PolicyRule, evaluate
from privacyfence.policy.scopes import SCOPE_SELECTORS

from ._v1_reference import V1Reference
from .test_conditions import FIXTURES as CONDITION_FIXTURES
from .test_scopes import FIXTURES as SCOPE_FIXTURES

_OP = "test.operation"
_V1 = V1Reference()


def _v1_should_auto_accept(rule_name: str, value, ctx) -> tuple[bool, str]:
    """The exact dispatch `AutoAcceptEvaluator.should_auto_accept()`/`_evaluate()` used to do for
    a single-entry rules_config -- reproduced here rather than on `V1Reference` itself, which is
    kept to just the bare `_rule_*` predicates per its own docstring."""
    fn = getattr(_V1, f"_rule_{rule_name}", None)
    if fn is None:
        return False, ""
    return (True, rule_name) if fn(value, ctx) else (False, "")


def _compare(rule_name, value, ctx):
    rules_config = {_OP: [{"rule": rule_name, "value": value}]}
    v1_result = _v1_should_auto_accept(rule_name, value, ctx)
    v2_ok, v2_rule = evaluate(compat.compile_rules(rules_config), _OP, ctx)
    return v1_result, (v2_ok, v2_rule)


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
    idempotency/marker/never-touches-v1 bookkeeping.

    P9: ``migrate_to_policy_v2`` now takes just ``cfg`` (it reads ``policy.resource_registry.
    effective_v1_rules(cfg)`` internally, since nothing else needs that merged view anymore) --
    every fixture here builds a ``cfg`` with an explicit ``auto_accept_rules`` section instead of
    passing the equivalent dict as a second argument.
    """

    def _rules_config(self):
        return {
            "drive.write_file": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "sheets.write_range": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "sheets.delete_dimensions": [{"rule": "approved_sandbox_folder", "value": ["F1"]}],
            "gmail.read_message": [{"rule": "shared_drive_exclusion", "value": None}],
        }

    def _cfg(self):
        return {"auto_accept_rules": self._rules_config()}

    def test_marker_is_set(self):
        cfg, migrated = compat.migrate_to_policy_v2(self._cfg())
        assert migrated is True
        assert cfg[store.MIGRATED_TO_POLICY_V2_MARKER] is True

    def test_writes_an_auto_accept_section(self):
        cfg, _ = compat.migrate_to_policy_v2(self._cfg())
        assert store.AUTO_ACCEPT_CONFIG_KEY in cfg
        assert cfg[store.AUTO_ACCEPT_CONFIG_KEY]["version"] == store.SCHEMA_VERSION
        assert cfg[store.AUTO_ACCEPT_CONFIG_KEY]["rules"]

    def test_does_not_mutate_the_caller_s_dict(self):
        original = self._cfg()
        snapshot = copy.deepcopy(original)
        compat.migrate_to_policy_v2(original)
        assert original == snapshot

    def test_never_touches_v1_sections(self):
        original = {**self._cfg(), "auto_accept_grants": {"drive": {}}}
        cfg, _ = compat.migrate_to_policy_v2(original)
        assert cfg["auto_accept_rules"] == original["auto_accept_rules"]
        assert cfg["auto_accept_grants"] == original["auto_accept_grants"]

    def test_already_migrated_config_is_returned_unchanged(self):
        cfg = {store.MIGRATED_TO_POLICY_V2_MARKER: True, **self._cfg()}
        result, migrated = compat.migrate_to_policy_v2(cfg)
        assert result is cfg
        assert migrated is False

    def test_idempotent_second_run_is_a_no_op(self):
        once, _ = compat.migrate_to_policy_v2(self._cfg())
        twice, migrated_again = compat.migrate_to_policy_v2(once)
        assert migrated_again is False
        assert twice == once

    def test_empty_rules_config_sets_the_marker_in_memory_but_signals_nothing_to_persist(self):
        # A fresh/empty v1 config compiles to no rules -- the returned copy still carries the
        # marker (so a later run with the same empty config doesn't redo the work), but the second
        # element is False: there's nothing worth a real disk write, a .bak, or a log line for.
        # This is what keeps daemon_main.run_app() from performing a real write on every single
        # startup of an install with zero configured auto-accept rules.
        cfg, migrated = compat.migrate_to_policy_v2({})
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

        cfg, _ = compat.migrate_to_policy_v2(self._cfg())
        via_disk = store.compile_rules_from_config(cfg)

        assert sorted(via_disk, key=lambda r: r.id) == sorted(direct, key=lambda r: r.id)

    def test_merges_same_scope_across_operations_into_one_stored_rule(self):
        cfg, _ = compat.migrate_to_policy_v2(self._cfg())
        rules = store.compile_rules_from_config(cfg)
        sandbox_rules = [r for r in rules if r.predicate == "approved_sandbox_folder"]
        assert len(sandbox_rules) == 1
        assert sandbox_rules[0].operations == frozenset(
            {"drive.write_file", "sheets.write_range", "sheets.delete_dimensions"}
        )

    def test_folds_a_v1_grant_into_the_same_result_as_an_equivalent_rule(self):
        """P9: migration now reads grant-expanded ``auto_accept_grants`` via
        ``policy.resource_registry.effective_v1_rules`` the same way it always read
        ``auto_accept_rules`` -- a trusted sandbox folder set only as a v1 grant must migrate to
        the identical v2 rule a hand-written ``approved_sandbox_folder`` rule entry would have."""
        via_grant, _ = compat.migrate_to_policy_v2({
            "auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}},
        })
        via_rule, _ = compat.migrate_to_policy_v2(self._cfg())
        sandbox_via_grant = [
            r for r in store.compile_rules_from_config(via_grant) if r.predicate == "approved_sandbox_folder"
        ]
        sandbox_via_rule = [
            r for r in store.compile_rules_from_config(via_rule) if r.predicate == "approved_sandbox_folder"
        ]
        assert sandbox_via_grant and sandbox_via_grant[0].value == sandbox_via_rule[0].value
