"""Which operations a stored rule is missing, for the verbs it already has (ADR 0166).

A rule id is derived from content that leaves the operations out (ADR 0074) and ``store.merge_rules``
unions them, so a rule saved before a release added a tool still shows the verb that tool belongs to
-- ``describe.rule_verbs`` reads the verbs off the operations the rule already carries -- but lacks
the new operation key, and the new tool is denied. This module names that gap. It is pure and
derived from the same catalogues the "add a rule" form compiles from (``propose.PROPOSABLE_SCOPES``,
``catalogue.EXTRA_SCOPES``), so what it reports is exactly what re-adding the rule's verbs from the
form would add, never a guess.

Nothing here writes anything. Stored rules are widened only by an explicit, confirmed update
(``SettingsController.update_policy_rule``); no load, upgrade or startup path calls into this module.
"""
from __future__ import annotations

from . import catalogue, describe, propose
from .engine import PolicyRule
from .registry import TOOL_REGISTRY


def _entry_matches_conditions(entry: propose.ProposableScope, rule: PolicyRule) -> bool:
    """Only an entry whose condition is the rule's own is counted. A rule carrying conditions the
    catalogue does not know, or an entry carrying one the rule lacks, is skipped rather than guessed
    at."""
    if entry.condition is None:
        return not rule.conditions
    return rule.conditions == (entry.condition,)


def missing_operations(rule: PolicyRule) -> frozenset[str]:
    """The operation keys ``rule``'s verbs imply today that it does not carry. Empty for a complete
    rule and for a predicate no catalogue knows (a hand-written rule)."""
    verbs = frozenset(describe.rule_verbs(rule))
    connectors = {propose.connector_of_operation(operation) for operation in rule.operations}
    expected: set[str] = set()
    for entry in propose.PROPOSABLE_SCOPES:
        # A predicate can serve two connectors (label_name_allowlist: Gmail and Contacts); only the
        # rule's own connectors count, or a Gmail rule would be offered Contacts operations.
        if entry.predicate != rule.predicate or entry.connector not in connectors:
            continue
        if not _entry_matches_conditions(entry, rule):
            continue
        for verb in verbs & entry.verbs:
            expected |= propose.operations_for(entry, verb)
    for extra in catalogue.EXTRA_SCOPES.values():
        if rule.predicate not in catalogue.extra_predicates(extra) or extra.connector not in connectors:
            continue
        for verb in verbs & frozenset(extra.verbs):
            if catalogue.extra_predicate_for(extra, verb) != rule.predicate:
                continue
            expected |= catalogue.extra_operations_for(extra, verb)
    return frozenset(expected) - rule.operations


def missing_tools(rule: PolicyRule) -> tuple[str, ...]:
    """The tools ``missing_operations`` would unblock: those whose operation is missing and whose
    verb the rule has -- ``describe.covered_tools``' own test, applied to the missing keys."""
    missing = missing_operations(rule)
    if not missing:
        return ()
    verbs = frozenset(describe.rule_verbs(rule))
    return tuple(sorted(
        entry.tool
        for entry in TOOL_REGISTRY.values()
        if entry.operation in missing and entry.verb is not None and entry.verb in verbs
    ))


__all__ = ["missing_operations", "missing_tools"]
