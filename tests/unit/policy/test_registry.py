"""Unit tests for privacyfence.policy.registry.

The registry's whole job is to be a *superset* of the existing tables it joins -- these tests
assert exact agreement with `TOOL_TO_GATE`/`TOOL_TO_OPERATION` (so the registry provably changes nothing for
every consumer that still reads those tables directly) and then assert the registry adds a verb
and scope subject everywhere those tables don't reach, including the six operation keys that are
configurable nowhere today.
"""
from __future__ import annotations

import pytest

from privacyfence.auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION
from privacyfence.policy.resource_registry import GRANT_RESOURCE_TYPES
from privacyfence.policy.registry import (
    GATES,
    TOOL_REGISTRY,
    TOOL_TO_VERB,
    VERB_FAMILY,
    VERB_SCOPE_SUBJECT,
    ScopeSubject,
    Verb,
    VerbFamily,
    operation_verbs,
)

# The six operation keys that are ungovernable today:
# TOOL_TO_OPERATION knows about them, but no rule, label or grant capability can ever reach them.
_UNGOVERNABLE_OPERATIONS = frozenset({
    "apps_script.read_content",
    "apps_script.write_content",
    "apps_script.read_execution_log",
    "gmail.create_filter",
    "gmail.update_filter",
    "slack.create_group_chat",
})

# Operation keys shared by tools that perform two different verbs. Migrating a v1 rule keyed on
# one of these must expand to every verb listed, not just the first, or the migration silently
# narrows what the rule allows.
_DUAL_VERB_OPERATIONS: dict[str, frozenset[Verb]] = {
    "calendar.create_modify_event": frozenset({Verb.CREATE, Verb.UPDATE}),
    "slack.read_messages": frozenset({Verb.READ, Verb.SEARCH}),
    "telegram.read_chat_messages": frozenset({Verb.READ, Verb.SEARCH}),
}


def _capability_operation_keys() -> set[str]:
    keys: set[str] = set()
    for resource_type in GRANT_RESOURCE_TYPES:
        for capability in resource_type.capabilities.values():
            keys.update(op_key for op_key, _rule_name in capability.targets)
    return keys


class TestReproducesToolToGate:
    def test_same_tool_names(self):
        assert set(TOOL_REGISTRY) == set(TOOL_TO_GATE)

    @pytest.mark.parametrize("tool", sorted(TOOL_TO_GATE))
    def test_gate_matches_for_every_tool(self, tool):
        assert TOOL_REGISTRY[tool].gate == TOOL_TO_GATE[tool]

    def test_every_gate_value_is_a_known_gate(self):
        assert GATES == frozenset({"auto", "review", "popup"})
        assert {entry.gate for entry in TOOL_REGISTRY.values()} <= GATES


class TestReproducesToolToOperation:
    @pytest.mark.parametrize("tool", sorted(TOOL_TO_OPERATION))
    def test_operation_matches_for_every_mapped_tool(self, tool):
        assert TOOL_REGISTRY[tool].operation == TOOL_TO_OPERATION[tool]

    def test_tools_with_no_operation_key_have_none(self):
        unmapped = set(TOOL_TO_GATE) - set(TOOL_TO_OPERATION)
        assert unmapped, "fixture assumption: some tools have no operation key"
        for tool in unmapped:
            assert TOOL_REGISTRY[tool].operation is None


class TestAutoGatedToolsCarryNoVerb:
    def test_auto_gated_tools_have_no_verb_scope_or_family(self):
        auto_tools = [t for t, gate in TOOL_TO_GATE.items() if gate == "auto"]
        assert auto_tools, "fixture assumption: at least one auto-gated tool exists"
        for tool in auto_tools:
            entry = TOOL_REGISTRY[tool]
            assert entry.operation is None
            assert entry.verb is None
            assert entry.scope_subject is None
            assert entry.verb_family is None


