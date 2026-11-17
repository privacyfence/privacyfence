"""RULE_EXEMPT_OPERATIONS (ADR 0322): the tag-writing operations no rule can cover.

The tools themselves do not exist yet, so these tests use the constant's values directly.
"""
from __future__ import annotations

import pytest

from privacyfence import auto_accept
from privacyfence.policy import catalogue, engine, propose, registry, suggest
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import ToolRegistryEntry, Verb

from ...helpers import make_ctx

EXEMPT = sorted(registry.RULE_EXEMPT_OPERATIONS)


def test_exempt_set_names_both_tag_tools():
    assert registry.RULE_EXEMPT_OPERATIONS == {"drive.set_file_tags", "drive.tag_folder_tree"}


@pytest.mark.parametrize("scope", propose.PROPOSABLE_SCOPES, ids=lambda s: s.predicate)
def test_no_proposable_scope_derives_an_exempt_operation(scope):
    for verb in Verb:
        assert not (propose.operations_for(scope, verb) & registry.RULE_EXEMPT_OPERATIONS)


@pytest.mark.parametrize("group", list(catalogue.EXTRA_SCOPES))
def test_no_extra_scope_derives_an_exempt_operation(group):
    extra = catalogue.EXTRA_SCOPES[group]
    for verb in Verb:
        assert not (catalogue.extra_operations_for(extra, verb) & registry.RULE_EXEMPT_OPERATIONS)


@pytest.mark.parametrize("operation", EXEMPT)
def test_engine_ignores_a_rule_naming_an_exempt_operation(operation):
    rule = PolicyRule(id="r1", predicate="gmail.anything", value=None, operations=frozenset({operation}))
    assert engine.find_matching_rule([rule], operation, make_ctx()) is None
    assert engine.evaluate([rule], operation, make_ctx()) == (False, "")
    verdict, rule_id, _reason = engine.preflight([rule], operation, {})
    assert (verdict, rule_id) == ("requires_review", "")


@pytest.mark.parametrize("operation", EXEMPT)
def test_suggested_rules_is_empty_for_an_exempt_operation(monkeypatch, operation):
    tool = f"test_{operation.replace('.', '_')}"
    entry = ToolRegistryEntry(tool=tool, gate="popup", operation=operation, verb=Verb.UPDATE, scope_subject=None)
    monkeypatch.setitem(registry.TOOL_REGISTRY, tool, entry)
    monkeypatch.setattr(suggest, "TOOL_REGISTRY", registry.TOOL_REGISTRY)
    assert suggest.suggested_rules(tool, {}) == []


@pytest.mark.parametrize("operation", EXEMPT)
def test_exempt_operations_are_excluded_from_temp_accept(operation):
    assert operation in auto_accept._TEMP_ACCEPT_EXCLUDED_OPERATIONS