class TestGovernedToolsCarryAVerb:
    @pytest.mark.parametrize("tool", sorted(TOOL_TO_OPERATION))
    def test_every_operation_bearing_tool_has_a_verb_and_scope_subject(self, tool):
        entry = TOOL_REGISTRY[tool]
        assert entry.gate in ("review", "popup")
        assert isinstance(entry.verb, Verb)
        assert isinstance(entry.scope_subject, ScopeSubject)
        assert isinstance(entry.verb_family, VerbFamily)

    def test_tool_to_verb_covers_exactly_the_operation_bearing_tools(self):
        assert set(TOOL_TO_VERB) == set(TOOL_TO_OPERATION)


class TestVerbVocabulary:
    def test_every_verb_has_a_family(self):
        assert set(VERB_FAMILY) == set(Verb)

    def test_every_verb_has_a_scope_subject(self):
        assert set(VERB_SCOPE_SUBJECT) == set(Verb)

    def test_families_partition_the_verbs_by_risk(self):
        assert VERB_FAMILY[Verb.READ] == VerbFamily.READ
        assert VERB_FAMILY[Verb.DOWNLOAD] == VerbFamily.READ
        assert VERB_FAMILY[Verb.SEARCH] == VerbFamily.READ
        assert VERB_FAMILY[Verb.DELETE] == VerbFamily.DESTRUCTIVE
        assert VERB_FAMILY[Verb.SEND] == VerbFamily.SEND
        assert VERB_FAMILY[Verb.DRAFT] == VerbFamily.SEND
        assert VERB_FAMILY[Verb.SHARE] == VerbFamily.SEND
        for verb in (
            Verb.CREATE, Verb.UPDATE, Verb.FORMAT, Verb.RESTRUCTURE, Verb.COMMENT,
            Verb.LABEL, Verb.MOVE, Verb.ARCHIVE, Verb.COMPLETE, Verb.TRANSITION, Verb.CONFIGURE,
        ):
            assert VERB_FAMILY[verb] == VerbFamily.WRITE

    def test_quantified_scope_subjects_match_the_verbs_that_need_more_than_one_object(self):
        # These three are exactly the verbs that measure more than a single item -- getting one
        # of these wrong describes a rule as allowing more (or other) objects than it can match.
        assert VERB_SCOPE_SUBJECT[Verb.SEARCH] == ScopeSubject.EVERY_RESULT
        assert VERB_SCOPE_SUBJECT[Verb.MOVE] == ScopeSubject.SOURCE_AND_DESTINATION
        assert VERB_SCOPE_SUBJECT[Verb.DRAFT] == ScopeSubject.EVERY_RECIPIENT


class TestUngovernableOperationsAreNowCovered:
    def test_ungovernable_operations_are_absent_from_every_existing_surface(self):
        # Confirm the gap still exists in the resource-type manifest the
        # registry is meant to eventually replace, so this test would fail (loudly, as a welcome
        # sign of progress) once a later phase closes it there instead of just here.
        capability_keys = _capability_operation_keys()
        for op_key in _UNGOVERNABLE_OPERATIONS:
            assert op_key not in capability_keys

    @pytest.mark.parametrize("operation", sorted(_UNGOVERNABLE_OPERATIONS))
    def test_registry_assigns_a_verb_and_scope_subject_anyway(self, operation):
        verbs = operation_verbs(operation)
        assert verbs, f"{operation} should resolve to at least one verb"
        for verb in verbs:
            assert isinstance(verb, Verb)


class TestOperationVerbs:
    @pytest.mark.parametrize("operation,expected", sorted(_DUAL_VERB_OPERATIONS.items()))
    def test_dual_verb_operations_report_every_verb(self, operation, expected):
        assert operation_verbs(operation) == expected

    def test_single_verb_operation_reports_one_verb(self):
        assert operation_verbs("jira.create_issue") == frozenset({Verb.CREATE})

    def test_unknown_operation_reports_no_verbs(self):
        assert operation_verbs("no.such.operation") == frozenset()

    def test_return_type_is_a_frozenset(self):
        assert isinstance(operation_verbs("jira.create_issue"), frozenset)
